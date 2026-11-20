#!/usr/bin/env python3
"""
portfolio.py — fetch daily series for a basket of holdings and analyze its
risk / return, end to end. Pure stdlib (urllib), no pip installs.

The point (same as chart.py / fundamentals.py): the daily price series for every
holding are fetched DIRECTLY over HTTP, concurrently, and reduced to a compact set
of risk/return numbers INSIDE this script — so a portfolio analysis costs one
command and a small summary instead of dumping N full price histories into context.

Returns are computed from SPLIT/DIVIDEND-ADJUSTED close (TIME_SERIES_DAILY_ADJUSTED),
so a split inside the window doesn't wreck the math. Weights are buy-and-hold: the
share counts are held fixed and the weights drift, which is how a real holding behaves.

Input (one of):
  --portfolio FILE   CSV with a ticker column + ONE of shares|weight|value
                     (auto-detected from the header, else from the magnitudes).
  --holdings "NVDA:100,AAPL:50,MSFT:40"   inline; ':' value is shares by default
                     (use --as weight|value to read them as weights / dollar values).

Outputs (pick with flags):
  (default)   text summary: portfolio stats, per-holding table, benchmark, correlations.
  --json      the same as compact JSON (feed a widget).
  --svg PATH  a self-contained landscape SVG dashboard (cumulative vs benchmark,
              correlation heatmap, risk-return scatter, weights donut + risk contribution).
  --html PATH a self-contained RESPONSIVE Chart.js dashboard (reflows 1 col -> 2 cols).

Key is read from .env (skill folder or cwd) as ALPHAVANTAGE_API_KEY, or the env.
Never pass the key on the command line or paste it into chat. The holdings file is
personal — keep it out of git (the skill's .gitignore already excludes *holdings*).
"""
from __future__ import annotations
import argparse, concurrent.futures, csv, datetime as dt, io, json, math, os, pathlib, re, sys, urllib.parse, urllib.request, webbrowser

import dashboard  # Ф7-2: the one renderer

# palette shared with fundamentals.py
TEAL, DK, LT, BLUE, AMBER, RED, GRA, INK = "#1D9E75", "#0F6E56", "#9FE1CB", "#378ADD", "#EFB366", "#E24B4A", "#8a8f98", "#1f2328"
TRADING_DAYS = 252


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def get_key(env_file=None):
    if os.environ.get("ALPHAVANTAGE_API_KEY"):
        return os.environ["ALPHAVANTAGE_API_KEY"]
    here = os.path.dirname(os.path.abspath(__file__))
    for path in ([env_file] if env_file else []) + [".env", os.path.join(here, ".env"), os.path.join(here, os.pardir, ".env")]:
        if path and os.path.exists(path):
            for ln in open(path, encoding="utf-8"):
                m = re.match(r'\s*ALPHAVANTAGE_API_KEY\s*=\s*(.+)', ln)
                if m:
                    return m.group(1).strip().strip('"').strip("'")
    sys.exit("error: ALPHAVANTAGE_API_KEY not found (set env or a .env beside SKILL.md)")


def av(params, key):
    url = "https://www.alphavantage.co/query?" + urllib.parse.urlencode(dict(params, apikey=key))
    with urllib.request.urlopen(url, timeout=60) as r:
        d = json.loads(r.read())
    if isinstance(d, dict) and ("Note" in d or "Information" in d) and len(d) == 1:
        # throttle != bad ticker: name the limits + this script's fan-out so the fix is obvious
        sys.exit("error: Alpha Vantage throttled (free tier = 25 req/day · 5/min; portfolio fans out one "
                 "adjusted-series call per holding CONCURRENTLY — premium recommended, or space the runs): "
                 + str(d)[:120])
    return d


# ----------------- holdings parsing -----------------
def _kind_from_header(cols):
    # check most-specific first: weight, then value, then shares — so a "Position Value"
    # column resolves to value (not shares via the "position" token).
    low = [c.lower() for c in cols]
    for i, c in enumerate(low):
        if any(w in c for w in ("weight", "pct", "percent", "%", "alloc")):
            return i, "weight"
    for i, c in enumerate(low):
        if any(w in c for w in ("value", "amount", "usd", "$", "mktval", "market", "cost", "basis", "notional")):
            return i, "value"
    for i, c in enumerate(low):
        if any(w in c for w in ("share", "qty", "quantity", "unit", "position")):
            return i, "shares"
    return None, None


def _is_num(x):
    try:
        float(str(x).strip().replace(",", "").replace("$", "").replace("%", ""))
        return True
    except ValueError:
        return False


def _ticker_col(cols):
    low = [c.lower() for c in cols]
    for i, c in enumerate(low):
        if any(w in c for w in ("ticker", "symbol", "sym")):
            return i
    return 0  # default: first column


