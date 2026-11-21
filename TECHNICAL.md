# Integrity Radar: technical notes

Details for developers and analysts. For a plain-language overview, see the [README](README.md).

## Run it

Needs Python 3.9 or newer. There are no dependencies to install and no API keys or accounts.

```bash
python server.py
```

Then open http://localhost:8765.

## How the score works

Every price is turned into a win probability for the home team, with the bookmaker's margin removed. Each case gets three separate outputs:

- **Review priority (0–100)**: which case to look at first. **Review** from 20, **Escalate** from 45. It is *not* a probability of match-fixing.
- **Data quality (high / medium / low)**: whether the evidence can be trusted (quote freshness, spreads, liquidity, match confidence, missing opening lines).
- **Explanation**: from the analyst's structured assessment of each possible legitimate cause (injury or lineup news, public news, weather for NFL, thin market, stale quote...), each marked Confirmed, Possible, No evidence found, Insufficient information or Not checked. Any confirmed cause makes the case "Explained". The assessments are saved with every decision.

Signals, using pre-match trading only:

| Signal | Fires when | Context adjustments |
|---|---|---|
| Markets disagree | Polymarket and the bookmaker differ by 3pp+ | Suppressed if the Polymarket quote is stale (no trade for 3h) or its spread is over 6¢. Discounted because ESPN publishes no quote time |
| Kalshi disagrees | Kalshi and Polymarket differ by 6pp+ | Same suppression. Kalshi needs a two-sided quote no wider than 10¢; last trades are never used as a price |
| Bookmaker line moved | DraftKings moved 4pp+ (even-odds equivalent) since opening | Only when a genuine opening line exists |
| Polymarket moved | 5pp+ (even-odds equivalent) over 24h | Needs 20h+ of history; reports the actual window. With 30+ collected fixtures in the league, fires only above the league's 90th percentile instead |
| Sharp jump | 6pp+ inside a two-hour window | |
| Bookmaker didn't follow | Polymarket moved 5pp+ while DraftKings moved under 2pp | Both measured over the same window, using the collector's bookmaker snapshots |

- **Probability range**: moves are measured in log-odds and expressed as "pp at even odds", so 85%→90% counts more than 50%→55%.
- **Time before start**: movement signals count 70% inside 2h of the start and 85% inside 6h, when lineup news routinely moves prices.
- **Liquidity**: Polymarket signals are weighted by volume (under $25k about a third, under $100k about 60%).
- **Correlated signals**: in each group (disagreement; movement) the strongest counts in full and the rest at half.

Thresholds are still hand-set for this prototype; the league baseline replaces the fixed movement threshold as history accumulates.

## Monitoring and storage

`server.py` runs a background collector that builds every league every 5 minutes (`IR_COLLECT_EVERY`, seconds; `0` disables) and writes to `data/integrity.db` (SQLite; `IR_DB` overrides the path):

- `snapshots`: prices, score and signals per fixture per run
- `signals`: when each signal was first and last seen, and its peak size
- `events`: analyst decisions. Immutable (SQLite triggers block updates and deletes); each row keeps the action, rationale, and the score, signals and evidence *at the moment of the decision*

Case statuses: `new` → `investigating` (first checklist tick) → `monitoring` / `dismissed` / `escalated`. Only dismissed and escalated count as resolved; monitored cases stay active.

## API

`server.py` serves the page and a small JSON API on `http://localhost:8765`. You can also call the endpoints from scripts or notebooks.

### `GET /api/live?league={nba|nfl|nhl}`

Today's and tomorrow's fixtures for one league, matched across all three markets and scored. Games come back sorted by score, highest first.

```bash
curl "http://localhost:8765/api/live?league=nba"
```

Abbreviated response (one game shown):

```json
{
  "league": "nba",
  "generated": "2026-11-13T02:00:27+00:00",
  "coverage": { "fixtures": 13, "polymarket": 13, "book": 10, "kalshi": 13 },
  "games": [
    {
      "id": "401909296",
      "name": "New York Knicks at Indiana Pacers",
      "start": "2026-11-14T00:00Z",
      "state": "pre",
      "home": { "abbreviation": "IND", "name": "Pacers", "color": "0c2340" },
      "away": { "abbreviation": "NY", "name": "Knicks", "color": "1d428a" },
      "book":   { "provider": "DraftKings", "homeNow": 0.321, "homeOpen": 0.457, "rawHome": "+199", "rawAway": "-241" },
      "pm":     { "homeNow": 0.285, "volume": 38502, "move24": -0.01, "maxJump": 0.04, "history": [[1793390430, 0.39], "..."] },
      "kalshi": { "homeNow": 0.285, "bid": 0.28, "ask": 0.29 },
      "divergence": -3.6,
      "bookMove": -13.6,
      "score": 27,
      "level": "watch",
      "reasons": [
        { "code": "book_move",  "pp": -13.6, "pts": 20.4 },
        { "code": "divergence", "pp": -3.6,  "pts": 6.5 }
      ]
    }
  ]
}
```

