"""
Pure logic for Dealer Service URL validation — no Streamlit dependency,
so it can be unit tested / reused in a CLI or scheduled job.
"""

import threading
from dataclasses import dataclass
import re

import requests
from urllib.parse import urlparse, urlsplit, urlunsplit, parse_qsl, urlencode, urljoin
from xml.etree import ElementTree
from html.parser import HTMLParser

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

# Make Python's SSL verification use the OS-native trust store (same certs a
# real browser trusts) instead of the bundled `certifi` list. This alone fixes
# the very common "loads fine in a browser, SSLCertVerificationError in
# requests" case -- usually a server serving an incomplete chain (missing
# intermediate CA) that browsers silently patch via AIA-fetching and requests
# doesn't, or a corporate TLS-inspection proxy whose root CA is trusted by the
# OS but absent from certifi. Falls back silently to certifi if unavailable.
try:
    import truststore
    truststore.inject_into_ssl()
    TRUSTSTORE_AVAILABLE = True
except ImportError:
    TRUSTSTORE_AVAILABLE = False

try:
    import google.generativeai as genai
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False

DEFAULT_THIRD_PARTY_DOMAINS = [
    "tekioncloud.com",
    "connectcdk.com",
    "mykaarma.com",
    "reyrey.net",
    "updatepromise.com",
    "dealer-fx.com",
    "xtime.com",
    "myxtime.com",
    "vinsolutions.com",
    "dealersocket.com",
    "servicerecap.com",
    "carnow.com",
]

DEFAULT_TRACKING_PARAMS = [
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "fbclid", "msclkid", "siteid", "cid", "source",
]

ERROR_PAGE_TEXT_SIGNATURES = [
    "page not found", "404 error", "404 - ", "we can't find that page",
    "oops", "page you requested", "does not exist", "cannot be found",
    "sorry, this page", "content not found", "error 404",
]

SERVICE_KEYWORDS = ["service", "schedule", "appointment", "maintenance", "repair", "booking"]

USER_AGENT = "Mozilla/5.0 (compatible; DealerURLValidator/1.0; +internal-QA-tool)"
REQUEST_TIMEOUT = 12
TEST_QUERY_STRING = "siteid=test&utm_source=test"

# Signals that requests' raw HTML is a JS-challenge/redirect wall rather than
# real content -- i.e. this URL needs a real browser (Playwright) to resolve.
JS_WALL_SIGNATURES = [
    "checking your browser", "just a moment", "enable javascript",
    "please turn javascript on", "ddos-guard", "cf-browser-verification",
    "_branch_match_id", "window.location", "meta http-equiv=\"refresh\"",
    "meta http-equiv='refresh'",
]
BROWSER_FETCH_TIMEOUT_MS = 20000


# --------------------------------------------------------------------------------------
# URL helpers
# --------------------------------------------------------------------------------------

def normalize_for_compare(url: str) -> str:
    if not url:
        return ""
    parsed = urlsplit(url.strip())
    netloc = parsed.netloc.lower().replace("www.", "")
    path = parsed.path.rstrip("/").lower()
    return f"{netloc}{path}"


def has_service_hash_fragment(url: str) -> bool:
    """Check if URL has a hash fragment (e.g., /#/service or /#service-scheduler).
    Returns True if fragment exists and contains scheduler-related keywords.
    """
    parsed = urlsplit(url.strip())
    if not parsed.fragment:
        return False
    fragment_lower = parsed.fragment.lower()
    return any(kw in fragment_lower for kw in SERVICE_KEYWORDS)


def has_scheduler_query_params(url: str) -> bool:
    """Check if URL has query parameters indicating a scheduler/schedule feature.
    Examples: ?schedule=true, ?service=true, ?appointment=1, etc.
    """
    parsed = urlsplit(url.strip())
    if not parsed.query:
        return False
    qs = dict(parse_qsl(parsed.query))
    truthy_values = {"", "1", "true", "yes", "on"}
    scheduler_keys = {kw.lower() for kw in SERVICE_KEYWORDS}
    for key, value in qs.items():
        if key.lower() in scheduler_keys and value.lower() in truthy_values:
            return True
    return False


def get_registered_domain(url: str) -> str:
    try:
        netloc = urlparse(url).netloc.lower().split(":")[0]
        parts = netloc.split(".")
        return ".".join(parts[-2:]) if len(parts) >= 2 else netloc
    except Exception:
        return ""


def has_tracking_params(url: str, tracking_keys) -> list:
    try:
        qs = dict(parse_qsl(urlparse(url).query))
    except Exception:
        return []
    keys_lower = {k.lower() for k in tracking_keys}
    return [k for k in qs.keys() if k.lower() in keys_lower]


def append_query_string(url: str, extra_qs: str) -> str:
    parsed = urlsplit(url)
    combined = parse_qsl(parsed.query) + parse_qsl(extra_qs)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(combined), parsed.fragment))


def build_test_query_string(tracking_params):
    """Construct a test query string that sets each tracking param to 'test'.
    Falls back to TEST_QUERY_STRING if tracking_params is falsy.
    """
    if not tracking_params:
        return TEST_QUERY_STRING
    pairs = [(k, "test") for k in tracking_params]
    return urlencode(pairs)


@dataclass
class ApiKeyStatus:
    api_key: str
    used_count: int = 0
    exhausted: bool = False
    last_error: str = ""


