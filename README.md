# Instagram Bulk Unlike

Bulk unlike all your liked Instagram posts — fast.

Uses the same internal web endpoint (`wbloks`) the Instagram UI uses when you select posts and click Unlike. No app passwords, no third-party services.

---

## How it works

**Live mode** (recommended): fetches your current liked posts directly via [instagrapi](https://github.com/subzeroid/instagrapi), unlikes each batch immediately, then fetches the next batch. No export file needed. Batches are sent concurrently in background threads so a slow response from Instagram never blocks progress.

**Export mode**: works from the `liked_posts.json` file in Instagram's "Download Your Data" archive. Media PKs are resolved locally (zero API calls) then unliked in batches.

---

## Setup

**1. Clone and install dependencies**

```bash
git clone https://github.com/iltan987/clear-insta-hist.git
cd clear-insta-hist
pip install -r requirements.txt
```

**2. Get your Instagram session ID**

1. Open [instagram.com](https://instagram.com) in your browser and log in.
2. Open DevTools (`F12`) → **Application** tab → **Cookies** → `https://www.instagram.com`
3. Find the cookie named `sessionid` and copy its value.

**3. Create your `.env` file**

```bash
cp .env.example .env
```

Paste your session ID into `.env`:

```
INSTAGRAM_SESSION_ID=your_session_id_here
```

> **Note:** paste the raw value. If it contains `%3A`, replace those with `:`.

---

## Usage

### Live mode — fetch and unlike in one go (recommended)

```bash
python bulk_unlike_web.py --live
```

Fetches 99 liked posts, unlikes them, fetches the next 99, and so on until the list is empty or the daily limit (5,000) is reached. Safe to re-run — already-processed posts are tracked in `wbloks_progress.json`.

### Export mode — use Instagram's data export

1. Request your data from **Instagram → Settings → Your activity → Download your information**.
2. Once received, place `liked_posts.json` from the archive into this directory.
3. Run:

```bash
python bulk_unlike_web.py
```

---

## Test before running

`test_live_batch.py` performs a single batch (99 posts) end-to-end with a manual verification step:

```bash
python test_live_batch.py
```

It will:
1. Fetch 99 liked posts
2. Print 5 sample URLs — open them and confirm they're liked
3. Wait for your keypress, then unlike all 99
4. Re-fetch and report whether the 5 samples are now unliked

---

## Configuration

All tuneable constants are at the top of `bulk_unlike_web.py`:

| Constant | Default | Description |
|---|---|---|
| `BATCH_SIZE` | `99` | Posts per wbloks request (API max) |
| `BATCH_DELAY` | `(5, 12)` | Random sleep between dispatches (seconds) |
| `DAILY_LIMIT` | `5000` | Max unlikes per run |
| `BATCH_WORKERS` | `3` | Concurrent background POST threads |

---

## Notes

- Session IDs expire. If the script exits with a login redirect error, grab a fresh `sessionid` from your browser.
- Instagram may rate-limit aggressively at higher volumes. If you see repeated errors, lower `BATCH_SIZE` or increase `BATCH_DELAY`.
- The `wbloks` endpoint is an internal Instagram API and may change without notice.

---

## Disclaimer

This tool is for personal use on your own account. Use it responsibly and in accordance with [Instagram's Terms of Use](https://help.instagram.com/581066165581870).
