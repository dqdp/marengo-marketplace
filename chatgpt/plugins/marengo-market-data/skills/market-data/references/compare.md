# Compare Reference (2-8 companies OR 2-8 funds side by side)

Open this when the user asks to **compare companies** — "сравни AAPL и MSFT", "X vs Y",
"который дешевле / кто растёт быстрее / чьи маржи лучше" — or to **compare ETFs/funds**
(v1.101.0: "сравни MSOS и MJ"). An ALL-fund set routes automatically to the ETF comparison:
expense / AUM / liquidity / yield (profile + computed TTM) / drawdown / returns / store-vol
(IV·rank·VRP, zero calls) best-in-row table, rebased-100 performance, and — for a PAIR — the
**holdings overlap** (normalized names: swap wrappers match their share twins; the info
discloses how much of each fund the list covers at all — a swap-based fund under-discloses,
so true overlap can only be HIGHER; cash/treasury sleeves never count). A MIXED company+fund
set is refused loud (no shared metric); `--deep` is company-only (warned, ignored for funds).
Peers are **USER-named**: when asked "compare NVDA with its peers", PROPOSE candidates in
chat and confirm — never auto-pick.

**Fund FLOWS / AUM-over-time (creations/redemptions)** are NOT in any configured provider
(AV has one current net_assets scalar; no shares-outstanding history; investigated
2026-07-15) — never fake AUM(t) from price (it strips exactly the flows signal). For
CRYPTO ETFs the public source is farside.co.uk/btc/ and /eth/ (daily per-fund US$m flows +
cumulative) — it sits behind a Cloudflare JS challenge, so curl/WebFetch get 403: open it
in the BUILT-IN BROWSER pane and read the table (works; probed 2026-07-15). Answer in chat;
no dashboard panel is possible. Cowork agents have no browser — say so there. Non-crypto
ETF flows have no free scriptable source (issuer pages / paywalled aggregators).

## Use the script — `scripts/compare.py`

```
<python> "<skill-dir>/scripts/compare.py" AAPL MSFT NVDA              # LIGHT (default): text
<python> "<skill-dir>/scripts/compare.py" AAPL MSFT --html cmp.html   # the dashboard
<python> "<skill-dir>/scripts/compare.py" AAPL MSFT NVDA --deep       # + statement rows (~13 calls/name)
<python> "<skill-dir>/scripts/compare.py" AAPL MSFT --cache ~/.avcache  # cached names join OFFLINE (deep columns)
```
Invoke by the **absolute skill path**. 2-8 distinct symbols, positional (2-4 is the sweet spot for a READ; 8 = the palette ceiling — a 9th chart series would recycle colours).

## Tiers & cost

- **LIGHT (default)** — K × `OVERVIEW` + 1 × `REALTIME_BULK_QUOTES` (one call covers every
  live price) + K × `TIME_SERIES_MONTHLY_ADJUSTED` ≈ **2K+1 calls** (K=4 → 9). Everything on
  the page is an OVERVIEW ratio (AV-computed TTM — the basis is disclosed; the MU lesson:
  those fields have been caught wrong, `--deep` recomputes from quarterlies).
- **`--deep`** — the full `fundamentals.gather()` per name (staggered): adds FCF yield /
  FCF conversion / dilution %/yr / net cash / F-Score / Altman Z on the computed basis.
- **`--cache DIR`** — the fundamentals `--save-raw` layout (`DIR/<SYM>/overview.json`…): a
  ticker analyzed earlier joins **offline with deep columns, 0 calls**; with `--deep`,
  fetched names are saved back. Tiers can MIX (cached-deep beside light) — deep rows just
  read n/a for light columns.

## The page

Spread cards (Cheapest fwd P/E · Fastest grower · Best net margin · PEG spread) →
**metrics table** (▲ = best in row) → **rebased-100 performance** (monthly adjusted closes,
each series to its own first value — currency/unit-free, dividends included) → **grouped
margins** → **growth-vs-multiple bubble** (x = rev YoY, y = fwd P/E, size ∝ cap; down-right
is cheap-for-its-growth). Text mode = the monospace transpose with ◀ winners.

## Honesty rules (baked in — never soften them in prose)

- **▲/◀ mark DIRECTIONAL rows only** (multiples lower-better; margins/growth/quality
  higher-better). Div yield, beta, 52w position, cap carry no mark — they're taste/context,
  not better/worse. **n/a never wins a row**; an all-n/a row is dropped.
- **Currencies**: ratios (%, ×) compare across currencies; **$-level rows** (market cap,
  net cash) rank only when every column shares one currency — else the row is unranked and
  each cell is currency-labeled.
- **As-of**: fiscal calendars differ — every column is stamped with ITS latest quarter, and
  the subtitle says **NOT simultaneous** when they diverge. Never present columns as one
  point in time.
- Banks/financials: gross margin and Altman are structurally n/a — the honest hole, not a
  defect.

## The comparative read (REQUIRED — extends the written-read mandate)

After the table/dashboard, 3-6 sentences in the user's language:
- **Cheaper for its growth** — read the BUBBLE (multiple vs growth), never the raw P/E row
  alone ("MSFT is the cheapest multiple, but NVDA's PEG 0.65 is the cheapest growth").
- **Margin level vs direction** — the table shows LEVEL; direction needs each company's own
  dashboard — say so rather than inferring trends from one snapshot.
- **Quality gap** (deep) — FCF conversion / F-Score / dilution spread: who converts profits
  to cash, who dilutes.
- **The decisive divergence** — the ONE row where the set genuinely splits (the compare
  analogue of "the flag").
- **What would flip the ranking** — the metric/event that reorders the set.
Anti-pattern: reading the table aloud row by row.

**Then END with the deeper-document OFFER** exactly like the company read (SKILL.md
"Neighbor plugins"): a COMPANY comparison maps to `/financial-analysis:comps` (the live
Excel comps model over this same peer set) — or `:competitive-analysis` (the landscape
deck) when the question is strategic positioning rather than valuation; name ONE by
context. On desktop/Cowork render the one-click card via
`<skill-dir>/scripts/offer.py` (check THIS session's own skill list first per SKILL.md),
PLUS the plain-text line naming the command. A comparative read that closes text-only,
card-less, is incomplete — the V-vs-MA session miss (2026-07-23): no offer at all.
An ETF comparison gets NO document offer (no fund-comparison document exists in the
bundles) — the one exception: a clearly THEMATIC fund set (both sides one sector) may
offer `/equity-research:sector` on the theme.

## Quirks

- A symbol missing from the bulk-quote response (halted/odd listing) → price/upside/52w-pos
  n/a, the rest of the column stands.
- A late listing rebases from ITS first month inside the window (says nothing about the
  earlier period — the line simply starts later).
- `REALTIME_BULK_QUOTES` accepts ≤50 symbols; we use one call for all 2-8.
- The comparison never fetches news (no sentiment column by design — headline moods don't
  compare honestly across coverage densities).
