#!/usr/bin/env python3
"""
moexopthist.py — a MOEX equity-option VOLATILITY-HISTORY store (the Tier-2 slice of
docs/moex-options-plan.md). The MOEX twin of `futhist.py` (which is the Databento twin of
`opthist.py`): daily settlement history reduced per (day, series) into records that MATCH
opthist's contract byte-for-byte, so opthist's builders (spec_history / build_overview /
build_history_section) render the History tab UNCHANGED — the ONE-CONTRACT lever.

MOEX history carries NO IV (the `optionboard` VOLAT is live-only), so IV is **recomputed** per
(day, strike) from that day's option SETTLEPRICE + the share's close via `greeks.implied_vol`
(Black-Scholes on the share, RUSFAR rate) — the futhist pattern. Records:
    {date, spot, pcr, cm30/60/90, skew30, series:[{dte, atmIV(DECIMAL), skew25}]}

DENSE (v1.136): rather than reduce only TODAY's listed contracts, DISCOVER every option that traded
across the window (assetcode-filtered per-date settlement boards) and resolve each SECID — even
long-expired ones — via the global /securities lookup, so the constant-maturity cone / skew / PCR
history are dense back to the discovery window's start.

INCREMENTAL (v1.137): a rebuild caches the three IMMUTABLE phases beside the store — a past date's
OI-bearing SECID set, a SECID's definition, and an EXPIRED contract's settlement history never change —
so a warm rebuild re-fetches only what genuinely moved (new dates, new contracts, still-LIVE contracts):
~1300 ISS round-trips -> ~200.
"""
from __future__ import annotations
import concurrent.futures
import datetime as dt
import gzip
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # Windows-safe
import greeks  # noqa: E402
import moex_options as mo  # noqa: E402
import opthist  # noqa: E402 — interp_cm_ranged, CM_TENORS, the store ladder, the builders (contract parity)

ISS = mo.ISS
_MAX_DTE = 400   # ignore very-far series in the term reduce (opthist parity)
_ATM_TOL = 0.12  # review P2: the recompute uses TODAY's strikes; on a past day whose spot sits >12%
#                  from the nearest listed strike, the "ATM" solve is a wing/ITM leg — drop that
#                  series' atmIV as unreliable (survivorship guard), never store a wing IV as ATM.


class MoexHistError(RuntimeError):
    """A transient, retryable store-build failure (a network outage during the concurrent pulls) —
    distinct from genuine no-data, so the CLI can say 'retry' instead of 'bad ticker'."""


# ----------------- fetch (seam-injectable, same `fetch(path)->text` seam as moex_options) -----------------
def _paged_history(base, fetch, block="history", cap=60):
    """Walk an ISS `history/…` endpoint's `start=` pages until a short/empty page (the endpoint caps at
    ~100 rows/page — a single un-paged pull silently truncates a multi-month window). Yields each row
    dict; `cap` bounds the pages (cap×100 rows) so a runaway can't loop forever."""
    start = 0
    for _ in range(cap):
        sep = "&" if "?" in base else "?"
        d = mo._iss("%s%sstart=%d" % (base, sep, start), fetch)
        rows = mo._rows(d.get(block, {}))
        if not rows:
            return
        for r in rows:
            yield r
        pagesize = 0
        try:   # review P3: a malformed/absent cursor block must not KeyError/IndexError the walk
            cblk = d.get(block + ".cursor", {}) or {}
            cur = cblk.get("data")
            if cur:
                c0 = dict(zip(cblk.get("columns", []), cur[0]))
                pagesize = int(c0.get("PAGESIZE") or 0)
                if start + len(rows) >= int(c0.get("TOTAL") or 0):
                    return
        except (ValueError, TypeError, IndexError, KeyError):
            pagesize = 0
        # Some ISS blocks (the stock history with iss.only=history) return NO cursor — a full page still
        # means "more to come". Fall back to ISS's default page size (100): a SHORT page is the last one.
        page = pagesize or 100
        if len(rows) < page:
            return
        start += len(rows)


