#!/usr/bin/env python3
"""futhist.py — the FUTURES vol-history store + backfill (Tier 1 slice (b);
design of record: docs/futures-full-plan.md).

The Databento twin of opthist's AV settlement store: `statistics` (settlement + OI,
L0 = full history, $0 on this key — preflight-verified 2026-07-16) reduced per
(day, series) through `futures_options.build_chain` — the SAME OI-filtered reducer
the live chain page uses (the v1.111 one-reducer rule: history == today's page by
construction) — into day records matching opthist's store contract EXACTLY:

    {"date", "spot", "pcr", "cm30", "cm60", "cm90", "skew30",
     "series": [{"dte", "atmIV" (DECIMAL — build_chain's percent ÷100 at this
                 boundary and nowhere else), "skew25" (vol points, the ~10%-OTM
                 moneyness proxy)}]}

so opthist's History/Overview builders (`spec_history` / `build_overview` /
`build_history_section`) consume them UNCHANGED. Forwards are strictly PER-SERIES
(each series' own underlying month settle — the v1.71 lesson; the record's scalar
`spot` is the FRONT series' settle, display/context only). A day with no solvable
ATM IV anywhere is SKIPPED (a hole, never a null record); a series whose underlying
has no settle that day is dropped with a stderr warning (never keyed to a guessed
forward). Strikes are capped at ±30% moneyness of the series' own forward (far-tail
OI dust can't move ATM/skew/PCR materially; the cap keeps the IV solve budget sane).

TRANSPORT (revised after the 2026-07-16 volume probe — whole-parent ranged statistics
would be 8.5GB/yr on LO alone): monthly 1-day definition SNAPSHOTS (union-merged, a
holiday walks forward) + id-SCOPED statistics pulls for only the instruments whose
strike sits within the ±30% band of that month's own forward, chunked ≤200 ids (the
connector /term ed67f30 pattern, production-proven), all gzip. Tens of MB per
root-year instead of gigabytes.

Pure stdlib. Store: `store/futures/<ROOT>.hist.json` (a bare JSON list of day
records, the opthist container) resolved via the v1.93-style ladder below.
"""
import argparse
import base64
import concurrent.futures
import csv
import datetime as dt
import io
import json
import os
import sys
import threading
import urllib.error
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chart  # noqa: E402 — http_get (cost preflight) + get_key
import dashboard  # noqa: E402 — write_atomic
import futures_options as fo  # noqa: E402 — build_chain, _make_fetch, _px, _oi, resolve_option_root
import opthist  # noqa: E402 — interp_cm, CM_TENORS (contract parity, never re-derived)

_MONEYNESS_CAP = 0.30   # per-series strike window: [0.7F .. 1.3F] (documented in the store meta)
_MAX_DTE = 400          # same horizon as gather_term's max_days
_CHUNK_IDS = 500        # ids per statistics request: the live HO smoke measured 5,268 in-band ids
                        # per month-chunk — at 200/request that was 27 high-latency pulls per month
                        # (the per-request server latency dominates, not bytes). 500 ids ≈ 4.5KB of
                        # URL — comfortably under the ~8KB 414 threshold the connector guards at 200
                        # (its ids ride longer whole-root URLs).


class FuthistError(RuntimeError):
    """An actionable backfill failure (vendor outage class, bad root) — catchable, never a traceback."""


def _make_fetch(dataset=None, env_file=None):
    """futures_options._make_fetch's twin, plus gzip — futhist's statistics pulls are the skill's
    biggest payloads (probe 2026-07-16), so every request advertises Accept-Encoding: gzip.
    chart.http_get stays the single retry authority (v1.111)."""
    dataset = dataset or fo.DATASET
    auth = base64.b64encode((chart.get_key("DATABENTO_API_KEY", env_file) + ":").encode()).decode()
    hdr = {"Authorization": "Basic " + auth}
    frontier = {}

    def fetch(schema, symbols, stype_in, start, endd):
        if schema not in frontier:
            try:
                frontier[schema] = chart.databento_frontier(dataset, schema, auth)
            except Exception:
                frontier[schema] = None
        # 422 past-frontier guard; a FAILED frontier probe clamps at today — the +1-day slop end
        # (today+1) would otherwise 422, a 4xx that never retries (review P3)
        e = min(endd, frontier[schema] or dt.date.today().isoformat())
        p = {"dataset": dataset, "symbols": symbols, "schema": schema, "stype_in": stype_in,
             "stype_out": "instrument_id", "encoding": "csv", "start": start, "end": e}
        url = "https://hist.databento.com/v0/timeseries.get_range?" + urllib.parse.urlencode(p)
        return list(csv.DictReader(io.StringIO(chart.http_get(url, hdr, gzip=True))))
    return fetch


def _chunks(seq, n):
    return [seq[i:i + n] for i in range(0, len(seq), n)]


def _resolve_store_dir(env=None, here=None, expanduser=os.path.expanduser, cache_parent=None):
    """The v1.93 store ladder, futures leaf: MARKET_DATA_STORE_FUTURES env override ->
    <skill-root>/store/futures when the skill's store/ parent exists (the futures leaf is
    CREATED on first write — unlike options it doesn't pre-exist) -> ~/.cache fallback.
    A separate env var from opthist's MARKET_DATA_STORE: that override points AT the
    options dir itself, so sharing it would collide the two stores in one directory."""
    env = os.environ if env is None else env
    override = (env.get("MARKET_DATA_STORE_FUTURES") or "").strip()
    if override:
        return os.path.abspath(os.path.expanduser(override))
    here = os.path.dirname(os.path.abspath(__file__)) if here is None else here
    fallback = expanduser("~/.cache/market-data/futures")
    fallback_parent = cache_parent or os.path.dirname(fallback)
    # A package may be mounted read-only by Desktop/Work.  Ask opthist for the futures leaf specifically:
    # an existing options cache must not prevent the bundled futures history from being unpacked.
    unpacked_parent = getattr(opthist, "_ensure_store_unpacked", lambda *a, **k: None)(
        here, cache_parent=fallback_parent, required_subdir="futures")
    store_parent = os.path.join(os.path.dirname(here), "store")
    if os.path.isdir(store_parent):
        return os.path.join(store_parent, "futures")
    if unpacked_parent and os.path.isdir(os.path.join(unpacked_parent, "futures")):
        return os.path.join(unpacked_parent, "futures")
    return fallback


