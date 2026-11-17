"""Cloud pipeline (GitHub Actions): your reactions in -> crawl -> retrain -> feed out. No API keys.

    python pipeline.py --data DATA_DIR --site SITE_DIR [--quick]

DATA_DIR is a checkout of the repo's `data` branch:
    videoxp.db.enc      the whole database, encrypted (crypto_box) with VXP_PASSPHRASE
Your devices post what you did (watched to X s / good / not good, by video id) to a keyless relay (RELAY);
each run reads what's new there. The relay keeps messages 12 hours and the workflow runs hourly.
SITE_DIR receives the GitHub Pages site:
    index.html          the app
    feed.json           what to play next: three pools (taste / essential / explore) with their vectors
    state.json          every video you rated + your taste vector + unfinished videos (read by all devices)

--quick: skip the big crawl (it runs at most every FULL_CRAWL_HOURS).
"""
import argparse, base64, glob, hashlib, json, logging, os, re, random, shutil, sys, tempfile, time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).parent
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("pipeline")

POOL_TASTE = 300          # best for your taste (model score + closeness to your taste vector)
POOL_MUST = 160           # essentials: best of the must-watch channels, spread over topics
POOL_EXPLORE = 160        # channels you've never been shown
PER_CHANNEL_TASTE = 8     # so one channel can't fill the taste pool
PER_TOPIC = 30            # + the best videos of every topic, so the app can rotate through all of them
PER_TOPIC_RANDOM = 15     # + random others per topic (new channels for the app's random picks)
SOURCE_ONLY_TOPICS = {"Personal Growth"}
KNN = 20                  # topic sorting: how many similar known videos vote
KNN_MIN_SHARE = 0.65      # … the winning topic needs this share of the vote
KNN_MIN_SIM = 0.45        # … and at least one really similar known video; otherwise the video stays unsorted
UNSORTED = "_unsorted"
FEATURED_MIN = 12         # featured channels (subscriptions.txt "| featured"): at least this many videos each in the feed
FEATURED_NEWEST = 4       # … of which their newest uploads
FEATURED_BOOST = 0.08     # … and a small lift so they come up regularly
SERIES_MAX = 15           # episodes shipped per series
# "Episode 4", "Ep. 4", "Part 2", "Chapter 3", "(3/10)", "3 of 10"
EPISODE = re.compile(r"\b(?:episode|ep\.?|part|pt\.?|chapter|chap\.?)\s*#?\s*(\d{1,3})\b|"
                     r"\((\d{1,3})\s*/\s*\d{1,3}\)|\b(\d{1,3})\s+of\s+\d{1,3}\b", re.I)


def episode(r):
    """(series key, episode number) from the title, or None. Same channel + same title before the marker = same series."""
    m = EPISODE.search(r["title"] or "")
    if not m:
        return None
    n = int(next(g for g in m.groups() if g))
    stem = re.sub(r"[^a-z0-9]+", " ", r["title"][:m.start()].lower()).strip()
    key = hashlib.sha1(f"{r['channel']}|{stem}".encode()).hexdigest()[:10]
    return key, n
SEARCHES_PER_RUN = 29     # category searches per full run (one per topic); their queries rotate
EXPAND_MAX = 15           # at most this many liked videos expanded per run
CRAWL_MINUTES = 25        # crawling stops after this long; training always gets its turn
PCA_DIM = 128             # app vectors: 384-d MiniLM embeddings projected to 128-d
# how far each reaction moves the taste vector (rushed = watched at ~2x or kept skipping ahead)
W_GOOD, W_RUSHED_GOOD, W_BAD, W_SOFT = 1.0, 0.4, -0.7, -0.35
RELAY = "https://ntfy.sh/vxp-6a706fa3aaeeab939c2b3a5e"     # same as in site/index.html
FULL_CRAWL_HOURS = 6


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--site", required=True)
    ap.add_argument("--quick", action="store_true")
    a = ap.parse_args()
    data, site = Path(a.data), Path(a.site)
    pw = os.environ["VXP_PASSPHRASE"]
    work = Path(tempfile.mkdtemp())
    os.environ["VXP_DB"] = str(work / "videoxp.db")

    import crypto_box
    enc = data / "videoxp.db.enc"
    if enc.exists():
        crypto_box.decrypt_file(enc, work / "videoxp.db", pw)
        log.info("database decrypted (%.1f MB)", (work / "videoxp.db").stat().st_size / 1e6)
    import db, model
    db.init()

    processed = ingest_events(data, pw)
    ingest_relay()
    full_due = time.time() - float(db.kv_get("last_full_crawl", 0)) > FULL_CRAWL_HOURS * 3600 - 600
    quick = (a.quick or not full_due) and not os.environ.get("VXP_FULL")    # manual runs always crawl fully
    try:
        crawl(quick)
        if not quick:
            db.kv_set("last_full_crawl", time.time())
    except Exception:
        log.exception("crawl failed; continuing with the videos we have")
    model.train_and_score()
    export_site(site)

    db.vacuum()
    crypto_box.encrypt_file(work / "videoxp.db", enc, pw)
    for f in processed:
        f.unlink()
    log.info("encrypted database written (%.1f MB)", enc.stat().st_size / 1e6)


