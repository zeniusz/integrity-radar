"""SQLite persistence: market snapshots, signal first-seen times and immutable analyst decision events."""
import csv
import io
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

DB_PATH = Path(os.environ.get("IR_DB", Path(__file__).resolve().parent.parent / "data" / "integrity.db"))

STATUSES = ("new", "investigating", "monitoring", "dismissed", "escalated")
ACTION_TO_STATUS = {"investigate": "investigating", "monitor": "monitoring",
                    "dismiss": "dismissed", "escalate": "escalated"}
RESOLVED = {"dismissed", "escalated"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
  league TEXT, case_id TEXT, t REAL, state TEXT,
  pm REAL, book REAL, kalshi REAL, pm_move_eff REAL,
  score INTEGER, level TEXT, codes TEXT
);
CREATE INDEX IF NOT EXISTS snap_case ON snapshots(case_id, t);
CREATE TABLE IF NOT EXISTS signals (
  case_id TEXT, code TEXT, first_seen REAL, last_seen REAL, first_pp REAL, peak_pp REAL,
  PRIMARY KEY (case_id, code)
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id TEXT NOT NULL, league TEXT, fixture TEXT, start TEXT,
  at REAL NOT NULL, action TEXT NOT NULL, status TEXT NOT NULL, rationale TEXT,
  score INTEGER, level TEXT, signals TEXT, evidence TEXT
);
CREATE INDEX IF NOT EXISTS ev_case ON events(case_id, id);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
  BEGIN SELECT RAISE(ABORT, 'decision events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
  BEGIN SELECT RAISE(ABORT, 'decision events are immutable'); END;
"""

_lock = threading.Lock()
_init_done = set()


def connect(path=None):
    p = Path(path or DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p), timeout=10)
    con.row_factory = sqlite3.Row
    if str(p) not in _init_done:
        con.executescript(SCHEMA)
        _init_done.add(str(p))
    return con


def case_id(g):
    return f"{g['league']}-{g['id']}"


# ---------------------------------------------------------------- snapshots
def record_snapshot(games, t=None, path=None):
    t = t or time.time()
    with _lock, connect(path) as con:
        for g in games:
            cid = case_id(g)
            pm, book, kal = g.get("pm") or {}, g.get("book") or {}, g.get("kalshi") or {}
            codes = [r["code"] for r in g.get("reasons", []) if "pts" in r]
            con.execute("INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (g["league"], cid, t, g["state"], pm.get("homeNow"), book.get("homeNow"),
                         kal.get("homeNow"), (pm.get("move") or {}).get("eff"), g.get("score"), g.get("level"),
                         "|".join(codes)))
            for r in g.get("reasons", []):
                if "pts" not in r:
                    continue
                pp = abs(r.get("pp") or 0)
                con.execute("""INSERT INTO signals VALUES (?,?,?,?,?,?)
                               ON CONFLICT(case_id, code) DO UPDATE SET last_seen=excluded.last_seen,
                               peak_pp=MAX(peak_pp, excluded.peak_pp)""", (cid, r["code"], t, t, pp, pp))


def book_window_move(cid, window_s, now=None, path=None):
    """Bookmaker move over (roughly) the given window, from our own snapshots."""
    now = now or time.time()
    with connect(path) as con:
        rows = con.execute("SELECT t, book FROM snapshots WHERE case_id=? AND book IS NOT NULL AND t>=? ORDER BY t",
                           (cid, now - window_s - 3600)).fetchall()
    if len(rows) < 2:
        return None
    first, last = rows[0], rows[-1]
    return {"pp": (last["book"] - first["book"]) * 100, "windowH": (last["t"] - first["t"]) / 3600}


def signal_history(cid, path=None):
    with connect(path) as con:
        return [dict(r) for r in con.execute(
            "SELECT code, first_seen, last_seen, first_pp, peak_pp FROM signals WHERE case_id=? ORDER BY first_seen",
            (cid,))]


def move_baseline(league, path=None):
    """Sorted absolute 24h effective moves, one per fixture (its latest pre-match snapshot)."""
    with connect(path) as con:
        rows = con.execute("""SELECT ABS(pm_move_eff) v FROM snapshots s
            WHERE league=? AND state='pre' AND pm_move_eff IS NOT NULL
              AND t=(SELECT MAX(t) FROM snapshots WHERE case_id=s.case_id AND state='pre' AND pm_move_eff IS NOT NULL)""",
                           (league,)).fetchall()
    return sorted(r["v"] for r in rows)


# ---------------------------------------------------------------- decisions
def add_event(cid, action, rationale="", snapshot=None, path=None, at=None):
    if action not in ACTION_TO_STATUS:
        raise ValueError(f"unknown action {action!r}")
    snap = snapshot or {}
    status = ACTION_TO_STATUS[action]
    cur = case_status(cid, path=path)
    if cur in RESOLVED and action == "investigate":
        raise ValueError("case already resolved")
    with _lock, connect(path) as con:
        c = con.execute(
            "INSERT INTO events (case_id, league, fixture, start, at, action, status, rationale, score, level, signals, evidence)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, snap.get("league"), snap.get("fixture"), snap.get("start"), at or time.time(), action, status,
             rationale or "", snap.get("score"), snap.get("level"),
             json.dumps(snap.get("signals") or []), json.dumps(snap.get("evidence") or {})))
        return c.lastrowid


def events(cid=None, league=None, path=None):
    q, args = "SELECT * FROM events", []
    if cid:
        q, args = q + " WHERE case_id=?", [cid]
    elif league:
        q, args = q + " WHERE league=?", [league]
    with connect(path) as con:
        out = []
        for r in con.execute(q + " ORDER BY id", args):
            d = dict(r)
            d["signals"], d["evidence"] = json.loads(d["signals"] or "[]"), json.loads(d["evidence"] or "{}")
            out.append(d)
        return out


def case_status(cid, path=None):
    with connect(path) as con:
        r = con.execute("SELECT status FROM events WHERE case_id=? ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
    return r["status"] if r else "new"


def statuses(league, path=None):
    with connect(path) as con:
        rows = con.execute("""SELECT case_id, status, at, rationale FROM events e WHERE league=? AND
                              id=(SELECT MAX(id) FROM events WHERE case_id=e.case_id)""", (league,)).fetchall()
    return {r["case_id"]: {"status": r["status"], "at": r["at"], "rationale": r["rationale"]} for r in rows}


def events_csv(path=None):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["event_id", "case_id", "league", "fixture", "start", "decided_at_utc", "action", "status",
                "rationale", "score_at_decision", "level_at_decision", "signals_at_decision", "evidence_at_decision"])
    for e in events(path=path):
        w.writerow([e["id"], e["case_id"], e["league"], e["fixture"], e["start"],
                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(e["at"])), e["action"], e["status"], e["rationale"],
                    e["score"], e["level"], json.dumps(e["signals"]), json.dumps(e["evidence"])])
    return buf.getvalue()


# ---------------------------------------------------------------- detector evaluation
def evaluation(path=None):
    """How the live detector behaves: alert volume and what analysts concluded."""
    with connect(path) as con:
        per = con.execute("""SELECT league, COUNT(DISTINCT case_id) n,
              COUNT(DISTINCT CASE WHEN level IN ('watch','alert') THEN case_id END) flagged,
              COUNT(DISTINCT CASE WHEN level='alert' THEN case_id END) alerts,
              MIN(t) since FROM snapshots WHERE state='pre' GROUP BY league""").fetchall()
        outc = con.execute("""SELECT league, status, COUNT(*) c FROM events e
              WHERE id=(SELECT MAX(id) FROM events WHERE case_id=e.case_id) GROUP BY league, status""").fetchall()
    out = {}
    for r in per:
        out[r["league"]] = {"fixtures": r["n"], "flagged": r["flagged"], "alerts": r["alerts"], "since": r["since"],
                            "flaggedPer100": round(r["flagged"] / r["n"] * 100, 1) if r["n"] else None,
                            "alertsPer100": round(r["alerts"] / r["n"] * 100, 1) if r["n"] else None,
                            "outcomes": {}}
    for r in outc:
        out.setdefault(r["league"], {"fixtures": 0, "flagged": 0, "alerts": 0, "outcomes": {}})["outcomes"][r["status"]] = r["c"]
    for v in out.values():
        o = v["outcomes"]
        decided = o.get("dismissed", 0) + o.get("escalated", 0)
        v["dismissRate"] = round(o.get("dismissed", 0) / decided, 3) if decided else None
    return out
