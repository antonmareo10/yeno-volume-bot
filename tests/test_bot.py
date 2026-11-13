"""Unit tests for the Clean Crossing decision engine (stdlib unittest)."""

from __future__ import annotations

import copy
import json
import math
import random
import sys
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import yeno_volume_bot as ccb  # noqa: E402

NOW = 1_789_880_823.619


def observation(**changes):
    """A contract-shaped request (mirrors the public sample's structure)."""
    obs = {
        "schemaVersion": 1,
        "timestamp": NOW,
        "market": {"id": "m-1", "secondsToClose": 60},
        "account": {"cashUsd": 10, "eligibleVolumeUsd": 0, "completedCycles": 0, "position": None},
        "rules": {
            "maximumBuyCashUsd": 5,
            "targetVolumeUsd": 1000,
            "evaluationWindowHours": 24,
            "onePositionAtATime": True,
            "buyFeesIncludedInMaximum": True,
        },
        "reference": {
            "btcMidUsd": 80_600.0,
            "openingTargetUsd": 80_444.946,
            "observedAt": NOW - 0.4,
            "targetObservedAt": NOW - 250.0,
            "targetProvisional": False,
        },
        "books": {
            "YES": {"bids": [[0.89, 100]], "asks": [[0.90, 100]], "minOrderSize": 5},
            "NO": {"bids": [[0.10, 100]], "asks": [[0.11, 100]], "minOrderSize": 5},
        },
    }
    for path, value in changes.items():
        node = obs
        keys = path.split(".")
        for key in keys[:-1]:
            node = node[key]
        node[keys[-1]] = value
    return obs


def position(**changes):
    pos = {
        "market_id": "m-1",
        "outcome": "YES",
        "shares": 5.5,
        "buy_gross_usd": 4.95,
        "buy_fees_usd": 0.06,
        "sell_gross_usd": 0,
        "sell_fees_usd": 0,
    }
    pos.update(changes)
    return pos


def assert_valid(test: unittest.TestCase, decision, obs=None):
    test.assertIsInstance(decision, dict)
    test.assertIn(decision.get("action"), {"HOLD", "BUY", "SELL"})
    if decision["action"] == "BUY":
        test.assertIn(decision["outcome"], {"YES", "NO"})
        test.assertGreater(decision["maxCashUsd"], 0)
        test.assertLessEqual(decision["maxCashUsd"], 5)
        if obs is not None:
            test.assertLessEqual(decision["maxCashUsd"], float(obs["account"]["cashUsd"]) + 1e-9)
            test.assertIsNone(obs["account"]["position"])
            test.assertGreater(float(obs["market"]["secondsToClose"]), 15)
    if decision["action"] == "SELL" and obs is not None:
        test.assertIsNotNone(obs["account"]["position"])
    json.dumps(decision)


class FeeTests(unittest.TestCase):
    def test_protocol_fee_matches_polymarket_table(self):
        # 100 shares at 0.50 -> $1.75 protocol fee (Polymarket crypto table) + 1% overlay
        self.assertAlmostEqual(ccb.side_fees([(0.5, 100)]), 1.75 + 0.5, places=9)

    def test_fee_is_symmetric_and_rounded_to_five_decimals(self):
        a = ccb.side_fees([(0.3, 7)])
        b = ccb.side_fees([(0.7, 3)])  # same protocol fee per share curve, different overlay
        self.assertAlmostEqual(round(a, 5), a, places=12)
        self.assertAlmostEqual(round(b, 5), b, places=12)
        protocol_30 = ccb.side_fees([(0.3, 100)]) - 0.3
        protocol_70 = ccb.side_fees([(0.7, 100)]) - 0.7
        self.assertAlmostEqual(protocol_30, protocol_70, places=9)

    def test_plan_buy_respects_fee_inclusive_budget(self):
        asks = [(0.95, 3.0), (0.96, 50.0)]
        shares, gross, fees = ccb.plan_buy(asks, 5.0, 0.99)
        self.assertLessEqual(gross + fees, 5.0 + 1e-6)
        self.assertGreater(shares, 5.0)

    def test_plan_sell_partial_depth(self):
        sold, gross, _ = ccb.plan_sell([(0.98, 2.0), (0.97, 1.0)], 5.0)
        self.assertAlmostEqual(sold, 3.0)
        self.assertAlmostEqual(gross, 0.98 * 2 + 0.97)


