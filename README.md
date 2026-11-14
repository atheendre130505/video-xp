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
| captions | C | tap → CC |
| full screen on / off | F | tap → ⛶ |

- Nothing asks you to rate. The buttons stay hidden until you tap (phone) or move the mouse (laptop).
- When a video ends it waits: the next one starts only after you tap 👍 or 👎.
- Every video plays at 1.25×. There is no speed control and no skipping ahead or back within a video.
- Anything you reacted to is never shown again (it stays in the database for learning).
- **Ads:** sponsor reads inside a video are skipped automatically (SponsorBlock data). If YouTube plays its own ad,
  an "Ad" label appears and your tap reaches YouTube's Skip button. Trailers and product promos are kept out of
  the feed. Opening the site in Brave blocks YouTube's own ads altogether.
- Leave the tab or app and the video pauses; come back and it carries on.
- Close the app mid-video and it resumes there, on any synced device.

## What plays
Topics take turns: after a video, the next one comes from the next topic in the cycle (29 topics, from History → Physics →
Film → Economics → … to Nature; edit `CATEGORIES` in fetcher.py), the best video of that topic for you. A series (Episode 1, 2, 3…, Part 1/2…) plays in order
to the end before the topic moves on, and always starts from its earliest episode you haven't seen.
The pool mixes your subscriptions, the must-watch channels in `essentials.txt` and new channels found by topic searches.

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

## Devices stay in step (no setup)
Every device announces what it is doing (playing / paused, which video, where) to a shared relay (ntfy.sh)
and listens live for the others. Only one video plays at a time: when another device starts, this one pauses
and lines up the same video at the same spot. If both try to play at once, the laptop wins. Opening the app on
either device continues from the most recent position. The cloud reads the relay hourly, retrains, and publishes `state.json` (every video you rated, your
taste vector, unfinished videos).

## Privacy
The relay and `state.json` hold video ids and positions only (no name, no account), but they are not
encrypted: someone who found the relay address in the page could see which videos you rated.
The full database lives encrypted (AES-256-GCM) on the `data` branch. `subscriptions.txt` is public.
