"""Pick what plays next: highest predicted watch share (videos.p_watch from model.py), kept mixed.

Exploration: early on ~half the picks try the category you've seen least (philosophy, finance, …); this decays
to 10% as you watch more. Exploiting: best prediction, but not the same channel twice in a row, and not the same
category three times in a row unless that video is clearly better (20%+) than the best alternative.
"""
import random
from collections import Counter
import db, model

POOL_SQL = """SELECT id, topic, title, channel, duration, views, likes, p_watch FROM videos
              WHERE enriched=1 AND embeddable=1 AND p_watch IS NOT NULL
              AND id NOT IN (SELECT video_id FROM events) AND id NOT IN (SELECT video_id FROM history)
              ORDER BY p_watch DESC LIMIT 600"""


def explore_rate(n_watched):
    return max(0.10, 0.5 * 0.93 ** n_watched)


def next_videos(exclude=(), n=2, recent=()):
    """recent: (channel, topic) of the videos just shown, newest last."""
    exclude = set(exclude)
    pool = [r for r in db.q(POOL_SQL) if r["id"] not in exclude]
    shown = Counter(r["topic"] for r in db.q(
        "SELECT v.topic FROM events e JOIN videos v ON v.id = e.video_id GROUP BY e.video_id"))
    n_watched = sum(shown.values())
    eps = explore_rate(n_watched)
    recent, rng, picks = list(recent), random.Random(), []
    while pool and len(picks) < n:
        last_ch = recent[-1][0] if recent else None
        streak_topic = recent[-1][1] if len(recent) >= 2 and recent[-1][1] == recent[-2][1] else None
        if rng.random() < eps:
            cats = {r["topic"] for r in pool}
            cat = min(cats, key=lambda c: shown[c] + rng.random())
            v = next(r for r in pool if r["topic"] == cat)               # best video in the least-seen category
            v["why"] = "explore"
        else:
            ok = [r for r in pool if r["channel"] != last_ch] or pool
            v = ok[0]
            if streak_topic and v["topic"] == streak_topic:
                alt = next((r for r in ok if r["topic"] != streak_topic), None)
                if alt and v["p_watch"] < 1.2 * alt["p_watch"]:
                    v = alt
            v["why"] = "exploit"
        pool.remove(v)
        shown[v["topic"]] += 1
        recent.append((v["channel"], v["topic"]))
        picks.append(v)
    return picks, {"explore": round(eps, 2), "watched": n_watched, "model": model.status}
