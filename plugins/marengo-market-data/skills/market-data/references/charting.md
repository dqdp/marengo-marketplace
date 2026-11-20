# Charting Reference (plot a historical series)

Open this when the user wants a CHART / plot / "нарисуй график" of historical
data — candles, line, OHLCV. Charting is PRESENTATION, not a data source.

## Default path — `scripts/chart.py` (one command, no token overhead)

`scripts/chart.py` fetches the series over HTTP, resamples it, and writes an SVG
**entirely inside the script**. The price series NEVER passes through the agent's
context, so a chart costs one command + one summary line instead of dumping and
re-emitting hundreds of bars. Pure stdlib, no pip installs.

**For a chart, never go through the MCP servers.** The script hits the SAME data
endpoints the MCP tools wrap — for `--source databento` it calls
`hist.databento.com` `metadata.get_dataset_range` + `timeseries.get_range`
directly; for `av-*` the Alpha Vantage REST endpoints; for `moex` the ISS API —
but over HTTP with the `.env` key, so a chart costs **zero MCP round-trips and
zero "loading tools" / `ToolSearch` schema fetches**, and no bars enter context.
Building a chart by calling `timeseries_get_range` / `TIME_SERIES_*` through the
MCP and feeding the result into a render is the regression this path exists to
prevent: it pulls the whole series into context AND pays the deferred-tool-schema
load. The frontier/metadata fetch inside the script is itself conditional (only
when `end` exceeds the dataset frontier or for intraday), so a daily in-window
chart is often a single HTTP call. If the `.env` key is missing, fix the key —
do not fall back to the MCP path to draw a chart.

```
<python> "<skill-dir>/scripts/chart.py" <SYMBOL> --source <src> --interval <bar> --days <N> --out <file.svg>
```

**Invoke the BUNDLED script — never reimplement charting.** `scripts/chart.py`
lives in THIS skill's folder (next to `SKILL.md`), which is almost never your
current working directory — in a normal session cwd is the user's project. So a
bare `<python> scripts/chart.py` resolves only when you happen to be in the skill
root and otherwise fails with "no such file". Call it by an ABSOLUTE path built
from the skill directory instead, e.g.
`<python> "<skill-dir>/scripts/chart.py" <SYMBOL> --source …` where `<skill-dir>`
is the folder containing `SKILL.md`. If you can't find it, locate it (`find … -name chart.py`
under the skill install dir) or ask — do NOT write your own plotting script. A
hand-rolled script re-introduces every problem this one solves: the series lands
back in context, and you lose the `.env` auto-resolution, MOEX engine/market
resolution + ISS pagination, the resampling, and the provenance subtitle. The
`.env` lookup is already path-robust (the script searches its own folder, not just
cwd) — the INVOCATION path is the one thing the caller must get right.

| `--source` | For | Needs key | Notes |
|---|---|---|---|
| `databento` | CME futures / options-on-futures | `DATABENTO_API_KEY` | `--stype continuous` for `{root}.v.0`; `--dataset` (default GLBX.MDP3). Fetches `ohlcv-1h` (or `-1d`) and clamps `end` to the dataset frontier. |
| `av-crypto` | crypto (BTC, ETH, …) | `ALPHAVANTAGE_API_KEY` | symbol = the BARE asset + `--market` (`BTC --market USD`, never `BTCUSD`). Fetches `CRYPTO_INTRADAY` 60min. |
| `av-equity` | equities / ETFs | `ALPHAVANTAGE_API_KEY` | `TIME_SERIES_INTRADAY` (realtime, RTH-only) or `_DAILY` for `--interval 1d`. Daily is RAW by default; `--adjusted` → `TIME_SERIES_DAILY_ADJUSTED` (see below). |
| `moex` | Moscow Exchange — indices, shares, bonds, RUB FX, FORTS futures/options (IMOEX, SBER, USD000UTSTOM…) | none (public ISS API) | engine/market auto-resolved per security; `--moex-market index\|shares\|bonds\|currency\|futures\|options` to force one. `--interval 1d/1h`; volume = turnover. Full detail in `references/moex.md`. |

