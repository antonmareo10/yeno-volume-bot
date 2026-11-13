#!/usr/bin/env python3
"""Clean Crossing: a YeNo x Builderr volume bot (paper trading, stdlib only).

Strategy in one paragraph
-------------------------
Every completed BUY+SELL cycle pays the 1% YeNo overlay on both sides, so
$1,000 of eligible volume costs at least $10 in overlay alone - all of the
starting cash.  A bot therefore only qualifies if its trades carry a real,
positive edge.  This bot looks for one specific, causal edge: late in each
five-minute BTC Up/Down market it prices the favourite from the public BTC
reference (gap to the opening target, realised volatility, time left) and
buys it only when the executable ask, after both fee layers and expected exit
costs, is cheaper than that fair value.  High-priced favourites also have the
lowest dynamic protocol fee per dollar of volume (0.07 x (1 - p)).  It sells
when the book catches up with fair value, when the favourite reaches ~0.99,
when the reference turns against it (stop), and unconditionally a few seconds
before the close, so it is flat at every market boundary.  If it falls behind
the volume schedule it gradually accepts thinner edges so it still crosses
$1,000 inside the 24-hour window.

Every decision is validated against the observation (position, cash, $5
fee-inclusive cap, minimum order size, 15-second entry block, book depth) and
any unexpected input or exception degrades to HOLD.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import threading
from dataclasses import dataclass, field, asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

HOLD: dict[str, Any] = {"action": "HOLD"}
SELL: dict[str, Any] = {"action": "SELL"}

PROTOCOL_FEE_RATE = 0.07  # Polymarket crypto taker fee rate (contract)
OVERLAY_FEE_RATE = 0.01   # YeNo overlay on gross notional, each side (contract)
CONTRACT_MAX_BUY_USD = 5.0
ENTRY_BLOCK_SECONDS = 15.0
TARGET_VOLUME_USD = 1000.0
EPS = 1e-9


@dataclass
class Config:
    """Strategy parameters.  Defaults are the submitted configuration."""

    # --- entry window -----------------------------------------------------
    entry_max_seconds: float = 180.0      # only enter in the last N seconds
    entry_min_seconds: float = 17.0       # contract blocks entries < 15 s
    max_cycles_per_market: int = 4
    reentry_cooldown_seconds: float = 1.0
    buy_retry_cooldown_seconds: float = 1.5

    # --- fair value model -------------------------------------------------
    max_reference_age_seconds: float = 1.5
    default_sigma_usd: float = 8.0        # $/sqrt(s) before enough data
    min_sigma_usd: float = 2.5
    max_sigma_usd: float = 40.0
    sigma_safety: float = 1.15            # sigma multiplier prior (fat tails)
    basis_sd_usd: float = 4.0             # reference vs settlement source
    vol_fast_half_life_s: float = 120.0
    vol_slow_half_life_s: float = 1800.0
    vol_min_samples: int = 60

    # --- online calibration of the sigma multiplier from market outcomes ---
    calibrate: bool = True
    calib_min_markets: int = 24
    calib_window_markets: int = 400
    calib_prior_sd: float = 0.15          # log-space prior around sigma_safety
    calib_min_mult: float = 0.95
    calib_max_mult: float = 2.0
    calib_min_seconds: float = 15.0
    calib_max_seconds: float = 180.0
    calib_step_seconds: float = 5.0
    calib_label_max_seconds: float = 8.0

    # --- entry filters ----------------------------------------------------
    min_fair: float = 0.90
    min_ask: float = 0.80
    max_ask: float = 0.975
    max_book_disagreement: float = 0.15   # |fair - book mid| sanity bound
    price_tolerance: float = 0.01         # consume asks up to best + this
    exit_slippage: float = 0.01           # expected bid discount at exit
    min_edge_usd: float = 0.05            # required EV per cycle (dollars)
    min_shares_buffer: float = 0.03       # keep >= minOrderSize*(1+buffer)
    cash_reserve_usd: float = 0.0

    # --- volume pacing ----------------------------------------------------
    pace_goal_hours: float = 18.0         # aim to cross well before 24 h
    pace_warmup_hours: float = 1.0
    pace_relax_usd: float = 0.10          # max edge relaxation when behind
    pace_tighten_usd: float = 0.0         # extra edge required when ahead
    min_edge_floor_usd: float = -0.05
    pace_full_relax_behind: float = 0.25  # behind by 25% of target -> fully relaxed
    relaxed_min_fair: float = 0.85        # entry filters used when fully behind
    relaxed_min_ask: float = 0.75
    relaxed_entry_max_seconds: float = 240.0
    # extra relaxation only when behind AND sitting on a cash cushion: spending a
    # little of a large cushion is cheap insurance against missing the target
    cushion_cash_usd: float = 14.0        # cushion starts above this cash...
    cushion_full_usd: float = 6.0         # ...and is full this many dollars above it
    cushion_relax_usd: float = 0.10
    cushion_min_fair: float = 0.80
    cushion_min_ask: float = 0.70

    # --- exit -------------------------------------------------------------
    exit_deadline_seconds: float = 5.0    # unconditional flatten
    take_profit_price: float = 0.99
    caught_up_exit: bool = True           # sell when the book reaches fair...
    take_profit_tolerance: float = 0.005  # ...i.e. bid >= fair - tol...
    caught_up_min_pnl_usd: float = 0.0    # ...and the whole cycle is not a loss
    stop_fair: float = 0.70               # sell if fair of held side drops
    stop_drop_no_reference: float = 0.08  # price stop when reference missing

    # --- fallback target (when the official opening target is missing) ----
    use_estimated_target: bool = True
    estimated_target_max_gap_s: float = 1.5
    estimated_target_min_samples: int = 5
    estimated_target_max_basis_usd: float = 15.0

    def updated(self, **overrides: Any) -> "Config":
        values = asdict(self)
        for key, value in overrides.items():
            if key not in values:
                raise KeyError(f"unknown config key: {key}")
            values[key] = value
        return Config(**values)


# ---------------------------------------------------------------------------
# small numeric helpers
# ---------------------------------------------------------------------------

def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def norm_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def round5(value: float) -> float:
    return round(value + 1e-12, 5)


def side_fees(fills: list[tuple[float, float]]) -> float:
    """Contract fees for one side: dynamic protocol fee + 1% overlay."""
    gross = sum(price * shares for price, shares in fills)
    protocol = sum(PROTOCOL_FEE_RATE * shares * price * (1.0 - price) for price, shares in fills)
    return round5(protocol) + round5(gross * OVERLAY_FEE_RATE)


def parse_levels(rows: Any, *, descending: bool) -> list[tuple[float, float]]:
    levels: list[tuple[float, float]] = []
    if not isinstance(rows, list):
        return levels
    for row in rows:
        try:
            price = _finite(row[0])
            size = _finite(row[1])
        except (IndexError, KeyError, TypeError):
            continue
        if price is None or size is None:
            continue
        if 0.0 < price < 1.0 and size > 0.0:
            levels.append((price, size))
    levels.sort(key=lambda level: level[0], reverse=descending)
    return levels


def plan_buy(asks: list[tuple[float, float]], budget: float, price_cap: float) -> tuple[float, float, float]:
    """Shares, gross and fees for spending at most ``budget`` on asks <= cap."""
    fills: list[tuple[float, float]] = []
    spent = 0.0
    for price, size in asks:
        if price > price_cap + EPS:
            break
        unit = price * (1.0 + OVERLAY_FEE_RATE) + PROTOCOL_FEE_RATE * price * (1.0 - price)
        affordable = (budget - spent) / unit
        quantity = min(size, affordable)
        if quantity <= EPS:
            break
        fills.append((price, quantity))
        spent += quantity * unit
        if quantity < size:
            break
    shares = sum(quantity for _, quantity in fills)
    gross = sum(price * quantity for price, quantity in fills)
    return shares, gross, side_fees(fills) if fills else 0.0


def plan_sell(bids: list[tuple[float, float]], shares: float) -> tuple[float, float, float]:
    """Sold shares, gross and fees for selling ``shares`` into bids."""
    remaining = shares
    fills: list[tuple[float, float]] = []
    for price, size in bids:
        quantity = min(size, remaining)
        if quantity <= EPS:
            break
        fills.append((price, quantity))
        remaining -= quantity
        if remaining <= EPS:
            break
    sold = sum(quantity for _, quantity in fills)
    gross = sum(price * quantity for price, quantity in fills)
    return sold, gross, side_fees(fills) if fills else 0.0


# ---------------------------------------------------------------------------
# online state
# ---------------------------------------------------------------------------

class VolEstimator:
    """EWMA realised variance of the BTC reference in $^2 per second."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        prior = cfg.default_sigma_usd ** 2
        self.fast_var = prior
        self.slow_var = prior
        self.samples = 0
        self.last_at: float | None = None
        self.last_mid: float | None = None

    def update(self, observed_at: float, mid: float) -> None:
        if self.last_at is not None and observed_at <= self.last_at + EPS:
            return
        if self.last_at is not None and self.last_mid is not None:
            dt = observed_at - self.last_at
            if 0.2 <= dt <= 10.0:
                r2 = (mid - self.last_mid) ** 2 / dt
                cap = 25.0 * max(self.slow_var, self.cfg.min_sigma_usd ** 2)
                r2 = min(r2, cap)
                self.fast_var = self._blend(self.fast_var, r2, dt, self.cfg.vol_fast_half_life_s)
                self.slow_var = self._blend(self.slow_var, r2, dt, self.cfg.vol_slow_half_life_s)
                self.samples += 1
        self.last_at = observed_at
        self.last_mid = mid

    @staticmethod
    def _blend(current: float, sample: float, dt: float, half_life: float) -> float:
        alpha = 1.0 - math.exp(-math.log(2.0) * dt / half_life)
        return (1.0 - alpha) * current + alpha * sample

    def sigma(self) -> float:
        sigma = math.sqrt(max(self.fast_var, self.slow_var))
        if self.samples < self.cfg.vol_min_samples:
            sigma = max(sigma, self.cfg.default_sigma_usd)
        return min(max(sigma, self.cfg.min_sigma_usd), self.cfg.max_sigma_usd)


