# Video XP

A reels-style YouTube feed: it plays, you keep watching or move on, and a small neural network learns
what you'll keep watching. No choosing, no playlists.

**Open it:** https://atheendre130505.github.io/video-xp/ (phone or PC; `start.bat` / `Video XP.url` open the same page)

## Controls
| | Desktop | Phone |
|---|---|---|
| next / back | → / ← | swipe up / down |
| pause | click, Space | tap |
| 👍 👎, CC, speed | move the mouse · L / D / C / [ ] | tap the screen |
| only a topic or a channel | 🔍 or `/`, type, Enter | 🔍 |
| back to your feed | ✕ on the pill · Esc | ✕ on the pill |

## How it works
GitHub Actions runs every 6 hours (and shortly after a device uploads watch data):
1. **Candidates** (yt-dlp page listings, no API key): every video of the channels in `subscriptions.txt`,
   rotating searches across 16 categories, and more of whatever you keep watching. School/exam videos are dropped.
2. **Features** (SQLite): title+channel embedding (MiniLM), length, views, your affinity to the channel,
   whether you're subscribed, similarity to what you kept watching / skipped.
3. **Model:** an MLP predicting how much of a video you'll watch (👍/👎 count double).
4. **Publish:** the ranked feed + a compact catalog go to GitHub Pages. The app re-ranks as you watch and
   syncs encrypted watch data back.

## Privacy
This repo is public. The database, imported history and watch data exist here **only encrypted**
(AES-256-GCM) on the `data` branch. The published site holds the video catalog, not your history.
`subscriptions.txt` is public; delete lines you'd rather not show.

## Syncing a device (optional, once per device)
Without this the app still works and adapts on the device; with it the cloud model learns from that device.
1. Open the `SETUP_LINK` from your local `cloud.env` on the device (stores the passphrase there).
2. In the app: ⚙ → paste a fine-grained GitHub token (this repo only, *Contents: Read and write*),
   made at https://github.com/settings/personal-access-tokens/new