class DecisionShapeTests(unittest.TestCase):
    def setUp(self):
        self.bot = ccb.CleanCrossingBot()

    def test_garbage_inputs_hold(self):
        for bad in (None, [], "x", 3, {}, {"market": None}, {"market": {}, "account": {}, "books": {}}):
            self.assertEqual(self.bot.decide(bad), {"action": "HOLD"})

    def test_nan_and_strings_in_fields_hold_or_valid(self):
        obs = observation(**{"account.cashUsd": float("nan")})
        self.assertEqual(self.bot.decide(obs), {"action": "HOLD"})
        obs = observation(**{"market.secondsToClose": "soon"})
        self.assertEqual(self.bot.decide(obs), {"action": "HOLD"})
        obs = observation(**{"books.YES.asks": [["a", "b"], [None], 7]})
        assert_valid(self, self.bot.decide(obs), obs)

    def test_random_fuzz_never_raises_or_breaks_contract(self):
        rng = random.Random(7)
        base = observation()
        for _ in range(3000):
            obs = copy.deepcopy(base)
            obs["timestamp"] = NOW + rng.uniform(0, 600)
            obs["market"]["secondsToClose"] = rng.choice([rng.uniform(-5, 300), 0, 15, 16, None])
            obs["account"]["cashUsd"] = rng.choice([10, 0, 0.5, 4.99, 5.0, 25, -1])
            if rng.random() < 0.4:
                obs["account"]["position"] = position(shares=rng.choice([5.2, 0.0, 0.3, -1]),
                                                      outcome=rng.choice(["YES", "NO", "MAYBE"]))
            for side in ("YES", "NO"):
                p = round(rng.uniform(0.0, 1.0), 3)
                obs["books"][side] = {
                    "bids": [[p, rng.choice([0, 1, 100])]] if rng.random() < 0.9 else [],
                    "asks": [[min(p + 0.01, 0.999), rng.choice([0, 3, 100])]] if rng.random() < 0.9 else [],
                    "minOrderSize": rng.choice([5, 0, None]),
                }
            ref = obs["reference"]
            ref["observedAt"] = obs["timestamp"] - rng.uniform(-1, 3)
            ref["btcMidUsd"] = rng.choice([80_500.0, 0, None, float("inf")])
            ref["targetProvisional"] = rng.choice([True, False, None])
            if rng.random() < 0.1:
                obs.pop("reference")
            decision = self.bot.decide(obs)
            self.assertIsInstance(decision, dict)
            self.assertIn(decision["action"], {"HOLD", "BUY", "SELL"})
            if decision["action"] == "BUY":
                self.assertIsNone(obs["account"]["position"])
                self.assertLessEqual(decision["maxCashUsd"], min(5.0, obs["account"]["cashUsd"]) + 1e-9)
                self.assertGreater(decision["maxCashUsd"], 0)
            if decision["action"] == "SELL":
                self.assertIsNotNone(obs["account"]["position"])
        self.assertEqual(self.bot.errors, 0, "no exception path should be hit by fuzzed contract inputs")