def fetch_contract_history(secid, fetch=None, since=None):
    """One contract's daily history -> {date_iso: {'settle': float|None, 'oi': int}} over its listed
    life (paged). `since` (ISO date, inclusive) limits the pull to that date forward — a TAIL fetch for
    a still-LIVE cached contract (v1.137): a deep LEAP's multi-year history is ~5 pages, but its
    unfetched tail since yesterday is ONE, so tailing collapses the warm-rebuild round-trips. `fetch(
    path)->text` is the injectable seam (the default hits keyless ISS through chart.http_get)."""
    base = "/history/engines/futures/markets/options/securities/%s.json?iss.meta=off&iss.only=history" % secid
    if since:
        base += "&from=%s" % since
    out = {}
    for r in _paged_history(base, fetch):
        day = r.get("TRADEDATE")
        if day:
            out[day] = {"settle": mo._ff(r.get("SETTLEPRICE")), "oi": int(mo._ff(r.get("OPENPOSITION")) or 0)}
    return out


def fetch_share_history(sym, start, fetch=None, till=None):
    """The underlying SHARE's daily close per date -> {date_iso: close} (paged). LEGALCLOSEPRICE (the
    official close) preferred, CLOSE as the fallback. `start`/`till` are ISO dates."""
    till = till or dt.date.today().isoformat()
    base = ("/history/engines/stock/markets/shares/boards/TQBR/securities/%s.json"
            "?iss.meta=off&iss.only=history&from=%s&till=%s" % (sym, start, till))
    out = {}
    for r in _paged_history(base, fetch):
        day = r.get("TRADEDATE")
        c = mo._ff(r.get("LEGALCLOSEPRICE")) or mo._ff(r.get("CLOSE"))
        if day and c and c > 0:
            out[day] = c
    return out


# ----------------- store (opthist's dir ladder + JSON, same as futhist) -----------------
def _store_dir():
    base = opthist._resolve_store_dir() if hasattr(opthist, "_resolve_store_dir") else opthist.CACHE_DIR
    d = os.path.join(os.path.dirname(base.rstrip("/")), "moexopt") if base else None
    return d or os.path.join(os.path.expanduser("~"), ".cache", "market-data", "moexopt")


def _store_path(sym):
    return os.path.join(_store_dir(), "%s.hist.json" % sym.upper())


def load_store(sym):
    try:
        with open(_store_path(sym), encoding="utf-8") as f:
            v = json.load(f)
        return v if isinstance(v, list) else []
    except (OSError, ValueError):
        v = opthist._load_packaged_store_json("moexopt", sym)
        return v if isinstance(v, list) else []


def _sweep_tmp(d):
    """Sweep >1h `.tmp` orphans in dir `d` (a SIGKILL between mkstemp-write and os.replace)."""
    import time
    try:
        for fn in os.listdir(d):
            if fn.endswith(".tmp"):
                fp = os.path.join(d, fn)
                try:
                    if time.time() - os.path.getmtime(fp) > 3600:
                        os.unlink(fp)
                except OSError:
                    pass
    except OSError:
        pass


def save_store(sym, hist):
    d = _store_dir()
    import tempfile
    tmp = None
    try:
        os.makedirs(d, exist_ok=True)
        _sweep_tmp(d)
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(hist, f)
        os.replace(tmp, _store_path(sym))
        tmp = None
    except OSError as e:
        print("warning: could not save the MOEX vol store for %s (%s)" % (sym, e), file=sys.stderr)
    finally:
        if tmp and os.path.exists(tmp):   # replace failed -> don't leave the orphan behind
            try:
                os.unlink(tmp)
            except OSError:
                pass


# ----------------- incremental cache (the rebuild speedup — v1.137) -----------------
# A full --history rebuild re-fetches EVERYTHING (~1300 ISS round-trips for GAZP). Three phases are
# IMMUTABLE once past: a PAST date's OI-bearing SECID set, a SECID's definition, and an EXPIRED
# contract's settlement history. We cache all three per-assetcode beside the store and re-fetch only
# what moved. Best-effort: a missing/corrupt cache just starts empty (ISS stays the source of truth).
def _cache_dir():
    return os.path.join(_store_dir(), "cache")


