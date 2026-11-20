"""Signals, review priority and data quality.

The priority score orders the review queue. It is NOT a probability of match-fixing.
"""
import time

from .odds import effective_pp

REVIEW_AT, ESCALATE_AT = 20, 45
PM_STALE_AFTER = 3 * 3600        # no Polymarket trade for 3h -> its price may not be current
PM_MAX_SPREAD = 0.06             # wider book than 6c -> mid is not comparable
MIN_MOVE_COVERAGE_H = 20         # a "24h move" needs at least 20h of history
BASELINE_MIN_N = 30              # fixtures needed before league history replaces fixed thresholds

# Strongly correlated signals share a group: the strongest counts in full, the rest at half.
GROUPS = {"divergence": "disagree", "kalshi_gap": "disagree",
          "pm_move": "movement", "pm_jump": "movement", "book_move": "movement",
          "unconfirmed": "unconfirmed"}


def liquidity_weight(volume):
    return 0.35 if volume < 25_000 else 0.6 if volume < 100_000 else 0.85 if volume < 500_000 else 1.0


def time_weight(hours_to_start):
    """Late moves are routinely explained by lineup news; moves far from kickoff are less common."""
    if hours_to_start is None:
        return 1.0
    return 0.7 if hours_to_start < 2 else 0.85 if hours_to_start < 6 else 1.0


def _percentile(sorted_vals, x):
    if not sorted_vals:
        return None
    below = sum(1 for v in sorted_vals if v < x)
    return below / len(sorted_vals)


def combine(reasons):
    """Sum points with correlated-signal discount. Mutates each reason's 'pts'."""
    best = {}
    for r in reasons:
        if "raw" not in r:
            continue
        grp = GROUPS.get(r["code"], r["code"])
        if grp not in best or r["raw"] > best[grp]["raw"]:
            best[grp] = r
    total = 0.0
    for r in reasons:
        if "raw" not in r:
            continue
        full = best[GROUPS.get(r["code"], r["code"])] is r
        r["pts"] = round(r["raw"] * (1.0 if full else 0.5), 1)
        if not full:
            r["correlated"] = True
        total += r["pts"]
        del r["raw"]
    return total


