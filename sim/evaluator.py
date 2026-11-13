"""Paper evaluator reconstructed from the public YeNo evaluator contract.

Implements, per bot: $10 start cash, $5 fee-inclusive BUY cap, one position
or pending BUY, 250 ms latency with execution against the first *later* L2
update of the order's own outcome, depth-limited partial fills, minimum order
size for BUYs, entries blocked inside the final 15 s, books older than 2 s
treated as stale (no request), dynamic taker fee 0.07*q*p*(1-p) rounded to
five decimals plus the 1% overlay on gross notional on each side, completed
cycle volume accounting, settlement of anything still held at the close
(adds cash, zero volume), and the first clean $1,000 crossing.

It is NOT the official evaluator.  Where the contract is silent, the more
conservative interpretation was chosen (documented inline).
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .market import Market, MarketGenerator, Scenario

LATENCY_S = 0.25
STALE_BOOK_S = 2.0
ENTRY_BLOCK_S = 15.0
START_CASH = 10.0
MAX_BUY = 5.0
TARGET = 1000.0
DECISION_TIMEOUT_S = 0.2
MAX_CONSECUTIVE_INVALID = 20
EPS = 1e-9


def round5(value: float) -> float:
    return round(value + 1e-12, 5)


def side_fees(fills: list[tuple[float, float]]) -> tuple[float, float]:
    gross = sum(p * q for p, q in fills)
    protocol = round5(sum(0.07 * q * p * (1.0 - p) for p, q in fills))
    overlay = round5(0.01 * gross)
    return protocol, overlay


@dataclass
class Position:
    market_id: str
    outcome: str
    shares: float
    buy_gross: float
    buy_fees: float
    sell_gross: float = 0.0
    sell_fees: float = 0.0
    opened_at: float = 0.0
    entry_vwap: float = 0.0

    def to_obs(self) -> dict[str, Any]:
        return {
            "market_id": self.market_id,
            "outcome": self.outcome,
            "shares": round(self.shares, 6),
            "buy_gross_usd": round(self.buy_gross, 6),
            "buy_fees_usd": round(self.buy_fees, 6),
            "sell_gross_usd": round(self.sell_gross, 6),
            "sell_fees_usd": round(self.sell_fees, 6),
        }


@dataclass
class Pending:
    action: str
    outcome: str
    max_cash: float
    placed_at: float


@dataclass
class Account:
    name: str
    decide: Callable[[dict[str, Any]], Any]
    cash: float = START_CASH
    volume: float = 0.0
    cycles: int = 0
    position: Position | None = None
    pending: Pending | None = None
    protocol_fees: float = 0.0
    overlay_fees: float = 0.0
    decisions: int = 0
    invalid: int = 0
    consecutive_invalid: int = 0
    rejected: int = 0
    zero_fills: int = 0
    slow_decisions: int = 0
    settled_positions: int = 0
    settled_pnl: float = 0.0
    peak_equity: float = START_CASH
    max_drawdown: float = 0.0
    qualified: bool = False
    crossing_hours: float | None = None
    cash_at_crossing: float | None = None
    stopped: str | None = None
    cycle_log: list[dict[str, Any]] = field(default_factory=list)
    max_decision_ms: float = 0.0

    @property
    def active(self) -> bool:
        return not self.qualified and self.stopped is None


class PaperEvaluator:
    def __init__(self, scenario: Scenario, seed: int, bots: dict[str, Callable[[], Callable[[dict[str, Any]], Any]]]):
        self.sc = scenario
        self.seed = seed
        self.accounts = [Account(name=name, decide=factory()) for name, factory in bots.items()]
        self.start_at: float | None = None
        self.markets_seen = 0

    # ------------------------------------------------------------------ run
    def run(self) -> dict[str, Any]:
        gen = MarketGenerator(self.sc, self.seed)
        for market in gen.markets():
            if self.start_at is None:
                self.start_at = market.open_at
            self.markets_seen += 1
            self._run_market(market)
            if not any(acc.active for acc in self.accounts):
                break
        results = {}
        for acc in self.accounts:
            if acc.stopped is None and not acc.qualified:
                acc.stopped = "window-ended"
            results[acc.name] = self._summary(acc)
        return results

    def _run_market(self, market: Market) -> None:
        books: dict[str, tuple[float, list, list] | None] = {"YES": None, "NO": None}
        refs = market.references
        ref_index = -1
        for update in market.books:
            now = update.at
            books[update.side] = (now, update.bids, update.asks)
            while ref_index + 1 < len(refs) and refs[ref_index + 1].at <= now + EPS:
                ref_index += 1
            reference = refs[ref_index].payload if ref_index >= 0 else None
            seconds = market.close_at - now
            if seconds <= 0:
                break
            for acc in self.accounts:
                if not acc.active:
                    continue
                if acc.pending is not None:
                    if update.side == acc.pending.outcome and now >= acc.pending.placed_at + LATENCY_S - EPS:
                        self._execute(acc, market, update, now)
                        if not acc.active:
                            continue
                    else:
                        continue
                yes, no = books["YES"], books["NO"]
                if yes is None or no is None:
                    continue
                if now - yes[0] > STALE_BOOK_S or now - no[0] > STALE_BOOK_S:
                    continue
                self._ask(acc, market, now, seconds, yes, no, reference)
        self._close_market(market)

    # ------------------------------------------------------------ requests
    def _observation(self, acc: Account, market: Market, now: float, seconds: float, yes, no, reference) -> dict[str, Any]:
        obs: dict[str, Any] = {
            "schemaVersion": 1,
            "timestamp": round(now, 3),
            "market": {"id": market.market_id, "secondsToClose": round(seconds, 3)},
            "account": {
                "cashUsd": round(acc.cash, 6),
                "eligibleVolumeUsd": round(acc.volume, 6),
                "completedCycles": acc.cycles,
                "position": acc.position.to_obs() if acc.position else None,
            },
            "rules": {
                "maximumBuyCashUsd": MAX_BUY,
                "targetVolumeUsd": TARGET,
                "evaluationWindowHours": 24,
                "onePositionAtATime": True,
                "buyFeesIncludedInMaximum": True,
            },
            "books": {
                "YES": {"bids": [list(l) for l in yes[1]], "asks": [list(l) for l in yes[2]], "minOrderSize": market.min_order_size},
                "NO": {"bids": [list(l) for l in no[1]], "asks": [list(l) for l in no[2]], "minOrderSize": market.min_order_size},
            },
        }
        if reference is not None:
            obs["reference"] = dict(reference)
        return obs

    def _ask(self, acc: Account, market: Market, now: float, seconds: float, yes, no, reference) -> None:
        obs = self._observation(acc, market, now, seconds, yes, no, reference)
        acc.decisions += 1
        started = time.perf_counter()
        try:
            decision = acc.decide(obs)
        except Exception:  # noqa: BLE001
            decision = None
        elapsed = time.perf_counter() - started
        acc.max_decision_ms = max(acc.max_decision_ms, elapsed * 1000.0)
        if elapsed > DECISION_TIMEOUT_S:
            acc.slow_decisions += 1
            self._invalid(acc)
            return
        self._mark(acc, yes, no)
        if not isinstance(decision, dict):
            self._invalid(acc)
            return
        action = decision.get("action")
        if action == "HOLD":
            acc.consecutive_invalid = 0
            return
        if action == "SELL":
            if acc.position is None or acc.position.market_id != market.market_id:
                self._invalid(acc)
                return
            acc.consecutive_invalid = 0
            acc.pending = Pending("SELL", acc.position.outcome, 0.0, now)
            return
        if action == "BUY":
            outcome = decision.get("outcome")
            amount = decision.get("maxCashUsd")
            if acc.position is not None or outcome not in ("YES", "NO") or isinstance(amount, bool):
                self._invalid(acc)
                return
            try:
                amount = float(amount)
            except (TypeError, ValueError):
                self._invalid(acc)
                return
            if not (0.0 < amount <= MAX_BUY + EPS) or amount > acc.cash + EPS or math.isnan(amount):
                self._invalid(acc)
                return
            acc.consecutive_invalid = 0
            if seconds <= ENTRY_BLOCK_S:
                acc.rejected += 1  # conservative: blocked entry is a rejection
                return
            acc.pending = Pending("BUY", outcome, amount, now)
            return
        self._invalid(acc)

    def _invalid(self, acc: Account) -> None:
        acc.invalid += 1
        acc.consecutive_invalid += 1
        if acc.consecutive_invalid >= MAX_CONSECUTIVE_INVALID:
            acc.stopped = "repeated-decision-errors"

    def _mark(self, acc: Account, yes, no) -> None:
        equity = acc.cash
        if acc.position is not None:
            book = yes if acc.position.outcome == "YES" else no
            bid = book[1][0][0] if book[1] else 0.0
            equity += acc.position.shares * bid
        acc.peak_equity = max(acc.peak_equity, equity)
        acc.max_drawdown = max(acc.max_drawdown, acc.peak_equity - equity)

    # ----------------------------------------------------------- execution
    def _execute(self, acc: Account, market: Market, update, now: float) -> None:
        pending = acc.pending
        acc.pending = None
        stress = self.sc.adverse_fill_usd
        if pending.action == "BUY":
            asks = [(min(p + stress, 0.999), s) for p, s in update.asks]
            if not asks:
                acc.zero_fills += 1
                return
            best = asks[0][0]
            unit_best = best * 1.01 + 0.07 * best * (1.0 - best)
            requested = pending.max_cash / unit_best
            if requested + EPS < market.min_order_size:
                acc.rejected += 1
                return
            fills: list[tuple[float, float]] = []
            budget = pending.max_cash
            spent = 0.0
            remaining = requested
            for price, size in asks:
                unit = price * 1.01 + 0.07 * price * (1.0 - price)
                quantity = min(size, remaining, (budget - spent) / unit)
                quantity = math.floor(quantity * 10000.0) / 10000.0
                if quantity <= 0:
                    break
                fills.append((price, quantity))
                spent += quantity * unit
                remaining -= quantity
                if remaining <= EPS or spent >= budget - 1e-6:
                    break
            if not fills:
                acc.zero_fills += 1
                return
            protocol, overlay = side_fees(fills)
            gross = sum(p * q for p, q in fills)
            while gross + protocol + overlay > pending.max_cash + EPS and fills:
                # rounding pushed the order over the ceiling: trim the last level
                p, q = fills[-1]
                q = math.floor((q - 0.01) * 10000.0) / 10000.0
                fills[-1] = (p, q)
                if q <= 0:
                    fills.pop()
                protocol, overlay = side_fees(fills)
                gross = sum(p * q for p, q in fills)
            shares = sum(q for _, q in fills)
            if shares <= 0 or gross + protocol + overlay > acc.cash + EPS:
                acc.zero_fills += 1
                return
            acc.cash -= gross + protocol + overlay
            acc.protocol_fees += protocol
            acc.overlay_fees += overlay
            acc.position = Position(
                market_id=market.market_id,
                outcome=pending.outcome,
                shares=shares,
                buy_gross=gross,
                buy_fees=protocol + overlay,
                opened_at=now,
                entry_vwap=gross / shares,
            )
            return

        # SELL
        pos = acc.position
        if pos is None:
            return
        bids = [(max(p - stress, 0.001), s) for p, s in update.bids]
        if not bids:
            acc.zero_fills += 1
            return
        remaining = pos.shares
        fills = []
        for price, size in bids:
            quantity = min(size, remaining)
            if quantity <= 0:
                break
            fills.append((price, quantity))
            remaining -= quantity
            if remaining <= EPS:
                break
        protocol, overlay = side_fees(fills)
        gross = sum(p * q for p, q in fills)
        sold = sum(q for _, q in fills)
        acc.cash += gross - protocol - overlay
        acc.protocol_fees += protocol
        acc.overlay_fees += overlay
        pos.shares -= sold
        pos.sell_gross += gross
        pos.sell_fees += protocol + overlay
        if pos.shares <= 1e-6:
            self._complete_cycle(acc, pos, now)

    def _complete_cycle(self, acc: Account, pos: Position, now: float) -> None:
        acc.position = None
        acc.cycles += 1
        acc.volume += pos.buy_gross + pos.sell_gross
        pnl = pos.sell_gross - pos.sell_fees - pos.buy_gross - pos.buy_fees
        acc.cycle_log.append(
            {
                "at_h": round((now - self.start_at) / 3600.0, 4),
                "market": pos.market_id,
                "outcome": pos.outcome,
                "entry": round(pos.entry_vwap, 4),
                "exit": round(pos.sell_gross / max(pos.buy_gross / pos.entry_vwap, EPS), 4),
                "pnl": round(pnl, 5),
                "hold_s": round(now - pos.opened_at, 2),
            }
        )
        if acc.volume >= TARGET - EPS:
            acc.qualified = True
            acc.crossing_hours = (now - self.start_at) / 3600.0
            acc.cash_at_crossing = acc.cash

    def _close_market(self, market: Market) -> None:
        for acc in self.accounts:
            if not acc.active:
                continue
            acc.pending = None  # anything not executed by the close is cancelled
            pos = acc.position
            if pos is not None and pos.market_id == market.market_id:
                won = (pos.outcome == "YES") == market.yes_wins
                payout = pos.shares * (1.0 if won else 0.0)
                acc.cash += payout
                acc.settled_positions += 1
                acc.settled_pnl += payout + pos.sell_gross - pos.sell_fees - pos.buy_gross - pos.buy_fees
                acc.position = None  # settled: flat again, the cycle adds zero volume

    # ------------------------------------------------------------- results
    def _summary(self, acc: Account) -> dict[str, Any]:
        pnl = [c["pnl"] for c in acc.cycle_log]
        return {
            "qualified": acc.qualified,
            "cash_at_crossing": None if acc.cash_at_crossing is None else round(acc.cash_at_crossing, 4),
            "crossing_hours": None if acc.crossing_hours is None else round(acc.crossing_hours, 3),
            "final_cash": round(acc.cash, 4),
            "volume": round(acc.volume, 2),
            "cycles": acc.cycles,
            "fees": round(acc.protocol_fees + acc.overlay_fees, 4),
            "protocol_fees": round(acc.protocol_fees, 4),
            "overlay_fees": round(acc.overlay_fees, 4),
            "decisions": acc.decisions,
            "invalid": acc.invalid,
            "rejected": acc.rejected,
            "zero_fills": acc.zero_fills,
            "slow_decisions": acc.slow_decisions,
            "max_decision_ms": round(acc.max_decision_ms, 3),
            "settled_positions": acc.settled_positions,
            "settled_pnl": round(acc.settled_pnl, 4),
            "flat_at_stop": acc.position is None and acc.pending is None,
            "max_drawdown": round(acc.max_drawdown, 4),
            "stopped": acc.stopped or ("qualified" if acc.qualified else None),
            "mean_cycle_pnl": round(sum(pnl) / len(pnl), 5) if pnl else None,
            "win_rate": round(sum(1 for x in pnl if x > 0) / len(pnl), 4) if pnl else None,
            "mean_entry": round(sum(c["entry"] for c in acc.cycle_log) / len(pnl), 4) if pnl else None,
            "markets": self.markets_seen,
        }
