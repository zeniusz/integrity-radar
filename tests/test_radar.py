"""Offline tests. Upstream APIs are mocked, so nothing depends on live sports markets.

Run:  python -m unittest discover tests
"""
import os
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

from radar import live, matching, odds, scoring, sources, storage

NOW = 1_800_000_000.0
KICK = NOW + 10 * 3600


def team(abbr, name, loc):
    return {"abbreviation": abbr, "name": name, "location": loc, "displayName": f"{loc} {name}"}


def game(**kw):
    g = {"id": "1", "league": "nba", "state": "pre", "start": "2027-01-15T08:00Z", "kickoff": KICK,
         "home": team("IND", "Pacers", "Indiana"), "away": team("NY", "Knicks", "New York")}
    g.update(kw)
    return g


def pm(**kw):
    d = {"homeNow": 0.40, "volume": 1_000_000, "quoteAge": 600, "spread": 0.01}
    d.update(kw)
    return d


class Odds(unittest.TestCase):
    def test_american(self):
        self.assertAlmostEqual(odds.american_to_prob("-200"), 2 / 3)
        self.assertAlmostEqual(odds.american_to_prob("+100"), 0.5)
        self.assertEqual(odds.american_to_prob("EVEN"), 0.5)
        self.assertIsNone(odds.american_to_prob("abc"))
        self.assertIsNone(odds.american_to_prob(None))

    def test_devig(self):
        h, a = odds.devig2(0.55, 0.50)
        self.assertAlmostEqual(h + a, 1)
        self.assertEqual(odds.devig2(None, 0.5), (None, None))

    def test_effective_pp_grows_near_extremes(self):
        self.assertAlmostEqual(odds.effective_pp(0.5, 0.55), 5.0, delta=0.1)
        self.assertGreater(odds.effective_pp(0.85, 0.90), 7)


class EspnParsing(unittest.TestCase):
    def ev(self, open_=True):
        ml = {"home": {"close": {"odds": "+150"}}, "away": {"close": {"odds": "-170"}}}
        if open_:
            ml["home"]["open"] = {"odds": "+120"}
            ml["away"]["open"] = {"odds": "-140"}
        return {"id": "9", "name": "x", "date": "2027-01-15T00:00Z",
                "status": {"type": {"state": "pre", "shortDetail": ""}},
                "competitions": [{"competitors": [{"homeAway": "home", "team": team("IND", "Pacers", "Indiana")},
                                                  {"homeAway": "away", "team": team("NY", "Knicks", "New York")}],
                                  "odds": [{"moneyline": ml, "provider": {"name": "DraftKings"}}]}]}

    def test_missing_opening_is_none_not_current(self):
        g = sources.parse_espn_event(self.ev(open_=False), "nba")
        self.assertIsNone(g["book"]["homeOpen"])
        g["kickoff"] = KICK
        scoring.score_game(g, now=NOW)
        self.assertNotIn("book_move", [r["code"] for r in g["reasons"]])
        self.assertNotIn("bookMove", g)

    def test_opening_kept(self):
        g = sources.parse_espn_event(self.ev(), "nba")
        self.assertIsNotNone(g["book"]["homeOpen"])


class Kalshi(unittest.TestCase):
    def test_last_trade_never_used(self):
        mid, why = sources.kalshi_quote({"yes_bid_dollars": 0, "yes_ask_dollars": 0, "last_price_dollars": 0.6})
        self.assertIsNone(mid)
        self.assertIn("two-sided", why)

    def test_wide_spread_rejected(self):
        mid, why = sources.kalshi_quote({"yes_bid_dollars": 0.30, "yes_ask_dollars": 0.55})
        self.assertIsNone(mid)
        self.assertIn("wide", why)

    def test_valid_mid(self):
        mid, _ = sources.kalshi_quote({"yes_bid_dollars": 0.40, "yes_ask_dollars": 0.42})
        self.assertAlmostEqual(mid, 0.41)


