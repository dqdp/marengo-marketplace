#!/usr/bin/env python3
"""
options.py — fetch an equity/ETF option chain and analyze it, end to end.
Pure stdlib (urllib), no pip installs.

The point (same as chart.py / fundamentals.py / portfolio.py): Alpha Vantage's
REALTIME_OPTIONS returns the WHOLE chain (every expiration — ~14k contracts, far over
the MCP token limit), so it's fetched DIRECTLY over HTTP, filtered to ONE expiration,
and reduced to a compact set of numbers INSIDE this script. Greeks/IV are AV-native
(read, never recomputed — greeks.py is only for Databento futures options).

v1 is EQUITY/ETF only (AV). Futures options (Databento + scripts/greeks.py / Black-76)
are a separate path — not here.

Computes: the IV smile (call & put IV vs strike), ATM IV, 25-delta skew (risk reversal),
put/call ratio (OI and volume), max pain, and the open-interest profile by strike —
over a ±N-strike window around ATM.

Outputs (pick with flags):
  (default)   text summary: ATM IV / skew / PCR / max pain + a per-strike table.
  --json      the same as compact JSON (feed a widget).
  --svg PATH  a self-contained landscape SVG dashboard (smile, OI-by-strike, strikes table).
  --html PATH a self-contained RESPONSIVE Chart.js dashboard (reflows 1 col -> 2 cols).

Usage:
  python3 options.py SPY                          # nearest usable expiration
  python3 options.py AAPL --expiry 2026-07-17     # a specific expiration
  python3 options.py QQQ --html chain.html
  python3 options.py SPY --window 25 --svg chain.svg --png

Key is read from .env (skill folder or cwd) as ALPHAVANTAGE_API_KEY, or the env.
Never pass the key on the command line or paste it into chat.
"""
from __future__ import annotations
import argparse, concurrent.futures, datetime as dt, gzip, http.client, json, math, os, pathlib, queue, re, sys, urllib.parse, urllib.request, webbrowser
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dashboard  # Ф3: options builds a dashboard SPEC; dashboard.py is the one renderer

TEAL, DK, LT, BLUE, AMBER, RED, GRA, INK = "#1D9E75", "#0F6E56", "#9FE1CB", "#378ADD", "#EFB366", "#E24B4A", "#8a8f98", "#1f2328"


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


# --- invocation guards (docs/command-layer-plan.md §A): refuse symbols this script would MIS-SERVE ---
# Cash indices aren't on AV — their options need an ETF proxy (confirm it with the user). VIX is special:
# VIX options price off VIX FUTURES, not spot, so no proxy is honest.
INDEX_PROXY = {"SPX": "SPY", "SPXW": "SPY", "XSP": "SPY", "NDX": "QQQ", "RUT": "IWM", "DJX": "DIA",
               "VIX": None}

# Futures roots whose options are NOT on GLBX (ICE softs / not listed) but which collide with
# a LIVE US listing (AV LISTING_STATUS sweep 2026-07-04). The GLBX-listed collision roots
# (ZM, BZ, HE, MGC, BTC…) moved into futures_options.OPTION_ROOTS after the 2026-07-04 GLBX
# universe audit — this set is the honest remainder: no futures route exists on this
# subscription, so the ONLY reachable meaning is the equity namesake (--equity).
NAMESAKE_ROOTS = {
    "KC": ("Coffee C future", "ICE", "Kingsoft Cloud Holdings"),
    "CC": ("Cocoa future", "ICE", "Chemours Company"),
    "SB": ("Sugar No.11 future", "ICE", "Safe Bulkers Inc"),
    "SIL": ("Micro Silver future (no listed options)", "COMEX", "Global X Silver Miners ETF"),
}


def _script_cmd(script):
    return f'python3 "{os.path.join(os.path.dirname(os.path.abspath(__file__)), script)}"'


def _route_guard(sym, equity_ok=False):
    """Classify-first enforcement. A KNOWN FUTURES ROOT here would return the EQUITY NAMESAKE's chain
    (CL -> Colgate-Palmolive, ES -> Eversource…) with exit 0 — a plausible, correctly-formatted, WRONG
    instrument. Refuse it unless --equity says the namesake is really wanted. Indices always refuse."""
    if sym in INDEX_PROXY:
        p = INDEX_PROXY[sym]
        if p is None:
            sys.exit(f"error: '{sym}' options price off VIX FUTURES, not spot — no clean ETF proxy "
                     f"exists and a spot smile would mislead; not rendering one.")
        sys.exit(f"error: '{sym}' is a cash INDEX — index options aren't on Alpha Vantage/this skill.\n"
                 f"  ETF-proxy chain -> {_script_cmd('options.py')} {p}   (confirm the proxy with the user first)")
    if equity_ok:
        return
    try:
        import futures_options as _fo   # sibling script; lazy so a standalone copy still runs
    except ImportError:
        return
    if sym in _fo.OPTION_ROOTS:
        sys.exit(f"error: '{sym}' is a known FUTURES root — this script analyzes EQUITY/ETF options.\n"
                 f"  options on the future -> {_script_cmd('futures_options.py')} {sym}\n"
                 f"  the equity namesake   -> re-run with --equity")
    if sym in NAMESAKE_ROOTS:
        fut, venue, eq = NAMESAKE_ROOTS[sym]
        sys.exit(f"error: '{sym}' is a FUTURES root ({fut}, {venue}) with an equity namesake ({eq}) — "
                 f"a bare symbol is ambiguous, and its options are NOT on this skill's GLBX (CME group) "
                 f"subscription, so no futures route exists here.\n"
                 f"  the equity namesake -> re-run with --equity")


class ChainError(RuntimeError):
    """Fatal data-layer error (entitlement sample / throttle / dead chain / bad expiry).
    fatal=True: dashboard.serve stops polling the metered key and keeps the error on the badge.
    RAISED, never sys.exit'ed: sys.exit is SystemExit (a BaseException), which slips past the
    --watch poll thread's `except Exception` and silently kills it — the live page then hangs
    at "connecting…" forever. main() converts ChainError -> sys.exit for one-shot runs."""

    fatal = True


def _sample_payload(opt):
    """AV's 'artificial sample' response (a key without realtime-options entitlement): a message about
    the premium plan + placeholder rows (symbol/contractID 'XXYYZZ…', expiration '2099-…'). Modeled on
    the REAL shape (references/alpha-vantage.md)."""
    o = opt or {}
    rows = o.get("data") or []
    if any("XXYYZZ" in (str(r.get("symbol", "")) + str(r.get("contractID", "")))
           or str(r.get("expiration", "")).startswith("2099") for r in rows):
        return True
    msg = str(o.get("message", "")).lower()
    return ("premium" in msg or "sample" in msg) and not rows


def _throttle_note(d):
    """A rate-limit note anywhere in the payload (AV free tier = 25 requests/day, 5/minute)."""
    for k in ("Note", "Information"):
        v = str((d or {}).get(k, "")).lower()
        if v and any(t in v for t in ("rate limit", "requests per", "call frequency", "per minute", "per day")):
            return True
    return False


def _throttle_message():
    return ("error: Alpha Vantage throttled (free tier = 25 req/day · 5/min; realtime options need "
            "premium) — wait a minute and retry")


def _check_chain_payload(symbol, opt):
    """Entitlement != throttle != bad ticker — three different fixes, three distinct errors."""
    if _sample_payload(opt):
        raise ChainError(f"error: Alpha Vantage returned its SAMPLE payload for {symbol} — this key lacks "
                         f"realtime-options entitlement (premium plan); use HISTORICAL_OPTIONS or upgrade the key")
    if _throttle_note(opt):
        raise ChainError(_throttle_message())


def _fetch_contracts(symbol, key, fetch=None):
    """The chain listing ONLY (one AV call, no quote) — powers --list-expiries."""
    if fetch is None:
        dashboard.check_no_net("Route: pull REALTIME_OPTIONS (datatype=json) via the MCP connector, "
                               "save it, render with --from-dir DIR.")
    fetch = fetch or (lambda p: av(p, key))
    opt = fetch({"function": "REALTIME_OPTIONS", "symbol": symbol, "require_greeks": "true"})
    _check_chain_payload(symbol, opt)
    return opt.get("data") or []


def list_expiries_lines(contracts, today=None):
    """One line per listed expiration ('YYYY-MM-DD · N DTE · M strikes') — the validate-before-invoke
    pre-check an agent runs before committing to an --expiry."""
    today = today or dt.date.today()
    by = {}
    for r in contracts or []:
        e = str(r.get("expiration") or "")
        try:
            d = dt.date.fromisoformat(e)
        except ValueError:
            continue
        by.setdefault((e, (d - today).days), set()).add(str(r.get("strike")))
    return [f"{e} · {days} DTE · {len(ks)} strikes" for (e, days), ks in sorted(by.items())]


# --- parse-time validators (§0.5): bad values die at argparse (exit 2), never mid-run ---
def _arg_date(s):
    try:
        dt.date.fromisoformat(s)
        return s
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{s}' is not YYYY-MM-DD") from None


def _arg_watch(s):
    # 1s floor is safe here: one poll = TWO AV requests (chain + spot) -> 120 req/min at 1s,
    # a fifth of the 600/min premium budget, and dashboard.serve enforces >=1s spacing AFTER
    # each poll completes, so fast intervals cannot busy-spin or overlap.
    v = int(s)
    if not 1 <= v <= 3600:
        raise argparse.ArgumentTypeError("--watch seconds must be within [1, 3600]")
    return v


def _arg_posint(s):
    v = int(s)
    if v <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return v


def _arg_port(s):
    v = int(s)
    if not 1024 <= v <= 65535:
        raise argparse.ArgumentTypeError("port must be within [1024, 65535]")
    return v


def av(params, key):
    url = "https://www.alphavantage.co/query?" + urllib.parse.urlencode(dict(params, apikey=key))
    # gzip (v1.85, probed 2026-07-13): a HISTORICAL_OPTIONS settlement chain is ~1.2MB raw but
    # ~45KB gzipped — the opthist backfill rides THIS function, so the universe-scale warm
    # (~28k pulls) is ~1.3GB on the wire instead of ~34GB. AVSession (the --watch pool) already
    # spoke gzip; this brings the one-shot path to parity.
    req = urllib.request.Request(url, headers={"Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=60) as r:
        body = r.read()
        try:
            enc = (r.headers.get("Content-Encoding") or "").lower()
        except AttributeError:
            enc = ""   # test stubs without headers -> plain
        if enc == "gzip":
            import gzip as _gz
            body = _gz.decompress(body)
        d = json.loads(body)
    if isinstance(d, dict) and ("Note" in d or "Information" in d) and len(d) == 1:
        if _throttle_note(d):
            sys.exit(_throttle_message())
        sys.exit("error: Alpha Vantage returned an informational response: " + str(d)[:160])
    return d


def ff(v):  # safe float
    try:
        return float(v) if v not in (None, "", "None") else None
    except (TypeError, ValueError):
        return None


class AVSession:
    """Keep-alive connection pool to Alpha Vantage, for the --watch live loop. Reuses the
    TLS connection across polls (no handshake each cycle) and requests gzip — the option
    chain is ~1 MB of text, so it compresses ~10x and that dominates the per-poll cost.
    Pool size = gather()'s concurrency (2). Unlike av(), it RAISES on throttle/transient
    errors (never sys.exit) so the watch loop can skip a cycle and retry, not die.

    This is the polling backend behind the snapshot seam (`gather(..., fetch=session.get)`).
    A Databento-Live socket backend could implement the same `get`-like provider later
    (hold the stream + return the latest chain), reusing build()/render()/the watch loop."""
    HOST = "www.alphavantage.co"

    def __init__(self, key, size=2):
        self.key = key
        self._pool = queue.Queue()
        for _ in range(size):
            self._pool.put(None)  # connections opened lazily, then kept warm

    def get(self, params):
        conn = self._pool.get()
        try:
            for attempt in range(2):  # one retry: idle keep-alive sockets get closed server-side
                try:
                    if conn is None:
                        conn = http.client.HTTPSConnection(self.HOST, timeout=60)
                    conn.request("GET", "/query?" + urllib.parse.urlencode(dict(params, apikey=self.key)),
                                 headers={"User-Agent": "market-data", "Accept-Encoding": "gzip",
                                          "Connection": "keep-alive"})
                    r = conn.getresponse()
                    body = r.read()  # must fully read to reuse the connection
                except (http.client.HTTPException, OSError):
                    if conn:
                        try:
                            conn.close()
                        except OSError:
                            pass
                    conn = None
                    if attempt == 1:
                        raise
                    continue
                if r.status != 200:
                    raise OSError(f"Alpha Vantage HTTP {r.status}")
                if (r.getheader("Content-Encoding") or "").lower() == "gzip":
                    body = gzip.decompress(body)
                d = json.loads(body)
                if isinstance(d, dict) and ("Note" in d or "Information" in d) and len(d) == 1:
                    raise RuntimeError(_throttle_message() if _throttle_note(d)
                                       else "Alpha Vantage informational response: " + str(d)[:120])
                return d
        finally:
            self._pool.put(conn)  # warm connection back to the pool (or None to re-open next time)

    def close(self):
        while True:
            try:
                c = self._pool.get_nowait()
            except queue.Empty:
                return
            if c:
                try:
                    c.close()
                except OSError:
                    pass


# ----------------- fetch -----------------
def gather(symbol, key, expiry, window, fetch=None):
    # `fetch(params)->dict` is the snapshot provider seam: the default opens a fresh
    # connection per call (av); --watch passes a keep-alive AVSession.get.
    # Gate the DEFAULT (real-network) path — this is the entrypoint an agent hits, and an
    # egress-blocked sandbox must fast-fail to --from-dir here, not only on --list-expiries
    # (MARKET_DATA_NO_NET=auto probes first, so an OPEN-egress sandbox falls straight through).
    if fetch is None:
        dashboard.check_no_net("Route: pull REALTIME_OPTIONS (datatype=json, return_full_data=true) via "
                               "the MCP connector, save it to a file whose name contains 'option', then "
                               "render with --from-dir DIR.")
    fetch = fetch or (lambda p: av(p, key))
    opt_params = {"function": "REALTIME_OPTIONS", "symbol": symbol, "require_greeks": "true"}
    if expiry:   # server-side series filter (verified live 2026-07-04: ~19x smaller payload) —
        opt_params["expiration"] = expiry   # a watch re-poll fetches ONLY the displayed series
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        opt_fut = ex.submit(fetch, opt_params)
        q_fut = ex.submit(fetch, {"function": "GLOBAL_QUOTE", "symbol": symbol, "entitlement": "realtime"})
        opt = opt_fut.result()
        quote = q_fut.result()
    _check_chain_payload(symbol, opt)   # entitlement/throttle die loud — never masquerade as "bad ticker"
    if expiry and not (opt.get("data") or []):
        # verified live 2026-07-04: AV answers an UNLISTED expiration= with data:[] + "success" —
        # without this branch the empty chain would be misdiagnosed as a bad ticker.
        e = ChainError(f"error: no {symbol} contracts expiring {expiry} (unlisted expiration?) — "
                       f"run --list-expiries to see the listed dates")
        # v1.125: AV ALSO 200-OKs an empty chain under load-shed for a LIVE name (the PEG lesson) —
        # on the --watch re-poll of an already-validated pin that is transient: non-fatal, so
        # dashboard.serve skips the cycle and keeps polling (one-shot CLI still exits via main;
        # entitlement/throttle above keep the fatal class default).
        e.fatal = False
        raise e
    return build(symbol, opt.get("data") or [], quote, expiry, window)


def _read_dir_chain(d):
    """The saved-payload loader behind --from-dir: (chain rows, quote) from a directory.
    Chain = the file whose name contains 'option'; quote = 'quote' (optional)."""
    import glob
    files = [fp for fp in glob.glob(os.path.join(d, "*")) if os.path.isfile(fp)]

    def load(fp):
        try:
            return json.load(open(fp, encoding="utf-8"))
        except (ValueError, OSError):
            return None

    chain = None
    for fp in sorted(files):
        if "option" in os.path.basename(fp).lower():
            obj = load(fp)
            if isinstance(obj, dict) and isinstance(obj.get("data"), list):
                chain = obj["data"]
                break
            if isinstance(obj, list):
                chain = obj
                break
    if chain is None:
        sys.exit(f"--from-dir {d}: no readable REALTIME_OPTIONS file (a filename containing 'option' "
                 f"with a 'data' list of contracts)")
    quote = None
    for fp in sorted(files):
        if "quote" in os.path.basename(fp).lower():
            quote = load(fp)
            break
    return chain, quote


def read_from_dir(symbol, d, expiry, window):
    """MCP-fallback for egress-blocked sandboxes (the chart.py --from / fundamentals
    --from-dir analogue). Save the REALTIME_OPTIONS result (return_full_data=true) to a
    file whose name contains 'option', and optionally a GLOBAL_QUOTE result to a file
    whose name contains 'quote'. The chain is found by a substring on the filename."""
    chain, quote = _read_dir_chain(d)
    return build(symbol, chain, quote, expiry, window)


TERM_BUCKETS = [0.80, 0.85, 0.90, 0.95, 1.00, 1.05, 1.10, 1.15, 1.20]   # moneyness cols (±2.5% tol)

# Alpha Vantage emits implied_volatility == 0.01488 (bit-identical) as a "couldn't compute IV" sentinel for
# illiquid / near-expiry settlement contracts. It slipped through the v>0 gate and printed as a fake ~1.5% ATM
# IV (poisoned the vol-cone MIN + the term-through-time heatmap). Drop EXACTLY the sentinel (and non-positive)
# to None — a SURGICAL match, so a genuinely low but real sub-2% IV (e.g. a T-bill ETF in a calm regime) is
# NOT erased (agent-review 2026-07-08: on live chains only the bit-identical sentinel was ever seen < 2%).
AV_IV_SENTINEL = 0.01488


def plausible_iv(v):
    """AV-native IV as a fraction, or None when it's non-positive or AV's 0.01488 sentinel (a hole, not a real IV).
    The ONLY place the sentinel is recognized — shared by every IV read (options + opthist) so the guard can't drift."""
    if v is None or v <= 0:
        return None
    return None if abs(v - AV_IV_SENTINEL) <= 5e-6 else v


# v1.130: per-strike turnover needs a minimal standing book to be a ratio, not noise — below this
# many contracts (both legs combined) the panel leaves the strike empty (the OI/volume reads keep it).
_TURN_OI_FLOOR = 10

# ---------------- v1.130 ΔOI (1d): yesterday's per-contract OI baseline ----------------
# ONE metered HISTORICAL_OPTIONS pull on a normal day (a first run across a holiday, or before
# yesterday's chain is published, probes up to 4 dates — cheap empties, each cacheable date then
# served from disk forever): a PAST date's chain is IMMUTABLE, so both the reduced OI map and
# "that date was a holiday" (empty) are disk-cached FOREVER — but an empty for a date YOUNGER
# than 2 calendar days is AMBIGUOUS (holiday vs not-yet-published, the GLD publication-latency
# class, v1.92.1) and is never written to disk. An error/throttle caches NOTHING (failure ≠
# no-data — the v1.120 rule) and memoizes only in-process so a --watch session never re-burns
# the call every poll.
_DROI_ENV = "MARKET_DATA_DROI"
_DROI_MEMO = {}   # (SYM, asof, wkey) -> baseline dict | None; process-lifetime (incl. memoized failures)


def _droi_dir():
    """Resolve the ΔOI baseline cache dir. `MARKET_DATA_DROI` = a dir override, or 0/off/no/false
    (case-insensitive — the v1.126 lesson) to disable; default ~/.cache/market-data/droi.
    None = disabled (no pull, no cache, honest n/a)."""
    v = os.environ.get(_DROI_ENV, "").strip()
    if v.lower() in ("0", "off", "no", "false"):
        return None
    if v.lower() in ("1", "on", "yes", "true"):   # truthy = DEFAULT-ON, never a "./1" junk dir
        v = ""
    if v:
        return os.path.abspath(os.path.expanduser(v))
    return os.path.join(os.path.expanduser("~"), ".cache", "market-data", "droi")


def _prev_bdays(asof, n=4):
    """The n previous BUSINESS days before `asof` (ISO), newest first — Sat/Sun skipped; a market
    holiday costs one probe that is then cached-empty forever, and the walk goes one day further."""
    d = dt.date.fromisoformat(asof)
    out = []
    while len(out) < n:
        d -= dt.timedelta(days=1)
        if d.weekday() < 5:
            out.append(d.isoformat())
    return out


def _droi_reduce(rows_):
    """HISTORICAL_OPTIONS rows -> {expiration: {'call': {strike: oi}, 'put': {...}}} (floats via ff;
    malformed rows dropped)."""
    by = {}
    for c in rows_ or []:
        if not isinstance(c, dict):
            continue
        e, t, k, oi = c.get("expiration"), c.get("type"), ff(c.get("strike")), ff(c.get("open_interest"))
        if not e or t not in ("call", "put") or k is None or oi is None:
            continue
        by.setdefault(e, {"call": {}, "put": {}})[t][k] = oi
    return by


def _droi_cache_load(ddir, sym, date):
    """The cached baseline for (sym, date): {'empty': True} | {'byExp': …} | None (miss). JSON turns
    float strike keys into strings — restored here. A corrupt file is unlinked (miss -> refetch)."""
    path = os.path.join(ddir, "%s.%s.json.gz" % (sym, date))
    import gzip
    import zlib
    try:
        with open(path, "rb") as f:
            raw = json.loads(gzip.decompress(f.read()).decode("utf-8"))
        if raw.get("empty"):
            return {"empty": True}
        return {"byExp": {e: {s: {float(k): v for k, v in m.items()} for s, m in sides.items()}
                          for e, sides in (raw.get("byExp") or {}).items()}}
    except FileNotFoundError:
        return None
    except (OSError, ValueError, EOFError, zlib.error, AttributeError):
        try:
            os.unlink(path)
        except OSError:
            pass
        return None


def _droi_cache_save(ddir, sym, date, obj):
    """Best-effort atomic write (mkstemp + replace) + a sweep of baselines older than ~200 days (past
    the longest window, 1Q ≈ 3 months, so a fresh 1m/1Q file is never swept) + a >1h .tmp-orphan sweep —
    the cache is valid forever, the date sweep is only disk hygiene. Any failure is swallowed (the
    baseline still serves this render from memory)."""
    import gzip
    import tempfile
    try:
        import time
        os.makedirs(ddir, exist_ok=True)
        # v1.132: keep baselines reachable by the LONGEST window (1Q ≈ 3 calendar months) — the sweep
        # must not delete the 1m/1Q file it just wrote (immutable + valid forever; this is pure hygiene).
        cutoff = (dt.date.today() - dt.timedelta(days=200)).isoformat()
        now = time.time()
        for fn in os.listdir(ddir):
            parts = fn.split(".")
            _fp = os.path.join(ddir, fn)
            try:
                if fn.endswith(".json.gz") and len(parts) >= 3 and parts[-3] < cutoff:
                    os.unlink(_fp)
                elif fn.endswith(".tmp") and now - os.path.getmtime(_fp) > 3600:
                    os.unlink(_fp)   # review P3: a SIGKILL between mkstemp and replace leaks a .tmp
            except OSError:
                pass
        fd, tmp = tempfile.mkstemp(dir=ddir, suffix=".tmp")
        with os.fdopen(fd, "wb") as f:
            f.write(gzip.compress(json.dumps(obj).encode("utf-8")))
        os.replace(tmp, os.path.join(ddir, "%s.%s.json.gz" % (sym, date)))
    except OSError:
        pass


# v1.132 (user decision 2026-07-23): the ΔOI baseline is now WINDOWED — the OI·Δ panel carries a
# per-panel toggle (the engine's `variants` vtoggle) over these horizons. (key, label, business-days-
# back | calendar-months-back). 1d/1w anchor on trading days; 1m/1Q on the calendar (a month/quarter
# ago), then snap to the nearest prior trading day. Each window is one immutable HISTORICAL_OPTIONS
# pull, disk-cached forever and shared across windows that resolve to the same date (≤4 distinct
# pulls per symbol-day, then free).
DOI_WINDOWS = [("1d", "1d", 1, 0), ("1w", "1w", 5, 0), ("1m", "1m", 0, 1), ("1Q", "1Q", 0, 3)]


def _month_minus(d, months):
    """`months` calendar months before date `d`, clamping the day to the target month's length."""
    import calendar
    y, m = d.year, d.month - months
    while m <= 0:
        m += 12
        y -= 1
    return dt.date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def _window_candidates(asof, wkey, n=4):
    """The candidate baseline dates for a window, newest first (≤n business days), walking back over
    weekends/holidays from the window's anchor: 1d = the prior business day; 1w = the 5th business day
    back; 1m/1Q = a calendar month/quarter ago snapped to the nearest prior business day."""
    _cfg = {w: (bd, mo) for w, _l, bd, mo in DOI_WINDOWS}.get(wkey, (1, 0))
    bd, mo = _cfg
    if mo:
        out, d = [], _month_minus(dt.date.fromisoformat(asof), mo)
        while len(out) < n:
            if d.weekday() < 5:
                out.append(d.isoformat())
            d -= dt.timedelta(days=1)
        return out
    skip = bd - 1   # 1d -> skip 0 (1st business day back); 1w -> skip 4 (the 5th onward)
    allb = _prev_bdays(asof, skip + n)
    return allb[skip:skip + n]


def _probe_baseline(sym, dates, ddir, fetch, tdy):
    """Walk candidate business days (newest first) for the first one with a real settlement chain,
    reduced to per-contract OI. Returns ({'date','byExp'} | None, failed_bool): failed=True on an AV
    error/throttle (caller degrades + doesn't re-burn); a young (<2d) empty is never disk-cached (the
    GLD publication-latency class); an old empty is a holiday, cached forever."""
    for d in dates:
        got = _droi_cache_load(ddir, sym, d)
        if got is not None:
            if got.get("empty"):
                continue
            return {"date": d, "byExp": got["byExp"]}, False
        try:
            payload = fetch({"function": "HISTORICAL_OPTIONS", "symbol": sym, "date": d, "datatype": "json"})
        except (Exception, SystemExit):   # av() sys.exits on a throttle — best-effort here, never fatal
            payload = None
        if not isinstance(payload, dict) or "data" not in payload:
            return None, True
        rows_ = payload.get("data") or []
        if not rows_:
            if dt.date.fromisoformat(d) <= tdy - dt.timedelta(days=2):
                _droi_cache_save(ddir, sym, d, {"empty": True})
            continue
        by = _droi_reduce(rows_)
        _droi_cache_save(ddir, sym, d, {"byExp": by})
        return {"date": d, "byExp": by}, False
    return None, False   # no usable date in this window (all young-empty / holidays)


def fetch_oi_baselines(symbol, asof, key, fetch=None, _today=None, windows=None):
    """v1.132: the multi-window ΔOI baselines — {wkey: {'date','byExp'} | None} per configured window
    (1d/1w/1m/1Q). Per-date chains are immutable, so pulls are disk-cached forever and SHARED across
    windows resolving to the same date. An AV error on any probe degrades the REMAINING windows to
    absent (failure ≠ no-data — the v1.120 rule) and stops probing this call (a throttle hits all).
    MARKET_DATA_DROI=0 disables (returns {}). `fetch` / `_today` are test seams; `windows` restricts
    the set (default all)."""
    sym = symbol.upper()
    ddir = _droi_dir()
    if ddir is None:
        return {}
    try:
        asof = (asof or dt.date.today().isoformat())[:10]
        dt.date.fromisoformat(asof)
    except (ValueError, TypeError):
        asof = dt.date.today().isoformat()
    fetch = fetch or (lambda p: av(p, key))
    tdy = _today or dt.date.today()
    keys = [w for w, _l, _b, _m in DOI_WINDOWS if (windows is None or w in windows)]
    out = {}
    todo = []
    for wkey in keys:
        mk = (sym, asof, wkey)
        if mk in _DROI_MEMO:
            out[wkey] = _DROI_MEMO[mk]   # served free (incl. a memoized failure — never re-burns)
        else:
            todo.append(wkey)
    # review P3 (v1.132): the windows are INDEPENDENT (distinct anchor dates, per-date disk cache is
    # concurrency-safe), so fetch them CONCURRENTLY — this cuts a cold symbol-day's first-paint from ~4
    # sequential HISTORICAL_OPTIONS pulls to one round-trip, AND a throttle now degrades ONLY the windows
    # it actually hits (a sibling whose date is already on disk still resolves) instead of `break`-ing the
    # rest. Each window memoizes its own result/failure. Best-effort throughout.
    if todo:
        def _one(wkey):
            res, _failed = _probe_baseline(sym, _window_candidates(asof, wkey), ddir, fetch, tdy)
            return wkey, res
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(todo))) as ex:
            for wkey, res in ex.map(_one, todo):
                _DROI_MEMO[(sym, asof, wkey)] = res
                out[wkey] = res
    return {w: out[w] for w in keys}   # preserve DOI_WINDOWS order


