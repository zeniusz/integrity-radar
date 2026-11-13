"""Integrity Radar - local data server.

Pure Python standard library, no API keys, no registrations.
Sources:
  - ESPN public scoreboard (fixtures + DraftKings opening/current moneyline)
  - Polymarket Gamma API (prediction-market prices) + CLOB price history
  - Kalshi public market data (second prediction market)
  - football-data.co.uk CSVs (historical opening/closing bookmaker odds, for backtests)

Run:  python server.py   then open http://localhost:8765
"""
import csv
import gzip
import io
import json
import re
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = 8765
ROOT = Path(__file__).parent
UA = "IntegrityRadarDemo/1.0"

LEAGUES = {
    "nba": {"espn": "basketball/nba", "kalshi": "KXNBAGAME", "pm_series": "10345", "name": "NBA"},
    "nhl": {"espn": "hockey/nhl", "kalshi": "KXNHLGAME", "pm_series": "10346", "name": "NHL"},
    "nfl": {"espn": "football/nfl", "kalshi": "KXNFLGAME", "pm_series": "12185", "name": "NFL"},
}
FD_LEAGUES = {"E0": "Premier League", "D1": "Bundesliga", "SP1": "La Liga", "I1": "Serie A", "F1": "Ligue 1"}

# ---------------------------------------------------------------- http + cache
_cache: dict = {}
_lock = threading.Lock()


def fetch(url: str, ttl: int = 60, as_json: bool = True):
    now = time.time()
    with _lock:
        hit = _cache.get(url)
        if hit and now - hit[0] < ttl:
            return hit[1]
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    data = json.loads(raw) if as_json else raw.decode("utf-8-sig", errors="replace")
    with _lock:
        _cache[url] = (now, data)
    return data


# ---------------------------------------------------------------- odds maths
def american_to_prob(s):
    if s is None:
        return None
    s = str(s).strip().upper()
    if s in ("EVEN", "EV", "PK"):
        return 0.5
    try:
        o = float(s)
    except ValueError:
        return None
    if o == 0:
        return None
    return -o / (-o + 100) if o < 0 else 100 / (o + 100)


def devig2(a, b):
    """Remove bookmaker margin from a two-way market."""
    if a is None or b is None or a + b == 0:
        return None, None
    return a / (a + b), b / (a + b)


def decimal_devig(odds):
    inv = []
    for o in odds:
        try:
            o = float(o)
        except (TypeError, ValueError):
            return None
        if o <= 1:
            return None
        inv.append(1 / o)
    t = sum(inv)
    return [x / t for x in inv], t - 1  # probabilities, overround


# ---------------------------------------------------------------- sources
def espn_games(league: str):
    path = LEAGUES[league]["espn"]
    base = f"https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard"
    urls = [base]
    if league != "nfl":  # NFL default scoreboard already returns the whole week
        tomorrow = (datetime.now(timezone.utc) + timedelta(hours=-5) + timedelta(days=1)).strftime("%Y%m%d")
        urls.append(f"{base}?dates={tomorrow}")
    games, seen = [], set()
    for u in urls:
        try:
            data = fetch(u, ttl=90)
        except Exception:
            continue
        for ev in data.get("events", []):
            if ev["id"] in seen:
                continue
            seen.add(ev["id"])
            comp = ev["competitions"][0]
            teams = {c["homeAway"]: c["team"] for c in comp["competitors"]}
            if "home" not in teams or "away" not in teams:
                continue
            g = {
                "id": ev["id"],
                "league": league,
                "name": ev.get("name"),
                "start": ev.get("date"),
                "state": ev["status"]["type"]["state"],  # pre / in / post
                "statusText": ev["status"]["type"].get("shortDetail"),
                "home": {k: teams["home"].get(k) for k in ("abbreviation", "name", "location", "displayName", "logo", "color", "alternateColor")},
                "away": {k: teams["away"].get(k) for k in ("abbreviation", "name", "location", "displayName", "logo", "color", "alternateColor")},
                "book": None,
            }
            odds = (comp.get("odds") or [None])[0]
            if odds and odds.get("moneyline"):
                ml = odds["moneyline"]
                h_c = american_to_prob((ml.get("home") or {}).get("close", {}).get("odds"))
                a_c = american_to_prob((ml.get("away") or {}).get("close", {}).get("odds"))
                h_o = american_to_prob((ml.get("home") or {}).get("open", {}).get("odds"))
                a_o = american_to_prob((ml.get("away") or {}).get("open", {}).get("odds"))
                cur, _ = devig2(h_c, a_c)
                opn, _ = devig2(h_o, a_o)
                if cur is not None:
                    g["book"] = {
                        "provider": (odds.get("provider") or {}).get("name", "Bookmaker"),
                        "homeNow": cur,
                        "homeOpen": opn if opn is not None else cur,
                        "margin": (h_c + a_c - 1),
                        "rawHome": (ml.get("home") or {}).get("close", {}).get("odds"),
                        "rawAway": (ml.get("away") or {}).get("close", {}).get("odds"),
                        "rawHomeOpen": (ml.get("home") or {}).get("open", {}).get("odds"),
                        "spread": odds.get("details"),
                    }
            games.append(g)
    return games