`--interval` accepts `1min`/`5min`/`15min`/`30min`, `1h`/`2h`/`4h`, `1d`, `1w`,
`1mo` (MOEX 1-min archive goes back to ~2011; sub-hour resamples from the finest
ISS base that divides the target). `--days` is fractional (e.g. `0.1` ≈ a few
hours) for short intraday windows. The script resamples the base bars
(open=first, high=max, low=min, close=last, volume=sum), UTC-aligned
(MOEX uses native ISS weekly/monthly candles). Long histories page fully (MOEX ISS
is paginated at ~500 bars/call — the script follows the cursor, so e.g. 20y of
daily or full monthly history comes back complete, not truncated).
`--days` sets the lookback window. `--theme light|dark`. `--open` opens the SVG in
the default browser (cross-platform). Output is candles + volume + SMA-20, with a
right-side price axis, a signed period % change in the header, and a last-price
guide line.

### Per-source gotchas (symbol format · bar base · adjustment)

- **av-equity DAILY is RAW (unadjusted) by default** — a split shows as a FALSE
  price cliff (NVDA's 10:1 in June 2024 looks like a −90% day). `--adjusted` pulls
  `TIME_SERIES_DAILY_ADJUSTED` and rescales the WHOLE bar (O/H/L/C by
  adjusted_close/close), so multi-year charts across a split stay continuous; it may
  need a premium key, which is why raw stays the default. The subtitle names the
  series kind either way — `· raw (unadjusted)` / `· adjusted` — so the cliff is
  attributable on the chart itself.
- **Crypto symbol = the BARE asset + `--market`**: `BTC --market USD`, never
  `BTCUSD` (not an AV crypto symbol).
- **Sub-hour bars:** the AV intraday base bar is **60min** for BOTH av-equity and
  av-crypto, and the databento intraday base is `ohlcv-1h` — a sub-hour `--interval`
  cannot subdivide those and comes back as 60-min buckets. Only **MOEX** goes finer
  (resamples from the 10/1-min ISS bases).
- **FX has NO chart path in this skill** — chart.py has no `av-fx` source. Non-RUB
  FX quotes/series go through the AV MCP `FX_*` functions directly; RUB crosses
  chart via `--source moex` (`USD000UTSTOM`…).
- **Wrong-source flags WARN; bad values die at parse.** A provider-specific flag on
  the wrong `--source` now prints a stderr warning instead of being silently
  swallowed (`--market` w/o av-crypto, `--dataset`/`--stype` w/o databento,
  `--moex-market` w/o moex, `--adjusted` off av-equity daily). A malformed
  `--interval`/`--days` dies AT ARGPARSE — "bad interval 'banana' (use e.g. 1h, 4h,
  1d, 1w, 1mo)" / "--days must be positive" — never as a mid-run traceback.

## Egress-blocked sandbox — the `--from` fallback (do NOT hand-roll a renderer)

Some sandboxes (e.g. agent-mode / "Home") block direct HTTP egress to the data
hosts while still allowing the MCP connectors to return data. There the normal
fetch (`--source av-equity` etc.) fails with a network error, and the ONLY way to
get the series is through the MCP. When that happens:

1. Pull the series via the MCP connector (yes, it enters context — unavoidable when
   egress is blocked; that's the sandbox's limitation, not a free choice).
2. Save it to a local file — CSV with header `timestamp,open,high,low,close[,volume]`
   (header order/casing flexible, volume optional), or JSON (list of objects, or
   `[ts,o,h,l,c,v]` rows, or `{"bars":[…]}`). Prices must be real numbers, not
   Databento 1e9-scaled ints.
3. Render with the SAME bundled script:
   `<python> "<skill-dir>/scripts/chart.py" <SYM> --from series.csv --interval 1d --source-label "Alpha Vantage equity (via MCP)" --out chart.svg --png`