class Matching(unittest.TestCase):
    def pm_event(self, slug, start, outcomes=("Pacers", "Knicks")):
        return {"slug": slug, "start": start, "outcomes": list(outcomes), "prices": [0.4, 0.6], "tokens": ["a", "b"]}

    def test_polymarket_unique_high(self):
        g = game(start="2027-01-16T00:00Z")
        m = matching.match_polymarket(g, [self.pm_event("nba-ny-ind-2027-01-15", "2027-01-16T00:00:00Z")])
        self.assertEqual(m["confidence"], "high")
        self.assertEqual(m["homeIndex"], 0)

    def test_polymarket_ambiguous_rejected(self):
        g = game(start="2027-01-16T00:00Z")
        evs = [self.pm_event("nba-ny-ind-2027-01-15", "2027-01-16T02:00:00Z"),
               self.pm_event("nba-ny-ind-2027-01-16", "2027-01-16T03:00:00Z")]
        m = matching.match_polymarket(g, evs)
        self.assertIsNone(m["event"])
        self.assertIn("ambiguous", m["reason"])

    def test_polymarket_wrong_date_rejected(self):
        g = game(start="2027-01-16T00:00Z")
        m = matching.match_polymarket(g, [self.pm_event("nba-ny-ind-2027-01-20", None)])
        self.assertEqual(m["reason"], "not listed")

    def test_kalshi_alias_and_confidence(self):
        g = game(start="2027-01-16T00:00Z")
        ev = {"KXNBAGAME-27JAN15NYKIND": {"date": "27JAN15", "teams": {"NYK": {"mid": 0.6}, "IND": {"mid": 0.4}}}}
        m = matching.match_kalshi(g, ev)
        self.assertEqual(m["confidence"], "high")
        self.assertEqual(m["team"]["mid"], 0.4)

    def test_kalshi_ambiguous(self):
        g = game(start="2027-01-16T00:00Z")
        t = {"NYK": {"mid": 0.6}, "IND": {"mid": 0.4}}
        ev = {"A-27JAN14NYKIND": {"date": "27JAN14", "teams": t}, "B-27JAN16NYKIND": {"date": "27JAN16", "teams": t}}
        self.assertIsNone(matching.match_kalshi(g, ev)["team"])


class Scoring(unittest.TestCase):
    def codes(self, g):
        return [r["code"] for r in g["reasons"]]

    def test_boundaries(self):
        g = scoring.score_game(game(), now=NOW)
        self.assertEqual((g["score"], g["level"]), (0, "normal"))
        for score, level in ((19, "normal"), (20, "watch"), (44, "watch"), (45, "alert")):
            with mock.patch.object(scoring, "combine", return_value=score):
                self.assertEqual(scoring.score_game(game(), now=NOW)["level"], level)

    def test_stale_quote_suppresses_divergence(self):
        g = scoring.score_game(game(pm=pm(quoteAge=5 * 3600), book={"provider": "DK", "homeNow": 0.30, "homeOpen": 0.30}), now=NOW)
        self.assertNotIn("divergence", self.codes(g))
        self.assertIn("suppressed", self.codes(g))
        self.assertEqual(g["dataQuality"]["grade"], "low")

    def test_fresh_quote_scores_divergence(self):
        g = scoring.score_game(game(pm=pm(), book={"provider": "DK", "homeNow": 0.30, "homeOpen": 0.30}), now=NOW)
        self.assertIn("divergence", self.codes(g))

    def test_wide_spread_suppresses(self):
        g = scoring.score_game(game(pm=pm(spread=0.10), kalshi={"homeNow": 0.50}), now=NOW)
        self.assertNotIn("kalshi_gap", self.codes(g))

    def test_partial_history_does_not_fire_24h_move(self):
        pts = [[NOW - 6 * 3600, 0.40], [NOW, 0.55]]
        mv = scoring.move_over_window(pts)
        self.assertFalse(mv["covered"])
        self.assertAlmostEqual(mv["windowH"], 6)
        g = scoring.score_game(game(pm=pm(move=mv)), now=NOW)
        self.assertNotIn("pm_move", self.codes(g))

    def test_full_history_move(self):
        pts = [[NOW - 25 * 3600, 0.40], [NOW - 24 * 3600, 0.40], [NOW, 0.50]]
        mv = scoring.move_over_window(pts)
        self.assertTrue(mv["covered"])
        g = scoring.score_game(game(pm=pm(move=mv)), now=NOW)
        self.assertIn("pm_move", self.codes(g))

    def test_unconfirmed_needs_same_window(self):
        mv = {"pp": 8, "eff": 8, "windowH": 24, "covered": True}
        book = {"provider": "DK", "homeNow": 0.40, "homeOpen": 0.40}
        g = scoring.score_game(game(pm=pm(move=mv), book=book), now=NOW)
        self.assertNotIn("unconfirmed", self.codes(g))
        g = scoring.score_game(game(pm=pm(move=mv), book=book, bookWindowMove={"pp": 0.5, "windowH": 23}), now=NOW)
        self.assertIn("unconfirmed", self.codes(g))

    def test_correlated_signals_discounted(self):
        mv = {"pp": 10, "eff": 10, "windowH": 24, "covered": True}
        g = scoring.score_game(game(pm=pm(move=mv, maxJump=0.10)), now=NOW)
        r = {x["code"]: x for x in g["reasons"]}
        self.assertTrue(r["pm_jump"].get("correlated"))
        self.assertEqual(g["score"], int(r["pm_move"]["pts"] + r["pm_jump"]["pts"] + 0.5))

    def test_baseline_replaces_fixed_threshold(self):
        mv = {"pp": 6, "eff": 6, "windowH": 24, "covered": True}
        busy = [5 + i * 0.1 for i in range(40)]  # league where 6pp moves are routine
        g = scoring.score_game(game(pm=pm(move=mv)), now=NOW, baseline={"pm_move": busy})
        self.assertNotIn("pm_move", self.codes(g))
        g = scoring.score_game(game(pm=pm(move=mv)), now=NOW, baseline={"pm_move": [1.0] * 40})
        self.assertEqual(g["reasons"][0]["percentile"], 100)


