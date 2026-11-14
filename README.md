# Video XP

A reels-style YouTube feed: it plays, you keep watching or swipe on, and a small neural network learns
what you'll keep watching. No choosing, no playlists.

- **Phone / anywhere:** GitHub Pages app, fed by GitHub Actions (crawl → retrain → publish every 6 h, and
  shortly after your phone uploads watch data).
- **On the PC:** `start.bat` runs the same idea locally (Flask) with live retraining after every video.

## Controls
| | Desktop | Phone |
|---|---|---|
| next / back | → / ← | swipe up / down |
| pause | click, Space | tap |
| 👍 👎, CC, speed | move the mouse · L / D / C / [ ] | tap the screen |
| only a topic or a channel | 🔍 or `/`, type, Enter | 🔍 |
| back to your feed | ✕ on the pill · Esc | ✕ on the pill |

## How it works
1. **Candidates:** every video of the channels in `subscriptions.txt` + rotating searches across 16 categories
   (philosophy, history, humanities, art, physics, chemistry, biology, math, finance, economics, …) + more of
   whatever you keep watching. School/exam videos are filtered out.
2. **Features** (SQLite): title+channel embedding (MiniLM), length, views, likes, like ratio, comments, age,
   your affinity to the channel, whether you're subscribed, similarity to what you kept watching / skipped.
3. **Model:** MLP predicting how much of a video you'll watch (👍/👎 count double); history import = weak signal.
4. **Phone:** downloads the ranked feed + a compact catalog, re-ranks as you swipe, and syncs encrypted watch data back.

## Privacy
This repo is public. Your database, imported history and watch data exist here **only encrypted**
(AES-256-GCM, passphrase in GitHub Secrets and on your phone) on the `data` branch. The published site
contains the recommended-video catalog, not your history. `subscriptions.txt` is public; delete lines you'd rather not show.

## Setup (once)
1. **YouTube Data API key** (free): console.cloud.google.com → new project → *APIs & Services* → enable
   **YouTube Data API v3** → *Credentials* → *Create credentials* → **API key**.
2. On the PC: `.venv\Scripts\python cloud_setup.py` (asks for a passphrase + the key, uploads your encrypted data,
   starts the first run).
3. On the phone: open **https://atheendre130505.github.io/video-xp/** → tap → ⚙ → enter the passphrase and a
   fine-grained GitHub token (this repo only, *Contents: Read and write*).
