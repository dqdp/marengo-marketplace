# Options Reference (analyze an equity/ETF option chain)

Open this when the user wants to **analyze an option chain** for an equity or ETF —
"what's the IV / skew on SPY", "show me the smile", "where's max pain", "put/call ratio",
"open interest by strike". This is the chain-analysis playbook; it is **equity/ETF only**
(Alpha Vantage, native greeks). For options on FUTURES (ES, CL, GC…) use Databento +
`scripts/greeks.py` (Black-76) — that's Branch A in `references/databento.md`, NOT this.

## Use the script — `scripts/options.py`

```
<python> "<skill-dir>/scripts/options.py" SPY                       # nearest usable expiry, text
<python> "<skill-dir>/scripts/options.py" AAPL --expiry 2026-07-17  # a specific expiration
<python> "<skill-dir>/scripts/options.py" SPY --term                # chain page + CROSS-expiration analytics
   # appended (combines with --expiry/--window): ATM-IV/skew term curve + moneyness×expiry IV surface +
   # series table — from the SAME single REALTIME_OPTIONS payload (AV sends every expiration anyway).
   # Zero extra calls; one-shot (--watch doesn't apply).
<python> "<skill-dir>/scripts/options.py" NVDA --full --html out.html   # COMBINED tabbed page: Overview·Chain·Term·History
   # the DEFAULT answer to a bare/ambiguous "analyze options on X". Overview = the cheap/rich synthesis
   # (IV-rank tiles + a by-tenor 30/90d IV-range rank track + CM-IV history); Chain = the per-expiration drill
   # (smile/OI/GEX/ladder) with an expiration <select> listing EVERY expiration; Term = the cross-expiration
   # surface (sqrt-time ATM-IV/skew curve + IV+OI surfaces + all-expirations series table); History = --history.
   # Overview+History are cache-first from the opthist store — a COLD name shows "warming" placeholders + is
   # queued for the NIGHTLY prewarm (no in-run backfill); run `options.py SYM --history` to fill them now.
   # Store ladder (v1.93.0): MARKET_DATA_STORE env -> <skill-root>/store/options/ if present ->
   # ~/.cache/market-data/options. Cowork: the store RIDES THE SKILL DIR (the sandbox home is throwaway) —
   # a warm <skill>/store/options/ makes Overview/History work there; check IT before calling a name cold.
   # Chain+Term from ONE live chain pull. Client-swapped tabs. Add --watch [N] to STREAM the Chain tab live
   # (SSE, the pinned series; the switcher STAYS and LIVE re-pins to whichever expiration you pick); the
   # Overview/Term/History tabs stay the one-shot pull.
<python> "<skill-dir>/scripts/options.py" NVDA --history --html out.html  # RETROSPECTIVE vol history (the "cheap/rich vs its past" panel)
   # IV rank/percentile, const-maturity IV history, vol cone, 25Δ-skew & PCR history, term-through-time
   # heatmap, earnings overlay — from a HISTORICAL_OPTIONS settlement backfill (opthist store; --years N,
   # default 2 so the window control's "All" spans >1Y). A dashboard-level window control (All/1Y/6M/3M/1M).
<python> "<skill-dir>/scripts/options.py" AAPL --list-expiries      # pre-check: date · DTE · strikes
<python> "<skill-dir>/scripts/options.py" --prewarm [--daily-once]  # warm the vol-history store for the whole
   # universe (v1.85: the bundled 76-ticker seed + S&P 500/Nasdaq-100 constituents accreted via
   # `--accrete-etf SPY,QQQ` + crypto ETFs IBIT/FBTC/ETHA/... — ~580 names, 1y deep for the tail).
   # --daily-once = exit free if already completed today (the laptop-friendly launchd job runs
   # RunAtLoad + every 6h instead of a fixed 04:00 a powered-off machine would miss).
<python> "<skill-dir>/scripts/options.py" --accrete-etf SPY,QQQ     # add an ETF's constituents to the universe
<python> "<skill-dir>/scripts/options.py" QQQ --html chain.html     # responsive dashboard
<python> "<skill-dir>/scripts/options.py" SPY --window 25 --svg chain.svg --png
<python> "<skill-dir>/scripts/options.py" CL --equity               # the equity NAMESAKE of a futures root
```
Invoke by the **absolute skill path** (cwd is the user's project, not the skill folder).

## Routing — the strike→expiry→time taxonomy (which panel?)

An option chain has three analysis axes, each adding a dimension:

- **strike** → the **smile** for ONE expiration (skew / max-pain / PCR / OI *right now*): `--expiry D`.
- **+ expiry** → the **term structure** across *today's* expirations (the surface): `--term`.
- **+ time** → the **retrospective** — is vol cheap/rich vs its OWN past (IV rank, cone, skew/PCR
  trend, term-through-time): `--history`. **NB:** `--term` is a *today* snapshot, NOT the retrospective
  — the real retrospective is `--history` (this used to be mislabelled).

"Проанализируй опционы на X" / "analyze AAPL options" is ambiguous. Route it:

1. **Bare / ambiguous → `--full`** — the combined tabbed page (Overview · Chain · Term · History) is the
   default answer (a synthesis, not one of the three axes), so you don't have to guess. Overview leads
   with the cheap/rich read; the tabs cover every axis. No pre-pull question needed — the Chain tab lists
   every expiration in its switcher.
2. **A clear single intent → that ONE panel:** a date / "nearest" / "front month" → the single-series
   smile (`--expiry D`); "term structure" / "по всем экспирациям" → `--term`; "IV rank" / "cheap or
   rich" / "historical vol" → `--history`.
3. **Genuinely split and you need a date for the single-series path → ASK**, after a cheap pre-check:
   run `--list-expiries` (ONE call — `date · DTE · strikes`, nearest marked), present it, and offer the
   **nearest with DTE ≥ 1** as the default. `--list-expiries` reads the chain once and exits without
   rendering, so it's the cheap pre-check.
4. **Single series ⇒ always pass `--expiry`** (never bare `options.py SYM`). With `--expiry`
   set, `gather()` pushes AV's server-side `expiration=` filter → it pulls **only that one
   series** (~19× smaller payload; the whole ~14k-contract surface, ~1 MB, is otherwise
   pulled just to discover the expiration list). `--watch` does the same after its first
   discovery poll (it PINS the expiry, then re-polls only that series), so `--watch --expiry
   <date>` keeps EVERY poll on the filtered one-series path. The whole surface is pulled
   ONLY for `--term` and the one-off `--list-expiries` — both genuinely need every series.

## Routing guards (refused inputs — by design)

The script classifies FIRST and refuses symbols it would MIS-SERVE (exit 1, before
any AV call):

- **Known futures roots are REFUSED** unless `--equity`. `CL` here is
  Colgate-Palmolive, not crude — a plausible, correctly-formatted, WRONG chain
  (confirmed live). The error names both routes:
  `options on the future -> <python> "<skill-dir>/scripts/futures_options.py" CL` ·
  `the equity namesake   -> re-run with --equity`. Same for ES/NG/SI/HG… (everything
  in `futures_options.OPTION_ROOTS`) **plus the `NAMESAKE_ROOTS` set** — collision
  roots whose futures options are NOT on GLBX (ICE softs `KC`/`CC`/`SB` + `SIL`): their
  error offers ONLY `--equity` (no futures route on this subscription). GLBX-listed
  collision roots (ZM, BZ, HE, MGC, BTC…) live in `OPTION_ROOTS` since the 2026-07-04
  audits (`docs/collision-audit-2026-07-04.md`, `docs/databento-audit-2026-07-04.md`).
- **Cash indices are refused with the ETF-proxy suggestion** (index options aren't
  on Alpha Vantage): SPX/SPXW/XSP → SPY, NDX → QQQ, RUT → IWM, DJX → DIA — "confirm
  the proxy with the user first".
- **VIX is refused with NO proxy**: "'VIX' options price off VIX FUTURES, not spot —
  no clean ETF proxy exists and a spot smile would mislead; not rendering one."

One command fetches `REALTIME_OPTIONS` (the WHOLE chain — every expiration, ~14k
contracts, **far over the MCP token limit**) + a `GLOBAL_QUOTE` spot **directly over
HTTP**, filters to ONE expiration, and reduces it to a compact set of numbers inside the
script. This is the options analogue of `chart.py` / `fundamentals.py`: the giant chain
never enters context. **Greeks/IV are AV-native — read them, never recompute** (greeks.py
is only for Databento futures options).

## What it computes (default = the FULL tradeable ladder; `--window N` zooms)

- **Strike set** — the DEFAULT is the **full tradeable ladder**: every strike with
  open interest on either side (drops dead never-traded listings, keeps the whole
  real chain — NOT a ±window). An explicit `--window N` ZOOMS to ±N strikes around
  ATM; it is never the default.
- **Volatility smile** — call & put IV vs strike. The dashboard plots the **OTM leg on
  each side** (puts below ATM, calls above): the ITM legs are illiquid and their IV is
  stale noise, so the standard market smile uses OTM. (The text/JSON keep both raw legs.)
- **ATM IV** — average of the call & put IV at the ATM strike (nearest to spot).
- **Expected move (v1.82.0)** — the 1σ band priced by this expiration: spot × ATM IV ×
  √(DTE/365), shown ±$ and ±% (card + text). Plain arithmetic on the AV-native ATM IV
  (nothing re-solved); degenerate inputs (no IV / 0 DTE) → n/a, never a fake 0. This is
  the number the written read translates IV into English with.
- **25-delta skew (risk reversal)** = IV(25Δ put) − IV(25Δ call), in vol points. Positive
  = put skew (the usual equity shape: downside puts bid over upside calls).
- **Put/call ratio** — by open interest and by volume (over the whole expiration).
- **Bid/ask spread (v1.82.0)** — per-strike OTM-leg spread as % of mid (put wing below
  ATM, call above; the tighter leg at the money): the **Spread** column on the chain
  table + the **median OTM spread** in the text — the tradeability read ("this smile
  point exists" vs "this contract is actually tradeable"). No two-sided quote → n/a
  (an honest hole, never 0%). Wide spreads = trust the smile shape less.
- **Smile-relative rich/cheap (v1.83.0)** — the **vs smile** column: each strike's OTM-leg IV
  minus a LOCAL smile fit (linear interpolation of its nearest quoted OTM neighbours, excluding
  itself), in vol points. `+` = the strike's IV pokes ABOVE its neighbours (locally rich), `−` =
  below (cheap). This is rich/cheap **relative to this chain's own smile, not a model** — AV's
  `REALTIME_OPTIONS_FMV` (model fair value) is NOT entitled on this key (verified Unauthorized
  2026-07-13), so the user chose the smile-relative substitute. An unbracketed edge stays n/a
  (never extrapolate a wing). Read it as "which contract looks off vs its neighbours", not a
  valuation — a big residual is usually a stale/illiquid quote (cross-check its Spread).
- **VRP — IV vs realized (v1.83.0, `--history`/`--full`)** — a **VRP · 30d** card (30d
  constant-maturity IV minus 20-day realized/historical vol, vol pts) + a grey **realized HV20**
  dashed overlay on the constant-maturity IV history. Positive = options price more vol than the
  stock delivered (the variance risk premium — the usual state, sellers earn it); negative =
  realized is running hotter than implied (e.g. right after a big move). Realized comes from the
  daily adjusted-close series (`TIME_SERIES_DAILY_ADJUSTED`, one call, best-effort).
- **Max pain** — the settlement strike that minimizes total payout to option holders
  (writers' max-profit strike), computed across all strikes in the expiration.
- **Open-interest profile** by strike (calls vs puts), with the max-pain strike marked.
- **Per-strike θ/vega (v1.130.0)** — the **Vega** and **Θ/day** chain-table columns: the
  OTM leg's AV-native greeks (the Spread column's wing rule; MEAN of the legs at the
  money). Vega = $ per share per 1 vol pt, Θ = $ decay per share per calendar day
  (×100 per contract). Where the vol exposure sits / what holding the strike costs.
- **OI · Δ — "the book with caps" (v1.130.0, merged v1.131.0, windowed v1.132.0)** —
  the OI panel itself carries the change as STACKED segments per side: base = what
  carried over (min of today/baseline), a green cap = contracts BUILT since the
  baseline, a translucent red ghost up to the baseline level = UNWOUND. One scale
  (contracts): the segment sizes ARE the ΔOI, read in context of the wall they
  changed; hover for exact numbers. A **window toggle (1d · 1w · 1m · 1Q)** top-right
  picks the horizon (1w = 5 trading days; 1m/1Q = a calendar month/quarter ago snapped
  to a trading day) — each window is ONE immutable `HISTORICAL_OPTIONS` pull, disk-
  cached forever in `~/.cache/market-data/droi` (≤4 pulls per symbol-day, then free;
  `MARKET_DATA_DROI` overrides the dir or `=0` disables). The active window's BASELINE
  DATE rides the panel title (`· Δ vs YYYY-MM-DD (1m)`). On `--full` the window toggle
  and the expiration switcher are orthogonal (the baseline date is one per symbol, so
  it changes only on a window swap). OI publishes once daily (a stable intraday
  baseline); an all-green bar = a new listing / a strike that didn't exist at the
  baseline; a strike unwound to ZERO leaves the tradeable ladder (disappears, no
  ghost); a whole expiration absent at the baseline (a weekly that didn't exist a
  quarter ago) reads n/a for that window. Offline `--from-dir` / a throttled baseline →
  the panel degrades to the plain two-series OI chart with an honest note, never fake
  zeros.
- **Turnover (vol/OI) by strike (v1.130.0)** — today's volume ÷ standing OI, both legs:
  fresh flow vs the legacy book. >1× = today alone out-traded the entire standing OI
  there (new positioning or a roll); ~0 at a big wall = a stale "memorial" level.
  Tiny books (<10 contracts) are left empty — a 3-lot ratio is noise. **PCR(vol)** now
  also rides the PCR card's sub line, with a divergence word when the PCR(vol)/PCR(OI)
  ratio leaves 0.8–1.25 (±25% in ratio terms).
- Native greeks (delta + the OTM-leg vega/theta shown in the table; gamma powers GEX;
  rho stays in the raw chain).

**Expiration default:** the nearest expiration with **DTE ≥ 1** (a 0DTE chain is mostly
intrinsic and its IV is degenerate). Pass `--expiry YYYY-MM-DD` for a specific one; if it
doesn't exist the error lists the available expirations (nearest marked). **The pre-check
is `--list-expiries`**: one AV call, prints one line per expiration
(`date · DTE · strikes`) and exits — no dashboard — so an agent picks `--expiry`
deterministically instead of guessing. **Say the DTE** — a 1-DTE smile is a different
animal from a 45-DTE one.

## Failure modes — three DISTINCT errors (entitlement ≠ throttle ≠ bad ticker)

- **Entitlement (sample payload):** a key without realtime-options entitlement gets
  AV's ARTIFICIAL SAMPLE (a premium-plan `message` + placeholder rows `XXYYZZ…` /
  expiration `2099-…`). The script detects it and exits — "Alpha Vantage returned its
  SAMPLE payload for X — this key lacks realtime-options entitlement (premium plan);
  use HISTORICAL_OPTIONS or upgrade the key" — the fake numbers are never rendered.
- **Throttle:** a rate-limit note exits naming the free-tier limits — "Alpha Vantage
  throttled (free tier = 25 req/day · 5/min; realtime options need premium) — wait a
  minute and retry".
- **Bad ticker:** an empty chain exits "no option contracts for X (bad ticker /
  non-equity / no entitlement?)".
