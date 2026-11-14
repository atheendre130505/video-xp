"""YouTube Data API v3 client for the cloud pipeline (GitHub's servers get blocked by yt-dlp's scraping).

Free quota: 10,000 units/day.  Costs: playlistItems/channels/videos.list = 1 unit per call (50 items),
search.list = 100 units.  So: every candidate gets full stats (cheap); searches are rationed.
Needs env YT_API_KEY (a GitHub Actions secret).
"""
import json, os, re, time, logging, urllib.request, urllib.parse
import db

log = logging.getLogger("yt_api")
API = "https://www.googleapis.com/youtube/v3/"
units = {"used": 0}
DAILY_BUDGET = 8000                   # leave headroom under the 10k quota


class QuotaExceeded(Exception):
    pass


def _get(endpoint, cost=1, **params):
    if units["used"] + cost > DAILY_BUDGET:
        raise QuotaExceeded()
    params["key"] = os.environ["YT_API_KEY"]
    url = API + endpoint + "?" + urllib.parse.urlencode(params)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                units["used"] += cost
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            if e.code == 403 and "quota" in body.lower():
                raise QuotaExceeded()
            if e.code == 404:
                return None
            if attempt == 2:
                log.warning("%s failed: %s %s", endpoint, e.code, body[:200])
                return None
            time.sleep(3 * (attempt + 1))


def iso_duration(s):
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    if not m:
        return None
    d, h, mi, se = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + se


# ---------------- channels ----------------
def resolve_channel(url):
    """@handle or /channel/UC… URL -> (channel_id, title, uploads_playlist)."""
    tail = url.rstrip("/").split("youtube.com/")[-1]
    if tail.startswith("channel/"):
        d = _get("channels", part="snippet,contentDetails", id=tail.split("/")[1])
    else:
        d = _get("channels", part="snippet,contentDetails", forHandle=tail.lstrip("@"))
    items = (d or {}).get("items") or []
    if not items:
        return None
    c = items[0]
    return c["id"], c["snippet"]["title"], c["contentDetails"]["relatedPlaylists"]["uploads"]


def playlist_video_ids(playlist_id, max_items):
    ids, token = [], None
    while len(ids) < max_items:
        d = _get("playlistItems", part="contentDetails", playlistId=playlist_id, maxResults=50,
                 **({"pageToken": token} if token else {}))
        if not d:
            break
        ids += [it["contentDetails"]["videoId"] for it in d.get("items", [])]
        token = d.get("nextPageToken")
        if not token:
            break
    return ids[:max_items]


# ---------------- videos ----------------
def video_details(ids):
    """Full stats for videos (50 per unit). Returns list of row dicts ready for the videos table."""
    out = []
    for i in range(0, len(ids), 50):
        d = _get("videos", part="snippet,contentDetails,statistics,status", id=",".join(ids[i:i + 50]),
                 maxResults=50)
        for v in (d or {}).get("items", []):
            sn, st, cd, stat = v["snippet"], v.get("statistics", {}), v["contentDetails"], v.get("status", {})
            out.append({
                "id": v["id"], "title": sn.get("title"), "channel": sn.get("channelTitle"),
                "channel_url": f"https://www.youtube.com/channel/{sn.get('channelId')}",
                "duration": iso_duration(cd.get("duration")),
                "views": int(st["viewCount"]) if "viewCount" in st else None,
                "likes": int(st["likeCount"]) if "likeCount" in st else None,
                "comment_count": int(st["commentCount"]) if "commentCount" in st else None,
                "upload_date": (sn.get("publishedAt") or "")[:10].replace("-", ""),
                "description": (sn.get("description") or "")[:3000], "tags": json.dumps(sn.get("tags") or []),
                "embeddable": int(bool(stat.get("embeddable", True)) and sn.get("liveBroadcastContent") == "none"
                                  and stat.get("privacyStatus") == "public"),
            })
    return out


def search(query, max_results=50):
    d = _get("search", cost=100, part="id", type="video", q=query, maxResults=max_results,
             videoDuration="medium", relevanceLanguage="en", safeSearch="moderate")
    ids = [it["id"]["videoId"] for it in (d or {}).get("items", [])]
    d2 = _get("search", cost=100, part="id", type="video", q=query, maxResults=max_results, videoDuration="long",
              relevanceLanguage="en", safeSearch="moderate") if len(ids) < max_results else None
    return ids + [it["id"]["videoId"] for it in (d2 or {}).get("items", [])]


# ---------------- storing ----------------
def store(rows, topic, source, min_dur=180, max_dur=4 * 3600, curriculum=None):
    """Insert/refresh full-detail rows (already looked up) as candidates."""
    now = time.time()
    kept = 0
    with db.conn() as c:
        for r in rows:
            ok = r["duration"] and min_dur <= r["duration"] <= max_dur and r["embeddable"] \
                 and not (curriculum and curriculum.search(r["title"] or ""))
            c.execute("""INSERT INTO videos (id, topic, title, channel, channel_url, duration, views, likes, comment_count,
                         upload_date, description, tags, embeddable, enriched, enriched_at, source, added_at)
                         VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?)
                         ON CONFLICT(id) DO UPDATE SET views=excluded.views, likes=excluded.likes,
                         comment_count=excluded.comment_count, embeddable=excluded.embeddable, enriched=1,
                         enriched_at=excluded.enriched_at""",
                      (r["id"], topic, r["title"], r["channel"], r["channel_url"], r["duration"], r["views"], r["likes"],
                       r["comment_count"], r["upload_date"], r["description"], r["tags"], int(bool(ok)), now, source, now))
            kept += bool(ok)
    return kept
