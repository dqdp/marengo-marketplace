# MOEX Reference (Moscow Exchange via the public ISS API — keyless)

For current quotes prefer the official BCS MCP. Follow the Current MOEX quotes route in SKILL.md. The ISS sections below describe delayed snapshots, history and analytics.

Open this for any Russian-market request: MOEX indices, Russian shares, OFZ /
corporate bonds, RUB FX pairs, and FORTS futures/options. Neither MCP connector
covers Russian markets (Databento = CME only; Alpha Vantage has no MOEX / Russian
listings), so MOEX is a **third source**, reached through the public ISS API
(`iss.moex.com`) — **no API key**.

Surfaced today through `scripts/chart.py --source moex` (charts). The same ISS
endpoints below can be called directly for other data (last price, order book,
security master) when a request needs more than a chart.

## Segments (ISS engine / market)

| Asset | engine / market | Example secids |
| --- | --- | --- |
| Indices | `stock / index` | IMOEX, RTSI, MOEXBC, sector (MOEXOG…) |
| Shares / ETFs | `stock / shares` | SBER, GAZP, LKOH, YDEX (board TQBR) |
| Bonds | `stock / bonds` | OFZ, e.g. SU26238RMFS4 — full board below (`scripts/moex_bonds.py`) |
| FX | `currency / selt` | USD000UTSTOM (USD/RUB), EUR_RUB__TOM, CNYRUB_TOM |
| Futures (FORTS) | `futures / forts` | RIH6 (RTS), SiH6 (USD/RUB), GZH6 |
| Options (FORTS) | `futures / options` | FORTS option secids |

`scripts/chart.py` **auto-resolves** engine/market from the security's primary
board (`/iss/securities/{SECID}.json`), so `--source moex` works for any secid
above without naming the segment; pass `--moex-market` to force one.

**Find the secid FIRST for a named instrument.** An agent asked to "chart USD/RUB"
or "the RTS future" must map the NAME → a MOEX secid before invoking — the ISS
security search is that discovery step: `/iss/securities.json?q=usd` (or `q=rts`).
Common ones: USD/RUB → `USD000UTSTOM`, EUR/RUB → `EUR_RUB__TOM`, CNY/RUB →
`CNYRUB_TOM`; FORTS front months roll `H/M/U/Z` + a year digit (RTS `RIH6`, USD/RUB
`SiU6`, gas `GZH6`). Passing a bare name (`USDRUB`) fails with "MOEX ISS does not
know 'X'".

**Cross-exchange collision:** MOEX `Si` (the USD/RUB FUTURE on FORTS) is NOT CME
`SI` (silver). Case + venue decide — a `Si…`/FORTS request routes to MOEX, `SI`/
silver to Databento (see SKILL.md's collision table).

**When auto-resolve isn't enough:** `chart.py` picks the security's PRIMARY board
(`is_primary`, else `is_traded`, else row 0). If the resulting series looks thin or
odd — a security quoted on several boards where the primary isn't the liquid one —
pin the segment with `--moex-market` (e.g. `--moex-market shares`).

## Charting

```
<python> "<skill-dir>/scripts/chart.py" IMOEX         --source moex --interval 1d --days 182 --png
<python> "<skill-dir>/scripts/chart.py" SBER          --source moex --interval 1d --days 90  --png
<python> "<skill-dir>/scripts/chart.py" USD000UTSTOM  --source moex --interval 1h --days 5   --png
<python> "<skill-dir>/scripts/chart.py" SU26238RMFS4  --source moex --moex-market bonds --interval 1d --days 90
```

Intervals: `1` / `10` / `60` min, daily, weekly, monthly (the script maps
`1d→24`, `1h→60`, `1w→7`, `1mo→31`; finer targets pick the finest base that divides
them — `30m→10` but `15m→1`). Volume panel = rouble turnover (`value`); indices
have no share volume. Speed: ~0.7s per ISS call (auto-resolve adds one lookup
call; pass `--moex-market` to skip it).

## Raw ISS endpoints (beyond charts)

- Candles: `/iss/engines/{engine}/markets/{market}/securities/{SECID}/candles.json?interval=24&from=YYYY-MM-DD&till=YYYY-MM-DD`
- Segment/board lookup: `/iss/securities/{SECID}.json?iss.only=boards&iss.meta=off`
- Last price / marketdata snapshot: `/iss/engines/{engine}/markets/{market}/securities/{SECID}.json`
- Security search: `/iss/securities.json?q=sber`

All return JSON (add `?iss.meta=off` to trim metadata, or use `.csv` for CSV).

## Freshness & coverage (verified)

- **Two data layers.** (1) Historical OHLCV **candles** — what `chart.py` uses —
  end-of-period. (2) A **marketdata snapshot** (`last`/`bid`/`offer`/`systime`) via
  `/iss/engines/{engine}/markets/{market}/securities/{SECID}.json?iss.only=marketdata`.
- **Delay: ~15 min on the public/free tier** (MOEX policy); true real-time needs an
  authorized/paid ISS connection. No public real-time socket. The snapshot is
  **delayed data — NEVER present it as the live price** (SKILL.md hard safety rule):
  always label the delay and carry the `SYSTIME`; if the user needs a real-time
  tick, say it isn't available on the public feed.
- **Derivatives (FORTS) ARE covered** — futures and options via `futures/forts`
  and `futures/options`. Verified: `RIH7` returned daily candles + a delayed quote
  (`LAST`, current `SYSTIME`). Front contract changes quarterly (H/M/U/Z + year
  digit); discover live secids via the FORTS securities list or `/iss/securities.json?q=`.

## History depth (verified via `/iss/.../candleborders.json`)

Deep daily/weekly/monthly; intraday is shorter and uniform; FORTS is per-contract.

| Security | daily/weekly/monthly from | intraday (1/10/60min) from |
| --- | --- | --- |
| RTSI (index) | **1995-09** | 2011-12 |
| IMOEX (index) | **1997-09** | 2011-11/12 |
| USD/RUB (FX) | 2003-04 | 2011-12 |
| SBER (share) | 2007-07 (listing) | 2011-11/12 |
| FORTS contract (e.g. RIH7) | contract listing only (~months–2y) | contract listing only |

- **Daily+** goes back to the security's inception (indices to the mid/late-1990s,
  shares to their listing, FX to 2003).