`--from` implies `--source file`; `--source-label` stamps honest provenance into the
subtitle (say "via MCP" so the chart isn't mistaken for a direct pull). The file is
treated as the intended window (all rows kept, only resampled to `--interval`) unless
you pass `--start`/`--days`. **Do NOT write your own plotting script and do NOT
`import chart` to poke its internals** — `--from` is the supported entry point; it
reuses the renderer, resampling, axis/SMA/provenance, and avoids hard-coding output
paths. The real fix is still to OPEN egress to the data host (`alphavantage.co`,
`hist.databento.com`, `iss.moex.com`) so the script fetches directly and no series
enters context; `--from` is the graceful fallback for when you can't.

## Portability

Pure Python stdlib — runs on any machine with a Python 3 interpreter, no
`pip install`. The script and the SVG it writes are fully cross-platform; an SVG
opens in any browser on macOS / Windows / Linux. Caveats:
- Invocation is `python3` here; on Windows use `python -X utf8` or `py -3 -X utf8`.
- Inline PNG (`--png`) auto-selects a converter: `rsvg-convert` (any OS) →
  `qlmanage`+`sips` (macOS) → headless Chrome/Edge. On Windows/Linux install
  `rsvg-convert` for Mac-speed; otherwise just open the `.svg` (`--open`). The
  chart generation itself needs none of these.
- Heavy plotting libs (matplotlib/mplfinance) are intentionally NOT used: they'd
  require installs and break the zero-dependency, runs-anywhere property. The
  stdlib SVG is the more portable choice; a matplotlib backend would only help by
  emitting a PNG directly (useful off macOS) and could be added as an optional
  `--backend` later if needed.

Keys live in a gitignored `.env` at the project root (or the environment) — see
`.env.example`. Never pass a key on the command line or in chat.

Examples:
```
<python> "<skill-dir>/scripts/chart.py" BTC    --source av-crypto  --interval 4h --days 14 --out btc.svg
<python> "<skill-dir>/scripts/chart.py" AAPL   --source av-equity  --interval 1d --days 90 --out aapl.svg
<python> "<skill-dir>/scripts/chart.py" ES.v.0 --source databento  --interval 4h --days 14 --stype continuous --out es.svg
```

### Showing the chart
The script writes an `.svg` (760x440 viewBox). The display rule is the canonical one in
`references/fundamentals.md` ("Showing it to the user") — same single default:
- **DEFAULT → text takeaway + the chart in the Preview Pane.** When the user wants to SEE a
  chart, serve it in the **Claude Code Preview Pane**: wrap the SVG in `.preview/index.html`
  (`<!doctype html><body style="margin:0">…svg…</body>`), point `.claude/launch.json` at a
  static server, `preview_start`, then confirm it landed by data — an SVG is DOM, `read_page`
  sees it; screenshot only in dev sessions or on anomaly (the fundamentals.md calibrated rule).
  In-app and sandbox-safe (no Apple Events). Always state source/symbol/window/freshness in
  TEXT beside it.
- **`--open` (external browser) → EXPLICIT-ONLY** ("открой в браузере"). Local-machine only;
  blocked under the sandbox (Apple Events / -600) so run that command unsandboxed; useless in
  agent-mode/Home (browser opens inside the container). `pathlib.as_uri()` keeps the URL valid
  on Windows.
- **`--png` + `Read` → agent-side verify only** (the user's client shows a file link, not the
  image). Install `rsvg-convert` (fast, cross-platform, sandbox-safe; avoids the `qlmanage` hang).

Do NOT paste the SVG/series back into a widget call, and do NOT re-emit a rendered
SVG through the visualization widget — it streams token-by-token for minutes (the
exact overhead this path exists to avoid). If an inline in-chat visual is needed,
build a SMALL dedicated widget, not the full rendered SVG.

For a RICH, resizable dashboard that must fit an inline widget / expandable artifact /
the Claude Code Preview Pane, the companion `scripts/fundamentals.py` has a `--html`
mode: a self-contained responsive Chart.js page (Chart.js inlined — no CDN, so it renders
in egress-blocked/in-app viewers) whose grid reflows (one column when
narrow, two columns from 680px up — capped at two) instead of going tiny like a
fixed-aspect SVG. The
full display recipe — including the `.preview/` + `.claude/launch.json` + `preview_start`
Preview-Pane path — is in `references/fundamentals.md` ("Showing it to the user").

### Reporting
The script's subtitle already embeds provenance (source · base→interval · bar
count · window · UTC). Still state the takeaway and freshness in TEXT — a chart
carries no `dataAge`, and intraday crypto/equity is a sweep of the period, not a
single live tick (cross-check with a live quote if "now" matters).

## Fallback — host-rendered widget (only when interactivity is explicitly needed)

If the user specifically wants an interactive in-chat widget (hover, zoom) and the
series is small, an inline HTML+SVG candlestick can be rendered by the host
(Claude Code visualization / Claude Desktop Artifacts). This embeds the data as
tokens, so reserve it for small series and explicit interactivity requests —
otherwise use `scripts/chart.py`.

## Offline test
`<python> scripts/test_chart.py` checks interval parsing, resampling, and SVG
rendering with no network (stdlib only) — wire it into CI alongside greeks.
