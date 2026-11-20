#!/usr/bin/env python3
"""
moex_options.py — Russian EQUITY option chain (MOEX FORTS, premium-on-spot) dashboard.

The MOEX twin of options.py / futures_options.py: a STANDALONE producer (its own reducer +
to_spec, per the deliberate per-script-renderer decision) that feeds the SAME dashboard.py engine
and greeks.py. Design of record: docs/moex-options-plan.md.

Data source — the keyless public ISS `optionboard` statistics endpoint (the ONLY place MOEX
publishes per-contract IV, `VOLAT`, in percent). Target = the 41 premium-on-spot STOCK options
(SBER, GAZP, LKOH…): options on the SHARE, priced Black-Scholes. IV is the exchange's own; the
skill computes greeks (Δ/Γ/Θ/vega) via greeks.py with a RUSFAR risk-free rate.

HONESTY (hard rules): public ISS is DELAYED ~15 min — the badge says `delayed`, never LIVE, and
carries the board SYSTIME; RUB throughout; IV is the exchange's, greeks are computed; thin books
(~12 strikes/series) show n/a for a no-quote leg, never 0.
"""
from __future__ import annotations
import argparse
import concurrent.futures
import datetime as dt
import json
import os
import sys
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # Windows-safe
import dashboard  # noqa: E402
import greeks  # noqa: E402
from chart import http_get  # noqa: E402

ISS = "https://iss.moex.com/iss"
# chart.http_get RAISES after exhausting retries — a network outage / DNS / read timeout (OSError,
# incl. socket.timeout) or an HTTP 4xx/5xx (urllib.error.URLError, incl. HTTPError). _main converts
# these to a clean sys.exit (the essential board/expirations pulls don't swallow them — only the
# best-effort profile/spot/rate helpers do). ValueError guards a malformed JSON body.
_NET_ERRORS = (urllib.error.URLError, OSError, ValueError)
RUB_RATE_ENV = "MARKET_DATA_RUB_RATE"
_RATE_FALLBACK = 0.15   # if RUSFAR can't be read: a plausible RUB default (only Δ/Γ, not the smile)
_TURN_OI_FLOOR = 10     # v1.138: per-strike turnover needs a minimal standing book to be a ratio, not
#                         noise — below this many standing contracts (call+put OI) the turnover is None


# ----------------- fetch layer (seam-injectable) -----------------
def _iss(path, fetch=None):
    """GET an ISS path -> parsed JSON. `fetch(path)->text` is the injectable seam (tests pass a stub;
    the default hits the live keyless ISS through chart.http_get's retrying getter)."""
    fetch = fetch or (lambda p: http_get(ISS + p))
    return json.loads(fetch(path))


def _rows(block):
    """An ISS block {columns, data} -> a list of dicts."""
    cols = block.get("columns") or []
    return [dict(zip(cols, r)) for r in (block.get("data") or [])]


def fetch_expirations(sym, fetch=None):
    """[{series_type ∈ W|M|Q, expiration}] for an asset, soonest first."""
    d = _iss("/statistics/engines/futures/markets/options/assets/%s/expirations.json?iss.meta=off" % sym, fetch)
    return [{"series_type": r.get("series_type"), "expiration": r.get("expiration_date")}
            for r in _rows(d.get("expirations", {})) if r.get("expiration_date")]


def fetch_board(sym, expiration=None, fetch=None):
    """The optionboard for one asset (one series if `expiration` given, else the near series):
    {'call': [...], 'put': [...], 'asset': {...}} with the raw ISS rows."""
    q = "/statistics/engines/futures/markets/options/assets/%s/optionboard.json?iss.meta=off" % sym
    if expiration:
        q += "&expiration_date=" + expiration
    d = _iss(q, fetch)
    asset_rows = _rows(d.get("asset", {}))
    return {"call": _rows(d.get("call", {})), "put": _rows(d.get("put", {})),
            "asset": asset_rows[0] if asset_rows else {}}


def _board_from_raw(raw):
    """A RAW saved ISS optionboard response ({call,put,asset} each a {columns,data} block — what the
    live endpoint and --from-dir save) -> the reduced {call:[…], put:[…], asset:{…}} that build_chain
    consumes. Idempotent: an already-reduced board (lists / an asset dict) passes through unchanged."""
    def _blk(v):
        return _rows(v) if isinstance(v, dict) and "columns" in v else (v or [])
    asset = raw.get("asset")
    if isinstance(asset, dict) and "columns" in asset:
        ar = _rows(asset)
        asset = ar[0] if ar else {}
    return {"call": _blk(raw.get("call")), "put": _blk(raw.get("put")), "asset": asset or {}}


def fetch_profile(sym, fetch=None):
    """The underlying SHARE profile in ONE TQBR call: {spot (live LAST), changePct, name, asOf}.
    The freshest real timestamp the skill has (marketdata SYSTIME) — the delayed 'as of'. Any failure
    -> {} (build_chain falls back to the board settle for spot and a plain 'delayed' note)."""
    try:
        d = _iss("/engines/stock/markets/shares/boards/TQBR/securities/%s.json"
                 "?iss.meta=off&iss.only=securities,marketdata" % sym, fetch)
        sec = (_rows(d.get("securities", {})) or [{}])[0]
        md = (_rows(d.get("marketdata", {})) or [{}])[0]
        spot = _ff(md.get("LAST"))
        return {"spot": spot if (spot and spot > 0) else None,
                "changePct": _ff(md.get("LASTCHANGEPRCNT")),
                "name": sec.get("SHORTNAME") or sec.get("SECNAME") or "",
                "asOf": md.get("SYSTIME") or md.get("UPDATETIME") or None,
                "sharesOut": _ff(sec.get("ISSUESIZE")),   # for the header market-cap (spot × shares)
                "currency": sec.get("CURRENCYID")}         # cap only shown for a RUB listing (SUR/RUB)
    except (Exception, SystemExit):
        return {}


def fetch_spot(sym, fetch=None):
    """The live (delayed) underlying SHARE price from the TQBR board, or None. Freshest source of
    the spot the options price off; build_chain falls back to the board's UNDERLYINGSETTLEPRICE."""
    try:
        d = _iss("/engines/stock/markets/shares/boards/TQBR/securities/%s.json"
                 "?iss.meta=off&iss.only=marketdata" % sym, fetch)
        rows = _rows(d.get("marketdata", {}))
        if rows:
            v = rows[0].get("LAST")
            return float(v) if v not in (None, "", 0) else None
    except (Exception, SystemExit):
        return None
    return None


# RUSFAR tenor family (secid -> approx tenor days): the RUB secured-funding risk-free proxy.
_RUSFAR = [("RUSFAR", 1), ("RUSFAR1W", 7), ("RUSFAR2W", 14), ("RUSFAR1MN", 30)]


def fetch_rate(dte=None, fetch=None):
    """The RUB risk-free rate as a FRACTION (e.g. 0.139), from RUSFAR — the tenor nearest `dte`
    (O/N · 1w · 2w · 1m). Env MARKET_DATA_RUB_RATE overrides (a fraction). Any failure -> the
    documented fallback. Only Δ/Γ depend on r; the smile uses the exchange IV directly."""
    env = os.environ.get(RUB_RATE_ENV, "").strip()
    if env:
        try:
            return float(env)
        except ValueError:
            pass
    secid = "RUSFAR"
    if dte:
        secid = min(_RUSFAR, key=lambda t: abs(t[1] - dte))[0]
    try:
        d = _iss("/engines/stock/markets/index/boards/MMIX/securities/%s.json"
                 "?iss.meta=off&iss.only=marketdata" % secid, fetch)
        rows = _rows(d.get("marketdata", {}))
        if rows:
            v = rows[0].get("CURRENTVALUE") or rows[0].get("LASTVALUE")
            if v not in (None, "", 0):
                return float(v) / 100.0   # RUSFAR is quoted in percent
    except (Exception, SystemExit):
        pass
    # review P3: the rate is the one path where a silent fallback would misattribute "RUSFAR" — say so
    print("note: RUSFAR rate unavailable — using the %.1f%% fallback (set --rate / %s to override); "
          "affects Δ/Γ only, not the exchange-IV smile." % (_RATE_FALLBACK * 100, RUB_RATE_ENV), file=sys.stderr)
    return _RATE_FALLBACK


def _ff(v):
    """Safe float, else None."""
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


# ----------------- analysis -----------------
class ChainError(RuntimeError):
    """A loud, catchable chain failure (bad ticker / non-optionable / empty board)."""


def reduce_board(board):
    """The raw ISS board -> ({strike: {'call': rec, 'put': rec}}, asset) where each rec carries
    {iv (fraction), oi, vol, bid, ask, theor}. VOLAT is percent -> fraction; a non-positive/absent
    IV is None (a hole)."""
    by = {}

    def _rec(r):
        iv = _ff(r.get("VOLAT"))
        return {"iv": (iv / 100.0 if (iv and iv > 0) else None),
                "oi": _ff(r.get("OPENPOSITION")), "vol": _ff(r.get("VOLTODAY")),
                "bid": _ff(r.get("BID")), "ask": _ff(r.get("OFFER")), "theor": _ff(r.get("THEORPRICE")),
                "secid": r.get("SECID")}   # v1.141: the per-contract SECID keys the ΔOI baseline match
    for side, rows in (("call", board.get("call") or []), ("put", board.get("put") or [])):
        for r in rows:
            k = _ff(r.get("STRIKE"))
            if k is not None:
                by.setdefault(k, {})[side] = _rec(r)
    return by, (board.get("asset") or {})


def _greeks_at(spot, K, iv, dte, rate, is_call):
    """Black-Scholes greeks at K from a given IV (fraction). None on degenerate inputs. greeks.py is
    the ONLY place greeks are computed — the smile IV in and the Δ out use the same model + rate.
    q=0 (no dividend yield): MOEX single-name dividends are DISCRETE and lumpy, not a continuous yield,
    so modelling one would be its own approximation; the forward is thus slightly overstated and Δ is
    approximate across a dividend date — a documented Tier-1 simplification (the panel/read say so).
    The exchange IV (VOLAT) that drives the smile is unaffected — only Δ/Γ use r and q."""
    if iv is None or spot is None or K is None or not dte or dte <= 0 or spot <= 0 or K <= 0:
        return None
    try:
        return greeks.black_scholes(spot, K, dte / 365.0, iv, r=rate, is_call=is_call)
    except Exception:
        return None


def _parse_date(s):
    """ISO date or None — never raises (the CLI validates --expiry; this guards internal callers)."""
    try:
        return dt.date.fromisoformat(s) if s else None
    except (ValueError, TypeError):
        return None


# ----------------- ΔOI windows (v1.141: the equity "book with caps" mirror) -----------------
# The OI panel gains a per-window toggle (1d/1w/1m/1Q) showing what was BUILT vs UNWOUND since a past
# settlement. Baseline = each contract's OPENPOSITION on the window's anchor date, from the assetcode-
# filtered per-date settlement history (moexopthist), keyed by SECID (today's chain matches by it). Past
# settlements are IMMUTABLE -> cached forever (moexopthist's per-assetcode gzip cache), shared across
# windows resolving to the same date. Failure ≠ no-data: an ISS error degrades to absent (plain OI panel).
_DOI_WINDOWS = [("1d", 1, 0), ("1w", 5, 0), ("1m", 0, 1), ("1Q", 0, 3)]   # (key, business-days, cal-months)
_DROI_MEMO = {}   # (assetcode, asof, wkey) -> baseline|None (in-process; a memoized failure never re-burns)


