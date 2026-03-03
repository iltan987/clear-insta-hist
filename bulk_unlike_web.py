"""
bulk_unlike_web.py
------------------
Bulk unlike liked Instagram posts using the SAME web endpoint the Instagram
UI uses — batches up to 50 posts per request, mimicking exactly what happens
when you select posts and click Unlike in the browser.

How it works
------------
The UI calls:
  POST /async/wbloks/fetch/?appid=com.instagram.privacy.activity_center.liked_unlike
with `items_for_action` = "{media_pk}_{author_user_pk},{media_pk}_{author_user_pk},..."

Phase 1 — RESOLVE (one-time, cached in resolved_cache.json)
  - media_pk   : computed LOCALLY from the URL shortcode (base62 decode, zero API calls)
  - author_pk  : always 0 — the wbloks endpoint does not validate it, so no
                 per-user API lookups are needed at all

Phase 2 — UNLIKE
  - Scrape fresh fb_dtsg + lsd CSRF tokens from instagram.com (no browser needed)
  - POST items in batches of BATCH_SIZE to the wbloks endpoint
  - Progress saved after every batch so the script is resumable

Usage
-----
  python bulk_unlike_web.py            # full run
  python bulk_unlike_web.py --resolve-only  # only run Phase 1 (useful to pre-cache overnight)
"""

import argparse
import json
import os
import re
import sys
import time
import random
import threading
from concurrent.futures import ThreadPoolExecutor, Future
from pathlib import Path
from urllib.parse import unquote
import requests
from dotenv import load_dotenv
try:
    from instagrapi import Client as _IgClient
except ImportError:
    _IgClient = None  # type: ignore[assignment,misc]

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
LIKED_POSTS_FILE = Path("liked_posts.json")
RESOLVED_CACHE   = Path("resolved_cache.json")   # {url: "media_pk_0" | "SKIP"}
USER_PK_CACHE    = Path("user_pk_cache.json")     # kept for backwards compatibility, no longer written
PROGRESS_FILE    = Path("wbloks_progress.json")   # set of completed "media_pk_author_pk" strings

# ---------------------------------------------------------------------------
# Tuning
# ---------------------------------------------------------------------------
BATCH_SIZE    = 99          # items per wbloks POST (API max is 99)
BATCH_DELAY   = (5, 12)     # seconds between batches
DAILY_LIMIT   = 5000        # effectively unlimited for bulk; set lower if paranoid
BATCH_WORKERS = 3           # concurrent wbloks POSTs in-flight at once
RESOLVE_DELAY = (1.0, 2.5)  # kept for reference; no longer used (resolve is now local)

WBLOKS_BASE_URL = (
    "https://www.instagram.com/async/wbloks/fetch/"
    "?appid=com.instagram.privacy.activity_center.liked_unlike&type=action"
)

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# Helpers — export parsing
# ---------------------------------------------------------------------------

def media_pk_from_shortcode(shortcode: str) -> int:
    """Convert an Instagram shortcode (e.g. 'DByrxaItajH') to a numeric media pk.
    This is pure base62 arithmetic — no network request needed."""
    TABLE = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    n = 0
    for char in shortcode:
        n = n * 64 + TABLE.index(char)
    return n


def shortcode_from_url(url: str) -> str | None:
    m = re.search(r"instagram\.com/(?:p|reel|tv)/([A-Za-z0-9_-]+)", url)
    return m.group(1) if m else None


def parse_export(filepath: Path) -> list[dict]:
    """Return list of {url, username} dicts from the Instagram export JSON."""
    print(f"[parse] Reading '{filepath}' …")
    with filepath.open(encoding="utf-8") as fh:
        data = json.load(fh)

    entries = []
    for item in data:
        url = username = None
        for lv in item.get("label_values", []):
            if lv.get("label") == "URL":
                url = lv.get("href") or lv.get("value", "")
                if not url.startswith("https://"):
                    url = None
            if lv.get("title") == "Owner":
                for sub in lv.get("dict", []):
                    for kv in sub.get("dict", []):
                        if kv.get("label") == "Username":
                            username = kv.get("value", "").strip() or None
        if url:
            entries.append({"url": url, "username": username})

    print(f"[parse] {len(entries):,} posts  |  "
          f"{len({e['username'] for e in entries if e['username']})}"
          f" unique authors")
    return entries

