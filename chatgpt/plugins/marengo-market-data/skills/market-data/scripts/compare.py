#!/usr/bin/env python3
"""
compare.py — side-by-side COMPANY comparison (2-8 names), Alpha Vantage.

The questions N separate dashboards can't answer on one axis: which is cheaper FOR ITS
GROWTH (the bubble map), whose margins lead (grouped bars), who ran better rebased to 100
(one line chart), and the ranked metrics table with best-in-row marks.

LIGHT tier (default): K × OVERVIEW + 1 × REALTIME_BULK_QUOTES + K × TIME_SERIES_MONTHLY_ADJUSTED
(~2K+1 calls). --deep adds the statement-derived rows (FCF yield/conversion, dilution, net cash,
F-Score, Altman) via the full fundamentals gather per name (~13 calls each, staggered).
--cache DIR reuses fundamentals --save-raw payloads per ticker (DIR/<SYM>/overview.json…):
a cached name joins the comparison OFFLINE with deep columns; on --deep, fetched names are
saved back into the cache.

HONESTY RULES (design of record docs/ownership-tabs-compare-plan.md):
- ratios (%, ×) compare across currencies; $-LEVEL rows rank only when every column shares
  one currency — else the row is unranked and currency-labeled;
- per-column as-of dates are stamped (fiscal quarters differ — never implied simultaneous);
- an ETF/fund (empty OVERVIEW) is REFUSED loudly (compare companies; a fund is a basket);
- n/a never wins a row; peers are USER-named (never auto-picked).

Usage:
  python3 scripts/compare.py AAPL MSFT NVDA [GOOG ...]        # light, text
  python3 scripts/compare.py AAPL MSFT --deep --html cmp.html # + statements, dashboard
  python3 scripts/compare.py AAPL MSFT --cache ~/.avcache     # reuse saved payloads
"""
from __future__ import annotations
import argparse
import datetime as dt
import json
import re
import os
import pathlib
import sys
import time
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # sibling imports (Windows-safe)
import dashboard  # noqa: E402  — the ONE spec renderer
import fundamentals as fu  # noqa: E402  — av()/f()/b()/gather()/read_from_dir()/build() reuse


# ----------------- column extraction -----------------
def _pos52(price, lo, hi):
    if price is None or lo is None or hi is None or hi <= lo:
        return None
    return max(0.0, min(100.0, (price - lo) / (hi - lo) * 100))


def _upside(target, price):
    if target is None or not price:
        return None
    return (target - price) / price * 100


def _light_col(sym, ov, q):
    """One comparison column from OVERVIEW + a REALTIME_BULK_QUOTES row. The LIGHT tier —
    everything here is an OVERVIEW field (AV-computed TTM ratios, basis disclosed); the
    statement-derived keys stay None (honest holes) until --deep."""
    if not ov or "Symbol" not in ov:
        sys.exit(f"error: {sym} has no OVERVIEW (an ETF/fund or a bad ticker) — comparison is "
                 f"company-only. For a fund run fundamentals.py {sym} (the ETF profile), or use "
                 f"it as a benchmark line elsewhere.")
    f = fu.f
    price = f(q or {}, "close")
    pct = lambda k: (f(ov, k) * 100 if f(ov, k) is not None else None)  # noqa: E731
    return {
        "sym": sym, "tier": "light", "name": ov.get("Name") or sym, "sector": ov.get("Sector"),
        "currency": ov.get("Currency") or "USD", "stmtCur": None,
        "asOf": ov.get("LatestQuarter") or "?", "quoteTs": (q or {}).get("timestamp"),
        "price": price, "capB": (f(ov, "MarketCapitalization") / 1e9
                                 if f(ov, "MarketCapitalization") is not None else None),
        "fwdPE": f(ov, "ForwardPE"), "trailPE": f(ov, "TrailingPE"), "peg": f(ov, "PEGRatio"),
        "ps": f(ov, "PriceToSalesRatioTTM"), "pb": f(ov, "PriceToBookRatio"),
        "evEbitda": f(ov, "EVToEBITDA"), "roe": pct("ReturnOnEquityTTM"),
        "grossM": None, "opM": pct("OperatingMarginTTM"), "netM": pct("ProfitMargin"),
        "marginBasis": "OVERVIEW TTM fields (AV-computed; --deep recomputes from quarterlies)",
        "revYoY": pct("QuarterlyRevenueGrowthYOY"), "epsYoY": pct("QuarterlyEarningsGrowthYOY"),
        "divYield": pct("DividendYield"), "beta": f(ov, "Beta"),
        "pos52w": _pos52(price, f(ov, "52WeekLow"), f(ov, "52WeekHigh")),
        "upside": _upside(f(ov, "AnalystTargetPrice"), price),
        "fcfYield": None, "fcfConv": None, "dilution": None, "netCashB": None,
        "fScore": None, "altman": None,
    }