SIGMA_GRID = (0.85, 0.9, 0.95, 1.0, 1.05, 1.1, 1.15, 1.2, 1.3, 1.4, 1.5, 1.65, 1.8, 2.0)


class Calibrator:
    """Learns the sigma multiplier from the realised outcomes of finished markets.

    During a market it keeps one snapshot (gap, sigma^2 * horizon, basis^2) per
    ``calib_step_seconds`` bucket of time-to-close.  When the market id changes
    it labels that market from what was already observed at the end of it (the
    YES book within the last seconds, or a decisive reference gap) and adds the
    market's mean log-likelihood for every multiplier on the grid.  The fitted
    multiplier is the penalised maximum-likelihood point (log-normal prior
    centred on ``sigma_safety``).  Strictly causal: a market only informs
    decisions in later markets.
    """

    def __init__(self, cfg: "Config") -> None:
        self.cfg = cfg
        self.history: list[list[float]] = []
        self.totals = [0.0] * len(SIGMA_GRID)
        self.current: str | None = None
        self.snapshots: list[tuple[float, float, float]] = []
        self.last_bucket: int | None = None
        self.last_seen: tuple[float, float | None, float | None, float | None, float] | None = None
        self.multiplier = cfg.sigma_safety
        self.labelled = 0

    def observe(self, market_id: str, seconds: float, yes_mid: float | None,
                gap: float | None, var_h: float | None, basis2: float) -> None:
        cfg = self.cfg
        if market_id != self.current:
            self._finish()
            self.current = market_id
            self.snapshots = []
            self.last_bucket = None
            self.last_seen = None
        if gap is not None and var_h is not None and cfg.calib_min_seconds <= seconds <= cfg.calib_max_seconds:
            bucket = int(seconds // cfg.calib_step_seconds)
            if bucket != self.last_bucket:
                self.last_bucket = bucket
                self.snapshots.append((gap, var_h, basis2))
        self.last_seen = (seconds, yes_mid, gap, var_h, basis2)

    def _finish(self) -> None:
        cfg = self.cfg
        if not self.snapshots or self.last_seen is None:
            return
        seconds, yes_mid, gap, var_h, basis2 = self.last_seen
        label: int | None = None
        if seconds <= cfg.calib_label_max_seconds and yes_mid is not None:
            if yes_mid >= 0.9:
                label = 1
            elif yes_mid <= 0.1:
                label = 0
        if label is None and gap is not None and var_h is not None and seconds <= ENTRY_BLOCK_SECONDS:
            z = gap / math.sqrt(var_h + basis2)
            if abs(z) >= 3.0:
                label = 1 if z > 0 else 0
        if label is None:
            return
        contribution = []
        count = len(self.snapshots)
        for k in SIGMA_GRID:
            total = 0.0
            for g, v, b2 in self.snapshots:
                z = g / math.sqrt(k * k * v + b2)
                total += math.log(max(norm_cdf(z if label else -z), 1e-6))
            contribution.append(total / count)
        self.history.append(contribution)
        self.totals = [a + b for a, b in zip(self.totals, contribution)]
        if len(self.history) > cfg.calib_window_markets:
            old = self.history.pop(0)
            self.totals = [a - b for a, b in zip(self.totals, old)]
        self.labelled += 1
        self._refit()

    def _refit(self) -> None:
        cfg = self.cfg
        if len(self.history) < cfg.calib_min_markets:
            self.multiplier = cfg.sigma_safety
            return
        centre = math.log(cfg.sigma_safety)
        best_k, best_score = cfg.sigma_safety, -math.inf
        for k, total in zip(SIGMA_GRID, self.totals):
            score = total - (math.log(k) - centre) ** 2 / (2.0 * cfg.calib_prior_sd ** 2)
            if score > best_score:
                best_k, best_score = k, score
        self.multiplier = min(max(best_k, cfg.calib_min_mult), cfg.calib_max_mult)


@dataclass
class MarketState:
    market_id: str
    close_at: float
    cycles: int = 0
    position_was_open: bool = False
    last_flat_at: float = -math.inf
    last_buy_attempt_at: float = -math.inf
    entry_price: float | None = None
    official_target: float | None = None
    estimated_target: float | None = None
    estimate_done: bool = False
    basis_recorded: bool = False


@dataclass
class Reference:
    mid: float
    observed_at: float
    target: float | None
    target_is_estimate: bool


class CleanCrossingBot:
    """Stateful decision engine.  ``decide`` never raises."""

    def __init__(self, config: Config | None = None) -> None:
        self.cfg = config or Config()
        self.lock = threading.Lock()
        self.vol = VolEstimator(self.cfg)
        self.market: MarketState | None = None
        self.first_timestamp: float | None = None
        self.last_timestamp: float | None = None
        # recent reference mids (observed_at, mid) for target estimation
        self.mid_history: list[tuple[float, float]] = []
        # official target minus our own estimate, for markets that had both
        self.basis_samples: list[float] = []
        self.errors = 0
        self.last_reason = ""
        self.calibrator = Calibrator(self.cfg)

    # -- public ---------------------------------------------------------------
    def decide(self, observation: Any) -> dict[str, Any]:
        with self.lock:
            try:
                decision = self._decide(observation)
            except Exception:  # noqa: BLE001 - any surprise must degrade to HOLD
                self.errors += 1
                return dict(HOLD)
            return decision if _valid_shape(decision) else dict(HOLD)

    # -- internals ------------------------------------------------------------
    def _decide(self, obs: Any) -> dict[str, Any]:
        if not isinstance(obs, dict):
            return dict(HOLD)
        market = obs.get("market")
        account = obs.get("account")
        books = obs.get("books")
        if not isinstance(market, dict) or not isinstance(account, dict) or not isinstance(books, dict):
            return dict(HOLD)
        now = _finite(obs.get("timestamp"))
        seconds = _finite(market.get("secondsToClose"))
        market_id = market.get("id")
        cash = _finite(account.get("cashUsd"))
        if now is None or seconds is None or market_id is None or cash is None:
            return dict(HOLD)
        market_id = str(market_id)

        self._track_time(now)
        state = self._market_state(market_id, now, seconds)
        reference = self._reference(obs.get("reference"), now, state)

        if self.cfg.calibrate:
            self._feed_calibrator(market_id, seconds, books, reference)

        position = account.get("position")
        if position is not None and not isinstance(position, dict):
            return dict(HOLD)

        # bookkeeping for cycles / cooldowns
        if position is None and state.position_was_open:
            state.cycles += 1
            state.last_flat_at = now
            state.entry_price = None
        state.position_was_open = position is not None

        if position is not None:
            return self._exit_decision(position, books, seconds, reference, state, market_id)
        return self._entry_decision(obs, account, books, seconds, reference, state, now, cash)

    def _feed_calibrator(self, market_id: str, seconds: float, books: dict[str, Any], ref: Reference | None) -> None:
        yes_mid = None
        book = books.get("YES")
        if isinstance(book, dict):
            bids = parse_levels(book.get("bids"), descending=True)
            asks = parse_levels(book.get("asks"), descending=False)
            if bids and asks:
                yes_mid = 0.5 * (bids[0][0] + asks[0][0])
            elif bids:
                yes_mid = bids[0][0]
            elif asks:
                yes_mid = asks[0][0]
        gap = var_h = None
        basis2 = self.cfg.basis_sd_usd ** 2
        if ref is not None and ref.target is not None and not ref.target_is_estimate:
            gap = ref.mid - ref.target
            var_h = self.vol.sigma() ** 2 * max(seconds, 0.25)
        self.calibrator.observe(market_id, seconds, yes_mid, gap, var_h, basis2)

    def _track_time(self, now: float) -> None:
        if self.first_timestamp is None:
            self.first_timestamp = now
        self.last_timestamp = now

    def _market_state(self, market_id: str, now: float, seconds: float) -> MarketState:
        if self.market is None or self.market.market_id != market_id:
            self.market = MarketState(market_id=market_id, close_at=now + seconds)
        return self.market

    def _reference(self, raw: Any, now: float, state: MarketState) -> Reference | None:
        cfg = self.cfg
        if not isinstance(raw, dict):
            return None
        mid = _finite(raw.get("btcMidUsd"))
        observed_at = _finite(raw.get("observedAt"))
        if mid is None or observed_at is None or mid <= 0.0:
            return None
        if observed_at > now + EPS:  # never use a future-stamped value
            return None
        self.vol.update(observed_at, mid)
        self._remember_mid(observed_at, mid)
        if now - observed_at > cfg.max_reference_age_seconds + EPS:
            return None

        open_at = state.close_at - 300.0
        if state.estimated_target is None and cfg.use_estimated_target and not state.estimate_done:
            state.estimated_target = self._estimate_target(open_at)
            # one closest-to-open lookup is enough once the open is well behind us
            state.estimate_done = state.estimated_target is not None or now > open_at + 5.0

        target = _finite(raw.get("openingTargetUsd"))
        target_at = _finite(raw.get("targetObservedAt"))
        provisional = raw.get("targetProvisional", True)
        official_ok = (
            target is not None
            and target > 0.0
            and target_at is not None
            and target_at <= now + EPS
            and provisional is False
            and abs(target - mid) < 0.05 * mid
        )
        if official_ok:
            state.official_target = target
            if state.estimated_target is not None and not state.basis_recorded:
                self.basis_samples.append(target - state.estimated_target)
                self.basis_samples = self.basis_samples[-200:]
                state.basis_recorded = True
            return Reference(mid, observed_at, target, False)

        if cfg.use_estimated_target and state.estimated_target is not None and self._basis_trusted():
            return Reference(mid, observed_at, state.estimated_target, True)
        return Reference(mid, observed_at, None, False)

    def _remember_mid(self, observed_at: float, mid: float) -> None:
        history = self.mid_history
        if history and observed_at <= history[-1][0] + EPS:
            return
        history.append((observed_at, mid))
        if len(history) > 1200:
            del history[:400]

    def _estimate_target(self, open_at: float) -> float | None:
        best: tuple[float, float] | None = None
        for observed_at, mid in reversed(self.mid_history):
            gap = abs(observed_at - open_at)
            if best is None or gap < best[0]:
                best = (gap, mid)
            if observed_at < open_at - 5.0:
                break
        if best is None or best[0] > self.cfg.estimated_target_max_gap_s:
            return None
        return best[1]

    def _basis_trusted(self) -> bool:
        samples = self.basis_samples
        if len(samples) < self.cfg.estimated_target_min_samples:
            return False
        mean = sum(samples) / len(samples)
        var = sum((x - mean) ** 2 for x in samples) / max(len(samples) - 1, 1)
        return abs(mean) + 2.0 * math.sqrt(var) <= self.cfg.estimated_target_max_basis_usd

    def _basis_sd(self, ref: Reference) -> float:
        base = self.cfg.basis_sd_usd
        if not ref.target_is_estimate or not self.basis_samples:
            return base
        samples = self.basis_samples
        mean = sum(samples) / len(samples)
        var = sum((x - mean) ** 2 for x in samples) / max(len(samples) - 1, 1)
        return math.sqrt(base ** 2 + mean ** 2 + var)

    def _fair_yes(self, ref: Reference | None, seconds: float) -> float | None:
        if ref is None or ref.target is None:
            return None
        horizon = max(seconds, 0.25)
        multiplier = self.calibrator.multiplier if self.cfg.calibrate else self.cfg.sigma_safety
        sigma = self.vol.sigma() * multiplier
        sd = math.sqrt(sigma * sigma * horizon + self._basis_sd(ref) ** 2)
        return norm_cdf((ref.mid - ref.target) / sd)

    # -- exit -----------------------------------------------------------------
    def _exit_decision(
        self,
        position: dict[str, Any],
        books: dict[str, Any],
        seconds: float,
        ref: Reference | None,
        state: MarketState,
        market_id: str,
    ) -> dict[str, Any]:
        cfg = self.cfg
        held_market = position.get("market_id", position.get("marketId"))
        if held_market is not None and str(held_market) != market_id:
            return dict(HOLD)  # position from a closed market: wait for settlement
        side = position.get("outcome")
        shares = _finite(position.get("shares"))
        if side not in ("YES", "NO") or shares is None or shares <= EPS:
            return dict(HOLD)
        book = books.get(side)
        if not isinstance(book, dict):
            return dict(HOLD)
        bids = parse_levels(book.get("bids"), descending=True)
        if not bids:
            return dict(HOLD)  # nothing to sell into yet
        best_bid = bids[0][0]

        if seconds <= cfg.exit_deadline_seconds + EPS:
            self.last_reason = "deadline"
            return dict(SELL)

        fair_yes = self._fair_yes(ref, seconds)
        if fair_yes is None:
            entry = state.entry_price
            if best_bid >= cfg.take_profit_price - EPS:
                self.last_reason = "take-profit-price"
                return dict(SELL)
            if entry is not None and best_bid <= entry - cfg.stop_drop_no_reference + EPS:
                self.last_reason = "stop-no-reference"
                return dict(SELL)
            return dict(HOLD)

        fair = fair_yes if side == "YES" else 1.0 - fair_yes
        if fair < cfg.stop_fair - EPS:
            self.last_reason = "stop-fair"
            return dict(SELL)
        if best_bid >= cfg.take_profit_price - EPS:
            self.last_reason = "take-profit-price"
            return dict(SELL)
        if cfg.caught_up_exit and best_bid >= fair - cfg.take_profit_tolerance - EPS:
            if self._cycle_pnl_if_sold(position, bids, shares) >= cfg.caught_up_min_pnl_usd - EPS:
                self.last_reason = "caught-up"
                return dict(SELL)
        return dict(HOLD)

    @staticmethod
    def _cycle_pnl_if_sold(position: dict[str, Any], bids: list[tuple[float, float]], shares: float) -> float:
        _, gross, fees = plan_sell(bids, shares)
        paid = (_finite(position.get("buy_gross_usd")) or 0.0) + (_finite(position.get("buy_fees_usd")) or 0.0)
        received = (_finite(position.get("sell_gross_usd")) or 0.0) - (_finite(position.get("sell_fees_usd")) or 0.0)
        return received + gross - fees - paid

    # -- entry ----------------------------------------------------------------
    def _entry_decision(
        self,
        obs: dict[str, Any],
        account: dict[str, Any],
        books: dict[str, Any],
        seconds: float,
        ref: Reference | None,
        state: MarketState,
        now: float,
        cash: float,
    ) -> dict[str, Any]:
        cfg = self.cfg
        behind, ahead = self._pace(account, now)
        cushion = behind * min(max((cash - cfg.cushion_cash_usd) / cfg.cushion_full_usd, 0.0), 1.0)
        entry_max = cfg.entry_max_seconds + behind * (cfg.relaxed_entry_max_seconds - cfg.entry_max_seconds)
        min_fair = cfg.min_fair - behind * (cfg.min_fair - cfg.relaxed_min_fair)
        min_fair -= cushion * (cfg.relaxed_min_fair - cfg.cushion_min_fair)
        min_ask = cfg.min_ask - behind * (cfg.min_ask - cfg.relaxed_min_ask)
        min_ask -= cushion * (cfg.relaxed_min_ask - cfg.cushion_min_ask)
        if not (max(cfg.entry_min_seconds, ENTRY_BLOCK_SECONDS + 1.0) < seconds <= entry_max):
            return dict(HOLD)
        if state.cycles >= cfg.max_cycles_per_market:
            return dict(HOLD)
        if now - state.last_flat_at < cfg.reentry_cooldown_seconds - EPS:
            return dict(HOLD)
        if now - state.last_buy_attempt_at < cfg.buy_retry_cooldown_seconds - EPS:
            return dict(HOLD)

        fair_yes = self._fair_yes(ref, seconds)
        if fair_yes is None:
            return dict(HOLD)

        rules = obs.get("rules") if isinstance(obs.get("rules"), dict) else {}
        rule_cap = _finite(rules.get("maximumBuyCashUsd"))
        cap = CONTRACT_MAX_BUY_USD if rule_cap is None else min(rule_cap, CONTRACT_MAX_BUY_USD)
        budget = math.floor(min(cap, cash - cfg.cash_reserve_usd) * 100.0 + 1e-9) / 100.0
        if budget <= 0.0:
            return dict(HOLD)

        edge_floor = max(
            cfg.min_edge_usd - cfg.pace_relax_usd * behind + cfg.pace_tighten_usd * ahead,
            cfg.min_edge_floor_usd,
        ) - cfg.cushion_relax_usd * cushion

        best: tuple[float, str, float, float] | None = None  # (ev, side, max_cash, price)
        for side in ("YES", "NO"):
            fair = fair_yes if side == "YES" else 1.0 - fair_yes
            if fair < min_fair - EPS:
                continue
            book = books.get(side)
            if not isinstance(book, dict):
                continue
            asks = parse_levels(book.get("asks"), descending=False)
            bids = parse_levels(book.get("bids"), descending=True)
            if not asks or not bids:
                continue
            best_ask = asks[0][0]
            if not (min_ask - EPS <= best_ask <= cfg.max_ask + EPS):
                continue
            mid = 0.5 * (best_ask + bids[0][0])
            if abs(fair - mid) > cfg.max_book_disagreement + EPS:
                continue
            min_size = _finite(book.get("minOrderSize"))
            min_size = 5.0 if min_size is None or min_size <= 0 else min_size

            shares, gross, fees = plan_buy(asks, budget, best_ask + cfg.price_tolerance)
            if shares <= EPS:
                continue
            # require the minimum size even if the price moves up by the tolerance
            worst = min(best_ask + cfg.price_tolerance, 0.999)
            worst_unit = worst * (1.0 + OVERLAY_FEE_RATE) + PROTOCOL_FEE_RATE * worst * (1.0 - worst)
            spend = gross + fees
            if spend / worst_unit < min_size * (1.0 + cfg.min_shares_buffer) - EPS:
                continue
            if shares < min_size * (1.0 + cfg.min_shares_buffer) - EPS:
                continue
            exit_value = fair - cfg.exit_slippage
            exit_cost = OVERLAY_FEE_RATE * exit_value + PROTOCOL_FEE_RATE * exit_value * (1.0 - exit_value)
            ev = shares * (exit_value - exit_cost) - spend
            if ev < edge_floor - EPS:
                continue
            max_cash = math.floor(min(budget, spend + 0.005) * 100.0 + 1e-9) / 100.0
            if max_cash <= 0.0 or max_cash > budget + EPS:
                continue
            if best is None or ev > best[0]:
                best = (ev, side, max_cash, best_ask)

        if best is None:
            return dict(HOLD)
        ev, side, max_cash, price = best
        self.last_reason = f"entry ev={ev:.3f} fair={fair_yes if side == 'YES' else 1 - fair_yes:.4f} ask={price}"
        state.last_buy_attempt_at = now
        state.entry_price = price
        return {"action": "BUY", "outcome": side, "maxCashUsd": max_cash}

    def _pace(self, account: dict[str, Any], now: float) -> tuple[float, float]:
        """(behind, ahead) in [0, 1] versus a linear schedule to the target."""
        cfg = self.cfg
        volume = _finite(account.get("eligibleVolumeUsd")) or 0.0
        if self.first_timestamp is None:
            return 0.0, 0.0
        elapsed_h = (now - self.first_timestamp) / 3600.0
        if elapsed_h < cfg.pace_warmup_hours:
            return 0.0, 0.0
        goal = TARGET_VOLUME_USD
        expected = goal * min(elapsed_h / cfg.pace_goal_hours, 1.0)
        if volume >= expected:
            ahead = min((volume - expected) / (0.5 * expected), 1.0) if expected > 0 else 0.0
            return 0.0, ahead
        behind = min((expected - volume) / goal / cfg.pace_full_relax_behind, 1.0)
        return behind, 0.0


def _valid_shape(decision: Any) -> bool:
    if not isinstance(decision, dict):
        return False
    action = decision.get("action")
    if action in ("HOLD", "SELL"):
        return len(decision) == 1
    if action == "BUY":
        amount = decision.get("maxCashUsd")
        return (
            decision.get("outcome") in ("YES", "NO")
            and isinstance(amount, (int, float))
            and 0.0 < float(amount) <= CONTRACT_MAX_BUY_USD
        )
    return False


# ---------------------------------------------------------------------------
# HTTP server (same contract as the official starter: POST /decide)
# ---------------------------------------------------------------------------

BOT = CleanCrossingBot()
MAX_BODY_BYTES = 1_000_000


class Handler(BaseHTTPRequestHandler):
    server_version = "CleanCrossing/1.0"

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0].rstrip("/") != "/decide":
            self.send_error(404)
            return
        decision: dict[str, Any] = dict(HOLD)
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if 0 < length <= MAX_BODY_BYTES:
                observation = json.loads(self.rfile.read(length))
                decision = BOT.decide(observation)
        except Exception:  # noqa: BLE001 - malformed request still gets a valid HOLD
            decision = dict(HOLD)
        self._send_json(decision)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0].rstrip("/") in ("/health", "/healthz"):
            self._send_json({"ok": True, "bot": "clean-crossing"})
            return
        self.send_error(404)

    def log_message(self, _format: str, *_args: Any) -> None:
        return


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Clean Crossing YeNo /decide server")
    parser.add_argument("--host", default=os.environ.get("YENO_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("YENO_PORT", os.environ.get("PORT", "8080"))))
    args = parser.parse_args(argv)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    print(f"Clean Crossing listening on http://{args.host}:{args.port}/decide", file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