STORE_DIR = _resolve_store_dir()


def _store_path(root):
    return os.path.join(STORE_DIR, "%s.hist.json" % root.upper())


def load_store(root):
    try:
        with open(_store_path(root), encoding="utf-8") as f:
            v = json.load(f)
        # _desentinel also range-heals legacy cm*/skew30 that pre-v1.125 writes flat-extrapolated
        # past the day's listed-DTE span (the opthist read-heal; records share the contract)
        return opthist._desentinel(v) if isinstance(v, list) else []
    except (OSError, ValueError):
        v = opthist._load_packaged_store_json("futures", root)
        return opthist._desentinel(v) if isinstance(v, list) else []


def _write_store(root, records):
    os.makedirs(STORE_DIR, exist_ok=True)
    dashboard.write_atomic(_store_path(root), json.dumps(records))


_INT64_MAX = 9223372036854775807


def _ns_date(v):
    """A Databento ns-epoch cell -> UTC date, or None for missing/junk/UNDEF sentinels."""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    if n <= 0 or n >= _INT64_MAX:
        return None
    return dt.datetime.fromtimestamp(n / 1e9, dt.timezone.utc).date()


def _month_chunks(start, end):
    """Half-open [start..end) calendar-month chunks as ISO pairs — bounds each statistics
    response (the whole-range pull on a big root is the unbounded-payload failure mode)."""
    out, cur = [], start
    while cur < end:
        nxt = (cur.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
        out.append((cur.isoformat(), min(nxt, end).isoformat()))
        cur = min(nxt, end)
    return out


def _month_covered(cs, ce, have, today):
    """True when a month window's weekdays are already in the store — the month is SKIPPED
    entirely (live 2026-07-16: the already-complete July month re-streamed 106MB for zero new
    records and died mid-body). Half-open [cs..ce) clipped to <= today; the holiday tolerance
    is PROPORTIONAL — weekdays//8 missing allowed (a 22-weekday month tolerates 2, a 12-weekday
    half-chunk its 1 MLK/Juneteenth, a 3-weekday sliver none: 3 missing of 3 is not a holiday,
    it's a hole). Without the tolerance no holiday month would ever skip."""
    s = dt.date.fromisoformat(cs[:10])
    e = min(dt.date.fromisoformat(ce[:10]), today)
    weekdays = []
    d = s
    while d < e:
        if d.weekday() < 5:
            weekdays.append(d.isoformat())
        d += dt.timedelta(days=1)
    if not weekdays:
        return True
    if weekdays[-1] not in have:   # the trailing day's OI publishes overnight — an intraday pull
        # leaves it incomplete/poisoned; come back for it WHILE it can still publish. Recency-gated
        # (v1.125): unconditional, a month whose last weekday is an exchange holiday (May 2027:
        # Memorial Day = the last Monday) could never be covered — statistics never publish for it —
        # and re-entered the backfill queue on every nightly prewarm forever. Once the edge is old,
        # the proportional tolerance below governs (the missing tail day is counted in `missing`).
        if (today - dt.date.fromisoformat(weekdays[-1])).days <= 4:
            return False
    missing = sum(1 for w in weekdays if w not in have)
    return missing <= len(weekdays) // 8


def _classify_parent_422(e):
    """A parent-pull HTTPError: the vendor's smart-symbology outage (seen live 2026-07-16 —
    EVERY stype_in=parent request 422'd symbology_invalid_request while raw/continuous
    resolved fine) is a RETRYABLE outage class, not a bad root — a plain 4xx-never-retry
    rule would misclassify it (design doc, probe notes). Anything else re-raises as-is."""
    if e.code == 422:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        if "symbology_invalid_request" in body:
            raise FuthistError(
                "Databento's parent smart-symbology resolution is failing (a vendor-side outage "
                "— seen 2026-07-16; raw/continuous pulls are unaffected). This is RETRYABLE — "
                "retry the backfill later; the root itself is fine.") from e
    raise e


def _defs_map(rows):
    """Definition rows -> {iid: {strike, is_call, exp(date), under}} (options only; the
    futures/spread rows in a parent listing are skipped; last definition wins per iid)."""
    out = {}
    for r in rows:
        cls = (r.get("instrument_class") or "").strip()
        if cls not in ("C", "P"):
            continue
        K = fo._px(r.get("strike_price"))
        exp = _ns_date(r.get("expiration"))
        iid = str(r.get("instrument_id") or "").strip()
        if K is None or exp is None or not iid:
            continue
        out[iid] = {"strike": K, "is_call": cls == "C", "exp": exp,
                    "under": (r.get("underlying") or "").strip(),
                    "stem": (r.get("raw_symbol") or "").split(" ", 1)[0]}
    return out


def _series_index(dmap):
    """{(exp, under): [iids]} — one entry per option series."""
    idx = {}
    for iid, d in dmap.items():
        idx.setdefault((d["exp"], d["under"]), []).append(iid)
    return idx


def _stats_by_day(rows, acc):
    """Fold statistics rows into {(day, iid): {stat_type: (price_raw, qty_raw)}}. The day
    key is ts_ref — the trading day the stat REFERS to (a settlement often publishes with
    ts_event the NEXT morning); ts_event is only the fallback. Later rows win (corrections)."""
    for r in rows:
        day = _ns_date(r.get("ts_ref")) or _ns_date(r.get("ts_event"))
        if day is None:
            continue
        iid = str(r.get("instrument_id") or "").strip()
        st = str(r.get("stat_type") or "").strip()
        if not iid or not st:
            continue
        acc.setdefault((day, iid), {})[st] = (r.get("price"), r.get("quantity"))


def _underlying_settles(fetch, unders, start, end, workers=8):
    """{(under_raw_symbol, day): settlement} — ONE ranged raw-symbol statistics pull per
    distinct underlying future (a dozen per root, tiny payloads), pulled CONCURRENTLY:
    the fixed per-pull server latency, not bytes, is the cost (the connector ed67f30
    lesson — a sequential live run sat 37 minutes without finishing, 2026-07-16).
    A TRANSIENT pull failure ABORTS the run (review P1: swallowing it as "no data" silently
    re-spotted every record to the next series' settle and dropped the front from cm* —
    and idempotence made the wrong records permanent). An empty SUCCESS stays empty: that
    underlying's series drop per-day with the honest warning."""
    def _one(u):
        try:
            return u, fetch("statistics", u, "raw_symbol", start.isoformat(), end.isoformat()), None
        except Exception as e:                    # post-retry transient — never "no data"
            return u, [], e
    settles, fails = {}, []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, max(1, len(unders)))) as ex:
        for u, rows, err in ex.map(_one, unders):
            if err is not None:
                fails.append(f"{u} ({err})")
                continue
            for r in rows or []:
                if str(r.get("stat_type") or "").strip() != "3":
                    continue
                day = _ns_date(r.get("ts_ref")) or _ns_date(r.get("ts_event"))
                px = fo._px(r.get("price"))
                if day is not None and px is not None:
                    settles[(u, day)] = px
    if fails:
        raise FuthistError("error: underlying settlement pull failed for %s — a transient after "
                           "retries; NOTHING was written (a missing forward would fabricate wrong "
                           "records). Re-run to retry." % ", ".join(sorted(fails)))
    return settles