def _deep_col(D):
    """One comparison column from a full fundamentals D (fetched or --cache-read): the light
    fields on the COMPUTED basis + the statement-derived rows."""
    if D.get("kind") == "etf":
        sys.exit(f"error: {D.get('symbol')} is an ETF/fund — comparison is company-only "
                 f"(compare its holdings, or use it as a benchmark).")
    fs = D.get("fScore")
    return {
        "sym": D["symbol"], "tier": "deep", "name": D.get("name") or D["symbol"], "sector": D.get("sector"),
        "currency": D.get("currency") or "USD", "stmtCur": D.get("stmtCurrency"),
        "asOf": D.get("latestQuarter") or "?", "quoteTs": None,
        "price": D.get("price"), "capB": D.get("marketCapB"),
        "fwdPE": D.get("fwdPE"), "trailPE": D.get("trailingPE"), "peg": D.get("peg"),
        "ps": D.get("ps"), "pb": D.get("pb"), "evEbitda": D.get("evEbitda"),
        "roe": D.get("roe"), "grossM": D.get("grossMarginTTM"), "opM": D.get("opMargin"),
        "netM": D.get("profitMargin"),
        "marginBasis": (D.get("marginBasis") or {}).get("net") or "computed TTM",
        "revYoY": D.get("revGrowthYoYPct"), "epsYoY": D.get("epsGrowthYoYPct"),
        "divYield": D.get("divYieldPct"), "beta": D.get("beta"),
        "pos52w": _pos52(D.get("price"), D.get("wkLow"), D.get("wkHigh")),
        "upside": _upside(D.get("analystTarget"), D.get("price")),
        "fcfYield": D.get("fcfYieldPct"), "fcfConv": D.get("fcfConversion"),
        "dilution": D.get("dilutionCagrPct"), "netCashB": D.get("netCashNowB"),
        "fScore": (fs.get("score") if fs else None), "altman": D.get("altmanZ"),
    }


# ----------------- v1.101: the ETF branch (user 2026-07-15: "сравни MSOS и MJ") -----------------
def _refuse_mixed(etf_syms, co_syms):
    """Funds compare with funds, companies with companies — a mixed set has no shared metric
    (a fund has no margins/PE; a company has no expense ratio/holdings). Loud, names both sides."""
    if etf_syms and co_syms:
        sys.exit(f"error: mixed set — {', '.join(etf_syms)} are funds but {', '.join(co_syms)} are "
                 f"companies; compare funds with funds (or a company with the fund's holdings).")


def _etf_col(D):
    """One ETF comparison column from a fundamentals.build_etf D — profile scalars, the
    series-derived risk/return set (v1.92 seriesStats) and the store-vol context (v1.87
    volSnap, zero AV calls)."""
    ss = D.get("seriesStats") or {}
    rets = ss.get("rets") or {}
    vs = D.get("volSnap") or {}
    age = None
    if D.get("inceptionDate"):
        try:
            import datetime as _dt
            age = round((_dt.date.today() - _dt.date.fromisoformat(D["inceptionDate"])).days / 365.25, 1)
        except ValueError:
            pass
    return {
        "sym": D["symbol"], "tier": "etf", "name": D.get("name") or D["symbol"],
        "currency": "USD", "stmtCur": None, "marginBasis": None,
        "asOf": D.get("asOfSeries") or "?", "quoteTs": None, "price": D.get("price"),
        "leveraged": D.get("leveraged") not in (None, "", "NO"),
        "expense": D.get("expenseRatioPct"), "aumB": D.get("netAssetsB"),
        "advM": (ss.get("advUsd3m") / 1e6 if ss.get("advUsd3m") is not None else None),
        "divY": D.get("divYieldPct"), "ttmY": (D.get("dist") or {}).get("ttmYieldPct"),
        "turnover": D.get("turnoverPct"), "ageY": age,
        "nHold": D.get("nHoldings") or None, "top10": D.get("conc10"),
        "pos52w": ss.get("pos52"), "vs200": ss.get("vs200Pct"),
        "maxDD": ss.get("maxDDPct"), "curDD": ss.get("curDDPct"),
        "r1m": rets.get("1M"), "r3m": rets.get("3M"), "r6m": rets.get("6M"),
        "r1y": rets.get("1Y"), "r3y": rets.get("3Y"), "itdY": rets.get("ITD/y"),
        "iv30": (vs.get("cm30") * 100 if isinstance(vs.get("cm30"), (int, float)) else None),
        "ivRank": vs.get("rank"), "vrp": vs.get("vrp"),
    }


