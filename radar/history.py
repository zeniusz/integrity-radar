"""Historical market analysis: opening vs closing bookmaker prices in European football.

This is market context. It does not validate the live cross-market detector
(see storage.evaluation for that).
"""
import csv
import io

from .net import fetch
from .odds import decimal_devig

FD_LEAGUES = {"E0": "Premier League", "D1": "Bundesliga", "SP1": "La Liga", "I1": "Serie A", "F1": "Ligue 1"}


def build_history(div: str, season: str):
    text = fetch(f"https://www.football-data.co.uk/mmz4281/{season}/{div}.csv", ttl=3600, as_json=False)
    return analyse_rows(list(csv.DictReader(io.StringIO(text))), div, season)


def analyse_rows(rows, div, season):
    matches = []
    for r in rows:
        if not r.get("HomeTeam") or not r.get("FTR"):
            continue
        o = decimal_devig([r.get("AvgH"), r.get("AvgD"), r.get("AvgA")])
        c = decimal_devig([r.get("AvgCH"), r.get("AvgCD"), r.get("AvgCA")])
        if not o or not c:
            continue
        po, pc = o[0], c[0]
        drift = [(pc[i] - po[i]) * 100 for i in range(3)]
        k = max(range(3), key=lambda i: abs(drift[i]))
        try:
            mx = [float(r["MaxCH"]), float(r["MaxCD"]), float(r["MaxCA"])]
            av = [float(r["AvgCH"]), float(r["AvgCD"]), float(r["AvgCA"])]
            disp = max((mx[i] / av[i] - 1) * 100 for i in range(3))
        except (KeyError, ValueError, ZeroDivisionError):
            disp = None
        res = {"H": 0, "D": 1, "A": 2}.get(r["FTR"])
        matches.append({
            "date": r["Date"], "home": r["HomeTeam"], "away": r["AwayTeam"],
            "score": f'{r.get("FTHG")}-{r.get("FTAG")}', "result": res,
            "open": [round(x, 4) for x in po], "close": [round(x, 4) for x in pc],
            "drift": [round(x, 2) for x in drift], "driftOutcome": k, "driftPP": round(drift[k], 2),
            "dispersion": round(disp, 2) if disp is not None else None,
        })
    absd = sorted(abs(m["driftPP"]) for m in matches)
    n = len(absd)
    if not n:
        return {"div": div, "name": FD_LEAGUES.get(div, div), "season": season, "n": 0, "meanAbsDrift": 0,
                "sdDrift": 0, "buckets": [], "brierOpen": None, "brierClose": None, "matches": []}
    mean = sum(absd) / n
    sd = (sum((x - mean) ** 2 for x in absd) / n) ** 0.5
    for m in matches:
        m["z"] = round((abs(m["driftPP"]) - mean) / (sd or 1), 2)
    buckets = []
    for lo, hi in ((0, 3), (3, 6), (6, 10), (10, 100)):
        sub = [m for m in matches if lo <= abs(m["driftPP"]) < hi and m["driftPP"] > 0]
        if sub:
            hit = sum(1 for m in sub if m["result"] == m["driftOutcome"]) / len(sub)
            exp_open = sum(m["open"][m["driftOutcome"]] for m in sub) / len(sub)
            exp_close = sum(m["close"][m["driftOutcome"]] for m in sub) / len(sub)
            buckets.append({"range": f"+{lo}–{hi}pp" if hi < 100 else f"+{lo}pp or more", "n": len(sub), "hit": hit,
                            "openProb": exp_open, "closeProb": exp_close})
    brier_o = sum(sum(((1 if m["result"] == i else 0) - m["open"][i]) ** 2 for i in range(3)) for m in matches) / n
    brier_c = sum(sum(((1 if m["result"] == i else 0) - m["close"][i]) ** 2 for i in range(3)) for m in matches) / n
    return {"div": div, "name": FD_LEAGUES.get(div, div), "season": season, "n": n,
            "meanAbsDrift": mean, "sdDrift": sd, "buckets": buckets,
            "brierOpen": brier_o, "brierClose": brier_c, "matches": matches}