SLUG_RE = re.compile(r"^(nba|nhl|nfl)-[a-z0-9]+-[a-z0-9]+-\d{4}-\d{2}-\d{2}$")


def polymarket_events(league: str):
    data = []
    for offset in (0, 100, 200, 300):
        page = fetch(f"https://gamma-api.polymarket.com/events?series_id={LEAGUES[league]['pm_series']}"
                     f"&active=true&closed=false"
                     f"&limit=100&offset={offset}", ttl=60)
        data.extend(page)
        if len(page) < 100:
            break
    out = []
    for e in data:
        if not SLUG_RE.match(e.get("slug", "")):
            continue
        ml = next((m for m in e.get("markets", []) if m.get("sportsMarketType") == "moneyline"), None)
        if not ml:
            continue
        try:
            outcomes = json.loads(ml["outcomes"])
            prices = [float(x) for x in json.loads(ml["outcomePrices"])]
            tokens = json.loads(ml["clobTokenIds"])
        except Exception:
            continue
        out.append({
            "slug": e["slug"], "start": e.get("startTime"), "outcomes": outcomes, "prices": prices,
            "tokens": tokens, "volume": float(ml.get("volumeNum") or 0), "liquidity": float(ml.get("liquidityNum") or 0),
            "volume24h": float(e.get("volume24hr") or 0),
            "url": f"https://polymarket.com/event/{e['slug']}",
        })
    return out


def polymarket_history(token: str):
    url = f"https://clob.polymarket.com/prices-history?market={token}&interval=1w&fidelity=30"
    try:
        return fetch(url, ttl=120).get("history", [])
    except Exception:
        return []


KALSHI_ALIAS = {  # ESPN abbreviation -> Kalshi abbreviation where they differ
    "GS": "GSW", "NY": "NYK", "SA": "SAS", "NO": "NOP", "UTAH": "UTA", "WSH": "WAS",
    "LA": "LAK", "NJ": "NJD", "TB": "TBL", "SJ": "SJS",
}


def kalshi_markets(league: str):
    series = LEAGUES[league]["kalshi"]
    url = f"https://api.elections.kalshi.com/trade-api/v2/markets?limit=1000&status=open&series_ticker={series}"
    try:
        mk = fetch(url, ttl=60).get("markets", [])
    except Exception:
        return {}
    events: dict = {}
    for m in mk:
        ev = m.get("event_ticker", "")
        abbr = m["ticker"].rsplit("-", 1)[-1]
        bid = float(m.get("yes_bid_dollars") or 0)
        ask = float(m.get("yes_ask_dollars") or 0)
        last = float(m.get("last_price_dollars") or 0)
        mid = (bid + ask) / 2 if bid > 0 and ask > 0 and ask >= bid else (last or None)
        events.setdefault(ev, {"date": ev.split("-")[1][:7] if "-" in ev else "", "teams": {}})
        events[ev]["teams"][abbr] = {"mid": mid, "bid": bid, "ask": ask, "volume": float(m.get("volume_fp") or 0),
                                     "ticker": m["ticker"]}
    return events