def _prev_bdays_m(asof, n):
    d, out = dt.date.fromisoformat(asof), []
    while len(out) < n:
        d -= dt.timedelta(days=1)
        if d.weekday() < 5:
            out.append(d.isoformat())
    return out


def _month_minus_m(d, months):
    import calendar
    y, m = d.year, d.month - months
    while m <= 0:
        m += 12
        y -= 1
    return dt.date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def _window_dates_m(asof, wkey, n=4):
    """Candidate baseline trading days for a window, newest first — 1d/1w anchor on business days back;
    1m/1Q on the calendar (a month/quarter ago) snapped to the nearest prior business day; ≤n walk over
    weekends/holidays."""
    bd, mo = {w: (b, m) for w, b, m in _DOI_WINDOWS}.get(wkey, (1, 0))
    if mo:
        out, d = [], _month_minus_m(dt.date.fromisoformat(asof), mo)
        while len(out) < n:
            if d.weekday() < 5:
                out.append(d.isoformat())
            d -= dt.timedelta(days=1)
        return out
    skip = bd - 1   # 1d -> the 1st business day back; 1w -> the 5th onward
    return _prev_bdays_m(asof, skip + n)[skip:skip + n]


def _oi_on_date(assetcode, date, fetch):
    """{SECID: OI} for an assetcode on one settlement date (the assetcode-filtered per-date history)."""
    import moexopthist as mh
    base = ("/history/engines/futures/markets/options/securities.json?iss.meta=off"
            "&iss.only=history,history.cursor&assetcode=%s&date=%s" % (assetcode, date))
    out = {}
    for r in mh._paged_history(base, fetch):
        s, oi = r.get("SECID"), _ff(r.get("OPENPOSITION"))
        if s and oi is not None:
            out[s] = oi
    return out


def _probe_baseline_m(assetcode, dates, fetch, tdy):
    """Walk candidate business days (newest first) for the first with a real settlement -> {date, oi:{secid:
    oi}}. A young (<2d) empty is NOT cached (publication latency); an old empty is a holiday, cached forever.
    An ISS error -> (None, failed=True): the caller degrades, doesn't re-burn."""
    import moexopthist as mh
    for d in dates:
        cached = mh._load_cache(assetcode, "droi-" + d)   # {} miss | {"empty":1} | {secid: oi}
        if cached:
            if cached.get("empty"):
                continue
            return {"date": d, "oi": cached}, False
        try:
            oi = _oi_on_date(assetcode, d, fetch)
        except (Exception, SystemExit):
            return None, True
        if not oi:
            if dt.date.fromisoformat(d) <= tdy - dt.timedelta(days=2):
                mh._save_cache(assetcode, "droi-" + d, {"empty": 1})
            continue
        mh._save_cache(assetcode, "droi-" + d, oi)
        return {"date": d, "oi": oi}, False
    return None, False


def fetch_oi_baselines(sym, asof, fetch=None, windows=None, _today=None):
    """Per-window ΔOI baselines {wkey: {date, oi:{secid: oi}} | None} for 1d/1w/1m/1Q. Windows are
    INDEPENDENT (distinct anchor dates, per-date disk cache) -> fetched CONCURRENTLY. MARKET_DATA_DROI=0
    disables (returns {}). `fetch`/`_today` are test seams; `windows` restricts the set."""
    if os.environ.get("MARKET_DATA_DROI", "").strip() == "0":
        return {}
    import moexopthist as mh
    ac = mh._asset_code(sym, fetch=fetch)
    if not ac:
        return {}
    try:
        asof = (asof or dt.date.today().isoformat())[:10]
        dt.date.fromisoformat(asof)
    except (ValueError, TypeError):
        asof = dt.date.today().isoformat()
    tdy = _today or dt.date.today()
    keys = [w for w, _b, _m in _DOI_WINDOWS if (windows is None or w in windows)]
    out, todo = {}, []
    for wkey in keys:
        mk = (ac, asof, wkey)
        if mk in _DROI_MEMO:
            out[wkey] = _DROI_MEMO[mk]
        else:
            todo.append(wkey)

    def _one(wkey):
        res, _f = _probe_baseline_m(ac, _window_dates_m(asof, wkey), fetch, tdy)
        return wkey, res
    if todo:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(todo))) as ex:
            for wkey, res in ex.map(_one, todo):
                _DROI_MEMO[(ac, asof, wkey)] = res
                out[wkey] = res
    return {w: out[w] for w in keys}


def _droi_net(v):
    vv = [x for x in v if x is not None]
    return sum(vv) if vv else None


def _attach_droi(an, baselines):
    """Attach per-window ΔOI ladders to `an` from `baselines` (fetch_oi_baselines output). Sets an['doi']
    = {wkey: {date, dOICall, dOIPut, callNet, putNet}}, an['doiWindows'], an['doiDefault']. A contract
    absent in a baseline counts from 0 (a new listing's first OI is a real build); today's OI None -> None.
    No usable baseline -> `an` untouched (to_spec renders the plain OI panel)."""
    if not baselines:
        return an
    doi = {}
    for wkey, _b, _m in _DOI_WINDOWS:
        prev = baselines.get(wkey)
        if not prev or not prev.get("oi"):
            continue
        boi = prev["oi"]
        dc = [None if c is None else (c - (boi.get(s) or 0.0)) for c, s in zip(an["callOI"], an["callSec"])]
        dp = [None if c is None else (c - (boi.get(s) or 0.0)) for c, s in zip(an["putOI"], an["putSec"])]
        doi[wkey] = {"date": prev["date"], "dOICall": dc, "dOIPut": dp,
                     "callNet": _droi_net(dc), "putNet": _droi_net(dp)}
    if doi:
        an["doi"] = doi
        an["doiWindows"] = [w for w, _b, _m in _DOI_WINDOWS if w in doi]
        an["doiDefault"] = an["doiWindows"][0]
    return an


