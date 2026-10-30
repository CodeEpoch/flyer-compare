"""Tiny local web app: serves the deals page plus a settings/refresh API.

Listens on 127.0.0.1 only and exits by itself after IDLE_MINUTES without the
page open, so nothing keeps running in the background.
"""

import json
import re
import socket
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import autorun
import customflyer
import grocery
import locate

PORT = 5178
URL = f"http://127.0.0.1:{PORT}/"
IDLE_MINUTES = 30
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/data/catalog.js": ("data/catalog.js", "text/javascript; charset=utf-8"),
    "/stores.html": ("stores.html", "text/html; charset=utf-8"),
    "/i18n.js": ("i18n.js", "text/javascript; charset=utf-8"),
    "/flyer.html": ("flyer.html", "text/html; charset=utf-8"),
}

job = {"running": False, "message": "", "error": "", "finished_at": None}
last_seen = time.time()
lock = threading.Lock()
rebuild_pending = False


def run_job(fn):
    """Run one background job at a time (refresh / address change)."""
    with lock:
        if job["running"]:
            return False
        job.update(running=True, message="Starting...", error="")

    def work():
        global rebuild_pending
        try:
            fn()
            job["message"] = "Done"
        except Exception as e:
            job["error"] = str(e)
            grocery.log(f"Error: {e}")
        finally:
            with lock:
                again, rebuild_pending = rebuild_pending, False
                job.update(running=False, finished_at=time.time())
            if again:
                request_rebuild()

    threading.Thread(target=work, daemon=True).start()
    return True


def request_rebuild():
    """Rebuild the catalog after a quick settings change. Never refused: if
    something is already running, the rebuild runs right after it."""
    global rebuild_pending
    if not run_job(lambda: grocery.build_catalog(grocery.load_config())):
        rebuild_pending = True


grocery.on_log = lambda msg: job.__setitem__("message", msg)


def public_settings(cfg):
    keys = ("address", "postal_code", "weekly_refresh", "refresh_day", "refresh_time",
            "run_on_startup", "open_page_on_startup", "store_search_km", "ocr_engine", "exclude_stores")
    out = {k: cfg[k] for k in keys}
    state = grocery.read_json(grocery.STATE, {})
    out["last_refresh"] = state.get("last_refresh")
    out["custom_flyers"] = [{"url": e["url"], "store": e["store"],
                             "address": cfg["store_overrides"].get(e["store"], {}).get("address", ""),
                             "status": state.get("custom", {}).get(e["url"])} for e in cfg["custom_flyers"]]
    ok, why = customflyer.claude_available(cfg)
    out["claude"] = {"ready": ok, "why": why, "key_saved": bool(cfg["anthropic_api_key"])}  # never send the key back
    out["paddle_ready"] = customflyer.paddle_available()
    try:
        out["installed"] = autorun.status()
    except Exception as e:
        out["installed"] = {"error": str(e)}
    return out


def validate(body, cfg):
    new = dict(cfg)
    for k in ("weekly_refresh", "run_on_startup", "open_page_on_startup"):
        if k in body:
            new[k] = bool(body[k])
    if "refresh_day" in body:
        if body["refresh_day"] not in grocery.DAYS:
            raise ValueError("Pick a day of the week.")
        new["refresh_day"] = body["refresh_day"]
    if "refresh_time" in body:
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(body["refresh_time"])):
            raise ValueError("Time must look like 08:00.")
        new["refresh_time"] = body["refresh_time"]
    if "ocr_engine" in body:
        if body["ocr_engine"] not in ("auto", "paddle", "claude"):
            raise ValueError("Unknown flyer reader.")
        new["ocr_engine"] = body["ocr_engine"]
    if body.get("anthropic_api_key"):  # blank = keep the saved key
        key = str(body["anthropic_api_key"]).strip()
        new["anthropic_api_key"] = "" if key == "-" else key[:300]
    return new


