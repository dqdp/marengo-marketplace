# Alpha Vantage Reference (equities, ETFs, options, FX, crypto, indices, macro, news)

Open this file when a request routes to Alpha Vantage: equities/ETFs, equity/ETF
options, FX, crypto, indices, fundamentals, earnings, macro, technical indicators,
news/sentiment, or benchmark commodity spot. The router (`SKILL.md`) decides
routing and the shared options model; this file holds the AV-specific detail.

Asset-class coverage verified live 2026-06-29 (real data on the current key).

## Key tier & limits

Free keys = **25 requests/day · 5/minute**. The skill's dashboard scripts fan out
CONCURRENT calls — `fundamentals.py` 8–9 per run, `portfolio.py` N+2 (one adjusted
series per holding + benchmark + risk-free) — so they effectively need a **premium
key**. On a free key expect the scripts' throttle error naming exactly these limits
("free tier = 25 req/day · 5/min"), not silent partial data. Realtime options
additionally need the premium options entitlement (the sample-payload tell in
Branch B below).

## Interface

Alpha Vantage MCP exposes each function as its OWN directly-callable tool —
`GLOBAL_QUOTE`, `REALTIME_OPTIONS`, `RSI`, `CPI`, `NEWS_SENTIMENT`, etc. (verified
against the connected server 2026-06-30). Call the function tool by name with its
params; there is NO `TOOL_LIST`/`TOOL_GET`/`TOOL_CALL` wrapper layer. Inspect a
function's params/response from its tool schema. AV is its own MCP server, not a
script in this skill, and not a fallback for Databento tasks.

## Quirks & failure modes (verified live 2026-06-30)

- **Empty `{}` means failure, not success.** AV returns an empty object (not an
  error) for an invalid/unknown symbol or wrong params — e.g. `GLOBAL_QUOTE ZZZZ99`
  → `{}`. Treat empty/`{}` as "no data / bad symbol / wrong params" and say so;
  never report it as a valid quote. Confirm odd tickers with `SYMBOL_SEARCH`.
- **`VWAP` is intraday-only.** `interval=daily|weekly|monthly` silently returns
  `{}`. Use `1min/5min/15min/30min/60min`.
- **`REALTIME_OPTIONS` supports `expiration=YYYY-MM-DD`** (verified live 2026-07-04:
  SPCX full chain 3,806 rows / 181 KB wire vs one series 180 rows / 9.4 KB — ~19x). The
  scripts push a known `--expiry` down to the API; only the DISCOVERY call (no expiry yet,
  or `--list-expiries`) pulls the full chain.
- **`SYMBOL_SEARCH` top match may be a foreign listing.** "tesla" returns
  `0R0X.LON` (matchScore 1.0) ABOVE `TSLA` (0.889). Resolve names to the **US
  primary ticker**: filter `region=United States` / pick the plain US ticker —
  never take match #1 blindly.
- **Ticker strings are not globally unique.** The 2026-07-04 `LISTING_STATUS` sweep
  (`docs/collision-audit-2026-07-04.md`) found 23 futures roots and 35 MOEX tickers
  colliding with live US listings (`ZM`=Zoom≠Soybean Meal, `ROST`=Ross Stores≠Росинтер,
  `SIBN`=SI-BONE≠Газпромнефть, `FLOT`=iShares ETF≠Совкомфлот…). A valid-looking AV
  response proves the string exists on a US exchange — NOT that it is the user's
  instrument. Classify first (SKILL.md ladder).
- **`OVERVIEW` (REST) = `COMPANY_OVERVIEW` (MCP tool).** Same endpoint, two names:
  the REST function name is `OVERVIEW` (what `fundamentals.py` calls over HTTP);
  the MCP server exposes it as the `COMPANY_OVERVIEW` tool. Don't hunt for a REST
  `COMPANY_OVERVIEW` — it doesn't exist.
- **`CORRELATION`/`COVARIANCE` need ≥2 symbols.** One symbol returns a trivial
  `[[1]]` with no error — pass the full basket.
