"""BGG rulebook downloader using the api.geekdo.com JSON API.

File listing:    GET https://api.geekdo.com/api/files?objecttype=thing&objectid={id}&...
Authentication:  POST https://boardgamegeek.com/login/api/v1  (sets SessionID cookie)
Download URL:    Playwright (non-headless) renders the filepage SPA, extracts the
                 signed /file/download_redirect/{token}/{filename} URL from the DOM.
File transfer:   curl_cffi streams from S3 (AWS presigned URL) via redirect chain.

Why non-headless Playwright?
  boardgamegeek.com is behind Cloudflare's managed challenge. Headless Chromium is
  detected and blocked (403). Non-headless passes. The browser is only needed to get
  the signed token — the actual file download goes directly to S3 via curl_cffi.
"""

import re
import time
from pathlib import Path

from curl_cffi import requests as cffi_requests

_FILES_API = "https://api.geekdo.com/api/files"
_LOGIN_URL = "https://boardgamegeek.com/login/api/v1"
_BGG_BASE = "https://boardgamegeek.com"
_BGG_RANKINGS_URL = "https://boardgamegeek.com/browse/boardgame"
ENGLISH_LANGUAGE_ID = 2184
_BROWSE_PAGE_DELAY = 2.0

# Matches the signed download redirect path in the rendered filepage DOM.
_REDIRECT_RE = re.compile(r'/file/download_redirect/[^\s"\'<>]+')


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def make_session() -> cffi_requests.Session:
    """Return a curl_cffi Session that mimics Chrome's TLS fingerprint."""
    s = cffi_requests.Session(impersonate="chrome124")
    s.headers.update({
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://boardgamegeek.com/",
    })
    return s


def login(session: cffi_requests.Session, username: str, password: str) -> None:
    """Log in to BGG, storing auth cookies (SessionID etc.) on the session."""
    resp = session.post(
        _LOGIN_URL,
        json={"credentials": {"username": username, "password": password}},
        headers={"Content-Type": "application/json"},
    )
    resp.raise_for_status()
    if "SessionID" not in session.cookies:
        raise ValueError(
            "Login succeeded but no SessionID cookie received. "
            "Check username/password."
        )


# ---------------------------------------------------------------------------
# Rankings
# ---------------------------------------------------------------------------

def get_top_rankings(
    session: cffi_requests.Session,
    limit: int = 100,
) -> list[dict]:
    """Scrape the BGG rankings browse page and return up to *limit* games.

    Requires a curl_cffi session (Chrome TLS fingerprint) to pass Cloudflare.
    No login required — the rankings page is public.
    Returns a list of dicts with 'id' and 'name' keys, in rank order.
    """
    results: list[dict] = []
    seen_ids: set[str] = set()
    pages_needed = (limit + 99) // 100  # BGG shows 100 games per page

    for page in range(1, pages_needed + 1):
        url = _BGG_RANKINGS_URL if page == 1 else f"{_BGG_RANKINGS_URL}/page/{page}"
        resp = session.get(url)
        resp.raise_for_status()

        # Game name links have class='primary'; image links do not.
        # Pattern: <a  href="/boardgame/174430/gloomhaven"  class='primary' >Gloomhaven</a>
        matches = re.findall(
            r'href="/boardgame/(\d+)/[^"]*"\s+class=\'primary\'\s*>([^<]+)</a>',
            resp.text,
        )
        for game_id, name in matches:
            if game_id not in seen_ids:
                seen_ids.add(game_id)
                results.append({"id": game_id, "name": name.strip()})
                if len(results) >= limit:
                    break

        if len(results) >= limit:
            break

        if page < pages_needed:
            time.sleep(_BROWSE_PAGE_DELAY)

    return results


# ---------------------------------------------------------------------------
# File listing
# ---------------------------------------------------------------------------

def get_files(
    session: cffi_requests.Session,
    bgg_id: int,
    language_id: int = ENGLISH_LANGUAGE_ID,
    delay: float = 0.5,
) -> list[dict]:
    """Fetch all file entries for a BGG game, paginating through all pages."""
    all_files: list[dict] = []
    page = 1
    while True:
        resp = session.get(_FILES_API, params={
            "objecttype": "thing",
            "objectid": bgg_id,
            "languageid": language_id,
            "sort": "hot",
            "pageid": page,
        })
        resp.raise_for_status()
        data = resp.json()
        all_files.extend(data.get("files", []))
        end_page = data.get("config", {}).get("endpage", 1)
        if page >= end_page:
            break
        page += 1
        time.sleep(delay)
    return all_files


