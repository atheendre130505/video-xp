"""Finds candidate videos on YouTube with yt-dlp page listings. No API key, no per-video requests.

One request returns a whole page of videos (title, length, channel, views):
    - every channel in subscriptions.txt (whole catalogue the first time, newest uploads afterwards)
    - searches across the categories below
    - more like whatever you keep watching (similar search + that channel's uploads)
"""
import re, time, logging
from pathlib import Path
import yt_dlp
import db

log = logging.getLogger("fetcher")

CATEGORIES = {   # queries chosen to find documentaries / explainers, not school lessons
    "Philosophy": ["philosophy explained", "stoicism", "existentialism explained", "ethics thought experiment"],
    "History": ["history documentary", "ancient civilizations documentary", "history of empires",
                "modern history explained"],
    "Humanities": ["anthropology documentary", "linguistics explained", "mythology explained",
                   "history of religion documentary", "literature analysis video essay"],
    "Art": ["art history documentary", "famous paintings explained", "architecture documentary",
            "music theory explained", "film analysis video essay"],
    "Physics": ["physics documentary", "quantum mechanics mysteries", "relativity explained visually",
                "unsolved problems in physics"],
    "Chemistry": ["chemistry documentary", "the chemistry of everyday things", "periodic table elements story"],
    "Biology": ["biology documentary", "evolution documentary", "how the human body works documentary",
                "genetics explained"],
    "Math": ["beautiful math explained", "unsolved math problems", "history of mathematics documentary"],
    "Finance": ["personal finance explained", "how investing works", "financial crisis explained", "how banks work"],
    "Economics": ["economics explained", "economic history documentary", "why countries are rich or poor",
                  "behavioral economics"],
    "Psychology": ["psychology explained", "how the brain works", "cognitive biases explained"],
    "Technology": ["how technology works", "artificial intelligence explained", "engineering explained"],
    "Geopolitics": ["geopolitics explained", "why borders are where they are", "geography explained"],
    "Space": ["space documentary", "black holes explained", "astronomy mysteries"],
    "Business": ["business case study", "how companies make money", "startup story documentary"],
    "Nature": ["nature documentary", "animal behavior documentary"],
}
# school-curriculum / exam-prep videos are dropped as candidates
CURRICULUM = re.compile(r"\b(class\s*\d{1,2}|std\s*\d{1,2}|grade\s*\d{1,2}|ncert|cbse|icse|jee|neet|upsc|ssc|"
                        r"board exams?|one shot|chapter\s*\d+|lecture\s*\d+|ch[\s.-]*\d+|for kids|exam|syllabus|"
                        r"revision|gcse|a[ -]level|ap (physics|chemistry|biology)|mcqs?|pyqs?)\b", re.I)
SUBSCRIPTIONS_FILE = Path(__file__).with_name("subscriptions.txt")
SUB_LATEST = 600            # first crawl of a subscribed channel: newest videos …
SUB_POPULAR = 300           # … plus its most popular ones (covers huge channels without listing 100k videos)
SUB_REFRESH = 40            # later crawls: just the newest
MIN_DURATION, MAX_DURATION = 3 * 60, 4 * 60 * 60
BOT = ("not a bot", "Please sign in.")

blocked = False             # YouTube asked us to stop (bot check): the caller ends the crawl for this run
_BASE = {"quiet": True, "no_warnings": True, "skip_download": True, "noprogress": True, "extract_flat": True}


def _flat(url, n):
    global blocked
    try:
        with yt_dlp.YoutubeDL({**_BASE, "playlistend": n}) as ydl:
            return ydl.extract_info(url, download=False)
    except Exception as ex:
        if any(s in str(ex) for s in BOT):
            blocked = True
        log.warning("listing %s failed: %s", url[:80], str(ex)[:120])
        return None


def _store(entries, topic, source, channel=None, channel_url=None):
    rows = []
    for e in entries:
        d = e.get("duration")
        if not e.get("id") or (d and not MIN_DURATION <= d <= MAX_DURATION) or "/shorts/" in (e.get("url") or "") \
                or CURRICULUM.search(e.get("title") or ""):
            continue
        rows.append({"id": e["id"], "title": e.get("title"), "duration": d, "view_count": e.get("view_count"),
                     "channel": e.get("channel") or e.get("uploader") or channel,
                     "channel_url": channel_url or e.get("channel_url") or e.get("uploader_url")})
    db.add_candidates(topic, rows, source)
    return len(rows)


def search(query, topic, n=20, source="seed"):
    res = _flat(f"ytsearch{n}:{query}", n)
    return _store(res.get("entries") or [], topic, source) if res else 0


def channel_uploads(url, topic, n=15, source="expand"):
    url = url.rstrip("/")
    res = _flat(url + "/videos", n)
    if not res:
        return 0
    return _store(res.get("entries") or [], topic, source, channel=res.get("channel") or res.get("uploader"),
                  channel_url=url)


def subscriptions():
    """Channel URLs from subscriptions.txt."""
    out = []
    if SUBSCRIPTIONS_FILE.exists():
        for line in SUBSCRIPTIONS_FILE.read_text(encoding="utf-8").splitlines():
            s = line.split("#", 1)[0].strip()
            if s:
                out.append(s if s.startswith("http") else f"https://www.youtube.com/{s}")
    return out


def crawl_subscription(url):
    url = url.rstrip("/")
    first = not db.searched("sub", url)
    res = _flat(url + "/videos", SUB_LATEST if first else SUB_REFRESH)
    if not res:
        return 0
    name = res.get("channel") or res.get("uploader") or url
    n = _store(res.get("entries") or [], name, "sub", channel=name, channel_url=url)
    if first and res.get("channel_id"):
        time.sleep(2)
        pop = _flat(f"https://www.youtube.com/playlist?list=UULP{res['channel_id'][2:]}", SUB_POPULAR)
        if pop:
            n += _store(pop.get("entries") or [], name, "sub", channel=name, channel_url=url)
    db.mark_searched("sub", url)
    db.kv_set("subname:" + url, name)
    return n