# fund metric rows: (key, label, decimals, direction, gate). Returns rank high; expense low;
# yield/turnover/age/concentration/vol stay UNRANKED (preference or context, not better/worse);
# ITD %/yr unranked — different inceptions make it non-comparable.
_M_ETF = [
    ("expense", "expense ratio %", 2, "low", None),
    ("aumB", "net assets $B", 1, "high", None),
    ("advM", "avg $ vol 3m $M", 0, "high", None),
    ("divY", "div yield %", 2, None, None),
    ("ttmY", "TTM yield %", 2, None, None),
    ("turnover", "turnover %", 0, None, None),
    ("ageY", "age (years)", 1, None, None),
    ("nHold", "holdings #", 0, None, None),
    ("top10", "top-10 weight %", 0, None, None),
    ("pos52w", "52w position %", 0, None, None),
    ("vs200", "vs 200d MA %", 1, None, None),
    ("maxDD", "max drawdown %", 0, "high", None),
    ("curDD", "now from peak %", 0, None, None),
    ("r1m", "return 1M %", 1, "high", None), ("r3m", "return 3M %", 1, "high", None),
    ("r6m", "return 6M %", 1, "high", None), ("r1y", "return 1Y %", 1, "high", None),
    ("r3y", "return 3Y %", 1, "high", None), ("itdY", "ITD %/yr", 1, None, None),
    ("iv30", "IV 30d %", 1, None, None), ("ivRank", "IV rank %", 0, None, None),
    ("vrp", "VRP 30d", 1, None, None),
]

_CASHY = {"CASH", "USD", "US DOLLAR", "US DOLLARS"}


def _norm_hold(name):
    """Holding-name key for overlap matching: uppercase, punctuation out, legal/wrapper suffix
    tokens stripped from the TAIL — 'CURALEAF HOLDINGS INC SWAP' and 'CURALEAF HOLDINGS INC'
    both key to 'CURALEAF' (MSOS holds swaps, MJ holds the shares — same bet)."""
    toks = re.sub(r"[^A-Z0-9& ]", " ", (name or "").upper()).split()
    drop = {"SWAP", "INC", "CORP", "CO", "LTD", "PLC", "SA", "AG", "NV", "ADR", "SUB",
            "SUBORDIN", "CL", "CLASS", "A", "B", "SHS", "ORD", "HOLDINGS", "HLDGS", "THE"}
    while toks and toks[-1] in drop:
        toks.pop()
    return " ".join(toks)


def _holdings_overlap(Da, Db):
    """Pairwise holdings overlap with HONEST coverage: shared names (normalized — swaps match
    their plain twins), each side's shared weight, and how much of each fund the DISCLOSED
    list covers at all (a swap-based fund may disclose well under 100% — MSOS live: 42.5%).
    Cash/treasury sleeves never count as overlap."""
    def keyed(D):
        out = {}
        for h in (D.get("holdings") or []):
            k = _norm_hold(h.get("name")) or str(h.get("symbol") or "").upper()
            if not k or h.get("weightPct") in (None, 0):
                continue
            if k in _CASHY or "TREASURY" in k or k == "N/A":
                continue
            e = out.setdefault(k, {"name": h.get("name") or h.get("symbol") or k, "w": 0.0})
            e["w"] = round(e["w"] + h["weightPct"], 4)
        return out
    A, B = keyed(Da), keyed(Db)
    covA = round(sum(h["weightPct"] for h in (Da.get("holdings") or []) if h.get("weightPct")), 1)
    covB = round(sum(h["weightPct"] for h in (Db.get("holdings") or []) if h.get("weightPct")), 1)
    shared = sorted(set(A) & set(B), key=lambda k: -(A[k]["w"] + B[k]["w"]))
    return {"rows": [[str(A[k]["name"]).title(), A[k]["w"], B[k]["w"]] for k in shared],
            "sharedPctA": round(sum(A[k]["w"] for k in shared), 2),
            "sharedPctB": round(sum(B[k]["w"] for k in shared), 2),
            "covA": covA, "covB": covB, "nA": len(A), "nB": len(B)}


def build_etf_compare(Ds, monthly, days=365):
    """The fund set (arg order) -> the comparison dict: _etf_col columns + the shared rebased
    machinery + the pairwise overlap (2 funds only — a 3-way number would mislead)."""
    cols = [_etf_col(D) for D in Ds]
    C = build_compare(cols, monthly, days=days, deep=False)
    C["etf"] = True
    C["overlap"] = _holdings_overlap(Ds[0], Ds[1]) if len(Ds) == 2 else None
    C["leverageMix"] = len({c["leveraged"] for c in cols}) > 1
    return C