class EntryTests(unittest.TestCase):
    def test_buys_underpriced_favourite_within_caps(self):
        bot = ccb.CleanCrossingBot()
        obs = observation()
        decision = bot.decide(obs)
        assert_valid(self, decision, obs)
        self.assertEqual(decision["action"], "BUY")
        self.assertEqual(decision["outcome"], "YES")
        # enough cash for >= minOrderSize shares even one tick higher, fees included
        unit = 0.91 * 1.01 + 0.07 * 0.91 * 0.09
        self.assertGreaterEqual(decision["maxCashUsd"] / unit, 5.0)

    def test_no_entry_inside_final_seconds(self):
        for seconds in (16.0, 15.0, 3.0, 0.0):
            bot = ccb.CleanCrossingBot()
            self.assertEqual(bot.decide(observation(**{"market.secondsToClose": seconds}))["action"], "HOLD")

    def test_no_entry_without_trustworthy_reference(self):
        cases = [
            {"reference.targetProvisional": True},
            {"reference.observedAt": NOW - 5.0},  # stale
            {"reference.observedAt": NOW + 1.0},  # future-stamped
            {"reference.targetObservedAt": NOW + 1.0},
            {"reference.openingTargetUsd": None},
            {"reference": None},
        ]
        for change in cases:
            bot = ccb.CleanCrossingBot(ccb.Config(use_estimated_target=False))
            self.assertEqual(bot.decide(observation(**change))["action"], "HOLD", change)

    def test_no_entry_when_cash_cannot_buy_min_size(self):
        bot = ccb.CleanCrossingBot()
        self.assertEqual(bot.decide(observation(**{"account.cashUsd": 4.0}))["action"], "HOLD")

    def test_small_cash_buy_never_exceeds_cash(self):
        bot = ccb.CleanCrossingBot()
        obs = observation(**{"account.cashUsd": 4.8, "books.YES.asks": [[0.80, 100]], "books.YES.bids": [[0.79, 100]],
                             "reference.btcMidUsd": 80_520.0, "market.secondsToClose": 40})
        decision = bot.decide(obs)
        assert_valid(self, decision, obs)

    def test_thin_ask_depth_not_enough_for_min_size(self):
        bot = ccb.CleanCrossingBot()
        obs = observation(**{"books.YES.asks": [[0.90, 3], [0.95, 100]]})
        decision = bot.decide(obs)
        # 3 shares at 0.90 then the next level is beyond the price tolerance
        self.assertEqual(decision["action"], "HOLD")

    def test_no_buy_when_book_disagrees_strongly(self):
        bot = ccb.CleanCrossingBot()
        obs = observation(**{"books.YES.asks": [[0.60, 100]], "books.YES.bids": [[0.59, 100]]})
        self.assertEqual(bot.decide(obs)["action"], "HOLD")

    def test_rule_cap_lower_than_five_is_respected(self):
        bot = ccb.CleanCrossingBot()
        obs = observation(**{"rules.maximumBuyCashUsd": 4.6, "books.YES.asks": [[0.85, 100]],
                             "books.YES.bids": [[0.84, 100]]})
        decision = bot.decide(obs)
        if decision["action"] == "BUY":
            self.assertLessEqual(decision["maxCashUsd"], 4.6)

    def test_buy_retry_cooldown(self):
        bot = ccb.CleanCrossingBot()
        first = bot.decide(observation())
        self.assertEqual(first["action"], "BUY")
        again = bot.decide(observation(timestamp=NOW + 0.5))
        self.assertEqual(again["action"], "HOLD")


class ExitTests(unittest.TestCase):
    def test_never_buys_with_open_position(self):
        bot = ccb.CleanCrossingBot()
        obs = observation(**{"account.position": position()})
        self.assertIn(bot.decide(obs)["action"], {"HOLD", "SELL"})

    def test_deadline_forces_sell(self):
        bot = ccb.CleanCrossingBot()
        obs = observation(**{"account.position": position(), "market.secondsToClose": 4.0,
                             "books.YES.bids": [[0.50, 100]], "books.YES.asks": []})
        self.assertEqual(bot.decide(obs)["action"], "SELL")

    def test_sell_without_asks_near_expiry(self):
        bot = ccb.CleanCrossingBot()
        obs = observation(**{"account.position": position(), "market.secondsToClose": 3.0,
                             "books.YES.bids": [[0.99, 2]], "books.YES.asks": []})
        self.assertEqual(bot.decide(obs)["action"], "SELL")

    def test_take_profit_at_099(self):
        bot = ccb.CleanCrossingBot()
        obs = observation(**{"account.position": position(), "books.YES.bids": [[0.99, 100]],
                             "books.YES.asks": [[0.995, 100]]})
        self.assertEqual(bot.decide(obs)["action"], "SELL")

    def test_stop_when_fair_collapses(self):
        bot = ccb.CleanCrossingBot()
        obs = observation(**{"account.position": position(), "reference.btcMidUsd": 80_420.0,
                             "books.YES.bids": [[0.40, 100]], "books.YES.asks": [[0.41, 100]]})
        self.assertEqual(bot.decide(obs)["action"], "SELL")

    def test_holds_winner_far_from_close(self):
        bot = ccb.CleanCrossingBot()
        obs = observation(**{"account.position": position(), "books.YES.bids": [[0.90, 100]],
                             "books.YES.asks": [[0.91, 100]]})
        self.assertEqual(bot.decide(obs)["action"], "HOLD")

    def test_position_from_previous_market_is_not_sold(self):
        bot = ccb.CleanCrossingBot()
        obs = observation(**{"account.position": position(market_id="old"), "market.secondsToClose": 2.0})
        self.assertEqual(bot.decide(obs)["action"], "HOLD")

    def test_no_bids_hold(self):
        bot = ccb.CleanCrossingBot()
        obs = observation(**{"account.position": position(), "market.secondsToClose": 2.0, "books.YES.bids": []})
        self.assertEqual(bot.decide(obs)["action"], "HOLD")

    def test_flat_never_sells(self):
        bot = ccb.CleanCrossingBot()
        for seconds in (299, 60, 10, 1):
            self.assertNotEqual(bot.decide(observation(**{"market.secondsToClose": seconds}))["action"], "SELL")


