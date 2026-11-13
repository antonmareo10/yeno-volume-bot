"""Named scenarios.  ``calibrated`` is tuned so the official House Bot v1
reproduces the order of magnitude of its published development benchmark;
the others stress the assumptions that matter most."""

from __future__ import annotations

from dataclasses import replace

from .market import Scenario


def efficient() -> Scenario:
    # fast, unbiased market maker: almost no causal edge from the reference
    return Scenario(name="efficient", mm_lag_s=(0.1, 0.4), mm_vol_mult=1.0)


def base() -> Scenario:
    return Scenario(name="base")


def calibrated() -> Scenario:
    return Scenario(name="calibrated", mm_lag_s=(0.5, 2.0), mm_vol_mult=1.0)


def SCENARIO(name: str, **changes) -> Scenario:  # noqa: N802 - tiny helper
    return replace(calibrated(), name=name, **changes)


SCENARIOS = {
    "efficient": efficient,
    "base": base,
    "calibrated": calibrated,
    "stress-2c": lambda: SCENARIO("stress-2c", adverse_fill_usd=0.02),
    "stress-1c": lambda: SCENARIO("stress-1c", adverse_fill_usd=0.01),
    "thin-books": lambda: SCENARIO("thin-books", depth_median=12.0, thin_level_prob=0.2),
    "high-vol": lambda: SCENARIO("high-vol", sigma_median=10.0, jump_rate_per_hour=6.0),
    "low-vol": lambda: SCENARIO("low-vol", sigma_median=3.5),
    "few-targets": lambda: SCENARIO("few-targets", target_missing_prob=0.5),
    "coarse-ticks": lambda: SCENARIO("coarse-ticks", fine_ticks=False),
    "wide-spread": lambda: SCENARIO("wide-spread", spread=0.02),
}
