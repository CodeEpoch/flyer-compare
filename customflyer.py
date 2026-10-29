"""Flyers from any web page (stores not on Flipp).

Finds the flyer images (or PDF) on a page, reads them, and saves the result
in the same shape as a Flipp flyer under data/flyers/custom-*.json so the
rest of the app treats it like any other store.

Three readers:
  paddle  - PaddleOCR (setup_paddleocr.ps1 installs it into .venv-ocr). Free and
            offline, reads English and Chinese, run as a separate process.
  windows - Windows' built-in OCR. Free, nothing to install, but rougher.
  claude  - Claude vision via the Anthropic API (pip install anthropic, plus an
            API key). Most accurate; costs a few cents per flyer page.
Both OCR readers feed the same layout rules, which work prices out from the
"Reg: $x" and "save $y" labels and skip items they can't price.
"""

import base64
import hashlib
import html.parser
import json
import os
import re
import subprocess
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CUSTOM = ROOT / "data" / "custom"
UA = {"User-Agent": "Mozilla/5.0 (personal grocery comparer)"}
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")
SKIP_WORDS = ("logo", "banner", "icon", "background", "sprite", "social", "facebook", "instagram")
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(), r.headers.get_content_type()


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40]


# ---------------------------------------------------------------- finding the flyer

class _Links(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.found, self.title, self._in_title = [], "", False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "title":
            self._in_title = True
        for key in ("src", "href", "data", "data-src", "data-original"):
            if a.get(key):
                self.found.append((tag, a[key], a))

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data


def find_sources(url, depth=0):
    """Flyer image/PDF URLs on a page (follows one level of iframes)."""
    body, ctype = fetch(url)
    path = urllib.parse.urlparse(url).path.lower()
    if ctype.startswith("image/") or ctype == "application/pdf" or path.endswith(IMAGE_EXT + (".pdf",)):
        return [url], ""
    p = _Links()
    p.feed(body.decode("utf-8", "replace"))
    out = []
    for tag, ref, attrs in p.found:
        full = urllib.parse.urljoin(url, ref)
        low = urllib.parse.urlparse(full).path.lower()
        if any(w in low for w in SKIP_WORDS):
            continue
        if tag == "iframe" and depth == 0 and not low.endswith(IMAGE_EXT + (".pdf",)):
            try:
                out += find_sources(full, depth + 1)[0]
            except Exception:
                pass
        elif low.endswith(".pdf"):
            out.append(full)
        elif low.endswith(IMAGE_EXT) and tag in ("img", "a", "source", "embed", "object"):
            # small images (buttons, thumbnails) aren't flyer pages
            try:
                if int(attrs.get("width", 9999)) < 400:
                    continue
            except ValueError:
                pass
            out.append(full)
    return list(dict.fromkeys(out)), p.title.strip()


# ---------------------------------------------------------------- Windows OCR reader

def windows_ocr(image_path):
    r = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "ocr.ps1"), str(image_path)],
        capture_output=True, timeout=180, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if r.returncode != 0:
        raise RuntimeError("Windows OCR failed: " + r.stderr.decode("utf-8", "replace").strip()[:300])
    rows = [json.loads(l) for l in r.stdout.decode("utf-8-sig").splitlines() if l.strip()]
    return rows[0], rows[1:]


# ---------------------------------------------------------------- PaddleOCR reader

PADDLE_PY = ROOT / ".venv-ocr" / "Scripts" / "python.exe"


def paddle_available():
    return PADDLE_PY.exists()