def change_custom(body):
    """Add / remove / re-read a flyer link. Returns an error message or None."""
    cfg = grocery.load_config()
    action, url = body.get("action"), str(body.get("url") or "").strip()[:500]
    if action == "add":
        if not re.match(r"https?://[^\s/]+\.[^\s]+$", url):
            return "Enter a full web address starting with http:// or https://."
        if any(e["url"] == url for e in cfg["custom_flyers"]):
            return "That flyer link is already added."
        store = str(body.get("store") or "").strip()[:80] or urllib.parse.urlparse(url).hostname.removeprefix("www.")
        address = str(body.get("address") or "").strip()[:300]

        def add():
            c = grocery.load_config()
            if address:
                spot = locate.geocode(address)
                c["store_overrides"][store] = {"address": address, "lat": spot["lat"], "lon": spot["lon"],
                                               "found": spot["display"]}
            c["custom_flyers"].append({"url": url, "store": store})
            grocery.save_config(c)
            grocery.sync_custom(c, only_url=url)
            grocery.build_catalog(c)
        run_job(add)
    elif action == "remove":
        cfg["custom_flyers"] = [e for e in cfg["custom_flyers"] if e["url"] != url]
        grocery.save_config(cfg)
        run_job(lambda: (grocery.sync_custom(cfg, only_url="-"), grocery.build_catalog(cfg)))
    elif action == "reread":
        run_job(lambda: (grocery.sync_custom(cfg, only_url=url), grocery.build_catalog(cfg)))
    else:
        return "Unknown action."
    return None


def change_exclusion(body):
    cfg = grocery.load_config()
    store = str(body.get("store") or "").strip()[:120]
    if not store:
        return "No store given."
    with lock:  # several quick clicks must not overwrite each other's saves
        cfg = grocery.load_config()
        others = [s for s in cfg["exclude_stores"] if s.lower() != store.lower()]
        cfg["exclude_stores"] = others + [store] if body.get("exclude") else others
        grocery.save_config(cfg)
    if body.get("exclude") or not run_job(lambda: grocery.refresh(grocery.load_config())):
        request_rebuild()  # restoring re-fetches its flyer when possible; otherwise the cached copy is used
    return None


def set_store_address(body):
    """Pin a store's location with its real address (beats the OpenStreetMap guess)."""
    store = str(body.get("store") or "").strip()[:120]
    address = str(body.get("address") or "").strip()[:300]
    if not store:
        return "No store given."
    spot = None
    if address:
        try:
            spot = locate.geocode(address)
        except Exception as e:
            return str(e)
    with lock:
        cfg = grocery.load_config()
        if spot:
            cfg["store_overrides"][store] = {"address": address, "lat": spot["lat"], "lon": spot["lon"],
                                             "found": spot["display"]}
        else:
            cfg["store_overrides"].pop(store, None)
        grocery.save_config(cfg)
    request_rebuild()
    return None


IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
               ".gif": "image/gif", ".bmp": "image/bmp"}


def custom_store(name):
    """Only stores you added a flyer link for can be edited (and their files served)."""
    return next((e["store"] for e in grocery.load_config()["custom_flyers"] if e["store"] == name), None)