# ---------------------------------------------------------------------------
# Phase 1 — Resolution
# ---------------------------------------------------------------------------

def load_json_file(path: Path, default):
    if path.exists():
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)
    return default


def save_json_file(path: Path, obj) -> None:
    with path.open("w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)


def resolve_phase(entries: list[dict]) -> dict[str, str]:
    """
    Build/update resolved_cache.json: {url -> "media_pk_0" | "SKIP"}.

    media_pk is computed locally from the URL shortcode (zero API calls).
    author_pk is always 0 — the wbloks endpoint accepts it and does not validate it,
    so the expensive per-username user_info lookup is unnecessary.
    """
    resolved: dict[str, str] = load_json_file(RESOLVED_CACHE, {})

    unresolved = [e for e in entries if e["url"] not in resolved]
    if not unresolved:
        print(f"[resolve] All {len(entries):,} URLs already in cache — nothing to do.")
        return resolved

    print(f"[resolve] Resolving {len(unresolved):,} URLs locally (no API calls) …")
    skipped = 0
    for e in unresolved:
        shortcode = shortcode_from_url(e["url"])
        if not shortcode:
            print(f"  [warn] Cannot extract shortcode from: {e['url']}")
            resolved[e["url"]] = "SKIP"
            skipped += 1
            continue
        media_pk = media_pk_from_shortcode(shortcode)
        resolved[e["url"]] = f"{media_pk}_0"

    save_json_file(RESOLVED_CACHE, resolved)
    print(f"[resolve] Done. {len(unresolved) - skipped:,} resolved, {skipped:,} skipped.")
    return resolved

# ---------------------------------------------------------------------------
# Phase 2 — Unlike via wbloks
# ---------------------------------------------------------------------------
# Static Comet/wbloks fields (from real browser capture; refresh if requests
# start failing with error 1357054 again by grabbing a new curl from DevTools).
_COMET_DYN  = ("7xeUjG1mxu1syUbFp41twpUnwgU7SbzEdF8aUco2qwJxS0DU2wx609vCwjE1EE2C"
               "w8G11wBz81s8hwGxu786a3a1YwBgao6C0Mo2swlo5qfK0EUjwGzEaE2iwNwmE7G4-"
               "5o4q3y1Sw62wLyESE7i3vwDwHg2ZwrUdUbGwmk0zU8oC1Iwqo5p0OwUQp6x6U42Un"
               "AwCAxW1oxe6U5q0EoKmUhw5nyEcE4y16wAwj8")
_COMET_CSR  = ("gLMrMH92YiMx6sQDlvr9_BiNB9EXhBnSnLCXoGhfpEgBJ1ncAmdGiDWybxaV8lh8"
               "KFUx4Qbih8Ne5UK6VoGKqr8K4Q9yQEgK4pXKm4QaqGimAmi7rxLgpxjDByt4HByoJ"
               "7Bxmaz8GfDKQcK5p8KnHmfCjwxxC5XwkV80QO0CU21waK0kW0jDwkoC4U05oG00hI"
               "20GV81si1u0ja1iXHEE9o0yV0cudAK04Uo0pECo1ZQ48vg2L8n8QbB82a682yUmyEv"
               "g1V61d81SDl097Cg2Dw5DwiYg1nwtlw8WbDg9859zU5UM1QE0VmbK7oco6c8FpxE06"
               "fO01dyw2iE0Wy")
_COMET_HSDP = ("gP31092142ARVxApdT2iaBsAIp4Q98OluzPBIMJmwzTJDFi2b513-684kg267VoCy"
               "0k4S29hwgwqA15gd9o5a4sxF8movzoW2y1iwaC3u0jG0mu0qq04IE0jUw8-3613wRw"
               "6fU1so982tw13C8wgo4y0iC0xU")
_COMET_HBLP = ("1O0MUO1px-awSyrx-0BUtwxCxG0yp9Uy3B7AwlE4a26Eap8LwAzbgSi9wEy84C2O"
               "E7x2u2K1pwhXwmohxGdz8F0xm1Oxy0Do1kE5e0cSw5Xwfm0BE5a0ZUhw58Ewowaq3"
               "G1kwNwYCy8lxq1TwRw2lo982twiE1p82xw8q3q0H85y2e8wNzUmwNwg8cU2ixCp0Pw"
               "kawl8")
_COMET_SJSP = ("gP31092142ARVxApdT2iaBsAIYDfgAz9lWczBIMJmxOZXpWkwyUy4Hxy1Kx-mc81g"
               "jo8B60uU4p0QwtF84qewuU3xw")
_COMET_S    = "46u7rd:g4tebr:6m9qi6"   # session-state triplet (per-tab in browser)
_COMET_BKV  = "29d0fd2d0bf67787771d758433b17814a729d9b4a57b07a39f1cc6507b480e39"  # JS bundle hash

# Relay container IDs emitted by the wbloks layout.  The server validates that
# they are plausible integers; the exact values don't matter.
_CONTAINER_ID = 1038143070
_ELEMENT_ID   = 1038143071
_SPINNER_ID   = 1038143072

BROWSER_UA_EDGE = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36 Edg/145.0.0.0"
)


