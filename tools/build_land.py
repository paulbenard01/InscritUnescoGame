"""Build the detailed map geometry the game draws.

The map is the puzzle now, so it has to survive being zoomed into. The
geometry compiled into heritle.html is Natural Earth 110m land, pre-projected
to integer coordinates on a 440x220 canvas: one unit is about 90 km, so past
two or three times zoom the coastline turns into a staircase and there are no
country borders at all to place a guess against.

This builds a finer replacement -- Natural Earth 50m *countries*, so the map
carries internal borders as well as coastline -- and writes it to
data/land.json, which the game fetches the way it fetches the dataset. The
coarse inline path stays in the HTML as the fallback, so opening the file
directly over file:// still draws a world.

Run:  python tools/build_land.py            # writes data/land.json
      python tools/build_land.py --check    # report size and vertex count only

Needs network, so in practice it runs in CI (.github/workflows/build-land.yml).
"""
import argparse
import json
import math
import os
import sys

import requests

# Natural Earth is public domain. This mirror serves it as plain GeoJSON, which
# avoids decoding TopoJSON quantisation by hand for no benefit.
SOURCE = ("https://raw.githubusercontent.com/nvkelso/natural-earth-vector/"
          "master/geojson/ne_50m_admin_0_countries.geojson")
ATTRIBUTION = "Natural Earth (public domain), 50m admin-0 countries"

MAP_W, MAP_H = 440, 220        # must match the constants in heritle.html
MAX_ZOOM = 12                  # likewise

# At full zoom the view is MAP_W/MAX_ZOOM units wide across roughly 360 screen
# pixels, so one world unit is about 10 px and a tenth of a unit is one pixel.
# Simplifying below that spends bytes on detail no one can see.
TOLERANCE = 0.5 / MAX_ZOOM     # world units
DECIMALS = 2
# Rings smaller than about a pixel at full zoom are noise at every zoom level.
MIN_RING_EXTENT = 0.12

# Natural Earth already splits its geometries at the antimeridian, so a ring
# that still wraps is unexpected. Count them rather than discarding quietly.
STATS = {"wrapped": 0}


def project(lng, lat):
    return ((lng + 180.0) / 360.0 * MAP_W, (90.0 - lat) / 180.0 * MAP_H)


def simplify(points, tol):
    """Douglas-Peucker, iterative so a long coastline cannot blow the stack."""
    if len(points) < 3:
        return points
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        first, last = stack.pop()
        if last <= first + 1:
            continue
        ax, ay = points[first]
        bx, by = points[last]
        dx, dy = bx - ax, by - ay
        norm = math.hypot(dx, dy)
        worst, worst_i = -1.0, -1
        for i in range(first + 1, last):
            px, py = points[i]
            if norm == 0:
                d = math.hypot(px - ax, py - ay)
            else:
                d = abs(dy * px - dx * py + bx * ay - by * ax) / norm
            if d > worst:
                worst, worst_i = d, i
        if worst > tol:
            keep[worst_i] = True
            stack.append((first, worst_i))
            stack.append((worst_i, last))
    return [p for p, k in zip(points, keep) if k]


def ring_to_path(ring):
    """One projected, simplified ring, as SVG path data and as raw subpaths.

    The path draws it; the subpaths are what a tap is tested against, so both
    come from exactly the same simplification -- a country whose outline is
    drawn one way and hit-tested another would reject taps that visibly land
    inside it.

    Rings that straddle the antimeridian are dropped at the wrap rather than
    drawn across it: on an equirectangular canvas a segment from +179 to -179
    is a line all the way back across the map, which is what put streaks over
    the old map.
    """
    pts = [project(x, y) for x, y in ring
           if isinstance(x, (int, float)) and isinstance(y, (int, float))]
    if len(pts) < 4:
        return None, []
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    if (max(xs) - min(xs)) < MIN_RING_EXTENT and (max(ys) - min(ys)) < MIN_RING_EXTENT:
        return None, []

    pts = simplify(pts, TOLERANCE)
    if len(pts) < 4:
        return None, []

    out, prev = [], None
    parts = []
    for x, y in pts:
        x, y = round(x, DECIMALS), round(y, DECIMALS)
        if prev is not None and abs(x - prev[0]) > MAP_W / 2:
            # Wraps the antimeridian: end this subpath and start a new one.
            # Joining across would draw a line back over the whole map, which
            # is what streaked the old one.
            STATS["wrapped"] += 1
            if len(out) >= 3:
                parts.append(out)
            out = []
            prev = None
        if prev == (x, y):
            continue
        out.append((x, y))
        prev = (x, y)
    if len(out) >= 3:
        parts.append(out)

    def fmt(v):
        s = f"{v:.{DECIMALS}f}".rstrip("0").rstrip(".")
        return s if s not in ("", "-0") else "0"

    chunks = []
    for part in parts:
        chunks.append("M" + "L".join(f"{fmt(x)},{fmt(y)}" for x, y in part) + "Z")
    return ("".join(chunks) or None), parts


