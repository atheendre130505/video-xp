"""Deep model: predict how much of a video you'll watch.

Features per video (stored / cached in SQLite):
  text      384-d embedding of "title | channel" (all-MiniLM-L6-v2, pretrained, runs on CPU)
  video     length (log + buckets), views, likes, like ratio, comments, age, and "is this known" flags
  you       how often you watch the channel (Takeout history), your average watch share on that channel here,
            similarity to what you recently kept watching / skipped, similarity to your history overall,
            whether you're subscribed to the channel (subscriptions.txt)

Target    engagement in 0..1 = watched / min(half the video, 4 min), capped at 1; 👍 = 1, 👎 = 0
Data      every video you were shown (weight 1, 👍/👎 2) + Takeout history as weak positives (0.1)
          + random unwatched candidates as weak negatives (fade out as real data grows)
Network   MLP 400 -> 256 -> 64 -> 1 (ReLU, dropout), BCE on soft targets, Adam. Retrains in a few seconds
          after new watch data; every candidate's prediction is written to videos.p_watch.
"""
import math, random, threading, time, logging
from collections import defaultdict
import numpy as np
import db

log = logging.getLogger("model")
EMB_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
EMB_DIM = 384
TOP_LOOKUPS = 40            # after each scoring pass: full lookup for this many top un-looked-up candidates
EXPLORE_LOOKUPS = 10        # plus a few random ones, so exploration has material
HISTORY_CAP = 3000
HISTORY_WEIGHT = 0.1        # Takeout history only nudges taste; what you keep watching here counts 10x more
PER_CATEGORY_LOOKUPS = 4    # every category keeps a few playable videos ready, so the feed can stay mixed

_st, _st_lock = None, threading.Lock()
_net, _net_lock = None, threading.Lock()
status = {"trained_on": 0, "last_train": None, "candidates": 0}


# ---------------- embeddings ----------------
def _encoder():
    global _st
    with _st_lock:
        if _st is None:
            from sentence_transformers import SentenceTransformer
            _st = SentenceTransformer(EMB_MODEL, device="cpu")
    return _st


def text_of(v):
    return f"{v.get('title') or ''} | {v.get('channel') or ''}"


def embeddings(items):
    """items: list of (key, text) -> {key: vec}; computes and stores missing ones."""
    keys = [k for k, _ in items]
    have = {}
    for i in range(0, len(keys), 900):
        chunk = keys[i:i + 900]
        for r in db.q(f"SELECT id, vec FROM emb WHERE id IN ({','.join('?' * len(chunk))})", chunk):
            # stored as float16 (half the size; plenty for similarity); older rows are float32
            dt = np.float16 if len(r["vec"]) == EMB_DIM * 2 else np.float32
            have[r["id"]] = np.frombuffer(r["vec"], dtype=dt).astype(np.float32)
    todo = [(k, t) for k, t in items if k not in have]
    if todo:
        vecs = _encoder().encode([t for _, t in todo], batch_size=128, normalize_embeddings=True,
                                 show_progress_bar=False).astype(np.float32)
        with db.conn() as c:
            c.executemany("INSERT OR REPLACE INTO emb VALUES (?,?)",
                          [(k, v.astype(np.float16).tobytes()) for (k, _), v in zip(todo, vecs)])
        have.update({k: v for (k, _), v in zip(todo, vecs)})
    return have


# ---------------- features ----------------
def video_numbers(v):
    d = (v.get("duration") or 0) / 60
    views, likes, cc = v.get("views"), v.get("likes"), v.get("comment_count")
    age = None
    if v.get("upload_date"):
        try:
            age = (time.time() - time.mktime(time.strptime(v["upload_date"], "%Y%m%d"))) / (365 * 86400)
        except Exception:
            pass
    return [
        math.log1p(d) / 5, float(d < 8), float(8 <= d < 20), float(20 <= d < 45), float(45 <= d < 90), float(d >= 90),
        math.log10((views or 0) + 1) / 8, float(views is not None),
        math.log10((likes or 0) + 1) / 7, min((likes or 0) / views / 0.05, 1.0) if views and likes else 0.0,
        float(likes is not None),
        math.log10((cc or 0) + 1) / 5, float(cc is not None),
        min(age, 20) / 20 if age is not None else 0.0, float(age is not None),
    ]


N_VIDEO = len(video_numbers({}))
N_USER = 7
IN_DIM = EMB_DIM + N_VIDEO + N_USER


