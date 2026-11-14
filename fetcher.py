"""YouTube via yt-dlp (no API key), kept cheap on purpose.

Stage 1, candidates: one request returns a whole page of videos (title, length, channel, sometimes views).
    - category searches (philosophy, history, finance, economics, …), refreshed daily
    - latest uploads of the channels you watch most (from your imported history)
    - more like whatever you keep watching (similar search + that channel's uploads)
Stage 2, full lookup (one request per video): only for the candidates the model ranks highest; adds likes,
comment count, views, upload date, tags, and whether the video can be embedded.
"""
import json, re, threading, time, logging
from concurrent.futures import ThreadPoolExecutor
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
SUB_LATEST = 600            # per subscribed channel: newest videos …
SUB_POPULAR = 300           # … plus its most popular ones (covers huge channels like TEDx / CNBC without listing 100k)
SUB_REFRESH = 24 * 3600
PER_SEARCH = 20
SEARCH_TTL = 24 * 3600
HISTORY_CHANNELS = 25       # your most-watched channels (Takeout) whose latest uploads become candidates
PER_CHANNEL = 12
LOOKUP_THREADS = 3
PAUSE = 1.0                 # seconds between lookups per thread (polite: avoids YouTube's bot check)
BOT_COOLDOWN = 600          # first pause when YouTube shows the bot check; doubles if it repeats
BOT = ("not a bot", "Please sign in.")
TRANSIENT = ("getaddrinfo", "TransportError", "timed out", "HTTP Error 429", "HTTP Error 5", "Connection",
             "Temporary failure", "Unable to download API page")
MIN_DURATION, MAX_DURATION = 3 * 60, 4 * 60 * 60

on_new_candidates = lambda: None    # set by app.py: tells the model to rescore when new candidates arrive
_BASE = {"quiet": True, "no_warnings": True, "skip_download": True, "noprogress": True}
_pool = ThreadPoolExecutor(max_workers=3)
_wake = threading.Event()


# ---------------- politeness ----------------
def cooldown_left():
    return max(0.0, float(db.kv_get("bot_cooldown_until", 0)) - time.time())


