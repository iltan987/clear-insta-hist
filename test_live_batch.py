"""
test_live_batch.py
------------------
One-shot test of the live unlike flow:
  1. Fetches 99 liked posts via instagrapi
  2. Prints 5 sample URLs so you can open them and confirm they are liked
  3. Waits for your keypress
  4. Sends the unlike batch via wbloks
  5. Fetches liked posts again and reports whether the 5 samples are gone
"""

import os
import sys
from urllib.parse import unquote
from dotenv import load_dotenv

# Reuse all the real machinery from the main script
from bulk_unlike_web import (
    _IgClient,
    BATCH_SIZE,
    fetch_tokens,
    post_batch,
)

SAMPLE_N = 5  # how many URLs to show for manual verification


def main() -> None:
    load_dotenv()
    session_id = unquote(os.getenv("INSTAGRAM_SESSION_ID", "").strip())
    if not session_id:
        sys.exit("[ERROR] INSTAGRAM_SESSION_ID not set in .env")

    if _IgClient is None:
        sys.exit("[ERROR] instagrapi is not installed. Run: pip install instagrapi")

    # ------------------------------------------------------------------ #
    # Step 1: fetch one batch of liked posts
    # ------------------------------------------------------------------ #
    print(f"[test] Logging in via instagrapi …")
    cl = _IgClient()
    cl.login_by_sessionid(session_id)

    print(f"[test] Fetching up to {BATCH_SIZE} liked posts …")
    medias = cl.liked_medias(amount=BATCH_SIZE)
    if not medias:
        sys.exit("[test] No liked posts found — nothing to do.")

    print(f"[test] Got {len(medias)} liked posts.\n")

    # ------------------------------------------------------------------ #
    # Step 2: show 5 sample URLs for manual inspection
    # ------------------------------------------------------------------ #
    samples = medias[:SAMPLE_N]
    sample_pks = {m.pk for m in samples}

    print(f"--- {SAMPLE_N} sample URLs (open these and confirm they are liked) ---")
    for m in samples:
        print(f"  https://www.instagram.com/p/{m.code}/")
    print()

    input("Press ENTER when you have verified the posts are liked …\n")

    # ------------------------------------------------------------------ #
    # Step 3: unlike the full batch
    # ------------------------------------------------------------------ #
    item_keys = [f"{m.pk}_0" for m in medias]
    print(f"[test] Fetching tokens …")
    tokens, ig_session = fetch_tokens(session_id)

    print(f"[test] Sending unlike batch ({len(item_keys)} items) …")
    success = post_batch(session_id, tokens, item_keys, ig_session=ig_session)

    if not success:
        sys.exit("[test] Batch POST returned failure — see response above.")

    print("\n[test] Batch sent. Waiting 5 s for Instagram to process …")
    import time; time.sleep(5)

    # ------------------------------------------------------------------ #
    # Step 4: verify the 5 samples are no longer in the liked list
    # ------------------------------------------------------------------ #
    print(f"[test] Re-fetching liked posts to verify …")
    medias_after = cl.liked_medias(amount=BATCH_SIZE * 2)  # fetch wider net
    after_pks = {m.pk for m in medias_after}

    print(f"\n--- Verification results ---")
    all_gone = True
    for m in samples:
        url = f"https://www.instagram.com/p/{m.code}/"
        gone = m.pk not in after_pks
        status = "✓ unliked" if gone else "✗ still liked"
        print(f"  {status}  {url}")
        if not gone:
            all_gone = False

    print()
    if all_gone:
        print("[test] All 5 sample posts are confirmed unliked. 🎉")
    else:
        print("[test] Some posts still appear liked — Instagram feed may lag. "
              "Try refreshing the pages manually in a minute.")


if __name__ == "__main__":
    main()