def _day_record(root, day, dmap, sidx, stats, settles, cap=_MONEYNESS_CAP):
    """One trading day -> an opthist-contract record, or None (a dead day — skipped, the
    equity reduce_asof rule). ATM IV / skew25 / OI totals come from build_chain per series
    (the OI-filtered reducer the live page uses); cm*/skew30 via opthist.interp_cm."""
    series_entries, coi, poi, fronts, dropped = [], 0, 0, [], []
    oi_rows = 0
    for (exp, under), iids in sorted(sidx.items()):
        dte = (exp - day).days
        if not (1 <= dte <= _MAX_DTE):
            continue
        F = settles.get((under, day))
        if F is None:
            dropped.append(f"{under or '?'}/{exp}")   # consolidated below — one line per day, and only
            continue                                  # for a LIVE day (weekend part-days spammed 5 lines each)
        contracts = []
        for iid in iids:
            st = stats.get((day, iid))
            if not st:
                continue
            d = dmap[iid]
            if not (F * (1 - cap) <= d["strike"] <= F * (1 + cap)):
                continue
            px = fo._px(st["3"][0]) if "3" in st else None
            if px is None and "7" in st and "8" in st:
                lo, hi = fo._px(st["7"][0]), fo._px(st["8"][0])
                px = (lo + hi) / 2 if (lo is not None and hi is not None) else None
            if "9" in st:
                oi_rows += 1
            contracts.append({"strike": d["strike"], "is_call": d["is_call"],
                              "oi": fo._oi(st.get("9")), "settlement": px})
        if not contracts:
            continue
        an = fo.build_chain(root, F, contracts, exp.isoformat(), max(1, dte) / 365.0, today=day)
        series_entries.append({
            "dte": dte,
            # THE units boundary: build_chain speaks PERCENT (66.2), the store contract is
            # DECIMAL (0.662) — adapted here and nowhere else (design-doc trap #1)
            "atmIV": (round(an["atmIV"] / 100.0, 5) if an["atmIV"] is not None else None),
            "skew25": an["skew25"],
            # h2 extras BEYOND the opthist contract (builders ignore unknown keys): the per-series
            # forward + stem let the --full header compute an honest change% for ANY target series
            "fwd": round(F, 4), "stem": dmap[iids[0]].get("stem", "")})
        coi += an["callOItotal"]
        poi += an["putOItotal"]
        fronts.append((dte, F))
    if oi_rows == 0:
        # MID-PUBLICATION: settlements are out (18:30) but the overnight OI (stat 9, ~00:28 next
        # day) isn't — a real historical GLBX day always publishes OI. Storing this day would bake
        # a pcr-None, dead-strike-poisoned record that idempotence makes PERMANENT (review P2 —
        # 5 of 124 live records). Skip it un-stored: the trailing-day rule re-pulls and heals it.
        return None
    if not any(s["atmIV"] for s in series_entries):
        return None               # a dead date -> a hole, never a null record (reduce_asof parity)
    if dropped:                   # the day is LIVE but partial — say so, once (never a guessed forward)
        print(f"warning: {day}: {len(dropped)} series dropped (no underlying settlement): "
              + ", ".join(dropped), file=sys.stderr)
    agg = {"date": day.isoformat(), "spot": round(min(fronts)[1], 4), "series": series_entries,
           "pcr": (round(poi / coi, 4) if coi else None)}
    iv_pts = [(s["dte"], s["atmIV"]) for s in series_entries]
    sk_pts = [(s["dte"], s["skew25"]) for s in series_entries]
    for t in opthist.CM_TENORS:
        v = opthist.interp_cm_ranged(iv_pts, t)   # v1.125: range-honest — no extrapolated far tenors
        agg["cm%d" % t] = round(v, 5) if v is not None else None
    s30 = opthist.interp_cm_ranged(sk_pts, 30)
    agg["skew30"] = round(s30, 2) if s30 is not None else None
    return agg


