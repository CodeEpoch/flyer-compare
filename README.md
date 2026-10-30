# Flyer Deals

Compares this week's grocery flyers from stores near you and shows them as an
Amazon-style page. Similar items across stores collapse into one card with the
cheapest store highlighted. Python standard library only.

## Use

Double-click **FlyerDeals.pyw**. It starts a small local app at http://127.0.0.1:5178
and opens it in your browser. The app stops by itself 30 minutes after you close the page.

In **Settings**:
- **Address**: a street address or just a postal code. It picks the flyers for your area and
  works out the distance to each store's nearest branch (via OpenStreetMap).
- **Automatic refresh**: once a week on the day/time you choose (Windows Task Scheduler,
  catches up if the PC was off).
- **When Windows starts**: optionally refresh if this week's flyers are missing, and/or open the page.

- **Flyers from other websites**: paste a store's flyer page (e.g. https://kingsfairsupermarket.com/flyer.html).
  The flyer images/PDF on it are read every week (only re-read when the images change):
  - *PaddleOCR* (free, offline, reads English and Chinese): run `setup_paddleocr.ps1` once to install it
    into `.venv-ocr` (~1 GB). It runs as a separate process only while a flyer is read.
  - PaddleOCR's text is turned into items by working prices out from the big printed price and the
    "Reg $x / save $y" labels; items it can't price are skipped.
    Partial and approximate; those cards say "Read by OCR".
  - *Claude* (`pip install anthropic` + an Anthropic API key in Settings or `ANTHROPIC_API_KEY`):
    reads every item accurately, including PDFs. Costs a few cents per flyer page.
  - **✎ Mark items** (Stores page) opens a panel editor. Draw **▭ Areas** (key A) around the parts of a page
    that hold groceries: each area is OCR'd on its own (cropped and enlarged, so small prices read better)
    and gets its own grid; everything outside your areas is ignored. Drag an area's edges to resize it.
    Areas are remembered and re-read sharply on next week's flyer. Inside, each page or area is a comic-book grid of panels
    (one per item) made of straight lines only, so panels never overlap. Drag a line to resize the panels
    on both sides, cut a panel with ┃ / ━ (keys V / H), or draw a long line across many panels with
    ✎ (L): parallel lines that overlap it and sit within a few pixels snap onto it. Click a line to remove
    it, or ✂ split it into sections where lines on both sides meet it. Each panel lists the text found in
    it: click a word to fill the selected field, or re-read the panel sharper. Panels without a name and
    price aren't saved. Saved grids make items "Checked by you" and are reused on next
    week's flyer when the page size matches.

**Stores page** (🏪 Stores in the header): every store in one list. Set a store's real address
(fixes or adds its distance; saved to `store_overrides` in `config.json`), show/exclude it, use it in
comparisons, mark it as My store, and add / re-read / remove flyer links from other websites.
Switch to **Map** to see you and each store's nearest branch on an OpenStreetMap map (needs internet).

Filters: category, distance, **My stores** (type a name; shows everything those stores have on sale),
**Compare stores** (which stores take part in comparisons; ⊘ on hover excludes a store entirely, saved
to `exclude_stores` in `config.json`), and items at 2+ stores.

## Files

| File | What |
|---|---|
| `config.json` | Your settings |
| `data/flyers/<id>.json` | Raw flyer + item data per flyer (expired ones are deleted) |
| `data/location.json` | Your coordinates + grocery shops nearby |
| `data/catalog.json` / `catalog.js` | Normalized, grouped, categorized items the page shows |
| `data/last_run.log` | Log of refreshes |

**Language**: the picker in the header switches between English, 简体中文 and 繁體中文 (both pages;
remembered per browser). Interface text lives in `i18n.js`; store and product names stay as printed.

Command line: `py grocery.py` (refresh), `py grocery.py --offline` (rebuild from cache).

`match_threshold` (0–1) in `config.json` controls how loosely items are merged; raise it if
unrelated items share a card. Flyer data comes from Flipp's public (unofficial) endpoints, so it
may change without notice.
# flyer-compare
