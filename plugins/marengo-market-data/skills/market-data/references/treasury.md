# US Treasury curve — `scripts/treasury.py`

Design of record: `docs/us-treasury-curve-plan.md`. Provider: Alpha Vantage (first source: the US
Treasury). Keyed — `ALPHAVANTAGE_API_KEY` from the gitignored `.env`, never on the CLI.

## What exists

`TREASURY_YIELD` — constant-maturity rates, `unit: percent`, **newest row first**, `interval` ∈
daily / weekly / monthly. Six maturities and no others:

| maturity | rows (daily) | earliest |
| --- | --- | --- |
| 3month | 11 713 | 1981-09-01 |
| 2year | 13 083 | 1976-06-01 |
| 5year | 16 843 | 1962-01-02 |
| 7year | 14 888 | 1969-07-01 |
| 10year | 16 843 | 1962-01-02 |
| 30year | 12 898 | 1977-02-15 |

Companions: `FEDERAL_FUNDS_RATE` (daily, effective — not the target range) and `CPI` (monthly index,
1982-84 = 100).

**There is NO per-issue/CUSIP data at this provider.** A US analogue of `moex_bonds.py <SECID>` is
impossible; a curve page is not. Recorded so nobody re-probes it.

## The traps (probed live 2026-07-25)

1. **Silent maturity substitution — the headline one.** `maturity=1year`, `6month`, `20year` and even
   `maturity=banana` all return the **10-YEAR** series: HTTP 200, no error field, 16 843 rows. Only
   `name` betrays it. Trusting the request would draw six copies of the 10-year labelled 3m…30y — a
   fabricated curve that looks entirely plausible. Every leg therefore re-checks `name`, and a
   mismatch DROPS the tenor and says so on the page. (Fourth endpoint in this skill to answer a
   question it was not asked, after ISS's `from`/`till`, `emitent_id` and `params.dates`.)
2. **`value: "."` is a hole** — 719 of 16 843 daily rows on the 10-year. As a float it becomes 0.0
   and drags every average, minimum and historical leg through the floor.
3. **`INFLATION` is stale.** AV's own inflation series ends **2024-01-01** while CPI is current, so
   the real rate is computed year-over-year from the CPI INDEX. Using `INFLATION` would print a
   two-year-old number as today's.
4. **The data lags the clock** ~1 business day. The page is dated by its DATA, never by
   `date.today()`.
5. **Six knots, not a continuum.** No 1y, 3y or 20y — nothing is interpolated into a knot that does
   not exist.

## Reading the page

- **Наклон 10л−2г** is the difference of two published series — arithmetic, not a model. Below zero
  is an inversion; the card counts consecutive sessions. Historically it has preceded recessions, and
  the page says exactly that and no more: it does not predict or date one.
- **Реальная 10л** is nominal minus REALIZED year-over-year CPI. It is **not** a breakeven and not
  expected inflation — AV publishes no TIPS series, so market expectations are simply not visible
  here. CPI also lags the curve by ~1 month, which the panel states.
- Ставка ФРС beside the tenors answers what the tenors alone cannot: is the market ahead of policy
  or behind it.

## Cost

8 calls cold (6 tenors + fed funds + CPI), day-cached per (series, day) under
`MARKET_DATA_TREASURY_CACHE`. AV's free tier is 25 requests/day, so two cold runs in a day is a
third of the budget — the cache is not a nicety. Measured: 5.9 s cold, **0.16 s** warm.

## Review round 1 (5 blocking)

- **A throttle envelope is not data.** `{"Information": "…25 requests per day…"}` arrives as HTTP
  200 and a truthy dict; cached as a series it poisoned the WHOLE DAY — the page could not recover
  even after the limit cleared. Nothing throttled is ever cached now, and once the provider says
  "rate limit" the remaining calls are skipped rather than deepening it.
- **The module degrades instead of throwing.** `http_get` re-raises on 4xx and after its retries, so
  one flaky call out of eight killed a run whose other seven legs had already landed. Per-leg
  failures are now best-effort and named; `read_from_dir` no longer crashes on a bad `--from-dir`;
  a wrong-shaped payload cannot AttributeError out of `build()`.
- **The headline verdict must belong to the page's own date.** `slope`/`inverted`/`slopeRun` and the
  fed-funds card read the last row of THEIR OWN series, so one lagging tenor printed
  «+34 б.п. — нормальный наклон» above a curve that was inverted at `as_of`. All of them are now
  taken at `as_of`.
- **A historical leg is one date, not an average of dates.** Each tenor walks back independently, so
  a tenor whose publication paused (the 30-year did, 2002-2006) could contribute a point months
  older than the label. A tenor more than `_LEG_SLACK_DAYS` stale is EXCLUDED, and any remaining
  spread is printed in the series name.
- **WHY a tenor is missing is itself a provenance claim.** Reporting a throttle, a failed pull, a
  wrong shape or an empty series as "the provider silently substituted another tenor" would be this
  module telling a specific lie about where its data came from. Each cause is now named separately,
  and only a genuine substitution names the series that arrived instead.
- Plus: a missing CPI/fed-funds series leaves an «н/д» card instead of silently deleting the section;
  `--years` is bounded (an unbounded value overflowed the date arithmetic); the day cache sweeps
  other days and writes through a PID-unique temp; the headline number falls back to a real tenor
  when the 10-year is the one that dropped.

`mutate.py` found **seven vacuous tests of mine** across the two rounds — most instructively, a
fixture whose 10y and 2y drifted at the SAME rate, which made the slope constant over time so
"take the slope at as_of" could not be told from "take the latest print".