- **Dead chain:** a chain whose strikes ALL carry IV=None exits loud — "no usable IV
  for X (index, unentitled key, or a dead chain) — nothing to analyze" — instead of
  rendering an all-n/a dashboard with exit 0.

## Egress: probe first — direct fetch is the happy path, `--from-dir` is the blocked fallback

**Do NOT assume the sandbox blocks egress — it usually doesn't (Cowork).** Run the script directly
with `MARKET_DATA_NO_NET=auto`: it probes egress once and self-routes — reachable → a direct fetch
that renders the real dashboard; blocked → a fast-fail carrying this same `--from-dir` route (no
60s timeout burn). Assuming "no egress" and skipping the script is the AMAT-session failure (the
agent hand-rolled the chain in chat instead of running `options.py`).

MCP `--from-dir` fallback — ONLY after the probe reports egress blocked but the AV **MCP** connector
still works:

1. Call `REALTIME_OPTIONS` for the ticker with `require_greeks=true` and
   `return_full_data=true` (so the huge payload offloads to a file), and save it to a file
   whose name contains **`option`**. Optionally save a `GLOBAL_QUOTE` result to a file
   whose name contains **`quote`** (else spot is estimated from the 0.5-delta strike).
   NOTE (Cowork): the oversized payload auto-saves to a HOST tool-results path the sandbox
   CANNOT read (and it's over Read's 25k-token cap) — you must WRITE it into the mounted dir
   yourself for `--from-dir` to find it.
