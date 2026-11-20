# Databento Reference (futures & options on futures)
## CME contract lookup and quotes (desktop and cloud)

For these intents use Marengo by default, including on a machine with the old local Databento connector. This route takes precedence over script/dashboard instructions below. Consult `marengo_platform_catalog` and describe the advertised action.

- Known CME contract: request one quote with `marengo_databento_live_get_futures_quote`. For a directory ID pass the ID as a string with `stype_in=instrument_id`.
- Product name or option criteria: use `marengo_databento_instruments_search`. Pass product, call/put, strike and expiration when specified. Read subsequent result pages via platform cache using the returned cursor. Follow up on ambiguous families or multiple matching series. Use a unique matching ID; describe nearest as nearest observed when coverage is partial.
- Streaming best bid/ask: use `marengo_databento_live_stream_bba` start/read/stop for that ID. Advance after to next_cursor, report missed_events, distinguish an empty read from terminal feed status, and stop the handle when finished.
- On missing capability, OAuth, entitlement or provider failure report that condition. On confirmation_required direct the owner to the portal. Use the legacy local route for these operations only if explicitly selected. Never substitute settlement, Alpha Vantage, a dashboard or a local key for a missing CME quote.
- ES futures options use Marengo. SPY equity options and AAPL quotes use the existing Alpha Vantage connector. Current MOEX quotes prefer the BCS MCP. The existing full-chain and analytical dashboard workflows retain their own data sources.


Open this file when a request routes to Databento: CME/CBOT/NYMEX/COMEX futures,
options on those futures, GLBX.MDP3 order-book/series data, Databento symbology,
metadata, or batch. The router (`SKILL.md`) decides routing and the shared options
model; this file holds the Databento-specific detail.

Verified against the live MCP on 2026-06-29.

## Legacy local connector and analytical workflows

The tool names, key setup and quote fallbacks below apply to explicitly selected local mode or the legacy analytics workflow. For cloud contract searches and quotes use the Marengo route above.

### Expected local Databento MCP tools

- Live quote (true socket): `get_live_futures_quote` — any futures or
  options-on-futures symbol on GLBX.MDP3; preferred for current quotes.
- Legacy convenience (ES/NQ ONLY): `get_futures_quote` (historical REST, not a
  socket), `get_historical_bars`. Also `get_session_info`.
- Workhorse historical/series: `timeseries_get_range` (any dataset/symbol/schema,
  incl. CL, other commodities, options on futures).
- Symbology: `symbology_resolve`.
- Metadata: `metadata_list_datasets`, `metadata_list_schemas`,
  `metadata_list_publishers`, `metadata_list_fields`, `metadata_get_cost`,
  `metadata_get_dataset_range`.
- Batch: `batch_submit_job`, `batch_list_jobs`, `batch_download` (keep zero-cost
  preflight on by default; confirm scope/cost before submit — it can be paid).
- Reference: `reference_search_securities`, `reference_get_corporate_actions`,
  `reference_get_adjustments`. NOTE: `reference_search_securities` is not
  subscribed on this key — use `definition` (via `timeseries_get_range`) for
  symbol discovery.

If the Databento MCP server/tool is missing, stop and ask the user to install or
enable it; do not reroute to the wrong provider.

**Loading the tool schemas in one shot.** MCP tools are deferred by the harness —
their schemas are not in context until `ToolSearch` fetches them, which is the
"loading tools" / "Finding tools" step. This is a harness mechanism; `SKILL.md`
is plain instruction text and cannot pre-register schemas, so the step can't be
removed by editing the skill. To minimise it, fetch exactly the tools you need in
a SINGLE targeted call rather than a fuzzy keyword search, e.g. for a quote +
range probe: `ToolSearch select:get_live_futures_quote,metadata_get_dataset_range,timeseries_get_range`.
One round-trip loads all three schemas. **For charts this step is avoidable
entirely — `scripts/chart.py` hits the Databento HTTP endpoints directly and
touches no MCP tool (see `references/charting.md`); use the MCP path only for
quotes, options, microstructure, symbology, metadata, or batch.**

## Historical guardrails (Standard CME profile)

- L0 `ohlcv-1s/1m/1h/1d`, `definition`, `statistics`, `status`: full history.
- L1 `trades`, `mbp-1`, `tbbo`, `bbo-1s`, `bbo-1m`: rolling last 12 months.
- L2 `mbp-10`, L3 `mbo`: rolling last 1 month.
- `timeseries_get_range` rejects `ALL_SYMBOLS` and caps output with
  `MCP_DIRECT_MAX_RECORDS` (default 10000). Use `batch_submit_job` for large
  covered exports / `ALL_SYMBOLS`.
- Verify the user's entitlement profile before applying these to large requests.

## Live vs historical (operating reality)