def build_chain(sym, board, profile, rate, expiration, today=None):
    """The analysis dict `an` from a reduced board — mirrors options.build / futures build_chain, but
    IV comes from the exchange (VOLAT) and greeks are Black-Scholes on the SHARE. `profile` =
    fetch_profile's {spot, changePct, name, asOf} (or {} / a bare float for back-compat). DEFAULT
    ladder = every strike with OI on either side. IVs stored in PERCENT (like futures_options)."""
    today = today or dt.date.today()
    if isinstance(profile, (int, float)):        # back-compat: a bare spot float
        profile = {"spot": float(profile)}
    profile = profile or {}
    by, asset = reduce_board(board)
    if not by:
        raise ChainError("error: no %s option board for %s (bad ticker / non-optionable / empty)" % (expiration or "", sym))
    spot = profile.get("spot")
    spot_settle = _ff(asset.get("UNDERLYINGSETTLEPRICE"))
    spot_settle = spot_settle if (spot_settle and spot_settle > 0) else None   # review P3: 0.0 settle is not a spot
    spot_estimated = spot is None                # live share LAST missing -> the board's settle (mark it)
    spot = spot if spot is not None else spot_settle
    if spot is None or spot <= 0:
        raise ChainError("error: no usable underlying spot for %s (share price + board settle both "
                         "missing or non-positive)" % sym)

    def _oi(rec, side):
        return int((rec.get(side) or {}).get("oi") or 0)
    strikes = sorted(k for k, r in by.items() if _oi(r, "call") > 0 or _oi(r, "put") > 0) or sorted(by)
    if len(strikes) < 2:
        raise ChainError("error: %s %s has <2 tradeable strikes" % (sym, expiration or ""))
    # v1.143: some MOEX options are quoted on a LOT (PLZL/T: LOTSIZE 10 → strike = per-share × 10, so the
    # ladder sits ~10× the share). Map such strikes to SHARE terms so ATM/moneyness/IV/greeks are consistent
    # with the share spot. Decide the scale from the FULL LISTED ladder (every key in `by`), NOT the
    # OI-filtered `strikes`: an illiquid LEAP series' open interest can cluster entirely in deep-OTM call
    # LEAPS whose median sits far above spot and would FAKE a lot-scale on a plainly 1× blue-chip (MGNT's
    # 2028 LEAPS did exactly this — review P1). A genuinely lot-quoted ladder is shifted in ENTIRETY — even
    # its LOWEST listed strike sits well above the share (PLZL's floor ~7× spot) — while any normal name lists
    # a near/ITM strike below spot; gate on that (min > 2× spot), then size the scale from the median. The
    # exchange IV is unchanged (per-contract VOLAT, unit-free). Scale is echoed in `an` for the lead note.
    # NOTE: the 2× floor is EMPIRICAL — live floors are PLZL ~3.95× / T ~2.79× (T's margin is modest). The
    # authoritative signal is the option contract's own LOTSIZE from the ISS /securities def; a follow-up
    # would thread that in (the stock's TQBR LOTSIZE does NOT work — SBER is lot-10 yet strikes are per-share).
    import math
    _full = sorted(by)
    strike_scale = 1
    if spot and _full and _full[0] > 2 * spot:
        strike_scale = 10 ** round(math.log10(_full[len(_full) // 2] / spot))
    if strike_scale > 1:
        by = {k / strike_scale: v for k, v in by.items()}
        strikes = sorted(set(k / strike_scale for k in strikes))
    atm = min(strikes, key=lambda k: abs(k - spot))
    ai = strikes.index(atm)

    def _iv(rec, side):
        return (rec.get(side) or {}).get("iv")
    callIV = [(round(_iv(by[k], "call") * 100, 2) if _iv(by[k], "call") else None) for k in strikes]
    putIV = [(round(_iv(by[k], "put") * 100, 2) if _iv(by[k], "put") else None) for k in strikes]
    callOI = [_oi(by[k], "call") for k in strikes]
    putOI = [_oi(by[k], "put") for k in strikes]
    callVol = [(by[k].get("call") or {}).get("vol") for k in strikes]
    putVol = [(by[k].get("put") or {}).get("vol") for k in strikes]
    callSec = [(by[k].get("call") or {}).get("secid") for k in strikes]   # v1.141: SECID per strike for ΔOI
    putSec = [(by[k].get("put") or {}).get("secid") for k in strikes]

    # review P3: ATM IV = the OTM COMPOSITE at the ATM STRIKE — the SAME k<=atm rule _smile_iv/the chain
    # table use (put leg at/below the ATM strike, call above), so the ATM IV card can't disagree with the
    # smile. atm_leg_call records which leg it came from, so the ATM Θ card uses the matching option.
    atm_leg_call = putIV[ai] is None
    atm_iv = callIV[ai] if atm_leg_call else putIV[ai]

    # 25Δ skew proxy = IV(put ~10% OTM) − IV(call ~10% OTM), vol points (moneyness proxy, like futures).
    # review P3: a ±5% moneyness TOLERANCE — a thin/far series whose listed strikes never reach ~10% OTM
    # returns None (an honest hole), never a near-ATM put-vs-call spread mislabeled as the 10%-OTM skew.
    def iv_at(target, arr):
        _tol = 0.05 * spot
        cand = [(abs(k - target), arr[i]) for i, k in enumerate(strikes)
                if arr[i] is not None and abs(k - target) <= _tol]
        return min(cand)[1] if cand else None
    p90, c110 = iv_at(0.90 * spot, putIV), iv_at(1.10 * spot, callIV)
    skew = round(p90 - c110, 1) if (p90 is not None and c110 is not None) else None

    coi, poi = sum(callOI), sum(putOI)
    cvol = sum(v for v in callVol if v) or 0.0
    pvol = sum(v for v in putVol if v) or 0.0

    # v1.138 liquidity + flow, from the BID/OFFER/VOLTODAY the board already carries (were reduced, dropped).
    # Spread% = (ask−bid)/mid on the OTM leg (put wing below ATM, call above, tighter-of-two at the money) —
    # the tradeability read. Turnover = today's volume ÷ the standing book per strike (where FRESH flow
    # concentrates), floored so a tiny book isn't a noisy ratio.
    def _spr(k, side):
        r = by[k].get(side) or {}
        b, a = r.get("bid"), r.get("ask")
        if not b or not a or a < b:   # missing / crossed / non-positive -> honest hole
            return None
        mid = (a + b) / 2.0
        return (a - b) / mid * 100.0 if mid > 0 else None
    callSpr = [_spr(k, "call") for k in strikes]
    putSpr = [_spr(k, "put") for k in strikes]
    otmSpr = []
    for i, k in enumerate(strikes):
        if k < atm:
            otmSpr.append(putSpr[i])
        elif k > atm:
            otmSpr.append(callSpr[i])
        else:
            _both = [s for s in (callSpr[i], putSpr[i]) if s is not None]
            otmSpr.append(min(_both) if _both else None)
    _sv = sorted(s for s in otmSpr if s is not None)
    med_spr = ((_sv[len(_sv) // 2] if len(_sv) % 2 else (_sv[len(_sv) // 2 - 1] + _sv[len(_sv) // 2]) / 2.0)
               if _sv else None)
    turnover = []
    for i in range(len(strikes)):
        _toi = (callOI[i] or 0) + (putOI[i] or 0)
        if (callVol[i] is None and putVol[i] is None) or _toi < _TURN_OI_FLOOR:
            turnover.append(None)
        else:
            turnover.append(((callVol[i] or 0) + (putVol[i] or 0)) / _toi)
    turnover_all = ((cvol + pvol) / (coi + poi)) if ((coi + poi) and (cvol or pvol)) else None

    def payout(S):
        return sum(callOI[i] * max(0.0, S - k) + putOI[i] * max(0.0, k - S) for i, k in enumerate(strikes))
    maxpain = min(strikes, key=payout) if (coi or poi) else None
    exp = expiration or asset.get("LASTDELDATE")
    ed = _parse_date(exp)
    dte = (ed - today).days if ed else None
    em = (spot * (atm_iv / 100.0) * (max(dte, 0) / 365.0) ** 0.5) if (atm_iv and dte and dte > 0) else None
    return {
        "symbol": sym.upper(), "name": profile.get("name") or "", "expiration": exp, "dte": dte,
        "spot": spot, "spotEstimated": spot_estimated, "changePct": profile.get("changePct"),
        "sharesOut": profile.get("sharesOut"), "currency": profile.get("currency"),
        "strikeScale": strike_scale,
        "asOf": profile.get("asOf"), "rate": rate,
        "atmStrike": atm, "atmIV": atm_iv, "atmLegCall": atm_leg_call, "skew25": skew,
        "expMove": em, "expMovePct": (em / spot * 100 if (em and spot) else None),
        "pcrOI": (poi / coi if coi else None), "pcrVol": (pvol / cvol if cvol else None),
        "maxPain": maxpain,
        "callOItotal": coi, "putOItotal": poi, "nStrikes": len(strikes), "strikes": strikes,
        "callIV": callIV, "putIV": putIV, "callOI": callOI, "putOI": putOI,
        "callVol": callVol, "putVol": putVol, "callSec": callSec, "putSec": putSec,
        "callSpr": callSpr, "putSpr": putSpr, "otmSpr": otmSpr, "medSpr": med_spr,
        "turnover": turnover, "turnoverAll": turnover_all,
        "underlyingSettle": spot_settle,
    }


def gex_ladder(an, iv_col):
    """Naive dealer GEX per strike: Γ(K) × (callOI − putOI). Black-Scholes Γ is call/put-identical, so
    ONE Γ per strike from the OTM-composite IV the smile/Δ use. netOI 0 -> real 0.0; no solvable IV
    with real OI -> None (dropped)."""
    out = []
    for i, k in enumerate(an["strikes"]):
        net = (an["callOI"][i] or 0) - (an["putOI"][i] or 0)
        if net == 0:
            out.append(0.0)
            continue
        g = _greeks_at(an["spot"], k, (iv_col[i] / 100.0 if iv_col[i] else None), an["dte"], an["rate"], True)
        out.append(g.gamma * net if g else None)
    return out


def _smile_iv(an):
    """The OTM composite IV per strike (percent): put IV for K<=ATM, call IV for K>ATM; a bracketed
    hole is linearly interpolated + marked. Returns (iv_col, interp_flags)."""
    strikes, atm = an["strikes"], an["atmStrike"]
    otm = [(an["putIV"][i] if k <= atm else an["callIV"][i]) for i, k in enumerate(strikes)]
    interp = [False] * len(strikes)
    for i in range(len(strikes)):
        if otm[i] is not None:
            continue
        li = next((j for j in range(i - 1, -1, -1) if otm[j] is not None), None)
        ri = next((j for j in range(i + 1, len(strikes)) if otm[j] is not None), None)
        if li is None or ri is None:
            continue   # unbracketed edge -> leave the hole, never extrapolate a wing
        k0, k1 = strikes[li], strikes[ri]
        otm[i] = otm[li] + (otm[ri] - otm[li]) * (strikes[i] - k0) / (k1 - k0) if k1 != k0 else otm[li]
        interp[i] = True
    return otm, interp


def _num(x, d=None):
    if x is None:
        return "n/a"
    if d is None:
        ax = abs(x)
        d = 0 if ax >= 100 else (1 if ax >= 10 else 2)
    return format(x, ",.%df" % d)


def _ivfmt(x):
    return "n/a" if x is None else "%.1f%%" % x


def _lead(an):
    """The page-level lead (the About, v1.110 unified-header style): the FORTS/premium/RUB/delayed
    identity + a delayed 'as of' meta line, shown under the header and above the tabs — the same block
    every other dashboard carries. Replaces the old About panel + subtitle."""
    nm = ("%s (%s) — " % (an["name"], an["symbol"])) if an.get("name") else ("%s — " % an["symbol"])
    txt = (nm + "Russian equity options on the Moscow Exchange (FORTS): premium-style, exercising into "
           "the SHARE, quoted in roubles. Implied volatility is the exchange's own (ISS VOLAT); the skill "
           "computes greeks (Black-Scholes on the share, RUSFAR risk-free). Public ISS data is DELAYED "
           "~15 minutes — never a live tick.")
    if an.get("strikeScale", 1) > 1:   # lot-quoted contract (PLZL/T): strikes shown per SHARE (exchange ×lot)
        txt += (" Strikes are shown PER SHARE; the exchange lists them ×%d (this contract is on a lot of %d "
                "shares), so an exchange strike of e.g. %d reads here as %d."
                % (an["strikeScale"], an["strikeScale"], int(an["atmStrike"] * an["strikeScale"]),
                   int(an["atmStrike"])))
    asof = an.get("asOf")
    meta = ("as of %s · delayed ~15 min" % asof) if asof else "delayed ~15 min"
    return {"text": txt, "meta": meta}


def _series_label(an):
    """None-safe series caption for the card scopeLabel / text header: 'YYYY-MM-DD · Nd' when both
    known, else whatever is known, else 'near series' — never the literal 'None · Noned' (review P3)."""
    exp, dte = an.get("expiration"), an.get("dte")
    if exp and dte is not None:
        return "%s · %sd" % (exp, dte)
    if exp:
        return str(exp)
    return "near series"


def _change_fmt(pct):
    return "" if pct is None else ("%+.2f%%" % pct)


def _right_small(an):
    """The header change/cap line (v1.138, equity parity): 'change% · cap ₽X.XXT'. Cap = LIVE spot ×
    shares outstanding (ISSUESIZE) so it tracks the price; shown only for a RUB listing (a USD-quoted
    board would make ₽×shares a wrong unit). Estimated-spot marks the whole line ' est'."""
    parts = []
    chg = _change_fmt(an.get("changePct"))
    if chg:
        parts.append(chg)
    so, sp, cur = an.get("sharesOut"), an.get("spot"), an.get("currency")
    if so and sp and (cur in (None, "SUR", "RUB")):
        cap = dashboard.cap_str(sp * so / 1e9, cur="₽")
        if cap:
            parts.append("cap " + cap + (" est" if an.get("spotEstimated") else ""))
    return " · ".join(parts)


def _closes_from_hist(hist):
    """The MOEX vol store already carries the SHARE CLOSE per date (each record's `spot` = that day's
    LEGALCLOSEPRICE), so realized vol / VRP need NO extra pull — just map {date: close} for opthist's
    `closes=` param. Raw (not split/div-adjusted): an ex-dividend gap adds a little realized-vol noise
    over the window — a documented approximation (the VRP card is a rough implied-vs-realized read)."""
    return {r["date"]: r["spot"] for r in (hist or []) if r.get("date") and r.get("spot")}


def _pcr_vol_sub(an):
    """The PCR card's sub-line: PCR by today's VOLUME (flow) + a divergence word when flow leans
    differently than the standing OI book (thresholds ±25%/20%, the equity twin). None -> no sub."""
    pv, po = an.get("pcrVol"), an.get("pcrOI")
    if pv is None:
        return None
    dvg = ""
    if po:
        r = pv / po
        dvg = (" · flow puts-heavier than the book" if r >= 1.25
               else (" · flow calls-heavier than the book" if r <= 0.8 else ""))
    return ["vol %s%s" % (_num(pv, 2), dvg)]


def to_spec(an):
    """The analysis -> a dashboard SPEC (unified stacked header + page lead + cards + OTM smile + OI +
    GEX + chain table). MOEX-honest provenance; greeks computed (Black-Scholes on the share), IV from
    the exchange. The header carries the SPOT top-right (v1.110 parity with every other dashboard)."""
    strikes, atm = an["strikes"], an["atmStrike"]
    iv_col, interp = _smile_iv(an)
    smile_put = [iv_col[i] if k <= atm else None for i, k in enumerate(strikes)]
    smile_call = [iv_col[i] if k >= atm else None for i, k in enumerate(strikes)]
    gex = gex_ladder(an, iv_col)
    known = [v for v in gex if v is not None]
    net_gex = sum(known) if known else None
    gex_approx = "≈" if (known and any(v is None for v in gex)) else ""
    # ATM greeks from the ATM composite IV — Γ/vega are call≡put by parity, but Θ differs (materially at
    # the high RUB rate), so use the SAME leg the ATM IV came from (review P3).
    g = _greeks_at(an["spot"], atm, (an["atmIV"] / 100.0 if an["atmIV"] else None), an["dte"], an["rate"],
                   bool(an.get("atmLegCall")))
    _slab = _series_label(an)

    def _x(card):
        return dict(card, scope="expiry", scopeLabel=_slab)
    cards = [
        _x({"label": "ATM IV", "value": _ivfmt(an["atmIV"]),
            "hint": "Implied volatility at the at-the-money strike — the EXCHANGE's own value (ISS VOLAT), "
                    "annualized. Read as-is; the skill never recomputes IV on this path."}),
        _x({"label": "Exp move", "value": ("n/a" if an["expMove"] is None else
                                           "±₽%s (±%s%%)" % (_num(an["expMove"]), _num(an["expMovePct"], 1))),
            "hint": "The 1σ move priced in by expiry: spot × ATM IV × √(DTE/365), in roubles. "
                    "~68% of outcomes land inside if the IV is 'right'; a straddle breaks even near its edges."}),
        _x({"label": "25Δ skew", "value": ("n/a" if an["skew25"] is None else "%+.1f" % an["skew25"]),
            "hint": "IV(put ~10% OTM) − IV(call ~10% OTM), vol points (a moneyness proxy, not true delta "
                    "buckets). Positive = downside puts richer than upside calls."}),
        _x({"label": "PCR (OI)", "value": _num(an["pcrOI"], 2), "sub": _pcr_vol_sub(an),
            "hint": "Put/call ratio by open interest over the ladder. >1 = more puts outstanding; hedging "
                    "flows skew it — positioning, not a direction call. The sub is PCR by today's VOLUME "
                    "(flow); a divergence word fires when flow leans differently than the standing book."}),
        _x({"label": "Max pain", "value": _num(an["maxPain"]),
            "hint": "The strike minimizing option writers' total intrinsic payout at expiry. A pin-gravity "
                    "heuristic near expiry, not a forecast."}),
        _x({"label": "DTE", "value": ("n/a" if an["dte"] is None else str(an["dte"])),
            "hint": "Calendar days to this series' expiration. Greeks/decay quoted per calendar day."}),
        _x({"label": "Net GEX", "value": ("n/a" if net_gex is None else gex_approx + _num(net_gex)),
            "hint": "Σ Γ×netOI across strikes (Black-Scholes Γ from the exchange IV). Sign = the "
                    "naive dealer assumption (long calls / short puts). Positive dampens moves, negative amplifies."}),
        _x({"label": "Γ (ATM)", "value": _num(g.gamma, 4) if g else "n/a",
            "hint": "Gamma at the ATM strike (Black-Scholes, computed): Δ change per ₽1 share move, per share."}),
        _x({"label": "Θ (ATM/day)", "value": _num(g.theta / 365.0, 3) if g else "n/a",
            "hint": "ATM daily time decay (computed), roubles per share per calendar day. Accelerates into expiry."}),
    ]

    def _ivcell(i):
        s = _ivfmt(iv_col[i])
        return "~" + s if (interp[i] and iv_col[i] is not None) else s
    dg = [_greeks_at(an["spot"], k, (iv_col[i] / 100.0 if iv_col[i] else None), an["dte"], an["rate"], k > atm)
          for i, k in enumerate(strikes)]

    def _sprcell(i):
        s = an["otmSpr"][i]
        return ("%.1f%%" % s) if s is not None else "n/a"
    rows = [[_num(k), _ivcell(i), (_num(x.delta, 2) if x else "n/a"),
             _num(an["callOI"][i], 0), _num(an["putOI"][i], 0), _sprcell(i),
             (_num(x.vega / 100.0, 3) if x else "n/a"),       # vega per 1 vol point (OTM leg)
             (_num(x.theta / 365.0, 3) if x else "n/a")]      # theta per calendar day (OTM leg)
            for i, (k, x) in enumerate(zip(strikes, dg))]
    ai = strikes.index(atm) if atm in strikes else None

    def _rowstyle(i):
        if ai is None or abs(i - ai) > 10:
            return ""
        if i == ai:
            return "background:rgba(74,134,240,0.60);font-weight:600"
        a = 0.46 * (1 - abs(i - ai) / 10)
        return "background:rgba(74,134,240,%.3f)" % a if a >= 0.001 else ""
    rowstyle = [_rowstyle(i) for i in range(len(strikes))]
    _est = " est" if an.get("spotEstimated") else ""
    # v1.141: the OI panel is the stacked "book with caps" (per side a BASE = min(today, baseline), a green
    # BUILD cap = growth, a translucent red GHOST = unwound, top edge at the baseline) with a 1d/1w/1m/1Q
    # window toggle (the engine's `variants` vtoggle) — when a ΔOI baseline resolved; else the plain 2-series
    # chart + an honest note. One scale (contracts): the segment sizes ARE the ΔOI. Mirrors the equity panel.
    _GHOST = "rgba(227,73,72,0.35)"
    _doi = an.get("doi")

    def _oi_seg(cur, dlt):
        base, build, ghost = [], [], []
        for c0, dl in zip(cur, dlt):
            if c0 is None:                       # no bar at all
                base.append(0); build.append(0); ghost.append(0)
            elif dl is None:                     # listed, delta unknown -> a plain bar
                base.append(c0); build.append(0); ghost.append(0)
            else:
                y = c0 - dl
                base.append(min(c0, y)); build.append(max(0.0, dl)); ghost.append(max(0.0, -dl))
        return base, build, ghost

    def _oi_stack(dc, dp, lab):
        _cb, _cbl, _cgh = _oi_seg(an["callOI"], dc)
        _pb, _pbl, _pgh = _oi_seg(an["putOI"], dp)
        return [
            {"name": "Call OI", "color": "#2a78d6", "stack": "c", "values": _cb},
            {"name": "+ built (%s)" % lab, "color": "#1baf7a", "stack": "c", "values": _cbl},
            {"name": "− unwound (%s)" % lab, "color": _GHOST, "stack": "c", "values": _cgh},
            {"name": "Put OI", "color": "#eb6834", "stack": "p", "values": _pb},
            {"name": "+ built (%s)" % lab, "color": "#1baf7a", "stack": "p", "values": _pbl, "legendHide": True},
            {"name": "− unwound (%s)" % lab, "color": _GHOST, "stack": "p", "values": _pgh, "legendHide": True}]

    _oi_base = {"labels": strikes, "markers": {"maxPain": an["maxPain"]}}
    _win_menu = " / ".join(an.get("doiWindows") or [])

    def _oi_info(date, lab):
        return ("Contracts OUTSTANDING per strike and side (call blue / put orange) with the change over "
                "the selected horizon (%s) overlaid: the green cap = contracts BUILT since %s; the "
                "translucent red ghost = UNWOUND (its top edge = the baseline level). Toggle the horizon "
                "(%s) top-right — 1w = 5 trading days, 1m/1Q = a calendar month/quarter ago snapped to a "
                "trading day (an unpublished/holiday date walks one business day further, and THAT date is "
                "the honest baseline). One scale — the segment sizes ARE the ΔOI; hover for exact numbers. "
                "From the MOEX assetcode SETTLEMENT history (delayed, immutable, cached forever). A strike — "
                "or a whole expiration — absent at the baseline counts from 0 and reads all-green (it did "
                "not exist then; common on short weeklies under 1m/1Q). A strike unwound to zero leaves the "
                "ladder (no ghost). Positioning, not volume; red dashes = max pain." % (lab, date, _win_menu))
    if _doi:
        _opts = [{"name": w, "title": "Open interest by strike · Δ vs %s (%s)" % (_doi[w]["date"], w),
                  "info": _oi_info(_doi[w]["date"], w),
                  "data": dict(_oi_base, series=_oi_stack(_doi[w]["dOICall"], _doi[w]["dOIPut"], w))}
                 for w in an["doiWindows"]]
        _oi_panel = {"type": "bars", "title": _opts[0]["title"], "info": _opts[0]["info"], "full": True,
                     "data": _opts[0]["data"], "variants": {"label": "Δ", "index": 0, "options": _opts}}
    else:
        _oi_panel = {"type": "bars", "title": "Open interest by strike", "full": True, "data": dict(
            _oi_base, series=[{"name": "Call OI", "color": "#2a78d6", "values": [x or 0 for x in an["callOI"]]},
                              {"name": "Put OI", "color": "#eb6834", "values": [x or 0 for x in an["putOI"]]}]),
            "info": ("Open contracts per strike and side — positioning, not volume. Walls = hedging "
                     "concentration / pin candidates into expiry; red dashes = max pain. The Δ overlay "
                     "(build/unwind vs a prior settlement) is not computed for this render (offline "
                     "--from-dir, a throttled baseline pull, or MARKET_DATA_DROI=0) — re-render online.")}
    return {
        # v1.110 UNIFIED HEADER (user request): stacked ticker + name + board type on the left, the SPOT
        # top-right (change% under it). ' est' marks a spot fell back to the board settlement (no live LAST).
        "head": {"leftBig": an["symbol"], "leftName": an.get("name") or "", "leftType": "Options",
                 "rightBig": "₽" + _num(an["spot"]) + _est, "rightSmall": _right_small(an)},
        "lead": _lead(an),
        "title": "%s options%s" % (an["symbol"], (" · " + _slab) if _slab != "near series" else ""),
        "dataSource": "MOEX ISS · delayed ~15 min",
        "cards": cards,
        "panels": [
            {"type": "smile", "title": "Vol smile (OTM composite)", "full": True, "info":
                "Implied vol per strike — the exchange's own IVs (ISS VOLAT, read as-is): the OTM "
                "composite (put IV below ATM, call above). A bracketed hole is interpolated and marked "
                "'~'; edge gaps stay empty (never extrapolate a wing). Red dashes = max pain.", "data": {
                "strikes": strikes, "atm": atm, "putIV": smile_put, "callIV": smile_call,
                "interp": interp, "markers": {"maxPain": an["maxPain"]}}},
            _oi_panel,
            {"type": "bars", "title": "Dealer gamma exposure (GEX) by strike", "allowEmpty": True, "info":
                "Γ × net OI per strike (Black-Scholes Γ from the exchange IV, computed). Sign = the "
                "naive dealer assumption (long calls / short puts — real positioning isn't observable). "
                "A strike with no solvable greek is dropped (n/a ≠ 0).", "data": {
                "labels": strikes, "series": [{"name": "GEX (Γ×netOI)", "values": gex, "colorBySign": True}]}},
            {"type": "bars", "title": "Turnover (vol/OI) by strike", "allowEmpty": True, "info":
                "Today's traded VOLUME ÷ the standing open interest per strike (both legs) — where FRESH "
                "flow concentrates vs legacy positioning. A strike with <%d standing contracts is dropped "
                "(a tiny book makes the ratio noise, not signal). ISS VOLTODAY is delayed ~15 min." % _TURN_OI_FLOOR,
             "data": {"labels": strikes, "series": [{"name": "Turnover", "values": an["turnover"], "color": "#8a63d2"}]}},
            {"type": "table", "title": "Chain (full tradeable ladder)", "info":
                "One row per tradeable strike. IV = the same OTM composite as the smile (exchange VOLAT; "
                "'~' = interpolated; 'n/a' = no reliable value). Δ is the OTM leg's Black-Scholes delta "
                "(signed by wing) from that IV + the RUSFAR rate, NO dividend yield (q=0 — MOEX dividends "
                "are discrete, so Δ is approximate across a dividend date). Spread = (ask−bid)/mid on the "
                "OTM leg (tradeability; n/a = no/crossed quote). Vega = ₽/share per 1 vol point; Θ/day = "
                "₽/share decay per calendar day — both the OTM leg (Black-Scholes, computed). Blue heat = "
                "proximity to ATM.", "data": {
                "columns": ["Strike", "IV", "Δ (OTM)", "Call OI", "Put OI", "Spread", "Vega", "Θ/day"],
                "rows": rows, "rowstyle": rowstyle}},
        ],
    }


# ----------------- text -----------------
def render_text(an):
    L = []
    _spot = "₽%s%s" % (_num(an["spot"]), " (settle)" if an.get("spotEstimated") else "")
    L.append("%s OPTIONS (MOEX) · %s · spot %s · IV=exchange, greeks computed"
             % (an["symbol"], _series_label(an), _spot))
    sk = "n/a" if an["skew25"] is None else "%+.1f pts" % an["skew25"]
    em = ("n/a" if an["expMove"] is None else "±₽%s (±%s%%)" % (_num(an["expMove"]), _num(an["expMovePct"], 1)))
    L.append("ATM IV %s (K %s)  ·  exp move %s  ·  25Δ skew %s" % (_ivfmt(an["atmIV"]), _num(an["atmStrike"]), em, sk))
    _spr = ("n/a" if an.get("medSpr") is None else "%.1f%%" % an["medSpr"])
    _to = ("n/a" if an.get("turnoverAll") is None else _num(an["turnoverAll"], 2))
    L.append("PCR(OI) %s  ·  PCR(vol) %s  ·  max pain %s  ·  call OI %s / put OI %s  ·  med OTM spread %s  ·  turnover %s overall"
             % (_num(an["pcrOI"], 2), _num(an.get("pcrVol"), 2), _num(an["maxPain"]),
                _num(an["callOItotal"], 0), _num(an["putOItotal"], 0), _spr, _to))
    L.append("")
    iv_col, interp = _smile_iv(an)
    L.append("%8s %9s %9s %9s" % ("STRIKE", "IV", "CALL_OI", "PUT_OI"))
    for i, k in enumerate(an["strikes"]):
        mark = "  <ATM" if k == an["atmStrike"] else ("  <pain" if k == an["maxPain"] else "")
        L.append("%8s %9s %9s %9s%s" % (_num(k), _ivfmt(iv_col[i]), _num(an["callOI"][i], 0), _num(an["putOI"][i], 0), mark))
    L.append("")
    L.append("source: MOEX ISS (delayed ~15 min) · IV = exchange VOLAT · greeks Black-Scholes (computed) · "
             "data analysis, not investment advice")
    return "\n".join(L)


# ----------------- term (cross-expiration) -----------------
_TERM_BUCKETS = [0.80, 0.85, 0.90, 0.95, 1.00, 1.05, 1.10, 1.15, 1.20]   # moneyness cols (±2.5% tol)


def gather_term(sym, fetch=None, today=None, max_series=None):
    """Pull EVERY listed series' board and build each chain — the cross-expiration inventory. Profile
    (spot/name/asOf) and the RUSFAR rate are fetched ONCE and reused (the rate is the 1M tenor, the
    longest RUSFAR publishes — reused across the term; it only nudges Δ/Γ, not the exchange-IV smile).
    A series too thin to chart (<2 strikes) is skipped. Returns T = {symbol, name, asOf, series:[an…],
    surface}."""
    today = today or dt.date.today()
    exps = fetch_expirations(sym, fetch=fetch)
    if not exps:
        raise ChainError("error: no listed expirations for %s (bad ticker / non-optionable)" % sym)
    profile = fetch_profile(sym, fetch=fetch)
    rate = fetch_rate(30, fetch=fetch)   # RUSFAR 1M (longest tenor) reused across the term — documented
    _exps = exps[:(max_series or len(exps))]

    def _one_series(e):   # v1.143: pull each series' board CONCURRENTLY (was sequential — 20 series ≈ 10s)
        try:
            board = fetch_board(sym, e["expiration"], fetch=fetch)
            an = build_chain(sym, board, profile, rate, e["expiration"], today=today)
            an["seriesType"] = e.get("series_type")
            return an
        except ChainError:
            return None
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(12, len(_exps) or 1)) as ex:
        series = [an for an in ex.map(_one_series, _exps) if an is not None]
    if not series:
        raise ChainError("error: no %s series has enough strikes to chart" % sym)
    series.sort(key=lambda a: (a["dte"] if a["dte"] is not None else 10 ** 9))
    try:   # v1.141: ONE ΔOI baseline fetch (per assetcode+asof, 4 windows cached) covers ALL series' SECIDs
        _bl = fetch_oi_baselines(sym, profile.get("asOf"), fetch=fetch)
        for an in series:
            _attach_droi(an, _bl)
    except (Exception, SystemExit):
        pass   # ΔOI is best-effort — a baseline failure degrades to the plain OI panel
    return {"symbol": sym.upper(), "name": profile.get("name") or "", "asOf": profile.get("asOf"),
            "series": series, "surface": _iv_surface(series), "oiSurface": _oi_surface(series)}


def _iv_surface(series):
    """OTM-leg IV (percent) at the nearest listed strike per moneyness bucket × series — each row keyed
    to its OWN spot (like futures, keyed to each forward). None where no strike sits within ±2.5%."""
    cols = ["%d%%" % round(b * 100) for b in _TERM_BUCKETS]
    matrix = []
    for an in series:
        spot = an["spot"]
        iv_col, _ = _smile_iv(an)
        row = []
        for b in _TERM_BUCKETS:
            target = b * spot
            cand = [(abs(k - target), iv_col[i]) for i, k in enumerate(an["strikes"])
                    if iv_col[i] is not None and abs(k - target) <= 0.025 * spot]
            row.append(round(min(cand)[1], 1) if cand else None)
        matrix.append(row)
    return {"cols": cols, "matrix": matrix}


def _oi_surface(series):
    """The OI twin of _iv_surface: total open interest (call+put) at the nearest listed strike per
    moneyness bucket × series — where positioning concentrates across the curve. None where no strike
    sits within ±2.5% of the bucket (holes stay holes; 0 = a real datum where a strike exists but has
    no OI)."""
    cols = ["%d%%" % round(b * 100) for b in _TERM_BUCKETS]
    matrix = []
    for an in series:
        spot = an["spot"]
        row = []
        for b in _TERM_BUCKETS:
            target = b * spot
            cand = [(abs(k - target), (an["callOI"][i] or 0) + (an["putOI"][i] or 0))
                    for i, k in enumerate(an["strikes"]) if abs(k - target) <= 0.025 * spot]
            row.append(min(cand)[1] if cand else None)
        matrix.append(row)
    return {"cols": cols, "matrix": matrix}


_QMONTHS = {3, 6, 9, 12}   # MOEX FORTS quarterly months (Mar/Jun/Sep/Dec — H/M/U/Z), the deepest series


def _series_marks(S):
    """Mark the NEAREST quarterly expiration (a standard Mar/Jun/Sep/Dec series — the deepest, most-traded
    tenor) and the MOST LIQUID one — the two series a reader usually reaches for first, like the equity/
    futures listings. Returns {index: "Q"|"★"|"Q★"}. S is dte-sorted (nearest first). Liquidity is today's
    VOLUME (actual trading), NOT open interest: on names that list deep-OTM LEAPS call walls (GAZP out to
    2030) total OI is dominated by held far-call positions nobody trades, so max-OI would flag a dead LEAPS
    — volume flags the series actually changing hands. Fallback to total OI only if the term has no volume
    yet (a quiet day / pre-session)."""
    marks = {}

    def _is_q(a):   # ISS's own W/M/Q series_type is authoritative when present; else the standard months
        st = a.get("seriesType")
        if st:
            return st == "Q"
        d = _parse_date(a.get("expiration") or "")
        return d is not None and d.month in _QMONTHS
    for i, a in enumerate(S):   # nearest quarterly: the first Q series that hasn't expired
        if _is_q(a) and (a.get("dte") is None or a["dte"] >= 0):
            marks[i] = "◆"
            break

    def _sum(a, k):
        return sum(v for v in (a.get(k) or []) if v)
    vols = [_sum(a, "callVol") + _sum(a, "putVol") for a in S]
    if any(vols):
        li = max(range(len(S)), key=lambda i: vols[i])
    else:   # no trading yet today -> fall back to open interest
        ois = [((a.get("callOItotal") or 0) + (a.get("putOItotal") or 0)) for a in S]
        li = max(range(len(S)), key=lambda i: ois[i]) if any(ois) else None
    if li is not None:
        marks[li] = marks.get(li, "") + "★"
    return marks


def to_term_spec(T):
    """The term analysis -> a dashboard SPEC: ATM-IV/skew curve + IV surface + per-series table."""
    S = T["series"]
    front = S[0]
    slope = (round(S[-1]["atmIV"] - front["atmIV"], 1)
             if (len(S) > 1 and S[-1]["atmIV"] is not None and front["atmIV"] is not None) else None)
    marks = _series_marks(S)

    def _oi(x):   # compact OI: 5041584 -> "5.0M", 731502 -> "732k", 1665 -> "1665", 0/None -> "—"
        x = int(x or 0)
        if x >= 1_000_000:
            return "%.1fM" % (x / 1e6)
        if x >= 1000:
            return "%.0fk" % (x / 1e3)
        return str(x) if x else "—"
    # v1.145: per-row OI HEAT (AV parity, mirrors options._oi_heat_rowstyle) — shade EACH row by its total
    # (call+put) open interest, LOG-scaled (OI is very skewed) on the seq 'hot' red hue matching the OI
    # surface, so every large expiration reads at a glance (not just the single ★). A 0-OI row stays blank;
    # the ◆/★ semantic rides the Expiry glyph + the legend, NOT the fill (so the key survives the heat).
    import math
    _ois = [((r.get("callOItotal") or 0) + (r.get("putOItotal") or 0)) for r in S]
    _pos = [o for o in _ois if o > 0]
    _olo, _ohi = (math.log(min(_pos)), math.log(max(_pos))) if _pos else (0.0, 0.0)
    rows, rowstyle = [], []
    for i, r in enumerate(S):
        mk = marks.get(i, "")
        rows.append([r["expiration"] + ((" " + mk) if mk else ""),
                     ("n/a" if r["dte"] is None else str(r["dte"])),
                     (r.get("seriesType") or ""), _ivfmt(r["atmIV"]),
                     ("%+.1f" % r["skew25"] if r["skew25"] is not None else "n/a"),
                     _oi(r.get("callOItotal")), _oi(r.get("putOItotal")), str(r["nStrikes"])])
        _o = _ois[i]
        _t = 0.0 if (_o <= 0 or _ohi <= _olo) else (math.log(_o) - _olo) / (_ohi - _olo)
        rowstyle.append(("background:rgba(226,75,74,%s)" % round(0.06 + 0.40 * _t, 3)) if _o > 0 else "")
    # skew term slope: farthest − front 25Δ skew (does downside skew steepen with tenor?)
    _sku = [s for s in (r.get("skew25") for r in S) if s is not None]
    sk_slope = round(_sku[-1] - _sku[0], 1) if len(_sku) >= 2 else None
    # per-series ATM greeks (Black-Scholes at the ATM strike from the exchange ATM IV) + vega-tenor
    _atmg = [_greeks_at(r["spot"], r["atmStrike"], (r["atmIV"] / 100.0 if r["atmIV"] else None),
                        r["dte"], r["rate"], bool(r.get("atmLegCall"))) for r in S]

    def _peaknorm(vals):   # scale each greek to its own PEAK magnitude (=1) so the three term SHAPES compare
        av = [abs(v) if v is not None else None for v in vals]
        m = max((v for v in av if v is not None), default=0.0)
        return [(v / m if (v is not None and m) else None) for v in av]
    _gam = _peaknorm([(g.gamma if g else None) for g in _atmg])
    _veg = _peaknorm([(g.vega if g else None) for g in _atmg])
    _the = _peaknorm([(g.theta if g else None) for g in _atmg])
    _vn = _vd = 0.0   # vega-exposure-weighted average DTE — the surface's effective duration
    for i, r in enumerate(S):
        g = _atmg[i]
        if g and r.get("dte") is not None:
            w = abs(g.vega) * ((r.get("callOItotal") or 0) + (r.get("putOItotal") or 0))
            _vn += r["dte"] * w
            _vd += w
    vtenor = (_vn / _vd) if _vd else None
    _xlab = [r["expiration"][2:] for r in S]
    return {
        "title": "%s options · term structure · %d series" % (T["symbol"], len(S)),
        "dataSource": "MOEX ISS · delayed ~15 min",
        "cards": [
            {"label": "Series", "value": str(len(S)),
             "hint": "Listed expirations charted (nearest first; a series too thin to chart is dropped)."},
            {"label": "Front ATM IV", "value": _ivfmt(front["atmIV"]),
             "hint": "ATM implied vol of the nearest expiration — the short end of the curve (exchange VOLAT)."},
            {"label": "Term slope", "value": ("%+.1f" % slope if slope is not None else "n/a"),
             "hint": "Farthest ATM IV minus front, vol points. Positive = contango (normal); negative = "
                     "inverted — near-dated stress prices above the back."},
            {"label": "Skew term slope", "value": ("%+.1f" % sk_slope if sk_slope is not None else "n/a"),
             "hint": "Farthest minus front 25Δ skew (vol pts). Positive = downside skew STEEPENS with tenor "
                     "(long-dated crash premium richer); negative = the near-term skew is steeper (front fear)."},
            {"label": "Vega tenor", "value": ("~%dd" % round(vtenor) if vtenor is not None else "n/a"),
             "hint": "Vega-exposure-weighted average DTE: Σ(DTE · ATM vega · total OI) ÷ Σ(ATM vega · OI) — "
                     "where the listed vega exposure concentrates along the term (the surface's duration)."},
            {"label": "Front DTE", "value": ("n/a" if front["dte"] is None else str(front["dte"])),
             "hint": "Calendar days to the nearest expiration."},
        ],
        "panels": [
            {"type": "line", "title": "ATM IV & 25Δ skew by expiration", "full": True, "info":
                "The vol term structure from the exchange's per-contract IVs. Solid = ATM IV; dashed = the "
                "~10%-OTM skew proxy in vol points. Upward = contango; a front bump = event risk. The dashed "
                "skew line BREAKS at any far series with no strike ~10% OTM (a thin LEAPS ladder → skew n/a — "
                "an honest hole, matching the 'n/a' in the table's 25Δ skew column), so it spans only the "
                "series where a wing strike exists.",
             "data": {"labels": _xlab, "series": [
                 {"name": "ATM IV %", "values": [r["atmIV"] for r in S]},
                 {"name": "25Δ skew (vol pts)", "values": [r["skew25"] for r in S], "dash": [6, 4]}]}},
            {"type": "line", "title": "Put/call ratio (OI) by expiration", "full": True, "info":
                "Put/call ratio by open interest per expiration (put OI ÷ call OI) — the positioning term "
                "structure. >1 = more puts than calls outstanding on that tenor. NOTE: a far LEAPS carrying a "
                "deep-OTM CALL wall reads very low here (held far calls, not tradeable flow) — read the near "
                "tenors, where the number reflects real hedging.",
             "data": {"labels": _xlab, "series": [{"name": "PCR (OI)", "values": [r.get("pcrOI") for r in S]}]}},
            {"type": "line", "title": "ATM greeks by expiration (each scaled to its peak)", "full": True, "info":
                "ATM Γ / vega / Θ per expiration (Black-Scholes at the ATM strike, computed) — each series "
                "scaled to its OWN peak magnitude (=1) so the three different-scale greeks compare as term "
                "SHAPES on one axis: Γ and Θ peak near-term (gamma/decay concentrate at the front), vega peaks "
                "far-term (long-dated options carry more vol sensitivity). Absolute per-contract values live on "
                "the Chain tab; a missing greek is a gap, never fabricated.",
             "data": {"labels": _xlab, "y1pct": False, "series": [
                 {"name": "Γ (ATM)", "values": _gam},
                 {"name": "vega", "values": _veg, "dash": [6, 4]},
                 {"name": "Θ decay", "values": _the, "dash": [2, 3]}]}},
            dashboard.add_hm_color_toggle({"type": "heatmap", "title": "IV surface (moneyness × expiry)", "info":
                "OTM-leg IV at the nearest listed strike per 5% moneyness bucket — keyed to EACH series' "
                "own spot. n/a = no strike within ±2.5% (holes stay holes, nothing interpolated across "
                "series). Hotter = richer vol. linear/log toggles the colour scale.",
             "data": {"rows": ["%s · %sd" % ((r.get("seriesType") or ""), r["dte"]) for r in S],
                      "cols": T["surface"]["cols"], "matrix": T["surface"]["matrix"], "ramp": "seq"}}),
            dashboard.add_hm_color_toggle({"type": "heatmap", "title": "Open interest by moneyness × expiry", "info":
                "Total open interest (call+put) at the nearest listed strike per 5% moneyness bucket — the OI "
                "twin of the IV surface. Hotter = more open contracts (where positioning concentrates across "
                "the curve; the deep-OTM call walls on a name like GAZP light up the far/high-moneyness cells). "
                "0 = a real datum (a strike with no OI); n/a = no listed strike within ±2.5%. linear/log toggles "
                "the scale — OI is very skewed, so log spreads the mid-range.",
             "data": {"rows": ["%s · %sd" % ((r.get("seriesType") or ""), r["dte"]) for r in S],
                      "cols": T["oiSurface"]["cols"], "matrix": T["oiSurface"]["matrix"],
                      "ramp": "seq", "cellFmt": "int"}}),
            {"type": "table", "title": "Series", "info":
                "One row per listed expiration. ATM IV = exchange VOLAT; 25Δ skew = the ~10%-OTM proxy; "
                "Call/Put OI = total open interest that series. Row shade = total OI (call+put), log-scaled "
                "red — hotter = more open contracts (where positioning concentrates across the curve). "
                "n/a = no solvable value — never filled.",
             "data": {"columns": ["Expiry", "DTE", "Type", "ATM IV", "25Δ skew", "Call OI", "Put OI", "Strikes"],
                      "rows": rows, "rowstyle": rowstyle, "legend": [
                          {"sym": "▰", "color": "rgba(226,75,74,.46)", "label": "total OI (call+put), log-scaled"},
                          {"sym": "★", "color": "#1D9E75", "label": "most liquid (today's volume)"},
                          {"sym": "◆", "color": "#2a78d6", "label": "nearest quarterly (MOEX W/M/Q class)"}]}},
        ],
    }


def combined_spec(specs, T, target_idx=0):
    """A SWITCHABLE combined page: the chosen series' chain (smile/OI/GEX/table) + an EXPIRATION
    SWITCHER swapping which series those charts show (every series' page pre-built — greeks computed
    once), plus the cross-expiration term panels appended below as the fixed overview."""
    S = T["series"]
    tsp = to_term_spec(T)
    chain_n = len(specs[target_idx]["panels"])
    panels = []
    for j in range(chain_n):
        p = dict(specs[target_idx]["panels"][j])
        if p.get("variants"):   # v1.141: the ΔOI OI panel — series[i] IS expiration i's window options-set;
            #                      switchSeries re-points variants.options to series[idx] keeping the window index
            p["series"] = [specs[i]["panels"][j].get("variants", {}).get(
                "options", [{"data": specs[i]["panels"][j]["data"]}]) for i in range(len(specs))]
            p["seriesIsVariants"] = True
        else:
            p["series"] = [specs[i]["panels"][j]["data"] for i in range(len(specs))]
        panels.append(p)
    panels += tsp["panels"]
    perm = [c for c in (next((x for x in tsp["cards"] if x["label"] == lab), None)
                        for lab in ("Series", "Front ATM IV", "Term slope")) if c]
    card_series = [perm + list(specs[i]["cards"]) for i in range(len(specs))]
    out = dict(specs[target_idx])
    out["title"] = tsp["title"]
    out["panels"] = panels
    out["cards"] = card_series[target_idx]
    out["cardSeries"] = card_series
    _marks = _series_marks(S)
    out["switcher"] = {"label": "Expiration", "index": target_idx,
                       "options": ["%s · %s DTE%s" % (s["expiration"], s["dte"],
                                                      (" · " + _marks[i]) if i in _marks else "")
                                   for i, s in enumerate(S)],
                       "expiries": [s["expiration"] for s in S],
                       "hint": "switch which expiration the chain charts (smile / OI / GEX / ladder) show — "
                               "the term panels below stay as the cross-expiration overview. "
                               "◆ = nearest quarterly; ★ = most liquid (highest volume today)"}
    return out


# ----------------- fetch orchestration -----------------
def gather(sym, expiration=None, fetch=None, today=None):
    """Fetch the board + share profile + rate and build the chain for one series (or the near series)."""
    board = fetch_board(sym, expiration, fetch=fetch)
    profile = fetch_profile(sym, fetch=fetch)
    _exp = expiration or (board.get("asset") or {}).get("LASTDELDATE")
    _ed = _parse_date(_exp)
    _dte = (_ed - (today or dt.date.today())).days if _ed else None
    rate = fetch_rate(_dte, fetch=fetch)
    an = build_chain(sym, board, profile, rate, expiration, today=today)
    try:   # v1.141: attach the ΔOI windows (best-effort — a baseline failure degrades to the plain OI panel)
        _attach_droi(an, fetch_oi_baselines(sym, an.get("asOf"), fetch=fetch))
    except (Exception, SystemExit):
        pass
    return an


def moex_session(now=None):
    """The MOEX FORTS (derivatives) session tile: Mon–Fri ~09:00–23:50 Moscow time (the main + evening
    session, simplified — the midday clearing pauses are not modelled). Returns the phase + an
    open/close schedule the engine live-ticks (countdown.phaseFromSchedule). A missing tz database ->
    None (the tile is omitted, never faked). NB: the DATA is still delayed ~15 min — this is the venue
    clock, not a live-price claim."""
    try:
        from zoneinfo import ZoneInfo
        mz = ZoneInfo("Europe/Moscow")
    except Exception:
        return None
    now = (now or dt.datetime.now().astimezone())
    if now.tzinfo is None:
        now = now.astimezone()
    msk = now.astimezone(mz)
    midnight = msk.replace(hour=0, minute=0, second=0, microsecond=0)
    sched = []
    for dday in range(0, 9):
        day = midnight + dt.timedelta(days=dday)
        if day.weekday() <= 4:                     # Mon–Fri
            o = day.replace(hour=9)
            c = day.replace(hour=23, minute=50)
            if o > msk:
                sched.append({"at": int(o.timestamp() * 1000), "label": "opens"})
            if c > msk:
                sched.append({"at": int(c.timestamp() * 1000), "label": "closes"})
    sched.sort(key=lambda x: x["at"])
    sched = sched[:8]
    nxt = sched[0] if sched else None
    status = "Open" if (nxt and nxt["label"] == "closes") else (
        "Closed · weekend" if msk.weekday() >= 5 else "Closed")

    def _dur(secs):
        secs = max(0, int(secs))
        d_, h, m = secs // 86400, secs % 86400 // 3600, secs % 3600 // 60
        return ("%dd %dh" % (d_, h) if d_ else ("%dh %02dm" % (h, m) if h else "%dm" % m))
    cd = (nxt["label"] + " in " + _dur(nxt["at"] / 1000 - msk.timestamp())) if nxt else ""
    return {"status": status, "schedule": sched, "countdownText": cd,
            "hoursMsk": "Mon–Fri 09:00–23:50 MSK (data delayed ~15 min)"}


# MOEX vol-history provenance: the store's IV is RECOMPUTED (not exchange-native like the live board),
# reconstructed from EVERY contract that traded across the window (incl. now-expired ones — v1.136 dense
# backfill), so the constant-maturity history is dense back to the discovery window's start.
_HIST_SUB = ("MOEX ISS settlement history · IV RECOMPUTED (Black-Scholes on the share) · dense "
             "reconstruction across listed & expired contracts · delayed — not investment advice")
_SKEW_HINT = ("IV(put ~10% OTM) − IV(call ~10% OTM), vol points — a moneyness proxy. Positive = "
              "downside puts richer (the usual single-name shape).")


def build_full_spec(sym, T, specs, target_idx, hist=None):
    """The COMBINED [Overview · Chain · Term · History] page — the equity/futures build_full mirror on
    the sections engine. Chain = the switchable per-expiration drill (combined_spec); Term = the
    cross-expiration overview (to_term_spec); Overview = a LIVE synthesis from the term pull (session
    tile + front ATM/skew + term slope + PCR); History = opthist's builder over the MOEX vol store when
    warm (`hist`), else a 'warming' placeholder. The unified header + lead ride from the target spec."""
    combined = combined_spec(specs, T, target_idx)
    n_chain = len(specs[target_idx]["panels"])
    chain = {"id": "smile", "name": "Chain", "cards": combined["cards"],
             "panels": combined["panels"][:n_chain], "switcher": dict(combined["switcher"]),
             "cardSeries": combined["cardSeries"]}
    chain["switcher"]["hint"] = "chain (smile · OI · GEX · ladder) for the picked expiration"
    tsp = to_term_spec(T)
    term = {"id": "term", "name": "Term", "cards": tsp["cards"], "panels": tsp["panels"]}
    # Overview — live synthesis from the term pull (never blank; History is the only 'warming' tab)
    S = T["series"]
    front = S[0] if S else {}
    usable = [s for s in S if s.get("atmIV") is not None]
    if len(usable) >= 2:
        f2, b2 = usable[0]["atmIV"], usable[-1]["atmIV"]
        slope_lbl = "flat" if abs(f2 - b2) < 0.05 else ("front-rich" if f2 > b2 else "back-rich")
        slope_sub = ["%.1f%% → %.1f%%" % (f2, b2)]
    else:
        slope_lbl, slope_sub = "n/a", None
    ov_cards = [
        {"label": "ATM IV · front", "value": _ivfmt(front.get("atmIV")), "scope": "expiry",
         "scopeLabel": ("nearest · %sd" % front["dte"]) if front.get("dte") is not None else "nearest",
         "hint": "At-the-money implied vol of the nearest expiration — the exchange's VOLAT."},
        {"label": "25Δ skew · front",
         "value": ("%+.1f" % front["skew25"]) if front.get("skew25") is not None else "n/a",
         "scope": "expiry",
         "scopeLabel": ("nearest · %sd" % front["dte"]) if front.get("dte") is not None else "nearest",
         "hint": "IV(put ~10% OTM) − IV(call ~10% OTM), vol points. Positive = downside puts richer."},
        {"label": "Term slope", "value": slope_lbl, "sub": slope_sub,
         "hint": "Nearest vs farthest ATM IV in the term pull. Front-rich = a near-term event; "
                 "back-rich = contango."},
        {"label": "PCR · OI · front", "value": ("%.2f" % front["pcrOI"]) if front.get("pcrOI") is not None else "n/a",
         "scope": "expiry", "scopeLabel": ("nearest · %sd" % front["dte"]) if front.get("dte") is not None else "nearest",
         "hint": "Put/call open interest of the nearest expiration."},
    ]
    sess = moex_session()
    if sess:
        ov_cards.insert(0, {"label": "Session · FORTS", "value": sess["status"], "small": True,
                            "countdown": {"schedule": sess["schedule"], "text": sess["countdownText"],
                                          "phaseFromSchedule": True},
                            "hint": "MOEX derivatives session (%s). The countdown ticks live in your "
                                    "browser; the DATA stays delayed ~15 min — this is the venue clock, "
                                    "not a live price." % sess["hoursMsk"]})
    # the WARM store gives the Overview its retrospective context (cheap/rich) — mirrors the futures/equity
    # Overview: an IV-rank tile + the store-derived IV charts (range bars + CM-IV history). Live front IV
    # alone can't say "is this high?"; the rank + the range chart vs the stored history can. The live front
    # cards stay ON TOP (freshest), the settlement panels sit below.
    ov_panels = []
    _closes = _closes_from_hist(hist) if hist else {}
    if hist:
        import opthist
        _r = opthist.iv_rank_at(hist, 30)
        if _r:
            ov_cards.insert(2 if sess else 1, {   # right after ATM IV · front
                "label": "IV rank · 30d", "value": "%.0f%%" % _r["rank"],
                "sub": ["%.0f%% percentile · vs %d sessions" % (_r["pct"], len(hist))],
                "hint": "Where 30d constant-maturity ATM IV sits in its own settlement-history range "
                        "(0%% cheapest · 100%% richest); the sub is the percentile. From the MOEX vol "
                        "store (IV recomputed). Full drill: the History tab."})
        _vrp = opthist._vrp_card(hist, _closes)   # implied (30d CM IV) vs realized (HV20 from stored closes)
        if _vrp:
            ov_cards.append(_vrp)
        ov_panels = opthist.build_overview(
            hist, skew_hint=_SKEW_HINT, closes=_closes,
            chain_note="(Chain/Term show the LIVE front→tail; History/Overview panels are settlement.)"
        ).get("panels", [])
    overview = {"id": "overview", "name": "Overview", "cards": ov_cards, "panels": ov_panels}
    if hist:
        import opthist
        history = opthist.build_history_section(sym.upper(), hist, subtitle=_HIST_SUB,
                                                skew_hint=_SKEW_HINT, closes=_closes)
        _sc = {"label": "Vol store", "value": (hist[-1].get("date") or "n/a"),
               "hint": "Overview's History tab comes from the MOEX settlement-history store as of %s — "
                       "daily ISS settlements with IV RECOMPUTED (Black-Scholes on the share). Refresh: "
                       "moex_options.py %s --history." % (hist[-1].get("date"), sym.upper())}
        history["cards"] = [_sc] + (history.get("cards") or [])
        if history.get("cardSeries"):
            history["cardSeries"] = [[_sc] + c for c in history["cardSeries"]]
    else:
        history = {"id": "history", "name": "History", "state": "warming", "cards": [], "panels": [],
                   "warmingText": ("Volatility history (IV rank · cone · CM-IV · skew/PCR history) needs "
                                   "the MOEX settlement store — fill it:  moex_options.py %s --history  "
                                   "(daily ISS settlements, IV recomputed Black-Scholes on the share; "
                                   "depth is bounded by how long the listed contracts have traded)."
                                   % sym.upper())}
    sections = [overview, chain, term, history]
    for s in sections:
        s.pop("subtitle", None)
        if s.get("switcher"):
            s["switcher"].pop("subtitles", None)
    out = dict(specs[target_idx])   # carries the unified head + lead from the target series
    out.pop("about", None)
    out["title"] = "%s options · full" % sym.upper()
    out["sections"] = sections
    out["active"] = "overview"
    out.pop("cards", None)
    out.pop("panels", None)
    return out


def _arg_date(s):
    """argparse validator: a valid ISO date, else a clean error (mirrors futures_options.py's guard —
    a bad --expiry must die at parse time, not as an uncaught ValueError inside build_chain)."""
    if _parse_date(s) is None:
        raise argparse.ArgumentTypeError("expiration must be YYYY-MM-DD, got %r" % s)
    return s


def _fund_links(html_path, sym, bg=False, _spawn=None, embed=False, fundamentals_fname=None):
    """Create/link the MOEX fundamentals sibling; embed it in Cowork for --full."""
    fname, sib = dashboard.sibling_target(
        html_path,
        fundamentals_fname,
        sym.lower() + ".html",
        sym.lower() + "_fundamentals.html",
    )
    label = "%s fundamentals ↗" % sym
    links = dashboard.sibling_links(html_path, [(fname, label)])
    if not bg or os.environ.get("MARKET_DATA_NO_SIBLING"):
        return links, None
    cw = dashboard.in_cowork()
    if links and not (cw and embed):
        return links, None
    here = os.path.dirname(os.path.abspath(__file__))
    parent_fname = dashboard.sibling_basename(os.path.basename(html_path), sym.lower() + "_options.html")
    argv = [sys.executable, os.path.join(here, "moex_fundamentals.py"), sym, "--html", sib,
            "--options-link", parent_fname]
    if cw:
        emit = (os.path.join(os.path.dirname(sib), ".%s.%d.spec.json" % (fname[:-5], os.getpid()))
                if embed else None)
        ok_child = (_spawn or dashboard.render_sync)(argv + (["--emit-spec", emit] if emit else []))
        sspec = None
        if ok_child and emit:
            try:
                with open(emit, encoding="utf-8") as f:
                    sspec = json.load(f)
            except (OSError, ValueError):
                pass
        if emit:
            try:
                os.unlink(emit)
            except OSError:
                pass
        if sspec is not None:
            return [], {"label": "%s fundamentals" % sym, "backLabel": "%s options" % sym,
                        "fname": fname, "spec": sspec}
        if ok_child:
            return dashboard.sibling_links(html_path, [(fname, label)]) or [], None
        return [], None
    if (_spawn or dashboard.spawn_bg)(argv):
        print("note: rendering %s in the background — the '%s' chip appears when it lands" % (sib, label),
              file=sys.stderr)
        return [{"text": label, "href": fname, "pending": True}], None
    return [], None


def _main(argv=None):
    ap = argparse.ArgumentParser(description="Russian equity option chain (MOEX FORTS, premium-on-spot)")
    ap.add_argument("symbol")
    ap.add_argument("--expiry", type=_arg_date, help="expiration YYYY-MM-DD (default: the near series)")
    ap.add_argument("--rate", type=float, help="RUB risk-free rate as a fraction (default: RUSFAR)")
    ap.add_argument("--term", action="store_true",
                    help="cross-expiration page: ATM-IV/skew curve + IV surface + a per-series table + "
                         "an expiration switcher (every series pulled — thin, cached within the run)")
    ap.add_argument("--full", action="store_true",
                    help="COMBINED tabbed page: Overview + Chain + Term + History (History fills from the "
                         "MOEX vol store when warm — run --history to build it)")
    ap.add_argument("--history", action="store_true",
                    help="VOL-HISTORY page (IV rank / cone / CM-IV / skew & PCR history) built from a MOEX "
                         "settlement backfill — daily ISS settlements with IV recomputed (Black-Scholes on "
                         "the share). A separate page; also fills the --full History tab.")
    ap.add_argument("--years", type=float, default=0.5,
                    help="--history discovery window in years (default 0.5): how far back to sample the "
                         "option boards for contracts to reconstruct. Deeper = a longer one-time backfill.")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--html", help="write a self-contained interactive HTML dashboard")
    ap.add_argument("--emit-spec", metavar="PATH", default=None,
                    help="(--full) also dump the page SPEC as JSON for the sibling embed")
    ap.add_argument("--fundamentals-link", help=argparse.SUPPRESS)
    ap.add_argument("--from-dir", help="build from a saved RAW ISS optionboard.json response (offline)")
    a = ap.parse_args(argv)
    sym = a.symbol.upper()
    if a.rate is not None:
        os.environ[RUB_RATE_ENV] = str(a.rate)

    if a.history:   # the standalone VOL-HISTORY page — a MOEX settlement backfill, IV recomputed
        import moexopthist
        if a.years <= 0:
            sys.exit("error: --years must be positive")
        try:
            hist = moexopthist.update_store(sym, years=a.years)
        # review P1: _NET_ERRORS is a TUPLE — nesting it in the except tuple (…, _NET_ERRORS) is a nested
        # tuple, which CPython refuses to catch (TypeError). FLATTEN with + so a transient outage exits clean.
        except (moexopthist.MoexHistError,) + _NET_ERRORS as e:   # a transient outage says 'retry', not 'bad ticker'
            sys.exit("error: %s" % e if isinstance(e, moexopthist.MoexHistError)
                     else "error: MOEX ISS unreachable for %s (%s) — retry" % (sym, type(e).__name__))
        if not hist:
            sys.exit("error: no MOEX settlement history for %s (thin/short listing, or a bad ticker)" % sym)
        import opthist
        _closes = _closes_from_hist(hist)   # VRP + realized-HV20 overlay from the stored share closes
        if a.html:
            dashboard.deliver_html(a.html, opthist.render_history_html(sym, hist, subtitle=_HIST_SUB,
                                                                       skew_hint=_SKEW_HINT, closes=_closes),
                                   offers=["/equity-research:earnings-preview " + sym,
                                           "/equity-research:catalysts"])
            print("%s  |  %s MOEX vol history (%d sessions)" % (a.html, sym, len(hist)))
        elif a.json:   # review P2: pass the MOEX subtitle/skew_hint so the JSON spec isn't AV-labelled
            print(json.dumps(opthist.spec_history(sym, hist, subtitle=_HIST_SUB, skew_hint=_SKEW_HINT,
                                                  closes=_closes), separators=(",", ":"), ensure_ascii=False))
        else:
            # opthist.history_text hardcodes an Alpha Vantage source footer — swap it for the honest
            # MOEX provenance (this store's IV is RECOMPUTED, never AV-native).
            _t = opthist.history_text(sym, hist).replace(
                "source: Alpha Vantage HISTORICAL_OPTIONS · AV-native IV",
                "source: MOEX ISS settlement history · IV recomputed (Black-Scholes on the share, delayed)")
            print(_t)
        return

    if a.emit_spec and not a.full:
        print("warning: --emit-spec applies to --full only — ignored", file=sys.stderr)

    if a.term or a.full:
        if a.from_dir:   # review P3: --from-dir needs every series' board -> not supported for --term/--full (warn, don't silently hit the network)
            print("warning: --from-dir does not apply to --term/--full (they pull every listed series) "
                  "— ignored, fetching live", file=sys.stderr)
        try:
            T = gather_term(sym)
        except ChainError as e:
            sys.exit(str(e))
        except _NET_ERRORS as e:   # review P1: a network outage / DNS / timeout / HTTP error dies clean, not a traceback
            sys.exit("error: MOEX ISS unreachable for %s (%s: %s) — retry, or the venue/network may be "
                     "down" % (sym, type(e).__name__, str(e)[:120]))
        specs, keep = [], []
        for i, an in enumerate(T["series"]):
            try:
                specs.append(to_spec(an))
                keep.append(i)
            except Exception as e:   # review P3: an `an` was already built by gather_term, so a to_spec
                keep.append(None)    # failure is a BUG, not thin data — drop the series but say so on stderr
                print("warning: %s %s dropped from the term view (%s: %s)"
                      % (sym, an.get("expiration"), type(e).__name__, str(e)[:100]), file=sys.stderr)
        keep = [i for i in keep if i is not None]
        if len(keep) < len(T["series"]):
            T["series"] = [T["series"][i] for i in keep]
            T["surface"] = _iv_surface(T["series"])
        if not specs:
            sys.exit("error: no %s series has enough strikes to chart" % sym)
        # target = the requested --expiry (nearest listed) else the front series
        target_idx = 0
        if a.expiry:
            target_idx = min(range(len(T["series"])),
                             key=lambda i: abs((_parse_date(T["series"][i]["expiration"]) or dt.date.today())
                                               - (_parse_date(a.expiry) or dt.date.today())))
        if a.full:
            import moexopthist
            _hist = moexopthist.load_store(sym)   # cache-first: fill the History tab if the store is warm
            spec = build_full_spec(sym, T, specs, target_idx, hist=(_hist or None))
        else:
            spec = combined_spec(specs, T, target_idx)
        if a.emit_spec and a.full:
            dashboard.write_atomic(a.emit_spec, json.dumps(spec, ensure_ascii=False))
        if a.html:
            if not a.from_dir and not os.environ.get("MARKET_DATA_NO_SIBLING"):
                dashboard.write_atomic(a.html, dashboard.render_interactive_html(spec))
            links, sibling = _fund_links(a.html, sym, bg=not a.from_dir, embed=bool(a.full),
                                         fundamentals_fname=a.fundamentals_link)
            if links:
                spec["links"] = links
            if sibling:
                spec["sibling"] = sibling
            # the GMKN + ROSN Cowork sessions 2026-07-24 both closed card-less here while YDEX
            # offered the preview and the user CLICKED it — the offer was non-deterministic, which
            # is exactly what a printed note at the write is meant to remove. RU names are NOT
            # withheld: since v1.147 the documents source their history from moex_fundamentals.py.
            dashboard.deliver_html(a.html, dashboard.render_interactive_html(spec),
                                   offers=["/equity-research:earnings-preview " + sym,
                                           "/equity-research:catalysts"])
            print("%s  |  %s MOEX options · %s (%d series)"
                  % (a.html, sym, "full" if a.full else "term", len(specs)))
            if sibling:
                dashboard.write_reverse_sibling(sibling, spec, a.html)
        elif a.json:
            print(json.dumps(spec, separators=(",", ":"), ensure_ascii=False))
        else:
            print(render_text(T["series"][target_idx]))
            print("\n%s: %d series · front ATM IV %s · term slope %s"
                  % (sym, len(specs), _ivfmt(T["series"][0]["atmIV"]),
                     ("%+.1f" % (T["series"][-1]["atmIV"] - T["series"][0]["atmIV"])
                      if (len(T["series"]) > 1 and T["series"][-1]["atmIV"] and T["series"][0]["atmIV"]) else "n/a")))
        return

    try:
        if a.from_dir:
            # review P2: consume the RAW ISS response ({call,put,asset} as {columns,data} blocks) —
            # reshape it exactly as fetch_board does so a user who saved the endpoint's own output works.
            # review P1: a missing/misnamed file, malformed JSON, or a garbage rate dies CLEAN, not a traceback.
            try:
                raw = json.load(open(os.path.join(a.from_dir, "optionboard.json"), encoding="utf-8"))
            except (OSError, ValueError) as e:
                sys.exit("--from-dir %s: no readable optionboard.json (%s)" % (a.from_dir, str(e)[:100]))
            try:
                _offr = float(os.environ.get(RUB_RATE_ENV, _RATE_FALLBACK))
            except ValueError:
                sys.exit("error: %s must be a number (a rate fraction), got %r"
                         % (RUB_RATE_ENV, os.environ.get(RUB_RATE_ENV)))
            an = build_chain(sym, _board_from_raw(raw), {}, _offr, a.expiry)
        else:
            an = gather(sym, a.expiry)
    except ChainError as e:
        sys.exit(str(e))
    except _NET_ERRORS as e:   # review P1: online path — a network failure dies clean, not a traceback
        sys.exit("error: MOEX ISS unreachable for %s (%s: %s) — retry, or the venue/network may be down"
                 % (sym, type(e).__name__, str(e)[:120]))
    if a.html:
        spec = to_spec(an)
        if not a.from_dir and not os.environ.get("MARKET_DATA_NO_SIBLING"):
            dashboard.write_atomic(a.html, dashboard.render_interactive_html(spec))
        spec["links"] = _fund_links(a.html, sym, bg=not a.from_dir,
                                     fundamentals_fname=a.fundamentals_link)[0]
        dashboard.deliver_html(a.html, dashboard.render_interactive_html(spec),
                               offers=["/equity-research:earnings-preview " + sym,
                                       "/equity-research:catalysts"])
        print("%s  |  %s MOEX options (%s DTE)" % (a.html, sym, an["dte"]))
    elif a.json:
        print(json.dumps(an, separators=(",", ":"), ensure_ascii=False))
    else:
        print(render_text(an))


def main(argv=None):
    _main(argv)


if __name__ == "__main__":
    main()
