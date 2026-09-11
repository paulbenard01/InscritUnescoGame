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


class Figure:
    """The little guy on his own -- head, torso, arms, legs -- smooth-unioned.

    The disc is deliberately NOT part of this field. Tracing the subtraction
    meant the outer edge came back as a smoothed polyline pretending to be a
    circle, and it showed: the rim was subtly out of round. The rim is drawn as
    true arcs now, and only the figure is traced.
    """

    def __init__(self, spec):
        self.s = spec

    def __call__(self, x, y):
        s = self.s
        k = s["fillet"]
        d = circle(x, y, *s["head"])
        d = smin(d, capsule(x, y, *s["torso"]), k)
        for limb in s["arms"] + s["legs"]:
            d = smin(d, capsule(x, y, *limb), k)
        return d


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


def run_to_bezier(points, decimals=1):
    """An OPEN stretch of curve: Catmull-Rom through the points, with the ends
    clamped. Used for the part of the outline that follows the figure, between
    the places where it crosses the rim."""
    n = len(points)
    f = lambda v: f"{round(v, decimals):g}"
    at = lambda i: points[max(0, min(n - 1, i))]
    d = []
    for i in range(n - 1):
        p0, p1, p2, p3 = at(i - 1), points[i], points[i + 1], at(i + 2)
        c1 = (p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6)
        c2 = (p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6)
        d.append(f"C{f(c1[0])} {f(c1[1])},{f(c2[0])} {f(c2[1])},{f(p2[0])} {f(p2[1])}")
    return "".join(d)


def crossings(points, cx, cy, r):
    """Where the figure's outline crosses the rim, and exactly on it.

    Returns [(index, point)] in contour order. The interpolated point is pushed
    out to radius r to the last decimal, so an arc can start and end on it
    without a kink.
    """
    out = []
    n = len(points)
    for i in range(n):
        a, b = points[i], points[(i + 1) % n]
        da = math.hypot(a[0] - cx, a[1] - cy) - r
        db = math.hypot(b[0] - cx, b[1] - cy) - r
        if (da <= 0 < db) or (db <= 0 < da):
            t = da / (da - db)
            px = a[0] + (b[0] - a[0]) * t
            py = a[1] + (b[1] - a[1]) * t
            m = math.hypot(px - cx, py - cy) or 1.0
            out.append((i, (cx + (px - cx) * r / m, cy + (py - cy) * r / m)))
    return out


def arc_back(start, end, cx, cy, r, inside_figure, decimals=1):
    """The rim, from end round to start, taking whichever of the two arcs does
    not run through the figure. One exact A command -- a real circle, not a
    polyline impersonating one."""
    f = lambda v: f"{round(v, decimals):g}"
    a0 = math.atan2(end[1] - cy, end[0] - cx)
    a1 = math.atan2(start[1] - cy, start[0] - cx)
    for sweep in (1, 0):
        delta = (a1 - a0) % (2 * math.pi) if sweep else -((a0 - a1) % (2 * math.pi))
        mid = a0 + delta / 2
        if inside_figure(cx + r * math.cos(mid), cy + r * math.sin(mid)) > 0:
            large = 1 if abs(delta) > math.pi else 0
            return f"A{f(r)} {f(r)} 0 {large} {sweep} {f(start[0])} {f(start[1])}"
    return f"L{f(start[0])} {f(start[1])}"      # should not happen


def build(spec, n=640, spacing=9.0):
    """Trace the figure, then close each region with a true arc of the rim.

    The outline of the mark is two different things: where the figure bounds it
    the edge is a traced curve, and where the rim bounds it the edge is a
    circle. Tracing both gave a wobbly circle, so each is now produced the way
    it should be and they are stitched at the crossings.
    """
    s = spec
    cx, cy, r = s["disc"]
    field = Figure(spec)
    loops = contours(field, n=n)
    if not loops:
        return "", 0
    # The figure is one connected blob, so one contour.
    pts = resample(smooth(resample(max(loops, key=len), 2.5)), spacing)
    xs = crossings(pts, cx, cy, r)
    if len(xs) < 2:
        raise SystemExit("the figure does not cross the rim -- nothing to open")

    inside = lambda x, y: math.hypot(x - cx, y - cy) <= r
    out = []
    for k in range(len(xs)):
        i0, p0 = xs[k]
        i1, p1 = xs[(k + 1) % len(xs)]
        # The stretch of contour between this crossing and the next.
        idx = [(i0 + 1 + j) % len(pts) for j in range((i1 - i0) % len(pts))]
        stretch = [p0] + [pts[j] for j in idx] + [p1]
        mid = stretch[len(stretch) // 2]
        if not inside(*mid):
            continue                      # a foot, outside the disc: not an edge
        f = lambda v: f"{round(v, 1):g}"
        out.append(f"M{f(p0[0])} {f(p0[1])}" + run_to_bezier(stretch)
                   + arc_back(p0, p1, cx, cy, r, field) + "Z")
    return "".join(out), len(out)


# --------------------------------------------------------------- the figure --

# The disc sits high in the frame so the legs have somewhere to spill.
DISC = (128, 114, 100)

# Two weights of the same figure. The heavier one has thicker limbs and a
# bigger head, because at sixteen pixels in a browser tab the lighter one
# closes up into a blob.
SHAPES = {
    # Proportions worth writing down, because two rounds went wrong on them.
    #
    # The legs must leave the BOTTOM of the torso, not its sides. When they
    # started 31px above the torso's lower cap they sprouted from the middle of
    # his body, forking at y144 with the arms joining at y111 -- 32px of torso
    # between shoulder and hip, which read as hips up under the armpits and a
    # crotch to match.
    #
    # And they must be wide-set and near-vertical, with the torso widened to
    # match, so the gap between them is wide enough to read as two legs. Fork
    # the legs low AND splay them outward and the fillet merges them into one
    # flared trunk -- a robe, not a person. The two constraints pull against
    # each other; this is where they balance.
    "logo": dict(
        disc=DISC, fillet=7,
        head=(130, 58, 25),
        torso=(128, 100, 128, 138, 28),
        arms=[(112, 112, 62, 84, 12), (144, 110, 194, 72, 12)],
        legs=[(107, 148, 100, 240, 11), (149, 148, 156, 240, 11)],
    ),
    "favicon": dict(
        disc=DISC, fillet=8,
        head=(130, 60, 28),
        torso=(128, 102, 128, 138, 30),
        arms=[(112, 114, 62, 86, 14), (144, 112, 194, 76, 14)],
        legs=[(106, 148, 98, 238, 13), (150, 148, 158, 238, 13)],
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