class ApiRotationManager:
    def __init__(self, api_keys):
        self.lock = threading.Lock()
        self.keys = [ApiKeyStatus(api_key=k) for k in api_keys]
        self.current_index = 0

    def next_key(self):
        with self.lock:
            if not self.keys:
                return None

            current = self.keys[self.current_index]
            if not current.exhausted:
                return current

            n = len(self.keys)
            for offset in range(1, n):
                candidate_index = (self.current_index + offset) % n
                candidate = self.keys[candidate_index]
                if not candidate.exhausted:
                    self.current_index = candidate_index
                    return candidate
            return None

    def record_use(self, api_key, exhausted=False, error=""):
        with self.lock:
            for key_status in self.keys:
                if key_status.api_key == api_key:
                    key_status.used_count += 1
                    if exhausted:
                        key_status.exhausted = True
                    if error:
                        key_status.last_error = error
                    return

    def get_status_records(self):
        return [
            {
                "api_key": self._redact(key_status.api_key),
                "used_count": key_status.used_count,
                "exhausted": key_status.exhausted,
                "last_error": key_status.last_error,
            }
            for key_status in self.keys
        ]

    @staticmethod
    def _redact(api_key: str) -> str:
        if len(api_key) <= 8:
            return "*****"
        return f"{api_key[:4]}...{api_key[-4:]}"


def ai_suggest_url(service_url: str, ai_api_keys, ai_rotation_manager=None, gemini_model="gemini-3-flash-preview", _retry_count=0, _rejected_urls=None) -> tuple[str, str, str]:
    """Gemini-based AI bonus validation for service URL suggestions.
    Returns (AI_Suggested_URL, AI_Suggested_URL_Confidence, AI_Debug_Message).
    
    Args:
        service_url: The URL to evaluate
        ai_api_keys: List of Gemini API keys
        ai_rotation_manager: Optional ApiRotationManager for key rotation
        gemini_model: Gemini model name to use (default: gemini-3-flash-preview)
        _retry_count: Internal retry counter (0-2 max attempts)
        _rejected_urls: Internal list of URLs that were rejected for redirecting
    
    Returns:
        Tuple of (suggested_url, confidence_score, debug_message)
    """
    if _rejected_urls is None:
        _rejected_urls = []
    
    max_retries = 2
    if not GEMINI_AVAILABLE:
        return "", "", "Gemini library not available. Install google-generativeai."
    
    if not ai_api_keys:
        return "", "", "No API keys provided."
    
    key_status = None
    api_key = None
    
    if ai_rotation_manager is not None:
        key_status = ai_rotation_manager.next_key()
        if key_status is None:
            return "", "", "No available API key; all keys exhausted."
        api_key = key_status.api_key
    else:
        api_key = ai_api_keys[0]
    
    try:
        # Fetch the page content
        try:
            response = requests.get(
                service_url,
                timeout=REQUEST_TIMEOUT,
                headers={"User-Agent": USER_AGENT},
                allow_redirects=True,
            )
            response.raise_for_status()
            final_url = response.url
            page_content = response.text[:2000]
        except Exception as e:
            if key_status is not None:
                ai_rotation_manager.record_use(api_key, error=f"Failed to fetch URL: {str(e)}")
            return "", "", f"Error fetching page: {str(e)}"

        was_redirected = normalize_for_compare(final_url) != normalize_for_compare(service_url)
        redirect_note = ""
        if was_redirected:
            redirect_note = (
                f"\n\nNOTE: This URL redirects to {final_url} — the current URL may not be the optimal entry point. "
                "If the redirect is a service or scheduling page, prefer the final URL or a non-redirecting equivalent."
            )

        # Configure Gemini API
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(gemini_model)

        # Create prompt for Gemini
        prompt = f"""Analyze this service URL and its page content:

URL: {service_url}
Final URL after redirects: {final_url}

Page Content Preview:
{page_content}{redirect_note}

Based on the content, please respond in this exact format:
SUGGESTED_URL: [URL or "N/A"]
CONFIDENCE: [High/Medium/Low]
REASONING: [Brief explanation]

If this appears to be a valid service/scheduling page, suggest any improvements. If you cannot determine or no improvement is needed, set SUGGESTED_URL to "N/A".
IMPORTANT: Only suggest a URL that is stable and does not redirect after a normal GET. If the candidate URL redirects to another page, or canonicalizes to a different page that breaks tracking, do NOT suggest it; return "N/A" instead.
IMPORTANT: Only suggest a URL that returns HTTP 200 and is a live service/scheduling page. Do not suggest URLs that are 404s, dead links, or error pages. Return "N/A" if uncertain.
IMPORTANT: Prefer concrete, direct service URLs (e.g., /schedule-service.htm, /service/schedule-service/, /appointment/) over generic landing pages.
IMPORTANT: If the entered URL redirects to a different URL, strongly consider suggesting either the final redirected URL or an alternative that does NOT redirect."""

        # Call Gemini API
        response = model.generate_content(prompt)
        response_text = response.text
        
        # Parse the response
        suggested_url = ""
        confidence = ""
        
        lines = response_text.strip().split("\n")
        for line in lines:
            if line.startswith("SUGGESTED_URL:"):
                suggested_url = line.replace("SUGGESTED_URL:", "").strip()
                if suggested_url.lower() == "n/a":
                    suggested_url = ""
            elif line.startswith("CONFIDENCE:"):
                confidence = line.replace("CONFIDENCE:", "").strip()

        # Check if suggested URL redirects; if so, retry with feedback
        if suggested_url and _retry_count < max_retries:
            redirects, redirect_target = url_redirects_to_different_target(suggested_url)
            if redirects:
                _rejected_urls.append((suggested_url, redirect_target))
                rejected_list = "\n".join(
                    f"- {url} redirects to {target}" 
                    for url, target in _rejected_urls
                )
                retry_prompt = f"""The previous AI suggestion was rejected because it redirects (which breaks tracking). 

Previously rejected suggestions:
{rejected_list}

Please analyze this service URL again and suggest a DIFFERENT URL that:
1. Does NOT redirect to another page
2. Returns HTTP 200 and is a live service/scheduling page
3. Is NOT any of the previously suggested URLs

URL: {service_url}
Final URL after redirects: {final_url}

Page Content Preview:
{page_content}{redirect_note}

Based on the content, please respond in this exact format:
SUGGESTED_URL: [URL or "N/A"]
CONFIDENCE: [High/Medium/Low]
REASONING: [Brief explanation]

IMPORTANT: Only suggest a URL that is stable and does not redirect."""
                
                genai.configure(api_key=api_key)
                model = genai.GenerativeModel(gemini_model)
                retry_response = model.generate_content(retry_prompt)
                retry_response_text = retry_response.text
                
                retry_suggested_url = ""
                for line in retry_response_text.strip().split("\n"):
                    if line.startswith("SUGGESTED_URL:"):
                        retry_suggested_url = line.replace("SUGGESTED_URL:", "").strip()
                        if retry_suggested_url.lower() == "n/a":
                            retry_suggested_url = ""
                        break
                
                if retry_suggested_url:
                    return ai_suggest_url(
                        service_url,
                        [api_key],
                        ai_rotation_manager=ai_rotation_manager,
                        gemini_model=gemini_model,
                        _retry_count=_retry_count + 1,
                        _rejected_urls=_rejected_urls,
                    )

        # Record successful use
        if key_status is not None:
            ai_rotation_manager.record_use(api_key)
        
        redacted_key = ApiRotationManager._redact(api_key)
        debug_msg = (
            f"API Key: {redacted_key}\n"
            f"Model: {gemini_model}\n"
            f"Status: Success"
        )
        if _retry_count > 0:
            debug_msg += f" (Retry {_retry_count}/{max_retries}: previous suggestions were rejected for redirecting)"
        debug_msg += (
            f"\nPrompt Sent:\n{prompt}\n\n"
            f"Response: {response_text[:300]}"
        )
        
        return suggested_url, confidence, debug_msg
    
    except Exception as e:
        error_str = str(e)
        is_quota_error = "quota" in error_str.lower() or "rate_limit" in error_str.lower() or "429" in error_str
        
        if key_status is not None:
            ai_rotation_manager.record_use(api_key, exhausted=is_quota_error, error=error_str)
        
        if is_quota_error and ai_rotation_manager is not None:
            next_key_status = ai_rotation_manager.next_key()
            if next_key_status is not None and next_key_status.api_key != api_key:
                return ai_suggest_url(
                    service_url,
                    [next_key_status.api_key],
                    ai_rotation_manager=ai_rotation_manager,
                    gemini_model=gemini_model,
                )
        
        redacted_key = ApiRotationManager._redact(api_key) if api_key else "N/A"
        return "", "", f"Error: {error_str} (Key: {redacted_key}, Model: {gemini_model})"