def editor_action(path, body):
    """Box editor API. Returns (status, response)."""
    store = custom_store(str(body.get("store") or ""))
    if not store:
        return 404, {"error": "Unknown store."}
    digest = str(body.get("digest") or "")
    if path == "/api/editor/prepare":  # make sure each page has OCR words to read boxes from
        if not run_job(lambda: customflyer.ensure_words(store, grocery.log)):
            return 409, {"error": "Busy with another update; try again in a moment."}
        return 202, {"ok": True}
    if path == "/api/editor/parse":  # read several panels at once: {"boxes": [[x, y, w, h], ...]}
        words = customflyer.load_words(store, digest)
        boxes = body.get("boxes")
        if not words:
            return 400, {"error": "No text found for this page yet."}
        if not isinstance(boxes, list) or len(boxes) > 500 or any(not (isinstance(b, list) and len(b) == 4) for b in boxes):
            return 400, {"error": "Bad request"}
        return 200, {"fields": [customflyer.parse_box(words["meta"], words["words"], [float(v) for v in b]) for b in boxes]}
    if path == "/api/editor/area":  # OCR one area you drew (cropped + enlarged) and grid it: ~10-30 s
        rect = body.get("rect")
        if not (isinstance(rect, list) and len(rect) == 4 and all(isinstance(v, (int, float)) for v in rect)) \
                or rect[2] < 20 or rect[3] < 20:
            return 400, {"error": "Draw a bigger area."}
        try:
            return 200, {"container": customflyer.read_area(store, digest, rect, grocery.log)}
        except (ValueError, RuntimeError) as e:
            return 400, {"error": str(e)}
    if path == "/api/editor/sharpen":  # OCR one panel, enlarged, for text the reading missed: ~10 s
        box = body.get("box")
        if not (isinstance(box, list) and len(box) == 4 and all(isinstance(v, (int, float)) for v in box)) \
                or box[2] < 8 or box[3] < 8:
            return 400, {"error": "Bad request"}
        try:
            fields = customflyer.sharpen_panel(store, digest, box, grocery.log)
        except (ValueError, RuntimeError) as e:
            return 400, {"error": str(e)}
        return 200, {"fields": fields}
    if path == "/api/editor/save":
        try:
            n = customflyer.save_manual(store, digest, body.get("layout"))
        except ValueError as e:
            return 400, {"error": str(e)}
    elif path == "/api/editor/reset":
        customflyer.reset_manual(store, digest)
        n = None
    else:
        return 404, {"error": "Not found"}
    total = grocery.reassemble_custom(store)
    request_rebuild()
    return 200, {"saved": n, "total": total, "state": customflyer.editor_state(store)}


