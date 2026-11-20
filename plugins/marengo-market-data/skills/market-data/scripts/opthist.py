#!/usr/bin/env python3
"""Historical option-vol analytics for the equity/ETF path (Ф-history).

Backfills Alpha Vantage HISTORICAL_OPTIONS (full EOD/settlement chains, IV + greeks,
15+ yrs) for a set of past dates, reduces each date's chain to a compact daily aggregate
(const-maturity ATM IV, 25Δ skew, PCR) using the SAME reduction as options.build_term but
keyed off the AS-OF date (not today), and computes the history-only analytics that a single
snapshot can't give: IV rank / percentile, the vol cone, and the const-maturity IV history.

Reduce-on-ingest: the ~1.5 MB raw chain per date is turned into ~1 KB of aggregate and
discarded — the giant payloads never persist or enter context. Pure reduce/metric functions
(reduce_asof / interp_cm / iv_rank / vol_cone) take plain dicts and are unit-tested offline;
only backfill() touches the network, through an injectable fetch seam.
"""
import os, re, sys, json, datetime as dt, concurrent.futures, threading, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import options  # av(), ff(), get_key() — reuse the AV HTTP + safe-float
import dashboard  # render_html(spec) — the shared spec-driven dashboard engine

ff = options.ff
PACKAGED_STORE_TGZ = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "store.tgz"
)
_PACKAGED_STORE_MAX_MEMBER = 16 * 1024 * 1024
CM_TENORS = (30, 60, 90)                      # constant-maturity headline tenors (days)
RANGEBAR_TENORS = (30, 90)                    # Overview IV-rank range bar: one coloured dot per tenor (user 2026-07-08)
# The stored PCR (put/call OI ratio) sums only NEAR-DATED series (<= this DTE). A whole-book sum is
# dominated by deep-OTM LEAPS call walls on names that list far series (MOEX GAZP lists calls out to
# 2030 with ~zero put OI -> a whole-book PCR of ~0.04 while the front month is ~1.16); the near window
# tracks tradeable sentiment and matches the front-series PCR the live chain page shows (user 2026-07-24).
# TRANSITION CAVEAT (review P2): AV's update_store is INCREMENTAL (only new dates are reduced; stored
# records are never re-reduced — the raw chains aren't kept), so a store warmed BEFORE this change keeps
# its old whole-book pcr on old dates and gets near-window pcr on new ones — a one-time basis discontinuity
# in the "Put/call ratio history" chart at the changeover date. It is small for US names (no GAZP-class far
# call walls) and self-corrects on a full `--history` re-backfill. The MOEX path (moexopthist.build_records)
# re-reduces EVERY date each run, so its stored pcr is uniformly near-window — no discontinuity there.
_PCR_MAX_DTE = 90
CONE_TENORS = (14, 30, 60, 90, 120)           # vol-cone tenor grid
HM_TENORS = (7, 14, 30, 60, 90, 120, 180, 270)   # term-through-time heatmap: denser + out to 270d (a day
#                                                  without that far a listing shows a hole, never extrapolated)
def _ensure_store_unpacked(here=None, cache_parent=None, required_subdir="options"):
    """Make a packaged warm store available and return the parent holding its leaves.

    A private package carries ``store.tgz`` because the uncompressed store is too large for the skill
    package limit.  Desktop/Work tasks can mount the skill itself read-only, however.  In that case
    unpacking beside the scripts fails and a cache-first dashboard would falsely look cold.  Prefer the
    portable ``<skill>/store`` location, then fall back to the writable ``~/.cache/market-data`` parent.
    ``required_subdir`` lets the futures caller request ``futures`` even when an options cache already
    exists.  An explicit MARKET_DATA_STORE override returns before this function, so it is never touched.
    """
    here = os.path.dirname(os.path.abspath(__file__)) if here is None else here
    skill = os.path.dirname(here)
    tgz = os.path.join(skill, "store.tgz")
    local_parent = os.path.join(skill, "store")
    fallback_parent = (cache_parent or
                       os.path.dirname(os.path.expanduser("~/.cache/market-data/options")))
    fallback_parent = os.path.abspath(os.path.expanduser(fallback_parent))
    if os.path.isdir(os.path.join(local_parent, required_subdir)):
        return local_parent
    if not os.path.isfile(tgz):
        return fallback_parent if os.path.isdir(os.path.join(fallback_parent, required_subdir)) else None

    def unpack_into(stage_parent, destination):
        """Extract the archive into `destination`; report (carries_leaf, placed).

        The caller needs that answer to tell "the unpack went wrong here, try the writable
        fallback" apart from "this archive simply has no such leaf" — the second stage would
        extract the same bytes to the same absence, wasting a full extraction and leaving a
        duplicate copy of the store behind.
        """
        import tarfile, tempfile, shutil
        os.makedirs(stage_parent, exist_ok=True)
        tmp = tempfile.mkdtemp(prefix=".store-unpack-", dir=stage_parent)
        try:
            with tarfile.open(tgz, "r:gz") as tf:
                # Our own archive, but Python 3.9 lacks extractall(filter=): reject paths that could
                # escape the temporary directory before extracting.
                for m in tf.getmembers():
                    parts = m.name.replace("\\", "/").split("/")
                    if os.path.isabs(m.name) or ".." in parts:
                        raise ValueError("unsafe tar member %r" % m.name)
                tf.extractall(tmp)          # -> options/ futures/ fundamentals/, all inside tmp
            carries_leaf = os.path.isdir(os.path.join(tmp, required_subdir))
            os.makedirs(destination, exist_ok=True)
            for sub in os.listdir(tmp):
                dst = os.path.join(destination, sub)
                if not os.path.exists(dst):
                    try:
                        os.rename(os.path.join(tmp, sub), dst)
                    except OSError:
                        pass                # a concurrent unpack won the race for this leaf — fine
            # placed: did the leaf actually ARRIVE? It can be missing with nothing raised — a file
            # already occupying the name is skipped by the loop above — and that is a failure with
            # a real cause, not the "this archive has no such leaf" absence.
            return carries_leaf, os.path.isdir(os.path.join(destination, required_subdir))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    errors = []
    for stage_parent, destination, result_parent in (
        (skill, local_parent, local_parent),
        (fallback_parent, fallback_parent, fallback_parent),
    ):
        try:
            carries_leaf, placed = unpack_into(stage_parent, destination)
            if os.path.isdir(os.path.join(result_parent, required_subdir)):
                return result_parent
            if not carries_leaf:
                return None                 # absence, not failure: say nothing, retry nothing
            if not placed:                  # it IS in the archive and still did not arrive
                reason = ("%s: unpacked, but %s could not be placed in %s (something else occupies "
                          "that name)" % (stage_parent, required_subdir, destination))
                if reason not in errors:
                    errors.append(reason)
                continue                    # the other stage may still have a usable parent
        except Exception as e:
            reason = "%s: %s" % (type(e).__name__, e)
            if reason not in errors:
                errors.append(reason)
    # Only a real failure of BOTH stages is reported, and only once: this note used to fire from
    # inside the loop, so a successful unpack missing the requested leaf printed "failed ()" —
    # an empty reason for a failure that never happened (review P2, 2026-08-06).
    if errors:
        sys.stderr.write(
            "market-data: store.tgz unpack failed (%s) — bundled history remains readable directly\n"
            % "; ".join(errors)
        )
    return None


def _resolve_store_dir(env=None, here=None, expanduser=os.path.expanduser, cache_parent=None):
    """Store-path ladder (v1.93.0, the Cowork fix): MARKET_DATA_STORE env override ->
    <skill-root>/store/options when it exists -> ~/.cache/market-data/options.
    Cowork mounts ONLY the skill snapshot into its sandbox (the VM home is throwaway),
    so the store must ride the skill dir to be visible there; the repo-dev copy reaches
    the same canonical store through a symlinked store/options (isdir follows links).
    A shipped store.tgz (v1.123.1, the 30MB package limit) is unpacked here first."""
    env = os.environ if env is None else env
    override = (env.get("MARKET_DATA_STORE") or "").strip()
    if override:
        # expand + absolutize: a "~/store" or relative override must not create a literal
        # "./~" dir or fork the store per-CWD (review P3, 2026-07-14)
        return os.path.abspath(os.path.expanduser(override))
    here = os.path.dirname(os.path.abspath(__file__)) if here is None else here
    fallback = expanduser("~/.cache/market-data/options")
    fallback_parent = cache_parent or os.path.dirname(fallback)
    unpacked_parent = _ensure_store_unpacked(here, cache_parent=fallback_parent)
    local = os.path.join(os.path.dirname(here), "store", "options")
    if os.path.isdir(local):
        return local
    if unpacked_parent and os.path.isdir(os.path.join(unpacked_parent, "options")):
        return os.path.join(unpacked_parent, "options")
    return fallback


CACHE_DIR = _resolve_store_dir()


class _Throttled(Exception):
    """Raised when Alpha Vantage throttles/stops a backfill mid-run. Carries `partial` — the
    aggregates gathered before the stop — so the caller persists them and reports, instead of
    draining the metered key or silently discarding paid work."""

    def __init__(self, partial):
        super().__init__("Alpha Vantage throttled the backfill")
        self.partial = partial


def _hist_rows(payload):
    """Rows from a HISTORICAL_OPTIONS payload. A valid response ALWAYS carries a 'data' list (possibly
    empty = a legitimate non-trading date). A response with NO 'data' key is an AV error / entitlement /
    sample payload and must die LOUD — never masquerade as an empty (non-trading) chain, which would
    silently truncate the history (wrong IV rank/cone) or prune a live ticker as dead."""
    if isinstance(payload, dict) and "data" in payload:
        return payload.get("data") or []
    sys.exit("error: Alpha Vantage returned no option 'data' (entitlement / bad symbol / sample payload): "
             + str(payload)[:160])


def hist_fetch(symbol, date, key):
    """One past date's full chain -> list of contract rows ([] for a non-trading date; dies loud on
    an AV error/entitlement payload via _hist_rows)."""
    return _hist_rows(options.av({"function": "HISTORICAL_OPTIONS", "symbol": symbol,
                                  "date": date, "datatype": "json"}, key))


def fetch_earnings(symbol, key):
    """Best-effort earnings overlay: past announcement dates (EARNINGS quarterlyEarnings.reportedDate)
    + the next report date (EARNINGS_CALENDAR CSV). Both small non-chain calls; any failure just
    returns empty so the dashboard never breaks on the overlay. -> {'past':[dates], 'next':date|None}."""
    import urllib.request, urllib.parse

    def _get(params):
        u = "https://www.alphavantage.co/query?" + urllib.parse.urlencode(dict(params, apikey=key))
        return urllib.request.urlopen(u, timeout=30).read().decode()
    past, nxt = [], None
    try:
        e = json.loads(_get({"function": "EARNINGS", "symbol": symbol}))
        past = sorted(q["reportedDate"] for q in (e.get("quarterlyEarnings") or [])
                      if isinstance(q.get("reportedDate"), str) and len(q["reportedDate"]) == 10)
    except Exception:
        pass
    try:
        rows = [ln for ln in _get({"function": "EARNINGS_CALENDAR", "symbol": symbol,
                                   "horizon": "3month"}).strip().splitlines()[1:] if ln.strip()]
        if rows:
            p = rows[0].split(",")
            if len(p) > 2 and len(p[2]) == 10:
                nxt = p[2]
    except Exception:
        pass
    return {"past": past, "next": nxt}


def _annualized_hv(logrets):
    """Annualized realized (historical) volatility from daily log-returns: sample stdev × √252, as a
    FRACTION. None with <2 returns. v1.83.0 VRP — the realized twin of implied ATM IV."""
    n = len(logrets)
    if n < 2:
        return None
    m = sum(logrets) / n
    var = sum((r - m) ** 2 for r in logrets) / (n - 1)
    return (var ** 0.5) * (252 ** 0.5)


def _realized_from_sorted(px_sorted, asof, window=20):
    """HV from an ALREADY-sorted [(date_str, close)] ascending list — the shared core so
    _realized_series doesn't re-sort the whole history once per display date (review 2026-07-13)."""
    import math
    # last window+1 prices dated on/before asof (bisect on the sorted date keys)
    import bisect
    hi = bisect.bisect_right([d for d, _ in px_sorted], asof)
    if hi < window + 1:
        return None
    tail = [c for _, c in px_sorted[hi - (window + 1):hi]]
    rets = [math.log(tail[i] / tail[i - 1]) for i in range(1, len(tail))]
    return _annualized_hv(rets)


def realized_at(closes, asof, window=20):
    """Annualized realized vol of the last `window` daily log-returns whose date is <= asof.
    `closes` = an iterable of (YYYY-MM-DD, close); None if fewer than window+1 usable prices on/before
    asof (never a fake 0). Uses adjusted closes so a split doesn't spike the vol. v1.83.0."""
    asof = asof if isinstance(asof, str) else asof.isoformat()
    px = sorted((d if isinstance(d, str) else d.isoformat(), c) for d, c in closes if c and c > 0)
    return _realized_from_sorted(px, asof, window)


def fetch_daily_closes(symbol, key):
    """Best-effort daily adjusted-close series for the realized-vol overlay: {date: adjusted_close}.
    Any failure -> {} (the VRP overlay just doesn't appear). Split-adjusted '5. adjusted close' — for
    realized vol we want a continuous return series, so the adjusted close is right. v1.83.0 (VRP)."""
    import urllib.request, urllib.parse
    try:
        u = "https://www.alphavantage.co/query?" + urllib.parse.urlencode(
            {"function": "TIME_SERIES_DAILY_ADJUSTED", "symbol": symbol, "outputsize": "full",
             "entitlement": "realtime", "apikey": key})
        payload = json.loads(urllib.request.urlopen(u, timeout=30).read().decode())
        ts = payload.get("Time Series (Daily)") or {}
        out = {}
        for d, row in ts.items():
            c = ff(row.get("5. adjusted close") or row.get("4. close"))
            if c and c > 0:
                out[d] = c
        return out
    except Exception:
        return {}


