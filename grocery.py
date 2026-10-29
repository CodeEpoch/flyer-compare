"""Grocery flyer comparer.

Pulls this week's grocery flyers for nearby stores (via Flipp's public flyer
JSON), caches them under data/, groups similar items across stores, and writes
data/catalog.json + data/catalog.js for index.html to render.

Usage:
    py grocery.py                 fetch new flyers, rebuild catalog
    py grocery.py --offline       rebuild catalog from cache only
    pyw grocery.py --auto         scheduled weekly run (refreshes if this week's data is missing)
    pyw grocery.py --auto startup Windows start-up run (honours the start-up settings)
    pyw FlyerDeals.pyw            open the app (local page + settings)

Standard library only.
"""

import json
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import categories
import customflyer
import locate

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
FLYERS = DATA / "flyers"
CONFIG = ROOT / "config.json"
STATE = DATA / "state.json"
LOCATION = DATA / "location.json"
API = "https://backflipp.wishabi.com/flipp"
UA = {"User-Agent": "Mozilla/5.0 (personal grocery comparer)"}
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

DEFAULT_CONFIG = {
    "address": "",
    "postal_code": "",
    "locale": "en",
    "flyer_categories": ["Groceries"],
    "stores": [],              # empty = every store with a matching flyer
    "exclude_stores": [],
    "include_upcoming_flyers": False,
    "match_threshold": 0.5,
    "workers": 6,
    "store_search_km": 25,     # how far to look for store branches (distance filter max)
    "weekly_refresh": True,
    "refresh_day": "Thursday", # most Canadian flyers start Thursday
    "refresh_time": "08:00",
    "run_on_startup": False,   # at login: refresh if this week's flyers are missing
    "open_page_on_startup": False,
    "custom_flyers": [],       # [{url, store}] stores not on Flipp
    "store_overrides": {},     # {store: {address, lat, lon, found}} your store's real location
    "ocr_engine": "auto",      # auto (Claude if an API key is set, else Windows OCR) | windows | claude
    "claude_model": "claude-opus-5-5",
    "anthropic_api_key": "",   # optional; ANTHROPIC_API_KEY env var works too
}

on_log = None  # set by the server to show progress in the page


# ---------------------------------------------------------------- utilities

def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    try:
        print(line, flush=True)
    except Exception:  # pyw has no console
        pass
    with open(DATA / "last_run.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")
    if on_log:
        on_log(msg)


def read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def get_json(url, retries=3):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)
        except Exception as e:
            if attempt == retries - 1:
                raise
            time.sleep(1.5 * (attempt + 1))


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    saved = read_json(CONFIG, {})
    if "open_browser_on_startup" in saved:  # renamed setting
        saved.setdefault("open_page_on_startup", saved.pop("open_browser_on_startup"))
    cfg.update(saved)
    cfg["postal_code"] = cfg["postal_code"].replace(" ", "").upper()
    return cfg


def save_config(cfg):
    write_json(CONFIG, {k: cfg[k] for k in DEFAULT_CONFIG})


def parse_dt(s):
    return datetime.fromisoformat(s) if s else None


# ---------------------------------------------------------------- fetching

def is_excluded(merchant, cfg):
    return merchant.strip().lower() in {x.strip().lower() for x in cfg["exclude_stores"]}


def store_matches(merchant, cfg):
    if is_excluded(merchant, cfg):
        return False
    m = merchant.lower()
    return not cfg["stores"] or any(x.lower() in m for x in cfg["stores"])


def list_flyers(cfg):
    q = urllib.parse.urlencode({"locale": cfg["locale"], "postal_code": cfg["postal_code"]})
    flyers = get_json(f"{API}/flyers?{q}")["flyers"]
    now = datetime.now(timezone.utc)
    cats = {c.lower() for c in cfg["flyer_categories"]}
    keep = []
    for f in flyers:
        if cats and not cats & {c.lower() for c in f.get("categories") or []}:
            continue
        if not store_matches(f["merchant"].strip(), cfg):
            continue
        start, end = parse_dt(f["valid_from"]), parse_dt(f["valid_to"])
        if end and end < now:
            continue
        if start and start > now and not cfg["include_upcoming_flyers"]:
            continue
        keep.append(f)
    return keep