class UserContext:
    """Everything about you the features need, computed once per scoring pass."""

    def __init__(self, emb_of):
        self.hist_chan = {r["channel"]: r["n"] for r in
                          db.q("SELECT channel, COUNT(*) n FROM history WHERE channel IS NOT NULL GROUP BY channel")}
        self.subs = db.subscribed_names()
        self.events = engagement_by_video()
        self.chan_eng = defaultdict(list)
        vids = list(self.events)
        rows = {r["id"]: r for r in db.q(
            f"SELECT id, channel FROM videos WHERE id IN ({','.join('?' * len(vids))})", vids)} if vids else {}
        for vid, (y, _) in self.events.items():
            ch = (rows.get(vid) or {}).get("channel")
            if ch:
                self.chan_eng[ch].append(y)
        recent = sorted(self.events.items(), key=lambda kv: -kv[1][1]["ts"])
        kept = [vid for vid, (y, _) in recent if y >= 0.6][:30]
        skipped = [vid for vid, (y, _) in recent if y <= 0.15][:30]
        self.kept = {vid: emb_of[vid] for vid in kept if vid in emb_of}
        self.skipped = {vid: emb_of[vid] for vid in skipped if vid in emb_of}
        hist_vecs = [emb_of[k] for k in emb_of if k.startswith("h:")]
        self.hist_mean = _unit(np.mean(hist_vecs, axis=0)) if hist_vecs else None

    def user_numbers(self, v, e, exclude=None):
        ch = v.get("channel")
        ys = list(self.chan_eng.get(ch, []))
        if exclude is not None and exclude in self.events and ys:
            ys.remove(self.events[exclude][0]) if self.events[exclude][0] in ys else None
        chan_eng = (sum(ys) + 0.3 * 2) / (len(ys) + 2)
        return [
            min(math.log1p(self.hist_chan.get(ch, 0)) / math.log(50), 1.0),
            chan_eng, min(len(ys) / 10, 1.0),
            _sim(e, self.kept, exclude), _sim(e, self.skipped, exclude),
            float(e @ self.hist_mean) if self.hist_mean is not None else 0.0,
            float(ch in self.subs),                                       # a channel you're subscribed to
        ]


def _unit(x):
    return x / (np.linalg.norm(x) or 1)


def _sim(e, group, exclude):
    vecs = [v for k, v in group.items() if k != exclude]
    return float(e @ _unit(np.mean(vecs, axis=0))) if vecs else 0.0


def engagement_by_video():
    """video_id -> (engagement 0..1, {weight, ts})"""
    agg = {}
    for e in db.events():
        a = agg.setdefault(e["video_id"], {"watched": 0.0, "duration": 0.0, "liked": None, "ts": 0})
        a["watched"] += e["watched"] or 0
        a["duration"] = max(a["duration"], e["duration"] or 0)
        a["ts"] = max(a["ts"], e["ts"] or 0)
        if e["liked"] is not None:
            a["liked"] = e["liked"]
    out = {}
    for vid, a in agg.items():
        if a["liked"] == 1:
            out[vid] = (1.0, {"w": 2.0, "ts": a["ts"]})
        elif a["liked"] == -1:
            out[vid] = (0.0, {"w": 2.0, "ts": a["ts"]})
        else:
            target = min(0.5 * (a["duration"] or 600), 240)
            out[vid] = (min(a["watched"] / target, 1.0), {"w": 1.0, "ts": a["ts"]})
    return out


# ---------------- network ----------------
def _build():
    import torch.nn as nn
    return nn.Sequential(nn.Linear(IN_DIM, 256), nn.ReLU(), nn.Dropout(0.2),
                         nn.Linear(256, 64), nn.ReLU(), nn.Dropout(0.1), nn.Linear(64, 1))