def fetch_daily_closes_raw(symbol, key, outputsize="full", _urlopen=None):
    """Best-effort RAW daily closes {date: close} for the v1.120 spot anchor — TIME_SERIES_DAILY
    `4. close`, deliberately NOT the adjusted series: option strikes live in that date's OWN
    split terms, so anchoring a pre-split record with an adjusted close would mis-place ATM by
    the split factor. Any failure -> {} (the caller falls back to the front-window chain scan).
    Gzip'd + paced + `outputsize=` (review 2026-07-18): the nightly prewarm pulls this once per
    name — un-gzipped `full` across the universe is ~1GB wire/night and 630 unpaced request
    slots; the steady state (only recent grid dates missing) rides `compact` (100 sessions).
    `_urlopen` is the offline-test seam (receives the urllib Request)."""
    import urllib.request, urllib.parse, gzip as _gz
    opener = _urlopen or urllib.request.urlopen
    try:
        _pace()                     # rides the same global 450/min budget as the chain pulls
        u = "https://www.alphavantage.co/query?" + urllib.parse.urlencode(
            {"function": "TIME_SERIES_DAILY", "symbol": symbol, "outputsize": outputsize,
             "entitlement": "realtime", "apikey": key})
        req = urllib.request.Request(u, headers={"Accept-Encoding": "gzip"})
        raw = opener(req, timeout=30).read()
        if raw[:2] == b"\x1f\x8b":
            raw = _gz.decompress(raw)
        payload = json.loads(raw.decode())
        ts = payload.get("Time Series (Daily)") or {}
        out = {}
        for d, row in ts.items():
            c = ff(row.get("4. close"))
            if c and c > 0:
                out[d] = c
        return out
    except Exception:
        return {}


_PRICE_CLASS_RE = re.compile(r"^([A-Z]{2,})([ABCK])$")   # a COLLAPSED class share: BRKB / HEIA / MOGA


def closes_for_dates(symbol, key, dates, _fetch=None, outputsize=None):
    """RAW closes covering `dates`, resolving the OPTIONS-vs-PRICE symbol split (v1.122.0).

    `_sym_norm` collapses class shares because AV's OPTIONS endpoints key them that way (BRK.B ->
    BRKB), but TIME_SERIES_DAILY keys them HYPHENATED — and the collapsed form is not merely empty
    there: "BRKB" resolves to a DEFUNCT namesake (probed 2026-07-19: 14 closes ending 2017-09-27).
    An emptiness test would therefore pass on garbage, so the gate is COVERAGE of the dates we are
    actually anchoring: zero overlap on a class-share-shaped symbol falls through to the hyphen
    form. A normal ticker covers on the first pull and never burns the second call; a symbol with
    no class-share shape (or no coverage either way) returns whatever it got — an honest {} lets
    the caller fall back to the front-window chain scan.

    Note the residual hole this does NOT close: a namesake that PARTLY overlaps the wanted dates
    still passes the coverage gate. Nothing in the payload identifies the security, so catching
    that needs a cross-check (e.g. against the chain's own strike range), deliberately not built."""
    fetch = _fetch or fetch_daily_closes_raw
    kw = {} if outputsize is None else {"outputsize": outputsize}
    want = set(dates or ())
    out = fetch(symbol, key, **kw) or {}
    if want and out and not (want & set(out)):
        # v1.126: the alt fires only on the DEFUNCT-NAMESAKE shape (a NON-empty payload with zero
        # coverage — BRKB's 2017 ghost). An EMPTY first pull is ambiguous (throttle wave vs no
        # data), and every ≥3-letter ticker ending in A/B/C/K (NVDA, TSLA, ARKK…) matched the
        # class-share regex — a store-wide heal in a throttle wave burned a paced phantom second
        # call per name. Tradeoff (deliberate): a TRUE class share whose collapsed form is
        # genuinely EMPTY on the price endpoint loses the hyphen retry — the caller's chain-scan
        # fallback still anchors it.
        m = _PRICE_CLASS_RE.match(symbol.upper())
        if m:
            alt = fetch(m.group(1) + "-" + m.group(2), key, **kw) or {}
            if want & set(alt):
                return alt
    return out


def _realized_series(closes, dates, window=20):
    """Annualized realized vol (%) at each date in `dates` from the daily `closes` map — aligned to the
    (possibly coarsened) history display dates. None where there isn't enough trailing history. v1.83.0.
    Sorts the close series ONCE (not per date — review 2026-07-13)."""
    items = closes.items() if isinstance(closes, dict) else closes
    px = sorted((d if isinstance(d, str) else d.isoformat(), c) for d, c in items if c and c > 0)
    return [(round(hv * 100, 2) if (hv := _realized_from_sorted(px, d, window)) is not None else None) for d in dates]


def _spot_from_chain(contracts, asof):
    """Estimate the underlying from the chain: the strike whose call delta is nearest 0.5 —
    scanned within ONE near expiration only (dte 5–45, else the nearest ≥1). v1.120.0: the
    old ALL-expiration scan was the store's IV-inflation defect — under lognormal pricing
    K(Δ=0.5) = F·e^(σ²T/2), so on a high-IV name a 2-year LEAP's Δ0.5 strike legitimately
    sits at ~2× spot and strike-grid luck decided the race (real MSTR 2026-07-16 record:
    winner strike 210 exp 2028, gap 0.00042, vs the true ATM 97 gap 0.00232 — close 94.03,
    cm30 inflated 128% vs the true 88%). Inside dte ≤45 the same factor is ≤~1.05 even at
    σ=1.5, so the near-series scan is safe. HISTORICAL_OPTIONS carries no quote, so the
    chain itself sets the moneyness; an authoritative daily close beats this estimate
    entirely (reduce_asof's spot_hint)."""
    if isinstance(asof, str):
        asof = dt.date.fromisoformat(asof)
    by_dte = {}
    for c in contracts:
        if c.get("type") != "call":
            continue
        d, k = ff(c.get("delta")), ff(c.get("strike"))
        if d is None or k is None:
            continue
        try:
            ed = dt.date.fromisoformat(c.get("expiration"))
        except (ValueError, TypeError):
            continue
        dte = (ed - asof).days
        if dte < 1:
            continue
        by_dte.setdefault(dte, []).append((abs(d - 0.5), k))
    if not by_dte:
        return None
    window = [t for t in by_dte if 5 <= t <= 45]
    target = min(window) if window else min(by_dte)
    return min(by_dte[target])[1]


def _iv_of(c):
    return options.plausible_iv(ff((c or {}).get("implied_volatility")))   # FRACTION; 0/None/AV-sentinel -> None (a hole)


def interp_cm(points, target):
    """Linear interpolate value at target DTE from [(dte, val)] (val may be None).
    Flat-extrapolate past the ends. None if no usable points."""
    pts = sorted((d, v) for d, v in points if v is not None and d is not None)
    if not pts:
        return None
    if target <= pts[0][0]:
        return pts[0][1]
    if target >= pts[-1][0]:
        return pts[-1][1]
    for (d0, v0), (d1, v1) in zip(pts, pts[1:]):
        if d0 <= target <= d1:
            return v0 + (v1 - v0) * ((target - d0) / (d1 - d0)) if d1 > d0 else v0
    return pts[-1][1]


def _iv_points(series):
    """(dte, atmIV) points for CM interpolation — drops holes AND AV's 0.01488 sentinel (options.plausible_iv)
    so a LEGACY store that already reduced the sentinel (pre-fix; the prewarmed tickers do) doesn't poison the
    cone MIN / term-through-time heatmap on READ. The write side (_iv_of) keeps NEW stores clean."""
    return [(s["dte"], s["atmIV"]) for s in series
            if s.get("dte") is not None and options.plausible_iv(s.get("atmIV")) is not None]


def _plausible_skew(skew, iv):
    """25Δ skew (vol pts) or None if it's implausibly large vs its own ATM IV — the legacy AV 0.01488-sentinel
    signature: a 25Δ leg ≈ 0 makes |skew| ≈ ATM IV·100. A REAL risk-reversal is a small fraction of the ATM
    (both 25Δ legs sit near it), so the 0.85·ATM gate never nulls a genuine skew. Read-side LEGACY-store guard;
    NEW stores drop a sentinel leg on write (_iv_of) → their skew is already None. A stored skew is a scalar
    difference (the raw legs are gone), so it can only be guarded, not healed like cmXX."""
    if skew is None or not iv or iv <= 0:
        return skew
    return None if abs(skew) > 0.85 * iv * 100 else skew


_CM_EDGE_TOL = 7   # calendar-DTE clamp window past the listed span (see interp_cm_ranged)


def interp_cm_ranged(points, target):
    """interp_cm, but HONEST at the wings (v1.125): a target outside the points' [min..max] DTE
    span is NOT flat-extrapolated — except within _CM_EDGE_TOL days of an edge, where the edge
    value stands in as a near-tenor proxy (a monthly-cycle chain's front sits 31–35 dte on every
    post-roll day; refusing those would punch monthly holes into every no-weeklies cm30 history).
    Beyond the tolerance: None. A chain listing ONLY ~30d must not fabricate cm60/cm90 (pre-fix
    a single-series day stored cm30=cm60=cm90, term_shape read a fake 'flat' and the history
    lines re-plotted the front IV as 60/90d — while the heatmap's _cm_at said None)."""
    pts = [(d, v) for d, v in points if v is not None and d is not None]
    if not pts:
        return None
    lo, hi = min(d for d, _ in pts), max(d for d, _ in pts)
    if target < lo - _CM_EDGE_TOL or target > hi + _CM_EDGE_TOL:
        return None
    return interp_cm(pts, max(lo, min(hi, target)))


def _cm_at(series, target):
    """Const-maturity ATM IV at `target` DTE for ONE day, but HONEST at the wings: a target beyond the day's
    listed [min..max] DTE span (± the _CM_EDGE_TOL near-miss clamp) returns None (a hole), never a
    flat-extrapolation. For the term heatmap so a far tenor (e.g. 270d) on a day with no that-far listing
    stays blank instead of faking the last value."""
    return interp_cm_ranged(_iv_points(series), target)


def reduce_asof(contracts, asof, max_days=400, spot_hint=None):
    """One historical chain -> a daily aggregate keyed to `asof` (a date or ISO string).
    Per expiration: ATM IV (OTM-leg native IV at the strike nearest spot) and 25Δ skew
    (IV(put δ≈−0.25) − IV(call δ≈+0.25), vol points). Then const-maturity 30/60/90d IV and
    30d skew by DTE interpolation, and PCR by open interest over the NEAR-dated series (dte <= _PCR_MAX_DTE;
    far LEAPS call walls would swamp a whole-chain ratio — see the constant).
    `spot_hint` (v1.120.0) = that day's authoritative RAW close: it WINS over the chain
    estimate outright (the chain scan is the no-close fallback — see _spot_from_chain).
    Returns None if the chain has no usable ATM IV at asof (dead/empty date)."""
    if isinstance(asof, str):
        asof = dt.date.fromisoformat(asof)
    if not contracts:
        return None
    spot = spot_hint if (spot_hint and spot_hint > 0) else _spot_from_chain(contracts, asof)
    if not spot or spot <= 0:
        return None
    by_exp = {}
    for c in contracts:
        e = c.get("expiration")
        try:
            ed = dt.date.fromisoformat(e)
        except (ValueError, TypeError):
            continue
        dte = (ed - asof).days
        if 1 <= dte <= max_days:
            by_exp.setdefault(e, (dte, []))[1].append(c)

    series, call_oi, put_oi = [], 0.0, 0.0
    for e, (dte, rows) in sorted(by_exp.items()):
        calls = {ff(c["strike"]): c for c in rows if c.get("type") == "call" and ff(c.get("strike")) is not None}
        puts = {ff(c["strike"]): c for c in rows if c.get("type") == "put" and ff(c.get("strike")) is not None}
        strikes = sorted(set(calls) | set(puts))
        if not strikes:
            continue
        atm_iv, skew = options.atm_iv_skew(strikes, calls, puts, spot, _iv_of)   # shared live/history reduction
        series.append({"dte": dte, "atmIV": (round(atm_iv, 5) if atm_iv else None), "skew25": skew})
        if dte <= _PCR_MAX_DTE:   # PCR = NEAR-dated book only (far LEAPS call walls would swamp it)
            for c in rows:
                oi = ff(c.get("open_interest")) or 0
                if c.get("type") == "call":
                    call_oi += oi
                elif c.get("type") == "put":
                    put_oi += oi

    if not any(s["atmIV"] for s in series):
        return None
    iv_pts = [(s["dte"], s["atmIV"]) for s in series]
    sk_pts = [(s["dte"], s["skew25"]) for s in series]
    agg = {"date": asof.isoformat(), "spot": round(spot, 4),
           "series": series, "pcr": (round(put_oi / call_oi, 4) if call_oi else None)}
    for t in CM_TENORS:
        v = interp_cm_ranged(iv_pts, t)   # v1.125: range-honest — no flat-extrapolated far tenors
        agg["cm%d" % t] = round(v, 5) if v is not None else None
    s30 = interp_cm_ranged(sk_pts, 30)
    agg["skew30"] = round(s30, 2) if s30 is not None else None
    return agg


BACKFILL_RPM = 250   # nominal key cap 600/min (user 2026-07-13), but EMPIRICALLY a paced 450/min run
                     # still tripped the throttle after ~520 calls (~70s) — the sustained cap behaves
                     # like ~300/min, so we cruise at 250 with margin. Raise only after a full clean run.
_PACE = {"next": 0.0, "lock": threading.Lock()}