def fetch_flyer(flyer, cfg):
    """Download one flyer + per-item details (deal text like '2/', '/lb')."""
    items = get_json(f"{API}/flyers/{flyer['id']}?locale={cfg['locale']}")["items"]
    items = [i for i in items if i.get("price") not in (None, "")]

    def detail(item):
        try:
            return get_json(f"{API}/items/{item['id']}")["item"]
        except Exception:
            return item  # fall back to the summary record

    with ThreadPoolExecutor(max_workers=cfg["workers"]) as pool:
        details = list(pool.map(detail, items))
    return {"flyer": flyer, "items": details, "fetched_at": datetime.now().isoformat()}


def sync_flyers(cfg):
    if not cfg["postal_code"]:
        raise ValueError("No postal code yet. Enter your address in Settings.")
    FLYERS.mkdir(parents=True, exist_ok=True)
    active = list_flyers(cfg)
    active_ids = {str(f["id"]) for f in active}
    new = [f for f in active if not (FLYERS / f"{f['id']}.json").exists()]
    for n, f in enumerate(new, 1):
        path = FLYERS / f"{f['id']}.json"
        log(f"Fetching {n}/{len(new)}: {f['merchant'].strip()} - {f['name']}")
        try:
            data = fetch_flyer(f, cfg)
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            log(f"  saved {len(data['items'])} items")
        except Exception as e:
            log(f"  failed: {e}")
    for path in FLYERS.glob("*.json"):  # drop expired flyers (custom ones are managed separately)
        if path.stem not in active_ids and not path.stem.startswith("custom-"):
            path.unlink()
    log(f"{len(active)} active flyers")
    state = read_json(STATE, {})
    state.update(last_refresh=datetime.now().isoformat(timespec="seconds"), postal_code=cfg["postal_code"])
    write_json(STATE, state)


def custom_path(store):
    return FLYERS / f"custom-{customflyer.slug(store)}.json"


def sync_custom(cfg, only_url=None):
    """Read flyers from the user's own links. Unchanged images reuse last week's reading."""
    FLYERS.mkdir(parents=True, exist_ok=True)
    wanted = {custom_path(e["store"]).name for e in cfg["custom_flyers"] if not is_excluded(e["store"], cfg)}
    for path in FLYERS.glob("custom-*.json"):
        if path.name not in wanted:
            path.unlink()
    state = read_json(STATE, {})
    status = state.setdefault("custom", {})
    for e in cfg["custom_flyers"]:
        if is_excluded(e["store"], cfg) or (only_url and e["url"] != only_url):
            continue
        log(f"Checking {e['store']} flyer...")
        try:
            data = customflyer.read_flyer(e, cfg, log)
            write_json(custom_path(e["store"]), data)
            status[e["url"]] = {"ok": True, "items": len(data["items"]), "reader": data["flyer"]["reader"],
                                "at": data["fetched_at"]}
            log(f"  {e['store']}: {len(data['items'])} items")
        except Exception as ex:
            status[e["url"]] = {"ok": False, "error": str(ex), "at": datetime.now().isoformat(timespec="seconds")}
            log(f"  {e['store']} failed: {ex}")
    write_json(STATE, state)


# ---------------------------------------------------------------- normalizing

LB_PER_KG = 2.20462

STOP = set("""
a an and or with the of in on for to from by per each ea pk pack pkg size sizes family value
selected select varieties variety assorted fresh product products new style regular original
plus mini ct count bag box tray club jumbo large small medium only save sale bonus
canada canadian ontario grown imported local no name brand our finest
""".split())

STORE_BRANDS = [
    "president's choice", "presidents choice", "pc blue menu", "blue menu", "pc", "no name",
    "compliments", "great value", "kirkland signature", "kirkland", "selection", "irresistibles",
    "our finest", "longo's", "longos", "farm boy", "sensations", "farmer's market", "first street",
    "life brand", "equate", "personnelle", "sunset fresh",
]

