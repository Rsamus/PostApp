#!/usr/bin/env python3
"""
Holt Mapillary-Straßenfotos für die Fotozone und legt sie als kleine JPEGs ab.

Aufruf:
    MAPILLARY_TOKEN=MLY|... python3 tools/fetch_photos.py \
        --lat 50.3273 --lng 11.7005 --radius-km 3 --max 800 --out www/data

Ergebnis in --out:
    photos.json         Liste: id, x, z (Meter wie in index.json), h (Kompass in Grad),
                        u (Mapillary-Nutzername), f (Dateiname), d (Aufnahmedatum)
    photos/<id>.jpg     Bild, 512 px breit

Lizenz: Alle Mapillary-Bilder stehen unter CC BY-SA 4.0. Die App muss zu jedem
Bild den Nutzernamen zeigen; deshalb steht er in photos.json.

Vorgehen: Die Fotozone wird in ein Raster von ~0,01° zerlegt, jedes Rasterfeld
per bbox-Abfrage geholt (die API verlangt kleine Bboxen). Danach werden die
Bilder ausgedünnt: pro 40-m-Zelle und Blickrichtungssektor (45°) nur das neueste,
bis --max erreicht ist. So verteilen sich die Fotos gleichmäßig statt sich an
einer stark fotografierten Straße zu häufen.
"""
import argparse, io, json, math, os, sys, time
from collections import defaultdict

import requests
from PIL import Image

API = "https://graph.mapillary.com/images"
FIELDS = "id,geometry,compass_angle,captured_at,creator,thumb_1024_url,is_pano"
EARTH = 6378137.0
RAD = math.pi / 180.0


def project(lat, lon, lat0, lon0, cos0):
    return ((lon - lon0) * RAD * EARTH * cos0, -(lat - lat0) * RAD * EARTH)


def get(session, url, params=None, tries=6):
    for i in range(tries):
        r = session.get(url, params=params, timeout=60)
        if r.status_code == 200:
            return r
        if r.status_code in (429, 500, 502, 503, 504):
            wait = 2 ** i
            print(f"  HTTP {r.status_code}, warte {wait}s", file=sys.stderr)
            time.sleep(wait)
            continue
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    raise RuntimeError("zu viele Fehlversuche: " + url)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lng", type=float, required=True)
    ap.add_argument("--radius-km", type=float, default=3)
    ap.add_argument("--max", type=int, default=800)
    ap.add_argument("--width", type=int, default=512)
    ap.add_argument("--out", default="www/data")
    args = ap.parse_args()

    token = os.environ.get("MAPILLARY_TOKEN", "").strip()
    if not token:
        print("MAPILLARY_TOKEN fehlt – überspringe Fotos, schreibe leere photos.json")
        os.makedirs(args.out, exist_ok=True)
        with open(os.path.join(args.out, "photos.json"), "w") as fh:
            json.dump([], fh)
        return

    lat0, lon0 = args.lat, args.lng
    cos0 = math.cos(lat0 * RAD)
    r_m = args.radius_km * 1000
    dlat = r_m / 111320
    dlon = r_m / (111320 * cos0)
    step = 0.01

    session = requests.Session()
    session.headers["Authorization"] = "OAuth " + token

    found = {}
    lat = lat0 - dlat
    while lat < lat0 + dlat:
        lon = lon0 - dlon
        while lon < lon0 + dlon:
            bbox = f"{lon:.5f},{lat:.5f},{min(lon+step, lon0+dlon):.5f},{min(lat+step, lat0+dlat):.5f}"
            params = {"fields": FIELDS, "bbox": bbox, "limit": 2000}
            url = API
            while url:
                r = get(session, url, params)
                data = r.json()
                for img in data.get("data", []):
                    found[img["id"]] = img
                url = data.get("paging", {}).get("next")
                params = None
            lon += step
        lat += step
    print(f"{len(found)} Bilder in der Fotozone gefunden")

    # Ausdünnen: pro 40-m-Zelle und 45°-Sektor das neueste Bild
    cands = []
    for img in found.values():
        if img.get("is_pano"):
            continue
        try:
            lon, lat = img["geometry"]["coordinates"]
        except (KeyError, TypeError, ValueError):
            continue
        x, z = project(lat, lon, lat0, lon0, cos0)
        if x * x + z * z > r_m * r_m:
            continue
        heading = float(img.get("compass_angle") or 0.0)
        cands.append((img, x, z, heading))

    buckets = defaultdict(list)
    for c in cands:
        img, x, z, heading = c
        key = (math.floor(x / 40), math.floor(z / 40), int(heading // 45) % 8)
        buckets[key].append(c)
    chosen = []
    for key, lst in buckets.items():
        lst.sort(key=lambda c: c[0].get("captured_at", 0), reverse=True)
        chosen.append(lst[0])
    chosen.sort(key=lambda c: c[0].get("captured_at", 0), reverse=True)
    # gleichmäßig über die Zone verteilen: bei Überschuss jedes n-te nehmen
    if len(chosen) > args.max:
        stride = len(chosen) / args.max
        chosen = [chosen[int(i * stride)] for i in range(args.max)]
    print(f"{len(chosen)} Bilder ausgewählt (max {args.max})")

    photo_dir = os.path.join(args.out, "photos")
    os.makedirs(photo_dir, exist_ok=True)
    for f in os.listdir(photo_dir):
        os.remove(os.path.join(photo_dir, f))

    out = []
    total = 0
    for i, (img, x, z, heading) in enumerate(chosen):
        try:
            r = get(session, img["thumb_1024_url"])
            im = Image.open(io.BytesIO(r.content)).convert("RGB")
            w, h = im.size
            im = im.resize((args.width, max(1, int(h * args.width / w))), Image.LANCZOS)
            fname = f"{img['id']}.jpg"
            path = os.path.join(photo_dir, fname)
            im.save(path, "JPEG", quality=72, optimize=True)
            total += os.path.getsize(path)
        except Exception as e:  # ein kaputtes Bild soll den Lauf nicht abbrechen
            print(f"  übersprungen {img['id']}: {e}", file=sys.stderr)
            continue
        creator = img.get("creator") or {}
        captured = img.get("captured_at")
        out.append({
            "id": img["id"], "x": round(x, 1), "z": round(z, 1), "h": round(heading, 1),
            "u": creator.get("username", "unbekannt"), "f": fname,
            "d": time.strftime("%Y-%m-%d", time.gmtime(captured / 1000)) if captured else "",
        })
        if (i + 1) % 100 == 0:
            print(f"  {i+1}/{len(chosen)} geladen")

    with open(os.path.join(args.out, "photos.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, separators=(",", ":"))
    print(f"{len(out)} Fotos gespeichert, {total/1e6:.1f} MB")


if __name__ == "__main__":
    main()