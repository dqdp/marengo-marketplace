# Portfolio Reference (analyze risk / return)

Open this when the user wants to **analyze a portfolio / basket** — "how risky is my
portfolio", "what's my Sharpe / drawdown / correlation", "how am I doing vs the S&P",
"what's driving my risk". Equities/ETFs route to Alpha Vantage; this is the
portfolio-analytics playbook on top of it.

## Use the script — `scripts/portfolio.py`

One command fetches the **split/dividend-adjusted** daily series for every holding +
the benchmark **directly over HTTP, concurrently**, and reduces them to a compact set
of risk/return numbers. This is the portfolio analogue of `chart.py` / `fundamentals.py`:
the per-ticker price histories never enter context (they would be N×20-year series), and
the math (return, vol, Sharpe, SORTINO, CALMAR, drawdown + underwater curve, correlation,
correlation-to-benchmark, risk contribution, beta) is done locally in pure stdlib.
Since **v1.68.0 the page IS the dashboard.py engine** (bubble/heatmap/line-dash/fill blocks;
the bespoke Chart.js template is gone). Returns are WINDOW returns, not P&L since purchase
(the subtitle says so; cost-basis P&L waits on cost/date columns). Key from `.env`.

**Standing holdings file:** with no `--holdings`/`--portfolio`, portfolio.py reads
`<skill-root>/holdings.csv` (the `.env` pattern — survives reinstalls, package-guarded from
shipping). 'show my portfolio' needs no args. A future broker MCP just writes this same file
(format contract: docs/universal-portfolio-plan.md). Cowork/egress-blocked: MARKET_DATA_NO_NET=1
fast-fails to the MCP + --from-dir route.

