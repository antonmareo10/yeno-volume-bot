# Clean Crossing — a YeNo × Builderr volume bot

A paper-trading bot for the [YeNo × Builderr Volume Bot Challenge](https://builderr.ai/challenges/yeno-volume-bots):
five-minute BTC Up/Down markets, $10 of simulated cash, $5 fee-inclusive cap per BUY, and the goal of
completing **$1,000 of BUY+SELL volume within 24 hours while flat at the first clean crossing**, ranked by
cash at that crossing.

Python standard library only. One file (`yeno_volume_bot.py`) is the whole submission; everything else is
tests and a local paper harness.

## Run it (one command)

```bash
python3 yeno_volume_bot.py            # serves POST http://127.0.0.1:8080/decide
```

Options: `--host 0.0.0.0 --port 9000` (or env `YENO_HOST` / `YENO_PORT` / `PORT`). `GET /health` returns
`{"ok": true}`. With the official starter kit's smoke test (from <https://builderr.ai/downloads/yeno-smoke-test.py>):

```bash
python3 yeno-smoke-test.py --url http://127.0.0.1:8080/decide --observation yeno-sample-observation.json
# -> "contractSmokePassed": true
```

Install: nothing to install (`pip install -r requirements.txt` is a no-op). Tested on CPython 3.11.9 and
3.14.2 (Windows); the unit tests pass on both.

## The economics that drive the design

From the official contract (`yeno-evaluator-contract.md`):

* every fill pays the dynamic taker fee `0.07 × shares × p × (1 − p)` **and** a 1% YeNo overlay on gross
  notional, on both the BUY and the SELL side;
* only fully closed BUY→SELL cycles count; settlements/redemptions add zero volume;
* one position or pending BUY at a time, $5 fee-inclusive per BUY, `minOrderSize` shares minimum per BUY,
  no new entries in the final 15 s, 250 ms latency against a *later* book update.

Consequences:

1. **The 1% overlay alone costs exactly $10 per $1,000 of volume — the entire starting cash.** No amount of
   cost-cutting qualifies a bot without a real trading edge; with no edge a bot runs out of cash around
   $500–600 of volume (which is where every published diagnostic entry stopped).
2. The protocol fee per dollar traded is `0.07 × (1 − p)`: 3.5% at p = 0.5 but 0.2% at p = 0.97.
   High-priced favourites are by far the cheapest place to create volume.
3. Each cycle creates ≈ $10 of volume (≈ $5 in, ≈ $5 out), so ~100 cycles are needed out of ~288 markets.
   Throughput matters as much as edge.
4. With $5 at p ≥ 0.99 you cannot buy 5 shares, and with cash below ≈ $4.9 you cannot buy 5 shares of a
   0.97 favourite: protecting cash protects the ability to trade at all.

## Strategy

**Edge:** price the favourite from the public BTC reference and buy it only when the market is cheaper than
that fair value by more than all costs.

* **Fair value.** `P(YES) = Φ(gap / sqrt((k·σ)²·t + b²))` with `gap = btcMidUsd − openingTargetUsd`, `t` the
  seconds to close, `σ` an EWMA realised volatility of the reference (fast/slow, the larger is used), `b` a
  basis allowance between the reference and the settlement source.
* **Online calibration of `k`.** After each market ends, the bot labels it from what it already saw at the
  end (the YES book in the last seconds or a decisive reference gap) and scores its earlier snapshots under a
  grid of multipliers; `k` is the penalised maximum-likelihood value (prior centred on 1.15, floor 0.95).
  Strictly causal: a market only informs later markets.
* **Entry.** Only in the last 17–180 s, only the side with fair ≥ 0.90 and ask in [0.80, 0.975], only if the
  book is not wildly different from fair (|fair − mid| ≤ 0.15), only if the visible depth within one cent
  buys ≥ 1.03 × `minOrderSize` shares even if the price ticks up, and only if
  `EV = shares·(fair − slippage − exit fees) − (cost + buy fees) ≥ $0.05`. `maxCashUsd` is the cost of exactly
  those shares (≤ $5, ≤ cash), so the BUY never walks deep into the book.
* **Exit.** Sell when the bid reaches 0.99; when the book has caught up with fair *and* the whole cycle is not
  a loss; when fair drops below 0.70 (stop); and unconditionally at ≤ 5 s to close (a SELL can execute against
  bids even when asks are gone). Positions are never held into settlement on purpose.
* **Pacing.** Against a linear schedule to $1,000 by hour 18, the bot relaxes its filters only when behind
  (EV down to −$0.05, fair down to 0.85, ask down to 0.75, window up to 240 s), so it still crosses inside
  24 h when edges are thin, and keeps the strict filters (more cash per cycle) when on schedule. If it is
  behind **and** holds a cash cushion (> $14), it relaxes a little further (EV down to −$0.15, fair 0.80,
  ask 0.70): spending a few cents of a large cushion is cheap insurance against ending at $990.
* **Missing targets.** If the official opening target is missing, the bot may use its own reading of the
  reference at the market open — only after it has measured, on markets that had both, that the official
  target and its estimate agree (|mean| + 2·sd ≤ $15), with that basis added to the uncertainty.

**Reliability.** `decide()` never raises: every field is parsed defensively, anything unexpected returns
HOLD, BUY is only returned when flat, inside the window, `0 < maxCashUsd ≤ min(5, cash, rule cap)`, and
SELL only when a position in the *current* market exists. Malformed HTTP bodies get a 200 HOLD. In-process
decisions are typically far below 1 ms (slowest observed across all held-out runs: 17 ms, limit 200 ms); over
local HTTP the measured median was 6.6 ms and p99 22 ms (500 requests).

## Local paper harness (and its limits)

The starter kit ships no replay harness and no recorded data, and this entry only used the official kit and
the public Polymarket fee docs. So `sim/` contains:

* `sim/evaluator.py` — a reconstruction of the published contract: $10 start, $5 fee-inclusive BUY cap,
  one position/pending BUY, 250 ms latency with execution against the first *later* update of the order's
  own outcome, depth-limited partial fills, BUY rejection below `minOrderSize`, entries blocked in the final
  15 s, no requests while either book is older than 2 s, both fee layers rounded to 5 decimals, completed-cycle
  volume, settlement of anything held at the close (cash only, zero volume), decision-error counting
  (stop after 20 consecutive invalid decisions — an assumption), first clean $1,000 crossing.
* `sim/market.py` — **synthetic** seeded markets: BTC path with regime volatility and jumps, a reference feed
  with source lag/noise/outages, an opening target that arrives late, is provisional, or is missing (25%),
  YES/NO books from a market maker that prices from a lagged BTC price, finite depth, thin levels, 0.001 ticks
  near the extremes, quiet periods and one-sided books near expiry.

How much edge exists depends entirely on the market-maker knobs, which are assumptions, not facts about YeNo.
`calibrated` is a sluggish market maker (quotes chase fair value with a 10 s time constant); of the worlds
tried it is the one where the official House Bot v1 comes closest to its published development benchmark —
and it still does somewhat worse here than published, so this world is if anything conservative for that
style of edge. The other worlds remove most of the edge (`efficient`, `modest`) or stress execution.

Reproduce (official house/starter bots are not vendored; point `--kit` at the downloaded kit):

```bash
python3 run_sim.py --scenario calibrated --seeds 100-109 --bots clean,house,starter --kit PATH/TO/KIT --out results/calibrated.json
python3 run_sim.py --scenario calibrated --seeds 100 --hours 2 --bots http,clean --url http://127.0.0.1:8080/decide
python3 summarize_results.py results
```

Scenarios: `calibrated`, `mild-sluggish`, `modest`, `static-bias`, `lag-only`, `efficient`, `base`, and the
`calibrated` variants `stress-1c`, `stress-2c`, `thin-books`, `high-vol`, `low-vol`, `few-targets`,
`coarse-ticks`, `wide-spread`. One world × 10 seeds × 2 bots takes ≈ 1.5 min on one core (the runner lowers
its own priority). The `http` bot drives a running server exactly like the official evaluator; on a 2-hour
path it produced results identical to the in-process bot.

## Measured results (synthetic, held-out seeds)

Held-out seeds 100–109 (never used for tuning; tuning used seeds 1–9), full 24-hour paths, bot code at
commit `b47e024`, official House Bot v1 and starter bot loaded unchanged from the downloaded kit. Raw output:
`results/*.json`; table produced by `python3 summarize_results.py results`. **These are synthetic-world
numbers, not a forecast of the official score.**

| World (what it assumes) | Bot | Qualified | Median cash at crossing (min–max) | Median crossing h (max) | Median volume | Invalid decisions |
|---|---|---|---|---|---|---|
| calibrated (sluggish MM, 10 s) | **Clean Crossing** | **10/10** | **$35.73** (30.00–39.08) | 16.6 (19.1) | $1,004 | 0 |
| | House Bot v1 | 0/10 | – | – | $359 | 0 |
| | starter (HOLD) | 0/10 | – | – | $0 | 0 |
| mild-sluggish (4 s) | **Clean Crossing** | **10/10** | **$23.07** (13.85–27.00) | 18.2 (22.0) | $1,005 | 0 |
| | House Bot v1 | 0/10 | – | – | $278 | 0 |
| modest (static favourite underpricing, MM σ × 1.15) | **Clean Crossing** | 3/10 | $7.76 (6.36–9.71) | 19.1 (20.1) | $657 | 0 |
| | House Bot v1 | 0/10 | – | – | $223 | 0 |
| efficient (fast, unbiased MM) | Clean Crossing | 0/10 | – | – | $317 | 0 |
| | House Bot v1 | 0/10 | – | – | $212 | 0 |
| calibrated + 1¢ adverse fills | **Clean Crossing** | **10/10** | **$26.00** (23.39–29.68) | 17.0 (19.1) | $1,004 | 0 |
| | House Bot v1 | 0/10 | – | – | $223 | 0 |
| calibrated + 2¢ adverse fills | **Clean Crossing** | **10/10** | **$16.14** (13.85–19.18) | 17.8 (19.5) | $1,005 | 0 |
| | House Bot v1 | 0/10 | – | – | $148 | 0 |
| calibrated, 50% markets without target | **Clean Crossing** | **10/10** | **$33.72** (26.21–38.64) | 17.9 (20.5) | $1,004 | 0 |
| | House Bot v1 | 0/10 | – | – | $317 | 0 |
| calibrated, thin books | **Clean Crossing** | **10/10** | **$33.35** (28.95–38.88) | 15.8 (18.2) | $1,006 | 0 |
| | House Bot v1 | 0/10 | – | – | $333 | 0 |

Other measured facts (calibrated world, Clean Crossing): median 98 completed cycles, fees $17.44
($10.04 overlay + $7.38 protocol — the overlay floor is unavoidable), median max drawdown $1.41, zero
positions left to settlement, zero rejected BUYs, every path flat at the stop, slowest in-process decision
2.8 ms. The house bot's many rejections come from sending the exact cost of 5 shares as `maxCashUsd`: one
tick of adverse movement in the 250 ms latency leaves it below `minOrderSize`.

How to read this honestly:

* Where the market leaves no causal edge (`efficient`) **no bot can qualify** (the overlay alone costs the
  whole $10); Clean Crossing then bleeds to ≈ $3 like everyone else. In the thin-edge `modest` world it
  qualifies 3/10. Its advantage is real only to the extent that YeNo's books lag or under-price late favourites
  — the published diagnostics (entries ending with *more* cash than they started while trading ≈ 0.97
  favourites) suggest they do, but this repo has not seen real YeNo data.
* The house bot never qualified in any synthetic world here, while its published development benchmark
  reached $943; so the synthetic worlds are less favourable to *its* style than Builderr's recorded regimes.

## Tests

```bash
python3 -m unittest discover -s tests
```

44 tests: contract fee math, buy/sell planning, garbage and fuzzed inputs (3,000 random observations, no
invalid action and no exception path), entry/exit rules, the 15-second block, stale/provisional/future
reference handling, pacing/cushion behaviour, calibrator recovery of a known multiplier, decision latency,
and the HTTP server
(round trip, malformed body → HOLD, health, 404), plus harness accounting (volume = executed BUY + SELL
notional of closed cycles, fees, min-size rejection, later-update execution, 15 s block, settlement adds
zero volume, stale books not requested, crossing).

## License

MIT © 2026 Antonio González Fernández
