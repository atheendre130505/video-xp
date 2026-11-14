"""Cloud pipeline (GitHub Actions): watch data in -> crawl -> retrain -> site out. No API keys.

    python pipeline.py --data DATA_DIR --site SITE_DIR [--quick]

DATA_DIR is a checkout of the repo's `data` branch:
    videoxp.db.enc      the whole database, encrypted (crypto_box) with VXP_PASSPHRASE
    events/*.json       watch data your devices uploaded (encrypted the same way, in the browser)
SITE_DIR receives the GitHub Pages site (index.html + the recommended-video catalog).

--quick (a device uploaded watch data and sent a "watch-data" repository_dispatch): no category searches,
        just your data + more of what you kept watching.
"""
import argparse, glob, json, logging, os, random, shutil, sys, tempfile, time
from pathlib import Path

ROOT = Path(__file__).parent
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("pipeline")

FEED_TOP = 400            # best predictions
FEED_PER_TOPIC = 6        # + the best few of every category/channel, so the app can keep things mixed
FEED_EXPLORE = 80         # + random others, for exploration
SEARCHES_PER_RUN = 12     # category searches per full run; categories and their queries rotate
EXPAND_MAX = 15           # at most this many "kept watching" videos expanded per run
CRAWL_MINUTES = 20        # crawling stops after this long; training always gets its turn
PCA_DIM = 128             # browser vectors: 384-d MiniLM embeddings projected to 128-d


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
    try:
        crawl(a.quick)
    except Exception:
        log.exception("crawl failed; continuing with the videos we have")
    model.train_and_score()
    export_site(site)

    db.vacuum()
    crypto_box.encrypt_file(work / "videoxp.db", enc, pw)
    for f in processed:
        f.unlink()
    log.info("encrypted database written (%.1f MB)", enc.stat().st_size / 1e6)


# ---------------- watch data from your devices ----------------
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
    log.info("ingested %d watch events from %d uploads", n, len(files))
    return [Path(f) for f in files]


# ---------------- crawling (yt-dlp page listings) ----------------
def crawl(quick):
    import db, fetcher, model
    deadline = time.time() + CRAWL_MINUTES * 60
    go = lambda: not fetcher.blocked and time.time() < deadline

    # 1) more of what you kept watching: similar videos + that channel's uploads
    eng = model.engagement_by_video()
    since = float(db.kv_get("last_expand_ts", 0))
    kept = sorted((vid for vid, (y, m) in eng.items() if y >= 0.6 and m["ts"] > since),
                  key=lambda v: -eng[v][1]["ts"])[:EXPAND_MAX]
    n = 0
    for vid in kept:
        v = db.video(vid)
        if not v or not go():
            continue
        if v.get("channel_url"):
            n += fetcher.channel_uploads(v["channel_url"], v["topic"])
        n += fetcher.search((v["title"] or "")[:80], v["topic"], 15, "expand")
    db.kv_set("last_expand_ts", time.time())
    log.info("expanded from %d kept videos: %d candidates listed", len(kept), n)
    if quick:
        return

    # 2) subscriptions: whole catalogue the first time, newest uploads afterwards
    n = 0
    for url in fetcher.subscriptions():
        if not go():
            break
        n += fetcher.crawl_subscription(url)
        time.sleep(1)
    log.info("subscriptions: %d videos listed", n)

    # 3) categories: a few searches per run, rotating through categories and their queries
    cats = list(fetcher.CATEGORIES)
    start = int(db.kv_get("cat_rotate", 0))
    for i in range(SEARCHES_PER_RUN):
        if not go():
            break
        cat = cats[(start + i) % len(cats)]
        qs = fetcher.CATEGORIES[cat]
        qi = int(db.kv_get("cat_q:" + cat, 0))
        got = fetcher.search(qs[qi % len(qs)], cat)
        db.kv_set("cat_q:" + cat, qi + 1)
        log.info("category %s (%s): %d listed", cat, qs[qi % len(qs)], got)
    db.kv_set("cat_rotate", (start + SEARCHES_PER_RUN) % len(cats))
    if fetcher.blocked:
        log.warning("YouTube refused listings from this server this run; using the videos we have")


# ---------------- what the app downloads ----------------
def export_site(site):
    """feed.json   the videos to play next (best predictions + a few per category + random for exploring)
       catalog.*   every playable video (for "only AI videos" / "only this channel" / browsing a channel):
                   catalog.json = titles etc., catalog.bin = int8 128-d vectors, pca.bin = 384->128 projection
                   so the browser can embed what you type with the same MiniLM model and compare."""
    import numpy as np
    import db, model
    rows = db.q("""SELECT id, topic, title, channel, duration, views, p_watch FROM videos
                   WHERE embeddable=1 AND enriched != -1 AND p_watch IS NOT NULL AND title IS NOT NULL
                   ORDER BY p_watch DESC""")
    seen = {r["id"] for r in db.q("SELECT DISTINCT video_id id FROM events UNION SELECT DISTINCT video_id FROM history")}
    emb = model.embeddings([(r["id"], model.text_of(r)) for r in rows])
    E = np.stack([emb[r["id"]] for r in rows]).astype(np.float32)

    mean = E.mean(axis=0)
    _, _, vt = np.linalg.svd(E - mean, full_matrices=False)
    P = vt[:PCA_DIM].T.astype(np.float32)                       # 384 x 128
    Z = (E - mean) @ P
    Z /= np.linalg.norm(Z, axis=1, keepdims=True) + 1e-9
    Zq = np.clip(np.round(Z * 127), -127, 127).astype(np.int8)

    site.mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / "site" / "index.html", site / "index.html")
    cols = ["id", "title", "channel", "duration", "topic", "p", "views", "seen"]
    cat = [[r["id"], r["title"], r["channel"], r["duration"], r["topic"], round(float(r["p_watch"]), 4),
            r["views"], int(r["id"] in seen)] for r in rows]
    (site / "catalog.json").write_text(json.dumps({"cols": cols, "rows": cat, "dim": PCA_DIM},
                                                  separators=(",", ":")), encoding="utf-8")
    (site / "catalog.bin").write_bytes(Zq.tobytes())
    (site / "pca.bin").write_bytes(np.concatenate([mean.astype(np.float32), P.ravel()]).tobytes())

    unseen = [i for i, r in enumerate(rows) if r["id"] not in seen]
    chosen = list(dict.fromkeys(unseen[:FEED_TOP]))
    have, per = set(chosen), {}
    for i in unseen:
        t = rows[i]["topic"]
        if per.get(t, 0) < FEED_PER_TOPIC:
            per[t] = per.get(t, 0) + 1
            if i not in have:
                chosen.append(i); have.add(i)
    rest = [i for i in unseen if i not in have]
    chosen += random.Random(int(time.time() // 3600)).sample(rest, min(FEED_EXPLORE, len(rest)))
    # self-contained (rows + vectors), so the app starts playing before the big catalog has downloaded
    import base64
    feed = {"generated": int(time.time()), "model": model.status, "cols": cols, "dim": PCA_DIM,
            "rows": [cat[i] for i in chosen], "vec": base64.b64encode(Zq[chosen].tobytes()).decode()}
    (site / "feed.json").write_text(json.dumps(feed, separators=(",", ":")), encoding="utf-8")
    size = lambda f: (site / f).stat().st_size / 1e6
    log.info("site: catalog %d videos (%.1f MB json + %.1f MB vectors), feed %d", len(rows), size("catalog.json"),
             size("catalog.bin"), len(chosen))


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    main()
