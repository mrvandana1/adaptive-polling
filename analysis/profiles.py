"""Ground-truth load profiles. MUST mirror loadtest/scenarios.js `points()`."""

DEFAULTS = dict(base=10.0, peak=150.0, warm=60.0, ramp=60.0, hold=120.0, cool=45.0, accel_step=5.0)


def points(scenario, **kw):
    """Return (breakpoints [(t, rps)], marks {name: t})."""
    p = {**DEFAULTS, **kw}
    b, pk, w, rp, h, c = p["base"], p["peak"], p["warm"], p["ramp"], p["hold"], p["cool"]
    if scenario == "steady":
        return [(0, b), (w + h + c, b)], {}
    pts = [(0, b), (w, b)]
    if scenario == "step":
        pts += [(w + 1, pk), (w + h, pk)]
        burst_end = w + h
    elif scenario == "ramp":
        pts += [(w + rp, pk), (w + rp + h, pk)]
        burst_end = w + rp + h
    elif scenario == "accel":
        n = int(rp / p["accel_step"])
        for i in range(1, n + 1):
            pts.append((w + p["accel_step"] * i, b + (pk - b) * (i / n) ** 2))
        pts.append((w + rp + h, pk))
        burst_end = w + rp + h
    else:
        raise ValueError(scenario)
    pts += [(burst_end + 1, b), (burst_end + c, b)]
    return pts, {"burst_start": w, "burst_end": burst_end}


def rate_at(pts, t):
    if t <= pts[0][0]:
        return pts[0][1]
    for (t0, r0), (t1, r1) in zip(pts, pts[1:]):
        if t0 <= t <= t1:
            return r0 if t1 == t0 else r0 + (r1 - r0) * (t - t0) / (t1 - t0)
    return pts[-1][1]


def duration(pts):
    return pts[-1][0]


def crossing_time(pts, threshold):
    """First t at which the true rate >= threshold (relative to t=0)."""
    t = 0.0
    while t <= pts[-1][0]:
        if rate_at(pts, t) >= threshold:
            return t
        t += 0.1
    return None
