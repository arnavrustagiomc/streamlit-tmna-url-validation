# Dealer Service URL Validator

Latest update: multi-page Streamlit validator with a standard QA checker and an optional Gemini-powered AI validator, automatic API key rotation, browser-based fallback for JavaScript-heavy pages, and Excel export with validation results.

## Overview

This project validates `Dealer_Service_URL` values against a dealer `Dealer_URL` and flags risky or broken URLs using the QA matrix used for dealer/service scheduling pages.

It supports:
- A standard validation page for bulk .xlsx files
- An AI validation page for optional Gemini-based suggestions
- Tracking parameter detection (`utm_*`, `gclid`, etc.)
- Domain verification for known third-party scheduler providers
- Optional sitemap/homepage-based URL suggestions
- Browser-based fetching via Playwright for pages that need JavaScript or redirect handling
- Results export back to Excel with the original data plus validation columns

## Current app layout

When you run the app, it opens with a multi-page navigator:
- `Standard Validator` — default page for the core QA workflow
- `AI Validator` — optional advanced page that includes Gemini validation, API key input, and API rotation reporting

Run it with:

```bash
streamlit run app.py
```

## Requirements

- Python 3.10+ recommended
- Windows/macOS/Linux supported
- Internet access to dealer domains is required
- A browser runtime is required for Playwright-based checks

## Environment setup

Create and activate a virtual environment, then install dependencies.

Windows (PowerShell):

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install --upgrade pip
pip install -r requirements.txt
python -m playwright install chromium
```

Windows (cmd.exe):

```cmd
python -m venv .venv
.venv\Scripts\activate
pip install --upgrade pip
pip install -r requirements.txt
python -m playwright install chromium
```

macOS / Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
python -m playwright install chromium
```

If a project-local `env/` directory already exists, you can reuse it instead of creating a new virtual environment.

## How to use

1. Open the app in Streamlit.
2. Upload a dealer profile `.xlsx` file in the sidebar.
3. Choose the `Dealer_URL` and `Dealer_Service_URL` columns.
4. Adjust settings under the sidebar:
   - Tracking query-string params to flag
   - Third-party scheduler domains
   - Validation categories to run
   - Optional replacement-URL suggestion via sitemap/homepage crawl
   - Optional AI validation on the AI page
5. Set the row limit or leave it at `0` for the full file.
6. Click `Run validation`.
7. Review the results table and download the output `.xlsx` file.

## Standard validation settings

The standard validator supports these checks:
- Query String Tests
- Tracking Detection
- Domain Verification

The app also supports an optional bonus pass for replacement URL suggestions based on sitemap/homepage crawling.

## AI validation page

The AI page is for Gemini-based validation. It adds:
- `AI-based bonus validation via Gemini`
- Multiple Gemini API keys for rotation
- Automatic failover when a key is exhausted or rate-limited
- A debug payload toggle for inspecting the prompt/response
- Export of API rotation status in a separate worksheet

### AI requirements

The AI page requires a valid Gemini API key and the installed package from `requirements.txt`:

```bash
pip install -r requirements.txt
```

Add one or more keys, one per line, in the sidebar and select the model to use.

## Output columns

The exported workbook includes the original input data plus the validation columns below:
- `Error_Type`
- `Notes`
- `Suggested_New_URL`
- `Suggested_URL_Confidence`
- `AI_Suggested_URL`
- `AI_Suggested_URL_Confidence`
- `AI_Suggested_URL_Validation`
- `AI_Suggested_URL_Validation_Notes`
- `AI_Debug_Message`

If the AI page is used, the workbook also includes an `API_Rotation` sheet with per-key usage and exhaustion status.

## Validation categories and detection logic

| Error_Type | Detection approach |
|---|---|
| No URL | Empty or missing value |
| Same as Dealer_URL | Normalized homepage match |
| Homepage Scheduler Detection | Hash fragment or scheduler-style query param on homepage |
| 3rd Party Scheduler | Service domain matches a known third-party scheduler domain |
| Different Domain (Unverified) | Different registered domain from dealer site |
| Unreachable / Timeout | HTTP/network/browser fetch failure |
| Tracking Appends Entered | Tracking params survive appended query-string test |
| Error Page Entered | HTTP error or heuristic soft-404 text match |
| Redirect Drops Query String | Query params dropped after redirect |
| Query String Renders Error Page | Appended tracking params trigger an error page |
| Query String Test Failed (network error) | Appended test URL fails to fetch |
| Query String Dropped on Append | Test params do not persist after redirect |
| No Issue Detected | Validation checks pass |
| Suggested_New_URL | Best-effort sitemap/homepage-based suggestion |
| AI_Suggested_URL | Gemini analysis result |

## AI validation behavior and safeguards

The AI layer is intentionally conservative.

It only suggests URLs when the candidate is:
- Stable after a normal GET request
- Live and returning HTTP 200
- A likely service/scheduling page
- Not a generic landing page or redirecting canonical URL

If the AI is uncertain, it returns `N/A` rather than guessing.

Built-in constraints in the prompt include:
- Prefer direct service URLs such as `/schedule-service.htm`, `/service/schedule-service/`, or `/appointment/`
- Reject redirect-heavy, canonicalized, or tracking-breaking suggestions
- Ignore dead links and 404/error pages
- Treat AI output as a human-verification lead, not an automatic fix

## API key rotation logic

The AI validator supports multiple keys:
- Keys are processed sequentially and rotated automatically
- When a key hits quota or a rate-limit condition, it is marked exhausted
- The app falls back to the next available key
- A status table records usage and failure history

## Limitations

- AI suggestions are based on fetched page content and do not fully render JavaScript-heavy sites
- Single-page apps may hide service links in client-side navigation
- AI cannot guarantee downstream tracking behavior on the final destination
- The validation logic is best-effort and still requires a human to review unusual edge cases

## Troubleshooting

If the app fails to load pages correctly:
- Ensure Playwright Chromium is installed
- Confirm `playwright` is installed from `requirements.txt`
- Check that outbound access to dealer sites is allowed
- For AI mode, verify that the Gemini API key is valid and not quota-exhausted

## Notes

This tool is intended for internal QA checking of dealer service URLs and should be used alongside business validation rules where necessary.
