"""Cloud pipeline (GitHub Actions): watch data in -> crawl -> retrain -> feed out.

    python pipeline.py --data DATA_DIR --site SITE_DIR [--quick]

DATA_DIR is a checkout of the repo's `data` branch:
    videoxp.db.enc      the whole database, encrypted (crypto_box) with VXP_PASSPHRASE
    events/*.json       watch data your phone uploaded (encrypted the same way, in the browser)
SITE_DIR receives index.html + feed.json for GitHub Pages (public: only the recommended-video feed).

--quick (the phone uploaded watch data and sent a "watch-data" repository_dispatch): no searches, just your data
        + channel uploads of what you kept watching.
"""
import argparse, base64, glob, json, logging, os, random, shutil, sys, tempfile, time
from pathlib import Path

ROOT = Path(__file__).parent
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("pipeline")

FEED_TOP = 400            # best predictions
FEED_PER_TOPIC = 6        # + the best few of every category/channel, so the phone can keep things mixed
FEED_EXPLORE = 80         # + random others, for exploration
SUB_FIRST = 1000          # first crawl of a subscribed channel: newest uploads …
SUB_POPULAR = 300         # … plus its "popular videos" playlist
SUB_REFRESH = 50          # later runs: newest only
SEARCHES_PER_RUN = 6      # category searches (100 units each) per full run; categories rotate
EXPANSION_SEARCHES = 3    # "more like this" searches for videos you kept watching


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
    import db, model, yt_api, fetcher
    db.init()

    processed = ingest_events(data, pw)
    if not os.environ.get("YT_API_KEY"):
        log.warning("no YT_API_KEY secret: skipping crawling, only retraining on existing videos")
    else:
        try:
            crawl(quick=a.quick)
        except yt_api.QuotaExceeded:
            log.warning("YouTube API daily budget reached; continuing with what we have")
    log.info("YouTube API units used this run: %d", yt_api.units["used"])

    model.train_and_score(request_lookups=False)
    export_site(site)

    db.vacuum()
    crypto_box.encrypt_file(work / "videoxp.db", enc, pw)
    for f in processed:
        f.unlink()
    log.info("encrypted database written (%.1f MB)", enc.stat().st_size / 1e6)


# ---------------- watch data from the phone ----------------
def ingest_events(data, pw):
    import crypto_box, db
    files = sorted(glob.glob(str(data / "events" / "*.json")))
    n = 0
    for f in files:
        try:
            batch = json.loads(crypto_box.decrypt(Path(f).read_bytes(), pw))
        except Exception as ex:
            log.warning("can't read %s (%s); skipping", f, ex)
            continue
        with db.conn() as c:
            for e in batch.get("events", []):
                c.execute("INSERT INTO events VALUES (?,?,?,?,?,?)",
                          (e["id"], "", float(e.get("watched") or 0), float(e.get("duration") or 0),
                           e.get("liked"), float(e.get("ts") or time.time())))
                n += 1
    log.info("ingested %d watch events from %d phone uploads", n, len(files))
    return [Path(f) for f in files]


# ---------------- crawling (YouTube Data API) ----------------
def crawl(quick):
    import db, fetcher, yt_api, model
    cur = fetcher.CURRICULUM
    known = lambda ids: {r["id"] for r in db.q(
        f"SELECT id FROM videos WHERE id IN ({','.join('?' * len(ids))}) AND enriched=1", ids)} if ids else set()

    def add(ids, topic, source):
        new = [i for i in dict.fromkeys(ids) if i not in known(list(ids))]
        return yt_api.store(yt_api.video_details(new), topic, source, curriculum=cur) if new else 0

    # 1) more of what you kept watching: that channel's newest uploads (cheap) + a similar search (rationed)
    eng = model.engagement_by_video()
    since = float(db.kv_get("last_expand_ts", 0))
    kept = [vid for vid, (y, m) in eng.items() if y >= 0.6 and m["ts"] > since]
    searches = 0
    for vid in kept:
        v = db.video(vid)
        if not v or not v.get("channel_url") or "/channel/UC" not in v["channel_url"]:
            continue
        cid = v["channel_url"].rsplit("/", 1)[-1]
        add(yt_api.playlist_video_ids("UU" + cid[2:], 50), v["topic"], "expand")
        if not quick and searches < EXPANSION_SEARCHES:
            add(yt_api.search(v["title"][:80], 25), v["topic"], "expand")
            searches += 1
    db.kv_set("last_expand_ts", time.time())
    log.info("expanded from %d kept videos", len(kept))
    if quick:
        return

    # 2) subscriptions: whole catalogue the first time, newest uploads afterwards
    for url in fetcher.subscriptions():
        key = "api_chan:" + url
        info = db.kv_get(key)
        if info:
            cid, title, uploads = json.loads(info)
        else:
            r = yt_api.resolve_channel(url)
            if not r:
                log.warning("can't resolve %s", url)
                continue
            cid, title, uploads = r
            db.kv_set(key, json.dumps(r))
        db.x("INSERT OR REPLACE INTO kv VALUES (?,?)", ("subname:" + url, title))
        first = not db.kv_get("api_sub_done:" + url)
        ids = yt_api.playlist_video_ids(uploads, SUB_FIRST if first else SUB_REFRESH)
        if first:
            ids += yt_api.playlist_video_ids("UULP" + cid[2:], SUB_POPULAR)
        n = add(ids, title, "sub")
        db.kv_set("api_sub_done:" + url, time.time())
        log.info("subscription %s: %d new videos", title, n)

    # 3) categories: a few searches per run, rotating through categories and their queries
    cats = list(fetcher.CATEGORIES)
    start = int(db.kv_get("cat_rotate", 0))
    for i in range(SEARCHES_PER_RUN):
        cat = cats[(start + i) % len(cats)]
        qs = fetcher.CATEGORIES[cat]
        qi = int(db.kv_get("cat_q:" + cat, 0))
        n = add(yt_api.search(qs[qi % len(qs)]), cat, "seed")
        db.kv_set("cat_q:" + cat, qi + 1)
        log.info("category %s (%s): %d new videos", cat, qs[qi % len(qs)], n)
    db.kv_set("cat_rotate", (start + SEARCHES_PER_RUN) % len(cats))


