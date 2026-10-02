"""Builds ``app/seed/areas.json``, the §10.6 areas gazetteer, from public data.gov.sg datasets.

Not run by the bot: the generated JSON is committed and seeded at startup. To refresh it,
download these GeoJSON files (data.gov.sg → dataset → Download, or the ``poll-download`` API)
into one folder and run ``python -m scripts.build_areas <folder>``:

- ``planning.geojson``: Master Plan 2019 Planning Area Boundary (No Sea),
  ``d_4765db0e87b9c86336792efe8a1f7a66``
- ``subzone.geojson``: Master Plan 2019 Subzone Boundary (No Sea),
  ``d_8594ae9ff96d0c708bc2af633048edfb``
- ``mrt_exits.geojson``: LTA MRT Station Exit (GEOJSON), ``d_b39d3a0871985372d7e1637193335da5``

Centres are polygon centroids (planning areas, subzones) or the mean of a station's exits.
Hand-added neighbourhoods and extra aliases (short forms, Chinese names) are below.
"""

from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path
from typing import Any

from app.decisions.text import normalise

OUT = Path(__file__).resolve().parent.parent / "app" / "seed" / "areas.json"

STATION_RADIUS_M = 1000
SUBZONE_RADIUS = (1000, 1500)
PLANNING_RADIUS = (1500, 3000)

# Popular neighbourhoods that aren't a planning area, subzone or station of the same name.
NEIGHBOURHOODS: list[dict[str, Any]] = [
    {
        "name": "Holland Village",
        "lat": 1.3113,
        "lng": 103.7958,
        "radius_m": 800,
        "aliases": ["holland v", "hv", "荷兰村"],
    },
    {
        "name": "Dempsey Hill",
        "lat": 1.3046,
        "lng": 103.8095,
        "radius_m": 700,
        "aliases": ["dempsey"],
    },
    {"name": "Robertson Quay", "lat": 1.2906, "lng": 103.8389, "radius_m": 600, "aliases": []},
    {
        "name": "Keong Saik",
        "lat": 1.2806,
        "lng": 103.8418,
        "radius_m": 500,
        "aliases": ["keong saik road"],
    },
    {"name": "Duxton Hill", "lat": 1.2789, "lng": 103.8432, "radius_m": 500, "aliases": ["duxton"]},
    {"name": "Joo Chiat", "lat": 1.3135, "lng": 103.9020, "radius_m": 1000, "aliases": ["如切"]},
    {"name": "Katong", "lat": 1.3050, "lng": 103.9050, "radius_m": 1000, "aliases": ["加东"]},
    {
        "name": "Kampong Glam",
        "lat": 1.3022,
        "lng": 103.8590,
        "radius_m": 600,
        "aliases": ["kampong gelam", "haji lane", "arab street"],
    },
    {
        "name": "East Coast Park",
        "lat": 1.3010,
        "lng": 103.9120,
        "radius_m": 2000,
        "aliases": ["ecp"],
    },
]

# Extra aliases for areas from the datasets (target = area name as generated).
EXTRA_ALIASES: dict[str, list[str]] = {
    "Tiong Bahru": ["tb", "中峇鲁"],
    "Ang Mo Kio": ["amk", "宏茂桥"],
    "Choa Chu Kang": ["cck", "蔡厝港"],
    "Toa Payoh": ["tpy", "大巴窑"],
    "Bukit Timah": ["bt timah", "武吉知马"],
    "Bukit Batok": ["bt batok"],
    "Bukit Merah": ["bt merah"],
    "Bukit Panjang": ["bt panjang"],
    "Downtown Core": ["cbd"],
    "Orchard": ["orchard road", "乌节"],
    "Chinatown": ["牛车水"],
    "Little India": ["小印度"],
    "Tampines": ["淡滨尼"],
    "Yishun": ["义顺"],
    "Bishan": ["碧山"],
    "Jurong East": ["裕廊东"],
    "Bedok": ["勿洛"],
    "Serangoon": ["实龙岗"],
    "Hougang": ["后港"],
    "Sengkang": ["盛港"],
    "Punggol": ["榜鹅"],
    "Sentosa": ["圣淘沙"],
    "Serangoon Garden": ["serangoon gardens", "sg gardens"],
}

_SMALL = {"of", "the"}


def title(raw: str) -> str:
    words = raw.strip().lower().split()
    out = [
        w if w in _SMALL and i else "-".join(p.capitalize() for p in w.split("-"))
        for i, w in enumerate(words)
    ]
    return " ".join(out)