def _rulebook_tier(f: dict) -> int:
    """Score a file entry by how likely it is to be the main rulebook.

    Higher tier = stronger preference. Within a tier, sort by community votes.
    Tier -1: explicitly undesirable (solo, variant, faq, reference card, …)
    Tier  0: generic PDF — no rulebook signal
    Tier  1: contains "rule" (singular) or whole-word "EN" but no avoidance terms
    Tier  2: contains "rules" (plural) but no avoidance terms
    Tier  3: contains "rulebook", "complete rules", or "core rules"
    """
    _AVOID = {"solo", "variant", "player aid", "reference card", "quick reference", "faq", "errata"}
    _TIER3 = {"rulebook", "complete rules", "core rules"}

    combined = (f["title"] + " " + f["filename"]).lower()
    combined_orig = f["title"] + " " + f["filename"]

    if any(term in combined for term in _AVOID):
        return -1
    if any(term in combined for term in _TIER3):
        return 3
    if "rules" in combined:
        return 2
    if "rule" in combined or re.search(r"\bEN\b", combined_orig):
        return 1
    return 0


def pick_rulebook(files: list[dict]) -> dict | None:
    """Return the best English rulebook PDF using tier-based scoring.

    Tiers (highest wins):
      3 — "rulebook", "complete rules", or "core rules" in title/filename
      2 — "rules" (plural) in title/filename, no avoidance terms
      1 — "rule" (singular) or whole-word "EN" in title/filename, no avoidance terms
      0 — any other PDF (no rulebook keywords)
     -1 — explicitly undesirable: solo, variant, player aid, reference card,
           quick reference, faq, or errata

    Within each tier, the file with the most community votes wins.
    Returns None if no PDFs are found.
    """
    pdfs = [f for f in files if f["filename"].lower().endswith(".pdf")]
    if not pdfs:
        return None
    return max(pdfs, key=lambda f: (_rulebook_tier(f), int(f["numpositive"] or 0)))


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def _get_signed_url(filepage_href: str, username: str, password: str) -> str:
    """Use a non-headless browser to render the filepage and extract the
    signed /file/download_redirect/{token}/... URL from the DOM.

    The browser is needed because:
    1. boardgamegeek.com uses Cloudflare managed challenge (blocks headless)
    2. The filepage is a React SPA — download URL is only in the rendered DOM
    3. The /api/file/downloadurls endpoint requires session state the browser holds
    """
    from playwright.sync_api import sync_playwright

    filepage_url = _BGG_BASE + filepage_href

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        try:
            context = browser.new_context()
            page = context.new_page()

            # Land on BGG home first so login cookies are scoped correctly
            page.goto(_BGG_BASE, wait_until="domcontentloaded", timeout=30_000)
            page.evaluate(
                """([u, p]) => fetch('/login/api/v1', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({credentials: {username: u, password: p}}),
                    credentials: 'include',
                })""",
                [username, password],
            )

            page.goto(filepage_url, wait_until="domcontentloaded", timeout=60_000)
            # Wait for React SPA to render the download link
            page.wait_for_timeout(8_000)

            html = page.content()
        finally:
            browser.close()

    matches = _REDIRECT_RE.findall(html)
    if not matches:
        raise ValueError(
            f"Could not find a /file/download_redirect/ URL in the rendered filepage "
            f"{filepage_url!r}. The page may not have loaded fully or the BGG login failed."
        )
    return _BGG_BASE + matches[0]


def download_file(
    session: cffi_requests.Session,
    file_entry: dict,
    dest: Path,
    username: str,
    password: str,
) -> Path:
    """Download the PDF for a file_entry to dest. Returns the written path.

    Flow:
      1. Non-headless browser renders the filepage → extracts signed redirect URL
      2. curl_cffi follows the redirect chain to S3 → streams the PDF
    """
    signed_url = _get_signed_url(file_entry["href"], username, password)

    resp = session.get(signed_url, allow_redirects=True, stream=True)
    resp.raise_for_status()

    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as fh:
        for chunk in resp.iter_content(chunk_size=65_536):
            fh.write(chunk)
    return dest