def ai_confirm_service_url(candidate_url: str, ai_api_keys=None, ai_rotation_manager=None, gemini_model="gemini-3-flash-preview"):
    """Ask Gemini to confirm whether a given URL/page is a service/scheduling page.
    Returns (is_service: bool, confidence: str, debug_msg: str).
    """
    if not GEMINI_AVAILABLE:
        return False, "", "Gemini library not available."
    ai_api_keys = ai_api_keys or []

    key_status = None
    api_key = None
    if ai_rotation_manager is not None:
        key_status = ai_rotation_manager.next_key()
        if key_status is None:
            return False, "", "No available API key; all keys exhausted."
        api_key = key_status.api_key
    else:
        if not ai_api_keys:
            return False, "", "No API keys provided."
        api_key = ai_api_keys[0]

    try:
        # Fetch page content (small preview)
        try:
            resp = requests.get(candidate_url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT}, allow_redirects=True)
            resp.raise_for_status()
            page_preview = (resp.text or "")[:2000]
        except Exception as e:
            return False, "", f"Failed to fetch candidate URL for AI confirmation: {e}"

        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(gemini_model)

        prompt = f"""Determine whether the following URL and page content represent a service/scheduling page.

URL: {candidate_url}

Page Content Preview:
{page_preview}

Answer in this exact format:
IS_SERVICE: [Yes/No]
CONFIDENCE: [High/Medium/Low]
REASONING: [brief explanation]

Only return Yes if you are reasonably confident this is a service, schedule, or appointment page. Otherwise return No.
"""

        response = model.generate_content(prompt)
        text = response.text or ""

        is_service = False
        confidence = ""
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("IS_SERVICE:"):
                val = line.split(":", 1)[1].strip().lower()
                is_service = val.startswith("y")
            elif line.startswith("CONFIDENCE:"):
                confidence = line.split(":", 1)[1].strip()

        if key_status is not None:
            ai_rotation_manager.record_use(api_key)

        debug_msg = (
            f"API Key: {ApiRotationManager._redact(api_key)}\n"
            f"Model: {gemini_model}\n"
            f"Prompt Sent:\n{prompt}\n\n"
            f"Response: {text[:400]}"
        )

        return is_service, confidence, debug_msg
    except Exception as e:
        err = str(e)
        exhausted = "quota" in err.lower() or "rate_limit" in err.lower() or "429" in err
        if key_status is not None:
            ai_rotation_manager.record_use(api_key, exhausted=exhausted, error=err)
        if exhausted and ai_rotation_manager is not None:
            next_key_status = ai_rotation_manager.next_key()
            if next_key_status is not None and next_key_status.api_key != api_key:
                return ai_confirm_service_url(candidate_url, ai_api_keys, ai_rotation_manager=ai_rotation_manager, gemini_model=gemini_model)
        return False, "", f"Error during AI confirmation: {err}"