def _to_etf_spec(C):
    cols, syms = C["cols"], C["symbols"]

    def _best(key, direction, fmt):
        have = [(c["sym"], c.get(key)) for c in cols if c.get(key) is not None]
        if len(have) < 2:
            return None
        s, v = (min(have, key=lambda x: x[1]) if direction == "low" else max(have, key=lambda x: x[1]))
        return f"{s} · {fmt(v)}"
    cards = []
    _c1 = _best("expense", "low", lambda v: f"{v:.2f}%")
    if _c1:
        cards.append({"label": "Cheapest fee", "value": _c1,
                      "hint": "Lowest net expense ratio of the set — the one certain edge; fees "
                              "compound against you every year."})
    _c2 = _best("r1y", "high", lambda v: f"{v:+.1f}%")
    if _c2:
        cards.append({"label": "Best 1Y return", "value": _c2,
                      "hint": "Adjusted-close total return, trailing 12 months."})
    _c3 = _best("advM", "high", lambda v: f"${v:,.0f}M/day")
    if _c3:
        cards.append({"label": "Deepest liquidity", "value": _c3,
                      "hint": "Highest average daily dollar volume (3m) — cheaper to get in and out."})
    ov = C.get("overlap")
    if ov and ov["rows"]:
        a, b = syms[0], syms[1]
        cards.append({"label": "Holdings overlap", "value": f"{len(ov['rows'])} shared names",
                      "sub": f"{a} {ov['sharedPctA']:.0f}% · {b} {ov['sharedPctB']:.0f}%",
                      "hint": "Weight each fund holds in names the other also holds (normalized "
                              "names — swap wrappers match their share twins). High overlap = "
                              "the same bet in two wrappers; the fee decides."})
    _trows, _tcs = _rows(C)
    asof = " · ".join(f"{c['sym']} {c['asOf']}" for c in cols)
    panels = [
        {"type": "table", "title": "Metrics · best in row", "full": True, "info":
            "One column per fund. BOLD GREEN = best of DIRECTIONAL rows only (expense lower-"
            "better; AUM/liquidity/returns/max-drawdown higher-better); yield/turnover/"
            "concentration/vol rows are context, not ranked; ITD %/yr is unranked — inceptions "
            "differ. Series metrics are adjusted-close (dividends folded in).", "data": {
            "columns": ["Metric"] + syms, "rows": _trows, "cellstyle": _tcs,
            "legend": [{"sym": "■", "color": "#1D9E75",
                        "label": "bold green = best in row (directional metrics only)"}]}},
    ] + ([] if not any(any(v is not None for v in (x.get("values") or []))
                       for x in (C["rebased"]["series"] or [])) else [
        {"type": "line", "title": "Performance · rebased = 100", "full": True, "info":
            "Monthly ADJUSTED closes rebased to the first value in the window — dividends "
            "included; a later inception simply starts later.",
         "data": {"labels": C["rebased"]["labels"], "xall": True, "series": C["rebased"]["series"]}},
    ] if C["rebased"]["labels"] else [])
    if ov and ov["rows"]:
        a, b = syms[0], syms[1]
        panels.append({"type": "table", "title": "Holdings overlap", "info":
            f"Names both funds hold (normalized — swap wrappers match share twins), by combined "
            f"weight. HONEST COVERAGE: the disclosed lists cover {ov['covA']:.0f}% of {a} and "
            f"{ov['covB']:.0f}% of {b} — a swap-based fund discloses less, so the true overlap "
            f"can only be HIGHER. Cash/treasury sleeves never count.",
            "data": {"columns": ["Holding", f"{a} %", f"{b} %"],
                     "rows": [[r[0], fu.b(r[1], 2), fu.b(r[2], 2)] for r in ov["rows"]]}})
    return {
        "title": " vs ".join(syms) + " · ETF comparison",
        "subtitle": ("as of " + asof
                     + (" · NOT simultaneous" if C["asOfSpread"] else "")
                     + (" · ⚠ leverage differs ("
                        + " vs ".join(f"{c['sym']} {'leveraged' if c['leveraged'] else '1x'}"
                                      for c in cols) + ")" if C.get("leverageMix") else "")
                     + " · ETF profile + adjusted series · Alpha Vantage · not advice"),
        "cards": cards, "panels": panels,
    }


def _render_etf_text(C):
    cols = C["cols"]
    w = 11
    L = ["COMPARE ETF      " + "".join(f"{c['sym']:>{w}}" for c in cols),
         "as of            " + "".join(f"{c['asOf']:>{w}}" for c in cols)]
    for row in _rows(C, mark="◀")[0]:
        label, cells = row[0], row[1:]
        L.append(f"{label:<17}" + "".join(f"{c:>{w}}" for c in cells))
    ov = C.get("overlap")
    if ov and ov["rows"]:
        a, b = C["symbols"][0], C["symbols"][1]
        L.append("")
        L.append(f"OVERLAP    {len(ov['rows'])} shared · {a} {ov['sharedPctA']:.1f}% / "
                 f"{b} {ov['sharedPctB']:.1f}% of fund"
                 f"  ·  lists cover {ov['covA']:.0f}% / {ov['covB']:.0f}% (swap sleeves under-disclose)")
        for r in ov["rows"][:8]:
            L.append(f"  {r[0][:34]:<34} {fu.b(r[1],2):>7}% {fu.b(r[2],2):>7}%")
    if C.get("leverageMix"):
        L.append("⚠ leverage differs — not an apples comparison "
                 + "(" + " vs ".join(f"{c['sym']} {'leveraged' if c['leveraged'] else '1x'}" for c in cols) + ")")
    if C["asOfSpread"]:
        L.append("as-of dates DIFFER — columns are not simultaneous")
    L.append("source: Alpha Vantage ETF profile + adjusted series · data analysis, not investment advice")
    return "\n".join(L)