- **Intraday (1/10/60min)** starts ~**Dec 2011** for everything — MOEX's intraday
  candle archive begins then (~14 years), regardless of how old the security is.
- **FORTS futures/options** only have their own contract life; there is no
  auto-stitched continuous series — pick the active contract, or chain contracts
  yourself for a long history.
- Check exact ranges per security/interval with
  `/iss/engines/{engine}/markets/{market}/securities/{SECID}/candleborders.json`.

## Caveats

- Snapshot is delayed, not a live tick — don't present it as realtime (see the
  hard safety rule above and in SKILL.md reporting rules).
- RUB-denominated; FX pairs are RUB crosses; timestamps are Moscow exchange time.
- Public API — be polite (no tight loops), narrow `from`/`till`.
- Data-for-charts/reads source, not an MCP tool: no native greeks/options
  analytics. To price FORTS options, feed quotes into `scripts/greeks.py`
  (Black-76 for futures-margined; Black-Scholes for premium-on-spot equity).

## Russian equity option chains — `scripts/moex_options.py` (v1.134.0)

A full options dashboard for **Russian STOCK options** (MOEX FORTS, premium-style,
exercising into the SHARE): SBER, GAZP, LKOH, ROSN, ALRS, MTSS, TATN, MAGN… — the
~41 `margin_style=P` / `option_on_spot=1` assets. On the SAME `dashboard.py` engine
as the equity/futures chain pages.

- **Data**: the keyless statistics `optionboard` — the ONLY place MOEX publishes
  per-contract IV. Per series (call+put): `STRIKE`, `VOLAT` (IV, **percent** → the skill's
  fraction), `THEORPRICE`, `BID`/`OFFER`, `VOLTODAY`, `OPENPOSITION`; the `asset` block
  carries the ATM `CENTRALSTRIKE`, `UNDERLYINGSETTLEPRICE`, `LASTDELDATE`. Series list:
  `…/assets/{SYM}/expirations.json` (W/M/Q). Spot + change% + name: the TQBR share board
  (`fetch_profile`). RUB risk-free: **RUSFAR** (`RUSFAR`/`RUSFAR1MN` on board MMIX,
  `CURRENTVALUE` percent) — the tenor nearest the DTE; `--rate` / `MARKET_DATA_RUB_RATE`
  override.
- **IV is the exchange's** (`VOLAT`, read as-is); **greeks are COMPUTED** (Black-Scholes on
  the share, RUSFAR rate) — the smile/skew need no rate, only Δ/Γ do. Say so in the read.
- **Commands**: `moex_options.py SYM` (near series) · `--expiry YYYY-MM-DD` · `--term` (curve
  + IV surface + series table + switcher) · `--full` (Overview · Chain · Term · History tabs) ·
  `--history` (v1.135.0 — the vol-history page). text / `--json` / `--html` /
  `--from-dir <raw ISS optionboard.json>` (offline, single-series).
- **Vol history (`--history`, `scripts/moexopthist.py`)** — IV rank / vol cone / constant-maturity
  IV / skew & PCR history, from a MOEX **settlement** backfill. The history endpoints carry NO IV
  (VOLAT is live-only), so IV is **RECOMPUTED** per (day, strike) from that day's option settlement +
  the share's close via `greeks.implied_vol` (Black-Scholes on the share, RUSFAR rate) — the futhist
  pattern; records match `opthist`'s shape so its builders render the page unchanged.
  **Dense reconstruction (v1.136)**: rather than reduce only the contracts listed TODAY (which left an
  OLD day carrying just the tenors those survivors still spanned — a recent-biased, crash-week-dominated
  cone), the store now DISCOVERS every option that traded across the window — it samples `assetcode`-
  filtered per-date settlement boards, resolves each SECID (even long-expired) to strike/type/expiration
  via the global `/securities/{SECID}` lookup, and pulls each contract's own settlement history — so the
  constant-maturity cone / IV-rank / skew history are DENSE back to the discovery window's start (SBER:
  cm30 140 / skew30 136 of ~284 sessions, earliest ~Jan-2026, median cm30 ~18% — representative, not the
  old 12/133 recent sample). Cost: a **~1–3 min COLD backfill** per symbol (the store then persists;
  `--full` and re-renders are instant). **Incremental (v1.137)**: a rebuild caches the three IMMUTABLE
  phases per assetcode beside the store — a past date's OI-bearing SECID set, a SECID's definition, and
  an EXPIRED contract's settlement history never change — so a warm rebuild re-fetches only what moved
  (new dates/contracts + still-LIVE contracts), dropping ~1300 ISS round-trips toward ~200 (names whose
  window is mostly expired freeze nearly all of it; names with deep live LEAPS, e.g. GAZP out to 2030,
  still re-pull those). Caches live under `~/.cache/market-data/moexopt/cache/` and are best-effort (a
  missing/corrupt cache just re-fetches — ISS stays the source of truth). `--years` (default 0.5) sets the
  discovery window; depth beyond it rides each contract's own listed life. Futures-margined twins (strike ≈
  the future price, NAME says «на фьюч») are pre-filtered by strike magnitude + the resolved contract name.
  A day still needs ≥2 bracketing tenors with a valid ATM IV to carry a CM point (thin/holiday days stay
  honest holes, never a fabricated 0). **PCR is NEAR-dated (≤90d series only, `opthist._PCR_MAX_DTE`)** —
  a whole-book sum is swamped by deep-OTM LEAPS call walls on names that list far series (GAZP lists calls
  out to 2030 with ~zero put OI → a whole-book PCR of ~0.04 while the near book is call-heavy but
  meaningful; the front month alone is ~1.16). The same near window governs the AV/opthist PCR (shared
  reduce). Store: `~/.cache/market-data/moexopt/<SYM>.hist.json`.
  `--full` fills its History tab from this store when warm, else shows a 'warming' note naming
  `--history`. HONESTY: the recomputed IV is NOISIER than the live exchange VOLAT — near-expiry and
  crash-week solves can be extreme (real vol spikes, but read the cone with that in mind); every
  surface says "IV RECOMPUTED (Black-Scholes on the share)", never "Alpha Vantage", never "live".