# ---------------- reactions from your devices ----------------
def ingest_events(data, pw):
    import crypto_box, db
    files = sorted(glob.glob(str(data / "events" / "*.json")))
    n = 0
    for f in files:
        try:
            batch = json.loads(crypto_box.decrypt(Path(f).read_bytes(), pw)).get("events", [])
        except Exception as ex:
            log.warning("can't read %s (%s); skipping", f, ex)
            continue
        db.add_events(batch)
        n += len(batch)
    log.info("ingested %d events from %d uploads", n, len(files))
    return [Path(f) for f in files]


def ingest_relay():
    """What your devices posted since the last run (watched to X s / good / not good)."""
    import urllib.request, db
    since = db.kv_get("relay_since") or "12h"
    try:
        with urllib.request.urlopen(f"{RELAY}/json?poll=1&since={since}", timeout=30) as r:
            lines = r.read().decode("utf-8").splitlines()
    except Exception as ex:
        log.warning("relay not reachable (%s); next run will catch up", ex)
        return
    n, last = 0, None
    for line in lines:
        try:
            m = json.loads(line)
            if m.get("event") != "message":
                continue
            last = m["id"]
            events = [e for e in json.loads(m["message"]).get("events", []) if isinstance(e, dict) and e.get("id")]
        except Exception:
            continue
        db.add_events(events)
        n += len(events)
    if last:
        db.kv_set("relay_since", last)
    log.info("relay: %d events from %d messages", n, len(lines))


# ---------------- crawling (yt-dlp page listings) ----------------
def crawl(quick):
    import db, fetcher, model
    deadline = time.time() + CRAWL_MINUTES * 60
    go = lambda: not fetcher.blocked and time.time() < deadline

    # 1) more of what you called good: similar videos + that channel's uploads
    eng = model.engagement_by_video()
    since = float(db.kv_get("last_expand_ts", 0))
    liked = sorted((vid for vid, (y, m) in eng.items() if y == 1 and m["ts"] > since),
                   key=lambda v: -eng[v][1]["ts"])[:EXPAND_MAX]
    n = 0
    for vid in liked:
        v = db.video(vid)
        if not v or not go():
            continue
        if v.get("channel_url"):
            n += fetcher.channel_uploads(v["channel_url"], v["topic"])
        n += fetcher.search((v["title"] or "")[:80], v["topic"], 15, "expand")
    db.kv_set("last_expand_ts", time.time())
    log.info("expanded from %d liked videos: %d candidates listed", len(liked), n)
    if quick:
        return

    n = 0
    for url in fetcher.subscriptions():
        if not go():
            break
        n += fetcher.crawl_channel(url)
        time.sleep(1)
    log.info("subscriptions: %d videos listed", n)

    n = 0
    for url, topic in fetcher.essentials():
        if not go():
            break
        n += fetcher.crawl_channel(url, topic, "essential", latest=100, popular=150)
        time.sleep(1)
    log.info("essentials: %d videos listed", n)

    cats = list(fetcher.CATEGORIES)
    start = int(db.kv_get("cat_rotate", 0))
    for i in range(SEARCHES_PER_RUN):
        if not go():
            break
        cat = cats[(start + i) % len(cats)]
        qs = fetcher.CATEGORIES[cat]
        qi = int(db.kv_get("cat_q:" + cat, 0))
        fetcher.search(qs[qi % len(qs)], cat)
        db.kv_set("cat_q:" + cat, qi + 1)
    db.kv_set("cat_rotate", (start + SEARCHES_PER_RUN) % len(cats))
    if fetcher.blocked:
        log.warning("YouTube refused listings from this server this run; using the videos we have")