# ---------------------------------------------------------------- matching
def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def match_polymarket(game, pm_events):
    def aliases(t):
        return {_norm(t.get(k)) for k in ("name", "location", "displayName", "abbreviation") if t.get(k)}
    ha, aa = aliases(game["home"]), aliases(game["away"])
    gs = datetime.fromisoformat(game["start"].replace("Z", "+00:00"))
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
        if e.get("start"):
            es = datetime.fromisoformat(e["start"].replace("Z", "+00:00"))
            if abs((es - gs).total_seconds()) > 18 * 3600:
                continue
        return e, hi
    return None, None


def match_kalshi(game, k_events):
    gs = datetime.fromisoformat(game["start"].replace("Z", "+00:00")) - timedelta(hours=5)  # US date
    cands = set()
    for d in (gs, gs - timedelta(days=1), gs + timedelta(days=1)):
        cands.add(d.strftime("%y%b%d").upper())
    def keys(abbr):
        return {abbr, KALSHI_ALIAS.get(abbr, abbr)}
    hk, ak = keys(game["home"]["abbreviation"]), keys(game["away"]["abbreviation"])
    for ev, info in k_events.items():
        if info["date"] not in cands:
            continue
        teams = set(info["teams"])
        h = next((x for x in hk if x in teams), None)
        a = next((x for x in ak if x in teams), None)
        if h and a:
            return info["teams"][h], ev
    return None, None


# ---------------------------------------------------------------- signals
def score_game(g):
    """Combine cross-market signals into a 0-100 review-priority score with reasons."""
    reasons, s = [], 0.0
    pm, book, kal = g.get("pm"), g.get("book"), g.get("kalshi")
    liq_w = 1.0
    if pm:
        vol = pm["volume"]
        liq_w = 0.35 if vol < 25_000 else 0.6 if vol < 100_000 else 0.85 if vol < 500_000 else 1.0
        g["liquidityWeight"] = liq_w
    pre = g["state"] == "pre"
    if pm and book and pre:
        div = (pm["homeNow"] - book["homeNow"]) * 100
        g["divergence"] = div
        if abs(div) >= 3:
            pts = min(abs(div), 15) * 3.0 * liq_w; s += pts
            reasons.append({"code": "divergence", "pp": round(div, 1), "pts": round(pts, 1)})
    if book:
        bm = (book["homeNow"] - book["homeOpen"]) * 100
        g["bookMove"] = bm
        if abs(bm) >= 4:
            pts = min(abs(bm), 15) * 1.5; s += pts
            reasons.append({"code": "book_move", "pp": round(bm, 1), "pts": round(pts, 1)})
    if pm and pm.get("move24") is not None:
        m24 = pm["move24"] * 100
        if abs(m24) >= 5:
            pts = min(abs(m24), 20) * 1.6 * liq_w; s += pts
            reasons.append({"code": "pm_move", "pp": round(m24, 1), "pts": round(pts, 1)})
        if pm.get("maxJump") and pm["maxJump"] * 100 >= 6:
            pts = min(pm["maxJump"] * 100, 15) * 1.2 * liq_w; s += pts
            reasons.append({"code": "pm_jump", "pp": round(pm["maxJump"] * 100, 1), "at": pm.get("maxJumpAt"), "pts": round(pts, 1)})
        if book is not None and abs(m24) >= 5 and abs(g.get("bookMove", 0)) < 2:
            pts = 10 * liq_w; s += pts
            reasons.append({"code": "unconfirmed", "pp": round(m24, 1), "pts": round(pts, 1)})
    if kal and kal.get("homeNow") is not None and pm and pre:
        kd = (kal["homeNow"] - pm["homeNow"]) * 100
        g["kalshiGap"] = kd
        if abs(kd) >= 6:
            pts = min(abs(kd), 15) * 1.0; s += pts
            reasons.append({"code": "kalshi_gap", "pp": round(kd, 1), "pts": round(pts, 1)})
    if pm and liq_w < 0.6:
        reasons.append({"code": "thin", "vol": round(pm["volume"])})
    g["score"] = round(min(s, 100))
    g["level"] = "alert" if g["score"] >= 45 else "watch" if g["score"] >= 20 else "normal"
    g["reasons"] = reasons
    return g