def _make_session(session_id: str) -> requests.Session:
    """Return a requests.Session with Instagram cookies in a raw Cookie header.

    Setting the cookie via s.cookies would URL-encode the ':' characters in the
    session ID, causing Instagram to return a stripped-down HTML page without the
    fb_dtsg token.  Using a header string avoids that encoding.
    """
    s = requests.Session()
    s.headers.update({
        "User-Agent": BROWSER_UA_EDGE,
        "Accept-Language": "en-US,en;q=0.9",
        "Cookie": f"sessionid={session_id}",
    })
    return s


def _scrape_tokens(html: str, s: requests.Session) -> dict:
    """Extract session-specific Comet tokens from the likes page HTML."""
    def find(patterns, default=""):
        for pat in patterns:
            m = re.search(pat, html)
            if m:
                return m.group(1)
        return default

    fb_dtsg = find([
        r'"fb_dtsg"\s*,\s*\[\]\s*,\s*\{"token"\s*:\s*"([^"]+)"',
        r'"token"\s*:\s*"(NAfs[^"]+)"',
    ])
    if not fb_dtsg:
        sys.exit("[ERROR] Could not extract fb_dtsg — is your sessionid valid?")

    lsd = find([
        r'"LSD"\s*,\s*\[\]\s*,\s*\{"token"\s*:\s*"([^"]+)"',
        r'name="lsd"\s+value="([^"]+)"',
        r'"lsd"\s*:\s*"([^"]+)"',
    ])
    rev  = find([r'"client_revision"\s*:\s*(\d+)', r'"__rev"\s*:\s*(\d+)',
                 r'"server_revision"\s*:\s*(\d+)'], default="0")
    hsi  = find([r'"hsi"\s*:\s*"(\d+)"', r'"__hsi"\s*:\s*"(\d+)"'])
    hs   = find([r'"__hs"\s*:\s*"([^"]+)"', r'"haste_session"\s*:\s*"([^"]+)"'])
    bkv  = find([r'"__bkv"\s*:\s*"([a-f0-9]+)"', r'__bkv=([a-f0-9]+)'])

    csrftoken = s.cookies.get("csrftoken", "")
    if not csrftoken:
        csrftoken = find([r'"csrf_token"\s*:\s*"([^"]+)"'])
    if csrftoken:
        current = str(s.headers.get("Cookie", ""))
        if "csrftoken" not in current:
            s.headers["Cookie"] = current + f"; csrftoken={csrftoken}"

    spin_t  = str(int(time.time()))
    jazoest = "2" + str(sum(ord(c) for c in rev))

    return {
        "fb_dtsg": fb_dtsg, "lsd": lsd, "csrftoken": csrftoken,
        "__rev": rev, "__spin_r": rev, "__spin_b": "trunk", "__spin_t": spin_t,
        "__hsi": hsi, "__hs": hs, "jazoest": jazoest, "__bkv": bkv,
        "__crn": "comet.igweb.PolarisYourActivityInteractionsRoute",
    }