- **`DIVIDENDS` amounts are as-paid, NOT split-adjusted** (pre-split AAPL rows show
  $3.29 etc.); old rows carry `None` for declaration/record/payment dates. Don't
  compare raw amounts across a split.
- **`TIME_SERIES_INTRADAY` defaults `extended_hours=true`** → the latest bars are
  pre/post-market and can contain outlier prints (e.g. a 268 low against a 281
  close). Set `extended_hours=false` for clean RTH analysis.
- **Bulk over loops:** for several US symbols use ONE `REALTIME_BULK_QUOTES`
  (≤100 symbols, includes extended-hours fields) instead of N× `GLOBAL_QUOTE`.

## Live-data default (entitlement)

Return live data by default; don't ask or add EOD-vs-realtime caveats unless the
user asks or live is unavailable.

The `entitlement` parameter exists ONLY on US equity/ETF price functions:
- Accept `entitlement` → pass `entitlement=realtime` by default: `GLOBAL_QUOTE`,
  `REALTIME_BULK_QUOTES`, `TIME_SERIES_INTRADAY`, `TIME_SERIES_DAILY[_ADJUSTED]`,
  `TIME_SERIES_WEEKLY[_ADJUSTED]`, `TIME_SERIES_MONTHLY[_ADJUSTED]`, and the
  technical indicators. Without realtime these return the last completed session
  (looks stale on weekends/holidays).
- Do NOT accept `entitlement` (already realtime/fixed-cadence) — call normally:
  FX (`FX_*`), crypto (`CURRENCY_EXCHANGE_RATE`, `CRYPTO_INTRADAY`,
  `DIGITAL_CURRENCY_*`), `GOLD_SILVER_SPOT`, the realtime options family,
  commodities, macro, `INDEX_DATA`, fundamentals, dividends/splits, earnings,
  news/sentiment.
- For historical equity series, `entitlement=realtime` doesn't change past bars; it
  only makes the latest point the in-progress current-day bar. For strict backtests
  on completed candles, omit `entitlement` or drop the latest in-progress bar.
- `entitlement=delayed` (15-min) is only a fallback if a realtime equity call is
  explicitly rejected.

## Equity/ETF options (Branch B)

Simpler and richer than Databento futures options: AV returns greeks/IV natively.

- `REALTIME_OPTIONS` returns the real live chain with two-sided quotes, sizes,
  volume, open interest, and (with `require_greeks=true`) delta/gamma/theta/vega/IV,
  dated today. Default for current/live chains. `REALTIME_OPTIONS_FMV`,
  `REALTIME_PUT_CALL_RATIO`, `REALTIME_VOLUME_OPEN_INTEREST_RATIO` also work.
- `HISTORICAL_OPTIONS` / `HISTORICAL_PUT_CALL_RATIO` for past dates (default =
  previous session, or `date=YYYY-MM-DD`), also with greeks/IV.
- No `entitlement` param — plan-gated. AV IS the greeks/IV source for equity
  options (Databento gives none).
- Coverage = equities + ETFs (incl. daily expiries + LEAPS — verified SPY to 2028).
  Cash-index options are NOT available here: `options.py` refuses them and names the
  proxy where one is defensible (`SPX→SPY`, `NDX→QQQ`, `RUT→IWM`, `DJX→DIA`; confirm
  the proxy with the user). `VIX` has no clean spot-ETF proxy because VIX options price
  off VIX futures.
- Live during US options RTH 09:30-16:00 ET; outside RTH = last session. The `date`
  field shows the session; no intraday tick timestamp — cross-check the underlying
  quote if second-level freshness matters.
- Entitlement tell: if `REALTIME_OPTIONS` returns an ARTIFICIAL SAMPLE (a `message`
  about a 600/1200 req-min premium plan + placeholder rows `contractID`/`symbol`
  "XXYYZZ…", `expiration` "2099-99-99", `date` "2049-99-99"), the key is NOT
  options-entitled → don't present those fake numbers, fall back to
  `HISTORICAL_OPTIONS`, tell the user realtime options need the premium plan. (The
  current key returns real data.)