def _defs_snapshot(fetch, opt, cs, ce, max_walk=5):
    """ONE day of parent definitions inside [cs..ce) — the month's instrument snapshot. A
    holiday/empty first day walks forward (≤max_walk days, the connector dfe5591 lookback
    mirrored). A whole-parent DEFS day is ~1-10MB (probed) — the ranged defs pull it
    replaces was 0.5-3GB/yr. Returns [] when the whole walk is empty (a dead month)."""
    s = dt.date.fromisoformat(cs[:10])
    hard_end = dt.date.fromisoformat(ce[:10])
    for i in range(max_walk):
        d0 = s + dt.timedelta(days=i)
        if d0 >= hard_end:
            break
        try:
            rows = fetch("definition", opt + ".OPT", "parent",
                         d0.isoformat(), (d0 + dt.timedelta(days=1)).isoformat())
        except urllib.error.HTTPError as e:
            _classify_parent_422(e)
        if rows:
            return rows
    return []


_HIST_STRIKE_CAP = 100   # h4-B: strikes per series the backfill pulls — the history aggregates need
                         # near-F density (atmIV) + the 0.9F/1.1F wings (skew25), never a fat root's
                         # full $0.5 grid (CL: ~3x fewer ids -> ~3x fewer high-latency chunk pulls)


def _progressive_strikes(strikes, F, cap=_HIST_STRIKE_CAP):
    """A PROGRESSIVE per-series grid for the backfill's id selection (the connector
    selectWindowStrikes idea, aggregate-scoped): dense near the forward (floor gap 0.25% of F),
    widening toward the wings (gap grows to ~20% of the distance from F), the 0.9F/1.1F skew
    wings guaranteed, a hard cap thinning the densest interior (never the ATM). A thin ladder
    (wide gaps) passes through untouched."""
    ss = sorted(set(strikes))
    if not ss or not (F and F > 0):
        return ss
    atm_i = min(range(len(ss)), key=lambda i: abs(ss[i] - F))
    floor_gap = 0.0025 * F
    kept = {ss[atm_i]}
    last = ss[atm_i]
    for i in range(atm_i + 1, len(ss)):          # walk UP from the money
        if ss[i] - last >= max(floor_gap, 0.2 * (ss[i] - F)):
            kept.add(ss[i])
            last = ss[i]
    last = ss[atm_i]
    for i in range(atm_i - 1, -1, -1):           # walk DOWN
        if last - ss[i] >= max(floor_gap, 0.2 * (F - ss[i])):
            kept.add(ss[i])
            last = ss[i]
    for wing in (0.9 * F, 1.1 * F):              # the skew25 wings are load-bearing — guarantee them
        kept.add(min(ss, key=lambda k: abs(k - wing)))
    arr = sorted(kept)
    while len(arr) > cap:                        # thin the densest interior; ATM + extremes survive
        best_i, best_gap = -1, float("inf")
        for i in range(1, len(arr) - 1):
            if arr[i] == ss[atm_i]:
                continue
            g = min(arr[i] - arr[i - 1], arr[i + 1] - arr[i])
            if g < best_gap:
                best_gap, best_i = g, i
        if best_i < 0:
            break
        arr.pop(best_i)
    return arr


def _candidate_iids(sidx, dmap, settles, cs, ce, cap=_MONEYNESS_CAP):
    """The month's id-scope: instruments of series ALIVE in [cs..ce) whose strike sits within
    ±cap of that month's own forward (the FIRST settle of the month for the series' underlying
    — per-series, never the front's), then PROGRESSIVELY thinned (h4-B: dense near F, capped —
    the aggregates never need a fat root's full fine grid). A series with no settle that month
    contributes nothing (its days would drop at the day level anyway)."""
    s = dt.date.fromisoformat(cs[:10])
    e = dt.date.fromisoformat(ce[:10])
    out = set()
    for (exp, under), iids in sidx.items():
        if exp < s or (exp - e).days > _MAX_DTE:
            continue
        F = None
        d = s
        while d < e:
            if (under, d) in settles:
                F = settles[(under, d)]
                break
            d += dt.timedelta(days=1)
        if F is None:
            continue
        band = sorted({dmap[iid]["strike"] for iid in iids
                       if F * (1 - cap) <= dmap[iid]["strike"] <= F * (1 + cap)})
        keep = set(_progressive_strikes(band, F))
        for iid in iids:
            if dmap[iid]["strike"] in keep:
                out.add(iid)
    return out


_PARENT_MONTH_MB = 64   # hybrid threshold: a month whose whole-parent statistics bill at most this
                        # is pulled as ONE parent request (HO ≈ 14MB/mo — the id-scoping was designed
                        # for LO's 918MB months and over-generalized; user 2026-07-16: "зачем нельзя
                        # одним-несколькими запросами?"). Bigger months stay id-scoped.