def _cache_path(assetcode, kind):
    return os.path.join(_cache_dir(), "%s.%s.json.gz" % (assetcode, kind))


def _load_cache(assetcode, kind):
    import zlib
    p = _cache_path(assetcode, kind)
    try:
        with gzip.open(p, "rt", encoding="utf-8") as f:
            v = json.load(f)
        return v if isinstance(v, dict) else {}
    except FileNotFoundError:
        return {}   # simply absent (cold) — nothing to clean up
    except (OSError, ValueError, EOFError, zlib.error):
        # a CORRUPT cache (a mid-stream bit-flip raises zlib.error, which is NOT an OSError — the v1.128
        # bug class) is best-effort: DELETE the poisoned file so it can't crash-loop every rebuild, then
        # start empty and re-fetch (ISS stays the source of truth).
        try:
            os.unlink(p)
        except OSError:
            pass
        return {}


def _save_cache(assetcode, kind, obj):
    import tempfile
    d = _cache_dir()
    tmp = None
    try:
        os.makedirs(d, exist_ok=True)
        _sweep_tmp(d)
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        os.close(fd)
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump(obj, f, separators=(",", ":"))
        os.replace(tmp, _cache_path(assetcode, kind))
        tmp = None
    except OSError:
        pass   # best-effort — a failed cache write just means the next run re-fetches
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


# ----------------- reduce -----------------
def _reduce_day(sym, day, by_series, share_close, rate):
    """One trading day -> an opthist-shaped record (or None for a dead day). `by_series` = {expiration:
    [{strike, is_call, settle, oi}]}. IV solved per contract (Black-Scholes on the share), then the
    SAME `moex_options.build_chain` reduces each series (feeding the solved IV as the board's VOLAT)."""
    series_entries, coi, poi, fronts = [], 0, 0, []
    for exp, rows in sorted(by_series.items()):
        ed = mo._parse_date(exp)
        if ed is None:
            continue
        dte = (ed - day).days
        if not (1 <= dte <= _MAX_DTE):
            continue
        board = {"call": [], "put": [], "asset": {"UNDERLYINGSETTLEPRICE": share_close, "LASTDELDATE": exp}}
        for r in rows:
            # correctness backstop (ALWAYS on — keyed off the day's share close, never the possibly-None
            # spot0 the cheap pre-filter uses): a premium equity strike sits near the share; a futures-
            # margined twin's strike is the FUTURE price (~100×). Drop a wildly-off strike so a
            # mis-classified leak (fail-open NAME, or a strike that slipped the pre-filter) can't bias
            # this series' OI / PCR / ladder.
            if share_close and not (0.2 * share_close <= r["strike"] <= 5.0 * share_close):
                continue
            iv = None
            if r["settle"] and r["settle"] > 0:
                iv = greeks.implied_vol(r["settle"], r["strike"], max(1, dte) / 365.0,
                                        S=share_close, r=rate, is_call=r["is_call"], model="bs")
            # feed the SOLVED IV back as a percent VOLAT so build_chain's reducer reads it like the live board
            rec = {"STRIKE": r["strike"], "VOLAT": (iv * 100.0 if iv else 0),
                   "OPENPOSITION": r["oi"]}
            board["call" if r["is_call"] else "put"].append(rec)
        try:
            an = mo.build_chain(sym, board, {"spot": share_close}, rate, exp, today=day)
        except mo.ChainError:
            continue
        # review P2 (survivorship): the strikes are TODAY's — if the day's spot sits far from the
        # nearest listed strike, the "ATM" solve is really a wing/ITM leg. Drop that series' atmIV
        # (an honest hole) rather than store a wing IV mislabeled as ATM; OI/skew still ride.
        _atm_iv = an["atmIV"]
        if _atm_iv is not None and share_close and abs(an["atmStrike"] - share_close) > _ATM_TOL * share_close:
            _atm_iv = None
        series_entries.append({"dte": dte,
                               "atmIV": (round(_atm_iv / 100.0, 5) if _atm_iv is not None else None),
                               "skew25": an["skew25"]})
        if dte <= opthist._PCR_MAX_DTE:   # PCR = NEAR-dated book only (opthist parity — far LEAPS call
            coi += an["callOItotal"]      # walls, e.g. GAZP out to 2030, would swamp a whole-book sum)
            poi += an["putOItotal"]
        fronts.append((dte, share_close))
    if not any(s["atmIV"] for s in series_entries):
        return None                 # a dead day -> a hole, never a null record (reduce_asof parity)
    agg = {"date": day.isoformat(), "spot": round(share_close, 4), "series": series_entries,
           "pcr": (round(poi / coi, 4) if coi else None)}
    iv_pts = [(s["dte"], s["atmIV"]) for s in series_entries]
    sk_pts = [(s["dte"], s["skew25"]) for s in series_entries]
    for t in opthist.CM_TENORS:
        v = opthist.interp_cm_ranged(iv_pts, t)
        agg["cm%d" % t] = round(v, 5) if v is not None else None
    s30 = opthist.interp_cm_ranged(sk_pts, 30)
    agg["skew30"] = round(s30, 2) if s30 is not None else None
    return agg