_req_n = [0]


def _next_req() -> str:
    _req_n[0] += 1
    n, s = _req_n[0], ""
    while n:
        s = chr(ord("a") + (n - 1) % 26) + s
        n = (n - 1) // 26
    return s


def _post_headers(tokens: dict) -> dict:
    return {
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "Referer": "https://www.instagram.com/your_activity/interactions/likes/",
        "X-Requested-With": "XMLHttpRequest",
        "X-CSRFToken": tokens.get("csrftoken", ""),
        "Origin": "https://www.instagram.com",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "User-Agent": BROWSER_UA_EDGE,
        "sec-ch-ua": '"Not:A-Brand";v="99", "Microsoft Edge";v="145", "Chromium";v="145"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "priority": "u=1, i",
    }


def _build_body(tokens: dict, params_obj: dict) -> dict:
    body: dict = {
        "__d": "www", "__user": "0", "__a": "1",
        "__req": _next_req(), "__comet_req": "7",
        "dpr": "1", "__ccg": "GOOD",
        "__rev": tokens["__rev"], "__spin_r": tokens["__spin_r"],
        "__spin_b": tokens["__spin_b"], "__spin_t": tokens["__spin_t"],
        "__crn": tokens["__crn"],
        "__hs": tokens.get("__hs") or "20515.HYP:instagram_web_pkg.2.1...0",
        "__hsi": tokens.get("__hsi", ""),
        "__s": _COMET_S, "__dyn": _COMET_DYN, "__csr": _COMET_CSR,
        "__hsdp": _COMET_HSDP, "__hblp": _COMET_HBLP, "__sjsp": _COMET_SJSP,
        "fb_dtsg": tokens["fb_dtsg"], "jazoest": tokens["jazoest"], "lsd": tokens["lsd"],
        "params": json.dumps(params_obj, separators=(",", ":")),
    }
    return body


def fetch_tokens(session_id: str) -> tuple:
    """Load the likes page, scrape session-specific tokens.
    Returns (tokens_dict, requests.Session).
    """
    session_id = unquote(session_id)  # handle %3A-encoded values from .env
    print("[tokens] Loading instagram.com/your_activity/interactions/likes/ …")
    s    = _make_session(session_id)
    resp = s.get("https://www.instagram.com/your_activity/interactions/likes/", timeout=20)
    resp.raise_for_status()
    if "accounts/login" in resp.url or "login" in resp.url:
        sys.exit("[ERROR] Instagram redirected to login — your sessionid has expired. "
                 "Get a fresh one from browser DevTools (Application → Cookies → sessionid).")
    tokens = _scrape_tokens(resp.text, s)
    c = tokens.get("csrftoken", "")
    print(f"  csrftoken={c[:10]}…" if c else "  [warn] csrftoken not found")
    print(f"[tokens] fb_dtsg={tokens['fb_dtsg'][:14]}…  lsd={tokens['lsd']}  "
          f"__rev={tokens['__rev']}  __bkv={tokens['__bkv'] or '(none)'}  "
          f"__hs={tokens['__hs'][:20]}…")
    return tokens, s