def stores_overview():
    """Every store the app knows about, for the Stores page."""
    cfg = grocery.load_config()
    catalog = grocery.read_json(grocery.DATA / "catalog.json", {"groups": [], "store_info": {}})
    state = grocery.read_json(grocery.STATE, {}).get("custom", {})
    counts, flyer = {}, {}
    for g in catalog["groups"]:
        for o in g["offers"]:
            counts[o["store"]] = counts.get(o["store"], 0) + 1
            flyer.setdefault(o["store"], {"url": o["flyer_url"], "valid_to": o["valid_to"]})
    custom = {e["store"]: e for e in cfg["custom_flyers"]}
    excluded = {s.lower() for s in cfg["exclude_stores"]}
    names = set(catalog.get("stores", [])) | set(cfg["exclude_stores"]) | set(custom)
    rows = []
    for s in sorted(names, key=str.lower):
        c = custom.get(s)
        rows.append({
            "store": s, "items": counts.get(s, 0), "excluded": s.lower() in excluded,
            "info": catalog["store_info"].get(s), "override": cfg["store_overrides"].get(s),
            "flyer": flyer.get(s), "source": "link" if c else "flipp",
            "link": c["url"] if c else None, "status": state.get(c["url"]) if c else None,
        })
    return {"home": catalog.get("home"), "stores": rows, "job": job}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep quiet
        pass

    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def trusted(self):
        # Only our own page: blocks other websites (DNS rebinding / cross-site posts)
        host_ok = self.headers.get("Host") in (f"127.0.0.1:{PORT}", f"localhost:{PORT}")
        return host_ok and self.headers.get("X-Flyer-Deals") == "1"

    def do_GET(self):
        global last_seen
        last_seen = time.time()
        path = self.path.split("?")[0]
        if path in STATIC:
            file, ctype = STATIC[path]
            f = grocery.ROOT / file
            return self.send(200, f.read_bytes(), ctype) if f.exists() else self.send(404, b"", "text/plain")
        if path == "/api/status":
            return self.send(200, job)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if path == "/api/flyer-image":  # a downloaded flyer page, for the box editor
            store = custom_store(query.get("store", [""])[0])
            cur = customflyer._load(customflyer.folder_for(store) / "current.json", None) if store else None
            page = next((p for p in (cur or {}).get("pages", []) if str(p["n"]) == query.get("n", [""])[0]), None)
            f = customflyer.folder_for(store) / page["file"] if page else None
            if not f or not f.exists() or f.suffix.lower() not in IMAGE_TYPES:
                return self.send(404, b"", "text/plain")
            return self.send(200, f.read_bytes(), IMAGE_TYPES[f.suffix.lower()])
        if path == "/api/editor/words":  # a page's OCR words, so the editor can show what's in a panel
            if not self.trusted():
                return self.send(403, {"error": "forbidden"})
            store = custom_store(query.get("store", [""])[0])
            words = customflyer.load_words(store, query.get("digest", [""])[0]) if store else None
            return self.send(200, {"words": (words or {}).get("words", [])})
        if path == "/api/editor":
            if not self.trusted():
                return self.send(403, {"error": "forbidden"})
            store = custom_store(query.get("store", [""])[0])
            state = customflyer.editor_state(store) if store else None
            return self.send(200, state) if state else self.send(404, {"error": "Read this flyer first."})
        if path in ("/api/settings", "/api/stores"):
            if not self.trusted():
                return self.send(403, {"error": "forbidden"})
            if path == "/api/stores":
                return self.send(200, stores_overview())
            return self.send(200, public_settings(grocery.load_config()))
        self.send(404, b"Not found", "text/plain")

    def do_POST(self):
        global last_seen
        last_seen = time.time()
        if not self.trusted():
            return self.send(403, {"error": "forbidden"})
        try:
            length = min(int(self.headers.get("Content-Length") or 0), 500_000)  # a page of boxes can be ~60 KB
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self.send(400, {"error": "Bad request"})
        path = self.path.split("?")[0]

        if path == "/api/ping":
            return self.send(200, {"ok": True})

        if path == "/api/refresh":
            cfg = grocery.load_config()
            if not cfg["postal_code"]:
                return self.send(400, {"error": "Enter your address first."})
            started = run_job(lambda: grocery.refresh(cfg))
            return self.send(202 if started else 409, job)

        if path.startswith("/api/editor/"):
            code, out = editor_action(path, body)
            return self.send(code, out)

        if path == "/api/custom" and job["running"]:
            return self.send(409, {"error": "Busy with another update; try again in a moment."})
        if path in ("/api/custom", "/api/exclude", "/api/store"):
            err = {"/api/custom": change_custom, "/api/exclude": change_exclusion,
                   "/api/store": set_store_address}[path](body)
            if err:
                return self.send(400, {"error": err})
            if path == "/api/custom":
                return self.send(200, public_settings(grocery.load_config()))
            return self.send(200, stores_overview())

        if path == "/api/settings":
            cfg = grocery.load_config()
            try:
                new = validate(body, cfg)
                grocery.save_config(new)
                autorun.apply(new)
            except (ValueError, RuntimeError) as e:
                return self.send(400, {"error": str(e)})
            address = str(body.get("address") or "").strip()[:300]
            if address and address != cfg["address"]:
                def relocate():
                    c = grocery.load_config()
                    if grocery.update_location(c, address):
                        grocery.refresh(c)   # new area: new flyers
                    else:
                        grocery.build_catalog(c)  # same flyers, new distances
                run_job(relocate)
            return self.send(200, public_settings(grocery.load_config()))

        self.send(404, {"error": "Not found"})


def already_running():
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def main(open_browser=True):
    if already_running():
        if open_browser:
            webbrowser.open(URL)
        return
    grocery.DATA.mkdir(exist_ok=True)
    if not (grocery.DATA / "catalog.js").exists():
        grocery.build_catalog(grocery.load_config())
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)

    def idle_watch():
        while True:
            time.sleep(30)
            if not job["running"] and time.time() - last_seen > IDLE_MINUTES * 60:
                httpd.shutdown()
                return

    threading.Thread(target=idle_watch, daemon=True).start()
    if open_browser:
        webbrowser.open(URL)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