def backfill(root, years=1.0, fetch=None, today=None, progress=None, env_file=None,
             option_root=None, chunk_ids=_CHUNK_IDS, workers=8, size_fetch=None):
    """Fill the store for `root` over the trailing `years` — the probe-revised transport:
    monthly 1-day definition snapshots (union-merged) + one tiny ranged settle pull per
    distinct underlying + HYBRID month statistics: `size_fetch` (metadata.get_billable_size,
    free — main() wires the live one) sizes each month; a THIN month (≤_PARENT_MONTH_MB)
    is ONE whole-parent request, a fat one goes id-SCOPED (≤chunk_ids per request). No
    seam / a failed probe -> id-scoped (conservative). All independent pulls run
    CONCURRENTLY (workers=8, the connector's own fan-out — the fixed per-pull server
    latency dominates; a sequential live run sat 37 min, 2026-07-16), and every COMPLETED
    month's day records are written to the store IMMEDIATELY, so a killed/failed run
    resumes past what it already landed. A month is ATOMIC: a failed chunk aborts the
    whole month un-written (half a month's chunks would fabricate wrong day records, not
    merely fewer). Idempotent — existing dates are never re-reduced. Returns the number
    of day records ADDED."""
    today = today or dt.date.today()
    start = today - dt.timedelta(days=int(round(365 * years)))
    opt = (option_root or fo.resolve_option_root(root)).upper()
    _have0 = {r.get("date") for r in load_store(root)}
    months = [m for m in _month_chunks(start, today) if not _month_covered(m[0], m[1], _have0, today)]
    if not months:                            # everything already stored -> not one network call
        return 0
    if fetch is None:
        fetch = _make_fetch(fo.DATASET, env_file)
    dmap = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, len(months))) as ex:
        for rows in ex.map(lambda m: _defs_snapshot(fetch, opt, m[0], m[1]), months):
            dmap.update(_defs_map(rows))
    if not dmap:
        raise FuthistError(f"error: no option definitions for {root.upper()} ({opt}.OPT) in "
                           f"{start}..{today} — wrong root, or use --option-root")
    sidx = _series_index(dmap)
    unders = sorted({d["under"] for d in dmap.values() if d["under"]})
    # (f) settle-span narrowing: only PENDING months' days get reduced, so the settle pulls span
    # from the FIRST pending month — a nightly accrete with one open month pulls ~a month of
    # settles per underlying, never the whole year again
    settles_start = max(start, dt.date.fromisoformat(months[0][0][:10]))
    settles = _underlying_settles(fetch, unders, settles_start, today, workers=workers)
    existing = load_store(root)
    have = {r.get("date") for r in existing}
    tally = {"added": 0}
    lock = threading.Lock()      # guards existing/have/tally + the store write (months land out of order)

    def _do_month(m):
        cs, ce = m
        cs_d, ce_d = dt.date.fromisoformat(cs), dt.date.fromisoformat(ce)
        # +1-day SLOP on the statistics window end: the server's range filter works on ts_EVENT,
        # and a day-X OI (stat 9) publishes ~00:28 on X+1 with ts_ref still X (probed live on
        # HOQ6, 2026-07-16) — without the slop every month-END day lost its OI (pcr None, 5 of
        # 124 live records) and the trailing day's ATM went dead-strike-poisoned. Days are
        # clipped back to [cs..ce) at the reduce, so the spill never double-reduces.
        ce_slop = (ce_d + dt.timedelta(days=1)).isoformat()
        use_parent = False
        if size_fetch is not None:
            try:                                     # free billable-size probe decides the transport
                use_parent = float(size_fetch(symbols=opt + ".OPT", schema="statistics",
                                              start=cs, end=ce)) <= _PARENT_MONTH_MB * 1e6
            except Exception:
                use_parent = False                   # probe down -> the conservative id-scoped path
        stats = {}
        if use_parent:                               # a THIN month: the whole root in ONE request
            try:
                _stats_by_day(fetch("statistics", opt + ".OPT", "parent", cs, ce_slop) or [], stats)
            except urllib.error.HTTPError as e:
                _classify_parent_422(e)
            iids = None
        else:
            iids = _candidate_iids(sidx, dmap, settles, cs, ce)
            chunks = _chunks(sorted(iids, key=lambda x: (len(x), x)), chunk_ids)

            def _pull(chunk, _cs=cs, _ce=ce_slop):
                try:
                    return fetch("statistics", ",".join(chunk), "instrument_id", _cs, _ce) or []
                except urllib.error.HTTPError as e:
                    _classify_parent_422(e)
            if chunks:
                with concurrent.futures.ThreadPoolExecutor(
                        max_workers=min(workers, len(chunks))) as ex:
                    for rows in ex.map(_pull, chunks):   # any failed chunk aborts the month UN-written
                        _stats_by_day(rows, stats)
        with lock:                           # reduce + persist under the lock (shared store state)
            month_added = 0
            for day in sorted({d for (d, _) in stats}):
                if not (cs_d <= day < ce_d):         # slop spill (a neighbour month's day) — not ours
                    continue
                if day.isoformat() in have or not (start <= day <= today):
                    continue
                rec = _day_record(root, day, dmap, sidx, stats, settles)
                if rec:
                    existing.append(rec)
                    have.add(day.isoformat())
                    month_added += 1
            if month_added:                  # the month is complete -> persist NOW (resume point)
                # v1.126: RE-MERGE over fresh disk (under the in-process lock) — two PROCESSES on
                # one root (a manual backfill beside the 6h tick) each held their own view and the
                # later full replace dropped the other's months (self-healing via the covered-month
                # skip, but a wasted re-pull). This run's records win per date; a date-less corrupt
                # disk record is dropped (it would KeyError the sort). Known window (accepted): a
                # foreign record absorbed at an EARLIER month's merge can overwrite a newer disk
                # copy of that date at a later month's write — records are deterministic per day.
                _fresh = {r.get("date"): r for r in load_store(root)
                          if isinstance(r, dict) and r.get("date")}
                _fresh.update({r["date"]: r for r in existing
                               if isinstance(r, dict) and r.get("date")})
                existing[:] = sorted(_fresh.values(), key=lambda r: r["date"])
                _write_store(root, existing)
                tally["added"] += month_added
        if progress:
            progress(cs, ce, -1 if iids is None else len(iids))   # -1 = whole-parent month

    # MONTHS fan out too (the 55-min live lesson, 2026-07-16: 4 SEQUENTIAL whole-parent months at
    # ~8-12 min of server-side prep each dominated the run; they are independent — the wall becomes
    # ~the slowest month). Month-level concurrency stays modest (4): the id-scoped path nests its
    # own chunk pool, and a failed month must not stop the others from persisting (errors collected,
    # the FIRST re-raised after all months settle — resume then picks up the missing months).
    errs = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(months))) as ex:
        for f in [ex.submit(_do_month, m) for m in months]:
            try:
                f.result()
            except Exception as e:           # noqa: BLE001 — collected and re-raised below
                errs.append(e)
    if errs:
        for e in errs[1:]:
            print(f"warning: additionally failed month: {e}", file=sys.stderr)
        for e in errs:                       # an ACTIONABLE FuthistError (vendor outage) outranks a
            if isinstance(e, FuthistError):  # generic URLError from an earlier-submitted month (review P3)
                raise e
        raise errs[0]
    return tally["added"]


