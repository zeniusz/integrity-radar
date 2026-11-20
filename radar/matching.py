"""Fixture matching across venues. Every match is unique or rejected, and carries a confidence."""
import re
from datetime import datetime, timedelta

KALSHI_ALIAS = {  # ESPN abbreviation -> Kalshi abbreviation where they differ
    "GS": "GSW", "NY": "NYK", "SA": "SAS", "NO": "NOP", "UTAH": "UTA", "WSH": "WAS",
    "LA": "LAK", "NJ": "NJD", "TB": "TBL", "SJ": "SJS",
}


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def match_polymarket(game, pm_events):
    """Return {"event", "homeIndex", "confidence"} or {"event": None, "reason"}."""
    def aliases(t):
        return {_norm(t.get(k)) for k in ("name", "location", "displayName", "abbreviation") if t.get(k)}
    ha, aa = aliases(game["home"]), aliases(game["away"])
    gs = _ts(game["start"])
    cands = []
    for e in pm_events:
        names = [_norm(o) for o in e["outcomes"]]
        if len(names) != 2:
            continue
        if names[0] in ha and names[1] in aa:
            hi = 0
        elif names[1] in ha and names[0] in aa:
            hi = 1
        else:
            continue
        # slug encodes the venue's own game date; it must agree with the fixture (US date)
        slug_date = e["slug"][-10:]
        us_date = (gs - timedelta(hours=5)).strftime("%Y-%m-%d")
        if slug_date not in (us_date, (gs - timedelta(hours=5) - timedelta(days=1)).strftime("%Y-%m-%d"),
                             (gs - timedelta(hours=5) + timedelta(days=1)).strftime("%Y-%m-%d")):
            continue
        gap_h = None
        if e.get("start"):
            gap_h = abs((_ts(e["start"]) - gs).total_seconds()) / 3600
            if gap_h > 6:
                continue
        conf = "high" if gap_h is not None and gap_h <= 1 and slug_date == us_date else "medium"
        cands.append({"event": e, "homeIndex": hi, "confidence": conf})
    if not cands:
        return {"event": None, "reason": "not listed"}
    high = [c for c in cands if c["confidence"] == "high"]
    if len(high) == 1:
        return high[0]
    if len(cands) == 1:
        return cands[0]
    return {"event": None, "reason": f"ambiguous ({len(cands)} candidate markets)"}


def match_kalshi(game, k_events):
    """Return {"team", "event", "confidence"} or {"team": None, "reason"}."""
    gs = _ts(game["start"]) - timedelta(hours=5)  # US date
    exact = gs.strftime("%y%b%d").upper()
    near = {(gs + timedelta(days=d)).strftime("%y%b%d").upper() for d in (-1, 1)}

    def keys(abbr):
        return {abbr, KALSHI_ALIAS.get(abbr, abbr)}
    hk, ak = keys(game["home"]["abbreviation"]), keys(game["away"]["abbreviation"])
    cands = []
    for ev, info in k_events.items():
        if info["date"] != exact and info["date"] not in near:
            continue
        teams = set(info["teams"])
        if len(teams) != 2:  # a game-winner event must be exactly these two teams
            continue
        h = next((x for x in hk if x in teams), None)
        a = next((x for x in ak if x in teams), None)
        if h and a and h != a:
            cands.append({"team": info["teams"][h], "event": ev,
                          "confidence": "high" if info["date"] == exact else "low"})
    if not cands:
        return {"team": None, "reason": "not listed"}
    high = [c for c in cands if c["confidence"] == "high"]
    if len(high) == 1:
        return high[0]
    if len(cands) == 1:
        return cands[0]
    return {"team": None, "reason": f"ambiguous ({len(cands)} candidate events)"}
