"""Flyers from any web page (stores not on Flipp).

Finds the flyer images (or PDF) on a page, reads them, and saves the result
in the same shape as a Flipp flyer under data/flyers/custom-*.json so the
rest of the app treats it like any other store.

Two readers:
  paddle  - PaddleOCR (setup_paddleocr.ps1 installs it into .venv-ocr). Free and
            offline, reads English and Chinese, run as a separate process.
  claude  - Claude vision via the Anthropic API (pip install anthropic, plus an
            API key). Most accurate; costs a few cents per flyer page.
PaddleOCR's words feed layout rules that work prices out from the "Reg: $x"
and "save $y" labels and skip items they can't price.
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


# ---------------------------------------------------------------- PaddleOCR reader

PADDLE_PY = ROOT / ".venv-ocr" / "Scripts" / "python.exe"


def paddle_available():
    return PADDLE_PY.exists()


NO_READER = "No flyer reader is set up: run setup_paddleocr.ps1 to install PaddleOCR, or add an Anthropic API key in Settings."


def paddle_ocr(image_path, crop=None):
    """Runs paddle_ocr.py in its own environment; the models leave memory when it exits.
    With crop [x, y, w, h], only that area is read (enlarged); boxes stay in page pixels."""
    if not paddle_available():
        raise RuntimeError(NO_READER)
    args = [str(PADDLE_PY), str(ROOT / "paddle_ocr.py"), str(image_path)]
    if crop:
        args += ["--crop", ",".join(str(int(v)) for v in crop)]
    r = subprocess.run(args, capture_output=True, timeout=900, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
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


def _join_split_prices(words):
    """Big prices print the cents small and raised, so OCR can return "6" and "99" as two words:
    join a 1-2 digit word with a 2-digit word right after it on the same line ("699" = $6.99)."""
    joined, used = {}, set()
    for i, a in enumerate(words):  # a: the dollars, b: the raised cents just to its right (may overlap a little)
        if i in used or not re.fullmatch(r"\d{1,2}", a["t"]):
            continue
        for j, b in enumerate(words):
            if j != i and j not in used and re.fullmatch(r"\d{2}", b["t"]) and b["x"] > a["x"] \
                    and -a["w"] * 0.5 <= b["x"] - (a["x"] + a["w"]) < a["h"] * 0.8 and abs(b["y"] - a["y"]) < a["h"] * 0.6:
                used.update((i, j))
                joined[i] = {**a, "t": a["t"] + b["t"], "y": min(a["y"], b["y"]),
                             "w": b["x"] + b["w"] - a["x"], "h": max(a["y"] + a["h"], b["y"] + b["h"]) - min(a["y"], b["y"])}
                break
    return [joined.get(i, w) for i, w in enumerate(words) if i in joined or i not in used]


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
    words = _join_split_prices(words)
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


# ---------------------------------------------------------------- one flyer source
# Per store, data/custom/<store>/ holds:
#   page<n>.<ext>         the flyer images as downloaded
#   pages.json            automatic readings, keyed "<image digest>:<reader>"
#   words-<digest>.json   the OCR words + boxes of each image (what the box editor reads from)
#   current.json          which images make up this week's flyer
#   manual.json           your boxes/corrections per image, plus the last layout as a template

def folder_for(store):
    return CUSTOM / slug(store)


def _load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _save(path, obj):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def pick_engine(cfg):
    engine = cfg.get("ocr_engine", "auto")
    if engine not in ("claude", "paddle"):
        engine = "auto"  # includes the old "windows" setting
    ok, why = claude_available(cfg)
    if engine == "claude" and not ok:
        raise RuntimeError(why)
    if engine == "auto":  # best available: Claude, then PaddleOCR
        engine = "claude" if ok else "paddle"
    if engine == "paddle" and not paddle_available():
        raise RuntimeError(NO_READER)
    return engine


def read_flyer(entry, cfg, log):
    """Download and read one custom flyer. Returns the flyer record (Flipp-shaped)."""
    url, store = entry["url"], entry["store"]
    sources, title = find_sources(url)
    if not sources:
        raise RuntimeError("No flyer images or PDF found on that page.")
    engine = pick_engine(cfg)
    reader_name = {"claude": "Claude", "paddle": "PaddleOCR"}[engine]

    folder = folder_for(store)
    folder.mkdir(parents=True, exist_ok=True)
    cached = _load(folder / "pages.json", {})
    current = []
    for n, src in enumerate(sources, 1):
        data, ctype = fetch(src, timeout=60)
        digest = hashlib.sha256(data).hexdigest()[:16]
        key = f"{digest}:{engine}"
        ext = ".pdf" if ctype == "application/pdf" else Path(urllib.parse.urlparse(src).path).suffix or ".jpg"
        local = folder / f"page{n}{ext}"
        if not local.exists() or local.read_bytes() != data:
            local.write_bytes(data)
        current.append({"n": n, "digest": digest, "key": key, "file": local.name, "src": src})
        if key in cached:  # unchanged image: reuse last week's reading
            continue
        if ctype == "application/pdf" and engine != "claude":
            raise RuntimeError("This flyer is a PDF. Reading PDFs needs the Claude reader (add an API key).")
        log(f"Reading {store} flyer page {n}/{len(sources)} with {reader_name}...")
        size = _image_size(local) or (0, 0)
        if engine == "claude":
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
            meta, words = paddle_ocr(local)
            _save(folder / f"words-{digest}.json", {"engine": engine, "meta": meta, "words": words})
            for area in template_areas(store, n, [meta["width"], meta["height"]]):  # areas you drew last time
                log(f"Reading a marked area of {store} page {n}...")
                a_meta, a_words = paddle_ocr(local, area)
                merge_words(store, digest, area, a_meta, a_words, engine)
            page = {"src": src, "size": [meta["width"], meta["height"]],
                    "items": parse_ocr(meta, words, src), "dates": find_dates(" ".join(w["t"] for w in words))}
        cached[key] = page
    keep = {c["key"] for c in current}
    _save(folder / "pages.json", {k: v for k, v in cached.items() if k in keep})
    digests = {c["digest"] for c in current}
    for f in folder.glob("words-*.json"):  # drop words of images that left the flyer
        if f.stem[6:] not in digests:
            f.unlink()
    _save(folder / "current.json", {"title": title, "url": url, "engine": engine, "pages": current})
    return assemble(store)


def ensure_words(store, log):
    """Make sure every page of this week's flyer has OCR words (for the box editor)."""
    folder = folder_for(store)
    cur = _load(folder / "current.json", None)
    if not cur:
        raise RuntimeError("Read the flyer first (Re-read on the Stores page).")
    for c in cur["pages"]:
        if not (folder / f"words-{c['digest']}.json").exists():
            log(f"Finding the text on {store} page {c['n']}...")
            meta, words = paddle_ocr(folder / c["file"])
            _save(folder / f"words-{c['digest']}.json", {"engine": "paddle", "meta": meta, "words": words})