def parse_holdings_csv(path):
    """Return (entries, kind) where entries = [(ticker, number), ...]. The number's
    meaning (shares|weight|value) is auto-detected from the header, else from the
    magnitudes (fractional values summing to ~1 -> weights; otherwise shares)."""
    rows = list(csv.reader(open(path, encoding="utf-8-sig")))
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        sys.exit(f"error: {path} is empty")
    first = rows[0]
    header_kw = any(w in c.lower() for c in first for w in
                    ("ticker", "symbol", "share", "weight", "value", "qty", "quantity",
                     "amount", "price", "cost", "basis", "alloc", "%", "units", "position"))
    # A DATA row's quantity cell parses as a number; a header's does not. Treat row 1 as a
    # header ONLY if it has a header keyword OR its value cell isn't numeric — so a long /
    # foreign ticker in row 1 (2222.SR, ABCDEFG, a ticker with a digit) isn't mistaken for a
    # header and silently dropped.
    row1_numeric = len(first) >= 2 and _is_num(first[1])
    has_header = header_kw or not row1_numeric
    if has_header:
        tcol = _ticker_col(first)
        vcol, kind = _kind_from_header(first)
        body = rows[1:]
    else:
        tcol, vcol, kind, body = 0, 1, None, rows
    entries = []
    eff_vcol = vcol if vcol is not None else 1   # v1.126: the guard must use the column actually
    for r in body:                               # READ below — a 1-cell row passed max(tcol, 0)
        if len(r) <= max(tcol, eff_vcol):        # and then crashed on r[1] (IndexError, not the
            continue                             # graceful "no rows parsed" exit)
        tk = r[tcol].strip().upper()
        if not tk:
            continue
        raw = r[vcol if vcol is not None else 1].strip().replace(",", "").replace("$", "").replace("%", "")
        try:
            entries.append((tk, float(raw)))
        except ValueError:
            continue
    if not entries:
        sys.exit(f"error: no (ticker, number) rows parsed from {path}")
    if kind is None:  # magnitude heuristic
        tot = sum(v for _, v in entries)
        # only FRACTIONAL values summing to ~1 are unambiguously weights; an integer basket
        # (e.g. "60,40" or two 50-share lots summing to 100) is read as SHARES — for percent
        # weights, use a `weight`/`%` header or --as weight.
        fractional = any(not float(v).is_integer() for _, v in entries)
        kind = "weight" if (fractional and 0.95 <= tot <= 1.05) else "shares"
    return entries, kind


def parse_holdings_inline(s, kind):
    entries = []
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            sys.exit(f"error: bad --holdings item '{part}' (use TICKER:NUMBER)")
        tk, raw = part.split(":", 1)
        try:
            val = float(raw.strip().replace("$", "").replace("%", ""))
        except ValueError:
            sys.exit(f"error: bad --holdings value in '{part}' (use TICKER:NUMBER, e.g. NVDA:100)")
        entries.append((tk.strip().upper(), val))
    return entries, kind


# ----------------- fetch -----------------
def _parse_av_daily(d):
    ts = d.get("Time Series (Daily)") or {}
    out = {}
    for date, row in ts.items():
        v = row.get("5. adjusted close") or row.get("4. close")
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if fv > 0:  # drop zero/negative prints — they'd blow up the return ratios in _stats
            out[date] = fv
    if not out:
        return None
    return out


def fetch_series(symbol, key):
    d = av({"function": "TIME_SERIES_DAILY_ADJUSTED", "symbol": symbol, "outputsize": "full", "entitlement": "realtime"}, key)
    s = _parse_av_daily(d)
    if not s:
        raise SystemExit(f"error: no daily series for {symbol} (bad ticker / no entitlement?): {str(d)[:140]}")
    return s


def fetch_riskfree(key):
    try:
        d = av({"function": "TREASURY_YIELD", "interval": "daily", "maturity": "3month"}, key)
        for row in d.get("data", []):
            if row.get("value") not in (None, "", ".", "None"):
                return float(row["value"]) / 100.0
    except Exception:
        pass
    return None


def gather(entries, kind, benchmark, key, days):
    dashboard.check_no_net("Route: pull each ticker's TIME_SERIES_DAILY_ADJUSTED via the Alpha "
                           "Vantage MCP connector, save one file per ticker, render with --from-dir DIR.")
    syms = [t for t, _ in entries]
    allsyms = syms + ([benchmark] if benchmark else [])
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(allsyms) + 1)) as ex:
        futs = {s: ex.submit(fetch_series, s, key) for s in allsyms}
        rf_fut = ex.submit(fetch_riskfree, key)
        series = {s: futs[s].result() for s in syms}
        bench = futs[benchmark].result() if benchmark else None
        rf = rf_fut.result()
    return build(entries, kind, series, benchmark, bench, rf, days)


def read_from_dir(entries, benchmark, d, kind=None):
    """MCP-fallback for egress-blocked sandboxes (the chart.py --from / fundamentals
    --from-dir analogue). Each ticker's TIME_SERIES_DAILY_ADJUSTED result is found by a
    substring match on the filename (so the harness's saved tool-result files drop in
    as-is — name them so each contains the ticker). A file containing 'treasury'/'yield'
    supplies the risk-free rate (optional)."""
    import glob
    files = [fp for fp in glob.glob(os.path.join(d, "*")) if os.path.isfile(fp)]

    def load(fp):
        try:
            return json.load(open(fp, encoding="utf-8"))
        except (ValueError, OSError):
            return None

    def find_series(sym):
        # match the ticker as a DELIMITED token (bounded by start/end or a non-alphanumeric),
        # not a bare substring — else a short ticker (C/F/T) matches almost any filename and
        # the benchmark can grab a holding's file. e.g. "C" matches C_daily.json, not BTC.json.
        pat = re.compile(r'(?<![a-z0-9])' + re.escape(sym.lower()) + r'(?![a-z0-9])')
        cands = [fp for fp in sorted(files) if pat.search(os.path.basename(fp).lower())]
        # prefer files that also look like a daily-series payload
        cands.sort(key=lambda fp: 0 if any(w in os.path.basename(fp).lower() for w in ("daily", "adjust", "series")) else 1)
        for fp in cands:
            obj = load(fp)
            s = _parse_av_daily(obj) if isinstance(obj, dict) else None
            if s:
                return s
        return None

    series = {}
    for tk, _ in entries:
        s = find_series(tk)
        if not s:
            sys.exit(f"--from-dir {d}: no readable daily-adjusted series file for {tk} "
                     f"(save its TIME_SERIES_DAILY_ADJUSTED result to a file whose name contains '{tk}')")
        series[tk] = s
    bench = find_series(benchmark) if benchmark else None
    if benchmark and bench is None:   # v1.126: was a SILENT drop (holdings exit loud, the
        print(f"warning: --from-dir {d}: no readable series file for benchmark {benchmark} — "
              f"beta/vs-benchmark lines omitted", file=sys.stderr)   # benchmark just vanished)
    rf = None
    for fp in files:
        if any(w in os.path.basename(fp).lower() for w in ("treasury", "yield", "riskfree")):
            obj = load(fp)
            for row in (obj or {}).get("data", []) if isinstance(obj, dict) else []:
                if row.get("value") not in (None, "", ".", "None"):
                    rf = float(row["value"]) / 100.0
                    break
            if rf is not None:
                break
    return build(entries, kind, series, benchmark, bench, rf, None)


