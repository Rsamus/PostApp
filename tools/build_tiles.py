#!/usr/bin/env python3
"""
Zerlegt einen OSM-Auszug (.osm.pbf) in Kacheln für den Gangfolge Simulator.

Aufruf:
    python3 tools/build_tiles.py region.osm.pbf \
        --lat 50.3273 --lng 11.7005 --radius-km 30 --out www/data

Ergebnis in --out:
    index.json            Nullpunkt, Kachelgröße, Liste aller Kacheln mit Zählern
    tiles/<ix>_<iz>.json.gz   je Kachel: roads, buildings, addresses in Metern
                              relativ zum Nullpunkt (x nach Osten, z nach Süden,
                              identisch zur Projektion in index.html)

Koordinaten werden auf 0,1 m gerundet. Ein Straßenzug wird in jede Kachel
geschrieben, die eines seiner Segmente berührt, damit die Randprüfung beim
Fahren an Kachelgrenzen keine Lücke hat.
"""
import argparse, gzip, json, math, os, sys, time
from collections import defaultdict

import osmium

EARTH = 6378137.0
RAD = math.pi / 180.0

# Fahrbahnbreiten in Metern; nur diese Klassen werden übernommen.
ROAD_WIDTH = {
    "motorway": 12, "motorway_link": 8, "trunk": 10, "trunk_link": 7,
    "primary": 9, "primary_link": 7, "secondary": 8, "secondary_link": 6.5,
    "tertiary": 7, "tertiary_link": 6, "unclassified": 5.5, "residential": 6,
    "living_street": 5.5, "service": 4, "track": 3.5, "road": 5.5,
}


def project(lat, lon, lat0, lon0, cos0):
    x = (lon - lon0) * RAD * EARTH * cos0
    z = -(lat - lat0) * RAD * EARTH
    return round(x, 1), round(z, 1)


class Collector(osmium.SimpleHandler):
    def __init__(self, lat0, lon0, radius_m, cell):
        super().__init__()
        self.lat0, self.lon0 = lat0, lon0
        self.cos0 = math.cos(lat0 * RAD)
        self.r2 = radius_m * radius_m
        self.cell = cell
        self.tiles = defaultdict(lambda: {"roads": [], "buildings": [], "addresses": []})
        self.n_roads = self.n_bld = self.n_addr = 0
        self.skipped = 0

    def inside(self, x, z):
        return x * x + z * z <= self.r2

    def tile_key(self, x, z):
        return (math.floor(x / self.cell), math.floor(z / self.cell))

    def tiles_for_points(self, pts):
        keys = set()
        for i in range(len(pts)):
            keys.add(self.tile_key(*pts[i]))
            if i:
                # Kacheln entlang des Segments abdecken (bei langen Segmenten)
                ax, az = pts[i - 1]; bx, bz = pts[i]
                n = int(math.hypot(bx - ax, bz - az) / self.cell) + 1
                for k in range(1, n):
                    t = k / n
                    keys.add(self.tile_key(ax + (bx - ax) * t, az + (bz - az) * t))
        return keys

    def way(self, w):
        tags = w.tags
        hw = tags.get("highway")
        is_bld = "building" in tags
        if not hw and not is_bld:
            return
        if hw and (hw not in ROAD_WIDTH or tags.get("area") == "yes"):
            return
        try:
            pts = [project(n.lat, n.lon, self.lat0, self.lon0, self.cos0) for n in w.nodes]
        except osmium.InvalidLocationError:
            self.skipped += 1
            return
        if len(pts) < 2 or not any(self.inside(x, z) for x, z in pts):
            return

        if hw:
            rec = {"w": ROAD_WIDTH[hw], "p": pts}
            if tags.get("name"):
                rec["n"] = tags["name"]
            if tags.get("oneway") == "yes":
                rec["o"] = 1
            for key in self.tiles_for_points(pts):
                self.tiles[key]["roads"].append(rec)
            self.n_roads += 1
            return

        # Gebäude: geschlossener Ring, mindestens 4 Punkte (letzter = erster)
        if len(pts) < 4:
            return
        if pts[0] == pts[-1]:
            pts = pts[:-1]
        if len(pts) < 3:
            return
        height = None
        try:
            if tags.get("height"):
                height = float(tags["height"].split()[0].replace(",", "."))
        except ValueError:
            height = None
        if not height:
            try:
                levels = float(tags.get("building:levels", "").replace(",", "."))
                height = levels * 3.2 + 1.2
            except ValueError:
                height = 3.0 if tags.get("building") in ("garage", "shed", "hut", "roof") else 8.0
        cx = sum(p[0] for p in pts) / len(pts)
        cz = sum(p[1] for p in pts) / len(pts)
        rec = {"h": round(height, 1), "p": pts}
        self.tiles[self.tile_key(cx, cz)]["buildings"].append(rec)
        self.n_bld += 1
        if tags.get("addr:housenumber"):
            self.add_address(cx, cz, tags)

    def node(self, n):
        tags = n.tags
        if "addr:housenumber" not in tags:
            return
        x, z = project(n.location.lat, n.location.lon, self.lat0, self.lon0, self.cos0)
        if self.inside(x, z):
            self.add_address(x, z, tags)

    def add_address(self, x, z, tags):
        street = tags.get("addr:street", "")
        text = (street + " " if street else "") + tags["addr:housenumber"]
        self.tiles[self.tile_key(x, z)]["addresses"].append(
            {"x": round(x, 1), "z": round(z, 1), "t": text})
        self.n_addr += 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pbf")
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lng", type=float, required=True)
    ap.add_argument("--radius-km", type=float, required=True)
    ap.add_argument("--cell", type=int, default=500, help="Kachelkante in Metern")
    ap.add_argument("--out", default="www/data")
    args = ap.parse_args()

    t0 = time.time()
    col = Collector(args.lat, args.lng, args.radius_km * 1000, args.cell)
    col.apply_file(args.pbf, locations=True, idx="flex_mem")
    print(f"gelesen in {time.time()-t0:.0f}s: {col.n_roads} Straßenzüge, "
          f"{col.n_bld} Gebäude, {col.n_addr} Hausnummern, {col.skipped} Wege ohne Geometrie")

    tiles_dir = os.path.join(args.out, "tiles")
    os.makedirs(tiles_dir, exist_ok=True)
    for f in os.listdir(tiles_dir):
        os.remove(os.path.join(tiles_dir, f))

    index = {
        "origin": {"lat": args.lat, "lng": args.lng},
        "radius_m": int(args.radius_km * 1000),
        "cell": args.cell,
        "built": time.strftime("%Y-%m-%d"),
        "tiles": {},
    }
    total = 0
    for (ix, iz), data in col.tiles.items():
        if not (data["roads"] or data["buildings"] or data["addresses"]):
            continue
        name = f"{ix}_{iz}"
        raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        with gzip.open(os.path.join(tiles_dir, name + ".json.gz"), "wb", compresslevel=9) as fh:
            fh.write(raw)
        size = os.path.getsize(os.path.join(tiles_dir, name + ".json.gz"))
        total += size
        index["tiles"][name] = [len(data["roads"]), len(data["buildings"]), len(data["addresses"])]

    with open(os.path.join(args.out, "index.json"), "w", encoding="utf-8") as fh:
        json.dump(index, fh, ensure_ascii=False, separators=(",", ":"))

    print(f"{len(index['tiles'])} Kacheln, {total/1e6:.1f} MB komprimiert")
    if total > 150e6:
        print("WARNUNG: über 150 MB – Radius verkleinern oder Gebäude reduzieren", file=sys.stderr)


if __name__ == "__main__":
    main()