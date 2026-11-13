"""Tests for the local paper harness accounting (not the official evaluator)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sim.evaluator import PaperEvaluator, side_fees  # noqa: E402
from sim.market import BookUpdate, Market, MarketGenerator, ReferenceUpdate, Scenario  # noqa: E402


def make_market(updates, yes_wins=True, market_id="t-1", open_at=1000.0):
    return Market(
        market_id=market_id,
        open_at=open_at,
        close_at=open_at + 300.0,
        target=100.0,
        settle_price=101.0 if yes_wins else 99.0,
        yes_wins=yes_wins,
        books=updates,
        references=[ReferenceUpdate(open_at, {"btcMidUsd": 100.0, "observedAt": open_at})],
        has_target=True,
    )


def ladder(t, yes_bid, yes_ask, size=100.0):
    return [
        BookUpdate(t, "YES", [[yes_bid, size]], [[yes_ask, size]]),
        BookUpdate(t + 0.01, "NO", [[round(1 - yes_ask, 3), size]], [[round(1 - yes_bid, 3), size]]),
    ]


class ScriptedBot:
    def __init__(self, script):
        self.script = script  # list of (min_timestamp, decision)
        self.calls = 0

    def __call__(self, obs):
        self.calls += 1
        for i, (at, decision) in enumerate(self.script):
            if obs["timestamp"] >= at:
                self.script.pop(i)
                return decision
        return {"action": "HOLD"}


def evaluator_with(bot):
    ev = PaperEvaluator(Scenario(), seed=0, bots={"b": lambda: bot})
    ev.start_at = 1000.0
    return ev


class HarnessTests(unittest.TestCase):
    def test_full_cycle_volume_and_fees(self):
        updates = ladder(1100.0, 0.89, 0.90) + ladder(1100.5, 0.89, 0.90) + ladder(1150.0, 0.94, 0.95) + ladder(1150.5, 0.94, 0.95)
        bot = ScriptedBot([(1100.0, {"action": "BUY", "outcome": "YES", "maxCashUsd": 5}), (1150.0, {"action": "SELL"})])
        ev = evaluator_with(bot)
        ev._run_market(make_market(updates))
        acc = ev.accounts[0]
        self.assertEqual(acc.cycles, 1)
        self.assertIsNone(acc.position)
        log = acc.cycle_log[0]
        shares = round(acc.volume - 0, 6)  # placeholder to keep names explicit
        buy_unit = 0.90 * 1.01 + 0.07 * 0.90 * 0.10
        expected_shares = int(5 / buy_unit * 10000) / 10000
        buy_gross = 0.90 * expected_shares
        sell_gross = 0.94 * expected_shares
        self.assertAlmostEqual(acc.volume, buy_gross + sell_gross, places=4)
        fees = sum(side_fees([(0.90, expected_shares)])) + sum(side_fees([(0.94, expected_shares)]))
        self.assertAlmostEqual(acc.cash, 10 - buy_gross + sell_gross - fees, places=4)
        self.assertAlmostEqual(log["pnl"], sell_gross - buy_gross - fees, places=4)
        self.assertGreater(shares, 0)

    def test_buy_below_min_size_is_rejected(self):
        updates = ladder(1100.0, 0.89, 0.90) + ladder(1100.5, 0.89, 0.90)
        bot = ScriptedBot([(1100.0, {"action": "BUY", "outcome": "YES", "maxCashUsd": 1.0})])
        ev = evaluator_with(bot)
        ev._run_market(make_market(updates))
        acc = ev.accounts[0]
        self.assertEqual(acc.rejected, 1)
        self.assertIsNone(acc.position)
        self.assertEqual(acc.cash, 10.0)

    def test_invalid_actions_counted(self):
        updates = ladder(1100.0, 0.49, 0.50) + ladder(1101.0, 0.49, 0.50)
        bot = ScriptedBot([(1100.0, {"action": "SELL"}), (1100.0, {"action": "BUY", "outcome": "YES", "maxCashUsd": 6})])
        ev = evaluator_with(bot)
        ev._run_market(make_market(updates))
        self.assertEqual(ev.accounts[0].invalid, 2)

    def test_execution_uses_later_update_of_own_outcome(self):
        # decision at 1100.0 sees ask 0.50; the next YES update (1100.3) has ask 0.60
        updates = ladder(1100.0, 0.49, 0.50) + [BookUpdate(1100.1, "NO", [[0.5, 100]], [[0.51, 100]]),
                                                BookUpdate(1100.3, "YES", [[0.59, 100]], [[0.60, 100]])]
        bot = ScriptedBot([(1100.0, {"action": "BUY", "outcome": "YES", "maxCashUsd": 5})])
        ev = evaluator_with(bot)
        ev._run_market(make_market(updates))
        self.assertAlmostEqual(ev.accounts[0].position.entry_vwap if ev.accounts[0].position else 0.6, 0.60)

    def test_entries_blocked_in_final_15_seconds(self):
        updates = ladder(1290.0, 0.89, 0.90) + ladder(1290.5, 0.89, 0.90)
        bot = ScriptedBot([(1290.0, {"action": "BUY", "outcome": "YES", "maxCashUsd": 5})])
        ev = evaluator_with(bot)
        ev._run_market(make_market(updates))
        self.assertEqual(ev.accounts[0].rejected, 1)
        self.assertEqual(ev.accounts[0].cash, 10.0)

    def test_open_position_settles_with_zero_volume(self):
        updates = ladder(1100.0, 0.89, 0.90) + ladder(1100.5, 0.89, 0.90)
        bot = ScriptedBot([(1100.0, {"action": "BUY", "outcome": "YES", "maxCashUsd": 5})])
        ev = evaluator_with(bot)
        ev._run_market(make_market(updates, yes_wins=False))
        acc = ev.accounts[0]
        self.assertIsNone(acc.position)
        self.assertEqual(acc.volume, 0.0)
        self.assertEqual(acc.settled_positions, 1)
        self.assertLess(acc.cash, 5.1)

    def test_stale_books_are_not_requested(self):
        updates = [BookUpdate(1100.0, "YES", [[0.49, 100]], [[0.5, 100]]), BookUpdate(1103.0, "NO", [[0.49, 100]], [[0.5, 100]])]
        bot = ScriptedBot([])
        ev = evaluator_with(bot)
        ev._run_market(make_market(updates))
        self.assertEqual(bot.calls, 0)

    def test_crossing_stops_account(self):
        updates = ladder(1100.0, 0.89, 0.90) + ladder(1100.5, 0.89, 0.90) + ladder(1150.0, 0.94, 0.95) + ladder(1150.5, 0.94, 0.95)
        bot = ScriptedBot([(1100.0, {"action": "BUY", "outcome": "YES", "maxCashUsd": 5}), (1150.0, {"action": "SELL"})])
        ev = evaluator_with(bot)
        ev.accounts[0].volume = 995.0
        ev._run_market(make_market(updates))
        acc = ev.accounts[0]
        self.assertTrue(acc.qualified)
        self.assertAlmostEqual(acc.cash_at_crossing, acc.cash)

    def test_generator_is_deterministic(self):
        sc = Scenario(hours=0.25)
        a = [m.books[5].asks for m in MarketGenerator(sc, 11).markets()]
        b = [m.books[5].asks for m in MarketGenerator(sc, 11).markets()]
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