def score_game(g, now=None, baseline=None):
    """baseline: optional {"pm_move": sorted abs effective-pp moves for this league}."""
    now = now or time.time()
    reasons, quality = [], []
    pm, book, kal = g.get("pm"), g.get("book"), g.get("kalshi")
    pre = g["state"] == "pre"
    kickoff = g.get("kickoff")
    hrs = (kickoff - now) / 3600 if kickoff and pre else None
    tw = time_weight(hrs)

    liq_w = 1.0
    pm_ok = False
    if pm:
        liq_w = liquidity_weight(pm["volume"])
        g["liquidityWeight"] = liq_w
        age = pm.get("quoteAge")
        pm["stale"] = age is not None and age > PM_STALE_AFTER
        spread = pm.get("spread")
        pm["wide"] = spread is not None and spread > PM_MAX_SPREAD
        pm_ok = not pm["stale"] and not pm["wide"]
        if pm["stale"]:
            quality.append(("low", f"No Polymarket trade for {age / 3600:.1f}h, price may be stale"))
        if pm["wide"]:
            quality.append(("medium", f"Polymarket spread {spread * 100:.0f}¢, mid price is soft"))
        if liq_w < 0.6:
            quality.append(("medium", "Thin Polymarket market"))
        if pm.get("matchConfidence") == "medium":
            quality.append(("medium", "Polymarket match is not exact on start time"))
    if book:
        quality.append(("medium", f"{book['provider']} quote time not published by source"))
        if book.get("homeOpen") is None:
            quality.append(("medium", f"No {book['provider']} opening line, line movement not measured"))
    if kal and kal.get("matchConfidence") == "low":
        quality.append(("low", "Kalshi match relies on a neighbouring date"))

    # --- disagreement (only between comparable, current quotes)
    if pm and book and pre:
        div = (pm["homeNow"] - book["homeNow"]) * 100
        g["divergence"] = div
        if abs(div) >= 3:
            if pm_ok:
                raw = min(abs(div), 15) * 3.0 * liq_w * 0.85  # 0.85: book quote time unknown
                reasons.append({"code": "divergence", "pp": round(div, 1), "raw": raw})
            else:
                reasons.append({"code": "suppressed", "of": "divergence", "pp": round(div, 1),
                                "why": "stale" if pm["stale"] else "wide spread"})
    if kal and kal.get("homeNow") is not None and pm and pre:
        kd = (kal["homeNow"] - pm["homeNow"]) * 100
        g["kalshiGap"] = kd
        if abs(kd) >= 6:
            if pm_ok:
                reasons.append({"code": "kalshi_gap", "pp": round(kd, 1), "raw": min(abs(kd), 15) * 1.0})
            else:
                reasons.append({"code": "suppressed", "of": "kalshi_gap", "pp": round(kd, 1),
                                "why": "stale" if pm["stale"] else "wide spread"})

    # --- movement (probability-range adjusted)
    if book and book.get("homeOpen") is not None:
        bm = (book["homeNow"] - book["homeOpen"]) * 100
        g["bookMove"] = bm
        eff = effective_pp(book["homeOpen"], book["homeNow"])
        if abs(eff) >= 4:
            reasons.append({"code": "book_move", "pp": round(bm, 1), "eff": round(eff, 1),
                            "raw": min(abs(eff), 15) * 1.5 * tw})
    mv = (pm or {}).get("move")
    if mv and mv.get("covered"):
        m = mv["pp"]
        eff = mv["eff"]
        base = (baseline or {}).get("pm_move")
        pctl = _percentile(base, abs(eff)) if base and len(base) >= BASELINE_MIN_N else None
        fires = pctl >= 0.9 if pctl is not None else abs(eff) >= 5
        if fires:
            r = {"code": "pm_move", "pp": round(m, 1), "eff": round(eff, 1), "windowH": mv["windowH"],
                 "raw": min(abs(eff), 20) * 1.6 * liq_w * tw}
            if pctl is not None:
                r["percentile"] = round(pctl * 100)
            reasons.append(r)
        # bookmaker did not follow: needs bookmaker movement over the SAME window (from our snapshots)
        bw = g.get("bookWindowMove")
        if book is not None and abs(eff) >= 5:
            if bw and bw["windowH"] >= mv["windowH"] * 0.8:
                if abs(bw["pp"]) < 2:
                    reasons.append({"code": "unconfirmed", "pp": round(m, 1), "bookPP": round(bw["pp"], 1),
                                    "windowH": round(bw["windowH"], 1), "raw": 10 * liq_w})
            else:
                quality.append(("medium", "Bookmaker history too short to check whether it followed"))
    elif mv:
        quality.append(("medium", f"Only {mv['windowH']:.0f}h of Polymarket history, 24h move not measured"))
    if pm and pm.get("maxJump") and pm["maxJump"] * 100 >= 6:
        reasons.append({"code": "pm_jump", "pp": round(pm["maxJump"] * 100, 1), "at": pm.get("maxJumpAt"),
                        "raw": min(pm["maxJump"] * 100, 15) * 1.2 * liq_w * tw})

    if pm and liq_w < 0.6:
        reasons.append({"code": "thin", "vol": round(pm["volume"])})

    total = combine(reasons)
    g["score"] = int(min(total, 100) + 0.5)  # half-up, matches the UI
    g["level"] = "alert" if g["score"] >= ESCALATE_AT else "watch" if g["score"] >= REVIEW_AT else "normal"
    g["reasons"] = reasons
    g["timeWeight"] = tw
    g["dataQuality"] = grade(quality, has_market=bool(pm or book or kal))
    return g


def grade(issues, has_market=True):
    if not has_market:
        return {"grade": "none", "issues": ["No market prices this fixture"]}
    levels = [lvl for lvl, _ in issues]
    g = "low" if "low" in levels else "medium" if levels.count("medium") >= 2 else "high"
    return {"grade": g, "issues": [t for _, t in issues]}


def move_over_window(pts, window_s=24 * 3600, tolerance_s=2 * 3600):
    """Move from the point ~window_s before the last point. Reports the real window used."""
    if len(pts) < 2:
        return None
    now_t, now_p = pts[-1]
    target = now_t - window_s
    past = [p for p in pts if p[0] <= target + tolerance_s]
    ref = past[-1] if past else pts[0]
    window_h = (now_t - ref[0]) / 3600
    return {"pp": (now_p - ref[1]) * 100, "eff": effective_pp(ref[1], now_p),
            "windowH": round(window_h, 1), "covered": window_h >= MIN_MOVE_COVERAGE_H,
            "from": ref[0], "to": now_t}


def max_jump(pts, lookback_s=48 * 3600, steps=4):
    if not pts:
        return 0.0, None
    now_t = pts[-1][0]
    recent = [p for p in pts if p[0] >= now_t - lookback_s]
    best, at = 0.0, None
    for i in range(1, len(recent)):
        j0 = max(0, i - steps)  # ~2h at 30-min fidelity
        d = abs(recent[i][1] - recent[j0][1])
        if d > best:
            best, at = d, recent[i][0]
    return best, at