```
<python> "<skill-dir>/scripts/portfolio.py" --holdings "NVDA:100,AAPL:50,MSFT:40"        # text summary
<python> "<skill-dir>/scripts/portfolio.py" --portfolio holdings.csv --html dash.html    # responsive dashboard
<python> "<skill-dir>/scripts/portfolio.py" --portfolio holdings.csv --svg dash.svg --png # SVG + PNG (agent verify)
<python> "<skill-dir>/scripts/portfolio.py" --holdings "NVDA:0.5,AAPL:0.5" --as weight --json
```
Invoke by the **absolute skill path** (cwd is the user's project, not the skill folder).

## Key tier & failure modes (read before invoking)

- **Concurrency × tier:** one adjusted-series call per holding + the benchmark fire
  CONCURRENTLY (plus the risk-free pull) — a free AV key (25 req/day · 5/min)
  throttles on any real basket. The throttle error names the limits and the fan-out
  ("Alpha Vantage throttled (free tier = 25 req/day · 5/min; portfolio fans out one
  adjusted-series call per holding CONCURRENTLY — premium recommended, or space the
  runs)"). Premium recommended.
- **`TIME_SERIES_DAILY_ADJUSTED` is historically premium-gated:** if AV returns a
  premium note, the KEY/tier is the cause — not the ticker. Don't burn calls
  retrying other symbols.
- **Validate every ticker before invoking — one bad symbol aborts the whole basket.**
  By design (a silent partial basket would misprice risk); the error names the
  offender: "no daily series for X (bad ticker / no entitlement?)".
- **Bad values die at parse, not mid-run:** `--holdings "NVDA:abc"` → "error: bad
  --holdings value in 'NVDA:abc' (use TICKER:NUMBER, e.g. NVDA:100)"; a non-positive
  `--days` dies at argparse ("must be a positive integer").

## Input — three ways to give holdings

- **`--portfolio FILE`** — a CSV with a ticker column + ONE quantity column. The kind is
  **auto-detected** from the header name: `shares`/`qty`/`quantity`/`units`/`position` →
  shares; `weight`/`pct`/`%`/`alloc` → weights; `value`/`amount`/`usd`/`$`/`market` →
  dollar values. With no recognizable header it falls back to magnitudes (the column
  summing to ~1 or ~100 is read as weights, else shares). A header-less `TICKER,NUMBER`
  CSV works too.
- **`--holdings "NVDA:100,AAPL:50"`** — inline. The number is **shares** by default; pass
  `--as weight` or `--as value` to read the inline numbers as weights / dollar values.
- **Agent-built file.** If the user pastes positions or a screenshot of their brokerage,
  build the CSV for them (ticker + one quantity column) and point `--portfolio` at it.

The holdings file is **personal** — the skill's `.gitignore` excludes `*holdings*` and
`portfolio.csv`. Never commit positions, never echo a full holdings dump into chat.

## What it computes (all local, all adjusted)

- **Per holding:** weight, market value, annualized return (geometric), annualized
  volatility, Sharpe, max drawdown, and **% risk contribution** (share of portfolio
  variance, summing to 100% — not the same as weight; a volatile/correlated name carries
  more risk than its weight).
- **Portfolio:** annualized return/vol/Sharpe/max-drawdown from the **buy-and-hold value
  series** (share counts held fixed, so weights drift the way a real holding does), plus
  **beta** vs the benchmark.
- **Correlation matrix** of daily returns across holdings.
- **Benchmark** (default **SPY**, `--benchmark TICKER`, `--benchmark ""` to skip):
  return/vol/drawdown and a cumulative line to compare against.
- **Risk-free** is pulled live (3-month Treasury via `TREASURY_YIELD`) for the Sharpe
  ratio; if unavailable it falls back to 0 and **says so** (`riskFreeKnown=false`).
- **Window:** `--days` calendar days (default 365). Needs ≥3 overlapping trading days
  across all holdings + benchmark or it errors (mismatched/late-listed tickers).

## Egress-blocked sandbox — the `--from-dir` MCP fallback

Same pattern as `chart.py --from` / `fundamentals.py --from-dir`. When direct HTTP egress
is blocked (agent-mode / Home) but the AV **MCP** connector still works:

1. For each ticker **and** the benchmark, call `TIME_SERIES_DAILY_ADJUSTED`
   (`outputsize=full`, `return_full_data=true` so it offloads to a file) and save each
   result to a file whose **name contains that ticker** (e.g. `NVDA_daily_adjusted.json`).
2. Optionally save a `TREASURY_YIELD` result to a file whose name contains
   `treasury`/`yield` for the risk-free rate (else it assumes 0).
3. Render from the directory — no HTTP:
   ```
   <python> "<skill-dir>/scripts/portfolio.py" --holdings "NVDA:100,AAPL:50" --from-dir <dir> --html dash.html
   ```
Caveat (honest): in this mode the series DO pass through context (the MCP pull is
unavoidable when egress is blocked). Prefer direct HTTP whenever egress is open.

## Output modes — when to use each

| Mode | What | Who sees it |
| --- | --- | --- |
| (default) text | portfolio stats + per-holding table + benchmark + correlation highlights | the chat (always) |
| `--json` | the same as compact JSON | feeds a widget |
| `--svg` / `--png` | self-contained landscape dashboard (cumulative-vs-benchmark, correlation heatmap, risk-return scatter, weights donut, risk-contribution bars) / its PNG | file / agent verify |
| `--html` | **truly self-contained** **responsive** Chart.js dashboard — grid reflows one column when narrow, **two columns from 680px up** (capped at two); line + bubble scatter + doughnut + a colored correlation grid + a holdings table; **Chart.js inlined (no CDN — renders in egress-blocked/in-app viewers)** + dark-mode CSS | inline widget / expandable artifact / Preview Pane / in-app viewer |
| `--open` | writes the dashboard (HTML if `--html` was also passed, else SVG) and opens it in the **default browser** | the user, on their screen |

**Showing it to the user** follows the canonical rule in `references/fundamentals.md`
("Showing it to the user"): the DEFAULT is **text + the `--html` dashboard in the Preview
Pane** (side panel, self-verified by data+console; screenshots only in dev sessions or on
anomaly — the fundamentals.md calibrated rule) for an "analyze my portfolio" request;
**text only** for a one-number question (e.g. "what's my Sharpe?"); external browser
(`--open`), an inline `--json` widget, or PNG **only when the user asks by name**. Never
re-emit the big SVG through `show_widget` (it streams for minutes).

## Reading the numbers — quirks to flag

- **Weights drift; the script is buy-and-hold.** It holds the implied share counts fixed
  over the window, so the cumulative line is the historical performance of *today's*
  basket. It does NOT model contributions, rebalancing, or trades during the window. Say
  so if the user has been actively trading.
- **Risk contribution ≠ weight.** A name can be 25% of the portfolio but 50% of the risk
  (high vol + high correlation). That gap is the point of the risk-contribution panel.
- **Returns are split/dividend-adjusted** (`5. adjusted close`), so a split inside the
  window won't fake a crash, and total return includes dividends.
- **Benchmark default is SPY** (an ETF proxy for the S&P 500). For a non-US or sector
  book, set `--benchmark` to something appropriate and say which.
- **Short/derivative/levered positions** aren't modeled — this is a long-only,
  share-count basket. Flag anything that doesn't fit.

## Reporting & safety

- State provenance in TEXT (Alpha Vantage, split/dividend-adjusted, window, as-of date,
  benchmark) next to any chart.
- This is **data analysis, not investment advice** — keep that framing, especially on
  risk/allocation calls.

## Offline test

`<python> scripts/test_portfolio.py` checks the pure pieces (parsing incl. CSV
auto-detect, the stats math, correlation, risk-contribution summing to 1, text+SVG+HTML
rendering incl. a crashing ticker, and the `--from-dir` file path) with no network —
wire it into CI alongside `test_chart.py` / `test_fundamentals.py` / `test_greeks.py`.