def looks_like_error_page(status_code: int, text: str) -> bool:
    if status_code >= 400:
        return True
    if not text:
        return False
    lowered = text.lower()
    return any(sig in lowered for sig in ERROR_PAGE_TEXT_SIGNATURES)


def get_error_type_detail(status_code: int, text: str) -> str:
    """Return a description of what kind of error was detected."""
    if status_code >= 400:
        # HTTP error status
        error_map = {
            400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
            405: "Method Not Allowed", 408: "Request Timeout", 410: "Gone",
            429: "Too Many Requests", 500: "Internal Server Error", 502: "Bad Gateway",
            503: "Service Unavailable", 504: "Gateway Timeout",
        }
        reason = error_map.get(status_code, "HTTP Error")
        return f"HTTP {status_code} ({reason})"
    # Soft 404: status 200 with error-page text
    if text:
        lowered = text.lower()
        for sig in ERROR_PAGE_TEXT_SIGNATURES:
            if sig in lowered:
                return f"Soft 404 (contains '{sig}')"
    return "Error page detected"


class _BrowserResponse:
    """Minimal stand-in for a requests.Response, populated from Playwright,
    so classify_row() can keep using resp.url / resp.status_code / resp.text
    unchanged regardless of which fetch path was used."""
    def __init__(self, url, status_code, text):
        self.url = url
        self.status_code = status_code if status_code is not None else 200
        self.text = text or ""


# One Playwright browser per worker thread, reused across rows (ThreadPoolExecutor
# in app.py reuses the same threads across submitted tasks, so this amortizes the
# ~1-2s browser launch cost instead of paying it on every single row like a naive
# per-call `async with async_playwright()` would).
_thread_local = threading.local()


def _get_thread_browser():
    if not PLAYWRIGHT_AVAILABLE:
        return None, "playwright package not installed (pip install playwright)"
    if not hasattr(_thread_local, "browser"):
        try:
            _thread_local.pw = sync_playwright().start()
            _thread_local.browser = _thread_local.pw.chromium.launch(headless=True)
        except Exception as e:
            _thread_local.browser = None
            _thread_local.launch_error = (
                f"chromium launch failed ({e}) -- run `playwright install chromium`"
            )
    if getattr(_thread_local, "browser", None) is None:
        return None, getattr(_thread_local, "launch_error", "browser unavailable")
    return _thread_local.browser, None


def _looks_like_js_wall(resp) -> bool:
    """Heuristic for 'this page needs a real browser to resolve' -- either the
    request errored out, came back oddly thin, or the body contains a known
    JS-challenge / client-side-redirect signature (same pattern test2.py works
    around by using Playwright instead of requests)."""
    if resp is None:
        return True
    if resp.status_code in (403, 429, 503):
        return True
    text = (resp.text or "").lower()
    if len(text.strip()) < 300:
        return True
    return any(sig in text for sig in JS_WALL_SIGNATURES)


def fetch_rendered(url: str, timeout_ms: int = BROWSER_FETCH_TIMEOUT_MS):
    """Fetch a URL with a real headless browser -- executes JS, follows
    client-side/branch-link redirects, and waits for things to settle, the
    same way test2.py's get_final_url() does. Returns (final_url, status,
    html, error)."""
    browser, launch_err = _get_thread_browser()
    if browser is None:
        return None, None, None, launch_err
    page = None
    try:
        page = browser.new_page(user_agent=USER_AGENT)
        response = page.goto(url, wait_until="load", timeout=timeout_ms)
        final_url = page.url
        # Give a post-load JS redirect (branch.io style) a moment to fire,
        # then re-read the URL -- mirrors the wait test2.py leaves commented out.
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
            final_url = page.url
        except Exception:
            pass
        status = response.status if response else None
        text = page.content()
        return final_url, status, text, None
    except Exception as e:
        return None, None, None, str(e)
    finally:
        if page is not None:
            page.close()


def safe_get(url: str, timeout=REQUEST_TIMEOUT, allow_browser_fallback=True):
    headers = {"User-Agent": USER_AGENT}
    resp = None
    err = None
    try:
        resp = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        if not _looks_like_js_wall(resp):
            return resp, None
    except requests.exceptions.RequestException as e:
        err = str(e)

    if not allow_browser_fallback:
        if resp is not None:
            return resp, None  # best effort with what requests got, even if it looked thin
        return None, err or "request failed"

    final_url, status, text, browser_err = fetch_rendered(url)
    if browser_err:
        if resp is not None:
            return resp, None  # requests result, even if suspicious, beats nothing
        return None, f"{err or 'request failed'} | browser fallback unavailable: {browser_err}"

    return _BrowserResponse(final_url, status, text), None


