"""SQLite storage: candidate videos + their features, what you watched, your imported history, embeddings."""
import sqlite3, time, json, os
from pathlib import Path

DB_PATH = Path(os.environ.get("VXP_DB") or Path(__file__).with_name("videoxp.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id TEXT PRIMARY KEY,
    topic TEXT NOT NULL,         -- category / seed that found it (e.g. "Philosophy", or a channel)
    title TEXT,
    channel TEXT,
    channel_url TEXT,
    duration INTEGER,
    views INTEGER,
    likes INTEGER,
    comment_count INTEGER,
    upload_date TEXT,
    description TEXT,
    tags TEXT,                   -- JSON list
    embeddable INTEGER DEFAULT 1,
    enriched INTEGER DEFAULT 0,  -- full lookup: 0 not yet, 2 running, 1 done, -1 failed/unavailable
    priority INTEGER DEFAULT 0,  -- lookup workers only take priority >= 1 (set by the model for its top picks)
    source TEXT,                 -- sub (subscribed channel) | essential | seed (category search) | expand
    added_at REAL,
    enriched_at REAL,
    p_watch REAL,                -- model: predicted share of the video you'd watch
    scored_at REAL
);
CREATE INDEX IF NOT EXISTS idx_videos_queue ON videos(enriched, priority);
-- one row per report from a device. liked: 1 good, -1 not good, NULL = no reaction yet (in progress).
-- soft = 1 when the -1 came from moving on within 10 s. pos = where playback was (seconds), for resuming.
CREATE TABLE IF NOT EXISTS events (video_id TEXT, topic TEXT, watched REAL, duration REAL, liked INTEGER, ts REAL,
                                   pos REAL, soft INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS history (video_id TEXT, title TEXT, channel TEXT, channel_url TEXT, ts TEXT,
                                    PRIMARY KEY (video_id, ts));
CREATE TABLE IF NOT EXISTS emb (id TEXT PRIMARY KEY, vec BLOB);          -- title|channel embedding (float32)
CREATE TABLE IF NOT EXISTS searches (topic TEXT, query TEXT, ts REAL, PRIMARY KEY (topic, query));
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""
KEEP_COLS = ("id", "topic", "title", "channel", "channel_url", "duration", "views", "likes", "comment_count",
             "upload_date", "description", "tags", "embeddable", "enriched", "priority", "source", "added_at",
             "enriched_at", "p_watch", "scored_at")
OLD_TABLES = ("links", "containers", "members", "forum_posts", "crawl_state", "forum_channels", "channel_lists")


def conn():
    c = sqlite3.connect(DB_PATH, timeout=60, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=60000")
    return c


def init():
    with conn() as c:
        c.executescript(SCHEMA)
        _cleanup(c)
        ecols = {r["name"] for r in c.execute("PRAGMA table_info(events)")}
        for col, typ in (("pos", "REAL"), ("soft", "INTEGER DEFAULT 0")):
            if col not in ecols:
                c.execute(f"ALTER TABLE events ADD COLUMN {col} {typ}")
        c.execute("UPDATE videos SET enriched=0 WHERE enriched=2")      # lookups interrupted by a restart


def _cleanup(c):
    """One-time removal of what earlier versions (forums, comment-link mining) left behind."""
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='forum_posts'").fetchone() and \
       {r["name"] for r in c.execute("PRAGMA table_info(videos)")} <= set(KEEP_COLS):
        return
    for t in OLD_TABLES:
        c.execute(f"DROP TABLE IF EXISTS {t}")
    c.execute("""DELETE FROM videos WHERE source NOT IN ('search','seed','expand','channel','hist_channel','sub','essential')
                 AND id NOT IN (SELECT video_id FROM events)""")
    c.execute("UPDATE videos SET source='seed' WHERE source='search'")
    for col in [r["name"] for r in c.execute("PRAGMA table_info(videos)")]:
        if col not in KEEP_COLS:
            c.execute(f"ALTER TABLE videos DROP COLUMN {col}")
    c.execute("DELETE FROM emb WHERE id NOT LIKE 'h:%' AND id NOT IN (SELECT id FROM videos)")


def vacuum():
    c = sqlite3.connect(DB_PATH)
    c.execute("VACUUM")
    c.close()


def q(sql, args=()):
    with conn() as c:
        return [dict(r) for r in c.execute(sql, args)]


def x(sql, args=()):
    with conn() as c:
        return c.execute(sql, args).rowcount


def add_candidates(topic, entries, source):
    with conn() as c:
        for e in entries:
            c.execute("""INSERT OR IGNORE INTO videos (id, topic, title, channel, channel_url, duration, views,
                         added_at, source) VALUES (?,?,?,?,?,?,?,?,?)""",
                      (e["id"], topic, e.get("title"), e.get("channel"), e.get("channel_url"), e.get("duration"),
                       e.get("view_count"), time.time(), source))
            if e.get("view_count"):          # keep view counts fresh on re-listing
                c.execute("UPDATE videos SET views=? WHERE id=?", (e["view_count"], e["id"]))


def claim():
    """Atomically take the most urgent video waiting for a full lookup."""
    with conn() as c:
        r = c.execute("""UPDATE videos SET enriched=2 WHERE id = (
                           SELECT id FROM videos WHERE enriched=0 AND priority>=1 ORDER BY priority DESC, added_at LIMIT 1)
                         RETURNING id""").fetchone()
        return dict(r) if r else None


def save_lookup(vid, info):
    with conn() as c:
        c.execute("""UPDATE videos SET title=?, channel=?, channel_url=?, duration=?, views=?, likes=?, comment_count=?,
                     upload_date=?, description=?, tags=?, embeddable=?, enriched=1, enriched_at=? WHERE id=?""",
                  (info.get("title"), info.get("channel"), info.get("channel_url") or info.get("uploader_url"),
                   info.get("duration"), info.get("view_count"), info.get("like_count"), info.get("comment_count"),
                   info.get("upload_date"), (info.get("description") or "")[:3000], json.dumps(info.get("tags") or []),
                   0 if info.get("playable_in_embed") is False else 1, time.time(), vid))


def mark(vid, **fields):
    if fields.get("enriched") == -1:
        fields["enriched_at"] = time.time()
    with conn() as c:
        c.execute(f"UPDATE videos SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?", (*fields.values(), vid))


def video(vid):
    r = q("SELECT * FROM videos WHERE id=?", (vid,))
    return r[0] if r else None


def add_events(batch):
    with conn() as c:
        c.executemany("INSERT INTO events (video_id, topic, watched, duration, liked, ts, pos, soft) VALUES (?,'',?,?,?,?,?,?)",
                      [(e["id"], float(e.get("watched") or 0), float(e.get("duration") or 0), e.get("liked"),
                        float(e.get("ts") or time.time()), e.get("pos"), int(bool(e.get("soft")))) for e in batch])


def events():
    return q("SELECT * FROM events ORDER BY ts")


def searched(topic, query):
    r = q("SELECT ts FROM searches WHERE topic=? AND query=?", (topic, query))
    return r[0]["ts"] if r else None


def mark_searched(topic, query):
    x("INSERT OR REPLACE INTO searches VALUES (?,?,?)", (topic, query, time.time()))


def subscribed_names():
    """Display names of subscribed channels (written by whichever crawler listed them)."""
    return {r["value"] for r in q("SELECT value FROM kv WHERE key LIKE 'subname:%'")}


def kv_get(key, default=None):
    r = q("SELECT value FROM kv WHERE key=?", (key,))
    return r[0]["value"] if r else default


def kv_set(key, value):
    x("INSERT OR REPLACE INTO kv VALUES (?,?)", (key, str(value)))