class CalibratorTests(unittest.TestCase):
    def _simulate(self, true_k: float, markets: int = 300, seed: int = 3) -> float:
        cfg = ccb.Config()
        cal = ccb.Calibrator(cfg)
        rng = random.Random(seed)
        sigma = 6.0
        for m in range(markets):
            # BTC path at 1 s resolution with volatility true_k * sigma
            price, target = 0.0, 0.0
            path = []
            for t in range(300):
                price += rng.gauss(0.0, true_k * sigma)
                path.append(price)
            final = path[-1]
            for t, value in enumerate(path):
                seconds = 300 - t - 1
                yes_mid = None
                if seconds <= 5:
                    yes_mid = 0.99 if final >= target else 0.01
                cal.observe(f"m{m}", float(seconds), yes_mid, value - target, sigma ** 2 * max(seconds, 0.25), 0.0)
        cal.observe("end", 299.0, None, None, None, 0.0)
        return cal.multiplier

    def test_recovers_multiplier_from_outcomes(self):
        self.assertTrue(1.2 <= self._simulate(1.3) <= 1.5, self._simulate(1.3))
        self.assertTrue(0.95 <= self._simulate(1.0) <= 1.1, self._simulate(1.0))

    def test_needs_minimum_markets(self):
        cfg = ccb.Config()
        cal = ccb.Calibrator(cfg)
        self.assertEqual(cal.multiplier, cfg.sigma_safety)


class LatencyTests(unittest.TestCase):
    def test_decide_is_fast(self):
        bot = ccb.CleanCrossingBot()
        obs = observation()
        started = time.perf_counter()
        for i in range(2000):
            obs["timestamp"] = NOW + i * 0.3
            obs["reference"]["observedAt"] = obs["timestamp"] - 0.2
            bot.decide(obs)
        per_call_ms = (time.perf_counter() - started) / 2000 * 1000
        self.assertLess(per_call_ms, 5.0)


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), ccb.Handler)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _post(self, body: bytes, path: str = "/decide"):
        request = urllib.request.Request(self.url + path, data=body, method="POST",
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=2) as response:
            return response.status, json.loads(response.read())

    def test_decide_roundtrip(self):
        status, payload = self._post(json.dumps(observation(**{"market.id": "http-1"})).encode())
        self.assertEqual(status, 200)
        self.assertIn(payload["action"], {"HOLD", "BUY", "SELL"})

    def test_malformed_body_returns_hold(self):
        status, payload = self._post(b"{not json")
        self.assertEqual((status, payload), (200, {"action": "HOLD"}))

    def test_health(self):
        with urllib.request.urlopen(self.url + "/health", timeout=2) as response:
            self.assertEqual(json.loads(response.read())["ok"], True)

    def test_unknown_path_404(self):
        with self.assertRaises(urllib.error.HTTPError):
            self._post(b"{}", path="/nope")


if __name__ == "__main__":
    unittest.main()
