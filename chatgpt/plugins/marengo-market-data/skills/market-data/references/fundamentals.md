# Fundamentals Reference (analyze a company)

Open this when the user wants to **analyze / разобрать a company** (fundamentals,
"how is NVDA doing", valuation, financial health, earnings quality). Equities route
to Alpha Vantage; this is the company-analysis playbook on top of it.

## Use the script — `scripts/fundamentals.py`

One command fetches OVERVIEW + GLOBAL_QUOTE + INCOME_STATEMENT + BALANCE_SHEET +
CASH_FLOW + EARNINGS + EARNINGS_ESTIMATES + TIME_SERIES_DAILY_ADJUSTED (outputsize=full,
the price panel) (+ NEWS_SENTIMENT) **directly over HTTP, staggered-concurrently**, and
reduces them to ~30 computed numbers. This is the fundamentals analogue of
`chart.py`: it sidesteps the fact that the statement/news endpoints **overflow the
MCP token limit** (each is 40-85k chars) and removes the fragile hand-written `jq`
on saved tool-result files. Pure stdlib; key from `.env` (`ALPHAVANTAGE_API_KEY`).

```
<python> "<skill-dir>/scripts/fundamentals.py" NVDA            # text summary (default)
<python> "<skill-dir>/scripts/fundamentals.py" NVDA --html f.html   # responsive dashboard (the rich default — Preview Pane)
<python> "<skill-dir>/scripts/fundamentals.py" NVDA --html f.html --summary-out f.txt  # one gather -> dashboard + text twin
<python> "<skill-dir>/scripts/fundamentals.py" NVDA --open     # rich dashboard in the browser
<python> "<skill-dir>/scripts/fundamentals.py" NVDA --json     # compact data for a widget
<python> "<skill-dir>/scripts/fundamentals.py" NVDA --svg f.svg --png   # file + PNG (agent-side)
```
Invoke by the **absolute skill path** (cwd is the user's project, not the skill
folder). `--no-news` skips the slowest fetch. Flags compose.

**Key tier:** the 8–9 calls fire staggered (~0.15s apart, one 2s burst retry) — on a free
AV key (25 req/day · 5/min) ONE run eats a third of the day's budget and panels degrade
to n/a SILENTLY; a complete dashboard effectively needs a premium key. A hard throttle
exits loud, naming the limits ("Alpha Vantage throttled (free tier = 25 req/day · 5/min;
fundamentals fans out 8-9 staggered calls — premium recommended, or space the runs)").

**Do NOT pull statements through the MCP and hand-jq them, and do NOT reimplement
this** (matplotlib / bespoke SVG). The script already handles the overflow, the
concurrency, the None-holes, negative values, and provenance. The ONE sanctioned
MCP path is `--from-dir` below, for egress-blocked sandboxes only.

## Egress-blocked sandbox — the `--from-dir` MCP fallback

When direct HTTP egress is blocked (agent-mode / Home — same as the chart.py `--from`
case) the script can't fetch, but the AV **MCP** connector still can. Then:

1. Call each AV MCP tool for the ticker **with `return_full_data=true`** (so the big
   statement/news payloads offload to files instead of truncating): `COMPANY_OVERVIEW`,
   `GLOBAL_QUOTE`, `INCOME_STATEMENT`, `BALANCE_SHEET`, `CASH_FLOW`, `EARNINGS`,
   `EARNINGS_ESTIMATES`, `TIME_SERIES_DAILY_ADJUSTED` (outputsize=full), `NEWS_SENTIMENT`.
2. Put each result in one directory. The script finds files by a **substring** in the
   name, so you can drop the harness's overflow tool-result files in **as-is** (their
   names contain `INCOME_STATEMENT`, `BALANCE_SHEET`, …); save the small inline ones
   (overview, quote) to files whose names contain `overview` / `quote`.
3. Render from the directory — no HTTP:
   ```
   <python> "<skill-dir>/scripts/fundamentals.py" PLTR --from-dir <dir> --svg out.svg --png
   ```
Keywords matched: `overview`, `(global_)quote`, `income`, `balance`, `cash`,
`earnings`, `estimat*`, `daily` (the price history), `news`/`sentiment`. Overview + the 3 statements + earnings are required;
quote and news are optional (their panels degrade to n/a / "no news data"). The quote
is normalized whether it arrives as the REST `{"Global Quote":…}` object or the MCP
CSV-in-`result` shape.

Caveat (honest): in this mode the series DOES pass through context (the MCP pull is
unavoidable when egress is blocked) — that's the cost of the environment, not a choice.
Prefer direct HTTP (no `--from-dir`) whenever egress is open.

## Output modes — when to use each

| Mode | What | Who sees it |
| --- | --- | --- |
| (default) text | ~30-line summary: snapshot, valuation, trend, balance, capital return, EPS surprises, analyst, sentiment | the chat (always) |
| `--json` | compact JSON, heavy series EXCLUDED (priceOHLC/deep/epsDeep; `--json-full` includes them, ~100KB+) | feeds a widget |
| `--svg` / `--png` | self-contained landscape dashboard (8 panels) / its PNG | file / agent verify |
| `--html` | **truly self-contained** **responsive** Chart.js dashboard — the chart-panel grid REFLOWS one column when narrow, **two columns from 680px up** (capped at two, so the dense bar panels stay wide and readable instead of going tiny like the fixed-aspect SVG); embedded data + **Chart.js inlined (no network — the vendored `scripts/chart.umd.min.js` is embedded, so it renders in egress-blocked/in-app viewers too)** + dark-mode CSS | inline widget / expandable artifact / Preview Pane / in-app viewer |
| `--open` | writes the dashboard (HTML if `--html` was also passed, else SVG) and opens it in the **default browser** | the user, on their screen |

