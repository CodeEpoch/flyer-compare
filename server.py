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
        if body["ocr_engine"] not in ("auto", "paddle", "windows", "claude"):
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
            length = min(int(self.headers.get("Content-Length") or 0), 10_000)
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