- **HONESTY (hard, per the delay rule above)**: public ISS is **DELAYED ~15 min** — the header,
  the lead's "as of … · delayed ~15 min" meta, and the footer all say so; NEVER "live". RUB (₽)
  everywhere; a no-quote leg is n/a (never a fabricated 0); a spot that fell back to the board
  settlement is marked " est"/"(settle)". History tab is a 'warming' placeholder — no MOEX vol
  store yet (Tier 2). Futures-margined MOEX options (Si/CNY/RTS, Black-76) are NOT covered yet.

### The written read (RU equity chains)

ORDER (SKILL.md web-discipline): `moex_options.py SYM --full --html out.html` renders the combined
page — go straight to `--full`
(no `--list-expiries` pre-pull, MOEX has no connector), **PRESENT THE FILE THE INSTANT IT RENDERS**, and
only THEN write the read + the offer. The read draws from the dashboard's OWN numbers; do NOT web-search
the earnings date before presenting (the offer's earnings-preview pins it). Then the 3–6-sentence
synthesis, MOEX-honest:

The HTML producer creates the reciprocal fundamentals sibling automatically, using the same desktop
pending-link and Cowork embedded-board contract as US equities. A fundamentals render creates
`moex_options.py SYM --full`; an options render creates `moex_fundamentals.py SYM`. Each child receives
the actual descriptive parent filename, so both directions work even when the OpenAI output is not
named `sym.html`. `MARKET_DATA_NO_SIBLING` stops recursion. Claude/Cowork's legacy presentation and
offer flow is otherwise unchanged.

- **Level & delay**: ATM IV as the market's vol quote for the near series — and state the ~15-min
  delay + that IV is the exchange's (`VOLAT`), greeks computed. A MOEX vol store now EXISTS (v1.135+,
  `moexopthist.py` / `--history`, IV recomputed Black-Scholes on the share): when it is WARM for the
  name, cheap/rich is rank-vs-own-history + the vol cone like the AV path; when it is COLD (History tab
  says "warming"), fall back to **SHAPE-relative** (the term structure), NOT a percentile — say which
  one you're using, don't imply a rank you don't have.
- **Expected move**: spot × ATM IV × √(DTE/365) in **roubles** — "the market prices ±₽X (±Y%) by
  <expiry>".
- **Skew**: the ~10%-OTM proxy; Russian single-name equity typically shows a **put skew** (downside
  bid), like the US — call it out and name the sign.
- **Term structure**: front-rich (a near-term event / high near-dated IV) vs back-rich (contango);
  the Term tab's curve + the Overview slope tile name it.
- **Positioning**: PCR(OI), the OI walls, max pain as a pin heuristic (not a target), Net GEX / the
  naive dealer sign.
- **Liquidity honesty**: FORTS single-name books are THIN (~12 strikes/series, many zero bids) — say
  when the surface is too sparse to over-read; an interpolated smile point is marked `~`.

Worked shape (not a template): «SBER: рынок закладывает ±₽14 (±5.4%) к пятнице при ATM IV 42%
(биржевой VOLAT, данные ISS с задержкой ~15 мин, греки посчитаны Black-Scholes). Улыбка с put-
перекосом (+10 пунктов) — защита от падения дороже апсайда; термструктура front-rich (42% фронт
против 28% на дальних) — весь риск в ближней экспирации. Стены OI на 280/290 коллах и 240 путах,
max pain 260; истории волатильности по MOEX пока нет, поэтому «дорого/дёшево» — относительно формы
кривой, не своего перцентиля. Ликвидность тонкая — 12 страйков в серии, крылья читать осторожно.»

**Then END with the deeper-document OFFER** — the SAME closing step the equity/ETF options read
carries (`options.md`), and the one the MOEX path was MISSING: on 2026-07-24 the YDEX (Яндекс)
Cowork session offered `equity-research:earnings-preview` as a one-click card (the user clicked it →
a real `YDEX_Q2_2026_earnings_preview.docx`), while the GMKN and ROSN sessions — same MOEX single-name
options shape — closed text-only, card-less. Without this step the offer is NON-DETERMINISTIC (whatever
the model improvises); make it deterministic. Pick ONE company document by context:
- A MOEX single-name almost always carries a **near-dated event** driving the term backwardation (a
  high near-week IV over the month/quarter — the YDEX and GMKN reads both showed it); that event is
  usually **earnings** → `/equity-research:earnings-preview` (the option market's expected move pairs
  naturally with the scenarios). A dated NON-earnings catalyst → `/equity-research:catalysts`.
