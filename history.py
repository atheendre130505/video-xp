"""Import YouTube watch history from Google Takeout (watch-history.json or watch-history.html)."""
import html, json, re
import db

RE_ID = re.compile(r"[?&]v=([A-Za-z0-9_-]{11})")
RE_HTML = re.compile(
    r'Watched\s*(?:&nbsp;|\s)*<a href="(?P<url>[^"]+)">(?P<title>.*?)</a><br>\s*'
    r'(?:<a href="(?P<curl>[^"]+)">(?P<chan>.*?)</a><br>)?\s*(?P<ts>[^<]+)<br>', re.S)


def parse(raw: bytes):
    text = raw.decode("utf-8", errors="replace").lstrip("﻿")
    rows = []
    if text.lstrip().startswith("["):
        for e in json.loads(text):
            if any("Google Ads" in (d.get("name") or "") for d in e.get("details") or []):
                continue  # ads you "watched"
            m = RE_ID.search(e.get("titleUrl") or "")
            if not m:
                continue
            sub = (e.get("subtitles") or [{}])[0]
            title = re.sub(r"^Watched\s+", "", e.get("title") or "")
            rows.append((m.group(1), title, sub.get("name"), sub.get("url"), e.get("time")))
    else:
        for m in RE_HTML.finditer(text):
            idm = RE_ID.search(html.unescape(m["url"]))
            if idm:
                rows.append((idm.group(1), html.unescape(m["title"]), html.unescape(m["chan"] or ""),
                             m["curl"], m["ts"].strip()))
    return rows


def import_bytes(raw: bytes):
    rows = parse(raw)
    with db.conn() as c:
        c.executemany("INSERT OR IGNORE INTO history VALUES (?,?,?,?,?)", rows)
    return summary() | {"parsed": len(rows)}


def summary():
    s = db.q("""SELECT COUNT(*) watches, COUNT(DISTINCT video_id) videos, COUNT(DISTINCT channel) channels,
                MIN(ts) first, MAX(ts) last FROM history""")[0]
    s["top_channels"] = db.q("""SELECT channel, COUNT(*) n FROM history WHERE channel IS NOT NULL AND channel != ''
                                GROUP BY channel ORDER BY n DESC LIMIT 10""")
    return s