def build_live(league: str):
    games = espn_games(league)
    pm_events = polymarket_events(league)
    k_events = kalshi_markets(league)
    work = []
    for g in games:
        e, idx = match_polymarket(g, pm_events)
        if e:
            g["pm"] = {"homeNow": e["prices"][idx], "volume": e["volume"], "liquidity": e["liquidity"],
                       "url": e["url"], "token": e["tokens"][0], "flip": idx != 0, "slug": e["slug"]}
            work.append(g)
        k, kev = match_kalshi(g, k_events)
        if k and k["mid"] is not None:
            g["kalshi"] = {"homeNow": k["mid"], "bid": k["bid"], "ask": k["ask"], "volume": k["volume"],
                           "url": f"https://kalshi.com/markets/{LEAGUES[league]['kalshi'].lower()}/{kev.lower()}"}

    def add_history(g):
        hist = polymarket_history(g["pm"]["token"])
        flip = g["pm"]["flip"]
        pts = [[h["t"], (1 - h["p"]) if flip else h["p"]] for h in hist]
        g["pm"]["history"] = pts
        kickoff = datetime.fromisoformat(g["start"].replace("Z", "+00:00")).timestamp()
        g["kickoff"] = kickoff
        pre = [p for p in pts if p[0] < kickoff]
        if g["state"] != "pre" and pre:
            g["pm"]["preMatchClose"] = pre[-1][1]
        pts = pre  # integrity signals use pre-match trading only; in-play moves follow the score
        if len(pts) >= 2:
            now_t = pts[-1][0]
            past = [p for p in pts if p[0] <= now_t - 24 * 3600]
            ref = past[-1][1] if past else pts[0][1]
            g["pm"]["move24"] = pts[-1][1] - ref
            recent = [p for p in pts if p[0] >= now_t - 48 * 3600]
            best, at = 0.0, None
            for i in range(1, len(recent)):
                # biggest move within any ~2h window (4 points at 30-min fidelity)
                j0 = max(0, i - 4)
                d = abs(recent[i][1] - recent[j0][1])
                if d > best:
                    best, at = d, recent[i][0]
            g["pm"]["maxJump"], g["pm"]["maxJumpAt"] = best, at

    with ThreadPoolExecutor(8) as ex:
        list(ex.map(add_history, work))
    for g in games:
        score_game(g)
    games.sort(key=lambda g: (-g["score"], g["start"]))
    return {
        "league": league, "generated": datetime.now(timezone.utc).isoformat(), "games": games,
        "coverage": {"fixtures": len(games), "polymarket": sum(1 for g in games if g.get("pm")),
                     "book": sum(1 for g in games if g.get("book")), "kalshi": sum(1 for g in games if g.get("kalshi"))},
    }


# ---------------------------------------------------------------- backtest
def build_backtest(div: str, season: str):
    text = fetch(f"https://www.football-data.co.uk/mmz4281/{season}/{div}.csv", ttl=3600, as_json=False)
    rows = list(csv.DictReader(io.StringIO(text)))
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
        # bookmaker disagreement at close: best price vs average price on the drifted outcome
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
    mean = sum(absd) / n if n else 0
    sd = (sum((x - mean) ** 2 for x in absd) / n) ** 0.5 if n else 1
    for m in matches:
        m["z"] = round((abs(m["driftPP"]) - mean) / (sd or 1), 2)
    # Is the late money informative? Compare opening vs closing probability for the eventual result.
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


# ---------------------------------------------------------------- server
class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(ROOT), **kw)

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        try:
            if u.path == "/api/live":
                lg = q.get("league", "nba")
                if lg not in LEAGUES:
                    return self._json({"error": "unknown league"}, 400)
                return self._json(build_live(lg))
            if u.path == "/api/backtest":
                div, season = q.get("div", "E0"), q.get("season", "2526")
                if div not in FD_LEAGUES or not re.fullmatch(r"\d{4}", season):
                    return self._json({"error": "bad params"}, 400)
                return self._json(build_backtest(div, season))
        except Exception as exc:  # surface upstream problems to the UI
            return self._json({"error": f"{type(exc).__name__}: {exc}"}, 502)
        return super().do_GET()


if __name__ == "__main__":
    print(f"Integrity Radar running on http://localhost:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