- `get_live_futures_quote` is a TRUE socket: pass one symbol via `raw_symbol`,
  `instrument_id`, `continuous`, or `parent`; `dataset` defaults to GLBX.MDP3. It
  returns one top-of-book update with `dataAge` ~0s. Verified live: ES, `CLQ6`,
  `LOQ6 C7000` (WTI option), `GC.v.0`.
  - Continuous tip: use volume continuous `{root}.v.0` (liquid front), and pass
    `stype_in=continuous` with it — `stype_in` defaults to `raw_symbol`, so a bare
    `{root}.v.0` fails with "Failed to resolve symbol" (verified: `ES.v.0` resolves
    only with `stype_in=continuous`). The bare `ES`/`NQ` aliases are the sole
    exception (they default to continuous). Calendar `{root}.c.0` can point at an
    illiquid contract that never updates and times out. `raw_symbol` also works.
  - `stype_in=parent` with `{asset}.OPT` subscribes to the whole chain and returns
    the first live update — verified for `ON.OPT`, `OG.OPT`, `SO.OPT` at 0-1s. The
    live parent does NOT hit the heavy-chain timeout that the historical parent
    pull does (it subscribes, not scans) — works even for gold/silver.
  - A timeout ≠ wrong symbol. A live call returns the FIRST update; an illiquid or
    closed contract may produce none before `timeout_ms` → timeout. A WRONG symbol
    returns a symbology error instead. On timeout: raise `timeout_ms`, query during
    active hours, or read `timeseries_get_range`.
    - Thin OTM strikes need a longer wait, not a fallback. Off-ATM option strikes
      (energy/grains/metals) update less often, so the first socket tick can take
      ~15-20s. Raise `timeout_ms` to ~20000 and retry BEFORE concluding illiquid or
      switching to `timeseries_get_range`. Verified: `LOQ6 P6650` (25Δ WTI put)
      timed out at 8000ms but returned a clean live quote (`dataAge 0s`) at 20000ms.
- `get_futures_quote` (latest historical REST, not a socket) is an ES/NQ-only
  legacy tool. `get_historical_bars` now accepts ANY symbol: ES/NQ are continuous
  front-contract aliases, and any other symbol is a `raw_symbol` unless you pass
  `stype_in` (`raw_symbol`/`continuous`/`parent`/`instrument_id`) — e.g. `CL.v.0`
  with `stype_in=continuous`, or a raw `CLZ6`. It returns OHLCV (`1h`/`H4`/`1d`);
  for other schemas/depth use `timeseries_get_range`.
  - The old intraday HTTP 422 is FIXED: `get_historical_bars` no longer sends an
    `end`, so it returns up to the latest available bar. (Previously it set
    `end`=now, which exceeded the ohlcv frontier and 422'd during the active
    session.) For a pinned historical window, `timeseries_get_range` still takes an
    explicit `end` ≤ the dataset frontier.
- `timeseries_get_range` (Historical API) is for history/bars/depth/series/
  definitions/full chains. Most recent data is near-real-time for top-of-book/trade
  schemas (`mbp-1`, `tbbo`, `trades`, `bbo-*`, `ohlcv-1s/1m`) to within ~1-2 min;
  deep book (`mbo`, `mbp-10`) lags hours.
- Always check `metadata_get_dataset_range` for the current `end` first; an `end`
  past the frontier returns HTTP 422 (`data_end_after_available_end`).
- Symbology: `raw_symbol`/`continuous`/`parent` inputs must use
  `stype_out=instrument_id` (raw→raw and continuous→raw_symbol both 422). Times
  need full ISO 8601 with seconds and `Z` (e.g. `2026-06-29T09:00:00Z`).
- Databento returns raw data only — NO greeks/IV. Compute them with
  `scripts/greeks.py` (Black-76 for futures options).

## Option asset codes (parent = `{option_asset}.OPT`)

The option asset is a DISTINCT code from the futures root for energy/grains/metals,
but EQUALS the root for equity-index and livestock.