| Field | Meaning |
|---|---|
| `state` | `pre` (before the start), `in` (in play) or `post` (finished) |
| `book.homeNow`, `book.homeOpen` | Bookmaker win probability for the home team, now and at opening, with the margin removed |
| `pm.homeNow` | Polymarket price for the home team (0–1). `pm.history` is `[unix_time, price]` pairs every 30 minutes over 7 days |
| `pm.move24`, `pm.maxJump` | Change over the 24 hours before kickoff, and the largest move inside any two-hour window |
| `kalshi.homeNow` | Midpoint of Kalshi's bid and ask for the home team |
| `divergence`, `bookMove`, `kalshiGap` | Differences in percentage points (pp) |
| `score`, `level` | 0–100 review priority; `normal` below 20, `watch` from 20, `alert` from 45 |
| `dataQuality` | `{grade, issues}` |
| `missing` | Per source: `not listed`, `source unavailable`, `ambiguous (...)`, or why a listed quote was unusable |
| `pm.quoteAge`, `pm.spread`, `pm.move` | Seconds since last trade, bid-ask spread, and the 24h move with its real window (`windowH`, `covered`) |
| `series` | Observed prices per collector run, `[t, polymarket, bookmaker, kalshi]`, `null` where not observed |
| `signalHistory`, `case` | First-seen times from the collector; case status and its decision events |
| `sources` | Per source: `ok`, `partial`, `error`, `fetchedAt` |
| `reasons` | The signals behind the score, each with its size (`pp`) and points added (`pts`) |

A missing `book`, `pm` or `kalshi` object means there is no usable price; `missing` says why.

### `POST /api/decision`

Body: `{"caseId": "nba-401909296", "action": "investigate|monitor|dismiss|escalate", "rationale": "...", "snapshot": {...}}`. Appends an immutable event and returns the case's status and events.

### `GET /api/decisions.csv`

Every decision event, with the score, signals and evidence recorded at the time.

### `GET /api/evaluation`

Detector behaviour from collected data: fixtures seen, flagged and escalate-level per 100 fixtures, analyst outcomes, and dismissed share (a rough false-alert rate).

### `GET /api/history?div={E0|D1|SP1|I1|F1}&season={YYYY}`

Historical market analysis (formerly `/api/backtest`, which still works): one soccer season's opening vs closing odds. This is market context; it does not validate the live detector. `div` is the league (Premier League, Bundesliga, La Liga, Serie A, Ligue 1) and `season` is four digits, e.g. `2526` for 2025/26.

```bash
curl "http://localhost:8765/api/backtest?div=E0&season=2526"
```

The response has season stats (`n`, `meanAbsDrift`, `brierOpen`, `brierClose`), a `buckets` table comparing opening probabilities with how often outcomes happened, and a `matches` list. Each match has its `open` and `close` probabilities for home/draw/away, the biggest move (`driftPP`), how unusual it was (`z`), the bookmaker spread at close (`dispersion`) and the result.

### Errors

Bad parameters return `400`, and a failing upstream source returns `502`, both as `{"error": "..."}`.

## Data sources

All public, no registration and no API keys:

| Source | Endpoint used | Used for |
|---|---|---|
| ESPN | `site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard` | Fixtures, team details, DraftKings opening and current moneyline |
| Polymarket Gamma API | `gamma-api.polymarket.com/events?series_id=…` | Game markets and current prices |
| Polymarket CLOB API | `clob.polymarket.com/prices-history?market={token}` | 7-day price history |
| Kalshi | `api.elections.kalshi.com/trade-api/v2/markets?series_ticker=…` | Second prediction market (current bid and ask) |
| football-data.co.uk | `football-data.co.uk/mmz4281/{season}/{div}.csv` | Historical opening and closing odds for backtests |

These are unofficial or public endpoints and may change without notice. The server caches responses for 60–120 seconds (an hour for backtest CSVs) to keep request volume low.

## Limits

- ESPN shows one bookmaker's opening and current line with no quote timestamps; bookmaker history exists only from the collector's own snapshots.
- Polymarket history is sampled every 30 minutes; Kalshi shows current quotes only.
- Team news is not connected; the evidence timeline marks it as unverified.
- The price chart shows Polymarket history plus bookmaker and Kalshi prices from the collector's snapshots. Dots are observations, lines between them are interpolated, dashed segments cross gaps of over 90 minutes. Volume history is not available from these sources.

## Deployment

`render.yaml` deploys the server on Render's free tier. Environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `PORT` | `8765` | Port to listen on (set by the host) |
| `HOST` | `127.0.0.1` | `0.0.0.0` when hosted |
| `IR_READONLY` | `0` | `1` makes `POST /api/decision` return 403 and hides decision controls |
| `IR_COLLECT_EVERY` | `300` | Collector interval in seconds, `0` disables |
| `IR_DB` | `data/integrity.db` | SQLite path |

`GET /healthz` returns `{"ok": true}` for the host's health check.

## Tests

```bash
python -m unittest discover tests
```

Covers price conversion, matching (aliases, ambiguity, dates), scoring boundaries, missing opening lines, stale and wide quotes, partial history, same-window comparison, correlated signals, league baselines, case-state transitions, immutable decision events and source failures. Upstream APIs are mocked.

## Project structure

```
server.py          Web server, JSON API, background collector
radar/net.py       HTTP fetch with cache
radar/odds.py      Price conversion, margin removal, log-odds moves
radar/sources.py   ESPN, Polymarket, Kalshi clients with source status
radar/matching.py  Unique fixture matching with confidence
radar/scoring.py   Signals, priority, data quality
radar/live.py      Builds the scored live view
radar/storage.py   SQLite snapshots, signal history, decision events, evaluation
radar/history.py   Historical market analysis (football-data.co.uk)
tests/             Offline unit tests
index.html         Single-page interface (no build step)
DESIGN.md          Visual design plan
docs/              Screenshots used in the README
```