# ---------------------------------------------------------------- your panels
# A page is a grid of panels like a comic book: a tree of straight cuts.
#   leaf:  {"t": "leaf", name, price, qty, unit, regular_price, deal_text, "edited": [...]}
#   split: {"t": "split", "dir": "v" | "h", "cuts": [absolute x (v) or y (h) positions], "kids": [...]}
# "v" cuts are vertical lines (panels side by side); "h" cuts are horizontal lines (stacked).
# Panels can't overlap, and moving a line resizes every panel on both sides of it.

FIELDS = ("name", "price", "qty", "unit", "regular_price", "deal_text")
MIN_PANEL = 12  # px


def parse_box(meta, words, box):
    """Read one item from the OCR words inside a panel [x, y, w, h]."""
    x, y, w, h = box
    inside = [wd for wd in words if x <= wd["x"] + wd["w"] / 2 <= x + w and y <= wd["y"] + wd["h"] / 2 <= y + h]
    inside = _join_split_prices(inside)
    lines = sorted(_lines(inside), key=lambda ln: (ln["y"], ln["x"]))
    reg, reg_ids, reg_ln = None, set(), None
    for ln in lines:
        m = re.match(r"(?i)\s*R\S{0,2}g\S?\s*:?\s*(\S+)", ln["text"])
        if m and ln["text"][:1] in "Rr":
            val = _money(m.group(1)) if m.group(1)[:1] in "$Ss&" else _money("$" + m.group(1).lstrip(":;'"))
            if val is not None:
                reg, reg_ids, reg_ln = val, {id(wd) for wd in ln["words"]}, ln
                break
    if reg_ln:  # the name is the block of lines stacked right above the Reg label (not a section header)
        name_lines, edge = [], reg_ln["y"]
        for ln in sorted((ln for ln in lines if ln["y"] < reg_ln["y"]), key=lambda ln: -ln["y"]):
            if edge - ln["y"] > ln["h"] * 2.8:
                break
            edge = ln["y"]
            if _is_name(ln["text"]):
                name_lines.insert(0, ln)
    else:
        name_lines = [ln for ln in lines if _is_name(ln["text"])][:3]
    name = _clean_name(" ".join(ln["text"] for ln in name_lines))
    rest = [wd for wd in inside if id(wd) not in reg_ids]
    # "save $3.00" is often read "53.00" (the $ as a 5); inside one panel that's safe to take as $3.00
    saves = [v for wd in sorted(rest, key=lambda wd: wd["y"])
             if (v := _money(wd["t"]) if not re.fullmatch(r"5\d[.,]\d\d", wd["t"]) else _money("$" + wd["t"][1:])) is not None]
    bubs = [b for wd in rest if (b := _bubble_price(wd["t"]))]
    bigs = [wd["t"] for wd in sorted(rest, key=lambda wd: -wd["h"])
            if re.fullmatch(r"\d{2,4}", wd["t"]) and not 1900 <= int(wd["t"]) <= 2100]
    unit = "lb" if any(re.fullmatch(r"(?i)/?lbs?\.?", wd["t"]) for wd in rest) else "ea"

    save = next((s for s in saves if reg and 0.05 * reg <= s <= 0.6 * reg), None)
    derived = round(reg - save, 2) if save else None
    qty, price = 1, None
    for q, p in bubs:
        if not reg or 0.3 * reg <= p / q < reg:
            qty, price = q, p
            break
    if price is None:
        # In a panel you've drawn, the big printed price is the strongest clue: take one that matches a
        # "save" label if there is one, else the biggest plausible one, and only then fall back to reg - save.
        ok = [int(tok) / 100 for tok in bigs[:3] if not reg or 0.3 * reg <= int(tok) / 100 < reg]
        matched = next(((p, s) for p in ok for s in saves if reg and abs(reg - s - p) <= 0.02), None)
        if matched:
            price, save = matched
        elif ok:
            price = ok[0]
            save = round(reg - price, 2) if reg else None
    if price is None and derived:
        price = derived
    story = " · ".join(t for t in (f"Reg ${reg:.2f}" if reg else "", f"save ${save:.2f}" if save else "") if t)
    return {"name": name, "price": price, "qty": qty, "unit": unit, "regular_price": reg, "deal_text": story}


