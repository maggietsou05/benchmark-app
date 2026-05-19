"""Diagnose what fetch_url_text actually sees for a given URL.

Usage:
    venv\\Scripts\\python.exe diag_url.py <URL>

Prints HTTP status, content-type, raw HTML length, extracted-text length,
final URL after redirects, and a snippet of both raw HTML and extracted
text — enough to tell apart a bot block (403 / 429 / JS-shell with no
content) from a real page our stripper just didn't like.
"""

import sys

import httpx

from core import _TextExtractor


USER_AGENT = "Mozilla/5.0 (compatible; MonitorBenchmarkBot/1.0; internal-MMD)"


def diagnose(url: str) -> None:
    print(f"Fetching: {url}\n")

    try:
        r = httpx.get(
            url,
            follow_redirects=True,
            timeout=15.0,
            headers={"User-Agent": USER_AGENT},
        )
    except httpx.HTTPError as e:
        print(f"Network error: {e}")
        return

    print(f"HTTP {r.status_code} {r.reason_phrase}")
    print(f"Final URL : {r.url}")
    print(f"Content-Type: {r.headers.get('content-type')}")
    print(f"Raw HTML  : {len(r.text):,} chars")

    parser = _TextExtractor()
    parser.feed(r.text)
    extracted = "\n".join(parser.parts)
    print(f"Extracted : {len(extracted):,} chars of visible text")

    print("\n--- first 800 chars of raw HTML ---")
    print(r.text[:800])
    print("\n--- first 800 chars of extracted text ---")
    print(extracted[:800] if extracted else "(empty)")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: diag_url.py <URL>")
        sys.exit(1)
    diagnose(sys.argv[1])