def fetch_rate_history(fetch=None, start=None, till=None):
    """RUSFAR daily rate history as {date_iso: fraction} (percent → fraction). Best-effort — an empty
    result lets build_records fall back to a single current rate. Review P2: RU policy rates swing, so
    the historical IV solve should use each day's OWN rate, not one current value."""
    base = "/history/engines/stock/markets/index/securities/RUSFAR.json?iss.meta=off&iss.only=history"
    if start:
        base += "&from=%s" % start
    if till:
        base += "&till=%s" % till
    out = {}
    try:
        for r in _paged_history(base, fetch):
            day, v = r.get("TRADEDATE"), mo._ff(r.get("CLOSE") or r.get("LEGALCLOSEPRICE"))
            if day and v and v > 0:
                out[day] = v / 100.0
    except (Exception, SystemExit):
        return {}
    return out


def _rate_for(day_iso, rate_hist, fallback):
    """The rate for a day: the exact RUSFAR close, else the most recent prior close, else the fallback."""
    if not rate_hist:
        return fallback
    if day_iso in rate_hist:
        return rate_hist[day_iso]
    prior = [d for d in rate_hist if d <= day_iso]
    return rate_hist[max(prior)] if prior else fallback


def fetch_secid_def(secid, fetch=None):
    """Resolve ANY option SECID (even long-expired) to its definition via the global ISS security
    lookup: {'strike', 'is_call', 'exp', 'assetcode', 'premium'} or None. `premium` is True for a
    premium-on-spot equity option (exercises into the SHARE — NAME has no 'фьюч'), False for the
    futures-margined twin (same assetcode, strike ~future price). This is the lever that makes the
    DENSE history possible: the per-date settlement pull gives only SECID+settle, and expired contracts
    are gone from the live board, but this lookup reconstructs strike/type/EXPIRATION for any of them."""
    try:
        d = mo._iss("/securities/%s.json?iss.meta=off" % secid, fetch)
        desc = d.get("description", {})
        f = {r[0]: r[2] for r in desc.get("data", [])} if desc.get("data") else {}
    except (Exception, SystemExit):
        return None
    k, t = mo._ff(f.get("STRIKE")), f.get("OPTIONTYPE")
    exp = f.get("LSTDELDATE") or f.get("LASTDELDATE")
    if k is None or t not in ("C", "P") or not exp:
        return None
    return {"strike": k, "is_call": t == "C", "exp": exp, "assetcode": f.get("ASSETCODE"),
            "premium": ("фьюч" not in (f.get("NAME") or ""))}


def _asset_code(sym, fetch=None):
    """The ISS option ASSETCODE for a share ticker (SBER -> SBRF) — resolved from a current option
    SECID's definition (the history endpoints filter server-side by this code, not the share ticker)."""
    board = mo.fetch_board(sym, fetch=fetch)
    for side in ("call", "put"):
        for r in board.get(side) or []:
            sec = r.get("SECID")
            if sec:
                dfn = fetch_secid_def(sec, fetch=fetch)
                if dfn and dfn.get("assetcode"):
                    return dfn["assetcode"]
    return None