def _billable_size_fetch(env_file=None):
    """The LIVE metadata.get_billable_size closure for the hybrid month-transport decision
    (zero-cost; the returned value is bytes)."""
    key = chart.get_key("DATABENTO_API_KEY", env_file)
    hdr = {"Authorization": "Basic " + base64.b64encode((key + ":").encode()).decode()}

    def size_fetch(symbols, schema, start, end):
        p = {"dataset": fo.DATASET, "symbols": symbols, "schema": schema,
             "stype_in": "parent", "start": start, "end": end, "mode": "historical"}
        url = ("https://hist.databento.com/v0/metadata.get_billable_size?"
               + urllib.parse.urlencode(p))
        return int(json.loads(chart.http_get(url, hdr, timeout=30, retries=1)))
    return size_fetch


PREWARM_ROOTS = [
    # tier-1 (user decision 2026-07-23, superseding the v1.116 "liquid four"): every root a bare
    # "проанализируй опционы на X" plausibly names — full energy + metals complexes, index, rates
    # (the largest CME options OI), the WASDE grains, euro FX, CME crypto. Micros (MCL/MNG/OMG/
    # MES/MNQ — vol duplicates the majors) and livestock/dairy (thin options) stay out.
    "ES", "NQ",                                  # index
    "CL", "NG", "HO", "RB", "BZ",                # energy (BZ = Brent, BZO options)
    "GC", "SI", "HG", "PL", "PA",                # metals
    "ZC", "ZS", "ZW",                            # grains
    "ZN", "ZB", "SR3",                           # rates
    "6E",                                        # FX
    "BTC", "ETH",                                # crypto
]                                          # override
                                           # with MARKET_DATA_FUT_PREWARM=HO,CL,... (empty = off)


def defs_prewarm_roots(accrete_roots, roots_env=None):
    """The DEFS-ONLY tier: every root that gets a definitions SNAPSHOT but no statistics accrete.

    Measured on the direct path 2026-08-07 (`--full --source databento`): PL, which has both a
    snapshot and a year of vol history, built in 44s cold-day / 16s on a same-day repeat; ZL,
    which had NEITHER, took 200s — and its History tab was EMPTY.  Different roots, so the gap
    between them is not the measurement; the same-root A/B is: ZL with a snapshot and the
    day-cache off ran 114s, i.e. the snapshot is worth 86s (-43%), and the rest is its statistics
    pulls, which the day-cache absorbs on a repeat.  What both rule OUT is history DEPTH: ZL
    carried none in either run and was still the slow one, because history is read off local disk
    in milliseconds and a cold store renders an honest 'warming' tab for free.  So the snapshot is
    a real lever at 5-200 KB per root, while the accrete tier's cost (a YEAR of statistics per
    root) is what must stay bounded — hence two tiers, not one bigger list.

    Returns the roots in `MARKET_DATA_FUT_DEFS_PREWARM` (comma list; EMPTY turns the tier off),
    else every root in the OPTION_ROOTS table, MINUS anything the accrete tier already refreshes.
    Exclusion and dedup are by OPTION CODE, not root name: two roots may map to one code, and
    pulling the same listing twice is the whole cost of this tier paid for nothing."""
    env = roots_env if roots_env is not None else os.environ.get("MARKET_DATA_FUT_DEFS_PREWARM")
    wanted = ([r.strip().upper() for r in env.split(",") if r.strip()] if env is not None
              else list(fo.OPTION_ROOTS))
    seen = {fo.resolve_option_root(r).upper() for r in (accrete_roots or [])}
    out = []
    for r in wanted:
        opt = fo.resolve_option_root(r).upper()
        if opt in seen:
            continue
        seen.add(opt)
        out.append(r)
    return out


def _stamp_path():
    return os.path.join(STORE_DIR, "prewarm.stamp")