This table IS the lookup — do NOT rediscover a listed code with definition probes
(that trial-and-error is the #1 time-sink; verified live 2026-07-01).

| Underlying future | Futures root | Option asset / parent |
| --- | --- | --- |
| WTI crude | CL | `LO` → `LO.OPT` |
| Natural gas (Henry Hub) | NG | `ON` → `ON.OPT` |
| Heating oil (ULSD) | HO | `OH` → `OH.OPT` |
| RBOB gasoline | RB | `OB` → `OB.OPT` |
| Gold | GC | `OG` → `OG.OPT` |
| Silver | SI | `SO` → `SO.OPT` |
| Platinum | PL | `PO` → `PO.OPT` |
| **Palladium** | **PA** | **`PAO` → `PAO.OPT`** (verified) |
| Copper | HG | `HXE` → `HXE.OPT` (verified) |
| Corn | ZC | `OZC` → `OZC.OPT` |
| Soybeans | ZS | `OZS` → `OZS.OPT` |
| Wheat | ZW | `OZW` → `OZW.OPT` |
| Soybean oil | ZL | `OZL` → `OZL.OPT` |
| Euro FX | 6E | `EUU` → `EUU.OPT` (verified) |
| Live cattle | LE | `LE` → `LE.OPT` (shares root) |
| E-mini S&P 500 | ES | `ES` → `ES.OPT` (shares root) |

Only for a code NOT in this table: ONE small `definition` pull on a candidate
`{asset}.OPT` (`stype_in=parent`, `stype_out=instrument_id`, `limit≤5`) and read the
`asset`/`underlying` fields. Script note: `BZ` (Brent — lists as `BZO`) and `ZM`
(bean meal) are NOT in `futures_options.py`'s `OPTION_ROOTS` table; for an unmapped
root the script probes `{root}.OPT` as a guess (stderr warning) and `--option-root
CODE` overrides it (e.g. `BZ --option-root BZO`). Raw option symbol:
`{asset}{MonthCode}{YearDigit} {C|P}{strike×100 for energy}` — e.g. `LOQ6 C7000` =
$70.00 call on the August WTI future; `PAOU6 C1220` = Sep-2026 $1220 palladium call
(1- or 2-digit year both resolve: `PAOU6` == `PAOU26`). That strike suffix
(×100 for energy, index points for ES…) is a DISPLAY convention and
product-specific: when building a chain, read the numeric `strike_price`
(definition) / `price` (statistics) fields ÷1e9 (Databento fixed-point) — NEVER
parse strikes out of the raw-symbol suffix. Precious-metal options (PA/PL) are
AMERICAN-style and list only on the QUARTERLY cycle (H/M/U/Z, on the like future);
their $5 strike ladder is a CURRENT example, not an invariant — read the grid from
the series' definition listing (see "Strike grids are per-series" in Branch A2).

## Trading hours (for live quotes)

Energy and metals trade ~24h (Globex Sun-Fri) → live works around the clock.
Grains (CBOT) and livestock (CME) have limited sessions — livestock
futures/options ~08:30-13:05 CT only. Outside a product's hours
`get_live_futures_quote` gets no update and times out: closed market, not missing
data → use `timeseries_get_range` for the last session.

## Chain-size rule (HISTORICAL parent pulls only)

WTI, NatGas, Corn, Platinum option chains return fine from a synchronous historical
parent (`{asset}.OPT`) with a narrow window + small `limit`. Very large chains —
gold (`OG.OPT`) and silver — TIME OUT on synchronous historical parent pulls
(thousands of contracts); for historical data on those, target a specific contract
by raw symbol or use `batch_submit_job`. This does NOT affect the LIVE socket
parent (which subscribes, not scans). Coverage is never the issue; only historical
parent chain size is.

## Branch A — options on futures playbook

Greeks are NOT in the data — compute with `scripts/greeks.py` (Black-76).

ES option series asset codes (no more guessing):
- Quarterly (3rd Friday, Mar/Jun/Sep/Dec): `ES`
- End-of-month (last business day): `EW`
- Friday weeklies: `EW1`..`EW5` (Nth Friday; 3rd Friday of a quarterly month is
  `ES`, not `EW3`)
- Monday `E1A`..`E5A` · Tuesday `E1B`..`E5B` · Wednesday `E1C`..`E5C` ·
  Thursday `E1D`..`E5D` (digit = Nth occurrence of that weekday in the month;
  A=Mon…D=Thu; Friday uses `EW{n}`). Verified: `E2D` = 2nd Thursday = 2026-07-09.

Raw symbol: `{series}{MonthCode}{YearDigit} {C|P}{strike}` — month code = EXPIRATION
month (F G H J K M N Q U V X Z), year = last digit, strike in index points. E.g.
`E2DN6 P7475` = 9-Jul-2026 7475 put. Strikes currently 5 pts near ATM, 25 pts
further out — a CURRENT example, not an invariant (grids are per-series; see
Branch A2). Underlying/center for any ES weekly/EOM is the front quarterly future
(currently `ESU6` = `ES.v.0`).

Three steps (use the fewest calls — see the router's "fewest calls" rule):
1. Center = `get_live_futures_quote {root}.v.0` with `stype_in=continuous`. SKIP this
   when a specific strike is named — just quote that contract.
2. Select the EXACT contract by request type (see the shared selection list in
   `SKILL.md`). For named/non-nearest expiry, map the date → series code via the
   table above + set the month code. For other CME products, swap in that product's
   option asset code and `{root}.v.0` center with `stype_in=continuous`.
   - N-delta: CONSTRUCT a few candidate strike symbols around center (the current
     5/25-pt grid — check the listing if a symbol misses) and quote them with
     `get_live_futures_quote`, then
     `scripts/greeks.py iv --model b76 --type <call|put> --F <fwd> --K <K>
     --T <years> --price <mid>` for IV+delta (forward from put-call parity), or the
     `strike` subcommand to solve the strike for a target delta. Do NOT pull the
     chain. A `definition` probe is only a fallback when the symbol can't be built.
3. Quote with `get_live_futures_quote`; fall back on issues:
   - TIMEOUT (illiquid/deep ITM-OTM/far-dated/off-hours): not a wrong symbol, not
     missing data. Fall back to `timeseries_get_range` (`mbp-1`/`bbo-1m`, last
     minutes) for the last resting bid/ask, or `statistics` for prior settlement.
     Verified: `E2DN6 P7700` timed out live but its book (~232.5/241.5) came back
     instantly from historical `mbp-1`.
   - SYMBOLOGY ERROR (wrong series/strike): do ONE `definition` probe on
     `{series}.OPT` (`stype_in=parent`, small `limit`); skip `UD:` combo rows (real
     outrights have `security_type` OOF); re-quote.

Speed: for a live quote skip `metadata_get_dataset_range` (only needed for
historical) and never pull the full chain — construct the symbol or pull a band.

## Branch A2 — whole-chain vol surface / smile ("analyze the options on X")

When the request is the WHOLE chain (smile / skew / max pain / OI by strike), not a
single quote, this ENTIRE sequence is implemented by one command — run it, don't
hand-execute the steps:

**FIRST QUESTION (connector-gated — do NOT pay for it twice).** A futures root carries
MANY expirations (PA lists 14; the nearest is often a thin, near-expiry serial). WITH the
local connector running (`connector-web`): unless the user already named an expiration,
run `--list-series` FIRST, present the listing, and ask which expiration they want —
offering the nearest as the default, or `--term` for the cross-expiration view of all at
once. `--list-series` hits the connector's keyless `/series` (instant, from the
long-lived defs catalog); NO Databento key. Skip the question when the user was explicit
(a date, "nearest", "front month", or "all expirations"/"term structure").
**WITHOUT the connector (cloud Cowork — there is no localhost connector there, ever):
skip the pre-listing entirely for a general "analyze the options on X" and go STRAIGHT to
`--full`** — it picks the nearest series itself, and the page's Term tab + Series table
answer "what expirations exist?" anyway. `--list-expiries` (the direct sibling) is a
whole-root parent definitions pull — ~40-60s+ of Databento server prep on big roots, NOT
a free pre-check — so running it before `--full` just fronts the same pull `--full` needs
(same-day repeats ride the local day-cache, so it's no longer paid twice — but it still
buys you nothing the `--full` page doesn't show). The CL cloud post-mortem (2026-07-23):
connector listing died → `--list-expiries` → `--full` → a `--full` re-run for the log =
the multi-minute pipeline paid 3× for one answer.

```
<python> "<skill-dir>/scripts/futures_options.py" <ROOT> --list-series   # FIRST QUESTION: keyless /series — every expiration + DTE + strikes, nearest marked; ask which
   # add --liquidity to also rank by open interest (most-liquid series); one whole-root OI pull, connector day-cached
<python> "<skill-dir>/scripts/futures_options.py" <ROOT>            # text summary (nearest quarterly, full ladder, ATM greeks)
<python> "<skill-dir>/scripts/futures_options.py" <ROOT> --html out.html   # + self-contained dashboard (smile + Δ/Γ/greeks + GEX profile + ATM heat-map table)
   # GEX panel (Ф5): Γ×netOI per strike in Δ-contracts/pt, sign-coloured bars, Γ-flip vline + Net-GEX/Γ-flip
   # cards. HONEST-modeling notes live in the panel info: naive dealer sign (long calls/short puts — real
   # positioning is NOT observable from OI) and a sticky-strike flip sweep; unsolvable-IV strikes are
   # excluded, never zero-filled (table n/a, net sum marked ≈). Greeks stay SKILL-side (greeks.py; the
   # connector serves IV only) — so GEX ticks on the local --watch page but is as-of-render in a Cowork
   # bridge artifact (same rule as the greeks cards).
<python> "<skill-dir>/scripts/futures_options.py" <ROOT> --watch    # LIVE self-updating side panel (SSE per-block deltas)
   # default --source auto: polls the local Databento connector (keyless) -> real ● LIVE tick badge when the
   # connector runs --live and its asOf is fresh; else --source databento (settlement fallback, .env key).
<python> "<skill-dir>/scripts/futures_options.py" <ROOT> --full --html out.html > root_full.log   # the COMBINED tabbed page (v1.114):
   # [Overview · Chain · Term · History] — Chain+Term are EXACTLY the --term build; Overview/History are the
   # retrospective (IV rank / vol cone / skew & PCR history) from the futhist SETTLEMENT store (Databento
   # statistics reduced through the same build_chain, Black-76 computed IV — the subtitle says so). A COLD
   # root renders honest 'warming' tabs naming the fill command:  futhist.py <ROOT> --backfill --years 1
   # (~$0 on L0 entitlements — the backfill prints a cost preflight first; resumable, month-atomic).
   # One-shot static; --watch does not apply (v1).
   # STDOUT ONLY — NEVER `2>&1` (v1.157.0). stderr carries the provenance notes, the `wrote …`
   # confirmation and the CLOSING-STEP reminder; merging both streams into the log meant a
   # SUCCESSFUL run handed back an EMPTY tool result — no numbers, no filename, nothing to write a
   # read from. That is the mechanism behind the ZW (wheat) and PA (palladium) Cowork sessions
   # 2026-07-30, where the dashboard landed fast and the written read never came until the user
   # kicked the session. Both pages stamped v1.154.0, so this was never a stale-copy problem.
   # CAPTURE THE LOG ON THE FIRST RUN (the `> root_full.log` above): stdout is the full text
   # analysis the written read sources its numbers from, and a long chain table can overflow a tool
   # result — NEVER re-run the pipeline just to re-capture output (the CL cloud session paid the
   # whole multi-minute build a second time for exactly that; same-day re-runs ride the day-cache
   # now, but the re-run is still pure waste — the log already has everything). Read the log ONCE
   # (tail + the summary head) — a dozen piecemeal greps is a dozen agent turns.
   # SANDBOX TIMEOUT (the cloud CL post-mortem): give the FIRST --full of a day a tool timeout
   # ≥900s — a snapshot-less cold root can run 5-8 min in a cloud sandbox and the default ~5-min
   # kill wastes the run mid-pipeline (the day-cache lets a retry resume, but one long run is
   # cheaper). With the shipped defs snapshot (v1.129) a warm-store root is ~1-2.5 min cold.
   # COLD ROOT (no futhist store — the PA session catch 2026-07-23): the page renders honest
   # 'warming' History/Overview tabs and is STATIC — it will never fill itself in. Deliver the
   # chain/term dashboard immediately, SAY History is warming, then in the SAME session run
   # `futhist.py ROOT --backfill --years 1`, RE-render --full and RE-present the file — never
   # leave the user with the truncated page as the final answer.
<python> "<skill-dir>/scripts/futures_options.py" <ROOT> --term            # chain page + CROSS-expiration analytics
   # appended (combines with --expiry): ATM-IV/skew term curve + moneyness×expiry IV surface + series table.
   # PER-SERIES forwards (each series' own underlying month — keying off the front would warp the surface).
   # --watch/--artifact don't apply. SOURCE: connector-FIRST (auto) — GET /term/:root.json is DAY-CACHED
   # in the databento MCP connector (term-data.ts): the FIRST pull of a trading day runs the slow
   # Databento side, every later run is sub-second. --source databento = the direct path — v1.128/9:
   # gzip'd, statistics id-scoped to the kept series (never the whole root), ONE batched settle pull
   # (map_symbols), every response rides a local same-day DAY-CACHE (~/.cache/market-data/
   # futures_daycache; MARKET_DATA_FUT_DAYCACHE=0 disables, or set it to a dir; the key is
   # DATE-level, so an intra-day frontier step never invalidates it), and the DEFINITION listing is
   # served from the shipped defs SNAPSHOT (<store>/futures/defs/, TTL 5d, --fresh-defs forces
   # live; write-through on any live listing; the futhist prewarm keeps it fresh). Measured (CL):
   # snapshot-less cold 1:40-2:27 → cold-with-snapshot (a fresh cloud VM off the zip) ~1:01 →
   # same-day repeat ~18s. A stderr note names the source either way. HONESTY: the key is
   # date-level, so a re-run AFTER the ~18:00-ET settle publication can serve the morning body
   # until midnight (MARKET_DATA_FUT_DAYCACHE=0 forces fresh; the --watch poll bypasses the
   # cache by design and always sees new settles).
   # OPENAI HEAVY-RUN GUARD (v1.160): run exactly ONE foreground command and write to exactly ONE
   # dashboard path. Set the external tool timeout to >=900s, capture this first log, and WAIT — never
   # start a parallel/nohup retry or a second writer. The packaged OpenAI script ignores accidental
   # MARKET_DATA_FUT_DAYCACHE=0 / MARKET_DATA_FUT_DEFS_SNAPSHOT=0 kill-switches, while explicit
   # --fresh-defs still forces a live definition read and then refreshes the snapshot. A per-output
   # lease rejects a concurrent heavy render of the same HTML. The lease is held by the OS, so a
   # killed render frees it IMMEDIATELY — if it says one is already running, one really is: wait
   # for that process, never advise waiting out a timer (there is none) or deleting the .lock.
   # Claude/local runs keep honoring those environment overrides exactly as before.
<python> "<skill-dir>/scripts/futures_options.py" <ROOT> --expiry YYYY-MM-DD   # a specific series instead of nearest quarterly
<python> "<skill-dir>/scripts/futures_options.py" <ROOT> --list-expiries       # pre-check: the listed series, definition-only
<python> "<skill-dir>/scripts/futures_options.py" <ROOT> --option-root CODE --source databento  # unmapped-root override (Brent: BZ --option-root BZO); direct source only — the connector refuses the flag
```

Workflow contract (the validate-before-invoke pieces):

- **`--list-series`** (the FIRST QUESTION) prints the option-expiration series from the
  connector's keyless `/series` — a header (`ROOT (OPT) — N series · asOf …`), one line per
  series (`stem · expiration · DTE · kind · strikes`, `+ OI` with `--liquidity`), the nearest
  future series and the most-liquid one marked, and a prompt naming the default (nearest) and
  how to pick (`--expiry`) or see all (`--term`). Served INSTANTLY from the long-lived defs
  catalog (no key), so it is the cheapest possible "what's available?" — present it and ask
  BEFORE the chain pull. Needs `connector-web` running; a down connector exits with a message
  pointing at `--list-expiries`. Honest: the most-OI mark appears ONLY when the connector
  actually supplied OI (`--liquidity`), never guessed from strike counts.
- **`--list-expiries`** prints one line per LISTED series —
  `stem · expiration · DTE · strikes · quarterly|serial · underlying <FUT>` — from the
  definition listing ONLY and exits. With a fresh defs SNAPSHOT (v1.129, ≤5d — the zip
  ships them for the prewarm roots) it is ~instant and free; snapshot-less it is a
  whole-root parent defs pull (~40-60s+ of server prep on big roots, day-cached same-day).
  It is the Databento-DIRECT sibling of `--list-series` (adds the
  underlying future per series), so it needs the Databento key even with `--source
  connector` — use it when the connector is down AND the user's question is specifically
  "what expirations exist"; for a general chain analysis go straight to `--full` (the
  same listing rides the snapshot/day-cache into it — see FIRST QUESTION above).
- **`--expiry YYYY-MM-DD`** picks the nearest LISTED series within **±7 days**, ELSE
  errors listing the real expirations ("error: no listed <ROOT> option series within
  7 days of <date>. Listed expirations: … (pick one, or drop --expiry for the nearest
  quarterly)"). An exact-date tie prefers the QUARTERLY stem, then the bigger ladder.
- **The chosen series is always echoed to stderr** —
  `series: <STEM> · exp <date> · <N> DTE · <M> strikes · quarterly|serial · underlying
  <FUT>` — so a mis-pick can't stay silent.
- **A fallback forward is marked in the artifact subtitle** — `fwd≈continuous front
  (verify month)` / `fwd≈mid strike (no forward data)` — plus a stderr warning; never
  silently presented as settlement-grade (see step 1 below).

The steps below document WHAT it does (and the invariants to preserve if you ever touch
the code) — do not improvise or discover them by hand:

1. **Option code** from the table (no discovery; unmapped root → probe + `--option-root`,
   see above). **Forward** = the settlement of the future named by the option
   definition's **`underlying` field — READ it, never construct the symbol**: the
   future's year width can differ from the option's (NG option `ONU6` → future
   `NGU26`, not `NGU6`, which 422s `symbology_invalid_request`). Fallback = the front
   continuous `{root}.v.0` settlement, marked `fwd≈continuous front (verify month)`
   (in a roll window the continuous can be ANOTHER month); last resort = the mid
   listed strike, marked `fwd≈mid strike`.
2. **Pick exactly ONE expiration — default; NEVER enumerate every expiry.** Scanning
   all series across a chain is THE cause of the long delays (times out on gold/silver).
   Default = **nearest quarterly**, chosen DATA-DRIVEN: group the definition listing by
   raw-symbol stem and pick the nearest FUTURE `expiration` among the quarterly months
   (H/M/U/Z). Do NOT build the series symbol from a computed month/year — GLBX writes the
   year 1-digit for the current year (`PAOU6`) but 2-digit for later years (`PAOH27`), and
   metals options expire the month BEFORE the contract month, so a computed string breaks at
   the roll. "Nearest most-liquid" is optional (one OI scan).
3. **Pull that ONE expiration's FULL strike ladder — ALL strikes, NEVER a narrow
   band.** A general "show options on X" wants the WHOLE strike set of the chosen
   expiration, not a ±window. Two-step, and mind the DEFINITION-vs-MARKET-DATA split:
   - **Enumerate** the strikes from a parent `definition` LISTING (`stype_in=parent`,
     `stype_out=instrument_id`), keeping only rows whose `raw_symbol` / `expiration`
     is the chosen series. This is lightweight METADATA (a listing, cacheable) — it's
     the only place you touch the parent, and it does NOT count as "pulling all
     expiries' data".
   - **Then pull MARKET DATA** — `statistics` — for ONLY that one expiration's strikes
     (by their `instrument_id`, chunked to dodge URL-length 414s). `stat_type` codes:
     **3 = settlement · 7 = lowest offer · 8 = highest bid · 9 = open interest** (7/8
     give a bid/offer mid when a settlement is missing). Decode fixed-point prices
     ÷1e9, and treat the UNDEF sentinels as MISSING, never as values: price
     `INT64_MAX` = 9223372036854775807, quantity `INT32_MAX` = 2147483647.
     NEVER pull statistics/quotes across the whole parent: that all-expiry
     market-data scan is exactly what times out and what the user forbids.
   HARD RULE: parent / continuous / raw inputs ALWAYS resolve to `instrument_id`
   (raw→raw and continuous→raw_symbol BOTH 422 — the #1 avoidable failure).
   - **Default SHOWN set = the TRADEABLE ladder: strikes with `OI > 0` (or a non-zero
     settlement).** This is still the WHOLE series (typically ~50–150 strikes), NOT a
     ±window — it only drops dead far-OTM listings whose settlement ≈ 0 and whose
     solved IV is numerical noise (a full palladium listing runs to ~3500 strike with
     zero OI). Showing EVERY dead listing is explicit-request-only.
   Never silently show a sub-range. A ±window is only for an explicit "zoom near ATM"
   request; a specific expiry/date is only when the user names one — otherwise the
   default is nearest-quarterly (or most-liquid), the full TRADEABLE ladder.
4. **Illiquidity is EXPECTED for thin metals** (PA, PL, far HG/GC strikes) and
   off-hours: the live socket TIMES OUT and `mbp-1`/`bbo` is empty even ATM — a
   settlement-marked market with no resting quotes, NOT a wrong symbol or missing
   data. Do NOT chase live quotes or RFQ — **price the smile off the settlements**
   (step 3). A live tick-smile exists only for liquid roots (ES/CL/GC/NG).
5. **IV + greeks per strike** vs the forward:
   `scripts/greeks.py iv --model b76 --type <call|put> --F <fwd> --K <K> --T <yrs>
   --price <settlement>`. Assemble ATM IV, 25Δ skew, the per-strike smile, max pain
   and put/call from OI. One `symbology_resolve` (`instrument_id`→`raw_symbol`) maps
   IDs back to strikes to label — don't loop per-symbol resolves.
6. **Render the dashboard — do not stop at text; default to the LIVE side panel.**
   `futures_options.py` emits a self-contained OTM-composite smile (puts left of ATM, calls
   right) + OI bars + max-pain marker via the centralized `dashboard.py` renderer, shown in
   the in-app **side panel (Preview Pane)**, NOT an external browser. A **single series →
   LIVE (`--watch`)** by default (point `.claude/launch.json` at the `--watch` command's
   loopback port, `preview_start`, then self-verify by data+console — screenshots only in
   dev sessions or on anomaly (the fundamentals.md calibrated rule)); **`--term` → a static
   `--html`** (live across many series is redundant — `--watch` doesn't apply). External
   browser (`--open` / the live `--watch` page popped out) and inline widgets are
   EXPLICIT-only. In Home/Cowork (no Preview Pane), write the self-contained file and offer
   it. (Full display rules: `fundamentals.md`.)
   **After the dashboard: the written read (next section).** Rendering is not the answer —
   the interpretive read is.

**Strike grids are per-series.** Spacing, range, and the listed-strike set are read
from THAT series' definition listing — never assumed, never reconstructed from a
formula. Weekly / EOM / serial ladders differ from the quarterly ladder of the same
root, and the exchange adds strikes as the market moves. The "$5 ladder" (PA/PL) and
"5/25 pt" (ES) figures elsewhere in this file are CURRENT EXAMPLES, not invariants.

Speed: table code + ONE expiration (nearest quarterly) + its FULL strike ladder (one
parent `definition` filtered to that expiry + one `statistics`) = the whole smile in
~4 calls. The palladium session was slow ONLY because it broke these rules — it
rediscovered PAO, hit the raw_symbol/instrument_id 422 twice, enumerated every expiry,
looped strike-by-strike, and timed out a giant multi-symbol pull. Full ladder ≠ slow;
ALL expiries = slow.

## The written read (REQUIRED for every "analyze the options on a future" answer)

The dashboard shows the surface; **your job is what the vol is saying.** After the text
summary and the dashboard, write **3–6 sentences** in the user's language: the vol regime,
what the skew/term imply, positioning, and the event inside the window. Descriptive, not
advice ("the market prices a ±9-point move by August" — never "sell the strangle"). Don't
restate the table — connect it. Futures differ from the equity read (`references/options.md`)
in four ways — center, cheap/rich basis, skew convention, and a SECOND term structure —
spelled out below.

A good futures-options read covers (pick what the data supports):
- **Center = the per-series FORWARD, not spot.** Each series prices off its OWN underlying
  month (CL Aug options off CLQ, Dec off CLZ) — state the forward and that it is
  SETTLEMENT-priced (a prior-session settle, not a live tick) unless the page shows the
  ● LIVE badge (a fresh connector tick feed; only liquid roots ES/CL/GC/NG stream — thin
  metals (PA/PL) and thin roots are settlement-only). Name the chosen series and its DTE.
- **Cheap or rich — HISTORY-relative first (v1.114), shape-relative always.** The futhist
  settlement store gives futures the same IV rank / percentile / vol cone equities have —
  read them off the `--full` Overview/History tabs ("66% today = the 33rd rank of its
  6-month range, median 67%, max 131%" is a real cheap/rich call). A COLD root has no
  store yet — then the read stays SHAPE-relative (say so): the **vol term structure**
  (below) and, when useful, realized vol of the underlying future. No VRP card for
  futures yet (a roll-adjusted realized series is deferred — the hole is honest).
- **Expected move** — forward · ATM IV · √(DTE/365) → "the market prices ±X points (±Y%) by
  <expiry>". Read the **Exp move card** off the page (v1.82.0) — the single most useful
  translation of IV into English.
- **Skew — mind the product's convention.** The script's 25Δ skew = putIV − callIV, so a
  NEGATIVE value = calls richer (upside bid). On the FUTURES page it is a ~10%-OTM
  moneyness PROXY (putIV@0.90F − callIV@1.10F — the card hint says so), not the equity
  read's true-delta buckets — don't compare the number delta-for-delta across products.
  Unlike equities (puts-bid is the normal state),
  many futures are the opposite by nature: energy (crude/products) is often CALL-skewed
  (supply-shock/upside risk), grains swing with weather, metals run put or symmetric. Read
  the DEVIATION from the product's own normal, not against an equity prior — and name it
  ("HO is call-skewed, typical for distillate upside-risk hedging").
- **TWO term structures — name both.** (a) The **futures curve**: the underlying forwards
  across months — backwardation (front dearer) signals physical tightness, contango a
  well-supplied market. (b) The **vol term**: ATM IV by expiry (the `--term` ATM-IV curve) —
  front-loaded / backwardated IV = a near-term event or tightness is priced, a smooth rise
  (IV contango) = a calm front. They often move together (a backwardated crude curve with
  front-loaded vol = a tight, nervous prompt). Since v1.111 the ATM-IV term curve is populated
  even on thin roots — use it.
- **Positioning** — PCR (OI), the OI walls (where the big strikes sit), max pain as "the
  writers' magnet, not a target", Net GEX / Γ-flip when shown (naive dealer sign — long
  calls / short puts — stated on the panel; above the flip dealers dampen moves, below they
  amplify).
- **Event risk** — an expiration straddling a known event prices differently by construction:
  energy = EIA inventories / OPEC / weather; grains = WASDE / planting / harvest; metals &
  rates = FOMC / macro prints. Also flag the ROLL — the option expiry vs the future's expiry
  (metals options expire the month BEFORE the contract), and near first-notice/expiry the
  front future itself is rolling.
- **Liquidity honesty** — thin OI, interpolated smile points (`~`), a coarsened near-money
  grid (connector `/term`) vs the full tradeable ladder (`--source databento`), wide DTE gaps:
  say when the surface is too thin to trust before interpreting its shape. Illiquidity is
  EXPECTED for thin metals (PA/PL, far HG/GC) and off-hours — a settlement-priced market with
  no resting quotes is not missing data.

Worked example (the shape, not a template):

> «Опционы на мазут (HO) на 42 дня (форвард серии 3.88, settlement) торгуются с высокой
> волатильностью и выраженным скью в сторону роста: ATM IV 66%, 25Δ skew −11.7 (коллы дороже путов — типично для дистиллята,
> хеджируют апсайд), OI почти весь в коллах (PCR 0.18), стена на 4.00/4.50. Рынок закладывает
> ±0.87 (±22.5%) к концу августа. Вол-структура в бэквордации — 77.7% на фронте против 37.6%
> на 286 днях — и фьючерсная кривая тоже в бэквордации (форвард 4.01→3.05), т.е. тесный,
> нервный промпт. Данные расчётные (settlement, живых тиков у HO нет), near-money сетка тонкая —
> 7 торгуемых страйков, часть IV интерполирована.»

Anti-pattern: reading a futures chain like an equity — assuming puts-bid is "normal", quoting
an ATM IV as cheap/rich with no history AND no term/shape context, or forgetting that the
center is a per-series forward and the quotes are settlements.

**The read ENDS with the offer (v1.128 — the fundamentals.md/options.md closing step,
extended to futures; the old "futures pages get no offer" rule is retired).** Pick ONE (at
most two) by context from SKILL.md's palette: a strong commodity story (vol percentile,
term shape, positioning — the CL-class read) → `/equity-research:sector <complex>` (the
energy/metals/ag landscape report); "как это сыграть через акции" →
`/equity-research:screen`; the calendar IS the question (rates roots around FOMC, a
WASDE/OPEC+ cluster) → `/equity-research:catalysts`. Company-shaped documents
(dcf/comps/earnings*) never fit a commodity. THE CARD IS PART OF THE OFFER: where
show_widget exists, render the one-click card via `<skill-dir>/scripts/offer.py`
(checking THIS session's own skill list first per SKILL.md) PLUS the one plain-text line
naming the command — a futures read that names the document but skips the card is
incomplete, the same rule the equity checklists carry.

## What actually makes a commodity dashboard slow (measured 2026-08-07)

Direct path, `--full --source databento`, same box and session:

| Root | Cached | Wall clock | History tab |
|---|---|---|---|
| PL | defs snapshot + vol store | **44 s** | full, 259 days |
| PL | + same-day day-cache (repeat) | **16 s** | full |
| ZL | nothing | **200 s** | EMPTY ('warming') |
| ZL | snapshot only, day-cache OFF (A/B) | **114 s** | EMPTY ('warming') |
| KE | snapshot only, cold day-cache | **95 s** | EMPTY ('warming') |

Read the ZL rows, not the PL-vs-ZL gap: those are different roots with different ladders, so only
the same-root A/B measures the snapshot. It is worth **86 s (200 → 114, -43%)**; the rest of ZL's
time is its statistics pulls, which the same-day day-cache then absorbs on any repeat.

What the table rules OUT is history depth. ZL carried NO history in every row and was still the
slow one — history is read off local disk in milliseconds and a cold store renders an honest
'warming' tab for free. So "show less history" is not a speed lever; there is nothing to win
there. The snapshot is a lever, and it costs 5-200 KB per root.

Hence **two prewarm tiers** (v1.166.0), `scripts/futhist.py`:

- **Accrete tier** — `PREWARM_ROOTS`, 21 liquid roots. A YEAR of statistics each; this is the
  expensive one, so the list stays curated. `MARKET_DATA_FUT_PREWARM` overrides.
- **Defs-only tier** — `defs_prewarm_roots()`: every OTHER root in `OPTION_ROOTS` (ZL, ZM, KE,
  6J, TN, LE/HE/GF/DC/CB, the micros). Definition snapshot only, no statistics. Runs last in
  `prewarm()`, best-effort per root, never enters the accrete result or the day stamp — a
  defs-only root must not make tomorrow's `--daily-once` think its year of statistics ran. It
  DOES run on an already-stamped day: the stamp guards a year of statistics, not a 5-day-TTL
  listing. `MARKET_DATA_FUT_DEFS_PREWARM=ZL,KE` narrows it; an EMPTY value turns it off;
  `MARKET_DATA_FUT_DEFS_SNAPSHOT=0` skips it entirely.

```bash
python3 scripts/futhist.py --defs-only        # the tier alone, no accrete (add a ROOT for just one)
```

The `_refresh_defs` age gate (TTL/2 ≈ 2 days) means a steady state pulls each root ~2×/week.

**Distribution (v1.166.1).** The snapshots ride the SHAREABLE zip too, as a `store.tgz` carrying
`futures/defs/` and nothing else — cloud Cowork installs from that zip, and without this the tier
above stopped at the distribution boundary. It is a tarball rather than an unpacked `store/`
directory on purpose: an unpacked one would flip `futhist._resolve_store_dir` onto
`<skill>/store/futures`, which is read-only on a Desktop/Work mount, while the tarball is read
through `opthist._load_packaged_store_bytes` and leaves the resolver alone. The leak check verifies
the tarball's own member list against `futures/defs/<CODE>.json.gz`, so a vol-store record cannot
ride along; the vol store stays private-build-only. Zip 1.2 → 2.4 MB.

Note the shelf life: a packaged snapshot is subject to the same **5-day TTL** as a local one
(`MARKET_DATA_FUT_DEFS_TTL_DAYS`), so a zip older than that installs cold again. Re-upload after a
prewarm run when the cloud side matters.

## Commodities (Databento side)

GLBX.MDP3 — futures + options on futures (CME/CBOT/NYMEX/COMEX), full depth,
near-real-time top-of-book. Front-month roots verified 2026-06-29:
- Energy: `CL` (WTI), `BZ` (Brent), `NG` (Henry Hub), `HO` (ULSD), `RB` (RBOB).
- Metals: `GC`, `SI`, `HG` (copper), `PL`, `PA`.
- Grains/oilseeds (CBOT): `ZC`, `ZS`, `ZW`, `ZL` (bean oil), `ZM` (bean meal).
- Livestock (CME): `LE`, `HE` (lean hogs), `GF` (feeder cattle).
- NOT in GLBX.MDP3: ICE softs — coffee `KC`, sugar `SB`, cotton `CT`, cocoa `CC`
  (separate dataset). Route softs to Alpha Vantage spot series.
- Constructible: forward/term-structure curves (a root across months) and crack
  spreads (CL vs RB/HO).

## Databento provider facts

- Default dataset: `GLBX.MDP3`. Allowed datasets gated by
  `MCP_DATABENTO_ALLOWED_DATASETS` when set by the operator.
- L0 full-history / L1 last-12-mo / L2-L3 last-1-mo schemas as in the guardrails.
- Direct output capped by `MCP_DIRECT_MAX_RECORDS` (default 10000) → batch for
  large/ALL_SYMBOLS.
- Distribution: Databento ships as a local MCP server / Desktop Extension asking
  for `DATABENTO_API_KEY` via sensitive config — never via prompts or committed
  files. It must not bundle or request Alpha Vantage credentials.
