# Video XP

YouTube and Instagram recommend for watch time, which favours drama and clickbait. Video XP recommends for
**your judgement**: it plays a video, you say **good** or **not good**, and it learns. No choosing, no search.

**Open it:** https://atheendre130505.github.io/video-xp/ (phone or PC; `start.bat` / `Video XP.url` open the same page)

## Using it
| | Desktop | Phone |
|---|---|---|
| good / not good | G / B (or move the mouse → 👍 👎) | tap → 👍 👎 |
| next video | ↓ (or N): asks for your reaction first | swipe up |
| previous video | ↑ | swipe down |
| pause | click, Space | tap |
| skip 5 s ahead / back | → / ←, or double-click right / left | double-tap right / left |
| captions, speed | C, [ ] | tap → CC, 1× |

- A video never advances on its own, and next always asks for your reaction first.
- **Rushing** (watching at about 2x, or skipping ahead 3+ times) is a signal: a rushed "good" counts less,
  and a rushed video you leave without reacting counts as a soft "not good".
- Anything you reacted to is never shown again (it stays in the database for learning).
- **Ads:** sponsor reads inside a video are skipped automatically (SponsorBlock data). If YouTube plays its own ad,
  an "Ad" label appears and your tap reaches YouTube's Skip button. Trailers and product promos are kept out of
  the feed. Opening the site in Brave blocks YouTube's own ads altogether.
- Close the app mid-video and it resumes there, on any synced device.

## What plays
Every 5 videos: **3 for your taste**, **1 essential** (must-watch channels from `essentials.txt`, rotating through the
topics you've seen least), **1 explore** (a channel you've never been shown).

## How it learns (GitHub Actions, every 6 h and shortly after you react)
1. **Candidates** (yt-dlp page listings, no API key): channels in `subscriptions.txt` and `essentials.txt`,
   searches across 16 categories, and more like what you called good. School/exam videos are dropped;
   shouting titles are kept out of the essential and explore slots.
2. **NLP model:** every title+channel is embedded with all-MiniLM-L6-v2 (a small pretrained language model),
   so "Japanese joinery" sits near "Swiss watchmaking" without shared tags.
3. **Taste vector:** the average direction of what you called good, pushed away from not good and rushed-and-abandoned videos.
   Ranking uses closeness to it; the app also nudges it instantly on your device after every reaction.
4. **Deep model:** an MLP on the embedding + video numbers + your reactions per channel, trained on your
   reactions once there are 20+. Before that, a simple prior (subscriptions, essentials, popularity).

Not used: watch time as a goal, your old YouTube history (only to avoid repeats), transcripts/descriptions
(they need per-video requests that YouTube blocks from cloud servers).

## Privacy
This repo is public. The database, reactions, taste vector and resume points exist here **only encrypted**
(AES-256-GCM). The public feed is a list of candidate videos, not what you watched.
`subscriptions.txt` is public; delete lines you'd rather not show.

## Syncing a device (once per device)
Without it the app works but only learns on that device. With it, the cloud model learns and devices stay in step.
1. Open the `SETUP_LINK` from your local `cloud.env` on the device (stores the passphrase there).
2. In the app: ⚙ → paste a fine-grained GitHub token (this repo only, *Contents: Read and write*),
   made at https://github.com/settings/personal-access-tokens/new
