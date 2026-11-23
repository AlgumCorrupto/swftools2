"""Tiny polygon tessellator: loops (with holes, even-odd/winding by orientation) -> triangles."""
import math, heapq

def area2(p): return sum(p[i][0] * p[(i + 1) % len(p)][1] - p[(i + 1) % len(p)][0] * p[i][1] for i in range(len(p)))

TOL = 8.0   # twips (0.4 px): max distance between a curve and its flattening
SIMPLIFY = 0.0   # twips: Douglas-Peucker tolerance applied to every edge chain (0 = off). Fewer polygons, shared borders stay shared.
MIN_AREA = 0.0   # twips^2: fill loops smaller than this are dropped (specks of traced/auto-vectorised art)

def flatten_q(p0, p1, p2, tol=None):
    tol = TOL if tol is None else tol
    dev = math.hypot(p0[0] - 2 * p1[0] + p2[0], p0[1] - 2 * p1[1] + p2[1]) / 4.0     # max chord error is dev / n^2
    n = max(1, min(32, int(math.ceil(math.sqrt(dev / tol)))))
    out = []
    for k in range(1, n + 1):
        t = k / n; u = 1 - t
        out.append((round(u * u * p0[0] + 2 * u * t * p1[0] + t * t * p2[0]), round(u * u * p0[1] + 2 * u * t * p1[1] + t * t * p2[1])))
    return out

def stitch(edges):
    """edges: list of (a,b) directed; -> list of closed loops (lists of points)"""
    from collections import defaultdict
    from collections import Counter
    cnt = Counter((a, b) for a, b in edges if a != b)
    nxt = defaultdict(list)
    for (a, b), c in cnt.items():       # cancel shared interior edges (a->b against b->a)
        net = c - cnt.get((b, a), 0)
        for _ in range(max(net, 0)): nxt[a].append(b)
    loops = []
    for start in list(nxt.keys()):
        while nxt[start]:
            loop = [start]; cur = start; ok = False
            while True:
                if not nxt[cur]: break
                b = nxt[cur].pop(); 
                if b == start: ok = True; break
                loop.append(b); cur = b
            if ok and len(loop) >= 3: loops.append(loop)
    return loops

def pt_in_poly(pt, poly):
    x, y = pt; c = False
    for i in range(len(poly)):
        x1, y1 = poly[i]; x2, y2 = poly[(i + 1) % len(poly)]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1: c = not c
    return c

def _cross(o, a, b): return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

def _seg_inter(a, b, c, d):
    d1 = _cross(a, b, c); d2 = _cross(a, b, d); d3 = _cross(c, d, a); d4 = _cross(c, d, b)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)) and d1 and d2 and d3 and d4

def _clear(m, v, ring):
    """True if segment m-v crosses no edge of `ring` (bbox reject first: most edges are nowhere near the segment)"""
    mnx, mxx = (m[0], v[0]) if m[0] < v[0] else (v[0], m[0]); mny, mxy = (m[1], v[1]) if m[1] < v[1] else (v[1], m[1])
    p = ring[-1]
    for q in ring:
        px = p[0]; qx = q[0]
        if not ((px < mnx and qx < mnx) or (px > mxx and qx > mxx)):
            py = p[1]; qy = q[1]
            if not ((py < mny and qy < mny) or (py > mxy and qy > mxy)) and _seg_inter(m, v, p, q): return False
        p = q
    return True

def _wedge(p, v, q, m):
    """is point m inside the interior angle at v (prev p, next q)?  (outer ring area2>0: convex corner has cross>0)"""
    if _cross(p, v, q) > 0: return _cross(v, m, q) <= 0 and _cross(v, p, m) <= 0
    return _cross(v, m, p) > 0 or _cross(v, q, m) > 0

def bridge(outer, holes):
    poly = list(outer)
    for hole in sorted(holes, key=lambda h: -max(x for x, y in h)):
        mi = max(range(len(hole)), key=lambda i: hole[i][0]); m = hole[mi]
        best = None
        # lazily walk the outer vertices nearest-first (a heap instead of sorting all of them: the first few almost always work)
        heap = [((v[0] - m[0]) ** 2 + (v[1] - m[1]) ** 2, oi) for oi, v in enumerate(poly)]
        heapq.heapify(heap)
        while heap:
            _, oi = heapq.heappop(heap); v = poly[oi]
            # a vertex that was bridged earlier appears twice: only the copy whose interior wedge contains m may be used
            if not _wedge(poly[oi - 1], v, poly[(oi + 1) % len(poly)], m): continue
            if not _wedge(hole[mi - 1], m, hole[(mi + 1) % len(hole)], v): continue
            if _clear(m, v, poly) and _clear(m, v, hole): best = oi; break
        if best is None: best = 0
        h = hole[mi:] + hole[:mi]
        poly = poly[:best + 1] + h + [h[0]] + poly[best:]
    return poly