- **Russian fundamentals are NOT in Alpha Vantage** (no RU coverage), but the skill now has a scoped
  numbers module: **`scripts/moex_fundamentals.py SYM --json`** (v1.146+) — structured quarterly + annual
  revenue/EBITDA/net income + P/E, EV/EBITDA, P/S, ROE from smart-lab (compiled МСФО/РСБУ, ₽bn, release-
  lagged), cross-checked against a Yahoo TTM snapshot, with MOEX ISS for cap/shares/dividends. So the
  earnings-preview document should source the HISTORY from `moex_fundamentals.py --json` (or `--save-raw`)
  — OUR structured numbers, not a fresh web scrape — and web-source ONLY the FORWARD view (upcoming date,
  guidance, consensus — none of which is in any keyless source). The OPTION figures still come from our
  scripts. Honesty rides along (smart-lab is a second-hand compilation, ₽bn-rounded, lagged — never
  "live"/"primary filing"; a bank shows net income/ROE, no EBITDA). Proven end-to-end: the YDEX
  earnings-preview DOCX (Q1-2026 выручка ₽372.7 млрд, EBITDA ₽73.3 млрд) — the history now comes
  first-hand from `moex_fundamentals.py YDEX`, only the 29-июля date stays web-sourced.
- **Sector as a COMPLEMENT, not a replacement** (user 2026-07-24): default to the ONE company card. Add
  a SECOND `/equity-research:sector <complex>` card ONLY when the commodity/sector story is genuinely
  load-bearing for the name — a metals/mining producer (GMKN → палладий/никель/металлы, PLZL → золото,
  ALRS → алмазы), an oil major (ROSN/LKOH/TATN → нефть), a fertiliser/steel name. A bank (SBER/VTBR),
  a tech/internet name (YDEX/OZON), a retailer (MGNT) → company card only (no organic sector overlay).