def fetch_prev_oi(symbol, asof, key, fetch=None, _today=None):
    """The 1-business-day ΔOI baseline (v1.130) — a thin back-compat wrapper over the windowed fetch:
    returns just the '1d' baseline dict or None."""
    return fetch_oi_baselines(symbol, asof, key, fetch=fetch, _today=_today, windows=("1d",)).get("1d")


def _droi_net(v):
    vv = [x for x in v if x is not None]
    return sum(vv) if vv else None


def _attach_droi(D, baselines):
    """Mutate D with per-window ΔOI ladders. `baselines` = {wkey: {'date','byExp'} | None} (or a legacy
    single {'date','byExp'} — wrapped as the '1d' window). Sets D['doi'] = {wkey: {date, dOICall,
    dOIPut, callNet, putNet}} for each window with a usable baseline, D['doiWindows'] (ordered) and
    D['doiDefault']. No usable baseline -> D untouched (to_spec renders the plain OI panel). A CONTRACT
    absent in the baseline inside a LISTED series counts from 0 (a new listing's first OI is a real
    build); the whole series absent -> all None (nothing to difference); today's OI None -> None."""
    if not baselines:
        return D
    if "byExp" in baselines or "date" in baselines:   # legacy single baseline -> the 1d window
        baselines = {"1d": baselines}
    win = D["strikes"]
    doi = {}
    for wkey, _lab, _b, _m in DOI_WINDOWS:
        prev = baselines.get(wkey)
        if not prev or not prev.get("byExp"):
            continue
        # review P2 (v1.132): a WHOLE expiration absent from a past immutable settlement baseline
        # genuinely did not exist then (a weekly listed after that date — the NORM under 1m/1Q), so
        # it counts from 0 exactly like an absent STRIKE (all-green = "all built since the baseline").
        # This keeps the panel consistent with the honest text twin and the absent-strike rule.
        pe = (prev["byExp"] or {}).get(D["expiration"]) or {}
        _pc, _pp = pe.get("call") or {}, pe.get("put") or {}
        dc = [None if c is None else (c - (_pc.get(k) or 0.0)) for k, c in zip(win, D["callOI"])]
        dp = [None if c is None else (c - (_pp.get(k) or 0.0)) for k, c in zip(win, D["putOI"])]
        doi[wkey] = {"date": prev["date"], "dOICall": dc, "dOIPut": dp,
                     "callNet": _droi_net(dc), "putNet": _droi_net(dp)}
    if doi:
        D["doi"] = doi
        D["doiWindows"] = [w for w, _l, _b, _m in DOI_WINDOWS if w in doi]
        D["doiDefault"] = D["doiWindows"][0]
    return D


def _asof_of(contracts):
    """The chain's own as-of date (first row carrying one) — the fetch_prev_oi anchor for the paths
    that fetch the baseline BEFORE build() resolves D['asOf']."""
    for c in contracts or []:
        if isinstance(c, dict) and c.get("date"):
            return c["date"]
    return dt.date.today().isoformat()


def _term_fetch(symbol, key):
    """The term-mode fetch: the FULL chain (no expiration pushdown — term needs every series) + spot."""
    dashboard.check_no_net("Route: pull REALTIME_OPTIONS (datatype=json, NO expiration filter) via the "
                           "MCP connector, save it, render with --term --from-dir DIR.")
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        opt_fut = ex.submit(av, {"function": "REALTIME_OPTIONS", "symbol": symbol, "require_greeks": "true"}, key)
        q_fut = ex.submit(av, {"function": "GLOBAL_QUOTE", "symbol": symbol, "entitlement": "realtime"}, key)
        opt, quote = opt_fut.result(), q_fut.result()
    _check_chain_payload(symbol, opt)
    return (opt.get("data") or []), quote


def _full_fetch(sym, key, from_dir, hist, _term=None, _earn=None, _clos=None, _dir=None):
    """v1.123.2 — the --full page's INDEPENDENT pulls (chain + earnings + daily-closes) fetched
    CONCURRENTLY (the fundamentals.py 9-in-parallel pattern) so the wall is the slowest single call, not
    their sum — the Cowork "slow loading" was this SERIAL ~8s fetch chain, not the render (the page paints
    in ~56ms). Returns (contracts, tquote, earnings, closes). earnings/closes ride the same warm-store +
    key gate as before (want_e = hist and key; want_c also excludes from_dir). The --from-dir OFFLINE
    guarantee rides on the caller passing key=None there (options.py:main) -> want_e/want_c both False, so
    a lone dir read and no network. _term/_earn/_clos/_dir are test seams."""
    import opthist
    term = _term or _term_fetch
    earn = _earn or opthist.fetch_earnings
    clos = _clos or opthist.fetch_daily_closes
    rdir = _dir or _read_dir_chain
    want_e, want_c = bool(hist and key), bool(hist and key and not from_dir)
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        f_chain = ex.submit(rdir, from_dir) if from_dir else ex.submit(term, sym, key)
        f_earn = ex.submit(earn, sym, key) if want_e else None
        f_clos = ex.submit(clos, sym, key) if want_c else None
        contracts, tquote = f_chain.result()
        earnings = f_earn.result() if f_earn else None
        closes = f_clos.result() if f_clos else None
    return contracts, tquote, earnings, closes


def atm_iv_skew(strikes, calls, puts, spot, iv_of):
    """Per-expiration ATM IV + 25Δ skew from ONE expiration's strike→contract maps — the reduction
    shared by the live term structure (build_term) and the historical as-of reduction
    (opthist.reduce_asof), which had drifted into two copies. ATM IV = OTM-leg native IV at the
    strike nearest spot (put at/below spot, else call), with the other real quote as fallback. 25Δ
    skew = IV(put δ≈−0.25) − IV(call δ≈+0.25) in vol points, or None when nothing sits within 0.12
    of ±0.25 (a hole, not a far-wing fake). `iv_of` is injected so each caller keeps its own IV-field
    semantics. Returns (atm_iv_fraction | None, skew_volpts | None); ATM IV is UNROUNDED — the caller
    rounds (history 5dp, term 4dp) so outputs stay byte-identical to the pre-extraction code."""
    if not strikes:
        return None, None
    atm_k = min(strikes, key=lambda k: abs(k - spot))
    atm_iv = iv_of(puts.get(atm_k)) if atm_k <= spot else iv_of(calls.get(atm_k))
    if atm_iv is None:   # the OTM leg carried nothing at the money -> try the other REAL quote
        atm_iv = iv_of(calls.get(atm_k) if atm_k <= spot else puts.get(atm_k))

    def _near(side, want):
        best, gap = None, 1e9
        for c in side.values():
            d = ff(c.get("delta"))
            if d is None:
                continue
            g = abs(d - want)
            if g < gap:
                best, gap = c, g
        return best if gap <= 0.12 else None   # nothing near ±25Δ -> no skew, not a far-wing fake
    piv, civ = iv_of(_near(puts, -0.25)), iv_of(_near(calls, 0.25))
    skew = round((piv - civ) * 100, 2) if (piv is not None and civ is not None) else None   # vol points
    return atm_iv, skew


def _oi_heat_rowstyle(series):
    """Per-row heat for the Series table: shade EACH row by its TOTAL open interest (call+put) so every
    large expiration reads at a glance — not just the single ★ most-liquid one. LOG-scaled (OI is very
    skewed) using the seq 'hot' hue (red, matching the OI surface), alpha ∝ magnitude with a visible
    floor so mid rows still register; a 0-OI row stays unstyled. The ◆/★ markers ride the Expiry glyph
    and the table legend, not the fill (so the semantic key survives the heat)."""
    import math
    ois = [max(0, (s.get("oiTotal") or 0)) for s in series]
    pos = [o for o in ois if o > 0]
    if not pos:
        return ["" for _ in series]
    lo, hi = math.log(min(pos)), math.log(max(pos))
    out = []
    for o in ois:
        if o <= 0:
            out.append("")           # a genuinely empty expiration reads as blank, not faint-red
            continue
        t = 0.0 if hi <= lo else (math.log(o) - lo) / (hi - lo)   # 0..1, log-normalized across the term
        out.append("background:rgba(226,75,74,%s)" % round(0.06 + 0.40 * t, 3))
    return out


def build_term(symbol, contracts, quote, max_series=10, max_days=400, include=None):
    """Cross-EXPIRATION analytics from the ONE REALTIME_OPTIONS payload (AV returns every listed
    expiration in a single response — --term just stops throwing the other series away; zero extra
    calls). AV-native IVs/deltas, read as-is, never recomputed. Per expiration (nearest max_series
    within max_days): ATM IV = the OTM-leg native IV at the strike nearest spot (put at/below spot,
    else call — the futures composite rule); 25Δ skew = IV(put δ nearest −0.25) − IV(call δ nearest
    +0.25) from NATIVE deltas; plus the moneyness×expiry IV surface — per 5% bucket the nearest
    listed strike within ±half-bucket of bucket×spot, missing/zero IV stays None (a hole is a hole:
    never interpolated across strikes OR series)."""
    if not contracts:
        raise ChainError(f"error: no option contracts for {symbol} (bad ticker / non-equity / no entitlement?)")
    spot = _spot_from_quote(quote)
    if spot is None or spot <= 0:
        raise ChainError(f"error: no usable spot for {symbol} — term moneyness needs the underlying price")
    today = dt.date.today()

    def _valid(e):
        try:
            dt.date.fromisoformat(e)
            return True
        except (ValueError, TypeError):
            return False
    by_exp = {}
    for c in contracts:
        e = c.get("expiration")
        if e and _valid(e):
            by_exp.setdefault(e, []).append(c)
    exps = [e for e in sorted(by_exp) if 1 <= (dt.date.fromisoformat(e) - today).days <= max_days]
    if not exps:
        raise ChainError(f"error: no future expirations within {max_days}d in the {symbol} chain")
    kept = exps[:max_series]
    # An explicit --expiry beyond the cap (11th+ series / >max_days) is FORCED into the term set so the
    # combined page shows the series the user asked for (it silently fell back to the front otherwise —
    # gate regression, 2026-07-06). Only future expirations: the term view has no place for 0-DTE/past.
    if (include and include not in kept and include in by_exp
            and (dt.date.fromisoformat(include) - today).days >= 1):
        kept = sorted(set(kept) | {include})
    exps = kept

    def _iv_of(c):
        return plausible_iv(ff((c or {}).get("implied_volatility")))   # FRACTION; 0/None/AV-sentinel -> None (a hole)

    series, matrix, oimatrix = [], [], []
    for e in exps:
        rows = by_exp[e]
        calls = {ff(c["strike"]): c for c in rows if c.get("type") == "call" and ff(c.get("strike")) is not None}
        puts = {ff(c["strike"]): c for c in rows if c.get("type") == "put" and ff(c.get("strike")) is not None}
        strikes = sorted(set(calls) | set(puts))
        if not strikes:
            continue
        atm_iv, skew = atm_iv_skew(strikes, calls, puts, spot, _iv_of)   # shared live/history reduction
        dte = (dt.date.fromisoformat(e) - today).days
        oi_call = sum((ff(c.get("open_interest")) or 0) for c in rows if c.get("type") == "call")
        oi_put = sum((ff(c.get("open_interest")) or 0) for c in rows if c.get("type") == "put")
        atm_k = min(strikes, key=lambda k: abs(k - spot))   # AV-native ATM greeks (Γ≈call≡put; θ per the leg) for the term view
        _ac, _ap = calls.get(atm_k), puts.get(atm_k)

        def _atm_g(fld):
            v = ff((_ac or {}).get(fld))
            return v if v is not None else ff((_ap or {}).get(fld))
        series.append({"expiration": e, "dte": dte, "atmIV": (round(atm_iv, 4) if atm_iv else None),
                       "skew25": skew, "nContracts": len(rows), "oiCall": int(oi_call), "oiPut": int(oi_put),
                       "oiTotal": int(oi_call + oi_put), "quarterly": _is_quarterly(e),
                       "atmGamma": _atm_g("gamma"), "atmVega": _atm_g("vega"), "atmTheta": _atm_g("theta")})
        row, oi_row = [], []
        for b in TERM_BUCKETS:
            kstar = b * spot
            k = min(strikes, key=lambda x: abs(x - kstar))
            if abs(k - kstar) > 0.025 * spot:   # no listed strike within ±half-bucket -> a hole (both surfaces)
                row.append(None)
                oi_row.append(None)
                continue
            v = _iv_of(puts.get(k)) if k <= spot else _iv_of(calls.get(k))
            row.append(round(v * 100, 1) if v is not None else None)   # surface displays PERCENT
            c_oi = ff((calls.get(k) or {}).get("open_interest")) or 0
            p_oi = ff((puts.get(k) or {}).get("open_interest")) or 0
            oi_row.append(int(c_oi + p_oi))   # total call+put OI at the bucket strike (0 = a real datum)
        matrix.append(row)
        oimatrix.append(oi_row)
    if not series:
        raise ChainError(f"error: no usable series for the {symbol} term structure")
    return {"symbol": symbol.upper(), "spot": spot, "series": series,
            "surface": {"cols": [f"{round(b*100)}%" for b in TERM_BUCKETS], "matrix": matrix},
            "oiSurface": {"cols": [f"{round(b*100)}%" for b in TERM_BUCKETS], "matrix": oimatrix}}


