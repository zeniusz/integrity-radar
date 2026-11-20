"""Upstream sources. Each returns (data, status) so the UI can tell
"no fixtures" / "not listed" apart from "source unavailable"."""
import json
import re
import time
from datetime import datetime, timedelta, timezone

from .net import fetch
from .odds import american_to_prob, devig2

LEAGUES = {
    "nba": {"espn": "basketball/nba", "kalshi": "KXNBAGAME", "pm_series": "10345", "name": "NBA"},
    "nhl": {"espn": "hockey/nhl", "kalshi": "KXNHLGAME", "pm_series": "10346", "name": "NHL"},
    "nfl": {"espn": "football/nfl", "kalshi": "KXNFLGAME", "pm_series": "12185", "name": "NFL"},
}

# A two-sided Kalshi quote wider than this is not treated as a usable price.
KALSHI_MAX_SPREAD = 0.10


def _status(ok, error=None, partial=False):
    return {"ok": ok, "partial": partial, "error": error, "fetchedAt": time.time()}


def espn_games(league: str):
    path = LEAGUES[league]["espn"]
    base = f"https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard"
    urls = [base]
    if league != "nfl":  # NFL default scoreboard already returns the whole week
        tomorrow = (datetime.now(timezone.utc) + timedelta(hours=-5) + timedelta(days=1)).strftime("%Y%m%d")
        urls.append(f"{base}?dates={tomorrow}")
    games, seen, errors = [], set(), []
    for u in urls:
        try:
            data = fetch(u, ttl=90)
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            continue
        for ev in data.get("events", []):
            if ev["id"] in seen:
                continue
            seen.add(ev["id"])
            g = parse_espn_event(ev, league)
            if g:
                games.append(g)
    if errors and len(errors) == len(urls):
        return [], _status(False, errors[0])
    return games, _status(True, errors[0] if errors else None, partial=bool(errors))


def parse_espn_event(ev, league, now=None):
    comp = ev["competitions"][0]
    teams = {c["homeAway"]: c["team"] for c in comp["competitors"]}
    if "home" not in teams or "away" not in teams:
        return None
    keys = ("abbreviation", "name", "location", "displayName", "logo", "color", "alternateColor")
    g = {
        "id": ev["id"],
        "league": league,
        "name": ev.get("name"),
        "start": ev.get("date"),
        "state": ev["status"]["type"]["state"],  # pre / in / post
        "statusText": ev["status"]["type"].get("shortDetail"),
        "home": {k: teams["home"].get(k) for k in keys},
        "away": {k: teams["away"].get(k) for k in keys},
        "book": None,
    }
    odds = (comp.get("odds") or [None])[0]
    if odds and odds.get("moneyline"):
        ml = odds["moneyline"]
        side = lambda s, w: ((ml.get(s) or {}).get(w) or {}).get("odds")
        h_c, a_c = american_to_prob(side("home", "close")), american_to_prob(side("away", "close"))
        h_o, a_o = american_to_prob(side("home", "open")), american_to_prob(side("away", "open"))
        cur, _ = devig2(h_c, a_c)
        opn, _ = devig2(h_o, a_o)
        if cur is not None:
            g["book"] = {
                "provider": (odds.get("provider") or {}).get("name", "Bookmaker"),
                "homeNow": cur,
                # No genuine opening quote -> no opening price. Never substitute the current one.
                "homeOpen": opn,
                "margin": (h_c + a_c - 1),
                "rawHome": side("home", "close"),
                "rawAway": side("away", "close"),
                "rawHomeOpen": side("home", "open"),
                "spread": odds.get("details"),
                # ESPN gives no quote timestamp; we only know when we fetched it.
                "observedAt": now or time.time(),
                "timestampKnown": False,
            }
    return g


SLUG_RE = re.compile(r"^(nba|nhl|nfl)-[a-z0-9]+-[a-z0-9]+-\d{4}-\d{2}-\d{2}$")


def polymarket_events(league: str):
    data = []
    try:
        for offset in (0, 100, 200, 300):
            page = fetch(f"https://gamma-api.polymarket.com/events?series_id={LEAGUES[league]['pm_series']}"
                         f"&active=true&closed=false&limit=100&offset={offset}", ttl=60)
            data.extend(page)
            if len(page) < 100:
                break
    except Exception as exc:
        if not data:
            return [], _status(False, f"{type(exc).__name__}: {exc}")
        return parse_polymarket(data), _status(True, f"{type(exc).__name__}: {exc}", partial=True)
    return parse_polymarket(data), _status(True)


def parse_polymarket(data):
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
        bid, ask = ml.get("bestBid"), ml.get("bestAsk")
        out.append({
            "slug": e["slug"], "start": e.get("startTime"), "outcomes": outcomes, "prices": prices,
            "tokens": tokens, "volume": float(ml.get("volumeNum") or 0), "liquidity": float(ml.get("liquidityNum") or 0),
            "volume24h": float(e.get("volume24hr") or 0),
            "bid": float(bid) if bid not in (None, "") else None,
            "ask": float(ask) if ask not in (None, "") else None,
            "url": f"https://polymarket.com/event/{e['slug']}",
        })
    return out


def polymarket_history(token: str):
    url = f"https://clob.polymarket.com/prices-history?market={token}&interval=1w&fidelity=30"
    try:
        return fetch(url, ttl=120).get("history", []), True
    except Exception:
        return [], False


def kalshi_markets(league: str):
    series = LEAGUES[league]["kalshi"]
    url = f"https://api.elections.kalshi.com/trade-api/v2/markets?limit=1000&status=open&series_ticker={series}"
    try:
        mk = fetch(url, ttl=60).get("markets", [])
    except Exception as exc:
        return {}, _status(False, f"{type(exc).__name__}: {exc}")
    return parse_kalshi(mk), _status(True)


def kalshi_quote(m):
    """Usable mid only from a valid, reasonably tight two-sided quote. Last trades are never used as a price."""
    bid = float(m.get("yes_bid_dollars") or 0)
    ask = float(m.get("yes_ask_dollars") or 0)
    if bid <= 0 or ask <= 0 or ask < bid:
        return None, "no two-sided quote"
    if ask - bid > KALSHI_MAX_SPREAD:
        return None, f"bid-ask spread {round((ask - bid) * 100)}¢ too wide"
    return (bid + ask) / 2, None


def parse_kalshi(mk):
    events: dict = {}
    for m in mk:
        ev = m.get("event_ticker", "")
        abbr = m["ticker"].rsplit("-", 1)[-1]
        mid, why = kalshi_quote(m)
        events.setdefault(ev, {"date": ev.split("-")[1][:7] if "-" in ev else "", "teams": {}})
        events[ev]["teams"][abbr] = {
            "mid": mid, "unusable": why,
            "bid": float(m.get("yes_bid_dollars") or 0), "ask": float(m.get("yes_ask_dollars") or 0),
            "last": float(m.get("last_price_dollars") or 0) or None,
            "volume": float(m.get("volume_fp") or 0), "ticker": m["ticker"],
        }
    return events