# --------------------------------------------------------------------------------------
# Core classification
# --------------------------------------------------------------------------------------

def classify_row(dealer_url, service_url, third_party_domains=None, tracking_params=None,
                  run_query_string_test=True, run_domain_checks=True, run_tracking_detection=True,
                  run_sitemap_suggestion=False, run_homepage_suggestion=False,
                  run_ai_validation=False, ai_api_keys=None, ai_rotation_manager=None,
                  gemini_model="gemini-3-flash-preview"):
    third_party_domains = third_party_domains or DEFAULT_THIRD_PARTY_DOMAINS
    tracking_params = tracking_params or DEFAULT_TRACKING_PARAMS
    ai_api_keys = ai_api_keys or []

    dealer_url = (dealer_url or "").strip()
    service_url = (service_url or "").strip()

    ai_suggested_url, ai_suggested_confidence, ai_debug_message = "", "", ""
    if run_ai_validation and service_url:
        ai_suggested_url, ai_suggested_confidence, ai_debug_message = ai_suggest_url(
            service_url, ai_api_keys, ai_rotation_manager=ai_rotation_manager, gemini_model=gemini_model
        )

    result = {
        "Error_Type": "", "Notes": "", "Suggested_New_URL": "",
        "Suggested_URL_Confidence": "", "AI_Suggested_URL": ai_suggested_url,
        "AI_Suggested_URL_Confidence": ai_suggested_confidence,
        "AI_Debug_Message": ai_debug_message,
        "AI_Suggested_URL_Validation": "",
        "AI_Suggested_URL_Validation_Notes": "",
    }

    if run_ai_validation and ai_suggested_url and ai_suggested_url.lower() not in ("nan", "none", "n/a"):
        normalized_ai_url = ai_suggested_url.strip()
        if normalize_for_compare(normalized_ai_url) != normalize_for_compare(service_url):
            ai_validation = validate_url_against_standard_checks(
                dealer_url,
                normalized_ai_url,
                third_party_domains=third_party_domains,
                tracking_params=tracking_params,
                run_query_string_test=run_query_string_test,
                run_domain_checks=run_domain_checks,
                run_tracking_detection=run_tracking_detection,
                run_sitemap_suggestion=False,
                run_homepage_suggestion=False,
            )
            result["AI_Suggested_URL_Validation"] = ai_validation.get("Error_Type", "")
            result["AI_Suggested_URL_Validation_Notes"] = ai_validation.get("Notes", "")

            if ai_validation.get("Error_Type", "") in {"Redirect Drops Query String", "Query String Dropped on Append"}:
                notes = ai_validation.get("Notes", "")
                if " -> " in notes:
                    parts = notes.split(" -> ")
                    if len(parts) >= 2:
                        redirect_target = parts[1].split(";")[0].strip()
                        if redirect_target and redirect_target != normalized_ai_url:
                            target_validation = validate_url_against_standard_checks(
                                dealer_url,
                                redirect_target,
                                third_party_domains=third_party_domains,
                                tracking_params=tracking_params,
                                run_query_string_test=run_query_string_test,
                                run_domain_checks=run_domain_checks,
                                run_tracking_detection=run_tracking_detection,
                                run_sitemap_suggestion=False,
                                run_homepage_suggestion=False,
                            )
                            if target_validation.get("Error_Type", "") == "No Issue Detected":
                                # Server-side validation passed for the redirect target.
                                # Ask the AI to *confirm* the redirect target is actually a service/scheduling page
                                ai_confirmed = False
                                ai_confidence = ""
                                ai_confirm_debug = ""
                                if ai_api_keys:
                                    try:
                                        ai_confirmed, ai_confidence, ai_confirm_debug = ai_confirm_service_url(
                                            redirect_target,
                                            ai_api_keys,
                                            ai_rotation_manager=ai_rotation_manager,
                                            gemini_model=gemini_model,
                                        )
                                    except Exception as _:
                                        ai_confirmed = False

                                if ai_confirmed and ai_confidence in ("High", "Medium"):
                                    result["AI_Suggested_URL"] = redirect_target
                                    result["AI_Suggested_URL_Validation"] = "No Issue Detected (AI confirmed)"
                                    result["AI_Suggested_URL_Validation_Notes"] = (
                                        f"Original suggestion {normalized_ai_url} redirects to {redirect_target}, "
                                        f"which validates cleanly and was confirmed by AI (confidence: {ai_confidence})."
                                    )
                                    if ai_confirm_debug:
                                        result["AI_Debug_Message"] = ai_confirm_debug
                                    return result
                                else:
                                    result["AI_Suggested_URL_Validation"] = "Redirecting Suggested URL"
                                    result["AI_Suggested_URL_Validation_Notes"] = (
                                        f"Original suggestion {normalized_ai_url} redirects to {redirect_target}, which validates server-side, "
                                        f"but the AI did not confirm it as a service page (AI confidence: {ai_confidence})."
                                    )
                                    if ai_confirm_debug:
                                        result["AI_Debug_Message"] = ai_confirm_debug
                                    return result
                
                result["AI_Suggested_URL_Validation"] = "Redirecting Suggested URL"
                result["AI_Suggested_URL_Validation_Notes"] = (
                    f"{normalized_ai_url} redirects or drops tracking params; a suggested service URL must not redirect because that can break query-string tracking."
                )

    if not service_url or service_url.lower() in ("nan", "none", "n/a"):
        result["Error_Type"] = "No URL"
        if (run_sitemap_suggestion or run_homepage_suggestion) and dealer_url:
            result.update(suggest_replacement_url(dealer_url, tracking_params, include_homepage_links=run_homepage_suggestion))
        return result

    if dealer_url and normalize_for_compare(service_url) == normalize_for_compare(dealer_url):
        # Check if service URL points to a specific module/section via hash fragment (e.g., /#/service-scheduler)  
        # or has scheduler-related query parameters (e.g., ?schedule=true)
        has_hash = has_service_hash_fragment(service_url) and not has_service_hash_fragment(dealer_url)
        has_scheduler_qs = has_scheduler_query_params(service_url) and not has_scheduler_query_params(dealer_url)
        
        if has_hash or has_scheduler_qs:
            result["Error_Type"] = "Homepage Scheduler Detection"
            if has_hash:
                result["Notes"] = "Scheduler is embedded in homepage via hash fragment (single-page app)"
            else:
                result["Notes"] = "Scheduler is accessed via query parameters on homepage"
        else:
            result["Error_Type"] = "Same as Dealer_URL (homepage)"
        if (run_sitemap_suggestion or run_homepage_suggestion):
            result.update(suggest_replacement_url(dealer_url, tracking_params, include_homepage_links=run_homepage_suggestion))
        return result

    service_domain = get_registered_domain(service_url)
    dealer_domain = get_registered_domain(dealer_url) if dealer_url else ""
    if run_domain_checks and service_domain and service_domain != dealer_domain:
        if any(tp in service_domain for tp in third_party_domains):
            result["Error_Type"] = "3rd Party Scheduler"
            result["Notes"] = f"Hosted on {service_domain}, not on dealer's own domain"
            return result
        # Different domain, but not on the known-scheduler list -- could be a
        # legitimate rebrand/alias site (as with Group 1 Toyota North Austin ->
        # toyotaofnorthaustin.com), so don't call it a confirmed 3rd-party
        # scheduler. Flag it separately for a human to eyeball rather than
        # either silently passing it or mislabeling it.
        result["Error_Type"] = "Different Domain (Unverified)"
        result["Notes"] = (
            f"Hosted on {service_domain}, not on dealer's own domain ({dealer_domain}), "
            f"but not on the known 3rd-party scheduler list -- verify manually"
        )
        return result

    tracking_hits = has_tracking_params(service_url, tracking_params) if run_tracking_detection else []

    resp, err = safe_get(service_url)
    if err:
        result["Error_Type"] = "Unreachable / Timeout"
        result["Notes"] = err[:200]
        return result

    final_url = resp.url
    status = resp.status_code
    text_sample = resp.text[:20000] if resp.text else ""

    if looks_like_error_page(status, text_sample):
        result["Error_Type"] = "Error Page Entered"
        error_detail = get_error_type_detail(status, text_sample)
        result["Notes"] = f"{error_detail} at {final_url}"
        if (run_sitemap_suggestion or run_homepage_suggestion) and dealer_url:
            result.update(suggest_replacement_url(dealer_url, tracking_params, include_homepage_links=run_homepage_suggestion))
        return result

    orig_qs = dict(parse_qsl(urlparse(service_url).query))
    final_qs = dict(parse_qsl(urlparse(final_url).query))
    # True whenever the entered URL itself gets redirected somewhere else on a
    # plain GET -- independent of whether it carried a query string. This is
    # the root-cause signal: a page that redirects will typically drop *any*
    # query string sent to it, whether the dealer put tracking params on the
    # entered URL or we append them ourselves below.
    base_was_redirected = normalize_for_compare(final_url) != normalize_for_compare(service_url)

    if base_was_redirected and orig_qs and not all(k in final_qs for k in orig_qs):
        result["Error_Type"] = "Redirect Drops Query String"
        result["Notes"] = f"{service_url} -> {final_url}"
        return result

    if run_tracking_detection and tracking_hits:
        result["Error_Type"] = "Tracking Appends Entered"
        result["Notes"] = f"Existing params: {', '.join(tracking_hits)}"
        return result

    if run_query_string_test:
        test_query = build_test_query_string(tracking_params)
        test_url = append_query_string(service_url, test_query)
        test_resp, test_err = safe_get(test_url)
        if test_err:
            result["Error_Type"] = "Query String Test Failed (network error)"
            result["Notes"] = test_err[:200]
            return result
        test_final_qs = dict(parse_qsl(urlparse(test_resp.url).query))
        test_text = test_resp.text[:20000] if test_resp.text else ""
        test_qs_keys = dict(parse_qsl(test_query))

        if looks_like_error_page(test_resp.status_code, test_text):
            result["Error_Type"] = "Query String Renders Error Page"
            error_detail = get_error_type_detail(test_resp.status_code, test_text)
            note = f"Base URL OK, but {test_url} -> {error_detail}"
            if base_was_redirected:
                note += f" (base URL itself redirects to {final_url}; the append does not follow that same redirect)"
            result["Notes"] = note
            return result

        if not all(k in test_final_qs for k in test_qs_keys):
            if base_was_redirected:
                # Same defect as the "Redirect Drops Query String" case above --
                # the entered URL just didn't happen to carry a query string of
                # its own, so we only surfaced it via the append test. Reclassify
                # under the same label so QA sees one consistent root cause.
                result["Error_Type"] = "Redirect Drops Query String"
                result["Notes"] = (
                    f"{service_url} -> {final_url} (redirect drops query strings; "
                    f"confirmed by appending test params: {test_url} -> {test_resp.url})"
                )
                return result
            result["Error_Type"] = "Query String Dropped on Append"
            result["Notes"] = f"{test_url} -> {test_resp.url}"
            return result

    result["Error_Type"] = "No Issue Detected"
    return result


