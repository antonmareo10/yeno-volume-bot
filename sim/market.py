"""Synthetic five-minute BTC Up/Down markets with L2 books.

No recorded YeNo/Polymarket data ships with the official starter kit, so this
module generates seeded, receive-time ordered market inputs with the features
the contract describes: a public BTC reference with timestamps, an opening
target that can arrive late, be provisional or be missing, two independently
updated L2 books (YES/NO) with tick sizes, finite depth, thin levels, stale
episodes and one-sided books near expiry, and a settlement price.

The market maker prices YES from a *lagged* BTC price with its own volatility
estimate (``mm_vol_mult`` x true volatility) plus autocorrelated noise.  Those
two knobs set how much causal edge exists for a bot that reads the public
reference; they are scenario parameters, not facts about the real venue.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field, asdict
from typing import Any

MARKET_SECONDS = 300.0
STEP = 0.1  # BTC path resolution in seconds


def norm_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


@dataclass
class Scenario:
    name: str = "base"
    hours: float = 24.0
    btc_start: float = 90000.0
    sigma_median: float = 6.0          # $/sqrt(s) true volatility (median regime)
    sigma_dispersion: float = 0.35     # lognormal sd of hourly regime
    jump_rate_per_hour: float = 3.0
    jump_sd_usd: float = 35.0
    basis_sd_usd: float = 2.0          # settlement oracle minus exchange price
    ref_period_s: float = 1.0
    ref_source_lag_s: tuple[float, float] = (0.2, 0.8)
    ref_noise_usd: float = 0.5
    ref_stale_prob: float = 0.03       # per market: one reference outage
    target_missing_prob: float = 0.25
    target_delay_s: tuple[float, float] = (1.0, 8.0)
    target_provisional_s: tuple[float, float] = (0.0, 5.0)
    mm_lag_s: tuple[float, float] = (0.5, 2.0)
    mm_vol_mult: float = 1.0
    mm_noise_z: float = 0.10           # AR(1) noise in z units
    mm_noise_tau_s: float = 5.0
    mm_smoothing_s: float = 0.0        # >0: quotes adjust gradually (z-space EWMA)
    spread: float = 0.01               # quoted spread around MM fair
    fine_ticks: bool = True            # 0.001 tick when price > 0.96 or < 0.04
    depth_median: float = 40.0
    depth_sigma: float = 0.9
    thin_level_prob: float = 0.08
    levels: int = 4
    book_interval_s: float = 0.7       # mean time between updates per side
    book_stale_prob: float = 0.04      # per market: one side goes quiet
    one_sided_after_s: float = 20.0    # extreme books can lose a side
    skip_market_prob: float = 0.02     # recorder-incomplete markets
    min_order_size: float = 5.0
    adverse_fill_usd: float = 0.0      # execution stress (per share, per side)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BookUpdate:
    at: float
    side: str
    bids: list[list[float]]
    asks: list[list[float]]


@dataclass
class ReferenceUpdate:
    at: float
    payload: dict[str, Any] | None


@dataclass
class Market:
    market_id: str
    open_at: float
    close_at: float
    target: float
    settle_price: float
    yes_wins: bool
    books: list[BookUpdate]
    references: list[ReferenceUpdate]
    has_target: bool
    min_order_size: float = 5.0


class MarketGenerator:
    """Generates consecutive markets for one seeded 24-hour path."""

    def __init__(self, scenario: Scenario, seed: int) -> None:
        self.sc = scenario
        self.rng = random.Random(seed)
        self.seed = seed
        self.price = scenario.btc_start
        self.basis = 0.0
        self.t0 = 1_790_000_000.0 + 300.0 * (seed % 1000)
        self.sigma = scenario.sigma_median
        self.regime_hour = -1

    def markets(self):
        sc = self.sc
        count = int(round(sc.hours * 3600.0 / MARKET_SECONDS))
        for index in range(count):
            open_at = self.t0 + index * MARKET_SECONDS
            hour = int(index * MARKET_SECONDS // 3600)
            if hour != self.regime_hour:
                self.regime_hour = hour
                self.sigma = min(max(sc.sigma_median * math.exp(self.rng.gauss(0.0, sc.sigma_dispersion)), 2.0), 25.0)
            path, oracle_open, oracle_close = self._path()
            if self.rng.random() < sc.skip_market_prob:
                continue  # recorder-incomplete market: excluded, time still passes
            yield self._market(index, open_at, path, oracle_open, oracle_close)

    # -- BTC path -------------------------------------------------------------
    def _path(self) -> tuple[list[float], float, float]:
        sc = self.sc
        rng = self.rng
        steps = int(MARKET_SECONDS / STEP) + 1
        path = [0.0] * steps
        sd = self.sigma * math.sqrt(STEP)
        jump_p = sc.jump_rate_per_hour * STEP / 3600.0
        basis_rho = math.exp(-STEP / 30.0)
        basis_sd = sc.basis_sd_usd * math.sqrt(1.0 - basis_rho ** 2)
        price = self.price
        basis = self.basis
        oracle_open = price + basis
        for i in range(steps):
            path[i] = price
            if i == steps - 1:
                break
            price += rng.gauss(0.0, sd)
            if rng.random() < jump_p:
                price += rng.gauss(0.0, sc.jump_sd_usd)
            basis = basis_rho * basis + rng.gauss(0.0, basis_sd)
        oracle_close = path[-1] + basis
        self.price = path[-1]
        self.basis = basis
        return path, oracle_open, oracle_close

    # -- market ---------------------------------------------------------------
    def _market(self, index: int, open_at: float, path: list[float], oracle_open: float, oracle_close: float) -> Market:
        sc = self.sc
        rng = self.rng
        close_at = open_at + MARKET_SECONDS
        target = round(oracle_open, 3)
        yes_wins = oracle_close >= target
        has_target = rng.random() >= sc.target_missing_prob
        omit_reference = (not has_target) and rng.random() < 0.5

        def price_at(t: float) -> float:
            x = (t - open_at) / STEP
            if x <= 0:
                return path[0]
            if x >= len(path) - 1:
                return path[-1]
            i = int(x)
            frac = x - i
            return path[i] * (1.0 - frac) + path[i + 1] * frac

        # reference stream
        references: list[ReferenceUpdate] = []
        target_at = open_at + rng.uniform(*sc.target_delay_s)
        final_at = target_at + rng.uniform(*sc.target_provisional_s)
        outage = None
        if rng.random() < sc.ref_stale_prob:
            start = open_at + rng.uniform(0.0, MARKET_SECONDS)
            outage = (start, start + rng.uniform(5.0, 20.0))
        t = open_at - rng.uniform(0.0, sc.ref_period_s)
        while t < close_at:
            if outage is None or not (outage[0] <= t < outage[1]):
                mid = price_at(t - rng.uniform(*sc.ref_source_lag_s)) + rng.gauss(0.0, sc.ref_noise_usd)
                if omit_reference:
                    payload = None
                else:
                    payload = {"btcMidUsd": round(mid, 3), "observedAt": round(t, 3)}
                    if has_target and t >= target_at:
                        payload.update(
                            openingTargetUsd=target,
                            targetObservedAt=round(target_at, 3),
                            targetProvisional=t < final_at,
                        )
                    else:
                        payload.update(openingTargetUsd=None, targetObservedAt=None, targetProvisional=True)
                references.append(ReferenceUpdate(t, payload))
            t += sc.ref_period_s * rng.uniform(0.8, 1.2)

        # market-maker books
        lag = rng.uniform(*sc.mm_lag_s)
        sigma_mm = self.sigma * sc.mm_vol_mult
        books: list[BookUpdate] = []
        noise_state = {"z": rng.gauss(0.0, sc.mm_noise_z), "t": open_at}

        def noise(t: float) -> float:
            dt = max(t - noise_state["t"], 0.0)
            rho = math.exp(-dt / sc.mm_noise_tau_s)
            noise_state["z"] = rho * noise_state["z"] + math.sqrt(max(1.0 - rho * rho, 0.0)) * rng.gauss(0.0, sc.mm_noise_z)
            noise_state["t"] = t
            return noise_state["z"]

        events: list[tuple[float, str]] = []
        for side in ("YES", "NO"):
            quiet = None
            if rng.random() < sc.book_stale_prob:
                start = open_at + rng.uniform(0.0, MARKET_SECONDS)
                quiet = (start, start + rng.uniform(3.0, 10.0))
            t = open_at + rng.uniform(0.0, 3.0)
            while t < close_at:
                if quiet is None or not (quiet[0] <= t < quiet[1]):
                    events.append((t, side))
                t += rng.expovariate(1.0 / sc.book_interval_s)
        events.sort()
        z_state: float | None = None
        last_t = open_at
        for t, side in events:
            remaining = max(close_at - t, 0.05)
            z = (price_at(t - lag) - target) / (sigma_mm * math.sqrt(remaining))
            if sc.mm_smoothing_s > 0:
                if z_state is None:
                    z_state = z
                else:
                    alpha = 1.0 - math.exp(-max(t - last_t, 0.0) / sc.mm_smoothing_s)
                    z_state += alpha * (z - z_state)
                last_t = t
                z = z_state
            p_yes = norm_cdf(z + noise(t))
            q = p_yes if side == "YES" else 1.0 - p_yes
            bids, asks = self._quote(q, remaining)
            books.append(BookUpdate(round(t, 3), side, bids, asks))

        return Market(
            market_id=f"sim-{self.seed}-{index:03d}",
            open_at=open_at,
            close_at=close_at,
            target=target,
            settle_price=oracle_close,
            yes_wins=yes_wins,
            books=books,
            references=references,
            has_target=has_target,
            min_order_size=sc.min_order_size,
        )

    def _quote(self, q: float, remaining: float) -> tuple[list[list[float]], list[list[float]]]:
        sc = self.sc
        rng = self.rng
        tick = 0.001 if sc.fine_ticks and (q > 0.96 or q < 0.04) else 0.01
        half = max(sc.spread / 2.0, tick / 2.0)
        if tick < 0.01:
            half = max(tick, half * rng.uniform(0.3, 1.0))
        bid = math.floor((q - half) / tick + 1e-9) * tick
        ask = math.ceil((q + half) / tick - 1e-9) * tick
        if ask - bid < tick - 1e-12:
            ask = bid + tick
        lo, hi = tick, 1.0 - tick
        bids: list[list[float]] = []
        asks: list[list[float]] = []
        one_sided = remaining < sc.one_sided_after_s
        if not (one_sided and q < 0.01):
            for k in range(sc.levels):
                price = round(bid - k * tick, 3)
                if price < lo - 1e-12:
                    break
                bids.append([price, self._size()])
        if not (one_sided and q > 0.99):
            for k in range(sc.levels):
                price = round(ask + k * tick, 3)
                if price > hi + 1e-12:
                    break
                asks.append([price, self._size()])
        return bids, asks

    def _size(self) -> float:
        sc = self.sc
        if self.rng.random() < sc.thin_level_prob:
            return round(self.rng.uniform(1.0, 5.0), 2)
        return round(max(1.0, sc.depth_median * math.exp(self.rng.gauss(0.0, sc.depth_sigma))), 2)