def post_batch(session_id: str, tokens: dict, item_keys: list,
               ig_session: requests.Session | None = None) -> bool | None:
    """POST a single batch to the wbloks unlike endpoint.

    Returns:
      True  — confirmed success
      None  — uncertain (timed out; action may or may not have processed)
      False — confirmed failure
    """
    params_obj = {
        "content_container_id": _CONTAINER_ID,
        "content_element_id":   _ELEMENT_ID,
        "content_spinner_id":   _SPINNER_ID,
        "main_order_state_value": True,
        "main_attribute_order_state_value": "newest_to_oldest",
        "main_date_start_state_value": -1, "main_date_end_state_value": -1,
        "main_authors_state_value": "",
        "main_filter_to_visible_on_facebook_value": False,
        "main_includes_location_value": False,
        "main_liked_privately_value": False,
        "main_content_type_value": 0,
        "main_content_types_value": "Posts, Reels",
        "main_account_history_events_state_value": "",
        "entrypoint": "", "shared_user_id": "",
        "main_filter_to_visible_from_facebook_value": False,
        "items_for_action": ",".join(item_keys),
        "number_of_items": len(item_keys),
    }
    body = _build_body(tokens, params_obj)
    bkv  = tokens.get("__bkv") or _COMET_BKV
    url  = WBLOKS_BASE_URL + f"&__bkv={bkv}"

    sess = ig_session
    if sess is None:
        sess = _make_session(session_id)
        c = tokens.get("csrftoken", "")
        if c:
            sess.headers["Cookie"] = str(sess.headers.get("Cookie", "")) + f"; csrftoken={c}"

    for _attempt in range(3):
        try:
            resp = sess.post(url, data=body, headers=_post_headers(tokens), timeout=30)
            break
        except requests.exceptions.Timeout:
            if _attempt == 2:
                print("  [warn] Timed out 3 times — action uncertain, will retry next run")
                return None  # uncertain: do NOT save to progress
            _wait = 15 * (_attempt + 1)
            print(f"  [warn] Timeout on attempt {_attempt + 1}/3, retrying in {_wait}s …")
            time.sleep(_wait)
    else:
        return None  # all retries exhausted, uncertain
    text = resp.text
    print(f"  HTTP {resp.status_code} | snippet: {text[:300]}")
    raw = text.lstrip("for (;;);")
    try:
        data = json.loads(raw)
        if "error" in data:
            print(f"  [error] {data['error']}: {data.get('errorSummary','')}")
            return False
        # Look for confirmation toast
        payload_str = json.dumps(data.get("payload", {}))
        if "unliked" in payload_str.lower() or "you unliked" in payload_str.lower():
            import re as _re
            m = _re.search(r'You unliked [^"\\]+', payload_str)
            if m:
                print(f"  [ok] Confirmed: {m.group(0)}")
        return True
    except Exception:
        # 500 can occur when Instagram's backend processed the action but failed
        # to serialize the response — the same behaviour seen in the web UI.
        if resp.status_code == 500:
            print("  [warn] HTTP 500 — action likely processed (Instagram backend glitch)")
            return True
        return False


