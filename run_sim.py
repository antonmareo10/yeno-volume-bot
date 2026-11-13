#!/usr/bin/env python3
"""Run bots through the local paper harness on seeded synthetic 24-hour paths.

Examples
--------
    python run_sim.py --scenario calibrated --seeds 100-129
    python run_sim.py --scenario calibrated --seeds 100-129 --kit PATH/TO/KIT --bots clean,house,starter
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:  # works under `python -I` too
    sys.path.insert(0, str(ROOT))

from sim.bots import clean_factory, house_factory, http_factory, starter_factory  # noqa: E402
from sim.evaluator import PaperEvaluator  # noqa: E402
from sim.scenarios import SCENARIOS  # noqa: E402


def low_priority() -> None:
    try:
        import ctypes  # Windows: idle priority so heavier jobs keep the CPU

        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x40)
    except Exception:  # noqa: BLE001
        try:
            import os

            os.nice(10)
        except Exception:  # noqa: BLE001
            pass


def parse_seeds(text: str) -> list[int]:
    seeds: list[int] = []
    for part in text.split(","):
        if "-" in part:
            lo, hi = part.split("-")
            seeds.extend(range(int(lo), int(hi) + 1))
        elif part:
            seeds.append(int(part))
    return seeds


def aggregate(rows: list[dict]) -> dict:
    vols = [r["volume"] for r in rows]
    qualified = [r for r in rows if r["qualified"]]
    crossing_cash = [r["cash_at_crossing"] for r in qualified]
    hours = [r["crossing_hours"] for r in qualified]
    return {
        "paths": len(rows),
        "qualified": len(qualified),
        "median_volume": round(statistics.median(vols), 2),
        "min_volume": round(min(vols), 2),
        "max_volume": round(max(vols), 2),
        "paths_ge_750": sum(1 for v in vols if v >= 750),
        "median_cash_at_crossing": round(statistics.median(crossing_cash), 3) if crossing_cash else None,
        "mean_cash_at_crossing": round(statistics.mean(crossing_cash), 3) if crossing_cash else None,
        "min_cash_at_crossing": round(min(crossing_cash), 3) if crossing_cash else None,
        "max_cash_at_crossing": round(max(crossing_cash), 3) if crossing_cash else None,
        "median_crossing_hours": round(statistics.median(hours), 2) if hours else None,
        "max_crossing_hours": round(max(hours), 2) if hours else None,
        "median_final_cash": round(statistics.median(r["final_cash"] for r in rows), 3),
        "median_fees": round(statistics.median(r["fees"] for r in rows), 3),
        "median_cycles": statistics.median(r["cycles"] for r in rows),
        "invalid_total": sum(r["invalid"] for r in rows),
        "rejected_total": sum(r["rejected"] for r in rows),
        "zero_fills_total": sum(r["zero_fills"] for r in rows),
        "settled_positions_total": sum(r["settled_positions"] for r in rows),
        "stopped_on_errors": sum(1 for r in rows if r["stopped"] == "repeated-decision-errors"),
        "flat_at_stop": sum(1 for r in rows if r["flat_at_stop"]),
        "max_decision_ms": max(r["max_decision_ms"] for r in rows),
        "mean_cycle_pnl": round(statistics.mean(r["mean_cycle_pnl"] for r in rows if r["mean_cycle_pnl"] is not None), 5)
        if any(r["mean_cycle_pnl"] is not None for r in rows)
        else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario", default="calibrated", choices=sorted(SCENARIOS))
    parser.add_argument("--seeds", default="100-109")
    parser.add_argument("--bots", default="clean")
    parser.add_argument("--kit", type=Path, help="folder with the official house/starter bot files")
    parser.add_argument("--config", default="{}", help="JSON overrides for the Clean Crossing config")
    parser.add_argument("--hours", type=float, default=None)
    parser.add_argument("--url", help="for --bots http: the /decide endpoint to drive over HTTP")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    low_priority()
    scenario = SCENARIOS[args.scenario]()
    if args.hours is not None:
        scenario.hours = args.hours
    overrides = json.loads(args.config)

    factories = {}
    for name in [b.strip() for b in args.bots.split(",") if b.strip()]:
        if name == "clean":
            factories[name] = clean_factory(**overrides)
        elif name == "house":
            if args.kit is None:
                parser.error("--kit is required for the house bot")
            factories[name] = house_factory(args.kit)
        elif name == "http":
            if not args.url:
                parser.error("--url is required for the http bot")
            factories[name] = http_factory(args.url)
        elif name == "starter":
            if args.kit is None:
                parser.error("--kit is required for the starter bot")
            factories[name] = starter_factory(args.kit)
        else:
            parser.error(f"unknown bot {name}")

    per_bot: dict[str, list[dict]] = {name: [] for name in factories}
    started = time.time()
    for seed in parse_seeds(args.seeds):
        results = PaperEvaluator(scenario, seed, factories).run()
        for name, row in results.items():
            row["seed"] = seed
            per_bot[name].append(row)
            if not args.quiet:
                print(
                    f"seed={seed} bot={name} q={row['qualified']} vol={row['volume']:.1f} "
                    f"cash@x={row['cash_at_crossing']} h={row['crossing_hours']} final={row['final_cash']:.2f} "
                    f"cyc={row['cycles']} fees={row['fees']:.2f} inv={row['invalid']} rej={row['rejected']} "
                    f"settled={row['settled_positions']} dd={row['max_drawdown']:.2f}",
                    flush=True,
                )
    summary = {name: aggregate(rows) for name, rows in per_bot.items()}
    report = {
        "scenario": scenario.to_dict(),
        "seeds": args.seeds,
        "config_overrides": overrides,
        "elapsed_s": round(time.time() - started, 1),
        "summary": summary,
        "runs": per_bot,
    }
    print(json.dumps({"scenario": args.scenario, "elapsed_s": report["elapsed_s"], "summary": summary}, indent=1))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