# --------------------------------------------------------------------------------------
# Bonus round: best-effort replacement URL suggestion via sitemap crawl
# --------------------------------------------------------------------------------------

def get_sitemap_urls(dealer_url: str, max_urls=500):
    domain_root = f"{urlparse(dealer_url).scheme or 'https'}://{urlparse(dealer_url).netloc}"
    candidates = [f"{domain_root}/sitemap.xml", f"{domain_root}/sitemap_index.xml"]

    resp, err = safe_get(f"{domain_root}/robots.txt", timeout=8)
    if resp and resp.status_code == 200:
        for line in resp.text.splitlines():
            if line.lower().startswith("sitemap:"):
                candidates.append(line.split(":", 1)[1].strip())

    urls = []
    seen_sitemaps = set()
    to_process = list(dict.fromkeys(candidates))

    while to_process and len(urls) < max_urls:
        sm_url = to_process.pop(0)
        if sm_url in seen_sitemaps:
            continue
        seen_sitemaps.add(sm_url)
        resp, err = safe_get(sm_url, timeout=8)
        if not resp or resp.status_code != 200:
            continue
        try:
            root = ElementTree.fromstring(resp.content)
        except ElementTree.ParseError:
            continue
        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        for sm in root.findall(".//sm:sitemap/sm:loc", ns):
            if sm.text and sm.text not in seen_sitemaps:
                to_process.append(sm.text.strip())
        for loc in root.findall(".//sm:url/sm:loc", ns):
            if loc.text:
                urls.append(loc.text.strip())
            if len(urls) >= max_urls:
                break

    return urls