# ----------------- assembly -----------------
def build_compare(cols, monthly, days=365, deep=False):
    """Columns (arg order preserved) + the rebased-100 monthly series + honesty metadata."""
    # v1.126: key the union by MONTH, not by raw month-end DATE — a stale --cache (its last
    # daily lands mid-month) beside a fresh monthly pull carried TWO dates for one month:
    # a duplicated x-label + complementary None holes. Per symbol the latest date in a month wins.
    mm = {}
    for _sym, _px in monthly.items():
        _m2 = {}
        for _d, _v in _px.items():
            _k = _d[:7]
            if _k not in _m2 or _d > _m2[_k][0]:
                _m2[_k] = (_d, _v)
        mm[_sym] = {k: dv[1] for k, dv in _m2.items()}
    months = sorted({m for series in mm.values() for m in series})
    keep = months[-max(2, days // 30 + 1):] if months else []
    series = []
    for c in cols:
        px = mm.get(c["sym"], {})
        vals, base = [], None
        for m in keep:
            v = px.get(m)
            if v is not None and base is None:
                base = v
            vals.append(round(v / base * 100, 2) if (v is not None and base) else None)
        series.append({"name": c["sym"], "values": vals})
    return {"symbols": [c["sym"] for c in cols], "cols": cols, "deep": deep,
            "rebased": {"labels": [m[:7] for m in keep], "series": series},
            "asOfSpread": len({c["asOf"] for c in cols}) > 1}


# (key, row label, decimals, direction low|high|None, currency-gate key |None)
_M = [
    ("fwdPE", "fwd P/E", 1, "low", None), ("trailPE", "trailing P/E", 1, "low", None),
    ("peg", "PEG", 2, "low", None), ("ps", "P/S", 1, "low", None), ("pb", "P/B", 1, "low", None),
    ("evEbitda", "EV/EBITDA", 1, "low", None),
    ("roe", "ROE %", 0, "high", None), ("grossM", "gross margin %", 0, "high", "marginBasis"),
    ("opM", "op margin %", 0, "high", "marginBasis"), ("netM", "net margin %", 0, "high", "marginBasis"),
    ("revYoY", "rev YoY %", 1, "high", None), ("epsYoY", "EPS YoY %", 1, "high", None),
    ("divYield", "div yield %", 2, None, None), ("beta", "beta", 2, None, None),
    ("pos52w", "52w position %", 0, None, None), ("upside", "analyst upside %", 1, "high", None),
    ("capB", "market cap $B", 0, None, "currency"),
]
_M_DEEP = [
    ("fcfYield", "FCF yield %", 1, "high", None), ("fcfConv", "FCF conversion", 2, "high", None),
    ("dilution", "dilution %/yr", 1, "low", None), ("netCashB", "net cash $B", 1, "high", "stmtCur"),
    ("fScore", "F-Score /9", 0, "high", None), ("altman", "Altman Z", 1, "high", None),
]


def _winners(vals, direction):
    """Indexes holding the best value (ties share the mark); n/a never wins."""
    have = [(i, v) for i, v in enumerate(vals) if v is not None]
    if direction is None or len(have) < 2:
        return set()
    best = min(v for _, v in have) if direction == "low" else max(v for _, v in have)
    return {i for i, v in have if v == best}


_WIN_STYLE = "color:#1D9E75;font-weight:700"   # best-in-row: bold green (user 2026-07-13 — no glyph)


def _rows(C, mark=None):
    """The metrics-table rows: [label, cell...] (+ the aligned per-cell style matrix when mark is
    None — the HTML path styles winners bold-green; text passes mark='◀'). Currency gate applies."""
    cols = C["cols"]
    out, cell_styles = [], []
    metrics = _M_ETF if C.get("etf") else _M + (_M_DEEP if C.get("deep") else [])
    for key, label, dec, direction, gate in metrics:
        vals = [c.get(key) for c in cols]
        if all(v is None for v in vals):
            continue                                    # an all-hole row says nothing — drop it
        mixed = False
        if gate:
            curs = {c.get(gate) for c in cols if c.get(gate)}
            mixed = len(curs) > 1
        # winners on the DISPLAYED rounding: 20.04 vs 20.01 both print 20.0 — a tie, not a winner
        disp = [None if v is None else round(v, dec) for v in vals]
        win = set() if mixed else _winners(disp, direction)
        cells, csty = [], [""]                          # the label column carries no style
        for i, v in enumerate(vals):
            cell = fu.b(v, dec)
            if mixed and v is not None and gate != "marginBasis":   # currency labels only; the
                cell += " " + (cols[i].get(gate) or "")             # basis disclosure lives in info
            if i in win and mark:
                cell += " " + mark
            cells.append(cell)
            csty.append(_WIN_STYLE if (i in win and not mark) else "")
        out.append([label] + cells)
        cell_styles.append(csty)
    return out, cell_styles


def to_spec(C):
    if C.get("etf"):
        return _to_etf_spec(C)
    cols = C["cols"]
    syms = C["symbols"]

    def _best(key, direction, fmt):
        have = [(c["sym"], c.get(key)) for c in cols if c.get(key) is not None]
        if len(have) < 2:
            return None
        s, v = (min(have, key=lambda x: x[1]) if direction == "low" else max(have, key=lambda x: x[1]))
        return f"{s} · {fmt(v)}"
    cards = []
    _c1 = _best("fwdPE", "low", lambda v: f"{v:.1f}×")
    if _c1:
        cards.append({"label": "Cheapest fwd P/E", "value": _c1,
                      "hint": "Lowest forward multiple of the set — cheap on next-12m consensus "
                              "earnings. Cheap for a REASON is the question the growth map answers."})
    _c2 = _best("revYoY", "high", lambda v: f"{v:+.1f}%")
    if _c2:
        cards.append({"label": "Fastest grower", "value": _c2,
                      "hint": "Highest latest-quarter revenue growth YoY. Pair with the multiple — "
                              "growth is what the market pays up for."})
    _bases = {c.get("marginBasis") for c in cols if c.get("marginBasis")}
    basis_txt = (_bases.pop() if len(_bases) == 1
                 else "bases DIFFER per column (mixed tiers — light OVERVIEW vs computed TTM)")
    _c3 = _best("netM", "high", lambda v: f"{v:.0f}%")
    if _c3:
        cards.append({"label": "Best net margin", "value": _c3,
                      "hint": "Highest net margin (" + basis_txt + ")."})
    _pegs = [c.get("peg") for c in cols if c.get("peg") is not None]
    if len(_pegs) >= 2:
        cards.append({"label": "PEG spread", "value": f"{min(_pegs):.2f}–{max(_pegs):.2f}",
                      "hint": "Cheapest-to-richest growth-adjusted multiple across the set — a wide "
                              "spread means the market treats these growth stories very differently."})
    pts, dropped = [], []
    _maxcap = max((c.get("capB") or 0 for c in cols), default=0)
    for c in cols:
        if c.get("revYoY") is not None and c.get("fwdPE") is not None:
            # RELATIVE sizing: the set's largest cap = the engine max (26) — no clamp saturation
            r = (8 + 18 * ((c.get("capB") or 0) / _maxcap) ** 0.5) if _maxcap else 8
            pts.append({"x": round(c["revYoY"], 2), "y": round(c["fwdPE"], 2),
                        "r": round(r, 1), "label": c["sym"]})
        else:
            dropped.append(c["sym"])
    tiers = {c.get("tier") for c in cols}
    asof = " · ".join(f"{c['sym']} {c['asOf']}" + (f" ({c.get('tier')})" if len(tiers) > 1 else "")
                      for c in cols)
    _trows, _tcs = _rows(C)
    panels = [
        {"type": "table", "title": "Metrics · best in row", "full": True, "info":
            "One column per company. The BOLD GREEN value is the best of DIRECTIONAL rows only "
            "(multiples lower-better, margins/growth higher-better); n/a never wins; $-level rows "
            "rank only within one currency (else labeled, unranked). Each column is as of ITS OWN "
            "latest quarter — see the subtitle.", "data": {
            "columns": ["Metric"] + syms, "rows": _trows, "cellstyle": _tcs,
            "legend": [{"sym": "■", "color": "#1D9E75", "label": "bold green = best in row (directional metrics only)"}]}},
    ] + ([] if not any(any(v is not None for v in (x.get("values") or []))
                       for x in (C["rebased"]["series"] or [])) else [
        {"type": "line", "title": "Performance · rebased = 100", "full": True, "info":
            "Monthly ADJUSTED closes, each series rebased to its own first value in the window — "
            "unit- and currency-free relative performance, dividends included via the adjusted close. "
            "A later listing simply starts later (its line says nothing about the earlier period).",
         "data": {"labels": C["rebased"]["labels"], "xall": True, "series": C["rebased"]["series"]}},
    ] if C["rebased"]["labels"] else []) + [
        {"type": "bars", "title": "Margins (%)", "info":
            "Gross / operating / net margin per company (" + basis_txt + "). "
            "LEVEL is the business model; DIRECTION needs each company's own dashboard.",
         "data": {"labels": syms, "series": [
             s for s in (
                 {"name": "gross", "color": "#1D9E75", "values": [c.get("grossM") for c in cols]},
                 {"name": "operating", "color": "#378ADD", "values": [c.get("opM") for c in cols]},
                 {"name": "net", "color": "#EFB366", "values": [c.get("netM") for c in cols]})
             if any(v is not None for v in s["values"])]}},
        {"type": "bubble", "title": "Growth vs multiple (size = market cap)", "info":
            "x = revenue growth YoY, y = forward P/E, bubble size ∝ market cap. Down-right is "
            "cheap-for-its-growth; up-left is expensive-and-slowing."
            + (f" Dropped (missing a coordinate): {', '.join(dropped)}." if dropped else ""),
         "data": {"points": pts, "xUnit": "% rev YoY", "yUnit": "× fwd P/E"}},
    ]
    return {
        "title": " vs ".join(syms) + " · comparison",
        "subtitle": ("as of " + asof
                     + (" · NOT simultaneous (fiscal calendars differ)" if C["asOfSpread"] else "")
                     + (" · MIXED tiers (cached deep + light)" if len({c.get("tier") for c in cols}) > 1
                        else (" · deep (statements)" if C.get("deep") else " · light (OVERVIEW ratios)"))
                     + " · Alpha Vantage · not advice"),
        "cards": cards, "panels": panels,
    }


def render_text(C):
    if C.get("etf"):
        return _render_etf_text(C)
    cols = C["cols"]
    w = 11
    L = ["COMPARE          " + "".join(f"{c['sym']:>{w}}" for c in cols),
         "as of            " + "".join(f"{c['asOf']:>{w}}" for c in cols)]
    for row in _rows(C, mark="◀")[0]:
        label, cells = row[0], row[1:]
        L.append(f"{label:<17}" + "".join(f"{c:>{w}}" for c in cells))
    L.append("")
    _bases = {c.get("marginBasis") for c in cols if c.get("marginBasis")}
    L.append("margins basis: " + (_bases.pop() if len(_bases) == 1
             else "DIFFER per column (mixed tiers — light OVERVIEW vs computed TTM)"))
    if C["asOfSpread"]:
        L.append("as-of dates DIFFER (fiscal calendars) — columns are not simultaneous")
    L.append("source: Alpha Vantage · ratios compare across currencies, $ levels only within one · "
             "data analysis, not investment advice")
    return "\n".join(L)


# ----------------- fetch -----------------
def _monthly_closes(payload):
    ts = (payload or {}).get("Monthly Adjusted Time Series") or {}
    out = {}
    for d, row in ts.items():
        v = fu.f(row, "5. adjusted close")
        if v and v > 0:
            out[d] = v
    return out


def _monthly_from_daily(px_payload):
    """Month-END adjusted closes downsampled from a CACHED daily series — a fully-cached
    comparison is truly OFFLINE (0 calls), as the plan promises."""
    ts = (px_payload or {}).get("Time Series (Daily)") or {}
    best = {}
    for d, row in ts.items():
        v = fu.f(row, "5. adjusted close") or fu.f(row, "4. close")
        if v and v > 0:
            m = d[:7]
            if m not in best or d > best[m][0]:
                best[m] = (d, v)
    return {d: v for d, v in best.values()}


def _cached_dir(cache, sym):
    d = os.path.join(cache, sym) if cache else None
    return d if (d and os.path.exists(os.path.join(d, "overview.json"))) else None


def gather_compare(symbols, deep=False, cache=None, env_file=None):
    """Per symbol: the cached fundamentals payloads (0 calls — deep columns, monthly downsampled
    from the cached daily) or the tier fetch. The key resolves LAZILY (a fully-cached run needs
    neither key nor network); ONE bulk-quote call covers every live price and fires only when at
    least one name is actually fetched. Sequential + staggered (rate-friendly)."""
    key_box = {}

    def _key():
        if "k" not in key_box:
            key_box["k"] = fu.get_key(env_file)
        return key_box["k"]
    cached = {sym: _cached_dir(cache, sym) for sym in symbols}
    fetch_syms = [sym for sym in symbols if not cached[sym]]
    quotes = {}
    if fetch_syms:
        bulk = fu.av({"function": "REALTIME_BULK_QUOTES", "symbol": ",".join(symbols)}, _key())
        quotes = {r.get("symbol"): r for r in (bulk.get("data") or [])}
        if not quotes:
            print("warning: REALTIME_BULK_QUOTES returned no rows — live prices/upside/52w-position "
                  "read n/a for the fetched columns", file=sys.stderr)
    cols, monthly, any_deep = [], {}, deep
    etf_Ds = {}                                    # sym -> fundamentals D (kind='etf'), arg order
    etf_probes = {}                                # sym -> the ETF_PROFILE payload from routing
    for sym in symbols:
        if cached[sym]:
            R = fu.read_from_dir(sym, cached[sym])
            D = fu.build(sym, R, offline=True)   # v1.125: a cached re-render must not restamp the LKG cache
            if D.get("kind") == "etf":             # v1.101: a cached FUND joins the ETF branch
                etf_Ds[sym] = D
                monthly[sym] = _monthly_from_daily(R.get("px"))
                continue
            col = _deep_col(D)
            if fu.f(quotes.get(sym) or {}, "close") is not None:   # freshen when a bulk row exists
                col["price"] = fu.f(quotes.get(sym), "close")
                col["quoteTs"] = (quotes.get(sym) or {}).get("timestamp")
            cols.append(col)
            monthly[sym] = _monthly_from_daily(R.get("px"))        # 0 extra calls
            any_deep = True
            continue
        if deep:
            save = os.path.join(cache, sym) if cache else None
            D = fu.gather(sym, _key(), with_news=False, save_dir=save)
            cols.append(_deep_col(D))
            time.sleep(1.5)                       # stagger companies under the burst detector
        else:
            ov = fu.av({"function": "OVERVIEW", "symbol": sym}, _key())
            time.sleep(0.25)
            if not ov or "Symbol" not in ov:      # v1.101: an empty OVERVIEW may be a FUND
                probe = fu.av({"function": "ETF_PROFILE", "symbol": sym}, _key())
                time.sleep(0.25)
                if fu._is_etf_profile(probe):
                    etf_probes[sym] = probe
                    continue                      # per-fund payloads fetched in the ETF pass below
            cols.append(_light_col(sym, ov, quotes.get(sym)))
        m = fu.av({"function": "TIME_SERIES_MONTHLY_ADJUSTED", "symbol": sym}, _key())
        time.sleep(0.25)
        monthly[sym] = _monthly_closes(m)
    _refuse_mixed(list(etf_probes) + [s for s in etf_Ds if s not in etf_probes],
                  [c["sym"] for c in cols])
    for sym, probe in etf_probes.items():         # the ETF fetch pass: px + dividends + name
        px = fu.av({"function": "TIME_SERIES_DAILY_ADJUSTED", "symbol": sym, "outputsize": "full"},
                   _key())
        time.sleep(0.25)
        div = fu._bg_av({"function": "DIVIDENDS", "symbol": sym}, _key())
        search = fu._bg_av({"function": "SYMBOL_SEARCH", "keywords": sym}, _key())
        row = quotes.get(sym) or {}
        gq = {}
        if fu.f(row, "close") is not None:
            gq["05. price"] = str(fu.f(row, "close"))
        if row.get("change_percent") not in (None, ""):
            gq["10. change percent"] = str(row.get("change_percent")).rstrip("%") + "%"
        R = {"etf": probe, "q": ({"Global Quote": gq} if gq else None), "px": px,
             "div": div, "search": search, "news": None}
        etf_Ds[sym] = fu.build_etf(sym, R)
        monthly[sym] = _monthly_from_daily(px)
    if etf_Ds:
        etf_Ds = {s: etf_Ds[s] for s in symbols if s in etf_Ds}    # arg order preserved
    return cols, monthly, any_deep, (etf_Ds or None)


# ----------------- CLI -----------------
def main(argv=None):
    ap = argparse.ArgumentParser(description="Compare 2-8 companies side by side (Alpha Vantage)")
    ap.add_argument("symbols", nargs="+", help="2-8 tickers, e.g. AAPL MSFT NVDA")
    ap.add_argument("--deep", action="store_true",
                    help="add the statement-derived rows (FCF/dilution/net cash/F-Score/Altman) — "
                         "the full fundamentals gather per name (~13 calls each)")
    ap.add_argument("--days", type=int, default=365, help="rebased-performance window (default 365)")
    ap.add_argument("--cache", metavar="DIR",
                    help="per-ticker fundamentals payload cache (the --save-raw format, DIR/<SYM>/): "
                         "cached names join OFFLINE with deep columns; --deep fetches save back here")
    ap.add_argument("--html", help="write the self-contained comparison dashboard here")
    ap.add_argument("--json", action="store_true", help="emit the comparison dict instead of text")
    ap.add_argument("--open", action="store_true", help="open the --html file in the default browser")
    ap.add_argument("--env-file")
    a = ap.parse_args(argv)
    syms = list(dict.fromkeys(s.strip().upper() for s in a.symbols if s.strip()))
    # cap = 8: the dashboard palette has exactly 8 colours — a 9th series recycles them and the
    # rebased/bubble charts stop being readable (2-4 is the sweet spot for the comparative read)
    if not 2 <= len(syms) <= 8:
        ap.error(f"compare takes 2-8 distinct symbols (got {len(syms)}; 2-4 reads best)")
    if a.days <= 0:
        ap.error("--days must be positive")
    if a.open and not a.html:
        print("warning: --open needs --html (there is no file to open) — ignored", file=sys.stderr)
    cols, monthly, deep, etf_Ds = gather_compare(syms, deep=a.deep, cache=a.cache, env_file=a.env_file)
    if etf_Ds:
        if a.deep:
            print("warning: --deep is company-only (funds have no statements) — ignored", file=sys.stderr)
        C = build_etf_compare(list(etf_Ds.values()), monthly, days=a.days)
    else:
        C = build_compare(cols, monthly, days=a.days, deep=deep)
    if a.html:
        # a comparison's natural document is the live multiples model or the landscape deck
        # (the V-vs-MA session 2026-07-23 offered neither, in prose or card)
        dashboard.deliver_html(a.html, dashboard.render_interactive_html(to_spec(C)),
                               offers=["/financial-analysis:comps " + " ".join(syms),
                                       "/financial-analysis:competitive-analysis " + " ".join(syms)])
        print(f"{a.html}  |  {' vs '.join(syms)} comparison dashboard")
        if a.open:
            webbrowser.open(pathlib.Path(os.path.abspath(a.html)).as_uri())
    elif a.json:
        print(json.dumps(C, separators=(",", ":")))
    else:
        print(render_text(C))


if __name__ == "__main__":
    main()