SIZE_RE = re.compile(
    r"\b\d+(?:[.,]\d+)?(?:\s*(?:-|/|=|x|to)\s*\d+(?:[.,]\d+)?)*\s*"
    r"(?:'s|’s|s|g|gr|kg|mg|ml|l|lb|lbs|oz|fl oz|pk|ct|pcs?|rolls?|pack)?\b",
    re.I,
)


def strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def clean_text(s):
    # Flipp sometimes returns UTF-8 mis-decoded as Latin-1 ("â¢" for ™)
    try:
        s = s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    s = re.sub(r"[®™©*]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def singular(w):
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 4 and w.endswith(("oes", "ches", "shes", "sses")):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us")):
        return w[:-1]
    return w


def tokens(name, brand):
    s = strip_accents(name.lower())
    for b in [brand.lower()] + STORE_BRANDS if brand else STORE_BRANDS:
        if b:
            s = re.sub(rf"(?<![a-z]){re.escape(strip_accents(b))}(?![a-z])", " ", s)
    s = SIZE_RE.sub(" ", s)
    words = re.findall(r"[a-z]+", s.replace("'", ""))
    return [singular(w) for w in words if len(w) > 1 and w not in STOP]


def parse_price(it):
    """Return (deal_price, qty, unit, unit_price, comparable_unit)."""
    try:
        price = float(str(it.get("current_price") or it.get("price")).replace(",", "."))
    except (TypeError, ValueError):
        return None
    pre = (it.get("pre_price_text") or "").lower()
    post = " ".join(filter(None, [it.get("post_price_text"), it.get("price_text")])).lower()
    m = re.search(r"(\d+)\s*(?:/|for)", pre)
    qty = int(m.group(1)) if m and int(m.group(1)) > 0 else 1
    each = price / qty

    unit = "ea"
    head = post.strip()[:12]  # only look at the leading unit text, not "or 4.29$ ea." trailers
    if re.search(r"(^|/|\s)lbs?\b", head):
        unit = "lb"
    elif re.search(r"(^|/|\s)kg\b", head):
        unit = "kg"
    elif re.search(r"/\s*100\s*g", head):
        unit = "100g"

    # Compare weight-priced items per lb so lb/kg/100g line up
    per_lb = {"lb": each, "kg": each / LB_PER_KG, "100g": each * 4.53592}.get(unit)
    comparable = ("lb", per_lb) if per_lb is not None else ("ea", each)
    return price, qty, unit, round(each, 2), comparable


def normalize(cfg):
    items = []
    cat_index = categories.build(singular)
    region = "en-us" if re.fullmatch(r"\d{5}", cfg["postal_code"]) else "en-ca"
    for path in sorted(FLYERS.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        f = data["flyer"]
        merchant = clean_text(f["merchant"])
        if is_excluded(merchant, cfg):  # cached until the next sync drops it
            continue
        flyer_url = f.get("url") or f"https://flipp.com/{region}/flyer/{f['id']}?postal_code={cfg['postal_code']}"
        for it in data["items"]:
            parsed = parse_price(it)
            if not parsed:
                continue
            price, qty, unit, each, (cmp_unit, cmp_price) = parsed
            name = clean_text(it.get("name") or "")
            brand = clean_text(it.get("brand") or "")
            toks = tokens(name, brand)
            if not name:
                continue
            items.append({
                "id": it["id"],
                "store": merchant,
                "store_logo": f.get("merchant_logo") or it.get("merchant_logo"),
                "name": name,
                "brand": brand,
                "description": clean_text(it.get("description") or ""),
                "price": price,
                "qty": qty,
                "unit": unit,
                "each": each,
                "cmp_unit": cmp_unit,
                "cmp_price": round(cmp_price, 4),
                "pre_text": clean_text(it.get("pre_price_text") or ""),
                "price_text": clean_text(it.get("price_text") or it.get("post_price_text") or ""),
                "sale_story": clean_text(it.get("sale_story") or ""),
                "image": (it.get("cutout_image_url") or "").replace("http://", "https://"),
                "flyer_page": (it.get("image_url") or "").replace("http://", "https://"),
                "link": it.get("ttm_url") or "",
                "flyer_url": flyer_url,
                # custom flyers: no product cut-out, so the page shows this region of the flyer page
                "crop": it.get("crop"), "page_size": it.get("page_size"),
                "ocr": f.get("reader") in ("windows", "paddle"),
                "valid_from": (it.get("valid_from") or f["valid_from"])[:10],
                "valid_to": (it.get("valid_to") or f["valid_to"])[:10],
                "category": categories.classify(toks, cat_index),
                "tokens": toks,
            })
    return items


# ---------------------------------------------------------------- grouping

def dedupe(items):
    """Stores often have overlapping flyers (weekly + insert); keep one copy."""
    seen, out = set(), []
    for it in items:
        key = (it["store"], it["name"].lower(), it["each"], it["unit"])
        if key not in seen:
            seen.add(key)
            out.append(it)
    return out


def similar(a, b, threshold):
    """a, b: word sets sharing a head noun. Needs 2+ shared words (or an exact
    match) so a lone 'sauce' doesn't swallow every sauce in town."""
    inter = len(a & b)
    if a == b:
        return 1.0
    if inter < 2:
        return 0.0
    if a <= b or b <= a:
        return max(threshold, inter / len(a | b))
    return inter / len(a | b)


def group_items(items, threshold):
    """Greedy clustering: items join a group with the same head noun (last
    content word) when their word sets overlap enough."""
    groups, by_head = [], {}
    # Short, generic names first so they become the group anchor
    for it in sorted(items, key=lambda x: (len(set(x["tokens"])), x["name"])):
        toks = set(it["tokens"])
        if not toks:
            groups.append({"tokens": toks, "items": [it]})
            continue
        head = it["tokens"][-1]
        best, best_score = None, 0.0
        for g in by_head.get(head, []):
            score = similar(toks, g["tokens"], threshold)
            if score >= threshold and score > best_score:
                best, best_score = g, score
        if best:
            best["items"].append(it)
        else:
            g = {"tokens": toks, "items": [it]}
            groups.append(g)
            by_head.setdefault(head, []).append(g)

    out = []
    for g in groups:
        offers = g["items"]
        # Compare within the most common unit (each vs per-lb)
        units = [o["cmp_unit"] for o in offers]
        main_unit = max(set(units), key=units.count)
        comparable = sorted((o for o in offers if o["cmp_unit"] == main_unit), key=lambda o: o["cmp_price"])
        others = sorted((o for o in offers if o["cmp_unit"] != main_unit), key=lambda o: o["cmp_price"])
        offers = comparable + others
        stores = {o["store"] for o in offers}
        best = comparable[0]
        # Savings vs. the cheapest *other* store (not the priciest, which is often a bigger pack)
        runner_up = next((o["cmp_price"] for o in comparable if o["store"] != best["store"]), best["cmp_price"])
        for o in offers:
            o.pop("tokens", None)
            o["cheapest"] = len(stores) > 1 and o is best
        cats = [o["category"] for o in offers if o["category"] != "Other"] or ["Other"]
        out.append({
            "title": best["name"],
            "category": max(set(cats), key=cats.count),
            "unit": main_unit,
            "store_count": len(stores),
            "best": best["cmp_price"],
            # A runner-up 3x the price is a different pack/product, not a real saving
            "savings": round(runner_up - best["cmp_price"], 2) if runner_up <= best["cmp_price"] * 3 else 0,
            "offers": offers,
        })
    out.sort(key=lambda g: (-g["store_count"], -g["savings"], g["title"]))
    return out


# ---------------------------------------------------------------- main

def update_location(cfg, address):
    """Geocode a new address and look up grocery branches around it.
    Returns True when the postal code changed (flyers need re-fetching)."""
    log("Finding your address...")
    spot = locate.geocode(address)
    if not spot["postal_code"] and not cfg["postal_code"]:
        raise ValueError("Couldn't work out a postal code. Please include it in the address.")
    log(f"Found {spot['display']}. Looking up nearby stores...")
    spot["shops"] = locate.nearby_shops(spot["lat"], spot["lon"], cfg["store_search_km"])
    spot["address"] = address
    spot["fetched_at"] = datetime.now().isoformat(timespec="seconds")
    write_json(LOCATION, spot)
    log(f"{len(spot['shops'])} grocery locations within {cfg['store_search_km']} km")
    fetched_for = read_json(STATE, {}).get("postal_code")  # area the cached flyers belong to
    changed = bool(spot["postal_code"]) and spot["postal_code"] != fetched_for
    cfg["address"] = address
    if spot["postal_code"]:
        cfg["postal_code"] = spot["postal_code"]
    save_config(cfg)
    return changed


def build_catalog(cfg):
    items = dedupe(normalize(cfg))
    groups = group_items(items, cfg["match_threshold"])
    stores = sorted({i["store"] for i in items})
    location = read_json(LOCATION, None)
    cat_counts = {}
    for g in groups:
        cat_counts[g["category"]] = cat_counts.get(g["category"], 0) + 1
    # Excluded and link-only stores get locations too, so the Stores page can map them
    known = set(stores) | set(cfg["exclude_stores"]) | {e["store"] for e in cfg["custom_flyers"]}
    store_info = locate.nearest_branches(sorted(known), location)
    for store, o in cfg["store_overrides"].items():  # an address you entered beats the OpenStreetMap guess
        if store in known:
            km = locate.km_between(location["lat"], location["lon"], o["lat"], o["lon"]) if location else None
            store_info[store] = {"km": round(km, 1) if km is not None else None, "branch": store,
                                 "address": o["address"], "lat": o["lat"], "lon": o["lon"], "manual": True}
    catalog = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "postal_code": cfg["postal_code"],
        "home": {k: location[k] for k in ("lat", "lon", "display", "address")} if location else None,
        "stores": stores,
        "store_info": store_info,
        "excluded": sorted(cfg["exclude_stores"], key=str.lower),
        "categories": [c for c in categories.ORDER if c in cat_counts],
        "item_count": len(items),
        "groups": groups,
    }
    payload = json.dumps(catalog, ensure_ascii=False, separators=(",", ":"))
    (DATA / "catalog.json").write_text(payload, encoding="utf-8")
    # index.html loads this as a script so it works from file:// with no server
    (DATA / "catalog.js").write_text("window.CATALOG=" + payload + ";", encoding="utf-8")
    multi = sum(1 for g in groups if g["store_count"] > 1)
    log(f"Catalog: {len(items)} items, {len(groups)} cards, {multi} compared across stores")


def refresh(cfg):
    try:
        sync_flyers(cfg)
    except Exception as e:
        log(f"Flyer sync failed ({e}); using cached data")
    sync_custom(cfg)
    build_catalog(cfg)


def last_scheduled_run(cfg, now):
    """Most recent refresh_day/refresh_time at or before now."""
    h, m = map(int, cfg["refresh_time"].split(":"))
    days_back = (now.weekday() - DAYS.index(cfg["refresh_day"])) % 7
    slot = (now - timedelta(days=days_back)).replace(hour=h, minute=m, second=0, microsecond=0)
    return slot if slot <= now else slot - timedelta(days=7)


def is_stale(cfg):
    """True when this week's flyers haven't been fetched yet."""
    last = read_json(STATE, {}).get("last_refresh")
    if not last:
        return True
    last, now = datetime.fromisoformat(last), datetime.now()
    if cfg["weekly_refresh"]:
        return last < last_scheduled_run(cfg, now)
    return now - last > timedelta(days=7)


def main(argv):
    DATA.mkdir(exist_ok=True)
    log_path = DATA / "last_run.log"
    if log_path.exists() and log_path.stat().st_size > 200_000:  # keep the log small
        log_path.unlink()
    cfg = load_config()

    if "--auto" in argv:
        startup = "startup" in argv
        if (not startup or cfg["run_on_startup"]) and is_stale(cfg) and cfg["postal_code"]:
            refresh(cfg)
        else:
            log("Auto run: flyers already up to date" if cfg["postal_code"] else "Auto run: no address set")
        if startup and cfg["open_page_on_startup"]:
            import server
            server.main(open_browser=True)
        return

    if "--offline" in argv:
        build_catalog(cfg)
    else:
        refresh(cfg)


if __name__ == "__main__":
    main(sys.argv[1:])