2. Render from the directory — no HTTP:
   ```
   <python> "<skill-dir>/scripts/options.py" SPY --from-dir <dir> --expiry 2026-07-17 --html chain.html
   ```
Caveat (honest): in this mode the chain DOES pass through context (the MCP pull is
unavoidable when egress is blocked). Prefer the direct `=auto` fetch whenever egress is open.

## Output modes — when to use each

| Mode | What | Who sees it |
| --- | --- | --- |
| (default) text | ATM IV / 25Δ skew / PCR / max pain + a per-strike table (IV/OI/delta, calls \| puts) | the chat (always) |
| `--json` | the same as compact JSON | feeds a widget |
| `--svg` / `--png` | self-contained landscape dashboard (smile, OI-by-strike + max pain, strikes table) / its PNG | file / agent verify |
| `--html` | **truly self-contained** **responsive** Chart.js dashboard — grid reflows one column when narrow, **two columns from 680px up** (capped at two); smile line + OI bar chart (with ATM & max-pain markers) + strikes table; **Chart.js inlined (no CDN — renders in egress-blocked/in-app viewers; the `--watch` live page too)** + dark-mode CSS | inline widget / expandable artifact / Preview Pane / in-app viewer |
|  | *(v1.90.1 / M7: `--html` is the SAME interactive engine page as `--full`/`--term`/`--watch`/fundamentals — one identity, ask-AI ✦ included; earlier builds wrote a no-JS SVG page here. The no-JS template remains an engine capability.)* |  |
| `--open` | writes the dashboard (HTML if `--html` was also passed, else SVG) and opens it in the **default browser** | the user, on their screen |
| `--watch [N]` `--port P` | LIVE via the central `dashboard.py` engine (same as futures): SSE per-block deltas, smile zoom, full-width bottom table, futures-style LIVE badge with the real poll age (2s default / 1s floor — the as-of date stays in the subtitle); a fatal ChainError (entitlement/dead chain/unlisted expiry) stops polling and stays on the badge | Preview Pane (no reload) |