def _hit_bot_check():
    if cooldown_left() > 0:
        return
    quiet = time.time() - float(db.kv_get("bot_last_strike", 0)) > 6 * 3600
    strikes = 1 if quiet else int(db.kv_get("bot_strikes", 0)) + 1        # strikes expire after 6 quiet hours
    wait = min(BOT_COOLDOWN * 2 ** (strikes - 1), 4 * 3600)
    db.kv_set("bot_last_strike", time.time())
    db.kv_set("bot_strikes", strikes)
    db.kv_set("bot_cooldown_until", time.time() + wait)
    log.warning("YouTube bot check hit (#%d): pausing %d min", strikes, wait // 60)


def _wait_cooldown():
    while cooldown_left():
        time.sleep(min(cooldown_left(), 30))


def ok_duration(d):
    return not d or MIN_DURATION <= d <= MAX_DURATION


# ---------------- stage 1: cheap candidates ----------------
def _flat(url, n):
    _wait_cooldown()
    try:
        with yt_dlp.YoutubeDL({**_BASE, "extract_flat": True, "playlistend": n}) as ydl:
            return ydl.extract_info(url, download=False)
    except Exception as ex:
        if any(s in str(ex) for s in BOT):
            _hit_bot_check()
        log.info("listing %s failed: %s", url[:80], str(ex)[:100])
        return None


def _store(entries, topic, source, channel=None, channel_url=None):
    rows = [{"id": e["id"], "title": e.get("title"), "duration": e.get("duration"),
             "view_count": e.get("view_count"), "channel": e.get("channel") or channel}
            for e in entries if e.get("id") and ok_duration(e.get("duration")) and "/shorts/" not in (e.get("url") or "")
            and not CURRICULUM.search(e.get("title") or "")]
    db.add_candidates(topic, rows, source)
    if rows:
        on_new_candidates()
    if channel_url and rows:
        db.x(f"UPDATE videos SET channel_url=COALESCE(channel_url, ?) WHERE id IN ({','.join('?' * len(rows))})",
             (channel_url, *[r["id"] for r in rows]))
    return len(rows)


def search(query, topic, n=PER_SEARCH, source="seed"):
    res = _flat(f"ytsearch{n}:{query}", n)
    return _store(res.get("entries") or [], topic, source) if res else 0


def channel_uploads(url, topic, n=PER_CHANNEL, source="hist_channel"):
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
    """All of a subscribed channel's videos (newest SUB_LATEST + most popular SUB_POPULAR) into the pool."""
    url = url.rstrip("/")
    first = not db.searched("sub", url)
    res = _flat(url + "/videos", SUB_LATEST if first else 40)        # daily refresh only needs the newest
    if not res:
        return 0
    name = res.get("channel") or res.get("uploader") or url
    n = _store(res.get("entries") or [], name, "sub", channel=name, channel_url=url)
    if first and res.get("channel_id"):
        time.sleep(3)
        pop = _flat(f"https://www.youtube.com/playlist?list=UULP{res['channel_id'][2:]}", SUB_POPULAR)
        if pop:
            n += _store(pop.get("entries") or [], name, "sub", channel=name, channel_url=url)
    db.mark_searched("sub", url)
    db.x("INSERT OR REPLACE INTO kv VALUES (?,?)", ("subname:" + url, name))
    log.info("subscription %s: %d videos", name, n)
    return n


def subscribed_names():
    return {r["value"] for r in db.q("SELECT value FROM kv WHERE key LIKE 'subname:%'")}


def _due(key):
    ts = db.searched("seed", key)
    return not ts or time.time() - ts > SEARCH_TTL


def seed():
    """Fill the pool: category searches round-robin (so every category gets videos early) + your top channels."""
    def job():
        for url in subscriptions():                                   # your subscriptions first
            ts = db.searched("sub", url)
            if not ts or time.time() - ts > SUB_REFRESH:
                crawl_subscription(url)
                time.sleep(4)                                         # spread requests out (YouTube rate limits)
        queues = {cat: list(qs) for cat, qs in CATEGORIES.items()}
        while any(queues.values()):
            for cat, qs in queues.items():
                if qs:
                    q = qs.pop(0)
                    if _due(q) and search(q, cat):
                        db.mark_searched("seed", q)
        for r in db.q("""SELECT channel_url, MAX(channel) channel, COUNT(*) n FROM history
                         WHERE channel_url LIKE '%youtube.com/%' GROUP BY channel_url ORDER BY n DESC LIMIT ?""",
                      (HISTORY_CHANNELS,)):
            key = "chan:" + r["channel_url"]
            if _due(key) and channel_uploads(r["channel_url"], "Your channels"):
                db.mark_searched("seed", key)
        log.info("candidate pool seeded")
    threading.Thread(target=job, daemon=True).start()


def expand_from(vid):
    """You kept watching (or liked) a video: queue similar videos and more from that channel."""
    v = db.video(vid)
    if not v:
        return
    tags = [t for t in json.loads(v.get("tags") or "[]") if len(t) > 3][:3]
    q = " ".join(tags) if tags else re.sub(r"[^\w\s]", " ", v["title"] or "")[:60]
    if q.strip():
        _pool.submit(search, q, v["topic"], 15, "expand")
    if v.get("channel_url"):
        _pool.submit(channel_uploads, v["channel_url"], v["topic"], 15, "expand")


# ---------------- stage 2: full lookup, only for the model's top picks ----------------
def lookup(vid):
    _wait_cooldown()
    try:
        with yt_dlp.YoutubeDL(dict(_BASE)) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False)
    except Exception as ex:
        msg = str(ex)
        if any(s in msg for s in BOT):
            db.mark(vid, enriched=0, priority=0)
            _hit_bot_check()
        elif any(s in msg for s in TRANSIENT):
            db.mark(vid, enriched=0)
            time.sleep(15)
        else:                                       # removed, private, age-gated…
            db.mark(vid, enriched=-1, embeddable=0)
        return
    db.save_lookup(vid, info)
    if info.get("live_status") in ("is_live", "is_upcoming") or not ok_duration(info.get("duration")):
        db.mark(vid, embeddable=0)


def request_lookups(ids, priority=4):
    if ids:
        db.x(f"UPDATE videos SET priority=MAX(priority, ?) WHERE enriched=0 AND id IN ({','.join('?' * len(ids))})",
             (priority, *ids))
        _wake.set()


def _worker():
    while True:
        if cooldown_left():
            time.sleep(min(cooldown_left(), 30))
            continue
        job = db.claim()
        if job:
            lookup(job["id"])
            time.sleep(PAUSE)
            continue
        _wake.wait(5)
        _wake.clear()


def start_workers():
    for _ in range(LOOKUP_THREADS):
        threading.Thread(target=_worker, daemon=True).start()
