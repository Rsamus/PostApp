#!/usr/bin/env python3
"""
Zählt 360°-Panoramen bei Mapillary im Umkreis eines Punkts. Lädt keine Bilder.

Aufruf:
    MAPILLARY_TOKEN=MLY|... python3 tools/count_panos.py --lat 50.3273 --lng 11.7005 --radius-km 5

Ausgabe: Gesamtzahl, Aufnahmefolgen, Nutzer, Jahre, grob abgedeckte Straßenkilometer
(belegte 40-m-Zellen) und die zehn längsten Aufnahmefolgen mit Ort und Datum.
"""
import argparse, math, os, sys, time
from collections import defaultdict, Counter

import requests

API = "https://graph.mapillary.com/images"
FIELDS = "id,geometry,captured_at,creator,sequence,is_pano"
EARTH = 6378137.0
RAD = math.pi / 180.0


def get(session, url, params=None, tries=6):
    for i in range(tries):
        r = session.get(url, params=params, timeout=60)
        if r.status_code == 200:
            return r
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(2 ** i); continue
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    raise RuntimeError("zu viele Fehlversuche: " + url)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lng", type=float, required=True)
    ap.add_argument("--radius-km", type=float, default=5)
    args = ap.parse_args()

    token = os.environ.get("MAPILLARY_TOKEN", "").strip()
    if not token:
        print("MAPILLARY_TOKEN fehlt."); sys.exit(1)

    lat0, lon0 = args.lat, args.lng
    cos0 = math.cos(lat0 * RAD)
    r_m = args.radius_km * 1000
    dlat, dlon = r_m / 111320, r_m / (111320 * cos0)
    step = 0.02

    session = requests.Session()
    session.headers["Authorization"] = "OAuth " + token

    found = {}
    lat = lat0 - dlat
    while lat < lat0 + dlat:
        lon = lon0 - dlon
        while lon < lon0 + dlon:
            bbox = f"{lon:.5f},{lat:.5f},{min(lon+step, lon0+dlon):.5f},{min(lat+step, lat0+dlat):.5f}"
            params = {"fields": FIELDS, "bbox": bbox, "is_pano": "true", "limit": 2000}
            url = API
            while url:
                data = get(session, url, params).json()
                for img in data.get("data", []):
                    if img.get("is_pano"):
                        found[img["id"]] = img
                url = data.get("paging", {}).get("next"); params = None
            lon += step
        lat += step

    inside = []
    for img in found.values():
        try:
            lon, lat = img["geometry"]["coordinates"]
        except (KeyError, TypeError, ValueError):
            continue
        x = (lon - lon0) * RAD * EARTH * cos0
        z = -(lat - lat0) * RAD * EARTH
        if x * x + z * z <= r_m * r_m:
            inside.append((img, x, z))

    print(f"Panoramen im Umkreis von {args.radius_km:g} km: {len(inside)}")
    if not inside:
        print("Keine 360°-Panoramen in diesem Gebiet."); return

    seqs = defaultdict(list)
    for img, x, z in inside:
        seqs[img.get("sequence") or img["id"]].append((img, x, z))
    cells = {(math.floor(x / 40), math.floor(z / 40)) for _, x, z in inside}
    users = Counter((img.get("creator") or {}).get("username", "?") for img, _, _ in inside)
    years = Counter(time.strftime("%Y", time.gmtime((img.get("captured_at") or 0) / 1000)) for img, _, _ in inside)

    print(f"Aufnahmefolgen: {len(seqs)}")
    print(f"Abgedeckte Straße (40-m-Zellen): ca. {len(cells) * 40 / 1000:.1f} km")
    print("Nutzer:", ", ".join(f"{u} ({n})" for u, n in users.most_common(6)))
    print("Jahre:", ", ".join(f"{y} ({n})" for y, n in sorted(years.items())))
    print("Längste Aufnahmefolgen:")
    for sid, lst in sorted(seqs.items(), key=lambda kv: -len(kv[1]))[:10]:
        img = lst[0][0]
        cx = sum(p[1] for p in lst) / len(lst); cz = sum(p[2] for p in lst) / len(lst)
        d = time.strftime("%Y-%m-%d", time.gmtime((img.get("captured_at") or 0) / 1000))
        ll_lat = lat0 - cz / (RAD * EARTH); ll_lon = lon0 + cx / (RAD * EARTH * cos0)
        print(f"  {len(lst):5d} Bilder  {d}  {(img.get('creator') or {}).get('username','?'):<16}"
              f"  Mitte {ll_lat:.5f}, {ll_lon:.5f}  ({math.hypot(cx, cz)/1000:.1f} km vom Zentrum)")


if __name__ == "__main__":
    main()