def get_homepage_links(dealer_url: str, max_links=200):
    """Fetch the dealer homepage and extract internal anchor hrefs (absolute).
    Returns a list of absolute URLs (deduped, limited to max_links).
    """
    resp, err = safe_get(dealer_url, timeout=8)
    if err or not resp or resp.status_code != 200:
        return []
    html = resp.text or ""

    class _LinkParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.links = []

        def handle_starttag(self, tag, attrs):
            if tag.lower() != "a":
                return
            for k, v in attrs:
                if k.lower() == "href" and v:
                    self.links.append(v)

    parser = _LinkParser()
    try:
        parser.feed(html)
    except Exception:
        pass

    dealer_domain = get_registered_domain(dealer_url)
    abs_links = []
    for href in parser.links:
        try:
            abs_url = urljoin(dealer_url, href)
        except Exception:
            continue
        if get_registered_domain(abs_url) == dealer_domain:
            abs_links.append(abs_url)

    out = []
    for u in abs_links:
        if u not in out:
            out.append(u)
        if len(out) >= max_links:
            break
    return out


def score_candidate(url: str) -> int:
    lowered = url.lower()
    score = 0
    for kw in SERVICE_KEYWORDS:
        if kw in lowered:
            score += 2 if kw in ("service", "schedule", "appointment") else 1
    if lowered.rstrip("/").endswith((".htm", ".html")) or lowered.count("/") <= 4:
        score += 1
    return score


def suggest_replacement_url(dealer_url: str, tracking_params, max_candidates_to_test=5, include_homepage_links=False):
    out = {"Suggested_New_URL": "", "Suggested_URL_Confidence": ""}
    try:
        urls = get_sitemap_urls(dealer_url)
    except Exception:
        urls = []

    if include_homepage_links:
        try:
            home_links = get_homepage_links(dealer_url)
            # prepend homepage links (de-duplicated below)
            urls = home_links + urls
        except Exception:
            pass

    if not urls:
        if include_homepage_links:
            out["Suggested_URL_Confidence"] = "None (no sitemap found; homepage links yielded no candidates)"
        else:
            out["Suggested_URL_Confidence"] = "None (no sitemap found)"
        return out

    # de-dup while preserving order
    seen = set()
    deduped = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            deduped.append(u)

    scored = sorted(
        ((score_candidate(u), u) for u in deduped if score_candidate(u) > 0),
        key=lambda x: -x[0],
    )

    if not scored:
        out["Suggested_URL_Confidence"] = "None (no service-like URLs in sitemap or homepage links)"
        return out

    for score, candidate in scored[:max_candidates_to_test]:
        resp, err = safe_get(candidate, timeout=8)
        if err or not resp:
            continue
        if looks_like_error_page(resp.status_code, resp.text[:20000] if resp.text else ""):
            continue
        test_query = build_test_query_string(tracking_params)
        test_url = append_query_string(candidate, test_query)
        test_resp, test_err = safe_get(test_url, timeout=8)
        qs_survives = False
        if test_resp and not test_err:
            final_qs = dict(parse_qsl(urlparse(test_resp.url).query))
            qs_survives = all(k in final_qs for k in dict(parse_qsl(test_query)))
        out["Suggested_New_URL"] = candidate
        out["Suggested_URL_Confidence"] = "High" if (score >= 3 and qs_survives) else "Medium"
        return out

    out["Suggested_URL_Confidence"] = "Low (candidates found but none loaded cleanly)"
    return out