## Live mode — `--watch` (the central dashboard.py engine, same as futures)

```
<python> "<skill-dir>/scripts/options.py" SPY --watch          # 2s polls
<python> "<skill-dir>/scripts/options.py" SPY --watch 1 --port 8765   # 1s: 120 req/min vs the 600/min budget
# then point the Preview Pane / a browser at http://127.0.0.1:8765
```
`--watch [N]` serves the **dashboard.py live page** (default **2s**, floor 1s — one poll =
2 AV calls (series + spot), so even 1s is 120 req/min against the 600/min premium budget;
default port 8765). After the discovery poll the provider **PINS the expiration** and
re-polls ONLY the displayed series (`expiration=` server-side filter, ~19x smaller — AV has
no delta API, no ETag/304, no socket: verified 2026-07-04, a series snapshot is the minimum
unit). The page loads once; the engine then pushes **per-block SSE deltas** with **silent
in-place patches** (`animation:false`, no reload, no redraw); a structure change (strike
axis / expiration / ATM move) sends one snapshot, then deltas resume. Smile zoom buttons,
the ATM-gradient table (full-width bottom row), and the max-pain vline all come from the
engine. `Ctrl-C` stops it.

**Liveness badge (user decision 2026-07-04):** the bar is the futures-style green ● LIVE
with the **real poll age** ("Ns ago") — AV options are polled snapshots, not a tick socket,
but at 1–2s polls a DELAYED label would mislead more than LIVE; the data's as-of date stays
in the subtitle. A **fatal** ChainError (entitlement sample / dead chain / unlisted expiry)
stops the metered polling and keeps the error on the badge; transient errors keep polling.