# ---------------- what the app downloads ----------------
def export_site(site):
    import numpy as np
    import db, model, fetcher
    rows = db.q("""SELECT rowid AS rid, id, topic, source, title, channel, channel_url, duration, views, p_watch, added_at FROM videos
                   WHERE embeddable=1 AND enriched != -1 AND p_watch IS NOT NULL AND title IS NOT NULL
                   AND source NOT IN ('hist_channel', 'channel')   -- channels from your old YouTube history: never shown
                   AND id NOT IN (SELECT video_id FROM events) AND id NOT IN (SELECT video_id FROM history)""")
    n_all = len(rows)
    rows = [r for r in rows if not fetcher.promo(r) and not fetcher.CURRICULUM.search(r["title"] or "")]   # no promos / school lessons
    log.info("left out %d promo / trailer videos", n_all - len(rows))
    reacted = model.engagement_by_video()                     # id -> (1 | 0, {w, ts, soft})
    ev_rows = {r["id"]: r for r in db.q("""SELECT id, title, channel, duration FROM videos
                                           WHERE id IN (SELECT DISTINCT video_id FROM events)""")}
    emb = model.embeddings([(r["id"], model.text_of(r)) for r in rows + list(ev_rows.values())])

    # shared 128-d space (PCA fitted on the unseen videos)
    E = np.stack([emb[r["id"]] for r in rows]).astype(np.float32)
    mean = E.mean(axis=0)
    _, _, vt = np.linalg.svd(E - mean, full_matrices=False)
    P = vt[:PCA_DIM].T.astype(np.float32)
    proj = lambda X: (lambda Z: Z / (np.linalg.norm(Z, axis=-1, keepdims=True) + 1e-9))((X - mean) @ P)
    Z = proj(E)

    # taste vector: pulled toward what you called good, pushed away from not good / rushed-and-abandoned; recent counts more
    taste = np.zeros(PCA_DIM, dtype=np.float32)
    order = sorted((v for v in reacted if v in emb), key=lambda v: -reacted[v][1]["ts"])
    for k, vid in enumerate(order):
        y, m = reacted[vid]
        w = (W_RUSHED_GOOD if m.get("soft") else W_GOOD) if y == 1 else (W_SOFT if m.get("soft") else W_BAD)
        taste += (0.985 ** k) * w * proj(emb[vid])
    if np.linalg.norm(taste) > 0:
        taste /= np.linalg.norm(taste)
    sim = Z @ taste

    # must-watch channels: topic as listed in essentials.txt now (so relabelling a channel there applies to its old videos)
    ess = {url.rstrip("/"): topic for url, topic in fetcher.essentials()}
    for r in rows:
        if r["source"] == "essential" and (r["channel_url"] or "").rstrip("/") in ess:
            r["topic"] = ess[r["channel_url"].rstrip("/")]
    # subscriptions pinned to a topic in subscriptions.txt ("@handle | Personal Growth")
    pinned = fetcher.pinned_topics()
    for r in rows:
        if r["channel"] in pinned:
            r["topic"] = pinned[r["channel"]]
    # one topic per video. Videos from topic searches / must-watch channels / pinned subscriptions keep theirs.
    # Every other video is sorted by its 20 most similar videos whose topic is known (similarity-weighted vote).
    # If the vote is split or nothing similar is known, the video stays unsorted and is left out of the rotation:
    # better to skip a doubtful video than to file it under the wrong topic.
    cats = list(fetcher.CATEGORIES)
    En = E / (np.linalg.norm(E, axis=1, keepdims=True) + 1e-9)
    lab = [i for i, r in enumerate(rows) if r["topic"] in fetcher.CATEGORIES and r["topic"] not in SOURCE_ONLY_TOPICS]
    unl = [i for i, r in enumerate(rows) if r["topic"] not in fetcher.CATEGORIES]
    lab_cat = np.array([cats.index(rows[i]["topic"]) for i in lab])
    L = En[lab]
    k = min(KNN, len(lab))
    featured_names = fetcher.featured_channels()           # featured podcasts/talks: never dropped, best guess wins
    for s0 in range(0, len(unl), 2000):
        chunk = unl[s0:s0 + 2000]
        S_ = En[chunk] @ L.T
        top = np.argpartition(-S_, k - 1, axis=1)[:, :k]
        for j, i in enumerate(chunk):
            nb = top[j]
            sims = S_[j, nb]
            votes = np.zeros(len(cats))
            np.add.at(votes, lab_cat[nb], np.maximum(sims, 0))
            best = int(votes.argmax())
            share = votes[best] / (votes.sum() or 1)
            sure = share >= KNN_MIN_SHARE and sims.max() >= KNN_MIN_SIM
            rows[i]["topic"] = cats[best] if sure or rows[i]["channel"] in featured_names else UNSORTED
    keep = [i for i, r in enumerate(rows) if r["topic"] != UNSORTED]
    log.info("topics: %d known, %d sorted by vote, %d left unsorted (not shown)",
             len(lab), len(unl) - (len(rows) - len(keep)), len(rows) - len(keep))
    rows, E, Z, sim = [rows[i] for i in keep], E[keep], Z[keep], sim[keep]

    # series: same channel + same title stem + an episode number; at least two episodes to count
    eps = defaultdict(dict)                                   # key -> {episode number: row index}
    for i, r in enumerate(rows):
        e = episode(r)
        if e:
            eps[e[0]].setdefault(e[1], i)
    series = {k: v for k, v in eps.items() if len(v) >= 2}
    ser_of = {}
    for k, v in series.items():
        topic = rows[v[min(v)]]["topic"]                      # a whole series stays in one topic
        for n, i in v.items():
            ser_of[i] = (k, n)
            rows[i]["topic"] = topic
    log.info("topics assigned; %d series found", len(series))

    shown_channels = {r["channel"] for r in db.q(
        "SELECT DISTINCT v.channel FROM events e JOIN videos v ON v.id = e.video_id")}
    idx = list(range(len(rows)))
    chosen = {}                                               # row index -> kind

    # T: taste — model score + closeness to the taste vector, capped per channel
    per = defaultdict(int)
    for i in sorted(idx, key=lambda i: -(rows[i]["p_watch"] + 0.3 * float(sim[i]))):
        if per[rows[i]["channel"]] < PER_CHANNEL_TASTE:
            per[rows[i]["channel"]] += 1
            chosen[i] = "t"
            if len(chosen) >= POOL_TASTE:
                break

    # M: must watch — the essentials list, most-viewed first, round-robin over topics, no shouting titles
    by_topic = defaultdict(list)
    for i in idx:
        r = rows[i]
        if r["source"] == "essential" and i not in chosen and not fetcher.clickbait(r["title"]):
            by_topic[r["topic"]].append(i)
    for t in by_topic:
        by_topic[t].sort(key=lambda i: -(rows[i]["views"] or 0))
    n_m = 0
    while n_m < POOL_MUST and any(by_topic.values()):
        for t in list(by_topic):
            if by_topic[t] and n_m < POOL_MUST:
                chosen[by_topic[t].pop(0)] = "m"; n_m += 1

    # X: explore — channels new to you (found via topic searches), decent reach, calm titles, at most 2 per channel
    rng = random.Random(int(time.time() // 3600))
    known = db.subscribed_names() | {r["channel"] for r in rows if r["source"] in ("sub", "essential")}
    pool = [i for i in idx if i not in chosen and rows[i]["channel"] not in shown_channels
            and rows[i]["channel"] not in known           # new to you: not subscribed, not on the essentials list
            and (rows[i]["views"] or 0) >= 20000 and not fetcher.clickbait(rows[i]["title"])
            and not fetcher.filler(rows[i])]
    rng.shuffle(pool)
    per, n_x = defaultdict(int), 0
    for i in pool:
        if per[rows[i]["channel"]] < 2:
            per[rows[i]["channel"]] += 1
            chosen[i] = "x"; n_x += 1
            if n_x >= POOL_EXPLORE:
                break

    # every topic: its best videos (the app rotates through topics)
    by_t = defaultdict(list)
    for i in idx:
        by_t[rows[i]["topic"]].append(i)
    for t, ii in by_t.items():
        for i in sorted(ii, key=lambda i: -(rows[i]["p_watch"] + 0.3 * float(sim[i])))[:PER_TOPIC]:
            chosen.setdefault(i, "t")
        rest = [i for i in ii if i not in chosen and not fetcher.clickbait(rows[i]["title"]) and not fetcher.filler(rows[i])]
        for i in rng.sample(rest, min(PER_TOPIC_RANDOM, len(rest))):
            chosen[i] = "x"
    # series: if any episode is in the feed, ship the series (so it can be played in order)
    for i in list(chosen):
        if i in ser_of:
            for n, j in sorted(series[ser_of[i][0]].items())[:SERIES_MAX]:
                chosen.setdefault(j, chosen[i])

    # featured channels: always at least FEATURED_MIN videos each, their newest uploads first
    featured = fetcher.featured_channels()
    for ch in featured:
        mine = [i for i in idx if rows[i]["channel"] == ch]
        newest = sorted(mine, key=lambda i: (-(rows[i]["added_at"] or 0), rows[i]["rid"]))[:FEATURED_NEWEST]
        best = sorted(mine, key=lambda i: -(rows[i]["p_watch"] + 0.3 * float(sim[i])))
        for i in list(dict.fromkeys(newest + best))[:FEATURED_MIN]:
            chosen.setdefault(i, "t")
    for i in idx:
        if rows[i]["channel"] in featured:
            rows[i]["p_watch"] = min(1.0, rows[i]["p_watch"] + FEATURED_BOOST)
    log.info("featured channels in the feed: %s", {ch: sum(rows[i]["channel"] == ch for i in chosen) for ch in featured})

    sel = sorted(chosen, key=lambda i: -rows[i]["p_watch"])
    q8 = lambda M: np.clip(np.round(M * 127), -127, 127).astype(np.int8)
    gen = int(time.time())                                    # same stamp on feed + state: the app checks they match
    feed = {"generated": gen, "dim": PCA_DIM,
            "topics": [c for c in cats if any(rows[i]["topic"] == c for i in sel)],   # the rotation order
            "cols": ["id", "title", "channel", "duration", "topic", "p", "views", "kind", "s", "n"],
            "rows": [[rows[i]["id"], rows[i]["title"], rows[i]["channel"], rows[i]["duration"], rows[i]["topic"],
                      round(float(rows[i]["p_watch"]), 4), rows[i]["views"], chosen[i],
                      ser_of.get(i, (None, None))[0], ser_of.get(i, (None, None))[1]] for i in sel],
            "vec": base64.b64encode(q8(Z[sel]).tobytes()).decode()}
    site.mkdir(parents=True, exist_ok=True)
    for name in ("index.html", "icon.png"):
        shutil.copy(ROOT / "site" / name, site / name)
    (site / "feed.json").write_text(json.dumps(feed, separators=(",", ":")), encoding="utf-8")

    # state for all your devices: what you rated (never shown again), taste vector, unfinished videos
    last = {}
    for e in db.events():
        last[e["video_id"]] = e                               # oldest first -> latest wins
    cutoff = time.time() - 30 * 86400
    progress = [{"id": vid, "t": ev_rows[vid]["title"], "c": ev_rows[vid]["channel"], "d": ev_rows[vid]["duration"],
                 "pos": e["pos"], "ts": e["ts"]}
                for vid, e in last.items()
                if vid not in reacted and vid in ev_rows and e.get("pos") and (e["ts"] or 0) > cutoff]
    progress.sort(key=lambda p: -p["ts"])
    rated = [r["video_id"] for r in db.q("SELECT DISTINCT video_id FROM events WHERE liked IS NOT NULL")]
    state = {"generated": gen, "taste": [round(float(x), 4) for x in taste], "rated": rated,
             "progress": progress[:10], "reactions": len(reacted)}
    (site / "state.json").write_text(json.dumps(state, separators=(",", ":")), encoding="utf-8")
    kinds = defaultdict(int)
    for k in chosen.values():
        kinds[k] += 1
    log.info("feed: %d taste + %d essential + %d explore (%.0f KB) | %d reactions, %d unfinished",
             kinds["t"], kinds["m"], kinds["x"], (site / "feed.json").stat().st_size / 1024, len(reacted), len(progress))


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    main()