- Render the one-click card via `<skill-dir>/scripts/offer.py "<exact command>" "<label>"` → show_widget
  (checking THIS session's own skill list first per SKILL.md — pass `--skill-available <skill>` when the
  mapped skill is mounted), PLUS the plain-text line naming the command (the card can hang at "Connecting
  to visualize…"). An OFFER, never an auto-run.

## Russian company fundamentals — `scripts/moex_fundamentals.py` (v1.160.0)

A scoped RU fundamentals producer with a responsive dashboard and compact text twin:
`moex_fundamentals.py SYM --html out.html --summary-out out.txt` performs one gather and writes both.
It also creates/embeds the reciprocal `moex_options.py SYM --full` sibling. Structured RU fundamentals for a
MOEX-listed company, the RU counterpart to the AV `fundamentals.py` role. `moex_fundamentals.py SYM`
(text · `--json` · `--save-raw DIR` · `--from-dir DIR`). It fills the gap where Alpha Vantage has zero
RU coverage, so a written read can cite OUR numbers and the `earnings-preview` offer consumes our
structured history instead of web-reading it.

**Sources (in fallback order, never hard-fails):**
- **smart-lab.ru/q/SYM/f/q/ (+ /f/y/)** — PRIMARY. Keyless HTML, per-period Выручка/EBITDA/Чистая
  прибыль/Долг/Активы/Капитализация + P/E · EV/EBITDA · P/S · ROE + dividends, keyed by a stable
  machine-readable `field="…"` attribute (the Cyrillic labels are the canary in the test). Values are
  ROUNDED to ₽bn, release-lagged, a second-hand COMPILATION of the issuers' МСФО/РСБУ reports — **never
  a primary filing, never live, never the upcoming quarter**. Banks (SBER/VTBR) carry net income/ROE but
  NO EBITDA/revenue → the record is sector-aware (`sectorShape: "bank"|"corp"`; a bank never fabricates an
  EBITDA line).
- **Yahoo quoteSummary/SYM.ME** — keyless JSON CROSS-CHECK / fallback. A live TTM snapshot from
  `financialData` (revenue matched GAZP's smart-lab LTM to the decimal); its per-statement modules are
  frozen ≤2021–2023 for RU names, so TTM net income is DERIVED (revenue × margin), not read from the stale
  statements. Currency-tagged — GMKN reports in USD, so a Yahoo figure is NEVER mixed into the ₽ series
  and the cross-check only fires within one currency.
- **MOEX ISS** — the market wrapper (shares/cap/name/currency via `moex_options.fetch_profile`; dividends
  via `/securities/SYM/dividends.json`). First-party price/shares/cap even when the scrapes disagree.

A total miss returns an honest `unavailable` record (not a crash, no fabricated zeros); a hole is `None`.

**The dashboard** (v1.149): `moex_fundamentals.py SYM --html out.html` renders the tabbed board on the
same `dashboard.py` engine as every other page — **[Overview · Financials · Dividends]**:
- **Overview** — the synthesis cards (Rev YoY / margins / net cash / P/E / EV-EBITDA / ROE / div yield /
  52w range / vs-200d MA; a BANK instead gets NIM + capital adequacy, since "net cash" is meaningless
  when deposits are the liabilities), the **price kline** (FULL ISS listing history), the **options-vol
  strip** (30/60/90 CM IV · rank · realized · VRP from the local `moexopthist` store — omitted, never
  faked, when the store is cold or the name has no chain), the headline statement combo and the
  **drawdown** panel (its title NAMES the window — the peak is all-time only as far back as ISS
  publishes candles for that listing).
- **Financials** — revenue/net-income, EBITDA, FCF, balance, **cost structure** (opex/staff/amortisation/
  interest), **per-share** (EPS/BV/FCF) and a **multiples trend**; a BANK gets its own set instead:
  income mix (NII + fees), balance sheet, loan book corp-vs-retail, funding, asset quality & capital
  (NPL/CoR/CAR) and provisions.
- **Dividends** — the ISS dividend history by year (first-party) + **capital returned** (dividends paid,
  with the share count as a context line; RU has no keyless ₽ buyback figure, and the panel says so).
  A ZERO year is printed only for a COMPLETED year; the CURRENT year stays empty because most RU
  registers close mid-year, so "nothing yet" is not a cancellation.
- Deliberately ABSENT (never faked as an empty tab): analyst estimates/consensus, next-earnings date,
  insider flow, 13F holders, scored news — none exist in a keyless RU source. The forward view rides
  the `earnings-preview` offer instead.
- Realized vol uses DIVIDEND-ADJUSTED ISS closes (ISS publishes raw ones; an 8-15% RU yield would
  otherwise enter realized vol as a one-day crash).

**In the written read**: when `moex_fundamentals.py SYM --json` has data, cite OUR numbers — the
revenue/EBITDA/net-income TREND, LTM margins and multiples (P/E · EV/EBITDA · P/S · ROE), the dividend —
alongside the options read, WITH the smart-lab provenance ("сводка по отчётности, ₽млрд, с лагом
публикации") and the bank-vs-corp distinction. Do NOT present it as a live filing. The FORWARD view
(upcoming date/guidance/consensus) is NOT in the module — that stays web-sourced by the offer's
earnings-preview document.

**OpenAI closing block:** the top-of-SKILL fast lane is mandatory. After the dashboard link and written
read, call `render_deep_analysis_offer` exactly once: `initiating-coverage` is always primary and the
reporting-cycle action is always `secondary`. Use earnings analysis for a report published in the last
30 days, earnings preview for a report due within 45 days, otherwise the latest-quarter earnings
analysis. If the keyless RU sources do not expose dependable dates, use latest-quarter analysis rather
than omitting the second action. Pass `defaultLanguage=ru|en`; the shared selector controls both actions.

**Claude/Cowork closing OFFER** — the SAME legacy closing step the RU OPTIONS read carries above,
and the one this path was MISSING when it shipped (the NVTK Cowork session 2026-07-24 closed text-only,
card-less — exactly the v1.144 defect class: a NEW read path shipped without the closing step, so the
offer became whatever the model improvised). A RU COMPANY read ends with a one-click card, always:
- **ONE company document by default**, picked by context: a near report driving the story →
  `/equity-research:earnings-preview`; a fresh quarter already out → `/equity-research:earnings`;
  a valuation question ("дорого или дёшево" — our P/E · EV/EBITDA · P/S are right there) →
  `/financial-analysis:dcf` or `:comps`.
- The document now sources its HISTORY from **our** `moex_fundamentals.py SYM --json` (revenue/EBITDA/
  net income trend, margins, multiples, dividends) instead of re-scraping the web; only the FORWARD view
  (upcoming date, guidance, consensus) stays web-sourced, because no keyless source carries it.
- **SECTOR as a COMPLEMENT, not a default second card**: add `/equity-research:sector <complex>` ONLY
  when the commodity/sector story is genuinely load-bearing — an oil & gas / LNG producer (NVTK, ROSN,
  LKOH, TATN), a metals/mining name (GMKN, PLZL, ALRS), a fertiliser/steel name. NEVER for a bank
  (SBER/VTBR), a tech/internet name (YDEX, OZON) or a retailer (MGNT), where it would be noise.
- Render the card via `<skill-dir>/scripts/offer.py "<exact command>" "<label>"` → show_widget (check
  THIS session's own skill list first per SKILL.md; pass `--skill-available <skill>` when the mapped
  skill is mounted), PLUS the plain-text line naming the command (the card can hang at "Connecting to
  visualize…"). An OFFER, never an auto-run. Naming the document in prose WITHOUT the card is a MISS.

**Breakage detection**: smart-lab is an unofficial scrape (a layout change would silently break the
parser). Two guards — the test's CANARY (asserts the Cyrillic labels resolve in a captured fixture; a
label change FAILS a test, never returns wrong/empty data) + a live smoke (`moex_fundamentals.py GAZP`).
If a live pull returns `unavailable` for a known-liquid name, re-verify the parser against the live page.

## Russian bonds — `scripts/moex_bonds.py` (v1.150.0)

The RU bond path. Everything below was probed LIVE 2026-07-24/25 (design of record:
`docs/moex-bonds-plan.md`). Keyless MOEX ISS throughout, and for bonds ISS is **richer than what the
skill gets for equities**: yield, duration, Z-spread and G-spread are published first-party per
instrument, and the zero-coupon curve arrives as ready points.

```
<python> "<skill-dir>/scripts/moex_bonds.py" SU26238RMFS4 --html out.html   # ONE issue
<python> "<skill-dir>/scripts/moex_bonds.py" --market --html out.html      # the curve + the market
```
Also `--json` · `--save-raw DIR` / `--from-dir DIR` · `--emit-spec` · `--min-turnover`.
Per-day gzip cache (`MARKET_DATA_MOEXBONDS_CACHE`, dir or `off`). ~1-2s warm for an issue, ~7s cold
for the market page. **Present the file the instant it renders**, then write the read.

### Endpoints
| What | Path |
| --- | --- |
| quote + analytics | `/engines/stock/markets/bonds/securities/<SECID>` → `securities` · `marketdata` · **`marketdata_yields`** (`EFFECTIVEYIELD`, `DURATION` in DAYS, `ZSPREADBP`, `GSPREADBP`, `YIELDDATETYPE`, `YIELDDATE`, `ZCYCMOMENT`) |
| coupons / amortisation / offers | `/securities/<SECID>/bondization.json?limit=unlimited` — **`limit=unlimited` is required**, the default truncates a long schedule |
| the G-curve (КБД) | `/engines/stock/zcyc.json` → `yearyields` (ready points) + `params` (NSS) |
| the curve in the past | `/history/engines/stock/zcyc.json?**date=**YYYY-MM-DD` — **`from`/`till` are SILENTLY IGNORED** and answered with TODAY's curve (verified: `from=2025-07-20` returned 22 514 rows all dated 2026-07-24). Always re-check the `tradedate` you got back |
| the universe | `/engines/stock/markets/bonds/boards/{TQOB,TQCB}/securities.json?iss.only=securities,marketdata,marketdata_yields` — ONE call per board, analytics included. TQOB 62 OFZ · TQCB 3 026 corporates |
| daily history | `/history/engines/stock/markets/bonds/securities/<SECID>` — **pages at 100 rows** and does not say so; walk `start=` until a short page |
| issuer link | `/securities.json?q=<name|isin>` → `emitent_id` ties a bond to the issuer's EQUITY (verified: FEES and RU000A0JSQ58 share 710) |

### The trap: half the market is not a plain bond
TQCB by `BONDTYPE` (live): фикс 970 · **флоатер 585** · структурная 494 · амортизируемая 475 ·
фикс-с-неизвестным-купоном 275 · **валютная 200** · линкер 9. ISS publishes a yield and a spread for
ALL of them, and for the floaters those numbers are nonsense — the module suppresses them:

- **Флоатер → NO YTM, NO spread, not on the curve.** ЯНДЕКС1Р1 publishes `EFFECTIVEYIELD -1.4007`
  and `GSPREADBP -1522`. Not an ISS bug: YTM is undefined when future coupons float. The page says so
  and shows the CURRENT coupon instead.
- **A yield without its basis is the worst number on the page.** `YIELDDATETYPE` is `MATDATE` or
  `OFFER`; ФСК РС-22 reads **-28.891% to an offer four days out**. Always quote the basis in the read.
- **Валютная (USD/CNY/EUR/CHF) → the yield is in the currency of the nominal**, so no ₽-curve spread,
  not on the ₽ scatter, tagged with its currency.
- **~0 duration → no spread and no curve point**: at 1 day to the yield date, ФСК's G = Z = -4295bp is
  annualisation noise, not a credit premium.
- **Amortising**: ISS keeps `coupons.facevalue` at the ORIGINAL nominal while the coupon value falls
  (ГТЛК 2P-13: 13.11 → 5.24) — the outstanding face is computed from the amortisation legs.
- **`BUYBACKDATE` `'0000-00-00'`** is ISS's NULL sentinel, not a date.
- **NO CREDIT RATINGS.** Not in ISS; АКРА/Эксперт РА/НКР are bot-walled. Do NOT scrape them and do NOT
  infer a rating from the yield. `LISTLEVEL` (1/2/3) and the `RUCB*` rating-bucket indices are
  surrogates and must be labelled as such.
- **Liquidity**: of 3 026 TQCB issues 1 779 traded today but only ~252 turned over >₽10M. Universe
  panels filter by turnover and SAY the filter. ОФЗ 46012 at ₽38k turnover printed a 2.43% yield
  against a 14% curve — a stale print, excluded from the map.

### The curve maths (the one thing we compute)
Today's curve uses the PUBLISHED `yearyields` — no maths. Historical legs must be evaluated from the
NSS `params` (the history endpoint carries no ready points): MOEX's published constants a1=0, a2=0.6,
k=1.6, `a(i+1)=a(i)+b(i)`, `b(i+1)=b(i)·k`, nine Gaussian corrections on a Nelson-Siegel base in BASIS
POINTS, and `Y = 100·(exp(G/10000) − 1)` — the **exponential** conversion (the linear reading is off by
1.17pp, the exponential by <0.04pp). Because ISS publishes params AND yearyields at the same timestamp,
`curve_fit_error()` checks our implementation against first-party output every run; beyond 0.05pp the
historical legs are DROPPED rather than drawn.

### Written read — the ordering
1. **What the paper is** — issuer, coupon type, maturity/offer, currency, amortisation. If it is a
   floater/structured/linker, say that FIRST: it decides which numbers exist at all.
2. **Yield AND its basis** — «доходность 15.5% к погашению 2041-05-15», never a bare number. If there
   is an offer before maturity, the offer is the real horizon.
3. **Spread to the sovereign curve** — the credit premium (G-spread), with the honest caveat when the
   horizon is too short or the bond is not ₽ fixed-coupon.
4. **Cash flow** — coupon size/frequency, НКД, the next payment, amortisation legs, the nearest offer.
5. **Liquidity** — turnover and trade count; a thin bond's price is not a price.
6. **Risk of the TYPE** — floater = rate risk moved to the coupon; amortising = shortening duration;
   валютная = FX risk; structured = payoff not derivable. Never a credit rating we do not have.

**Then END with the deeper-document OFFER** — the same closing step the RU options and RU fundamentals
reads carry (the v1.149.1 lesson: a NEW read path that ships without it closes card-less). For a bond,
the organic follow-up is about the ISSUER'S CREDIT, not the instrument:
- **Corporate issue** → the issuer's own numbers first: `moex_fundamentals.py <EQUITY> --json` for
  leverage, then ONE card — `/financial-analysis:comps` (does this issuer's spread fit its leverage vs
  peers) or `/equity-research:thesis`. Resolve the equity via `emitent_id`.
- **ОФЗ / the market page** → a macro/rates framing card (`/equity-research:sector` on the RU rates
  complex) ONLY when the ask is genuinely about the direction of rates; for one OFZ, usually no second card.
- Render via `<skill-dir>/scripts/offer.py "<exact command>" "<label>"` → show_widget, PLUS the
  plain-text line naming the command. An OFFER, never an auto-run.

**Breakage detection**: ISS is first-party and stable, but it degrades by SILENCE, not by error — an
ignored parameter returns today's curve, a paged endpoint returns page 1, a floater returns a yield.
The suite pins each of those with a real captured payload. If a live pull looks too clean (a "history"
that stops months ago, a "1Y назад" curve dated today), suspect a silently ignored parameter first.

### Analytics — what ISS does not compute (v1.151.0)

v1.150 only re-presented published fields. These are DERIVED, and each one is gated on a check that
can fail (design of record: `docs/moex-bonds-analytics-plan.md`).

**Rate sensitivity.** Modified duration, **convexity** (ISS publishes none) and a −300…+300 bp
scenario ladder (price + 1-year total return), computed by discounting the real `bondization`
schedule at the exchange's own yield. **The gate is the price itself**: our PV must reproduce the
market DIRTY price within 0.5%. Live: ОФЗ 26238 0.14% ✅ · РусГид2Р07 0.13% ✅ · ФСК РС-22 **44.8% ❌**
— because ISS quotes ФСК to an OFFER four days out, so its flows end at the offer, not at the 2027
maturity. That single gate refuses yield-to-offer paper, floaters and structured notes without a
blacklist. Linkers are excluded EXPLICITLY as well (their flows are nominal, their yield is real, and
ОФЗ-ИН 52002 misses the gate by only 0.66% vs a 0.5% threshold — too close to rely on).
Read: quote convexity when the question is "а если ставки развернутся" — a long bond gains more on
a rally than it loses on an equal selloff (ОФЗ 26238: +22.4% vs −16.4%), a short one barely at all.

**Floater coupon rate.** `valueprc` comes back empty, but `value` does not: rate = value ÷ face
OUTSTANDING × 365/period. ЯНДЕКС1Р1: 22.6% → 16.04% over a year, against RUSFAR 19.7% → 13.9% — the
distance between them IS the issue's premium over the money market (+2.14 pp). Say «восстановлено из
выплат»: it is derived, never an exchange field. Use `faceAfter`, not the original face — on an
amortising floater the two readings differ by a fifth (ГТЛК 1P-01: 9.20% vs 7.60%).

**Yield percentile.** Where today's yield sits in the bond's own window (ОФЗ 26238: 92nd percentile
of its year — cheap relative to itself). Gated on the same rule as the yield: ISS does send a
YIELDCLOSE series for a floater, but it is not the quantity we suppressed and we cannot name it, so
it is not ranked either.

**Breakeven inflation** (market page). Nominal curve at a linker's duration minus its real yield:
5.47% at 1.5y → 8.57% at 6.2y, i.e. the market prices accelerating inflation. Zero extra calls.
ОФЗ-ИН indexation lags CPI ~3 months, so it is an approximation and the panel says so; a linker's own
yield still never appears as a point on the nominal curve — only the DIFFERENCE is comparable.

### v1.152 — the «Рынок» tab, the issuer curve, and what the charts actually show

**The market rides as a fourth TAB** on the issue page (`--no-market` skips it *and* the two
analytics that ride its pulls: the issuer curve and the История sovereign line). Its data is not
bond-specific, so the cold ~4 s is day-cached across every bond (a second bond that day: ~1 s) and
merging saves ~300 KB because the engine ships once instead of twice. `--market` stays a standalone
entry point.

**The ISSUER YIELD CURVE** — the honest per-bond analogue of the КБД. Resolution is a trap:
`securities.json?emitent_id=` SILENTLY IGNORES the filter and `/iss/emitents/<id>/securities` 404s.
What works: the bond's own description carries `EMITTER_ID`, and a shortname-prefix search returns
`emitent_id` per row — the search is the heuristic, the ID is what makes it correct. Then intersect
with the board already pulled for the tab. Gates, all learned from live data: ONE basis per curve
(ГТЛК has issues quoted to an offer beside issues quoted to maturity — mixing them is the module's
cardinal sin, drawn); traded today; and a ₽ issue printing more than 100 bp THROUGH the sovereign
curve (ГТЛК БО-08: 7.05% against 13.78%, on ₽4 967 and 3 trades) is a broken print, not a premium.
Every exclusion is counted and disclosed. Live Селигдар3Р: 7 issues, premiums 260–407 bp, and the
bond itself is the CHEAPEST against its own issuer — the question the sovereign curve cannot answer.

**Visualisation, reworked on the user's critique (2026-07-25).** A chart earns its place only if it
shows something a card cannot:
- **История** no longer plots price AND yield. Measured on live data, their daily changes correlate
  **−0.995** (ОФЗ 26238) and **−0.978** (Селигдар3Р) — the second line spent an axis restating the
  first. It now plots the yield against **ОФЗ at the same duration** (12 monthly ZCYC slices via
  `date=`, cached per DATE so all bonds share them): the gap between the lines IS the credit
  premium, so «rates moved» and «this credit re-priced» finally separate. The sovereign leg uses
  **that day's** duration, which ISS sends per row — a year ago the bond was a year longer.
  For a floater/fx/linker/offer issue the tab shows the PRICE instead: the yield is one we refuse
  to name, and a premium against the ₽ curve would be the mislabel this module exists to prevent.
- **Денежный поток**: for a plain bond the coupons are identical BY CONSTRUCTION, so bars could not
  show dynamics. It is now the **cumulative return** against what a buyer pays today (price + НКД),
  with the payback point marked; its steepness is the duration made visible. Bars stay where the
  payments genuinely vary (amortising, floater, step). Refused outright when face and settlement
  currencies differ — they cannot be summed.
- **Breakeven inflation** is a LINE with visible points plus a card: four numbers are a term
  structure, and bars claimed four independent categories.

**Engine changes (affect EVERY dashboard in the skill, not just bonds):** the `bubble` renderer now
honours a per-point `color` (it was palette-by-index, so a single point could only be singled out by
size); the `line` renderer honours a per-series `spanGaps` (a sparse monthly series beside a daily
one otherwise draws NOTHING — isolated points at pointRadius 0 with no segment between them); and
the CARD row is laid out from its own MEASURED width instead of an `@media` query on the viewport —
in an embedded viewer the container can shrink while the viewport does not (expand to fullscreen,
then restore), and the cards stayed at 5 tracks inside a 450 px column, tiny tiles that no resize
could fix.

### v1.152.1 — data depth (measured, not assumed)

| ряд | глубина | цена |
| --- | --- | --- |
| история цены/доходности | ВСЯ жизнь выпуска | ОФЗ 26238: 1356 строк / 14 страниц / **1.9 s**; ГТЛК 1P-01 (2016) >20 страниц |
| срезы КБД | 12 **квартальных** = ~3 года | ~1 s за срез, итого ~11.4 s — столько же, сколько 12 месячных за 1 год |
| перцентиль доходности | окно 1 год (заголовок) + 3 года (подпись) | бесплатно, считается по уже загруженной истории |

**Окно перцентиля называется не для красоты.** ОФЗ 26238 живёт с 2021 года, когда ставки были ~7%,
до 2026-го с ~15%: за год бумага в 92-м перцентиле, за три — в 84-м. Безымянное число описывало бы
смену режима, а не бумагу, поэтому окно указывается и на карточке, и в тексте.

**ISS завышает заявленную глубину.** `zcyc.json?iss.only=params.dates` обещает архив с 2014-01-06, но
`date=2016-06-20` и `date=2018-06-20` возвращают ПУСТО — фактически данные примерно с 2020. Третий
случай того же класса (`from`/`till` игнорируются, `emitent_id` игнорируется, `params.dates` завышает):
глубину и охват проверять фактом, а не заявлением эндпоинта.

### v1.153 — the ISSUER-GROUP page and clickable member issues

`moex_bonds.py --issuer NAME --html out.html` builds the group board: **Обзор** (median yield with
the min–max RANGE beside it — never a fake "average bond" — duration range, median G-spread, the
issuer's own credit curve, and the table of issues), **Денежный поток** (the combined payment
calendar: coupons and redemptions charted SEPARATELY, because return of capital is not income), and
**Рынок** (the same market tab the issue page carries). Resolution reuses the v1.152 machinery, so
the same warning applies: a NAME is not an identifier — live, `q=Селигдар` returns 19 bonds across
TWO issuer ids, and the page states in its lead which id it kept and how many bonds it left out.
`--min-turnover` and `--no-market` behave as on the other pages.

**The issues table links to each issue's own dashboard — and why it is built the way it is.** The
obvious question is why the page does not simply render an issue on click, given that an issue
render is about a second. The answer is not speed: a dashboard is a **static, self-contained file**,
so at click time there is no process behind it — JavaScript in the page cannot run Python, reach ISS
or write a file. The one mode with a server (`dashboard.py --watch`) would make the page die with
that process, which is the opposite of what these files are for.

So the member pages are **pre-rendered beside the group page**, which is affordable for a reason
worth remembering: the expensive part of an issue render is **not per-bond**. Profiled cold, 15.4 s
of a 21 s render is the 12 monthly ZCYC slices, and those are cached under GLOBAL keys — by the time
the group page exists they are warm. Measured on Селигдар (14 issues): 17.9 s for the first issue,
then **0.3–1.2 s** each, **24.9 s for the group page and all 14 members together** — about one cold
render, not fourteen. `--no-members` skips it.

**And the way back.** Clicking a row is a real navigation to another file, so a member page
rendered from a group carries a header chip «← <эмитент> · все выпуски» (`spec["links"]`, the same
relative-only ladder). A stand-alone issue render gets none — there would be nothing to return to,
and a chip to a file that was never written is the dead link the ladder exists to prevent.

A link is never allowed to be dead. Clicking runs the engine's existing chip ladder (`a.tlink`,
shared with `a.hdrlink`): HEAD-probe the relative file → navigate if this viewer serves it → else
copy «Покажи дашборд облигации <SECID> (<name>)» and toast it, so the AGENT does the render. Two
consequences for the agent: **present the member files as well as the group page** (a viewer serves
only what was presented, and the probe is what decides), and a link is only ever relative — the
engine refuses a scheme, a `//host` or an absolute path in a row link, because table rows are
producer data.

**The table itself obeys the module's cardinal rule**: a yield appears ONLY with the basis it was
computed to («18.04% к погаш.» / «−28.89% к оферте»), and floaters, structured and fx issues show
«—» with the type column saying why — the v1.150 P0 defect was exactly a bare yield beside a
maturity column, and a per-issuer table re-arms that trap.

**Review round 1 on the group page (7 blocking).** Every finding was the same shape — a number
computed over one population and captioned with another:
- The calendar pulled schedules for the first 12 SECIDs of the **search** (sorted by ISIN, so the
  oldest and often redeemed issues first) and disclosed the count against **board members**, giving
  arithmetically impossible captions like «по 12 выпускам из 7» while aggregating paper the page
  never lists. Membership is now resolved BEFORE the pulls, and the two numbers share a population.
- The board is what MAKES a member, but it was pulled only under `with_market` — so
  `--issuer --no-market` printed «Выпусков 0» beside a calendar covering 2 issues. The board and the
  ZCYC are now always pulled (same day-cache keys); `--no-market` drops the TAB, not the population.
  With no board at all the count reads «н/д · доска биржи не пришла», never a fabricated 0.
- A row's «выпусков» counted cash-flow LEGS, so every amortisation date double-counted.
- Foreign-currency issues were summed into a «₽ на облигацию» total; they are now excluded and the
  exclusion is stated.
- The medians were computed over a looser filter than the curve drawn above them: live ГТЛК printed
  «разброс 7.05–17.81%» under a curve that had thrown that 7.05% print out. Both now call ONE
  predicate, `curve_gate()` — extracted precisely because duplicating it is what let them diverge.
- A foreign-currency yield sat unmarked in the same column as rouble ones (and the panel text
  claimed fx issues have no yield at all); at the pixels the same held for LINKERS, whose published
  yield is a REAL yield. Both are now marked («(USD)», «· реальн.») and neither enters the medians.
- The pre-render had no bound and no give-up rule (up to 100 full dashboards against a degraded
  ISS): capped at `_GROUP_MEMBERS_MAX`, stops after `_GROUP_GIVEUP` consecutive failures, and the
  table says how many pages actually landed.

Also fixed: the issuer-curve panel explained why «this bond» was off the curve on a page that names
no bond, while its own search-truncated disclosure could never fire there; a lossy cache filename
let two issuer names share one file (the second run rendering the FIRST issuer's issues under the
requested name); and a FAILED issuer search was indistinguishable from an issuer with no bonds —
it now exits non-zero instead of publishing «Выпусков 0».