## Egress: probe first — direct fetch is the happy path, `--from-dir` is the blocked fallback

**Do NOT assume the sandbox blocks egress — it usually doesn't (Cowork).** Run the script directly
with `MARKET_DATA_NO_NET=auto`: it probes egress once and self-routes — reachable → a direct fetch
that renders the real dashboard; blocked → a fast-fail carrying this same `--from-dir` route (no
60s timeout burn). Assuming "no egress" and skipping the script is the AMAT-session failure (the
agent hand-rolled the chain in chat instead of running `options.py`).

MCP `--from-dir` fallback — ONLY after the probe reports egress blocked but the AV **MCP** connector
still works:

1. Call `REALTIME_OPTIONS` for the ticker with `require_greeks=true` and
   `return_full_data=true` (so the huge payload offloads to a file), and save it to a file
   whose name contains **`option`**. Optionally save a `GLOBAL_QUOTE` result to a file
   whose name contains **`quote`** (else spot is estimated from the 0.5-delta strike).
   NOTE (Cowork): the oversized payload auto-saves to a HOST tool-results path the sandbox
   CANNOT read (and it's over Read's 25k-token cap) — you must WRITE it into the mounted dir
   yourself for `--from-dir` to find it.
2. Render from the directory — no HTTP:
   ```
   <python> "<skill-dir>/scripts/options.py" SPY --from-dir <dir> --expiry 2026-07-17 --html chain.html
   ```
