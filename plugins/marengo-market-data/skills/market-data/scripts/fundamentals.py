#!/usr/bin/env python3
"""
fundamentals.py — fetch + analyze a company's fundamentals from Alpha Vantage,
end to end, and emit a compact summary. Pure stdlib (urllib), no pip installs.

The point (same as chart.py): the heavy AV payloads (income/balance/cash/earnings/
news each overflow the MCP token limit) are fetched DIRECTLY over HTTP and reduced
to ~30 numbers inside this script — so a company analysis costs one command and a
small summary instead of dumping statements into context and hand-writing jq.

Outputs (pick with flags):
  (default)   text summary to stdout — snapshot, revenue/margin/FCF trend, balance
              strength, capital return, EPS-surprise streak, analyst view, sentiment.
  --json      the same, as compact JSON (feed it to a native dashboard widget).
  --svg PATH  a self-contained SVG dashboard (portable: any browser, Home, files).

Usage:
  python3 fundamentals.py NVDA
  python3 fundamentals.py NVDA --svg nvda.svg
  python3 fundamentals.py NVDA --json
  python3 fundamentals.py NVDA --no-news        # skip the news-sentiment fetch

Key is read from .env (skill folder or cwd) as ALPHAVANTAGE_API_KEY, or the env.
Never pass the key on the command line or paste it into chat.
"""
from __future__ import annotations
import argparse, concurrent.futures, datetime as dt, http.client, json, os, pathlib, re, sys, urllib.parse, urllib.request, webbrowser
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dashboard  # Ф7: fundamentals builds a SPEC; dashboard.py is the one renderer


def esc(s):  # XML-escape SVG text (company names like "AT&T", titles with "&")
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

UP, NI, BLUE, TEAL, GRA, INK, AMBER, RED = "#9FE1CB", "#0F6E56", "#378ADD", "#1D9E75", "#8a8f98", "#1f2328", "#EFB366", "#E24B4A"


def get_key(env_file=None):
    if os.environ.get("ALPHAVANTAGE_API_KEY"):
        return os.environ["ALPHAVANTAGE_API_KEY"]
    here = os.path.dirname(os.path.abspath(__file__))
    for path in ([env_file] if env_file else []) + [".env", os.path.join(here, ".env"), os.path.join(here, os.pardir, ".env")]:
        if path and os.path.exists(path):
            for ln in open(path, encoding="utf-8"):
                m = re.match(r'\s*ALPHAVANTAGE_API_KEY\s*=\s*(.+)', ln)
                if m:
                    return m.group(1).strip()
    sys.exit("error: ALPHAVANTAGE_API_KEY not found (set env or a .env beside SKILL.md)")


def av(params, key, _net_retried=False, _burst_retried=False):
    url = "https://www.alphavantage.co/query?" + urllib.parse.urlencode(dict(params, apikey=key))
    try:
        # gzip (v1.84, probed 2026-07-13): the 3 heavy payloads (daily-full + insider + 13F) are
        # 6.5MB raw but 413KB gzipped — uncompressed they CONTEND for bandwidth inside the
        # concurrent fan-out (wall 3s -> 13s). AV honors Accept-Encoding on every probed endpoint.
        req = urllib.request.Request(url, headers={"Accept-Encoding": "gzip"})
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read()
            enc = ""
            try:
                enc = (r.headers.get("Content-Encoding") or "").lower()
            except AttributeError:
                pass   # test stubs / exotic responses without headers -> treat as plain
            if enc == "gzip":
                import gzip as _gz
                body = _gz.decompress(body)
            d = json.loads(body)
    # outage net (review 2026-07-04): HTML-over-200 (ValueError) and a mid-body drop
    # (IncompleteRead = HTTPException, NOT OSError) are the two most common real failures
    except (TimeoutError, urllib.error.URLError, OSError, http.client.HTTPException, ValueError,
            EOFError) as e:   # EOFError = a TRUNCATED gzip body (review P3-4) — an outage, not a crash
        if not _net_retried:                    # one endpoint hanging must not kill the 9-call gather
            import time as _t
            _t.sleep(1.5)
            return av(params, key, _net_retried=True, _burst_retried=_burst_retried)
        sys.exit(f"error: Alpha Vantage {params.get('function')} timed out / unreachable "
                 f"after a retry ({type(e).__name__}) — their side; try again in a minute")
    if (isinstance(d, dict) and not _burst_retried
            and "burst" in str(d.get("Information", "")).lower()):
        import time as _t                       # 10 concurrent calls can trip AV's burst detector;
        _t.sleep(2.0)                           # the flag is SEPARATE from the net retry — a timeout
        return av(params, key, _net_retried=_net_retried, _burst_retried=True)   # retry often lands IN a burst
    if isinstance(d, dict) and ("Note" in d or "Information" in d) and len(d) == 1:
        # throttle != bad ticker: name the limits + this script's fan-out so the fix is obvious
        sys.exit("error: Alpha Vantage throttled (free tier = 25 req/day · 5/min; fundamentals fans out "
                 "8-9 staggered calls — premium recommended, or space the runs): " + str(d)[:120])
    return d


def f(d, k):  # None-safe float ("None" strings are common in AV)
    v = (d or {}).get(k)
    try:
        return float(v) if v not in (None, "None", "") else None
    except (TypeError, ValueError):
        return None


REL_MIN = 0.75   # per-ticker relevance gate for news (headlines list AND the mood aggregate)


RAW_NAMES = {  # gather(save_dir=...) file names — MUST match read_from_dir's find() keywords
    "ov": "overview.json", "q": "global_quote.json", "inc": "income.json", "bal": "balance.json",
    "cf": "cashflow.json", "earn": "earnings.json", "est": "estimates.json",
    "px": "daily_adjusted.json", "news": "news_sentiment.json",
    "etf": "etf_profile.json", "cal": "earnings_calendar.json", "div": "dividends.json",
    "search": "symbol_search.json", "pxi": "intraday.json",
    "ins": "insider_transactions.json", "inst": "institutional_holdings.json",
    "sec": "sector_spdr.json"}   # v1.148: {etf, closes} — the sector-SPDR series the RS tile compares against


def _bg_av(params, key):
    """Best-effort fetch for the OPTIONAL heavy ownership payloads (v1.84): a throttle/entitlement
    sys.exit or any error degrades to None + ONE stderr warning — a 2.5MB optional payload must
    never kill the core dashboard (the 10 core jobs keep av()'s die-loud behavior)."""
    try:
        return av(params, key)
    except (SystemExit, Exception) as e:
        print(f"warning: {params.get('function')} unavailable ({str(e)[:60]}) — "
              f"the ownership block is skipped", file=sys.stderr)
        return None


def _fetch_next_earnings(symbol, key):
    """Next scheduled report date from EARNINGS_CALENDAR (CSV-only — av() is JSON, so a dedicated
    fetch). Best-effort: any failure -> None (the card/line just doesn't appear). -> {'next': date|None}."""
    try:
        u = "https://www.alphavantage.co/query?" + urllib.parse.urlencode(
            {"function": "EARNINGS_CALENDAR", "symbol": symbol, "horizon": "3month", "apikey": key})
        with urllib.request.urlopen(u, timeout=20) as r:
            rows = [ln for ln in r.read().decode().strip().splitlines()[1:] if ln.strip()]
        if rows:
            import csv as _csv                # symbol,name,reportDate,fiscalDateEnding,estimate,currency —
            p = next(_csv.reader([rows[0]]))  # csv-parsed (v1.126): a quoted "Name, Inc." comma shifted
            if len(p) > 2 and len(p[2]) == 10:  # the columns and silently dropped the card
                return {"next": p[2]}
    except Exception:
        pass
    return {"next": None}


def gather(symbol, key, with_news=True, save_dir=None):
    # Cowork/agent-mode sandboxes MAY block egress: a direct fetch burns ~60s of timeouts
    # before failing. Fast-fail with the working route instead (Palantir session lesson).
    # =auto probes first, so an OPEN-egress sandbox falls through and fetches directly.
    dashboard.check_no_net("Route: pull the 9 endpoints via the Alpha Vantage MCP connector "
                           "(return_full_data=true), then render offline with --from-dir "
                           "<dir of tool-result files>; see references/fundamentals.md "
                           "(Cowork playbook)")
    # fetch all endpoints CONCURRENTLY — wall time ≈ the slowest single call, not the
    # sum of 6-7 sequential round-trips (the dominant cost of a fundamentals pull).
    jobs = {
        # v1.84 Ownership: the two HEAVY payloads (measured 2026-07-13: INSIDER 2.7s/2.5MB,
        # INSTITUTIONAL 1.6s/1.8MB) go FIRST so their longer transfer overlaps the rest of the
        # fan-out (wall stays ≈ the slowest call). Fetched BEST-EFFORT (_bg_av) — optional data.
        "ins": {"function": "INSIDER_TRANSACTIONS", "symbol": symbol},
        "inst": {"function": "INSTITUTIONAL_HOLDINGS", "symbol": symbol},
        "ov": {"function": "OVERVIEW", "symbol": symbol},
        "q": {"function": "GLOBAL_QUOTE", "symbol": symbol, "entitlement": "realtime"},
        "inc": {"function": "INCOME_STATEMENT", "symbol": symbol},
        "bal": {"function": "BALANCE_SHEET", "symbol": symbol},
        "cf": {"function": "CASH_FLOW", "symbol": symbol},
        "earn": {"function": "EARNINGS", "symbol": symbol},
        "est": {"function": "EARNINGS_ESTIMATES", "symbol": symbol},
        "px": {"function": "TIME_SERIES_DAILY_ADJUSTED", "symbol": symbol, "outputsize": "full"},
        # v1.83.0: ETF_PROFILE so an ETF/fund renders instead of exiting 'no OVERVIEW' — concurrent,
        # so ~0 wall cost; a company's ETF_PROFILE is empty and simply ignored by build().
        "etf": {"function": "ETF_PROFILE", "symbol": symbol},
        # v1.96.0: distribution history for the ETF page (companies get theirs from cash flow);
        # concurrent -> ~0 wall cost, unused by the company build
        "div": {"function": "DIVIDENDS", "symbol": symbol},
        # v1.97.0: fund NAME for the ETF page (ETF_PROFILE carries none); best-effort — an
        # identity miss must never kill the page. Companies ignore it (OVERVIEW has Name).
        "search": {"function": "SYMBOL_SEARCH", "keywords": symbol},
        # v1.100.0: intraday kline frames (15m + local 60m); best-effort — a throttle/entitlement
        # miss keeps the D/W/M chart, never kills the page
        "pxi": {"function": "TIME_SERIES_INTRADAY", "symbol": symbol, "interval": "15min",
                "outputsize": "full", "entitlement": "realtime"},
    }
    if with_news:
        tf = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=14)).strftime("%Y%m%dT%H%M")
        jobs["news"] = {"function": "NEWS_SENTIMENT", "tickers": symbol, "time_from": tf, "limit": 50, "sort": "LATEST"}
    import time as _t
    # +2 workers: the core jobs + the EARNINGS_CALENDAR future + the optional sector-SPDR pull (which is
    # submitted mid-flight once OVERVIEW names the sector) — sizing for +1 queued the sector behind a
    # core job in the worst case (review P3).
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs) + 2) as ex:
        futs = {}
        for k, p in jobs.items():               # stagger the fan-out: ~12 calls over ~1.8s stays
            # the two optional heavy payloads ride the best-effort wrapper; core jobs die loud
            futs[k] = ex.submit(_bg_av if k in ("ins", "inst", "search", "pxi") else av, p, key)
            _t.sleep(0.15)
        cal_fut = ex.submit(_fetch_next_earnings, symbol, key)   # v1.83.0 next-report date (CSV endpoint)
        # v1.148 sector context: WHICH SPDR to compare against is only known once OVERVIEW returns
        # (it carries the sector) — so resolve OVERVIEW first and submit the SPDR pull into the SAME
        # live executor, where it overlaps the rest of the fan-out (review P3: a sequential pull
        # after the fan-out would add a full AV round-trip — up to ~3.5s with av()'s retries — before
        # the page exists, against the shipped present-first rule). Day-cached per (SPDR, date), so
        # the first company of a sector that day pays for it and everyone else that day reads disk.
        _ovd = futs["ov"].result()
        _ovd = _ovd if isinstance(_ovd, dict) else {}
        _setf = _sector_etf(_ovd.get("Sector"))
        if _setf:
            _t.sleep(0.15)   # the sector pull joins the SAME staggered cadence as the core jobs: an
            #                  unstaggered extra request raises the burst risk for CORE calls, and a
            #                  throttled core call sys.exit()s the whole page (review P3)
        _sec_fut = ex.submit(fetch_sector_closes, _setf, key) if _setf else None
        R = {k: futs[k].result() for k in futs}
        R["cal"] = cal_fut.result()
        if _sec_fut is not None:                      # best-effort: a miss just drops the tile
            try:
                _scl = _sec_fut.result()
            except (SystemExit, Exception) as _e:     # nothing from an OPTIONAL tile may kill the page
                print("warning: sector context unavailable (%s) — the tile is skipped" % str(_e)[:60],
                      file=sys.stderr)
                _scl = None
            R["sec"] = {"etf": _setf, "closes": _scl} if _scl else None
    if save_dir:
        # cache the raw payloads: every later render of this ticker is a FREE offline
        # --from-dir re-render (<0.1s, 0 AV calls) instead of a 2.7s 9-call refetch.
        # The cache is ANCILLARY: a write failure warns and never wastes the metered fetch.
        try:
            os.makedirs(save_dir, exist_ok=True)
            for k, payload in R.items():
                # skip the v1.83.0 side-payloads when empty so a COMPANY cache stays lean (no stray
                # etf_profile/earnings_calendar files): only persist an ETF profile with data / a real date
                if k == "etf" and not _is_etf_profile(payload):
                    continue
                if k == "cal" and not (payload or {}).get("next"):
                    continue
                # v1.84: keep a company cache lean — persist the heavy ownership payloads only
                # when they actually carry rows (an ETF/rejected name gets a `message` dict)
                if k == "ins" and not (isinstance(payload, dict) and payload.get("data")):
                    continue
                if k == "inst" and not (isinstance(payload, dict) and payload.get("holdings")):
                    continue
                if payload is not None:
                    _tmp = os.path.join(save_dir, RAW_NAMES[k] + ".tmp")
                    with open(_tmp, "w", encoding="utf-8") as fh:
                        json.dump(payload, fh)
                    os.replace(_tmp, os.path.join(save_dir, RAW_NAMES[k]))   # atomic: no truncated cache files
        except OSError as e:
            print(f"warning: --save-raw could not write {save_dir} ({e}); rendering anyway", file=sys.stderr)
    return build(symbol, R)


def _norm_quote(raw):
    """Normalize a GLOBAL_QUOTE payload to the REST {'Global Quote': {...}} shape — the
    AV MCP returns it as a CSV string under 'result' instead of the REST object."""
    import csv as _csv
    import io as _io
    if not raw:
        return {}
    if "Global Quote" in raw:
        return raw
    if isinstance(raw.get("result"), str) and "," in raw["result"]:
        rows = list(_csv.DictReader(_io.StringIO(raw["result"])))
        if rows:
            r = rows[0]
            return {"Global Quote": {"05. price": r.get("price"), "10. change percent": r.get("changePercent")}}
    if "05. price" in raw:
        return {"Global Quote": raw}
    return {}


def _norm_px(raw):
    """Normalize a daily price payload to the REST {'Time Series (Daily)': {...}} shape.
    The AV MCP connector returns TIME_SERIES_DAILY_ADJUSTED as CSV under 'result' (real
    Cowork session 2026-07-04: header 'timestamp,open,high,low,close,adjusted_close,volume,
    dividend_amount,split_coefficient') — the manual reshaping of exactly this payload was
    the Cowork path's biggest time sink."""
    import csv as _csv
    import io as _io
    if not isinstance(raw, dict):
        return {}
    if "Time Series (Daily)" in raw:
        return raw
    res = raw.get("result")
    if isinstance(res, str) and res.lstrip().startswith("{"):
        try:
            res_d = json.loads(res)
        except ValueError:
            return {}
        return res_d if isinstance(res_d, dict) and "Time Series (Daily)" in res_d else {}
    if isinstance(res, str) and res:
        head = res.splitlines()[0]
        if "timestamp" in head and "," in head:
            colmap = {"open": "1. open", "high": "2. high", "low": "3. low", "close": "4. close",
                      "adjusted_close": "5. adjusted close", "volume": "6. volume",
                      "dividend_amount": "7. dividend amount", "split_coefficient": "8. split coefficient"}
            ts = {}
            for row in _csv.DictReader(_io.StringIO(res)):
                day = row.get("timestamp")
                if day:
                    ts[day] = {v: row[k] for k, v in colmap.items() if row.get(k) not in (None, "")}
            return {"Time Series (Daily)": ts} if ts else {}
    return {}


def read_from_dir(symbol, d):
    """MCP-fallback for egress-blocked sandboxes (the chart.py --from analogue): assemble
    R from a directory of saved AV results instead of fetching over HTTP. Each endpoint's
    file is found by a substring in its name, so the harness's overflow tool-result files
    can be dropped in as-is. Save each AV MCP result (use return_full_data=true) to a file
    whose name contains the endpoint word: overview, (global_)quote, income, balance, cash,
    earnings, news."""
    import glob
    files = [fp for fp in glob.glob(os.path.join(d, "*")) if os.path.isfile(fp)]

    _by_name = {os.path.basename(fp): fp for fp in files}

    def find(*keys, exact=None, exclude=()):
        # our own --save-raw names take precedence over stale MCP dumps in the same dir
        # (an uppercase OVERVIEW_dump.json sorts BEFORE overview.json — review 2026-07-04).
        # `exclude` skips names containing any listed substring (review 2026-07-13: 'earnings_calendar'
        # contains 'earning' and would be mis-picked by the quarterly `earn` find).
        if exact and exact in _by_name:
            try:
                return json.load(open(_by_name[exact], encoding="utf-8"))
            except (ValueError, OSError):
                pass
        for fp in sorted(files):
            base = os.path.basename(fp).lower()
            if any(x in base for x in exclude):
                continue
            if any(k in base for k in keys):
                try:
                    return json.load(open(fp, encoding="utf-8"))
                except (ValueError, OSError):
                    continue
        return None

    ov = find("overview", exact=RAW_NAMES["ov"])
    etf = find("etf_profile", "etf", exact=RAW_NAMES["etf"])
    # An ETF/fund returns an EMPTY OVERVIEW — accept the dir if ETF_PROFILE is present (v1.83.0).
    # A genuinely bad ticker has neither → the original honest exit.
    if not ov or "Symbol" not in ov:
        if _is_etf_profile(etf):
            rawq = find("global_quote", "quote", exact=RAW_NAMES["q"])
            # ETF_PROFILE carries no Symbol, so the company path's dir-mismatch guard can't fire here.
            # Cross-check the QUOTE's own symbol when present, so a wrong-ticker dir is still refused
            # (review 2026-07-13) — best-effort: only a DEFINITE mismatch exits.
            qsym = ((rawq or {}).get("Global Quote", {}).get("01. symbol")
                    if isinstance(rawq, dict) else None)
            if qsym and str(qsym).upper() != str(symbol).upper():
                sys.exit(f"--from-dir {d}: this cache's quote is {qsym} but {symbol} was requested "
                         f"— wrong directory? (per-ticker dirs: .avcache/<SYMBOL>)")
            return {"ov": {}, "etf": etf, "q": _norm_quote(rawq),
                    "px": _norm_px(find("daily", "monthly", "price_hist", "time_series", exact=RAW_NAMES["px"])),
                    "cal": find("earnings_calendar", "calendar", exact=RAW_NAMES["cal"]),
                    # v1.96: the ETF page renders news + distributions too
                    "news": find("news", "sentiment", exact=RAW_NAMES["news"]),
                    "div": find("dividend", exact=RAW_NAMES["div"]),
                    # v1.97: the fund NAME (SYMBOL_SEARCH — ETF_PROFILE carries none)
                    "search": find("search", exact=RAW_NAMES["search"]),
                    # v1.100: intraday kline frames
                    "pxi": find("intraday", exact=RAW_NAMES["pxi"])}
        sys.exit(f"--from-dir {d}: need a readable OVERVIEW json (a filename containing 'overview'), "
                 f"or an ETF_PROFILE json for a fund")
    if str(ov.get("Symbol", "")).upper() != str(symbol).upper():
        sys.exit(f"--from-dir {d}: this cache belongs to {ov.get('Symbol')} but {symbol} was requested "
                 f"— wrong directory? (per-ticker dirs: .avcache/<SYMBOL>)")
    return {"ov": ov, "q": _norm_quote(find("global_quote", "quote", exact=RAW_NAMES["q"])),
            "inc": find("income", exact=RAW_NAMES["inc"]), "bal": find("balance", exact=RAW_NAMES["bal"]),
            "cf": find("cash", exact=RAW_NAMES["cf"]),
            "earn": find("earnings.", "earning", exact=RAW_NAMES["earn"], exclude=("calendar",)),
            "est": find("estimat", exact=RAW_NAMES["est"]),
            "px": _norm_px(find("daily", "monthly", "price_hist", "time_series", exact=RAW_NAMES["px"])),
            "news": find("news", "sentiment", exact=RAW_NAMES["news"]),
            "etf": etf, "cal": find("earnings_calendar", "calendar", exact=RAW_NAMES["cal"]),
            "ins": find("insider", exact=RAW_NAMES["ins"]),
            "inst": find("institutional", "holders", "13f", exact=RAW_NAMES["inst"], exclude=("insider",)),
            "pxi": find("intraday", exact=RAW_NAMES["pxi"]),   # v1.100: intraday kline frames
            "sec": find("sector_spdr", exact=RAW_NAMES["sec"])}   # v1.148: the sector-SPDR closes (offline replay)


# statement fields where a missing value means UNKNOWN (propagate None), vs. fields where
# absence genuinely means zero (a company that pays no dividend simply has no line)
_TTM_ZERO_OK = {"stockBasedCompensation", "dividendPayout", "proceedsFromRepurchaseOfEquity"}


def _q4_span_ok(rows4):
    """4 quarterly reports form a TTM only if newest->oldest fiscalDateEnding spans ~3
    quarters (<=330d; consecutive quarters ~274d, one MISSING quarter ~366d)."""
    try:
        dn = dt.date.fromisoformat(str(rows4[0].get("fiscalDateEnding")))
        do = dt.date.fromisoformat(str(rows4[3].get("fiscalDateEnding")))
    except (ValueError, TypeError):
        return False
    return (dn - do).days <= 330


def _ttm_windows(rows, fields, n_windows=None):
    """Rolling 4-quarter sums over quarterly reports (rows newest-first) ->
    list of (end_date, {field: sum-or-None}) oldest-first. A None in any window quarter
    poisons that window's value for strict fields (review 2026-07-04: or-0 silently turned
    a missing netIncome into a confidently wrong TTM margin)."""
    out = []
    cap = max(0, len(rows) - 3) if n_windows is None else min(n_windows, max(0, len(rows) - 3))
    for i in range(cap):
        win = rows[i:i + 4]
        ok = _q4_span_ok(win)               # a hole makes it a 15-month "TTM" — poison the window
        vals = {}
        for k in fields:
            xs = [f(r, k) for r in win]
            if not ok:
                vals[k] = None
            elif k in _TTM_ZERO_OK:
                vals[k] = sum(x or 0 for x in xs)
            else:
                vals[k] = None if any(x is None for x in xs) else sum(xs)
        out.append((str(win[0].get("fiscalDateEnding", ""))[:7], vals))
    return list(reversed(out))