def _is_quarterly(e):
    """True for a STANDARD quarterly expiration — the 3rd Friday of Mar/Jun/Sep/Dec (the liquid, listed-far-out
    contracts traders anchor on). weekday()==4 is Friday; the 3rd Friday always falls on day 15-21."""
    try:
        d = dt.date.fromisoformat(e)
    except (ValueError, TypeError):
        return False
    return d.month in (3, 6, 9, 12) and d.weekday() == 4 and 15 <= d.day <= 21


def _is_monthly(e):
    """True for a STANDARD monthly expiration — the 3rd Friday of ANY month (the listed 'monthlies';
    quarterlies are a subset). The near-term milestone between the front weekly and the far quarterly."""
    try:
        d = dt.date.fromisoformat(e)
    except (ValueError, TypeError):
        return False
    return d.weekday() == 4 and 15 <= d.day <= 21


def _highlight(series):
    """(nearest-quarterly index, most-liquid index) — the two series worth flagging in the switcher + Series
    table. Nearest quarterly = the first (series are date-ascending); most liquid = max total OI (None when
    the whole chain has no OI, e.g. brand-new weeklies)."""
    q = next((i for i, s in enumerate(series) if s.get("quarterly")), None)
    liq = max(range(len(series)), key=lambda i: series[i].get("oiTotal") or 0) if series else None
    if liq is not None and not (series[liq].get("oiTotal") or 0):
        liq = None
    return q, liq


# quarterly = blue, most-liquid = amber — used by the switcher options + legend AND the Series-table rows.
_HL_Q, _HL_L = "#2a78d6", AMBER   # ◆ nearest-quarterly (call-blue) · ★ most-liquid (AMBER — the shared palette amber)


def _series_marks(series):
    """Per-series marker glyphs (◆ nearest quarterly · ★ most OI) + colours + a legend, shared by the Chain
    switcher and the Term Series table so they read identically."""
    q, liq = _highlight(series)
    marks = []
    for i in range(len(series)):
        sym = ("◆" if i == q else "") + ("★" if i == liq else "")
        marks.append({"sym": sym, "color": (_HL_Q if i == q else _HL_L)} if sym else None)
    legend = ([{"sym": "◆", "color": _HL_Q, "label": "nearest quarterly"}] if q is not None else []) \
        + ([{"sym": "★", "color": _HL_L, "label": "most open interest"}] if liq is not None else [])
    return marks, legend


def _term_index(series, field):
    """Index a per-series field to the NEAREST expiration (front = 1.0) so greeks of different scales share
    one axis as term SHAPES. Front = the first non-None value; a None value (or a None/0 front) -> a gap
    (never fabricated). Theta (negative) indexes to a positive magnitude ratio."""
    vals = [s.get(field) for s in series]
    front = next((v for v in vals if v is not None), None)
    if not front:
        return [None] * len(vals)
    return [(round(v / front, 3) if v is not None else None) for v in vals]


def _peak_list(vals):
    """Scale a list to its OWN peak magnitude (=1.0); holes stay None; a None/0 peak -> all None. Robust to a
    near-zero front (unlike front-indexing): the SHAPE of a series on a shared 0..1 axis."""
    base = max((abs(v) for v in vals if v is not None), default=None)
    if not base:
        return [None] * len(vals)
    return [(round(abs(v) / base, 3) if v is not None else None) for v in vals]


def _term_index_peak(series, field):
    """A term field indexed to its own peak (=1.0): Γ/Θ peak at the front, vega at the back — all in 0..1."""
    return _peak_list([s.get(field) for s in series])


def _vega_weighted_tenor(series):
    """The vega-EXPOSURE-weighted average tenor (days): Σ(DTE · ATM vega · total OI) ÷ Σ(ATM vega · OI) —
    where the listed vol/vega exposure concentrates along the term (the surface's effective duration). A series
    missing vega / OI / DTE is skipped; no usable weight -> None (never invented)."""
    num = den = 0.0
    for s in series:
        v, oi, d = s.get("atmVega"), s.get("oiTotal"), s.get("dte")
        if v is None or oi is None or d is None:
            continue
        w = abs(v) * oi
        num += w * d
        den += w
    return (num / den) if den > 0 else None


def _tenor_label(d):
    """A short calendar tenor label for the term-curve axis: <1y in days (30d/90d/180d), else years (1y/2y)."""
    if d < 365:
        return "%dd" % d
    y = d / 365.0
    return ("%dy" % round(y)) if abs(y - round(y)) < 0.12 else ("%.1fy" % y)


def to_term_spec(T):
    """The term analysis -> a dashboard SPEC: ATM-IV/skew term curve + the IV surface + a series table."""
    S = T["series"]
    front = S[0]
    labels = [r["expiration"][5:] for r in S]           # MM-DD within the year horizon
    atm = [(round(r["atmIV"] * 100, 2) if r["atmIV"] is not None else None) for r in S]   # % for the chart
    skw = [r["skew25"] for r in S]
    # SQRT-time x-axis: points sit at sqrt(DTE) (the vol-term-structure standard — near-dated detail is
    # legible, far-dated compressed honestly, no categorical "even-spacing" lie); ticks at round tenors.
    dtes = [r["dte"] for r in S]
    xnum = [max(d, 0.5) ** 0.5 for d in dtes]
    _lo, _hi = min(dtes), max(dtes)
    xticks = [{"v": t ** 0.5, "t": _tenor_label(t)} for t in (7, 30, 90, 180, 365, 730, 1095)
              if _lo <= t <= _hi]
    slope = (round((S[-1]["atmIV"] - front["atmIV"]) * 100, 2)
             if (len(S) > 1 and S[-1]["atmIV"] is not None and front["atmIV"] is not None) else None)
    _hi_q, _hi_l = _highlight(S)   # tint + glyph the nearest-quarterly (◆) and most-liquid (★) rows
    _tbl_leg = ([{"sym": "◆", "color": _HL_Q, "label": "nearest quarterly"}] if _hi_q is not None else []) \
        + ([{"sym": "★", "color": _HL_L, "label": "most open interest"}] if _hi_l is not None else [])
    # every term line shows a visible dot at each expiration; hover names it (date · DTE); ◆/★ dots emphasised
    plabels = [f"{r['expiration']} · {r['dte']}d" for r in S]
    pmarks = [({"sym": "◆", "color": _HL_Q} if i == _hi_q else
               {"sym": "★", "color": _HL_L} if i == _hi_l else None) for i in range(len(S))]
    _pts = {"showPoints": True, "pointLabels": plabels, "pointMarks": pmarks}

    # ATM greeks term (two client-switchable normalizations — a toggle on the chart, see the panel below)
    def _gk_series(norm):
        return [{"name": "Γ (ATM)", "values": norm(S, "atmGamma")},
                {"name": "vega (ATM)", "values": norm(S, "atmVega"), "dash": [6, 4]},
                {"name": "Θ (ATM)", "values": norm(S, "atmTheta"), "dash": [2, 3]}]
    _gk_peak = {"labels": labels, "xnum": xnum, "xticks": xticks, **_pts, "series": _gk_series(_term_index_peak)}
    _gk_front = {"labels": labels, "xnum": xnum, "xticks": xticks, "ylog": True, **_pts,
                 "series": _gk_series(_term_index)}
    # ATM IV & 25Δ skew — a toggle: ABSOLUTE (native %/vol-pts, the default) or each scaled TO PEAK (0..1
    # shapes — read whether IV and skew move together across the term regardless of their different units)
    _iv_abs = {"labels": labels, "xnum": xnum, "xticks": xticks, **_pts, "series": [
        {"name": "ATM IV %", "values": atm}, {"name": "25Δ skew (vol pts)", "values": skw, "dash": [6, 4]}]}
    _iv_peak = {"labels": labels, "xnum": xnum, "xticks": xticks, **_pts, "series": [
        {"name": "ATM IV (÷peak)", "values": _peak_list(atm)},
        {"name": "25Δ skew (÷peak)", "values": _peak_list(skw), "dash": [6, 4]}]}
    rows = [[r["expiration"] + ("  " + ("◆" if i == _hi_q else "") + ("★" if i == _hi_l else "")
             if (i == _hi_q or i == _hi_l) else ""),
             str(r["dte"]), iv(r["atmIV"]) if r["atmIV"] is not None else "n/a",
             (f"{r['skew25']:+.1f}" if r["skew25"] is not None else "n/a"), str(r["nContracts"]),
             num(r.get("oiCall", 0), 0), num(r.get("oiPut", 0), 0),   # total call / put OI per expiration
             (f"{r['oiPut'] / r['oiCall']:.2f}" if r.get("oiCall") else "n/a")]   # PCR = putOI / callOI
            for i, r in enumerate(S)]
    rowstyle = _oi_heat_rowstyle(S)   # H: per-row OI heat over EVERY row (all large series shaded, not just ★)
    # --- extra term tiles (user-agreed): skew term slope + the ◆/★ reference-series anchors + vega tenor.
    # These summarise the WHOLE term / the meaningful expirations — not just the (often thin) front series.
    _sk_back = next((s["skew25"] for s in reversed(S) if s["skew25"] is not None), None)
    _sk_slope = (round(_sk_back - front["skew25"], 1)
                 if (front.get("skew25") is not None and _sk_back is not None) else None)
    _vtenor = _vega_weighted_tenor(S)

    # nearest STANDARD monthly (3rd Friday) — the near-term milestone between the front weekly and the far
    # quarterly. Drop it when it coincides with the front (idx 0, already the Front tiles) or the ◆ quarterly.
    _hi_m = next((i for i, s in enumerate(S) if _is_monthly(s["expiration"])), None)
    if _hi_m is not None and _hi_m in (0, _hi_q):
        _hi_m = None

    def _anchor(idx, glyph, name, desc):   # a compact tile for a reference series (nearest monthly / ◆ quarterly / ★ most-liquid)
        if idx is None:
            return None
        a = S[idx]
        sk = (" · Δ%+.1f" % a["skew25"]) if a.get("skew25") is not None else ""
        return {"label": (glyph + " " + name) if glyph else name,
                "value": iv(a["atmIV"]) if a["atmIV"] is not None else "n/a",
                "sub": ["%s · %dd%s" % (a["expiration"][5:], a["dte"], sk)],
                "hint": ("The %s expiration %s (%d DTE): ATM IV %s%s — %s."
                         % (name.lower(), a["expiration"], a["dte"],
                            iv(a["atmIV"]) if a["atmIV"] is not None else "n/a",
                            (", 25Δ skew %+.1f vol pts" % a["skew25"]) if a.get("skew25") is not None else "", desc))}
    _cards = [
        {"label": "Spot", "value": "$" + num(T["spot"], 2),   # equity price cards carry $ (review M4)
         "hint": "Underlying price (AV GLOBAL_QUOTE at fetch) — every moneyness bucket is keyed off it."},
        {"label": "Front ATM IV", "value": iv(front["atmIV"]) if front["atmIV"] is not None else "n/a",
         "hint": "ATM implied vol of the NEAREST expiration — the short-dated end of the curve."},
        {"label": "Term slope · live", "value": (f"{slope:+.1f}" if slope is not None else "n/a"),
         "hint": "Live (REALTIME_OPTIONS): ATM IV of the farthest shown series minus the front, vol points. "
                 "Positive = contango (normal); negative = inverted — near-dated stress (event/earnings) prices "
                 "above the back. (Overview shows the settlement-history slope as a regime label.)"},
        {"label": "Skew term slope", "value": (f"{_sk_slope:+.1f}" if _sk_slope is not None else "n/a"),
         "hint": "Farthest minus front 25Δ skew (vol pts). Positive = downside skew STEEPENS with tenor "
                 "(long-dated crash premium richer); negative = the near-term skew is steeper (front event/crash fear)."},
        {"label": "Series", "value": str(len(S)),
         "hint": "Expirations shown (nearest first, capped) — all from the single AV payload."},
        {"label": "Front DTE", "value": str(front["dte"]),
         "hint": "Calendar days to the nearest expiration."},
        # near-term expiration milestones, in order out: monthly -> quarterly -> most-liquid. When the ◆
        # quarterly IS the ★ most-liquid series (common: the Sep/Dec quarterly usually carries the most OI),
        # MERGE them into ONE ◆★ tile instead of two identical ones.
        _anchor(_hi_m, "◇", "Monthly", "the nearest standard monthly (3rd Friday)"),
        *([_anchor(_hi_q, "◆★", "Quarterly · most liquid",
                   "the nearest standard quarterly roll (3rd Friday of Mar/Jun/Sep/Dec) — also the expiration with the most open interest")]
          if (_hi_q is not None and _hi_q == _hi_l)
          else [_anchor(_hi_q, "◆", "Quarterly", "the nearest standard quarterly roll (3rd Friday of Mar/Jun/Sep/Dec)"),
                _anchor(_hi_l, "★", "Most liquid", "the expiration with the most open interest")]),
        {"label": "Vega tenor", "value": (_tenor_label(round(_vtenor)) if _vtenor is not None else "n/a"),
         "hint": "Vega-exposure-weighted average tenor: Σ(DTE · ATM vega · total OI) ÷ Σ(ATM vega · OI). Where the "
                 "listed vega exposure concentrates along the term — the surface's effective duration."},
    ]
    _cards = [c for c in _cards if c is not None]
    return {
        "title": f"{T['symbol']} options · term structure · {len(S)} series",
        "subtitle": (f"Alpha Vantage REALTIME_OPTIONS · one payload, every expiration · AV-native IV/greeks "
                     f"(never recomputed) · spot {num(T['spot'])} · not advice"),
        "cards": _cards,
        "panels": [
            {"type": "line", "title": "ATM IV & 25Δ skew by expiration", "full": True, "info":
                "The vol term structure: AV-native ATM IV per expiration (solid) and the 25Δ skew "
                "(dashed, native deltas). Upward = contango; a front bump = near-dated event risk. "
                "Toggle 'to peak' scales each to its own max (0..1) to compare their term SHAPES. "
                "The x-axis is SQRT-time (days out) — near-dated detail spread, far-dated compressed.",
             "data": _iv_abs, "variants": {"label": "scale", "index": 0, "options": [
                 {"name": "absolute", "data": _iv_abs}, {"name": "to peak", "data": _iv_peak}]}},
            {"type": "line", "title": "Put/call ratio (OI) by expiration", "full": True, "info":
                "Full-chain put/call ratio by open interest per expiration (putOI ÷ callOI) — the "
                "positioning term structure. >1 = more put than call OI on that tenor (hedging / bearish "
                "tilt); a front spike often flags event hedging. Same SQRT-time x-axis as the vol curve.",
             "data": {"labels": labels, "xnum": xnum, "xticks": xticks, **_pts, "series": [
                 {"name": "PCR (OI)", "values": [(round(r["oiPut"] / r["oiCall"], 3) if r.get("oiCall") else None)
                                                 for r in S]}]}},
            {"type": "line", "title": "ATM greeks by expiration", "full": True, "info":
                "AV-native ATM Γ / vega / Θ per expiration (never recomputed). Two ways to fit three "
                "different-scale greeks on one axis — toggle on the chart: 'to peak' scales each greek to its "
                "own max (=1, linear) — robust, shows each term SHAPE (Γ/Θ peak near-term, vega far-term); 'to "
                "front' indexes each to the nearest expiration (=1) on a LOG axis — keeps the ×-relative-to-"
                "front reading. Absolute per-contract values live on the Chain tab. Same SQRT-time x-axis; a "
                "missing greek shows a gap (never fabricated).",
             "data": _gk_peak, "variants": {"label": "scale", "index": 0, "options": [
                 {"name": "to peak · linear", "data": _gk_peak},
                 {"name": "to front · log", "data": _gk_front}]}},
            dashboard.add_hm_color_toggle({"type": "heatmap", "title": "IV surface (moneyness × expiry)", "info":
                "OTM-leg native IV at the nearest listed strike per 5% moneyness bucket (±half-bucket "
                "tolerance). n/a = no strike/quote there — holes stay holes: nothing is interpolated "
                "across strikes or series. Hotter = richer vol. Toggle 'log' colour to read finer gradations "
                "when a few strikes/series carry outlier IV.",
             "data": {"rows": [f"{r['expiration'][5:]} · {r['dte']}d" for r in S],
                      "cols": T["surface"]["cols"], "matrix": T["surface"]["matrix"], "ramp": "seq"}}),
            dashboard.add_hm_color_toggle({"type": "heatmap", "title": "Open interest by moneyness × expiry", "info":
                "Total open interest (call+put) at the nearest listed strike per 5% moneyness bucket — "
                "the OI twin of the IV surface above. Hotter = more open contracts (where positioning "
                "concentrates across the curve). 0 = a real datum (no OI there); n/a = no listed strike "
                "within tolerance — holes stay holes. Toggle 'log' colour — OI is very skewed, so log spreads "
                "the mid-range.",
             "data": {"rows": [f"{r['expiration'][5:]} · {r['dte']}d" for r in S],
                      "cols": T["oiSurface"]["cols"], "matrix": T["oiSurface"]["matrix"],
                      "ramp": "seq", "cellFmt": "int"}}),
            {"type": "table", "title": "Series", "info":
                "One row per expiration from the single payload. n/a = the feed carried no usable value — "
                "never filled. ◆ (blue) = the nearest standard quarterly (3rd Friday of Mar/Jun/Sep/Dec); "
                "★ (amber) = the most liquid expiration by total open interest.",
             "data": {"columns": ["Expiry", "DTE", "ATM IV", "25Δ skew", "Contracts", "Call OI", "Put OI", "PCR"],
                      "rows": rows, "rowstyle": rowstyle, "legend": _tbl_leg}},
        ],
    }


def _term_keep(T, keep):
    """Drop term rows for series the chain build rejected (<3 strikes) so the switcher options, the
    IV surface AND the OI surface stay index-aligned with the built specs — a matrix longer than the
    series list makes the heatmap renderer bail out ('no matrix')."""
    if len(keep) == len(T["series"]):
        return T
    return {**T, "series": [T["series"][i] for i in keep],
            "surface": {**T["surface"], "matrix": [T["surface"]["matrix"][i] for i in keep]},
            "oiSurface": {**T["oiSurface"], "matrix": [T["oiSurface"]["matrix"][i] for i in keep]}}


def combined_spec(specs, T, target_idx=0, series_tile=None):
    """A SWITCHABLE combined --term page: the chosen series' chain (smile/OI/table) + an EXPIRATION
    SWITCHER that swaps WHICH series those charts show — every series' page is pre-built here (AV-native
    IV/greeks, read once), so the swap is client-side with zero network — plus the cross-expiration term
    panels (curve/surface/series-table) appended below as the fixed overview. All from the SAME single
    REALTIME_OPTIONS payload (--term costs no extra calls on the equity path)."""
    S = T["series"]
    tsp = to_term_spec(T)
    chain_n = len(specs[target_idx]["panels"])   # the per-series chain panels
    panels = []
    for j in range(chain_n):
        p = dict(specs[target_idx]["panels"][j])
        if p.get("variants"):
            # v1.132: a variant panel (the OI·Δ window toggle) — each expiration carries its OWN
            # per-window options; the switcher swaps the whole options-set, the vtoggle picks the
            # window WITHIN it (orthogonal). `p["variants"]` stays = the target's (active index +
            # its options); switchSeries re-points .options to series[idx] on an expiration swap.
            p["series"] = [specs[i]["panels"][j].get("variants", {}).get("options",
                                                                         [{"data": specs[i]["panels"][j]["data"]}])
                           for i in range(len(specs))]
            p["seriesIsVariants"] = True
        else:
            p["series"] = [specs[i]["panels"][j]["data"] for i in range(len(specs))]
        panels.append(p)
    panels += tsp["panels"]
    # PERMANENT cross-expiration tiles lead every series' card set — they frame the GENERAL page and do
    # NOT swap with the switcher; the per-series chain tiles follow and DO swap.
    # G: the Series tile is the RICH _series_tile (◇/◆/★ ladder) — IDENTICAL to the Overview one — not
    # the plain count-only term-table "Series" card. `series_tile` lets --full pass the FULL-term tile:
    # this `T` is the CHARTABLE subset (thin <3-strike series dropped), but the count/ladder must match
    # Overview + the Term table, which cover EVERY expiration. The other two ride from the term spec.
    _series = series_tile if series_tile is not None else _series_tile(T)
    perm = [_series] + [c for c in (next((x for x in tsp["cards"] if x["label"] == lab), None)
                                    for lab in ("Front ATM IV", "Term slope · live")) if c]  # Spot already on each per-series card (constant)
    card_series = [perm + list(specs[i]["cards"]) for i in range(len(specs))]
    out = dict(specs[target_idx])
    out["title"] = tsp["title"]                   # GENERAL page = all-series title, not the target series'
    out["panels"] = panels
    out["cards"] = card_series[target_idx]
    out["cardSeries"] = card_series
    suffix = f" · +term ({len(S)} series, switchable)"
    _marks, _legend = _series_marks(S)   # ◆ nearest quarterly · ★ most OI — highlighted in the dropdown + legend
    out["switcher"] = {"label": "Expiration", "index": target_idx,
                       "options": [f"{s['expiration']} · {s['dte']} DTE" for s in S],
                       "expiries": [s["expiration"] for s in S],   # --watch: the client posts this to /pin on swap
                       "marks": _marks, "legend": _legend,
                       # per-series subtitles so window notes / as-of track the selection
                       "subtitles": [specs[i].get("subtitle", "") + suffix for i in range(len(specs))],
                       "hint": "switch which expiration the chain charts (smile / OI / table) show — "
                               "the term panels below stay as the cross-expiration overview"}
    out["subtitle"] = specs[target_idx].get("subtitle", "") + suffix
    return out