Caveat (honest): in this mode the chain DOES pass through context (the MCP pull is
unavoidable when egress is blocked). Prefer the direct `=auto` fetch whenever egress is open.

## Output modes — when to use each

| Mode | What | Who sees it |
| --- | --- | --- |
| (default) text | ATM IV / 25Δ skew / PCR / max pain + a per-strike table (IV/OI/delta, calls \| puts) | the chat (always) |
| `--json` | the same as compact JSON | feeds a widget |
| `--svg` / `--png` | self-contained landscape dashboard (smile, OI-by-strike + max pain, strikes table) / its PNG | file / agent verify |
| `--html` | **truly self-contained** **responsive** Chart.js dashboard — grid reflows one column when narrow, **two columns from 680px up** (capped at two); smile line + OI bar chart (with ATM & max-pain markers) + strikes table; **Chart.js inlined (no CDN — renders in egress-blocked/in-app viewers; the `--watch` live page too)** + dark-mode CSS | inline widget / expandable artifact / Preview Pane / in-app viewer |
| `--open` | writes the dashboard (HTML if `--html` was also passed, else SVG) and opens it in the **default browser** | the user, on their screen |
| `--watch [N]` `--port P` | LIVE via the central `dashboard.py` engine (same as futures): SSE per-block deltas, smile zoom, full-width bottom table, futures-style LIVE badge with the real poll age (2s default / 1s floor — the as-of date stays in the subtitle); a fatal ChainError (entitlement/dead chain/unlisted expiry) stops polling and stays on the badge | Preview Pane (no reload) |

## Live mode — `--watch` (live smile, per-point updates, no reload)

```
<python> "<skill-dir>/scripts/options.py" SPY --watch 10 --port 8765
# then point the Preview Pane / a browser at http://127.0.0.1:8765
```
`--watch [N]` runs a **local SSE server** (default **2s**, floor 1s — one poll = 2 AV calls (series + spot), so even 1s is 120 req/min against the 600/min premium budget; default port 8765). After the discovery poll the provider PINS the expiration and re-polls ONLY the displayed series (`expiration=` server-side filter, ~19x smaller). The page loads
**once** and builds the charts; a background thread polls the chain every N seconds, and
the server streams **only the strikes whose IV/OI/delta changed** (Server-Sent Events). The
page **patches just those smile points + their OI bars + table cells** (Chart.js animates
only the moved points; the changed row flashes) — **no page reload, no full redraw**. When
the strike axis / expiration / ATM moves it sends one full snapshot (to re-mask the OTM
legs), then resumes deltas. `Ctrl-C` stops it.

**The LIVE bar is real, not decorative.** The server emits a `poll` event on **every** poll
(even when nothing changed), so the bar shows the actual poll timestamp, whether the last
poll changed anything ("опрошено … без изменений" vs "N точек обновлено"), and a **countdown
that ticks down to the next poll** (reset by each real poll event — a genuine liveness signal,
not a cosmetic timer). So a quiet/closed market still visibly ticks instead of looking frozen.

**Honest caveat — it's still polling under the hood.** Alpha Vantage `REALTIME_OPTIONS` is a
REST snapshot (no socket for equity options), so the **server** re-fetches every N seconds;
but the **page is event-driven and granular** — only deltas cross the wire and only changed
points repaint. Intraday, during market hours, within the key's rate limit (10s is fine on a
premium key). The bar reads "server polls, page gets only deltas" so it isn't a tick tape.

Optimised: a **keep-alive connection pool** (reuses the TLS connection across polls, no
handshake each cycle) + **gzip** (the chain is ~1 MB of text → ~10× smaller); a quiet/closed
market produces empty deltas, so nothing repaints. The fetch is the **snapshot-provider
seam** — `gather(..., fetch=session.get)` — so a **Databento Live socket** backend could
replace the poller for futures options (deltas from ticks, zero polling; binary DBN +
heartbeat keep-alive + per-tick Black-76 via `scripts/greeks.py`) behind the SAME page/diff
front-end. That socket path needs the `databento` package (breaks the pure-stdlib rule) and
is a separate, deferred build — the equity polling backend here is the stdlib default.

