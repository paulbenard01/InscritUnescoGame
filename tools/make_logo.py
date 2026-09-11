"""Build the Heritle mark: a figure traced out of a disc.

    python tools/make_logo.py          # writes assets/*.svg

The mark is not assembled from shapes. The figure is a signed distance field --
head, torso, two arms, two legs -- unioned with a *smooth* minimum so every
joint is filleted instead of butted together, then subtracted from the disc.
The zero contour of that field is traced with marching squares and fitted with
centripetal Catmull-Rom, so the outline is one continuous curve: no straight
edges, no corners, no seams where two primitives meet.

Hard geometry smashed together is what the first attempt was, and it looked it.
A person is not a stack of rectangles.

The legs run past the bottom of the disc, so the carve reaches the rim and
opens the circle rather than sitting politely inside it. The figure leans
slightly and one arm is higher than the other: a perfectly symmetrical mark
reads as machined, and this one is about people.

Only the disc is painted -- the figure is a hole in it -- so the mark works on
any background and inverts by changing one colour. logo.svg takes
currentColor and inherits whatever it is placed in.

Pure Python on purpose: numpy is not installed in the build sandbox.
"""
import math

# ---------------------------------------------------------------- the field --

def circle(px, py, cx, cy, r):
    return math.hypot(px - cx, py - cy) - r


def capsule(px, py, ax, ay, bx, by, r):
    """Distance to a thick segment -- an organic limb, round at both ends."""
    apx, apy = px - ax, py - ay
    abx, aby = bx - ax, by - ay
    denom = abx * abx + aby * aby
    t = 0.0 if denom == 0 else max(0.0, min(1.0, (apx * abx + apy * aby) / denom))
    return math.hypot(apx - abx * t, apy - aby * t) - r


def smin(a, b, k):
    """Polynomial smooth minimum: a union that fillets the joint instead of
    creasing it. k is the radius of the fillet, in the same units as the field."""
    if k <= 0:
        return min(a, b)
    h = max(0.0, min(1.0, 0.5 + 0.5 * (b - a) / k))
    return b * (1 - h) + a * h - k * h * (1 - h)


def smax(a, b, k):
    return -smin(-a, -b, k)


class Mark:
    """disc minus figure, as one field."""

    def __init__(self, spec):
        self.s = spec

    def figure(self, x, y):
        s = self.s
        k = s["fillet"]
        d = circle(x, y, s["head"][0], s["head"][1], s["head"][2])
        d = smin(d, capsule(x, y, *s["torso"]), k)
        for limb in s["arms"] + s["legs"]:
            d = smin(d, capsule(x, y, *limb), k)
        return d

    def __call__(self, x, y):
        s = self.s
        disc = circle(x, y, s["disc"][0], s["disc"][1], s["disc"][2])
        # Soften where the carve meets the rim, so the legs open the circle
        # rather than chipping it.
        return smax(disc, -self.figure(x, y), s["carve"])


# ------------------------------------------------------------ marching squares --

def contours(field, lo=0.0, hi=256.0, n=384):
    """Zero contours of the field, as closed polylines."""
    step = (hi - lo) / n
    grid = [[field(lo + i * step, lo + j * step) for i in range(n + 1)]
            for j in range(n + 1)]

    def interp(v1, v2, p1, p2):
        if v1 == v2:
            return p1
        t = v1 / (v1 - v2)
        return (p1[0] + (p2[0] - p1[0]) * t, p1[1] + (p2[1] - p1[1]) * t)

    segs = []
    for j in range(n):
        for i in range(n):
            x0, y0 = lo + i * step, lo + j * step
            x1, y1 = x0 + step, y0 + step
            a, b = grid[j][i], grid[j][i + 1]
            c, d = grid[j + 1][i + 1], grid[j + 1][i]
            idx = (1 if a < 0 else 0) | (2 if b < 0 else 0) | \
                  (4 if c < 0 else 0) | (8 if d < 0 else 0)
            if idx in (0, 15):
                continue
            top = interp(a, b, (x0, y0), (x1, y0))
            right = interp(b, c, (x1, y0), (x1, y1))
            bottom = interp(d, c, (x0, y1), (x1, y1))
            left = interp(a, d, (x0, y0), (x0, y1))
            table = {
                1: [(left, top)], 2: [(top, right)], 3: [(left, right)],
                4: [(right, bottom)], 6: [(top, bottom)], 7: [(left, bottom)],
                8: [(bottom, left)], 9: [(bottom, top)], 11: [(bottom, right)],
                12: [(right, left)], 13: [(right, top)], 14: [(top, left)],
                5: [(left, top), (right, bottom)],      # saddle
                10: [(top, right), (bottom, left)],
            }
            segs.extend(table[idx])

    # Stitch the segments into closed loops by matching endpoints.
    key = lambda p: (round(p[0], 4), round(p[1], 4))
    starts = {}
    for s0, s1 in segs:
        starts.setdefault(key(s0), []).append((s0, s1))
    loops, used = [], set()
    for seg in segs:
        if id(seg) in used:
            continue
        loop = [seg[0], seg[1]]
        used.add(id(seg))
        cur = seg[1]
        while True:
            nxt = None
            for cand in starts.get(key(cur), []):
                if id(cand) not in used:
                    nxt = cand
                    break
            if nxt is None:
                break
            used.add(id(nxt))
            cur = nxt[1]
            if key(cur) == key(loop[0]):
                break
            loop.append(cur)
        if len(loop) > 12:
            loops.append(loop)
    return loops