def _series_tile(T):
    """The Series-count Overview tile with the ◇ nearest-monthly · ◆ nearest-quarterly · ★ most-liquid
    ladder — shared by the warm (settlement) and cold (live-synthesis) --full Overview so they read alike."""
    S = T["series"]
    hq, hl = _highlight(S)
    hm = next((i for i, s in enumerate(S) if _is_monthly(s["expiration"])), None)
    bits = []
    if hm is not None and hm not in (0, hq):
        bits.append("◇ " + S[hm]["expiration"][5:])
    if hq is not None and hq == hl:
        bits.append("◆★ " + S[hq]["expiration"][5:])
    else:
        if hq is not None:
            bits.append("◆ " + S[hq]["expiration"][5:])
        if hl is not None:
            bits.append("★ " + S[hl]["expiration"][5:])
    return {"label": "Series", "value": str(len(S)),
            "sub": [" · ".join(bits)] if bits else None,
            "hint": "Future expirations listed. ◇ nearest monthly · ◆ nearest quarterly · "
                    "★ most open interest — the key series (full ladder on the Chain switcher + Term table)."}


def _overview_live(symbol, T, loading=False):
    """The COLD-name Overview for --full: the LIVE synthesis from the ONE chain pull (the settlement
    vol-history store isn't warm yet, so the retrospective panels can't be built). Mirrors
    build_overview's headline — front ATM IV / 25Δ skew / term slope / PCR — read live from the nearest
    series, and GATES the genuinely-retrospective reads (IV rank/percentile, the vol cone, constant-
    maturity IV history). When `loading` (a served page IS backfilling the history in the background),
    the gate is an ACTIVE 'loading…' state with a blinking-dots note that fills in live; otherwise it's a
    passive '—' tile + a note naming the exact warm command. Never a blank tab; zero extra network."""
    S = T["series"]
    front = S[0]
    fatm, sk = front.get("atmIV"), front.get("skew25")
    slope = (round((S[-1]["atmIV"] - fatm) * 100, 1)
             if (len(S) > 1 and S[-1].get("atmIV") is not None and fatm is not None) else None)
    pcr = (front["oiPut"] / front["oiCall"]) if front.get("oiCall") else None
    scope = ("nearest · %dd DTE" % front["dte"]) if front.get("dte") is not None else "nearest expiry"
    if loading:   # a served page is backfilling the vol history NOW — active loading, fills in via SSE
        iv_sub, iv_tail = ["loading…"], "Loading the settlement vol-history backfill now — it fills in automatically."
        note = {"type": "note", "full": True, "loading": True, "title": "Volatility history — loading",
                "text": "Loading volatility history — IV rank, the vol cone and constant-maturity IV history"}
    else:         # a static page — passive gate naming the exact warm command
        cta = ("IV rank / percentile, the vol cone and constant-maturity IV history need the settlement vol "
               "store — not warm for %s yet.\nIt fills on the nightly prewarm, or run now:  options.py %s --history"
               % (symbol.upper(), symbol))
        iv_sub, iv_tail = ["needs vol history"], cta
        note = {"type": "note", "full": True, "title": "Volatility history — warming", "text": cta}
    cards = [
        _series_tile(T),
        {"label": "IV rank · 30d", "value": "—", "sub": iv_sub,
         "hint": "The cheap/rich read — where 30d ATM IV sits in its own settlement history. " + iv_tail},
        {"label": "ATM IV · front", "value": iv(fatm) if fatm is not None else "n/a",
         "scope": "expiry", "scopeLabel": scope,
         "hint": "Live (REALTIME_OPTIONS) at-the-money implied vol of the nearest expiration — AV-native, "
                 "read as-is. The full smile is the Chain tab."},
        {"label": "25Δ skew · front", "value": ("%+.1f" % sk) if sk is not None else "n/a",
         "scope": "expiry", "scopeLabel": scope,
         "hint": "Live IV(25Δ put) − IV(25Δ call) of the nearest expiration, vol points. Positive = downside puts bid."},
        {"label": "Term slope · live", "value": ("%+.1f" % slope) if slope is not None else "n/a",
         "hint": "Live: ATM IV of the farthest shown series minus the front, vol points. Positive = contango "
                 "(normal); negative = inverted — near-dated event/stress. The settlement-history regime label "
                 "fills once the vol store warms."},
        {"label": "PCR · OI", "value": ("%.2f" % pcr) if pcr is not None else "n/a",
         "hint": "Live full-chain put/call ratio by open interest of the nearest expiration. >1 = more put than call OI."},
    ]
    return {"id": "overview", "name": "Overview", "cards": cards, "panels": [note]}


def build_full(symbol, contracts, quote, hist, earnings=None, expiry=None, window=None, exchange=None, live=False, loading=False, profile=None, closes=None, prev_oi=None):
    """The combined --full page as a SECTIONED spec [Overview | Chain | Term | History] for the tabs
    engine (dashboard.render_interactive_html), from the ONE whole-chain payload. The **Term** tab is
    the full cross-expiration inventory — its curve / IV+OI surfaces / Series table cover EVERY listed
    future expiration. The **Chain** tab OWNS the per-expiration drill (smile / OI / GEX / ladder) via an
    expiration switcher that lists EVERY chartable expiration (a series too thin to chart, <3 strikes,
    drops from the switcher but still rides the Term table). Overview + History come from the settlement
    store `hist` (cache-first, no extra pull); a cold name gets 'warming' placeholders and leads on the
    Chain tab. `exchange` (optional) adds a listing-venue header chip. The caller `accrete`s cold names."""
    import opthist
    _BIG = 10 ** 9
    T = build_term(symbol, contracts, quote, max_series=_BIG, max_days=_BIG, include=(expiry or None))
    D = build(symbol, contracts, quote, expiry, window)
    ti = next((i for i, s in enumerate(T["series"]) if s["expiration"] == D["expiration"]), None)
    if expiry and ti is None:   # a same-day/past listed --expiry: build() takes it, but Term is future-only.
        raise ChainError(f"error: --expiry {D['expiration']} cannot be shown on the --full page (future "
                         f"series only) — run without --full for that single series")   # never silently re-anchor
    tsp = to_term_spec(T)                            # Term tab = ALL future expirations (curve/surface/table)
    # A chain per EVERY expiration — the switcher shows them all (nearest-N was uninteresting: fresh weeklies
    # with no OI etc.). Thin series (<3 strikes) drop from the switcher; Term still lists them. Under --watch
    # the switcher STAYS: live re-pins to whichever series is selected (the server re-polls it).
    specs, keep = [], []
    for i, s in enumerate(T["series"]):
        try:
            _d = build(symbol, contracts, quote, s["expiration"], window)
            _d["profile"] = profile                 # so the Overview About names the underlying issuer
            _attach_droi(_d, prev_oi)               # v1.130: the 1-day ΔOI ladder (None-safe no-op)
            specs.append(to_spec(_d))
            keep.append(i)
        except ChainError:
            continue
    if not specs:
        raise ChainError(f"error: no {symbol} series has enough strikes to chart with --full")
    T_switch = _term_keep(T, keep)               # switcher aligned to the chartable subset (thin dropped)
    target_idx = next((i for i, s in enumerate(T_switch["series"]) if s["expiration"] == D["expiration"]), 0)
    combined = combined_spec(specs, T_switch, target_idx, series_tile=_series_tile(T))  # G: full-term Series tile == Overview (T_switch is the chartable subset)
    n_chain = len(specs[target_idx]["panels"])   # the per-series chain panels (with .series[])
    smile = {"id": "smile", "name": "Chain", "cards": combined["cards"],
             "panels": combined["panels"][:n_chain], "switcher": combined["switcher"],
             "cardSeries": combined["cardSeries"]}
    smile["switcher"]["hint"] = "chain (smile · OI · GEX · ladder) for the picked expiration"   # short: fits one line
    term = {"id": "term", "name": "Term", "cards": tsp["cards"], "panels": tsp["panels"]}  # comparison-only, no chain
    if hist:
        overview = opthist.build_overview(hist, earnings, closes=closes)
        history = opthist.build_history_section(symbol, hist, earnings, closes=closes)   # carries the window switcher
        active = "overview"
    else:
        # Cold name: the Overview is a LIVE synthesis (never a blank tab). The retrospective History tab
        # has nothing live to synthesize -> it's a placeholder. `loading` (a served page IS backfilling
        # the history now) shows the blinking-dots 'loading' state that fills in via SSE; otherwise the
        # passive 'warming' placeholder naming the warm command.
        overview = _overview_live(symbol, T, loading=loading)
        if loading:
            history = {"id": "history", "name": "History", "state": "loading", "cards": [], "panels": [],
                       "loadingText": "Loading volatility history — IV rank, the vol cone, 25Δ-skew & PCR history, "
                                      "term-through-time"}
        else:
            history = {"id": "history", "name": "History", "state": "warming", "cards": [], "panels": [],
                       "warmingText": ("Volatility history needs the settlement store — warming up. It fills after "
                                       "the nightly prewarm, or run:  options.py %s --history" % symbol)}
        active = "smile"
    if live:               # --watch leads on (and streams) the Chain tab; the SSE deltas target it
        active = "smile"
    # v1.110: the underlying's identity is the page-level LEAD block now (shared with the fundamentals
    # dashboard) + a compact Contract tile — NOT a section About tile. Built in the final assembly below.
    # NO second header line anywhere (user's standing call). Overview/History ARE the settlement cache,
    # dated to a possibly older close than the live-pull header chip — so the honest "as of <close> · NOT
    # live" provenance rides as a TILE on those two tabs, not a subtitle. Chain/Term carry nothing (they
    # ARE the live pull the header dates). Every section/switcher subtitle is stripped.
    settle = hist[-1].get("date") if hist else None
    if settle:
        # Lead the warm Overview with the Series-count ladder tile (◇ monthly · ◆ quarterly · ★ most-liquid,
        # shared with the cold path), then build_overview's rich settlement synthesis. (Spot is the page
        # header — no duplicate tile.)
        _ser = _series_tile(T)
        _sc = {"label": "Settlement", "value": settle,
               "hint": "Overview & History are settlement-cache data as of %s (last close) — NOT live, unlike "
                       "the Chain / Term tabs (the live pull the header is dated to)." % settle}
        overview["cards"] = [_ser] + overview.get("cards", [])   # Settlement tile dropped from Overview (user 2026-07-08); stays on History
        history["cards"] = [_sc] + history.get("cards", [])
        if history.get("cardSeries"):                  # keep the per-window card sets aligned with the shown cards
            history["cardSeries"] = [[_sc] + c for c in history["cardSeries"]]
    # Trading-session tile — the ONE tile about LIVE data, so it rides the CHAIN tab. Double-width; shows the
    # status + a LIVE countdown to the next open/close (client-ticked from the embedded schedule). The exact
    # session hours ride the TOOLTIP only (not printed on the tile). Prepended to the cards AND every switcher
    # card set (global — survives an expiration switch).
    sess = opthist.market_session(exchange, assume_us=True)   # F: an AV-pullable chain is US-listed (so an ETF w/o an OVERVIEW venue still gets the session tile)
    if sess:
        _sess_card = {"label": "Session · " + sess["exchange"], "value": sess["status"],   # a normal (uniform) tile
                      "small": True,   # one-line compact value — "Closed · weekend" must not blow the card row up
                      "countdown": {"schedule": sess["schedule"], "text": sess["countdownText"]},
                      "hint": ("%s regular %s (%s local); pre-market from %s, after-hours to %s. Now %s / %s. "
                               "Holidays are not checked (weekends are)." % (sess["exchange"], sess["hoursExch"],
                               sess["hoursLocal"], sess["preEt"], sess["postEt"], sess["nowExch"], sess["nowLocal"]))}
        smile["cards"] = [_sess_card] + smile["cards"]
        if smile.get("cardSeries"):
            smile["cardSeries"] = [[_sess_card] + c for c in smile["cardSeries"]]
    # Spot lives in the page header (live) -> drop the redundant Spot tile from every --full tab.
    def _no_spot(cards):
        return [c for c in (cards or []) if c.get("label") != "Spot"]
    smile["cards"], term["cards"] = _no_spot(smile["cards"]), _no_spot(term["cards"])
    overview["cards"] = _no_spot(overview.get("cards"))
    if smile.get("cardSeries"):
        smile["cardSeries"] = [_no_spot(c) for c in smile["cardSeries"]]
    for _sec in (overview, smile, term, history):
        _sec.pop("subtitle", None)
        if _sec.get("switcher"):
            _sec["switcher"].pop("subtitles", None)
    # v1.110: header chips are GONE (user 2026-07-15). The underlying's company blurb rides the page-level
    # LEAD block (SHARED verbatim with the fundamentals dashboard — same OVERVIEW Description via profile_get)
    # with FY-end · as-of · site trailing it; the standardized contract terms + the sector/industry
    # classification (the identity we removed from the chips) collapse into ONE compact Contract tile on
    # Overview (not bloated). Alpha Vantage / not-advice moved to the footer (dataSource).
    _p = profile or {}
    _lead_meta = ("FY ends %s · " % _p["fiscalYearEnd"] if _p.get("fiscalYearEnd") else "") \
        + ("as of %s" % (D.get("asOf") or "n/a"))
    lead = {"text": _p.get("description") or "", "meta": _lead_meta, "site": _p.get("site") or ""}
    _cls = " · ".join(x for x in (_p.get("sector"), _p.get("industry")) if x)
    _contract_subs = ["physically settled" + (" · " + exchange if exchange else "")]
    if _cls:
        _contract_subs.append(_cls)
    _contract = {"label": "Contract", "value": "American · ×100", "sub": _contract_subs,
                 "hint": ("US-listed equity/ETF options are standardized: American-style exercise, physical "
                          "settlement, standard multiplier 100 shares per contract (adjusted / non-standard "
                          "contracts can differ — verify terms with the OCC). Chain, IV and greeks are "
                          "Alpha Vantage REALTIME_OPTIONS values, read as-is.")}
    _ovc = overview.get("cards", [])
    overview["cards"] = _ovc[:1] + [_contract] + _ovc[1:]   # Contract sits right AFTER the Series lead tile (keep Series first)
    # v1.113: an ESTIMATED spot marks the head — price AND cap carry ' est' like the single-series
    # Spot-card/text twins (today build_term refuses a quote-less payload before this head is
    # assembled, so the marker is belt-and-suspenders on --full — but the display rule must hold
    # if any path ever sets the flag; the live poll path CAN, see the produce() head rewrite).
    _est = " est" if D.get("spotEstimated") else ""
    out = {
        "title": "%s options · full analysis" % D["symbol"],
        # v1.110: ticker + underlying NAME (shared with fundamentals) + the "Options" board type under it;
        # the sibling-board link pins by the price on the right (all engine-side, user 2026-07-15).
        # v1.112: the change/cap line comes from _right_small — ONE rule with the live poll, so it ticks.
        "head": {"leftBig": D["symbol"], "leftName": _p.get("name") or "", "leftType": "Options",
                 "rightBig": "$" + num(D["spot"], 2) + _est,
                 "rightSmall": _right_small(_change_from_quote(quote), D.get("spot"),
                                            _p.get("sharesOutstanding"), est=D.get("spotEstimated"))},
        "lead": lead, "dataSource": "Alpha Vantage",
        "sections": [overview, smile, term, history], "active": active}
    if live:   # the SSE stream feeds ONLY this section; the client gates deltas to it (ignores them off-tab)
        out["liveSection"] = "smile"
    return out


def render_term_text(T):
    S = T["series"]
    L = [f"{T['symbol']} OPTIONS TERM STRUCTURE · spot {num(T['spot'])} · {len(S)} series",
         f"{'EXPIRY':>12}{'DTE':>6}{'ATM_IV':>9}{'25D_SKEW':>10}{'CONTRACTS':>11}"]
    for r in S:
        av_ = iv(r["atmIV"]) if r["atmIV"] is not None else "n/a"
        sk = f"{r['skew25']:+.1f}" if r["skew25"] is not None else "n/a"   # hoisted: nested f-quotes break 3.9
        L.append(f"{r['expiration']:>12}{r['dte']:>6}{av_:>9}{sk:>10}{r['nContracts']:>11}")
    return "\n".join(L)


def _spot_from_quote(quote):
    if not isinstance(quote, dict):
        return None
    gq = quote.get("Global Quote") or quote.get("Global Quote - DATA") or {}
    p = ff(gq.get("05. price"))
    if p is not None:
        return p
    # MCP CSV-in-result shape
    res = quote.get("result")
    if isinstance(res, str) and "," in res:
        import csv as _csv
        import io as _io
        rows = list(_csv.DictReader(_io.StringIO(res)))
        if rows:
            return ff(rows[0].get("price"))
    return None


def _right_small(chg, spot, shares, est=False):
    """The header change/cap line — ONE formatting rule shared by build_full (first paint) and the
    live --full poll (every tick), so the line can never freeze out of step with the spot beside it
    (review P2 2026-07-15). cap = LIVE spot x cached shares outstanding (shares are slow-moving and
    TTL-refreshed by opthist.profile_get; spot is live) — never a frozen cached MarketCap. v1.113:
    display via the SHARED dashboard.cap_str rule ($M under $1B / integer $B / $X.XXT at a trillion
    — num(x,0) used to print 'cap $0B' on a sub-$0.5B name and '$3,213B' at a trillion); est=True
    (an ESTIMATED spot) marks the cap ' est' — never a silent number off a guessed price."""
    chg_s = ("%+.2f%%" % chg) if chg is not None else ""
    capB = (spot * shares / 1e9) if (shares and spot) else None
    _cs = dashboard.cap_str(capB)
    cap_s = ("cap " + _cs + (" est" if est else "")) if _cs else ""
    return " · ".join(x for x in (chg_s, cap_s) if x)


def _change_from_quote(quote):
    """The day's change percent (float, signed) from a GLOBAL_QUOTE — for the header's change/cap line
    (v1.110, both boards). REST '10. change percent' ("1.75%") or the MCP CSV 'changePercent' (the AV
    GLOBAL_QUOTE CSV column — NOT the REALTIME_BULK_QUOTES snake_case 'change_percent'). None if absent
    — the header just omits it (never a fake 0)."""
    if not isinstance(quote, dict):
        return None
    gq = quote.get("Global Quote") or quote.get("Global Quote - DATA") or {}
    cp = gq.get("10. change percent")
    if cp is None:
        res = quote.get("result")
        if isinstance(res, str) and "," in res:
            import csv as _csv
            import io as _io
            rows = list(_csv.DictReader(_io.StringIO(res)))
            cp = rows[0].get("changePercent") if rows else None   # GLOBAL_QUOTE CSV column (== fundamentals._norm_quote)
    if cp is None:
        return None
    try:
        return float(str(cp).strip().rstrip("%"))
    except ValueError:
        return None