**Showing it to the user** (an options override of the canonical `references/fundamentals.md`
rule): always print the text summary to chat, and render the dashboard in the in-app **side
panel (Preview Pane)** — **never an external browser by default**. Which dashboard depends on
the scope resolved by the FIRST QUESTION:
- **Single series → LIVE by default (`--watch`)** in the Preview Pane. The live self-updating
  smile is the happy path: point `.claude/launch.json` at the `--watch` command's loopback
  port, `preview_start`, then self-verify by data+console (screenshots only in dev sessions
  or on anomaly — the calibrated rule in `references/fundamentals.md`). Pass `--expiry` so every
  poll stays on the filtered one-series path (see FIRST QUESTION).
- **`--term` (cross-expiration) → a static `--html` dashboard** in the Preview Pane — live
  across many series is redundant (one payload; `--watch` doesn't apply to `--term`).
- **One-number question** (e.g. "what's the ATM IV?") → **text only**, no dashboard.

External browser (`--open`), an inline `--json` widget, or PNG are EXPLICIT-only — render them
only when the user asks by name. Never re-emit the big SVG through `show_widget`.

**Cross-links (v1.87.0, self-materializing since v1.94.0):** write per-ticker files
(`<sym>_options.html` beside the fundamentals HTML in the same served directory) — the
header gains a `SYM fundamentals ↗` chip when the sibling exists. On a LIVE render a MISSING
sibling is rendered automatically in the background (stderr notes it) and the chip bakes hidden
(`pending`) — the page HEAD-polls (~86s) and reveals it when the file lands; no dead link ever
shows, no manual re-render needed. `--from-dir` never spawns — there re-render the earlier file
to make the pair bidirectional. One-shot pages only — `--watch` served pages carry no links.
Since v1.159.1 the parent passes its actual basename across the background render, so a descriptive
fundamentals file such as `texas-instruments-txn-dashboard.html` receives a working reverse link;
the parent is written before the child checks it, removing the short startup race.
`<sym>_options.html` MUST be the **`--full`** page (all four tabs) — the fundamentals chip
lands there, and a single-series render under that name strands the reader on a cut-down page.
Rendering just one chain (an `--expiry` drill)? Use a suffixed name (`<sym>_options_aug.html`)
so the canonical slot keeps the full page.

**After the dashboard: the written read (below).** Rendering is not the answer — the
interpretive read is.

## The written read (REQUIRED for every equity/ETF "analyze the options" answer; futures chains → `references/databento.md`)

The dashboard shows the surface; **your job is what the vol is saying.** After the text
summary and the dashboard, write **3–6 sentences** in the user's language: the vol
regime, what the skew/term imply, positioning, and the event inside the window.
Descriptive, not advice ("the market prices a ±6% move through earnings" — never
"sell the straddle"). Don't restate the table — connect it. **Then END with the
deeper-document OFFER** exactly like the company read (SKILL.md "Neighbor plugins":
pick ONE document by context — earnings inside the window / next report ≤~3 weeks →
`/equity-research:earnings-preview`; a dated non-earnings catalyst → `:catalysts`;
on desktop/Cowork render the one-click card via `<skill-dir>/scripts/offer.py`,
checking THIS session's own skill list first per SKILL.md, PLUS the plain-text line
naming the command). An equity/ETF options read that closes text-only, card-less, is
incomplete — the CAT-session miss (2026-07-23): the right document was named but the
offer.py card was never rendered. Futures chains carry their OWN offer palette since
v1.128 (`references/databento.md` written read — `:sector`/`:screen`/`:catalysts`).

A good options read covers (pick what the data supports):
- **Cheap or rich** — anchor to IV rank/percentile and the vol cone (`--history` /
  `--full` Overview), and to the **VRP** (30d IV vs 20d realized): rich implied over a
  quiet tape is the seller's edge; IV below realized (negative VRP) means the tape is
  moving more than options price. If only a snapshot was pulled, SAY the read is
  snapshot-only — an ATM IV number without its own history is not a cheap/rich call.
- **Expected move** — spot · ATM IV · √(DTE/365) → "the market prices ±$X (±Y%) by
  <expiry>". The single most useful translation of IV into English; since v1.82.0 the
  **Exp move card** computes it — read it off the page (both equity and futures).
- **Skew** — positive 25Δ skew = puts bid (downside hedging demand, the normal equity
  state; steepening = fear building). Flat/negative = calls bid — unusual, call it out
  (squeeze, takeover, retail chase).
- **Term structure** — front premium over back = an event is priced (check: is the
  earnings date inside the front expiry? the Overview/History tabs carry it); inversion =
  near-term stress; smooth contango = calm. Name the slope's shape, not just its sign.
- **Positioning** — PCR (OI) vs its history, the OI walls (where the big strikes sit),
  max pain as "the writers' magnet, not a target", Net GEX / Γ-flip when shown (above
  flip = dealers dampen moves, below = they amplify).
- **Flow vs the book (v1.130.0)** — read the OI panel's Δ caps/ghosts + turnover +
  the PCR card's vol sub as ONE unit: is the dominant OI wall being BUILT (green cap)
  or UNWOUND (red ghost) since the baseline date, does today's turnover concentrate
  at it (live level) or elsewhere (rotation), and does PCR(vol) lean with or against
  PCR(OI) (fresh fear vs old hedges)? One sentence in the read: "the 330 call wall
  grew +3.6k against a puts-light tape" beats three disconnected metrics.
- **Event risk** — an expiration straddling earnings/a known event prices differently
  by construction; say which expiries carry the event and which are clean.
- **Liquidity honesty** — thin OI, interpolated smile points (`~`), wide DTE gaps: say
  when the surface is too thin to trust before interpreting its shape.

Worked example (the shape, not a template):

> «Vol в NVDA сейчас дешёвый относительно своей истории: IV rank 18%, 30d ATM IV 38%
> против медианы конуса 45%. Рынок закладывает ±5.9% к пятнице — скромно для недели
> с отчётом внутри экспирации, и именно отчёт держит фронт: термструктура в бэквордации
> (38% фронт против 33% на 90d), дальние серии чистые. Skew умеренно положительный
> (+4.2 пункта) — путы дороже, но без паники, а стена OI на 180 путах совпадает с
> max pain 182.5. Γ-flip на 178: выше рынок в зоне, где дилеры гасят движение.
> Поверхность ликвидна до ±15% от спота, дальше точки интерполированы — крылья читать
> осторожно.»

Anti-pattern: printing ATM IV / skew / PCR / max pain as four disconnected sentences
with no regime call, no expected move, and no event context — that's the metrics line
again, not a read.

## Reading the numbers — quirks to flag

- **0DTE / near-expiry IV is degenerate.** Deep-ITM contracts near expiry trade at
  intrinsic, so their IV is ~0 or garbage and OI/volume can be thin. The smile uses OTM
  legs to dodge this, but on a 1-DTE chain even the OTM wings are jumpy — say the DTE and
  offer a longer expiration for a cleaner read.
- **Spot vs ATM strike.** ATM = the listed strike nearest spot; the script states both.
  If no quote is available it estimates spot from the 0.5-delta call and flags it (`*`).
- **Max pain is a magnet theory, not a forecast.** Report it as "the strike where option
  writers pay out least", not a price target.
- **25Δ skew sign.** Positive = puts richer than calls (downside fear) — the normal equity
  state. A flat or negative skew (calls bid) is unusual and worth calling out (e.g., a
  squeeze / takeover name).
- **Indices (SPX/NDX/VIX) aren't here.** AV options cover equities/ETFs — the script
  refuses indices itself with the ETF-proxy suggestion (SPY/QQQ/IWM/DIA; VIX has no
  honest proxy — see the routing guards above). Futures options → Databento Branch A.

## Reporting & safety

- State provenance in TEXT (Alpha Vantage realtime options, expiration, DTE, as-of) next
  to any chart. Greeks are AV-native (not recomputed).
- This is **data analysis, not investment advice** — keep that framing.

## Offline test

`<python> scripts/test_options.py` checks the pure pieces (safe float, spot-from-quote,
expiration selection, ATM IV, 25-delta selection, PCR, max pain, the ±N window, text+SVG+
HTML rendering, and the `--from-dir` file path) with no network — wire it into CI
alongside `test_chart.py` / `test_fundamentals.py` / `test_portfolio.py` / `test_greeks.py`.