def validate_url_against_standard_checks(dealer_url, service_url, third_party_domains=None, tracking_params=None,
                                         run_query_string_test=True, run_domain_checks=True,
                                         run_tracking_detection=True, run_sitemap_suggestion=False,
                                         run_homepage_suggestion=False):
    """Run the standard validation pipeline against a candidate URL without AI recursion."""
    redirects, redirect_target = url_redirects_to_different_target(service_url)
    if redirects:
        return {
            "Error_Type": "Redirecting Suggested URL",
            "Notes": f"{service_url} -> {redirect_target}; suggested URLs must not redirect because tracking query strings can be dropped or broken.",
            "Suggested_New_URL": "",
            "Suggested_URL_Confidence": "",
        }
    return classify_row(
        dealer_url,
        service_url,
        third_party_domains=third_party_domains,
        tracking_params=tracking_params,
        run_query_string_test=run_query_string_test,
        run_domain_checks=run_domain_checks,
        run_tracking_detection=run_tracking_detection,
        run_sitemap_suggestion=run_sitemap_suggestion,
        run_homepage_suggestion=run_homepage_suggestion,
        run_ai_validation=False,
        ai_api_keys=[],
        ai_rotation_manager=None,
    )


def url_redirects_to_different_target(url: str, timeout=REQUEST_TIMEOUT):
    """Return (True, final_url) if the URL resolves to a different target after redirects.
    We treat any non-trivial redirect as invalid for a suggested service URL because
    it can break tracking query-string preservation and create inconsistent QA results.
    """
    if not url:
        return False, ""
    try:
        response = requests.get(
            url,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT},
            allow_redirects=True,
        )
        final_url = response.url or url
        redirecting = normalize_for_compare(final_url) != normalize_for_compare(url)
        return redirecting, final_url
    except Exception:
        return False, ""


def _candidate_is_live_service_url(url: str) -> bool:
    """Return True only when a candidate is a same-site service page that loads cleanly."""
    if not url:
        return False
    redirects, redirect_target = url_redirects_to_different_target(url)
    if redirects:
        return False
    try:
        resp, err = safe_get(url, timeout=8)
        if err or not resp:
            return False
        if looks_like_error_page(resp.status_code, (resp.text or "")[:20000]):
            return False
        path = urlsplit(url).path.lower()
        return any(kw in path for kw in SERVICE_KEYWORDS)
    except Exception:
        return False


def find_non_redirecting_alternative(service_url: str, suggested_url: str, html_text: str = "") -> str:
    """Look for a different same-site service URL that is live and stable.
    Prefers concrete service pages such as /schedule-service.htm over generic /service/ URLs.
    """
    if not service_url:
        return ""

    base_url = service_url.strip()
    base_domain = get_registered_domain(base_url)
    candidates = []
    seen = set()

    # 1) Pull usable service links from the page HTML itself.
    for href in re.findall(r'''href=["']?([^"' >]+)''', html_text or "", flags=re.IGNORECASE):
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        try:
            absolute = urljoin(base_url, href)
        except Exception:
            continue
        if get_registered_domain(absolute) != base_domain:
            continue
        if absolute not in seen:
            seen.add(absolute)
            candidates.append(absolute)

    # 2) Add likely explicit service-page variants for dealer sites that use .htm file names.
    parsed = urlsplit(base_url)
    scheme_netloc = f"{parsed.scheme}://{parsed.netloc}"
    extra_variants = [
        f"{scheme_netloc}/schedule-service.htm",
        f"{scheme_netloc}/service/schedule-service.htm",
        f"{scheme_netloc}/service/schedule-service/",
        f"{scheme_netloc}/service/",
        f"{scheme_netloc}/service/index.htm",
        f"{scheme_netloc}/service/appointment/",
        f"{scheme_netloc}/appointment/",
        f"{scheme_netloc}/schedule-service/",
        f"{scheme_netloc}/service.htm",
    ]
    for cand in extra_variants:
        if cand not in seen:
            seen.add(cand)
            candidates.append(cand)

    if suggested_url:
        for cand in [suggested_url, urlsplit(suggested_url).path.rstrip("/") or suggested_url]:
            if cand and cand not in seen:
                seen.add(cand)
                candidates.append(cand)

    scored = []
    for candidate in candidates:
        if not candidate:
            continue
        candidate = candidate.strip()
        if normalize_for_compare(candidate) == normalize_for_compare(base_url):
            continue
        path = urlsplit(candidate).path.lower()
        if not any(kw in path for kw in SERVICE_KEYWORDS):
            continue
        if not _candidate_is_live_service_url(candidate):
            continue
        score = 0
        for kw in SERVICE_KEYWORDS:
            if kw in path:
                score += 3 if kw in ("service", "schedule", "appointment") else 1
        if ".htm" in path or path.endswith(".php"):
            score += 2
        if "/schedule-service" in path or "/service/" in path:
            score += 1
        scored.append((score, candidate))

    if not scored:
        return ""

    scored.sort(key=lambda x: (-x[0], x[1]))
    return scored[0][1]