def _stamp_read():
    try:
        with open(_stamp_path(), encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def _refresh_defs(root, today, fetch=None):
    """v1.129 F4: keep the root's defs SNAPSHOT fresh so package.sh/prewarm_sync always ship a
    recent listing (the snapshot is what saves cloud the ~2.5-min whole-root defs pull). Refresh
    when the snapshot is ABSENT or older than TTL/2 (~2d) — one metered whole-root defs pull per
    refresh, so the age gate keeps it to ~2/week per root. Returns True when a live pull ran
    (fo._list_series write-throughs the snapshot itself). `fetch` is the test seam.
    Honors the MARKET_DATA_FUT_DEFS_SNAPSHOT kill-switch (snapshot off -> nothing to refresh,
    NO network) — without this, any prewarm(...) call under the hermetic test pin still fired a
    REAL whole-root defs pull per root (the exact leak that hit the canonical store 2026-07-23:
    the pre-v1.129 prewarm tests pass no defs_refresh_fn, so the DEFAULT ran live)."""
    if not fo._defs_snapshot_enabled():
        return False
    opt = fo.resolve_option_root(root).upper()
    snap = fo._defs_snapshot_load(opt, today)
    if snap is not None:
        try:
            if (today - dt.date.fromisoformat(snap[1])).days <= max(1, fo._defs_ttl_days() // 2):
                return False
        except (ValueError, TypeError):
            pass                                   # unparsable asOf -> treat as stale
    fo._list_series(root, fetch or _make_fetch(), today, fresh=True)
    return True


def _defs_tier(accrete_roots, today, defs_refresh_fn=None, defs_roots_env=None):
    """The defs-only pass: refresh the definition snapshot of every root the accrete tier does not
    cover. Best-effort per root, and it runs AFTER the stamp — a kill mid-tier must not cost the
    accrete its clean day. The kill-switch skips the tier ENTIRELY (not just per root): with
    snapshots off there is nothing to refresh, and the hermetic suites stay silent as well as
    network-free."""
    if not fo._defs_snapshot_enabled():
        return 0
    droots = defs_prewarm_roots(accrete_roots, roots_env=defs_roots_env)
    if not droots:
        return 0
    pulled = 0
    for r in droots:
        try:
            if (defs_refresh_fn or _refresh_defs)(r, today):
                pulled += 1
        except Exception as e:                # noqa: BLE001 — one thin root's flake stops nothing
            print(f"prewarm: {r} defs-only snapshot refresh failed — {e}", file=sys.stderr)
    print(f"prewarm: defs-only tier — {pulled} of {len(droots)} snapshots pulled "
          f"(the rest were within the {max(1, fo._defs_ttl_days() // 2)}-day age gate; "
          f"MARKET_DATA_FUT_DEFS_PREWARM= turns the tier off)", file=sys.stderr)
    return pulled


def prewarm(roots=None, years=1.0, daily_once=False, backfill_fn=None, today=None, roots_env=None,
            defs_refresh_fn=None, defs_roots_env=None):
    """The nightly futures accrete (slice f) — the opthist prewarm rules mirrored: the liquid
    four by default (MARKET_DATA_FUT_PREWARM overrides; an EMPTY value turns it off), BEST-EFFORT
    per root (one failure never stops the rest), and the daily-once stamp is written ONLY on a
    COMPLETE clean pass — a failed root leaves the day unstamped so the next tick retries.
    A warmed root's accrete is cheap: the covered-month skip + the settle-span narrowing leave
    ~one open month of pulls. `backfill_fn`/`roots_env`/`today` are test seams.

    Then the DEFS-ONLY tier (defs_prewarm_roots) refreshes the definition snapshot of every OTHER
    root the skill can name — the 156s that separated a snapshot-less ZL from PL. It runs LAST and
    is fully best-effort: its roots never enter the returned accrete result (which drives the
    "+N day records" report and the exit code) and never enter the day stamp, or a --daily-once
    tick tomorrow would conclude ZL's year of statistics had already been pulled."""
    today = today or dt.date.today()
    if roots is None:
        env_val = roots_env if roots_env is not None else os.environ.get("MARKET_DATA_FUT_PREWARM")
        if env_val is not None:
            roots = [r.strip().upper() for r in env_val.split(",") if r.strip()]
        else:
            roots = list(PREWARM_ROOTS)
    if daily_once:
        # v1.126: the stamp carries the ROOT SET — a clean manual single-root pass (HO) used to
        # stamp the bare date and silently suppress the nightly liquid-four until tomorrow. The
        # day is skipped only when TODAY's stamp already covers every requested root; a legacy
        # date-only stamp covers nothing (one extra clean pass, then it carries roots).
        _st = _stamp_read() or ""
        _sd, _, _sr = _st.partition("|")
        if _sd == today.isoformat() and _sr and set(roots) <= set(_sr.split(",")):
            # The stamp guards the YEAR of statistics, NOT a 5-day-TTL listing: skipping the defs
            # tier here would let a snapshot rot for as long as the accrete kept passing.
            _defs_tier(roots, today, defs_refresh_fn, defs_roots_env)
            return {}
    backfill_fn = backfill_fn or (lambda r, y: backfill(r, years=y))
    out, clean = {}, True
    for r in roots:
        try:
            out[r] = backfill_fn(r, years)
        except Exception as e:                # noqa: BLE001 — best-effort: log, continue, no stamp
            out[r] = str(e)
            clean = False
            print(f"prewarm: {r} failed — {e}", file=sys.stderr)
            continue
        try:
            # v1.129 F4: opportunistic defs-snapshot refresh — best-effort, a flake NEVER blocks
            # the accrete result or the stamp (the stamp guards accrete completeness only).
            (defs_refresh_fn or _refresh_defs)(r, today)
        except Exception as e:                # noqa: BLE001
            print(f"prewarm: {r} defs-snapshot refresh failed — {e}", file=sys.stderr)
    if clean and out:
        try:
            os.makedirs(STORE_DIR, exist_ok=True)
            _pv = _stamp_read() or ""
            _pd, _, _pr = _pv.partition("|")
            _acc = set(roots) | ({r for r in _pr.split(",") if r} if _pd == today.isoformat() else set())
            with open(_stamp_path(), "w", encoding="utf-8") as f:
                f.write(today.isoformat() + "|" + ",".join(sorted(_acc)))   # same-day root sets accumulate
        except OSError:
            pass
    _defs_tier(roots, today, defs_refresh_fn, defs_roots_env)
    return out


def _defs_only_cli(root=None, env_file=None, today=None, refresh_fn=None, roots_env=None):
    """`--defs-only`: refresh definition snapshots and pull no statistics at all. Returns the
    process exit code (1 when any root failed) — the tier is best-effort per root, exactly like
    the nightly one. ONE authenticated fetch serves the whole tier; `refresh_fn`/`roots_env`/
    `today` are the test seams."""
    today = today or dt.date.today()
    roots = [root.upper()] if root else defs_prewarm_roots([], roots_env=roots_env)
    if not roots:
        print("defs-only: the tier is empty (MARKET_DATA_FUT_DEFS_PREWARM is set to nothing)")
        return 0
    if not fo._defs_snapshot_enabled():
        print("defs-only: snapshots are OFF (MARKET_DATA_FUT_DEFS_SNAPSHOT=0) — nothing to refresh")
        return 0
    fetch = None
    if refresh_fn is None:
        refresh_fn = _refresh_defs
        try:
            fetch = _make_fetch(env_file=env_file)
        except Exception as e:                # noqa: BLE001 — a missing key must not traceback
            print(f"defs-only: no usable Databento credentials — {e}", file=sys.stderr)
            return 2
    bad = 0
    for r in roots:
        opt = fo.resolve_option_root(r).upper()
        try:
            pulled = refresh_fn(r, today, fetch=fetch)
            print(f"  {r} ({opt}): {'pulled' if pulled else 'within the age gate — skipped'}")
        except Exception as e:                # noqa: BLE001 — one thin root's flake stops nothing
            bad += 1
            print(f"  {r} ({opt}): FAILED — {e}")
    print(f"defs-only: {len(roots) - bad} of {len(roots)} roots OK")
    return 1 if bad else 0


def _preflight(opt, start, end, env_file=None, cost_fetch=None):
    """Zero-cost metadata.get_cost estimate, printed to stderr BEFORE any pull (the design
    rule: every backfill states its cost first). Best-effort — a preflight failure warns
    and the backfill proceeds (L0 schemas measured $0.00 on this key, 2026-07-16)."""
    try:
        if cost_fetch is None:
            import base64
            key = chart.get_key("DATABENTO_API_KEY", env_file)
            hdr = {"Authorization": "Basic "
                   + base64.b64encode((key + ":").encode()).decode()}

            def cost_fetch(symbols, schema, start, end):
                p = {"dataset": fo.DATASET, "symbols": symbols, "schema": schema,
                     "stype_in": "parent", "start": start, "end": end, "mode": "historical"}
                url = ("https://hist.databento.com/v0/metadata.get_cost?"
                       + urllib.parse.urlencode(p))
                return float(json.loads(chart.http_get(url, hdr)))
        total = sum(cost_fetch(symbols=opt + ".OPT", schema=s, start=start, end=end)
                    for s in ("statistics", "definition"))
        print(f"cost preflight: ${round(total, 2)} (statistics+definition · {start}..{end})",
              file=sys.stderr)
    except Exception as e:
        print(f"warning: cost preflight failed ({e}) — proceeding (L0 schemas measured "
              f"$0.00 on this key, 2026-07-16)", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description="Futures vol-history store (Databento statistics "
                                             "backfill) — design: docs/futures-full-plan.md")
    ap.add_argument("root", nargs="?", default=None,
                    help="futures root (CL, ES, GC, NG, HO…) — the OPTION code resolves via the "
                         "OPTION_ROOTS table (--option-root overrides). Optional with --prewarm.")
    ap.add_argument("--backfill", action="store_true", help="fill the store over --years")
    ap.add_argument("--prewarm", action="store_true",
                    help="accrete the tier-1 set (21 roots since v1.129: index ES/NQ · energy "
                         "CL/NG/HO/RB/BZ · metals GC/SI/HG/PL/PA · grains ZC/ZS/ZW · rates "
                         "ZN/ZB/SR3 · 6E · BTC/ETH; MARKET_DATA_FUT_PREWARM overrides) — "
                         "best-effort per root; with --daily-once a complete pass stamps the day and "
                         "same-day reruns no-op (the nightly prewarm_sync.sh entrypoint)")
    ap.add_argument("--daily-once", action="store_true",
                    help="with --prewarm: skip when today's pass already completed cleanly")
    ap.add_argument("--defs-only", action="store_true",
                    help="refresh the DEFINITION snapshots and nothing else — the whole "
                         "OPTION_ROOTS table minus the accrete tier (MARKET_DATA_FUT_DEFS_PREWARM "
                         "overrides; a bare ROOT does just that root). Measured 2026-08-07, "
                         "same-root A/B: ZL --full ran 200s with no snapshot, 114s with one "
                         "(-43%%; day-cache off both times). Costs one whole-root definition pull "
                         "per stale root and no statistics at all")
    ap.add_argument("--years", type=float, default=1.0, help="backfill depth (default 1.0)")
    ap.add_argument("--status", action="store_true", help="show the store depth and exit")
    ap.add_argument("--option-root", default=None, help="override the option asset code (BZ->BZO)")
    ap.add_argument("--env-file", default=None, help="path to a .env with DATABENTO_API_KEY")
    a = ap.parse_args()
    if a.defs_only:
        sys.exit(_defs_only_cli(a.root, env_file=a.env_file))
    if a.prewarm:
        roots = [a.root.upper()] if a.root else None
        res = prewarm(roots=roots, years=a.years, daily_once=a.daily_once)
        if not res:
            print("prewarm: nothing to do (already stamped today, or the root set is empty)")
        for r, v in res.items():
            print(f"  {r}: {'+%d day records' % v if isinstance(v, int) else 'FAILED — ' + v}")
        sys.exit(0 if all(isinstance(v, int) for v in res.values()) else 1)
    if not a.root:
        ap.error("root is required unless --prewarm")
    root = a.root.upper()
    if a.status or not a.backfill:
        H = load_store(root)
        if H:
            print(f"{root}: {len(H)} day records · {H[0]['date']} .. {H[-1]['date']} "
                  f"· store {_store_path(root)}")
        else:
            print(f"{root}: store empty ({_store_path(root)}) — run --backfill")
        return
    today = dt.date.today()
    start = today - dt.timedelta(days=int(round(365 * a.years)))
    opt = (a.option_root or fo.resolve_option_root(root)).upper()
    _preflight(opt, start.isoformat(), today.isoformat(), env_file=a.env_file)

    def prog(cs, ce, n):
        how = "whole-parent (thin month)" if n < 0 else f"{n:,} instruments in band"
        print(f"  {cs}..{ce}: {how} · month persisted", file=sys.stderr, flush=True)
    try:
        sz = None
        try:
            sz = _billable_size_fetch(a.env_file)
        except Exception:
            pass                                    # no key/net -> backfill's id-scoped default
        added = backfill(root, years=a.years, option_root=a.option_root,
                         env_file=a.env_file, progress=prog, size_fetch=sz)
    except FuthistError as e:
        sys.exit(str(e))
    H = load_store(root)
    print(f"{root}: +{added} day records (store now {len(H)} · "
          f"{H[0]['date'] if H else '—'} .. {H[-1]['date'] if H else '—'})")


if __name__ == "__main__":
    main()
