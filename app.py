import logging, socket
from flask import Flask, jsonify, request, send_from_directory
import db, fetcher, rank, history, model

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
logging.getLogger("werkzeug").setLevel(logging.WARNING)
app = Flask(__name__, static_folder="static")
app.config["MAX_CONTENT_LENGTH"] = 300 * 1024 * 1024
PORT = 5055


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.get("/api/reel")
def reel():
    exclude = [x for x in request.args.get("exclude", "").split(",") if x]
    recent = [tuple(p.split("~", 1)) for p in request.args.get("recent", "").split("|") if "~" in p]
    items, meta = rank.next_videos(exclude, n=int(request.args.get("n", 2)), recent=recent)
    return jsonify({"items": items, "meta": meta})


@app.post("/api/event")
def event():
    e = request.get_json(force=True)
    watched, dur = float(e.get("watched") or 0), float(e.get("duration") or 0)
    db.add_event(e["id"], watched, dur, e.get("liked"))
    if e.get("liked") == 1 or (dur and watched >= min(0.5 * dur, 240)):
        fetcher.expand_from(e["id"])          # kept watching: more like it, more from that channel
    model.mark_dirty()                        # retrain with the new data point
    return jsonify(ok=True)


@app.post("/api/unplayable")
def unplayable():
    db.mark(request.get_json(force=True)["id"], embeddable=0)
    return jsonify(ok=True)


@app.post("/api/history")
def import_history():
    f = request.files.get("file")
    if not f:
        return jsonify(error="no file"), 400
    r = history.import_bytes(f.read())
    fetcher.seed()
    model.mark_dirty()
    return jsonify(r)


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))          # no packet is sent; just picks the Wi-Fi interface
        return s.getsockname()[0]
    except Exception:
        return None
    finally:
        s.close()


if __name__ == "__main__":
    db.init()
    fetcher.on_new_candidates = model.mark_dirty     # new candidates -> rescore (batched: at most every 20 s)
    fetcher.start_workers()
    fetcher.seed()            # cheap candidate pages: category searches + your top history channels
    model.start_trainer()     # trains, scores every candidate, requests full lookups for the top ones
    ip = lan_ip()
    print(f"\n  Video XP on this computer:  http://localhost:{PORT}")
    if ip:
        print(f"  On your phone (same Wi-Fi): http://{ip}:{PORT}\n")
    app.run(host="0.0.0.0", port=PORT, threaded=True)   # 0.0.0.0 = reachable from devices on your Wi-Fi