def _pace(rpm=None, _time=time):
    """Global request pacer: spaces request STARTS at 60/rpm seconds so the whole 6-worker
    backfill volley stays under the key's per-minute cap (unpaced, ~6 in-flight × sub-second
    responses hovered right at the 600/min limit and tripped AV's throttle mid-universe).
    Shared across threads via the module lock; `_time` is injectable for the offline test."""
    interval = 60.0 / (rpm or BACKFILL_RPM)
    with _PACE["lock"]:
        now = _time.monotonic()
        wait = _PACE["next"] - now
        _PACE["next"] = max(now, _PACE["next"]) + interval
    if wait > 0:
        _time.sleep(wait)


def backfill(symbol, dates, key, fetch=None, workers=6, progress=None, empties=None, closes=None):
    """Fetch + reduce each date concurrently (reduce-on-ingest). Returns aggregates sorted
    by date (non-trading/empty dates dropped). `fetch(symbol,date,key)->contracts` is the
    injectable network seam; `progress(done,total)` is an optional callback. `closes`
    (v1.120.0) = {date: raw close} spot anchors; a date without one falls back to the
    front-window chain estimate — callers with a key pass fetch_daily_closes_raw's map."""
    fetch = fetch or hist_fetch
    closes = closes or {}
    out, done = [], 0
    stop = threading.Event()

    def one(d):
        if stop.is_set():           # a sibling already throttled -> skip the fetch, don't touch the key
            return None
        try:
            _pace()                 # global 450/min budget (the hard key cap is 600/min)
            rows = fetch(symbol, d, key)
        except SystemExit:          # an AV throttle (options.av): flag the stop so queued dates bail
            stop.set()
            raise
        except Exception:           # a transient/network error on ONE date -> drop it (NOT a tombstone)
            return None
        ds = d if isinstance(d, str) else d.isoformat()
        agg = reduce_asof(rows, d, spot_hint=closes.get(ds))
        if agg is None and empties is not None:
            empties.add(d)          # fetch OK but the chain is empty/unusable -> a tombstone candidate
        return agg
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
    futs = {ex.submit(one, d): d for d in dates}
    try:
        for fut in concurrent.futures.as_completed(futs):
            try:
                agg = fut.result()
            except SystemExit:      # throttled: stop, cancel the still-queued dates (don't drain the key)
                stop.set()
                for f in futs:
                    f.cancel()
                raise _Throttled(sorted(out, key=lambda a: a["date"]))
            done += 1
            if progress:
                progress(done, len(dates))
            if agg:
                out.append(agg)
    finally:
        for f in futs:
            f.cancel()
        ex.shutdown(wait=False)
    return sorted(out, key=lambda a: a["date"])


