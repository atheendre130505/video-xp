"""Finds candidate videos on YouTube with yt-dlp page listings. No API key, no per-video requests.

One request returns a whole page of videos (title, length, channel, views):
    - every channel in subscriptions.txt (whole catalogue the first time, newest uploads afterwards)
    - the "must watch" channels in essentials.txt
    - searches across the categories below
    - more like whatever you keep watching (similar search + that channel's uploads)
"""
import re, time, logging
from pathlib import Path
import yt_dlp
import db

log = logging.getLogger("fetcher")

CATEGORIES = {   # dict order = the order topics take turns in (alternating very different areas)
    "History": ["history documentary", "ancient civilizations documentary", "history of empires", "modern history explained"],
    "Physics": ["physics documentary", "quantum mechanics mysteries", "relativity explained visually", "unsolved problems in physics"],
    "Film": ["filmmaking explained", "cinematography breakdown", "film analysis video essay", "how movies are made documentary"],
    "Economics": ["economics explained", "economic history documentary", "why countries are rich or poor", "behavioral economics"],
    "Biology": ["biology documentary", "evolution documentary", "genetics explained", "how cells work"],
    "Philosophy": ["philosophy explained", "stoicism", "existentialism explained", "ethics thought experiment"],
    "Game Design": ["game design analysis", "level design explained", "history of video games documentary", "why this game works"],
    "Space": ["space documentary", "black holes explained", "astronomy mysteries", "exoplanets explained"],
    "Literature & Writing": ["literature analysis video essay", "storytelling explained", "great novels explained", "how to write well"],
    "Engineering": ["engineering explained", "how it's made engineering", "megaprojects documentary", "how bridges work"],
    "Personal Growth": ["how to live a good life", "practical philosophy for everyday life", "emotional intelligence explained",
                        "how to build good habits", "relationships psychology explained"],
    "Art": ["art history documentary", "famous paintings explained", "sculpture documentary", "how artists see"],
    "Psychology": ["psychology explained", "how the brain works", "cognitive biases explained", "neuroscience documentary"],
    "Chemistry": ["chemistry documentary", "the chemistry of everyday things", "periodic table elements story"],
    "Music": ["music theory explained", "how music works documentary", "composer documentary", "why this song works"],
    "Finance": ["personal finance explained", "how investing works", "financial crisis explained", "how banks work"],
    "Religion & Mythology": ["mythology explained", "history of religion documentary", "ancient myths explained"],
    "Math": ["beautiful math explained", "unsolved math problems", "history of mathematics documentary"],
    "Architecture & Design": ["architecture documentary", "why buildings look the way they do", "design history documentary",
                              "typography explained"],
    "Geopolitics": ["geopolitics explained", "why borders are where they are", "geography explained"],
    "Health & Medicine": ["how the human body works documentary", "history of medicine", "nutrition science explained",
                          "how vaccines work"],
    "Language & Linguistics": ["linguistics explained", "history of languages", "how writing systems work"],
    "Computer Science": ["computer science explained", "how computers work", "algorithms explained visually"],
    "Craftsmanship": ["traditional craftsmanship documentary", "japanese woodworking joinery", "master craftsman documentary",
                      "how it's made by hand"],
    "Earth & Climate": ["geology documentary", "climate science explained", "how volcanoes work", "ocean documentary"],
    "Humanities": ["anthropology documentary", "sociology explained", "how cultures differ documentary"],
    "AI": ["artificial intelligence explained", "how neural networks work", "AI research explained"],
    "Food Science": ["food science explained", "history of food documentary", "the science of cooking"],
    "Business": ["business case study", "how companies make money", "startup story documentary"],
    "Nature": ["nature documentary", "animal behavior documentary", "wildlife documentary"],
}
# school-curriculum / exam-prep videos are dropped as candidates
CURRICULUM = re.compile(r"\b(class\s*\d{1,2}|std\s*\d{1,2}|grade\s*\d{1,2}|ncert|cbse|icse|jee|neet|upsc|ssc|"
                        r"board exams?|one shot|chapter\s*\d+|lecture\s*\d+|ch[\s.-]*\d+|for kids|exam|syllabus|"
                        r"revision|gcse|a[ -]level|ap (physics|chemistry|biology)|mcqs?|pyqs?|maths|worksheet|homework|"
                        r"speed,? distance and time|class notes|tuition|cbse|igcse)\b", re.I)
CLICKBAIT = re.compile(r"(!!+|\?\?+|\b(shocking|insane|you won'?t believe|gone wrong|destroy(s|ed)|exposed|"
                       r"mind[- ]?blow\w*|secret they|nobody tells|will blow|must see|omg|wtf)\b|[🌀-🫿])", re.I)


def clickbait(title):
    t = title or ""
    letters = [c for c in t if c.isalpha()]
    caps = sum(c.isupper() for c in letters) / len(letters) if len(letters) >= 12 else 0
    return bool(CLICKBAIT.search(t)) or caps > 0.6