def unlike_phase(entries: list[dict], resolved: dict[str, str]) -> None:
    load_dotenv()
    session_id = unquote(os.getenv("INSTAGRAM_SESSION_ID", "").strip())
    if not session_id:
        sys.exit("[ERROR] INSTAGRAM_SESSION_ID not set in .env")

    done: set[str] = set(load_json_file(PROGRESS_FILE, []))

    # Build ordered pending list (preserve export order)
    pending = []
    for e in entries:
        key = resolved.get(e["url"])
        if key and key != "SKIP" and key not in done:
            pending.append(key)

    total = len(pending)
    print(f"\n[unlike] {total:,} items pending  |  batch size: {BATCH_SIZE}  |  "
          f"~{total // BATCH_SIZE + 1} batches  |  workers: {BATCH_WORKERS}")

    if not pending:
        print("[unlike] Nothing to do — all posts already unliked.")
        return

    tokens, ig_session = fetch_tokens(session_id)
    done_lock = threading.Lock()
    unliked = 0
    batches = [pending[i:i + BATCH_SIZE] for i in range(0, len(pending), BATCH_SIZE)]
    total_batches = len(batches)

    def _dispatch(batch: list[str], bnum: int) -> bool | None:
        ok = post_batch(session_id, tokens, batch, ig_session=ig_session)
        if ok is True:
            print(f"  [batch {bnum}/{total_batches}] ✓ {len(batch)} unliked in background")
        elif ok is None:
            print(f"  [batch {bnum}/{total_batches}] ? timed out — will retry next run (not saved to progress)")
        else:
            print(f"  [batch {bnum}/{total_batches}] ✗ failed (logged, continuing)")
        return ok

    with ThreadPoolExecutor(max_workers=BATCH_WORKERS) as pool:
        pending_futures: list[tuple[int, list[str], Future]] = []

        for batch_num, batch in enumerate(batches, 1):
            if unliked >= DAILY_LIMIT:
                print(f"[unlike] Daily limit of {DAILY_LIMIT:,} reached.")
                break

            # Drain finished futures and persist progress
            still_pending = []
            for bnum, keys, fut in pending_futures:
                if fut.done():
                    if fut.result() is True:  # only save confirmed batches
                        with done_lock:
                            for k in keys:
                                done.add(k)
                            unliked += len(keys)
                        save_json_file(PROGRESS_FILE, sorted(done))
                        print(f"  [progress] total unliked this run: {unliked:,}  |  "
                              f"remaining: {total - unliked:,}")
                else:
                    still_pending.append((bnum, keys, fut))
            pending_futures = still_pending

            # Back-pressure: wait if all workers are busy
            while len(pending_futures) >= BATCH_WORKERS:
                time.sleep(1)
                still_pending = []
                for bnum, keys, fut in pending_futures:
                    if fut.done():
                        if fut.result() is True:  # only save confirmed batches
                            with done_lock:
                                for k in keys:
                                    done.add(k)
                                unliked += len(keys)
                            save_json_file(PROGRESS_FILE, sorted(done))
                            print(f"  [progress] total unliked this run: {unliked:,}  |  "
                                  f"remaining: {total - unliked:,}")
                    else:
                        still_pending.append((bnum, keys, fut))
                pending_futures = still_pending

            print(f"\n[unlike] Dispatching batch {batch_num}/{total_batches} "
                  f"— {len(batch)} items …")
            fut = pool.submit(_dispatch, batch, batch_num)
            pending_futures.append((batch_num, batch, fut))

            if batch_num < total_batches and unliked < DAILY_LIMIT:
                delay = random.uniform(*BATCH_DELAY)
                print(f"  sleeping {delay:.1f}s before next dispatch …")
                time.sleep(delay)

        # Wait for all remaining in-flight batches
        for bnum, keys, fut in pending_futures:
            if fut.result() is True:
                with done_lock:
                    for k in keys:
                        done.add(k)
                    unliked += len(keys)
                save_json_file(PROGRESS_FILE, sorted(done))

    print(f"\n[done] Unliked {unliked:,} posts this run.")

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def live_unlike_loop(session_id: str) -> None:
    """Fetch liked posts one batch at a time and unlike in background threads.

    Dispatches each wbloks POST to a thread pool so fetching and unliking
    happen concurrently. Up to BATCH_WORKERS batches can be in-flight at once.
    Keys are marked done optimistically; any failed batch is logged but does
    not stop the loop.
    """
    if _IgClient is None:
        sys.exit("[ERROR] instagrapi is not installed. Run: pip install instagrapi")

    print("[live] Logging in via instagrapi …")
    cl = _IgClient()
    cl.login_by_sessionid(session_id)

    tokens, ig_session = fetch_tokens(session_id)
    done_lock = threading.Lock()
    done: set[str] = set(load_json_file(PROGRESS_FILE, []))
    total_unliked = 0
    round_num = 0

    def _dispatch(keys: list[str], rnum: int) -> bool | None:
        """Runs in a worker thread — POST the batch, log result."""
        ok = post_batch(session_id, tokens, keys, ig_session=ig_session)
        if ok is True:
            print(f"  [round {rnum}] ✓ {len(keys)} unliked in background")
        elif ok is None:
            print(f"  [round {rnum}] ? timed out — will retry next run (not saved to progress)")
        else:
            print(f"  [round {rnum}] ✗ batch failed (logged, continuing)")
        return ok

    with ThreadPoolExecutor(max_workers=BATCH_WORKERS) as pool:
        pending_futures: list[tuple[int, list[str], Future]] = []

        while total_unliked < DAILY_LIMIT:
            # Drain finished futures and persist progress
            still_pending = []
            for rnum, keys, fut in pending_futures:
                if fut.done():
                    result = fut.result()
                    if result is True:  # confirmed — save to progress
                        with done_lock:
                            for k in keys:
                                done.add(k)
                            total_unliked += len(keys)
                            save_json_file(PROGRESS_FILE, sorted(done))
                        print(f"  [progress] total unliked this run: {total_unliked:,}")
                    else:  # None (timeout) or False (failed) — un-mark so next fetch retries
                        with done_lock:
                            for k in keys:
                                done.discard(k)
                        tag = "timed out" if result is None else "failed"
                        print(f"  [round {rnum}] {tag} — keys un-marked, will retry on next fetch")
                else:
                    still_pending.append((rnum, keys, fut))
            pending_futures = still_pending

            # Back-pressure: don't get too far ahead of the workers
            if len(pending_futures) >= BATCH_WORKERS:
                time.sleep(1)
                continue

            round_num += 1
            print(f"\n[live] Round {round_num} — fetching up to {BATCH_SIZE} liked posts …")
            medias = cl.liked_medias(amount=BATCH_SIZE)

            if not medias:
                print("[live] No more liked posts — waiting for in-flight batches …")
                break

            with done_lock:
                item_keys = [f"{m.pk}_0" for m in medias if f"{m.pk}_0" not in done]
                # Optimistically mark as done so next fetch doesn't re-queue them
                for k in item_keys:
                    done.add(k)

            if not item_keys:
                print("[live] All fetched posts already queued/done — stopping.")
                break

            print(f"[live] {len(medias)} fetched, dispatching {len(item_keys)} to background …")
            fut = pool.submit(_dispatch, item_keys, round_num)
            pending_futures.append((round_num, item_keys, fut))

            delay = random.uniform(*BATCH_DELAY)
            print(f"  sleeping {delay:.1f}s before next fetch …")
            time.sleep(delay)

        # Wait for all remaining in-flight batches
        for rnum, keys, fut in pending_futures:
            if fut.result() is True:
                with done_lock:
                    for k in keys:
                        done.add(k)
                save_json_file(PROGRESS_FILE, sorted(done))

    print(f"\n[done] Dispatched batches for {total_unliked:,} posts via live mode.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--resolve-only", action="store_true",
                        help="Only run Phase 1 (resolve URLs), skip Phase 2 (unlike).")
    parser.add_argument("--live", action="store_true",
                        help="Fetch current liked posts via instagrapi instead of export file.")
    args = parser.parse_args()

    if args.live:
        load_dotenv()
        session_id = unquote(os.getenv("INSTAGRAM_SESSION_ID", "").strip())
        if not session_id:
            sys.exit("[ERROR] INSTAGRAM_SESSION_ID not set in .env")
        live_unlike_loop(session_id)
        return

    if not LIKED_POSTS_FILE.exists():
        sys.exit(f"[ERROR] '{LIKED_POSTS_FILE}' not found.")

    entries = parse_export(LIKED_POSTS_FILE)
    if not entries:
        sys.exit("[ERROR] No entries parsed from export file.")

    resolved = resolve_phase(entries)

    if args.resolve_only:
        print("[done] Resolve-only mode — exiting before unlike phase.")
        return

    unlike_phase(entries, resolved)


if __name__ == "__main__":
    main()