def _ring_centroid(ring: list[list[float]]) -> tuple[float, float, float]:
    """(signed area, cx, cy) of one ring in lng/lat degrees (shoelace)."""
    a = cx = cy = 0.0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1], strict=True):
        cross = x1 * y2 - x2 * y1
        a += cross
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    a /= 2
    if a == 0:
        return 0.0, ring[0][0], ring[0][1]
    return a, cx / (6 * a), cy / (6 * a)


def centroid_and_radius(geom: dict[str, Any], clip: tuple[int, int]) -> tuple[float, float, int]:
    polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
    total = sx = sy = 0.0
    for poly in polys:
        for i, ring in enumerate(poly):
            pts = [p[:2] for p in ring]
            a, cx, cy = _ring_centroid(pts)
            a = abs(a) * (1 if i == 0 else -1)  # holes subtract
            total += a
            sx += a * cx
            sy += a * cy
    lng, lat = sx / total, sy / total
    m2 = total * 110_574 * 111_320 * math.cos(math.radians(lat))
    radius = math.sqrt(m2 / math.pi)
    return round(lat, 5), round(lng, 5), int(min(max(radius, clip[0]), clip[1]) // 50 * 50)


_STATION = re.compile(r"^(.*) (MRT|LRT) STATION$")


def build(folder: Path) -> list[dict[str, Any]]:
    areas: dict[str, dict[str, Any]] = {}

    def add(name: str, kind: str, lat: float, lng: float, radius: int, aliases: list[str]) -> None:
        if name not in areas:
            areas[name] = {
                "name": name,
                "kind": kind,
                "lat": lat,
                "lng": lng,
                "radius_m": radius,
                "aliases": aliases,
            }

    for n in NEIGHBOURHOODS:
        add(n["name"], "neighbourhood", n["lat"], n["lng"], n["radius_m"], list(n["aliases"]))
    for f in json.loads((folder / "planning.geojson").read_text())["features"]:
        lat, lng, r = centroid_and_radius(f["geometry"], PLANNING_RADIUS)
        add(title(f["properties"]["PLN_AREA_N"]), "planning_area", lat, lng, r, [])
    for f in json.loads((folder / "subzone.geojson").read_text())["features"]:
        lat, lng, r = centroid_and_radius(f["geometry"], SUBZONE_RADIUS)
        add(title(f["properties"]["SUBZONE_N"]), "subzone", lat, lng, r, [])

    exits: dict[tuple[str, str], list[tuple[float, float]]] = {}
    for f in json.loads((folder / "mrt_exits.geojson").read_text())["features"]:
        m = _STATION.match(f["properties"]["STATION_NA"].strip())
        if m is None:
            continue  # stations still under construction are listed by code only
        lng, lat = f["geometry"]["coordinates"][:2]
        exits.setdefault((title(m.group(1)), m.group(2)), []).append((lat, lng))
    for (base, line), pts in sorted(exits.items(), key=lambda kv: (kv[0][1] != "MRT", kv[0][0])):
        lat = round(sum(p[0] for p in pts) / len(pts), 5)
        lng = round(sum(p[1] for p in pts) / len(pts), 5)
        add(f"{base} {line}", "mrt", lat, lng, STATION_RADIUS_M, [])

    for name, extra in EXTRA_ALIASES.items():
        if name not in areas:
            raise SystemExit(f"EXTRA_ALIASES target {name!r} isn't an area")
        areas[name]["aliases"] += extra

    # Aliases are unique: the area's own name first, then a station's short forms ("tiong bahru
    # mrt", "tiong bahru station"), and a bare station name only if no area already has it.
    taken: set[str] = set()
    for a in areas.values():
        own = [normalise(a["name"]), *(normalise(x) for x in a["aliases"])]
        a["aliases"] = [x for x in dict.fromkeys(own) if x and x not in taken]
        taken.update(a["aliases"])
    for a in areas.values():
        if a["kind"] != "mrt":
            continue
        base, line = a["name"].rsplit(" ", 1)
        extra = [f"{base} {line} station", f"{base} station", base]
        for x in (normalise(e) for e in extra):
            if x not in taken:
                a["aliases"].append(x)
                taken.add(x)
    return sorted(areas.values(), key=lambda a: (a["kind"] != "neighbourhood", a["name"]))


def main() -> None:
    folder = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    areas = build(folder)
    lines = ",\n".join("  " + json.dumps(a, ensure_ascii=False) for a in areas)
    OUT.write_text(f"[\n{lines}\n]\n", encoding="utf-8")
    kinds: dict[str, int] = {}
    for a in areas:
        kinds[a["kind"]] = kinds.get(a["kind"], 0) + 1
    print(f"wrote {len(areas)} areas to {OUT}: {kinds}")


if __name__ == "__main__":
    main()