def iso_of(props):
    """Natural Earth's ISO code for a feature, or None.

    ISO_A2 is "-99" for places without one -- disputed or partially recognised
    territories, mostly. Those are still drawn; they just cannot be guessed,
    which matches the dataset, whose countries all come from Wikidata's P297.
    """
    for key in ("ISO_A2_EH", "ISO_A2", "iso_a2_eh", "iso_a2"):
        v = props.get(key)
        if v and v not in ("-99", "-099") and len(v) == 2:
            return v.upper()
    return None


def build():
    print(f"fetching {SOURCE}")
    r = requests.get(SOURCE, timeout=300)
    r.raise_for_status()
    gj = r.json()
    feats = gj.get("features", [])
    print(f"  {len(feats)} features")

    rings, dropped, no_iso = 0, 0, 0
    shapes = {}          # ISO -> list of rings, each a flat [x,y,x,y,...]
    other = []           # rings with no ISO: drawn, but not guessable
    for f in feats:
        geom = f.get("geometry") or {}
        kind, coords = geom.get("type"), geom.get("coordinates")
        if kind == "Polygon":
            polys = [coords]
        elif kind == "MultiPolygon":
            polys = coords
        else:
            continue
        iso = iso_of(f.get("properties") or {})
        if not iso:
            no_iso += 1
        for poly in polys:
            for ring in poly:          # ring 0 is the outline, the rest holes
                _d, parts = ring_to_path(ring)
                if not parts:
                    dropped += 1
                    continue
                rings += 1
                for part in parts:
                    flat = []
                    for x, y in part:
                        flat.append(round(x, DECIMALS))
                        flat.append(round(y, DECIMALS))
                    if iso:
                        shapes.setdefault(iso, []).append(flat)
                    else:
                        other.append(flat)

    print(f"  {rings} rings kept, {dropped} too small or degenerate, "
          f"{STATS['wrapped']} antimeridian splits")
    print(f"  {len(shapes)} countries hit-testable, {no_iso} features with no ISO code")
    # The rings are the only copy of the geometry. The game builds its drawing
    # path from them, so what is drawn and what a tap is tested against cannot
    # drift apart -- and the file is not carrying the same coastlines twice.
    return {
        "shapes": shapes,
        "other": other,
        "width": MAP_W,
        "height": MAP_H,
        "source": SOURCE,
        "attribution": ATTRIBUTION,
        "tolerance": TOLERANCE,
    }


def main():
    # Before anything reads these names: the argument defaults below do, and
    # Python rejects a global declaration that comes after a use in the same
    # function. It is a compile-time error that ast.parse does not see -- only
    # compiling the module does, which is why it reached CI.
    global SOURCE, TOLERANCE, MIN_RING_EXTENT

    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "land.json"))
    ap.add_argument("--check", action="store_true",
                    help="report the size it would write, without writing")
    ap.add_argument("--scale", default="50m", choices=["110m", "50m", "10m"],
                    help="Natural Earth resolution to build from")
    ap.add_argument("--max-zoom", type=int, default=MAX_ZOOM,
                    help="deepest zoom the geometry must survive; sets the tolerance")
    ap.add_argument("--min-extent", type=float, default=MIN_RING_EXTENT,
                    help="drop rings smaller than this, in world units")
    args = ap.parse_args()

    # Resolution, detail and the smallest island kept are one decision, so they
    # move together: a finer source is pointless if the simplification then
    # throws the detail away again.
    SOURCE = SOURCE.replace("ne_50m", "ne_" + args.scale)
    TOLERANCE = 0.5 / args.max_zoom
    MIN_RING_EXTENT = args.min_extent
    print(f"scale={args.scale} max_zoom={args.max_zoom} "
          f"tolerance={TOLERANCE:.4f} ({TOLERANCE * 40075 / MAP_W:.1f} km) "
          f"min_extent={MIN_RING_EXTENT} ({MIN_RING_EXTENT * 40075 / MAP_W:.1f} km)")

    data = build()
    blob = json.dumps(data, separators=(",", ":"))
    kb = len(blob) / 1024
    verts = sum(len(r) for rs in data["shapes"].values() for r in rs) // 2
    verts += sum(len(r) for r in data["other"]) // 2
    print(f"  {verts} vertices, {kb:.0f} KB of JSON")
    if kb > 1600:
        print("  WARNING: larger than intended -- raise TOLERANCE and rebuild.",
              file=sys.stderr)
    if args.check:
        return 0
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as fh:
        fh.write(blob)
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