def _build_price_ohlc(R):
    """Split-adjusted OHLCV frames (D/W/M) from a TIME_SERIES_DAILY_ADJUSTED payload — shared by
    the company build and the ETF branch (both draw the same kline). None when there's no series.
    SPLIT-ONLY adjustment (review 2026-07-04): adjClose/close also folds DIVIDENDS in, which
    redraws old candles below their traded range on long histories. Walk newest->oldest accumulating
    '8. split coefficient'; prices BEFORE a split divide by the cumulative factor, volume multiplies."""
    ts = (R.get("px") or {}).get("Time Series (Daily)") or {}
    _days = []
    cum = 1.0
    rev_rows = []
    for k, v in sorted(ts.items(), reverse=True):
        c0 = f(v, "4. close")
        sc = f(v, "8. split coefficient") or 1.0
        if not c0:
            if sc != 1.0:
                cum *= sc                    # a coefficient on a junk/halted row still applies to older rows
            continue
        rev_rows.append((k, v, cum))
        if sc != 1.0:
            cum *= sc                        # rows OLDER than this day carry the bigger divisor
    for k, v, fac in reversed(rev_rows):
        c0 = f(v, "4. close")
        _days.append((k, round((f(v, "1. open") or c0) / fac, 2), round((f(v, "2. high") or c0) / fac, 2),
                      round((f(v, "3. low") or c0) / fac, 2), round(c0 / fac, 2),
                      int((f(v, "6. volume") or 0) * fac)))

    def _ms(d_):
        return int(dt.datetime.fromisoformat(d_ + "T00:00:00+00:00").timestamp() * 1000)

    def _bucket(key):
        agg, first = {}, {}
        for d_, o_, h_, l_, c_, v_ in _days:
            kk = key(d_)
            if kk not in agg:
                agg[kk] = [o_, h_, l_, c_, v_]; first[kk] = d_       # bucket's first trading day -> ts
            else:
                a = agg[kk]; a[1] = max(a[1], h_); a[2] = min(a[2], l_); a[3] = c_; a[4] += v_
        ks = sorted(agg)
        return {"labels": ks, "ts": [_ms(first[k]) for k in ks],
                "o": [agg[k][0] for k in ks], "h": [agg[k][1] for k in ks],
                "l": [agg[k][2] for k in ks], "c": [agg[k][3] for k in ks], "v": [agg[k][4] for k in ks]}

    def _iso_week(d_):
        y, wk, _ = dt.date.fromisoformat(d_).isocalendar()
        return f"{y}-{wk:02d}"
    if not _days:
        return None
    _dd = _days[-756:]                                              # ~3y of daily candles
    _fr_d = {"labels": [d_[5:] for d_, *_ in _dd], "ts": [_ms(d_) for d_, *_ in _dd],
             "o": [x[1] for x in _dd], "h": [x[2] for x in _dd],
             "l": [x[3] for x in _dd], "c": [x[4] for x in _dd], "v": [x[5] for x in _dd],
             "zoom": {"def": 120, "step": 60, "min": 30}}
    _fr_w = _bucket(_iso_week); _fr_w["zoom"] = {"def": 104, "step": 52, "min": 26}
    _fr_m = _bucket(lambda d_: d_[:7]); _fr_m["zoom"] = {"def": 60, "step": 24, "min": 12}
    _maxc = max((x[4] for x in _days), default=0)
    _pprec = 0 if _maxc >= 100 else (2 if _maxc >= 10 else 3)       # drop the ".00" that eats axis width

    # v1.100: INTRADAY frames from ONE TIME_SERIES_INTRADAY 15min pull (~30 trailing days,
    # UNadjusted — a split inside the window would step; acceptable at that depth). "60m" is
    # bucketed locally like W/M from D. AV stamps intraday in the exchange TZ (meta
    # '6. Time Zone') — epoch via zoneinfo; a tzdata-less platform (bare Windows) falls back
    # to fixed UTC-5: candle SPACING stays right, clock labels may shift 1h around DST.
    frames = {}
    pxi = R.get("pxi") or {}
    ikey = next((k for k in pxi if str(k).startswith("Time Series")), None)
    if ikey:
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo((pxi.get("Meta Data") or {}).get("6. Time Zone") or "US/Eastern")
        except Exception:
            tz = dt.timezone(dt.timedelta(hours=-5))
        irows = []
        for ts_, v in sorted((pxi.get(ikey) or {}).items()):
            c0 = f(v, "4. close")
            if not c0:
                continue
            try:
                ms = int(dt.datetime.fromisoformat(str(ts_)).replace(tzinfo=tz).timestamp() * 1000)
            except ValueError:
                continue                     # a junk timestamp must not kill the whole chart
            irows.append((str(ts_), ms, f(v, "1. open") or c0, f(v, "2. high") or c0,
                          f(v, "3. low") or c0, c0, int(f(v, "5. volume") or 0)))
        if irows:
            frames["15m"] = {"labels": [r[0][5:16] for r in irows], "ts": [r[1] for r in irows],
                             "o": [r[2] for r in irows], "h": [r[3] for r in irows],
                             "l": [r[4] for r in irows], "c": [r[5] for r in irows],
                             "v": [r[6] for r in irows],
                             "zoom": {"def": 130, "step": 65, "min": 26}}
            hagg, hfirst = {}, {}
            for ts_, ms, o_, h_, l_, c_, v_ in irows:
                kk = ts_[:13]               # "YYYY-MM-DD HH" — the trading hour
                if kk not in hagg:
                    hagg[kk] = [o_, h_, l_, c_, v_]; hfirst[kk] = ms
                else:
                    a = hagg[kk]; a[1] = max(a[1], h_); a[2] = min(a[2], l_); a[3] = c_; a[4] += v_
            hks = sorted(hagg)
            frames["60m"] = {"labels": [k[5:] for k in hks], "ts": [hfirst[k] for k in hks],
                             "o": [hagg[k][0] for k in hks], "h": [hagg[k][1] for k in hks],
                             "l": [hagg[k][2] for k in hks], "c": [hagg[k][3] for k in hks],
                             "v": [hagg[k][4] for k in hks],
                             "zoom": {"def": 84, "step": 42, "min": 21}}
    frames.update({"d": _fr_d, "w": _fr_w, "m": _fr_m})
    # default frame by data density: <8 monthly candles (fresh IPO) -> a near-empty M view;
    # start on D instead (SPCX probe 2026-07-04: IPO+3wk = 2 monthly candles)
    return {"frame": "m" if len(_fr_m["ts"]) >= 8 else "d", "pricePrec": _pprec,
            "frames": frames}


# ================= v1.84.0 Phase A — Ownership data layer (insider + 13F reduces) =================
# Design of record: docs/ownership-tabs-compare-plan.md (+ the two 2026-07-13 design-agent specs).
# Phase A adds ONLY the D slots (D['insiders'], D['inst']); the cards/panels/text are Phase B.

def _pi(x):
    """Int-ish string -> float or None ('1426283914', '-9970306'; commas tolerated). 13F numerics
    arrive as strings."""
    try:
        return float(str(x).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _pp(x):
    """'75%' / '1.92%' / '-0.86%' -> float or None; 'n/a'/'' -> None (a hole, never 0)."""
    s = str(x or "").strip().rstrip("%")
    try:
        return float(s)
    except ValueError:
        return None


def _title_tier(title):
    """Executive-title tier: 1 = C-suite (CEO/CFO/COO/President-not-Vice/Chairman-not-Vice),
    3 = director/10% owner/trustee, 2 = everything else incl. unknown. Highest tier wins."""
    t = " " + (title or "").upper() + " "
    if ("CHIEF EXECUTIVE" in t or " CEO" in t or "CHIEF FINANCIAL" in t or " CFO" in t
            or "CHIEF OPERATING" in t or " COO" in t
            or (("PRESIDENT" in t or "CHAIRMAN" in t) and "VICE" not in t)):
        return 1
    if any(k in t for k in ("CHIEF", "OFFICER", "VICE PRESIDENT", " VP", "EVP", "SVP",
                            "GENERAL COUNSEL", "TREASURER", "SECRETARY", "PRINCIPAL ACCOUNTING")):
        return 2
    if any(k in t for k in ("DIRECTOR", "10%", "BENEFICIAL OWNER", "TRUSTEE")):
        return 3
    return 2


_RECENT_CAP = 300   # v1.103 safety cap on the 12m insider-event list (runaway feeds; cut disclosed)

# ---- v1.107: last-known-good cache (AV's estimates feed blinks EMPTY intra-day — MU 2026-07-15) ----
_LKG_DAYS = 7        # estimates move in weeks: an hours-old cache is honest, a month-old one is a lie
_LKG_BASE = None     # test seam — suites point this at a tempdir so they never touch the real store


def _lkg_path(kind, symbol):
    """store/fundamentals sibling of the opthist store (same resolution ladder root — rides the
    Cowork snapshot sync for free); ~/.cache fallback mirrors opthist's."""
    base = _LKG_BASE
    if base is None:
        try:
            import opthist
            base = os.path.join(os.path.dirname(opthist.CACHE_DIR), "fundamentals")
        except Exception:
            base = os.path.expanduser("~/.cache/market-data/fundamentals")
    return os.path.join(base, f"{symbol.upper()}.{kind}.json")


def _lkg_save(kind, symbol, payload):
    """Best-effort last-known-good write — a cache failure must never break a render."""
    try:
        p = _lkg_path(kind, symbol)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p + ".tmp", "w", encoding="utf-8") as fh:
            json.dump({"asOf": dt.date.today().isoformat(), "payload": payload}, fh)
        os.replace(p + ".tmp", p)
    except Exception:
        pass


def _lkg_load(kind, symbol, max_days=_LKG_DAYS):
    """(payload, asOf-date) from the last good pull, or (None, None) — absent, stale (> max_days)
    and corrupt files all degrade silently; the CALLER stamps the date wherever cache is shown."""
    try:
        with open(_lkg_path(kind, symbol), encoding="utf-8") as fh:
            d = json.load(fh)
        as_of = str(d.get("asOf") or "")
        age = (dt.date.today() - dt.date.fromisoformat(as_of)).days
        if 0 <= age <= max_days:
            return d.get("payload"), as_of
    except Exception:
        pass
    return None, None