## Showing it to the user (READ THIS — one happy path, the rest on request)

A rendered dashboard does **not** appear in the user's chat just because you can see it.
There is ONE default, so the same thing happens every time — no surprise browser windows,
no minutes-long widget streams. (This is the user's standing preference; it is the
canonical rule for `chart.py` / `portfolio.py` dashboards too. **Options are the one
exception**: an option-chain analysis defaults to the **LIVE `--watch` side panel for a
single series** and a **static `--html` for `--term`** — see `references/options.md` /
`references/databento.md` Branch A2.)

**OpenAI desktop / Work (`market-data-host.json` says `{"host":"openai"}`):** use the
top-of-SKILL fast lane for both US and MOEX/Russian companies. Route US names to `fundamentals.py`
and MOEX names to `moex_fundamentals.py`. Render `--html` and `--summary-out` in the SAME command; immediately post
the large absolute HTML link in commentary as one localized clickable H2 (`## [Open …](<path>)` or
`## [Открыть …](<path>)`), with no separate heading and no `**` inside the file-link label; write the
read from the text sidecar; render one
deep-analysis block last. For a company, that one block always leads with initiating coverage and
always has a reporting-cycle second button: earnings analysis for a report published in the last
30 days, earnings preview when the next report is within 45 days, otherwise analysis of the latest
published quarter. An unavailable MOEX reporting date falls back to latest-quarter analysis; it
never removes the second button. Pass the current chat language as `defaultLanguage`; the block's shared
Russian/English selector applies to either action. The file link is
the default dashboard surface. Do not add the native
dashboard MCP App to the same turn unless the user explicitly requests that experiment, because two
MCP Apps can race when a completed/background task is restored. This OpenAI rule does not alter the
Claude/Cowork path below.

**THE DEFAULT — text + the Preview Pane (side panel) + YOUR written read:**
- **Always:** print the **text summary** to chat (it carries the NUMBERS; the data never
  entered context — the TAKEAWAY is yours to write, see "The written read" below).
- **For an "analyze / разбери" request** — a company or portfolio, where the
  visual IS the point — ALSO render `--html` to the **Claude Code Preview Pane**.
  (**Option chains have their own default** — live `--watch` for a single
  series, static `--html` for `--term`; see the exception above.) Recipe: write the HTML to `.preview/index.html`, point
  `.claude/launch.json` at a static server (`python -m http.server <PORT> --directory
  .preview`), `preview_start`. Concurrent-session note (v1.118.0): `autoPort: true` hands the
  free port ONLY via the `PORT` env var — it never rewrites args — so the repo's launch.json
  wraps the command as `sh -c '… http.server "${PORT:-8913}" …'`; a hardcoded port in
  runtimeArgs + autoPort silently binds the WRONG port under collision. It works under the
  sandbox (no Apple Events), reflows to fit (1 column narrow → 2 from 680px, capped), and you
  confirm it in-chat without leaving the app — the only rich surface you can both render AND
  verify.
- **Self-verify by DATA, not pixels (v1.117.3 — the user's AMD-session calibration).** In an
  ANALYSIS session (shipped renderer, no code changed) the render check is: the text twin
  already carries the same numbers BY CONSTRUCTION, so verify only that the page is ALIVE —
  `read_console_messages` clean (no JS death) and, if in doubt, `read_page` (DOM: tabs
  mounted, head value non-empty). **NO screenshots and no per-tab clicking** unless the user
  asks, or the cheap probe finds an anomaly (console error / empty DOM / 'warming' where the
  store should be warm) — then escalate to pixels. Screenshots stay MANDATORY in DEV sessions
  (renderer/producer code changed this session): Chart.js draws into `<canvas>`, which
  `read_page` cannot see — pixels are the only witness for chart/theme/layout changes.
  (Screenshot gotcha, when you do capture: use a FRESH server without `preview_resize`, or the
  frame lands top-left and looks tiny — that's the tool, not the dashboard; measure via
  `preview_eval` if unsure.)
- **For a point-fact lookup** — one number ("what's NVDA's P/E / ATM IV / Sharpe?") — **text
  only**, no dashboard.
- **Egress-blocked / embedded viewer (e.g. the Cowork in-app HTML preview)** — the `--html`
  file is **self-contained** (Chart.js is inlined, no CDN), so it renders fully there too; no
  fallback needed. This was a real bug pre-v1.41.4: the page loaded Chart.js from cdnjs, which a
  sandboxed viewer blocks → all canvases silently empty. Never reintroduce an external
  `<script src>` in a `--html` renderer.

**EXPLICIT-ONLY — do these ONLY when the user asks for them by name:**
- **External browser (`--open`)** — "открой в браузере / pop it out full-screen". Crisp, but a
  separate window you can't verify; blocked under the sandbox (Apple Events / -600 → run that
  command unsandboxed) and useless in agent-mode/Home (browser opens inside the container).
  URL uses `pathlib.as_uri()` so it's valid on Windows.
- **Inline `--json` widget in chat** — "виджет в чат". A small widget (metric row + one or two
  key charts); keep the header's right padding ≥ 40px (the host's "…" control sits top-right).
- **PNG via `Read`** — agent-side verify only; the user's client shows a file link, not the
  image. (Normally you verify via the Preview Pane screenshot instead.)