def ear_clip(poly):
    """poly oriented with area2 > 0 (y-down 'interior right'), may contain bridge duplicates.
    Linked-list ear clipping that only tests the *reflex* vertices against each candidate ear (a convex vertex can never be
    inside an ear), with a bbox reject first.  The old version tested every vertex of the polygon for every ear, with a restart
    from vertex 0 after each cut: O(n^3).  This is O(n * reflex) in practice."""
    n = len(poly)
    if n < 3: return []
    P = list(poly)
    prv = [(i - 1) % n for i in range(n)]; nxt = [(i + 1) % n for i in range(n)]
    reflex = set(i for i in range(n) if _cross(P[prv[i]], P[i], P[nxt[i]]) <= 0)
    tris = []; count = n; cur = 0; fails = 0
    while count > 3:
        a, b, c = prv[cur], cur, nxt[cur]
        A, B, C = P[a], P[b], P[c]
        ear = False
        if _cross(A, B, C) > 0:
            ear = True
            x0 = min(A[0], B[0], C[0]); x1 = max(A[0], B[0], C[0]); y0 = min(A[1], B[1], C[1]); y1 = max(A[1], B[1], C[1])
            for r in reflex:
                if r == a or r == b or r == c: continue
                p = P[r]
                if p[0] < x0 or p[0] > x1 or p[1] < y0 or p[1] > y1: continue
                if p == A or p == B or p == C: continue
                if _cross(A, B, p) >= 0 and _cross(B, C, p) >= 0 and _cross(C, A, p) >= 0: ear = False; break
        if ear:
            tris.append([A, B, C])
            nxt[a] = c; prv[c] = a; count -= 1; reflex.discard(b)
            for v in (a, c):
                if _cross(P[prv[v]], P[v], P[nxt[v]]) > 0: reflex.discard(v)
                else: reflex.add(v)
            cur = c if count & 1 else a      # alternate sides so we do not sweep one way only
            fails = 0
            continue
        cur = nxt[cur]; fails += 1
        if fails > count:       # no ear found in a full lap: drop a degenerate vertex, else give up
            dropped = False; v = cur
            for _ in range(count):
                if _cross(P[prv[v]], P[v], P[nxt[v]]) == 0:
                    pv, nv = prv[v], nxt[v]; nxt[pv] = nv; prv[nv] = pv; reflex.discard(v); count -= 1; cur = nv; dropped = True; break
                v = nxt[v]
            fails = 0
            if not dropped: break
    rest = []; v = cur
    for _ in range(count): rest.append(P[v]); v = nxt[v]
    if len(rest) == 3 and _cross(*rest) != 0: tris.append(rest)
    elif len(rest) > 3:   # fallback: fan
        tris += [[rest[0], rest[k], rest[k + 1]] for k in range(1, len(rest) - 1)]
    return tris

def triangulate(loops):
    """loops oriented: outer area2>0, holes area2<0 -> list of triangles (each 3 pts)"""
    outers = [l for l in loops if area2(l) > 0]; holes = [l for l in loops if area2(l) < 0]
    info = [(o, min(x for x, y in o), max(x for x, y in o), min(y for x, y in o), max(y for x, y in o), abs(area2(o))) for o in outers]
    groups = {id(o): [] for o in outers}
    for h in holes:
        px, py = h[0]
        cands = [i for i in info if i[1] <= px <= i[2] and i[3] <= py <= i[4] and pt_in_poly(h[0], i[0])]
        if cands: groups[id(min(cands, key=lambda i: i[5])[0])].append(h)
    tris = []
    for o in outers:
        tris += ear_clip(bridge(o, groups[id(o)]) if groups[id(o)] else o)
    return merge_convex(tris)


def _is_convex(poly):
    n = len(poly)
    for i in range(n):
        if _cross(poly[i], poly[(i + 1) % n], poly[(i + 2) % n]) < 0: return False
    return True