Branch B steps (use the fewest calls — see the router's "fewest calls" rule):
1. ONE call answers ATM / moneyness / N-delta: `REALTIME_OPTIONS` with `symbol`,
   `expiration=YYYY-MM-DD`, `require_greeks=true`. The response carries strikes and
   native `delta` for every contract, so read moneyness and N-delta straight from
   it — no separate center call. For MCP/tool calls, pass `expiration` (and/or
   `contract`) because full chains are too large to read inline. For the local CLI,
   `<python> "<skill-dir>/scripts/options.py" SYMBOL --list-expiries` deliberately calls
<python> "<skill-dir>/scripts/options.py" <SYM> --term        # CROSS-expiration: term curve + IV surface + series table
   # from the SAME single REALTIME_OPTIONS payload (every expiration arrives anyway) — zero extra calls
   `REALTIME_OPTIONS` without `expiration`, lists the expirations from that full-chain
   response, and exits before rendering a dashboard.
   - Liquid-underlying caveat (verified: SPY single expiry = ~670 rows / ~100KB):
     even ONE expiration can exceed the tool-output token limit and get spilled to a
     file. Don't try to read the whole response inline. Narrow further — pass
     `contract` for the exact contractID, or get the center first
     (`GLOBAL_QUOTE entitlement=realtime`) and target a strike band — or, when the
     response is saved to a file, extract just the needed strike(s) with jq/python
     (filter `type` + `strike` near center) rather than loading the full chain.
2. A `GLOBAL_QUOTE` (`entitlement=realtime`) center is only needed when the user
   wants the underlying price stated, or for a specific strike that must be quoted
   without pulling the chain at all (then just `GLOBAL_QUOTE` is unnecessary too —
   filter the chain to that strike).
3. Select the contract from the response by request type (see the shared selection
   list in `SKILL.md`). N-delta: read the native `delta` and pick |delta| ≈ target —
   NO computation (the big win over Branch A).

## Capability coverage (verified 2026-06-29)

| Class | Functions (representative) | Verified |
| --- | --- | --- |
| Equities/ETFs (realtime) | `GLOBAL_QUOTE`, `REALTIME_BULK_QUOTES`, `TIME_SERIES_*` | yes |
| Equity/ETF options + greeks/IV | `REALTIME_OPTIONS`, `HISTORICAL_OPTIONS`, `REALTIME_PUT_CALL_RATIO`, `REALTIME_OPTIONS_FMV`, `REALTIME_VOLUME_OPEN_INTEREST_RATIO` | yes — native greeks, dailies + LEAPS; NO cash-index options |
| Crypto | `CURRENCY_EXCHANGE_RATE` (rt + bid/ask), `CRYPTO_INTRADAY`, `DIGITAL_CURRENCY_*` | yes |
| Indices (prices) | `INDEX_DATA` (200+: SPX/NDX/VIX/DJI/COMP/RUT), `INDEX_CATALOG` | yes |
| FX | `CURRENCY_EXCHANGE_RATE`, `FX_INTRADAY/DAILY/WEEKLY/MONTHLY` | yes |
| Fundamentals | `COMPANY_OVERVIEW`, `INCOME_STATEMENT`, `BALANCE_SHEET`, `CASH_FLOW`, `ETF_PROFILE`, `EARNINGS_CALL_TRANSCRIPT`, `DIVIDENDS`, `SPLITS`, `INSIDER_TRANSACTIONS`, `INSTITUTIONAL_HOLDINGS` | yes |
| Earnings | `EARNINGS`, `EARNINGS_ESTIMATES`, `EARNINGS_CALENDAR`, `IPO_CALENDAR` | yes |
| Macro (US) | `TREASURY_YIELD`, `CPI`, `FEDERAL_FUNDS_RATE`, `REAL_GDP`, `REAL_GDP_PER_CAPITA`, `INFLATION`, `UNEMPLOYMENT`, `NONFARM_PAYROLL`, `RETAIL_SALES`, `DURABLES` | yes |
| Technical indicators (50+) | `RSI`, `MACD`, `ATR`, `VWAP`, `SMA/EMA/...`, `BBANDS`, `STOCH`, `OBV`, Hilbert | schema-confirmed (accept `entitlement`) |
| News & sentiment | `NEWS_SENTIMENT` (sentiment + ticker scores + topics), `ANALYTICS_*` | yes |
| Commodity spot | `WTI`, `BRENT`, `NATURAL_GAS`, `COPPER`, `ALUMINUM`, `WHEAT`, `CORN`, `COTTON`, `SUGAR`, `COFFEE`, `ALL_COMMODITIES`, `GOLD_SILVER_SPOT/HISTORY` | yes |
| Utility (not market data) | `PING`, `ADD_TWO_NUMBERS`, `SEARCH`, `FETCH`, `SYMBOL_SEARCH`, `MARKET_STATUS`, `TOP_GAINERS_LOSERS`, `LISTING_STATUS` | — |

Large-payload note: many endpoints overflow the tool token limit and get offloaded
to a file — `TIME_SERIES_*`, `INDEX_DATA`, `TREASURY_YIELD`, indicators (full
history), `NEWS_SENTIMENT` (~50 articles), `ETF_PROFILE` (full holdings),
`REALTIME_OPTIONS` (even one liquid expiry, see Branch B). Default `return_full_data=true`
so the client spills to a file, then extract the needed slice with jq/python rather
than reading the whole thing inline; and narrow by params where possible
(`tickers`/`topics`/`time_from` for news, `expiration`/`contract` for options,
date range for series). `limit` on `NEWS_SENTIMENT` does NOT reliably shrink the feed.

## Analytics & alternative data (verified live 2026-06-30)

Beyond raw prices, AV exposes statistical analytics, sentiment, and alt-data. The
high-value ones, with their real shapes and gotchas:

**`ANALYTICS_FIXED_WINDOW`** — portfolio statistics over ONE date window, one call.
Required: `symbols` (comma-list, ≤5 free / ≤50 premium), `range_param`
(e.g. `100day`, `6month`, `1year`, `full`, or dates), `interval` (`DAILY`/…),
`calculations` (comma-list). Calcs verified: `MEAN`, `STDDEV`, `VARIANCE`,
`CUMULATIVE_RETURN`, `MAX_DRAWDOWN` (returns the value + `drawdown_range` dates),
`CORRELATION`, `COVARIANCE` (matrix with `index` order), `AUTOCORRELATION`,
`MIN`/`MAX`/`MEDIAN`, `HISTOGRAM`. Stats are on RETURNS (under `RETURNS_CALCULATIONS`).
Use for "compare vol / drawdown / correlation of X, Y, Z over N days".

**`ANALYTICS_SLIDING_WINDOW`** — same metrics as a MOVING series. Adds
`window_size` (min 10). Free keys = 1 calc only. Returns `RUNNING_<METRIC>` keyed
by date plus a `window_start` map. Use for vol-regime / rolling-correlation work.

**`NEWS_SENTIMENT`** — news + sentiment. Each article carries `overall_sentiment_score`
/`_label` AND a `ticker_sentiment[]` array with per-ticker `ticker_sentiment_score`
/`_label` and `relevance_score`, plus `topics[]`. Label scale (from the response's
own `sentiment_score_definition`): ≤−0.35 Bearish · −0.35..−0.15 Somewhat-Bearish ·
−0.15..0.15 Neutral · 0.15..0.35 Somewhat-Bullish · ≥0.35 Bullish. Read the
symbol's sentiment from `ticker_sentiment[]`, not just the article-level score.
GOTCHAS: `limit` does NOT reliably shrink the feed (asked 10, got 50) and a default
pull (~50 articles ≈ 32k+ tokens) overflows the tool limit → set
`return_full_data=true` so the client offloads to a file, then jq/extract; and/or
narrow with `tickers` + `time_from`/`time_to` + `topics` + `sort=RELEVANCE`.

**Alt-data / screening (verified):**
- `COMPANY_OVERVIEW` — one compact record with 50+ fundamentals AND a sell-side
  block: `AnalystTargetPrice`, `AnalystRatingStrongBuy/Buy/Hold/Sell/StrongSell`,
  `Beta`, `50/200DayMovingAverage`, `52WeekHigh/Low`, `PercentInsiders/Institutions`.
- `INSIDER_TRANSACTIONS {symbol}` — SEC insider trades; `acquisition_or_disposal`
  A/D, shares, price. `from_date` filters. NB: `share_price` `0.0` = grant/gift/
  non-market line, not a real trade price.
- `TOP_GAINERS_LOSERS` — top-20 gainers/losers/most-active. Takes `entitlement`;
  without it = last session. Movers skew to microcaps/warrants — filter by
  `price`/`volume` for meaningful names.
- `ETF_PROFILE {symbol}` — expense ratio, sector weights, AND the FULL holdings
  list (SPY ≈ 500 rows) → sizable payload; expect a file offload for broad ETFs.
- `EARNINGS_CALENDAR`/`IPO_CALENDAR` — forward calendars (`horizon=3/6/12month`);
  `EARNINGS`/`EARNINGS_ESTIMATES` for history/estimates. NB: `EARNINGS_ESTIMATES`
  can return `{"estimates":[]}` for a covered name (e.g. NVDA) — plan/coverage
  gated; verify per-symbol, don't assume populated.

## Commodities (Alpha Vantage side)

Benchmark/global spot price series — NOT futures, no curve, no options. Functions:
`WTI`, `BRENT`, `NATURAL_GAS` (daily/weekly/monthly), `COPPER`, `ALUMINUM`,
`WHEAT`, `CORN`, `COTTON`, `SUGAR`, `COFFEE`, `ALL_COMMODITIES`,
`GOLD_SILVER_SPOT` (live spot) / `GOLD_SILVER_HISTORY`. None take `entitlement`.
Use for benchmark price levels/series — including ICE softs (coffee/sugar/cotton)
that Databento GLBX.MDP3 lacks — not for exchange microstructure.

## Data-shape note (Databento vs Alpha Vantage)

Don't normalize one provider into the other.
- Databento: record schemas (`trades`, `mbp-1`, `mbp-10`, `mbo`, `tbbo`, `bbo-*`,
  `ohlcv-*`, `definition`, `statistics`), UTC timestamps, instrument identifiers,
  CSV/DBN-style tabular; symbol models `raw_symbol`/`instrument_id`/`continuous`/
  parent; batch export files.
- Alpha Vantage: function-specific JSON/CSV with varying field names/nesting;
  public tickers / FX & crypto pairs / function params; adjusted vs raw equity
  series; per-function freshness & rate limits.
When comparing across sources, call out timestamp/timezone, adjusted vs raw, symbol
identity, depth/granularity, freshness, and rate-limit differences.

## Examples

- "INOD live data" → `GLOBAL_QUOTE` with `entitlement=realtime`.
- "AAPL 5-minute bars today" → `TIME_SERIES_INTRADAY` interval=5min,
  `entitlement=realtime`.
- "EUR/USD now" → `CURRENCY_EXCHANGE_RATE` or `FX_INTRADAY` (no entitlement).
  "BTC price" → `CURRENCY_EXCHANGE_RATE` (no entitlement).
- "MSFT fundamentals" → `COMPANY_OVERVIEW`. "US CPI" → `CPI` (no entitlement).
- "AAPL options chain now" → `REALTIME_OPTIONS` with `require_greeks=true` (real
  live chain + greeks; RTH 09:30-16:00 ET else last session). `HISTORICAL_OPTIONS`
  for past dates. Options on futures (e.g. "ES options") go to Databento.