**Environment fallback — no Preview Pane (web / Home / CLI):** write the `--html` (or `--svg`)
file, give the path, and OFFER `--open` or an inline widget — but don't fire all of them.

**Never** re-emit a big rendered SVG through `show_widget` — it streams token-by-token for
minutes (the exact waste these scripts exist to avoid). The same rules apply to `chart.py`
price charts — see `references/charting.md`.

## The written read (REQUIRED for every "analyze / разбери" answer)

The dashboard shows the data; **your job is the "so what."** After the text summary and
the dashboard, write a short interpretive read — **3–6 sentences**, in the user's
language: what the numbers add up to, the one or two things that stand out, and what to
watch next. **Then END with the deeper-document OFFER** (SKILL.md "Neighbor plugins": pick
ONE matching `/equity-research:*` / `financial-analysis:*` document by context; on
desktop/Cowork render the one-click CARD via `<skill-dir>/scripts/offer.py` — check THIS
session's own skill list first per SKILL.md — PLUS the plain-text line naming the command).
This applies to the ETF read too when the fund is THEMATIC (→ `/equity-research:sector` on
the theme — the MSOS-session miss 2026-07-23: `:sector` was named in text but the card was
never rendered); plain broad-market ETFs get no offer. A read that closes without the
offer — or names the document but skips the CARD where show_widget exists — is incomplete
(the AMD-experiment and CAT/MSOS-session misses). Descriptive synthesis, not advice ("the
market is paying 30× for decelerating growth" — never "buy/sell"). Do NOT restate every
metric — the text summary already printed them; pick what MATTERS and connect it.

A good company read covers (pick the relevant ones, not all):
- **Valuation vs the story** — is the multiple (fwd P/E, PEG, EV/EBITDA) justified by
  the growth/margin trajectory, or is it pricing a different company than the trend shows?
- **Growth quality** — revenue vs EPS growth (buyback-flattered?), accelerating or
  decelerating (the TTM-by-quarter panel catches the turn ~3 quarters before annuals).
- **Margin direction** — TTM vs annual: expanding or compressing, and why it matters here.
- **Balance-sheet health** — net cash vs net debt, the Altman zone, which F-Score
  components fail (the text names them).
- **Earnings quality** — FCF conversion vs net income, SBC trend and dilution CAGR
  (GAAP profit that never becomes cash is the classic flag).
- **Capital allocation** — buybacks vs dilution, dividend payout as % of FCF.
- **Company story or SECTOR story** (v1.148) — the Overview `Sector · <name> (<SPDR>)` tile is
  the company's ~3-month return MINUS its own sector SPDR's, with both legs in the sub. A big
  move that the whole sector made is NOT a company insight: say which it is ("+18% on the
  quarter, but XLK is +13% — most of it is the sector"), and note when the two diverge
  sharply (that IS the company-specific signal). The tile is absent for an unclassified name
  or a fund — then just skip the point, don't improvise a proxy. A LOAD-BEARING sector story
  (a commodity producer, a rate-driven financial, a genuine sector divergence) is exactly when
  the `:sector` complement card is organic beside the company document — see the offer rule.
- **The flag** — the ONE number that breaks (or makes) the story; every read should name it.
- **Ownership (v1.84.0)** — did anyone BUY in the open market (breadth, C-suite, a cluster),
  and does 13F breadth (holder counts) agree with 13F weight (shares)? Say which view you
  trust and why. Never read routine selling as a flag on its own.
- **What to watch** — the next catalyst: earnings date, estimate-revision drift, the
  metric whose direction decides the thesis.

Worked example (the shape, not a template to fill verbatim):

> «По цифрам NVDA — это рост, за который рынок платит вперёд: выручка +94% YoY при
> валовой марже 75%, но fwd P/E 38 и PEG ~1.1 означают, что замедление до "всего" +40%
> уже ломает мультипликатор. Качество прибыли высокое — FCF-конверсия >100%, чистый кэш
> $34B, F-Score 8/9. Флаг один: TTM-панель показывает первый квартал замедления
> последовательного роста — если следующий отчёт подтвердит перегиб, это главный риск
> мультипликатора. Смотреть: ревизии оценок за 30 дней (пока ↑) и валовую маржу —
> консенсус закладывает, что 75% удержится.»

Anti-pattern (don't do this): re-listing VALUATION/GROWTH/HEALTH lines as prose, one
sentence per line, with no connection between them and no flag — that's the text summary
again, not a read.

## Reading the numbers — quirks to flag

- **ETFs now render (v1.83.0).** `fundamentals.py SPY` no longer exits — an empty `OVERVIEW`
  routes to the **ETF_PROFILE branch** (`kind='etf'`): a distinct, smaller page (expense ratio,
  net assets, div yield, turnover, inception, a sector-weights DOUGHNUT (v1.95.0: composition,
  not ranking — an explicit GREY "Unclassified / non-equity" slice closes it to 100%. WHY the
  gap (investigated 2026-07-15): AV's fund-sector table is a SEPARATE vendor feed, NOT derived
  from the holdings list — near-complete only for flagship US funds (SPY 96.3%, QQQ 96.1%,
  XLE 99.7%), partial-to-broken elsewhere (GDX 32.4% with phantom IT/Comm rows while AV's own
  OVERVIEW says Basic Materials for names covering 57%+; KWEB 0% despite a complete holdings
  list; EEM 1.4%; EWJ/IBIT none). The grey slice therefore mixes genuinely non-classifiable
  sleeves (swaps/cash/T-bills/bullion) with names the feed failed to map. Never normalize it
  away; point readers at the holdings bars for the reliable composition. Zero-weight GICS
  padding rows are dropped; the text SECTORS line carries the same "unclassified N%"),
  top-15 holdings weight bars + the
  split-adjusted price kline; v1.96.0 adds the FLEX HEAD (price + change in the header, a
  visible `leveraged` pill for TQQQ-class funds — never AV's raw "YES"), a **Distributions per
  share** bars panel (AV `DIVIDENDS` — works for ETFs; TTM sum + indicated yield ride the
  Div-yield card sub and the text `DISTRIB` line; a non-distributing fund gets NO panel), and
  the **News sentiment** block the gather already pulled (same relevance gate + sentibar as
  companies; text `SENTIMENT` line; v1.98.0: the distributions panel is per-YEAR totals as
  HORIZONTAL rows, newest on top — a single-payment fund reads as one honest row — and the
  drawdown x-axis shows YEAR-only ticks via `data.xYear`; v1.100.0: the price kline gains
  INTRADAY frames — 15m (one best-effort TIME_SERIES_INTRADAY pull, ~30 trailing days,
  UNadjusted) + a locally-bucketed 60m, buttons "15m · 60m · D · W · M"; timestamps are
  epoch-converted from the payload's exchange TZ (zoneinfo; bare-Windows falls back to fixed
  UTC-5 — spacing right, clock labels may shift 1h around DST); a throttle/entitlement miss
  keeps the D/W/M chart, never kills the page; v1.99.0: the Top-15 holdings TABLE
  became horizontal WEIGHT BARS — heaviest on top, labels "Name · SYM" title-cased, "n/a"
  placeholders never leak (nameless swap/cash rows read "—"), and every horizontal bar chart
  sizes its canvas to the row count (24px/row, both twins) so a row-stretched panel never shows
  a squeezed chart over dead space); v1.97.0 adds the fund IDENTITY — `SYMBOL_SEARCH` carries
  the full name (`ETF_PROFILE` has none): "MSOS — AdvisorShares Pure US Cannabis ETF" leads the
  About, the head names the fund, text line 1 too; the About is a PLAIN full-width tile right
  under the header (`about.top`, flat pages only — tabbed pages keep the §6.6 Overview tile);
  a search miss falls back honestly to "SYM ETF" + the generic opener). ETFs have no income
  statement, so there are no company panels —
  for issuer fundamentals, analyze a specific holding. A genuinely bad ticker (empty OVERVIEW AND
  no ETF profile) still exits honestly. `--svg`/text/`--html`/`--json` all work; SVG is a compact
  profile card. Indices still route to their ETF proxy.
- **Next-earnings date (v1.83.0).** `EARNINGS_CALENDAR` (horizon 3m, CSV endpoint) adds a
  **Next earnings** card ("MM-DD · Nd") + a text `EARNINGS  next report YYYY-MM-DD (~Nd out)` line
  — a forward estimate (issuers move it), best-effort (absent → no card). Front-month IV ramps
  into the print and crushes after (cross-reference the options `--history` earnings overlay).
- **The page is TABBED since v1.84.0** — [Overview | Financials | Ownership | Estimates & News]
  (the same generic sections engine as options `--full`; client-swapped, Overview server-rendered
  first). Overview = About + kline + TTM + ALL headline cards; Financials = the 6 statement
  panels; Ownership (only when the data is present) = insider + 13F; Estimates & News = analyst
  + sentiment. The ETF page stays flat. In DEV sessions the screenshot sweep is ONE SCREENSHOT
  PER TAB; analysis sessions verify by data+console only (the "Self-verify by DATA" rule above).
- **Ownership: insider transactions (v1.84.0).** INSIDER_TRANSACTIONS (heavy, ~2.5MB — fetched
  gzip, best-effort). AV carries NO SEC Form-4 codes, so classes are HEURISTIC: `buy` = priced
  acquisition within 15% of that day's close; `exercise` = priced far below it; `◦` = a priced
  sale offset by same-day unpriced grant shares (likely sell-to-cover — kept in the $ flow, out
  of the discretionary counts); unpriced rows = grants/vests (excluded from $ flow — the user
  decision: the $ signal counts ONLY priced rows). READ IT AS: buys are the signal (breadth —
  distinct buyers, a ≥3-buyers-in-90d cluster, C-suite buys); steady selling is the BASELINE at
  stock-comp-heavy companies, never a red flag by itself.
- **Ownership: 13F holdings (v1.84.0).** INSTITUTIONAL_HOLDINGS. 13Fs lag up to 45 days and
  filers report on their own clock (staleness anchors on the MODAL quarter; grey table rows filed
  older). **The rename artifact**: a very large 'new' position is often a manager re-filing under
  a new entity (the old row still counted) — the headline card is therefore **net EX-NEW**, and
  suspects (>0.5% of the total) are flagged, never silently dropped. Count-based breadth vs
  share-weighted flow often DISAGREE (thousands of small trims vs one giant add) — both are
  shown. ◇ = index-heavy managers (mechanical flows). Long US-listed positions only — shorts/
  swaps invisible; ADR ownership % understates.
- **Options-vol term strip (v1.89.0; ZERO AV calls — was one grouped tile in v1.87).** A
  FULL-ROW table leading the Overview panels: rows **30d/60d/90d** constant-maturity ATM IV,
  columns IV / IV rank / percentile / realized HV (paired windows 30↔20d, 60↔40d, 90↔60d) /
  VRP — read from the LOCAL opthist settlement store (nightly-prewarmed, ~590-name universe)
  + realized vol from the daily series already fetched. The tenor COMPARISON is the point
  (front-rich vs contango at a glance); settlement as-of rides the title; the full drill is
  `options.py SYM --full`. A name that is optionable but UNWARMED gets a text hint
  (`VOL … run options.py SYM --history`), a non-optionable name gets nothing (no fake holes).
  ETF pages get the strip too (SPY/QQQ are optionable). The text VOL line carries 30d headline
  + 60/90 IVs.
- **ETF series stats (v1.92.0, zero extra calls).** The ETF page computes from the already-fetched
  daily series (adjusted close): 52w range, vs 200d MA, MAX DRAWDOWN (+ current from-peak),
  avg $ volume (3m, the liquidity gauge), a colored Returns strip (1M/3M/6M/1Y/3Y + ITD/y CAGR),
  a monthly-resampled drawdown panel, and a Top-10 concentration card. Text twins: RANGE /
  RETURNS / DRAWDOWN lines. Too-short series (<30 sessions) -> no cards, never fakes.
- **Cross-links (v1.87.0, self-materializing since v1.94.0).** Render per-ticker files into ONE
  directory — the preferred convention is `<sym>.html` (fundamentals) and `<sym>_options.html` (options),
  e.g. `.preview/aapl.html` ⇄ `.preview/aapl_options.html` — and each page gets a header chip to
  its sibling (`AAPL options ↗`). Since v1.159.1 a descriptive OpenAI fundamentals filename
  (`texas-instruments-txn-dashboard.html`) is passed verbatim to the options child, so its reverse
  chip returns to that real file instead of guessing `txn.html`. The parent file is materialized
  before the child starts, removing the background-render race. On a LIVE render a MISSING sibling is rendered automatically in
  the background (stderr notes it) and the chip bakes hidden (`pending`) — the page HEAD-polls the
  file (~86s) and reveals the chip when it lands, so no dead link ever shows and no manual
  re-render is needed. On `--from-dir` (offline) nothing spawns — there the old workflow stands:
  render both, then RE-RENDER the first for bidirectional chips. A non-optionable name's options
  chip simply never appears (the background render exits loud, the file never lands). The options
  sibling is always the **`--full` tabbed page** — never a single-series chain render
  (single-expiry drills go under a suffixed name). Relative hrefs are the one navigation the
  embedded pane allows (HEAD-fetch probed OK in the Preview Pane 2026-07-14; in the COWORK
  viewer the check resolved NEGATIVE on the live AVGO session 2026-07-18 — the poll is blocked,
  pending chips stay hidden there forever, hence the playbook's options-FIRST render order;
  same on a `file://`-opened page — fetch is origin-blocked, reload after the
  sibling lands bakes the normal chip). Side effect to know: the background options render
  `accrete()`s the name into the nightly prewarm universe — a one-off fundamentals look enrolls
  the ticker's vol history going forward (RPM-only key, deliberate). Served `--watch` pages
  never carry links. Keep copying the latest render to `index.html` as the pane entry point
  when that's how you opened it.
- **52w range + MA cards (v1.82.0).** Two cards close the card grid: **52w range** (price
  position in the 52-week span, 0..100% CLAMPED — a fresh 52w break prints 0/100, never an
  overshoot) and **vs 200d MA** (signed %, the 50-day rides the hint). Text gets a matching
  `RANGE` line (only the segments OVERVIEW actually carries). All four fields were parsed
  since v1.47 but rendered by NO output — n/a-safe, never a fake 0.
- **Estimates & the v1.47 additions.** The dashboard also shows: div yield/per-share/
  ex-date (OVERVIEW) + a dividends series (CASH_FLOW `dividendPayout`) beside buybacks
  (capital return); rev/EPS YoY growth; float + insider/institution ownership; R&D %% of
  revenue trend; latest-quarter inventory + current ratio; and forward EPS estimates with
  30-day revision counts (`EARNINGS_ESTIMATES` — real horizons are generic "fiscal year"/
  "fiscal quarter" ordered by date, NOT the docs demo's "current/next fiscal year"; some
  tickers return an EMPTY estimates array (MU 2026-07-04) — the EST line/panel degrades
  silently, that's expected).
- **Price panel = KLineChart (v1.53–54), FIRST panel, collapsible.** A vendored, self-hosted
  KLineChart v9.8 (`scripts/klinecharts.min.js`, inlined only when a `kline` block is
  present) — real candlesticks with split-adjusted OHLC **+ volume** (from
  TIME_SERIES_DAILY_ADJUSTED, resampled D/W/M), native scroll-zoom (= candle width) +
  drag-pan + OHLCV crosshair, D/W/M frame switch, and interactive indicator toggles: clean
  default = two MAs (calcParams [20,50]); MA/EMA/MACD/RSI/BOLL/VOL toggle on click; a ⚙ gear
  opens a per-active-indicator param editor (overrideIndicator). Title carries the company
  name; the y-axis price precision drops to 0 decimals for >$100 names (no ".00" eating axis
  width); a ▾ chevron collapses the WHOLE ROW (v1.56: chevrons live on row-start
  panels only; full-width panels fold alone); a `log` button toggles log/linear scale;
  BOLL ships WITHOUT its lower band (a negative DN on parabolic history broke scaling —
  and blanked the pane under log); candle-pane indicators STACK (isStack=true — false
  silently wiped pane-mates, review 2026-07-04); indicator sub-panes are height-bounded;
  after zoom/scroll settles (~800ms) the last candle snaps back to the right edge if a gap
  opened; candles are SPLIT-only adjusted ("8. split coefficient" — dividend adjustment
  redrew old candles below traded levels); the FCF panel splits into OCF + negative capex
  bars with an FCF line. Chart.js stays the engine for
  every business panel; the static SVG page falls back to a plain candle SVG. KLineChart is
  outside the Chart.js CHARTS registry, so the grid relayout resizes it explicitly, and
  klBuild disposes-before-init (idempotent). Annual histograms default to 9 columns (TTM 19).
- **Derived block (v1.48, computed locally — no extra calls).** TTM-by-quarter panel
  (rolling 4-quarter revenue/margin/FCF — catches a cycle turn the annual rows miss by
  ~3 quarters); FCF quality (conversion = FCF/NI, FCF yield vs cap, SBC-adjusted FCF);
  Piotroski F-Score /9 (failed components named in HEALTH) and Altman Z (safe >3 / grey /
  distress <1.81); net-debt/EBITDA ("none (net cash)" when cash-positive), dividend payout
  as %% of FCF, share-count CAGR. Cards F-Score/Altman Z/FCF yield are HTML+text (the SVG keeps its compact 9-card
  strip). EVERY chart panel carries +/\u2212/\u21ba range buttons over the FULL fetched history
  (annuals ~20y, EPS surprises up to 60q, TTM all ~78 windows); defaults stay compact
  (12 FY annual / 14 EPS quarters · 19 TTM windows · 60 monthly candles (short-history: <8 monthly bars auto-defaults the kline to D; all-empty chart panels are dropped from the grid; %-lines drop |v|>500% points (treasury micro-caps) and sub-$0.5B panels auto-rescale to $M — text/cards keep exact numbers). v1.60: an About strip (OVERVIEW Description, collapsed to one line) sits between the cards and the kline; Analyst view expands to a consensus table (EPS avg/low–high, Δ30d drift, ↑/↓ revisions, analysts, revenue — curQ/nextQ + FY horizons, Δ7d AND Δ30d drift, revenue range + analyst count on hover; AV has NO broker names/notes or target distributions). External links open IN the built-in browser pane (v1.87.1 — location.href assignment is the one navigation the pane allows; the pane's ← returns; the old Open/Copy menu is retired)) and News sentiment expands to the top-10 relevance≥0.75 headlines (chip, source · date · score; the summary is a hover tooltip, not inline text; box capped at 300px). The mood AGGREGATE honors the same 0.75 gate and says so in its caption; a grey 'full feed' line shows the UNGATED mean beside it (sampling probe 2026-07-04: high-relevance slices skew bullish on mega-caps — focused coverage is opinionated, roundups are Neutral-heavy). The AV tier caps the feed at the 50 LATEST articles regardless of limit=, so the tile title reports the REAL span (e.g. 'last 2d' on MSFT, not the requested 14d). Per-article sentiment/relevance are ALPHA VANTAGE's scores (external NLP); our only math is the gated mean + AV's own +-0.15/+-0.35 label bands. Headlines are blue external links (target=_blank); the subtitle domain is a clickable link too. Each panel carries a concise `info` string shown via an ℹ button (4th in the zoom cluster) that toggles a `.panelinfo` box. The kline defaults MINIMAL (220px base; VOL on by default = 330px effective) and grows +110px per open VOL/MACD/RSI sub-pane (klFit), and its toolbar is two wrapping tag-clouds (frames | indicators+log+gear+TradingView, no text labels) and carries a TradingView link (tradingview.com/symbols/EXCH-SYM); a drag-grip below the chart resizes it in small bounds. Panel explainers are hover-tooltips on the TITLE (dotted underline), not on the price chart. External links navigate the built-in browser pane in place (v1.87.1; back via the pane's ← control). Snap-to-right is scale-aware and guards active drag + history-browsing. PERFORMANCE (measured 2026-07-04): the 9 AV calls run in PARALLEL (wall = slowest ≈ 2.7s; DAILY_ADJUSTED full = 1.6s/1.4MB is the biggest); to_spec 0ms, render 4ms — rendering is never the bottleneck, refetch-per-iteration + AV throttle waits were. THE DEV LOOP: fetch once with `--save-raw DIR`, iterate offline with `--from-dir DIR` (0.09s, 0 calls, no throttle). av() retries one read-timeout per endpoint and exits cleanly naming the endpoint on a persistent outage (AV died mid-session during the probe). v1.62 review-gate honesty rules: TTM windows require CONTIGUOUS quarters (<=330d span, else None); margins computed independently (a bank's missing grossProfit no longer kills net/op) with the basis named in each card hint; missing OVERVIEW fields render n/a, never 0%/$0B; Altman Z is n/a for financials by construction; net debt/EBITDA distinguishes 'none (net cash)' from n/a; non-USD reportedCurrency suppresses cross-currency composites (FCF yield/Altman/ND-EBITDA) and is named in the subtitle; estimates keep ended-but-UNREPORTED periods (watermark = latest reported quarter); buybacks prefer paymentsForRepurchaseOfCommonStock (gross) and dividends prefer the common-stock field; all-sub-gate news renders 0-of-N with the full-feed mean; EPS-surprise clamps +-60% (declared in the title) on BOTH svg and interactive paths. GOTCHA: the AV news feed is served INCONSISTENTLY during their ingest bursts (AMD probe 2026-07-04: the same query returned 14 -> 1 -> 14 relevant-of-50 within an hour, window span jumping 3d->8d->3d) — a suspiciously empty headlines list on a covered name is usually a transient AV response; re-render before suspecting the pipeline), text & SVG stay on the default windows.
- **Foreign companies: fundamentals need a US listing that files with the SEC.** `OVERVIEW`
  covers US-domiciled names AND foreign companies with a proper **US-listed sponsored ADR**
  (Toyota `TM`, Honda `HMC`, Alibaba `BABA`, TSMC `TSM`, SAP `SAP` → full OVERVIEW). It does
  NOT cover: home-exchange-only listings (Hyundai's Korean `005380.KS` — AV won't even return
  it in `SYMBOL_SEARCH`), foreign local listings (`HYUNDAI.BSE`), or **unsponsored OTC/grey-
  market ADRs** — those return a live price/`GLOBAL_QUOTE` but an **empty `OVERVIEW`**
  (verified live 2026-07-04: `HYMTF`/`HYMLF` = Hyundai OTC ADRs → 0 OVERVIEW fields, quote
  only; `TM` = Toyota NYSE ADR → 55 fields). So for a foreign name: resolve to its
  sponsored US ADR first (`SYMBOL_SEARCH`, pick the NYSE/NASDAQ line, not `.XXX`/OTC); if only
  an OTC ADR exists, fundamentals aren't available — offer a price chart (`chart.py <ADR>`) or
  a peer that IS US-listed (Hyundai → no fundamentals; Toyota/Honda work), and say so plainly.
- **Margins: ONE basis, computed TTM.** The metric strip / QUALITY line shows gross/op/net
  computed from the LAST 4 QUARTERLY income reports (one consistent basis); the TREND rows
  are per-fiscal-year. Never mix them: in a fast cycle the fiscal year goes stale mid-year
  (MU 2026-07-04: annual gross 40% sat beside TTM net 56% — impossible-looking, both "true").
  AV's ready-made `OperatingMarginTTM` field is NOT trusted (MU: field said 80.4%, computed
  65.8%); `ProfitMargin`/`OperatingMarginTTM` are only the fallback when quarterlies are
  missing. A newly-profitable name's TTM net margin can still be flattered by a one-time
  deferred-tax release — cross-check against the computed operating margin.
- **Cash-flow quarterly `netIncome` can mismatch the EPS-implied figure** — an AV
  normalization quirk. Trust OCF/FCF and the annual numbers.
- **Net cash** = cash + short-term inv + long-term inv − total debt (the script sums the
  explicit balance fields because AV's `cashAndShortTermInvestments` is often cash-only).
  Negative = net debt (shown with its sign). The CARD uses the **latest QUARTERLY balance**
  (dated in the QUALITY line); the BALANCE trend rows stay annual. Annual-only was 10 months
  stale for MU (showed −$3B vs the real +$23.8B — a sign flip across 3 supercycle quarters).
- **SBC & dilution.** The "Stock comp ($B) · % of revenue" panel pairs stock-based comp
  (from CASH_FLOW) with its % of revenue — the engine behind share-count growth. A high
  but **falling** %-of-revenue means dilution is decelerating as the company scales.
- **EPS-surprise outliers.** A tiny estimate makes the % explode (e.g. −14,890%); the
  panel caps the bar scale and compacts the label to `±Nk%` so one outlier doesn't flatten
  the rest. The number is real, not a bug.
- **Segments** (e.g. gov vs commercial) are NOT in AV — say so rather than inventing them.

## Reporting & safety

- State provenance in TEXT next to any chart (source, as-of date, split-adjusted).
- Prices come from a live `GLOBAL_QUOTE` (realtime entitlement); fundamentals are
  as-of the latest reported quarter — say both.
- This is **data analysis, not investment advice** — keep that framing, especially on
  valuation/target calls.

## Offline test

`<python> scripts/test_fundamentals.py` checks the pure pieces (esc, None-safe float,
formatter, text+SVG rendering incl. negative bars and the EPS-outlier clamp) with no
network — wire it into CI alongside `test_chart.py` / `test_greeks.py` /
`test_portfolio.py` / `test_options.py`.

## Cowork playbook (agent-mode sandbox — Palantir + AMAT session lessons)

0. **Cross-link chips are AUTOMATIC in Cowork now (v1.123.2; cloud detection fixed v1.125.1).**
   `dashboard.in_cowork()` auto-detects LOCAL agent-mode (the `/sessions` mount) AND the CLOUD
   Cowork VM (the `/mnt/*` workspace mounts; `MARKET_DATA_COWORK=1/0` is the override for odd
   sandboxes — the INTC session 2026-07-23 proved `/sessions` alone was a cloud FALSE-NEGATIVE:
   a v1.124.0 render took the desktop spawn+pending path and the chip hid forever), and a MISSING
   sibling is rendered INLINE (synchronous, `dashboard.render_sync`) so its chip is a REAL link on
   first paint. This replaces the v1.119.0 "options-first" dance: pre-v1.123.2 the chip baked
   `pending` and relied on a detached background spawn + a page HEAD-poll — dead in Cowork (the
   spawn CAN run in the cloud VM — INTC's sibling landed fine — but the file-preview viewer blocks
   the reveal poll), so it stayed hidden FOREVER. Now just render EITHER page — its sibling + a
   working chip are produced for you. Rendering BOTH into the same dir still gives BIDIRECTIONAL
   chips (a single render's freshly-made sibling page lacks the reverse chip until the other side
   is rendered / re-rendered). Then present BOTH files from that one dir — in the CLOUD viewer the
   preview serves an UPLOADED copy of the page, so a chip's relative href can only resolve against
   a sibling that was ALSO presented; presenting one file leaves even a real chip dead (the INTC
   session presented only `intc.html` while `intc_options.html` sat unshared in the sandbox).
   v1.127 (the MA-session feedback: «дашборд тоже не открывается»): in Cowork the sibling is
   now EMBEDDED — each of the two files carries BOTH boards, and the header chip is a
   client-side BOARD SWAP (one click, instant, zero navigation — verified live: fundamentals ⇄
   options both ways, charts rebuilt, console clean). File-to-file navigation stays impossible
   in the viewer (signed claudeusercontent URLs — the QCOM 404), but it is no longer needed:
   present EITHER file (both still land in the outputs dir; presenting both just adds the
   viewer's own file switcher on top). The v1.126.1 HEAD-probe + ask-copy ladder remains the
   fallback for OLD pages and for plain file links.
1. **PROBE, don't assume egress is blocked — in Cowork it usually ISN'T.** Run the script with
   `MARKET_DATA_NO_NET=auto` (e.g. `MARKET_DATA_NO_NET=auto <python> scripts/fundamentals.py SYM
   --html out.html`): it probes egress ONCE and self-routes — egress up → a direct fetch (skip the
   whole MCP dance below); egress blocked → it fast-fails instantly to the `--from-dir` route
   instead of burning ~60s of timeouts. `=1` is a manual force-off; `=auto` is the safe default
   that never guesses wrong. Assuming "no egress" and skipping the script is the AMAT-session bug.
   Only if the probe reports blocked do steps 2–4 apply:
2. Fetch via the AV MCP connector, all 9 tools, `return_full_data=true`:
   COMPANY_OVERVIEW, GLOBAL_QUOTE, INCOME_STATEMENT, BALANCE_SHEET, CASH_FLOW, EARNINGS,
   EARNINGS_ESTIMATES, TIME_SERIES_DAILY_ADJUSTED (outputsize=full), NEWS_SENTIMENT
   (tickers=SYM, limit=50, sort=LATEST, time_from=14d ago). Big responses overflow to
   tool-result FILES automatically — copy/symlink them into one dir AS-IS (filenames keep the
   endpoint word, find() matches by substring; the MCP CSV wrappers for quote AND the daily
   series are normalized by _norm_quote/_norm_px — no manual reshaping, that was the 9-minute
   sink). Do NOT read payload files into context — pass PATHS only.
3. Small inline results (overview, quote, sometimes earnings/estimates — whatever did NOT
   overflow to a file) — write ALL of them in ONE bash command chaining heredocs
   (`cat > overview.json <<'EOF' ... EOF` × N in a single call), NOT per-file Write calls:
   the Amazon session spent ~1 min on 4 separate Writes where one bash call does it.
4. Render offline: `<python> scripts/fundamentals.py SYM --from-dir DIR --html out.html`
   (~0.1s), show with present_files (side panel). DIR and out.html live under the SESSION
   outputs dir (or /tmp) — never inside the user's repos or selected folders: stray
   *_dashboard.html in a project root is one `git add -A` away from being committed. No dev server: sandbox ports aren't
   reachable from the user's browser. The dir doubles as the ticker's cache (= --save-raw
   format; Symbol mismatch exits loud).
4b. OPTIONS in Cowork: same route — REALTIME_OPTIONS via the MCP connector with
   `datatype: "json"` EXPLICITLY (the connector DEFAULTS to csv, which the chain loader does
   not parse; probed 2026-07-05: the raw json overflow file renders end-to-end via
   `options.py SYM --from-dir DIR`, 25 expirations, zero reshaping) + optional GLOBAL_QUOTE.
   LIVE --watch does NOT work in the side panel and never will there: the SSE server binds
   loopback INSIDE the sandbox (no port forwarding to the user's browser) and the poll
   needs egress. Live smile = Claude Code / local terminal with --open. In Cowork offer a
   REFRESH-ON-REQUEST instead: re-pull the chain via MCP -> re-render -> present_files again.
   FUTURES smiles: the Databento connector IS reachable in Cowork — as an MCP server
   (get_futures_options_smile returns the SAME Chain JSON as /smile/:root.json; shared
   snapshot builder, verified in its source). Unreachable is only the SCRIPT's own HTTP
   poll (host loopback + egress). Gap: futures_options.py lacks --from-dir to consume a
   saved Chain JSON — until added, futures smiles in Cowork = the MCP tool's own text/JSON.
4c. LIVE in Cowork — the BRIDGE artifact (probed 2026-07-05: window.cowork.callMcpTool
   works in side-panel artifacts; MCP tools run HOST-side). For futures smiles:
   `futures_options.py ROOT --from-dir DIR --artifact page.html` renders the full dashboard
   whose page polls get_futures_options_smile every 2s (dashboard.POLL_DEFAULT_S) (same applyDelta patch path as SSE)
   -> open with create_artifact. Smile/OI/headline cards tick with an honest asOf badge
   (tick <120s / delayed / settlement — never claims live on stale); greeks cards + the
   chain table stay as rendered (Black-76 lives in Python) and the subtitle says so.
   True tick-liveness requires the host connector running --live; otherwise settlement
   snapshots, honestly badged. The bridge envelope is {content, structuredContent, isError};
   read structuredContent ?? JSON.parse(content[0].text).
5. NEVER build a hand-rolled 'live' substitute panel — the full dashboard IS the product
   (the Palantir-session self-made artifact was rejected by the user and violates this doc).