def paddle_ocr(image_path):
    """Runs paddle_ocr.py in its own environment; the models leave memory when it exits."""
    r = subprocess.run(
        [str(PADDLE_PY), str(ROOT / "paddle_ocr.py"), str(image_path)],
        capture_output=True, timeout=900, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if r.returncode != 0:
        err = r.stderr.decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError("PaddleOCR failed: " + (err[-1] if err else f"exit code {r.returncode}"))
    rows = [json.loads(l) for l in r.stdout.decode("utf-8").splitlines() if l.startswith("{")]
    return rows[0], rows[1:]


def _lines(words):
    """Group OCR words into text lines."""
    words = sorted(words, key=lambda w: (w["y"], w["x"]))
    lines = []
    for w in words:
        for ln in lines:
            gap = ln["h"] * 1.6
            if abs(ln["y"] - w["y"]) < max(8, ln["h"] * 0.6) and ln["x"] - gap <= w["x"] + w["w"] and w["x"] <= ln["x2"] + gap:
                ln["words"].append(w)
                ln["x"], ln["x2"] = min(ln["x"], w["x"]), max(ln["x2"], w["x"] + w["w"])
                ln["h"] = max(ln["h"], w["h"])
                break
        else:
            lines.append({"x": w["x"], "y": w["y"], "x2": w["x"] + w["w"], "h": w["h"], "words": [w]})
    for ln in lines:
        ln["text"] = " ".join(w["t"] for w in sorted(ln["words"], key=lambda w: w["x"]))
    return lines


def _clean_name(text):
    """Drop OCR junk words ('Sl.oo', 'Uhl', '•') and keep the readable product name."""
    words = [w.strip("_•*'\".,") for w in text.split()]
    keep = [w for w in words if re.fullmatch(r"[A-Za-z][A-Za-z&'/()\-]*[A-Za-z)]|&|w/", w) or CJK.fullmatch(w)]
    return " ".join(keep)


# Chinese words (PaddleOCR reads them; names become "Garlic Shoot 蒜芯")
CJK = re.compile(r"[㐀-䶿一-鿿][㐀-䶿一-鿿()（）·]*")


def _money(tok):
    """'$1.59', 'so.60', 'S2.oo', '$0:99', '$3i99' -> float, or None."""
    t = tok.strip(".,;:'\"()*>_")
    t = re.sub(r"^[Ss$&]", "$", t)
    if not t.startswith("$"):
        return None
    t = t[1:].translate(str.maketrans({"o": "0", "O": "0", "l": "1", "I": "1", "i": ".", ":": ".", "!": ".", ",": "."}))
    if re.fullmatch(r"[2-9]1\d\d", t):  # decimal point read as "1": "$7199" is $7.99
        t = t[0] + "." + t[2:]
    elif re.fullmatch(r"\d{3,4}", t):  # dropped decimal point: "$099"
        t = t[:-2] + "." + t[-2:]
    m = re.fullmatch(r"(\d{1,3})\.(\d{2})\d?", t)
    return float(m.group(1) + "." + m.group(2)) if m else None


def _bubble_price(tok):
    """Big sale-price text when OCR does catch it: '79t' (79c), '2/300', '899'."""
    t = tok.strip(".,;:'\"()")
    m = re.fullmatch(r"(\d{2})[¢tcC]", t)
    if m:
        return 1, int(m.group(1)) / 100
    m = re.fullmatch(r"(\d)/(\d{1,2})(\d{2})", t)
    if m:
        return int(m.group(1)), float(f"{m.group(2)}.{m.group(3)}")
    return None


def _is_name(text):
    if len(re.findall(r"[㐀-䶿一-鿿]", text)) >= 2 and not re.search(r"\$|\d\.\d", text):
        # a Chinese name line, unless it's a promo label (weekday-only, while stocks last, tasting, "great value")
        return not re.search(r"星期|售完|即止|试吃|試吃|超值|震撼|限时|限時", text)
    letters = sum(c.isalpha() and c.isascii() for c in text)
    return letters >= 3 and letters / max(len(text), 1) > 0.6 and re.search(r"[aeiouAEIOU]", text) \
        and not re.match(r"(?i)\s*(reg|save|thurs|sat|fri|demo|while|order)\b", text)


def parse_ocr(meta, words, image_url):
    """Items from OCR words, using each 'Reg: $x' label as the anchor of a product cell."""
    W, H = meta["width"], meta["height"]
    lines = _lines(words)
    regs = []
    for ln in lines:
        m = re.match(r"(?i)\s*R\S{0,2}g\S?\s*:?\s*(\S+)", ln["text"])
        if not m or not ln["text"][:1] in "Rr":
            continue
        val = _money(m.group(1)) if m.group(1)[:1] in "$Ss&" else _money("$" + m.group(1).lstrip(":;'"))
        if val is not None:
            regs.append({**ln, "reg": val})

    saves = [w for w in words if (v := _money(w["t"])) is not None and w not in
             [x for r in regs for x in r["words"]]]
    unit_words = [w for w in words if re.fullmatch(r"(?i)/?(lb|lbs|ea|box|bag|order|ph|pk)\.?/?", w["t"])]
    bubbles = [(w, b) for w in words if (b := _bubble_price(w["t"]))]
    # Big sale prices print the cents small and raised, so OCR reads "1.39" as "139" (PaddleOCR catches these)
    big_nums = [w for w in words if re.fullmatch(r"\d{2,4}", w["t"]) and not 1900 <= int(w["t"]) <= 2100]

    items = []
    for r in regs:
        # cell width: distance to the next Reg label to the right on this row
        right = [o["x"] for o in regs if o is not r and abs(o["y"] - r["y"]) < 60 and o["x"] > r["x"] + 50]
        cell_w = (min(right) - r["x"]) if right else W * 0.14
        below = [o["y"] for o in regs if o is not r and abs(o["x"] - r["x"]) < cell_w * 0.5 and o["y"] > r["y"] + 60]
        cell_bottom = min(below) - H * 0.06 if below else r["y"] + H * 0.2

        # name: English lines left-aligned with the Reg label, stacked directly above it
        above = sorted((ln for ln in lines if ln is not r and abs(ln["x"] - r["x"]) < 25
                        and 0 < r["y"] - ln["y"] < H * 0.07), key=lambda ln: -ln["y"])
        name_lines, edge = [], r["y"]
        for ln in above:
            if edge - ln["y"] > ln["h"] * 2.8:  # gap: that's another cell
                break
            edge = ln["y"]
            if _is_name(ln["text"]):
                name_lines.insert(0, ln)
        name = _clean_name(" ".join(ln["text"] for ln in name_lines))
        if not set(name.lower().split()) - {"fresh", "frozen", "live", "new", "rich", "organic", "w/", "&"}:
            continue  # only an adjective survived OCR
        top = name_lines[0]["y"]

        in_cell = lambda w: r["x"] - 20 <= w["x"] <= r["x"] + cell_w and r["y"] + 10 < w["y"] < cell_bottom
        save_w = sorted((w for w in saves if in_cell(w)), key=lambda w: (w["y"], w["x"]))
        save = _money(save_w[0]["t"]) if save_w else None  # nearest below the label
        bub = next((b for w, b in bubbles if in_cell(w)), None)
        unit_w = next((w for w in unit_words if in_cell(w)), None)
        unit = "lb" if unit_w and "lb" in unit_w["t"].lower() else "ea"

        # Only keep prices that make sense next to the regular price: a misread is worse than a gap
        reg, qty, price = r["reg"], 1, None
        derived = round(reg - save, 2) if reg and save and 0.05 * reg <= save <= 0.6 * reg else None
        big = None
        for w in sorted((w for w in big_nums if in_cell(w)), key=lambda w: -w["h"])[:2]:  # largest text first
            p = int(w["t"]) / 100
            if reg and 0.3 * reg <= p < reg and (derived is None or abs(p - derived) <= 0.02):
                big = p  # agrees with the Reg/save labels (or there's no save label to check against)
                break
        if bub and reg and 0.3 * reg <= bub[1] / bub[0] < reg:
            qty, price = bub
        elif big:
            price = big
        elif derived:
            price = derived
        if not price:
            continue

        bottom = max([w["y"] + w["h"] for w in save_w] + [r["y"] + r["h"] + H * 0.12])
        crop = [max(0, r["x"] - 10), max(0, top - 10), min(W, r["x"] + cell_w) - max(0, r["x"] - 10),
                min(H, bottom + 15) - max(0, top - 10)]
        story = " · ".join(x for x in (f"Reg ${r['reg']:.2f}" if r["reg"] else "",
                                       f"save ${save:.2f}" if save else "") if x)
        items.append({"name": name, "price": price, "qty": qty, "unit": unit,
                      "regular_price": r["reg"], "deal_text": story, "crop": crop})
    return items


def find_dates(text, today=None):
    """'OCT 29, 2026 - NOV 04, 2026' style flyer periods -> (from, to) ISO dates."""
    today = today or date.today()
    # OCR slips: "0CT 29，2026-N0V04" -> "OCT 29,2026-NOV04"
    text = text.replace("，", ",").replace("－", "-")
    text = re.sub(r"(?i)\b[A-Z0]{3}(?=\s*\d)",
                  lambda m: m.group(0).replace("0", "O") if m.group(0).replace("0", "O").lower() in MONTHS else m.group(0), text)
    pat = r"([A-Za-z]{3})[a-z]*\.?\s*(\d{1,2})(?:,?\s*(\d{4}))?"
    m = re.search(pat + r"\s*(?:-|–|to)\s*" + pat, text)
    if m:
        try:
            y2 = int(m.group(6) or m.group(3) or today.year)
            y1 = int(m.group(3) or y2)
            d1 = date(y1, MONTHS[m.group(1).lower()], int(m.group(2)))
            d2 = date(y2, MONTHS[m.group(4).lower()], int(m.group(5)))
            if d1 <= d2:
                return d1.isoformat(), d2.isoformat()
        except (KeyError, ValueError):
            pass
    return None


# ---------------------------------------------------------------- Claude vision reader

CLAUDE_SCHEMA = {
    "type": "object",
    "properties": {
        "valid_from": {"type": "string", "description": "YYYY-MM-DD, or empty if not shown"},
        "valid_to": {"type": "string", "description": "YYYY-MM-DD, or empty if not shown"},
        "items": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "size": {"type": "string"},
                "price": {"type": "number"},
                "qty": {"type": "integer"},
                "unit": {"type": "string", "enum": ["ea", "lb", "kg", "100g"]},
                "regular_price": {"type": ["number", "null"]},
                "deal_text": {"type": "string"},
                "box": {"type": "array", "items": {"type": "number"}},
            },
            "required": ["name", "size", "price", "qty", "unit", "regular_price", "deal_text", "box"],
            "additionalProperties": False,
        }},
    },
    "required": ["valid_from", "valid_to", "items"],
    "additionalProperties": False,
}