# --------------------------------------------------------------- smoothing --

def resample(points, spacing):
    """Even spacing along the curve, so the fit has no crowded or starved runs."""
    pts = points[:]
    if pts[0] != pts[-1]:
        pts.append(pts[0])
    total = sum(math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))
    count = max(12, int(round(total / spacing)))
    step = total / count
    out, i, carried = [pts[0]], 0, 0.0
    for _ in range(count - 1):
        want = step
        while i < len(pts) - 1:
            seg = math.dist(pts[i], pts[i + 1]) - carried
            if seg >= want:
                t = (carried + want) / max(1e-9, math.dist(pts[i], pts[i + 1]))
                out.append((pts[i][0] + (pts[i + 1][0] - pts[i][0]) * t,
                            pts[i][1] + (pts[i + 1][1] - pts[i][1]) * t))
                carried += want
                break
            want -= seg
            carried = 0.0
            i += 1
        else:
            break
    return out


def smooth(points, passes=12, lam=0.5, mu=-0.53):
    """Taubin smoothing on a closed polyline.

    Marching squares on a grid returns a slightly bumpy trace, and
    Catmull-Rom interpolates every point exactly -- so that bumpiness came out
    as visible ripple along the outline. This takes the high frequencies out
    before the curve is fitted. Taubin rather than plain Laplacian because
    Laplacian shrinks a closed loop a little on every pass, which would eat the
    limbs; the alternating positive and negative steps cancel that out.
    """
    pts = points[:]
    n = len(pts)
    for k in range(passes):
        w = lam if k % 2 == 0 else mu
        out = []
        for i in range(n):
            x0, y0 = pts[(i - 1) % n]
            x1, y1 = pts[i]
            x2, y2 = pts[(i + 1) % n]
            out.append((x1 + w * ((x0 + x2) / 2 - x1),
                        y1 + w * ((y0 + y2) / 2 - y1)))
        pts = out
    return pts


def to_bezier(points, decimals=1):
    """Closed cubic path through the points, Catmull-Rom converted to Bezier.
    Every node is smooth by construction -- there is nowhere for a corner."""
    n = len(points)
    f = lambda v: f"{round(v, decimals):g}"
    d = [f"M{f(points[0][0])} {f(points[0][1])}"]
    for i in range(n):
        p0 = points[(i - 1) % n]
        p1 = points[i]
        p2 = points[(i + 1) % n]
        p3 = points[(i + 2) % n]
        c1 = (p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6)
        c2 = (p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6)
        d.append(f"C{f(c1[0])} {f(c1[1])},{f(c2[0])} {f(c2[1])},{f(p2[0])} {f(p2[1])}")
    return "".join(d) + "Z"


def build(spec, n=640, spacing=9.0):
    """Trace, de-noise, then fit.

    A finer grid for a cleaner trace; a dense resample so smoothing has
    something even to work on; then a coarser resample for the fit, because
    fewer well-spaced nodes give a calmer curve than many crowded ones.
    """
    out = []
    for loop in contours(Mark(spec), n=n):
        dense = resample(loop, 2.5)
        out.append(to_bezier(resample(smooth(dense), spacing)))
    return "".join(out), len(out)


# --------------------------------------------------------------- the figure --

# The disc sits high in the frame so the legs have somewhere to spill.
DISC = (128, 114, 100)

# Two weights of the same figure. The heavier one has thicker limbs and a
# bigger head, because at sixteen pixels in a browser tab the lighter one
# closes up into a blob.
SHAPES = {
    "logo": dict(
        disc=DISC, fillet=8, carve=4,
        head=(131, 58, 24),
        torso=(128, 104, 127, 148, 27),
        arms=[(116, 112, 66, 80, 12), (140, 110, 190, 68, 12)],
        legs=[(108, 144, 86, 248, 14), (148, 144, 170, 248, 14)],
    ),
    "favicon": dict(
        disc=DISC, fillet=9, carve=4,
        head=(130, 60, 28),
        torso=(128, 104, 127, 146, 31),
        arms=[(114, 114, 66, 84, 15), (142, 112, 190, 74, 15)],
        legs=[(108, 142, 88, 242, 17), (148, 142, 168, 242, 17)],
    ),
}

GOLD = "#C9A24B"


def svg(path, fill, standalone=True):
    head = '<?xml version="1.0" encoding="UTF-8"?>\n' if standalone else ''
    return (f'{head}<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256"'
            f' role="img" aria-label="Heritle">'
            f'<path fill="{fill}" fill-rule="evenodd" d="{path}"/></svg>\n')


def main():
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = os.path.join(here, "assets")
    os.makedirs(out, exist_ok=True)
    paths = {}
    for name, spec in SHAPES.items():
        d, loops = build(spec)
        paths[name] = d
        print(f"  {name}: {loops} contour(s), {len(d)} bytes of path")
    files = {
        # currentColor, so it takes the colour of whatever it sits in.
        "logo.svg": svg(paths["logo"], "currentColor"),
        "logo-gold.svg": svg(paths["logo"], GOLD),
        "favicon.svg": svg(paths["favicon"], GOLD),
    }
    for name, content in files.items():
        with open(os.path.join(out, name), "w", encoding="utf-8") as f:
            f.write(content)
        print(f"  wrote assets/{name}  ({len(content)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