def train_and_score(request_lookups=True):
    """Retrain on everything known, then write p_watch for every candidate (and, locally, request lookups for the
    top ones; the cloud pipeline looks everything up anyway, so it passes request_lookups=False)."""
    import torch
    t0 = time.time()
    cand = db.q("""SELECT id, topic, title, channel, duration, views, likes, comment_count, upload_date, enriched
                   FROM videos WHERE embeddable=1 AND enriched != -1""")
    seen = {r["id"] for r in db.q("SELECT DISTINCT video_id id FROM history")} | set(engagement_by_video())
    hist = db.q("""SELECT video_id id, MAX(title) title, MAX(channel) channel FROM history
                   GROUP BY video_id ORDER BY MAX(ts) DESC LIMIT ?""", (HISTORY_CAP,))
    shown = db.q(f"""SELECT id, title, channel, duration, views, likes, comment_count, upload_date FROM videos
                     WHERE id IN (SELECT DISTINCT video_id FROM events)""")
    emb_of = embeddings([(r["id"], text_of(r)) for r in cand + shown] + [("h:" + h["id"], text_of(h)) for h in hist])
    U = UserContext(emb_of)

    # training rows
    X, Y, W = [], [], []
    ev = U.events
    for r in shown:
        if r["id"] in ev:
            y, meta = ev[r["id"]]
            e = emb_of[r["id"]]
            X.append(np.concatenate([e, video_numbers(r), U.user_numbers(r, e, exclude=r["id"])]))
            Y.append(y); W.append(meta["w"])
    n_real = len(X)
    for h in hist:                                                  # what you chose to watch on YouTube
        e = emb_of["h:" + h["id"]]
        X.append(np.concatenate([e, video_numbers({}), U.user_numbers(h, e)]))
        Y.append(0.85); W.append(HISTORY_WEIGHT)
    neg_w = 0.2 * max(0.2, 1 - n_real / 200)
    unseen = [r for r in cand if r["id"] not in seen]
    for r in random.Random(len(cand)).sample(unseen, min(len(unseen), max(len(hist), 300))):
        e = emb_of[r["id"]]                                         # random candidates: weak negatives
        # stats hidden like on history rows, so "history vs random" is learned from content + your affinities,
        # not from which rows happen to have view/like counts
        X.append(np.concatenate([e, video_numbers({}), U.user_numbers(r, e)]))
        Y.append(0.1); W.append(neg_w)
    if not X or not unseen:
        return
    Xt, Yt, Wt = (torch.tensor(np.array(a, dtype=np.float32)) for a in (X, Y, W))

    torch.manual_seed(0)
    net = _build()
    opt = torch.optim.Adam(net.parameters(), lr=1e-3, weight_decay=1e-4)
    lossf = torch.nn.BCEWithLogitsLoss(reduction="none")
    net.train()
    for _ in range(120):
        perm = torch.randperm(len(Xt))
        for i in range(0, len(Xt), 256):
            b = perm[i:i + 256]
            opt.zero_grad()
            loss = (lossf(net(Xt[b]).squeeze(1), Yt[b]) * Wt[b]).sum() / Wt[b].sum()
            loss.backward()
            opt.step()

    # score every unseen candidate, store in SQLite
    net.eval()
    feats = np.array([np.concatenate([emb_of[r["id"]], video_numbers(r), U.user_numbers(r, emb_of[r["id"]])])
                      for r in unseen], dtype=np.float32)
    with torch.no_grad():
        p = torch.sigmoid(net(torch.tensor(feats)).squeeze(1)).numpy()
    now = time.time()
    with db.conn() as c:
        c.executemany("UPDATE videos SET p_watch=?, scored_at=? WHERE id=?",
                      [(float(pi), now, r["id"]) for r, pi in zip(unseen, p)])
    global _net
    with _net_lock:
        _net = net
    # stage 2: full lookup only for the best candidates (+ a few random ones to explore)
    order = np.argsort(-p)
    top = [unseen[i]["id"] for i in order if unseen[i]["enriched"] == 0][:TOP_LOOKUPS]
    per_cat = defaultdict(list)
    for i in order:
        r = unseen[i]
        if r["enriched"] == 0 and len(per_cat[r["topic"]]) < PER_CATEGORY_LOOKUPS:
            per_cat[r["topic"]].append(r["id"])
    top += [v for ids in per_cat.values() for v in ids if v not in top]
    rest = [r["id"] for r in unseen if r["enriched"] == 0 and r["id"] not in top]
    if request_lookups:
        import fetcher
        fetcher.request_lookups(top + random.sample(rest, min(EXPLORE_LOOKUPS, len(rest))))
    status.update(trained_on=n_real, history=len(hist), candidates=len(unseen),
                  last_train=round(time.time() - t0, 1))
    log.info("trained on %d watched + %d history, scored %d candidates in %.1fs",
             n_real, len(hist), len(unseen), time.time() - t0)


# ---------------- background trainer ----------------
_dirty = threading.Event()


def mark_dirty():
    _dirty.set()


def start_trainer(min_gap=20, every=300):
    """Retrain soon after new watch data (and every few minutes as the pool grows)."""
    def loop():
        last = 0
        while True:
            _dirty.wait(every)
            _dirty.clear()
            time.sleep(max(0, min_gap - (time.time() - last)))
            try:
                train_and_score()
            except Exception as ex:
                log.exception("training failed: %s", ex)
            last = time.time()
    _dirty.set()
    threading.Thread(target=loop, daemon=True).start()