def _leaf(fields=None):
    leaf = {"t": "leaf", "edited": [], "name": "", "price": None, "qty": 1, "unit": "ea",
            "regular_price": None, "deal_text": ""}
    leaf.update({k: v for k, v in (fields or {}).items() if k in FIELDS})
    return leaf


def build_layout(page_items, region):
    """Starting grid for a region [x, y, w, h] from the detected items: cut along the gaps between them."""
    entries = [{"rect": it["crop"], "leaf": _leaf(it)} for it in page_items if it.get("crop")]
    ws = sorted(e["rect"][2] for e in entries) or [region[2]]
    hs = sorted(e["rect"][3] for e in entries) or [region[3]]
    return _build(entries, tuple(region), 0, (ws[len(ws) // 2], hs[len(hs) // 2]))


def _build(entries, region, depth, typ):
    x, y, w, h = region
    if not entries:
        return _leaf()
    if len(entries) == 1 or depth > 12:
        return entries[0]["leaf"]
    best = None
    for axis, d in ((0, "v"), (1, "h")):
        lo, hi = region[axis], region[axis] + region[axis + 2]
        # A detected box's start (where the name begins) is reliable; its far edge is a guess that often
        # runs into the next cell. So group by the first 60% of each box, and put each line right where
        # the next group's boxes start.
        sizes = sorted(e["rect"][axis + 2] for e in entries)
        cap = sizes[len(sizes) // 2] * 1.3  # an unusually wide/tall guess is capped near the typical item size
        spans = sorted((e["rect"][axis], e["rect"][axis] + min(e["rect"][axis + 2], cap) * 0.6) for e in entries)
        merged = []
        for s, t in spans:
            if merged and s <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], t)
            else:
                merged.append([s, t])
        gaps = [b[0] - a[1] for a, b in zip(merged, merged[1:])]
        gap = max(gaps, default=0)
        # cut only at the clear gaps here (the page's main divisions); smaller ones become lines further down
        cuts = [b[0] - 2 for b, g in zip(merged[1:], gaps) if g >= gap * 0.5]
        cuts = [c for c in cuts if lo + MIN_PANEL < c < hi - MIN_PANEL]
        if cuts and (best is None or gap > best[2]):
            best = (axis, d, gap, cuts)
    if best is None:  # boxes too tangled to cut cleanly: split at the median centre of the wider spread
        centres = [(e["rect"][0] + e["rect"][2] / 2, e["rect"][1] + e["rect"][3] / 2) for e in entries]
        axis = 0 if max(c[0] for c in centres) - min(c[0] for c in centres) >= \
            max(c[1] for c in centres) - min(c[1] for c in centres) else 1
        vals = sorted(c[axis] for c in centres)
        cut = (vals[len(vals) // 2 - 1] + vals[len(vals) // 2]) / 2
        if vals[0] == vals[-1]:
            return entries[0]["leaf"]  # duplicates of one box
        best = (axis, "v" if axis == 0 else "h", 0, [cut])
    axis, d, _, cuts = best
    cuts = [round(c) for c in cuts]
    groups = [[] for _ in range(len(cuts) + 1)]
    for e in entries:
        centre = e["rect"][axis] + e["rect"][axis + 2] / 2
        groups[sum(centre > c for c in cuts)].append(e)
    bounds = [region[axis]] + cuts + [region[axis] + region[axis + 2]]
    kids = []
    for i, g in enumerate(groups):
        sub = list(region)
        sub[axis], sub[axis + 2] = bounds[i], bounds[i + 1] - bounds[i]
        kids.append(_build(g, tuple(sub), depth + 1, typ))
    return {"t": "split", "dir": d, "cuts": cuts, "kids": kids}


def panels(node, region):
    """(rect, leaf) for every panel, in reading order."""
    if node["t"] == "leaf":
        yield [round(v) for v in region], node
        return
    axis = 0 if node["dir"] == "v" else 1
    bounds = [region[axis]] + node["cuts"] + [region[axis] + region[axis + 2]]
    for i, kid in enumerate(node["kids"]):
        sub = list(region)
        sub[axis], sub[axis + 2] = bounds[i], bounds[i + 1] - bounds[i]
        yield from panels(kid, sub)


def check_layout(node, region, depth=0):
    """Validate a grid sent from the page; returns a clean copy (raises ValueError)."""
    if depth > 30 or not isinstance(node, dict):
        raise ValueError("Bad layout.")
    if node.get("t") == "leaf":
        leaf = _leaf()
        leaf["name"] = str(node.get("name") or "").strip()[:200]
        leaf["deal_text"] = str(node.get("deal_text") or "")[:200]
        leaf["unit"] = node.get("unit") if node.get("unit") in ("ea", "lb", "kg", "100g") else "ea"
        for k in ("price", "regular_price"):
            try:
                leaf[k] = round(float(node[k]), 2) if node.get(k) not in (None, "") else None
            except (TypeError, ValueError):
                leaf[k] = None
        try:
            leaf["qty"] = max(1, int(node.get("qty") or 1))
        except (TypeError, ValueError):
            leaf["qty"] = 1
        leaf["edited"] = [f for f in node.get("edited") or [] if f in FIELDS]
        return leaf
    if node.get("t") != "split" or node.get("dir") not in ("v", "h"):
        raise ValueError("Bad layout.")
    axis = 0 if node["dir"] == "v" else 1
    lo, hi = region[axis], region[axis] + region[axis + 2]
    cuts = [round(float(c)) for c in node.get("cuts") or []]
    kids = node.get("kids") or []
    if not cuts or len(kids) != len(cuts) + 1 or any(not lo < c < hi for c in cuts) or cuts != sorted(set(cuts)):
        raise ValueError("Bad layout.")
    bounds = [lo] + cuts + [hi]
    out = []
    for i, kid in enumerate(kids):
        sub = list(region)
        sub[axis], sub[axis + 2] = bounds[i], bounds[i + 1] - bounds[i]
        out.append(check_layout(kid, sub, depth + 1))
    return {"t": "split", "dir": node["dir"], "cuts": cuts, "kids": out}


def _blank_template(node):
    """Next week's starting grid: same lines, fields to be re-read."""
    if node["t"] == "leaf":
        return _leaf()
    return {"t": "split", "dir": node["dir"], "cuts": list(node["cuts"]), "kids": [_blank_template(k) for k in node["kids"]]}


def load_words(store, digest):
    return _load(folder_for(store) / f"words-{digest}.json", None)


# ---------------------------------------------------------------- containers
# A page layout is {"containers": [{"rect": [x, y, w, h], "grid": <panel tree>, "whole": bool}]}.
# Containers are the areas you drew around the groceries: each is read on its own (cropped and
# enlarged, so small text comes out sharper) and has its own grid. Anything outside them is ignored.
# A page without drawn containers is one "whole" container covering the page.

def as_containers(layout, size):
    """Accept the older single-grid layout too."""
    if layout and "containers" in layout:
        return layout
    return {"containers": [{"rect": [0, 0, *size], "grid": layout, "whole": True}]}


def layout_panels(layout, size):
    """(rect, leaf) for every panel of every container."""
    for c in as_containers(layout, size)["containers"]:
        yield from panels(c["grid"], c["rect"])


def _overlap(a, b):
    return a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]


def merge_words(store, digest, rect, meta, new_words, engine):
    """Replace the page's words inside rect with a sharper reading of just that area."""
    path = folder_for(store) / f"words-{digest}.json"
    data = _load(path, None) or {"engine": engine, "meta": meta, "words": []}
    x, y, w, h = rect
    inside = lambda wd: x <= wd["x"] + wd["w"] / 2 <= x + w and y <= wd["y"] + wd["h"] / 2 <= y + h
    data["words"] = [wd for wd in data["words"] if not inside(wd)] + [wd for wd in new_words if inside(wd)]
    data.setdefault("areas", [])
    data["areas"] = [a for a in data["areas"] if a != list(rect)] + [list(rect)]
    _save(path, data)
    return data


def read_area(store, digest, rect, log=lambda m: None):
    """OCR one area of a page (cropped + enlarged), then build that area's grid from what's in it."""
    folder = folder_for(store)
    cur = _load(folder / "current.json", None)
    c = next((p for p in (cur or {}).get("pages", []) if p["digest"] == digest), None)
    if not c:
        raise ValueError("That flyer page isn't in this week's flyer anymore. Reload the editor.")
    rect = [int(round(v)) for v in rect]
    log(f"Reading an area of {store} page {c['n']}...")
    meta, words = paddle_ocr(folder / c["file"], rect)
    data = merge_words(store, digest, rect, meta, words, "paddle")
    return {"rect": rect, "grid": area_grid(data, rect), "whole": False}


def sharpen_panel(store, digest, box, log=lambda m: None):
    """OCR just one panel (enlarged) for text the page/area reading missed, then read it."""
    folder = folder_for(store)
    cur = _load(folder / "current.json", None)
    c = next((p for p in (cur or {}).get("pages", []) if p["digest"] == digest), None)
    if not c:
        raise ValueError("That flyer page isn't in this week's flyer anymore. Reload the editor.")
    box = [int(round(v)) for v in box]
    log(f"Reading a panel of {store} page {c['n']}...")
    meta, words = paddle_ocr(folder / c["file"], box)
    data = merge_words(store, digest, box, meta, words, "paddle")
    return parse_box(data["meta"], data["words"], box)


def area_grid(words_data, rect):
    """Find the items inside an area and grid it."""
    x, y, w, h = rect
    inside = [wd for wd in words_data["words"]
              if x <= wd["x"] + wd["w"] / 2 <= x + w and y <= wd["y"] + wd["h"] / 2 <= y + h]
    items = parse_ocr(words_data["meta"], inside, "")
    for it in items:  # keep detected boxes inside the area
        cx, cy, cw, ch = it["crop"]
        nx, ny = max(cx, x), max(cy, y)
        it["crop"] = [nx, ny, max(1, min(cx + cw, x + w) - nx), max(1, min(cy + ch, y + h) - ny)]
    return build_layout(items, rect)


def page_layout(store, c, page, manual):
    """(layout, source): your saved layout, else last week's re-read on this page, else one built
    from the automatic reading."""
    size = page["size"]
    m = manual["pages"].get(c["digest"])
    if m and m.get("layout"):
        return as_containers(m["layout"], size), "manual"
    tpl = manual["template"].get(str(c["n"]))
    words = load_words(store, c["digest"])
    if tpl and tpl.get("layout") and list(tpl.get("size") or []) == list(size) and words:
        layout = as_containers(json.loads(json.dumps(tpl["layout"])), size)
        for rect, leaf in layout_panels(layout, size):
            leaf.update(parse_box(words["meta"], words["words"], rect))
        return layout, "template"
    return as_containers(build_layout(page["items"], [0, 0, *size]), size), "auto"


def _layout_items(layout, size, checked):
    items = []
    for rect, leaf in layout_panels(layout, size):
        if leaf["name"] and leaf["price"]:
            items.append({**{k: leaf[k] for k in FIELDS}, "crop": rect, "checked": checked})
    return items


def _page_items(store, c, page, manual):
    """Items the app uses for one page, and where they came from."""
    layout, source = page_layout(store, c, page, manual)
    if source == "auto":
        return page["items"], source
    return _layout_items(layout, page["size"], checked=source == "manual"), source


def editor_state(store):
    """This week's pages with their containers and grids, for the editor."""
    folder = folder_for(store)
    cur = _load(folder / "current.json", None)
    if not cur:
        return None
    cached = _load(folder / "pages.json", {})
    manual = _load(folder / "manual.json", {"pages": {}, "template": {}})
    pages = []
    for c in cur["pages"]:
        page = cached.get(c["key"])
        if not page:
            continue
        layout, source = page_layout(store, c, page, manual)
        pages.append({"n": c["n"], "digest": c["digest"], "size": page["size"], "layout": layout, "source": source,
                      "has_words": (folder / f"words-{c['digest']}.json").exists()})
    return {"store": store, "url": cur["url"], "pages": pages}


def check_containers(layout, size):
    """Validate a layout sent from the page; returns a clean copy (raises ValueError)."""
    layout = as_containers(layout, size)
    conts = layout.get("containers")
    if not isinstance(conts, list) or not 1 <= len(conts) <= 60:
        raise ValueError("Bad layout.")
    out = []
    for c in conts:
        try:
            x, y, w, h = (int(round(float(v))) for v in c["rect"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("Bad layout.")
        if x < 0 or y < 0 or w < MIN_PANEL or h < MIN_PANEL or x + w > size[0] or y + h > size[1]:
            raise ValueError("An area runs off the page.")
        rect = [x, y, w, h]
        if any(_overlap(rect, o["rect"]) for o in out):
            raise ValueError("Areas can't overlap.")
        out.append({"rect": rect, "grid": check_layout(c.get("grid"), rect), "whole": bool(c.get("whole"))})
    return {"containers": out}


def save_manual(store, digest, layout):
    """Save your layout for one page. It also becomes the starting layout for next week's flyer."""
    folder = folder_for(store)
    cur = _load(folder / "current.json", None) or {"pages": []}
    c = next((p for p in cur["pages"] if p["digest"] == digest), None)
    if not c:
        raise ValueError("That flyer page isn't in this week's flyer anymore. Reload the editor.")
    size = _load(folder / "pages.json", {}).get(c["key"], {}).get("size")
    if not size:
        raise ValueError("That flyer page hasn't been read yet.")
    layout = check_containers(layout, size)
    if sum(1 for _ in layout_panels(layout, size)) > 500:
        raise ValueError("Too many panels.")
    manual = _load(folder / "manual.json", {"pages": {}, "template": {}})
    manual["pages"][digest] = {"layout": layout, "saved_at": datetime.now().isoformat(timespec="seconds")}
    blank = {"containers": [{**k, "grid": _blank_template(k["grid"])} for k in layout["containers"]]}
    manual["template"][str(c["n"])] = {"size": size, "layout": blank, "digest": digest}
    manual["pages"] = {d: v for d, v in manual["pages"].items() if d in {p["digest"] for p in cur["pages"]}}
    _save(folder / "manual.json", manual)
    return len(_layout_items(layout, size, True))


def template_areas(store, n, size):
    """The areas you drew on page n last time (re-read sharply on a new week's image)."""
    tpl = _load(folder_for(store) / "manual.json", {"template": {}}).get("template", {}).get(str(n))
    if not tpl or list(tpl.get("size") or []) != list(size) or "containers" not in (tpl.get("layout") or {}):
        return []
    return [k["rect"] for k in tpl["layout"]["containers"] if not k.get("whole")]


def reset_manual(store, digest):
    """Forget your grid for one page and go back to the automatic reading."""
    folder = folder_for(store)
    manual = _load(folder / "manual.json", {"pages": {}, "template": {}})
    manual["pages"].pop(digest, None)
    manual["template"] = {n: t for n, t in manual["template"].items() if t.get("digest") != digest}
    _save(folder / "manual.json", manual)


def assemble(store):
    """Build the Flipp-shaped flyer record from the cached readings and your boxes (no downloads)."""
    folder = folder_for(store)
    cur = _load(folder / "current.json", None)
    if not cur:
        raise RuntimeError("This flyer hasn't been read yet.")
    cached = _load(folder / "pages.json", {})
    manual = _load(folder / "manual.json", {"pages": {}, "template": {}})
    pages = [(c, cached[c["key"]]) for c in cur["pages"] if c["key"] in cached]
    dates = next((p["dates"] for _, p in pages if p.get("dates")), None)
    if not dates:  # not printed: assume this week
        today = date.today()
        dates = (today.isoformat(), (today + timedelta(days=6)).isoformat())
    fid = "custom-" + slug(store)
    items = []
    for c, p in pages:
        page_items, _ = _page_items(store, c, p, manual)
        for it in page_items:
            items.append({
                "id": f"{fid}-{len(items)}",
                "name": it["name"], "brand": "",
                "current_price": it["price"],
                "pre_price_text": f"{it['qty']}/" if it.get("qty", 1) > 1 else "",
                "post_price_text": {"lb": "/lb", "kg": "/kg", "100g": "/100g"}.get(it["unit"], ""),
                "sale_story": it.get("deal_text", ""),
                "image_url": p["src"],
                "crop": it.get("crop"), "page_size": p["size"],
                "checked": bool(it.get("checked")),
                "valid_from": dates[0], "valid_to": dates[1],
            })
    return {
        "flyer": {"id": fid, "merchant": store, "name": cur.get("title") or "Flyer", "url": cur["url"],
                  "valid_from": dates[0], "valid_to": dates[1], "categories": ["Groceries"],
                  "reader": cur["engine"]},
        "items": items,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
    }