def _reduce_insiders(R):
    """INSIDER_TRANSACTIONS (~7k rows, 2.5MB) -> compact aggregates, or None (absent/empty/
    entitlement payload — the block just doesn't render). AV carries NO SEC Form-4 codes, so
    classes are heuristic: `buy` = priced-A within 15% of that day's close (closes come FREE from
    the already-fetched daily series; <=5d walk-back for non-trading dates); `exercise` = priced-A
    far below it; a priced-D offset by same-day unpriced-A shares of the same executive = likely
    sell-to-cover (`stc` — stays IN the $ flow, OUT of the discretionary counts); unpriced rows =
    grants/vests (compensation plumbing, excluded from $ flow). The $ signal counts ONLY priced
    rows (user decision 2026-07-13). Windows anchor on TODAY. Never stores the raw rows in D."""
    raw = R.get("ins")
    rows = raw.get("data") if isinstance(raw, dict) else (raw if isinstance(raw, list) else None)
    if not rows or not isinstance(rows, list):
        return None
    ts = (R.get("px") or {}).get("Time Series (Daily)") or {}
    closes = {d: f(v, "4. close") for d, v in ts.items()}   # UNADJUSTED close — Form-4 prices are raw
    price_check = bool(closes)

    def close_near(dd):
        for k in range(6):   # <=5 calendar days back (weekend/holiday filings)
            c = closes.get((dd - dt.timedelta(days=k)).isoformat())
            if c:
                return c
        return None
    today = dt.date.today()
    cuts = {"3m": (today - dt.timedelta(days=91)).isoformat(),
            "6m": (today - dt.timedelta(days=182)).isoformat(),
            "12m": (today - dt.timedelta(days=365)).isoformat()}
    W = {w: {"buyUsdM": 0.0, "sellUsdM": 0.0, "omBuys": 0, "omBuyUsdM": 0.0, "stcSellUsdM": 0.0,
             "sells": 0, "discSells": 0, "t1Buys": 0, "grants": 0, "acq": 0,
             "_buyers": set(), "_sellers": set()} for w in cuts}
    # monthly buckets: the last 24 calendar months, ascending; 0 = genuinely no priced flow that month
    _midx = today.year * 12 + (today.month - 1)
    mlabels = ["%04d-%02d" % ((_midx - k) // 12, (_midx - k) % 12 + 1) for k in range(23, -1, -1)]
    mnet = {m: 0.0 for m in mlabels}
    mbuyers = {m: set() for m in mlabels}
    unprA = {}          # (executive, date) -> unpriced-A shares within 12m (the vest-pair ledger)
    pricedD = []        # 12m priced disposals, finalized in pass 2 (stc needs the full ledger)
    parsed = []         # for the recent-12 pick
    skipped = 0
    asof = None
    for r in rows:
        if not isinstance(r, dict):   # review P3-5: a junk element must not crash the whole build
            skipped += 1
            continue
        d = str(r.get("transaction_date") or "")
        try:
            dd = dt.date.fromisoformat(d)
        except ValueError:
            skipped += 1
            continue
        sh = f(r, "shares")
        ad = r.get("acquisition_or_disposal")
        if sh is None or ad not in ("A", "D"):
            skipped += 1
            continue
        px = f(r, "share_price")
        who, title = str(r.get("executive") or ""), str(r.get("executive_title") or "")
        usd = (sh * px if px else None)
        asof = d if (asof is None or d > asof) else asof
        wins = [w for w, c in cuts.items() if d >= c]
        month = d[:7]
        if px and px > 0:
            if ad == "A":
                c0 = close_near(dd) if price_check else None
                om = (not price_check) or (c0 is not None and px >= 0.85 * c0)
                # 'acq' = priced but UNVERIFIABLE (no close within the walk-back — e.g. a compact
                # --from-dir daily dump): in the $ flow, OUT of the buyer counts, DISCLOSED (review P2-2)
                cls = "buy" if om else ("exercise" if c0 is not None else "acq")
                for w in wins:
                    W[w]["buyUsdM"] += usd / 1e6
                    if om:
                        W[w]["omBuyUsdM"] += usd / 1e6
                        W[w]["_buyers"].add(who)
                if month in mnet:
                    mnet[month] += usd / 1e6
                    if om:
                        mbuyers[month].add(who)
                parsed.append({"d": d, "who": who, "title": title, "cls": cls, "stc": False,
                               "sh": sh, "px": px, "usd": usd})
            else:
                rr = {"d": d, "who": who, "title": title, "cls": "sale", "stc": False,
                      "sh": sh, "px": px, "usd": usd}
                if wins:
                    pricedD.append((who, d, sh, usd, wins, rr))
                if month in mnet:
                    mnet[month] -= usd / 1e6
                parsed.append(rr)
        else:
            if d >= cuts["12m"] and ad == "A":
                unprA[(who, d)] = unprA.get((who, d), 0.0) + sh
            parsed.append({"d": d, "who": who, "title": title,
                           "cls": ("grant" if ad == "A" else "disposal"), "stc": False,
                           "sh": sh, "px": None, "usd": None})
    if not parsed:
        return None
    # pass 2: the vest-pair (sell-to-cover) flag over the bounded 12m priced-D list. The ledger
    # DECREMENTS on each match (review P2-1: one 1,000-sh grant must not absorb two 700-sh sales),
    # and rows are marked DIRECTLY by reference (equal-size same-day sales never share a flag).
    for who, d, sh, usd, wins, rr in pricedD:
        avail = unprA.get((who, d), 0.0)
        stc = avail >= sh
        if stc:
            unprA[(who, d)] = avail - sh
            rr["stc"] = True
        for w in wins:
            W[w]["sellUsdM"] += usd / 1e6
            if stc:
                W[w]["stcSellUsdM"] += usd / 1e6
            else:
                W[w]["_sellers"].add(who)
    # v1.102: the feed fragments ONE Form-4 execution into price lots (MU live: a single EVP
    # sale shipped as 12 same-day rows — the whole "recent" table was one event). Aggregate
    # person·day·class(·stc) EVENTS: shares summed, price = $-weighted average over the priced
    # lots, lot count kept for disclosure. Window $ SUMS above stay row-summed (identical either
    # way); window COUNTS are recomputed from these events below (v1.125).
    _ev = {}
    for rr in parsed:
        k = (rr["d"], rr["who"], rr["cls"], bool(rr.get("stc")))
        e = _ev.get(k)
        if e is None:
            _ev[k] = e = {"d": rr["d"], "who": rr["who"], "title": rr["title"], "cls": rr["cls"],
                          "stc": bool(rr.get("stc")), "sh": 0.0, "usd": 0.0, "_pxSh": 0.0, "nlots": 0}
        e["sh"] += rr["sh"] or 0
        e["nlots"] += 1
        if rr.get("px"):
            e["usd"] += rr["usd"] or 0
            e["_pxSh"] += rr["sh"] or 0
    events = []
    for e in _ev.values():
        e["px"] = (round(e["usd"] / e["_pxSh"], 2) if e["_pxSh"] else None)
        e["usd"] = e["usd"] if e["_pxSh"] else None   # an all-unpriced event has no $ value, never a fake 0
        del e["_pxSh"]
        events.append(e)
    # v1.103 (user: "нужен полный список"): the FULL trailing-12m PRICED event list — the old
    # 12 cap was a v1.84 page guard for the then-raw lot rows; events are naturally bounded by
    # the window. Unpriced grant/disposal events carry no $ signal: counted, never listed.
    # A runaway feed still meets the safety cap (newest kept, cut disclosed).
    priced_ev = sorted((e for e in events if e["px"] is not None and e["d"] >= cuts["12m"]),
                       key=lambda e: (e["d"], e["usd"] or 0), reverse=True)
    unpriced_ev = sum(1 for e in events if e["px"] is None and e["d"] >= cuts["12m"])
    recent = priced_ev[:_RECENT_CAP]
    # v1.125: COUNT fields are EVENT-based — the recent table right below counts person·day·class
    # EVENTS (v1.102), while these counters ran per feed ROW (price lot): the card read
    # "12 disc. sales" beside the table's "1 event (sale ×12)" — a same-page contradiction
    # (the v1.109 spun-off follow-up). $ sums above stay row-summed (identical either way).
    for w, c in cuts.items():
        ew = [e for e in events if e["d"] >= c]
        W[w]["omBuys"] = sum(1 for e in ew if e["cls"] == "buy")
        W[w]["t1Buys"] = sum(1 for e in ew if e["cls"] == "buy" and _title_tier(e["title"]) == 1)
        W[w]["acq"] = sum(1 for e in ew if e["cls"] == "acq")
        W[w]["grants"] = sum(1 for e in ew if e["cls"] in ("grant", "disposal"))
        W[w]["sells"] = sum(1 for e in ew if e["cls"] == "sale")
        W[w]["discSells"] = sum(1 for e in ew if e["cls"] == "sale" and not e["stc"])
    windows = {}
    for w, acc in W.items():
        acc["netUsdM"] = acc["buyUsdM"] - acc["sellUsdM"]
        acc["uniqBuyers"] = len(acc.pop("_buyers"))
        acc["uniqSellers"] = len(acc.pop("_sellers"))
        windows[w] = acc
    n90 = windows["3m"]["uniqBuyers"]
    return {"asOf": asof, "nRows": len(rows), "skipped": skipped, "priceCheck": price_check,
            "windows": windows,
            "monthly": {"labels": mlabels, "netUsdM": [round(mnet[m], 4) for m in mlabels],
                        "buyers": [len(mbuyers[m]) for m in mlabels]},
            "recent": recent, "unpricedEvents": unpriced_ev,
            "recentTruncated": max(0, len(priced_ev) - _RECENT_CAP),
            "cluster": {"n90": n90, "flag": n90 >= 3}}


_IDX_STEMS = ("VANGUARD", "BLACKROCK", "STATE STREET", "GEODE", "NORTHERN TRUST",
              "BANK OF NEW YORK MELLON", "CHARLES SCHWAB INVESTMENT MANAGEMENT")


def _reduce_institutions(R, shares_float=None):
    """INSTITUTIONAL_HOLDINGS (13F, ~6k rows) -> compact aggregates, or None (ETF-rejected/absent/
    empty). Classification is BY SIGN first (the live vocabulary is FIVE change_type values incl.
    'initiated' and 'sold out'; sign-first reproduces AV's own header counts and degrades safely on
    unknown labels). The headline flow is decomposed: `netExNewShM` excludes ALL initiations because
    the largest 'new' filings are often a manager RE-FILING under a new entity (the rename artifact —
    verified live: ~85% of AAPL's raw 'accumulation'); suspects >0.5% of the total are flagged, never
    silently dropped. Staleness anchors on the MODAL last_reported quarter. Duplicate holder names are
    distinct CIK filers — never merged."""
    raw = R.get("inst")
    if not (isinstance(raw, dict) and isinstance(raw.get("holdings"), list) and raw["holdings"]):
        return None
    fc = {"inc": 0, "new": 0, "dec": 0, "exit": 0, "unch": 0}
    fs = {"inc": 0.0, "new": 0.0, "dec": 0.0, "exit": 0.0}
    parsed = []
    for r in raw["holdings"]:
        if not isinstance(r, dict):   # review P3-5: junk elements skipped, never a crash
            continue
        chg, held = _pi(r.get("shares_changed")), _pi(r.get("shares_held"))
        t = str(r.get("change_type") or "").strip().lower()
        if chg is None or chg == 0:
            b_ = "unch"
        elif chg < 0:
            b_ = "exit" if (t == "sold out" or held == 0) else "dec"
        else:
            b_ = "new" if (t == "initiated" or (held is not None and chg == held)) else "inc"
        fc[b_] += 1
        if b_ != "unch":
            fs[b_] += chg / 1e6
        parsed.append({"name": str(r.get("holder_name") or "").strip(), "held": held or 0.0,
                       "chg": chg, "chgPct": _pp(r.get("shares_changed_percentage")),
                       "typ": b_, "asof": str(r.get("last_reported") or "")})
    comp = sum(p["held"] for p in parsed)
    total = _pi(raw.get("total_institutional_shares"))
    if total is None:
        total = comp
    elif comp and abs(total - comp) / max(total, comp) > 0.01:
        print(f"warning: 13F header total ({total:.0f}) disagrees with the row sum ({comp:.0f}) "
              f">1% — using the computed sum (AV data drift?)", file=sys.stderr)
        total = comp
    if not total or total <= 0:
        return None
    import collections
    _dates = [p["asof"] for p in parsed if p["asof"]]
    mode = collections.Counter(_dates).most_common(1)[0][0] if _dates else ""
    stale = 100.0 * sum(p["held"] for p in parsed if p["asof"] and p["asof"] < mode) / total
    early = sum(1 for p in parsed if p["asof"] and p["asof"] > mode)
    renames = sorted(({"name": p["name"], "shM": round(p["chg"] / 1e6, 1)}
                      for p in parsed if p["typ"] == "new" and p["chg"] and p["chg"] > 0.005 * total),
                     key=lambda x: -x["shM"])
    live = sorted((p for p in parsed if p["held"] > 0), key=lambda p: -p["held"])   # exits (held 0) out
    top15 = [{"name": p["name"], "shM": round(p["held"] / 1e6, 1),
              "pctInst": round(100.0 * p["held"] / total, 2),
              "chgShM": (round(p["chg"] / 1e6, 1) if p["chg"] is not None else None),
              "chgPct": (None if p["typ"] == "new" else p["chgPct"]),   # 'new' has no meaningful Δ%
              "type": p["typ"], "asof": p["asof"],
              "idx": any(s in p["name"].upper() for s in _IDX_STEMS)} for p in live[:15]]
    hhi = sum((p["held"] / total) ** 2 for p in live)
    pf = (100.0 * total / shares_float) if (shares_float and shares_float > 0) else None
    if pf is not None and pf > 120:   # stale double-count guard: suppress, never clamp to 100
        pf = None
    return {"holders": int(_pi(raw.get("total_institutional_holders")) or len(parsed)),
            "sharesB": total / 1e9, "pctOwn": _pp(raw.get("total_institutional_ownership_percentage")),
            "pctFloat": pf, "flowCount": fc, "flowShM": {k: round(v, 4) for k, v in fs.items()},
            "netShM": round(sum(fs.values()), 4), "netExNewShM": round(sum(fs.values()) - fs["new"], 4),
            "breadthPct": (100.0 * (fc["inc"] + fc["new"]) / (fc["inc"] + fc["new"] + fc["dec"] + fc["exit"])
                           if (fc["inc"] + fc["new"] + fc["dec"] + fc["exit"]) else None),
            "top10PctInst": round(100.0 * sum(p["held"] for p in live[:10]) / total, 2),
            "effN": (round(1.0 / hhi) if hhi > 0 else None),
            "asofMode": mode, "stalePctInst": round(stale, 2), "earlyHolders": early,
            "renames": renames, "top15": top15}


# ---- v1.148 SECTOR CONTEXT: relative strength vs the company's OWN sector SPDR ------------------
# The dashboard showed the sector only as header TEXT; it never answered the question that decides
# how to read the move — is this a COMPANY story or a SECTOR story? AV's OVERVIEW `Sector` uses the
# Yahoo/Morningstar 11-sector taxonomy, which maps 1:1 onto the sector SPDR ETFs (probed live
# 2026-07-24 across AAPL/XOM/JPM/JNJ/PG/NEE/CAT/LIN/AMT/DUK/GOOGL/WMT/TSLA — every string below is
# a real observed value). An UNMAPPED / missing sector gets NO tile: we never guess a proxy.
_SECTOR_SPDR = {
    "TECHNOLOGY": "XLK", "FINANCIAL SERVICES": "XLF", "HEALTHCARE": "XLV",
    "CONSUMER CYCLICAL": "XLY", "CONSUMER DEFENSIVE": "XLP", "ENERGY": "XLE",
    "INDUSTRIALS": "XLI", "BASIC MATERIALS": "XLB", "UTILITIES": "XLU",
    "REAL ESTATE": "XLRE", "COMMUNICATION SERVICES": "XLC"}
_SECTOR_ENV = "MARKET_DATA_SECTOR"
_SECTOR_WIN = 63      # ~3 trading months — the headline window
_SECTOR_WIN_1M = 21   # ~1 trading month — the sub line


def _sector_etf(sector):
    """AV OVERVIEW `Sector` -> its sector-SPDR ticker. None = missing/unmapped (an unclassified
    name simply gets no tile — never a guessed proxy)."""
    return _SECTOR_SPDR.get(" ".join(str(sector or "").upper().split()))


def _sector_dir():
    """The sector-SPDR day-cache dir (mirrors options._droi_dir): `MARKET_DATA_SECTOR` = a dir
    override, or 0/off/no/false (case-insensitive — the v1.126 lesson) to disable caching entirely;
    default ~/.cache/market-data/sector."""
    v = os.environ.get(_SECTOR_ENV, "").strip()
    if v.lower() in ("0", "off", "no", "false"):
        return None
    if v.lower() in ("1", "on", "yes", "true"):   # truthy = DEFAULT-ON, never a "./1" junk dir
        v = ""
    if v:
        return os.path.abspath(os.path.expanduser(v))
    return os.path.join(os.path.expanduser("~"), ".cache", "market-data", "sector")


def _sector_cache_load(sdir, etf, date):
    """The cached SPDR closes for (etf, date), or None on a miss. A corrupt/truncated file is
    UNLINKED (the v1.128 poisoned-gzip class: a bad file must not crash-loop the day)."""
    import gzip
    import zlib
    path = os.path.join(sdir, "%s.%s.json.gz" % (etf, date))
    try:
        with open(path, "rb") as fh:
            raw = json.loads(gzip.decompress(fh.read()).decode("utf-8"))
        cl = raw.get("closes")
        return [[str(d), float(c)] for d, c in cl] if cl else None
    except FileNotFoundError:
        return None
    except (OSError, ValueError, EOFError, zlib.error, TypeError, AttributeError):
        try:
            os.unlink(path)
        except OSError:
            pass
        return None


def _sector_cache_save(sdir, etf, date, closes):
    """Best-effort atomic write (mkstemp + replace) + a sweep of files older than ~7 days and of
    >1h .tmp orphans. Any failure is swallowed — the series still serves THIS render from memory."""
    import gzip
    import tempfile
    import time
    try:
        os.makedirs(sdir, exist_ok=True)
        cutoff = (dt.date.today() - dt.timedelta(days=7)).isoformat()
        now = time.time()
        for fn in os.listdir(sdir):
            fp = os.path.join(sdir, fn)
            try:
                if fn.endswith(".tmp"):
                    if now - os.path.getmtime(fp) > 3600:
                        os.unlink(fp)
                elif fn.endswith(".json.gz") and fn.rsplit(".", 3)[-3] < cutoff:
                    os.unlink(fp)          # the series moves daily — an old day is dead weight
            except (OSError, IndexError, ValueError):
                pass
        fd, tmp = tempfile.mkstemp(dir=sdir, suffix=".tmp")
        with os.fdopen(fd, "wb") as fh:
            fh.write(gzip.compress(json.dumps({"etf": etf, "date": date,
                                               "closes": closes}).encode("utf-8")))
        os.replace(tmp, os.path.join(sdir, "%s.%s.json.gz" % (etf, date)))
    except (OSError, ValueError, TypeError):
        pass


def _adj_closes(payload, strict=True):
    """[[date, adjusted close], …] ASCENDING from a TIME_SERIES_DAILY_ADJUSTED payload. `strict`
    (the sector path) requires the ADJUSTED field and DROPS any row without it; the raw-close
    fallback is only for callers that tolerate it. Review P2: the fallback made this function
    contradict its own contract — a `--from-dir` dir holding a plain TIME_SERIES_DAILY dump (which
    read_from_dir's `find("daily", …)` happily picks), or a CSV with sporadically blank
    adjusted_close, yielded a RAW or MIXED-basis series; differenced against the always-adjusted
    SPDR leg, a 2:1 split inside the window fabricated a "-50.0% vs XLE · lagging its sector".
    Dropping the unusable rows shortens the window instead — and a too-short window drops the tile,
    which is the honest outcome. A 0/None close drops the day either way (opthist parity)."""
    ts = (payload or {}).get("Time Series (Daily)") or {}
    out = []
    for d, v in ts.items():
        c = f(v, "5. adjusted close")
        if c is None and not strict:
            c = f(v, "4. close")
        if c and c > 0:
            out.append([d, c])
    out.sort(key=lambda r: r[0])
    return out


_SECTOR_LOCKS = {}          # (etf, day) -> Lock: in-process single-flight for the sector day-cache
_SECTOR_LOCKS_GUARD = __import__("threading").Lock()


def fetch_sector_closes(etf, key, fetch=None, today=None):
    """The sector SPDR's adjusted daily closes, DAY-CACHED per (etf, date): the FIRST company of a
    given sector that day pays ONE AV call; every later company in that sector — and every
    re-render — reads the cache (0 calls). `compact` (~100 trading days) covers the 63-day window.
    Best-effort: ANY failure returns None (the tile is dropped, the page is untouched) and caches
    NOTHING (failure != no-data — a throttle must not freeze an empty series into the day)."""
    day = today or dt.date.today().isoformat()
    sdir = _sector_dir()
    if sdir:
        hit = _sector_cache_load(sdir, etf, day)
        if hit:
            return hit
    # SINGLE-FLIGHT (review P3): the cache is check-then-fetch-then-write, so N concurrent renders of
    # same-sector names each missed and each burned a metered call against a 25/day budget. Serialize
    # per (etf, day) and RE-CHECK the cache inside the lock — the losers then read the winner's file.
    # Cross-PROCESS concurrency can still duplicate at most one call per sector per day (a file lock
    # would trade that for a stall on a crashed holder); documented, not silently assumed away.
    with _SECTOR_LOCKS_GUARD:
        _lk = _SECTOR_LOCKS.setdefault((etf, day), __import__("threading").Lock())
    with _lk:
        if sdir:
            hit = _sector_cache_load(sdir, etf, day)
            if hit:
                return hit
        return _fetch_sector_closes_locked(etf, key, fetch, day, sdir)


def _fetch_sector_closes_locked(etf, key, fetch, day, sdir):
    """The metered leg of fetch_sector_closes, run under the per-(etf, day) single-flight lock."""
    try:
        payload = (fetch or av)({"function": "TIME_SERIES_DAILY_ADJUSTED", "symbol": etf,
                                 "outputsize": "compact"}, key)
        # review P3: the RESPONSE-SHAPE handling has to sit INSIDE the guard too. _adj_closes does
        # `(payload or {}).get(...)` then `.items()`, so a non-dict AV body raised AttributeError out of
        # the worker thread — and gather() re-raises it at _sec_fut.result(), killing the whole page
        # after the metered core fan-out was already paid for. "ANY failure returns None" now means it.
        closes = _adj_closes(payload)
    except (SystemExit, Exception) as e:
        # say WHY the tile vanished (the _bg_av house rule) — "no tile" must be distinguishable from
        # "unmapped sector". KeyboardInterrupt/GeneratorExit are BaseException and still propagate.
        print("warning: sector context unavailable (%s pull: %s) — the tile is skipped"
              % (etf, str(e)[:60]), file=sys.stderr)
        return None
    if not closes:
        return None
    # Drop TODAY's still-moving bar before caching. AV's daily series carries the CURRENT session and
    # updates it intraday, so freezing it for the local day made the SPDR leg a snapshot from first-
    # render time while the company leg is refetched every run — the tile then differenced two legs
    # captured hours apart and called the gap company-specific (review P3). Only COMPLETED sessions are
    # comparable across renders; the last completed bar is what both legs share anyway.
    closes = [r for r in closes if r[0] < day] or closes
    if sdir:
        _sector_cache_save(sdir, etf, day, closes)
    return closes


def _rs(co, se, n):
    """Company vs sector return over the SAME trailing n-trading-day window, aligned on the dates
    the two series SHARE (a halt/holiday in one must not silently shift the other's window).
    Returns {co, se, rs, from, to} — rs = the company's EXCESS return — or None when the shared
    window is shorter than n+1 points (a partial window would be a quiet lie) or a base is 0."""
    sd = {d: c for d, c in (se or [])}
    common = [(d, c) for d, c in (co or []) if d in sd]
    if len(common) <= n:
        return None
    d1, c1 = common[-1]
    d0, c0 = common[-1 - n]
    s1, s0 = sd[d1], sd[d0]
    if not c0 or not s0:
        return None
    cr, sr = c1 / c0 - 1.0, s1 / s0 - 1.0
    return {"co": cr, "se": sr, "rs": cr - sr, "from": d0, "to": d1}


def _sector_snapshot(R, win=_SECTOR_WIN, win1m=_SECTOR_WIN_1M):
    """The Overview sector-context read, computed OFFLINE from R (gather already did the one
    day-cached pull, so --from-dir replays it for free). None — and therefore NO tile — when the
    name has no sector, an unmapped one, no SPDR series (pull failed/disabled), or a price series
    too short for the window."""
    ov = (R or {}).get("ov")
    ov = ov if isinstance(ov, dict) else {}
    sector = ov.get("Sector")
    etf = _sector_etf(sector)
    # review P1 — US/USD ONLY. The SPDRs are USD; a XETRA/LSE/TSX listing whose sector string is one
    # of the 11 canonical values would otherwise have its EUR/GBX return differenced against XLK's
    # USD one, folding FX straight into the "relative strength" (a 4% EURUSD move over 3M would read
    # as 4 points of company alpha, labelled idiosyncratic). Gate on the VENUE first — the
    # `_session_card` rule (no assume_us: a non-US/unknown listing is simply omitted, never faked) —
    # then on currency as the `_cap_live_b` (v1.113 pence) twin. AV can omit Currency entirely, so
    # `in (None,"USD")` alone would treat an unknown as USD; the venue gate is what actually decides.
    if etf:
        try:
            import opthist
            _us = bool(opthist.market_session(ov.get("Exchange")))   # US venue -> a session; else None
        except Exception:
            _us = False
        if not _us or str(ov.get("Currency") or "USD").upper() != "USD":
            return None
    sec = (R or {}).get("sec")
    closes = sec.get("closes") if isinstance(sec, dict) else None
    if not etf or not closes:
        return None
    # The card LABELS the SPDR it derived from OVERVIEW's sector, but the series comes from R["sec"] —
    # and gather() writes the identity tag there for exactly this reason. A --from-dir replay reloads
    # sector_spdr.json unconditionally, and the save loop only writes on a SUCCESSFUL pull, so a stale
    # file can outlive a throttled run and be paired with a refreshed price series: the tile would then
    # print "+X% vs XLK" computed against a DIFFERENT ETF. Trust the tag, not the label (review P3).
    if sec.get("etf") and sec.get("etf") != etf:
        print("warning: the cached sector series is %s but %s maps to %s — the sector tile is skipped "
              "(stale --from-dir cache?)" % (sec.get("etf"), sector, etf), file=sys.stderr)
        return None
    co = _adj_closes((R or {}).get("px"))
    r = _rs(co, closes, win)
    if not r:
        return None
    r1 = _rs(co, closes, win1m) or {}
    return {"sector": sector, "etf": etf, "win": win, "co": r["co"], "se": r["se"], "rs": r["rs"],
            "from": r["from"], "to": r["to"],
            "co1m": r1.get("co"), "se1m": r1.get("se"), "rs1m": r1.get("rs")}


def _win_lab(n):
    """A trading-day count as the window label the card/text show ('3M'/'1M', else '<n>d') — review
    P3: the sub hardcoded '3M' while the hint derived the window, so a non-default `win` mislabelled."""
    return {_SECTOR_WIN: "3M", _SECTOR_WIN_1M: "1M"}.get(n) or ("%dd" % n)


def _sector_legs(sc, sym):
    """The shared 'XLE +8.0% · XOM +12.2% · 3M' leg string + the verdict word — ONE source for the
    card and the text twin so the two can never disagree. A None leg prints n/a, never a fake 0."""
    win = sc.get("win") or _SECTOR_WIN

    def _sp(v):   # signed percent, or an honest hole — never a fabricated 0.0%
        return "n/a" if v is None else "%+.1f%%" % (v * 100)
    legs = "%s %s · %s %s · %s" % (sc.get("etf"), _sp(sc.get("se")), sym,
                                   _sp(sc.get("co")), _win_lab(win))
    # a tie at DISPLAY rounding shares the mark (the compare.py precedent) — "+0.0% outperforming"
    # would be a distinction the printed number doesn't support
    rs_disp = round((sc.get("rs") or 0.0) * 100, 1)
    word = "in line with" if rs_disp == 0 else ("outperforming" if rs_disp > 0 else "lagging")
    return legs, word, win


def _x100(v):
    return None if v is None else v * 100


def _sector_card(D):
    """The Overview tile: the company's ~3-month return MINUS its sector SPDR's. Answers 'company
    story or sector story?' at a glance — and feeds the offer (a real divergence makes the
    `:sector` complement organic). None whenever there is no sector context (ETF page, unmapped
    sector, non-US/non-USD listing, cold/failed SPDR pull, too-short series) — dropped, never faked."""
    sc = (D or {}).get("sectorCtx")
    if not sc or sc.get("rs") is None:
        return None
    etf, sym = sc.get("etf"), (D.get("symbol") or "")
    lab = " ".join(w.capitalize() for w in str(sc.get("sector") or "").split())
    legs, word, win = _sector_legs(sc, sym)
    subs = [legs, "%s its sector" % word]
    if sc.get("rs1m") is not None:
        subs.append("%s %+.1f%% vs %s" % (_win_lab(_SECTOR_WIN_1M), sc["rs1m"] * 100, etf))
    return {"label": "Sector · %s (%s)" % (lab, etf),
            "value": "%+.1f%% vs %s" % (sc["rs"] * 100, etf),
            "sub": subs,
            "hint": ("The company's ~%s (%d trading days) return MINUS its sector SPDR's, on "
                     "ADJUSTED closes over the window both series share (%s → %s). Positive = "
                     "outperforming its sector — a company-specific move; negative = lagging it. "
                     "The sector's own return beside it shows how much of the move is sector-WIDE "
                     "rather than idiosyncratic. US/USD listings only (the SPDR is USD — an FX "
                     "leg would land inside the difference). Sector proxy = the %s SPDR ETF (not "
                     "an index membership weight)."
                     % (_win_lab(win), win, sc.get("from"), sc.get("to"), etf))}


def _sector_text_line(D):
    """The TEXT twin of the sector tile (review P2): the default output path is render_text, and
    `references/fundamentals.md` now asks the agent to QUOTE both legs ('+18%, but XLK is +13%') —
    without this line the one Overview number a text-only self-verification cannot see (the
    v1.117.3 verify-by-DATA contract). Built from the SAME _sector_legs as the card."""
    sc = (D or {}).get("sectorCtx")
    if not sc or sc.get("rs") is None:
        return None
    legs, word, _ = _sector_legs(sc, D.get("symbol") or "")
    return ("SECTOR     %s  ·  %+.1f%% vs %s (%s)  ·  as of %s"
            % (legs, sc["rs"] * 100, sc.get("etf"), word, sc.get("to")))


def _vol_snapshot(symbol, R):
    """Options-vol context for the Overview tab — ZERO AV calls (v1.87): the LOCAL opthist
    settlement store (nightly-prewarmed for the ~563-name universe) + realized vol from the
    daily series ALREADY in R. Returns the snapshot when warm, {'cold': True} when the name is
    optionable (a store skeleton exists) but unwarmed, None when there is no options signal at
    all (a non-optionable name gets neither card nor hint)."""
    try:
        import opthist
    except Exception:
        return None
    try:
        hist = opthist.load_store(symbol)
    except Exception:
        hist = []
    if not hist:
        try:
            return {"cold": True} if opthist.skel_load(symbol) else None
        except Exception:
            return None
    cur = hist[-1]
    r = opthist.iv_rank(hist) or {}
    ts = (R.get("px") or {}).get("Time Series (Daily)") or {}
    closes = []
    for d, v in ts.items():
        c = f(v, "5. adjusted close")
        if c is None:                       # prefer adjusted; a 0-adjusted day DROPS (opthist parity)
            c = f(v, "4. close")
        if c and c > 0:
            closes.append((d, c))
    hv = opthist.realized_at(closes, cur.get("date", ""), 20) if closes else None
    iv30 = cur.get("cm30")
    # v1.89 tenors: 30/60/90 rows for the full-width term strip (the ONE fat tile inflated its
    # whole card row). Rank per tenor from that tenor's own history; HV windows pair the tenor
    # (30↔20, 60↔40, 90↔60 trading days).
    tenors = []
    for t, w in ((30, 20), (60, 40), (90, 60)):
        iv = cur.get("cm%d" % t)
        tr = opthist.iv_rank_at(hist, t) or {}
        thv = opthist.realized_at(closes, cur.get("date", ""), w) if closes else None
        tenors.append({"t": t, "iv": iv, "rank": tr.get("rank"), "pct": tr.get("pct"), "hv": thv,
                       "vrp": (iv * 100 - thv * 100 if (iv is not None and thv is not None) else None)})
    return {"cm30": iv30, "rank": r.get("rank"), "pct": r.get("pct"), "hv20": hv,
            "vrp": (iv30 * 100 - hv * 100 if (iv30 is not None and hv is not None) else None),
            "asOf": cur.get("date"), "n": len(hist), "tenors": tenors}


def _term_shape_of(vs):
    """The verbal 30/60/90 shape from a volSnap dict — one classifier (opthist.term_shape),
    shared by the strip title and the text VOL line so the two never diverge."""
    try:
        import opthist
    except Exception:
        return None
    iv = {t["t"]: t.get("iv") for t in (vs.get("tenors") or [])}
    return opthist.term_shape(iv.get(30), iv.get(60), iv.get(90))


def _vol_panel(D):
    """The FULL-ROW options-vol term strip (v1.89 — the one fat tile inflated its whole card
    row; user 2026-07-14): a compact 30/60/90 table under the tiles. None when cold/absent."""
    vs = D.get("volSnap")
    if not vs or vs.get("cold") or not vs.get("tenors"):
        return None
    shape = _term_shape_of(vs)
    rows = []
    for t in vs["tenors"]:
        w = {30: 20, 60: 40, 90: 60}.get(t["t"], 20)
        rows.append(["%dd" % t["t"],
                     (b(t["iv"] * 100, 1) + "%") if t.get("iv") is not None else "n/a",
                     (f"{t['rank']:.0f}%" if t.get("rank") is not None else "n/a"),
                     (f"{t['pct']:.0f}%" if t.get("pct") is not None else "n/a"),
                     (f"{t['hv'] * 100:.1f}% ({w}d)" if t.get("hv") is not None else "n/a"),
                     (f"{t['vrp']:+.1f}" if t.get("vrp") is not None else "n/a")])
    return {"type": "table",
            "title": ("Vol · term — " + (shape + " · " if shape else "")
                      + f"settlement {vs.get('asOf') or '?'}"),
            # v1.108.1: the SHAPE read + source/drill only — each column is explained on its own
            # header hover (colhints), so the glossary is not repeated here
            "info": "Constant-maturity ATM IV by tenor; the SHAPE is the read — front-rich/"
                    "backwardation = near-term stress or a dated event, back-rich/contango = the "
                    "calm-market normal, humped/dished @60d = a mid-dated event priced in. "
                    "From the local settlement store (zero extra calls); each column is explained "
                    f"on hover. Live chain + full drill: options.py {D['symbol']} --full.",
            "data": {"columns": ["Tenor", "ATM IV", "IV rank", "Percentile", "Realized HV", "VRP"],
                     "rows": rows, "big": True,   # headline info reads at 14px (user 2026-07-14)
                     # v1.105: header hover hints (the .htip mechanism)
                     "colhints": {
                         "ATM IV": "constant-maturity at-the-money implied volatility, interpolated "
                                   "from the settlement store's listed expirations",
                         "IV rank": "where today's IV sits in this tenor's stored min–max range "
                                    "(0% = cheapest ever, 100% = richest)",
                         "Percentile": "share of stored days with LOWER IV than today",
                         "Realized HV": "annualized realized (close-to-close) volatility over the "
                                        "paired trailing window — 30↔20d, 60↔40d, 90↔60d",
                         "VRP": "volatility risk premium = IV − realized, in points: positive = "
                                "options priced RICH vs the movement the stock delivered"},
                     "cellstyle": [["", "font-weight:700", "", "", "", ""] for _ in rows]}}


def _vol_text_line(D):
    """The text VOL line (shared company/ETF; review P2-1: cm30 None reads n/a, never 0.0%)."""
    vs = D.get("volSnap")
    if not vs:
        return None
    if vs.get("cold"):
        return f"VOL        options vol history cold — run options.py {D['symbol']} --history for IV rank/VRP"
    _iv = (b(vs["cm30"] * 100, 1) + "%") if vs.get("cm30") is not None else "n/a"
    _vrp = (f"{vs['vrp']:+.1f} (IV {vs['cm30']*100:.0f}% vs HV20 {vs['hv20']*100:.0f}%)"
            if vs.get("vrp") is not None else "n/a")
    _term = "".join(f" · {t['t']}d {b(t['iv'] * 100, 1)}%"
                    for t in (vs.get("tenors") or []) if t["t"] != 30 and t.get("iv") is not None)
    _shape = _term_shape_of(vs)
    return (f"VOL        30d IV {_iv}"
            + (f" · IV rank {vs['rank']:.0f}%" if vs.get("rank") is not None else "")
            + _term
            + (f" · term {_shape}" if _shape else "")
            + f" · VRP {_vrp} · settlement {vs.get('asOf') or '?'}")


def _is_etf_profile(etf):
    """An ETF_PROFILE payload with usable content — distinguishes a real fund from an empty {} /
    an AV error dict. v1.92.1: holdings/sectors are NOT required — a PHYSICAL trust (GLD/SLV/USO
    holds bullion/futures, not securities) ships them empty with real scalars; the gate is any
    parseable core field (net assets / expense ratio / inception)."""
    if not isinstance(etf, dict):
        return False
    if etf.get("holdings") or etf.get("sectors"):
        return True
    return f(etf, "net_assets") is not None or f(etf, "net_expense_ratio") is not None \
        or bool(etf.get("inception_date"))


def _etf_series_stats(R):
    """Price-series stats for the ETF page (v1.92 — the page was thin while the FULL daily series
    was already fetched): 52w range position, vs 200d MA, max drawdown, avg $ volume (3m), a
    returns strip, and a monthly-resampled drawdown curve. Adjusted close throughout (dividends
    fold into returns — the honest total-return read; the kline stays split-only). None when the
    series is too short (<30 sessions) — no fake values."""
    ts = (R.get("px") or {}).get("Time Series (Daily)") or {}
    rows = []
    for d, v in sorted(ts.items()):
        c = f(v, "5. adjusted close")
        if c is None:
            c = f(v, "4. close")
        if c and c > 0:
            rows.append((d, c, f(v, "6. volume") or 0.0))
    if len(rows) < 30:
        return None
    closes = [c for _, c, _ in rows]
    last = closes[-1]
    w52 = closes[-252:]
    hi, lo = max(w52), min(w52)
    pos52 = max(0.0, min(100.0, (last - lo) / (hi - lo) * 100)) if hi > lo else None
    ma200 = (sum(closes[-200:]) / 200) if len(closes) >= 200 else None
    vs200 = ((last / ma200 - 1) * 100) if ma200 else None

    def _ret(n):
        return ((last / closes[-n - 1] - 1) * 100) if len(closes) > n else None
    years = len(closes) / 252.0
    itd = ((last / closes[0]) ** (1 / years) - 1) * 100 if years >= 1 else ((last / closes[0] - 1) * 100)
    rets = {"1M": _ret(21), "3M": _ret(63), "6M": _ret(126), "1Y": _ret(252), "3Y": _ret(756),
            "ITD/y": itd}
    peak, dd, mdd = closes[0], [], 0.0
    for c in closes:
        peak = max(peak, c)
        d_ = (c / peak - 1) * 100
        dd.append(d_)
        mdd = min(mdd, d_)
    step = max(1, len(rows) // 120)   # ~monthly resample: the dd curve stays light on 30y funds
    dd_lab = [rows[i][0][:7] for i in range(0, len(rows), step)]
    dd_val = [round(dd[i], 2) for i in range(0, len(dd), step)]
    if dd_lab[-1] != rows[-1][0][:7]:
        dd_lab.append(rows[-1][0][:7]); dd_val.append(round(dd[-1], 2))
    adv = sum(c * v for _, c, v in rows[-63:]) / min(63, len(rows))
    return {"pos52": pos52, "wkLo": lo, "wkHi": hi, "vs200Pct": vs200, "rets": rets,
            "maxDDPct": mdd, "curDDPct": dd[-1], "advUsd3m": adv,
            "ddLabels": dd_lab, "ddValues": dd_val}


def _reduce_news(feed, symbol):
    """NEWS_SENTIMENT feed -> (senti aggregate, curated articles) — shared by the company AND the
    ETF build (v1.96: the gather already pulls news for every symbol; the ETF page just never
    rendered it). Relevance gate REL_MIN on both the mood aggregate and the headlines list."""
    senti = None
    scores, scores_all, feed_days = [], [], []
    for a in feed:
        ts_ = str(a.get("time_published") or "")
        if len(ts_) >= 8:
            feed_days.append(ts_[:8])
        for t in a.get("ticker_sentiment", []):
            if t.get("ticker") == symbol:
                s = f(t, "ticker_sentiment_score")
                rel_ = f(t, "relevance_score")
                if s is None:
                    continue
                scores_all.append(s)   # ungated reference — the gate's selection skew must be visible
                # the INFOGRAPHIC honors the same relevance gate as the headlines list —
                # an off-topic 0.3-relevance piece must not tilt the mood (user 2026-07-04)
                if rel_ is not None and rel_ >= REL_MIN:
                    scores.append(s)
    if scores_all:
        # the AV tier caps the feed at the 50 LATEST articles (limit=200 probe still returned 50),
        # so on a heavily-covered name the "14d" request really spans ~1-2 days — report the TRUTH
        d0 = min(feed_days) if feed_days else None
        try:
            span = max(1, (dt.date.today() - dt.date(int(d0[:4]), int(d0[4:6]), int(d0[6:8]))).days) if d0 else None
        except ValueError:
            span = None                     # a junk time_published must not kill the whole build
        base = {"avgAll": round(sum(scores_all) / len(scores_all), 3), "nAll": len(scores_all),
                "spanD": span}
        if scores:
            bull = sum(1 for s in scores if s >= 0.15); bear = sum(1 for s in scores if s <= -0.15)
            avg = sum(scores) / len(scores)
            # label bands are Alpha Vantage's OWN documented convention (+-0.15 neutral, +-0.35 strong)
            lab = ("Bullish" if avg >= 0.35 else "Somewhat-Bullish" if avg >= 0.15 else
                   "Neutral" if avg > -0.15 else "Somewhat-Bearish" if avg > -0.35 else "Bearish")
            senti = dict(base, n=len(scores), avg=round(avg, 3), label=lab,
                         bull=bull, neutral=len(scores) - bull - bear, bear=bear)
        else:
            # everything below the relevance gate: the tile must say 0-of-N, NOT "no news data",
            # and the ungated reference matters most exactly here (review 2026-07-04)
            senti = dict(base, n=0, avg=None, label=None, bull=0, neutral=0, bear=0)

    # curated headlines for the expandable News tile: this ticker's relevance >= REL_MIN
    # (AV relevance is noisy — a 0.61 Chevron piece ranked for MSFT in the 2026-07-04 probe;
    # user raised the bar to 0.75), newest first (feed order); summary feeds the hover tooltip
    articles = []
    for a in feed:
        tk = next((x for x in a.get("ticker_sentiment", []) if x.get("ticker") == symbol), None)
        rel = f(tk or {}, "relevance_score")
        if tk is None or rel is None or rel < REL_MIN:
            continue
        ts_ = str(a.get("time_published") or "")
        url_ = str(a.get("url") or "")
        sm = str(a.get("summary") or "")
        articles.append({"d": f"{ts_[:4]}-{ts_[4:6]}-{ts_[6:8]}" if len(ts_) >= 8 else "",
                         "src": a.get("source"), "title": a.get("title"),
                         "url": url_ if url_.startswith("http") else "",
                         "label": tk.get("ticker_sentiment_label"),
                         "score": f(tk, "ticker_sentiment_score"),
                         "sum": (sm[:220] + "\u2026") if len(sm) > 220 else sm})
        # v1.106 (user 2026-07-15): NO cap \u2014 every relevance-gated article rides; the AV feed
        # itself returns at most ~50 and the scrollable box absorbs the depth
    return senti, articles


def _fund_name(search, symbol):
    """Fund identity from SYMBOL_SEARCH (v1.97: ETF_PROFILE carries NO name — 'MSOS ETF' said
    nothing about cannabis). Exact-symbol match only; AV shouts ALL-CAPS, so smart-case with
    the acronyms kept (US/ETF/SPDR/S&P...). None when absent — the caller falls back honestly."""
    brands = {"Advisorshares": "AdvisorShares", "Proshares": "ProShares", "Vaneck": "VanEck",
              "Ishares": "iShares", "Wisdomtree": "WisdomTree", "Graniteshares": "GraniteShares",
              "Yieldmax": "YieldMax", "Flexshares": "FlexShares", "Ultrapro": "UltraPro"}
    for m in (search or {}).get("bestMatches") or []:
        if str(m.get("1. symbol") or "").upper() == symbol.upper():
            nm = str(m.get("2. name") or "").strip()
            if nm:
                t = re.sub(r"\b(Etf|Etn|Us|Usa|Uk|Ii|Iii|Iv|Msci|Ftse|Spdr|Qqq|Reit|Esg|Ai|Ipo|Vix)\b",
                           lambda g: g.group(1).upper(), nm.title())
                t = re.sub(r"\b(" + "|".join(brands) + r")\b", lambda g: brands[g.group(1)], t)
                return t.replace("S&p", "S&P")
    return None


def _reduce_dividends(payload, price):
    """AV DIVIDENDS -> the ETF distributions block (v1.96): rows newest-first {ex, amt}, the
    trailing-365d sum and the TTM yield off the live price. None when the fund distributes
    nothing (physical trusts, most leveraged funds) — the panel is then omitted, never empty."""
    rows = []
    for r in (payload or {}).get("data") or []:
        ex = str(r.get("ex_dividend_date") or "")
        amt = f(r, "amount")
        if len(ex) == 10 and amt is not None and amt > 0:
            rows.append({"ex": ex, "amt": amt})
    if not rows:
        return None
    rows.sort(key=lambda r: r["ex"], reverse=True)
    cutoff = (dt.date.today() - dt.timedelta(days=365)).isoformat()
    ttm = round(sum(r["amt"] for r in rows if r["ex"] >= cutoff), 6) or None
    # v1.98: the PANEL shows per-YEAR totals (a quarterly payer's per-payment bars are noise;
    # annual sums show the payout trend), newest year first — horizontal rows on the page
    by = {}
    for r in rows:
        by[r["ex"][:4]] = by.get(r["ex"][:4], 0.0) + r["amt"]
    return {"rows": rows[:40], "ttm": ttm,
            "byYear": [{"y": y, "amt": round(a, 6)} for y, a in sorted(by.items(), reverse=True)][:25],
            "ttmYieldPct": (round(100.0 * ttm / price, 2) if (ttm and price) else None)}


def build_etf(symbol, R):
    """ETF/fund dashboard data from ETF_PROFILE (+ GLOBAL_QUOTE price + the daily price series).
    A fund has no income/balance/cash-flow, so this is a DISTINCT, smaller D (kind='etf'): expense
    ratio, net assets, yield, turnover, inception, sector weights and top holdings. v1.83.0 — before
    this, `fundamentals.py SPY` exited 'no OVERVIEW'."""
    etf = R["etf"]
    q = (R.get("q") or {}).get("Global Quote", {})
    na = f(etf, "net_assets")
    exp = f(etf, "net_expense_ratio")
    dy = f(etf, "dividend_yield")
    turn = f(etf, "portfolio_turnover")

    def _weighted(rows, key):
        out = []
        for r in rows or []:
            w = f(r, "weight")
            if w is not None:
                out.append(dict(r, _w=w))
        return sorted(out, key=lambda r: r["_w"], reverse=True)
    # zero-weight rows dropped: AV pads all 11 GICS sectors and a GDX-class fund carries 8 rows
    # of "0.00" noise; a real-but-tiny sleeve still shows (nonzero survives the filter)
    sectors = [{"sector": s.get("sector", ""), "weightPct": round(s["_w"] * 100, 2)}
               for s in _weighted(etf.get("sectors"), "weight") if s["_w"] > 0]
    # v1.100.2: a feed that classifies under 5% of the fund is USELESS (EEM live: 1.4% -> a
    # 98.6%-grey doughnut; the 2026-07-15 investigation: the fund-sector feed is a separate
    # vendor table, partial-to-broken off the flagship funds) — drop it, the panel and the
    # text SECTORS line vanish together. MSOS-class ~10% keeps rendering.
    if sum(s["weightPct"] for s in sectors) < 5.0:
        sectors = []
    holdings = [{"symbol": h.get("symbol", ""), "name": h.get("description", ""),
                 "weightPct": round(h["_w"] * 100, 2)}
                for h in _weighted(etf.get("holdings"), "weight")]
    senti, articles = _reduce_news((R.get("news") or {}).get("feed", []), symbol)
    return {
        "kind": "etf", "symbol": symbol,
        # v1.97: the real fund name from SYMBOL_SEARCH — "MSOS ETF" said nothing about cannabis;
        # honest fallback when the search is absent/missed
        "name": _fund_name(R.get("search"), symbol) or (symbol + " ETF"),
        "price": f(q, "05. price"),
        "changePct": float(q["10. change percent"].rstrip("%")) if q.get("10. change percent") else None,
        "netAssetsB": None if na is None else na / 1e9,
        "expenseRatioPct": None if exp is None else exp * 100,
        "divYieldPct": None if dy is None else dy * 100,
        "turnoverPct": None if turn is None else turn * 100,
        "inceptionDate": etf.get("inception_date"),
        "leveraged": etf.get("leveraged"),
        "volSnap": _vol_snapshot(symbol, R),   # v1.87 — ETFs are optionable too (SPY/QQQ/...)
        "nHoldings": len(holdings), "nSectors": len(sectors),
        "sectors": sectors, "holdings": holdings,
        # v1.96: news was ALREADY fetched for every symbol — reduce it for the ETF page too;
        # distributions from the new DIVIDENDS job
        "dist": _reduce_dividends(R.get("div"), f(q, "05. price")),
        "sentiment": senti, "articles": articles or None,
        # v1.101: the series' last trading day — the ETF comparison's per-column as-of stamp
        "asOfSeries": max(((R.get("px") or {}).get("Time Series (Daily)") or {}).keys(), default=None),
        "priceOHLC": _build_price_ohlc(R),
        "seriesStats": _etf_series_stats(R),   # v1.92 — the page was thin; the series was free
        "conc10": (round(sum(h["weightPct"] for h in holdings[:10]), 2) if holdings else None),
    }


def build(symbol, R, offline=False):
    """Compute the dashboard data dict from R (raw endpoint responses) — shared by the HTTP
    path (gather) and the MCP/file path (read_from_dir)."""
    ov = R["ov"]
    if not ov or "Symbol" not in ov:
        # ETFs/funds return an EMPTY OVERVIEW — route to the ETF_PROFILE branch when we have one
        # (v1.83.0: `fundamentals.py SPY` used to exit here). A genuinely bad ticker has neither.
        if _is_etf_profile(R.get("etf")):
            return build_etf(symbol, R)
        sys.exit(f"error: no OVERVIEW for {symbol} (bad ticker or non-equity?) — an ETF/fund needs "
                 f"ETF_PROFILE (fetched automatically); a bad ticker has neither")
    q = (R.get("q") or {}).get("Global Quote", {})
    inc = {r["fiscalDateEnding"][:7]: r for r in (R.get("inc") or {}).get("annualReports", [])}
    bal = {r["fiscalDateEnding"][:7]: r for r in (R.get("bal") or {}).get("annualReports", [])}
    cfa = {r["fiscalDateEnding"][:7]: r for r in (R.get("cf") or {}).get("annualReports", [])}
    earn = R.get("earn") or {}

    yrs = sorted(set(inc) & set(bal) & set(cfa))[-6:]
    if not yrs:
        sys.exit(f"error: no overlapping annual income/balance/cash-flow data for {symbol}")
    def col(src, k):
        return [f(src.get(y, {}), k) for y in yrs]
    rev = col(inc, "totalRevenue"); nin = col(inc, "netIncome"); gp = col(inc, "grossProfit")
    ocf = col(cfa, "operatingCashflow"); capex = col(cfa, "capitalExpenditures")
    fcf = [(ocf[i] - capex[i]) / 1e9 if ocf[i] is not None and capex[i] is not None else None for i in range(len(yrs))]
    sbc = col(cfa, "stockBasedCompensation")  # the engine behind dilution
    def _bb_row(row):
        # gross payments first — "proceeds" is a NET-of-issuance concept for many filers and
        # can even go negative while the buyback program runs (review 2026-07-04)
        for _k in ("paymentsForRepurchaseOfCommonStock", "paymentsForRepurchaseOfEquity"):
            v = f(row, _k)
            if v is not None:
                return v
        v = f(row, "proceedsFromRepurchaseOfEquity")
        return -v if v is not None else 0

    def _div_row(row):
        v = f(row, "dividendPayoutCommonStock")   # total includes preferred (banks/REITs)
        return v if v is not None else (f(row, "dividendPayout") or 0)
    buyback = [_bb_row(cfa.get(y, {})) / 1e9 for y in yrs]
    shares = [f(bal.get(y, {}), "commonStockSharesOutstanding") for y in yrs]
    assets = col(bal, "totalAssets"); equity = col(bal, "totalShareholderEquity")
    debt = col(bal, "shortLongTermDebtTotal")
    # liquid = cash + short-term inv + long-term inv; AV's "cashAndShortTermInvestments"
    # is unreliable (often equals cash only), so sum the explicit fields.
    cash_inv = [(f(bal.get(y, {}), "cashAndCashEquivalentsAtCarryingValue") or 0)
                + (f(bal.get(y, {}), "shortTermInvestments") or 0)
                + (f(bal.get(y, {}), "longTermInvestments") or 0) for y in yrs]
    netcash = [(cash_inv[i] - (debt[i] or 0)) / 1e9 for i in range(len(yrs))]

    # FRESHNESS (the MU lesson, 2026-07-04): the SAME payloads carry quarterlyReports; reading
    # only annuals left the net-cash card 10 months stale (-$3B shown vs the real +$24B) and
    # mixed margin bases (annual gross beside OVERVIEW-TTM net). Cards use the latest QUARTERLY
    # balance; TTM margins are COMPUTED from the last 4 quarterly income reports on one basis
    # (AV's own OperatingMarginTTM field proved wrong for MU: 80.4% vs 65.8% computed). The
    # FY trend rows stay annual — they are labelled fiscal-year series.
    qbal = (R.get("bal") or {}).get("quarterlyReports", [])
    if qbal:
        qb = qbal[0]
        _liq = ((f(qb, "cashAndCashEquivalentsAtCarryingValue") or 0)
                + (f(qb, "shortTermInvestments") or 0) + (f(qb, "longTermInvestments") or 0))
        _dtot = f(qb, "shortLongTermDebtTotal")
        if _dtot is None:                   # AV's computed total carries holes while components exist
            _std, _ltd = f(qb, "shortTermDebt"), f(qb, "longTermDebt")
            _dtot = ((_std or 0) + (_ltd or 0)) if (_std is not None or _ltd is not None) else None
        netcash_now = (_liq - _dtot) / 1e9 if _dtot is not None else None
        netcash_asof = qb.get("fiscalDateEnding", "")
    else:
        netcash_now, netcash_asof = netcash[-1], yrs[-1][:4]
    qinc = (R.get("inc") or {}).get("quarterlyReports", [])[:4]
    ttm = None
    margin_basis = {"gross": None, "net": None, "op": None}
    if len(qinc) == 4 and _q4_span_ok(qinc):
        def _s(k):
            xs = [f(r, k) for r in qinc]
            return None if any(x is None for x in xs) else sum(xs)
        t_rev = _s("totalRevenue")
        if t_rev:
            _gp, _oi, _ni = _s("grossProfit"), _s("operatingIncome"), _s("netIncome")
            # each margin independently: banks carry no grossProfit line — that must not
            # discard computable net/op (review 2026-07-04)
            ttm = {"gross": 100 * _gp / t_rev if _gp is not None else None,
                   "op": 100 * _oi / t_rev if _oi is not None else None,
                   "net": 100 * _ni / t_rev if _ni is not None else None}
            for _mk in margin_basis:
                if ttm[_mk] is not None:
                    margin_basis[_mk] = "computed TTM (last 4 quarterly reports)"

    rd = col(inc, "researchAndDevelopment")
    dividendsB = [_div_row(cfa.get(y, {})) / 1e9 for y in yrs]
    inv_now = cur_ratio = None
    if qbal:
        inv_now = (f(qbal[0], "inventory") or 0) / 1e9 or None
        _ca, _cl = f(qbal[0], "totalCurrentAssets"), f(qbal[0], "totalCurrentLiabilities")
        cur_ratio = (_ca / _cl) if (_ca and _cl) else None
    est_raw = (R.get("est") or {}).get("estimates") or []
    # v1.107: last-known-good — a GOOD pull persists; an EMPTY one (the feed blinks intra-day,
    # MU 2026-07-15) falls back to the ≤7d cache, ALWAYS stamped via estimatesCachedAsOf
    est_cached_asof = None
    if est_raw:
        if not offline:
            # v1.125: an OFFLINE --from-dir re-render must not restamp the LKG cache with TODAY —
            # a month-old saved dir would then serve month-old estimates as "cached <today>",
            # defeating the ≤7-day honesty gate (the v1.109 spun-off follow-up).
            _lkg_save("est", symbol, R.get("est"))
    else:
        _lkg_payload, _lkg_asof = _lkg_load("est", symbol)
        _cached_rows = (_lkg_payload or {}).get("estimates") or []
        if _cached_rows:
            est_raw = _cached_rows
            est_cached_asof = _lkg_asof

    def _est_row(r):
        if not r:
            return None
        eps = f(r, "eps_estimate_average")
        a30, a7 = f(r, "eps_estimate_average_30_days_ago"), f(r, "eps_estimate_average_7_days_ago")
        _b = lambda k: None if f(r, k) is None else f(r, k) / 1e9
        return {"date": r.get("date"), "eps": eps,
                "lo": f(r, "eps_estimate_low"), "hi": f(r, "eps_estimate_high"),
                "d30": round(eps - a30, 2) if (eps is not None and a30 is not None) else None,
                "d7": round(eps - a7, 2) if (eps is not None and a7 is not None) else None,
                "revB": _b("revenue_estimate_average"),
                "revLoB": _b("revenue_estimate_low"), "revHiB": _b("revenue_estimate_high"),
                "revN": int(f(r, "revenue_estimate_analyst_count") or 0),
                "n": int(f(r, "eps_estimate_analyst_count") or 0),
                "up30": int(f(r, "eps_estimate_revision_up_trailing_30_days") or 0),
                "down30": int(f(r, "eps_estimate_revision_down_trailing_30_days") or 0)}
    # real horizons are generic "fiscal year"/"fiscal quarter" (verified live 2026-07-04;
    # the docs demo shows "current/next fiscal year" — match by containment, order by date).
    # Watermark = latest REPORTED quarter, not today: the old date>=today rule hid the
    # ended-but-unreported consensus for ~4 weeks each quarter (review 2026-07-04).
    _wm = str((earn.get("quarterlyEarnings") or [{}])[0].get("fiscalDateEnding") or "") \
        or dt.date.today().isoformat()
    _fy = sorted((x for x in est_raw if "fiscal year" in str(x.get("horizon", "")).lower()
                  and str(x.get("date", "")) > _wm),
                 key=lambda x: str(x.get("date", "")))
    _cur, _nxt = _est_row(_fy[0] if _fy else None), _est_row(_fy[1] if len(_fy) > 1 else None)
    _qh = sorted((x for x in est_raw if "quarter" in str(x.get("horizon", "")).lower()
                  and str(x.get("date", "")) > _wm), key=lambda x: str(x.get("date", "")))
    estimates = ({"curFY": _cur, "nextFY": _nxt, "curQ": _est_row(_qh[0] if _qh else None),
                  "nextQ": _est_row(_qh[1] if len(_qh) > 1 else None)}
                 if (_cur or _nxt or _qh) else None)   # quarter-only payloads are real (review)

    # ---- v1.48 derived block (brainstorm 2026-07-04; all from already-fetched payloads) ----
    qinc_all = (R.get("inc") or {}).get("quarterlyReports", [])
    qcf_all = (R.get("cf") or {}).get("quarterlyReports", [])
    ttm_trend = None
    inc_w = _ttm_windows(qinc_all, ("totalRevenue", "netIncome"))
    cf_w = {d: v for d, v in _ttm_windows(qcf_all, ("operatingCashflow", "capitalExpenditures",
                                                    "stockBasedCompensation", "dividendPayout"))}
    if inc_w:
        ttm_trend = {"quarters": [d for d, _ in inc_w],
                     "revenueB": [None if v["totalRevenue"] is None else v["totalRevenue"] / 1e9 for _, v in inc_w],
                     "netMarginPct": [None if (not v["totalRevenue"] or v["netIncome"] is None)
                                      else 100 * v["netIncome"] / v["totalRevenue"] for _, v in inc_w],
                     "fcfB": [None if (d not in cf_w or cf_w[d]["operatingCashflow"] is None
                                       or cf_w[d]["capitalExpenditures"] is None)
                              else (cf_w[d]["operatingCashflow"] - cf_w[d]["capitalExpenditures"]) / 1e9
                              for d, _ in inc_w]}
    fcf_ttm = ttm_trend["fcfB"][-1] if (ttm_trend and ttm_trend["fcfB"] and ttm_trend["fcfB"][-1] is not None) else None
    ni_ttm = None
    if inc_w and inc_w[-1][1]["netIncome"]:
        ni_ttm = inc_w[-1][1]["netIncome"] / 1e9
    # conversion of LOSSES into a bigger burn must not read as great conversion (review)
    fcf_conv = (fcf_ttm / ni_ttm) if (fcf_ttm is not None and ni_ttm and ni_ttm > 0) else None
    _capr = f(ov, "MarketCapitalization")
    cap_b = _capr / 1e9 if _capr else None
    fcf_yield = (100 * fcf_ttm / cap_b) if (fcf_ttm is not None and cap_b) else None
    sbc_ttm = (cf_w[inc_w[-1][0]]["stockBasedCompensation"] / 1e9) if (inc_w and inc_w[-1][0] in cf_w) else None
    sbc_adj_fcf = (fcf_ttm - sbc_ttm) if (fcf_ttm is not None and sbc_ttm is not None) else None
    div_ttm = (cf_w[inc_w[-1][0]]["dividendPayout"] / 1e9) if (inc_w and inc_w[-1][0] in cf_w) else None
    payout_fcf = (100 * div_ttm / fcf_ttm) if (div_ttm is not None and fcf_ttm and fcf_ttm > 0) else None
    # zero-OK TTM holes cross-checked against independent evidence (review 2026-07-04):
    # 4xNone SBC quarterlies with real ANNUAL SBC -> "SBC-adj = FCF" was a silent lie
    _sbc_fy = (f(cfa.get(yrs[-1], {}), "stockBasedCompensation") or 0) / 1e9 if yrs else 0
    if sbc_ttm == 0 and _sbc_fy > 0.05:
        sbc_adj_fcf = None
    if div_ttm == 0 and (f(ov, "DividendYield") or 0) > 0:
        payout_fcf = None

    # Piotroski F-Score: 9 binary signals off the last two ANNUAL years (+ shares series)
    fscore = None
    if len(yrs) >= 2:
        y1, y0 = yrs[-1], yrs[-2]
        a1, a0 = f(bal.get(y1, {}), "totalAssets"), f(bal.get(y0, {}), "totalAssets")
        ni1, ni0 = f(inc.get(y1, {}), "netIncome"), f(inc.get(y0, {}), "netIncome")
        ocf1 = f(cfa.get(y1, {}), "operatingCashflow")
        roa1 = (ni1 / a1) if (ni1 is not None and a1) else None
        roa0 = (ni0 / a0) if (ni0 is not None and a0) else None
        ltd1, ltd0 = f(bal.get(y1, {}), "longTermDebt"), f(bal.get(y0, {}), "longTermDebt")
        ca1, cl1 = f(bal.get(y1, {}), "totalCurrentAssets"), f(bal.get(y1, {}), "totalCurrentLiabilities")
        ca0, cl0 = f(bal.get(y0, {}), "totalCurrentAssets"), f(bal.get(y0, {}), "totalCurrentLiabilities")
        cr1 = (ca1 / cl1) if (ca1 and cl1) else None
        cr0 = (ca0 / cl0) if (ca0 and cl0) else None
        gm1 = (f(inc.get(y1, {}), "grossProfit") or 0) / rev[-1] if rev[-1] else None
        gm0 = (f(inc.get(y0, {}), "grossProfit") or 0) / rev[-2] if rev[-2] else None
        at1 = (rev[-1] / a1) if (rev[-1] and a1) else None
        at0 = (rev[-2] / a0) if (rev[-2] and a0) else None
        checks = [
            ("ROA>0", roa1 is not None and roa1 > 0),
            ("OCF>0", ocf1 is not None and ocf1 > 0),
            ("ROA up", roa1 is not None and roa0 is not None and roa1 > roa0),
            ("OCF>NI", ocf1 is not None and ni1 is not None and ocf1 > ni1),
            ("leverage down", ltd1 is not None and ltd0 is not None
             and (a1 or 0) > 0 and (a0 or 0) > 0 and ltd1 / a1 <= ltd0 / a0
             or (ltd1 == 0 and ltd0 == 0)),   # missing debt data = check FAILS, not passes
            ("liquidity up", cr1 is not None and cr0 is not None and cr1 >= cr0),
            ("no dilution", shares[-1] is not None and shares[-2] is not None and shares[-1] <= shares[-2] * 1.002),
            ("gross margin up", gm1 is not None and gm0 is not None and gm1 > gm0),
            ("asset turnover up", at1 is not None and at0 is not None and at1 > at0),
        ]
        fscore = {"score": sum(1 for _, ok in checks if ok),
                  "failed": [nm for nm, ok in checks if not ok]}

    # Altman Z (classic): freshest balance (quarterly if present) + TTM income where possible.
    # NOT computed for financials — the 1968 model excludes banks/insurers by construction
    # (current-asset structure and sales/assets are meaningless there; review 2026-07-04).
    _secu = str(ov.get("Sector") or "").upper()
    _indu = str(ov.get("Industry") or "").upper()
    _is_fin = ("FINANC" in _secu) or any(k in _indu for k in ("BANK", "INSURAN", "CAPITAL MARKET", "CREDIT"))
    altman = None
    zb = qbal[0] if qbal else bal.get(yrs[-1], {})
    ta = f(zb, "totalAssets") or (assets[-1] if assets else None)
    tl = f(zb, "totalLiabilities")
    _zca, _zcl = f(zb, "totalCurrentAssets"), f(zb, "totalCurrentLiabilities")
    wc = (_zca - _zcl) if (_zca is not None and _zcl is not None) else None   # or-0 faked 0 working capital
    re_ = f(zb, "retainedEarnings")
    ebit = None
    if len(qinc_all) >= 4:
        _eq = [f(r, "operatingIncome") for r in qinc_all[:4]]
        if None not in _eq:                 # strict: an or-0 hole silently skewed Z (review)
            ebit = sum(_eq)
    if ebit is None:
        ebit = f(inc.get(yrs[-1], {}), "operatingIncome")
    sales = (inc_w[-1][1]["totalRevenue"] if inc_w else (rev[-1] if rev else None))
    if not _is_fin and ta and tl and wc is not None and re_ is not None and ebit is not None and sales and cap_b:
        altman = (1.2 * wc / ta + 1.4 * re_ / ta + 3.3 * ebit / ta
                  + 0.6 * (cap_b * 1e9) / tl + 1.0 * sales / ta)

    net_debt_ebitda = None
    if netcash_now is not None and netcash_now < 0:
        _ebitda = f(ov, "EBITDA")
        if _ebitda and _ebitda > 0:          # a negative-EBITDA ratio misleads more than n/a
            net_debt_ebitda = (-netcash_now * 1e9) / _ebitda
    _stmt_cur = ""
    for _src in ((inc.get(yrs[-1]) if yrs else None), (qinc[0] if qinc else None)):
        if _src and _src.get("reportedCurrency"):
            _stmt_cur = str(_src.get("reportedCurrency")).upper()
            break
    if _stmt_cur and _stmt_cur != "USD":
        # statements in filing currency vs USD price/cap: the cross-currency composites are
        # silently wrong — suppress rather than mislead (review 2026-07-04)
        fcf_yield = None
        altman = None
        net_debt_ebitda = None

    dilution_cagr = None
    _span_y = (int(yrs[-1][:4]) - int(yrs[0][:4])) if len(yrs) >= 2 else 0
    if shares and shares[0] and shares[-1] and _span_y >= 2:
        # >=2y REAL span (not row count — a gap year would overstate the rate); fresh IPOs
        # (SPCX probe: -77.6%/yr garbage from a 2-point series) stay gated out
        dilution_cagr = 100 * ((shares[-1] / shares[0]) ** (1 / _span_y) - 1)

    # FULL-depth series for the dashboard range buttons (annuals ~20y on AV; text/svg and
    # all computed metrics keep the 6y window — deep is additive, same col() math)
    yrs_all = sorted(set(inc) & set(bal) & set(cfa))

    def cola(src, k):
        return [f(src.get(y, {}), k) for y in yrs_all]
    _rev_a, _nin_a, _gp_a = cola(inc, "totalRevenue"), cola(inc, "netIncome"), cola(inc, "grossProfit")
    _ocf_a, _capex_a, _sbc_a = cola(cfa, "operatingCashflow"), cola(cfa, "capitalExpenditures"), cola(cfa, "stockBasedCompensation")
    deep = {
        "years": [y[:4] for y in yrs_all],
        "ocfB": [None if v is None else v / 1e9 for v in _ocf_a],
        "capexB": [None if v is None else v / 1e9 for v in _capex_a],
        "revenueB": [None if v is None else v / 1e9 for v in _rev_a],
        "netIncomeB": [None if v is None else v / 1e9 for v in _nin_a],
        "netMarginPct": [None if (_rev_a[i] in (None, 0) or _nin_a[i] is None) else 100 * _nin_a[i] / _rev_a[i]
                         for i in range(len(yrs_all))],
        "fcfB": [(_ocf_a[i] - _capex_a[i]) / 1e9 if _ocf_a[i] is not None and _capex_a[i] is not None else None
                 for i in range(len(yrs_all))],
        "sbcB": [None if v is None else v / 1e9 for v in _sbc_a],
        "sbcPctRev": [None if (_rev_a[i] in (None, 0) or _sbc_a[i] is None) else 100 * _sbc_a[i] / _rev_a[i]
                      for i in range(len(yrs_all))],
        "buybackB": [_bb_row(cfa.get(y, {})) / 1e9 for y in yrs_all],
        "dividendsB": [_div_row(cfa.get(y, {})) / 1e9 for y in yrs_all],
        "sharesB": [None if f(bal.get(y, {}), "commonStockSharesOutstanding") is None
                    else f(bal.get(y, {}), "commonStockSharesOutstanding") / 1e9 for y in yrs_all],
        "totalAssetsB": [None if v is None else v / 1e9 for v in cola(bal, "totalAssets")],
        "equityB": [None if v is None else v / 1e9 for v in cola(bal, "totalShareholderEquity")],
    }
    _qe_all = [r for r in earn.get("quarterlyEarnings", [])[:60] if f(r, "surprisePercentage") is not None]
    eps_deep = {"labels": [r["fiscalDateEnding"] for r in reversed(_qe_all)],
                "pct": [f(r, "surprisePercentage") for r in reversed(_qe_all)]}

    price_ohlc = _build_price_ohlc(R)

    qe = earn.get("quarterlyEarnings", [])[:8]
    surprises = [{"date": r["fiscalDateEnding"], "eps": f(r, "reportedEPS"), "est": f(r, "estimatedEPS"),
                  "surprisePct": f(r, "surprisePercentage")} for r in qe]
    # v1.159.4: the OpenAI follow-up router distinguishes a fresh published quarter from an
    # upcoming print.  AV already supplies reportedDate in EARNINGS, so expose the newest real
    # publication date in the compact text twin — no extra endpoint or web lookup.
    _last_report = next((r for r in earn.get("quarterlyEarnings", [])
                         if str(r.get("reportedDate") or "").strip()), {})
    _last_reported_date = str(_last_report.get("reportedDate") or "").strip() or None
    _last_reported_fiscal = str(_last_report.get("fiscalDateEnding") or "").strip() or None

    senti, articles = _reduce_news((R.get("news") or {}).get("feed", []), symbol)

    _pm = ttm["net"] if (ttm and ttm["net"] is not None) else (
        f(ov, "ProfitMargin") * 100 if f(ov, "ProfitMargin") is not None else None)
    if (ttm is None or ttm["net"] is None) and _pm is not None:
        margin_basis["net"] = "Alpha Vantage OVERVIEW field (quarterly TTM unavailable)"
    _om = ttm["op"] if (ttm and ttm["op"] is not None) else (
        f(ov, "OperatingMarginTTM") * 100 if f(ov, "OperatingMarginTTM") is not None else None)
    if (ttm is None or ttm["op"] is None) and _om is not None:
        margin_basis["op"] = "Alpha Vantage OVERVIEW field (quarterly TTM unavailable)"
    if (ttm is None or ttm["gross"] is None):
        margin_basis["gross"] = "last annual FY (quarterly TTM unavailable)"

    g = lambda k: ov.get(k)
    about = None
    if g("Description"):
        about = {"text": g("Description"),
                 "meta": " \u00b7 ".join(x for x in (g("Sector"), g("Industry"), g("Country")) if x),
                 "site": g("OfficialSite") or ""}
    return {
        "symbol": symbol, "name": g("Name"), "exchange": g("Exchange"), "sector": g("Sector"),
        "industry": g("Industry"), "country": g("Country"),
        "fyEnd": g("FiscalYearEnd"), "latestQuarter": g("LatestQuarter"), "currency": g("Currency"),
        "price": f(q, "05. price"), "changePct": float(q["10. change percent"].rstrip("%")) if q.get("10. change percent") else None,
        "marketCapB": cap_b,                # None when AV omits it — never a confident $0B
        "fwdPE": f(ov, "ForwardPE"), "trailingPE": f(ov, "TrailingPE"), "peg": f(ov, "PEGRatio"),
        "ps": f(ov, "PriceToSalesRatioTTM"), "pb": f(ov, "PriceToBookRatio"), "evEbitda": f(ov, "EVToEBITDA"),
        "roe": f(ov, "ReturnOnEquityTTM") * 100 if f(ov, "ReturnOnEquityTTM") is not None else None,
        "grossMarginTTM": ttm["gross"] if ttm else None,
        "profitMargin": _pm, "opMargin": _om, "marginBasis": margin_basis,
        "stmtCurrency": _stmt_cur or "USD",
        "netCashNowB": netcash_now, "netCashAsOf": netcash_asof,
        "divYieldPct": None if f(ov, "DividendYield") is None else f(ov, "DividendYield") * 100,
        "divKnownZero": (f(ov, "DividendYield") in (None, 0)
                         and ((dividendsB[-1] if dividendsB else 0) or 0) < 0.005),
        "divPerShare": f(ov, "DividendPerShare"), "exDivDate": ov.get("ExDividendDate"),
        "dividendsB": dividendsB,
        "revGrowthYoYPct": None if f(ov, "QuarterlyRevenueGrowthYOY") is None else f(ov, "QuarterlyRevenueGrowthYOY") * 100,
        "epsGrowthYoYPct": None if f(ov, "QuarterlyEarningsGrowthYOY") is None else f(ov, "QuarterlyEarningsGrowthYOY") * 100,
        "sharesFloatB": None if f(ov, "SharesFloat") is None else f(ov, "SharesFloat") / 1e9,
        # v1.113: shares outstanding (B) — the live header cap is price × THIS (the options-board
        # spot×shares rule), never the frozen OVERVIEW MarketCapitalization when both are present
        "sharesOutB": None if f(ov, "SharesOutstanding") is None else f(ov, "SharesOutstanding") / 1e9,
        "pctInsiders": f(ov, "PercentInsiders"), "pctInstitutions": f(ov, "PercentInstitutions"),
        "rdPctRev": [None if (rev[i] in (None, 0) or rd[i] is None) else 100 * rd[i] / rev[i] for i in range(len(yrs))],
        "inventoryNowB": inv_now, "currentRatio": cur_ratio, "estimates": estimates,
        # v1.107: set ONLY when the estimates above came from the LKG cache (never on a fresh pull)
        "estimatesCachedAsOf": (est_cached_asof if estimates else None),
        "ttmTrend": ttm_trend, "fcfConversion": fcf_conv, "fcfYieldPct": fcf_yield,
        "sbcAdjFcfTTMB": sbc_adj_fcf, "fScore": fscore, "altmanZ": altman,
        "netDebtEbitda": net_debt_ebitda, "payoutFcfPct": payout_fcf,
        "dilutionCagrPct": dilution_cagr, "deep": deep, "epsDeep": eps_deep,
        "priceOHLC": price_ohlc,
        "beta": f(ov, "Beta"), "wkHigh": f(ov, "52WeekHigh"), "wkLow": f(ov, "52WeekLow"),
        "dma50": f(ov, "50DayMovingAverage"), "dma200": f(ov, "200DayMovingAverage"),
        "nextEarnings": (R.get("cal") or {}).get("next"),   # v1.83.0 EARNINGS_CALENDAR next report date
        "lastEarningsReportedDate": _last_reported_date,
        "lastEarningsFiscalDate": _last_reported_fiscal,
        # v1.84 Phase A: the Ownership data slots (None when the payload is absent/rejected —
        # the Phase-B cards/panels/text simply don't render). Aggregates only, never raw rows.
        "volSnap": _vol_snapshot(symbol, R),   # v1.87 options-vol snapshot — zero AV calls
        "sectorCtx": _sector_snapshot(R),      # v1.148 sector RS — computed offline from R (gather pulled the SPDR)
        "insiders": _reduce_insiders(R),
        "inst": _reduce_institutions(R, shares_float=f(ov, "SharesFloat")),
        "analystTarget": f(ov, "AnalystTargetPrice"),
        "ratings": {k: int(f(ov, v) or 0) for k, v in
                    (("strongBuy", "AnalystRatingStrongBuy"), ("buy", "AnalystRatingBuy"), ("hold", "AnalystRatingHold"),
                     ("sell", "AnalystRatingSell"), ("strongSell", "AnalystRatingStrongSell"))},
        "years": [y[:4] for y in yrs],
        "revenueB": [None if v is None else v / 1e9 for v in rev],
        "netIncomeB": [None if v is None else v / 1e9 for v in nin],
        "netMarginPct": [None if (rev[i] in (None, 0) or nin[i] is None) else 100 * nin[i] / rev[i] for i in range(len(yrs))],
        "grossMarginPct": [None if (rev[i] in (None, 0) or gp[i] is None) else 100 * gp[i] / rev[i] for i in range(len(yrs))],
        "fcfB": fcf, "buybackB": buyback, "sharesB": [None if s is None else s / 1e9 for s in shares],
        "sbcB": [None if v is None else v / 1e9 for v in sbc],
        "sbcPctRev": [None if (rev[i] in (None, 0) or sbc[i] is None) else 100 * sbc[i] / rev[i] for i in range(len(yrs))],
        "totalAssetsB": [None if v is None else v / 1e9 for v in assets],
        "equityB": [None if v is None else v / 1e9 for v in equity],
        "netCashB": netcash, "totalDebtB": [None if v is None else v / 1e9 for v in debt],
        "epsSurprises": surprises, "sentiment": senti, "articles": articles or None, "about": about,
    }


# ----------------- text -----------------
def b(x, d=1):
    return "n/a" if x is None else f"{x:,.{d}f}"


def _pct_s(v, d=0):
    return "n/a" if v is None else b(v, d) + "%"     # a None percent never reads "n/a%" (v1.126)


def _usdb(x, d=1):
    return "n/a" if x is None else "$" + b(x, d) + "B"   # a None $-figure never reads "$n/aB" (v1.126)


def _cap_live_b(D):
    """The header/text/SVG cap in $B (v1.113): LIVE quote price × OVERVIEW SharesOutstanding — the
    SAME spot×shares rule as the options board, so the two unified boards (and this board's own
    text/SVG twins) agree on cap at the same instant; the frozen OVERVIEW MarketCapitalization
    drifts from the live price all session and stays only as the FALLBACK when either factor is
    absent. USD listings only: AV quotes LSE names in PENCE (GBX), so price×shares there would
    inflate ~100× under the hardcoded '$' — a KNOWN non-USD listing keeps the frozen cap (which AV
    reports in the listing's own currency). Display via dashboard.cap_str (the one shared rule)."""
    if D.get("currency") in (None, "USD") and D.get("price") and D.get("sharesOutB"):
        return D["price"] * D["sharesOutB"]
    return D.get("marketCapB")


def _range_pos(D, clamp=True):
    """Price position inside the 52-week range, 0..100 (CLAMPED by default — a fresh 52w break
    prints 0/100, never a negative/overshoot; clamp=False returns the RAW position so callers can
    say 'below/above range' honestly). None when OVERVIEW lacks the fields (rendered n/a, never a
    fake 0). v1.82.0: these OVERVIEW fields were parsed since v1.47 but rendered by NO output."""
    p, lo, hi = D.get("price"), D.get("wkLow"), D.get("wkHigh")
    if p is None or lo is None or hi is None or hi <= lo:
        return None
    pos = (p - lo) / (hi - lo) * 100
    return max(0.0, min(100.0, pos)) if clamp else pos


def _dma_diff(D, key):
    """Price vs a moving average (OVERVIEW 50/200DayMovingAverage), signed percent; None-safe."""
    p, m = D.get("price"), D.get(key)
    if p is None or not m or m <= 0:
        return None
    return (p - m) / m * 100


def _earn_days(datestr):
    """Calendar days from today to an ISO date, or None if unparseable. v1.83.0 (next-earnings card)."""
    try:
        return (dt.date.fromisoformat(datestr) - dt.date.today()).days
    except (ValueError, TypeError):
        return None


def _usd(m, signed=True):
    """Compact $ from MILLIONS: -182.3 -> '−$182M'; 2400 -> '+$2.4B'; 0.35 -> '+$350K' (v1.84)."""
    if m is None:
        return "n/a"
    s = ("+" if m >= 0 else "−") if signed else ""
    a = abs(m)
    if a >= 999.5:   # review P3-7: 999.6 must read $1.0B, not $1,000M
        return f"{s}${a / 1000:.1f}B"
    if a >= 10:
        return f"{s}${a:,.0f}M"
    if a >= 1:
        return f"{s}${a:.1f}M"
    return f"{s}${a * 1000:.0f}K"


def _short_title(t):
    """Canonical short form for C-suite titles; everything else truncates to 20 chars (v1.84)."""
    u = " " + (t or "").upper() + " "
    if "VICE" not in u:
        for pat, out in (("CHIEF EXECUTIVE", "CEO"), (" CEO", "CEO"), ("CHIEF FINANCIAL", "CFO"),
                         (" CFO", "CFO"), ("CHIEF OPERATING", "COO"), (" COO", "COO"),
                         ("CHAIRMAN", "Chairman"), ("PRESIDENT", "President")):
            if pat in u:
                return out
    t = t or ""
    return t if len(t) <= 20 else t[:19] + "…"   # review P3-6: never a mid-word chop without an ellipsis


def _ownership_ui(D):
    """The Ownership tab content (v1.84 Phase B) from the Phase-A slots — (overview_extra_cards,
    own_cards, own_panels). Everything degrades per-slot: a None slot contributes nothing (honest
    absence, never an empty shell). Caveat texts are the design-of-record wordings
    (docs/ownership-tabs-compare-plan.md + the 2026-07-13 agent specs)."""
    ins, inst = D.get("insiders"), D.get("inst")
    over_extra, cards, panels = [], [], []
    if ins:
        w6, w12 = ins["windows"]["6m"], ins["windows"]["12m"]
        cap = D.get("marketCapB")
        ofcap = (f" ≈{abs(w6['netUsdM']) / (cap * 1000) * 100:.2f}% of cap." if cap else "")
        pc_off = ("" if ins.get("priceCheck") else
                  " Price-vs-market check unavailable for this payload — priced acquisitions "
                  "may include option exercises.")
        _acq12 = w12.get("acq", 0)   # review P2-2: unverifiable priced-A must be DISCLOSED, not silent
        acq_note = (f" {_acq12} priced acquisition(s) in 12m could not be price-verified (outside "
                    "the daily-series coverage) and are excluded from the buyer counts." if _acq12 else "")
        c_net = {"label": "Insider net 6m", "value": _usd(w6["netUsdM"]),
                 "sub": [f"{w6['omBuys']} buys · {w6['discSells']} disc. sales"],
                 "hint": "Net $ insider flow over 6 months: priced acquisitions minus priced disposals "
                         "(Alpha Vantage INSIDER_TRANSACTIONS; unpriced grants/vests excluded). AV carries "
                         "no SEC Form-4 codes, so classes are heuristic — the sale side includes likely "
                         "sell-to-cover on vests, and steady selling is the BASELINE at stock-comp-heavy "
                         "companies, not a red flag by itself." + ofcap + acq_note + pc_off}
        cards.append(c_net)
        cards.append({"label": "Insider buyers 12m", "value": str(w12["uniqBuyers"]),
                      "sub": (["cluster: ≥3 buyers in 90d"] if ins["cluster"]["flag"] else
                              ([f"{w12['t1Buys']} C-suite buy(s)"] if w12["t1Buys"] else None)),
                      "hint": "Distinct insiders with an open-market-priced purchase in 12 months. Breadth "
                              "beats size: several independent buyers (a 'cluster') is the strongest "
                              "documented insider signal; one insider buying three times is one opinion. "
                              "Sales are deliberately not mirrored here — they are mostly "
                              "diversification, taxes or pre-set 10b5-1 plans."})
        over_extra.append(c_net)
    if inst:
        c_own = {"label": "Inst own",
                 "value": (b(inst["pctOwn"], 0) + "%") if inst.get("pctOwn") is not None else "n/a",
                 "sub": [f"{inst['holders']:,} filers · 13F"],
                 "hint": "Institutional ownership from SEC 13F filings (Alpha Vantage), % of shares "
                         "outstanding"
                         + (f" ≈{b(inst['pctFloat'], 0)}% of float" if inst.get("pctFloat") is not None else "")
                         + f". Filings lag up to 45 days after quarter end; most rows as of "
                           f"{inst['asofMode']}. For an ADR this counts only US-listed shares, so it "
                           "understates."}
        _net = inst.get("netExNewShM")
        FS = inst["flowShM"]
        _ren = ((": " + ", ".join(r["name"].title() for r in inst["renames"][:2])
                 + ("…" if len(inst["renames"]) > 2 else "") + " this quarter.")
                if inst.get("renames") else ".")
        cards.append(c_own)
        cards.append({"label": "13F net Q/Q",   # U+2212 minus — glyph-consistent with _usd (review P3-7)
                      "value": (f"{_net:+,.0f}M sh".replace("-", "−") if _net is not None else "n/a"),
                      "sub": [f"new {FS['new']:+,.0f}M · exits {FS['exit']:+,.0f}M"],
                      "hint": "Net shares added by institutions that ALREADY held the stock (increases "
                              "minus decreases minus sell-outs), latest reported quarter. New positions "
                              "are shown separately because very large 'new' filings are often a manager "
                              "re-filing under a new entity name, not new money" + _ren})
        over_extra.append(c_own)
    # ---- panels: [insider combo | 13F count] · [13F shares | doughnut] · tables full-width ----
    if ins and (any(ins["monthly"]["netUsdM"]) or any(ins["monthly"]["buyers"])):
        panels.append({"type": "combo", "title": "Insider net flow ($M/mo) · buyers", "newRow": True,
                       "zoom": {"def": 12, "step": 6, "min": 6},
                       # the right axis carries a COUNT of people, not dollars: with y1pct off it
                       # would otherwise print bare numbers next to a "$M" left axis (speccheck
                       # AXIS_UNIT_GAP)
                       "units": {"left": "$M", "right": "buyers"},
                       "info": "Monthly net insider $ flow (priced acquisitions − priced disposals) with "
                               "the count of distinct open-market buyers. Heuristic classes (no Form-4 codes "
                               "on this feed); grants and RSU vests excluded; sell-to-cover kept in the bars "
                               "but flagged out of the counts. Read the deviations, not the level.",
                       "data": {"labels": ins["monthly"]["labels"], "y1pct": False, "series": [
                           {"type": "bar", "name": "net flow $M", "values": ins["monthly"]["netUsdM"],
                            "colorBySign": True},
                           {"type": "line", "name": "buyers", "values": ins["monthly"]["buyers"], "axis": "y1"}]}})
    if inst:
        FC, FS = inst["flowCount"], inst["flowShM"]
        panels.append({"type": "bars", "title": "13F flow · holders (count)",
                       "units": {"left": "holders"},
                       "info": "How many institutions added / initiated / trimmed / exited in the latest "
                               "reported quarter (sign = buy or sell side). Count-based breadth is the robust "
                               "view: one giant filer counts once. 13F filings lag up to 45 days.",
                       "data": {"labels": ["added", "new", "trimmed", "exited"],
                                "series": [{"name": "holders", "colorBySign": True,
                                            "values": [FC["inc"], FC["new"], -FC["dec"], -FC["exit"]]}]}})
        panels.append({"type": "bars", "title": "13F flow · shares (M)",
                       "units": {"left": "M sh"},
                       "info": "The SAME quarter's flow weighted by shares — compare with the holder-count "
                               "panel: they often disagree (thousands of small trims vs one giant add). "
                               "'net ex-new' excludes initiated positions, because the largest 'new' filings "
                               "are often an existing manager re-filing under a new entity name (flagged in "
                               "the table), not fresh accumulation.",
                       "data": {"labels": ["added", "new", "trimmed", "exited", "net", "net ex-new"],
                                "series": [{"name": "Δ shares (M)", "colorBySign": True,
                                            "values": [FS["inc"], FS["new"], FS["dec"], FS["exit"],
                                                       inst["netShM"], inst["netExNewShM"]]}]}})
        if inst["holders"] >= 10 and inst["top15"]:   # nano-cap noise + all-exited books (review P3-1)
            top10 = inst["top15"][:10]
            panels.append({"type": "doughnut", "title": "Institutional concentration",
                           "info": f"Top-10 filers hold {b(inst['top10PctInst'], 0)}% of all institutional "
                                   f"shares (effective holder count ≈ {inst['effN']} of "
                                   f"{inst['holders']:,}). Concentrated books move the stock when one holder "
                                   "acts; a rename-inflated total can overstate the largest slices.",
                           "data": {"labels": [r["name"].title()[:22] for r in top10] + ["other filers"],
                                    "values": [round(r["pctInst"], 1) for r in top10]
                                    + [round(max(0.0, 100 - sum(r["pctInst"] for r in top10)), 1)],
                                    # the remainder is not a filer — grey, like the sector doughnut's
                                    # unclassified slice (v1.95 one-grey-for-non-data)
                                    "colors": [None] * len(top10) + ["#8a8f98"]}})

        def _hrow_style(r):
            if r["asof"] and r["asof"] < inst["asofMode"]:
                return "color:#8a8f98;font-style:italic"          # stale filing wins over heat
            if r["type"] == "new":
                return "background:rgba(55,138,221,0.18)"          # blue tint: new position
            a = min(0.35, abs(r["chgPct"] or 0) / 60)              # heat ∝ |Δ%|, capped
            if (r["chgShM"] or 0) > 0:
                return f"background:rgba(29,158,117,{a:.3f})"
            if (r["chgShM"] or 0) < 0:
                return f"background:rgba(226,75,74,{a:.3f})"
            return ""
        if inst["top15"]:
            panels.append({"type": "table", "title": f"Top {len(inst['top15'])} institutional holders",
                       "info": "Largest 13F filers by shares held. Δ = change vs the holder's prior "
                               "quarterly filing. Rows in grey filed for an OLDER quarter than the rest — "
                               "their Δ is stale. ◇ marks index-heavy managers whose flows largely "
                               "track index membership and fund flows, not a view on the company. Long "
                               "US-listed positions only — shorts, swaps and non-13F assets are invisible.",
                       "data": {"columns": ["Holder", "Shares (M)", "% inst", "Δ sh (M)", "Δ %", "As of"],
                                # v1.105: header hover hints (the .htip mechanism)
                                "colhints": {
                                    "% inst": "share of ALL 13F-reported institutional shares",
                                    "Δ sh (M)": "change in shares vs the holder's PRIOR quarterly filing",
                                    "Δ %": "the same change as a % of the prior position ('new' = initiated)",
                                    "As of": "the holder's filing quarter — grey italic rows filed for "
                                             "an OLDER quarter, so their Δ is stale"},
                                "rows": [[(r["name"].title() + (" ◇" if r["idx"] else "")),
                                          b(r["shM"], 1), b(r["pctInst"], 1) + "%",
                                          (f"{r['chgShM']:+,.1f}" if r["chgShM"] is not None else "—"),
                                          ("new" if r["type"] == "new" else
                                           (f"{r['chgPct']:+.1f}%" if r["chgPct"] is not None else "n/a")),
                                          r["asof"] or "—"] for r in inst["top15"]],
                                "rowstyle": [_hrow_style(r) for r in inst["top15"]],
                                # v1.107.1: the legend explains ALL the row tints, not just the marks
                                "legend": [{"sym": "■", "color": "#1D9E75",
                                            "label": "green = added since the prior filing · depth ∝ |Δ%|"},
                                           {"sym": "■", "color": "#E24B4A",
                                            "label": "red = trimmed"},
                                           {"sym": "■", "color": "#378ADD",
                                            "label": "blue = new position"},
                                           {"sym": "◇", "color": "#378ADD",
                                            "label": "index-heavy manager (mechanical flows)"},
                                           {"sym": "▪", "color": "#8a8f98",
                                            "label": "grey = filed for an older quarter (stale Δ)"}]}})
    if ins and ins["recent"]:
        _cut = (f" Newest {len(ins['recent'])} shown — {ins['recentTruncated']} older 12m events cut."
                if ins.get("recentTruncated") else "")
        _unp = (f" {ins['unpricedEvents']} unpriced grant/disposal events ride the aggregates, not the list."
                if ins.get("unpricedEvents") else "")
        # v1.104: row heat — HUE is the meaning (buy green / discretionary sale red), ALPHA is
        # the $ size vs the window's largest event (the 13F table's rule); mechanical rows
        # (stc / exercise / acq) stay untinted — they are not decisions. Buys are floored
        # visible: a small real buy is rarer and more informative than a big routine sale.
        _max_usd = max((r["usd"] or 0 for r in ins["recent"]), default=0) or 1.0

        def _irow_style(r):
            if r.get("stc") or r["cls"] not in ("buy", "sale"):
                return ""
            a = min(0.35, 0.05 + 0.30 * ((r["usd"] or 0) / _max_usd) ** 0.5)
            if r["cls"] == "buy":
                return f"background:rgba(29,158,117,{max(a, 0.15):.3f})"
            return f"background:rgba(226,75,74,{a:.3f})"
        panels.append({"type": "table",
                       "title": f"Insider activity · 12m · {len(ins['recent'])} events",
                       "info": f"EVERY priced person·day·action EVENT of the trailing 12 months, newest "
                               f"first, aggregated from {ins['nRows']:,} raw feed rows (the feed "
                               "fragments a single Form-4 execution into many price lots; ×N = lots "
                               "merged, price is the $-weighted average)." + _unp + _cut
                               + " Classes: buy = priced within 15% of that day's close; exercise = "
                               "priced well below it; acq = priced but unverifiable; ◦ = vest-linked "
                               "(likely sell-to-cover). Alpha Vantage provides no Form-4 transaction "
                               "codes — heuristics, not filings data.",
                       "data": {"scroll": True,
                                "columns": ["date", "insider", "title", "type", "shares", "avg price", "value"],
                                # v1.105: header hover hints (the .htip mechanism)
                                "colhints": {
                                    "type": "buy = open-market (priced ≈ that day's close) · sale = "
                                            "discretionary disposal · exercise = priced well below the "
                                            "close · acq = priced but unverifiable · ◦ = vest-linked "
                                            "sell-to-cover · ×N = price lots merged into one event",
                                    "shares": "total shares across the event's merged lots",
                                    "avg price": "$-weighted average price across the event's lots",
                                    "value": "summed $ over the event's priced lots"},
                                "rows": [[r["d"], r["who"][:24], _short_title(r["title"]),
                                          r["cls"] + (" ◦" if r.get("stc") else "")
                                          + (f" ×{r['nlots']}" if r.get("nlots", 1) > 1 else ""),
                                          f"{r['sh']:,.0f}",
                                          (f"${r['px']:,.2f}" if r["px"] else "—"),
                                          (_usd(r["usd"] / 1e6, signed=False) if r["px"] else "—")]
                                         for r in ins["recent"]],
                                "rowstyle": [_irow_style(r) for r in ins["recent"]],
                                "legend": [{"sym": "■", "color": "#1D9E75",
                                            "label": "green = open-market buy"},
                                           {"sym": "■", "color": "#E24B4A",
                                            "label": "red = discretionary sale · tint depth ∝ $ size (vs the largest 12m event)"},
                                           {"sym": "◦", "color": "#8a8f98",
                                            "label": "vest-linked (likely sell-to-cover) — untinted, mechanical"},
                                           {"sym": "×N", "color": "#8a8f98",
                                            "label": "price lots merged into one event"}]}})
    return over_extra, cards, panels


def render_etf_text(D):
    """ETF/fund text summary (kind='etf') — no income statement, so a compact profile read."""
    _lr = D.get("leveraged")
    _nm = D["name"] if (D.get("name") and D["name"] != f"{D['symbol']} ETF") else "ETF/fund"
    L = [f"{D['symbol']} · {_nm}" + ("" if _lr in (None, "NO", "")
         else " · leveraged" if str(_lr).upper() in ("YES", "TRUE") else f" · leveraged {_lr}")]
    _chg = D.get("changePct")
    L.append(f"price ${b(D['price'],2)} ({'+' if (_chg or 0)>=0 else ''}{b(_chg,2)}%)  ·  "
             f"net assets ${b(D['netAssetsB'],1)}B  ·  since {D.get('inceptionDate') or '?'}")
    L.append("")
    def _px(v, nd):   # a None percent reads "n/a", never "n/a%" (v1.92.2 — UUP turnover)
        return "n/a" if v is None else b(v, nd) + "%"
    L.append(f"PROFILE    expense ratio {_px(D.get('expenseRatioPct'),2)}  ·  div yield {_px(D.get('divYieldPct'),2)}  ·  "
             f"turnover {_px(D.get('turnoverPct'),0)}  ·  {D['nHoldings']} holdings"
             + (f"  ·  top-10 {b(D['conc10'],0)}%" if D.get("conc10") is not None else ""))
    ss = D.get("seriesStats")   # v1.92: the series lines (adjusted close, zero extra calls)
    if ss:
        L.append("RANGE      52w "
                 + (f"{b(ss['pos52'],0)}% of ${b(ss['wkLo'],2)}–${b(ss['wkHi'],2)}" if ss.get("pos52") is not None else "n/a")
                 + (f"  ·  vs 200d MA {ss['vs200Pct']:+.1f}%" if ss.get("vs200Pct") is not None else "")
                 + f"  ·  avg $vol(3m) {_usd(ss['advUsd3m'] / 1e6).lstrip('+')}")
        _rt = "  ·  ".join(f"{k} {ss['rets'][k]:+.1f}%" for k in ("1M", "3M", "6M", "1Y", "3Y", "ITD/y")
                           if ss["rets"].get(k) is not None)
        if _rt:
            L.append("RETURNS    " + _rt)
        L.append(f"DRAWDOWN   max {b(ss['maxDDPct'],0)}% (adjusted, full history)  ·  now {b(ss['curDDPct'],0)}% from peak")
    if D.get("sectors"):
        _sgap = round(100.0 - sum(s["weightPct"] for s in D["sectors"]), 1)   # the doughnut twin's number
        L.append("SECTORS    " + "  ·  ".join(f"{s['sector'].title()} {b(s['weightPct'],1)}%" for s in D["sectors"][:5])
                 + (f"  ·  unclassified {_sgap}%" if _sgap >= 0.5 else ""))
    if (D.get("dist") or {}).get("ttm"):
        _dd = D["dist"]
        L.append(f"DISTRIB    TTM ${b(_dd['ttm'],2)} ({_px(_dd['ttmYieldPct'],2)} of price)  ·  "
                 f"last {_dd['rows'][0]['ex']} ${b(_dd['rows'][0]['amt'],4)}")
    if D.get("sentiment"):
        s = D["sentiment"]
        _sp = f"last {s['spanD']}d" if s.get("spanD") else "14d"
        if s.get("n"):
            L.append(f"SENTIMENT  {'+' if s['avg']>=0 else ''}{s['avg']} {s['label']} ({_sp}, {s['n']} of {s.get('nAll', s['n'])} relevant: {s['bull']} bull / {s['neutral']} neu / {s['bear']} bear)")
        else:
            L.append(f"SENTIMENT  0 of {s.get('nAll', 0)} relevant (gate {REL_MIN}) · full feed {'+' if (s.get('avgAll') or 0) >= 0 else ''}{b(s.get('avgAll'),2)} over {s.get('nAll', 0)} articles")
    if D.get("holdings"):
        L.append("")
        L.append(f"{'TOP HOLDINGS':<28}{'WEIGHT':>8}")
        for h in D["holdings"][:10]:
            L.append(f"{(h['symbol']+' '+h['name'])[:28]:<28}{b(h['weightPct'],2)+'%':>8}")
    _vl = _vol_text_line(D)
    if _vl:
        L.append(_vl)
    L.append("")
    L.append("source: Alpha Vantage ETF_PROFILE + realtime quote · data analysis, not investment advice")
    return "\n".join(L)


def render_text(D):
    if D.get("kind") == "etf":
        return render_etf_text(D)
    L = []
    L.append(f"{D['symbol']} · {D['name']} · {D['exchange']} · {D['sector']}")
    _chg_s = (f"({D['changePct']:+.2f}%)" if D.get("changePct") is not None else "(chg n/a)")  # never "(+n/a%)"
    L.append(f"price ${b(D['price'],2)} {_chg_s}  ·  cap {dashboard.cap_str(_cap_live_b(D)) or 'n/a'}  ·  FY ends {D['fyEnd']} · as of {D['latestQuarter']}")
    L.append("")
    L.append(f"VALUATION  fwdP/E {b(D['fwdPE'])}  PEG {b(D['peg'],2)}  P/S {b(D['ps'])}  P/B {b(D['pb'])}  EV/EBITDA {b(D['evEbitda'])}  beta {b(D['beta'],2)}")
    # v1.82.0: the 52w-range / MA line (only the segments the OVERVIEW actually carries — no fake spans)
    _rp = _range_pos(D)
    _d50, _d200 = _dma_diff(D, "dma50"), _dma_diff(D, "dma200")
    _rseg = []
    if D.get("wkLow") is not None and D.get("wkHigh") is not None:
        # honest out-of-range wording (review P3 2026-07-13): the live quote can sit outside the
        # staler OVERVIEW bounds — a bare clamped "0% of range" beside a price line that contradicts
        # it reads as a bug; say below/above range instead.
        _rp_raw = _range_pos(D, clamp=False)
        _tag = (" (below range)" if _rp_raw is not None and _rp_raw < 0 else
                " (above range)" if _rp_raw is not None and _rp_raw > 100 else
                (f" ({b(_rp,0)}% of range)" if _rp is not None else ""))
        _rseg.append(f"52w ${b(D['wkLow'],0)}–${b(D['wkHigh'],0)}" + _tag)
    if _d50 is not None:
        _rseg.append(f"vs 50d {_d50:+.1f}%")
    if _d200 is not None:
        _rseg.append(f"vs 200d {_d200:+.1f}%")
    if _rseg:
        L.append("RANGE      " + "  ·  ".join(_rseg))
    _dv = ("no dividend" if D.get("divKnownZero") else
           "div yield n/a" if D.get("divYieldPct") is None else
           f"div yield {b(D['divYieldPct'],2)}% (${b(D['divPerShare'],2)}/sh, ex {D.get('exDivDate') or '?'})")
    L.append(f"GROWTH     rev YoY {_pct_s(D['revGrowthYoYPct'],1)}  ·  EPS YoY {_pct_s(D['epsGrowthYoYPct'],1)}  ·  {_dv}")
    if D.get("sharesFloatB") is not None or D.get("pctInstitutions") is not None:
        _own = [s for s in (
            (f"float {b(D['sharesFloatB'],2)}B sh" if D.get("sharesFloatB") is not None else None),
            (f"insiders {_pct_s(D['pctInsiders'],2)}" if D.get("pctInsiders") is not None else None),
            (f"institutions {_pct_s(D['pctInstitutions'],1)}" if D.get("pctInstitutions") is not None else None)) if s]
        L.append("OWNERSHIP  " + "  ·  ".join(_own))   # v1.126: only the segments that exist — no "n/aB sh"
    # v1.84 Phase B: the Ownership lines — absent when the slot is None (never an "n/a" line)
    ins = D.get("insiders")
    if ins:
        w6, w12 = ins["windows"]["6m"], ins["windows"]["12m"]
        L.append(f"INSIDERS   6m: bought {_usd(w6['buyUsdM'], signed=False)} ({w6['uniqBuyers']} buyers)"
                 f" · sold {_usd(w6['sellUsdM'], signed=False)} ({w6['discSells']} disc. + {w6['sells'] - w6['discSells']} vest-linked)"
                 f" · net {_usd(w6['netUsdM'])}  ·  12m net {_usd(w12['netUsdM'])} ({w12['t1Buys']} C-suite buys)"
                 + ("" if ins.get("priceCheck") else "  ·  price-check off"))
    inst = D.get("inst")
    if inst:
        FC, FS = inst["flowCount"], inst["flowShM"]
        _own13 = (f"{b(inst['pctOwn'],0)}% of shares" if inst.get("pctOwn") is not None
                  else "ownership % n/a")                                # review P3-1: no 'n/a% of shares'
        _eff = f" (eff ~{inst['effN']})" if inst.get("effN") is not None else ""
        L.append(f"13F        {_own13} · {inst['holders']:,} filers"
                 f" · top-10 hold {b(inst['top10PctInst'],0)}%{_eff}"
                 f" · as of {inst['asofMode']} ({b(inst['stalePctInst'],0)}% staler)")
        _bp = f" ({b(inst['breadthPct'],0)}% buy-side)" if inst.get("breadthPct") is not None else ""
        L.append(f"FLOW Q/Q   holders {FC['inc']:,} add / {FC['new']:,} new / {FC['dec']:,} trim / {FC['exit']:,} exit{_bp}")
        L.append(f"           shares {FS['inc']:+,.0f}M add · {FS['new']:+,.0f}M new · {FS['dec']:+,.0f}M trim"
                 f" · {FS['exit']:+,.0f}M exit → net {inst['netShM']:+,.0f}M ({inst['netExNewShM']:+,.0f}M ex-new)")
        if inst.get("renames"):
            _rn = inst["renames"]
            L.append(f"           ⚠ {len(_rn)} large initiation{'s' if len(_rn) > 1 else ''} look"
                     f"{'' if len(_rn) > 1 else 's'} like filer re-registration"
                     f"{'s' if len(_rn) > 1 else ''} ({_rn[0]['name'].title()}"
                     + (f"…, {sum(r['shM'] for r in _rn):,.0f}M sh" if len(_rn) > 1 else f", {_rn[0]['shM']:,.0f}M sh") + ")")
    if D.get("estimates"):
        e = D["estimates"]; cur, nxt = e.get("curFY"), e.get("nextFY")
        seg = []
        if cur: seg.append(f"FY EPS est {b(cur['eps'],2)} ({cur['n']} an)")
        if nxt: seg.append(f"next FY {b(nxt['eps'],2)}")
        rv = f"  ·  revisions 30d: {cur['up30']}\u2191/{cur['down30']}\u2193" if cur else ""
        if not seg:   # v1.126: a QUARTER-only payload printed a bare "EST" line while the HTML
            curq, nxtq = e.get("curQ"), e.get("nextQ")   # showed the quarter rows -- fall back
            if curq: seg.append(f"Q EPS est {b(curq['eps'],2)} ({curq['n']} an)")
            if nxtq: seg.append(f"next Q {b(nxtq['eps'],2)}")
            if curq: rv = f"  ·  revisions 30d: {curq['up30']}\u2191/{curq['down30']}\u2193"
        _ck = (f"  ·  (cached {D['estimatesCachedAsOf']} — this pull returned none)"
               if D.get("estimatesCachedAsOf") else "")
        if seg:
            L.append("EST        " + " \u2192 ".join(seg) + rv + _ck)
    _earn_bits = []
    if D.get("lastEarningsReportedDate"):
        _fq = f" (quarter {D['lastEarningsFiscalDate']})" if D.get("lastEarningsFiscalDate") else ""
        _earn_bits.append(f"latest report published {D['lastEarningsReportedDate']}{_fq}")
    if D.get("nextEarnings"):   # v1.83.0 EARNINGS_CALENDAR
        _ned = _earn_days(D["nextEarnings"])
        _earn_bits.append(f"next report {D['nextEarnings']}" + (f" (~{_ned}d out)" if _ned is not None else ""))
    if _earn_bits:
        L.append("EARNINGS   " + "  ·  ".join(_earn_bits))
    _vl = _vol_text_line(D)     # v1.87: the options-vol context (local store, zero calls)
    if _vl:
        L.append(_vl)
    _sl = _sector_text_line(D)  # v1.148: the sector-RS twin of the Overview tile
    if _sl:
        L.append(_sl)
    gm_t = "" if D.get("grossMarginTTM") is None else f"  gross {b(D['grossMarginTTM'],0)}%"
    _nc_t = _usdb(D["netCashNowB"], 1) + (f" @{D['netCashAsOf']}" if D.get("netCashAsOf") else "")
    L.append(f"QUALITY    ROE {_pct_s(D['roe'])}  net margin {_pct_s(D['profitMargin'])}  op margin {_pct_s(D['opMargin'])}{gm_t}"
             f"  ·  net cash {_nc_t}")
    fs = D.get("fScore")
    if fs or D.get("altmanZ") is not None:
        _fail = "" if not (fs and fs.get("failed")) else \
            f" (missed: {', '.join(fs['failed'][:3])}{'…' if len(fs['failed']) > 3 else ''})"
        z = D.get("altmanZ")
        _z = "n/a" if z is None else f"{z:.1f} ({'safe' if z > 2.99 else 'grey' if z > 1.81 else 'distress'})"
        L.append(f"HEALTH     F-Score {fs['score'] if fs else '?'}/9{_fail}  ·  Altman Z {_z}")
    _nde = D.get("netDebtEbitda")
    _dil = D.get("dilutionCagrPct")
    L.append(f"CASH       FCF conv {b(D.get('fcfConversion'),2)}  ·  FCF yield {_pct_s(D.get('fcfYieldPct'),1)}"
             f"  ·  SBC-adj FCF {_usdb(D.get('sbcAdjFcfTTMB'),1)}"
             + ("" if D.get('payoutFcfPct') is None else f"  ·  payout {b(D['payoutFcfPct'],0)}% of FCF")
             + f"  ·  net debt/EBITDA {(b(_nde,1) if _nde is not None else ('none (net cash)' if (D.get('netCashNowB') is not None and D['netCashNowB'] >= 0) else 'n/a'))}"
             + ("" if _dil is None else f"  ·  dilution {b(_dil,1)}%/yr"))   # v1.126: no n/a-with-unit tails
    L.append("")
    yrs = D["years"]
    L.append("TREND ($B)   " + "  ".join(f"FY{y[2:]:>5}" for y in yrs))
    def row(lab, arr, d=0):
        return f"{lab:<11}" + "  ".join(f"{b(v,d):>7}" for v in arr)
    L.append(row("revenue", D["revenueB"]))
    L.append(row("net income", D["netIncomeB"]))
    L.append(row("net margin%", D["netMarginPct"]))
    L.append(row("FCF", D["fcfB"]))
    L.append(row("buybacks", D["buybackB"]))
    L.append(row("dividends", D["dividendsB"]))
    L.append(row("R&D % rev", D["rdPctRev"]))

    L.append(row("shares(B)", D["sharesB"], 2))
    tt = D.get("ttmTrend")
    if tt and tt.get("quarters"):
        q6 = tt["quarters"][-6:]
        off = len(tt["quarters"]) - len(q6)
        L.append("")
        L.append("TTM ($B)   " + "".join(f"{q:>9}" for q in q6))

        def trow(name, vals, nd=0):
            cells = "".join(f"{b(v, nd):>9}" for v in vals[off:])
            return f"{name:<11}" + cells
        L.append(trow("revenue", tt["revenueB"]))
        L.append(trow("net mgn %", tt["netMarginPct"]))
        L.append(trow("FCF", tt["fcfB"], 1))
    L.append("")
    L.append("BALANCE ($B) " + "  ".join(f"FY{y[2:]:>5}" for y in yrs))
    L.append(row("assets", D["totalAssetsB"]))
    L.append(row("equity", D["equityB"]))
    L.append(row("net cash", D["netCashB"]))
    L.append(row("total debt", D["totalDebtB"]))
    L.append("")
    if D["epsSurprises"]:
        beats = sum(1 for s in D["epsSurprises"] if (s["surprisePct"] or 0) > 0)
        L.append(f"EPS SURPRISES (last {len(D['epsSurprises'])}q): {beats} beats")
        for s in D["epsSurprises"][:6]:
            L.append(f"  {s['date']}  EPS {b(s['eps'],2)} vs est {b(s['est'],2)}  ({'+' if (s['surprisePct'] or 0)>=0 else ''}{b(s['surprisePct'])}%)")
        L.append("")
    r = D["ratings"]; up = (100 * (D["analystTarget"] / D["price"] - 1)) if D["analystTarget"] and D["price"] else None
    _tgt_s = ("target n/a" if D.get("analystTarget") is None else
              f"target ${b(D['analystTarget'],0)}"
              + (f" ({'+' if up >= 0 else ''}{b(up,0)}% vs ${b(D['price'],0)})" if up is not None else ""))
    L.append(f"ANALYST    {_tgt_s}  ·  {r['strongBuy']} strong-buy / {r['buy']} buy / {r['hold']} hold / {r['sell']} sell / {r['strongSell']} strong-sell")   # v1.126: no "$n/a (+n/a%)"
    if D["sentiment"]:
        s = D["sentiment"]
        _sp = f"last {s['spanD']}d" if s.get("spanD") else "14d"
        if s.get("n"):
            L.append(f"SENTIMENT  {'+' if s['avg']>=0 else ''}{s['avg']} {s['label']} ({_sp}, {s['n']} of {s.get('nAll', s['n'])} relevant: {s['bull']} bull / {s['neutral']} neu / {s['bear']} bear)")
        else:
            _aa = s.get("avgAll")
            L.append(f"SENTIMENT  0 of {s.get('nAll', 0)} relevant (gate {REL_MIN}) · full feed {'+' if (_aa or 0) >= 0 else ''}{b(_aa,2)} over {s.get('nAll', 0)} articles")
    L.append("")
    L.append("source: Alpha Vantage · split-adjusted · data analysis, not investment advice")
    return "\n".join(L)


# ----------------- svg -----------------
def _render_etf_svg(D):
    """Compact ETF profile SVG (kind='etf') — the rich fund view is --html; this keeps every output
    mode valid (--svg/--png/default) with a readable card+holdings summary."""
    W, H = 1320, 672
    s = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" font-family="-apple-system,Segoe UI,Helvetica,sans-serif" role="img">',
         f'<title>{esc(D["symbol"])} ETF profile</title>',
         f'<rect width="{W}" height="{H}" fill="#ffffff"/>']

    def t(x, y, txt, sz=12, c=INK, w=400, a="start"):
        s.append(f'<text x="{x}" y="{y}" font-size="{sz}" font-weight="{w}" fill="{c}" text-anchor="{a}">{esc(txt)}</text>')
    t(30, 42, f"{D['symbol']} ETF", 24, INK, 800)
    t(30, 64, f"net assets ${b(D.get('netAssetsB'),1)}B · expense {b(D.get('expenseRatioPct'),2)}% · "
              f"yield {b(D.get('divYieldPct'),2)}% · {D.get('nHoldings',0)} holdings · Alpha Vantage ETF_PROFILE", 12, GRA)
    y = 110
    t(30, y, "Top holdings", 14, INK, 700)
    y += 26
    for h in (D.get("holdings") or [])[:12]:
        t(40, y, f"{h['symbol']}  {(h['name'] or '')[:34]}", 12, INK)
        t(360, y, f"{b(h['weightPct'],2)}%", 12, INK, 600, "end")
        y += 22
    ys = 110
    t(700, ys, "Sector weights", 14, INK, 700)
    ys += 26
    for sec in (D.get("sectors") or [])[:11]:
        t(710, ys, sec["sector"].title()[:32], 12, INK)
        t(1000, ys, f"{b(sec['weightPct'],1)}%", 12, INK, 600, "end")
        ys += 22
    s.append("</svg>")
    return "\n".join(s)


def render_svg(D):
    if D.get("kind") == "etf":
        return _render_etf_svg(D)
    W, H = 1320, 672
    s = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" font-family="-apple-system,Segoe UI,Helvetica,sans-serif" role="img">']
    s.append(f'<title>{esc(D["symbol"])} fundamentals dashboard</title>')
    s.append(f'<rect width="{W}" height="{H}" fill="#ffffff"/>')
    def t(x, y, txt, sz=12, c=INK, w=400, a="start"):
        s.append(f'<text x="{x}" y="{y}" font-size="{sz}" font-weight="{w}" fill="{c}" text-anchor="{a}">{esc(txt)}</text>')
    def card(x, y, w, h, title):
        s.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="#f6f7f9"/>')
        t(x + 16, y + 24, title, 13, INK, 700)
    yrs = D["years"]; n = len(yrs)

    def bars(x, y, w, h, vals, color, fmt="{:.0f}", overlay=None, ocolor=None,
             line=None, lmax=None, llab="", line_minmax=False):
        x0, x1 = x + 44, x + w - (54 if line else 16); base, top = y + h - 26, y + 40
        allv = [v for v in (list(vals) + list(overlay or [])) if v is not None] or [0]
        hi, lo = max(allv + [0]), min(allv + [0])
        span = (hi - lo) or 1
        hi += span * 0.16
        if lo < 0:
            lo -= span * 0.16
        yv = lambda v: base - (v - lo) / (hi - lo) * (base - top)
        z = yv(0)  # zero baseline (floats when there are negatives)
        s.append(f'<line x1="{x0}" y1="{yv(hi):.0f}" x2="{x1}" y2="{yv(hi):.0f}" stroke="#eceef1"/>')
        s.append(f'<line x1="{x0}" y1="{z:.0f}" x2="{x1}" y2="{z:.0f}" stroke="#dcdfe3"/>')
        slot = (x1 - x0) / n

        def draw(series, c, frac, lab):
            for i in range(n):
                v = series[i]
                if v is None:
                    continue
                cx = x0 + slot * (i + 0.5); bw = slot * frac; yy = yv(v)
                ytop, hh = min(yy, z), max(abs(yy - z), 1)
                s.append(f'<rect x="{cx-bw/2:.1f}" y="{ytop:.1f}" width="{bw:.1f}" height="{hh:.1f}" rx="3" fill="{c}"/>')
                if lab:
                    t(cx, (ytop - 5 if v >= 0 else ytop + hh + 11), fmt.format(v), 10, INK, 700, "middle")

        draw(vals, color, 0.55, True)
        if overlay is not None:
            draw(overlay, ocolor or NI, 0.3, False)
        for i, yr in enumerate(yrs):
            t(x0 + slot * (i + 0.5), base + 16, "FY" + yr[2:], 10, GRA, 500, "middle")
        if line:
            lv = [v for v in line if v is not None] or [0, 1]
            if line_minmax and max(lv) != min(lv):
                llo, lhi = min(lv), max(lv); lp = (lhi - llo) * 0.4 or 1; llo -= lp; lhi += lp
            else:
                llo, lhi = 0, (lmax or max(lv + [1]) * 1.15)
            ym = lambda v: base - (v - llo) / (lhi - llo) * (base - top)
            pts = " ".join(f'{x0+slot*(i+0.5):.1f},{ym(line[i]):.1f}' for i in range(n) if line[i] is not None)
            s.append(f'<polyline points="{pts}" fill="none" stroke="{BLUE}" stroke-width="2"/>')
            for i in range(n):
                if line[i] is not None:
                    s.append(f'<circle cx="{x0+slot*(i+0.5):.1f}" cy="{ym(line[i]):.1f}" r="2.5" fill="{BLUE}"/>')
            if line[-1] is not None:
                t(x1 + 6, ym(line[-1]) + 4, f'{line[-1]:.0f}{llab}', 10, BLUE, 700)

    # header
    t(30, 40, D["symbol"], 26, INK, 800)
    t(30 + 15 * len(D["symbol"]) + 14, 40, D["name"] or "", 16, GRA, 500)
    t(1290, 34, f'${b(D["price"],2)}', 22, INK, 800, "end")
    chgc = TEAL if (D["changePct"] or 0) >= 0 else RED
    _svg_chg = (f'{D["changePct"]:+.2f}%' if D.get("changePct") is not None else "chg n/a")  # never "+n/a%"
    t(1290, 54, f'{_svg_chg} · cap {dashboard.cap_str(_cap_live_b(D)) or "n/a"}', 13, chgc, 600, "end")
    t(30, 60, f'Alpha Vantage · {D["exchange"]} · FY ends {D["fyEnd"]} · as of {D["latestQuarter"]}', 11, GRA)

    # metric strip (9 cards)
    gm = D["grossMarginTTM"] if D.get("grossMarginTTM") is not None else D["grossMarginPct"][-1]
    mets = [("Fwd P/E", b(D["fwdPE"])), ("PEG", b(D["peg"], 2)),
            ("Gross margin", (b(gm, 0) + "%") if gm is not None else "n/a"),
            ("Net margin", b(D["profitMargin"], 0) + "%"), ("ROE", b(D["roe"], 0) + "%"),
            ("Net cash", _usdb(D["netCashNowB"], 0)), ("Beta", b(D["beta"], 2)),
            ("Div yield", (b(D["divYieldPct"], 2) + "%") if D.get("divYieldPct") else "\u2014"),
            ("Rev YoY", (b(D["revGrowthYoYPct"], 1) + "%") if D.get("revGrowthYoYPct") is not None else "n/a")]
    cw = (W - 60) / len(mets)
    for i, (lab, val) in enumerate(mets):
        x = 30 + i * cw
        s.append(f'<rect x="{x:.0f}" y="76" width="{cw-10:.0f}" height="56" rx="8" fill="#f6f7f9"/>')
        t(x + 14, 100, lab, 11, GRA, 500); t(x + 14, 122, val, 18, INK, 800)

    # ---- 3-column grid: row 1 (y=148, h=210), row 2 (y=374, h=190) ----
    C1, C2, C3, CW = 30, 460, 890, 400
    # row 1
    card(C1, 148, CW, 210, "Revenue & net income ($B)")
    t(252, 172, "■ rev", 9, TEAL, 700); t(296, 172, "■ net inc", 9, NI, 700); t(362, 172, "— mgn%", 9, BLUE, 700)
    bars(C1, 148, CW, 210, D["revenueB"], UP, "{:.0f}", overlay=D["netIncomeB"], ocolor=NI,
         line=D["netMarginPct"], llab="%", line_minmax=True)
    card(C2, 148, CW, 210, "Free cash flow ($B)")
    bars(C2, 148, CW, 210, D["fcfB"], TEAL)
    card(C3, 148, CW, 210, "Balance ($B)")
    t(1148, 172, "■ assets", 9, GRA, 700); t(1212, 172, "■ equity", 9, NI, 700)
    bars(C3, 148, CW, 210, D["totalAssetsB"], "#cfe9dd", "{:.0f}", overlay=D["equityB"], ocolor=NI)
    # row 2
    card(C1, 374, CW, 190, "Stock comp ($B) · % of revenue")
    t(360, 398, "— % rev", 9, BLUE, 700)
    bars(C1, 374, CW, 190, D["sbcB"], "#EFB366", "{:.0f}", line=D["sbcPctRev"], llab="%")
    card(C2, 374, CW, 190, "Buybacks ($B) · shares (B, line)")
    bars(C2, 374, CW, 190, D["buybackB"], "#7fc8a9", "{:.0f}", line=D["sharesB"], llab="B", line_minmax=True)
    card(C3, 374, CW, 190, "EPS surprise (last quarters)")
    es = list(reversed(D["epsSurprises"][:6]))
    es = [q for q in es if q.get("surprisePct") is not None]   # "no estimate" must not draw "+0%"
    if es:
        ex0, ex1 = C3 + 20, C3 + CW - 16; ebase = 374 + 190 - 28; etop = 374 + 44
        # cap the scale so one outlier (e.g. -14,890% on a near-zero estimate) doesn't
        # flatten the rest; the bar clamps to ±cap, the label keeps the truth.
        cap = min(max((abs(q["surprisePct"]) for q in es), default=1), 60) or 1
        clamped = [max(-cap, min(cap, sp["surprisePct"])) for sp in es]
        # adaptive zero baseline (like bars()): all-positive -> zero at the bottom so the
        # bars fill the panel; all-negative -> top; mixed -> floats. No wasted half.
        hi, lo = max(clamped + [0]), min(clamped + [0]); span = (hi - lo) or 1
        hi += span * 0.18
        if lo < 0:
            lo -= span * 0.18
        yv = lambda v: ebase - (v - lo) / (hi - lo) * (ebase - etop)
        z = yv(0)
        eslot = (ex1 - ex0) / len(es)
        def plab(v):
            return f'{v/1000:+.0f}k%' if abs(v) >= 1000 else f'{v:+.0f}%'
        for i, sp in enumerate(es):
            v = sp["surprisePct"]; cx = ex0 + eslot * (i + 0.5); bw = eslot * 0.5
            yy = yv(clamped[i]); ytop, hh = min(yy, z), max(abs(yy - z), 1)
            s.append(f'<rect x="{cx-bw/2:.1f}" y="{ytop:.1f}" width="{bw:.1f}" height="{hh:.1f}" rx="2" fill="{TEAL if v>=0 else RED}"/>')
            t(cx, (ytop - 4 if v >= 0 else ytop + hh + 11), plab(v), 8.5, INK, 600, "middle")
            t(cx, ebase + 14, sp["date"][2:7], 8.5, GRA, 500, "middle")
        s.append(f'<line x1="{ex0}" y1="{z:.0f}" x2="{ex1}" y2="{z:.0f}" stroke="#dcdfe3"/>')

    # ---- bottom row: analyst | sentiment ----
    card(30, 578, 625, 78, "Analyst view")
    up = (100 * (D["analystTarget"] / D["price"] - 1)) if D["analystTarget"] and D["price"] else None
    t(48, 626, f'Target ${b(D["analystTarget"],0)}', 16, INK, 800)
    upc = TEAL if (up or 0) >= 0 else RED
    t(48, 645, f'{"+" if (up or 0)>=0 else ""}{b(up,0)}% vs ${b(D["price"],0)}', 12, upc, 700)
    r = D["ratings"]; tot = sum(r.values()) or 1; rx = 320; rw = 320
    for val, c in ((r["strongBuy"], NI), (r["buy"], "#7fc8a9"), (r["hold"], AMBER), (r["sell"] + r["strongSell"], RED)):
        w_ = val / tot * rw
        s.append(f'<rect x="{rx:.1f}" y="612" width="{w_:.1f}" height="16" fill="{c}"/>'); rx += w_
    t(320, 645, f'{r["strongBuy"]} strong-buy · {r["buy"]} buy · {r["hold"]} hold · {r["sell"]+r["strongSell"]} sell', 10, GRA, 500)

    se = D["sentiment"]
    _set = (f"News sentiment · last {se['spanD']}d" if se and se.get("spanD") else "News sentiment · 14d")
    card(695, 578, 595, 78, _set)
    if se and se.get("n"):
        # composed like "Analyst view": value block left (2 lines), bar right half, counts under bar
        t(713, 626, f'{"+" if se["avg"]>=0 else ""}{se["avg"]:.2f}', 16, TEAL if se["avg"] >= 0 else RED, 800)
        t(713, 645, se["label"], 12, INK, 700)
        sx = 985; sw = 290; st = se["bull"] + se["neutral"] + se["bear"] or 1
        for val, c in ((se["bull"], TEAL), (se["neutral"], "#c7ccd3"), (se["bear"], RED)):
            w_ = val / st * sw; s.append(f'<rect x="{sx:.1f}" y="612" width="{w_:.1f}" height="16" rx="2" fill="{c}"/>'); sx += w_
        t(985, 645, f'{se["bull"]} bull · {se["neutral"]} neu · {se["bear"]} bear ({se["n"]} of {se.get("nAll", se["n"])})', 9.5, GRA, 500)
    elif se:
        _aa = se.get("avgAll")
        t(713, 626, f'0 of {se.get("nAll", 0)} relevant', 14, GRA, 700)
        if _aa is not None:
            t(713, 645, f'full feed {"+" if _aa >= 0 else ""}{_aa:.2f} over {se.get("nAll", 0)} articles', 10, GRA, 500)
    else:
        t(713, 626, "no news data", 12, GRA)

    t(30, H - 12, "Alpha Vantage · split-adjusted · data analysis, not investment advice", 10, GRA)
    s.append("</svg>")
    return "\n".join(s)


# ----------------- responsive html (Chart.js) -----------------
# For RESIZABLE targets: inline chat widget, an expandable artifact/app view, and the
# Claude Code Preview Pane. The chart-panel grid reflows ONE column when narrow/tall and
# TWO columns from 680px up (capped at two — a financial dashboard's dense bar panels stay
# wide and readable, instead of auto-fit packing 3-4 cramped columns on a wide screen).
# `min-width:0` on the panels/canvas wrappers lets the tracks actually shrink, so the
# Chart.js canvases never jam the grid wide. The SVG renderer stays for PNG / portable
# file / agent screenshot-verify.
def _qlab(d):
    """'2024-02-29' -> '24Q1' — the calendar quarter of the fiscal-quarter END date (v1.108:
    quarter charts label like a trading terminal; the engine's xQuarter ticks show the year
    alone when zoomed out and this full label when zoomed in)."""
    try:
        return f"{str(d)[2:4]}Q{(int(str(d)[5:7]) - 1) // 3 + 1}"
    except (ValueError, IndexError):
        return str(d)[2:7]


def _est_rows(est):
    """Estimates dict -> ordered detail rows for the Analyst tile (Q first, then FYs)."""
    if not est:
        return []
    rows = []
    for k, r in (("curQ", est.get("curQ")), ("nextQ", est.get("nextQ")),
                 ("curFY", est.get("curFY")), ("nextFY", est.get("nextFY"))):
        if not r or r.get("eps") is None:
            continue
        d_ = str(r.get("date") or "")
        rows.append(dict(r, label=(f"Q {d_[:7]}" if k in ("curQ", "nextQ") else f"FY{d_[:4]}")))
    return rows


def to_etf_spec(D):
    """ETF/fund dashboard SPEC (kind='etf'): profile cards + price kline + sector-weights bars +
    a top-holdings table. Reuses the shared dashboard.py block types — no company statements."""
    cards = [
        {"label": "Expense ratio", "value": (b(D["expenseRatioPct"], 2) + "%") if D.get("expenseRatioPct") is not None else "n/a",
         "hint": "Annual net expense ratio (ETF_PROFILE) — the fee drag on returns. Lower is cheaper."},
        {"label": "Net assets", "value": ("$" + b(D["netAssetsB"], 1) + "B") if D.get("netAssetsB") is not None else "n/a",
         "hint": "Total net assets under management (fund size / liquidity proxy)."},
        {"label": "Div yield", "value": (b(D["divYieldPct"], 2) + "%") if D.get("divYieldPct") is not None else "—",
         "hint": "Trailing distribution yield (ETF_PROFILE). — = none reported.",
         # v1.96: the computed twin — TTM distributions over the LIVE price (the profile figure
         # can lag); absent when the fund distributes nothing
         **({"sub": f"TTM ${b(D['dist']['ttm'], 2)} · {b(D['dist']['ttmYieldPct'], 2)}%"}
            if (D.get("dist") or {}).get("ttm") else {})},
        {"label": "Turnover", "value": (b(D["turnoverPct"], 0) + "%") if D.get("turnoverPct") is not None else "n/a",
         "hint": "Portfolio turnover — how much of the book trades per year. High turnover can mean more tax drag."},
        {"label": "Holdings", "value": (str(D["nHoldings"]) if D.get("nHoldings") else "—"),
         "hint": "Number of holdings in the fund (breadth / diversification). — = the profile "
                 "reports no constituents (physical/futures trusts hold bullion or contracts, "
                 "not securities)."},
        {"label": "Inception", "value": (D.get("inceptionDate") or "n/a"),
         "hint": "Fund inception date" + ("" if D.get("leveraged") in (None, "") else f" · leveraged: {D['leveraged']}") + "."},
    ]
    if D.get("conc10") is not None:
        cards.append({"label": "Top-10 weight", "value": b(D["conc10"], 0) + "%",
                      "hint": "Combined weight of the 10 largest holdings — how concentrated the "
                              "fund is. High = a few names drive the ride."})
    ss = D.get("seriesStats")   # v1.92 — from the already-fetched daily series (adjusted close)
    if ss:
        if ss.get("pos52") is not None:
            cards.append({"label": "52w range", "value": b(ss["pos52"], 0) + "%",
                          "sub": [f"${b(ss['wkLo'], 2)}–${b(ss['wkHi'], 2)}"],
                          "hint": "Where the price sits in its trailing 52-week span (adjusted "
                                  "close): 0% = at the low, 100% = at the high."})
        if ss.get("vs200Pct") is not None:
            cards.append({"label": "vs 200d MA", "value": f"{ss['vs200Pct']:+.1f}%",
                          "hint": "Distance from the 200-day moving average — the long-trend "
                                  "gauge. Above = uptrend territory, below = downtrend."})
        cards.append({"label": "Max drawdown", "value": b(ss["maxDDPct"], 0) + "%",
                      "sub": [f"now {b(ss['curDDPct'], 0)}% from peak"],
                      "hint": "Deepest peak-to-trough loss over the fetched history (adjusted "
                              "close, dividends folded in) — the worst ride a holder took."})
        cards.append({"label": "Avg $ vol · 3m", "value": "$" + _usd(ss["advUsd3m"] / 1e6).lstrip("+$−"),
                      "hint": "Average daily dollar volume over ~3 months — the liquidity gauge "
                              "(how much can trade without moving the price)."})
    panels = []
    _vpe = _vol_panel(D)
    if _vpe:
        panels.append(_vpe)
    if ss:
        _rc = [k for k in ("1M", "3M", "6M", "1Y", "3Y", "ITD/y") if ss["rets"].get(k) is not None]
        if _rc:
            panels.append({"type": "table", "title": "Returns — adjusted close · ITD/y = CAGR since inception",
                           "info": "Trailing total returns from the adjusted daily series (dividends "
                                   "folded in). ITD/y annualizes the whole fetched history.",
                           "data": {"columns": _rc, "big": True,
                                    "rows": [[f"{ss['rets'][k]:+.1f}%" for k in _rc]],
                                    "cellstyle": [[("color:#1D9E75" if ss["rets"][k] >= 0 else "color:#C0392B")
                                                   + ";font-weight:700" for k in _rc]]}})
    po = D.get("priceOHLC")
    if po and po.get("frames"):
        panels.append({"type": "kline", "full": True, "collapsible": True,
                       "title": f"{D['symbol']} ETF price chart (split-adjusted)",
                       "indicators": ["MA", "EMA", "MACD", "RSI", "BOLL", "VOL"],
                       "data": dict(po, on=["VOL"], barSpace=8)})
    if ss and ss.get("ddValues"):
        panels.append({"type": "line", "title": "Drawdown from peak (%) — adjusted close",
                       "info": "How far below its running all-time high the fund traded (monthly-"
                               "resampled, dividends folded in). The depth and LENGTH of the holes "
                               "is the risk a holder actually lived through.",
                       # xYear (v1.98): a 20y monthly axis printed "2006-05, 2007-11, ..." — the
                       # month is noise at that zoom; ticks show the year once, tooltips keep YYYY-MM
                       "data": {"labels": ss["ddLabels"], "xYear": True,
                                "series": [{"name": "drawdown %", "color": "#C0392B",
                                            "values": ss["ddValues"], "fill": True}]}})
    if D.get("sectors"):
        # v1.95: composition, not ranking -> doughnut; the UNCLASSIFIED remainder is REAL (AV maps
        # only the GICS-mappable sleeve — probed live: SPY 96.3%, GDX 32.4%, MSOS 9.9% classified)
        # and gets its own slice, never normalized away; sub-0.5pt rounding dust bakes no fake hole
        labs = [s["sector"].title() for s in D["sectors"]]
        vals = [s["weightPct"] for s in D["sectors"]]
        cols = [None] * len(labs)          # None -> engine palette; the remainder is NOT data -> grey
        gap = round(100.0 - sum(vals), 2)
        if gap >= 0.5:
            labs.append("Unclassified / non-equity")
            vals.append(gap)
            cols.append("#8a8f98")
        panels.append({"type": "doughnut", "title": "Sector weights (%)", "info":
            "GICS weights as REPORTED by Alpha Vantage's fund-sector feed — near-complete for "
            "flagship US funds (SPY ~96%), partial for specialist/international ones (GDX 32%, "
            "KWEB 0%; probed 2026-07-15). The feed is NOT derived from the holdings list, so the "
            "grey 'Unclassified' slice mixes genuinely non-classifiable sleeves (swaps, cash, "
            "T-bill collateral, bullion) with foreign/OTC names the feed simply failed to map. "
            "Shown as-is, never normalized — the holdings bars below are the reliable view.",
            "data": {"labels": labs, "values": vals, "colors": cols}})
    if D.get("holdings"):
        # v1.99: the table became horizontal WEIGHT BARS — the ladder shape is the point
        # (a 12.5% anchor vs 0.07% swap tails reads instantly); exact weights stay in the
        # tooltip and the text twin. "n/a" placeholders (swaps/cash rows) never leak.
        top = D["holdings"][:15]

        def _hlabel(h):
            nm = (h.get("name") or "").strip()
            sym_ = (h.get("symbol") or "").strip()
            nm = "" if nm.lower() == "n/a" else nm.title()[:30]
            sym_ = "" if sym_.lower() == "n/a" else sym_
            return (f"{nm} · {sym_}" if nm and sym_ else nm or sym_ or "—")
        panels.append({"type": "bars", "title": f"Top {len(top)} holdings · weight %", "info":
            "Largest positions by weight (ETF_PROFILE), heaviest on top. The top names drive "
            "most of the fund's day-to-day move; exact weights are in the hover and the text "
            "summary. Swap/cash sleeves often carry no ticker.",
            "data": {"labels": [_hlabel(h) for h in top], "horizontal": True,
                     "series": [{"name": "weight %", "color": "#2a78d6",
                                 "values": [h["weightPct"] for h in top]}]}})
    if D.get("dist"):
        # v1.96/98: distribution history (AV DIVIDENDS — works for ETFs; a non-distributing fund
        # has dist=None and no panel). Per-YEAR totals as HORIZONTAL rows, newest on top — pairs
        # with the tall news list, and a single-payment fund (MSOS: one $0.07 in 2021) reads as
        # one honest row, not a broken-looking fat bar.
        _by = D["dist"]["byYear"]
        panels.append({"type": "bars", "title": "Distributions per share", "info":
            "Cash distributions per share, summed per calendar year (AV DIVIDENDS). The TTM sum "
            "over the live price is the indicated yield on today's buy — compare it with the "
            "profile's trailing figure on the Div-yield card; the exact last ex-date is in the "
            "text summary.",
            "data": {"labels": [r["y"] for r in _by], "horizontal": True,
                     "series": [{"name": "$ / share · year", "color": UP, "values": [r["amt"] for r in _by]}]}})
    if D.get("sentiment") or D.get("articles"):
        # v1.96: the news the gather already pulls, on the ETF page too (same block as companies)
        panels.append({"type": "sentibar",
                       "title": (f"News sentiment · last {(D.get('sentiment') or {}).get('spanD')}d"
                                 if (D.get("sentiment") or {}).get("spanD") else "News sentiment · 14d"),
                       "info": "Per-article sentiment for this ticker, averaged over articles past the "
                               f"{REL_MIN} relevance gate; the grey line is the ungated full-feed mean.",
                       "data": dict(D.get("sentiment") or {}, articles=D.get("articles") or [], relmin=REL_MIN)})
    # AV's flag value is the bare "YES" — the page must say the WORD, not echo "· YES";
    # a future informative value ("3x") would ride along
    _lraw = D.get("leveraged")
    _lev = ("" if _lraw in (None, "", "NO")
            else " · leveraged" if str(_lraw).upper() in ("YES", "TRUE") else f" · leveraged {_lraw}")
    _chg = f"{D['changePct']:+.2f}%" if D.get("changePct") is not None else ""
    # v1.97: the About LEADS with the fund's identity (the SYMBOL_SEARCH name — "AdvisorShares
    # Pure US Cannabis ETF" says what MSOS is; the old text said nothing) and is promoted to
    # the top full-width tile (about.top). A name-less fund keeps the generic opener.
    _named = D.get("name") and D["name"] != f"{D['symbol']} ETF"
    _abopen = (f"{D['symbol']} — {D['name']}. " if _named
               else f"{D['symbol']} is an exchange-traded fund. ")
    return {
        "about": {"text": _abopen + "Profile, holdings and sector weights are "
                          "Alpha Vantage ETF_PROFILE values, read as-is; it has no company income statement — "
                          "for issuer fundamentals analyze a specific holding.",
                  "cue": "ETF profile · verify with the issuer", "top": True},
        "title": f"{D['symbol']} ETF · profile{_lev}",
        # v1.121: the STACKED v1.110 header (fund name under the ticker, board type below) — the
        # v1.96 leveraged PILL is gone per the v1.110 no-pills decision (user 2026-07-18, UVXY);
        # the word still rides the TITLE, the Inception card hint, and the text PROFILE line —
        # TQQQ-class decay stays visible, just not as a chip.
        "head": {"leftBig": D["symbol"], "leftName": (D["name"] if _named else ""),
                 "leftType": "ETF fundamentals",
                 "rightBig": f"${b(D.get('price'), 2)}", "rightSmall": _chg},
        "subtitle": f"Alpha Vantage ETF_PROFILE · net assets ${b(D.get('netAssetsB'),1)}B · not advice",
        # v1.126: the ETF footer carries the provider provenance like the company/options pages
        # (was subtitle-only — the v1.112 dataSource segment never reached the ETF board)
        "dataSource": "Alpha Vantage",
        "cards": cards, "panels": panels,
    }


def _session_card(D):
    """v1.109: the equity trading-session tile for the Overview tab — current phase (pre-market /
    open / after-hours / closed) with a LIVE countdown to the next regular open/close, client-ticked
    from the embedded schedule (dashboard.py tickSessions/.sess-cd — same machinery as the options
    Session tile). This is the STOCK session: regular 09:30–16:00 ET PLUS the pre-market/after-hours
    the equity itself trades — which its OPTIONS do NOT (deliberately a DIFFERENT market from the
    options chain's tile). Driven by the company's OWN listing venue from OVERVIEW (no assume_us), so
    a non-US / unknown listing returns None and the tile is simply omitted — never a faked US session."""
    try:
        import opthist
        sess = opthist.market_session(D.get("exchange"))   # the stock's real venue; non-US -> None
    except Exception:
        return None
    if not sess:
        return None
    return {"label": "Session · " + sess["exchange"], "value": sess["status"], "small": True,
            "countdown": {"schedule": sess["schedule"], "text": sess["countdownText"]},
            "hint": ("Equity trading session for %s. Regular %s (%s local); the STOCK also trades "
                     "pre-market from %s and after-hours to %s — its OPTIONS do not (a different "
                     "session). Now %s / %s. Live countdown to the next regular open/close; holidays "
                     "are not checked (weekends are)."
                     % (sess["exchange"], sess["hoursExch"], sess["hoursLocal"], sess["preEt"],
                        sess["postEt"], sess["nowExch"], sess["nowLocal"]))}


def to_spec(D):
    """Ф7 F1: the fundamentals dashboard as a dashboard.py SPEC — cards (with hover hints)
    + panels. Deep series feed the engine's range zoom; every layout/interaction feature
    (combo z-order, zoom semantics, tooltips, units, full rows) is THE ENGINE'S, not ours."""
    if D.get("kind") == "etf":
        return to_etf_spec(D)
    dp = D.get("deep") or {}
    eq = D.get("epsDeep") or {"labels": [], "pct": []}
    gm = D["grossMarginTTM"] if D.get("grossMarginTTM") is not None else (
        D["grossMarginPct"][-1] if D.get("grossMarginPct") else None)
    fs = D.get("fScore")
    fs_hint = ("Piotroski F-Score: 9 pass/fail accounting-quality checks (profitability, cash "
               "flow, leverage, margins, dilution). \u22657 strong, \u22643 weak."
               + ((" Missed: " + ", ".join(fs["failed"]) + ".") if fs and fs.get("failed") else ""))
    cards = [
        {"label": "Fwd P/E", "value": b(D["fwdPE"]), "hint": "Price / next-12m consensus EPS. For cyclicals a very low fwd P/E often means the market expects earnings to roll over."},
        {"label": "PEG", "value": b(D["peg"], 2), "hint": "P/E divided by expected EPS growth. <1 = growth priced cheaply; unreliable at cycle peaks."},
        {"label": "Gross margin", "value": (b(gm, 0) + "%") if gm is not None else "n/a",
         "hint": "Gross profit / revenue. Basis: "
                 + ((D.get("marginBasis") or {}).get("gross") or "computed TTM (last 4 quarterly reports)") + "."},
        {"label": "Net margin", "value": (b(D["profitMargin"], 0) + "%") if D.get("profitMargin") is not None else "n/a",
         "hint": "Net income / revenue. Basis: "
                 + ((D.get("marginBasis") or {}).get("net") or "computed TTM (last 4 quarterly reports)") + "."},
        {"label": "ROE", "value": (b(D["roe"], 0) + "%") if D.get("roe") is not None else "n/a",
         "hint": "Return on equity (TTM, Alpha Vantage OVERVIEW field). High ROE at a cycle peak reflects peak earnings, not durable quality."},
        {"label": "Net cash", "value": _usdb(D["netCashNowB"], 0), "hint": f"Cash + short/long-term investments \u2212 total debt, latest QUARTERLY balance ({D.get('netCashAsOf', '')}). Negative = net debt."},
        {"label": "Beta", "value": b(D["beta"], 2), "hint": "Volatility vs the market: 2.0 \u2248 moves twice the S&P's swing, both ways."},
        {"label": "Div yield", "value": (b(D["divYieldPct"], 2) + "%") if D.get("divYieldPct") else "\u2014", "hint": "Annual dividend / price. \u2014 means no dividend."},
        {"label": "Rev YoY", "value": (b(D["revGrowthYoYPct"], 1) + "%") if D.get("revGrowthYoYPct") is not None else "n/a", "hint": "Latest quarter revenue vs the same quarter a year ago."},
        {"label": "F-Score", "value": f"{fs['score']}/9" if fs else "n/a", "hint": fs_hint},
        {"label": "Altman Z", "value": b(D["altmanZ"], 1) if D.get("altmanZ") is not None else "n/a", "hint": "Altman Z-score, a bankruptcy-risk composite: >3 safe zone, 1.8\u20133 grey zone, <1.8 distress."},
        {"label": "FCF yield", "value": (b(D["fcfYieldPct"], 1) + "%") if D.get("fcfYieldPct") is not None else "n/a", "hint": "Trailing-12m free cash flow / market cap \u2014 the cash analogue of earnings yield."},
    ]
    # v1.82.0: 52w range + price-vs-MA \u2014 OVERVIEW fields parsed since v1.47 but shown by NO output.
    _rp = _range_pos(D)
    _d200, _d50 = _dma_diff(D, "dma200"), _dma_diff(D, "dma50")
    # hint fragments stay clean when OVERVIEW lacks a field (review P3: no "$n/a\u2013$n/a" on hover)
    _span = (f"${b(D['wkLow'], 0)}\u2013${b(D['wkHigh'], 0)}"
             if (D.get("wkLow") is not None and D.get("wkHigh") is not None) else "(range unavailable)")
    _h200 = f"(${b(D['dma200'], 0)})" if D.get("dma200") else "(unavailable)"
    _h50 = (f"vs the 50-day (${b(D['dma50'], 0)}): {_d50:+.1f}%"
            if (_d50 is not None and D.get("dma50")) else "50-day unavailable")
    cards += [
        {"label": "52w range", "value": (b(_rp, 0) + "%") if _rp is not None else "n/a",
         "hint": f"Where the price sits in its 52-week range {_span} "
                 "(0% = at the low, 100% = at the high; clamped on a fresh 52-week break). "
                 "OVERVIEW fields, as of their refresh."},
        {"label": "vs 200d MA", "value": (f"{_d200:+.1f}%" if _d200 is not None else "n/a"),
         "hint": f"Price vs the 200-day moving average {_h200} \u2014 the long-trend gauge; {_h50}. "
                 "A description of where price sits, not a signal."},
    ]
    # v1.83.0: next-earnings date (EARNINGS_CALENDAR) \u2014 only when known (a card that never fakes a date)
    _ne = D.get("nextEarnings")
    if _ne:
        _ned = _earn_days(_ne)
        cards.append({"label": "Next earnings", "value": _ne[5:] + (f" \u00b7 {_ned}d" if _ned is not None else ""),
                      "hint": f"Next scheduled report {_ne}{f' (~{_ned} days out)' if _ned is not None else ''} "
                              "(Alpha Vantage EARNINGS_CALENDAR, a forward estimate \u2014 issuers can move it). "
                              "IV usually ramps into the print and crushes after."})
    # off-scale % (treasury micro-caps: revenue~0 -> margin +-tens of thousands %) would
    # blow up the whole right axis; the CHART drops those points (text keeps exact numbers)
    def _pcl(vs):
        return [None if v is None or abs(v) > 500 else v for v in (vs or [])]
    # v1.84 tabs: panels build into SECTION lists (Overview | Financials | Ownership | Estimates & News);
    # the engine's generic sections machinery does the client swap. SVG/text read D — untouched.
    p_over, p_fin, p_est = [], [], []
    panels = p_over                       # kline + TTM land on the Overview tab
    po = D.get("priceOHLC")
    if po and po.get("frames"):
        _exch = str(D.get("exchange") or "").upper().replace(" ", "")
        _tv = (f"https://www.tradingview.com/symbols/{_exch}-{D['symbol']}/" if _exch
               else f"https://www.tradingview.com/symbols/{D['symbol']}/")
        pdata = dict(po, on=["VOL"], barSpace=8, tvUrl=_tv)         # clean default: volume only, no overlays
        panels.append({"type": "kline", "full": True, "collapsible": True,
                       "title": f"{D['symbol']} stocks price chart (split-adjusted)",
                       "indicators": ["MA", "EMA", "MACD", "RSI", "BOLL", "VOL"], "data": pdata})
    tt = D.get("ttmTrend")
    if tt and tt.get("quarters"):
        panels.append({"type": "combo", "title": "TTM by quarter ($B): revenue + FCF · margin",
                       "full": True, "zoom": {"def": 19, "step": 6, "min": 7},
                       "info": "Rolling 4-quarter revenue and free cash flow with net margin - seasonality removed, so the slope of the line is the signal, not any single bar.",
                       "units": {"left": "$B"},
                       "data": {"labels": [_qlab(x) for x in tt["quarters"]], "xQuarter": True, "series": [
                           {"type": "bar", "name": "revenue TTM", "values": tt["revenueB"], "color": UP},
                           {"type": "bar", "name": "FCF TTM", "values": tt["fcfB"], "color": TEAL},
                           {"type": "line", "name": "net margin %", "values": _pcl(tt["netMarginPct"]), "axis": "y1"}]}})
    zoom6 = {"def": 12, "step": 3, "min": 4}                         # 12 FY default -> from 2014 (user 2026-07-04)
    # semantic pairs (review 2026-07-04): [earnings level | earnings vs expectations],
    # [cash generated | cash returned], [dilution | balance structure]
    p_fin += [
        {"type": "combo", "title": "Revenue & net income ($B) · margin", "zoom": zoom6,
         "units": {"left": "$B"},
         "info": "Annual revenue vs net income with net margin; the gap between the bars is the cost of running the business, and a rising margin means it is shrinking.",
         "data": {"labels": dp.get("years") or [], "series": [
             {"type": "bar", "name": "revenue", "values": dp.get("revenueB") or [], "color": UP},
             {"type": "bar", "name": "net income", "values": dp.get("netIncomeB") or [], "color": NI},
             {"type": "line", "name": "net margin %", "values": _pcl(dp.get("netMarginPct")), "axis": "y1"}]}},
        {"type": "bars",
         "info": "Reported EPS vs consensus each quarter (%). Green beats, red misses; a steady run of small beats means beatable guidance. Capped at +/-60%.",
         "title": "EPS surprise (last quarters)"
                  + (" · capped ±60%" if any(v is not None and abs(v) > 60
                                             for v in (eq.get("pct") or [])) else ""),
         "units": {"left": "%"},
         "zoom": {"def": 14, "step": 6, "min": 4},   # 14 quarters default (user 2026-07-04)
         "data": {"labels": [_qlab(x) for x in eq.get("labels") or []], "xQuarter": True,
                  "series": [{"name": "surprise %", "colorBySign": True,
                              # one -14,890% print autoscaled every neighbor to invisibility —
                              # same ±60 cap as the SVG path, declared in the title (review)
                              "values": [None if v is None else max(-60.0, min(60.0, v))
                                         for v in (eq.get("pct") or [])]}]}},
        {"type": "combo", "title": "Free cash flow ($B): OCF − capex", "zoom": zoom6,
         "units": {"left": "$B"},
         "info": "Operating cash flow minus capex = free cash flow: the cash left for buybacks, dividends and debt - harder to massage than net income.",
         "data": {"labels": dp.get("years") or [], "series": [
             {"type": "bar", "name": "operating CF", "values": dp.get("ocfB") or [], "color": UP},
             {"type": "bar", "name": "capex", "values": [None if v is None else -v for v in (dp.get("capexB") or [])], "color": AMBER},
             {"type": "line", "name": "FCF", "values": dp.get("fcfB") or []}]}},
        {"type": "combo", "title": "Cap return ($B): bbs + divs · shares (B)", "zoom": zoom6,
         "units": {"left": "$B", "right": "B"},
         "info": "Buybacks plus dividends vs the share count; buybacks only help if the share line actually falls, else they just mop up stock comp.",
         "data": {"labels": dp.get("years") or [], "y1pct": False, "series": [
             {"type": "bar", "name": "buybacks $B", "values": dp.get("buybackB") or [], "color": "#7fc8a9"},
             {"type": "bar", "name": "dividends $B", "values": dp.get("dividendsB") or [], "color": "#EFB366"},
             {"type": "line", "name": "shares (B)", "values": dp.get("sharesB") or [], "axis": "y1"}]}},
        {"type": "combo", "title": "Stock comp ($B) · % of revenue", "zoom": zoom6,
         "units": {"left": "$B", "right": "% rev"},
         "info": "Stock comp in $ and % of revenue - a real cost that dilutes owners even though it never leaves the bank account.",
         "data": {"labels": dp.get("years") or [], "series": [
             {"type": "bar", "name": "SBC $B", "values": dp.get("sbcB") or [], "color": AMBER},
             {"type": "line", "name": "% of revenue", "values": _pcl(dp.get("sbcPctRev")), "axis": "y1"}]}},
        {"type": "combo", "title": "Balance ($B): assets vs equity", "zoom": zoom6, "units": {"left": "$B"},
         "info": "Total assets vs equity by year; the gap is liabilities, and equity outgrowing assets means less reliance on debt over time.",
         "data": {"labels": dp.get("years") or [], "series": [
             {"type": "bar", "name": "assets", "values": dp.get("totalAssetsB") or [], "color": UP},
             {"type": "bar", "name": "equity", "values": dp.get("equityB") or [], "color": NI}]}},
    ]
    p_est += [
        # v1.106: both tiles FULL-width, Analyst on top, News under it (user 2026-07-15) —
        # the tab is theirs; a 2-col pairing squeezed the article list for no reason
        {"type": "ratingbar", "title": "Analyst view", "newRow": True, "full": True,
         "info": "Consensus price target vs price and the buy/hold/sell split; expand 'estimates' for per-period EPS/revenue forecasts and their drift.",
         "data": dict(D.get("ratings") or {}, target=D.get("analystTarget"), price=D.get("price"),
                      est=_est_rows(D.get("estimates")), estAsOf=D.get("estimatesCachedAsOf"))},
        {"type": "sentibar", "full": True,
         "title": (f"News sentiment · last {(D.get('sentiment') or {}).get('spanD')}d"
                   if (D.get("sentiment") or {}).get("spanD") else "News sentiment · 14d"),
         "info": "Per-article sentiment past the 0.75 relevance gate; grey line = the ungated "
                 "full-feed mean. Every gated article is listed, with its AV summary.",
         "data": dict(D.get("sentiment") or {}, articles=D.get("articles") or [], relmin=REL_MIN)},
    ]
    def _post(pl):
        # data-driven degradation (SPCX probe): a chart panel whose every series is None/empty
        # renders as a bare 0..1 axis — drop it; the pair grid re-flows honestly
        pl = [p for p in pl if p["type"] not in ("bars", "combo")
              or any(v is not None for sr in p["data"]["series"] for v in (sr.get("values") or []))]
        # micro-cap rescale: a $B panel whose every left-axis value is <$0.5B reads as 0.00x —
        # flip that PANEL (independently) to $M; y1 series (%, share counts) untouched
        for p in pl:
            if p["type"] in ("bars", "combo") and (p.get("units") or {}).get("left") == "$B":
                mx = max((abs(v) for sr in p["data"]["series"] if sr.get("axis") != "y1"
                          for v in (sr.get("values") or []) if v is not None), default=0)
                if 0 < mx < 0.5:
                    for sr in p["data"]["series"]:
                        if sr.get("axis") != "y1":
                            sr["values"] = [None if v is None else round(v * 1000, 2) for v in sr["values"]]
                            sr["name"] = sr["name"].replace("$B", "$M")
                    p["units"]["left"] = "$M"
                    p["title"] = p["title"].replace("($B)", "($M)")
            # share counts get the same courtesy INDEPENDENTLY (INOD: shares 0.03B on a "B" axis
            # read as an unreadable 0,03 next to $M bars — 30M reads right; user 2026-07-04)
            if p["type"] in ("bars", "combo") and (p.get("units") or {}).get("right") == "B":
                my = max((abs(v) for sr in p["data"]["series"] if sr.get("axis") == "y1"
                          for v in (sr.get("values") or []) if v is not None), default=0)
                if 0 < my < 0.5:
                    for sr in p["data"]["series"]:
                        if sr.get("axis") == "y1":
                            sr["values"] = [None if v is None else round(v * 1000, 2) for v in sr["values"]]
                            sr["name"] = sr["name"].replace("(B)", "(M)")
                    p["units"]["right"] = "M"
                    p["title"] = p["title"].replace("shares (B)", "shares (M)")
        return pl
    _, own_cards, p_own = _ownership_ui(D)   # ownership tiles live on THEIR tab (Overview curated)
    p_over, p_fin, p_own, p_est = _post(p_over), _post(p_fin), _post(p_own), _post(p_est)
    chg = f"{D['changePct']:+.2f}%" if D.get("changePct") is not None else ""
    _ab = D.get("about") or {}
    # v1.110: the description is a PAGE-LEVEL lead block (under the header, above the tabs, always
    # visible) — shared verbatim with the options dashboard. Its meta line carries FY end · as-of
    # (· statement currency), and the site trails it. The header drops the chips; Alpha Vantage / not
    # advice moved to the footer (dataSource). leftSmall = "Fundamentals" is the only header difference.
    _lead_meta = f"FY ends {D.get('fyEnd', '?')} · last report as of {D.get('latestQuarter', '?')}"
    if D.get("stmtCurrency") not in (None, "", "USD"):
        _lead_meta += f" · statements {D['stmtCurrency']}"
    lead = {"text": _ab.get("text") or "", "meta": _lead_meta, "site": _ab.get("site") or ""}
    def _pick(*labels):
        by = {c["label"]: c for c in cards}
        return [by[l] for l in labels if l in by]
    # v1.84 tabs: [Overview | Financials | Ownership? | Estimates & News]. About rides the Overview
    # SECTION (§6.6 — on a sectioned page the engine ignores page-level about); headline cards ride
    # Overview in full, the other tabs repeat their topic tiles (the options Session-tile pattern).
    # v1.87.1 (user: "перегруженность плитками"): Overview carries ONLY the synthesis set —
    # one tile per question (valuation/growth/profitability/balance/price context/catalyst/vol);
    # the detail tiles live on their topic tabs; the vol snapshot is the full-row term strip (v1.89).
    sections = [
        {"id": "overview", "name": "Overview",
         # lead with the LIVE equity-session status (client-ticked countdown); omitted for a non-US
         # listing (v1.109). The STOCK session — its options trade a different one (user 2026-07-15).
         "cards": ([_ssc] if (_ssc := _session_card(D)) else [])
                  + _pick("Fwd P/E", "Rev YoY", "Net margin", "Net cash", "52w range", "vs 200d MA",
                          "Next earnings")
                  # v1.148: the sector-context tile (company story vs sector story) closes the
                  # synthesis set — it is price CONTEXT, so it sits with 52w/200d, not on a tab
                  + ([_sec] if (_sec := _sector_card(D)) else []),
         "panels": ([_vp] if (_vp := _vol_panel(D)) else []) + p_over},   # About is the page-level lead now (v1.110), not a section tile
        {"id": "financials", "name": "Financials", "panels": p_fin,
         "cards": _pick("PEG", "Gross margin", "Net margin", "ROE", "Net cash", "FCF yield",
                        "F-Score", "Altman Z", "Beta", "Div yield")},
    ]
    if own_cards or p_own:
        sections.append({"id": "ownership", "name": "Ownership", "cards": own_cards, "panels": p_own})
    sections.append({"id": "est", "name": "Estimates & News", "panels": p_est,
                     "cards": _pick("Fwd P/E", "PEG", "Rev YoY", "Next earnings")})
    # v1.113: cap = LIVE price × shares when USD (frozen fallback) — the options-board rule; see _cap_live_b
    _cs = dashboard.cap_str(_cap_live_b(D))
    _cap_s = f"cap {_cs}" if _cs else ""
    return {"title": f"{D['symbol']} · {D.get('name', '')}",
            # v1.110: ticker + its NAME (both boards) with the board TYPE under it (smaller-but-bold);
            # the sibling-board link pins by the price on the right (all engine-side, user 2026-07-15).
            "head": {"leftBig": D["symbol"], "leftName": D.get("name", ""), "leftType": "Fundamentals",
                     "rightBig": f"${b(D.get('price'), 2)}",
                     "rightSmall": " · ".join(x for x in (chg, _cap_s) if x)},
            "lead": lead, "dataSource": "Alpha Vantage",
            "sections": sections, "active": "overview"}


def _opt_links(html_path, sym, bg=False, _spawn=None, options_fname=None):
    """v1.87 cross-links: a header chip to the OPTIONS sibling (<sym>_options.html) when it
    exists next to this output file. v1.94 (twin of options._fund_links): with `bg=True` (a
    LIVE render — never --from-dir) a MISSING sibling is rendered in the BACKGROUND (detached
    options.py --full) and the chip bakes `pending` — hidden until the page's HEAD-poll sees
    the file land; non-optionable names simply never land, so the chip never shows. A failed
    spawn bakes nothing; MARKET_DATA_NO_SIBLING (set in every spawned child) stops chains.
    Returns (links, sibling): v1.127 — in Cowork the inline child ALSO emits its SPEC
    (--emit-spec) and `sibling` = {label, backLabel, fname, spec} for EMBEDDING (the viewer
    cannot navigate between files: the QCOM/MA sessions — the chip becomes a client-side board
    swap instead of a dead link). A failed spec read falls back to the plain file link."""
    fname, sib = dashboard.sibling_target(
        html_path,
        options_fname,
        sym.lower() + "_options.html",
        sym.lower() + "_options_sibling.html",
    )
    label = f"{sym} options ↗"
    links = dashboard.sibling_links(html_path, [(fname, label)])
    if not bg or os.environ.get("MARKET_DATA_NO_SIBLING"):
        return links, None
    cw = dashboard.in_cowork()
    if links and not cw:
        return links, None   # desktop: the existing file chip is a REAL link — nothing to do
    here = os.path.dirname(os.path.abspath(__file__))
    # --equity: the parent gather PROVED sym is an AV equity/ETF, and the options route guard
    # would otherwise refuse every futures-root namesake (CL=Colgate, CB=Chubb, ES=Eversource)
    parent_fname = dashboard.sibling_basename(os.path.basename(html_path), sym.lower() + ".html")
    argv = [sys.executable, os.path.join(here, "options.py"), sym, "--full", "--equity", "--html", sib,
            "--fundamentals-link", parent_fname]
    if cw:
        # v1.123.2 — Cowork: the detached spawn + the page's HEAD-poll are both dead, so a pending chip
        # never reveals. Render the options sibling INLINE (synchronous); v1.127: capture its spec too
        # and EMBED it — the chip swaps boards in place (file navigation is impossible in the viewer).
        # Review P1 (v1.127): this runs even when the sibling FILE already exists — the old
        # existing-file shortcut returned a DEAD chip there and a re-render DESTROYED the pair.
        emit = os.path.join(os.path.dirname(sib), ".%s.%d.spec.json" % (fname[:-5], os.getpid()))
        ok_child = (_spawn or dashboard.render_sync)(argv + ["--emit-spec", emit])
        sspec = None
        if ok_child:
            try:
                with open(emit, encoding="utf-8") as f:
                    sspec = json.load(f)
            except (OSError, ValueError):
                sspec = None
        try:
            os.unlink(emit)   # its own try (review P3): a failed unlink must not discard a loaded spec
        except OSError:
            pass
        if sspec is not None:
            return [], {"label": f"{sym} options", "backLabel": f"{sym} fundamentals",
                        "fname": fname, "spec": sspec}
        if ok_child:
            return (dashboard.sibling_links(html_path, [(fname, label)]) or []), None
        return [], None
    spawn = _spawn or dashboard.spawn_bg
    if spawn(argv):
        print(f"note: rendering {sib} in the background — the '{label}' chip appears on the page when it lands",
              file=sys.stderr)
        return [{"text": label, "href": fname, "pending": True}], None
    return [], None


def render_html(D, links=None, sibling=None):
    """Ф7 F2: the interactive dashboard IS the engine's page now (Chart.js + range zoom +
    tooltips + adaptive grid, SSE off). `links` (v1.87) = header cross-link chips to sibling
    dashboards (dashboard.sibling_links); `sibling` (v1.127) = the EMBEDDED options board —
    the chip swaps boards client-side (Cowork, where file navigation is impossible)."""
    spec = to_spec(D)
    if links:
        spec["links"] = links
    if sibling:
        spec["sibling"] = sibling
    return dashboard.render_interactive_html(spec)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Fetch + analyze company fundamentals (Alpha Vantage)")
    ap.add_argument("symbol")
    ap.add_argument("--svg", help="write a self-contained SVG dashboard to this path (fixed landscape; for PNG / portable file / screenshot-verify)")
    ap.add_argument("--html", help="write a self-contained RESPONSIVE HTML dashboard (Chart.js) — reflows to its container, so it fits an inline widget, an expandable artifact/app view, or the Preview Pane without going tiny")
    ap.add_argument("--summary-out", metavar="PATH",
                    help="also write the default compact text summary to PATH from this SAME gather; "
                         "use with --html to avoid a second provider fetch for the written read")
    ap.add_argument("--png", action="store_true", help="also write a PNG (cross-platform: rsvg-convert/qlmanage/headless browser)")
    ap.add_argument("--json", action="store_true",
                    help="emit compact JSON (feed a widget) instead of text — the heavy series "
                         "(priceOHLC/deep/epsDeep, ~100KB+) are EXCLUDED; use --json-full for everything")
    ap.add_argument("--json-full", action="store_true", help="--json including the heavy series")
    ap.add_argument("--open", action="store_true", help="open the SVG dashboard in the default browser (cross-platform: macOS/Windows/Linux) — the fast way to SHOW a user a rich dashboard")
    ap.add_argument("--no-news", action="store_true", help="skip the news-sentiment fetch")
    ap.add_argument("--emit-spec", metavar="PATH", default=None,
                    help="also dump the dashboard SPEC as JSON (v1.127: the sibling-embed seam — "
                         "a parent render reads it to embed this board for the client-side swap)")
    ap.add_argument("--options-link", help=argparse.SUPPRESS)
    ap.add_argument("--save-raw", metavar="DIR",
                    help="save the raw AV payloads to DIR while fetching; re-render later with "
                         "--from-dir DIR (offline, instant, 0 API calls)")
    ap.add_argument("--from-dir", help="MCP-fallback (egress-blocked sandboxes): build from a "
                    "directory of saved AV results (files named *overview*/*quote*/*income*/*balance*/"
                    "*cash*/*earnings*/*news*.json from the MCP, return_full_data=true) instead of HTTP")
    ap.add_argument("--env-file")
    a = ap.parse_args(argv)
    sym = a.symbol.upper()
    if a.from_dir:
        D = build(sym, read_from_dir(sym, a.from_dir), offline=True)
    else:
        D = gather(sym, get_key(a.env_file), with_news=not a.no_news, save_dir=a.save_raw)
    if a.from_dir and a.save_raw:
        print("warning: --save-raw is ignored with --from-dir (nothing was fetched)", file=sys.stderr)
    if a.emit_spec:   # independent of the render flags (review P3: it silently no-op'd without --html)
        with open(a.emit_spec, "w", encoding="utf-8") as f:
            json.dump(to_spec(D), f)   # v1.127: the sibling-embed seam (see --emit-spec help)
    if a.summary_out:
        dashboard.write_atomic(a.summary_out, render_text(D) + "\n")
    if a.svg or a.png or a.open or a.html:
        wrote = []
        if a.html:
            # v1.87 cross-links: chip to the options sibling — existing, or background-rendered
            # with a pending chip (v1.94; never on --from-dir). v1.127 Cowork: the sibling is
            # EMBEDDED (the chip swaps boards in place) and its FILE gets the reverse embed too.
            # v1.159.1: make the real parent filename visible BEFORE launching a background child;
            # that child receives this exact basename, so descriptive OpenAI filenames still get
            # a deterministic options -> fundamentals return link.
            if not a.from_dir and not os.environ.get("MARKET_DATA_NO_SIBLING"):
                dashboard.write_atomic(a.html, render_html(D))
            _links, _sib = _opt_links(a.html, D["symbol"], bg=not a.from_dir,
                                      options_fname=a.options_link)
            # A COMPANY page earns one of the company documents; a PLAIN ETF earns none (SKILL.md),
            # and only a THEMATIC one earns :sector — the script cannot tell thematic from
            # broad-market, so the note carries that condition instead of guessing (MSOS 2026-07-14
            # was a thematic ETF that closed text-only, so silence here would re-open that hole).
            _sym = D["symbol"]
            _offers = (["/equity-research:sector <theme> — ONLY if this ETF is THEMATIC; a plain "
                        "broad-market ETF earns NO offer"] if D.get("kind") == "etf" else
                       ["/equity-research:earnings %s <Qn FYnn>" % _sym,
                        "/equity-research:earnings-preview " + _sym,
                        "/financial-analysis:dcf " + _sym, "/financial-analysis:comps " + _sym])
            dashboard.deliver_html(a.html, render_html(D, links=_links, sibling=_sib),
                                   offers=_offers)
            print(f"{a.html}  |  {D['symbol']} responsive html dashboard")
            wrote.append(a.html)
            if _sib:
                dashboard.write_reverse_sibling(_sib, to_spec(D), a.html)
        if a.svg or a.png or not wrote:  # SVG when asked, or as the default when nothing else written
            out = a.svg or (D["symbol"].lower() + ("_etf.svg" if D.get("kind") == "etf" else "_fundamentals.svg"))
            open(out, "w", encoding="utf-8").write(render_svg(D))
            if D.get("kind") == "etf":
                print(f"{out}  |  {D['symbol']} ETF profile  |  net assets ${b(D.get('netAssetsB'),1)}B · {D.get('nHoldings',0)} holdings")
            else:
                print(f"{out}  |  {D['symbol']} dashboard  |  rev FY{D['years'][0][2:]}-{D['years'][-1][2:]} ${b(D['revenueB'][0],0)}->{b(D['revenueB'][-1],0)}B")
            if a.png:
                sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
                import chart  # sibling: reuse the cross-platform SVG->PNG rasterizer
                png = (out[:-4] if out.endswith(".svg") else out) + ".png"
                print(f"{png}  |  via {chart.rasterize(out, png)}")
            wrote.append(out)
        if a.open:
            # open the most interactive output (HTML if written, else SVG);
            # pathlib.as_uri() → file:///C:/... valid on Windows (not "file://C:\...")
            webbrowser.open(pathlib.Path(os.path.abspath(a.html if a.html else wrote[0])).as_uri())
    elif a.json or a.json_full:
        slim = D if a.json_full else {k: v for k, v in D.items()
                                      if k not in ("priceOHLC", "deep", "epsDeep")}
        print(json.dumps(slim, separators=(",", ":")))
    else:
        print(render_text(D))


if __name__ == "__main__":
    main()
