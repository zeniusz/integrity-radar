"""Builds the live, scored fixture list for one league."""
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from . import sources, storage
from .matching import match_kalshi, match_polymarket
from .scoring import max_jump, move_over_window, score_game
from .sources import LEAGUES


def _kickoff(g):
    return datetime.fromisoformat(g["start"].replace("Z", "+00:00")).timestamp()


def attach_history(g, hist, now):
    """Add Polymarket history-derived fields. Integrity signals use pre-match trading only."""
    flip = g["pm"]["flip"]
    pts = [[h["t"], (1 - h["p"]) if flip else h["p"]] for h in hist]
    g["pm"]["history"] = pts
    kickoff = g["kickoff"]
    pre = [p for p in pts if p[0] < kickoff]
    if g["state"] != "pre" and pre:
        g["pm"]["preMatchClose"] = pre[-1][1]
    if pts:
        g["pm"]["lastTradeAt"] = pts[-1][0]
        if g["state"] == "pre":
            g["pm"]["quoteAge"] = max(0, now - pts[-1][0])
    g["pm"]["move"] = move_over_window(pre)
    g["pm"]["move24"] = g["pm"]["move"]["pp"] / 100 if g["pm"]["move"] else None
    g["pm"]["maxJump"], g["pm"]["maxJumpAt"] = max_jump(pre)


def build_live(league: str, now=None, db=None, persist=True):
    now = now or time.time()
    games, s_espn = sources.espn_games(league)
    pm_events, s_pm = sources.polymarket_events(league)
    k_events, s_k = sources.kalshi_markets(league)
    work = []
    for g in games:
        g["caseId"] = storage.case_id(g)
        g["kickoff"] = _kickoff(g)
        g["missing"] = {}
        if not s_pm["ok"]:
            g["missing"]["polymarket"] = "source unavailable"
        else:
            m = match_polymarket(g, pm_events)
            e = m["event"]
            if e:
                idx = m["homeIndex"]
                bid, ask = e.get("bid"), e.get("ask")
                spread = (ask - bid) if bid is not None and ask is not None and ask >= bid else None
                g["pm"] = {"homeNow": e["prices"][idx], "volume": e["volume"], "liquidity": e["liquidity"],
                           "url": e["url"], "token": e["tokens"][0], "flip": idx != 0, "slug": e["slug"],
                           "spread": spread, "observedAt": now, "matchConfidence": m["confidence"]}
                work.append(g)
            else:
                g["missing"]["polymarket"] = m["reason"]
        if not s_k["ok"]:
            g["missing"]["kalshi"] = "source unavailable"
        else:
            k = match_kalshi(g, k_events)
            t = k["team"]
            if t and t["mid"] is not None:
                g["kalshi"] = {"homeNow": t["mid"], "bid": t["bid"], "ask": t["ask"], "volume": t["volume"],
                               "observedAt": now, "matchConfidence": k["confidence"],
                               "url": f"https://kalshi.com/markets/{LEAGUES[league]['kalshi'].lower()}/{k['event'].lower()}"}
            elif t:
                g["missing"]["kalshi"] = f"listed, but {t['unusable']}"
            else:
                g["missing"]["kalshi"] = k["reason"]
        if not g.get("book"):
            g["missing"]["book"] = "not listed"

    hist_fail = [0]

    def add_history(g):
        hist, ok = sources.polymarket_history(g["pm"]["token"])
        if not ok:
            hist_fail[0] += 1
        attach_history(g, hist, now)

    with ThreadPoolExecutor(8) as ex:
        list(ex.map(add_history, work))

    baseline = {"pm_move": storage.move_baseline(league, path=db)} if persist else None
    for g in games:
        if persist and g.get("book") and (g.get("pm") or {}).get("move"):
            g["bookWindowMove"] = storage.book_window_move(g["caseId"], g["pm"]["move"]["windowH"] * 3600, now, path=db)
        score_game(g, now=now, baseline=baseline)
    if persist:
        storage.record_snapshot(games, t=now, path=db)
        for g in games:
            g["signalHistory"] = storage.signal_history(g["caseId"], path=db)
            g["series"] = storage.series(g["caseId"], now=now, path=db)
    games.sort(key=lambda g: (-g["score"], g["start"]))
    return {
        "league": league, "generated": datetime.fromtimestamp(now, timezone.utc).isoformat(), "games": games,
        "baselineN": len((baseline or {}).get("pm_move") or []),
        "sources": {"espn": s_espn, "polymarket": dict(s_pm, historyFailures=hist_fail[0]), "kalshi": s_k},
        "coverage": {"fixtures": len(games), "polymarket": sum(1 for g in games if g.get("pm")),
                     "book": sum(1 for g in games if g.get("book")), "kalshi": sum(1 for g in games if g.get("kalshi"))},
    }