def _discover_secids(assetcode, fetch, dates, max_workers=8, disc_cache=None):
    """The union of option SECIDs that carried open interest on any of `dates` — an assetcode-filtered
    per-date settlement pull per date (server-side filtered to ~this asset's contracts, ~2k rows, not
    the 34k whole market). Sampling a handful of dates across the window captures the series/strikes
    that were listed then — INCLUDING now-expired ones (the survivorship fix). Returns
    (secids, n_failed_dates, n_dates_pulled): a date whose pull RAISES is counted as a failure (None),
    distinct from a date that returns nothing (empty set) — so the caller can tell a transient outage
    from a genuinely empty window (the futhist failure≠no-data rule). `disc_cache` (a {date: [SECIDs]}
    dict) serves a PAST date's immutable set from cache and records new pulls — cached dates cost no
    round-trip and are not counted toward the outage denominator."""
    def _one(day):
        out = set()
        try:
            base = ("/history/engines/futures/markets/options/securities.json?iss.meta=off"
                    "&iss.only=history,history.cursor&assetcode=%s&date=%s" % (assetcode, day))
            for r in _paged_history(base, fetch):
                s, oi = r.get("SECID"), mo._ff(r.get("OPENPOSITION")) or 0
                if s and oi > 0:
                    out.add(s)
        except (Exception, SystemExit):
            return None   # a FAILED pull (network) -> None, distinct from an empty set (no OI that date)
        return out
    secids, nfail, npull = set(), 0, 0
    pull_dates = []
    for d in dates:
        if disc_cache is not None and d in disc_cache:
            secids |= set(disc_cache[d])   # immutable past-date discovery — no round-trip
        else:
            pull_dates.append(d)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        for d, s in zip(pull_dates, ex.map(_one, pull_dates)):
            npull += 1
            if s is None:
                nfail += 1
            else:
                secids |= s
                if disc_cache is not None:
                    disc_cache[d] = sorted(s)   # record for the next run (a fresh pull only)
    return secids, nfail, npull


def _sample_dates(today, years, step_days):
    """A handful of dates across the window (every `step_days`, weekends nudged to Friday) for SECID
    discovery — enough to catch each period's then-listed near/quarterly series without pulling every
    date's whole board."""
    step_days = max(1, int(step_days))   # guard: 0 would never advance the loop variable
    out, d = [], today
    start = today - dt.timedelta(days=int(years * 365))
    while d >= start:
        dd = d - dt.timedelta(days=max(0, d.weekday() - 4))   # Sat/Sun -> Fri
        out.append(dd.isoformat())
        d -= dt.timedelta(days=step_days)
    return sorted(set(out))


