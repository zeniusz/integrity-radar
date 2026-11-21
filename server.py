"""Integrity Radar - local data server.

Pure Python standard library, no API keys, no registrations.
Sources:
  - ESPN public scoreboard (fixtures + DraftKings opening/current moneyline)
  - Polymarket Gamma API (prediction-market prices) + CLOB price history
  - Kalshi public market data (second prediction market)
  - football-data.co.uk CSVs (historical opening/closing bookmaker odds)

A background collector snapshots every league every few minutes into data/integrity.db,
so signals get a first-seen time and the bookmaker can be compared over the same window.

Run:  python server.py   then open http://localhost:8765
Env:  IR_COLLECT_EVERY=300 (seconds, 0 disables)  IR_DB=path/to.db
      PORT, HOST (0.0.0.0 in the cloud)  IR_READONLY=1 (block new decisions)
"""
import json
import os
import re
import threading
import time
import urllib.parse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from radar import storage
from radar.history import FD_LEAGUES, build_history
from radar.live import build_live
from radar.sources import LEAGUES

PORT = int(os.environ.get("PORT", "8765"))
HOST = os.environ.get("HOST", "127.0.0.1")
# Public deployments: visitors can read cases and decisions but not record new ones.
READONLY = os.environ.get("IR_READONLY", "0") == "1"
ROOT = Path(__file__).parent
COLLECT_EVERY = int(os.environ.get("IR_COLLECT_EVERY", "300"))
_live_lock = threading.Lock()  # one build at a time keeps snapshots ordered


def live(league):
    with _live_lock:
        d = build_live(league)
    d["readonly"] = READONLY
    st = storage.statuses(league)
    for g in d["games"]:
        s = st.get(g["caseId"])
        g["case"] = s or {"status": "new"}
        g["case"]["events"] = storage.events(cid=g["caseId"]) if s else []
    return d


def collector():
    while True:
        for lg in LEAGUES:
            try:
                with _live_lock:
                    build_live(lg)
            except Exception as exc:
                print(f"collector {lg}: {type(exc).__name__}: {exc}")
        time.sleep(COLLECT_EVERY)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(ROOT), **kw)

    def log_message(self, fmt, *args):
        pass

    def _send(self, body: bytes, ctype, code=200, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(json.dumps(obj).encode(), "application/json", code)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        if u.path.startswith("/data/") or u.path.endswith(".db"):
            return self._json({"error": "not found"}, 404)
        try:
            if u.path == "/api/live":
                lg = q.get("league", "nba")
                if lg not in LEAGUES:
                    return self._json({"error": "unknown league"}, 400)
                return self._json(live(lg))
            if u.path in ("/api/history", "/api/backtest"):
                div, season = q.get("div", "E0"), q.get("season", "2526")
                if div not in FD_LEAGUES or not re.fullmatch(r"\d{4}", season):
                    return self._json({"error": "bad params"}, 400)
                return self._json(build_history(div, season))
            if u.path == "/healthz":
                return self._json({"ok": True})
            if u.path == "/api/evaluation":
                return self._json(storage.evaluation())
            if u.path == "/api/decisions.csv":
                return self._send(storage.events_csv().encode(), "text/csv", extra={
                    "Content-Disposition": 'attachment; filename="integrity-decisions.csv"'})
        except Exception as exc:  # surface upstream problems to the UI
            return self._json({"error": f"{type(exc).__name__}: {exc}"}, 502)
        return super().do_GET()

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if u.path != "/api/decision":
            return self._json({"error": "not found"}, 404)
        if READONLY:
            return self._json({"error": "This public demo is read-only. Run it locally to record decisions."}, 403)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(min(n, 200_000)) or b"{}")
            cid = str(body.get("caseId", ""))
            if not re.fullmatch(r"(nba|nfl|nhl)-\w+", cid):
                return self._json({"error": "bad caseId"}, 400)
            eid = storage.add_event(cid, body.get("action"), str(body.get("rationale", ""))[:2000],
                                    snapshot=body.get("snapshot") or {})
            return self._json({"id": eid, "status": storage.case_status(cid), "events": storage.events(cid=cid)})
        except ValueError as exc:
            return self._json({"error": str(exc)}, 400)


if __name__ == "__main__":
    if COLLECT_EVERY > 0:
        threading.Thread(target=collector, daemon=True).start()
    print(f"Integrity Radar running on http://{HOST}:{PORT}{' (read-only)' if READONLY else ''}", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
