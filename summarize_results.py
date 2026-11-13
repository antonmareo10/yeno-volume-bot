#!/usr/bin/env python3
"""Print a Markdown table from the JSON files written by ``run_sim.py --out``."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ORDER = ["calibrated", "mild-sluggish", "modest", "efficient", "stress-1c", "stress-2c", "few-targets", "thin-books"]


def fmt(value, digits=2):
    return "-" if value is None else f"{value:.{digits}f}"


def main() -> None:
    folder = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent / "results")
    files = {p.stem: p for p in folder.glob("*.json")}
    names = [n for n in ORDER if n in files] + sorted(n for n in files if n not in ORDER)
    print("| World | Bot | Qualified | Median cash at crossing (min-max) | Median crossing h (max) "
          "| Median volume | Median final cash | Invalid | Rejected | Flat at stop |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for name in names:
        report = json.loads(files[name].read_text())
        for bot, s in report["summary"].items():
            cash = (f"{fmt(s['median_cash_at_crossing'])} ({fmt(s['min_cash_at_crossing'])}-{fmt(s['max_cash_at_crossing'])})"
                    if s["qualified"] else "-")
            hours = f"{fmt(s['median_crossing_hours'], 1)} ({fmt(s['max_crossing_hours'], 1)})" if s["qualified"] else "-"
            print(f"| {name} | {bot} | {s['qualified']}/{s['paths']} | {cash} | {hours} | {s['median_volume']:.0f} "
                  f"| {s['median_final_cash']:.2f} | {s['invalid_total']} | {s['rejected_total']} "
                  f"| {s['flat_at_stop']}/{s['paths']} |")


if __name__ == "__main__":
    main()