def build_records(sym, fetch=None, today=None, years=1.0, step_days=14, max_workers=12):
    """Assemble the DENSE MOEX vol-history (v1.136): DISCOVER every series/strike that was listed across
    the window (incl. now-EXPIRED ones — the survivorship fix), resolve each SECID to its definition,
    pull each contract's settlement history, solve IV, reduce per (day, series). Returns records sorted
    by date (opthist shape). Best-effort — a failed pull is skipped; a total outage raises MoexHistError."""
    import re
    sym = sym.upper()
    today = today or dt.date.today()
    board = mo.fetch_board(sym, fetch=fetch)                 # the near board: assetcode seed + a rough spot
    spot0 = mo._ff((board.get("asset") or {}).get("UNDERLYINGSETTLEPRICE")) or 0
    # resolve the ASSETCODE robustly from BOTH sides (a call-less board, or a single transient
    # /securities miss on one seed, must not abort the whole build). If the board NAMED contracts but
    # NONE of the first few resolve, the /securities endpoint is down -> a retryable outage, not a bad
    # ticker (failure≠no-data — the same rule the per-contract phase enforces below).
    seeds = [r.get("SECID") for side in ("call", "put")
             for r in (board.get(side) or []) if r.get("SECID")]
    assetcode = None
    for s in seeds[:8]:
        dfn0 = fetch_secid_def(s, fetch=fetch)
        if dfn0 and dfn0.get("assetcode"):
            assetcode = dfn0["assetcode"]
            break
    if not assetcode:
        if seeds:
            raise MoexHistError("MOEX ISS security lookup unreachable for %s (the near board listed %d "
                                "contracts, none resolved) — a transient outage; re-run to retry."
                                % (sym, len(seeds)))
        return []
    disc_dates = _sample_dates(today, years, step_days)
    disc_cache = _load_cache(assetcode, "disc")
    _dc = sum(1 for d in disc_dates if d in disc_cache)
    print("  MOEX vol backfill %s: dense reconstruction (~1-3 min cold, faster warm — immutable phases "
          "cached). Discovering across %d sampled dates (%d cached)…"
          % (sym, len(disc_dates), _dc), file=sys.stderr)
    secids, ndf, ndt = _discover_secids(assetcode, fetch, disc_dates, max_workers=max_workers,
                                        disc_cache=disc_cache)
    _save_cache(assetcode, "disc", disc_cache)
    if not secids:   # nothing found: distinguish a discovery-wide OUTAGE from a genuinely empty window
        if ndt and ndf == ndt:   # nothing cached AND every pulled date failed -> a transient outage
            raise MoexHistError("MOEX ISS discovery unreachable for %s (all %d pulled dates failed) — a "
                                "transient outage; nothing was stored. Re-run to retry." % (sym, ndt))
        return []
    if ndf:   # a PARTIAL discovery outage drops whatever series were listed only then -> say so
        print("warning: %s vol store — %d/%d pulled discovery dates failed (throttle/outage); the "
              "reconstructed series set may be missing contracts listed only then" % (sym, ndf, ndt),
              file=sys.stderr)
    # CHEAP pre-filter (bounds the resolve cost): a premium-on-spot strike sits near the share price;
    # a futures-margined twin's strike is the FUTURE price (~100× on SBER). Parse the SECID's leading
    # strike digits and drop the obvious futures-margined before the /securities resolution.
    def _strike_of(sec):
        m = re.match(r"^[A-Za-z]+(\d+)", sec)
        return int(m.group(1)) if m else None
    # v1.143: some equity options are LOT-quoted (PLZL/T: strike = per-share × 10) — the ladder sits ~10× the
    # share, so a flat 8×spot0 cutoff (tuned for the 1× premium-on-spot case) would drop the whole ATM region
    # and leave the reconstructed history empty. Estimate the lot-scale from the discovered ladder (median
    # exchange-strike vs the share spot, rounded to a power of 10 — the SAME signal build_chain uses live) and
    # widen the cutoff by it, so lot-scaled equity survives while a futures-margined twin (~100× the share, in
    # points) is still trimmed. The premium-on-spot flag (below) remains the AUTHORITATIVE filter — this only
    # bounds cost, so erring generous is safe.
    import math
    _exs = sorted(x for x in (_strike_of(s) for s in secids) if x)
    _scale = 1
    if spot0 and _exs:
        _r = _exs[len(_exs) // 2] / spot0
        if _r > 3:
            _scale = 10 ** round(math.log10(_r))
    cand = [s for s in secids if not (spot0 and (_strike_of(s) or 0) > 8 * spot0 * _scale)]
    # resolve every candidate -> {strike, is_call, exp, premium}; keep only PREMIUM-on-spot equity
    # options. A SECID's definition is IMMUTABLE -> served from the def cache; only NEW SECIDs are
    # fetched. A None resolve on a DISCOVERED (OI-bearing, real) SECID is almost always a /securities
    # outage -> count it, so an all-failed NEW resolve raises (retry) instead of 'no options'.
    def_cache = _load_cache(assetcode, "defs")
    defs, n_res_fail, to_resolve = {}, 0, []
    for sec in sorted(cand):
        c = def_cache.get(sec)
        if c is not None:               # cached def: [strike, is_call, exp, premium]
            if c[3]:
                defs[sec] = {"strike": c[0], "is_call": bool(c[1]), "exp": c[2],
                             "premium": True, "assetcode": assetcode}
        else:
            to_resolve.append(sec)
    print("  resolving %d candidate contracts (of %d discovered; %d cached)…"
          % (len(to_resolve), len(secids), len(cand) - len(to_resolve)), file=sys.stderr)

    def _rz(sec):
        return sec, fetch_secid_def(sec, fetch=fetch)
    if to_resolve:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
            for sec, dfn in ex.map(_rz, to_resolve):
                if dfn is None:
                    n_res_fail += 1
                else:
                    def_cache[sec] = [dfn["strike"], int(dfn["is_call"]), dfn["exp"],
                                      int(bool(dfn.get("premium")))]
                    if dfn.get("premium"):
                        defs[sec] = dfn
        _save_cache(assetcode, "defs", def_cache)
    if not defs:   # nothing premium: distinguish a resolve OUTAGE from a genuinely option-less asset
        if to_resolve and n_res_fail == len(to_resolve):
            raise MoexHistError("MOEX ISS security lookup unreachable for %s (all %d candidate resolves "
                                "failed) — a transient outage; nothing was stored. Re-run to retry."
                                % (sym, len(to_resolve)))
        return []
    if n_res_fail:   # a PARTIAL resolve outage biases the affected days' strike set -> say so
        print("warning: %s vol store — %d/%d newly-resolved contracts failed (throttle/outage); the "
              "affected days' strike set may be biased" % (sym, n_res_fail, len(to_resolve)), file=sys.stderr)
    # a contract set keyed like the old path so the reduce is unchanged. v1.143: map the exchange strike to
    # SHARE terms for lot-quoted names (PLZL/T: ×10) HERE, at consumption — _reduce_day's IV solve (S=share
    # close) and its 0.2×–5× share backstop both assume per-share strikes, and build_chain then sees ratio~1
    # (no double-scale). The DIVISION scale is decided from the PREMIUM defs only (the futures-margined twins
    # are already filtered here) with the same whole-ladder min-guard build_chain uses — a 1× name whose OI
    # or listing skews far-OTM must NOT trip (the pre-filter `_scale`, from the raw discovered set, stays a
    # generous cost bound only). The def cache keeps the raw exchange strike; scale=1 is a no-op.
    _ds = sorted(d["strike"] for d in defs.values() if d.get("strike"))
    _div_scale = 1
    if spot0 and _ds and _ds[0] > 2 * spot0:
        _div_scale = 10 ** round(math.log10(_ds[len(_ds) // 2] / spot0))
    contracts = {(dfn["exp"], dfn["strike"] / _div_scale, dfn["is_call"]): sec for sec, dfn in defs.items()}
    # per-contract settlement history. An EXPIRED contract's history is FINAL -> served from cache
    # (frozen once we fetched past its expiration); LIVE/new contracts are fetched. review P2: COUNT
    # fetch failures — a partial failure biases the strike set, an ALL-fetch-fail with nothing cached
    # is a transient outage, NOT 'no data'.
    chist_cache = _load_cache(assetcode, "chist")
    today_iso = today.isoformat()
    hist_by_sec, failed, to_fetch = {}, [], []
    secs = sorted(set(contracts.values()))

    def _unpack(h):
        return {d: {"settle": v[0], "oi": v[1]} for d, v in (h or {}).items()}
    # `since_of` is computed HERE (main thread) so the worker threads never read chist_cache concurrently
    # with the main loop's writes to it below — LIVE cached -> tail from the last cached date; new -> full.
    since_of = {}
    for sec in secs:
        ent = chist_cache.get(sec)
        if ent and ent.get("t", "") > defs[sec]["exp"]:   # fetched a day PAST expiration -> final, frozen
            #                                                (strict > so the expiration-day settle is captured)
            hist_by_sec[sec] = _unpack(ent.get("h"))
        else:
            to_fetch.append(sec)
            since_of[sec] = max(ent["h"]) if (ent and ent.get("h")) else None
    n_tail = sum(1 for s in to_fetch if since_of[s])
    _FAIL = object()

    def _one(sec):
        try:
            return sec, fetch_contract_history(sec, fetch=fetch, since=since_of[sec])
        except (Exception, SystemExit):
            return sec, _FAIL
    print("  settlement history: %d cached (expired) + %d tail (live) + %d full (new)…"
          % (len(secs) - len(to_fetch), n_tail, len(to_fetch) - n_tail), file=sys.stderr)
    if to_fetch:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
            for sec, h in ex.map(_one, to_fetch):
                ent = chist_cache.get(sec)
                if h is _FAIL:   # a transient miss keeps any cached history — don't drop the contract
                    failed.append(sec)
                    hist_by_sec[sec] = _unpack(ent.get("h") if ent else None)
                else:
                    merged = dict(ent.get("h") or {}) if ent else {}   # tail merges INTO the cached body
                    merged.update({d: [hv["settle"], hv["oi"]] for d, hv in h.items()})
                    chist_cache[sec] = {"t": today_iso, "h": merged}
                    hist_by_sec[sec] = _unpack(merged)
        _save_cache(assetcode, "chist", chist_cache)
    n_fail = len(failed)
    if to_fetch and n_fail == len(to_fetch) and not any(hist_by_sec.values()):   # all-fetch-fail, nothing cached
        raise MoexHistError("MOEX ISS history unreachable for %s (all %d contract pulls failed) — a "
                            "transient outage; nothing was stored. Re-run to retry." % (sym, len(to_fetch)))
    if n_fail:   # a PARTIAL failure biases the strike set for the affected days — say so, don't hide it
        print("warning: %s vol store — %d/%d contract history pulls failed (throttle/outage); the "
              "affected days' PCR/skew/ATM may be biased by the missing strikes" % (sym, n_fail, len(to_fetch)),
              file=sys.stderr)
    all_dates = sorted({d for h in hist_by_sec.values() for d in h})
    if not all_dates:
        return []
    share = fetch_share_history(sym, all_dates[0], fetch=fetch, till=all_dates[-1])
    fb_rate = mo.fetch_rate(30, fetch=fetch)                     # the fallback (one current RUSFAR-1M)
    rate_hist = fetch_rate_history(fetch=fetch, start=all_dates[0], till=all_dates[-1])   # per-date rate
    # regroup: {date: {expiration: [{strike, is_call, settle, oi}]}}
    by_day = {}
    for (exp, strike, is_call), sec in contracts.items():
        for day, row in hist_by_sec.get(sec, {}).items():
            by_day.setdefault(day, {}).setdefault(exp, []).append(
                {"strike": strike, "is_call": is_call,   # strike already ÷_div_scale (contracts key); the SETTLE
                 # premium is lot-quoted too (PLZL/T ×10) — divide it so the IV solve sees a per-share premium
                 # against the per-share strike + share spot, or it implies a ~10× inflated vol (v1.143).
                 "settle": (row["settle"] / _div_scale if row["settle"] is not None else None), "oi": row["oi"]})
    records = []
    for day_iso in sorted(by_day):
        d = mo._parse_date(day_iso)
        sc = share.get(day_iso)
        if d is None or not sc:
            continue
        rec = _reduce_day(sym, d, by_day[day_iso], sc, _rate_for(day_iso, rate_hist, fb_rate))
        if rec:
            records.append(rec)
    return records


def update_store(sym, fetch=None, today=None, years=0.5, step_days=14):
    """Rebuild + persist the DENSE store (discover + resolve + per-contract history across the window).
    v1.137: the three IMMUTABLE phases (past-date discovery, SECID defs, expired-contract history) are
    served from a per-assetcode cache beside the store, so a warm rebuild re-fetches only what moved
    (new dates/contracts + still-LIVE contracts). Returns the records."""
    recs = build_records(sym, fetch=fetch, today=today, years=years, step_days=step_days)
    if recs:
        save_store(sym, recs)
    return recs
