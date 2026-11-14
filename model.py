"""Model: predict whether you'd call a video good.

Signal    only your reactions: good / not good / moved on within 10 seconds (soft dislike).
          Watch time and your old YouTube history do NOT shape taste (history only prevents repeats).
Features  384-d embedding of "title | channel" (all-MiniLM-L6-v2, runs on CPU), video numbers (length, views, …),
          and you: your reactions on that channel, similarity to what you called good / not good, subscribed or not.
Network   MLP -> 256 -> 64 -> 1, BCE, Adam; trained from scratch each run once there are 20+ reactions.
          Before that a simple prior ranks (subscriptions + essentials + popularity).
          Every unseen video's score is written to videos.p_watch.
"""
import math, random, threading, time, logging
from collections import defaultdict
import numpy as np
import db

log = logging.getLogger("model")
EMB_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
EMB_DIM = 384
MIN_REACTIONS = 20          # below this the network isn't trained; a simple prior ranks instead

_st, _st_lock = None, threading.Lock()
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
N_USER = 5
IN_DIM = EMB_DIM + N_VIDEO + N_USER


class UserContext:
    """Everything about you the features need, computed once per scoring pass."""

    def __init__(self, emb_of):
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
        kept = [vid for vid, (y, _) in recent if y >= 0.6][:40]
        skipped = [vid for vid, (y, _) in recent if y <= 0.15][:40]
        self.kept = {vid: emb_of[vid] for vid in kept if vid in emb_of}
        self.skipped = {vid: emb_of[vid] for vid in skipped if vid in emb_of}

    def user_numbers(self, v, e, exclude=None):
        ch = v.get("channel")
        ys = list(self.chan_eng.get(ch, []))
        if exclude is not None and exclude in self.events and ys:
            ys.remove(self.events[exclude][0]) if self.events[exclude][0] in ys else None
        chan_eng = (sum(ys) + 0.3 * 2) / (len(ys) + 2)
        return [
            chan_eng, min(len(ys) / 10, 1.0),
            _sim(e, self.kept, exclude), _sim(e, self.skipped, exclude),
            float(ch in self.subs),                                       # a channel you're subscribed to
        ]


def _unit(x):
    return x / (np.linalg.norm(x) or 1)


def _sim(e, group, exclude):
    vecs = [v for k, v in group.items() if k != exclude]
    return float(e @ _unit(np.mean(vecs, axis=0))) if vecs else 0.0


def engagement_by_video():
    """video_id -> (label, {w, ts}). Only your reactions count:
         good = 1 (weight 2) · not good = 0 (weight 2) · moved on within 10 s = soft dislike, 0 (weight 1).
       Videos you haven't reacted to carry no label (they stay "in progress")."""
    agg = {}
    for e in db.events():                       # oldest first, so the latest reaction wins
        a = agg.setdefault(e["video_id"], {"liked": None, "soft": 0, "ts": 0})
        a["ts"] = max(a["ts"], e["ts"] or 0)
        if e["liked"] is not None:
            a["liked"], a["soft"] = e["liked"], e.get("soft") or 0
    out = {}
    for vid, a in agg.items():
        if a["liked"] == 1:
            out[vid] = (1.0, {"w": 2.0, "ts": a["ts"]})
        elif a["liked"] == -1:
            out[vid] = (0.0, {"w": 1.0 if a["soft"] else 2.0, "ts": a["ts"], "soft": bool(a["soft"])})
    return out


def prior(r, subs):
    """Before there are enough reactions: your subscriptions and the essentials list first, then popularity."""
    pop = min(math.log10((r.get("views") or 0) + 1) / 8, 1.0)
    return 0.3 + 0.25 * (r.get("channel") in subs) + 0.2 * (r.get("source") == "essential") + 0.15 * pop


# ---------------- network ----------------
def _build():
    import torch.nn as nn
    return nn.Sequential(nn.Linear(IN_DIM, 256), nn.ReLU(), nn.Dropout(0.2),
                         nn.Linear(256, 64), nn.ReLU(), nn.Dropout(0.1), nn.Linear(64, 1))


def train_and_score():
    """Retrain on your reactions, then write a score (videos.p_watch) for every video you haven't seen."""
    import torch
    t0 = time.time()
    cand = db.q("""SELECT id, topic, source, title, channel, duration, views, likes, comment_count, upload_date
                   FROM videos WHERE embeddable=1 AND enriched != -1 AND title IS NOT NULL""")
    seen = {r["id"] for r in db.q("SELECT DISTINCT video_id id FROM history UNION SELECT DISTINCT video_id FROM events")}
    shown = db.q("""SELECT id, title, channel, duration, views, likes, comment_count, upload_date FROM videos
                    WHERE id IN (SELECT DISTINCT video_id FROM events) AND title IS NOT NULL""")
    emb_of = embeddings([(r["id"], text_of(r)) for r in cand + shown])
    U = UserContext(emb_of)
    unseen = [r for r in cand if r["id"] not in seen]
    if not unseen:
        return
    heur = np.array([prior(r, U.subs) for r in unseen], dtype=np.float32)

    X, Y, W = [], [], []
    for r in shown:
        if r["id"] in U.events:
            y, meta = U.events[r["id"]]
            e = emb_of[r["id"]]
            X.append(np.concatenate([e, video_numbers(r), U.user_numbers(r, e, exclude=r["id"])]))
            Y.append(y); W.append(meta["w"])
    n_real = len(X)
    if n_real >= MIN_REACTIONS and 0 < sum(Y) < n_real:
        for r in random.Random(len(cand)).sample(unseen, min(len(unseen), 300)):
            e = emb_of[r["id"]]                                     # unrated videos: weak "probably average"
            X.append(np.concatenate([e, video_numbers(r), U.user_numbers(r, e)]))
            Y.append(0.35); W.append(0.05)
        Xt, Yt, Wt = (torch.tensor(np.array(a, dtype=np.float32)) for a in (X, Y, W))
        torch.manual_seed(0)
        net = _build()
        opt = torch.optim.Adam(net.parameters(), lr=1e-3, weight_decay=1e-3)
        lossf = torch.nn.BCEWithLogitsLoss(reduction="none")
        net.train()
        for _ in range(150):
            perm = torch.randperm(len(Xt))
            for i in range(0, len(Xt), 256):
                b = perm[i:i + 256]
                opt.zero_grad()
                loss = (lossf(net(Xt[b]).squeeze(1), Yt[b]) * Wt[b]).sum() / Wt[b].sum()
                loss.backward()
                opt.step()
        net.eval()
        feats = np.array([np.concatenate([emb_of[r["id"]], video_numbers(r), U.user_numbers(r, emb_of[r["id"]])])
                          for r in unseen], dtype=np.float32)
        with torch.no_grad():
            p = torch.sigmoid(net(torch.tensor(feats)).squeeze(1)).numpy()
        trust = min(0.85, n_real / 150 + 0.4)                       # lean on the network more as reactions accumulate
        p = trust * p + (1 - trust) * heur
        mode = "network"
    else:
        p, mode = heur, "starting out (subscriptions + essentials)"
    now = time.time()
    with db.conn() as c:
        c.executemany("UPDATE videos SET p_watch=?, scored_at=? WHERE id=?",
                      [(float(pi), now, r["id"]) for r, pi in zip(unseen, p)])
    status.update(trained_on=n_real, candidates=len(unseen), mode=mode, last_train=round(time.time() - t0, 1))
    log.info("%s: %d reactions, scored %d candidates in %.1fs", mode, n_real, len(unseen), time.time() - t0)