CLAUDE_PROMPT = """This is a page of a grocery store's weekly sale flyer. List every advertised product.

For each product:
- name: the English product name (include the brand), no size
- size: pack size or weight as printed (e.g. "500g", "12oz"), or ""
- price: the SALE price as a number in dollars for the whole deal. "99¢" is 0.99; a big "1" with small "39" is 1.39; "2/$3.00" has price 3.00 and qty 2
- qty: how many items the price buys (1 unless it's a multi-buy like 2/$3.00)
- unit: "lb", "kg" or "100g" if the price is per weight, otherwise "ea" (boxes, bags, orders and packs are "ea")
- regular_price: the "Reg" price if printed, else null
- deal_text: short notes such as "save $0.60" or "Sat-Sun only", or ""
- box: [x0, y0, x1, y1] of the product's whole cell (name, photo and price) as fractions 0-1 of the image width and height

Also give the flyer's valid dates (valid_from, valid_to) if printed. Skip store info, banners and anything without a price."""


def claude_available(cfg):
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False, "The anthropic package isn't installed (pip install anthropic)."
    if cfg.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True, ""
    return False, "No Anthropic API key set."


def parse_claude(cfg, data, media_type):
    import anthropic

    client = anthropic.Anthropic(api_key=cfg.get("anthropic_api_key") or None)
    block = {"type": "document" if media_type == "application/pdf" else "image",
             "source": {"type": "base64", "media_type": media_type,
                        "data": base64.standard_b64encode(data).decode()}}
    try:
        response = client.beta.messages.create(
            model=cfg.get("claude_model") or "claude-opus-5-5",
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            extra_body={"fallbacks": "default"},  # via extra_body: older SDKs lack the keyword
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": CLAUDE_SCHEMA}},
            messages=[{"role": "user", "content": [block, {"type": "text", "text": CLAUDE_PROMPT}]}],
        )
    except anthropic.AuthenticationError:
        raise RuntimeError("The Anthropic API key was rejected.")
    except anthropic.RateLimitError:
        raise RuntimeError("Anthropic API rate limit hit; try again later.")
    except anthropic.APIStatusError as e:
        raise RuntimeError(f"Anthropic API error {e.status_code}: {e.message}")
    except anthropic.APIConnectionError:
        raise RuntimeError("Couldn't reach the Anthropic API.")
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined to read this flyer.")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("The flyer had too many items to read in one go.")
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)


