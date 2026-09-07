"""
Pure logic for Dealer Service URL validation — no Streamlit dependency,
so it can be unit tested / reused in a CLI or scheduled job.
"""

import threading

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
                  run_sitemap_suggestion=False, run_homepage_suggestion=False):
    third_party_domains = third_party_domains or DEFAULT_THIRD_PARTY_DOMAINS
    tracking_params = tracking_params or DEFAULT_TRACKING_PARAMS

    result = {"Error_Type": "", "Notes": "", "Suggested_New_URL": "", "Suggested_URL_Confidence": ""}

    dealer_url = (dealer_url or "").strip()
    service_url = (service_url or "").strip()

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