# ----------------- compute -----------------
def build(symbol, contracts, quote, expiry, window):
    if not contracts:
        raise ChainError(f"error: no option contracts for {symbol} (bad ticker / non-equity / no entitlement?)")
    # group by expiration — keep only well-formed dates so a garbage value can't be picked
    today = dt.date.today()

    def _valid_date(e):
        try:
            dt.date.fromisoformat(e)
            return True
        except (ValueError, TypeError):
            return False
    exps = sorted({c.get("expiration") for c in contracts if c.get("expiration") and _valid_date(c.get("expiration"))})
    if not exps:
        raise ChainError(f"error: no valid expirations in the {symbol} chain")

    def dte_of(e):
        return (dt.date.fromisoformat(e) - today).days
    if expiry:
        if expiry not in exps:
            near = min(exps, key=lambda e: abs(dte_of(e) - dte_of(expiry))) if _valid_date(expiry) else exps[0]
            raise ChainError(f"error: {symbol} has no expiration {expiry}. Available (nearest {near}): {', '.join(exps[:12])}{' …' if len(exps)>12 else ''}")
        chosen = expiry
    else:
        future = [e for e in exps if dte_of(e) >= 1]
        chosen = future[0] if future else exps[0]
    dte = dte_of(chosen)

    rows = [c for c in contracts if c.get("expiration") == chosen]
    calls = {ff(c["strike"]): c for c in rows if c.get("type") == "call" and ff(c.get("strike")) is not None}
    puts = {ff(c["strike"]): c for c in rows if c.get("type") == "put" and ff(c.get("strike")) is not None}
    strikes = sorted(set(calls) | set(puts))
    if len(strikes) < 3:
        raise ChainError(f"error: {symbol} {chosen} has <3 strikes — pick another expiration")

    spot = _spot_from_quote(quote)
    spot_est = False
    if spot is None:  # estimate from the strike whose call delta is nearest 0.5
        cand = [(k, ff(c.get("delta"))) for k, c in calls.items() if ff(c.get("delta")) is not None]
        if cand:
            spot = min(cand, key=lambda kd: abs(kd[1] - 0.5))[0]
            spot_est = True
        else:
            spot = strikes[len(strikes) // 2]
            spot_est = True

    atm = min(strikes, key=lambda k: abs(k - spot))
    ai = strikes.index(atm)
    # DEFAULT (window is None) = the FULL TRADEABLE ladder — every strike with open interest on
    # either side (drops dead never-traded listings; keeps the whole real chain, NOT a ±window).
    # An explicit --window N zooms to ±N strikes around ATM.
    if window is None:
        def _oi(side, k):
            return ff((side.get(k) or {}).get("open_interest")) or 0
        win = [k for k in strikes if _oi(calls, k) > 0 or _oi(puts, k) > 0] or strikes
    else:
        lo = max(0, ai - window); hi = min(len(strikes), ai + window + 1)
        win = strikes[lo:hi]

    def g(d, k):
        return ff((d or {}).get(k))

    def ivg(d):   # native IV, sentinel-guarded — the Chain tab must agree with Term/Overview (one page, one truth)
        return plausible_iv(g(d, "implied_volatility"))

    atm_civ = ivg(calls.get(atm)); atm_piv = ivg(puts.get(atm))
    atm_iv = next((x for x in [
        (atm_civ + atm_piv) / 2 if (atm_civ is not None and atm_piv is not None) else None,
        atm_civ, atm_piv] if x is not None), None)

    # 25-delta skew (risk reversal): IV(25Δ put) - IV(25Δ call), over the whole expiry
    c25 = min(((k, c) for k, c in calls.items() if g(c, "delta") is not None and 0 < g(c, "delta") < 1),
              key=lambda kc: abs(g(kc[1], "delta") - 0.25), default=None)
    p25 = min(((k, c) for k, c in puts.items() if g(c, "delta") is not None and -1 < g(c, "delta") < 0),
              key=lambda kc: abs(g(kc[1], "delta") + 0.25), default=None)
    call25IV = ivg(c25[1]) if c25 else None
    put25IV = ivg(p25[1]) if p25 else None
    skew25 = (put25IV - call25IV) if (call25IV is not None and put25IV is not None) else None

    # put/call ratios over the full expiration
    def s(d, k):
        return sum(g(c, k) or 0 for c in d.values())
    coi, poi = s(calls, "open_interest"), s(puts, "open_interest")
    cvol, pvol = s(calls, "volume"), s(puts, "volume")
    pcr_oi = poi / coi if coi else None
    pcr_vol = pvol / cvol if cvol else None

    # max pain: settlement strike minimizing total payout to option holders. On a flat
    # payout plateau, min() returns the lowest strike of the tie — acceptable for a magnet level.
    def payout(K):
        cp = sum((g(c, "open_interest") or 0) * (K - k) for k, c in calls.items() if k < K)
        pp = sum((g(p, "open_interest") or 0) * (k - K) for k, p in puts.items() if k > K)
        return cp + pp
    max_pain = min(strikes, key=payout)

    def col(src, fld):
        return [g(src.get(st), fld) for st in win]

    # v1.82.0 liquidity: the payload carries two-sided quotes on every row (bid/ask/sizes) that were
    # previously dropped. Spread% = (ask−bid)/mid — the tradeability read ("this smile point exists"
    # vs "this contract is actually tradeable"). No quote / one-sided / crossed -> None (a hole, not 0%).
    def sprpct(side, k):
        b_, a_ = g(side.get(k), "bid"), g(side.get(k), "ask")
        if b_ is None or a_ is None or b_ <= 0 or a_ <= 0 or a_ < b_:
            return None
        mid = (a_ + b_) / 2
        return (a_ - b_) / mid * 100 if mid > 0 else None
    callSpr = [sprpct(calls, st) for st in win]
    putSpr = [sprpct(puts, st) for st in win]
    # OTM-leg spread (matches the smile's composite): put wing below ATM, call wing above; AT the
    # money take the tighter of the two quoted legs (the tradeable one).
    otmSpr = []
    for i, st in enumerate(win):
        if st < atm:
            otmSpr.append(putSpr[i])
        elif st > atm:
            otmSpr.append(callSpr[i])
        else:
            both = [v for v in (callSpr[i], putSpr[i]) if v is not None]
            otmSpr.append(min(both) if both else None)
    _sv = sorted(v for v in otmSpr if v is not None)
    medSpr = (_sv[len(_sv) // 2] if len(_sv) % 2 else (_sv[len(_sv) // 2 - 1] + _sv[len(_sv) // 2]) / 2) if _sv else None

    # v1.82.0 expected move: the 1σ straddle-implied band by this expiration — plain arithmetic on the
    # AV-native ATM IV (nothing re-solved). Degenerate inputs (no IV / no spot / 0 DTE) -> None, never 0.
    exp_move = (spot * atm_iv * math.sqrt(dte / 365.0)) if (spot is not None and atm_iv is not None and dte and dte > 0) else None

    # v1.83.0 smile-relative rich/cheap (the FMV endpoint is unentitled — user-chosen substitute
    # 2026-07-13): per strike, the OTM-composite IV vs a LOCAL smile fit (linear interpolation of the
    # nearest quoted OTM neighbours on each side, EXCLUDING self). Residual in vol points — a strike
    # whose IV pokes above the local smile is locally RICH, below is CHEAP. Pure IV, no greek recompute.
    # An unbracketed edge (no OTM quote further out) stays None — never extrapolate a wing to judge it.
    _cIV = [plausible_iv(v) for v in col(calls, "implied_volatility")]
    _pIV = [plausible_iv(v) for v in col(puts, "implied_volatility")]
    otm = [(_pIV[i] if k <= atm else _cIV[i]) for i, k in enumerate(win)]

    # v1.130 per-strike θ/vega — AV-native, previously fetched & DROPPED (only ATM was read, in
    # --term). Composite = the Spread column's OTM-leg rule (put wing below ATM, call above); AT the
    # money the MEAN of the quoted legs (θ/vega are near-equal across legs there — unlike spread,
    # where the tradeable minimum is the honest pick). A leg with no native value stays None.
    _cvga, _pvga = col(calls, "vega"), col(puts, "vega")
    _cthe, _pthe = col(calls, "theta"), col(puts, "theta")

    def _otm_pair(cv, pv, st):
        if st < atm:
            return pv
        if st > atm:
            return cv
        both = [v for v in (cv, pv) if v is not None]
        return (sum(both) / len(both)) if both else None
    otmVega = [_otm_pair(_cvga[i], _pvga[i], st) for i, st in enumerate(win)]
    otmTheta = [_otm_pair(_cthe[i], _pthe[i], st) for i, st in enumerate(win)]

    # v1.130 per-strike turnover: today's traded volume ÷ the standing book, both legs combined —
    # where FRESH flow concentrates vs legacy OI. Below _TURN_OI_FLOOR standing contracts the ratio
    # is statistically meaningless -> None (the strike still shows in the OI/volume reads); volume
    # missing on BOTH legs -> None (no volume data ≠ zero flow).
    _cvolL = [g(calls.get(st), "volume") for st in win]
    _pvolL = [g(puts.get(st), "volume") for st in win]
    _coiL = [g(calls.get(st), "open_interest") for st in win]
    _poiL = [g(puts.get(st), "open_interest") for st in win]
    turnover = []
    for i in range(len(win)):
        _toi = (_coiL[i] or 0) + (_poiL[i] or 0)
        if (_cvolL[i] is None and _pvolL[i] is None) or _toi < _TURN_OI_FLOOR:
            turnover.append(None)
        else:
            turnover.append(((_cvolL[i] or 0) + (_pvolL[i] or 0)) / _toi)

    def _rich_cheap(i):
        if otm[i] is None:
            return None
        li = next((j for j in range(i - 1, -1, -1) if otm[j] is not None), None)
        ri = next((j for j in range(i + 1, len(otm)) if otm[j] is not None), None)
        if li is None or ri is None:   # unbracketed edge -> no honest smile to compare against
            return None
        k0, k1, v0, v1 = win[li], win[ri], otm[li], otm[ri]
        fair = (v0 + v1) / 2 if k1 == k0 else v0 + (v1 - v0) * (win[i] - k0) / (k1 - k0)
        return round((otm[i] - fair) * 100, 2)   # vol points
    rich_cheap = [_rich_cheap(i) for i in range(len(win))]

    D = {
        "symbol": symbol, "expiration": chosen, "dte": dte, "asOf": (rows[0].get("date") if rows else None),
        # v1.112: the day's change% rides D so the live --full poll can re-derive the header change/cap
        # line next to the fresh spot (review P2 2026-07-15: rightSmall froze at first paint otherwise)
        "changePct": _change_from_quote(quote),
        "spot": spot, "spotEstimated": spot_est, "atmStrike": atm, "atmIV": atm_iv,
        "skew25": skew25, "call25IV": call25IV, "put25IV": put25IV,
        "call25Strike": c25[0] if c25 else None, "put25Strike": p25[0] if p25 else None,
        "pcrOI": pcr_oi, "pcrVol": pcr_vol, "callOItotal": coi, "putOItotal": poi,
        "maxPain": max_pain, "nContracts": len(rows), "nExpirations": len(exps),
        "expirations": exps, "window": window,
        "strikes": win,
        "callIV": _cIV, "putIV": _pIV,
        "callOI": _coiL, "putOI": _poiL,
        "callDelta": [g(calls.get(st), "delta") for st in win],
        "putDelta": [g(puts.get(st), "delta") for st in win],
        "callGamma": col(calls, "gamma"), "putGamma": col(puts, "gamma"),
        "callTheta": _cthe, "putTheta": _pthe,
        "callVega": _cvga, "putVega": _pvga,
        "otmVega": otmVega, "otmTheta": otmTheta,
        "callVol": _cvolL, "putVol": _pvolL,
        "turnover": turnover,
        # review P3: gate on any native volume EXISTING — an all-volume-missing chain must read
        # n/a, not a fabricated 0.00 (the per-strike rule's "no volume data ≠ zero flow", overall)
        "turnoverAll": ((cvol + pvol) / (coi + poi) if ((coi + poi) and any(
            g(c, "volume") is not None for m in (calls, puts) for c in m.values())) else None),
        "expMove": exp_move,
        "expMovePct": (exp_move / spot * 100 if (exp_move is not None and spot) else None),
        "callSprPct": callSpr, "putSprPct": putSpr, "otmSprPct": otmSpr, "medSprPct": medSpr,
        "richCheap": rich_cheap,
    }
    D["gex"] = _gex_ladder(D)
    # Dead-chain guard (plan A3): rows existed but carry NO usable IV anywhere (an index chain, an
    # unentitled key, a dead listing) -> fail LOUD. An all-n/a dashboard with exit 0 is worse than an
    # error, because it gets presented as real.
    if D["strikes"] and all(v is None for v in D["callIV"] + D["putIV"]):
        raise ChainError(f"error: no usable IV for {symbol} (index, unentitled key, or a dead chain) — nothing to analyze")
    return D


# ----------------- formatting -----------------
def iv(x, d=1):
    return "n/a" if x is None else f"{x*100:.{d}f}%"


def num(x, d=2):
    return "n/a" if x is None else f"{x:,.{d}f}"


def render_text(D):
    L = []
    spot = f"${num(D['spot'],2)}" + (" (est)" if D["spotEstimated"] else "")
    L.append(f"{D['symbol']} OPTIONS · exp {D['expiration']} ({D['dte']} DTE) · spot {spot} · as of {D['asOf']}")
    sk = D["skew25"]
    sktxt = "n/a" if sk is None else f"{sk*100:+.1f} pts (put {iv(D['put25IV'])} / call {iv(D['call25IV'])})"
    emtxt = ("n/a" if D.get("expMove") is None else
             f"±${num(D['expMove'],2)} (±{num(D['expMovePct'],1)}%)" + (" est" if D.get("spotEstimated") else ""))
    L.append(f"ATM IV {iv(D['atmIV'])} (K {num(D['atmStrike'],0)})  ·  exp move {emtxt}  ·  25Δ skew {sktxt}")
    spr = "n/a" if D.get("medSprPct") is None else f"{num(D['medSprPct'],1)}%"
    L.append(f"PCR(OI) {num(D['pcrOI'],2)}  ·  PCR(vol) {num(D['pcrVol'],2)}  ·  max pain {num(D['maxPain'],0)}  ·  call OI {num(D['callOItotal'],0)} / put OI {num(D['putOItotal'],0)}  ·  med OTM spread {spr}")
    # v1.83.0 smile-relative dislocations: count the strikes >1.5 vol pts off their local smile.
    # A payload WITHOUT the key (pre-v1.83 --from-dir) = not-computed, not a clean count (hole ≠ 0).
    if D.get("richCheap") is None:
        L.append("vs smile (rich/cheap vs local fit)  ·  not computed for this payload")
    else:
        _rcv = [v for v in D["richCheap"] if v is not None]
        _rich = sum(1 for v in _rcv if v > 1.5); _cheap = sum(1 for v in _rcv if v < -1.5)
        L.append(f"vs smile (rich/cheap vs local fit)  ·  {_rich} strikes >+1.5pt rich  ·  {_cheap} >1.5pt cheap"
                 + ("  (none notable)" if not (_rich or _cheap) else ""))
    # v1.130 flow lines (twin parity with the ΔOI/turnover panels + the PCR card sub); a chain with
    # NO volume data anywhere prints nothing here — never a fabricated "0.00 overall" (review P3)
    if D.get("turnover") is not None and (D.get("turnoverAll") is not None
                                          or any(v is not None for v in D["turnover"])):
        _hot = sum(1 for v in D["turnover"] if v is not None and v > 1.0)
        L.append(f"turnover (vol/OI) {num(D.get('turnoverAll'),2)} overall  ·  {_hot} strikes >1x "
                 f"(fresh flow > the standing book)")
    # v1.132: ΔOI over EACH available horizon (1d/1w/1m/1Q) — the text twin of the OI-panel window toggle
    _doiT = D.get("doi")
    if _doiT:
        def _sgn(x):
            return "n/a" if x is None else f"{x:+,.0f}"
        _wlabT = {w: lab for w, lab, _b, _m in DOI_WINDOWS}
        _segs = [f"{_wlabT[w]} vs {_doiT[w]['date']}: calls {_sgn(_doiT[w]['callNet'])} / puts {_sgn(_doiT[w]['putNet'])}"
                 for w in D.get("doiWindows", [])]
        L.append("ΔOI  ·  " + "  ·  ".join(_segs))
    else:
        L.append("ΔOI  ·  not computed for this payload (offline / throttled baseline / MARKET_DATA_DROI=0)")
    L.append("")
    L.append((f"±{D['window']} strikes around ATM" if D['window'] else "full tradeable ladder") + f" ({D['nContracts']} contracts this expiry, {D['nExpirations']} expirations total)")
    _ovgT = D.get("otmVega") or [None] * len(D["strikes"])   # pre-v1.130 D lacks them
    _othT = D.get("otmTheta") or [None] * len(D["strikes"])
    L.append(f"{'STRIKE':>8}{'CALL_IV':>9}{'CALL_OI':>9}{'CALL_Δ':>8}  |{'PUT_IV':>9}{'PUT_OI':>9}{'PUT_Δ':>8}"
             f"{'VEGA':>8}{'Θ/D':>8}")
    for i, st in enumerate(D["strikes"]):
        mark = "  <ATM" if st == D["atmStrike"] else ("  <pain" if st == D["maxPain"] else "")
        L.append(f"{num(st,0):>8}{iv(D['callIV'][i]):>9}{num(D['callOI'][i],0):>9}{num(D['callDelta'][i],2):>8}  |"
                 f"{iv(D['putIV'][i]):>9}{num(D['putOI'][i],0):>9}{num(D['putDelta'][i],2):>8}"
                 f"{num(_ovgT[i],2):>8}{num(_othT[i],2):>8}{mark}")
    L.append("")
    L.append("source: Alpha Vantage realtime options (native greeks) · data analysis, not investment advice")
    return "\n".join(L)


# ----------------- svg dashboard -----------------
def render_svg(D):
    W, H = 1320, 672
    s = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" font-family="-apple-system,Segoe UI,Helvetica,sans-serif" role="img">']
    s.append(f'<title>{esc(D["symbol"])} options dashboard</title>')
    s.append(f'<rect width="{W}" height="{H}" fill="#ffffff"/>')

    def t(x, y, txt, sz=12, c=INK, w=400, a="start"):
        s.append(f'<text x="{x}" y="{y}" font-size="{sz}" font-weight="{w}" fill="{c}" text-anchor="{a}">{esc(txt)}</text>')

    def card(x, y, w, h, title):
        s.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="#f6f7f9"/>')
        t(x + 16, y + 24, title, 13, INK, 700)

    # header
    t(30, 40, D["symbol"], 24, INK, 800)
    t(30 + 15 * len(D["symbol"]) + 12, 40, f'options · exp {D["expiration"]} · {D["dte"]} DTE', 14, GRA, 500)
    sp = f'${num(D["spot"],2)}'
    t(1290, 38, sp + (" est" if D["spotEstimated"] else ""), 20, INK, 800, "end")
    t(30, 60, f'Alpha Vantage realtime options · native greeks · as of {D["asOf"]}', 11, GRA)

    # metric strip
    sk = D["skew25"]
    mets = [("ATM IV", iv(D["atmIV"])), ("25Δ skew", "n/a" if sk is None else f'{sk*100:+.1f}pt'),
            ("PCR (OI)", num(D["pcrOI"], 2)), ("Max pain", num(D["maxPain"], 0)),
            ("ATM strike", num(D["atmStrike"], 0)), ("Spot", num(D["spot"], 0)),
            ("Contracts", str(D["nContracts"]))]
    cw = (W - 60) / len(mets)
    for i, (lab, val) in enumerate(mets):
        x = 30 + i * cw
        s.append(f'<rect x="{x:.0f}" y="74" width="{cw-10:.0f}" height="54" rx="8" fill="#f6f7f9"/>')
        t(x + 14, 97, lab, 11, GRA, 500); t(x + 14, 118, val, 17, INK, 800)

    strikes = D["strikes"]; n = len(strikes)

    # ---- row 1: volatility smile (left wide) | OI by strike (right) ----
    C1, CW1 = 30, 760
    card(C1, 142, CW1, 252, "Volatility smile (OTM IV vs strike)")
    px0, px1 = C1 + 48, C1 + CW1 - 16; py0, py1 = 178, 142 + 252 - 30
    # the smile uses the OUT-OF-THE-MONEY leg on each side (puts below ATM, calls above) —
    # the standard market smile; the ITM legs are illiquid and their IV is noise/stale.
    atmk = D["atmStrike"]
    putSmile = [D["putIV"][i] if strikes[i] <= atmk else None for i in range(n)]
    callSmile = [D["callIV"][i] if strikes[i] >= atmk else None for i in range(n)]
    t(px1 - 108, 166, "— put", 9, BLUE, 700); t(px1 - 58, 166, "— call", 9, TEAL, 700)
    ivs = [v for v in (putSmile + callSmile) if v is not None] or [0.1]
    vlo, vhi = min(ivs), max(ivs); vspan = (vhi - vlo) or 0.05
    vlo = max(0, vlo - vspan * 0.12); vhi += vspan * 0.12
    sx = lambda i: px0 + (px1 - px0) * i / (n - 1 if n > 1 else 1)
    sy = lambda v: py1 - (v - vlo) / (vhi - vlo) * (py1 - py0)
    for gy in (vlo, (vlo + vhi) / 2, vhi):
        yy = sy(gy)
        s.append(f'<line x1="{px0}" y1="{yy:.1f}" x2="{px1}" y2="{yy:.1f}" stroke="#eceef1"/>')
        t(px0 - 6, yy + 3, f'{gy*100:.0f}%', 9, GRA, 400, "end")
    # ATM marker
    ax = sx(strikes.index(D["atmStrike"])) if D["atmStrike"] in strikes else None
    if ax is not None:
        s.append(f'<line x1="{ax:.1f}" y1="{py0}" x2="{ax:.1f}" y2="{py1}" stroke="{GRA}" stroke-width="1" stroke-dasharray="3 3"/>')
        t(ax, py0 - 3, "ATM", 9, GRA, 700, "middle")

    def line(series, color):
        pts = [(sx(i), sy(v)) for i, v in enumerate(series) if v is not None]
        if len(pts) >= 2:
            s.append(f'<polyline points="{" ".join(f"{x:.1f},{y:.1f}" for x,y in pts)}" fill="none" stroke="{color}" stroke-width="2"/>')
        for x, y in pts:
            s.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="1.8" fill="{color}"/>')
    line(callSmile, TEAL); line(putSmile, BLUE)
    # strike axis ticks (first, atm, last)
    for i in sorted({0, strikes.index(D["atmStrike"]) if D["atmStrike"] in strikes else n // 2, n - 1}):
        t(sx(i), py1 + 15, num(strikes[i], 0), 9, GRA, 500, "middle")

    # OI by strike
    C2, CW2 = 810, 480
    card(C2, 142, CW2, 252, "Open interest by strike (call / put)")
    t(C2 + 250, 166, "■ call", 9, TEAL, 700); t(C2 + 300, 166, "■ put", 9, AMBER, 700)
    ox0, ox1 = C2 + 40, C2 + CW2 - 16; obase, otop = 142 + 252 - 30, 182
    oimax = max([v for v in (D["callOI"] + D["putOI"]) if v is not None] + [1])
    oslot = (ox1 - ox0) / n
    oy = lambda v: obase - (v / oimax) * (obase - otop)
    s.append(f'<line x1="{ox0}" y1="{obase}" x2="{ox1}" y2="{obase}" stroke="#dcdfe3"/>')
    for i in range(n):
        cx = ox0 + oslot * (i + 0.5)
        cv, pv = D["callOI"][i], D["putOI"][i]
        bw = oslot * 0.38
        if cv:
            s.append(f'<rect x="{cx-bw:.1f}" y="{oy(cv):.1f}" width="{bw:.1f}" height="{obase-oy(cv):.1f}" fill="{TEAL}" rx="1"/>')
        if pv:
            s.append(f'<rect x="{cx:.1f}" y="{oy(pv):.1f}" width="{bw:.1f}" height="{obase-oy(pv):.1f}" fill="{AMBER}" rx="1"/>')
    if D["maxPain"] in strikes:
        mpx = ox0 + oslot * (strikes.index(D["maxPain"]) + 0.5)
        s.append(f'<line x1="{mpx:.1f}" y1="{otop-6}" x2="{mpx:.1f}" y2="{obase}" stroke="{RED}" stroke-width="1.5" stroke-dasharray="3 2"/>')
        t(mpx, otop - 8, f'max pain {num(D["maxPain"],0)}', 9, RED, 700, "middle")
    for i in sorted({0, n // 2, n - 1}):
        t(ox0 + oslot * (i + 0.5), obase + 15, num(strikes[i], 0), 9, GRA, 500, "middle")

    # ---- row 2: strikes table (full width) ----
    card(30, 408, 1260, 246, "Strikes around ATM — IV / OI / delta")
    cols = [("STRIKE", 90), ("CALL IV", 90), ("CALL OI", 100), ("CALL Δ", 80),
            ("", 40), ("PUT IV", 90), ("PUT OI", 100), ("PUT Δ", 80)]
    tx = 50; ty = 434
    headx = [tx]
    for lab, wdt in cols:
        t(headx[-1], ty, lab, 9.5, GRA, 700); headx.append(headx[-1] + wdt)
    # show up to ~11 rows centered on ATM
    ai = strikes.index(D["atmStrike"]) if D["atmStrike"] in strikes else n // 2
    rlo = max(0, ai - 5); rhi = min(n, rlo + 11); rlo = max(0, rhi - 11)
    yy = ty + 18
    for i in range(rlo, rhi):
        st = strikes[i]
        is_atm = st == D["atmStrike"]; is_pain = st == D["maxPain"]
        if is_atm or is_pain:
            s.append(f'<rect x="42" y="{yy-12:.0f}" width="1236" height="17" rx="3" fill="{"#e7f4ee" if is_atm else "#fdeceb"}"/>')
        vals = [num(st, 0), iv(D["callIV"][i]), num(D["callOI"][i], 0), num(D["callDelta"][i], 2),
                ("ATM" if is_atm else "pain" if is_pain else ""),
                iv(D["putIV"][i]), num(D["putOI"][i], 0), num(D["putDelta"][i], 2)]
        for j, v in enumerate(vals):
            cc = INK if j == 0 else (TEAL if j == 4 and v == "ATM" else RED if j == 4 else "#3a3f45")
            t(headx[j], yy, v, 9.5, cc, 700 if (j == 0 or j == 4) else 400)
        yy += 19

    t(30, H - 10, "Alpha Vantage realtime options · native greeks · data analysis, not investment advice", 10, GRA)
    s.append("</svg>")
    return "\n".join(s)



def _gex_ladder(D):
    """Naive dealer GEX per strike: native Γ × (callOI − putOI), in Δ per 1-pt move (× 100 shares/contract
    for $). Γ is call≡put by parity, so read whichever leg AV populated (never recomputed — the AV-native
    rule). Honesty mirrors futures gex_ladder: netOI 0 -> 0.0 (a real zero); a strike with no native Γ ->
    None (unknown ≠ 0; the bar is dropped). Returns None when the chain carries no gamma at all (e.g. a
    chain built outside build() — the live reducer) so the caller can omit the panel."""
    cg, pg, co, po = D.get("callGamma"), D.get("putGamma"), D.get("callOI"), D.get("putOI")
    has_g = any(v is not None for v in (cg or [])) or any(v is not None for v in (pg or []))
    if not has_g or co is None or po is None:   # a REAL gamma value anywhere (a list of all-None -> no panel)
        return None
    out = []
    for i in range(len(D["strikes"])):
        net = (co[i] or 0) - (po[i] or 0)
        if net == 0:
            out.append(0.0)
            continue
        gm = cg[i] if (cg and cg[i] is not None) else (pg[i] if pg else None)
        out.append(gm * net if gm is not None else None)
    return out


def _underlying_lead(symbol, profile):
    """One About line naming the underlying ISSUER — so a page titled 'EA' also says what EA is:
    'Underlying: Electronic Arts Inc (EA) — Technology · Services-Prepackaged Software (NASDAQ).'
    Omits any missing part; None when there's no issuer name to show (keeps the generic basics alone)."""
    p = profile or {}
    name = p.get("name")
    if not name:
        return None
    bits = " · ".join(x for x in (p.get("sector"), p.get("industry")) if x)
    venue = (" (%s)" % p["exchange"]) if p.get("exchange") else ""
    return "Underlying: %s (%s)%s%s." % (name, symbol.upper(), (" — " + bits) if bits else "", venue)


def to_spec(D):
    """Convert the options analysis dict into a dashboard SPEC (docs/live-dashboard-spec.md):
    cards + an OTM-composite smile + OI bars + a naive-GEX bar chart (when the chain carries native Γ) +
    a chain table. IV decimals -> percent; the smile masks OTM-only (put IV for K<=ATM, call IV for
    K>=ATM) — the two legs MEET at the ATM strike (parity with futures_options), no gap beside it."""
    strikes = D["strikes"]
    atm = D["atmStrike"]

    def pct(x):
        return None if x is None else round(x * 100, 2)
    putIV = [pct(v) if strikes[i] <= atm else None for i, v in enumerate(D["putIV"])]
    callIV = [pct(v) if strikes[i] >= atm else None for i, v in enumerate(D["callIV"])]
    spot = f"${num(D['spot'], 2)}" + (" est" if D["spotEstimated"] else "")
    skew = "n/a" if D["skew25"] is None else f"{D['skew25'] * 100:+.1f}"
    # PER-EXPIRATION tiles carry scope="expiry" + the series they refer to (accent + caption; they swap with
    # the Chain switcher). Spot is GLOBAL. `_x` stamps the scope onto a card.
    _xlab = f"{D['expiration']} · {D['dte']} DTE"
    # v1.130: PCR(vol) surfaces on the card (computed since v1.82, text-only until now) + a divergence
    # word when today's flow leans differently than the standing book (thresholds ±25%/20%).
    _pcr_sub = None
    if D.get("pcrVol") is not None:
        _dvg = ""
        if D.get("pcrOI"):
            _r = D["pcrVol"] / D["pcrOI"]
            _dvg = (" · flow puts-heavier than the book" if _r >= 1.25 else
                    (" · flow calls-heavier than the book" if _r <= 0.8 else ""))
        _pcr_sub = [f"vol {num(D['pcrVol'], 2)}{_dvg}"]

    def _x(card):
        return dict(card, scope="expiry", scopeLabel=_xlab)
    cards = [
        {"label": "Spot", "value": spot,
         "hint": "The underlying share/ETF price the chain prices off (Alpha Vantage GLOBAL_QUOTE at "
                 "fetch time — delayed on the standard entitlement). Global to the underlying."},
        _x({"label": "ATM IV", "value": iv(D["atmIV"]),
            "hint": "Implied volatility at the at-the-money strike, annualized. AV-native (Alpha Vantage "
                    "computes IV/greeks on this path — the skill reads them, never recomputes)."}),
        _x({"label": "Exp move", "value": ("n/a" if D.get("expMove") is None else
                                           f"±${num(D['expMove'], 2)} (±{num(D['expMovePct'], 1)}%)"
                                           + (" est" if D.get("spotEstimated") else "")),
            "hint": "The 1σ move the market prices in by this expiration: spot × ATM IV × √(DTE/365) — "
                    "plain arithmetic on the AV-native ATM IV (nothing re-solved). ~68% of outcomes land "
                    "inside the band if the IV is 'right'; a straddle breaks even near its edges. "
                    "'est' = the spot itself is estimated (no quote), so the band inherits that."}),
        _x({"label": "25Δ skew", "value": skew,
            "hint": "IV(~25Δ put) − IV(~25Δ call), vol points, from AV-native deltas. Positive = downside "
                    "protection bid (puts richer) — the usual equity-index shape."}),
        _x(dict({"label": "PCR (OI)", "value": num(D["pcrOI"], 2),
                 "hint": "Put/call ratio by open interest over this expiration. >1 = more puts outstanding; "
                         "hedging demand skews it — positioning, not a direction call. The sub line is "
                         "PCR by today's VOLUME — when the vol/OI ratio leaves 0.8–1.25 (±25% in ratio "
                         "terms), TODAY'S flow leans differently than the standing book (fresh fear vs "
                         "old hedges); intraday-cumulative, so early prints are noisy."},
                **({"sub": _pcr_sub} if _pcr_sub else {}))),
        _x({"label": "Max pain", "value": num(D["maxPain"], 0),
            "hint": "The strike minimizing option writers' total intrinsic payout at expiry. A pin-gravity "
                    "heuristic near expiry, not a forecast."}),
        _x({"label": "DTE", "value": str(D["dte"]),
            "hint": "Calendar days to this expiration. Greeks/decay quoted per calendar day."}),
    ]
    _osp = D.get("otmSprPct") or [None] * len(strikes)   # older saved D (pre-v1.82 --from-dir) lacks it
    _rc = D.get("richCheap") or [None] * len(strikes)     # pre-v1.83 D lacks it
    _ovg = D.get("otmVega") or [None] * len(strikes)      # pre-v1.130 D lacks these three
    _oth = D.get("otmTheta") or [None] * len(strikes)
    _tov = D.get("turnover") or [None] * len(strikes)

    def _sfmt(v):
        return "n/a" if v is None else f"{num(v, 1)}%"

    def _rcfmt(v):
        return "n/a" if v is None else (f"+{num(v, 1)}" if v > 0 else num(v, 1))
    rows = [[num(st, 0), iv(D["callIV"][i]), num(D["callOI"][i], 0), num(D["callDelta"][i], 2),
             iv(D["putIV"][i]), num(D["putOI"][i], 0), num(D["putDelta"][i], 2), _sfmt(_osp[i]), _rcfmt(_rc[i]),
             num(_ovg[i], 2), num(_oth[i], 2)]
            for i, st in enumerate(strikes)]
    ai = strikes.index(atm) if atm in strikes else None

    def _rowstyle(i):
        # ATM-centered heat (parity with futures_options): strong dark-blue at the money (bold),
        # fading across the ±10 band, plain wings — readable in both themes.
        if ai is None or abs(i - ai) > 10:
            return ""
        if i == ai:
            return "background:rgba(74,134,240,0.60);font-weight:600"
        a = 0.46 * (1 - abs(i - ai) / 10)
        return f"background:rgba(74,134,240,{a:.3f})" if a >= 0.001 else ""

    rowstyle = [_rowstyle(i) for i in range(len(strikes))]
    # v1.131 (user decision off the v1.130 lookalike catch): ΔOI is MERGED into the OI panel as the
    # stacked "book with caps" — per side a BASE segment (what carried over: min(today, baseline)), a
    # green BUILD cap on top (growth over the window) and a translucent red GHOST up to the baseline
    # level (what was unwound). ONE scale (contracts): today's OI = base+cap top, baseline's = ghost
    # top, and the segment sizes ARE the ΔOI — the delta reads in context of the wall it changed.
    # v1.132 (user decision): the horizon is SELECTABLE (1d/1w/1m/1Q) via the engine's per-panel
    # `variants` vtoggle — each window is a precomputed data view; the switcher (expiration) and this
    # window toggle are orthogonal (the baseline DATE is one per symbol, so it rides the title and
    # changes only on a window swap, not an expiration swap). No baseline -> the plain 2-series chart.
    _GHOST = "rgba(227,73,72,0.35)"                # translucent = the secondary cue beyond hue
    _doi = D.get("doi")

    def _oi_seg(cur, dlt):
        base, build, ghost = [], [], []
        for c0, dl in zip(cur, dlt):
            if c0 is None:                          # no bar at all (matches the plain panel's 0)
                base.append(0); build.append(0); ghost.append(0)
            elif dl is None:                        # series listed, delta unknown -> a plain bar
                base.append(c0); build.append(0); ghost.append(0)
            else:
                y = c0 - dl
                base.append(min(c0, y)); build.append(max(0.0, dl)); ghost.append(max(0.0, -dl))
        return base, build, ghost

    def _oi_stack_series(dc, dp, lab):
        _cb, _cbl, _cgh = _oi_seg(D["callOI"], dc)
        _pb, _pbl, _pgh = _oi_seg(D["putOI"], dp)
        return [
            {"name": "Call OI", "color": "#2a78d6", "stack": "c", "values": _cb},
            {"name": "+ built (%s)" % lab, "color": "#1baf7a", "stack": "c", "values": _cbl},
            {"name": "− unwound (%s)" % lab, "color": _GHOST, "stack": "c", "values": _cgh},
            {"name": "Put OI", "color": "#eb6834", "stack": "p", "values": _pb},
            {"name": "+ built (%s)" % lab, "color": "#1baf7a", "stack": "p", "values": _pbl, "legendHide": True},
            {"name": "− unwound (%s)" % lab, "color": _GHOST, "stack": "p", "values": _pgh, "legendHide": True}]

    def _oi_title(date, lab):
        return "Open interest by strike · Δ vs %s (%s)" % (date, lab)

    _win_menu = " / ".join(D.get("doiWindows") or [])   # only the horizons that actually resolved (non-empty when doi exists)

    def _oi_info(date, lab):
        return (
            "Contracts OUTSTANDING per strike and side (call blue / put orange) with the change over "
            "the selected horizon (%s) overlaid: the green cap = contracts BUILT since %s, the "
            "translucent red ghost = contracts UNWOUND (its top edge marks the baseline level). "
            "Toggle the horizon (%s) top-right — 1w = 5 trading days, 1m/1Q = a calendar month/quarter "
            "ago snapped to a trading day (an unpublished/holiday date walks one business day further "
            "and THIS date is the honest baseline). One scale — the segment sizes ARE the ΔOI; hover "
            "for exact numbers; one immutable HISTORICAL_OPTIONS pull per window, disk-cached forever. "
            "OI publishes once daily (a stable intraday baseline, not a live tick). A strike — or a "
            "WHOLE expiration — absent at the baseline counts from 0 and reads all-green (it did not "
            "exist then, so all of its OI is genuinely new; common on short-dated weeklies under 1m/1Q). "
            "A strike unwound to ZERO leaves the tradeable ladder entirely (it disappears, no ghost). "
            "Positioning, not volume; a wall being BUILT means something different from one being "
            "unwound; red dashes = max pain." % (lab, date, _win_menu))

    _oi_base = {"labels": strikes, "markers": {"maxPain": D["maxPain"]}}
    if _doi:
        _wlabel = {w: lab for w, lab, _b, _m in DOI_WINDOWS}
        _opts = []
        for wkey in D["doiWindows"]:
            _w = _doi[wkey]
            _lab, _dt = _wlabel[wkey], _w["date"]
            _opts.append({"name": _lab, "title": _oi_title(_dt, _lab), "info": _oi_info(_dt, _lab),
                          "data": dict(_oi_base, series=_oi_stack_series(_w["dOICall"], _w["dOIPut"], _lab))})
        _oi_panel = {"type": "bars", "title": _opts[0]["title"], "info": _opts[0]["info"],
                     "full": True,   # v1.133 (user): the dense stacked book-with-caps reads better full-width
                     "data": _opts[0]["data"],
                     "variants": {"label": "Δ", "index": 0, "options": _opts}}
    else:
        _oi_panel = {"type": "bars", "title": "Open interest by strike", "full": True, "data": dict(
            _oi_base, series=[
                {"name": "Call OI", "color": "#2a78d6", "values": [x or 0 for x in D["callOI"]]},
                {"name": "Put OI", "color": "#eb6834", "values": [x or 0 for x in D["putOI"]]}]),
            "info": (
                "Contracts OUTSTANDING per strike and side within the shown window — positioning, not "
                "volume. Walls mark hedging concentration and pin candidates into expiry; red dashes = "
                "max pain. The Δ overlay (build/unwind vs a prior session) is not computed for this "
                "payload (offline --from-dir, a throttled baseline pull, or MARKET_DATA_DROI=0) — "
                "re-render online to fill it.")}
    panels = [
        {"type": "smile", "title": "Vol smile (OTM composite)", "full": True, "info":   # full width — the headline chart
            "Implied vol per strike from Alpha Vantage's own per-contract IVs (read as-is, never "
            "recomputed): the OTM composite — put IV below ATM, call IV above (the liquid legs). "
            "Holes are honest: a strike with no usable IV is left empty, never smoothed or "
            "extrapolated. Red dashes = max pain.", "data": {
            "strikes": strikes, "atm": atm, "putIV": putIV, "callIV": callIV,
            "markers": {"maxPain": D["maxPain"]}}},
        _oi_panel,
    ]
    gex = D.get("gex") if D.get("gex") is not None else _gex_ladder(D)   # build() precomputes; else derive if Γ present
    # ALWAYS emit the GEX panel (all-None when the chain carries no native Γ — the bars renderer drops empty bars,
    # honest "no Γ" ≠ 0) so EVERY series' Chain-tab panel set is structurally identical: combined_spec zips panels
    # by INDEX, and a series missing this panel would misalign the switcher / IndexError the --full page (review P2#5).
    panels.append(
        {"type": "bars", "title": "Dealer gamma exposure (GEX) by strike", "allowEmpty": True, "info":
            "Alpha-Vantage-native Γ × net OI (call − put) per strike (× 100 shares/contract for $ per "
            "1-pt move). Sign = the naive dealer assumption (long calls / short puts — real positioning "
            "isn't observable from OI). Γ is AV-native, read as-is (never recomputed); a strike with no "
            "native Γ is dropped (n/a ≠ 0). No Γ-flip line here — it needs a spot sweep we do not "
            "recompute on the AV-native path.", "data": {
            "labels": strikes, "series": [
                {"name": "GEX (Γ×netOI)", "values": (gex if gex is not None else [None] * len(strikes)),
                 "colorBySign": True}]}})
    # v1.130 turnover: fresh flow vs the legacy book — separates live levels from memorial walls.
    panels.append(
        {"type": "bars", "title": "Turnover (vol / OI) by strike", "allowEmpty": True, "info":
            "Today's traded volume ÷ standing open interest, both legs combined — where FRESH flow "
            "concentrates vs the legacy book. >1× = today alone traded more than the entire standing "
            "OI at that strike (new positioning or a roll); near 0 at a big wall = a stale, 'memorial' "
            "level. Strikes with a standing book under %d contracts are left empty (a 3-lot ratio is "
            "noise, not flow); volume accumulates through the session, so early prints run low." % _TURN_OI_FLOOR,
            "data": {
            "labels": strikes, "series": [
                {"name": "vol/OI", "values": [None if v is None else round(v, 2) for v in _tov]}]}})
    panels.append(
        {"type": "table", "title": (f"Chain (±{D['window']} strikes)" if D['window'] else "Chain (full tradeable ladder)"), "info":
            "Both legs per strike, AV-native IV/Δ per contract (the skill reads them, never "
            "recomputes). 'n/a' = the feed carried no usable value — left honest, never filled. "
            "Spread = the OTM leg's bid/ask width as % of mid (put wing below ATM, call above; "
            "tighter leg at the money) — the tradeability read; 'n/a' = no two-sided quote. "
            "'vs smile' = the OTM-leg IV minus a local smile fit (vol pts): + = locally RICH (IV pokes "
            "above its neighbours), − = CHEAP; relative to THIS chain's own smile, not a model — no AV "
            "FMV entitlement. Vega/Θ are the OTM leg's AV-native greeks (same wing rule as Spread). "
            "Blue heat = proximity to ATM; bold = the ATM row.", "data": {
            "columns": ["Strike", "Call IV", "Call OI", "Call Δ", "Put IV", "Put OI", "Put Δ", "Spread",
                        "vs smile", "Vega", "Θ/day"],
            "colhints": {
                "Vega": "AV-native vega of the OTM leg (put wing below ATM, call above; mean of the two "
                        "at the money) — $ change per share for a 1-vol-pt IV move; ×100 per contract. "
                        "Where the vol exposure sits along the ladder.",
                "Θ/day": "AV-native theta of the OTM leg — $ decay per share per calendar day (×100 per "
                         "contract), normally negative. What holding this strike costs in time."},
            "rows": rows,
            "rowstyle": rowstyle}})
    return {
        "about": {"text": (
            (_ul + "\n\n" if (_ul := _underlying_lead(D["symbol"], D.get("profile"))) else "")
            + "US-listed equity/ETF options: American-style, physically settled, standard "
            "multiplier 100 shares per contract (adjusted/non-standard contracts can differ). "
            "Chain, IV and greeks are Alpha Vantage REALTIME_OPTIONS values, read as-is — "
            "verify contract terms with the OCC."), "cue": "reference basics · verify with the OCC"},
        "title": f"{D['symbol']} options · {D['expiration']} · {D['dte']} DTE",
        "subtitle": f"Alpha Vantage realtime · as of {D['asOf']} · not advice",
        "cards": cards,
        "panels": panels,
    }


def render_html(D):
    """Equity options dashboard = a spec fed to the central dashboard.py renderer (Ф3)."""
    return dashboard.render_interactive_html(to_spec(D))


# ----------------- live (--watch): the centralized dashboard engine (v1.45 unify) -----------------
def make_provider(symbol, key=None, expiry=None, window=None, interval=10.0, produce=None):
    """Dashboard provider (DD-3) for the equity chain: AVSession-polled snapshots -> spec.
    Badge mode is 'delayed' — AV realtime options are POLLED snapshots, never tick-live; the
    green bar shows the poll age instead of claiming LIVE. `produce` is injectable for tests.
    Fatality is decided HERE (v1.125): a ChainError before the FIRST successful poll (a bad
    key, throttle, or a user-pinned expiration that is permanently unlisted) is fatal=True —
    dashboard.serve keeps the error on the badge and STOPS polling the metered key (pre-fix
    an unlisted --expiry re-polled 2 AV calls every 2s forever). AFTER a validated poll, an
    empty-chain ChainError stays non-fatal — AV 200-OKs an empty chain under load-shed (the
    PEG lesson) and the next cycle recovers."""
    sym = symbol.upper()
    if produce is None:
        session = AVSession(key)
        state = {"expiry": expiry, "ok": False}   # after the discovery poll, PIN the chosen expiration:

        def produce():              # every re-poll then asks the API for ONLY this series
            try:
                D = gather(sym, key, state["expiry"], window, fetch=session.get)
            except ChainError as e:
                if not state["ok"]:
                    e.fatal = True   # never-validated pin/key: stop the metered polling
                raise
            state["ok"] = True
            state["expiry"] = D["expiration"]
            # v1.130 ΔOI: memo + disk cache make this free after the first poll; OI updates once a
            # day, so the baseline is stable for the whole session (a midnight asOf roll re-anchors).
            _attach_droi(D, fetch_oi_baselines(sym, D.get("asOf"), key, fetch=session.get))
            return to_spec(D)
    # Badge styled like the futures LIVE bar (user decision 2026-07-04): AV has no tick socket,
    # but POLL_DEFAULT_S-second polls are close enough that DELAYED would mislead more than LIVE — the "Ns ago"
    # age keeps it honest, and the subtitle carries the data's as-of date.
    return dashboard.CallableProvider(produce, interval=interval, mode="tick",
                                      detail=f"{sym} · AV realtime options · polled")


def make_full_provider(symbol, key=None, expiry=None, window=None, exchange=None, interval=10.0, produce=None,
                       backfill=False, years=2.0, profile=None):
    """Live provider for `--full` served: builds the WHOLE combined page ONCE (all-expirations switcher +
    Term + the settlement store), then each poll re-fetches ONLY the PINNED Chain series (expiration=
    pushdown — cheap) and splices its fresh chain into the cached page's Chain section (cards by label so
    the permanent cross-series trio survives; panels by index). produce() returns the _with_active-flattened
    page (active=Chain), so dashboard.diff_spec sees only the Chain change; the client gates deltas to the
    Chain tab (spec.liveSection). The exposed `set_pin(expiry)` re-pins live to whichever series the client
    selects — the switcher stays. `produce` is injectable for tests.

    `backfill=True` + a COLD name (empty store): the first paint renders the Overview/History in the
    'loading' state (blinking dots) and a background thread runs the HISTORICAL_OPTIONS backfill; when it
    finishes it rebuilds the warm Overview + History and pushes them via `set_push` (a `sections` SSE event)
    so they fill in live — no reload, no waiting. Warm names / `backfill=False` are unchanged."""
    sym = symbol.upper()
    st = {"base": None, "pin": expiry, "push": None, "bf": False, "contracts": None, "quote": None}
    if produce is None:
        import opthist
        import threading
        session = AVSession(key)

        def _start_backfill():
            """Kick off the vol-history backfill in a daemon thread (once). On completion, swap the warm
            Overview/History into st['base'] AND push them to any connected clients. Best-effort + honest: a
            failed/throttled/unentitled backfill falls back to the passive 'run --history' note (never fakes
            data, never spins the loading dots forever)."""
            if st["bf"] or not (backfill and key):
                return
            st["bf"] = True

            def _run():
                # ALWAYS resolve the 'loading' state (warm on success, else the honest passive 'run --history'
                # note) — a failed/throttled backfill must NEVER spin blinking dots forever.
                try:
                    dates = opthist.history_dates(dt.date.today(), years=years, daily=15)
                    try:
                        hist = opthist.update_store(sym, dates, key,
                                                    fetch_closes=opthist.fetch_daily_closes_raw)
                    except opthist._Throttled as thr:
                        hist = thr.partial               # partial is saved + still useful — show what we got
                    earnings = opthist.fetch_earnings(sym, key) if hist else None
                    closes = opthist.fetch_daily_closes(sym, key) if hist else None   # v1.83.0 VRP
                    rebuilt = build_full(sym, st["contracts"], st["quote"], (hist or None), earnings,
                                         st["pin"], window, exchange, live=True, loading=False, profile=profile, closes=closes)
                    # loading=False + empty hist -> the cold STATIC page (live synthesis + passive 'run --history'
                    # note, History 'warming'); non-empty hist -> the warm Overview + History. Either way the
                    # blinking-dots 'loading' state is replaced.
                except (Exception, SystemExit) as e:   # HISTORICAL_OPTIONS entitlement-sample sys.exits (BaseException); dead feed
                    print("  --full backfill for %s did not complete (%s) — Overview/History fall back to "
                          "the 'run --history' note" % (sym, str(e)[:80] or type(e).__name__), file=sys.stderr)
                    try:
                        rebuilt = build_full(sym, st["contracts"], st["quote"], None, None,
                                             st["pin"], window, exchange, live=True, loading=False, profile=profile)
                    except Exception:
                        return                           # can't even build the fallback -> leave loading (last resort)
                bysid = {s["id"]: s for s in rebuilt["sections"]}
                for sid in ("overview", "history"):
                    if sid in bysid:
                        for i, s in enumerate(st["base"]["sections"]):
                            if s["id"] == sid:
                                st["base"]["sections"][i] = bysid[sid]   # fresh reload/poll reflects it too
                if st["push"] and "overview" in bysid and "history" in bysid:
                    st["push"]([bysid["overview"], bysid["history"]])    # stream them in (no reload)
            threading.Thread(target=_run, daemon=True).start()

        def produce():
            if st["base"] is None:                     # first paint: build the whole page once (heavy pull)
                try:
                    contracts, quote = _term_fetch(sym, key)   # av() sys.exits on a throttle — that BaseException
                except SystemExit as e:                        # slips past the poll thread's `except Exception` and
                    raise ChainError(str(e) or "Alpha Vantage throttled the first --full --watch pull")  # kills it silently -> page hangs at "loading…". Convert to a catchable ChainError.
                hist = opthist.load_store(sym)
                earnings = opthist.fetch_earnings(sym, key) if hist else None
                closes = opthist.fetch_daily_closes(sym, key) if hist else None   # v1.83.0 VRP
                st["contracts"], st["quote"] = contracts, quote   # kept for the background warm rebuild
                _prev = fetch_oi_baselines(sym, _asof_of(contracts), key, fetch=session.get)   # v1.132 ΔOI windows
                st["base"] = build_full(sym, contracts, quote, hist, earnings, st["pin"], window, exchange,
                                        live=True, loading=(backfill and not hist), profile=profile, closes=closes,
                                        prev_oi=_prev)
                st["pin"] = build(sym, contracts, quote, st["pin"], window)["expiration"]   # PIN the resolved series
                opthist.accrete(sym)
                if backfill and not hist:
                    _start_backfill()   # cold + served: load the vol history NOW (don't wait) -> fills in via SSE
                # ALIAS-FREE: return a deep copy so the SSE loop's cached `last` spec doesn't share mutable
                # cards/panels/head objects with st["base"] — otherwise the next poll mutates st["base"] IN PLACE,
                # `last` mutates with it, and diff_spec compares each object with ITSELF -> the page freezes at
                # first paint forever while the LIVE dot keeps ticking (agent-review P0 2026-07-08).
                return json.loads(json.dumps(dashboard._with_active(st["base"])))
            D = gather(sym, key, st["pin"], window, fetch=session.get)   # re-poll ONLY the pinned series
            st["pin"] = D["expiration"]
            # v1.130/v1.132: the poll splices fresh panels BY INDEX below — an unattached D would blank
            # the OI·Δ overlay every cycle. Memoized: free after the first poll (all windows).
            _attach_droi(D, fetch_oi_baselines(sym, D.get("asOf"), key, fetch=session.get))
            sec = next(s for s in st["base"]["sections"] if s["id"] == "smile")
            sw = sec.get("switcher") or {}
            if st["pin"] in (sw.get("expiries") or []):                # the switcher index follows the live re-pin,
                _ix = sw["expiries"].index(st["pin"])                  # so a snapshot/tab round-trip doesn't revert
                sw["index"] = _ix                                      # the dropdown + cards to the build-time target
            else:
                _ix = None
            if st["base"].get("head") and D.get("spot") is not None:   # keep the header spot LIVE (diff_spec carries head)
                # v1.113: keep the ' est' marker — the poll path has no build_term gate, so a malformed
                # mid-session quote CAN estimate the spot; nothing else on --full would disclose it
                _est = " est" if D.get("spotEstimated") else ""
                st["base"]["head"]["rightBig"] = "$" + num(D["spot"], 2) + _est
                # v1.112: the change/cap line ticks WITH the spot — same _right_small rule as build_full
                # (review P2 2026-07-15: rightSmall froze at first paint for the whole --watch session)
                st["base"]["head"]["rightSmall"] = _right_small(D.get("changePct"), D["spot"],
                                                                (profile or {}).get("sharesOutstanding"),
                                                                est=D.get("spotEstimated"))
            fresh = to_spec(D)
            fby = {c["label"]: c for c in fresh["cards"]}              # refresh matching cards (Spot / ATM IV / …),
            for c in sec["cards"]:                                     # KEEP the trio (Series/Front ATM IV/Term slope)
                fc = fby.get(c["label"])
                if fc:
                    c["value"] = fc["value"]
                    c["sub"] = fc.get("sub")   # v1.130 review P2: the PCR(vol) sub must tick with the
                    if not c["sub"]:           # value, not freeze at first paint (the v1.112 class)
                        c.pop("sub", None)
                    if fc.get("scopeLabel"):
                        c["scopeLabel"] = fc["scopeLabel"]
            if _ix is not None and isinstance(sec.get("cardSeries"), list) and _ix < len(sec["cardSeries"]):
                sec["cardSeries"][_ix] = [dict(c) for c in sec["cards"]]   # keep the pinned series' card snapshot fresh
            for i, p in enumerate(fresh["panels"]):                   # refresh the shown chain panels by index
                if i < len(sec["panels"]):
                    _dst = sec["panels"][i]
                    # v1.131 review P3: also refresh title + info — the OI·Δ panel's title carries the
                    # baseline date, so a mid-session baseline loss (midnight roll + a throttled prior
                    # pull) would otherwise leave a stale caption over a degraded chart. Cheap, honest.
                    _dst["title"] = p.get("title", _dst.get("title"))
                    _dst["info"] = p.get("info", _dst.get("info"))
                    _ser = _dst.get("series")                         # …and its per-series slot for the pinned index
                    if _dst.get("seriesIsVariants") and p.get("variants"):
                        # v1.132: refresh the WHOLE window options-set for the pinned expiration; if the
                        # panel degrades (baseline lost -> plain 2-series, no variants), drop variants so
                        # the client rebuilds a plain chart under the refreshed (Δ-less) title.
                        _opts = p["variants"]["options"]
                        if _ix is not None and isinstance(_ser, list) and _ix < len(_ser):
                            _ser[_ix] = _opts
                        _k = min((_dst.get("variants") or {}).get("index", 0), len(_opts) - 1)
                        _dst["variants"] = {"label": p["variants"].get("label", "Δ"), "index": _k, "options": _opts}
                        _dst["data"] = _opts[_k]["data"]
                    elif _dst.get("seriesIsVariants"):
                        # degraded: plain panel. Drop `series` TOO (review P2): it still held per-expiration
                        # window OPTIONS-LISTS; leaving it would feed switchSeries a list, not a data object,
                        # and blank the OI chart on the next expiration switch until the following poll heals.
                        _dst.pop("variants", None); _dst.pop("seriesIsVariants", None); _dst.pop("series", None)
                        _dst["data"] = p["data"]
                    else:
                        _dst["data"] = p["data"]
                        if _ix is not None and isinstance(_ser, list) and _ix < len(_ser):
                            _ser[_ix] = p["data"]
            return json.loads(json.dumps(dashboard._with_active(st["base"])))
    prov = dashboard.CallableProvider(produce, interval=interval, mode="tick",
                                      detail=f"{sym} · AV realtime · Chain tab live")

    def set_pin(exp):
        st["pin"] = exp        # the next poll re-polls this series — live follows the client's expiration pick
    prov.set_pin = set_pin

    def set_push(cb):
        st["push"] = cb        # serve() hands us a callback to stream `sections` updates (the finished backfill)
    prov.set_push = set_push
    prov.no_livebar = True     # sectioned page: never show the page-wide green bar (not even on the loading paint)
    return prov


def _fund_links(html_path, sym, bg=False, _spawn=None, embed=False, fundamentals_fname=None):
    """v1.87 cross-links: a header chip to the FUNDAMENTALS sibling (<sym>.html) when it exists
    next to this output file (dashboard.sibling_links: existence-gated, relative href — the one
    navigation the embedded pane allows). v1.94: with `bg=True` (a LIVE render — never --from-dir)
    a MISSING sibling is rendered in the BACKGROUND (detached fundamentals.py) and the chip bakes
    `pending` — hidden until the page's HEAD-poll sees the file land; a failed spawn bakes
    nothing (a chip nothing will reveal is a dead chip). MARKET_DATA_NO_SIBLING (set in every
    spawned child) stops sibling chains. Returns (links, sibling): v1.127 — with `embed=True`
    (the --full page) the Cowork inline child also emits its SPEC and `sibling` = {label,
    backLabel, fname, spec} for EMBEDDING: the viewer cannot navigate between files (QCOM/MA
    sessions), so the chip becomes a client-side board swap. Failed spec read -> plain link."""
    fname, sib = dashboard.sibling_target(
        html_path,
        fundamentals_fname,
        sym.lower() + ".html",
        sym.lower() + "_fundamentals.html",
    )
    label = f"{sym} fundamentals ↗"
    links = dashboard.sibling_links(html_path, [(fname, label)])
    if not bg or os.environ.get("MARKET_DATA_NO_SIBLING"):
        return links, None
    cw = dashboard.in_cowork()
    if links and not (cw and embed):
        return links, None   # desktop: a real link; Cowork non-embed pages keep the old contract
    here = os.path.dirname(os.path.abspath(__file__))
    parent_fname = dashboard.sibling_basename(os.path.basename(html_path), sym.lower() + "_options.html")
    argv = [sys.executable, os.path.join(here, "fundamentals.py"), sym, "--html", sib,
            "--options-link", parent_fname]
    if cw:
        # v1.123.2 — Cowork: spawn_bg's detached child AND the page's HEAD-poll are both dead, so a
        # pending chip would NEVER reveal. Render the sibling INLINE (synchronous); v1.127: on the
        # --full page capture its spec too and EMBED it (the chip swaps boards in place). Review P1:
        # with embed this runs even when the sibling FILE exists — the old existing-file shortcut
        # returned a DEAD chip there and a re-render DESTROYED the embedded pair.
        emit = (os.path.join(os.path.dirname(sib), ".%s.%d.spec.json" % (fname[:-5], os.getpid()))
                if embed else None)
        ok_child = (_spawn or dashboard.render_sync)(argv + (["--emit-spec", emit] if emit else []))
        sspec = None
        if ok_child and emit:
            try:
                with open(emit, encoding="utf-8") as f:
                    sspec = json.load(f)
            except (OSError, ValueError):
                sspec = None
        if emit:
            try:
                os.unlink(emit)   # own try (review P3): a failed unlink must not discard the spec
            except OSError:
                pass
        if sspec is not None:
            return [], {"label": f"{sym} fundamentals", "backLabel": f"{sym} options",
                        "fname": fname, "spec": sspec}
        if ok_child:
            return dashboard.sibling_links(html_path, [(fname, label)]) or [], None
        return [], None
    spawn = _spawn or dashboard.spawn_bg
    if spawn(argv):
        print(f"note: rendering {sib} in the background — the '{label}' chip appears on the page when it lands",
              file=sys.stderr)
        return [{"text": label, "href": fname, "pending": True}], None
    return [], None


def main(argv=None):
    try:
        return _main(argv)
    except ChainError as e:        # one-shot runs keep the loud CLI contract (exit 1 + message)
        sys.exit(str(e))


def _main(argv=None):
    ap = argparse.ArgumentParser(description="Analyze an equity/ETF option chain (Alpha Vantage)")
    ap.add_argument("symbol", nargs="?")   # optional: --prewarm operates on the universe, not one symbol
    ap.add_argument("--expiry", type=_arg_date, help="expiration YYYY-MM-DD (default: nearest with DTE>=1)")
    ap.add_argument("--term", action="store_true",
                    help="append CROSS-expiration analytics to the chain page: ATM-IV/skew term curve + "
                         "the moneyness×expiry IV surface + a per-series table — from the same single "
                         "REALTIME_OPTIONS payload (zero extra calls). Combines with --expiry/--window "
                         "(they shape the base chain); one-shot, --watch does not apply")
    ap.add_argument("--window", type=_arg_posint, default=None, help="±strikes around ATM to ZOOM to; default = the full tradeable ladder (every strike with open interest)")
    ap.add_argument("--history", action="store_true",
                    help="VOL HISTORY dashboard (IV rank/percentile, const-maturity IV history, vol cone, 25Δ "
                         "skew history, term-through-time) from a HISTORICAL_OPTIONS backfill — a separate page")
    ap.add_argument("--years", type=float, default=2.0,
                    help="--history / --prewarm depth in years (default 2.0 — so the History window's 'All' "
                         "spans more than '1Y'; ≈ 104 weekly points + a recent daily tail)")
    ap.add_argument("--prewarm", action="store_true",
                    help="refresh the cache (skeleton + history) for the prewarm universe, then exit — meant "
                         "for a nightly launchd / Task Scheduler job (set up via setup_check.py --install-prewarm)")
    ap.add_argument("--universe", help="--prewarm: a ticker-list file (default: bundled universe_options.txt + accreted)")
    ap.add_argument("--heal-spots", action="store_true", dest="heal_spots",
                    help="v1.120: re-anchor store records whose spot estimate sits >15%% above that day's "
                         "raw close (the LEAP \u03940.5 trap) — whole store, or one symbol if given; then exit")
    ap.add_argument("--limit", type=int, default=None, dest="prewarm_limit", help="--prewarm: cap the number of tickers")
    ap.add_argument("--equity", action="store_true",
                    help="I really mean the EQUITY namesake of a futures root (e.g. CL = Colgate, not crude)")
    ap.add_argument("--list-expiries", action="store_true",
                    help="print the listed expirations (date · DTE · strikes) and exit — the pre-check "
                         "to pick --expiry deterministically (one AV call, no dashboard)")
    ap.add_argument("--svg", help="write a self-contained SVG dashboard (fixed landscape)")
    ap.add_argument("--html", help="write a self-contained RESPONSIVE HTML dashboard (Chart.js, reflows 1->2 cols)")
    ap.add_argument("--png", action="store_true", help="also write a PNG (rsvg-convert/qlmanage/headless browser)")
    ap.add_argument("--json", action="store_true", help="emit compact JSON instead of text")
    ap.add_argument("--emit-spec", metavar="PATH", default=None,
                    help="(--full) also dump the page SPEC as JSON — the v1.127 sibling-embed seam "
                         "(a parent render reads it to embed this board for the client-side swap)")
    ap.add_argument("--fundamentals-link", help=argparse.SUPPRESS)
    ap.add_argument("--open", action="store_true", help="open the dashboard in the default browser")
    ap.add_argument("--from-dir", help="MCP-fallback (egress-blocked sandboxes): build from a directory with a saved "
                    "REALTIME_OPTIONS result (filename contains 'option') + optional GLOBAL_QUOTE ('quote')")
    ap.add_argument("--watch", type=_arg_watch, nargs="?", const=dashboard.POLL_DEFAULT_S, default=None, metavar="SECONDS",
                    help="LIVE mode: serve the dashboard.py live page — the provider re-polls ONLY "
                         "the displayed series every N seconds (default 2 = dashboard.POLL_DEFAULT_S; floor 1 = 2 AV calls/poll vs "
                         "the 600/min premium budget) and the engine pushes per-block SSE deltas "
                         "(silent patches, no reload; LIVE badge shows the real poll age). "
                         "Serve it in the Preview Pane; Ctrl-C to stop.")
    ap.add_argument("--port", type=_arg_port, default=8765, help="port for --watch live server (default 8765)")
    ap.add_argument("--full", action="store_true",
                    help="COMBINED tabbed dashboard: Overview + Chain + Term + History on one page "
                         "(Overview/History from the settlement cache; Chain/Term from one chain pull). "
                         "A COLD name defaults to a SERVED page that auto-loads its vol history in the "
                         "background (blinking dots -> fills in live); --static forces a one-shot page")
    ap.add_argument("--static", action="store_true",
                    help="--full: force a one-shot STATIC page (live synthesis + a passive 'run --history' "
                         "note) instead of the default served/auto-loading page for a cold name")
    ap.add_argument("--daily-once", action="store_true",
                    help="--prewarm: exit immediately (0 calls) if a prewarm already COMPLETED today — "
                         "the laptop-friendly schedule retries every few hours and this makes retries free")
    ap.add_argument("--accrete-etf", metavar="ETFS",
                    help="pull each ETF's ETF_PROFILE holdings and add every constituent to the prewarm "
                         "universe (e.g. --accrete-etf SPY,QQQ = the live S&P 500 + Nasdaq-100), then exit")
    ap.add_argument("--env-file")
    a = ap.parse_args(argv)

    if a.accrete_etf:   # universe expansion (v1.85): constituents-by-holdings, then exit
        import opthist
        key = get_key(a.env_file)
        total = 0
        for etf in [s.strip().upper() for s in a.accrete_etf.split(",") if s.strip()]:
            prof = av({"function": "ETF_PROFILE", "symbol": etf}, key)
            syms = [h.get("symbol") for h in (prof.get("holdings") or []) if h.get("symbol")]
            if not syms:
                print(f"  {etf}: no holdings (not an ETF / no profile) — skipped", file=sys.stderr)
                continue
            added = opthist.accrete_many(syms)
            total += added
            print(f"  {etf}: {len(syms)} constituents · {added} new in the universe", file=sys.stderr)
        print(f"accrete-etf done: {total} new tickers queued for prewarm (dead ones auto-prune "
              f"on their first empty pull)")
        return
    if a.prewarm:   # cache-warming job (no symbol): skeleton + history for the universe, then exit
        import opthist
        if a.heal_spots:
            print("warning: --heal-spots is ignored with --prewarm (run it separately)", file=sys.stderr)
        if a.daily_once and opthist.prewarm_stamp_fresh():
            print("prewarm: already completed today — nothing to do (--daily-once)", file=sys.stderr)
            return
        universe = opthist.load_universe(a.universe)
        print("prewarm: %d tickers (seed + accreted)%s" % (
            len(universe), (" · limit %d" % a.prewarm_limit) if a.prewarm_limit else ""), file=sys.stderr)
        s = opthist.prewarm(universe, get_key(a.env_file), years=a.years, limit=a.prewarm_limit,
                            progress=lambda i, n, sy, st: print(
                                "  %d/%d  %-18s · warmed %d · dead %d" % (i, n, sy, st["warmed"], st["dead"]),
                                file=sys.stderr, flush=True))
        print("prewarm done: %d warmed · %d dead-pruned · of %d  (%d skeletons already fresh)%s" % (
            s["warmed"], s["dead"], s["n"], s["skelFresh"],
            "  — STOPPED: Alpha Vantage throttled" if s.get("throttled") else ""))
        if not s.get("throttled") and not a.prewarm_limit:
            opthist.prewarm_stamp_write()   # a COMPLETE, unthrottled pass counts for --daily-once
        return
    if a.heal_spots:   # v1.120 store heal (the LEAP-trap spot poison): whole store or one symbol
        import opthist
        key = get_key(a.env_file)
        syms = [opthist._sym_norm(a.symbol.upper())] if a.symbol else None
        if syms and not opthist.load_store(syms[0]):
            print("warning: no history store for %s — nothing to heal (run --history first)" % syms[0],
                  file=sys.stderr)
        rep = opthist.heal_spots_many(key, symbols=syms,
                                      progress=lambda sy, d, sp, cl: print(
                                          "  %s %s  spot %.2f -> close %.2f" % (sy, d, sp, cl),
                                          file=sys.stderr, flush=True))
        print("heal-spots done: %d symbols · %d records checked · %d flagged · %d healed · "
              "%d dropped (unusable chain) · %d failed%s" % (
                  rep["symbols"], rep["checked"], rep["flagged"], rep["healed"], rep["dropped"],
                  rep["failed"],
                  (("  · ABORTED (throttle/dead): " + ",".join(rep["aborted"])) if rep.get("aborted") else "")
                  + (("  · SKIPPED (no closes this run, retry): " + ",".join(rep["skipped"]))
                     if rep.get("skipped") else "")))
        return
    if not a.symbol:
        ap.error("a symbol is required (unless --prewarm)")
    sym = a.symbol.upper()

    _route_guard(sym, a.equity)         # classify-first: futures roots / cash indices never mis-serve
    if a.port != 8765 and a.watch is None:
        print("warning: --port is ignored without --watch", file=sys.stderr)
    if a.list_expiries:                 # validate-before-invoke: the listing only, then stop
        if a.from_dir:                  # local data always wins: a saved chain carries every expiration
            contracts = _read_dir_chain(a.from_dir)[0]
            if not contracts:
                sys.exit(f"error: no option contracts for {sym} (bad ticker / non-equity / no entitlement?)")
            for ln in list_expiries_lines(contracts):
                print(ln)
        else:                           # skeleton cache: instant on repeat within a trading day
            import opthist
            skel, state = opthist.skel_get(sym, get_key(a.env_file))
            if not skel or not skel.get("expirations"):
                sys.exit(f"error: no option contracts for {sym} (bad ticker / non-equity / no entitlement?)")
            if state == "stale":   # live chain empty -> serving an OLD skeleton: label it LOUD, never as current
                print(f"# STALE: live chain empty — showing skeleton as of {skel.get('asOf')} "
                      f"({sym} may be delisted / its listing changed)")
                print(f"warning: {sym} live option chain came back empty — --list-expiries served from a STALE "
                      f"skeleton (as of {skel.get('asOf')}), not the current chain", file=sys.stderr)
            for ln in opthist.skel_expiry_lines(skel):
                print(ln)
            if state == "cached":
                print(f"(from skeleton cache · as of {skel.get('asOf')})", file=sys.stderr)
        return

    if a.emit_spec and not a.full:   # command-layer convention (review P3): never a silent no-op
        print("warning: --emit-spec applies to --full only — ignored", file=sys.stderr)
    if a.full:   # COMBINED tabbed page: Overview + Chain + Term + History (client-swapped tabs)
        import opthist
        for _on, _lbl in ((a.term, "--term"), (a.svg or a.png, "--svg/--png"), (a.history, "--history")):
            if _on:   # command-layer convention: an inapplicable flag WARNS on stderr, never silently no-ops
                print(f"warning: {_lbl} does not apply to --full — ignored", file=sys.stderr)
        if a.watch is not None and a.from_dir:
            print("warning: --watch needs a live key — ignored with --from-dir (static page)", file=sys.stderr)
        key = None if a.from_dir else get_key(a.env_file)   # --from-dir is keyless (egress-blocked / offline)
        hist0 = opthist.load_store(sym)                      # cache-first (one cheap file read, reused below)
        cold = bool(key) and not hist0                      # empty store + a key = cold
        # SERVED (auto-loading) page — the DEFAULT for a cold name (and always under --watch): the Chain tab
        # streams AND, when cold, a background thread backfills the vol history so Overview/History fill in
        # LIVE (blinking dots -> real charts, no reload, no waiting). A static file can't self-load, so
        # --html/--json/--static/--from-dir stay one-shot below.
        serve_it = (not a.from_dir) and bool(key) and (
            a.watch is not None or (cold and not a.static and not a.html and not a.json))
        _profile = opthist.profile_get(sym, key) if key else {}   # {exchange,name,sector,industry}: venue chip + About issuer line
        if serve_it:
            opthist.accrete(sym)   # queue for the nightly prewarm now (this run's served backfill fills it live)
            _iv = float(a.watch) if a.watch is not None else dashboard.POLL_DEFAULT_S
            _bf = " · cold: loading the vol history in the background" if cold else ""
            print(f"{sym} full analysis · SERVED{_bf} — the Chain tab streams; serve it in the Preview Pane "
                  f"(point the dev server at port {a.port}); Ctrl-C to stop.", file=sys.stderr)
            dashboard.serve(make_full_provider(sym, key, a.expiry, a.window,
                                               exchange=_profile.get("exchange"), profile=_profile,
                                               interval=_iv, backfill=True, years=a.years),
                            port=a.port, open_page=a.open)
            return
        if a.open and not a.html:
            print("warning: --open needs --html for --full (there is no file to open) — ignored", file=sys.stderr)
        if cold and a.html and not a.static:   # a static file can't auto-load — point at the served path
            print("note: a static --html file can't auto-load the vol history — drop --html (or add --watch) for "
                  "the SERVED page that backfills it live, or run `options.py %s --history` to fill it now." % sym,
                  file=sys.stderr)
        hist = hist0                                     # cache-first: Overview + History (empty = cold)
        # v1.123.2 — chain + earnings + daily-closes fetched CONCURRENTLY (was serial ~8s wall; the Cowork
        # "slow loading" is this fetch chain, not the render — the page paints in ~56ms). See _full_fetch.
        contracts, tquote, earnings, closes = _full_fetch(sym, key, a.from_dir, hist)
        exchange = _profile.get("exchange")                                       # key; --from-dir stays offline
        _prevF = fetch_oi_baselines(sym, _asof_of(contracts), key) if key else None    # v1.132 ΔOI windows (keyless = offline)
        try:
            spec = build_full(sym, contracts, tquote, hist, earnings, a.expiry, a.window, exchange,
                              profile=_profile, closes=closes, prev_oi=_prevF)   # About names the underlying issuer
        except ChainError as e:
            sys.exit(str(e))
        if not a.from_dir:
            opthist.accrete(sym)                         # queue for nightly prewarm — but never a --from-dir fixture
        warm = "warm" if hist else "cold — Overview live-synthesis · History warming"
        if a.emit_spec:
            with open(a.emit_spec, "w", encoding="utf-8") as f:
                json.dump(spec, f)   # v1.127: the sibling-embed seam (a parent render embeds this board)
        if a.html:
            if not a.from_dir and not os.environ.get("MARKET_DATA_NO_SIBLING"):
                dashboard.write_atomic(a.html, dashboard.render_interactive_html(spec))
            _links, _sib = _fund_links(a.html, sym, bg=not a.from_dir, embed=True,
                                       fundamentals_fname=a.fundamentals_link)
            spec["links"] = _links
            if _sib:
                spec["sibling"] = _sib   # v1.127 Cowork: the chip swaps boards in place (no navigation)
            dashboard.deliver_html(a.html, dashboard.render_interactive_html(spec),
                                   offers=["/equity-research:earnings-preview " + sym,
                                           "/equity-research:catalysts"])
            print(f"{a.html}  |  {sym} full analysis ({len(spec['sections'])} tabs · {warm})")
            if _sib:
                dashboard.write_reverse_sibling(_sib, spec, a.html)
            if a.open:
                webbrowser.open(pathlib.Path(os.path.abspath(a.html)).as_uri())
        elif a.json:
            print(json.dumps(spec, separators=(",", ":")))
        else:
            print(f"{sym} options · full analysis · Overview · Chain · Term · History"
                  + ("" if hist else "  [History warming — run --history or wait for prewarm]"))
            print(render_text(_attach_droi(build(sym, contracts, tquote, a.expiry, a.window), _prevF)))
        return

    if a.history:   # SEPARATE dashboard: vol history from a HISTORICAL_OPTIONS backfill (own page/spec)
        import opthist
        if a.term or a.watch is not None:
            print("warning: --history is a standalone page — --term/--watch ignored", file=sys.stderr)
        key = get_key(a.env_file)
        dates = opthist.history_dates(dt.date.today(), years=a.years, daily=15)
        try:
            hist = opthist.update_store(sym, dates, key, fetch_closes=opthist.fetch_daily_closes_raw,
                                        progress=lambda d, t: print("  backfill %d/%d" % (d, t), file=sys.stderr))
        except opthist._Throttled as thr:   # throttled mid-backfill: partial is saved; show it, don't crash
            hist = thr.partial
            print(f"warning: Alpha Vantage throttled the backfill — showing PARTIAL history ({len(hist)} "
                  f"sessions, saved to cache); rerun later to fill the rest", file=sys.stderr)
        if not hist:
            sys.exit(f"error: no historical option data for {sym} (throttled with nothing cached / bad "
                     f"ticker / non-equity / no entitlement?)")
        opthist.accrete(sym)   # this name now rides the nightly prewarm
        earnings = opthist.fetch_earnings(sym, key)
        closes = opthist.fetch_daily_closes(sym, key)   # v1.83.0: realized-vol overlay + VRP card (best-effort)
        if a.html:
            dashboard.deliver_html(
                a.html, opthist.render_history_html(sym, hist, earnings=earnings, closes=closes),
                offers=["/equity-research:earnings-preview " + sym,
                        "/equity-research:catalysts"])
            print(f"{a.html}  |  {sym} volatility history ({len(hist)} sessions)")
            if a.open:
                webbrowser.open(pathlib.Path(os.path.abspath(a.html)).as_uri())
        elif a.json:   # the spec is only needed here — don't build cone/interp for --html or text
            print(json.dumps(opthist.spec_history(sym, hist, earnings=earnings, closes=closes), separators=(",", ":")))
        else:
            print(opthist.history_text(sym, hist, closes=closes))
        return

    if a.term:
        if a.watch is not None:
            print("warning: --watch does not apply to --term (one-shot) — ignored", file=sys.stderr)
        if a.svg or a.png:
            print("warning: --svg/--png do not apply to --term — use --html (or --json) for the "
                  "cross-expiration page", file=sys.stderr)
        # COMBINED page: the chosen series (--expiry or nearest) + the term analytics, ONE payload.
        # The full chain is fetched WITHOUT the expiration pushdown — term needs every series; the
        # single-series build then filters locally.
        import opthist
        _tkey = None if a.from_dir else get_key(a.env_file)
        _tprofile = opthist.profile_get(sym, _tkey) if _tkey else {}   # About names the underlying issuer here too
        contracts, tquote = (_read_dir_chain(a.from_dir) if a.from_dir else _term_fetch(sym, _tkey))
        _prevT = fetch_oi_baselines(sym, _asof_of(contracts), _tkey) if _tkey else None   # v1.132 ΔOI windows
        try:
            D = _attach_droi(build(sym, contracts, tquote, a.expiry, a.window), _prevT)   # the target
            T = build_term(sym, contracts, tquote,
                           include=(D["expiration"] if a.expiry else None))   # --expiry beyond the cap rides along
        except ChainError as e:
            sys.exit(str(e))
        # Pre-build EVERY series' chain page so the switcher swaps client-side (AV-native IV, read once).
        # A series too thin to chart (<3 strikes) is dropped from BOTH the switcher and the term overview
        # so the two stay index-aligned — loudly (§0.5: never swallow silently).
        specs, keep = [], []
        for i, s in enumerate(T["series"]):
            try:
                _td = build(sym, contracts, tquote, s["expiration"], a.window)
                _td["profile"] = _tprofile
                _attach_droi(_td, _prevT)   # v1.130: every switcher series carries its ΔOI ladder
                specs.append(to_spec(_td))
                keep.append(i)
            except ChainError:
                continue
        if len(keep) < len(T["series"]):
            dropped = [s["expiration"] for i, s in enumerate(T["series"]) if i not in set(keep)]
            print(f"note: dropped from the term view (<3 strikes, unchartable): {', '.join(dropped)}",
                  file=sys.stderr)
        if not specs:
            sys.exit(f"error: no {sym} series has enough strikes to chart with --term")
        T = _term_keep(T, keep)
        target_idx = next((i for i, s in enumerate(T["series"]) if s["expiration"] == D["expiration"]), None)
        if target_idx is None:   # a same-day/past --expiry — the term view holds future series only
            sys.exit(f"error: --expiry {D['expiration']} cannot be shown on the --term page (future "
                     f"series only) — run without --term for that series")
        spec = combined_spec(specs, T, target_idx)
        if a.html:
            if not a.from_dir and not os.environ.get("MARKET_DATA_NO_SIBLING"):
                dashboard.write_atomic(a.html, dashboard.render_interactive_html(spec))
            spec["links"] = _fund_links(a.html, sym, bg=not a.from_dir,
                                         fundamentals_fname=a.fundamentals_link)[0]   # --term: link only, no embed
            dashboard.deliver_html(a.html, dashboard.render_interactive_html(spec),
                                   offers=["/equity-research:earnings-preview " + sym,
                                           "/equity-research:catalysts"])
            print(f"{a.html}  |  {D['symbol']} chain + term dashboard ({len(T['series'])} series)")
            if a.open:
                webbrowser.open(pathlib.Path(os.path.abspath(a.html)).as_uri())
        elif a.json:
            print(json.dumps(spec, separators=(",", ":")))
        else:
            print(render_text(D) + "\n\n" + render_term_text(T))
        return

    if a.watch is not None:  # live SSE server (HTTP only — a static dir snapshot can't update)
        if a.from_dir:
            ap.error("--watch needs live HTTP; it can't be combined with --from-dir")
        dashboard.serve(make_provider(sym, get_key(a.env_file), a.expiry, a.window,
                                      interval=float(a.watch)),
                        port=a.port, open_page=a.open)
        return

    if a.from_dir:
        D, _skey = read_from_dir(sym, a.from_dir, a.expiry, a.window), None
    else:
        _skey = get_key(a.env_file)
        D = gather(sym, _skey, a.expiry, a.window)
        _attach_droi(D, fetch_oi_baselines(sym, D.get("asOf"), _skey))   # v1.132 ΔOI windows (best-effort, cached)

    if a.svg or a.png or a.open or a.html:
        wrote = []
        if a.html:
            if _skey:                                    # About names the underlying issuer on the HTML dashboard
                import opthist
                D["profile"] = opthist.profile_get(sym, _skey)
            _sp = to_spec(D)
            if not a.from_dir and not os.environ.get("MARKET_DATA_NO_SIBLING"):
                dashboard.write_atomic(a.html, dashboard.render_interactive_html(_sp))
            _sp["links"] = _fund_links(a.html, sym, bg=not a.from_dir,
                                        fundamentals_fname=a.fundamentals_link)[0]   # single-series: link only
            # an options read is an EVENT-RISK read: SKILL.md pairs a high-IV chain with the
            # earnings preview, and a dated catalyst with the calendar (the CAT session 2026-07-23
            # closed its options read text-only and card-less — this is that hole's mechanism).
            dashboard.deliver_html(a.html, dashboard.render_interactive_html(_sp),
                                   offers=["/equity-research:earnings-preview " + D["symbol"],
                                           "/equity-research:catalysts"])
            print(f"{a.html}  |  {D['symbol']} options responsive html dashboard")
            wrote.append(a.html)
        if a.svg or a.png or not wrote:
            out = a.svg or (D["symbol"].lower() + "_options.svg")
            open(out, "w", encoding="utf-8").write(render_svg(D))
            print(f"{out}  |  {D['symbol']} options  |  exp {D['expiration']} ({D['dte']} DTE)  |  ATM IV {iv(D['atmIV'])} · max pain {num(D['maxPain'],0)}")
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
    # opthist does `import options`; when this file is run as __main__ that would execute it a SECOND
    # time as module "options" — a distinct module with its OWN ChainError, so `except ChainError` in
    # main() would miss errors raised through opthist -> options._fetch_contracts. Alias the running
    # module as "options" so the single class is shared.
    sys.modules.setdefault("options", sys.modules[__name__])
    main()