def _rank_of(xs):
    """IV rank + percentile of the LATEST value in a value list (holes/None dropped from the
    BANDS). v1.126: `cur` is strictly the latest entry — when the latest day carries a hole
    (legacy sentinel day, or a post-heal thinned chain) the rank is UNKNOWABLE and returns
    None, never an OLDER day's value promoted to 'current'.
    rank = (cur−min)/(max−min); percentile = share of days strictly below cur. None if <2 usable."""
    if not xs or xs[-1] is None:
        return None
    cur = xs[-1]
    xs = [x for x in xs if x is not None]
    if len(xs) < 2:
        return None
    lo, hi = min(xs), max(xs)
    rank = 0.0 if hi == lo else (cur - lo) / (hi - lo) * 100
    pct = sum(1 for v in xs if v < cur) / len(xs) * 100
    sv = sorted(xs)
    med = sv[len(sv) // 2] if len(sv) % 2 else (sv[len(sv) // 2 - 1] + sv[len(sv) // 2]) / 2
    return {"cur": cur, "min": lo, "max": hi, "med": med, "rank": round(rank, 1),
            "pct": round(pct, 1), "n": len(xs)}


def iv_rank(hist, field="cm30"):
    """IV rank + percentile of the latest value vs the field's history."""
    return _rank_of([h.get(field) for h in hist])


def iv_rank_at(hist, tenor):
    """IV rank for a constant-maturity `tenor`: the stored cm30/60/90 field when present, else the tenor is
    interpolated per date from that day's series (_cm_at) so 120d (never stored) still ranks — sub-floor
    sentinels already dropped by _iv_points. None if too thin."""
    field = "cm%d" % tenor
    if any(h.get(field) is not None for h in hist):
        return _rank_of([h.get(field) for h in hist])
    return _rank_of([_cm_at(h.get("series") or [], tenor) for h in hist])


def vol_cone(hist, tenors=CONE_TENORS):
    """Per-tenor percentile bands (min/p25/median/p75/max) of const-maturity ATM IV over
    history, plus the current value — each date's stored `series` interpolated to the grid."""
    def pctile(v, q):
        if not v:
            return None
        v = sorted(v)
        i = q * (len(v) - 1)
        lo = int(i)
        return v[lo] if lo == i or lo + 1 >= len(v) else v[lo] + (v[lo + 1] - v[lo]) * (i - lo)
    cone = []
    for t in tenors:
        raw = [_cm_at(h["series"], t) for h in hist]   # v1.125: range-honest (was interp_cm — extrapolated)
        col = [c for c in raw if c is not None]
        if not col:
            continue
        cone.append({"tenor": t, "min": pctile(col, 0), "p25": pctile(col, .25),
                     "med": pctile(col, .5), "p75": pctile(col, .75),
                     # cur = the LATEST day's honest value — None when today's chain doesn't reach
                     # this tenor (v1.125 review P3: col[-1] silently promoted an OLDER day to "cur")
                     "max": pctile(col, 1), "cur": raw[-1]})
    return cone


def _pct(v):
    return "n/a" if v is None else "%.1f%%" % (v * 100)


def _downsample_idx(n, maxn):
    """Indices of an evenly-spaced subset of size ≤ maxn that ALWAYS keeps the last point —
    caps the term-through-time heatmap so a long history stays a few dozen columns, not 60+."""
    if n <= maxn:
        return list(range(n))
    k = -(-n // maxn)   # ceil(n / maxn)
    return sorted(range(n - 1, -1, -k))


def _axis_labels(dates):
    """Adaptive x labels for the line charts: MM-DD when the window is short, else a month name
    at each month's first point (with a 'YY tag in January / at the start) and blanks between —
    so a long window reads as Jul · Aug · Sep… instead of a cramped wall of dates."""
    if len(dates) <= 26:
        return [d[5:] for d in dates]
    mon = ("", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    # Past ~16 months a label at EVERY month-start is ~24 ticks forced at 45° -> they overlap. Thin to
    # QUARTERLY (Jan/Apr/Jul/Oct) on long spans; keep monthly on ≤~16-month windows (the switcher's 6M/3M).
    span = (dt.date.fromisoformat(dates[-1]) - dt.date.fromisoformat(dates[0])).days
    quarterly = span > 480
    out, prev, labeled = [], None, False
    for d in dates:
        ym = d[:7]
        newmon = ym != prev
        if newmon:
            prev = ym
        m = int(d[5:7])
        if newmon and (not quarterly or m % 3 == 1):   # quarterly = months 1/4/7/10 only
            out.append(mon[m] + ("'" + d[2:4] if (m == 1 or not labeled) else ""))
            labeled = True
        else:
            out.append("")
    return out


def _cadence_gap(dates):
    """Min day-gap for the uniform LINE-chart cadence, chosen by the window's own span: keep DAILY
    detail on short windows (≤45d), collapse the mixed weekly+recent-daily store to a uniform ~weekly
    cadence on wide ones — so a categorical time axis reads honestly (no stretched daily tail; the
    window control gives the daily view when wanted)."""
    if len(dates) < 2:
        return 1
    span = (dt.date.fromisoformat(dates[-1]) - dt.date.fromisoformat(dates[0])).days
    return 1 if span <= 45 else (3 if span <= 130 else 7)


def _even_cadence(hist, min_gap_days):
    """Subset of `hist` whose consecutive dates are ≥ min_gap_days apart — anchored on the MOST RECENT
    point (always kept) and walking back, so the recent daily tail collapses to the same cadence as the
    older weekly points. Chart display only; the stats (iv_rank / cone) still use the FULL history. The
    endpoints AND each CM tenor's (30/60/90d) min/max days are FORCE-kept, so every plotted line reaches the
    range its rangebar / 'where today sits vs its range' caption advertises (never a line short of its extremes)."""
    if not hist or min_gap_days <= 1:
        return hist
    keep = {0, len(hist) - 1}                          # both endpoints — the axis starts/ends on real dates
    for _f in CM_TENORS:                               # each CM tenor's min/max day: every line (and its rangebar,
        cm = [(h["cm%d" % _f], i) for i, h in enumerate(hist) if h.get("cm%d" % _f) is not None]   # now 30d + 90d)
        if cm:                                         # must touch its own advertised extremes (never a line short of its range)
            keep.add(min(cm)[1])
            keep.add(max(cm)[1])
    last = dt.date.fromisoformat(hist[-1]["date"])
    for i in range(len(hist) - 2, -1, -1):
        d = dt.date.fromisoformat(hist[i]["date"])
        if (last - d).days >= min_gap_days:            # the regular cadence between the forced-keep points
            keep.add(i)
            last = d
    return [hist[i] for i in sorted(keep)]


def _display_hist(hist):
    """The uniform-cadence display subset both CM-line charts ride (Overview + History) — factored so the two
    producers can't drift on the 'no stretched daily tail' rule. Stats (iv_rank/cone) still use the FULL hist."""
    return _even_cadence(hist, _cadence_gap([h["date"] for h in hist]))


_EVENT_VLINE_CAP = 12   # densest calendar that still DRAWS: quarterly earnings fit (≤8 marks on a
# 2y store); a WEEKLY calendar (EIA — ~52 report days/yr, the CL user catch 2026-07-23) would
# wallpaper the CM-IV chart, so anything denser draws nothing (the Next-event card is separate).


def _earn_idx(dates, earnings):
    """Past earnings dates (from `earnings['past']`) -> the nearest index on this `dates` axis, in
    range only. Powers the red dashed 'E' vlines the line renderer draws from a panel's `earnIdx`.
    DENSITY GATE (v1.128): more than _EVENT_VLINE_CAP unique in-range marks -> [] — a weekly
    calendar on a year axis is wallpaper, not signal; a short window's sparse marks still draw,
    and the Next-event card (earnings['next']) never depends on this."""
    out = []
    for ed in (earnings or {}).get("past", []):
        if dates and dates[0] <= ed <= dates[-1]:
            out.append(min(range(len(dates)), key=lambda i: abs(
                (dt.date.fromisoformat(dates[i]) - dt.date.fromisoformat(ed)).days)))
    idx = sorted(set(out))
    return [] if len(idx) > _EVENT_VLINE_CAP else idx


def _evline_note(event_label):
    """The one legend sentence for the red dashed 'E' vlines — shared by spec_history,
    build_overview AND build_history_section so the info text can never drift from the marks."""
    return (" Red dashed 'E' lines mark past %s — IV usually ramps in and crushes after."
            % (event_label if event_label.endswith("s") else event_label + "s"))


def _vrp_card(hist, closes):
    """VRP card (v1.83.0): 30d const-maturity IV minus trailing realized vol, in vol points. Positive =
    options price MORE vol than the stock has delivered (the variance risk premium — the normal state);
    negative = realized is running hotter than implied. None-safe -> n/a. Shared by History + Overview."""
    cur = hist[-1] if hist else {}
    iv30 = cur.get("cm30")
    hv = realized_at(list(closes.items()) if isinstance(closes, dict) else list(closes), cur.get("date", ""), 20) \
        if (closes and cur.get("date")) else None
    vrp = (iv30 * 100 - hv * 100) if (iv30 is not None and hv is not None) else None
    return {"label": "VRP · 30d", "value": ("%+.1f" % vrp) if vrp is not None else "n/a",
            "sub": (["IV %.0f%% vs HV %.0f%%" % (iv30 * 100, hv * 100)] if vrp is not None else None),
            "hint": "Variance risk premium: 30-day constant-maturity IV minus 20-day realized (historical) "
                    "vol, in vol points. Positive = options price more vol than the stock delivered (the "
                    "usual state — sellers earn it); negative = realized is running hotter than implied."}


def spec_history(symbol, hist, cone_tenors=CONE_TENORS, earnings=None, closes=None,
                 subtitle=None, skew_hint=None, event_label="earnings"):
    """Build the --history dashboard SPEC: IV-rank cards, const-maturity IV history, the vol
    cone, 25Δ skew history, PCR history, and the term-through-time heatmap — all from the reduced
    daily aggregates (no raw chain). `earnings` = {'past':[dates], 'next':date} overlays past
    earnings dates on the IV chart and a next-report card. `closes` = {date: adjusted_close} adds a
    realized-vol (HV20) overlay on the IV history + a VRP card (v1.83.0). `subtitle`/`skew_hint`
    (v1.114) swap the two PROVIDER-specific strings for non-AV record producers — futhist's
    Databento/Black-76 records would otherwise render under an "AV-native IV" claim and the skew
    hint's equity-shape prior (energy is often call-skewed); defaults = the AV strings, so equity
    pages are byte-unchanged."""
    sym = symbol.upper()
    # Chart DISPLAY rides a uniform-cadence subset (no stretched daily tail on a categorical time axis);
    # the STATS below (iv_rank / cone / current) still use the FULL history so min/max/percentile are honest.
    disp = _display_hist(hist)
    dates = [h["date"] for h in disp]
    labels = _axis_labels(dates)          # adaptive month labels on long windows (the line charts)
    earn_idx = _earn_idx(dates, earnings)   # past earnings -> nearest axis index (the red dashed 'E' vlines)
    hm_hist = [disp[i] for i in _downsample_idx(len(disp), 32)]   # cap the heatmap at ~32 columns (cells are compact)
    hm_cols = [h["date"][5:] for h in hm_hist]                    # MM-DD per (sampled) column
    cm = {t: [(round(h.get("cm%d" % t) * 100, 2) if h.get("cm%d" % t) is not None else None) for h in disp]
          for t in CM_TENORS}
    r = iv_rank(hist) or {}
    cone = vol_cone(hist, cone_tenors)
    cur = hist[-1]
    # the term-through-time heatmap rides its OWN denser tenor ladder (out to 270d — the settlement chain
    # reaches far past the 120d cone grid); a tenor beyond a day's longest listing stays a hole (not extrapolated)
    hm_rows = ["%dd" % t for t in HM_TENORS]
    hm_matrix = [[(lambda v: round(v * 100, 1) if v is not None else None)(
        _cm_at(h["series"], t)) for h in hm_hist] for t in HM_TENORS]
    cards = [
        {"label": "IV rank · 30d", "value": ("%.0f%%" % r["rank"]) if r else "n/a",
         "hint": "Where 30d ATM IV sits in its own history range (min→max). Needs enough history to mean something."},
        {"label": "IV percentile", "value": ("%.0f%%" % r["pct"]) if r else "n/a",
         "hint": "Share of history days whose 30d ATM IV was below today's."},
        {"label": "30d ATM IV", "value": _pct(cur.get("cm30")),
         "hint": "Constant-maturity 30-day ATM IV, interpolated across expirations so it is roll-proof."},
        {"label": "25Δ skew · 30d", "value": (lambda sk: "%+.1f" % sk if sk is not None else "n/a")(
            _plausible_skew(cur.get("skew30"), cur.get("cm30"))),   # legacy sentinel-leg skew (|skew|≈ATM) -> n/a
         "hint": skew_hint or "IV(25Δ put) − IV(25Δ call), vol points. Positive = downside puts bid "
                              "(the usual equity shape)."},
        {"label": "IV range", "value": ("%.0f–%.0f%%" % (r["min"] * 100, r["max"] * 100)) if r else "n/a",
         "hint": "Min–max of 30d ATM IV across the shown history."},
    ]
    if closes:   # v1.83.0 VRP card leads with the cheap/rich-vs-realized read
        cards.append(_vrp_card(hist, closes))
    if earnings and earnings.get("next"):
        nd = earnings["next"]
        try:
            edte = (dt.date.fromisoformat(nd) - dt.date.fromisoformat(dates[-1])).days
        except (ValueError, IndexError):
            edte = None
        cards.append({"label": "Next " + event_label, "value": nd[5:],
                      "hint": "Next report %s%s — front-month IV usually ramps into it, then crushes after." % (
                          nd, (" (~%dd out)" % edte) if edte is not None else "")})
    panels = [
        {"type": "line", "title": "Constant-maturity ATM IV history", "full": True, "info":
            "30/60/90-day constant-maturity ATM implied vol over time (settlement history). Each point is "
            "interpolated to a fixed tenor, so the series stays comparable as specific expirations roll off."
            + (_evline_note(event_label) if earn_idx else "")
            + (" Grey dashed = 20-day realized (historical) vol — the gap to implied is the variance risk premium." if closes else ""),
         "data": {"labels": labels, "xall": True, "earnIdx": earn_idx, "series": [
             {"name": "30d", "values": cm[30]},
             {"name": "60d", "values": cm[60], "dash": [6, 4]},
             {"name": "90d", "values": cm[90], "dash": [2, 3]}]
             + ([{"name": "realized HV20", "values": _realized_series(closes, dates, 20), "color": "#8a8f98", "dash": [3, 3]}]
                if closes else [])}},
        {"type": "line", "full": True, "title": "Volatility cone", "info":
            "Percentile bands of const-maturity ATM IV by tenor over the shown history (min / p25 / median / "
            "p75 / max) with the current value. Current above the median = vol rich for that tenor.",
         "data": {"labels": [c["tenor"] and ("%dd" % c["tenor"]) for c in cone], "series": [
             {"name": "max", "values": [round(c["max"] * 100, 1) for c in cone], "dash": [2, 3]},
             {"name": "p75", "values": [round(c["p75"] * 100, 1) for c in cone]},
             {"name": "median", "values": [round(c["med"] * 100, 1) for c in cone]},
             {"name": "p25", "values": [round(c["p25"] * 100, 1) for c in cone]},
             {"name": "min", "values": [round(c["min"] * 100, 1) for c in cone], "dash": [2, 3]},
             {"name": "current", "values": [round(c["cur"] * 100, 1) if c["cur"] is not None else None
                                            for c in cone]}]}},   # None = an honest hole at that tenor
        {"type": "line", "title": "25Δ skew history · 30d", "info":   # half-width: pairs with PCR on wide screens
            "30-day 25Δ skew over time, vol points. Rises when downside protection gets bid — usually on selloffs.",
         "data": {"labels": labels, "xall": True, "series": [{"name": "25Δ skew (vol pts)",
                  "values": [_plausible_skew(h.get("skew30"), h.get("cm30")) for h in disp]}]}},   # legacy sentinel-leg skew -> holes
        {"type": "line", "title": "Put/call ratio history", "info":   # half-width: pairs with 25Δ skew on wide screens
            "Near-dated (≤90d) put/call ratio by open interest per session (put OI ÷ call OI, from the "
            "settlement chain — far LEAPS call walls are excluded so the ratio tracks tradeable sentiment). "
            "Above 1 = more put than call OI (hedging/bearish tilt); call-heavy names sit below 1.",
         "data": {"labels": labels, "xall": True, "series": [{"name": "PCR (OI)", "values": [
             (round(h["pcr"], 3) if h.get("pcr") is not None else None) for h in disp]}]}},
        dashboard.add_hm_color_toggle({"type": "heatmap", "full": True,
            "title": "Term structure through time — ATM IV by tenor", "info":
            "Const-maturity ATM IV (%) by tenor (rows) across dates (cols) — the time twin of the snapshot IV "
            "surface: how the term structure moved, not the smile across strikes. Hotter = richer vol. Toggle "
            "'log' colour to spread the low/mid band when a vol spike dominates the range.",
         "data": {"rows": hm_rows, "cols": hm_cols, "matrix": hm_matrix, "ramp": "seq"}}),
    ]
    return {
        "title": "%s options · volatility history · %d sessions" % (sym, len(hist)),
        "subtitle": ("%s · as of %s · NOT live · data analysis, not advice"
                     % (subtitle or "Alpha Vantage HISTORICAL_OPTIONS settlement history · "
                                    "AV-native IV (never recomputed)",
                        hist[-1]["date"] if hist else "n/a")),
        "cards": cards, "panels": panels}


def build_overview(hist, earnings=None, closes=None, skew_hint=None, chain_note=None,
                   event_label="earnings"):
    """The Overview SECTION for the combined --full page: the single most important read from each
    mode, assembled from the settlement history store (CACHE-FIRST — no live pull). IV rank / range
    from the CM-30 history; front ATM IV + 25Δ skew + term slope + PCR from the latest settlement
    `series`; next earnings; `closes` (daily adjusted closes) adds a VRP card (v1.83.0). Panels: the
    const-maturity IV history (where today sits vs its range)
    and a mini ATM-IV term curve. Returns {id,name,cards,panels}. Assumes a warm store; a cold name
    is handled by the caller (a 'warming' Overview is not built here). `skew_hint`/`chain_note`
    (v1.114): the futures --full page swaps the two provider-specific strings — its skew is the
    ~10%-OTM moneyness proxy, and its Chain/Term tabs are SETTLEMENTS (the default chain_note's
    "LIVE front→tail" claim would break the delayed-is-never-live rule there); defaults = the AV
    strings, equity pages byte-unchanged."""
    r = iv_rank(hist) or {}
    cur = hist[-1] if hist else {}
    series = sorted(cur.get("series", []), key=lambda s: s.get("dte", 0))
    front = series[0] if series else {}
    # Term slope needs TWO usable (real ATM IV) expirations: pick the nearest and the farthest with a real ATM
    # IV. A single expiration, a far-wing None hole, OR a legacy AV-sentinel (0.01488) must read n/a — never a
    # fake "flat". plausible_iv() guards a legacy store whose stored `series` still carries the sentinel raw.
    usable = [s for s in series if options.plausible_iv(s.get("atmIV")) is not None]
    fatm = options.plausible_iv(front.get("atmIV"))
    if len(usable) >= 2:
        f2, b2 = usable[0]["atmIV"], usable[-1]["atmIV"]
        slope_lbl = "flat" if abs(f2 - b2) < 5e-4 else ("front-rich" if f2 > b2 else "back-rich")
        slope_hint = ("Settlement history: nearest %s vs farthest %s ATM IV. Front-rich = a near-term event / "
                      "backwardation; back-rich = contango. %s"
                      % (_pct(f2), _pct(b2),
                         chain_note or "(Chain/Term show the LIVE front→tail slope in vol pts.)"))
        slope_sub = ["%s → %s" % (_pct(f2), _pct(b2))]   # the concrete nearest→farthest ATM IV behind the label
    else:
        slope_lbl, slope_hint, slope_sub = "n/a", "Not enough expirations with a usable ATM IV for a term slope.", None
    cards = [
        # IV rank leads; the percentile rides as its sub-line (one tile, not two — user 2026-07-08).
        {"label": "IV rank · 30d", "value": ("%.0f%%" % r["rank"]) if r else "n/a",
         "sub": (["%.0f%% percentile" % r["pct"]] if r else None),
         "hint": "Where 30d ATM IV sits in its own history range (min→max) — the cheap/rich read. The sub-line is "
                 "the percentile (share of history days below today). Full drill: History tab."},
        {"label": "ATM IV · front", "value": _pct(fatm), "scope": "expiry",
         "scopeLabel": ("nearest · %dd DTE" % front["dte"]) if front.get("dte") is not None else "nearest expiry",
         "hint": "At-the-money implied vol of the nearest expiration (settlement). Full smile: Chain tab."},
        {"label": "25Δ skew · front", "value": (lambda sk: "%+.1f" % sk if sk is not None else "n/a")(
            _plausible_skew(front.get("skew25"), front.get("atmIV"))),   # legacy sentinel-leg skew (|skew|≈ATM) -> n/a
         "scope": "expiry",
         "scopeLabel": ("nearest · %dd DTE" % front["dte"]) if front.get("dte") is not None else "nearest expiry",
         "hint": skew_hint or "IV(25Δ put) − IV(25Δ call) of the nearest expiration, vol points. "
                              "Positive = downside puts bid."},
        {"label": "Term slope · settlement", "value": slope_lbl, "sub": slope_sub, "hint": slope_hint},
        {"label": "PCR · OI", "value": ("%.2f" % cur["pcr"]) if cur.get("pcr") is not None else "n/a",
         "hint": "Near-dated (≤90d) put/call ratio by open interest (settlement; far LEAPS call walls "
                 "excluded). >1 = more put than call OI. Trend: History tab."},
    ]
    if closes:   # v1.83.0 VRP: is implied rich vs what the stock actually delivered
        cards.append(_vrp_card(hist, closes))
    if earnings and earnings.get("next"):
        nd = earnings["next"]
        try:   # a VALID ISO date or nothing — a malformed 'next' ("TBD", "2026-13-40") paints no garbage tile
            nds = dt.date.fromisoformat(str(nd))
        except (ValueError, TypeError):
            nds = None
        if nds is not None:
            try:
                edte = (nds - dt.date.fromisoformat(cur["date"])).days
            except (ValueError, KeyError, TypeError):
                edte = None
            cards.append({"label": "Next " + event_label, "value": nd[5:],
                          "hint": "Next report %s%s — front-month IV usually ramps in, then crushes after." % (
                              nd, (" (~%dd out)" % edte) if edte is not None else "")})
    disp = _display_hist(hist)   # uniform cadence: no stretched daily tail
    cm = {t: [(round(h.get("cm%d" % t) * 100, 2) if h.get("cm%d" % t) is not None else None) for h in disp]
          for t in CM_TENORS}
    _dates = [h["date"] for h in disp]
    labels = _axis_labels(_dates)                 # adaptive month labels — never a wall of raw ISO dates
    earn_idx = _earn_idx(_dates, earnings)        # red dashed 'E' vlines — IV spikes line up with earnings
    # Default the CM-IV chart to the TRAILING DENSE span (from the first display point that carries a CM
    # value). A dense equity store has cm from point 0 -> the whole series (unchanged). A RECONSTRUCTED
    # store (MOEX) has a sparse leading tail (early records span only far tenors -> cm None); cropping the
    # empty left shows the dense ~history the +/- can still expand back to All. (the constant-maturity IV
    _cm_first = next((i for i, v in enumerate(cm[30]) if v is not None), 0)
    _cm_defwin = max(1, len(cm[30]) - _cm_first)   # trailing dense span, in display points
    panels = []
    if r:   # IV-range bars: where each tenor's CM ATM IV sits in its own settlement-history min→median→max.
        _rbars = []
        for _t in RANGEBAR_TENORS:
            rr = iv_rank_at(hist, _t)
            if rr:
                _rbars.append({"label": "%dd" % _t, "min": round(rr["min"] * 100, 1), "max": round(rr["max"] * 100, 1),
                               "median": round(rr["med"] * 100, 1), "cur": round(rr["cur"] * 100, 1),
                               "rank": rr["rank"], "pct": rr["pct"]})
        panels.append(
            {"type": "rangebar", "full": True, "title": "ATM IV — where it sits in its range · by tenor", "info":
                "Each tenor's constant-maturity ATM IV against its own full settlement-history range: left = the "
                "range low (cheap), right = the high (rich); the dot is where today sits (rank %). The IV-rank card "
                "is the 30d read; the History tab has the full vol cone. Hover a dot for its range / percentile.",
             "data": {"unit": "%", "bars": _rbars}})
    panels += [
        {"type": "line", "full": True, "title": "Constant-maturity ATM IV — where today sits vs its range",
         "zoom": {"min": 8, "step": 13, "def": _cm_defwin},   # default = trailing dense span; +/- windows further; ↺ resets to it
         "info":
            "30/60/90-day constant-maturity ATM IV over the settlement history. Today's level against this range "
            "is the cheap/rich read (see the IV-rank card)."
            + (_evline_note(event_label) if earn_idx else "")
            + " +/- zoom into the recent window; the full vol-history dashboard (All/1Y/… control) is the History tab.",
         "data": {"labels": labels, "xall": True, "earnIdx": earn_idx, "series": [
             {"name": "30d", "values": cm[30]},
             {"name": "60d", "values": cm[60], "dash": [6, 4]},
             {"name": "90d", "values": cm[90], "dash": [2, 3]}]}},
    ]   # the "today" ATM-IV term curve lives on the Term tab (de-duped — it owns the cross-expiration view)
    return {"id": "overview", "name": "Overview", "cards": cards, "panels": panels}


WINDOWS = (("All", None), ("1Y", 365), ("6M", 182), ("3M", 91), ("1M", 31))


def _window_slices(hist):
    """[(label, hist-subset)] for the dashboard-level window control — trailing calendar
    windows off the latest date. Windows with <3 sessions (too short for a cone) are dropped."""
    if not hist:
        return []
    end = dt.date.fromisoformat(hist[-1]["date"])
    out = []
    for label, days in WINDOWS:
        sl = hist if days is None else [h for h in hist
                                        if h["date"] >= (end - dt.timedelta(days=days)).isoformat()]
        if len(sl) >= 3:
            out.append((label, sl))
    return out


def render_history_html(symbol, hist, earnings=None, closes=None, subtitle=None, skew_hint=None,
                        event_label="earnings"):
    """The --history page with a SINGLE dashboard-level window control. Every window's full
    spec is pre-computed in Python (IV history / skew / heatmap sliced, cone + IV-rank cards
    RE-COMPUTED over that window); a control bar swaps the active spec and calls the dashboard's
    own renderShell() — so all panels AND cards stay in sync (the per-panel zoom couldn't).
    `closes` (daily adjusted closes) adds the realized-vol overlay + VRP card (v1.83.0)."""
    slices = _window_slices(hist)
    base = dashboard.render_interactive_html(spec_history(symbol, hist, earnings=earnings, closes=closes,
                                                          subtitle=subtitle, skew_hint=skew_hint,
                                                          event_label=event_label))
    if len(slices) <= 1:
        return base
    winspecs = [{"label": lab, "spec": spec_history(symbol, sl, earnings=earnings, closes=closes,
                                                    subtitle=subtitle, skew_hint=skew_hint,
                                                    event_label=event_label)} for lab, sl in slices]
    bar = ('<div class="winbar"><span class="winlbl">window</span>'
           + "".join('<button class="%s" onclick="applyWindow(%d)">%s</button>'
                     % ("on" if i == 0 else "", i, w["label"]) for i, w in enumerate(winspecs))
           + '</div>')
    style = ('<style>.winbar{display:flex;gap:6px;align-items:center;margin:0 0 14px;flex-wrap:wrap}'
             '.winbar .winlbl{font-size:12px;color:#8a8f98;margin-right:4px}'
             '.winbar button{font:inherit;font-size:13px;padding:3px 12px;border-radius:6px;'
             'border:1px solid #777;background:transparent;color:inherit;cursor:pointer}'
             '.winbar button.on{background:rgba(55,138,221,.25);color:inherit;font-weight:600;border-color:#378ADD}</style>')
    payload = json.dumps(winspecs, separators=(",", ":")).replace("<", "\\u003c").replace(">", "\\u003e")
    script = ('<script>const WINSPECS=%s;let _win=0;'
              'function applyWindow(i){if(i===_win||!WINSPECS[i])return;_win=i;SPEC=WINSPECS[i].spec;'
              'renderShell();document.querySelectorAll(".winbar button")'
              '.forEach((b,j)=>b.classList.toggle("on",j===i));}</script>') % payload
    mark = '<div class="grid" id="grid">'
    if mark not in base or "</body>" not in base:   # engine template changed -> fail LOUD, don't silently drop the control
        print("warning: dashboard template markers changed — --history window control not injected "
              "(page still renders; fix render_history_html)", file=sys.stderr)
        return base
    html = base.replace(mark, style + bar + mark, 1)
    return html.replace("</body>", script + "</body>", 1)


def build_history_section(symbol, hist, earnings=None, closes=None, subtitle=None, skew_hint=None,
                          event_label="earnings"):
    """The History SECTION for the combined --full page: the standalone --history dashboard
    (spec_history) PLUS the same trailing-window control the standalone page has (All/1Y/6M/3M/1M),
    modeled as a SECTION-SCOPED switcher so it swaps client-side exactly like the Smile expiration
    switcher (dashboard mountSwitcher/switchSeries). Every window's spec is pre-built in Python
    (panels sliced, cone + IV-rank cards RE-COMPUTED over that window); the switcher zips them per
    panel/card. <2 usable windows -> a plain section (no control), same as render_history_html.
    `closes` adds the realized-vol overlay + VRP card (v1.83.0)."""
    slices = _window_slices(hist)
    if len(slices) <= 1:
        sp = spec_history(symbol, hist, earnings=earnings, closes=closes,
                          subtitle=subtitle, skew_hint=skew_hint, event_label=event_label)
        return {"id": "history", "name": "History", "cards": sp["cards"], "panels": sp["panels"],
                "subtitle": sp.get("subtitle")}
    wspecs = [spec_history(symbol, sl, earnings=earnings, closes=closes,
                           subtitle=subtitle, skew_hint=skew_hint,
                           event_label=event_label) for _, sl in slices]
    panels = []
    for j, bp in enumerate(wspecs[0]["panels"]):   # panel STRUCTURE is identical across windows; DATA swaps
        p = dict(bp)
        p["series"] = [w["panels"][j]["data"] for w in wspecs]
        # v1.128 gate follow-up (review P3): the info is PINNED to the All window while earnIdx
        # swaps per window — the density gate can zero the All-window marks while a SHORT window
        # (1M ≈ 4 weekly events ≤ cap) still draws its sparse ones, which would then render
        # legend-less. If ANY window carries marks, the legend sentence rides the pinned info.
        if any((w["panels"][j]["data"] or {}).get("earnIdx") for w in wspecs) \
                and _evline_note(event_label) not in (p.get("info") or ""):
            p["info"] = (p.get("info") or "") + _evline_note(event_label)
        panels.append(p)
    return {"id": "history", "name": "History", "cards": wspecs[0]["cards"], "panels": panels,
            "cardSeries": [w["cards"] for w in wspecs], "subtitle": wspecs[0].get("subtitle"),
            "switcher": {"label": "Window", "index": 0, "options": [lab for lab, _ in slices],
                         "subtitles": [w.get("subtitle", "") for w in wspecs],
                         "hint": "trailing window — IV rank, the cone & the charts recompute over it"}}


def _store_path(symbol):
    return os.path.join(CACHE_DIR, "%s.hist.json" % symbol.upper())


def _load_packaged_store_bytes(member_name, archive=None, max_member=_PACKAGED_STORE_MAX_MEMBER):
    """Read one bounded regular-file member from the immutable ``store.tgz`` seed.

    Callers name an exact member; traversal, platform separators, links, and oversized entries are
    refused.  This byte layer also serves compressed futures definition snapshots, while the JSON
    wrapper below keeps the historical-store contract unchanged.
    """
    member_name = str(member_name or "")
    parts = member_name.split("/")
    if (not member_name or len(member_name) > 240 or "\\" in member_name
            or any(not part or part in (".", "..")
                   or not re.fullmatch(r"[A-Za-z0-9._-]+", part) for part in parts)):
        return None
    tgz = archive or PACKAGED_STORE_TGZ
    if not os.path.isfile(tgz):
        return None
    try:
        import tarfile
        with tarfile.open(tgz, "r:gz") as tf:
            member = tf.getmember(member_name)
            if (not member.isfile() or member.size < 0 or member.size > max_member
                    or member.issym() or member.islnk()):
                return None
            stream = tf.extractfile(member)
            if stream is None:
                return None
            with stream:
                data = stream.read(max_member + 1)
        return data if len(data) <= max_member else None
    except (OSError, KeyError, ValueError, tarfile.TarError):
        return None


def _load_packaged_store_json(kind, name, archive=None):
    """Read one immutable warm-store JSON seed directly from ``store.tgz``.

    ChatGPT/Codex can mount both the installed skill and its task home read-only.  The packaged
    archive is therefore a real read layer, not merely an installer that must first materialize
    beside the scripts.  A writable CACHE_DIR remains the overlay and always wins in load_store.
    """
    kind = str(kind or "")
    name = str(name or "").upper()
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", kind) \
            or not re.fullmatch(r"[A-Z0-9][A-Z0-9._-]{0,63}", name):
        return None
    member_name = "%s/%s.hist.json" % (kind, name)
    try:
        raw = _load_packaged_store_bytes(member_name, archive=archive)
        return json.loads(raw.decode("utf-8")) if raw is not None else None
    except (UnicodeError, ValueError):
        return None


def _desentinel(hist):
    """Legacy stores (reduced before the sentinel guard) baked AV's 0.01488 into the per-day `series` AND into
    the stored const-maturity cm30/60/90 — as interpolation BLENDS (sentinel leg + a real leg) that a plausibility
    filter can't catch. Stored aggregates are never re-fetched (update_store only appends missing days), and
    iv_rank/iv_rank_at PREFER the stored field — so recompute cmXX from the CLEANED series (_iv_points drops the
    sentinel) for exactly the days whose series still carry it. Clean days are left byte-untouched. Mutates + returns."""
    for h in hist:
        if not isinstance(h, dict):   # a corrupt store element: leave it to the consumers' guards
            continue
        try:
            _desentinel_one(h)
        except (TypeError, ValueError):
            continue   # a corrupt record (non-numeric dte/IV) must not kill EVERY load (v1.125)
    return hist


def _desentinel_one(h):
    """One record's read-heal (see _desentinel): sentinel recompute + the v1.125 range gate."""
    series = h.get("series") or []
    pts = _iv_points(series)
    if any(s.get("atmIV") is not None and options.plausible_iv(s.get("atmIV")) is None for s in series):
        for t in CM_TENORS:
            v = interp_cm_ranged(pts, t)
            h["cm%d" % t] = round(v, 5) if v is not None else None
    elif pts:
        # v1.125: pre-fix stores FLAT-EXTRAPOLATED cmXX past the day's listed-DTE span (a thin
        # name with one ~30d series stored cm30=cm60=cm90 — a fabricated 'flat' that fed
        # term_shape/rank/history while the heatmap's _cm_at said None). Null the out-of-range
        # tenors on read; in-range values stay byte-identical. Same heal for skew30 below.
        lo, hi = min(d for d, _ in pts), max(d for d, _ in pts)
        for t in CM_TENORS:
            if h.get("cm%d" % t) is not None and not (lo - _CM_EDGE_TOL <= t <= hi + _CM_EDGE_TOL):
                h["cm%d" % t] = None
    skp = [s.get("dte") for s in series
           if s.get("skew25") is not None and s.get("dte") is not None]
    if h.get("skew30") is not None and skp and not (min(skp) - _CM_EDGE_TOL <= 30 <= max(skp) + _CM_EDGE_TOL):
        h["skew30"] = None


def load_store(symbol):
    try:
        with open(_store_path(symbol), encoding="utf-8") as f:
            return _desentinel(json.load(f))   # heal legacy sentinel-poisoned cmXX on read (no re-fetch)
    except (OSError, ValueError):
        bundled = _load_packaged_store_json("options", symbol)
        return _desentinel(bundled) if isinstance(bundled, list) else []


def save_store(symbol, hist):
    os.makedirs(CACHE_DIR, exist_ok=True)
    # v1.125: write_atomic's UNIQUE tmp — a fixed <SYM>.hist.json.tmp let two simultaneous
    # savers (heal + prewarm on one symbol) interleave into one tmp and os.replace torn JSON
    # (load_store would then read [] and the next prewarm re-backfilled the whole name)
    dashboard.write_atomic(_store_path(symbol), json.dumps(hist))


def _tomb_path(symbol):
    return os.path.join(CACHE_DIR, "%s.empty.json" % symbol.upper())


def _tomb_load(symbol):
    """Dates known to be empty (a market holiday, or a genuinely dead session) — fetched once and
    remembered so the weekly/monthly grid doesn't re-pull the same holiday Fridays every run."""
    try:
        with open(_tomb_path(symbol), encoding="utf-8") as f:
            return set(json.load(f))
    except (OSError, ValueError):
        return set()


def _tomb_add(symbol, dates):
    """Best-effort — a read-only cache must never break a render."""
    if not dates:
        return
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = _tomb_path(symbol) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(sorted(_tomb_load(symbol) | set(dates)), f)
        os.replace(tmp, _tomb_path(symbol))
    except OSError:
        pass


# ---------------- Phase 2: skeleton cache (FIRST QUESTION) + prewarm ----------------
def _fresh(asof_iso, max_days):
    try:
        return (dt.date.today() - dt.date.fromisoformat(asof_iso)).days <= max_days
    except (ValueError, TypeError):
        return False


def _skel_path(symbol):
    return os.path.join(CACHE_DIR, "%s.skel.json" % symbol.upper())


def skel_from_contracts(contracts):
    """Reduce a REALTIME_OPTIONS chain to STRUCTURE only — {expirations:[{date, nstrikes}]} sorted by
    date. Structure changes slowly, so this is the long-lived FIRST-QUESTION cache (the AV analogue of
    the Databento defs-catalog): re-serves --list-expiries without re-pulling the ~1 MB chain."""
    by_exp = {}
    for c in contracts:
        e, k = c.get("expiration"), ff(c.get("strike"))
        if e and k is not None:
            by_exp.setdefault(e, set()).add(k)
    return {"expirations": [{"date": e, "nstrikes": len(ks)} for e, ks in sorted(by_exp.items())]}


def skel_load(symbol):
    try:
        with open(_skel_path(symbol), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def skel_save(symbol, skel):
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = _skel_path(symbol) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(skel, f)
    os.replace(tmp, _skel_path(symbol))


def refresh_skeleton(symbol, key, fetch_contracts=None):
    """Pull the current chain and persist its structure with today's asOf. Returns the skeleton, or
    None on an empty chain (dead / non-optionable). fetch_contracts is the injectable network seam."""
    fetch_contracts = fetch_contracts or options._fetch_contracts
    rows = fetch_contracts(symbol, key)
    if not rows:
        return None
    skel = skel_from_contracts(rows)
    skel["symbol"], skel["asOf"] = symbol.upper(), dt.date.today().isoformat()
    skel_save(symbol, skel)
    return skel


def _meta_path(symbol):
    return os.path.join(CACHE_DIR, "%s.meta.json" % symbol.upper())


_PROFILE_FIELDS = ("exchange", "name", "sector", "industry", "description", "fiscalYearEnd", "site", "sharesOutstanding")
_PROFILE_TTL_DAYS = 90   # shares move on buybacks/dilution and blurbs get rewritten — refresh quarterly-ish,
                         # not never (review P2 2026-07-15: "cap = live spot x cached shares" pinned shares
                         # to the FIRST-EVER pull; a hiccup-cached null description never healed)
_PROFILE_NEG_TTL_DAYS = 7   # AV's OVERVIEW is {} for every ETF/fund — cache the MISS too (review P3
                            # 2026-07-15: every fund --full render burned one OVERVIEW call forever);
                            # kept SHORT because a new listing may gain an OVERVIEW


def _search_fund_name(sym, key, search=None):
    """v1.121: the OVERVIEW-less (fund) name — SYMBOL_SEARCH exact-symbol match, smart-cased via
    fundamentals._fund_name (imported FUNCTION-LOCALLY: a top-level import would load a second
    module copy whenever fundamentals runs as __main__ — the options.py dual-copy class — and
    invites a real cycle later; a raw exact match is the guarded fallback). One small call per
    fund per _PROFILE_NEG_TTL_DAYS, cached in the negative marker. `search` is the offline-test
    seam. Returns the name; None when the search genuinely finds no exact match; **False when
    the SEARCH ITSELF failed** (throttle/network/malformed) — callers must never negative-cache
    a failure as "no name" (the v1.120 rule: a throttled pull never lands as clean)."""
    try:
        payload = search() if search else options.av({"function": "SYMBOL_SEARCH", "keywords": sym}, key)
    except (Exception, SystemExit):
        return False
    if not isinstance(payload, dict) or "bestMatches" not in payload:
        # v1.126: a load-shed 200-OK {} (the PEG class) is a FAILURE, not an authoritative
        # no-match — only a PRESENT-but-empty bestMatches list means "this fund has no name
        # here"; anything else must never negative-cache or drop a stale marker's known name.
        return False
    try:
        import fundamentals as _fu
        return _fu._fund_name(payload, sym)
    except Exception:
        try:
            for r in (payload or {}).get("bestMatches") or []:
                if isinstance(r, dict) and (r.get("1. symbol") or "").upper() == sym.upper():
                    return (r.get("2. name") or "").strip() or None
        except Exception:
            return False               # malformed payload = a FAILED search, not a no-match
        return None


def profile_get(symbol, key, fetch=None, search=None):
    """Best-effort issuer profile from AV OVERVIEW — cached per ticker with an asOf stamp; a COMPLETE
    cache within _PROFILE_TTL_DAYS spends no call, a stale/incomplete one re-fetches and HEALS (v1.112).
    Carries {exchange, name, sector, industry} for the header/venue AND (v1.110) {description,
    fiscalYearEnd, site, sharesOutstanding} so the options dashboard's page-level LEAD block shows the
    SAME company blurb + FY/site meta as the fundamentals page (the OVERVIEW payload already holds all
    of it — no extra call). A throttle / bad key / offline returns the old cache (full or partial, or
    {}) — NEVER fatal, the chain pull already succeeded; a DEGRADED success (partial OVERVIEW) merges
    over the old cache instead of nulling known fields. An empty-but-SUCCESSFUL fetch with NO cache at
    all (funds have no OVERVIEW) writes a NEGATIVE marker honored for _PROFILE_NEG_TTL_DAYS, so ETF
    renders stop re-spending the call. `fetch` is the injectable AV seam for tests."""
    sym = symbol.upper()

    def _scrub(d):
        # a v1.110/v1.111-era cache can hold LITERAL "None" strings (written before _s filtered the AV
        # sentinel) — scrub them at load, or the merge below would resurrect them and restamp them fresh
        # (re-review P2 2026-07-15: the sentinel fix was a no-op for exactly the caches that had the bug)
        return {k: (None if isinstance(v, str) and v.strip() in ("", "None", "none", "-") else v)
                for k, v in d.items()}
    old = None
    stale_name = None
    try:
        with open(_meta_path(sym), encoding="utf-8") as f:
            m = json.load(f) or {}
        if m.get("empty"):                     # negative cache: a known OVERVIEW miss (ETF/fund)
            stale_name = m.get("name")         # kept: a throttled search on the TTL refresh below
            try:                               # must not drop a known fund name (v1.125)
                if (dt.date.today() - dt.date.fromisoformat(m["asOf"])).days <= _PROFILE_NEG_TTL_DAYS:
                    # v1.121: the marker may CARRY the fund's SYMBOL_SEARCH name — serve it free.
                    if m.get("name"):
                        return {"name": m["name"]}
                    if not m.get("nameChecked"):   # a LEGACY (pre-v1.121) nameless marker: heal it
                        nm = _search_fund_name(sym, key, search)   # in place with ONE search —
                        if nm is not False:                        # a FAILED search stamps nothing:
                            m["nameChecked"] = True                # the next render retries (v1.120
                            if nm:                                 # rule — failure ≠ no-name)
                                m["name"] = nm
                            try:
                                with open(_meta_path(sym), "w", encoding="utf-8") as f:
                                    json.dump(m, f)                # asOf preserved — no TTL renewal
                            except OSError:
                                pass
                        if nm:
                            return {"name": nm}
                    return {}
            except (KeyError, TypeError, ValueError):
                pass                           # unstamped/corrupt marker -> retry below
        elif all(k in m for k in _PROFILE_FIELDS):   # a COMPLETE cache (every field present, None allowed)
            prof_c = _scrub({k: m.get(k) for k in _PROFILE_FIELDS})
            try:
                fresh = (dt.date.today() - dt.date.fromisoformat(m["asOf"])).days <= _PROFILE_TTL_DAYS
            except (KeyError, TypeError, ValueError):
                fresh = False                  # a pre-v1.112 cache carries no stamp -> stale (heals once)
            if fresh:
                return prof_c
            old = prof_c                       # complete but STALE -> re-fetch; the full old rides on failure
        elif "name" in m:                      # an OLDER (pre-field) cache — reuse if the re-fetch can't run
            old = _scrub({k: m.get(k) for k in _PROFILE_FIELDS})
            if not any(old.values()):          # v1.125: an all-None legacy scrub is NOT a cache — a truthy
                old = None                     # dict here blocked the ETF negative marker forever
                                               # (one OVERVIEW burned per render)
    except (OSError, ValueError):
        pass
    try:
        fetch = fetch or (lambda: options.av({"function": "OVERVIEW", "symbol": sym}, key))
        ov = fetch() or {}
    except (Exception, SystemExit):           # av() sys.exits on a throttle — swallow it: the profile is optional
        # v1.125: a throttled FETCH on a stale NAMED marker still serves the known fund name
        # (the render half of the v1.121-deferred P4; the disk marker is untouched either way)
        return old or ({"name": stale_name} if stale_name else {})

    def _clean(v):
        v = (v or "").strip()
        return (v.title() if v.isupper() else v) or None   # AV Sector is UPPERCASE ("TECHNOLOGY") -> "Technology"
    def _s(v):
        v = (v or "").strip()
        return None if v in ("", "None", "none", "-") else v   # AV's literal "None" sentinel is a hole, not a blurb
    def _n(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    prof = {"exchange": _s(ov.get("Exchange")), "name": _s(ov.get("Name")),
            "sector": _clean(ov.get("Sector")), "industry": _clean(ov.get("Industry")),
            "description": _s(ov.get("Description")), "fiscalYearEnd": _s(ov.get("FiscalYearEnd")),
            "site": _s(ov.get("OfficialSite")),
            "sharesOutstanding": _n(ov.get("SharesOutstanding"))}
    if not any(prof.values()):                # EMPTY success: with a cache, it rides and the TTL is NOT renewed
        if not old:                           # (transient outage — the next render retries the heal, re-review P3);
            # a fund/no-OVERVIEW miss -> negative-cache it, WITH the SYMBOL_SEARCH name when one
            # exists (v1.121: the options page header must name the fund like the ETF page does)
            fund_name = _search_fund_name(sym, key, search)
            marker = {"symbol": sym, "asOf": dt.date.today().isoformat(), "empty": True}
            if fund_name is not False:     # a FAILED search is retried next render, never cached
                marker["nameChecked"] = True
            if fund_name:
                marker["name"] = fund_name
            elif fund_name is False and stale_name:
                # v1.125: a THROTTLED search on a marker REFRESH must not drop the known fund
                # name — carry it (nameChecked stays absent; the carried name serves fresh
                # renders, and the NEXT TTL refresh re-searches)
                marker["name"] = stale_name
            try:
                os.makedirs(CACHE_DIR, exist_ok=True)
                with open(_meta_path(sym), "w", encoding="utf-8") as f:
                    json.dump(marker, f)
            except OSError:
                pass
            if marker.get("name"):
                return {"name": marker["name"]}
        return old or prof
    if prof.get("name") is None and old:      # a DEGRADED pull (a real OVERVIEW always carries Name) -> merge-heal;
        # a FULL pull's Nones are AUTHORITATIVE — a removed site/blurb stays removed (re-review P2:
        # the unconditional merge resurrected dropped values forever, restamped fresh every TTL)
        prof = {k: (prof[k] if prof[k] is not None else old.get(k)) for k in _PROFILE_FIELDS}
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(_meta_path(sym), "w", encoding="utf-8") as f:
            json.dump({"symbol": sym, "asOf": dt.date.today().isoformat(), **prof}, f)
    except OSError:
        pass
    return prof


def exchange_get(symbol, key, fetch=None):
    """The listing venue only (NASDAQ / NYSE / …) — a thin back-compat wrapper over profile_get."""
    return profile_get(symbol, key, fetch).get("exchange")


# --- US equity/ETF trading session (Overview tile). Regular 09:30–16:00 ET + pre 04:00 / post 20:00.
# Holidays are NOT modelled (weekends are) — the tile says so; unknown/non-US venue -> None (tile omitted).
_US_VENUES = ("NASDAQ", "NYSE", "AMEX", "ARCA", "BATS", "CBOE", "NMS", "NGS", "NCM", "BX", "IEX")
_SESS_ET = {"pre": (4 * 60, 9 * 60 + 30), "regular": (9 * 60 + 30, 16 * 60), "post": (16 * 60, 20 * 60)}
_SESS_STATUS = {"pre": "Pre-market", "regular": "Open · regular", "post": "After-hours", "closed": "Closed"}


def _et_zone():
    """America/New_York, or a fixed UTC-5 fallback on a tzdata-less platform (bare Windows ships no IANA
    database). Never None: the tile RENDERS instead of vanishing — but on the fallback it is APPROXIMATE.
    Under EDT (most of the year) the fixed -5 is 1h off, so near a session boundary the phase, countdown
    AND clock labels ('UTC-05:00') can all read up to ~1h wrong. Mirrors the intraday fallback
    (fundamentals, v1.100); `pip install tzdata` on Windows restores exact ET."""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("America/New_York")
    except Exception:   # no tz database — fixed offset keeps the tile alive instead of dropping it
        return dt.timezone(dt.timedelta(hours=-5))


def _fmt_dur(s):
    """Coarse human duration for the session countdown ('4h 32m', '18m', '2d 3h'). Server-side bake;
    the JS fmtDur twin adds a live seconds field in the sub-hour branches ('18m 43s') so the ticking
    counter visibly moves — the server value is overwritten within one frame on an interactive page."""
    if s <= 0:
        return "now"
    d, s = divmod(int(s), 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m"
    return f"{s}s"


def market_session(exchange, now=None, assume_us=False, et_zone=None):
    """A session snapshot for the Overview tile: current phase (pre / regular / post / closed) with the
    regular hours shown in BOTH the exchange tz (ET) and the viewer's local tz, plus the current time on
    each clock. `now` may be aware or naive (naive = viewer-local); holidays are not checked (weekends
    are). Non-US / unknown venue -> None (the tile is simply omitted, never fatal). A tzdata-less
    platform no longer drops the tile — _et_zone falls back to a fixed UTC-5 offset. `et_zone` overrides
    the ET tzinfo (test seam).

    `assume_us=True`: the caller KNOWS the symbol is US-listed — e.g. an AV realtime-options chain, which
    is US-only — even when OVERVIEW (stock-only) gave no venue (an ETF like SPY). Then an EMPTY/blank
    venue falls back to a generic 'US equity' session so the tile still shows; a NAMED non-US venue
    still returns None (never fake a labelled non-US session)."""
    ex = (exchange or "").upper()
    if not any(v in ex for v in _US_VENUES):
        if assume_us and not ex:
            exchange = "US equity"   # AV-confirmed US-listed, venue unknown (ETF) -> generic US session
        else:
            return None
    etz = et_zone or _et_zone()   # ZoneInfo, or a fixed UTC-5 fallback on a tzdata-less platform (never None)
    now = now or dt.datetime.now()
    if now.tzinfo is None:
        now = now.astimezone()          # a naive now is the viewer's local wall clock
    loc = now.astimezone().tzinfo       # viewer local tz
    exch = now.astimezone(etz)          # same instant on the exchange clock
    mins = exch.hour * 60 + exch.minute
    if exch.weekday() >= 5:             # Sat / Sun
        phase, status = "closed", "Closed · weekend"
    else:
        phase = next((k for k, (a, b) in _SESS_ET.items() if a <= mins < b), "closed")
        status = _SESS_STATUS[phase]

    def _at(mo, tz):                     # today's ET minute-of-day -> a time on tz's clock
        mid = exch.replace(hour=0, minute=0, second=0, microsecond=0)
        return (mid + dt.timedelta(minutes=mo)).astimezone(tz)

    def _hm(x):
        return x.strftime("%H:%M")
    r0, r1 = _SESS_ET["regular"]
    eo, ec = _at(r0, etz), _at(r1, etz)
    lo, lc = eo.astimezone(loc), ec.astimezone(loc)
    etab, ltab = eo.tzname(), lo.tzname()
    # Upcoming MAIN-session boundaries (regular open 09:30 / close 16:00 ET) for the next ~8 trading days,
    # each a UTC epoch (ms) + whether it OPENS or CLOSES the session — the tile LIVE-counts down to the first.
    midnight = exch.replace(hour=0, minute=0, second=0, microsecond=0)
    sched = []
    for dday in range(0, 9):
        day = midnight + dt.timedelta(days=dday)
        if day.weekday() >= 5:             # skip weekends (holidays not modelled)
            continue
        for mo, lbl in ((r0, "opens"), (r1, "closes")):
            b = day + dt.timedelta(minutes=mo)
            if b > exch:
                sched.append({"at": int(b.timestamp() * 1000), "label": lbl})
    sched.sort(key=lambda x: x["at"])
    sched = sched[:8]
    cd_text, cd_label = "", ""
    if sched:
        cd_label = sched[0]["label"]
        cd_text = cd_label + " in " + _fmt_dur(int(sched[0]["at"] / 1000 - exch.timestamp()))
    return {
        "exchange": (exchange or "").strip(), "phase": phase, "status": status,
        "hoursExch": f"{_hm(eo)}–{_hm(ec)} {etab}",
        "hoursLocal": f"{_hm(lo)}–{_hm(lc)} {ltab}",
        "nowExch": f"{_hm(exch)} {etab}", "nowLocal": f"{_hm(now.astimezone(loc))} {ltab}",
        "preEt": f"{_hm(_at(_SESS_ET['pre'][0], etz))} {etab}",
        "postEt": f"{_hm(_at(_SESS_ET['post'][1], etz))} {etab}",
        "schedule": sched, "countdownText": cd_text, "countdownLabel": cd_label,
    }


def skel_expiry_lines(skel, today=None):
    """`date · DTE · strikes` lines from a cached skeleton (DTE recomputed vs today, expired dropped,
    nearest future marked) — the FIRST-QUESTION listing served from structure, no chain pull."""
    today = today or dt.date.today()
    rows = []
    for e in (skel or {}).get("expirations", []):
        try:
            dte = (dt.date.fromisoformat(e["date"]) - today).days
        except (ValueError, KeyError, TypeError):
            continue
        if dte >= 0:
            rows.append((e["date"], dte, int(e.get("nstrikes", 0))))
    return ["%s · %d DTE · %d strikes" % (d, dte, n) for d, dte, n in rows]


def skel_get(symbol, key, max_age_days=1, fetch_contracts=None):
    """(skeleton, state) — state is 'cached' (same-day cache hit), 'fresh' (live refresh) or 'stale'
    (the live chain came back empty, so the OLD skeleton is served and the caller MUST label it — never
    present it as current). skel is None only when there is neither a live chain nor any cached one."""
    skel = skel_load(symbol)
    if skel and _fresh(skel.get("asOf"), max_age_days):
        return skel, "cached"
    fresh = refresh_skeleton(symbol, key, fetch_contracts)
    if fresh is not None:
        return fresh, "fresh"
    return skel, "stale"


def _seen_path():
    return os.path.join(CACHE_DIR, "universe.seen")


def _seen_load():
    try:
        return {ln.strip().upper() for ln in open(_seen_path(), encoding="utf-8") if ln.strip()}
    except OSError:
        return set()


def accrete(symbol):
    """Lazy accretion: any ticker run through --history joins the prewarm universe (kept fresh nightly).
    Best-effort — a read-only/unwritable cache must NEVER break the --history render."""
    try:
        sym = _sym_norm(symbol.upper())
        if sym not in _seen_load():
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(_seen_path(), "a", encoding="utf-8") as f:
                f.write(sym + "\n")
    except OSError:
        pass


_SYM_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")   # real US ticker shapes (BRK.B, BF-B, SR3)
_CLASS_RE = re.compile(r"^([A-Z]{2,})[-.]([A-Z])$")  # class shares: BRK.B / BF-B / HEI-A


def _sym_valid(s):
    """ETF_PROFILE holdings carry non-ticker rows (cash/derivative buckets print "N/A", units
    carry spaces) — reject them here so they never join the universe as nightly tombstones."""
    return s != "N/A" and bool(_SYM_RE.match(s))


def _sym_norm(s):
    """AV's OPTIONS endpoints key class shares COLLAPSED (probed 2026-07-14: HISTORICAL_OPTIONS
    BRKB=2198 rows / HEIA=214 / MOGA=308, while every BRK-B/BRK.B/HEI-A form returns 0) —
    normalize a single trailing class letter so the store and prewarm speak the working form."""
    m = _CLASS_RE.match(s)
    return (m.group(1) + m.group(2)) if m else s


def accrete_many(symbols):
    """Bulk accretion for the universe expansion (v1.85: S&P-500-via-SPY-holdings + the ETF/crypto
    sets): dedupe against the seen set, uppercase, skip junk (incl. the ETF_PROFILE "N/A"
    placeholder rows). Returns how many NEW names joined.
    Best-effort like accrete() — an unwritable cache never breaks the caller."""
    added = 0
    try:
        seen = _seen_load()
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(_seen_path(), "a", encoding="utf-8") as f:
            for s in symbols:
                s = _sym_norm(str(s or "").strip().upper())
                if s and _sym_valid(s) and s not in seen:
                    f.write(s + "\n")
                    seen.add(s)
                    added += 1
    except OSError:
        pass
    return added


def _stamp_path():
    return os.path.join(CACHE_DIR, "prewarm.stamp")


def prewarm_stamp_write():
    """Record a COMPLETED (non-throttled) prewarm run. The laptop-friendly schedule (v1.85:
    launchd StartInterval + RunAtLoad instead of a fixed 04:00 a powered-off machine misses)
    retries every few hours — this stamp makes every same-day retry a free no-op."""
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(_stamp_path(), "w", encoding="utf-8") as f:
            json.dump({"date": dt.date.today().isoformat()}, f)
    except OSError:
        pass


def prewarm_stamp_fresh():
    """True when a prewarm already completed TODAY (--daily-once exits immediately, 0 calls)."""
    try:
        return json.load(open(_stamp_path(), encoding="utf-8")).get("date") == dt.date.today().isoformat()
    except (OSError, ValueError):
        return False


def _seen_drop(symbol):
    seen = _seen_load()
    if symbol.upper() in seen:
        seen.discard(symbol.upper())
        with open(_seen_path(), "w", encoding="utf-8") as f:
            f.write("".join(t + "\n" for t in sorted(seen)))


def load_universe(path=None):
    """The bundled seed list (or `path`) plus the lazily-accreted tickers, ORDERED so freshly-accreted
    names (a user just ran them through --full/--history; NOT in the curated seed) come FIRST — a mid-run
    AV throttle then can't starve exactly the names a user just asked about (the alphabetical seed is
    refreshed nightly regardless, and late-alphabet seed names are the ones a throttle truncates). Within
    each group: alphabetical. De-duplicated (a name in BOTH seed and accreted appears once)."""
    seed = set()
    try:
        for ln in open(path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "universe_options.txt"),
                       encoding="utf-8"):
            t = ln.split("#", 1)[0].strip().upper()
            if t:
                seed.add(t)
    except OSError:
        pass
    accreted = _seen_load()
    fresh = sorted(accreted - seed)                   # user-driven, at-risk -> front of the prewarm queue
    rest = sorted((seed | accreted) - set(fresh))     # the curated seed (+ any accreted already in it), alphabetical
    return fresh + rest


def prewarm(universe, key, years=1.0, daily=15, limit=None, progress=None,
            throttle_wait=180, throttle_retries=3, _sleep=None):
    """Nightly born-full/refresh: per ticker, refresh the skeleton (skip the pull if already today) +
    incrementally update the history store. Dead tickers (empty chain) are pruned from the accreted
    set. Idempotent + cheap on a warm cache. Returns a summary dict.
    SELF-HEALING (v1.85.2): AV's throttle is a load-based WAVE, not a clean rpm cap (empirical
    2026-07-13: a paced 250/min run died at ticker 21 while a full-speed burst minutes later ran
    clean) — so a _Throttled ticker is retried after `throttle_wait`s up to `throttle_retries`
    times (the partial store is already saved); only a ticker that NEVER clears stops the run."""
    _sleep = _sleep or time.sleep
    tickers = list(universe)[:limit] if limit else list(universe)
    # Counters partition honestly: warmed + dead == tickers processed (== n unless throttled cuts the
    # tail); skelFresh is a SUB-count of warmed (skeleton pull skipped as already-today, store still
    # updated), not a third bucket — it used to be added as "skipped" and read as if it excluded warmed.
    out = {"n": len(tickers), "warmed": 0, "skelFresh": 0, "dead": 0, "throttled": False}
    today = dt.date.today()
    for i, sym in enumerate(tickers):
        cached = skel_load(sym)
        if cached and _fresh(cached.get("asOf"), 0):        # skeleton already refreshed today
            out["skelFresh"] += 1
        else:
            try:
                _pace()                                     # skeleton pulls ride the same 450/min budget
                if refresh_skeleton(sym, key) is None:      # empty chain -> not optionable...
                    # ...UNLESS the store holds real history: AV under load can 200 an EMPTY chain
                    # for a live name (2026-07-14: PEG, parallel prewarms) — evidence beats one
                    # bad pull; treat it as transient and carry on to the store update.
                    if not load_store(sym):
                        _seen_drop(sym)
                        out["dead"] += 1
                        if progress:
                            progress(i + 1, len(tickers), sym + " (dead)", out)
                        continue
            except SystemExit:
                raise
            except Exception:
                pass
        cleared = False
        for attempt in range(throttle_retries + 1):
            try:
                update_store(sym, history_dates(today, years=years, daily=daily), key,
                             fetch_closes=fetch_daily_closes_raw)   # v1.120: raw-close spot anchors
                out["warmed"] += 1
                cleared = True
                break
            except _Throttled:      # a wave: wait it out and retry THIS ticker (partial is saved)
                if attempt >= throttle_retries:
                    break
                out["throttleWaits"] = out.get("throttleWaits", 0) + 1
                if progress:
                    progress(i + 1, len(tickers), sym + " (throttled — waiting %ds)" % throttle_wait, out)
                _sleep(throttle_wait)
            except SystemExit:
                raise
            except Exception:
                cleared = True      # a non-throttle error on ONE ticker never stops the universe
                break
        if not cleared:             # the wave never cleared inside the retry budget -> stop honestly
            out["throttled"] = True
            break
        if progress:
            # the RUNNING summary rides along so the log can say "how many are loaded so far"
            # (user 2026-07-13) — `out` is live-mutated above, the callback sees cumulative counts
            progress(i + 1, len(tickers), sym, out)
    return out


def term_shape(iv30, iv60, iv90):
    """Verbal vol-term shape for the 30/60/90 CM strip (v1.89.1 — the user asked for the
    contango/backwardation READ in words, incl. intermediates). Vocabulary matches the
    Term-slope card (front-/back-rich). Thresholds are RELATIVE to the front IV so the read is
    regime-independent (2 vol pts is noise at IV 60, an event at IV 12): |Δ90-30|/30 < 2% flat,
    2-8% mildly, >=8% pronounced; a 60d bump beyond ±2% of the 30/90 midpoint AND outside both
    wings reads humped/dished (a mid-dated event). None when a wing is missing — never fake."""
    if iv30 is None or iv90 is None or iv30 <= 0:
        return None
    d = (iv90 - iv30) / iv30 * 100.0
    if iv60 is not None:
        mid = (iv30 + iv90) / 2.0
        h = (iv60 - mid) / iv30 * 100.0
        if h >= 2.0 and iv60 > max(iv30, iv90):
            return "humped @60d"
        if h <= -2.0 and iv60 < min(iv30, iv90):
            return "dished @60d"
    if d <= -8.0:
        return "front-rich (backwardation)"
    if d <= -2.0:
        return "mildly front-rich"
    if d >= 8.0:
        return "back-rich (contango)"
    if d >= 2.0:
        return "mildly back-rich"
    return "flat"


def weekly_dates(end, weeks, weekday=4):
    """`weeks` past dates on `weekday` (Fri=4), oldest→newest, ending on/before `end`."""
    end = end if isinstance(end, dt.date) else dt.date.fromisoformat(end)
    anchor = end - dt.timedelta(days=(end.weekday() - weekday) % 7)
    return [(anchor - dt.timedelta(weeks=w)).isoformat() for w in range(weeks - 1, -1, -1)]


def _bdays(end, n):
    out, d = [], (end if isinstance(end, dt.date) else dt.date.fromisoformat(end))
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= dt.timedelta(days=1)
    return sorted(out)


def _monthly(newer_than, months):
    """`months` monthly points (~the 15th, snapped back to a weekday) going back from `newer_than`."""
    out, y, m = [], newer_than.year, newer_than.month
    for _ in range(max(0, months)):
        m -= 1
        if m == 0:
            m, y = 12, y - 1
        d = dt.date(y, m, 15)
        while d.weekday() >= 5:
            d -= dt.timedelta(days=1)
        out.append(d.isoformat())
    return out


def history_dates(end, years=1.0, daily=15):
    """Coarsening date grid so a multi-year backfill stays cheap: the last `daily` business days
    (recent detail) + WEEKLY for up to ~1 year + MONTHLY beyond a year (monthly is plenty for old
    vol context). Non-trading dates just return empty chains and get dropped on ingest."""
    end = end if isinstance(end, dt.date) else dt.date.fromisoformat(end)
    weeks = max(4, int(round(52 * min(years, 1.0))))
    dates = set(_bdays(end, daily)) | set(weekly_dates(end, weeks))
    if years > 1:
        dates |= set(_monthly(end - dt.timedelta(days=365), int(round((years - 1) * 12))))
    return sorted(dates)


def update_store(symbol, dates, key, fetch=None, workers=6, progress=None, fetch_closes=None):
    """Incremental backfill: load the store, fetch ONLY the dates not already cached, merge,
    persist, and return the full aggregate series. This is the cache seam — a re-run of the
    same window is instant; only genuinely new dates hit the network. `fetch_closes`
    (v1.120.0, e.g. fetch_daily_closes_raw) pulls the raw-close spot anchors ONCE per run;
    None (the offline/test default) = no pull, the chain fallback carries the estimate."""
    have = {h["date"]: h for h in load_store(symbol)}
    tomb = _tomb_load(symbol)
    missing = [d for d in dates if d not in have and d not in tomb]
    if not missing:
        return sorted(have.values(), key=lambda a: a["date"])
    closes = None
    if fetch_closes:
        try:                        # closes are an ACCURACY upgrade — never kill the backfill over them
            iso = [d if isinstance(d, str) else d.isoformat() for d in missing]
            recent = min(iso) >= (dt.date.today() - dt.timedelta(days=90)).isoformat()
            closes = closes_for_dates(symbol, key, iso, _fetch=fetch_closes,   # v1.122: class-share form
                                      outputsize=("compact" if recent else "full"))
        except Exception:
            closes = None
    throttled, empties = False, set()
    try:
        new = backfill(symbol, missing, key, fetch=fetch, workers=workers, progress=progress,
                       empties=empties, closes=closes)
    except _Throttled as t:          # persist the partial, then signal the caller to stop + report
        new, throttled = t.partial, True
    for a in new:
        have[a["date"]] = a
    if new:
        # v1.125: RE-MERGE over a FRESH load — `have` was snapshotted before the network-long
        # backfill, and a full overwrite from that stale snapshot CLOBBERED a concurrent
        # heal_spots save (healed spots reverted, heal-dropped dates resurrected, the heal
        # report left lying). Overlaying ONLY this run's new records on current disk keeps both
        # writers' work; heal-dropped dates stay dropped — they were present at entry, so they
        # are never in `missing`/`new`.
        fresh = {h["date"]: h for h in load_store(symbol)}
        fresh.update({a["date"]: a for a in new})
        have = fresh
        save_store(symbol, sorted(have.values(), key=lambda a: a["date"]))
    if not throttled:                # tombstone PAST empties (older than ~2 sessions; TODAY settles later)
        cutoff = (dt.date.today() - dt.timedelta(days=3)).isoformat()
        _tomb_add(symbol, {d for d in empties if d < cutoff})
    merged = sorted(have.values(), key=lambda a: a["date"])
    if throttled:
        raise _Throttled(merged)
    return merged


def heal_spots(symbol, key, fetch=None, closes=None, ratio=1.15, progress=None):
    """v1.120.0 store HEAL for the LEAP-trap poison (see _spot_from_chain): the write-side fix
    does not touch already-persisted records, so flag every record whose stored spot sits
    ABOVE ratio× that day's RAW close (the defect is strictly upward — Δ0.5 races only ever
    picked strikes at F·e^(σ²T/2) ≥ F), re-fetch those dates and re-reduce with the close as
    the spot anchor, then atomic-save. `fetch`/`closes` are test seams (live: hist_fetch +
    fetch_daily_closes_raw). An empty closes map is a NO-OP — never flag blind. A throttle
    mid-run persists the healed prefix before propagating.

    The two failure modes are NOT the same (v1.122.0, the 2026-07-19 sweep tail):
      * the re-fetch DIED (exception) -> `failed`: transient, KEEP the original and re-flag
        next run — bounded at one call/date/run, never silently dropped;
      * the re-reduce returned None -> `dropped`: AUTHORITATIVE, not a hiccup. At the true spot
        the chain carries no usable ATM IV, so the stored number was computed at the poisoned
        spot off strikes that are far OTM in reality (DBC 2025-08-15: the money strike 22 is the
        AV sentinel in every series while the only quoted strikes sit ~19% OTM — the record read
        cm30 41% off a 26-strike put). Retaining it keeps a KNOWN-wrong number feeding rank/cone
        forever AND re-burns a call every sweep; dropping the date is exactly what backfill does
        with an unusable chain, and leaves an honest hole.
    The save RE-MERGES over a fresh load by date, so a concurrent prewarm's new records survive
    (the 6h LaunchAgent race — review 2026-07-18).
    Returns {"checked","flagged","healed","failed","dropped"}."""
    rep = {"checked": 0, "flagged": 0, "healed": 0, "failed": 0, "dropped": 0}
    hist = load_store(symbol)
    if not hist:
        return rep
    if closes is None:
        closes = closes_for_dates(symbol, key, [h.get("date") for h in hist])   # v1.122: class-share form
    if not closes:
        return rep
    fetch = fetch or hist_fetch
    healed_aggs, drop_dates = {}, set()
    try:
        for h in hist:
            cl = closes.get(h.get("date"))
            if not cl or cl <= 0:
                continue
            rep["checked"] += 1
            sp = h.get("spot")
            if not sp or sp <= cl * ratio:
                continue
            rep["flagged"] += 1
            try:
                _pace()
                rows = fetch(symbol, h["date"], key)
                agg = reduce_asof(rows, h["date"], spot_hint=cl)
            except SystemExit:
                raise               # throttle: the finally below persists what already healed
            except Exception:
                rep["failed"] += 1
                continue
            if agg is None:         # authoritative: no usable ATM IV at the TRUE spot -> drop the poison
                drop_dates.add(h["date"])
                rep["dropped"] += 1
                continue
            healed_aggs[agg["date"]] = agg
            rep["healed"] += 1
            if progress:
                progress(symbol, h["date"], sp, cl)
    finally:
        if healed_aggs or drop_dates:
            fresh = {x["date"]: x for x in load_store(symbol)}   # re-merge: concurrent writes survive
            for d in drop_dates:                                 # only dates THIS run proved unusable
                fresh.pop(d, None)
            fresh.update(healed_aggs)
            save_store(symbol, sorted(fresh.values(), key=lambda a: a["date"]))
    return rep


def heal_spots_many(key, symbols=None, fetch=None, ratio=1.15, progress=None,
                    throttle_wait=180, throttle_retries=2, _sleep=None):
    """Store-wide heal pass. `symbols` defaults to every *.hist.json in the store. Per-symbol
    closes cost ONE call; only flagged dates re-pull chains. A SystemExit from one symbol
    (an AV throttle WAVE or a bad-symbol payload — options.av exits on both) must NOT kill
    the remaining ~600 (review P2, 2026-07-18): wait `throttle_wait`s and retry that symbol
    up to `throttle_retries` times (the prewarm v1.85.2 lesson — waves clear), then mark it
    in `aborted` and move on. heal_spots' partial saves make every re-run safe. Best run
    while the prewarm is idle (both writers RE-MERGE over a fresh load — v1.125 made
    update_store's save a re-merge too, so neither direction clobbers the other). Returns
    {"symbols", "reports": {sym: rep}, totals, "aborted": [syms]}."""
    _sleep = _sleep or time.sleep
    if symbols is None:
        try:
            symbols = sorted(f[:-len(".hist.json")] for f in os.listdir(CACHE_DIR)
                             if f.endswith(".hist.json") and not f.startswith("._"))
        except OSError:
            symbols = []
    out = {"symbols": len(symbols), "reports": {}, "checked": 0, "flagged": 0,
           "healed": 0, "failed": 0, "dropped": 0, "aborted": [], "skipped": []}
    for sym in symbols:
        rep = None
        for attempt in range(throttle_retries + 1):
            try:
                rep = heal_spots(sym, key, fetch=fetch, ratio=ratio, progress=progress)
                break
            except SystemExit:      # throttle wave OR dead symbol — indistinguishable here
                if attempt < throttle_retries:
                    _sleep(throttle_wait)
        if rep is None:
            out["aborted"].append(sym)
            continue
        if rep["checked"] == 0 and load_store(sym):
            out["skipped"].append(sym)   # non-empty store but ZERO records checked = the closes
            continue                     # pull came back empty (throttle wave swallowed to {}) —
                                         # honest-report it, never mark the symbol "clean" (re-review P3)
        out["reports"][sym] = rep
        for k in ("checked", "flagged", "healed", "failed", "dropped"):
            out[k] += rep[k]
    return out


def history_text(symbol, hist, closes=None):
    """Compact chat summary (the data never enters context via the chain — only these numbers).
    `closes` (daily adjusted closes) adds a VRP line (v1.83.0: IV vs realized)."""
    r = iv_rank(hist) or {}
    cur = hist[-1]
    out = ["%s OPTIONS · vol history · %d sessions · %s..%s · settlement (NOT live)" % (
        symbol.upper(), len(hist), hist[0]["date"], hist[-1]["date"])]
    if r:
        out.append("30d ATM IV %s  ·  IV rank %.0f%%  ·  percentile %.0f%%  ·  range %.1f–%.1f%%" % (
            _pct(cur.get("cm30")), r["rank"], r["pct"], r["min"] * 100, r["max"] * 100))
    if closes:   # v1.83.0 VRP: 30d IV vs 20d realized
        _hv = realized_at(list(closes.items()) if isinstance(closes, dict) else list(closes), cur.get("date", ""), 20)
        _iv30 = cur.get("cm30")
        if _hv is not None and _iv30 is not None:
            out.append("VRP(30d) %+.1f pts  ·  IV %.0f%% vs realized HV20 %.0f%%  (positive = options rich vs delivered)" % (
                _iv30 * 100 - _hv * 100, _iv30 * 100, _hv * 100))
    out.append("25Δ skew(30d) %s  ·  PCR(OI) %s" % (
        (lambda sk: "%+.1f pts" % sk if sk is not None else "n/a")(_plausible_skew(cur.get("skew30"), cur.get("cm30"))),
        ("%.2f" % cur["pcr"]) if cur.get("pcr") is not None else "n/a"))
    out.append("vol cone (cm ATM IV %):")
    for c in vol_cone(hist):
        out.append("  %4dd  min %4.1f  med %4.1f  max %4.1f  | cur %s" % (
            c["tenor"], c["min"] * 100, c["med"] * 100, c["max"] * 100,
            ("%4.1f" % (c["cur"] * 100)) if c["cur"] is not None else " n/a"))
    out.append("source: Alpha Vantage HISTORICAL_OPTIONS · AV-native IV · data analysis, not advice")
    return "\n".join(out)


if __name__ == "__main__":
    sym = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    weeks = int(sys.argv[2]) if len(sys.argv) > 2 else 6
    key = options.get_key()
    dates = weekly_dates(dt.date.today(), weeks)
    print("smoke: %s · %d weekly dates %s..%s" % (sym, len(dates), dates[0], dates[-1]), file=sys.stderr)
    hist = backfill(sym, dates, key,
                    progress=lambda d, t: print("  fetched %d/%d" % (d, t), file=sys.stderr))
    save_store(sym, hist)
    print("date        spot     cm30   cm60   cm90   skew30  pcr    nexp")
    for h in hist:
        print("%s  %8.2f  %5.1f  %5.1f  %5.1f  %6s  %5s  %3d" % (
            h["date"], h["spot"], h["cm30"] * 100, h["cm60"] * 100, h["cm90"] * 100,
            ("%.1f" % h["skew30"]) if h["skew30"] is not None else "n/a",
            ("%.2f" % h["pcr"]) if h["pcr"] is not None else "n/a", len(h["series"])))
    r = iv_rank(hist)
    if r:
        print("\nIV rank(cm30) = %.0f%%  ·  percentile %.0f%%  ·  range %.1f–%.1f%%  (n=%d)" % (
            r["rank"], r["pct"], r["min"] * 100, r["max"] * 100, r["n"]))
    print("\nvol cone (cm ATM IV %):")
    for c in vol_cone(hist):
        print("  %4dd  min %4.1f  p25 %4.1f  med %4.1f  p75 %4.1f  max %4.1f  | cur %s" % (
            c["tenor"], c["min"] * 100, c["p25"] * 100, c["med"] * 100,
            c["p75"] * 100, c["max"] * 100,
            ("%4.1f" % (c["cur"] * 100)) if c["cur"] is not None else " n/a"))
    out = sys.argv[3] if len(sys.argv) > 3 else None
    if out and hist:
        with open(out, "w", encoding="utf-8") as f:
            f.write(dashboard.render_interactive_html(spec_history(sym, hist)))
        print("wrote %s" % out, file=sys.stderr)