class Storage(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)

    def tearDown(self):
        try:
            os.remove(self.db)
        except OSError:
            pass

    def test_checklist_tick_is_not_resolution(self):
        cid = "nba-1"
        self.assertEqual(storage.case_status(cid, path=self.db), "new")
        storage.add_event(cid, "investigate", snapshot={"league": "nba"}, path=self.db)
        self.assertEqual(storage.case_status(cid, path=self.db), "investigating")
        storage.add_event(cid, "monitor", snapshot={"league": "nba"}, path=self.db)
        self.assertNotIn(storage.case_status(cid, path=self.db), storage.RESOLVED)
        storage.add_event(cid, "dismiss", "starter out", snapshot={"league": "nba"}, path=self.db)
        self.assertIn(storage.case_status(cid, path=self.db), storage.RESOLVED)
        with self.assertRaises(ValueError):
            storage.add_event(cid, "investigate", path=self.db)
        with self.assertRaises(ValueError):
            storage.add_event(cid, "bogus", path=self.db)

    def test_events_keep_snapshot_and_are_immutable(self):
        storage.add_event("nba-2", "escalate", "x", snapshot={"league": "nba", "score": 50, "signals": [{"code": "pm_jump"}]}, path=self.db)
        storage.add_event("nba-2", "dismiss", "y", snapshot={"league": "nba", "score": 10, "signals": []}, path=self.db)
        ev = storage.events(cid="nba-2", path=self.db)
        self.assertEqual([e["score"] for e in ev], [50, 10])
        self.assertEqual(ev[0]["signals"][0]["code"], "pm_jump")
        con = sqlite3.connect(self.db)
        with self.assertRaises(sqlite3.DatabaseError):
            con.execute("UPDATE events SET score=0")
        with self.assertRaises(sqlite3.DatabaseError):
            con.execute("DELETE FROM events")
        con.close()
        csv_text = storage.events_csv(path=self.db)
        self.assertIn("score_at_decision", csv_text)
        self.assertEqual(csv_text.count("nba-2"), 2)

    def test_snapshots_first_seen_and_book_window(self):
        g = game(book={"homeNow": 0.40}, reasons=[{"code": "pm_jump", "pp": 7.0, "pts": 8}], score=8, level="normal")
        storage.record_snapshot([g], t=NOW - 20 * 3600, path=self.db)
        g2 = dict(g, book={"homeNow": 0.41}, reasons=[{"code": "pm_jump", "pp": 9.0, "pts": 10}])
        storage.record_snapshot([g2], t=NOW, path=self.db)
        sh = storage.signal_history("nba-1", path=self.db)[0]
        self.assertEqual(sh["first_seen"], NOW - 20 * 3600)
        self.assertEqual(sh["peak_pp"], 9.0)
        ser = storage.series("nba-1", now=NOW, path=self.db)
        self.assertEqual([r[2] for r in ser], [0.40, 0.41])
        self.assertIsNone(ser[0][3])  # no Kalshi observation stays None, not 0
        bw = storage.book_window_move("nba-1", 24 * 3600, now=NOW, path=self.db)
        self.assertAlmostEqual(bw["pp"], 1.0)
        self.assertAlmostEqual(bw["windowH"], 20)


class LiveSourceFailures(unittest.TestCase):
    def test_source_down_is_reported_not_empty(self):
        g = game(start="2027-01-16T00:00Z", book=None)
        with mock.patch.object(sources, "espn_games", return_value=([g], {"ok": True})), \
             mock.patch.object(sources, "polymarket_events", return_value=([], {"ok": False, "error": "timeout"})), \
             mock.patch.object(sources, "kalshi_markets", return_value=({}, {"ok": True})):
            d = live.build_live("nba", now=NOW, persist=False)
        gg = d["games"][0]
        self.assertEqual(gg["missing"]["polymarket"], "source unavailable")
        self.assertEqual(gg["missing"]["kalshi"], "not listed")
        self.assertFalse(d["sources"]["polymarket"]["ok"])


if __name__ == "__main__":
    unittest.main()
