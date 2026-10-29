"""Find where you are and how far each flyer store's nearest branch is.

Uses OpenStreetMap: Nominatim (address -> coordinates), Photon as a fallback
for bare postal codes, and Overpass (grocery shops around you).
"""

import json
import math
import re
import time
import unicodedata
import urllib.parse
import urllib.request

UA = {"User-Agent": "FlyerDeals/1.0 (personal grocery comparer)"}
NOMINATIM = "https://nominatim.openstreetmap.org/search"
PHOTON = "https://photon.komoot.io/api/"
OVERPASS = "https://overpass-api.de/api/interpreter"
SHOP_TYPES = ("supermarket|greengrocer|butcher|department_store|wholesale|variety_store|deli|"
              "health_food|frozen_food|seafood|food|bakery|grocery")

CA_POSTAL = re.compile(r"\b([A-Za-z]\d[A-Za-z])\s?(\d[A-Za-z]\d)\b")
US_ZIP = re.compile(r"\b(\d{5})(?:-\d{4})?\s*(?:,?\s*(?:usa?|united states))?\s*$", re.I)


def http_json(url, data=None, timeout=30):
    req = urllib.request.Request(url, data=data, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def postal_from(text):
    m = CA_POSTAL.search(text or "")
    if m:
        return (m.group(1) + m.group(2)).upper()
    m = US_ZIP.search(text or "")
    return m.group(1) if m else ""


def geocode(address):
    """Return {lat, lon, display, postal_code} for a free-form address."""
    address = address.strip()
    postal = postal_from(address)
    tries = [address]
    without_postal = re.sub(r"\s*,\s*,", ",", CA_POSTAL.sub("", address)).strip(" ,")
    if without_postal and without_postal != address:
        tries.append(without_postal)

    for q in tries:
        url = NOMINATIM + "?" + urllib.parse.urlencode(
            {"q": q, "format": "jsonv2", "addressdetails": 1, "limit": 1, "countrycodes": "ca,us"})
        res = http_json(url)
        time.sleep(1)  # Nominatim usage policy: at most 1 request/second
        if res:
            r = res[0]
            return {
                "lat": float(r["lat"]), "lon": float(r["lon"]), "display": r["display_name"],
                "postal_code": postal or postal_from(r.get("address", {}).get("postcode", "")),
            }

    # Photon knows more postal codes, but can return a near-miss ("L3K" for "L3S"), so verify
    # it. Failing an exact match, a code in the same area (first 3 chars, "L3S") is close enough.
    features = []
    for q in dict.fromkeys([address, postal[:3]] if postal else [address]):
        res = http_json(PHOTON + "?" + urllib.parse.urlencode({"q": q, "limit": 5}))
        features += [f for f in res.get("features", [])
                     if f["properties"].get("countrycode", "").upper() in ("CA", "US")]
    for exact in (True, False):
        for f in features:
            p = f["properties"]
            found = postal_from(p.get("postcode") or p.get("name", ""))
            if postal and found and (found != postal if exact else found[:3] != postal[:3]):
                continue
            lon, lat = f["geometry"]["coordinates"]
            place = ", ".join(x for x in (p.get("name"), p.get("city"), p.get("state")) if x)
            display = place if exact else f"near {postal} ({place})"
            return {"lat": lat, "lon": lon, "display": display, "postal_code": postal or found}
    raise ValueError("Couldn't find that address. Try adding the street and city.")


def norm(s):
    s = "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", s.lower())


def nearby_shops(lat, lon, radius_km):
    q = (f'[out:json][timeout:90];nwr["shop"~"^({SHOP_TYPES})$"]'
         f"(around:{int(radius_km * 1000)},{lat},{lon});out center tags;")
    data = http_json(OVERPASS, data=urllib.parse.urlencode({"data": q}).encode(), timeout=120)
    shops = []
    for e in data.get("elements", []):
        t = e.get("tags", {})
        la = e.get("lat", e.get("center", {}).get("lat"))
        lo = e.get("lon", e.get("center", {}).get("lon"))
        names = sorted({norm(t.get(k)) for k in ("name", "brand", "name:en", "operator")} - {""})
        if la is None or not names:
            continue
        street = " ".join(x for x in (t.get("addr:housenumber"), t.get("addr:street")) if x)
        shops.append({
            "names": names, "name": t.get("name") or t.get("brand"),
            "address": ", ".join(x for x in (street, t.get("addr:city")) if x),
            "lat": la, "lon": lo,
        })
    return shops


def km_between(a_lat, a_lon, b_lat, b_lon):
    r = 6371.0
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp, dl = p2 - p1, math.radians(b_lon - a_lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _same_store(merchant_key, shop_name):
    if merchant_key == shop_name:
        return True
    if len(shop_name) >= 4 and merchant_key.startswith(shop_name):  # "wholesaleclubandclubentrepot"
        return True
    if len(merchant_key) >= 4 and shop_name.startswith(merchant_key):  # "nofrillsmarkham"
        return True
    return len(shop_name) >= 6 and shop_name in merchant_key  # "chalofreshco"


def nearest_branches(merchants, location):
    """{merchant: {km, branch, address, lat, lon} or None} for each flyer store."""
    out = {}
    if not location or not location.get("shops"):
        return out
    lat, lon = location["lat"], location["lon"]
    for m in merchants:
        key, best = norm(m), None
        for s in location["shops"]:
            if any(_same_store(key, n) for n in s["names"]):
                d = km_between(lat, lon, s["lat"], s["lon"])
                if best is None or d < best["km"]:
                    best = {"km": round(d, 1), "branch": s["name"], "address": s["address"],
                            "lat": s["lat"], "lon": s["lon"]}
        out[m] = best
    return out