def merge_convex(tris, maxv=24):
    """greedy Hertel-Mehlhorn: merge triangles that share an edge while the result stays convex"""
    polys = {i: list(t) for i, t in enumerate(tris) if _cross(*t) != 0}
    owner = {}
    def reg(i):
        p = polys[i]
        for k in range(len(p)): owner[(p[k], p[(k + 1) % len(p)])] = i
    def unreg(i):
        p = polys[i]
        for k in range(len(p)):
            if owner.get((p[k], p[(k + 1) % len(p)])) == i: del owner[(p[k], p[(k + 1) % len(p)])]
    for i in polys: reg(i)
    for i in list(polys.keys()):
        while i in polys:
            A = polys[i]; merged = False
            for k in range(len(A)):
                u, v = A[k], A[(k + 1) % len(A)]
                j = owner.get((v, u))
                if j is None or j == i or j not in polys: continue
                B = polys[j]
                if len(A) + len(B) - 2 > maxv: continue
                ia = A.index(v) if False else (k + 1) % len(A)           # A: ... u v ...
                a_part = [A[(ia + m) % len(A)] for m in range(len(A))]    # starts at v, ends at u
                ib = B.index(u) if B.count(u) == 1 else None
                if ib is None: continue
                b_part = [B[(ib + m) % len(B)] for m in range(len(B))]    # starts at u, ends at v
                cand = a_part + b_part[1:-1]
                # drop collinear vertices
                m = len(cand); cand = [cand[q] for q in range(m) if _cross(cand[q - 1], cand[q], cand[(q + 1) % m]) != 0] or cand
                if len(cand) >= 3 and _is_convex(cand):
                    unreg(i); unreg(j); del polys[j]; polys[i] = cand; reg(i); merged = True; break
            if not merged: break
    return list(polys.values())


# ---------------------------------------------------------------------------------------------- lower-poly
def _dp(pts, tol):
    """iterative Douglas-Peucker on an open polyline; endpoints are always kept"""
    n = len(pts)
    if n < 3: return list(pts)
    keep = [False] * n; keep[0] = keep[-1] = True; stack = [(0, n - 1)]; t2 = tol * tol
    while stack:
        i, j = stack.pop()
        if j <= i + 1: continue
        ax, ay = pts[i]; bx, by = pts[j]; dx, dy = bx - ax, by - ay; L2 = dx * dx + dy * dy
        best = -1.0; bk = -1
        for k in range(i + 1, j):
            px, py = pts[k]
            if L2 == 0: d2 = (px - ax) ** 2 + (py - ay) ** 2
            else:
                u = ((px - ax) * dx + (py - ay) * dy) / L2; u = 0.0 if u < 0 else (1.0 if u > 1 else u)
                d2 = (px - ax - u * dx) ** 2 + (py - ay - u * dy) ** 2
            if d2 > best: best = d2; bk = k
        if best > t2: keep[bk] = True; stack.append((i, bk)); stack.append((bk, j))
    return [p for p, k in zip(pts, keep) if k]

def simplify_edges(edges, tol=None):
    """edges: [(kind, pts, f0, f1, ln)] as parsed from a SWF shape.  Consecutive edges with the same fill/line styles that join
    end-to-start are flattened to one polyline, Douglas-Peucker simplified, and re-emitted as straight edges.  Because the
    simplification is done on the *edge chain* (not per fill region), the two fills on either side of a border get the same
    simplified border, so no cracks open up.  Chain end points (style changes / junctions) are never moved."""
    tol = SIMPLIFY if tol is None else tol
    if tol <= 0: return edges
    out = []; chain = []; key = None
    def flush():
        nonlocal chain
        if len(chain) > 1:
            if chain[0] == chain[-1] and len(chain) > 4:       # closed ring: split at the farthest vertex so DP has two real halves
                far = max(range(1, len(chain) - 1), key=lambda k: (chain[k][0] - chain[0][0]) ** 2 + (chain[k][1] - chain[0][1]) ** 2)
                sp = _dp(chain[:far + 1], tol)[:-1] + _dp(chain[far:], tol)
            else: sp = _dp(chain, tol)
            for a, b in zip(sp[:-1], sp[1:]): out.append(('l', [a, b], key[0], key[1], key[2]))
        chain = []
    for e in edges:
        kind, pts, f0, f1, ln = e; k = (f0, f1, ln)
        ps = [pts[0]] + (pts[1:] if kind == 'l' else flatten_q(*pts))
        if chain and k == key and chain[-1] == ps[0]: chain += ps[1:]
        else: flush(); chain = list(ps); key = k
    flush()
    return out

def drop_specks(loops, min_area=None):
    min_area = MIN_AREA if min_area is None else min_area
    return loops if min_area <= 0 else [l for l in loops if abs(area2(l)) / 2.0 >= min_area]