# ----------------- compute -----------------
def _stats(prices):
    """ann return (geometric), ann vol, Sharpe-ready daily returns, max drawdown."""
    rets = [prices[i] / prices[i - 1] - 1 for i in range(1, len(prices))]
    n = len(rets)
    if n == 0:
        return {"rets": [], "annRet": None, "annVol": None, "maxDD": None}
    total = prices[-1] / prices[0]
    annRet = total ** (TRADING_DAYS / n) - 1
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / (n - 1) if n > 1 else 0.0
    annVol = math.sqrt(var) * math.sqrt(TRADING_DAYS)
    peak = prices[0]; mdd = 0.0
    for p in prices:
        peak = max(peak, p)
        mdd = min(mdd, p / peak - 1)
    return {"rets": rets, "annRet": annRet, "annVol": annVol, "maxDD": mdd}


def build(entries, kind, series, bench_sym, bench_series, rf, days):
    """Compute the dashboard dict from raw daily series — shared by the HTTP path
    (gather) and the MCP/file path (read_from_dir)."""
    # window: keep the last ~`days` calendar days; align all holdings to common dates.
    tickers = [t for t, _ in entries]
    cutoff = None
    if days:
        cutoff = (dt.date.today() - dt.timedelta(days=int(days))).isoformat()
    common = None
    for t in tickers:
        ds = {d for d in series[t] if (cutoff is None or d >= cutoff)}
        common = ds if common is None else (common & ds)
    if bench_series is not None:
        bd = {d for d in bench_series if (cutoff is None or d >= cutoff)}
        common = bd if common is None else (common & bd)
    if not common or len(common) < 3:
        sys.exit("error: holdings have <3 overlapping trading days in the window (check tickers / --days)")
    dates = sorted(common)

    px = {t: [series[t][d] for d in dates] for t in tickers}  # aligned adjusted prices
    last_px = {t: px[t][-1] for t in tickers}

    # normalize the input quantity to SHARES (held fixed -> buy-and-hold, weights drift)
    qty = dict(entries)
    _neg = [t for t, v in qty.items() if v is None or v <= 0]
    if _neg:   # v1.126: a short/zero position used to crash render_svg (math.sqrt on a negative
        sys.exit("error: non-positive quantity for %s — shorts/zero positions are not supported "
                 "(shares/weights/values must be > 0)" % ", ".join(_neg))   # weight) — refuse loud
    if kind == "weight":
        tot = sum(qty.values()) or 1.0
        NOTIONAL = 100000.0
        shares = {t: (qty[t] / tot) * NOTIONAL / last_px[t] for t in tickers}
    elif kind == "value":
        shares = {t: qty[t] / last_px[t] for t in tickers}
    else:  # shares
        shares = {t: qty[t] for t in tickers}

    # portfolio value series (drifting weights are implicit in fixed shares)
    V = [sum(shares[t] * px[t][i] for t in tickers) for i in range(len(dates))]
    total_value = V[-1]
    if total_value <= 0:
        sys.exit("error: portfolio total value is zero/negative — check the quantities (weights/values must be positive)")
    weights = {t: shares[t] * last_px[t] / total_value for t in tickers}

    per = {t: _stats(px[t]) for t in tickers}
    pstat = _stats(V)
    bench = None
    if bench_series is not None:
        bpx = [bench_series[d] for d in dates]
        bstat = _stats(bpx)
        bench = {"symbol": bench_sym, "annRet": bstat["annRet"], "annVol": bstat["annVol"],
                 "maxDD": bstat["maxDD"], "cum": [bpx[i] / bpx[0] - 1 for i in range(len(bpx))]}

    rf_used = rf if rf is not None else 0.0
    def sharpe(s):
        return None if not s["annVol"] else (s["annRet"] - rf_used) / s["annVol"]

    # correlation matrix (daily returns)
    rets = {t: per[t]["rets"] for t in tickers}
    m = len(dates) - 1
    def corr(a, b):
        if m < 2:
            return 0.0
        ma, mb = sum(a) / m, sum(b) / m
        va = sum((x - ma) ** 2 for x in a); vb = sum((x - mb) ** 2 for x in b)
        cov = sum((a[i] - ma) * (b[i] - mb) for i in range(m))
        return cov / math.sqrt(va * vb) if va > 0 and vb > 0 else 0.0
    # diagonal is 1 by definition (a constant/halted series has zero variance -> corr() would
    # return 0, which would wrongly blank the heatmap diagonal)
    cmat = [[1.0 if i == j else round(corr(rets[tickers[i]], rets[tickers[j]]), 3)
             for j in range(len(tickers))] for i in range(len(tickers))]

    # risk contribution: %RC_i = w_i * (Σ w)_i / σ_p²  (Σ = annualized covariance). Note this
    # is a point-in-time decomposition on the CURRENT (drifted) weights, so its implied vol
    # sqrt(var_p) differs from the headline annVol (which is _stats on the buy-and-hold value
    # path); the %RC still sums to 1 and ranks the risk drivers correctly.
    means = {t: (sum(rets[t]) / m if m else 0.0) for t in tickers}
    def cov(ti, tj):
        if m < 2:
            return 0.0
        a, b = rets[ti], rets[tj]; ma, mb = means[ti], means[tj]
        return sum((a[i] - ma) * (b[i] - mb) for i in range(m)) / (m - 1) * TRADING_DAYS
    w = weights
    Sw = {ti: sum(cov(ti, tj) * w[tj] for tj in tickers) for ti in tickers}
    var_p = sum(w[ti] * Sw[ti] for ti in tickers)
    riskContrib = {t: (w[t] * Sw[t] / var_p if var_p > 0 else w[t]) for t in tickers}

    # portfolio beta + correlation vs benchmark
    beta = None
    corr_bench = None
    if bench is not None and m >= 2:
        bpx = [bench_series[d] for d in dates]
        br = [bpx[i] / bpx[i - 1] - 1 for i in range(1, len(bpx))]
        pr = pstat["rets"]
        mbr = sum(br) / m; mpr = sum(pr) / m
        vbr = sum((x - mbr) ** 2 for x in br)
        covpb = sum((pr[i] - mpr) * (br[i] - mbr) for i in range(m))
        beta = covpb / vbr if vbr > 0 else None
        corr_bench = round(corr(pr, br), 3)

    # Sortino (downside-only vol) + Calmar (return per unit of max drawdown)
    _neg = [min(r, 0.0) for r in pstat["rets"]]
    _dvol = (math.sqrt(sum(r * r for r in _neg) / len(_neg)) * math.sqrt(TRADING_DAYS)) if _neg else 0.0
    sortino = ((pstat["annRet"] - rf_used) / _dvol) if (_dvol > 0 and pstat["annRet"] is not None) else None
    calmar = (pstat["annRet"] / abs(pstat["maxDD"])) if (pstat["annRet"] is not None
                                                        and pstat["maxDD"] not in (None, 0)) else None

    holdings = [{
        "ticker": t, "shares": round(shares[t], 4), "weight": weights[t],
        "lastPx": last_px[t], "value": shares[t] * last_px[t],
        "annRet": per[t]["annRet"], "annVol": per[t]["annVol"],
        "sharpe": sharpe(per[t]), "maxDD": per[t]["maxDD"], "riskContrib": riskContrib[t],
    } for t in tickers]
    holdings.sort(key=lambda h: h["weight"], reverse=True)
    order = [h["ticker"] for h in holdings]
    idx = {t: i for i, t in enumerate(tickers)}
    cmat_sorted = [[cmat[idx[a]][idx[b]] for b in order] for a in order]

    # downsample the cumulative lines to ~120 points for a light payload
    cum_p = [V[i] / V[0] - 1 for i in range(len(dates))]
    step = max(1, len(dates) // 120)
    keep = set(range(0, len(dates), step))
    keep.add(len(dates) - 1)                     # always the last point
    # materialize dates/port/bench AFTER keep is finalized (below) so all series share indices
    peak = V[0]
    dd_full = []
    for v in V:                                  # underwater curve off the SAME value path
        peak = max(peak, v)
        dd_full.append(v / peak - 1)
    if dd_full:
        keep.add(dd_full.index(min(dd_full)))    # never downsample away the trough (it IS the maxDD card)
    _keep = sorted(keep)
    spark = {"dates": [dates[i] for i in _keep], "port": [round(cum_p[i], 4) for i in _keep],
             "dd": [round(dd_full[i], 4) for i in _keep] if dd_full else []}
    if bench is not None:
        spark["bench"] = [round(bench["cum"][i], 4) for i in _keep]

    return {
        "asOf": dates[-1], "start": dates[0], "nDays": len(dates), "nHoldings": len(tickers),
        "totalValue": total_value, "currency": "USD",
        "annRet": pstat["annRet"], "annVol": pstat["annVol"], "sharpe": sharpe(pstat),
        "maxDD": pstat["maxDD"], "beta": beta, "riskFree": rf_used, "riskFreeKnown": rf is not None,
        "sortino": sortino, "calmar": calmar, "corrBench": corr_bench,
        "benchmark": bench, "holdings": holdings, "order": order, "corr": cmat_sorted,
        "spark": spark,
    }


# ----------------- formatting -----------------
def pct(x, d=1):
    return "n/a" if x is None else f"{x*100:+.{d}f}%"


def pctu(x, d=1):  # unsigned percent
    return "n/a" if x is None else f"{x*100:.{d}f}%"


def num(x, d=2):
    return "n/a" if x is None else f"{x:,.{d}f}"


def render_text(D):
    L = []
    L.append(f"PORTFOLIO · {D['nHoldings']} holdings · ${num(D['totalValue'],0)} · {D['start']} -> {D['asOf']} ({D['nDays']} trading days)")
    rf = f"{D['riskFree']*100:.2f}%" + ("" if D["riskFreeKnown"] else " (assumed 0, treasury n/a)")
    L.append(f"return {pct(D['annRet'])} ann  ·  vol {pctu(D['annVol'])} ann  ·  Sharpe {num(D['sharpe'],2)}  ·  maxDD {pct(D['maxDD'])}  ·  rf {rf}")
    if D["benchmark"]:
        b = D["benchmark"]
        L.append(f"vs {b['symbol']}: {pct(b['annRet'])} ann / vol {pctu(b['annVol'])} / maxDD {pct(b['maxDD'])}   ·   portfolio beta {num(D['beta'],2)}")
    L.append("")
    L.append(f"{'TICKER':<8}{'WEIGHT':>8}{'VALUE':>13}{'RET':>9}{'VOL':>8}{'SHARPE':>8}{'MAXDD':>8}{'RISK%':>7}")
    for h in D["holdings"]:
        L.append(f"{h['ticker']:<8}{pctu(h['weight'],1):>8}{('$'+num(h['value'],0)):>13}{pct(h['annRet'],1):>9}"
                 f"{pctu(h['annVol'],1):>8}{num(h['sharpe'],2):>8}{pct(h['maxDD'],1):>8}{pctu(h['riskContrib'],1):>7}")
    L.append("")
    # correlation highlights
    order = D["order"]; cm = D["corr"]
    pairs = [(order[i], order[j], cm[i][j]) for i in range(len(order)) for j in range(i + 1, len(order))]
    if pairs:
        hi = max(pairs, key=lambda p: p[2]); lo = min(pairs, key=lambda p: p[2])
        L.append(f"correlations: most-correlated {hi[0]}/{hi[1]} {hi[2]:+.2f}  ·  least {lo[0]}/{lo[1]} {lo[2]:+.2f}")
    L.append("source: Alpha Vantage (split/dividend-adjusted) · buy-and-hold, weights drift · not investment advice")
    return "\n".join(L)


# ----------------- svg dashboard -----------------
def _corr_color(v):
    # diverging: +1 teal, 0 light grey, -1 red
    if v >= 0:
        t = v
        r = int(246 + (29 - 246) * t); g = int(247 + (158 - 247) * t); b = int(249 + (117 - 249) * t)
    else:
        t = -v
        r = int(246 + (226 - 246) * t); g = int(247 + (75 - 247) * t); b = int(249 + (74 - 249) * t)
    return f"rgb({r},{g},{b})"


def _polar(cx, cy, r, ang):
    return cx + r * math.cos(ang), cy + r * math.sin(ang)


def render_svg(D):
    W, H = 1320, 672
    s = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" font-family="-apple-system,Segoe UI,Helvetica,sans-serif" role="img">']
    s.append(f'<title>portfolio risk/return dashboard</title>')
    s.append(f'<rect width="{W}" height="{H}" fill="#ffffff"/>')

    def t(x, y, txt, sz=12, c=INK, w=400, a="start"):
        s.append(f'<text x="{x}" y="{y}" font-size="{sz}" font-weight="{w}" fill="{c}" text-anchor="{a}">{esc(txt)}</text>')

    def card(x, y, w, h, title):
        s.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="#f6f7f9"/>')
        t(x + 16, y + 24, title, 13, INK, 700)

    # header
    t(30, 40, "Portfolio", 24, INK, 800)
    t(170, 40, f'{D["nHoldings"]} holdings · {D["start"]} → {D["asOf"]}', 13, GRA, 500)
    t(1290, 34, f'${num(D["totalValue"],0)}', 22, INK, 800, "end")
    rc = TEAL if (D["annRet"] or 0) >= 0 else RED
    t(1290, 54, f'{pct(D["annRet"])} ann · Sharpe {num(D["sharpe"],2)}', 13, rc, 600, "end")

    # metric strip
    b = D["benchmark"]
    mets = [("Ann return", pct(D["annRet"])), ("Ann vol", pctu(D["annVol"])), ("Sharpe", num(D["sharpe"], 2)),
            ("Max drawdown", pct(D["maxDD"])), ("Beta " + (b["symbol"] if b else ""), num(D["beta"], 2) if D["beta"] is not None else "n/a"),
            ("Risk-free", pctu(D["riskFree"], 2)), ("Holdings", str(D["nHoldings"]))]
    cw = (W - 60) / len(mets)
    for i, (lab, val) in enumerate(mets):
        x = 30 + i * cw
        s.append(f'<rect x="{x:.0f}" y="66" width="{cw-10:.0f}" height="54" rx="8" fill="#f6f7f9"/>')
        t(x + 14, 89, lab, 11, GRA, 500); t(x + 14, 110, val, 17, INK, 800)

    # ---- row 1: cumulative line (left) | risk-return scatter (right) ----
    C1, CW1 = 30, 760
    card(C1, 134, CW1, 250, "Cumulative return vs benchmark")
    sp = D["spark"]; px0, px1 = C1 + 48, C1 + CW1 - 16; py0, py1 = 158, 134 + 250 - 28
    allv = list(sp["port"]) + list(sp.get("bench", []))
    lo, hi = min(allv + [0]), max(allv + [0]); span = (hi - lo) or 1; hi += span * 0.08; lo -= span * 0.08
    xs = lambda i: px0 + (px1 - px0) * i / (len(sp["port"]) - 1 if len(sp["port"]) > 1 else 1)
    ys = lambda v: py1 - (v - lo) / (hi - lo) * (py1 - py0)
    s.append(f'<line x1="{px0}" y1="{ys(0):.1f}" x2="{px1}" y2="{ys(0):.1f}" stroke="#dcdfe3"/>')
    for gv in (lo, (lo + hi) / 2, hi):
        t(px0 - 6, ys(gv) + 3, f'{gv*100:+.0f}%', 9, GRA, 400, "end")
    if sp.get("bench"):
        pts = " ".join(f'{xs(i):.1f},{ys(v):.1f}' for i, v in enumerate(sp["bench"]))
        s.append(f'<polyline points="{pts}" fill="none" stroke="{GRA}" stroke-width="1.6" stroke-dasharray="4 3"/>')
        t(px1, ys(sp["bench"][-1]) - 4, b["symbol"], 9, GRA, 700, "end")
    pts = " ".join(f'{xs(i):.1f},{ys(v):.1f}' for i, v in enumerate(sp["port"]))
    pcol = TEAL if sp["port"][-1] >= 0 else RED
    s.append(f'<polyline points="{pts}" fill="none" stroke="{pcol}" stroke-width="2.4"/>')
    t(px1, ys(sp["port"][-1]) + 11, f'{sp["port"][-1]*100:+.0f}%', 10, pcol, 700, "end")
    t(C1 + 16, 134 + 250 - 8, f'{sp["dates"][0]} → {sp["dates"][-1]}', 9, GRA, 400)

    # scatter: x = vol, y = return, bubble area ~ weight
    C2, CW2 = 810, 480
    card(C2, 134, CW2, 250, "Risk / return (bubble = weight)")
    hold = D["holdings"]
    sx0, sx1 = C2 + 44, C2 + CW2 - 18; sy0, sy1 = 162, 134 + 250 - 30
    vols = [h["annVol"] for h in hold if h["annVol"] is not None] + ([b["annVol"]] if b else []) + [D["annVol"] or 0]
    rets = [h["annRet"] for h in hold if h["annRet"] is not None] + ([b["annRet"]] if b else []) + [D["annRet"] or 0]
    vmax = max(vols + [0.01]) * 1.15; rmin = min(rets + [0]); rmax = max(rets + [0]); rspan = (rmax - rmin) or 1
    rmin -= rspan * 0.12; rmax += rspan * 0.12
    sX = lambda v: sx0 + (v / vmax) * (sx1 - sx0)
    sY = lambda r: sy1 - (r - rmin) / (rmax - rmin) * (sy1 - sy0)
    s.append(f'<line x1="{sx0}" y1="{sY(0):.1f}" x2="{sx1}" y2="{sY(0):.1f}" stroke="#dcdfe3"/>')
    s.append(f'<line x1="{sX(0):.1f}" y1="{sy0}" x2="{sX(0):.1f}" y2="{sy1}" stroke="#eceef1"/>')
    t(sx1, sy1 + 16, "vol →", 9, GRA, 500, "end")
    for gx in (0.0, vmax / 2, vmax):
        t(sX(gx), sy1 + 16, f'{gx*100:.0f}%', 9, GRA, 400, "middle")
    for h in hold:
        if h["annVol"] is None or h["annRet"] is None:
            continue
        r = max(4, math.sqrt(h["weight"]) * 34)
        s.append(f'<circle cx="{sX(h["annVol"]):.1f}" cy="{sY(h["annRet"]):.1f}" r="{r:.1f}" fill="{TEAL}" opacity="0.45"/>')
        t(sX(h["annVol"]), sY(h["annRet"]) + 3, h["ticker"], 9, INK, 700, "middle")
    if b:
        s.append(f'<rect x="{sX(b["annVol"])-5:.1f}" y="{sY(b["annRet"])-5:.1f}" width="10" height="10" fill="{GRA}"/>')
        t(sX(b["annVol"]), sY(b["annRet"]) - 9, b["symbol"], 9, GRA, 700, "middle")
    pvx, pvy = sX(D["annVol"] or 0), sY(D["annRet"] or 0)
    s.append(f'<path d="M{pvx:.1f},{pvy-7:.1f} L{pvx+7:.1f},{pvy:.1f} L{pvx:.1f},{pvy+7:.1f} L{pvx-7:.1f},{pvy:.1f} Z" fill="{BLUE}"/>')
    t(pvx, pvy - 11, "Portfolio", 9, BLUE, 800, "middle")

    # ---- row 2: heatmap | donut | risk contribution ----
    order = D["order"]; cm = D["corr"]; nn = len(order)
    C3, CW3 = 30, 410
    card(C3, 398, CW3, 250, "Correlation (daily returns)")
    gx0, gy0 = C3 + 70, 444; cell = min(28, (CW3 - 90) / max(nn, 1), (250 - 84) / max(nn, 1))
    for i, a in enumerate(order):
        t(gx0 - 6, gy0 + cell * (i + 0.62), a, 8.5, GRA, 600, "end")
        t(gx0 + cell * (i + 0.5), gy0 - 5, a, 8.5, GRA, 600, "middle")
    for i in range(nn):
        for j in range(nn):
            v = cm[i][j]
            s.append(f'<rect x="{gx0+cell*j:.1f}" y="{gy0+cell*i:.1f}" width="{cell-1:.1f}" height="{cell-1:.1f}" rx="2" fill="{_corr_color(v)}"/>')
            if cell >= 22:
                t(gx0 + cell * (j + 0.5), gy0 + cell * (i + 0.62), f'{v:+.1f}', 7.5, INK if abs(v) < 0.6 else "#ffffff", 600, "middle")

    # donut: weights
    C4 = 460
    card(C4, 398, 410, 250, "Weights")
    dcx, dcy, dr, dth = C4 + 120, 530, 78, 26
    ang = -math.pi / 2
    dcolors = [TEAL, BLUE, AMBER, DK, "#7fc8a9", "#9b8cff", "#e08a8a", LT, GRA, "#c7ccd3"]
    for k, h in enumerate(D["holdings"]):
        frac = h["weight"]; a1 = ang + frac * 2 * math.pi
        x0, y0 = _polar(dcx, dcy, dr, ang); x1, y1 = _polar(dcx, dcy, dr, a1)
        large = 1 if (a1 - ang) > math.pi else 0
        col = dcolors[k % len(dcolors)]
        s.append(f'<path d="M{x0:.1f},{y0:.1f} A{dr},{dr} 0 {large} 1 {x1:.1f},{y1:.1f}" fill="none" stroke="{col}" stroke-width="{dth}"/>')
        ang = a1
    t(dcx, dcy - 2, str(D["nHoldings"]), 20, INK, 800, "middle"); t(dcx, dcy + 15, "names", 9, GRA, 500, "middle")
    lx, ly = C4 + 230, 430
    for k, h in enumerate(D["holdings"][:9]):
        col = dcolors[k % len(dcolors)]
        s.append(f'<rect x="{lx}" y="{ly-8:.0f}" width="9" height="9" rx="2" fill="{col}"/>')
        t(lx + 14, ly, f'{h["ticker"]}  {pctu(h["weight"],1)}', 9.5, INK, 500); ly += 19

    # risk contribution bars
    C5 = 890
    card(C5, 398, 400, 250, "Risk contribution vs weight")
    hold = D["holdings"][:9]; rx0, rx1 = C5 + 60, C5 + 400 - 80; rtop = 432; rh = 16
    maxrc = max([abs(h["riskContrib"]) for h in hold] + [h["weight"] for h in hold] + [0.01])
    t(C5 + 60, rtop - 4, "■ risk%", 9, AMBER, 700); t(C5 + 120, rtop - 4, "■ weight%", 9, GRA, 700)
    yy = rtop + 8
    for h in hold:
        t(C5 + 16, yy + rh - 10, h["ticker"], 9.5, INK, 600)
        wlen = h["weight"] / maxrc * (rx1 - rx0)
        rlen = max(0, h["riskContrib"]) / maxrc * (rx1 - rx0)
        s.append(f'<rect x="{rx0}" y="{yy:.0f}" width="{wlen:.1f}" height="6" rx="2" fill="{GRA}" opacity="0.5"/>')
        s.append(f'<rect x="{rx0}" y="{yy+8:.0f}" width="{rlen:.1f}" height="6" rx="2" fill="{AMBER}"/>')
        t(rx1 + 6, yy + 11, pctu(h["riskContrib"], 0), 9, INK, 600)
        yy += 23
        if yy > 398 + 250 - 16:
            break

    t(30, H - 10, "Alpha Vantage (split/dividend-adjusted) · buy-and-hold, weights drift · data analysis, not investment advice", 10, GRA)
    s.append("</svg>")
    return "\n".join(s)


# ----------------- dashboard spec (Ф7-2: the ENGINE renders the page) -----------------
def to_spec(D):
    """Portfolio -> a dashboard.py SPEC. Layout (user defaults 2026-07-05): cumulative
    return full-width, [bubble | correlation], [drawdown | weights], holdings table."""
    b = D.get("benchmark")
    sp = D["spark"]
    pct100 = lambda xs: [round(x * 100, 2) for x in xs]  # noqa: E731
    cum_series = [{"name": "Portfolio", "values": pct100(sp["port"])}]
    if b and sp.get("bench"):
        cum_series.append({"name": b["symbol"], "values": pct100(sp["bench"]),
                           "dash": [4, 3], "color": "#8a8f98"})
    max_rc = max((h["riskContrib"] for h in D["holdings"]), default=0) or 1
    rows = [[h["ticker"], f"{h['weight'] * 100:.1f}%", f"${h['value']:,.0f}",
             pct(h["annRet"]), pctu(h["annVol"]), num(h["sharpe"], 2), pct(h["maxDD"]),
             f"{h['riskContrib'] * 100:.0f}%"] for h in D["holdings"]]
    rowstyle = [f"background:rgba(226,75,74,{0.42 * h['riskContrib'] / max_rc:.3f})"
                if h["riskContrib"] > 0 else "" for h in D["holdings"]]
    _hint = {
        "Ann return": "Geometric annualized return of the buy-and-hold value path over THIS window — not your P&L since purchase.",
        "Ann vol": "Annualized standard deviation of daily returns (sqrt-252 scaling).",
        "Sharpe": "(Ann return - risk-free) / ann vol. Penalizes upside and downside volatility equally.",
        "Sortino": "Like Sharpe but divides by DOWNSIDE deviation only — upside swings are not punished.",
        "Calmar": "Ann return / |max drawdown| — return earned per unit of worst pain.",
        "Max drawdown": "Deepest peak-to-trough fall of the portfolio value inside the window.",
        "Beta": "Sensitivity to the benchmark: 1.5 moves ~1.5x the benchmark's swing, both ways.",
        "Corr SPY": "Correlation of daily returns with the benchmark; high correlation = the benchmark already owns most of this risk.",
        "Risk-free": "Annualized 3-month T-bill used in Sharpe/Sortino.",
        "Holdings": "Positions in the basket; buy-and-hold, so weights drift with prices.",
    }
    bsym = b["symbol"] if b else None
    cards = [
        {"label": "Ann return", "value": pct(D["annRet"])},
        {"label": "Ann vol", "value": pctu(D["annVol"])},
        {"label": "Sharpe", "value": num(D["sharpe"], 2)},
        {"label": "Sortino", "value": num(D.get("sortino"), 2)},
        {"label": "Calmar", "value": num(D.get("calmar"), 2)},
        {"label": "Max drawdown", "value": pct(D["maxDD"])},
        {"label": "Beta", "value": num(D["beta"], 2)},
        {"label": "Corr SPY", "value": num(D.get("corrBench"), 2)},
        {"label": "Risk-free", "value": pctu(D["riskFree"], 2)},
        {"label": "Holdings", "value": str(D["nHoldings"])},
    ]
    if not bsym:
        cards = [c for c in cards if c["label"] not in ("Beta", "Corr SPY")]   # no benchmark -> drop the pair, don't show a phantom SPY
    for c in cards:
        lbl = c["label"]
        if lbl == "Corr SPY" and bsym and bsym != "SPY":
            c["label"] = f"Corr {bsym}"
        c.setdefault("hint", _hint.get(lbl, _hint.get("Corr SPY" if lbl.startswith("Corr") else lbl, "")))
    panels = [
        {"type": "line", "title": ("Cumulative return vs " + bsym) if bsym else "Cumulative return",
         "full": True, "units": {"left": "%"},
         "info": "Growth of the whole basket vs the benchmark over the window; the gap between the lines is your active return.",
         "data": {"labels": sp["dates"], "series": cum_series}},
        {"type": "bubble", "title": "Risk / return (bubble = weight)",
         "info": "Each holding by annualized volatility (x) and return (y); bubble size is its weight. Up-left beats down-right.",
         "data": {"points": [{"x": round((h["annVol"] or 0) * 100, 1), "y": round((h["annRet"] or 0) * 100, 1),
                              "r": 6 + round(h["weight"] * 30), "label": h["ticker"]} for h in D["holdings"]],
                  "benchmark": ({"x": round((b["annVol"] or 0) * 100, 1), "y": round((b["annRet"] or 0) * 100, 1),
                                 "label": bsym} if b else None)}},
        {"type": "heatmap", "title": "Correlation (daily returns)",
         "info": "Pairwise correlation of daily returns; deep green pairs move together, red pairs hedge each other.",
         "data": {"labels": D["order"], "matrix": D["corr"]}},
        {"type": "line", "title": "Drawdown (underwater)", "units": {"left": "%"},
         "info": "How far below its running peak the portfolio sat each day — depth AND duration of pain, which the max-drawdown card alone hides.",
         "data": {"labels": sp["dates"], "series": [
             {"name": "drawdown", "values": pct100(sp["dd"]), "fill": True, "color": "#C0392B"}]}},
        {"type": "doughnut", "title": "Weights",
         "info": "Current (drifted) weights of the basket at the window end.",
         "data": {"labels": D["order"], "values": [round(h["weight"] * 100, 1) for h in D["holdings"]]}},
        {"type": "table", "title": "Holdings · risk contribution",
         "data": {"columns": ["Ticker", "Weight", "Value", "Return", "Vol", "Sharpe", "MaxDD", "Risk%"],
                  "rows": rows, "rowstyle": rowstyle}},
    ]
    ann = pct(D["annRet"])
    return {"title": f"Portfolio · {D['nHoldings']} holdings",
            "head": {"leftBig": "Portfolio", "leftSmall": f"{D['nHoldings']} holdings",
                     "tags": [x for x in (bsym and f"vs {bsym}", f"{D['nDays']} trading days", "buy-and-hold") if x],
                     "rightBig": f"${D['totalValue']:,.0f}",
                     "rightSmall": f"{ann} ann · Sharpe {num(D['sharpe'], 2)}"},
            "subtitle": f"Alpha Vantage (split/dividend-adjusted) · {D['start']} → {D['asOf']} · "
                        f"window returns, not P&L since purchase · not advice",
            "cards": cards, "panels": panels}


def render_html(D):
    """Ф7-2 P3: the interactive page IS the engine's now — the bespoke Chart.js template,
    its inline bundle and ~150 lines of JS/CSS are gone."""
    return dashboard.render_interactive_html(to_spec(D))


def _arg_posint(s):
    """Parse-time validator (docs/command-layer-plan.md §0.5): non-positive dies at argparse, not mid-run."""
    v = int(s)
    if v <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return v


def default_holdings_path():
    """The standing holdings file: <skill root>/holdings.csv — the .env pattern. Survives
    reinstalls (unzip -o leaves extra files) and is package-guarded (never ships in a build).
    A future broker MCP just becomes another WRITER of this same file."""
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "holdings.csv")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Analyze a portfolio's risk / return (Alpha Vantage)")
    g = ap.add_mutually_exclusive_group(required=False)
    g.add_argument("--portfolio", help="CSV holdings file (ticker + shares|weight|value, auto-detected)")
    g.add_argument("--holdings", help='inline holdings, e.g. "NVDA:100,AAPL:50,MSFT:40"')
    ap.add_argument("--as", dest="as_kind", choices=["shares", "weight", "value"], default="shares",
                    help="how to read --holdings numbers (default shares); ignored for --portfolio (auto-detected)")
    ap.add_argument("--benchmark", default="SPY", help="benchmark ticker (default SPY; '' to skip)")
    ap.add_argument("--days", type=_arg_posint, default=365, help="lookback window in calendar days (default 365)")
    ap.add_argument("--svg", help="write a self-contained SVG dashboard (fixed landscape)")
    ap.add_argument("--html", help="write a self-contained RESPONSIVE HTML dashboard (Chart.js, reflows 1->2 cols)")
    ap.add_argument("--png", action="store_true", help="also write a PNG (rsvg-convert/qlmanage/headless browser)")
    ap.add_argument("--json", action="store_true", help="emit compact JSON instead of text")
    ap.add_argument("--open", action="store_true", help="open the dashboard in the default browser")
    ap.add_argument("--from-dir", help="MCP-fallback (egress-blocked sandboxes): build from a directory of saved "
                    "TIME_SERIES_DAILY_ADJUSTED results (one file per ticker, name contains the ticker)")
    ap.add_argument("--env-file")
    a = ap.parse_args(argv)

    if not a.portfolio and not a.holdings:
        dflt = default_holdings_path()
        if os.path.exists(dflt):
            a.portfolio = dflt
            print(f"holdings: {dflt}", file=sys.stderr)
        else:
            sys.exit("error: no holdings given — pass --holdings/--portfolio, or create the standing "
                     f"file {dflt} (CSV: ticker + shares|weight|value; auto-detected, never packaged)")
    if a.portfolio:
        entries, kind = parse_holdings_csv(a.portfolio)
    else:
        entries, kind = parse_holdings_inline(a.holdings, a.as_kind)
    bench = a.benchmark or None

    if a.from_dir:
        D = read_from_dir(entries, bench, a.from_dir, kind=kind)
    else:
        D = gather(entries, kind, bench, get_key(a.env_file), a.days)

    if a.svg or a.png or a.open or a.html:
        wrote = []
        if a.html:
            # no `offers`: SKILL.md's palette is per-NAME (company or commodity); a whole-portfolio
            # read has no matching document, so the note stops at present + read.
            dashboard.deliver_html(a.html, render_html(D))
            print(f"{a.html}  |  portfolio responsive html dashboard")
            wrote.append(a.html)
        if a.svg or a.png or not wrote:
            out = a.svg or "portfolio_dashboard.svg"
            open(out, "w", encoding="utf-8").write(render_svg(D))
            print(f"{out}  |  portfolio dashboard  |  {D['nHoldings']} holdings  |  ann {pct(D['annRet'])} · Sharpe {num(D['sharpe'],2)}")
            if a.png:
                sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
                import chart
                png = (out[:-4] if out.endswith(".svg") else out) + ".png"
                print(f"{png}  |  via {chart.rasterize(out, png)}")
            wrote.append(out)
        if a.open:
            webbrowser.open(pathlib.Path(os.path.abspath(a.html if a.html else wrote[0])).as_uri())
    elif a.json:
        print(json.dumps(D, separators=(",", ":")))
    else:
        print(render_text(D))


if __name__ == "__main__":
    main()