# ---------------- what the phone downloads ----------------
PCA_DIM = 128     # browser vectors: 384-d MiniLM embeddings projected to 128-d (keeps similarity, 3x smaller)


def export_site(site):
    """feed.json   ~600 videos to play next (best predictions + a few per category + random for exploring)
       catalog.*   every playable video (for "only AI videos" / "only this channel" / browsing a channel):
                   catalog.json = titles etc., catalog.bin = int8 128-d vectors, pca.bin = 384->128 projection
                   so the browser can embed what you type with the same MiniLM model and compare."""
    import numpy as np
    import db, model
    rows = db.q("""SELECT id, topic, title, channel, duration, views, upload_date, p_watch FROM videos
                   WHERE enriched=1 AND embeddable=1 AND p_watch IS NOT NULL ORDER BY p_watch DESC""")
    seen = {r["id"] for r in db.q("SELECT DISTINCT video_id id FROM events UNION SELECT DISTINCT video_id FROM history")}
    emb = model.embeddings([(r["id"], model.text_of(r)) for r in rows])
    E = np.stack([emb[r["id"]] for r in rows]).astype(np.float32)

    # PCA 384 -> 128 (fit on the catalog itself); browser applies the same projection to typed queries
    mean = E.mean(axis=0)
    _, _, vt = np.linalg.svd(E - mean, full_matrices=False)
    P = vt[:PCA_DIM].T.astype(np.float32)                       # 384 x 128
    Z = (E - mean) @ P
    Z /= np.linalg.norm(Z, axis=1, keepdims=True) + 1e-9
    Zq = np.clip(np.round(Z * 127), -127, 127).astype(np.int8)

    site.mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / "site" / "index.html", site / "index.html")
    cols = ["id", "title", "channel", "duration", "topic", "p", "views", "date", "seen"]
    cat = [[r["id"], r["title"], r["channel"], r["duration"], r["topic"], round(float(r["p_watch"]), 4),
            r["views"], r["upload_date"], int(r["id"] in seen)] for r in rows]
    (site / "catalog.json").write_text(json.dumps({"cols": cols, "rows": cat, "dim": PCA_DIM},
                                                  separators=(",", ":")), encoding="utf-8")
    (site / "catalog.bin").write_bytes(Zq.tobytes())
    (site / "pca.bin").write_bytes(np.concatenate([mean.astype(np.float32), P.ravel()]).tobytes())

    # the everyday feed: indices into the catalog (unseen only)
    unseen = [i for i, r in enumerate(rows) if r["id"] not in seen]
    chosen = list(dict.fromkeys(unseen[:FEED_TOP]))
    per = {}
    for i in unseen:
        t = rows[i]["topic"]
        if per.get(t, 0) < FEED_PER_TOPIC:
            per[t] = per.get(t, 0) + 1
            if i not in chosen:
                chosen.append(i)
    rest = [i for i in unseen if i not in set(chosen)]
    chosen += random.Random(int(time.time() // 3600)).sample(rest, min(FEED_EXPLORE, len(rest)))
    feed = {"generated": int(time.time()), "model": model.status, "feed": chosen}
    (site / "feed.json").write_text(json.dumps(feed, separators=(",", ":")), encoding="utf-8")
    size = lambda f: (site / f).stat().st_size / 1e6
    log.info("site: catalog %d videos (%.1f MB json + %.1f MB vectors), feed %d", len(rows), size("catalog.json"),
             size("catalog.bin"), len(chosen))


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    main()
