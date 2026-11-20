---
name: market-data
description: Understand or analyze any specific company, stock, ETF, asset, security, option chain/series, futures, or portfolio — MATCH ON INTENT, whatever the phrasing (a terse "analyze X", a bare ticker, or a long, indirect, multi-part request that boils down to sizing up an instrument our data providers cover). The DEFAULT skill for that intent across Databento, Alpha Vantage, and MOEX; it runs bundled Python scripts, renders the matching HTML dashboard, AND answers whatever specific question the request carries in the written read. Sources CME contract lookups and quotes from Marengo MCP and analytical dashboard numbers from those scripts — never a web search or subscription connectors. Use instead of hand-writing a report.
version: 1.168.0
triggers:
  - "databento"
  - "cme market data"
  - "standard cme"
  - "futures quote"
  - "futures historical bars"
  - "cme options"
  - "symbol resolution"
  - "alpha vantage"
  - "stock quote"
  - "stock price"
  - "live data"
  - "realtime quote"
  - "intraday bars"
  - "equity options"
  - "forex"
  - "crypto price"
  - "company fundamentals"
  - "analyze company"
  - "analyze stock"
  - "analyze ticker"
  - "analyze company results"
  - "company analysis"
  - "stock analysis"
  - "how is this company doing"
  - "проанализируй компанию"
  - "проанализируй акции"
  - "анализ компании"
  - "разбор компании"
  - "compare companies"
  - "compare stocks"
  - "portfolio risk"
  - "portfolio analysis"
  - "sharpe ratio"
  - "correlation matrix"
  - "options chain"
  - "implied volatility"
  - "volatility smile"
  - "max pain"
  - "put call ratio"
  - "technical indicator"
  - "market data"
  - "moex"
  - "moscow exchange"
  - "russian stocks"
  - "imoex"
---

# Market Data Skill (router)

Routes market-data requests across two configured MCP servers — **Databento**
(futures & options on futures) and **Alpha Vantage** (everything else) — plus a
third, **keyless** source (**MOEX ISS**) for Russian markets that neither
connector covers. This file is the thin router: it decides the source, holds the
shared options model, and the output rules. The deeper provider-specific detail
lives in bundled files:

## CME contract lookup and quotes (desktop and cloud)

For these intents use Marengo by default, including on a machine with the old local Databento connector. This route takes precedence over script/dashboard instructions below. Consult `marengo_platform_catalog` and describe the advertised action.

- Known CME contract: request one quote with `marengo_databento_live_get_futures_quote`. For a directory ID pass the ID as a string with `stype_in=instrument_id`.
- Product name or option criteria: use `marengo_databento_instruments_search`. Pass product, call/put, strike and expiration when specified. Read subsequent result pages via platform cache using the returned cursor. Follow up on ambiguous families or multiple matching series. Use a unique matching ID; describe nearest as nearest observed when coverage is partial.
- Streaming best bid/ask: use `marengo_databento_live_stream_bba` start/read/stop for that ID. Advance after to next_cursor, report missed_events, distinguish an empty read from terminal feed status, and stop the handle when finished.
- On missing capability, OAuth, entitlement or provider failure report that condition. On confirmation_required direct the owner to the portal. Use the legacy local route for these operations only if explicitly selected. Never substitute settlement, Alpha Vantage, a dashboard or a local key for a missing CME quote.
- ES futures options use Marengo. SPY equity options and AAPL quotes use the existing Alpha Vantage connector. Current MOEX quotes prefer the BCS MCP. The existing full-chain and analytical dashboard workflows retain their own data sources.

## Current MOEX quotes

For a current MOEX price or bid/ask (e.g. SBER, GAZP or a listed FORTS contract), use the authenticated BCS Trade API MCP first. Discover its advertised read-only market-data tools, match the instrument and board, and report the quote type, source and timestamp. Use only market-data reads. A last trade is not a two-sided quote. Do not present stale, delayed or unverified data as realtime. If BCS is unavailable, say why and label any available ISS snapshot as delayed. If the user explicitly requests realtime only, return unavailable. Keep ISS for history, reference data, bond analytics and the existing dashboards; distinguish their timestamps from a separate BCS quote.

## Python launcher (all hosts)

Every command below uses `<python>` as a placeholder: substitute `py -3 -X utf8` on native Windows
PowerShell, and `python3` on macOS, Linux, WSL, Claude Code, or Cowork. Never type the placeholder
literally. Resolve this once before running the first command; do not assume that native Windows
provides a `python3` executable.

## OpenAI static v1 policy (all instruments)

When `market-data-host.json` says `{"host":"openai"}`, this policy overrides every later display
default for companies, options, futures, charts, portfolios, bonds, rates, and comparisons:

- Never use `--watch` on an OpenAI host. Render one self-contained file with `--html` (or the
  script's other one-shot output explicitly requested by the user), then return a prominent local
  file link. Do not start an SSE or loopback server.
- Never use the Preview Pane on an OpenAI host. The OpenAI desktop/Work contract is the file link;
  the host may open it in the right panel after the user clicks, but automatic opening is not
  guaranteed.
- Claude/Cowork behavior is unchanged: without the OpenAI marker, keep the existing live
  `--watch`, Preview Pane, `present_files`, and embedded-sibling rules described later.

## OpenAI fast company-analysis lane

When `market-data-host.json` says `{"host":"openai"}`, the user asks to analyze a
US or MOEX/Russian company, and the current request is not an
explicit `financial-deep-analysis:*` invocation,
this short lane is MANDATORY and takes precedence over later presentation detail. In an explicit
deep-analysis task, market-data is evidence-only: use JSON/text modes and do not render HTML:

1. Run exactly ONE company producer command, producing the rich dashboard and its text twin
   from the same provider gather: `fundamentals.py` for US companies, or
   `moex_fundamentals.py` for MOEX/Russian companies:
   `<python> "<skill-dir>/scripts/fundamentals.py" SYM --html /absolute/output.html --summary-out /absolute/output.txt`, or
   `<python> "<skill-dir>/scripts/moex_fundamentals.py" SYM --html /absolute/output.html --summary-out /absolute/output.txt`.
   Do not run a second text/JSON command and do not scrape the large HTML for numbers.
2. The instant that command returns, send a commentary update immediately with one localized,
   large heading-link before the written analysis or any MCP App call. Use the current chat language:

   English: `## [Open interactive dashboard — <instrument> →](</absolute/output.html>)`

   Russian: `## [Открыть интерактивный дашборд — <instrument> →](</absolute/output.html>)`

   The entire H2 heading is the file link; do not emit a separate heading above it. Never put `**`
   inside a local file-link label: the Codex renderer can expose the asterisks literally and leave
   the link at body-text size.
3. Read the small `--summary-out` file and write the 3–6 sentence interpretation. Repeat the same
   large link at the top of the final answer. A file link is the reliable default; the OpenAI host
   may open it in the right panel, but never promise that it opened automatically.
4. Do not call `render_market_data_dashboard` on the default path. The nested-frame prototype is
   disabled in production because ChatGPT blocks nested frames unless the component declares and
   passes review for `frameDomains`. It is exposed only for local developer experiments when the MCP
   server starts with `MARKET_DATA_EXPERIMENTAL_MCP_DASHBOARD=1`; normal users always receive the
   self-contained HTML file link.
5. After the written read, render exactly ONE deep-analysis block, as the last tool call. For every
   US or MOEX/Russian company analysis, `initiating-coverage` is ALWAYS the primary action and
   `secondary` is ALWAYS present:
   use `earnings-analysis` if the latest report was published within the last 30 calendar days;
   otherwise use `earnings-preview` when the next report is due within the next 45 calendar days;
   otherwise offer `earnings-analysis` for the latest published quarter. MOEX keyless data commonly
   has no dependable next-report date; if dates are unavailable, fall back to that latest-quarter
   analysis rather than omitting the second action. Pass
   `defaultLanguage` as `ru` or `en` to match the current chat language; the shared selector lets the
   user change it before either click. Use this canonical argument shape with bare skill IDs:

   ```json
   {
     "primary": {"skill": "initiating-coverage", "subject": "Company (SYM)", "label": "Full coverage"},
     "secondary": {"skill": "earnings-analysis", "label": "Analyze latest earnings"},
     "defaultLanguage": "en"
   }
   ```

   Do not send `prompt`; the MCP server creates the safe native follow-up request.
   Never make two `render_deep_analysis_offer` calls: one MCP App block contains both clickable
   actions and avoids the host restore race. A slow or failed block must never delay the dashboard
   link.

This ordering is OpenAI-only. Claude/Cowork keeps its existing Preview Pane, `present_files`, and
`offer.py` path unchanged.

- `SETUP.md` + `scripts/setup_check.py` — first-run onboarding for a new machine
  (Python, API keys in `.env`, the sandbox/egress question, macOS+Windows). On a
  fresh install, run `<python> "<skill-dir>/scripts/setup_check.py"` (doctor) and follow any
  non-OK line; point the user to `SETUP.md` when keys/egress aren't configured.
- `references/databento.md` — Databento operating rules, the futures-options
  playbook (Branch A), commodities, symbology, batch, provider facts.
- `references/alpha-vantage.md` — entitlement defaults, the equity/ETF-options
  playbook (Branch B), full capability coverage, commodities, examples.
- `scripts/greeks.py` — Black-Scholes / Black-76 pricer + IV solver + greeks
  (`price` / `iv` / `strike` CLI). Used for Databento futures-options greeks; AV
  equity options already return native greeks.
- `scripts/chart.py` + `references/charting.md` — plot a historical series: the
  script fetches, resamples, and renders an SVG in one command (data stays out of
  context); the reference has usage, sources, and the display recipe. Always run
  the BUNDLED script by its path in this skill's folder (cwd is usually the user's
  project, not the skill root, so a bare `scripts/chart.py` won't resolve) — never
  reimplement charting inline.
- `scripts/fundamentals.py` + `references/fundamentals.md` — analyze a company: one
  command fetches ~13 Alpha Vantage endpoints concurrently (gzip; bypassing the statement/
  news MCP-overflow), and emits a text summary (default), a compact `--json` for an
  inline widget, a **TABBED** responsive dashboard (`--html`, v1.84.0: [Overview | Financials |
  Ownership | Estimates & News] on the generic sections engine — self-verify by data+console;
  screenshots only in DEV sessions or on anomaly, per `references/fundamentals.md`), or a
  fixed landscape dashboard SVG (`--svg`/`--png`/`--open`). Handles
  an **ETF/fund** (empty OVERVIEW → the `ETF_PROFILE` branch, flat page; v1.83.0), shows the
  **next-earnings date** (`EARNINGS_CALENDAR`, v1.83.0), and an **Ownership tab** (v1.84.0):
  insider flow (heuristic classes — no Form-4 codes; buys are the signal, sell-to-cover flagged
  `◦`) + 13F holders (net **ex-new** headline — the filer-rename artifact is flagged, count-vs-
  shares divergence shown, ◇ index giants, stale filings greyed), plus (v1.87.0) an **options-vol snapshot** on Overview (30d IV · IV rank · VRP from the LOCAL opthist store, zero AV calls), a **sector-context tile** (v1.148: `Sector · <name> (<SPDR>)` = the company's ~3-month return MINUS its own sector SPDR's, both legs in the sub — "company story or sector story?"; the SPDR series is DAY-CACHED per sector, so the first company of a sector that day pays one call and everyone else that day reads disk; unmapped sector / failed pull → the tile is simply absent, and `MARKET_DATA_SECTOR=0` disables it) and **cross-link chips** (render the fundamentals + `<sym>_options.html` into one dir — each page links its sibling, including when the fundamentals file has a descriptive OpenAI name such as `texas-instruments-txn-dashboard.html`; the options sibling is ALWAYS the `--full` tabbed page, never a single-series chain; since v1.94.0 a MISSING sibling is background-rendered automatically on live renders and its chip self-reveals when the file lands — no manual re-render; `--from-dir` never spawns; **v1.127 Cowork: the sibling is EMBEDDED — each file carries BOTH boards and the chip SWAPS them client-side (instant, zero navigation — the viewer cannot navigate between files), so present either file and the pair just works**). Run the BUNDLED script by
  absolute path; never pull statements through the MCP and hand-`jq` them. The reference holds
  the display rules and the data quirks.
- `scripts/compare.py` + `references/compare.md` — compare 2-8 COMPANIES side by side
  (v1.86.0): "сравни X и Y / X vs Y / который дешевле / кто растёт быстрее". LIGHT by
  default (K×OVERVIEW + ONE bulk quote + K×monthly ≈ 2K+1 calls): spread cards, a
  best-in-row ▲ metrics table, rebased-100 performance, grouped margins, and the
  growth-vs-multiple bubble (cheap-FOR-ITS-GROWTH, not cheap). `--deep` adds the
  statement rows (FCF/dilution/net cash/F-Score/Altman, ~13 calls/name); `--cache DIR`
  reuses fundamentals --save-raw payloads (cached names join OFFLINE). An ALL-FUND set
  routes to the ETF comparison (v1.101.0: "сравни MSOS и MJ" — expense/AUM/liquidity/yield/
  drawdown/returns/store-vol best-in-row + rebased perf + pairwise holdings OVERLAP with
  honest list-coverage disclosure; --deep is company-only and warned-ignored); FLOWS/AUM-over-time are in NO provider — for crypto ETFs read farside.co.uk/btc|eth in the built-in browser (Cloudflare blocks curl; see references/compare.md); a MIXED
  company+fund set is refused loud; peers are USER-named (propose + confirm, never auto-pick). Honesty:
  $-level rows rank only within one currency; per-column as-of stamps ("NOT simultaneous").
  Finish with the comparative read (the reference's checklist). Run by absolute path.
- `scripts/portfolio.py` + `references/portfolio.md` — analyze a portfolio's risk/return:
  one command fetches the split/dividend-adjusted daily series for every holding + the
  benchmark concurrently and computes (locally) return/vol/Sharpe/max-drawdown, the
  correlation matrix, per-name risk contribution, and beta. Input is a holdings CSV
  (`--portfolio`, auto-detecting shares/weight/value) or inline `--holdings`. Same output
  modes as fundamentals (text/`--json`/`--svg`/`--html`/`--open`/`--from-dir`). Run the
  BUNDLED script by absolute path; never pull N price histories through the MCP. The
  holdings file is personal — keep it out of git.
- `scripts/options.py` + `references/options.md` — analyze an EQUITY/ETF option chain:
  one command fetches the full `REALTIME_OPTIONS` chain (which overflows the MCP token
  limit) + a spot quote over HTTP, filters to one expiration, and computes the IV smile,
  ATM IV, the **expected move** (±$/±% by expiry, v1.82.0), 25-delta skew, put/call ratio,
  max pain, the OI-by-strike profile (native greeks), the per-strike **bid/ask spread**
  (OTM leg, % of mid — the tradeability column + median, v1.82.0), a **smile-relative rich/cheap**
  column (IV vs a local smile fit; the AV FMV endpoint is unentitled, v1.83.0), and — on
  `--history`/`--full` — a **VRP** read (IV vs 20d realized vol + a realized overlay, v1.83.0). `options.py TICKER [--expiry YYYY-MM-DD]`; same output modes (text/`--json`/
  `--svg`/`--html`/`--open`/`--from-dir`), plus `--term` — the chosen-series chain + cross-expiration
  analytics (ATM-IV/skew curve, moneyness×expiry IV surface, **OI surface** [v1.74.0], series table,
  Term-slope card) from the ONE REALTIME_OPTIONS payload (zero extra calls), with the same v1.72.0
  **expiration switcher** as futures (a `<select>` swaps which expiration the chain charts show
  client-side; every series pre-built; the general page leads with permanent cross-series tiles; OI bars
  are put=orange/call=blue to match the smile), plus
  `--watch [N]` — a LIVE smile page served by the SAME
  `dashboard.py` engine as futures (SSE per-block deltas, no reload, smile zoom buttons,
  ATM-gradient table full-width at the bottom, futures-style LIVE badge with the real poll age — AV options are 2s-polled (dashboard.POLL_DEFAULT_S)
  snapshots (user-accepted: frequent polls, not a tick socket); keep-alive+gzip under the hood);
  plus **`--history`** — the RETROSPECTIVE vol-history dashboard (IV rank/percentile, const-maturity IV
  history, vol cone, 25Δ-skew & PCR history, term-through-time heatmap, earnings overlay) from a
  `HISTORICAL_OPTIONS` settlement backfill (cached in the `opthist` store; `--years N`, default 2 — so the
  window control's "All" spans >1Y); and **`--full`** (v1.77.0) — the COMBINED tabbed page
  **[Overview · Chain · Term · History]** (client-swapped tabs): Overview = the cheap/rich synthesis
  (IV-rank tiles + a by-tenor 30/90d IV-range rank track + CM-IV history); Chain = the per-expiration drill
  (smile / OI / GEX / ladder) with an expiration `<select>` listing EVERY expiration; Term = the cross-expiration
  surface (sqrt-time ATM-IV/skew curve, IV+OI surfaces, all-expirations series table); History = the
  `--history` dashboard. Overview + History are cache-first from the `opthist` store — **a cold name shows
  "warming" placeholders and is QUEUED for the nightly prewarm** (no in-run backfill); tell the user to run
  `options.py SYM --history` to fill them now. The writable store resolves via a ladder (v1.93.0):
  `MARKET_DATA_STORE` env → `<skill-root>/store/options/` when present → `~/.cache/market-data/options`.
  A private OpenAI package also carries an immutable `store.tgz` seed (v1.158): `load_store` reads
  the requested ticker directly from that archive when the writable layer misses — no unpack and no
  writable skill/home assumption.
  In Cowork the store RIDES THE SKILL DIR (the sandbox home is throwaway — check `<skill>/store/options/`
  before declaring a name cold; a missing store there means the host hasn't synced, NOT that the ticker
  was never analyzed). Chain + Term come from ONE live chain pull. EQUITY/ETF
  ONLY — futures options go to `scripts/futures_options.py` (Branch A). Run the
  BUNDLED script by absolute path.
- `scripts/futures_options.py` + `references/databento.md` — analyze a FUTURES option chain
  (options on ES/CL/NG/GC/PA/HG/6E…): ONE deterministic command resolves the option code
  (PA→PAO, CL→LO, HG→HXE…), picks ONE series — the nearest quarterly by REAL expiration read
  from the live Databento definition listing (never a computed symbol string, so it survives the
  1-digit-2026/2-digit-2027 year width and metals' expire-month-before roll) — pulls that series'
  FULL tradeable ladder (every strike with open interest) from Databento Historical, and computes
  the OTM IV smile (Black-76 via `scripts/greeks.py`; STRICTLY the OTM leg — the noisy ITM leg is
  never substituted; a quote-less strike bracketed by real quotes is linearly interpolated and marked
  `~`/grey, an unbracketed edge gap stays n/a), ATM IV, the **expected move** (± points/±%,
  v1.82.0), 25Δ skew, put/call ratio, max pain,
  per-strike **Δ (OTM)** + **Γ**, **ATM greeks** (Γ/vega/Θ), OI by strike, and the **dealer GEX
  profile** (Ф5, v1.69.0): Γ×netOI per strike in Δ-contracts/pt (naive long-calls/short-puts dealer
  assumption, stated in the panel info), Net-GEX + sticky-strike **Γ flip** cards, sign-coloured
  bars with the flip vline; **--full** (v1.114.0) = the COMBINED tabbed page [Overview · Chain · Term · History] — Chain+Term are the --term build, Overview/History are the retrospective (IV rank / vol cone / skew & PCR history) from the **futhist settlement store** (`scripts/futhist.py ROOT --backfill --years 1` fills it; Databento statistics reduced through the same build_chain, Black-76 computed IV; a COLD root shows honest 'warming' tabs naming the command); **--term** (v1.71.0) = the SAME chain page (default or --expiry series) + cross-expiration analytics appended: ATM-IV/skew term curve, moneyness×expiry IV surface (seq-ramp heatmap), series table, Term-slope card — PER-SERIES forwards (each GLBX series prices off its own month), same parent-level pulls as term-only (no extra cost); an **expiration switcher** (v1.72.0) on the `--html` page pre-builds EVERY series' chain (smile/OI/GEX/table + cards, greeks computed once in Python) so a `<select>` swaps which expiration the chain charts show CLIENT-SIDE with zero extra network — the term panels below stay the fixed cross-expiration overview (v1.74.0: the GENERAL `--term` page is titled by all-series ("term structure · N series"), leads with PERMANENT cross-series tiles (Series · Front ATM IV · Term slope) while the per-series greeks tiles switch, and adds an **OI-surface heatmap** (total call+put OI by moneyness×expiry, the OI twin of the IV surface) below the IV surface; the chain label is honest per strike-scope — "full tradeable ladder" only on the direct pull, "±N strikes around ATM" on the connector /smile window, "near-money grid" on the connector /term coarsened pull; OI bars are coloured put=orange/call=blue to match the smile, GEX stays sign-coloured); SOURCE connector-first — the connector's /term/:root.json day-cache makes repeat runs sub-second (first run of a trading day = the slow Databento side), --source databento = direct (v1.128/9: gzip'd, statistics id-scoped to the kept series, ONE batched settle pull, a DATE-level same-day day-cache + the shipped defs SNAPSHOT (store/futures/defs, TTL 5d, --fresh-defs forces live, futhist prewarm keeps it fresh) — measured CL: snapshot-less cold 1:40-2:27 → fresh-VM-with-zip ~1:01 → same-day repeat ~18s; MARKET_DATA_FUT_DAYCACHE / MARKET_DATA_FUT_DEFS_SNAPSHOT override/disable; v1.166.0 — the snapshot, NOT the history depth, is what makes a commodity page fast: measured 2026-08-07 same-root A/B, ZL --full 200s with NO snapshot vs 114s with one (-43%, day-cache off both times) while its History tab was EMPTY either way; PL (snapshot + vol store) 44s cold-day / 16s same-day. So the nightly prewarm now runs TWO tiers — the 21-root accrete tier (a year of statistics each) plus a DEFS-ONLY tier over every other OPTION_ROOTS root (ZL/ZM/KE/6J/TN/livestock/micros; snapshot only, best-effort, never in the accrete result or the day stamp; `futhist.py --defs-only` runs it alone, MARKET_DATA_FUT_DEFS_PREWARM narrows it, empty turns it off)); one-shot (a snapshot-less big root's first run of a day is minutes on Databento's side — stderr says so); an **About strip** (curated contract specs + the definition's own underlying) sits between the cards and the grid on every futures/equity chain page; the chain table is heat-mapped
  around the ATM strike. `futures_options.py ROOT [--expiry YYYY-MM-DD]`; text (default) / `--json` /
  `--html` (self-contained dashboard) / `--watch [N]` — a LIVE self-updating side panel. `--source
  connector` (default auto) polls the local Databento connector (keyless) for a real ● LIVE tick badge
  that never claims live when the feed is stale; `--source databento` is the settlement fallback
  (metals have no live ticks). Argument is the futures ROOT only — never an option code (PAO) or a
  dated contract (PAOU6/ESU6). FIRST QUESTION (v1.73.0, connector-gated since v1.128): a futures root
  has MANY expirations (PA has 14), so WITH the local connector running, before the heavy chain pull run
  `--list-series` — the connector's keyless `/series` listing (every
  expiration with DTE + strike count, nearest marked, instant from the long-lived defs catalog;
  `--liquidity` also ranks by open interest) — present it and ASK which expiration to analyze, offering
  the nearest as the default (or `--term` for the cross-expiration view of all at once). WITHOUT the
  connector (cloud Cowork — no localhost connector there, ever): for a general "analyze the options on
  X" skip the pre-listing and go STRAIGHT to `--full` — it picks the nearest series itself and its Term
  tab/Series table show every expiration; `--list-expiries` is a whole-root defs pull (~40-60s+ on big
  roots, day-cached since v1.128) — reserve it for an explicit "what expirations exist?" ask. Pre-check with
  `--list-expiries` (Databento-direct definition listing, needs the key) before
  committing to `--expiry` (nearest listed series within ±7 days, else an error LISTING the real
  expirations); an unmapped root (e.g. Brent BZ→BZO) takes `--option-root CODE`; the chosen series is
  always echoed to stderr, and a fallback forward (continuous front / mid-strike) is marked in the
  subtitle. FUTURES ONLY — equity/ETF options stay on `scripts/options.py`
  (Branch B; it REFUSES known futures roots unless `--equity` says the equity namesake is wanted,
  and has its own `--list-expiries`). Run the BUNDLED script by absolute path.
- `scripts/moex_options.py` + `references/moex.md` — analyze a RUSSIAN EQUITY option chain (MOEX
  FORTS, premium-on-spot: SBER, GAZP, LKOH, ROSN, ALRS, MTSS, TATN, MAGN… the ~41 stock options).
  Keyless public ISS `optionboard` — the ONLY place MOEX publishes per-contract IV (`VOLAT`, exchange's
  own); the skill computes greeks (Black-Scholes on the SHARE via `scripts/greeks.py`, RUSFAR risk-free
  rate). Same shape as the equity/futures chain pages on the SAME `dashboard.py` engine: OTM-composite
  smile, OI by strike, dealer GEX (Γ×netOI, naive assumption stated), ATM IV, expected move, 25Δ-skew
  proxy, PCR, max pain, per-strike Δ; **--term** = ATM-IV/skew term curve + moneyness×expiry IV surface
  + series table + expiration switcher (every listed series pulled — thin, cached within the run);
  **--full** = the COMBINED tabbed page [Overview · Chain · Term · History] — Overview live-synthesized
  (a MOEX FORTS session tile + front ATM/skew + term slope + PCR), History a 'warming' placeholder (the
  MOEX vol-history store is a later slice). `moex_options.py SYM [--expiry YYYY-MM-DD] [--full|--term]`;
  text/`--json`/`--html`/`--from-dir` (a saved raw ISS `optionboard.json`) / `--rate` (override RUSFAR).
  HONESTY (hard): public ISS is DELAYED ~15 min — the header/lead/footer say so, NEVER "live"; RUB (₽)
  throughout; IV is the exchange's, greeks are COMPUTED; a no-quote leg is n/a, never a fabricated 0.
  EQUITY MOEX options only — futures-margined MOEX options (Si/CNY/RTS, Black-76) are a later slice.
  Run the BUNDLED script by absolute path.
- `scripts/moex_fundamentals.py` + `references/moex.md` — analyze a RUSSIAN COMPANY's FUNDAMENTALS
  (the RU counterpart to `fundamentals.py`, which has NO RU coverage — Alpha Vantage carries none). A
  numbers + a full **DASHBOARD** (v1.149): structured quarterly + annual revenue/EBITDA/net income +
  P/E · EV/EBITDA · P/S · ROE + dividends. Sources, fallback order, never hard-fails: **smart-lab.ru**
  (keyless HTML, PRIMARY — a second-hand compilation of the issuers' МСФО/РСБУ reports, ₽bn-rounded,
  release-lagged; sector-aware — banks (SBER/VTBR) get their OWN field set: NIM, capital adequacy, cost
  of risk, NPL, loan book, funding, never a fabricated EBITDA) → **Yahoo quoteSummary/SYM.ME** (keyless
  TTM cross-check + the business description for the page lead, currency-tagged — GMKN reports in USD,
  never mixed into the ₽ series) → **MOEX ISS** (price/cap/shares/dividends/identity). `moex_fundamentals.py
  SYM [--html out.html]`; text/`--json`/`--save-raw DIR`/`--from-dir DIR`/`--emit-spec`. `--html` renders
  the tabbed board [Overview · Financials · Dividends] on the same `dashboard.py` engine as every other
  page: kline from the FULL ISS listing history, 52w range, vs-200d MA, drawdown, beta vs IMOEX, the
  options-vol strip from the local MOEX vol store (`moexopthist`, warm for 26 liquid names), statement
  combos, a multiples trend, the ISS dividend history and a capital-returned panel. HONESTY (hard):
  smart-lab is NEVER "live"/"primary filing"; a hole is None, never a fabricated 0 (a dividend ZERO is
  only ever printed for a COMPLETED year — the current year stays empty because a register may not have
  closed yet); statements are ~5 quarters / 5 years deep on the free tier and the page SAYS so; a total
  miss returns an honest `unavailable` record. NO analyst/forward surface — the FORWARD view (upcoming
  date/guidance/consensus) is NOT in any keyless source, so the `earnings-preview` offer web-sources that
  while pulling the HISTORY from our `--json`. Run the BUNDLED script by absolute path.
- `scripts/dashboard.py` — the centralized dashboard renderer + live-push engine BEHIND the
  analysis scripts (not called directly for analysis): a declarative spec `{title, subtitle,
  cards[], panels[{type,data}]}` → one self-contained responsive HTML page (inlined Chart.js,
  JS-authoritative adaptive grid) and, via `serve()`/`--watch`, an SSE server pushing per-block
  deltas to a self-updating page with a liveness badge (tick/delayed/settlement). One server per
  dashboard, loopback-only. Producers (options/futures_options) build a spec and hand it here.
- `references/moex.md` — Moscow Exchange via the keyless public ISS API (indices,
  shares, bonds, RUB FX, FORTS futures/options): the source for Russian markets,
  which neither MCP connector covers. Charted via `scripts/chart.py --source moex`;
  Russian EQUITY OPTION CHAINS analyzed via `scripts/moex_options.py SYM [--full]`.

- `scripts/treasury.py` + `references/treasury.md` — the **US TREASURY CURVE** (v1.154). Alpha Vantage `TREASURY_YIELD` +
  `FEDERAL_FUNDS_RATE` + `CPI`, day-cached (8 calls cold, free the rest of the day; AV's free tier
  is 25/day). Two tabs: **Обзор** (the six constant-maturity tenors today vs 1M/1Y ago — each
  historical leg walking back to a real session and reporting the DATE IT GOT — plus the 10y−2y
  slope with an inversion count and the effective fed funds rate) and **История** (the tenors over
  N years with fed funds overlaid, and the REALIZED real 10y = nominal − YoY CPI).
  HONESTY: AV **silently answers ANY unsupported `maturity` with the 10-YEAR series** — `1year`,
  `6month`, `20year`, even `banana`, all HTTP 200 with 16 843 rows — so every leg re-checks the
  `name` it got back and a mismatch DROPS the tenor with the reason on the page; `value: "."`
  (719 of 16 843 rows) is a hole, never 0.0; the page is dated by its DATA (~1 business day behind
  the clock); only six tenors exist, nothing is interpolated into a knot AV does not publish; and
  the real rate is REALIZED, never a breakeven — **AV publishes no TIPS series**. There is likewise
  **no per-issue/CUSIP data anywhere in AV**, so a US analogue of the MOEX single-bond page is
  impossible; do not go looking for one.
- `scripts/moex_bonds.py` + `references/moex.md` — analyze a RUSSIAN BOND, or the RU bond MARKET
  (v1.150). Keyless MOEX ISS, which for bonds is RICHER than anything the skill gets for equities:
  yield, duration, **Z-spread and G-spread** are published first-party per instrument, and the
  zero-coupon curve (КБД / G-curve) comes as ready points. TWO pages on the same `dashboard.py`
  engine: `moex_bonds.py <SECID> --html out.html` = the ISSUE board [Обзор · Денежный поток ·
  История] (yield WITH what it is computed to, duration, spreads, coupon/НКД/оферта, the bond's own
  point on the sovereign curve, the full coupon+amortisation schedule from `bondization`, price-and-
  yield history); `moex_bonds.py --market --html out.html` = the MARKET board (the G-curve today vs
  1M/1Y ago, RGBITR, an OFZ duration×yield map, a turnover-filtered corporate table); and
  `moex_bonds.py --issuer NAME --html out.html` = the ISSUER-GROUP board (v1.153) — all of one
  issuer's bonds as ONE object: median yield and the duration/spread RANGE (never a fake "average
  bond"), its own credit curve, the combined payment calendar (where the issuer's redemption walls
  are — the thing a single-issue page cannot show), and a table of the issues. Each row LINKS to
  that issue's own dashboard: a static page cannot render on click, so the member pages are
  PRE-RENDERED beside the group page (cheap — the 12 monthly ZCYC slices that dominate an issue
  render are cached globally and already warm, so 14 issues cost ~25 s in total, not 14×18 s);
  `--no-members` skips that, and an unrendered link degrades to copying an ask for the chat.
  **Present the member files too**, not only the group page — a viewer serves only what was
  presented, and the links probe before navigating. Also
  text/`--json`/`--save-raw DIR`/`--from-dir DIR`/`--emit-spec`/`--min-turnover`.
  HONESTY (hard — HALF the corporate universe is not a plain fixed-coupon bond, and ISS publishes
  numbers for all of it anyway): a yield is NEVER shown without its basis (`YIELDDATETYPE` —
  ФСК РС-22 prints **-28.891% to an offer four days out**, which as "yield to maturity" is wrong by
  tens of points); **FLOATERS GET NO YTM AND NO SPREAD** (ЯНДЕКС1Р1 publishes -1.4007% and -1522bp —
  both meaningless, both suppressed, with the reason on the card); a foreign-currency issue's yield
  never touches the ₽ curve; a bond inside ~30 days of its yield date gets no spread and no point on
  the curve (annualisation noise, not a credit premium); a coupon of 0 with no rate set is a HOLE,
  not "₽0.00"; universe panels state their turnover filter; and there are **NO CREDIT RATINGS** —
  they are not in ISS and the agencies are bot-walled, so `LISTLEVEL` and the rating-bucket indices
  are shown as surrogates and labelled as such, never as "this bond is rated A".
  **DEPTH (v1.152.1)** — the history is the issue's WHOLE life (measured: ОФЗ 26238 = 1356 rows in
  14 pages, 1.9 s, day-cached — the old 400-day window was our limit, not the exchange's), the
  yield percentile NAMES its window and reports 1y and 3y side by side (ОФЗ 26238: 92nd vs 84th —
  a three-year window contains a different rate regime, so an unnamed number would describe the
  regime and not the bond), and the ZCYC slices are QUARTERLY, covering three years for the same
  ~12 s twelve monthly ones cost. NB ISS advertises the curve archive from 2014-01-06 but 2016 and
  2018 come back EMPTY — real data starts around 2020; check depth by fact, not by the claim.
  **ANALYTICS (v1.151)** — the things ISS does NOT compute, each gated on a check it can fail:
  **modified duration + CONVEXITY + a rate-scenario ladder** (−300…+300 bp → price and 1-year total
  return), published ONLY when discounting the real cash-flow schedule at the exchange's own yield
  reproduces the market dirty price within 0.5% — that one oracle refuses yield-to-offer issues,
  floaters, structured notes and linkers with no blacklist; a **FLOATER's coupon rate recovered from
  the ₽ payments** (ISS sends valueprc empty, but value is there) overlaid on RUSFAR, which turns the
  emptiest page in the module into the most informative; a **yield percentile** inside the bond's own
  window (the IV-rank analogue), suppressed for any bond whose yield we refuse to publish; and on the
  market page a **breakeven-inflation curve** (nominal ОФЗ minus ОФЗ-ИН real yields at the same
  duration — live 5.4% at 1.5y rising to 8.6% at 6.2y), with the ~3-month indexation lag stated.
  **v1.152** — the market rides as a fourth TAB («Рынок») on the issue page: its data is NOT
  bond-specific, so the ~4s pull is day-cached across every bond (a second bond that day costs 1.0s
  total) and merging saves ~300 KB because the engine ships once instead of twice; `--no-market`
  skips it, a failed pull simply drops the tab. Plus the **ISSUER YIELD CURVE** — the honest
  per-bond analogue of the КБД: every comparable issue that issuer has outstanding, duration ×
  yield, over the sovereign curve, answering «дорого ли против СОБСТВЕННОГО эмитента». Resolved via
  the bond's `EMITTER_ID` + a shortname-prefix search FILTERED by that id (`securities.json?
  emitent_id=` silently ignores the filter and `/iss/emitents/<id>/securities` 404s), then
  intersected with the board already pulled for the tab. One basis per curve (yield-to-offer issues
  are excluded, not mixed in), traded-today only, and a ₽ issue printing >100 bp THROUGH the
  sovereign curve is refused as a broken print — every exclusion counted and disclosed on the panel.

**Open the matching reference file before answering anything provider-specific.**
The router only covers routing, the shared options model, and reporting. The moment
a request needs a symbol built, a series/option code, an `entitlement` rule, a
fallback, or capability/coverage detail, READ the file and follow it — do not answer
those from memory. Concretely: any Databento / futures-options request → read
`references/databento.md`; any Alpha Vantage request (equities, options, FX, crypto,
indices, fundamentals, macro, indicators, news) → read `references/alpha-vantage.md`;
any Russian / MOEX market (index, share, bond, RUB FX, FORTS future/option) → read
`references/moex.md`; any request to CHART/plot/draw a historical series → read
`references/charting.md`; any request to ANALYZE a company / разобрать (fundamentals,
valuation, financial health, earnings quality, "how is X doing") → read
`references/fundamentals.md` and run `scripts/fundamentals.py`; any request to analyze a
PORTFOLIO / basket (risk, Sharpe, drawdown, correlation, "how am I doing vs the S&P",
"what's driving my risk") → read `references/portfolio.md` and run `scripts/portfolio.py`;
any request to COMPARE companies ("сравни X и Y", "X vs Y", "который дешевле", "кто
растёт быстрее") → read `references/compare.md` and run `scripts/compare.py` (2-8
user-named companies; ETFs refused; end with the comparative read);
any request to analyze an equity/ETF OPTION CHAIN (IV smile/skew, ATM IV, max pain,
put/call ratio, open interest by strike) → read `references/options.md` and run
`scripts/options.py`; any request to analyze an OPTION CHAIN ON A FUTURE — a smile / live panel
for a futures underlying (ES, CL, NG, GC, ZC, PA, HG, 6E…) → read `references/databento.md` and
run `scripts/futures_options.py` (deterministic: nearest quarterly series, full tradeable ladder,
Black-76 via `scripts/greeks.py`; add `--watch` for the self-updating side panel).

Tools come from the configured MCP servers, not from this file. If a needed server
or tool is missing, stop and ask the user to install/enable it — do not reroute to
the wrong provider just because another tool is available. API keys live in MCP
server / Desktop Extension config, never in prompts, this file, or screenshots.

## Capabilities at a glance (for "what can this do?" questions)

Answer capability questions from this section without reading the reference files.

**Databento** — CME/CBOT/NYMEX/COMEX only (GLBX.MDP3, history from 2010-06):
- Live + historical quotes for futures & options-on-futures (true socket, `dataAge` ~0s).
- Full microstructure: order book (`mbo`/`mbp-10`), `trades`, `tbbo`, `bbo`, OHLCV (1s→1d), `definition`, `statistics`. Top-of-book is near-real-time; deep book lags hours.
- Symbology resolution, dataset metadata, batch export (can be paid).
- NO greeks/IV — compute via `scripts/greeks.py` (Black-76).

**Alpha Vantage** — everything else (realtime on US equity/ETF price functions):
- Equities/ETFs: quotes, intraday→monthly series, bulk quotes, 50+ technical indicators (RSI/MACD/BBANDS/VWAP/…).
- Options (equity/ETF) with NATIVE greeks/IV, put-call & OI ratios, FMV.
- FX; crypto (rate/intraday/daily); indices (200+: SPX/NDX/VIX/DJI/RUT); gold/silver spot; commodity spot incl. ICE softs (coffee/sugar/cotton).
- Fundamentals: overview & ratios, income/balance/cash-flow, earnings, dividends/splits, ETF holdings, insider transactions, institutional holdings, earnings-call transcripts.
- Macro: real GDP, CPI, inflation, unemployment, nonfarm payrolls, fed funds, treasury yields, retail sales, durables.
- Analytics: portfolio stats (return, variance, stddev, drawdown, correlation, covariance, autocorrelation) over fixed or sliding windows.
- News & sentiment (per-article + per-ticker scores), top gainers/losers/most-active, global market status, earnings/IPO calendars.

**MOEX (Moscow Exchange, keyless ISS API)** — Russian markets neither connector covers:
- Indices (IMOEX, RTSI), shares (SBER, GAZP…), bonds (OFZ), RUB FX (USD/RUB…), FORTS futures/options.
- OHLC candles (1/10/60min, daily/weekly/monthly), charted via `scripts/chart.py --source moex` (engine/market auto-resolved). Public, no key, delayed/snapshot — no live socket.

Capability detail and gotchas live in the reference files; deeper analytics/sentiment usage is in `references/alpha-vantage.md`.

## Source routing (decide first)

**Default & web discipline (v1.124.0 — READ FIRST; these rules are canonical HERE and travel
into Cowork/cloud with the skill, where a local `~/.claude/CLAUDE.md` is NOT loaded).**
This skill is the DEFAULT whenever the user's INTENT is to analyze or understand a specific
instrument our providers cover — a company, stock, ETF, asset, security, option chain/series,
futures, or portfolio. Match on INTENT, never on an exact phrase: it fires equally on a terse
"analyze X", a bare ticker, or a long, indirect, multi-part request that boils down to sizing up
such an instrument. When it fires, RUN the bundled script (company/ETF → `fundamentals.py X
--html`; an options intent → `options.py X --full --html`) — never skip it — and answer the
request's specific question directly in the written read (`references/fundamentals.md`), drawing on
the dashboard's own figures. The dashboard is shown ALONGSIDE the direct answer, never as a
substitute for it, and never replaced by a hand-written text/Markdown report.
**Gate the dashboard on relevance:** show it only when at least ONE of its numbers or charts
actually bears on the question asked — and then USE that figure/chart in the answer. A dashboard
that contributes nothing to the user's specific ask is noise: answer directly instead (still
sourcing every number from the scripts, per below). A general "analyze X" makes the whole dashboard
relevant by definition, so it always renders.
- **Market numbers come from the scripts, not the web.** Price, quote, valuation/multiples,
  growth, margins, earnings, estimates, insider/13F, options IV/skew, and news sentiment ALL come
  from the bundled scripts / Alpha Vantage (`NEWS_SENTIMENT` already rides the fundamentals pull) —
  NEVER from a web search, NEVER from ANY subscription market-data MCP connector (FactSet / LSEG /
  Morningstar / PitchBook / S&P Kensho / Daloopa / Bloomberg — no subscription exists here, they
  answer nothing; the list is open-ended: if a connector needs a data subscription, it is off-source),
  never invented.
- **Web search is ONLY for filing/transcript facts the data layer genuinely lacks** (8-K/10-Q
  wording, a transcript quote) — NOT for prices, earnings figures, analyst targets, or headlines the
  AV feed already carries. Do NOT web-search "X Qn earnings results / analyst ratings / outlook":
  that data is in the dashboard, and searching it is both slower and off-source. For a RU/MOEX name
  the AV feed carries nothing, but the term-structure read still comes from the DASHBOARD (its
  backwardation/skew/PCR) — the only web-worthy fact is the upcoming earnings DATE, and even that is
  OPTIONAL, done AFTER the dashboard is presented, never before, and the offer's earnings-preview
  establishes it anyway. Don't turn it into a research detour that delays the render.
- **PRESENT THE DASHBOARD THE INSTANT IT RENDERS — before the written read, before any web lookup,
  before the offer widget.** The scripts render in ~2 s (a warm MOEX/AV page); the ONLY thing that
  may lag is a cold futures/vol store, and even then you deliver the 'warming' page immediately
  (the futures rule) and re-present after the backfill. Never make the user wait through analysis, a
  web search, or the offer's "Connecting to visualize…" before they see the file. Order is fixed:
  render → PRESENT the file → THEN the written read → THEN the offer. (SBER Cowork 2026-07-24 ran 11
  commands incl. web searches for the earnings date BEFORE presenting — ~7 min of nothing for a
  2-second dashboard. That is the anti-pattern this rule kills.)
  **THE SCRIPTS NOW SAY THIS BACK TO YOU (v1.157.0).** Every producer prints a closing-step note to
  STDERR at the write, naming the file to present, the written read, and — where one fits — the exact
  `offer.py` invocation with the context-appropriate commands. It is there because these rules,
  bold and current, still did not fire: the ZW (wheat) and PA (palladium) Cowork sessions 2026-07-30
  both drew a v1.154.0-stamped page and stopped — no read until the user kicked the session, the
  deeper document named in prose with no card. That is nine-plus recurrences of one class (CAT,
  GMKN, ROSN, NVTK, V-vs-MA, MSOS, ORCL, INTC, QCOM), so the reminder moved from a file read at
  session start to the tool result at the moment of the decision. Consequence for golden paths:
  redirect STDOUT ONLY, never `2>&1` — merging the streams is what made a successful futures `--full`
  return a completely empty tool result.
  **"Rendered" ≠ "presented" — and NEVER claim it's ready until it actually is.** The `--html` file
  existing on disk is NOT the user seeing it: you must ACTUALLY call the present step (Cowork
  `present_files` / the file card) FIRST, and only THEN say "готово / here's the dashboard". Do not
  announce "дашборд готов" and then keep pulling data — the PLZL Cowork session 2026-07-24 said
  "Дашборд готов" then ran 7 more commands (a separate `--json` + a cold `--history` reconstruction)
  before presenting, and the user replied "не вижу дашборда!". The `--full` page is SELF-CONTAINED
  (chain + term + the vol-store Overview/History are all in it): do NOT run extra `--json`/`--history`
  pulls for the written read — read the numbers off the ONE `--full` render (a single `--json` of the
  SAME `--full` if you need them machine-readable), and NEVER trigger a cold `--history` backfill
  inline during an analysis (it is the ~1–3 min reconstruction; a cold History tab shows 'warming'
  honestly — leave it, don't warm it mid-answer).

## Open interactive dashboard (OpenAI desktop and Work)

When `<skill-dir>/market-data-host.json` says `{"host":"openai"}`, write the export under the
task/session outputs directory (or the platform `<temp-dir>` only when no outputs directory is
available), never under a
selected project's `.preview/` directory. The large file link is the PRIMARY presentation path: post
it in commentary immediately after render and repeat it as the **first visible item** of the final
answer, before the written read, substituting the absolute output path and a concise instrument name.
Make the entire localized H2 heading clickable rather than placing a small link beneath a heading:

English: `## [Open interactive dashboard — <instrument> →](</absolute/path/to/dashboard.html>)`

Russian: `## [Открыть интерактивный дашборд — <instrument> →](</absolute/path/to/dashboard.html>)`

Never put `**` inside a local file-link label and never add a separate duplicate heading. The link is
not proof that a
pane opened; `--open` is still explicit-only because it opens an external browser. Do not call
`render_market_data_dashboard` on the default company-analysis path: the native component initially
renders inline, its `fullscreen` request is only a request, and combining it with a deep-analysis card
has produced a host-side re-attachment race. Keep that tool for an explicit native-dashboard request;
if used, make the deep-analysis offer plain text so the turn still contains only one MCP App.
On Claude/Cowork (no OpenAI host marker), keep the existing `present_files` / artifact-card path
unchanged; never call the OpenAI MCP App tool there.
- **Resolve a tricky ticker's identity inside the skill, not on the web.** 2-letter tickers and
  post-spinoff names (e.g. `GE` = GE Aerospace after the 2023–24 GE HealthCare / GE Vernova splits)
  are identified from AV `OVERVIEW` / `SYMBOL_SEARCH` — do not open a browser to ask "what is X now".
- **The deep-analysis DOCUMENT skills** (`financial-deep-analysis:*` on ChatGPT Work/Codex;
  legacy `equity-research:*` / `financial-analysis:*` on Claude) run ONLY on their explicit
  skill invocation (or an explicit ask for that document type); their trigger
  phrases must NOT hijack a data question — known collision phrases (v1.126): "quarterly results" /
  "earnings analysis" (→ THIS skill's analyze-X), "earnings calendar" / "upcoming events"
  (catalyst-calendar — our Next-earnings card answers the simple form), "review my positions"
  (thesis-tracker — a portfolio ask routes to portfolio.py), "what looks interesting"
  (idea-generation). When one DOES run, it still sources every market number from these
  scripts/stores per the rule above (details in the Neighbor-plugins note below).

Route by asset class and task, never by the bare phrase "market data".

**Classify the underlying FIRST — a bare symbol does not identify an instrument.** The same
string means different things per source, so classification precedes every script call:

0. **Collision check.** Many futures roots are ALSO real equity tickers (full-universe
   audit 2026-07-04, `docs/collision-audit-2026-07-04.md`): `CL` (Crude Oil /
   Colgate-Palmolive), `ES` (E-mini S&P / Eversource), `NG` (NatGas / NovaGold), `HG`
   (Copper / Hamilton Insurance), `SI` (Silver / Shoulder Innovations), `ZM` (Soybean Meal /
   **Zoom**), `BZ` (Brent / Kanzhun), `HE` (Lean Hogs / Hawaiian Electric), `BTC`
   (CME future / Grayscale ETF)… — all in `futures_options.OPTION_ROOTS` (36 roots since the
   2026-07-04 GLBX universe audit, incl. BZ->BZO, MGC->OMG, micro MCL/MNG, SR3, ZN/ZB).
   ICE softs (`KC` Coffee, `CC` Cocoa, `SB` Sugar) + `SIL` = `options.NAMESAKE_ROOTS`: NO
   futures route on this GLBX subscription — only the equity namesake via `--equity`.
   Every key of both tables is guarded. On a hit: futures meaning → `futures_options.py`;
   the EQUITY namesake → `options.py SYM --equity` (the guard refuses without it — by
   design, never silently). Cross-exchange: MOEX `Si` (USD/RUB future) ≠ CME `SI` (Silver);
   35 MOEX tickers also exist as US listings (`ROST`, `SIBN`, `FLOT`…) — the source decides,
   never the string. When the user's intent is unclear — ASK, don't guess.
1. **Futures root** (in `OPTION_ROOTS`, or a CME/GLBX product) → Databento scripts.
2. **Equity/ETF** → AV scripts. Resolve names to the US PRIMARY ticker first (never take
   `SYMBOL_SEARCH` match #1 blindly — foreign listings like `0R0X.LON` rank high).
3. **Cash index** (SPX/NDX/RUT/DJX/VIX) → exists on NO connected source; `options.py`
   refuses with the ETF proxy (SPY/QQQ/IWM/DIA — confirm the proxy with the user; VIX has
   NO clean proxy: its options price off VIX futures).
4. **MOEX name** (Cyrillic/RUB/secid shape, or a Latin RU blue chip GAZP/SBER/LKOH — these
   carry no signal and are unknown to AV) → `chart.py --source moex` for charts; an OPTION-chain
   intent on a Russian STOCK → `scripts/moex_options.py SYM [--full]` (the ~41 FORTS premium-on-spot
   equity options: SBER, GAZP, LKOH, ROSN, ALRS, MTSS, TATN, MAGN…; delayed ISS, exchange IV, computed
   greeks); a **company/FUNDAMENTALS** intent on a RU name (a bare "проанализируй компанию GAZP",
   "фундаментал Газпрома", revenue/EBITDA/margins/P·E) → `scripts/moex_fundamentals.py SYM` — the RU
   split of `fundamentals.py` (AV has NO RU coverage, so a MOEX name NEVER goes to `fundamentals.py`;
   it web-scrapes smart-lab primary, Yahoo TTM cross-check, ISS cap/shares/div — honest ₽bn/lagged
   provenance). The US-vs-RU test: an AV `OVERVIEW` resolves (non-empty) → `fundamentals.py`; a MOEX
   name (resolves on the ISS TQBR board / a known RU ticker, empty AV OVERVIEW) → `moex_fundamentals.py`.
   A **US RATES / yield-curve** intent — «что со ставками», «кривая трежерис», «инверсия кривой»,
   Treasury yields, the 10-year, the Fed's stance as CONTEXT behind an equity or futures read →
   `scripts/treasury.py`. It is a MARKET curve, not an instrument: there is no per-issue/CUSIP data
   at this provider, so never promise a US single-bond page. A RU rates question goes to
   `moex_bonds.py --market` instead (КБД), and the two are not interchangeable.
   A **BOND** intent on a RU name — a bond SECID/ISIN shape (`SU…RMFS…`, `RU000A…`), the words
   облигация/бонд/ОФЗ/выпуск/купон/доходность к погашению/оферта, or a request for the RU bond market
   or the yield curve (КБД/G-curve/RGBI) → `scripts/moex_bonds.py <SECID>` (one issue) or
   `moex_bonds.py --market` (the curve + market). A bond NEVER goes to `moex_fundamentals.py`: that
   module is the ISSUER's equity, a different instrument. If the ask is about an issuer's BONDS as
   a set rather than one paper — «облигации ГТЛК», «выпуски Селигдара», «что у эмитента в долге» —
   → `moex_bonds.py --issuer NAME`, which is the group page (medians + its own curve + the payment
   calendar + a linked table of the issues). If the ask is a company's CREDIT rather
   than one issue, run the issuer's fundamentals for leverage AND name its listed issues.
   Portfolio still has NO MOEX path; futures-margined MOEX options (Si/CNY/RTS) are a later slice.
5. **Crypto** → `chart.py --source av-crypto` with the BARE asset + `--market` (`BTC
   --market USD`, never `BTCUSD`). Single quotes → the AV MCP functions directly.
6. **FX** → no chart path in this skill; quotes/series via the AV MCP `FX_*`/
   `CURRENCY_EXCHANGE_RATE` functions directly.

**Golden paths — one canonical command per task (agents: use these verbatim, resolve
`<skill-dir>` to the folder containing this `SKILL.md`, and always pin `--source`
explicitly; `auto` flips by environment state and is for humans):**

```
futures-options FIRST QUESTION     <python> "<skill-dir>/scripts/futures_options.py" CL --list-series          # keyless connector /series: which expiration? present + ask BEFORE the chain pull. CONNECTOR ONLY — without it (cloud Cowork) skip the pre-listing and go straight to --full
futures-options chain (analysis)   <python> "<skill-dir>/scripts/futures_options.py" CL --source databento
futures COMBINED (bare intent)     <python> "<skill-dir>/scripts/futures_options.py" CL --full --html out.html > cl_full.log   # EXACTLY ONE foreground process and ONE output path; REDIRECT STDOUT ONLY — NEVER `2>&1`: stderr carries provenance plus the Claude/Cowork closing-step reminder. Capture the first log and do not rerun. OpenAI keeps the day-cache + defs snapshot enabled and rejects a concurrent writer for the same dashboard. Cloud tool timeout ≥900s.
futures-options LIVE panel         <python> "<skill-dir>/scripts/futures_options.py" CL --source connector --watch 10 --port 8788  # Claude/Cowork only; OpenAI static v1 never starts --watch
pre-check the listed series        <python> "<skill-dir>/scripts/futures_options.py" CL --list-expiries      # Databento-direct (needs the key); a whole-root defs pull — ~40-60s+ on big roots (day-cached same-day since v1.128), NOT a free pre-check; --list-series is the keyless connector sibling
equity-options FIRST QUESTION      <python> "<skill-dir>/scripts/options.py" AAPL --list-expiries              # which expiration? present + ask BEFORE the chain pull (reads the AV chain once — needs the key)
equity/ETF single series           <python> "<skill-dir>/scripts/options.py" AAPL --expiry 2026-07-17          # the resolved series (server-side expiration= filter, ~19x smaller); --term for the cross-expiration view
equity/ETF COMBINED (bare intent)  <python> "<skill-dir>/scripts/options.py" NVDA --full --html out.html       # tabbed Overview·Chain·Term·History — the default answer to a vague "analyze options on X"
equity/ETF COMBINED live           <python> "<skill-dir>/scripts/options.py" NVDA --full --watch 4 --port 8790  # same page, Chain tab STREAMS (SSE); serve in the Preview Pane; Claude/Cowork only, never OpenAI static v1
equity/ETF retrospective vol       <python> "<skill-dir>/scripts/options.py" NVDA --history --html out.html    # IV rank/cone/skew·PCR history/term-through-time — "is vol cheap or rich vs its past"
equity namesake of a futures root  <python> "<skill-dir>/scripts/options.py" CL --equity
MOEX company fundamentals         <python> "<skill-dir>/scripts/moex_fundamentals.py" GAZP --html out.html   # tabbed Overview·Financials·Dividends (v1.149); keyless smart-lab+ISS, ~10-14s (it pulls the FULL listing price history so the drawdown is honestly all-time). PRESENT THE FILE THE INSTANT IT RENDERS, then write the read. A RU name NEVER goes to fundamentals.py (Alpha Vantage has zero RU coverage)
MOEX bond (one issue)              <python> "<skill-dir>/scripts/moex_bonds.py" SU26238RMFS4 --html out.html  # tabbed Обзор·Денежный поток·История (v1.150); keyless ISS, ~1-2s. PRESENT THE FILE THE INSTANT IT RENDERS, then write the read. ALWAYS say what the yield is computed TO (к погашению vs к оферте) — the page does, and a yield quoted without it is the single most misleading number here
US TREASURY CURVE / rates      <python> "<skill-dir>/scripts/treasury.py" --html out.html                  # the US yield curve: 3m/2y/5y/7y/10y/30y today vs 1M/1Y ago, the 10л−2г slope with an inversion count, fed funds, and the realized real 10y. ~6s cold, day-cached after. Use for "что со ставками", "кривая трежерис", "инверсия кривой", yields/Fed context behind an equity or futures read. NO per-issue/CUSIP data exists at this provider — there is no US single-bond page
MOEX bonds of ONE ISSUER           <python> "<skill-dir>/scripts/moex_bonds.py" --issuer "Селигдар" --html out.html   # the GROUP page (v1.153): medians + duration/spread RANGE, the issuer's own credit curve, the combined payment calendar, and a table of the issues whose rows LINK to their own dashboards. Those member pages are PRE-RENDERED beside out.html (~25s for 14 issues — the curve slices are shared, so it is roughly one cold render, not N); PRESENT THEM TOO or the links fall back to an ask in chat. --no-members skips the pre-render
MOEX bond MARKET / yield curve      <python> "<skill-dir>/scripts/moex_bonds.py" --market --html out.html       # КБД today vs 1M/1Y ago + RGBITR + ОФЗ duration×yield map + a turnover-filtered corporate table; ~7s cold. Use for "что на рынке облигаций", "кривая ОФЗ", "куда идут ставки"
MOEX equity-options COMBINED       <python> "<skill-dir>/scripts/moex_options.py" SBER --full --html out.html   # tabbed Overview·Chain·Term·History (v1.134+); keyless ISS, ~2s warm. Go STRAIGHT to --full — NO pre-listing (MOEX has no connector; --list-expiries is a wasted extra ISS pull). PRESENT THE FILE THE INSTANT IT RENDERS, THEN write the read + the earnings-preview offer. Do NOT web-search the earnings date BEFORE presenting — the term-backwardation read comes from the dashboard's own numbers and the offer's earnings-preview pins the date (SBER Cowork 2026-07-24: 11 commands incl. web searches ran BEFORE the dashboard was shown → ~7 min for a 2-second render)
chart: CME future                  <python> "<skill-dir>/scripts/chart.py" ES.v.0 --source databento --stype continuous --interval 1d --days 90
chart: US equity                   <python> "<skill-dir>/scripts/chart.py" AAPL --source av-equity --interval 1d --days 365 [--adjusted]
chart: crypto                      <python> "<skill-dir>/scripts/chart.py" BTC --source av-crypto --market USD --interval 4h --days 30
chart: MOEX                        <python> "<skill-dir>/scripts/chart.py" SBER --source moex --interval 1d --days 180
fundamentals dashboard             <python> "<skill-dir>/scripts/fundamentals.py" NVDA --html out.html
compare companies (2-8)            <python> "<skill-dir>/scripts/compare.py" AAPL MSFT NVDA --html cmp.html   # 2-6 names; --deep for statement rows
portfolio risk/return              <python> "<skill-dir>/scripts/portfolio.py" --holdings "NVDA:100,AAPL:50" --days 365
one-off greeks / IV                <python> "<skill-dir>/scripts/greeks.py" iv --model b76 --type call --F 7467 --K 7575 --dte 21 --price 24.75
```

Same-name-different-meaning flags (documented variance — do not assume): bare `--watch` =
2s in options.py (POLL_DEFAULT_S; floor 1s; series-only re-polls) vs 60s in futures_options.py (settlement re-fetch is heavy); `--open`
opens the artifact FILE in 4 scripts but the live `--watch` PAGE in futures_options;
`--days` = chart lookback (float, 14) vs portfolio risk window (int, 365); `--expiry` =
exact-listed-or-error (options.py) vs nearest-within-±7d-or-error (futures_options.py).
Malformed values (`--interval banana`, `--watch -5`, bad dates) die AT PARSE; an
inapplicable provider flag warns on stderr. AV free tier = 25 req/day · 5/min —
fundamentals (6–7 concurrent) and portfolio (N+2 concurrent) effectively need a premium key.

| Request | Source |
| --- | --- |
| CME/CBOT/NYMEX/COMEX futures; options on those futures; GLBX.MDP3 OHLCV/trades/MBP/MBO/TBBO/BBO/definition/statistics; Databento symbology, metadata, batch | **Databento** → read `references/databento.md` |
| Equities/ETFs (quote, intraday, daily); equity/ETF options; FX; crypto; indices; fundamentals; earnings; macro; technical indicators; news/sentiment; benchmark commodity spot (incl. ICE softs KC/SB/CT/CC) | **Alpha Vantage** → read `references/alpha-vantage.md` |
| Russian markets — MOEX indices (IMOEX/RTSI), Russian shares, OFZ/corp bonds, RUB FX, FORTS futures/options | **MOEX ISS** (keyless public API) → read `references/moex.md` |

- Cash-index options (SPX/NDX/VIX) are on neither connector → `options.py` REFUSES them
  and names the ETF proxy (SPY/QQQ/IWM/DIA) — confirm the proxy with the user before
  running it. VIX is special-cased: no clean proxy (its options price off VIX futures).
- Ambiguous "get AAPL market data" → default Alpha Vantage (or ask) unless the user
  explicitly asks for Databento.
- Ambiguous "options" → ask whether they mean CME futures options (Databento) or
  equity/ETF options (Alpha Vantage). The word maps to different markets.
- Comparisons across domains (e.g. ES future vs SPY ETF) → use both, label
  provenance, and don't normalize one provider's schema into the other's.
- **Ticker collisions (Russian vs other).** Don't infer the market from a bare
  ticker. Route to MOEX on a Russian signal — Cyrillic, RUB, "Мосбиржа"/"MOEX", or
  a MOEX secid shape (FX `USD000UTSTOM`, OFZ `SU…RMFS…`, FORTS `RIH7`/`SiU6`). For
  an ambiguous plain ticker, confirm against each source's instrument search
  (MOEX `/iss/securities.json?q=`, AV `SYMBOL_SEARCH`, Databento `symbology_resolve`)
  or ASK — never guess US-vs-RU. A symbol can list on more than one source.

**Deep-analysis companion (host-adapted deliverable routing).** The separately installed
`financial-deep-analysis` OpenAI plugin contains 13 static research/modeling skills adapted
from Anthropic's Apache-2.0 `financial-services` workflows. It is not part of the market-data
analytics core and carries no API keys. Its bundled `financial-deep-analysis-offer` MCP server
adds a native inline card for an optional one-click follow-up in ChatGPT Work and Codex. A Claude
package keeps the original namespaced slash-command behavior through `scripts/offer.py`. This lets
one common router preserve the accepted analytical core while each host gets a surface it can
actually execute.
Routing rule: THIS skill stays the
default for any "analyze X" (data + dashboard + written read); their skills run only on the
explicit slash command or an explicit ask for that document type — never let their
earnings-analysis trigger hijack a plain "analyze X results". The FLIP SIDE (user decisions
2026-07-18, off the AMD + WDC trigger experiments): when THIS skill answers a COMPANY
"analyze X" — general OR results-flavored — OR an equity/ETF OPTIONS-chain read (the CAT
session 2026-07-23 closed its options read text-only, card-less) — OR a **MOEX / Russian
single-name OPTIONS-chain read** (the GMKN + ROSN Cowork sessions 2026-07-24 closed card-less
while YDEX offered `:earnings-preview` and the user clicked it → a real DOCX — the offer was
NON-DETERMINISTIC; `references/moex.md` now carries the closing step) — OR a **MOEX / Russian
COMPANY-fundamentals read** (`moex_fundamentals.py`; the NVTK session 2026-07-24 closed with
NEITHER card — the same hole, re-opened by a NEW read path shipping without the closing step:
`references/moex.md`'s fundamentals section now carries it too, incl. the sector-complement
rule — a gas/LNG or metals producer like NVTK/ROSN/GMKN DOES earn the second `:sector` card) —
OR a COMPANY
COMPARISON (compare.py → `:comps`; the V-vs-MA session 2026-07-23 offered nothing) — OR a
THEMATIC-ETF read (→ `:sector`; MSOS named it text-only) — OR a FUTURES-chain read
(v1.128 — the palette below has commodity-fit picks now) — END the written read
with an explicit OFFER of
the matching deeper document — pick by CONTEXT from the installed palette (v1.119.1), name
ONE (at most two), never the whole menu. SECTOR IS A COMPLEMENT, NOT A DEFAULT SECOND CARD
(user 2026-07-24, applies to ALL names not just MOEX): default to the ONE best-fit company
document; add a `:sector` card as a SECOND offer ONLY when the commodity/sector story is
genuinely load-bearing for the name (a metals/oil/mining producer — GMKN/PLZL/ROSN/LKOH),
never for a bank/tech/retailer where the sector overlay would be noise. RUSSIAN names: DON'T withhold the offer.
Alpha Vantage has no RU coverage, but since v1.147 THIS SKILL DOES — the company documents
(`:earnings-preview`/`:earnings`/`:dcf`) source their HISTORY from `moex_fundamentals.py SYM
--json` (revenue/EBITDA/net income, margins, multiples, dividends — smart-lab provenance rides
along), and only the FORWARD view (upcoming date/guidance/consensus) is web-sourced from issuer
IR + broker notes (АТОН/Finam/TASS/Interfax); our scripts own every market/vol number.
The identifiers below are the common routing vocabulary. THE CARD IS PART OF THE OFFER on
OpenAI: naming the document in prose while skipping the native MCP App card is a MISS, not a
lighter form of compliance — the card is the one-click UI the user expects:
  the fresh quarter IS the story → `/equity-research:earnings SYM Qn FYnn` (8–12-page update);
  next report ≤~3 weeks out (the Next-earnings card knows) → `/equity-research:earnings-preview`
  (scenarios + what to watch — pairs naturally with a high-IV/event-risk options read);
  a valuation question ("дорого или дёшево") → `/financial-analysis:dcf` or `:comps`;
  peers/comparison (incl. after a compare.py run) → `/financial-analysis:comps` (the live
  Excel model) or `:competitive-analysis` (the landscape deck);
  "полное покрытие с нуля" → `/equity-research:initiate` (the 5-task initiation report);
  a THEMATIC ETF / sector question → `/equity-research:sector` (the industry report — the
  one ETF-context exception to the no-offer rule below);
  a position the user keeps returning to → `/equity-research:thesis` (start tracking it);
  event-risk read (a high-IV chain, a dated catalyst) → `/equity-research:catalysts` (the
  catalyst calendar); a TRACKED name just reported → `/equity-research:model-update` (plug
  the quarter into its model); "что купить / найди идеи" → `/equity-research:screen`.
  FUTURES chains (v1.128 — the "no futures offer" rule is RETIRED; verified against the
  plugin skill texts: sector-overview takes a free-form sector/thematic-thesis scope,
  catalyst-calendar explicitly covers macro events & industry data releases,
  idea-generation takes sector+theme): a strong commodity story (vol percentile, term
  shape, positioning — the CL-class read) → `/equity-research:sector <complex>` (the
  energy/metals/ag landscape: players, value chain, the thematic thesis); "как это
  сыграть / через какие акции" → `/equity-research:screen` (sector+theme screen); the
  CALENDAR is the question (rates roots around FOMC, a WASDE/OPEC+ cluster) →
  `/equity-research:catalysts`. Company-shaped documents (dcf/comps/earnings*) still
  NEVER fit a commodity — don't offer them there.
On OpenAI, the `earnings-analysis` card requests a standalone PDF as the primary artifact; its
DOCX may remain as the editable source. Other companion skills keep their native DOCX/XLSX/PPTX
deliverables. Claude/Cowork keeps the existing Anthropic deliverable behavior unchanged. Plain
(non-thematic) ETF pages get NO offer, the sector case aside.
An OFFER, never an auto-run — the document can be a long task.

Command→skill map: `:earnings`→`earnings-analysis` · `:earnings-preview`→`earnings-preview`
· `:comps`→`comps-analysis` · `:dcf`→`dcf-model` · `:lbo`→`lbo-model` ·
`:sector`→`sector-overview` · `:initiate`→`initiating-coverage` ·
`:thesis`→`thesis-tracker` · `:competitive-analysis`→`competitive-analysis` ·
`:morning-note`→`morning-note` · `:catalysts`→`catalyst-calendar` ·
`:model-update`→`model-update` · `:screen`→`idea-generation`.

On an OpenAI host, the fast company-analysis lane above takes precedence for US and MOEX/Russian
companies: make one `render_deep_analysis_offer` call whose primary action is
`initiating-coverage`, whose `secondary` is the reporting-cycle action, and whose
`defaultLanguage` matches the chat. For non-company instruments, inspect the current task's skill
list and invoke the companion tool for exactly ONE best-fit document **after the written read**, as
the last tool call of the turn. This is the only OpenAI
one-click surface: the server returns an MCP Apps inline card whose button sends the native
`Use $financial-deep-analysis:<skill> for: <subject>` request plus an evidence-only/no-dashboard
contract. The `earnings-analysis` request additionally requires a standalone PDF. Do **not** merely put
that text in the answer, and do **not** run `scripts/offer.py` or use `dashboard.offer_widget` on
OpenAI: command stdout is not an interactive ChatGPT card.

If the companion skill or its MCP offer tool is absent, do not fabricate a button. Close with the
exact native invocation as ordinary text and say that the user must install/enable the companion
plugin and begin a new ChatGPT Work or Codex task. This fallback never emits a Claude slash command.
The static `market-data-host.json` marker makes host selection deterministic; do not infer OpenAI
support from Claude's plugin registry.

On Claude, the same `offer.py` call retains the legacy manifest/mirror probes and namespaced
slash commands. Pass `--lang ru|en` when the deliverable should be written in the language of the
conversation: it appends the SAME report-language instruction the OpenAI companion card sends, so
both hosts hand the document skill one contract. Omitted, nothing is added and the skill's own
default (English) applies — a Russian-language session that offers a document without it gets an
English deliverable. On any host without a renderable card, close the written read with the exact
host-native invocation as plain text. If the user directly asks for the deeper deliverable
and the mapped skill is available, invoke it directly rather than sending an offer.

When one of the companion skills runs on a
name this skill covers, source EVERY number from these scripts (`fundamentals.py SYM
--json`, non-presentation options JSON only when the document needs it, the opthist/futhist
stores) without creating/presenting dashboards — never from the bundle's
subscription MCP connectors (FactSet/LSEG/Morningstar/PitchBook/S&P Kensho/Daloopa/
Bloomberg… — no subscription here, they answer nothing; the list is open-ended) and never
invented — their own skill texts that say "search the web for latest earnings" or "use the
Kensho/Daloopa MCP" are OVERRIDDEN by this rule on any name our data layer covers. The Reporting rules below (timestamps,
delayed-is-never-live, provenance) apply to their documents exactly as to our dashboards.

Alpha Vantage default: for US equity/ETF price functions pass
`entitlement=realtime` by default (without it they return last-session/EOD).
Everything else (FX, crypto, indices, macro, fundamentals, news, options) takes no
`entitlement`. Full detail in `references/alpha-vantage.md`.

## Options model (shared across both sources)

Route the option by its UNDERLYING, then run the same three steps; only the data
source and whether greeks are native differ:
- Underlying is a FUTURE (ES, CL, NG, GC, ZC, …) → Databento. Greeks NOT provided —
  compute with `scripts/greeks.py` (Black-76). **Open `references/databento.md` and
  follow its Marengo lookup instructions** before quoting.
- Underlying is an EQUITY or ETF (AAPL, SPY, QQQ, …) → Alpha Vantage
  `REALTIME_OPTIONS` (greeks/IV/OI native — just read them). **Open
  `references/alpha-vantage.md` and follow Branch B** before quoting.

Use the FEWEST calls that answer the request:
- **Specific strike named** ("the 7300 put") → quote that contract directly. No
  center call.
- **Alpha Vantage ATM / moneyness / N-delta** → ONE `REALTIME_OPTIONS` call (with
  `expiration`, `require_greeks=true`); get moneyness and N-delta from the native
  `delta`/strikes in that one response. A separate `GLOBAL_QUOTE` center is only
  needed if the user wants the underlying price stated. For CLI pre-checks, use
  `<python> "<skill-dir>/scripts/options.py" SYMBOL --list-expiries`; it reads the full
  `REALTIME_OPTIONS` chain once, prints expirations, and exits without rendering.
- **Whole-chain analysis (equity/ETF)** — IV smile/skew, ATM IV, 25-delta skew, max
  pain, put/call ratio, OI by strike, or a dashboard → run **`scripts/options.py`**
  (read `references/options.md`). **ASK term-vs-single FIRST** (whole-chain rule below);
  for a single series pass **`--expiry`** so the fetch is the server-side-filtered ONE
  series (~19× smaller), not the whole surface. It fetches over HTTP (which overflows the
  MCP) and reduces locally, so don't pull `REALTIME_OPTIONS` into context and hand-`jq`
  it for these.
- **Whole-chain analysis (options on a FUTURE)** — the same smile/skew/ATM-IV/max-pain/
  PCR/OI-by-strike, or a (live) dashboard, for a futures underlying → run
  **`scripts/futures_options.py ROOT`** (read `references/databento.md`). It deterministically
  picks the nearest quarterly series from the live definition listing, pulls its full tradeable
  ladder from Databento Historical, and computes the smile with `scripts/greeks.py` (Black-76,
  settlement-priced). **ASK term-vs-single FIRST** (whole-chain rule below); outside OpenAI, a
  single series defaults to the LIVE `--watch` side panel, while the OpenAI static-v1 policy above
  requires a one-shot `--html`; `--term` gives the all-expiration view. Don't
  hand-pull the chain or reimplement greeks. This is the whole-chain path; the
  N-delta/single-contract path below stays lightweight.
- **Databento N-delta** → resolve candidate strikes with Marengo search, then quote their IDs with
  `marengo_databento_live_get_futures_quote` and compute with `scripts/greeks.py`; do NOT pull the
  chain. A `definition` probe is only a fallback when the symbol can't be built.

**Whole-chain analysis — the strike→expiry→time taxonomy.** An option chain has three analysis
axes, each adding a dimension (equity = Alpha Vantage; futures = Databento):
- **strike** → the **smile** for ONE expiration (skew / max-pain / PCR / OI *right now*): `SYM [--expiry D]`.
- **+ expiry** → the **term structure** across *today's* expirations (the surface): `SYM --term`.
- **+ time** → the **retrospective** — is vol cheap/rich vs its OWN past (IV rank, cone, skew/PCR trend,
  term-through-time): `SYM --history`. *(`--term` is a TODAY snapshot, NOT the retrospective — that's `--history`.)*

Routing a "проанализируй опционы" / "analyze the options on X" request:
1. **Bare / ambiguous → `--full`** (the COMBINED tabbed page: Overview · Chain · Term · History). It is
   the default answer — a *synthesis*, not one of the three axes — so you never have to guess. Overview
   leads with the cheap/rich read; the tabs cover every axis. **Both routes since v1.114**: equity/ETF =
   `options.py SYM --full`; futures = `futures_options.py ROOT --full` (Overview/History ride the futhist
   settlement store — a COLD root shows honest 'warming' tabs; fill with `futhist.py ROOT --backfill
   --years 1`).
2. **A clear single intent → that ONE panel** (no tabs overhead): a date / "nearest" / "front month" →
   the single-series smile (`--expiry D`); "term structure" / "по всем экспирациям" → `--term`; "IV rank" /
   "cheap or rich" / "historical / retrospective vol" → `--history`.
3. **Genuinely split (and you must pull anyway) → ASK**, offering the full set (= `--full`). Pre-check the
   expirations cheaply first — equity `--list-expiries` (one AV `REALTIME_OPTIONS` call — needs the key) /
   futures `--list-series` (keyless connector) — when the user needs to pick a date for the single-series path.
4. **Single series ⇒ pass the resolved `--expiry`** (never bare `options.py SYM` / `futures_options.py ROOT`):
   `--expiry` makes Alpha Vantage push the `expiration=` filter server-side (~19× smaller payload, verified)
   and pins the futures series. The WHOLE surface is pulled only for `--term` / `--full` and the one-off
   `--list-expiries` discovery (which genuinely need every series).
5. **Rendering default outside OpenAI:** show the dashboard in the in-app **side panel (Preview Pane)**,
   NOT an external browser — a **single series → LIVE (`--watch`)**; **`--term` / `--full` / `--history`
   → a static one-shot** (live across many series is redundant). On an OpenAI host, the static-v1
   policy above wins for every form: one-shot file + prominent link, never Preview Pane or `--watch`.
   External browser (`--open`), inline `--json` widget,
   or PNG are EXPLICIT-only (render on request). Per-provider recipe: `references/options.md` (equity) /
   `references/databento.md` Branch A2 (futures).
6. **Then write the interpretive read.** The dashboard is not the answer — finish every
   analyze-request with the 3–6-sentence written read per the reference's "The written read"
   checklist (`references/options.md` for equity/ETF chains, `references/databento.md` for
   FUTURES chains, `references/fundamentals.md` for companies):
   cheap/rich vs own history, expected move, what skew/term/positioning imply, the event inside
   the window. Descriptive synthesis, never advice.

Three steps:
1. **Center** (skip when a specific strike is named) = the underlying's live/last
   price — Marengo `marengo_databento_live_get_futures_quote {root}.v.0` with
   `stype_in=continuous` (the `.v.0` form needs it — `stype_in` defaults to
   `raw_symbol` and a bare `{root}.v.0` fails to resolve; `ES`/`NQ` aliases are the
   only exception). Alpha Vantage only if the underlying price is actually needed,
   `GLOBAL_QUOTE entitlement=realtime`.
2. **Select the EXACT contract** by request type (logic identical everywhere):
   - ATM → strike nearest center (or |delta|≈0.5).
   - specific strike → use it verbatim.
   - ITM/OTM/"deep" → by strike vs center (puts: ITM above center; calls: below),
     or by |delta|; "deep" = far from center / |delta| toward 1.
   - N-delta (e.g. 20Δ) → pick |delta| ≈ target. Databento: compute via
     `scripts/greeks.py` (forward from put-call parity). Alpha Vantage: read the
     native `delta` field — no computation.
   - nearest/named expiration → resolve the expiry, then the per-source symbol/param
     (Databento ES series codes & symbol format are in `references/databento.md`;
     Alpha Vantage takes `expiration=YYYY-MM-DD`).
3. **Quote it.** Databento: use Marengo one-shot Live with the selected ID; report unavailable on failure. Alpha Vantage: read
   the chain row from the `REALTIME_OPTIONS` response already pulled.

## Reporting rules (always)

- **An "analyze X" answer ends with a written read, not a render.** Text summary +
  dashboard + a 3–6-sentence interpretive synthesis ("so what": what stands out, what it
  implies, what to watch) per the "The written read" checklist in the matching reference
  (`references/fundamentals.md` / `references/options.md` / `references/databento.md` for
  futures chains). Point-fact lookups stay
  text-only — no read required for one number.
- **State the center.** Whenever the answer depends on a reference price (ATM,
  ITM/OTM/"deep", N-delta), say what the center is and its value (e.g. "center =
  ESU6 7464.6, live 0s"; when greeks/true ATM matter, the put-call-parity forward
  "forward ≈ 7467"). Name the chosen strike and why ("ATM 7475 = nearest listed").
- **On any fallback, say why** — never present a fallback as a live tick. Timeout →
  thin/closed market, served from historical (give the timestamp/age); symbology
  error → wrong symbol, re-resolved via `definition`. Distinguish the two.
- **Always carry the timestamp/`dataAge`** so live (0s) vs near-real-time historical
  (minutes) vs prior settlement is unambiguous.
- **Delayed data is NEVER presented as the live price — this is a hard safety rule.**
  Any delayed or snapshot quote (MOEX ISS marketdata ≈15 min, AV `entitlement=delayed`,
  Databento Historical-REST-as-quote, or any served-from-history fallback) MUST be
  labelled as delayed, by how much, with its source timestamp (`SYSTIME`/`dataAge`).
  Never substitute delayed data for "the current price" — a user may trade on it, so
  an unflagged delay is dangerous. If the user needs a true real-time tick and only
  delayed is available, say so plainly ("MOEX public ISS is ~15-min delayed; no
  realtime without an authorized feed") rather than implying it is live.
- **Charts are presentation, not data.** A rendered chart carries no provenance, so
  state the source, symbol, window, and freshness in TEXT next to it (e.g.
  "Databento GLBX.MDP3 · ohlcv-1d · ES.v.0 · 2026-04-01→06-29 · last 7495.5"). See
  `references/charting.md`.
- **Degenerate deep-ITM near expiry.** A deep-ITM option close to expiry trades at
  (or a hair below) intrinsic, so its time value ≈ 0 and IV is indeterminate —
  `scripts/greeks.py` will return "no solution (price below intrinsic?)". That is
  the correct result, NOT an error to hide: report it's trading at intrinsic,
  |delta| ≈ 1, time value ≈ 0, IV undefined. (Verified: ES `E1CN6 C7000`, center
  7510, mid 509.75 ≈ intrinsic 510.4 → no IV.) When the user's "nearest expiration"
  lands on a ~0DTE contract and they want meaningful greeks, say so and offer the
  next expiration. For computable greeks, pick a strike with real time value (ATM /
  the requested moneyness away from deep-ITM).

## Safety & setup notes

- Databento `batch_submit_job` can create a paid job — confirm dataset/symbols/
  schema/range/cost and keep `metadata_get_cost` zero-cost preflight on by default.
- Verify the user's Databento entitlement profile before applying Standard CME
  guardrails to large requests.
- Distribution: a self-contained `market-data` skill folder (this file +
  `references/` + `scripts/`), and Databento as a separate local MCP server /
  Desktop Extension (asks for `DATABENTO_API_KEY` via sensitive config). Alpha
  Vantage is its own separately-configured MCP server. Neither package bundles the
  other's credentials.

## Cowork / agent-mode

**Egress is often OPEN in Cowork — PROBE, don't assume "no egress".** The scripts are mounted in
the sandbox (`/sessions/*/mnt/.claude/skills/market-data/`) and run there. FIRST choice, ALWAYS:
run the bundled script directly with **`MARKET_DATA_NO_NET=auto`** — it probes egress once and
self-routes. Egress up → it fetches AV directly and renders the real dashboard (the fast, correct
path — e.g. `MARKET_DATA_NO_NET=auto <python> "<skill-dir>/scripts/options.py" AMAT --full --html
<outputs>/amat.html`). Egress blocked → it fast-fails with the MCP `--from-dir` route (no 60s
timeout burn). Do NOT hard-assume egress is blocked and skip the script — that guess IS the
AMAT-session failure (the agent hand-rolled the chain in chat instead of running `options.py`).

Resolve `<temp-dir>` with `<python> -c "import tempfile; print(tempfile.gettempdir())"`; never assume
the POSIX `/tmp` path on native Windows.

Blocked-egress fallback (only after the probe fails): pull endpoints via the Alpha Vantage MCP
connector (`return_full_data=true` — big payloads land as tool-result FILES; don't read them into
context), drop the small inline results (overview, quote) into a dir with ONE bash call (chained
heredocs, not per-file Writes), then `fundamentals.py SYM --from-dir <dir> --html out.html` and
show via present_files (side panel; there is no dev-server/Preview Pane path). Write ALL artifacts
(payload dir, out.html) under the SESSION outputs dir or the resolved platform `<temp-dir>` — NEVER into the user's repos/selected
folders (the Palantir session littered databento-mcp-server with dashboards a future `git add -A`
would swallow). Raw MCP shapes are understood as-is: CSV-in-`result` quote AND daily series need no
reshaping. NEVER hand-build a lightweight substitute dashboard — the skill's page is the product.
Options gotcha: the oversized full REALTIME_OPTIONS payload auto-saves to a HOST tool-results path
the sandbox CANNOT read (and it's too big for Read's 25k-token cap) — so the `--from-dir` bridge
only works if YOU write the payload into the mounted outputs dir; PREFER the direct `=auto` fetch.
REALTIME_OPTIONS via MCP needs datatype=json (connector defaults to csv); --watch/SSE never works
in the side panel (loopback + egress); LIVE there = the BRIDGE artifact: futures_options --artifact
polls get_futures_options_smile via window.cowork.callMcpTool (open via create_artifact). Details:
references/fundamentals.md (Cowork playbook).