def filler(r):
    """Background / sleep content: fine to exist, but not for the essential or explore slots."""
    return bool(re.search(r"\b(sleep\w*|asmr|relax\w*|ambience|lofi|white noise)\b", f"{r.get('title')} {r.get('channel')}", re.I))


PROMO = re.compile(r"(\b(official\s+)?(trailer|teaser|tv spot|commercial|promo|sneak peek|launch event)\b|"
                   r"#ad\b|#sponsored|\b(introducing|unboxing|now available|pre-?order|giveaway|coming soon)\b|"
                   r"\|\s*(apple|google|meta|samsung|marvel)\s*$)", re.I)
BRAND_CHANNELS = {"Apple", "Google", "Facebook", "Marvel Entertainment", "Meta", "Samsung"}


def promo(r):
    """The video itself is advertising: trailers, product launches, brand channels' short spots. Kept out of the feed."""
    if PROMO.search(r.get("title") or ""):
        return True
    return r.get("channel") in BRAND_CHANNELS and (r.get("duration") or 0) < 600


SUBSCRIPTIONS_FILE = Path(__file__).with_name("subscriptions.txt")
SUB_LATEST = 600            # first crawl of a subscribed channel: newest videos …
SUB_POPULAR = 300           # … plus its most popular ones (covers huge channels without listing 100k videos)
SUB_REFRESH = 40            # later crawls: just the newest
MIN_DURATION, MAX_DURATION = 3 * 60, 12 * 60 * 60     # long podcasts are welcome; 12h+ is livestream loops
BOT = ("not a bot", "Please sign in.")

blocked = False             # YouTube asked us to stop (bot check): the caller ends the crawl for this run
_BASE = {"quiet": True, "no_warnings": True, "skip_download": True, "noprogress": True, "extract_flat": True,
         # approximate upload dates ("3 days ago") from the same listing page: no extra requests
         "extractor_args": {"youtubetab": {"approximate_date": [""]}}}


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
                     "upload_date": e.get("upload_date"),
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


def _sub_lines():
    """(channel URL, pinned topic or None, featured?) per line of subscriptions.txt:
    "@handle", "@handle | Topic", "@handle | featured", "@handle | featured, Topic"."""
    out = []
    if SUBSCRIPTIONS_FILE.exists():
        for line in SUBSCRIPTIONS_FILE.read_text(encoding="utf-8").splitlines():
            s = line.split("#", 1)[0].strip()
            if s:
                h, _, rest = (x.strip() for x in s.partition("|"))
                tags = [t.strip() for t in rest.split(",") if t.strip()]
                topic = next((t for t in tags if t in CATEGORIES), None)
                out.append((h if h.startswith("http") else f"https://www.youtube.com/{h}", topic, "featured" in tags))
    return out


def subscriptions():
    """Channel URLs from subscriptions.txt."""
    return [u for u, _, _ in _sub_lines()]


def pinned_topics():
    """channel display name -> topic, for subscriptions pinned with "| Topic" (names learned when crawled)."""
    out = {}
    for url, topic, _ in _sub_lines():
        name = db.kv_get("subname:" + url.rstrip("/")) if topic in CATEGORIES else None
        if name:
            out[name] = topic
    return out


def featured_channels():
    """display names of channels marked "featured" in subscriptions.txt (names learned when crawled)."""
    return {n for url, _, feat in _sub_lines() if feat for n in [db.kv_get("subname:" + url.rstrip("/"))] if n}


def essentials():
    """(channel URL, topic) pairs from essentials.txt."""
    out = []
    f = Path(__file__).with_name("essentials.txt")
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            s = line.split("#", 1)[0].strip()
            if "|" in s:
                h, topic = (x.strip() for x in s.split("|", 1))
                out.append((h if h.startswith("http") else f"https://www.youtube.com/{h}", topic))
    return out


def crawl_channel(url, topic=None, source="sub", latest=SUB_LATEST, popular=SUB_POPULAR):
    """Whole catalogue the first time (newest `latest` + most popular `popular`), newest uploads afterwards.
    topic=None files the videos under the channel's own name (subscriptions)."""
    url = url.rstrip("/")
    first = not db.searched(source, url)
    res = _flat(url + "/videos", latest if first else SUB_REFRESH)
    if not res:
        return 0
    name = res.get("channel") or res.get("uploader") or url
    n = _store(res.get("entries") or [], topic or name, source, channel=name, channel_url=url)
    if first and res.get("channel_id"):
        time.sleep(2)
        pop = _flat(f"https://www.youtube.com/playlist?list=UULP{res['channel_id'][2:]}", popular)
        if pop:
            n += _store(pop.get("entries") or [], topic or name, source, channel=name, channel_url=url)
    db.mark_searched(source, url)
    if source == "sub":
        db.kv_set("subname:" + url, name)
    return n