# ---------------------------------------------------------------- one flyer source

def _image_size(path):
    """(width, height) of a JPEG/PNG without an imaging library; None if unknown."""
    d = Path(path).read_bytes()
    if d[:8] == b"\x89PNG\r\n\x1a\n":
        return int.from_bytes(d[16:20], "big"), int.from_bytes(d[20:24], "big")
    i = 2
    while d[:2] == b"\xff\xd8" and i < len(d) - 9:
        marker, length = d[i + 1], int.from_bytes(d[i + 2:i + 4], "big")
        if marker in (0xC0, 0xC1, 0xC2):
            return int.from_bytes(d[i + 7:i + 9], "big"), int.from_bytes(d[i + 5:i + 7], "big")
        i += 2 + length
    return None


def read_flyer(entry, cfg, log):
    """Download and read one custom flyer. Returns the flyer record (Flipp-shaped)."""
    url, store = entry["url"], entry["store"]
    sources, title = find_sources(url)
    if not sources:
        raise RuntimeError("No flyer images or PDF found on that page.")
    engine = cfg.get("ocr_engine", "auto")
    ok, why = claude_available(cfg)
    if engine == "claude" and not ok:
        raise RuntimeError(why)
    if engine == "paddle" and not paddle_available():
        raise RuntimeError("PaddleOCR isn't installed. Run setup_paddleocr.ps1.")
    if engine == "auto":  # best available: Claude, then PaddleOCR, then Windows OCR
        engine = "claude" if ok else "paddle" if paddle_available() else "windows"
    use_claude = engine == "claude"
    reader_name = {"claude": "Claude", "paddle": "PaddleOCR", "windows": "Windows OCR"}[engine]

    folder = CUSTOM / slug(store)
    folder.mkdir(parents=True, exist_ok=True)
    cache = folder / "pages.json"
    cached = json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else {}
    pages, dates, all_text = [], None, ""
    for n, src in enumerate(sources, 1):
        data, ctype = fetch(src, timeout=60)
        digest = hashlib.sha256(data).hexdigest()[:16]
        key = f"{digest}:{engine}"
        if key in cached:  # unchanged image: reuse last week's reading
            pages.append(cached[key])
            continue
        if ctype == "application/pdf" and not use_claude:
            raise RuntimeError("This flyer is a PDF. Reading PDFs needs the Claude reader (add an API key).")
        ext = ".pdf" if ctype == "application/pdf" else Path(urllib.parse.urlparse(src).path).suffix or ".jpg"
        local = folder / f"page{n}{ext}"
        local.write_bytes(data)
        log(f"Reading {store} flyer page {n}/{len(sources)} with {reader_name}...")
        size = _image_size(local) or (0, 0)
        if use_claude:
            out = parse_claude(cfg, data, ctype if ctype.startswith(("image/", "application/pdf")) else "image/jpeg")
            W, H = size
            items = []
            for it in out["items"]:
                b = it.pop("box", None)
                if W and b and len(b) == 4 and 0 <= b[0] < b[2] <= 1 and 0 <= b[1] < b[3] <= 1:
                    it["crop"] = [b[0] * W, b[1] * H, (b[2] - b[0]) * W, (b[3] - b[1]) * H]
                it["name"] = " ".join(x for x in (it["name"], it.pop("size", "")) if x)
                items.append(it)
            page = {"src": src, "size": size, "items": items,
                    "dates": [out["valid_from"], out["valid_to"]] if out["valid_from"] and out["valid_to"] else None}
        else:
            meta, words = paddle_ocr(local) if engine == "paddle" else windows_ocr(local)
            text = " ".join(w["t"] for w in words)
            page = {"src": src, "size": [meta["width"], meta["height"]],
                    "items": parse_ocr(meta, words, src), "dates": find_dates(text)}
        cached[key] = page
        pages.append(page)
    cache.write_text(json.dumps({k: v for k, v in cached.items() if v in pages}, indent=1), encoding="utf-8")

    dates = next((p["dates"] for p in pages if p.get("dates")), None)
    if not dates:  # not printed: assume this week
        today = date.today()
        dates = (today.isoformat(), (today + timedelta(days=6)).isoformat())
    fid = "custom-" + slug(store)
    items = []
    for p in pages:
        for i, it in enumerate(p["items"]):
            items.append({
                "id": f"{fid}-{len(items)}",
                "name": it["name"], "brand": "",
                "current_price": it["price"],
                "pre_price_text": f"{it['qty']}/" if it.get("qty", 1) > 1 else "",
                "post_price_text": {"lb": "/lb", "kg": "/kg", "100g": "/100g"}.get(it["unit"], ""),
                "sale_story": it.get("deal_text", ""),
                "image_url": p["src"],
                "crop": it.get("crop"), "page_size": p["size"],
                "valid_from": dates[0], "valid_to": dates[1],
            })
    return {
        "flyer": {"id": fid, "merchant": store, "name": title or "Flyer", "url": url,
                  "valid_from": dates[0], "valid_to": dates[1], "categories": ["Groceries"],
                  "reader": engine},
        "items": items,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
    }
