#!/usr/bin/env python3
"""
moex_bonds.py — Russian bonds (MOEX ISS): the ISSUE dashboard + the MARKET/curve dashboard.

Design of record: docs/moex-bonds-plan.md (every endpoint/field probed live 2026-07-24/25).

Bonds are the one RU asset class where ISS is RICHER than what the skill gets for equities: yield,
duration, Z-spread and G-spread are published FIRST-PARTY per instrument (`marketdata_yields`), and
the zero-coupon curve (КБД / G-curve) comes as ready points. Nothing here is scraped and nothing
needs a key.

  moex_bonds.py <SECID>              text read
  moex_bonds.py <SECID> --json       the structured record
  moex_bonds.py <SECID> --html P     the ISSUE dashboard  (Обзор · Денежный поток · История)
  moex_bonds.py --market [--html P]  the MARKET dashboard (кривая · ОФЗ · корпоративные)

SOURCES (all keyless, all first-party MOEX):
  /engines/stock/markets/bonds/securities/<SECID>   securities + marketdata + marketdata_yields
  /securities/<SECID>/bondization.json?limit=unlimited   coupons + amortizations + offers
  /engines/stock/zcyc.json                          the G-curve: NSS `params` + published `yearyields`
  /history/engines/stock/zcyc.json                  the curve back to 2014 — `params` ONLY
  /engines/stock/markets/bonds/boards/{TQOB,TQCB}/securities.json   the universe, WITH analytics

HONESTY (hard rules — half the corporate universe is NOT a plain fixed-coupon bond):
  * A yield is NEVER shown without what it is computed TO. ISS's `YIELDDATETYPE` distinguishes
    MATDATE from OFFER, and the difference is not cosmetic: ФСК РС-22 prints -28.891% to an offer
    four days out. Labelled "yield to maturity" that is wrong by tens of points.
  * FLOATERS GET NO YTM. ISS publishes `EFFECTIVEYIELD` for them anyway — ЯНДЕКС1Р1 reads -1.4007
    with a -1522bp G-spread — because YTM is undefined when the coupon floats. We drop it and say why.
  * A foreign-currency issue's yield NEVER touches the ₽ curve (no G/Z-spread card, not on the
    scatter) — the same currency discipline the RU-fundamentals module applies to a USD reporter.
  * NO CREDIT RATINGS. They are not in ISS and the agencies are bot-walled. `LISTLEVEL` and the
    rating-BUCKET indices are surrogates and are labelled as such — never as "this bond is rated A".
  * A hole is None: the card/point is absent, never a fabricated 0.
  * The turnover filter on any universe panel is stated on the panel.
  * ISS is the exchange's own data, so "официальные данные биржи" is fair here (unlike the smart-lab
    path) — but the `ZCYCMOMENT`/`SYSTIME` timestamps ride the page.
  * Not advice: the page describes instruments, it does not recommend them.
"""
from __future__ import annotations
import argparse
import datetime as dt
import gzip
import hashlib
import json
import math
import os
import re
import sys
import time as _time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # Windows-safe
import dashboard  # noqa: E402
import moex_options as mo  # noqa: E402 — the ISS base + the injectable seam + market_session

ISS = mo.ISS
CACHE_ENV = "MARKET_DATA_MOEXBONDS_CACHE"   # a dir override, or 'off'/'0' to disable the per-day cache
BOARD_OFZ = "TQOB"
BOARD_CORP = "TQCB"
MIN_TURNOVER = 1e7          # ₽10M/day — below this a "market" row is one trade, not a price
MIN_SCATTER_TURNOVER = 1e6  # ₽1M/day for the OFZ map: a ₽38k quote is a stale print, not a yield
_MIN_SPREAD_DAYS = 30       # below this duration a G/Z-spread is annualisation noise, not a premium
_RUB = ("SUR", "RUB")
_UP, _DOWN, _BLUE, _ORANGE = "#2e9e5b", "#C0392B", "#2a78d6", "#eb6834"

# The published MOEX G-curve constants (moex.com/s2532): a1 = 0, a2 = 0.6, k = 1.6,
# a(i+1) = a(i) + a2·k^(i-1), b(i+1) = b(i)·k. See § curve for why we can verify this live.
NSS_K, NSS_A1, NSS_A2 = 1.6, 0.0, 0.6
_CURVE_TOL_PP = 0.05        # our NSS vs the published yearyields; beyond this the historical legs go

_CUR_SYM = {"SUR": "₽", "RUB": "₽", "USD": "$", "EUR": "€", "CNY": "¥", "CHF": "CHF"}


# ----------------- small helpers -----------------
def _urlq(t):
    from urllib.parse import quote
    return quote(str(t))


def _ff(v):
    """A float, or None. ISS sends '', None and 0-as-missing in different columns."""
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _date(v):
    """An ISO date string, or None. ISS uses '0000-00-00' as its NULL-date sentinel — a naive
    parse turns that into a bogus year-0 offer, so it is rejected here at the seam."""
    if not v or not isinstance(v, str):
        return None
    v = v.strip()
    if not v or v.startswith("0000"):
        return None
    try:
        dt.date(int(v[0:4]), int(v[5:7]), int(v[8:10]))
    except (ValueError, IndexError):
        return None
    return v[:10]


def _d(s):
    return dt.date(int(s[0:4]), int(s[5:7]), int(s[8:10]))


def _num(v, nd=2):
    if v is None:
        return "n/a"
    return ("%%.%df" % nd) % v


def _cur(unit):
    return _CUR_SYM.get((unit or "").upper(), (unit or "").upper())


def _money(v, unit="SUR"):
    """₽/$ with RUSSIAN magnitude words — dashboard.cap_str emits "₽2B", which reads as a typo here."""
    if v is None:
        return "n/a"
    c = _cur(unit)
    for lim, suf in ((1e12, "трлн"), (1e9, "млрд"), (1e6, "млн"), (1e3, "тыс.")):
        if abs(v) >= lim:
            return "%s%s %s" % (c, ("%.2f" % (v / lim)).rstrip("0").rstrip("."), suf)
    return "%s%s" % (c, _num(v, 2))


def _first(block):
    rows = mo._rows(block or {})
    return rows[0] if rows else {}


def _pick(block, board=None):
    """The row for `board`, else row 0. A bond is quoted on SEVERAL boards with DIFFERENT settlement
    currencies (СибурХ1Р03: TQCB settles in SUR, TQOD in USD) and ISS does not order them for us —
    taking row 0 blindly mixes a rouble accrued-interest with a dollar nominal."""
    rows = mo._rows(block or {})
    if board:
        for r in rows:
            if r.get("BOARDID") == board:
                return r
    return rows[0] if rows else {}


# ----------------- per-day disk cache -----------------
def _cache_dir():
    v = os.environ.get(CACHE_ENV, "").strip()
    if v.lower() in ("0", "off", "false", "no"):
        return None
    if v:
        return os.path.abspath(os.path.expanduser(v))
    return os.path.join(os.path.expanduser("~"), ".cache", "market-data", "moexbonds")


def _cache_name(name, kind, day):
    raw = (name or "_").upper()
    safe = "".join(c for c in raw if c.isalnum() or c in "._-")
    # the strip is lossy, and since v1.153 a cache name can carry FREE TEXT (the --issuer name), so
    # two different issuers differing only in stripped characters shared one file and the second
    # run silently rendered the FIRST issuer's issues under the requested name. A short digest of
    # the ORIGINAL restores the distinction without making the filename unreadable.
    if safe != raw:
        safe = "%s-%s" % (safe, hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8])
    return "%s.%s.%s.json.gz" % (safe or "_", day, kind)


def _cache_path(name, kind, day):
    return os.path.join(_cache_dir(), _cache_name(name, kind, day))


def _cache_get(name, kind, day):
    d = _cache_dir()
    if not d:
        return None
    import zlib
    p = _cache_path(name, kind, day)
    try:
        with gzip.open(p, "rt", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None
    except (OSError, EOFError, ValueError, zlib.error):
        # a corrupt gzip BODY raises zlib.error (NOT an OSError subclass — the v1.128 opthist class).
        # Delete the poisoned file so it can't crash-loop every same-day run, then treat it as a miss.
        try:
            os.unlink(p)
        except OSError:
            pass
        return None


def _cache_put(name, kind, day, text):
    d = _cache_dir()
    if not d or not text:
        return
    import tempfile
    tmp = None
    try:
        os.makedirs(d, exist_ok=True)
        pref = "".join(c for c in (name or "_").upper() if c.isalnum() or c in "._-") + "."
        now = _time.time()
        for fn in os.listdir(d):
            fp = os.path.join(d, fn)
            if fn.startswith(pref) and fn.endswith(".json.gz") and (".%s." % day) not in fn:
                try:
                    os.unlink(fp)
                except OSError:
                    pass
            elif fn.endswith(".tmp"):
                # orphaned by a hard kill mid-write; without this they accumulate forever
                try:
                    if now - os.path.getmtime(fp) > 3600:
                        os.unlink(fp)
                except OSError:
                    pass
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        os.close(fd)
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, _cache_path(name, kind, day))
        tmp = None
    except OSError:
        pass
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


# ----------------- fetch layer (seam-injectable, never hard-fails) -----------------
def _pull(name, kind, path, get, seam, day):
    """One cached ISS GET -> parsed JSON, or None. Any failure degrades to None (the caller drops
    that panel); an injected `fetch` seam bypasses the cache entirely (the seam IS the truth)."""
    if not seam:
        hit = _cache_get(name, kind, day)
        if hit is not None:
            try:
                return json.loads(hit)
            except ValueError:
                pass
    try:
        text = get(path)
    except Exception:                                          # noqa: BLE001 — best-effort by contract
        return None
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not seam:
        _cache_put(name, kind, day, text)
    return obj


_HIST_PAGE = 100        # ISS pages /history at 100 rows and does NOT tell you it truncated
_HIST_MAX_PAGES = 30    # a full issue history: measured ОФЗ 26238 = 1356 rows / 14 pages / 1.9 s,
#                         ГТЛК 1P-01 (issued 2016) needs >20. Still bounded, and day-cached.


def _pull_paged(name, kind, base_path, get, seam, day, block="history"):
    """A paged ISS /history GET -> one merged payload, or None.

    ISS caps a /history response at 100 rows and does NOT say it truncated — reading only the first
    page put a 4-month-old window under a chart captioned as the last year, and did the same to the
    RGBITR panel. Every /history endpoint in this module goes through here."""
    if not seam:
        hit = _cache_get(name, kind, day)
        if hit is not None:
            try:
                return json.loads(hit)
            except ValueError:
                pass
    cols, data, ok, complete = None, [], False, False
    for _ in range(_HIST_MAX_PAGES):
        try:
            obj = json.loads(get(base_path + "&start=%d" % len(data)))
        except Exception:                                      # noqa: BLE001 — best-effort by contract
            break
        ok = True
        blk = (obj or {}).get(block) or {}
        rows = blk.get("data") or []
        cols = cols or blk.get("columns")
        data.extend(rows)
        if len(rows) < _HIST_PAGE:
            complete = True
            break
    if not ok:
        return None
    merged = {block: {"columns": cols or [], "data": data}}
    # only a COMPLETE walk is cached: caching a series truncated by a mid-walk failure would freeze
    # the short window for the rest of the day with no refetch
    if not seam and complete:
        _cache_put(name, kind, day, json.dumps(merged, ensure_ascii=False))
    return merged


def _pull_history(sid, frm, get, seam, day, board=None):
    """The bond's daily history, BOARD-SCOPED. An unscoped pull mixes off-market boards in (live
    SU26238RMFS4: 2 of 61 rows came from the PACT auction board, with duplicate dates and a None
    close, drawn as spurious dips and counted as trading days in the panel's own caption). BOARDID
    is requested too so the parse can drop anything that still slips through."""
    seg = ("/boards/%s" % board) if board else ""
    base = ("/history/engines/stock/markets/bonds%s/securities/%s.json?iss.meta=off&from=%s"
            "&iss.only=history"
            "&history.columns=BOARDID,TRADEDATE,CLOSE,YIELDCLOSE,DURATION,ACCINT,VALUE"
            % (seg, sid, frm))
    return _pull_paged(sid, "history", base, get, seam, day)


def _getter(fetch, _http):
    from chart import http_get
    return fetch or _http or (lambda p: http_get(ISS + p))


def gather(secid, fetch=None, _http=None, today=None, days=None, with_market=True):
    """Every ISS block the ISSUE page needs. Best-effort throughout: a missing block is None."""
    day = (today or dt.date.today()).isoformat()
    seam = fetch is not None
    get = _getter(fetch, _http)
    sid = (secid or "").upper()
    bond = _pull(sid, "bond", "/engines/stock/markets/bonds/securities/%s.json?iss.meta=off" % sid,
                 get, seam, day)
    # limit=unlimited is REQUIRED — the default page truncates a long coupon schedule.
    bz = _pull(sid, "bondization",
               "/securities/%s/bondization.json?iss.meta=off&limit=unlimited" % sid, get, seam, day)
    zc = _pull("_ZCYC", "zcyc", "/engines/stock/zcyc.json?iss.meta=off&iss.only=params,yearyields",
               get, seam, day)
    # the whole life of the issue, not an arbitrary window: it costs seconds and is day-cached,
    # and a percentile/chart over one year cannot see a rate cycle
    frm = ((today or dt.date.today()) - dt.timedelta(days=days or _HIST_SPAN_D)).isoformat()
    _my = _first((bond or {}).get("marketdata_yields"))
    _bd = _my.get("BOARDID") or _first((bond or {}).get("securities")).get("BOARDID")
    hist = _pull_history(sid, frm, get, seam, day, board=_bd)
    # the money-market base rate, for a FLOATER's coupon overlay only (one paged pull, day-cached)
    rus = None
    if classify(_first((bond or {}).get("securities")))["floater"]:
        rus = _pull_paged("_RUSFAR", "rusfar",
                          "/history/engines/stock/markets/index/boards/MMIX/securities/RUSFAR.json"
                          "?iss.meta=off&from=%s&iss.only=history&history.columns=TRADEDATE,CLOSE" % frm,
                          get, seam, day)
    out = {"bond": bond, "bondization": bz, "zcyc": zc, "history": hist, "rusfar": rus, "secid": sid}
    _sec = _first((bond or {}).get("securities"))
    # the ISSUER curve (corporates only — an ОФЗ's issuer curve IS the sovereign curve already on
    # the page) and the MARKET tab. Both best-effort: a failure costs the panel/tab, never the page.
    if _sec and not sid.startswith("SU"):
        try:
            isr = fetch_issuer(sid, _sec.get("SHORTNAME") or "", fetch=fetch, _http=_http, today=today)
            if isr.get("secids"):
                out["issuer"] = {"emitentId": isr["emitentId"], "prefix": isr["prefix"],
                                 "secids": sorted(isr["secids"]),
                                 "truncated": bool(isr.get("truncated"))}
        except Exception as _e:                                # noqa: BLE001
            print("note: issuer lookup failed (%r) — no issuer curve." % _e, file=sys.stderr)
    if with_market and _sec:          # an unresolvable SECID must not cost the market pull
        try:
            out["curves"] = fetch_curve_slices(get, seam, day, today)
        except Exception as _e:                                # noqa: BLE001
            print("note: ZCYC history failed (%r) — no G-spread history." % _e, file=sys.stderr)
        try:
            mk = gather_market(fetch=fetch, _http=_http, today=today)
            if mk.get("tqob") or mk.get("tqcb"):
                out["market"] = mk       # `board` is market["tqcb"] — kept as ONE copy, not two
        except Exception as _e:                                # noqa: BLE001
            print("note: market pull failed (%r) — no Рынок tab." % _e, file=sys.stderr)
    return out


def fetch_curve_slices(get, seam, day, today=None, slices=12):
    """Monthly ZCYC slices for the G-spread history: {date: params}.

    Cached per DATE and NOT per bond — the curve is the same for every issue, so the ~12 s cold cost
    is paid once a day across the whole module. `date=` is the working parameter (from/till are
    silently ignored) and each slice re-checks the tradedate it got back; a weekend walks back up to
    5 days. Best-effort: any slice that does not come back is simply absent from the series."""
    base = today or dt.date.today()
    out, misses = {}, 0
    for k in range(slices):
        if misses >= _CURVE_MISS_STOP:
            # a timing-out ZCYC endpoint would otherwise wedge EVERY plain render for minutes
            print("note: ZCYC history unavailable after %d misses — the G-spread history will be "
                  "shorter or absent." % misses, file=sys.stderr)
            break
        want = base - dt.timedelta(days=_CURVE_STEP_D * k)
        got = False
        for step in range(5):
            dd = (want - dt.timedelta(days=step)).isoformat()
            blk = _pull("_ZCYCD", "d" + dd,
                        "/history/engines/stock/zcyc.json?iss.meta=off&date=%s" % dd, get, seam, day)
            p = curve_params(blk, pick="last")
            if p and p.get("tradedate") == dd:
                out[dd] = p          # the ONE parsed row, not the 22 000-row intraday block
                got = True
                break
        misses = 0 if got else misses + 1
    return out


def gather_group(name, fetch=None, _http=None, today=None, with_market=True):
    """Everything the ISSUER-GROUP page needs. Best-effort throughout, like every other gather."""
    day = (today or dt.date.today()).isoformat()
    seam = fetch is not None
    get = _getter(fetch, _http)
    grp = resolve_issuer_group(name, fetch=fetch, _http=_http, today=today)
    out = {"issuerGroup": grp}
    if not grp.get("secids"):
        return out
    # The BOARD is what makes a member a member, and the curve needs the ZCYC — so both are pulled
    # ALWAYS, under the very same day-cache keys `gather_market` uses (`--market` later that day
    # costs nothing extra). `--no-market` drops the Рынок TAB, not the page's own population:
    # gating membership on it made `--issuer --no-market` print «Выпусков 0» beside a calendar
    # covering 2 issues — a self-contradiction, not a degradation (review r1 P1).
    only = "iss.only=securities,marketdata,marketdata_yields"
    out["board"] = _pull("_TQCB", "board",
                         "/engines/stock/markets/bonds/boards/%s/securities.json?iss.meta=off&%s"
                         % (BOARD_CORP, only), get, seam, day)
    out["zcyc"] = _pull("_ZCYC", "zcyc",
                        "/engines/stock/zcyc.json?iss.meta=off&iss.only=params,yearyields",
                        get, seam, day)
    if with_market:
        try:
            mk = gather_market(fetch=fetch, _http=_http, today=today)
            out["market"] = mk
            out["board"] = mk.get("tqcb") or out["board"]
            out["zcyc"] = mk.get("zcyc") or out["zcyc"]
        except Exception as _e:                                # noqa: BLE001
            print("note: market pull failed (%r)." % _e, file=sys.stderr)
    # the calendar needs each member's own schedule — for the issues this page actually LISTS, in ₽.
    # Pulling by search order instead spent the budget on the OLDEST (often redeemed) matches and
    # aggregated paper the page never shows, then disclosed the count against a different
    # population («по 12 выпускам из 7»).
    rows = {}
    try:
        rows = {r["secid"]: r for r in _board_rows(out.get("board"))}
    except Exception:                                          # noqa: BLE001
        rows = {}
    elig = [s for s in grp["secids"] if s in rows and not rows[s].get("fx")]
    bz = {}
    for sid in elig[:_GROUP_CAL_MAX]:
        blk = _pull(sid, "bondization",
                    "/securities/%s/bondization.json?iss.meta=off&limit=unlimited" % sid,
                    get, seam, day)
        if blk:
            bz[sid] = blk
    out["bondizations"] = bz
    return out


def gather_market(fetch=None, _http=None, today=None):
    """The MARKET page: the curve + both boards (each board is ONE call, analytics included)."""
    day = (today or dt.date.today()).isoformat()
    seam = fetch is not None
    get = _getter(fetch, _http)
    only = "iss.only=securities,marketdata,marketdata_yields"
    zc = _pull("_ZCYC", "zcyc", "/engines/stock/zcyc.json?iss.meta=off&iss.only=params,yearyields",
               get, seam, day)
    tqob = _pull("_TQOB", "board",
                 "/engines/stock/markets/bonds/boards/%s/securities.json?iss.meta=off&%s" % (BOARD_OFZ, only),
                 get, seam, day)
    tqcb = _pull("_TQCB", "board",
                 "/engines/stock/markets/bonds/boards/%s/securities.json?iss.meta=off&%s" % (BOARD_CORP, only),
                 get, seam, day)
    rgbi = _pull_paged("_RGBI", "index",
                       "/history/engines/stock/markets/index/securities/RGBITR.json?iss.meta=off"
                       "&from=%s&iss.only=history&history.columns=TRADEDATE,CLOSE"
                       % ((today or dt.date.today()) - dt.timedelta(days=365)).isoformat(),
                       get, seam, day)
    # /history/.../zcyc takes `date=` — it SILENTLY IGNORES from/till and hands back today's curve
    # instead (verified live: from=2025-07-20 returned 22,514 rows all dated 2026-07-24). Asking the
    # wrong way therefore does not fail, it LIES, so every leg re-checks the date it got back.
    zh = {}
    for lab, back in (("1M", 30), ("1Y", 365)):
        want = (today or dt.date.today()) - dt.timedelta(days=back)
        for step in range(5):                    # walk back off a weekend/holiday to a trading day
            dd = (want - dt.timedelta(days=step)).isoformat()
            blk = _pull("_ZCYCH", "zcych%s%d" % (lab, step),
                        "/history/engines/stock/zcyc.json?iss.meta=off&date=%s" % dd, get, seam, day)
            if (mo._rows((blk or {}).get("params") or {}) or [None])[0]:
                zh[lab] = {"want": dd, "block": blk}
                break
    return {"zcyc": zc, "tqob": tqob, "tqcb": tqcb, "rgbi": rgbi, "zcycHist": zh}


# ----------------- the curve (КБД / G-curve) -----------------
def nss_knots():
    """MOEX's fixed correction-term knots: a1=0, a2=0.6, k=1.6, a(i+1)=a(i)+b(i), b(i+1)=b(i)·k."""
    a, b = [NSS_A1, NSS_A2], [NSS_A2 - NSS_A1]
    for i in range(1, 9):
        b.append(NSS_K * b[i - 1])
        a.append(a[i] + b[i - 1])
    return a, b


def curve_params(zcyc, pick="first"):
    """The NSS parameter row {B1,B2,B3,T1,G1..G9, tradedate, tradetime}, or None.

    The live endpoint carries ONE row; the history endpoint carries every intraday recalculation of
    a day, so a historical leg takes the LAST row (that day's closing curve), not the 09:50 one."""
    try:
        rows = mo._rows((zcyc or {}).get("params") or {})
        row = (rows[-1] if pick == "last" else rows[0]) if rows else {}
    except Exception:                                          # noqa: BLE001
        return None
    if not row:
        return None
    out = {}
    for k in ("B1", "B2", "B3", "T1"):
        v = _ff(row.get(k, row.get(k.lower())))
        if v is None:
            return None
        out[k] = v
    for i in range(1, 10):
        out["G%d" % i] = _ff(row.get("G%d" % i, row.get("g%d" % i))) or 0.0
    if not out["T1"]:
        return None
    out["tradedate"] = row.get("tradedate")
    out["tradetime"] = row.get("tradetime")
    return out


def nss_yield(p, t):
    """The zero-coupon yield at tenor `t` YEARS, in percent per annum (effective).

    G(t) is in BASIS POINTS: a Nelson-Siegel base plus nine fixed-knot Gaussian corrections. The
    final effective yield is Y = 100·(exp(G/10000) − 1) — verified against MOEX's own published
    `yearyields` (the linear reading is off by 1.17pp, the exponential by <0.04pp; see
    curve_fit_error, which is the oracle this module runs before it ever DRAWS an NSS curve)."""
    if not p or not t or t <= 0:
        return None
    a, b = nss_knots()
    try:
        # a negative T1 overflows exp(-x) here, NOT at the final conversion — unguarded it killed the
        # whole market page instead of dropping the curve
        x = t / p["T1"]
        g = p["B1"] + (p["B2"] + p["B3"]) * (1 - math.exp(-x)) / x - p["B3"] * math.exp(-x)
        for i in range(9):
            g += p["G%d" % (i + 1)] * math.exp(-((t - a[i]) ** 2) / (b[i] ** 2))
        return 100.0 * (math.exp(g / 10000.0) - 1.0)
    except (OverflowError, ZeroDivisionError, ValueError):
        return None


def parse_curve(zcyc):
    """The PUBLISHED curve points — exact, no math of ours. {periods, yields, asOf} or None."""
    rows = mo._rows((zcyc or {}).get("yearyields") or {})
    pts = [(_ff(r.get("period")), _ff(r.get("value"))) for r in rows]
    pts = [(t, y) for t, y in pts if t is not None and y is not None]
    if not pts:
        return None
    pts.sort()
    asof = None
    if rows:
        asof = " ".join(x for x in (rows[0].get("tradedate"), rows[0].get("tradetime")) if x)
    return {"periods": [t for t, _ in pts], "yields": [y for _, y in pts], "asOf": asof or None}


def curve_fit_error(zcyc):
    """Max |our NSS − MOEX's published yearyields| in percentage points, or None.

    ISS publishes the parameters and the resulting curve at the SAME timestamp, which makes our
    implementation checkable against first-party output every single run. The HISTORY endpoint gives
    params only — so this is what licenses drawing a historical curve at all. If it drifts past
    _CURVE_TOL_PP the historical legs are dropped rather than drawn wrong."""
    p, pub = curve_params(zcyc), parse_curve(zcyc)
    if not p or not pub:
        return None
    err = None
    for t, y in zip(pub["periods"], pub["yields"]):
        ours = nss_yield(p, t)
        if ours is None:
            return None
        e = abs(ours - y)
        err = e if err is None else max(err, e)
    return err


def _hist_curve(zh_entry, periods):
    """A historical curve leg from `/history/.../zcyc` params (that endpoint has NO yearyields).

    Returns None unless the payload's OWN tradedate matches the day we asked for — the endpoint
    answers an unrecognised parameter with today's curve, which would relabel today as "a year ago"."""
    if not zh_entry:
        return None
    p = curve_params(zh_entry.get("block"), pick="last")
    if not p:
        return None
    if p.get("tradedate") != zh_entry.get("want"):
        return None
    ys = [nss_yield(p, t) for t in periods]
    if any(y is None for y in ys):
        return None
    return {"date": p.get("tradedate"), "yields": ys}


# ----------------- classification: what KIND of paper is this -----------------
def classify(sec, my=None):
    """The single most important step on this page: whether the published yield MEANS anything.

    Live counter-examples this guards against: ЯНДЕКС1Р1 (Флоатер) prints EFFECTIVEYIELD -1.4007 and
    GSPREADBP -1522; СибурХ1Р03 prints a perfectly good 8.3934 — in USD, against a ₽ curve."""
    bt = (sec.get("BONDTYPE") or sec.get("BOND_TYPE") or "").strip()
    low = bt.lower()
    unit = (sec.get("FACEUNIT") or sec.get("CURRENCYID") or "").upper()
    floater = "флоатер" in low or "неизвестным купоном" in low
    structured = "структурн" in low
    linker = "линкер" in low or "индексируем" in low
    fx = bool(unit) and unit not in _RUB
    amortising = "мортиз" in low
    ytm_ok = not (floater or structured)
    return {"floater": floater, "structured": structured, "linker": linker, "fx": fx,
            "amortising": amortising,
            "defaulted": bool(_ff(sec.get("HASDEFAULT")) or _ff(sec.get("HASTECHNICALDEFAULT"))),
            "ytmMeaningful": ytm_ok,
            # a spread against the ₽ sovereign curve only means something for a ₽ fixed-coupon bond
            "curveComparable": ytm_ok and not fx and not linker}


def _no_ytm_reason(flags, bt):
    if flags["floater"]:
        return ("Купон плавающий (%s) — доходность к погашению для такой бумаги не определена: "
                "будущие купоны неизвестны. Биржа всё равно публикует число (для этого выпуска "
                "отрицательное) — мы его не показываем." % (bt or "флоатер"))
    if flags["structured"]:
        return ("Структурная облигация: выплата зависит от базового актива, денежный поток "
                "не выводится из данных биржи — доходность не считается.")
    return None


# ----------------- the schedule -----------------
def _original_face(bz, fallback):
    """The face the bond was ISSUED with.

    ISS `FACEVALUE` is the CURRENT, already-amortised nominal (live: ГТЛК 1P-01 reads 243 on a ₽1000
    issue), so walking the amortisation legs down from it double-counts every leg already paid and
    clamps the whole schedule to 0 while principal is still outstanding. bondization DOES carry the
    original on each row (`initialfacevalue`), and as a last resort the legs themselves sum to it."""
    for blk in ("coupons", "amortizations"):
        for r in mo._rows((bz or {}).get(blk) or {}):
            v = _ff(r.get("initialfacevalue"))
            if v:
                return v
    legs = [_ff(r.get("value")) for r in mo._rows((bz or {}).get("amortizations") or {})]
    tot = sum(v for v in legs if v is not None)
    if tot:
        return tot
    return fallback


def _schedule(bz, face0, today):
    """coupons + amortisation + maturity, one sorted list, with the OUTSTANDING face computed.

    ISS keeps `coupons.facevalue` at the ORIGINAL nominal on an amortising issue while the coupon
    VALUE falls (ГТЛК 2P-13: 13.11 → 10.49 → 7.87 → 5.24). Reading that column would report a face
    of 1000 on a bond that has already repaid 60% of it, so the face is walked from the
    amortisation legs instead."""
    if not bz:
        return [], False
    items = []
    for r in mo._rows(bz.get("coupons") or {}):
        d0 = _date(r.get("coupondate"))
        if not d0:
            continue
        items.append({"date": d0, "kind": "coupon", "value": _ff(r.get("value")),
                      "percent": _ff(r.get("valueprc")), "faceAfter": None, "ord": 0})
    amortising = False
    for r in mo._rows(bz.get("amortizations") or {}):
        d0 = _date(r.get("amortdate"))
        if not d0:
            continue
        src = (r.get("data_source") or "").lower()
        kind = "maturity" if src == "maturity" else "amortization"
        if kind == "amortization":
            amortising = True
        val = _ff(r.get("value"))
        if val is None:
            fv, prc = _ff(r.get("facevalue")), _ff(r.get("valueprc"))
            val = (fv * prc / 100.0) if (fv is not None and prc is not None) else None
        items.append({"date": d0, "kind": kind, "value": val, "percent": _ff(r.get("valueprc")),
                      "faceAfter": None, "ord": 1 if kind == "amortization" else 2})
    if not items:
        return [], amortising
    items.sort(key=lambda s: (s["date"], s["ord"]))
    face = face0
    for s in items:
        if s["kind"] in ("amortization", "maturity") and face is not None and s["value"] is not None:
            face = max(0.0, round(face - s["value"], 6))
        s["faceAfter"] = face
        s["past"] = s["date"] < today.isoformat()
        s.pop("ord", None)
    return items, amortising


def _coupon(sec, settle=None):
    """The coupon, with ISS's "not set yet" distinguished from a genuine zero.

    A floater between resets reports COUPONVALUE = 0 with COUPONPERCENT = None (ЯНДЕКС1Р1, live):
    the next rate simply isn't fixed. Printing that as "₽0.00" invents a zero coupon. A REAL
    zero-coupon (дисконтная) issue reports 0 in BOTH — which is a fact and is kept."""
    pct, val = _ff(sec.get("COUPONPERCENT")), _ff(sec.get("COUPONVALUE"))
    unset = pct is None and (val is None or val == 0)
    return {"percent": pct, "value": None if unset else val, "unset": unset,
            "next": _date(sec.get("NEXTCOUPON")), "periodDays": _ff(sec.get("COUPONPERIOD")),
            # ACCRUEDINT is in the SETTLEMENT currency while `value` is in the FACE currency —
            # they are NOT the same unit on a currency-denominated, rouble-settled issue
            "accrued": _ff(sec.get("ACCRUEDINT")), "accruedCurrency": settle}


def _dur_str(dur):
    """Years past a year, days below it — '0.00 г.' for a bond one day from its offer is not a number."""
    if not dur:
        return "n/a"
    if dur["days"] < 365:
        return "%d дн." % round(dur["days"])
    return "%.2f г." % dur["years"]


def _offers(bz, today):
    out = []
    for r in mo._rows((bz or {}).get("offers") or {}):
        d0 = _date(r.get("offerdate"))
        if not d0:
            continue
        out.append({"date": d0, "price": _ff(r.get("price")), "type": (r.get("offertype") or "").strip(),
                    "from": _date(r.get("offerdatestart")), "till": _date(r.get("offerdateend"))})
    out.sort(key=lambda x: x["date"])
    return out


# ----------------- the record -----------------
def _rusfar_series(raw):
    out = []
    for r in mo._rows(((raw or {}).get("rusfar") or {}).get("history") or {}):
        d0, c = _date(r.get("TRADEDATE")), _ff(r.get("CLOSE"))
        if d0 and c:
            out.append({"date": d0, "close": c})
    out.sort(key=lambda x: x["date"])
    return out


def build(raw, today=None):
    today = today or dt.date.today()
    bond = (raw or {}).get("bond") or {}
    # the analytics block names the board the yields/spreads belong to — align every other block to it
    my = _first(bond.get("marketdata_yields"))
    board = my.get("BOARDID")
    sec, md = _pick(bond.get("securities"), board), _pick(bond.get("marketdata"), board)
    if not board:
        board = sec.get("BOARDID")
    secid = sec.get("SECID") or my.get("SECID") or (raw or {}).get("secid")
    if not sec or not secid:
        return {"unavailable": True, "secid": (raw or {}).get("secid"),
                "note": "ISS не отдала данные по этой бумаге (проверьте SECID/ISIN)."}
    flags = classify(sec, my)
    bt = (sec.get("BONDTYPE") or sec.get("BOND_TYPE") or "").strip()
    unit = (sec.get("FACEUNIT") or sec.get("CURRENCYID") or "").upper()
    settle = (sec.get("CURRENCYID") or unit or "").upper()

    ytm = None
    if flags["ytmMeaningful"]:
        v, known_basis = _ff(my.get("EFFECTIVEYIELD")), bool(my.get("YIELDDATETYPE"))
        if v is None:
            v, known_basis = _ff(md.get("YIELD")), False   # the fallback field carries NO basis
        if v is not None:
            if known_basis:
                to_type = (my.get("YIELDDATETYPE") or "").strip().upper()
                to_date = _date(my.get("YIELDDATE")) or _date(sec.get("MATDATE"))
                base = "к оферте" if to_type.startswith("OFFER") else "к погашению"
                to_type = "OFFER" if to_type.startswith("OFFER") else to_type
            else:
                # 697 of 3020 TQCB issues have no analytics row; `marketdata.YIELD` alone does not say
                # what it was computed TO, and defaulting it to "к погашению" is the very mislabel the
                # module exists to prevent — say the basis is unknown instead of guessing it
                to_type, to_date, base = None, None, "база расчёта не указана биржей"
            lab = base + ("" if unit in _RUB else " (%s)" % unit)
            ytm = {"value": v, "toType": to_type, "toDate": to_date, "label": lab, "basis": base,
                   "basisKnown": known_basis}

    dur_d = _ff(my.get("DURATION")) or _ff(md.get("DURATION"))
    duration = {"days": dur_d, "years": round(dur_d / 365.0, 3)} if dur_d else None

    # A spread is a CREDIT premium only while there is a horizon to earn it over. ФСК РС-22 sits one
    # day from its offer and ISS reports G = Z = -4295bp: arithmetically true (a 0.02% price gap
    # annualised over a day), meaningless as "premium over OFZ". Below _MIN_SPREAD_DAYS we drop it.
    spreads, spread_note = None, None
    if flags["curveComparable"]:
        g, z = _ff(my.get("GSPREADBP")), _ff(my.get("ZSPREADBP"))
        if g is not None or z is not None:
            if dur_d is not None and dur_d < _MIN_SPREAD_DAYS:
                spread_note = ("Спред к кривой не показан: до ближайшей даты расчёта доходности "
                               "осталось ~%d дн., на таком горизонте спред — арифметический шум "
                               "(биржа публикует %s б.п.), а не премия за риск."
                               % (round(dur_d), ("%+d" % round(g)) if g is not None else "?"))
            else:
                spreads = {"gBp": g, "zBp": z}

    face_now = _ff(sec.get("FACEVALUE"))          # the CURRENT nominal (already amortised)
    face0 = _original_face((raw or {}).get("bondization"), face_now)   # what it was ISSUED with
    schedule, am_sched = _schedule((raw or {}).get("bondization"), face0, today)
    offers_all = _offers((raw or {}).get("bondization"), today)
    nxt = [o for o in offers_all if o["date"] >= today.isoformat()]
    offer = nxt[0] if nxt else None
    if offer is None:
        bb = _date(sec.get("BUYBACKDATE"))                      # '0000-00-00' is rejected by _date
        if bb and bb >= today.isoformat():
            offer = {"date": bb, "price": None, "type": "Оферта", "from": None, "till": None}
    flags["amortising"] = flags["amortising"] or am_sched

    mat = _date(sec.get("MATDATE"))
    if schedule:
        last = schedule[-1]["date"]
        if not mat or last > mat:
            mat = last
    price = _ff(md.get("LAST")) or _ff(md.get("LCLOSEPRICE")) or _ff(sec.get("PREVLEGALCLOSEPRICE"))
    curve = parse_curve((raw or {}).get("zcyc"))

    by_date = {}
    for r in mo._rows(((raw or {}).get("history") or {}).get("history") or {}):
        d0 = _date(r.get("TRADEDATE"))
        if not d0:
            continue
        rb = r.get("BOARDID")
        if rb and board and rb != board:
            continue                                  # an off-market board (PACT auctions)
        _dur_d = _ff(r.get("DURATION"))
        row = {"date": d0, "close": _ff(r.get("CLOSE")), "yield": _ff(r.get("YIELDCLOSE")),
               "value": _ff(r.get("VALUE")),
               # the duration THAT DAY: a year ago this bond was a year longer, so pricing the
               # sovereign leg at today's duration mis-states the premium at every past point
               "durationYears": (_dur_d / 365.0) if _dur_d else None}
        prev = by_date.get(d0)
        # one date, several prints -> keep the one with a real close (a None close is not a day)
        if prev is None or (prev["close"] is None and row["close"] is not None):
            by_date[d0] = row
    hist = [r for r in by_date.values() if r["close"] is not None]
    hist.sort(key=lambda x: x["date"])

    _brd_rows = None
    _brd_src = (raw or {}).get("board") or ((raw or {}).get("market") or {}).get("tqcb")
    if _brd_src:
        try:
            _brd_rows = {r["secid"]: r for r in _board_rows(_brd_src)}
        except Exception:                                      # noqa: BLE001
            _brd_rows = None
    _rec_market = None
    if (raw or {}).get("market"):
        try:
            _rec_market = build_market((raw or {}).get("market"), today)
        except Exception:                                      # noqa: BLE001
            _rec_market = None
    return {
        "unavailable": False,
        "market": _rec_market, "_boardRows": _brd_rows,
        "secid": secid, "isin": sec.get("ISIN"), "shortName": sec.get("SHORTNAME") or "",
        "name": sec.get("SECNAME") or sec.get("NAME") or "", "board": sec.get("BOARDID"),
        "bondType": bt, "bondSubType": (sec.get("BONDSUBTYPE") or "").strip(),
        "currency": unit or None, "face": face_now, "faceOriginal": face0, "faceUnit": unit,
        "settleCurrency": settle,
        "price": price, "wapPrice": _ff(md.get("WAPRICE")), "changePct": _ff(md.get("LASTCHANGEPRCNT")),
        "ytm": ytm, "noYtmReason": None if flags["ytmMeaningful"] else _no_ytm_reason(flags, bt),
        "spreadNote": spread_note,
        "duration": duration, "spreads": spreads,
        "coupon": _coupon(sec, settle),
        "maturity": mat, "daysToMaturity": (_d(mat) - today).days if mat else None,
        # every "через N дн." on the page counts from the SAME `today` — mixing in the ISS session
        # timestamp made the yield card say 5409 while the maturity card said 5408
        "daysToYield": ((_d(ytm["toDate"]) - today).days if (ytm and ytm.get("toDate")) else None),
        "offer": offer, "offersAll": offers_all, "schedule": schedule,
        "listLevel": _ff(sec.get("LISTLEVEL")),
        "issueSize": _ff(sec.get("ISSUESIZE")), "placed": _ff(sec.get("ISSUESIZEPLACED")),
        "liquidity": {"valToday": _ff(md.get("VALTODAY")), "numTrades": _ff(md.get("NUMTRADES"))},
        "flags": flags, "curve": curve, "curveFitPp": curve_fit_error((raw or {}).get("zcyc")),
        "history": hist, "rusfar": _rusfar_series(raw),
        "issuer": (raw or {}).get("issuer"), "curves": (raw or {}).get("curves"),
        "asOf": md.get("SYSTIME") or my.get("SYSTIME") or sec.get("PREVDATE"),
        "zcycMoment": my.get("ZCYCMOMENT"),
        "units": {"price": "% от номинала", "yield": "% годовых", "spread": "б.п.",
                  "face": _cur(unit), "duration": "дней (ISS) / лет (наше деление на 365)"},
    }


# ----------------- ANALYTICS: what ISS does NOT compute -----------------
# Design of record: docs/moex-bonds-analytics-plan.md. v1.150 only re-presented published fields;
# everything here is DERIVED, and every derived number is gated on a check it can fail.
_RISK_TOL_PCT = 0.5      # |PV - dirty price| / dirty, in percent — above this we publish nothing
_SCEN_BP = (-300, -200, -100, 0, 100, 200, 300)
_HIST_SPAN_D = 3660     # ~10 years back: ISS simply returns what it has (from the issue date on)
_RUSFAR_STALE_D = 10     # past this the base-rate line is invention, not a carry-forward
_THROUGH_CURVE_BP = -100  # a ₽ corporate below the sovereign by more than this is a broken print
_MIN_FILTERED = 10        # below this many survivors a turnover filter is picking, not filtering
_BIG_BOARD = 20           # ...but only on a board large enough for a population to exist
_SEARCH_LIMIT = 100       # the issuer search is capped here and NOT paged — disclosed when hit
_CURVE_MISS_STOP = 3      # consecutive ZCYC slice misses before giving up (no multi-minute wedge)
_CURVE_STEP_D = 91        # QUARTERLY: a slice costs ~1 s whatever its era, so 12 quarterly slices
#                           cover three years (11.4 s measured) where 12 monthly ones covered one
#                           (11.8 s) — three times the span for the same price. NB ISS advertises
#                           the archive from 2014-01-06 but 2016 and 2018 come back EMPTY; the real
#                           data starts around 2020, so depth is checked by fact, not by the claim.
_THROUGH_UP_BP = 3000     # a ₽ issue this far ABOVE the sovereign curve is a broken print too


def cash_flows(d, today=None):
    """Future cash flows as [(years_from_today, amount, kind)].

    The KIND matters: an amortisation leg and the redemption are RETURN OF CAPITAL, not income.
    Summing them as coupons made a bond maturing inside a year print «полная доходность +108.9%»
    on an unchanged curve (live Ростел1P1R) — 450 TQCB issues mature within 12 months.

    Amounts come from the bondization schedule, so an amortising issue's declining coupons and its
    principal legs are already correct (the schedule walks the outstanding face)."""
    today = today or dt.date.today()
    out = []
    for s in d.get("schedule") or []:
        if s.get("value") is None:
            continue
        try:
            sd = _d(s["date"])
        except (ValueError, TypeError):
            continue
        if sd <= today:
            continue
        out.append(((sd - today).days / 365.0, s["value"], s["kind"]))
    return out


def risk(d, today=None):
    """Modified duration, CONVEXITY and the price the model implies — or None.

    ISS publishes a duration but no convexity, so this is the first genuinely computed thing in the
    module — which means it needs an oracle. Ours is the price itself: discounting the schedule at
    the exchange's own yield must reproduce the market DIRTY price. Live 2026-07-25: ОФЗ 26238
    PV 545.78 vs dirty 546.21 (0.08%), РусГид2Р07 994.40 vs 995.67 (0.13%) — but ФСК РС-22, whose
    yield is to an OFFER four days out, gives 1529.84 against a 1001.57 dirty price, because its
    cash flows END at the offer, not at the 2027 maturity.

    That failure is the POINT: gating on the oracle refuses yield-to-offer issues, floaters,
    structured notes, linkers and any mis-parsed schedule without a single special case."""
    y = ((d.get("ytm") or {}).get("value") or 0) / 100.0
    if not d.get("ytm") or not (d["ytm"].get("basisKnown", True)) or y <= -0.99:
        return None
    if not d["flags"]["ytmMeaningful"] or d["flags"]["linker"]:
        return None
    # A yield quoted to an OFFER is refused EXPLICITLY, not left to the price oracle: at par with a
    # level coupon, yield-to-offer EQUALS yield-to-maturity, so the maturity-flow PV reconciles and
    # the gate would happily publish a duration to a maturity the market is not pricing.
    if (d["ytm"] or {}).get("toType") == "OFFER":
        return None
    cfs = cash_flows(d, today)
    face = d.get("faceOriginal") or d.get("face")
    if len(cfs) < 2 or not face or d.get("price") is None:
        return None
    try:
        pv = sum(cf / (1 + y) ** t for t, cf, _k in cfs)
        mac = sum(t * cf / (1 + y) ** t for t, cf, _k in cfs) / pv
        conv = sum(t * (t + 1) * cf / (1 + y) ** (t + 2) for t, cf, _k in cfs) / pv
    except (ZeroDivisionError, OverflowError, ValueError):
        return None
    if pv <= 0:
        return None
    # the price is quoted as a % of the CURRENT face; accrued rides in the settlement currency but on
    # a rouble-settled rouble bond they coincide — a split-currency issue is excluded by the tolerance
    dirty = d["price"] * (d.get("face") or face) / 100.0 + (d["coupon"].get("accrued") or 0.0)
    fit = abs(pv - dirty) / dirty * 100.0 if dirty else None
    return {"pv": pv, "dirty": dirty, "fitPct": fit, "macaulay": mac, "modified": mac / (1 + y),
            "convexity": conv, "nFlows": len(cfs), "y": y, "face": face,
            "ok": fit is not None and fit <= _RISK_TOL_PCT}


def _reprice(cfs, y):
    # a discount base <= 0 does NOT raise in Python — (-0.5) ** 0.5 returns a COMPLEX number, which
    # would sail into the spec and blow up json.dump. Refuse it explicitly.
    if 1 + y <= 0:
        return None
    try:
        v = sum(cf / (1 + y) ** t for t, cf, _k in cfs)
    except (ZeroDivisionError, OverflowError, ValueError):
        return None
    return v if isinstance(v, float) and v == v and abs(v) != float("inf") else None


def scenarios(d, r, today=None):
    """What a parallel yield shift does to price and to a 1-year total return. Arithmetic on a
    STATED shift — not a forecast, and the panel says so."""
    if not r or not r.get("ok"):
        return []
    cfs = cash_flows(d, today)
    face = d.get("face") or r["face"]
    accrued = d["coupon"].get("accrued") or 0.0
    base = _reprice(cfs, r["y"])
    if not base:
        return []
    # COUPONS ONLY — принципал внутри года это возврат капитала, а не доход
    coupon_1y = sum(cf for t, cf, k in cfs if t <= 1.0 and k == "coupon")
    out = []
    for bp in _SCEN_BP:
        pv = _reprice(cfs, r["y"] + bp / 10000.0)
        if pv is None:
            continue
        clean = (pv - accrued) / face * 100.0
        base_clean = (base - accrued) / face * 100.0
        dp = (clean - base_clean) / base_clean * 100.0 if base_clean else None
        out.append({"bp": bp, "price": clean, "pricePct": dp,
                    "totalPct": (dp + coupon_1y / face * 100.0 / (base_clean / 100.0))
                                if dp is not None else None})
    return out


def coupon_rates(d):
    """A FLOATER's coupon rate, recovered from the ₽ payments.

    ISS sends `valueprc = None` for a floater, which reads as "no rate history" — but `value` is
    there, and the rate is value / face_outstanding × 365/period. Live ЯНДЕКС1Р1: 14.55 ₽ → 13.18 ₽
    monthly = 17.70% → 16.04% annualised, tracking the money market down. This is the floater's whole
    story and it was sitting in data v1.150 already pulled."""
    sch = [s for s in (d.get("schedule") or []) if s["kind"] == "coupon" and s.get("value")]
    out, prev = [], None
    for s in sch:
        try:
            cur = _d(s["date"])
        except (ValueError, TypeError):
            continue
        per = (cur - prev).days if prev else (d["coupon"].get("periodDays") or 0)
        prev = cur
        face = s.get("faceAfter") or d.get("faceOriginal") or d.get("face")
        if not per or per <= 0 or not face:
            continue
        out.append({"date": s["date"], "pct": s["value"] / face * 365.0 / per * 100.0,
                    "value": s["value"], "face": face, "past": s.get("past")})
    return out


def yield_stats(d, min_points=20, window_days=365):
    """Where today's yield sits inside its OWN window — the IV-rank analogue the options path uses.
    A yield at the 10th percentile of its year is expensive paper whatever the spread says.

    Gated on the SAME rule as the yield itself: if we refuse to publish a bond's yield we cannot
    rank it either. ISS does put a YIELDCLOSE series on a floater (ЯНДЕКС1Р1: 14.56–23.87, tracking
    the coupon rather than any yield-to-maturity), but we cannot say what that quantity IS — and
    a percentile of an unnamed series is a number pretending to be a measurement. The floater's
    coupon-rate panel already carries the equivalent context."""
    if (d.get("flags") or {}).get("ytmMeaningful") is False:
        return None
    ytm = d.get("ytm") or {}
    # A bond quoted to an OFFER has ytmMeaningful=True, so the flag alone let ФСК's -28.89%
    # annualisation artefact be RANKED: «Перцентиль 0 · -28.89% сейчас», which the card's own hint
    # then reads as "expensive against itself". 420 TQCB issues are quoted to an offer today. The
    # window also mixes to-offer and to-maturity quantities, so it cannot be ranked at all.
    if ytm.get("toType") != "MATDATE" or not ytm.get("basisKnown", True):
        return None
    hist = [x for x in (d.get("history") or []) if x.get("yield") is not None]
    if hist and window_days:
        # a percentile over a window that spans a RATE-REGIME change describes the regime, not the
        # bond: ОФЗ 26238's own life runs from ~7% rates in 2021 to ~15% in 2026
        try:
            cutoff = (_d(hist[-1]["date"]) - dt.timedelta(days=window_days)).isoformat()
            hist = [x for x in hist if x["date"] >= cutoff]
        except (ValueError, TypeError):
            pass
    ys = [x["yield"] for x in hist]
    if len(ys) < min_points:
        return None
    cur = ys[-1]
    lo, hi = min(ys), max(ys)
    srt = sorted(ys)
    mid = len(srt) // 2
    med = srt[mid] if len(srt) % 2 else (srt[mid - 1] + srt[mid]) / 2.0
    # «сейчас» must be the yield the page itself prints; a stale/truncated history would otherwise
    # publish a percentile against a number contradicting the bond's own yield card
    if ytm.get("value") is not None and abs(cur - ytm["value"]) > 1.0:
        return None
    pct = 100.0 * sum(1 for v in ys if v <= cur) / len(ys) if hi > lo else 50.0
    if hi > lo and approxeq(cur, hi):
        pct = 100.0
    elif hi > lo and approxeq(cur, lo):
        pct = 0.0
    return {"pct": pct, "min": lo, "max": hi, "med": med, "cur": cur, "n": len(ys),
            "windowDays": window_days}


def approxeq(a, b, tol=1e-12):
    return abs(a - b) <= tol


def breakevens(m, min_turnover=None):
    _thin = not (m.get("sessionOfz") or {}).get("usable", True)
    """Market-implied inflation: the NOMINAL curve at a linker's own duration minus its REAL yield.

    Live 2026-07-25: 5.37% at 1.5y rising to 8.57% at 6.2y. Zero extra calls — the linkers ride the
    TQOB pull already made. This is the ONE place a linker's yield meets the nominal curve, and it
    meets it as a DIFFERENCE; a linker is still never plotted as a point on the nominal curve."""
    cv = m.get("curve")
    if not cv:
        return []
    out = []
    # NO turnover floor here, deliberately, and it is a calibration decision, not an oversight:
    # ОФЗ-ИН are STRUCTURALLY thin. Measured live mid-session: ОФЗ 52005 ₽1 980 on 2 trades and the
    # other three had not traded at all that day, while ordinary ОФЗ ran to billions. The ₽1M floor
    # the duration×yield map uses would delete this panel permanently rather than occasionally.
    # The risk a floor was meant to address (a stale print moving a headline) is instead handled by
    # DISCLOSING each linker's own liquidity beside its number, so a 2-trade print is visible as one.
    for r in m.get("ofz") or []:
        if not r.get("linker") or r.get("currency") not in _RUB:
            continue
        t, real = r.get("durationYears"), r.get("realYield")
        if not t or real is None:
            continue
        nom = _curve_at(cv, t)
        if nom is None:
            continue
        out.append({"secid": r["secid"], "name": r["name"], "duration": t,
                    "nominal": nom, "real": real, "breakeven": nom - real,
                    "valToday": r.get("valToday"), "numTrades": r.get("numTrades")})
    out.sort(key=lambda x: x["duration"])
    return out


def _curve_fit_ok(d):
    """Did our NSS implementation reproduce the exchange's OWN published curve today?"""
    err = d.get("curveFitPp")
    if err is None:
        err = curve_fit_error(d.get("_zcyc") or {})
    return err is not None and err <= _CURVE_TOL_PP


def derive(d, today=None, curves=None):
    """Attach the derived analytics to the record.

    They used to be computed on a LOCAL copy inside to_spec(), so `--json` and render_text — the
    default output path and the documented sourcing path for the written read — carried none of
    them, while references/moex.md instructs the agent to quote convexity and the percentile."""
    if not d or d.get("unavailable"):
        return d
    r = risk(d, today)
    out = dict(d)
    out["risk"] = r
    out["scenarios"] = scenarios(d, r, today)
    out["yieldStats"] = yield_stats(d)                       # the headline: one year
    out["yieldStats3y"] = yield_stats(d, window_days=365 * 3)  # the cycle, where history reaches
    cr = coupon_rates(d) if d["flags"]["floater"] else []
    out["couponRates"] = cr
    _fit = curve_fit_error({"params": {"columns": [], "data": []}, "yearyields": {}}) \
        if False else _curve_fit_ok(d)
    out["gspreadHistory"] = gspread_history(d, curves or d.get("curves") or {}, fit_ok=_fit)
    _brd = d.get("_boardRows")
    if d.get("issuer") and _brd:
        out["issuerCurve"] = issuer_curve(d, _brd, set(d["issuer"].get("secids") or ()))
    if cr:
        last = cr[-1]
        base = None
        for x in (d.get("rusfar") or []):
            if x["date"] <= last["date"]:
                base = x
        out["frate"] = {"pct": last["pct"], "date": last["date"],
                        "base": (base or {}).get("close"), "baseDate": (base or {}).get("date"),
                        "spread": (last["pct"] - base["close"]) if base else None}
    # the aliases the card builders read
    out["_risk"], out["_scen"] = out["risk"], out["scenarios"]
    out["_ystats"], out["_frate"] = out["yieldStats"], out.get("frate")
    return out


# ----------------- the market record -----------------
def _board_rows(block):
    b = block or {}
    sec = {r.get("SECID"): r for r in mo._rows(b.get("securities") or {})}
    md = {r.get("SECID"): r for r in mo._rows(b.get("marketdata") or {})}
    my = {r.get("SECID"): r for r in mo._rows(b.get("marketdata_yields") or {})}
    out = []
    for sid, s in sec.items():
        m, y = md.get(sid, {}), my.get(sid, {})
        fl = classify(s, y)
        dur = _ff(y.get("DURATION")) or _ff(m.get("DURATION"))
        ytm = _ff(y.get("EFFECTIVEYIELD")) if fl["ytmMeaningful"] else None
        out.append({
            "secid": sid, "name": s.get("SHORTNAME") or "", "maturity": _date(s.get("MATDATE")),
            "currency": (s.get("FACEUNIT") or s.get("CURRENCYID") or "").upper(),
            "bondType": (s.get("BONDTYPE") or "").strip(),
            "couponPct": _ff(s.get("COUPONPERCENT")),
            "ytm": ytm, "ytmTo": (y.get("YIELDDATETYPE") or "").upper() or None,
            "durationYears": round(dur / 365.0, 3) if dur else None,
            # the SAME horizon gate the issue page applies: under ~30 days a spread is
            # annualisation noise, and the two surfaces must not disagree about that
            "gSpreadBp": (_ff(y.get("GSPREADBP"))
                          if (fl["curveComparable"] and dur and dur >= _MIN_SPREAD_DAYS) else None),
            "valToday": _ff(m.get("VALTODAY")), "numTrades": _ff(m.get("NUMTRADES")),
            "settleCurrency": (s.get("CURRENCYID") or "").upper(),
            "listLevel": _ff(s.get("LISTLEVEL")),
            "floater": fl["floater"], "fx": fl["fx"], "curveComparable": fl["curveComparable"],
            # a linker's published yield is a REAL yield — kept under its own name so it can never be
            # confused with a nominal one, and used only by the breakeven difference
            "linker": fl["linker"], "realYield": (ytm if fl["linker"] else None),
        })
    out.sort(key=lambda r: -(r["valToday"] or 0))
    return out


def session_liquidity(rows, floor):
    """Can today's turnover discriminate against `floor` at all?

    Every turnover filter here measures VALTODAY, which ACCUMULATES through the session, so early in
    the day it is small for everything and before the session it is zero. Measured live at 10:16 MSK:
    **0 of 3020** TQCB issues cleared the ₽10M floor and 1 of 62 ОФЗ cleared ₽1M — the page's content
    depended on the hour and read as "there is no market".

    The test is therefore whether ANYTHING clears the floor, not how many instruments traded (at
    10:16 68% of ОФЗ had trades — just tiny ones). When nothing does, the floor cannot separate a
    stale print from the market, so the filter is dropped and the page SAYS so: the yields shown are
    then the exchange's own last-session analytics, which is a fact, not a guess."""
    n = len(rows or [])
    top = max([(r.get("valToday") or 0) for r in (rows or [])] or [0])
    traded = sum(1 for r in (rows or []) if (r.get("valToday") or 0) > 0)
    clears = sum(1 for r in (rows or []) if (r.get("valToday") or 0) >= floor)
    # ONE instrument clearing the floor is not a usable filter either — live at 11:11 exactly one of
    # 3020 corporates had cleared ₽10M and the panel proudly read «Выпусков в выборке 1 из 3020».
    # A filter has to leave a POPULATION behind, or it is not filtering, it is picking.
    # Two DIFFERENT states, and conflating them was the bug: with NOTHING clearing the floor the
    # filter cannot discriminate at all (drop it, say so); with a handful clearing it still keeps
    # stale prints out — dropping it then would hand the map back exactly the ₽38k prints the floor
    # exists for. So the floor stays whenever anything clears; only the SAMPLE is disclosed as thin.
    need = _MIN_FILTERED if n >= _BIG_BOARD else 1
    return {"n": n, "traded": traded, "top": top, "clears": clears, "need": need,
            "usable": n > 0 and clears >= 1, "thin": 0 < clears < need}


def build_market(raw, today=None, min_turnover=MIN_TURNOVER):
    today = today or dt.date.today()
    ofz = _board_rows((raw or {}).get("tqob"))
    corp_all = _board_rows((raw or {}).get("tqcb"))
    liq_c = session_liquidity(corp_all, min_turnover)
    liq_o = session_liquidity(ofz, MIN_SCATTER_TURNOVER)
    corp = ([r for r in corp_all if (r["valToday"] or 0) >= min_turnover] if liq_c["usable"]
            else corp_all[:60])          # already sorted by turnover desc
    # the ₽ scatter takes ₽ fixed-coupon paper ONLY: a CNY ОФЗ's yield is a CNY yield
    scatter = [r for r in ofz if r["currency"] in _RUB and r["curveComparable"]
               and r["ytm"] is not None and r["durationYears"]
               and ((r["valToday"] or 0) >= MIN_SCATTER_TURNOVER or not liq_o["usable"])]
    curve = parse_curve((raw or {}).get("zcyc"))
    fit = curve_fit_error((raw or {}).get("zcyc"))
    hist_legs = []
    if curve and fit is not None and fit <= _CURVE_TOL_PP:
        for lab, blk in sorted(((raw or {}).get("zcycHist") or {}).items()):
            leg = _hist_curve(blk, curve["periods"])
            if leg:
                hist_legs.append(dict(leg, label=lab))
    rgbi = []
    for r in mo._rows(((raw or {}).get("rgbi") or {}).get("history") or {}):
        d0, c = _date(r.get("TRADEDATE")), _ff(r.get("CLOSE"))
        if d0 and c:
            rgbi.append({"date": d0, "close": c})
    # the SESSION the data is from, not the day we happened to run: a weekend render dated Friday's
    # board snapshot as today
    asof = (curve or {}).get("asOf") or (rgbi[-1]["date"] if rgbi else None) or today.isoformat()
    return {"asOf": asof, "runDate": today.isoformat(), "curve": curve, "curveFitPp": fit,
            "curveHist": hist_legs,
            "ofz": ofz, "corp": corp, "corpTotal": len(corp_all), "scatter": scatter,
            "minTurnover": min_turnover, "minScatterTurnover": MIN_SCATTER_TURNOVER, "rgbi": rgbi,
            "sessionCorp": liq_c, "sessionOfz": liq_o}


def _curve_at(curve, t):
    """Linear interpolation on the PUBLISHED points (used for a card, never for a drawn curve)."""
    if not curve or t is None:
        return None
    ps, ys = curve["periods"], curve["yields"]
    if t <= ps[0] or t >= ps[-1]:
        return ys[0] if t <= ps[0] else ys[-1]
    for i in range(1, len(ps)):
        if t <= ps[i]:
            w = (t - ps[i - 1]) / (ps[i] - ps[i - 1])
            return ys[i - 1] + w * (ys[i] - ys[i - 1])
    return None


# ----------------- the ISSUE spec -----------------
_TYPE_SHORT = [("флоатер", "флоатер"), ("валютн", "валютная"), ("структурн", "структурная"),
               ("мортиз", "с амортизацией"), ("линкер", "линкер"), ("индексируем", "линкер"),
               ("неизвестным купоном", "купон неизвестен"), ("дисконтн", "дисконтная"),
               ("конвертируем", "конвертируемая"), ("фикс", "фикс. купон")]


def _type_short(bt):
    """A table-width label. Truncating the ISS string mid-word gave "Фикс с известным купон"."""
    low = (bt or "").lower()
    for key, lab in _TYPE_SHORT:
        if key in low:
            return lab
    return (bt or "")[:16]


def _kind_label(d):
    f = d["flags"]
    bits = []
    if (d.get("secid") or "").startswith("SU") or "ОФЗ" in (d.get("shortName") or ""):
        bits.append("ОФЗ")
    else:
        bits.append("Корпоративная")
    if f["floater"]:
        bits.append("флоатер")
    if f["fx"]:
        bits.append("валютная (%s)" % d["currency"])
    if f["amortising"]:
        bits.append("с амортизацией")
    if f["structured"]:
        bits.append("структурная")
    return " · ".join(bits)


def _issue_cards(d):
    c = []
    f, cp = d["flags"], d["coupon"]
    if d["ytm"]:
        y = d["ytm"]
        if not y.get("basisKnown"):
            c.append({"label": "Доходность", "value": _num(y["value"], 2) + "%",
                      "sub": "база расчёта не указана биржей",
                      "hint": "Биржа отдала доходность без поля YIELDDATETYPE, то есть не сказала, к "
                              "какой дате она посчитана. У выпуска с офертой это разные числа, "
                              "поэтому подписать её «к погашению» здесь было бы догадкой."
                              + (" У этого выпуска есть оферта %s — вероятно, доходность к ней."
                                 % d["offer"]["date"] if d.get("offer") else "")})
            y = None
    if d["ytm"] and y:
        sub = y["basis"]
        if y.get("toDate"):
            sub += " " + y["toDate"]
            if d.get("daysToYield") is not None:
                sub += " · через %d дн." % d["daysToYield"]
        c.append({"label": "Доходность " + y["label"], "value": _num(y["value"], 2) + "%", "sub": sub,
                  "hint": "Эффективная доходность с биржи (ISS EFFECTIVEYIELD), %% годовых, посчитанная "
                          "К ДАТЕ %s. Это не «доходность вообще»: у выпуска с офертой она считается к "
                          "оферте, и после неё условия могут смениться." % (y.get("toDate") or "—")})
    elif d.get("noYtmReason"):
        c.append({"label": "Доходность не считается", "value": "—", "sub": "см. пояснение",
                  "hint": d["noYtmReason"]})
    if d["duration"]:
        c.append({"label": "Дюрация", "value": _dur_str(d["duration"]),
                  "sub": "%d дн. (ISS)" % round(d["duration"]["days"]),
                  "hint": "Дюрация с биржи, переведённая в годы делением на 365. Грубо: на столько "
                          "процентов изменится цена при сдвиге доходности на 1 п.п."})
    if d.get("spreadNote"):
        c.append({"label": "Спред к ОФЗ", "value": "—", "sub": "не показан",
                  "hint": d["spreadNote"]})
    if d["spreads"]:
        if d["spreads"].get("gBp") is not None:
            c.append({"label": "G-spread", "value": "%+d б.п." % round(d["spreads"]["gBp"]),
                      "hint": "Премия к кривой ОФЗ (КБД) на сопоставимом сроке, базисных пунктов. "
                              "Положительный = платят больше государства; отрицательный = меньше."})
        if d["spreads"].get("zBp") is not None:
            c.append({"label": "Z-spread", "value": "%+d б.п." % round(d["spreads"]["zBp"]),
                      "hint": "Параллельный сдвиг всей кривой, при котором дисконтированный денежный "
                              "поток даёт рыночную цену. Устойчивее G-spread на амортизации."})
    if cp["unset"]:
        c.append({"label": "Купон", "value": "не установлен",
                  "sub": ("след. выплата " + cp["next"]) if cp["next"] else None,
                  "hint": "Ставка следующего купона ещё не зафиксирована (обычная ситуация для "
                          "флоатера между пересчётами). Биржа отдаёт по этому полю ноль — это "
                          "отсутствие данных, а не нулевой купон, поэтому здесь стоит прочерк."})
    elif cp["percent"] is not None or cp["value"] is not None:
        v = ("%.2f%%" % cp["percent"]) if cp["percent"] is not None else "n/a"
        sub = ("%s%s" % (_cur(d["faceUnit"]), _num(cp["value"], 2))) if cp["value"] is not None else None
        if f["floater"]:
            sub = (sub or "") + " · ТЕКУЩИЙ купон, ставка плавающая"
        c.append({"label": "Купон", "value": v, "sub": sub,
                  "hint": "Ставка купона годовых и его размер на одну облигацию."
                          + (" У флоатера это ТОЛЬКО текущий период — следующие купоны неизвестны."
                             if f["floater"] else "")})
    if cp["accrued"] is not None:
        c.append({"label": "НКД", "value": _cur(cp.get("accruedCurrency") or d["faceUnit"]) + _num(cp["accrued"], 2),
                  "sub": ("след. купон " + cp["next"]) if cp["next"] else None,
                  "hint": "Накопленный купонный доход — его платит покупатель сверх цены. Биржа "
                          "публикует его в валюте РАСЧЁТОВ по этой доске, которая у валютного "
                          "выпуска может отличаться от валюты номинала."})
    if d["offer"]:
        c.append({"label": "Оферта", "value": d["offer"]["date"],
                  "sub": (d["offer"].get("type") or None),
                  "hint": "Ближайшая ПРЕДСТОЯЩАЯ оферта: дата, когда бумагу можно предъявить к выкупу. "
                          "После неё эмитент вправе изменить купон — доходность к погашению "
                          "за этой датой условна."})
    if d["maturity"]:
        c.append({"label": "Погашение", "value": d["maturity"],
                  "sub": ("через %d дн." % d["daysToMaturity"]) if d["daysToMaturity"] is not None else None,
                  "hint": "Дата возврата номинала."})
    if d["price"] is not None:
        c.append({"label": "Цена", "value": _num(d["price"], 2) + "%",
                  "sub": "%s%s номинала" % (_cur(d["faceUnit"]), _num(d["face"], 0)) if d["face"] else None,
                  "hint": "Цена в процентах от номинала (без НКД) — как котируются облигации."})
    if d["listLevel"]:
        c.append({"label": "Уровень листинга", "value": str(int(d["listLevel"])),
                  "hint": "Котировальный список Мосбиржи: 1 и 2 — с требованиями к эмитенту, 3 — "
                          "некотировальная часть. Это НЕ кредитный рейтинг: рейтингов АКРА/Эксперт РА "
                          "в данных биржи нет, и мы их не выдумываем."})
    r = d.get("_risk")
    if r and r.get("ok"):
        c.append({"label": "Мод. дюрация", "value": "%.2f" % r["modified"],
                  "sub": "цена ±%.1f%% на 100 б.п." % r["modified"],
                  "hint": "Модифицированная дюрация: на столько процентов меняется цена при сдвиге "
                          "доходности на 1 п.п. Посчитана нами по графику выплат; сходимость с "
                          "дюрацией биржи и с рыночной ценой — проверка, что модель верна."})
        c.append({"label": "Выпуклость", "value": "%.1f" % r["convexity"],
                  "sub": "поправка 2-го порядка",
                  "hint": "Выпуклость — биржа её НЕ публикует. Из-за неё падение ставок приносит "
                          "больше, чем такой же их рост отнимает: чем она выше, тем выгоднее бумага "
                          "при развороте ставок вниз."})
        _s3 = [x for x in (d.get("_scen") or []) if x["bp"] == -300]
        if _s3 and _s3[0]["totalPct"] is not None:
            c.append({"label": "При −300 б.п. за год", "value": "%+.1f%%" % _s3[0]["totalPct"],
                      "sub": "полная доходность",
                      "hint": "Полная доходность за год, если доходности параллельно упадут на "
                              "3 п.п.: переоценка цены плюс полученные купоны. Арифметика сдвига, "
                              "не прогноз."})
    ys, ys3 = d.get("_ystats"), d.get("yieldStats3y")
    if ys:
        _w = "%.0f г." % (ys.get("windowDays", 365) / 365.0)
        _sub = "%s: %.2f–%.2f%%" % (_w, ys["min"], ys["max"])
        if ys3 and ys3.get("n", 0) > ys.get("n", 0):
            _sub += " · 3 г.: %.0f-й" % ys3["pct"]
        c.append({"label": "Перцентиль доходности", "value": "%.0f" % ys["pct"], "sub": _sub,
                  "hint": "Где сегодняшняя доходность (%.2f%%) стоит внутри СОБСТВЕННОЙ истории "
                          "бумаги за ОКНО %s (%d торговых дней). 100 — доходность на максимуме "
                          "окна, то есть бумага дешева относительно себя самой; 0 — наоборот. "
                          "Окно названо не зря: за более длинный период в выборку попадает другой "
                          "режим ставок, и число начинает описывать смену режима, а не бумагу.%s"
                          % (ys["cur"], _w, ys["n"],
                             (" За 3 года (%d дн.) перцентиль %.0f-й." % (ys3["n"], ys3["pct"]))
                             if ys3 and ys3.get("n", 0) > ys.get("n", 0) else
                             " Истории на три года пока не хватает.")})
    fr = d.get("_frate")
    if fr:
        c.append({"label": "Ставка последнего купона", "value": "%.2f%%" % fr["pct"],
                  "sub": (("%s · спред к RUSFAR %+.2f п.п." % (fr["date"], fr["spread"]))
                          if fr.get("spread") is not None
                          else "%s · восстановлена из выплат" % fr["date"]),
                  "hint": "Ставка ПОСЛЕДНЕГО УСТАНОВЛЕННОГО купона (%s), восстановленная из рублёвой "
                          "выплаты — биржа её не публикует. Это не ставка текущего периода: если она "
                          "ещё не зафиксирована, карточка «Купон» так и говорит.%s"
                          % (fr["date"],
                             (" Спред посчитан к RUSFAR на ту же дату (%s, %.2f%%), а не к "
                              "сегодняшнему — иначе в него попал бы дрейф базы за весь период."
                              % (fr["baseDate"], fr["base"])) if fr.get("spread") is not None else "")})
    lq = d["liquidity"]
    if lq.get("valToday"):
        c.append({"label": "Оборот сегодня", "value": _money(lq["valToday"], d.get("settleCurrency") or d["faceUnit"]),
                  "sub": ("%d сделок" % lq["numTrades"]) if lq.get("numTrades") else None,
                  "hint": "Дневной оборот по бумаге. Низкий оборот = цена может быть неактуальной."})
    return c


def _curve_panel(d):
    """The bond's own point against the sovereign curve — only for ₽ fixed-coupon paper."""
    cv = d.get("curve")
    if not cv or not d["flags"]["curveComparable"] or not d.get("duration") or not d.get("ytm"):
        return None
    # the SAME horizon gate the spread card uses: refusing to print "-4295 б.п." and then plotting
    # the identical number as a point on the curve is the same claim, just drawn (it also dragged the
    # y-axis to -30 and flattened the curve itself into a line)
    if d["duration"]["days"] < _MIN_SPREAD_DAYS:
        return None
    t = d["duration"]["years"]
    ser = [{"name": "Кривая ОФЗ (КБД)", "values": cv["yields"], "color": _BLUE}]
    pt = [None] * len(cv["periods"])
    i = min(range(len(cv["periods"])), key=lambda j: abs(cv["periods"][j] - t))
    pt[i] = d["ytm"]["value"]
    ser.append({"name": d["shortName"] or d["secid"], "values": pt, "color": _ORANGE})
    gs = (d.get("spreads") or {}).get("gBp")
    return {"type": "line", "title": "Кривая бескупонной доходности (КБД) и эта бумага",
            "units": {"left": "% годовых"},
            "info": "Синяя линия — кривая ОФЗ, опубликованная биржей (%s). Точка — эта бумага, "
                    "отложенная на БЛИЖАЙШЕМ опубликованном узле кривой (%g г.) при её собственной "
                    "дюрации %.2f г. — узлы кривой редкие, поэтому маркер стоит на узле, а не точно "
                    "на дюрации. Премия к кривой на самой дюрации: %s."
                    % (cv.get("asOf") or "сегодня", cv["periods"][i], t,
                       ("%+d б.п." % round(gs)) if gs is not None else "G-spread не опубликован"),
            # showPoints is REQUIRED here, not decoration: the bond is a series of ONE non-null value
            # among nulls, and the engine's line default is pointRadius 0 — without markers the whole
            # point of the panel renders as an empty legend entry (caught in the Preview Pane).
            "data": {"labels": ["%g" % p for p in cv["periods"]], "series": ser, "showPoints": True}}


def _schedule_panels(d):
    out = []
    sch = d.get("schedule") or []
    if not sch:
        return out
    if payments_vary(d) is False:      # strictly level; None (unknown) keeps the per-payment bars
        # equal coupons make an equal-bar chart: no dynamics to see, by construction. The CUMULATIVE
        # return has shape — and where it crosses 100% is the payback point.
        cum = cumulative_flows(d)
        if len(cum) >= 3:
            pb = payback(cum)
            cur0 = _cur(d["faceUnit"])
            out.append({"type": "line", "title": "Накопленный возврат вложенного",
                        "units": {"left": "% от уплаченной цены"},
                        "info": "Сколько денег возвращается к какой дате, в процентах от того, что "
                                "платит покупатель СЕГОДНЯ (цена + НКД = %s%s). Пересечение 100%% — "
                                "точка окупаемости%s. Крутизна кривой и есть дюрация, увиденная "
                                "глазами: чем позже она поднимается, тем дольше деньги связаны. "
                                "Купоны у этого выпуска одинаковы, поэтому столбики по каждой "
                                "выплате показали бы ровный ряд без всякой динамики."
                                % (cur0, _num(cum[0]["invested"], 2),
                                   (" — %s" % pb) if pb else " в этом графике не достигается"),
                        "data": {"labels": [x["date"] for x in cum], "showPoints": True,
                                 "series": [{"name": "возвращено, %", "color": _UP,
                                             "values": [round(x["cumPct"], 2) for x in cum]}]}})
            out.append(_schedule_table(d, sch))
            return out
    labels = [s["date"] for s in sch]
    paid = [s["value"] if s.get("past") and s["kind"] == "coupon" else None for s in sch]
    fut = [s["value"] if (not s.get("past")) and s["kind"] == "coupon" else None for s in sch]
    ser = [{"type": "bar", "name": "купон выплачен", "values": paid, "color": "#9aa4b2"},
           {"type": "bar", "name": "купон впереди", "values": fut, "color": _UP}]
    # The principal is 20-30x a coupon (₽1000 vs ₽35): charted as a bar on the SAME axis it flattens
    # the entire coupon stream into the baseline — which is the one thing this panel exists to show.
    # It rides the RIGHT axis as the outstanding-face line instead (where an amortising issue's
    # step-down is exactly what you want to see), and the payment itself is in the table below.
    face_line = [s["faceAfter"] for s in sch]
    if any(v is not None for v in face_line):
        ser.append({"type": "line", "name": "номинал (остаток)", "values": face_line, "axis": "y1"})
    cur = _cur(d["faceUnit"])
    note = (" Выплата тела (погашение/амортизация) в график не вынесена — она в 20–30 раз больше "
            "купона и сплющила бы его; она есть в таблице ниже, а линия номинала показывает остаток.")
    if d["flags"]["amortising"]:
        note += (" Номинал гасится частями, поэтому купон в %s снижается вместе с ним." % cur)
    if d["flags"]["floater"]:
        note += (" Ставка плавающая: показаны только УЖЕ ИЗВЕСТНЫЕ купоны, дальше график обрывается — "
                 "это не конец выплат.")
    out.append({"type": "combo", "title": "Купонный поток и остаток номинала",
                "units": {"left": cur + " купон", "right": cur + " номинал"},
                "info": "Выплаты по выпуску из данных биржи (bondization)." + note,
                # y1pct=False: the engine suffixes the right axis with '%' unless told otherwise —
                # the face line would have read "1000%" (caught in the Preview Pane)
                "data": {"labels": labels, "series": ser, "y1pct": False}})
    out.append(_schedule_table(d, sch))
    return out


def _schedule_table(d, sch):
    cur = _cur(d["faceUnit"])
    rows = []
    for s in sch:
        rows.append([s["date"],
                     {"coupon": "купон", "amortization": "амортизация", "maturity": "погашение"}[s["kind"]],
                     _num(s["value"], 2), _num(s["percent"], 2) if s["percent"] is not None else "n/a",
                     _num(s["faceAfter"], 2), "выплачен" if s.get("past") else "впереди"])
    return {"type": "table", "title": "График выплат",
            "info": "Номинал после выплаты вычислен по легам амортизации — биржа в строке купона "
                    "оставляет ИСХОДНЫЙ номинал, даже когда часть его уже погашена.",
            "data": {"columns": ["дата", "тип", cur, "% год.", "номинал после", "статус"], "rows": rows}}


def _history_panels(d, amortising=False):
    h = d.get("history") or []
    if len(h) < 5:
        return []
    # v1.152 removed the price line, so for a bond whose yield we refuse to publish this tab would
    # be NOTHING BUT the suppressed series. Show the price instead — it is a fact for any bond.
    if not (d.get("flags") or {}).get("ytmMeaningful") or (d.get("ytm") or {}).get("toType") != "MATDATE":
        return _price_only_panel(d, h, amortising)
    labels = [x["date"] for x in h]
    # The price line was a MIRROR of the yield (measured on live data: daily changes correlate
    # -0.995 on ОФЗ 26238 and -0.978 on Селигдар3Р), so it spent a whole axis restating the same
    # series. What the yield alone cannot say is where the SOVEREIGN was — and that gap IS the
    # credit premium, i.e. "rates moved" vs "this credit re-priced". Price stays in the table.
    if not any(x.get("yield") is not None for x in h):
        return []          # a titled panel with an empty chart is worse than no panel
    ser = [{"type": "line", "name": "доходность, %", "values": [x["yield"] for x in h],
            "color": _ORANGE}]
    gh = {x["date"]: x for x in (d.get("gspreadHistory") or [])}
    if gh:
        # the sovereign leg is MONTHLY (12 slices) against a daily yield — without spanGaps its
        # isolated points draw nothing at all, legend and no line
        ser.append({"type": "line", "name": "ОФЗ на той же дюрации, %", "color": _BLUE,
                    "spanGaps": True, "dash": [5, 4],
                    "values": [round(gh[x["date"]]["sovereign"], 3) if x["date"] in gh else None
                               for x in h]})
    # `combo`, not `line`: only combo declares the y1 scale. On `line` a y1 series is handed to
    # Chart.js unmapped and it invents a SECOND LEFT axis, so the page showed two unlabelled left
    # axes while the panel's units promised the yield on the right.
    step = max(1, len(labels) // 12)
    labels = [d0 if i % step == 0 else "" for i, d0 in enumerate(labels)]   # combo has no xYear thinning
    return [{"type": "line", "title": "Доходность и суверенная кривая",
             "units": {"left": "% годовых"},
             "info": "Доходность по дневным закрытиям основной доски за %s — %s (%d торговых дней)."
                     "%s%s"
                     % (h[0]["date"], h[-1]["date"], len(h),
                        (" Синяя пунктирная — доходность ОФЗ на ТОЙ ЖЕ дюрации по %d МЕСЯЧНЫМ "
                         "срезам кривой (между ними линия соединяет соседние замеры, промежуточных "
                         "наблюдений у нас нет): расстояние между линиями и есть премия за "
                         "кредитный риск, поэтому видно, двигались ставки или переоценили именно "
                         "этот выпуск." % len(gh) if gh else
                         " Суверенная кривая за прошлые даты не получена, поэтому показана только "
                         "доходность выпуска — премию к ОФЗ здесь не рисуем."),
                        (" Цена вынесена в карточку «Цена» на Обзоре — на графике она была зеркалом "
                         "доходности (изменения коррелируют −0.99) и занимала ось, ничего не "
                         "добавляя. У этого выпуска она к тому же процент от ТЕКУЩЕГО номинала, "
                         "который гасится частями, так что через даты амортизации не сопоставима."
                         if amortising else
                         " Цена вынесена в карточку «Цена» на Обзоре: на графике она была зеркалом "
                         "доходности (изменения коррелируют −0.99).")),
             "data": {"labels": labels, "series": ser, "xYear": True}}]


def payments_vary(d):
    """Do this bond's payments actually CHANGE over time?

    A plain fixed-coupon bond's coupons are identical BY CONSTRUCTION, so a bar chart of them is a
    row of equal sticks — it cannot show dynamics because there is none (user, 2026-07-25). Bars are
    kept only where the payments genuinely vary: amortisation, a floater, a step coupon."""
    # Everything the chart would draw, MINUS the first coupon: a real issue very often has an odd
    # first payment for a short first period (ОФЗ 26238: 34.04 against 35.40 thereafter), and
    # counting that as "varying" would put every plain bond back on the equal-bar chart. Past
    # coupons count too — a floater's future ones have no rate yet, and its PAST ones are its story.
    vals = [s2["value"] for s2 in (d.get("schedule") or [])
            if s2["kind"] == "coupon" and s2.get("value") is not None][1:]
    if len(vals) < 2:
        return None          # not "level" — simply not enough KNOWN payments to say either way
    lo, hi = min(vals), max(vals)
    return bool(lo) and (hi - lo) / lo > 0.01


def cumulative_flows(d, today=None):
    """How much of what you PAY comes back, and by when — the shape the equal bars could not show.

    Measured against the DIRTY price (what a buyer actually parts with today), so the curve crossing
    100% is the real payback point and its steepness is the duration made visible."""
    today = today or dt.date.today()
    face = d.get("face") or d.get("faceOriginal")
    if not face or d.get("price") is None:
        return []
    # price×face is in the FACE currency while ACCRUEDINT is in the SETTLEMENT one — on a
    # currency-denominated rouble-settled issue adding them inflates «уплаченная цена» (СибурХ:
    # 128.98 instead of 102.60) and the curve then says a bond that repays in full never repays.
    # We cannot convert without an FX rate, so the panel is refused rather than shown wrong.
    if (d.get("settleCurrency") or d.get("faceUnit")) != d.get("faceUnit"):
        return []
    invested = d["price"] * face / 100.0 + (d["coupon"].get("accrued") or 0.0)
    if invested <= 0:
        return []
    out, run = [], 0.0
    for s2 in (d.get("schedule") or []):
        if s2.get("value") is None or s2.get("past"):
            continue
        try:
            if _d(s2["date"]) <= today:
                continue
        except (ValueError, TypeError):
            continue
        run += s2["value"]
        out.append({"date": s2["date"], "kind": s2["kind"], "amount": s2["value"],
                    "cum": run, "invested": invested, "cumPct": run / invested * 100.0})
    return out


def payback(cum):
    """The first date the cumulative return covers the price — or None (never a guessed date)."""
    for x in cum or []:
        if x["cumPct"] >= 100.0:
            return x["date"]
    return None


def gspread_history(d, curves, fit_ok=True):
    """The bond's premium over the SOVEREIGN through time.

    Price and yield are a mirror (measured: daily changes correlate -0.995), so plotting both spends
    an axis restating one series. What the yield alone cannot say is where the sovereign curve was —
    and that difference separates "rates moved" from "this credit re-priced"."""
    out = []
    # the SAME comparability rule the yield card and the curve panel apply: a floater's, an fx
    # issue's, a linker's or an offer-quoted issue's published YIELDCLOSE is not a ₽ yield-to-
    # maturity, so differencing it against the ₽ sovereign curve would print exactly the «премия за
    # кредитный риск» this module refuses to print on the very same page.
    if not fit_ok:
        # the same precondition build_market applies to its historical curve legs: we do not DRAW a
        # reconstructed NSS curve unless our implementation reproduced the published one today
        return out
    if not (d.get("flags") or {}).get("curveComparable"):
        return out
    if (d.get("ytm") or {}).get("toType") != "MATDATE":
        return out
    by = {x["date"]: x for x in (d.get("history") or []) if x.get("yield") is not None}
    cur_dur = (d.get("duration") or {}).get("years")
    for day in sorted(curves or {}):
        row = by.get(day)
        p = curve_params({"params": curves[day]}, pick="last") if isinstance(curves[day], dict) \
            and "B1" not in curves[day] else curves[day]
        if not row or not p:
            continue                       # a date without BOTH legs is dropped, never interpolated
        dur = row.get("durationYears") or cur_dur      # that day's own duration, not today's
        if not dur:
            continue
        sov = nss_yield(p, dur)
        if sov is None:
            continue
        out.append({"date": day, "ytm": row["yield"], "sovereign": sov, "duration": dur,
                    "gBp": (row["yield"] - sov) * 100.0})
    return out


def _risk_panels(d, r, today=None):
    """Price↔yield with the current point, plus the scenario ladder. Absent unless the model
    reproduced the market price (see risk())."""
    if not r or not r.get("ok"):
        return []
    cfs = cash_flows(d, today)
    face = d.get("face") or r["face"]
    accrued = d["coupon"].get("accrued") or 0.0
    pts, labs, kept = [], [], []
    for b in range(-400, 401, 50):
        yy = r["y"] + b / 10000.0
        pv = _reprice(cfs, yy)
        if pv is None:
            continue                       # a dropped grid point must not shift the marker
        kept.append(yy)
        labs.append("%.1f" % (yy * 100))
        pts.append((pv - accrued) / face * 100.0)
    if not kept:
        return []
    here = [None] * len(labs)
    _i = min(range(len(kept)), key=lambda j: abs(kept[j] - r["y"]))
    here[_i] = pts[_i]
    out = [{"type": "line", "title": "Чувствительность: цена ↔ доходность",
            "units": {"left": "% от номинала"},
            "info": "Как переоценится бумага при параллельном сдвиге доходности. Линия ВЫГНУТА — это "
                    "выпуклость: при падении ставок цена растёт сильнее, чем падает при таком же "
                    "росте. Оранжевая точка — сегодня. Считаем дисконтированием реального графика "
                    "выплат по доходности биржи; модель признана годной только потому, что "
                    "воспроизвела рыночную цену с точностью %.2f%%." % r["fitPct"],
            # the "сейчас" marker sits ON a curve point, so at the engine's uniform 2.6px it is
            # indistinguishable from every other dot — pointMarks is what makes it read as a marker
            "data": {"labels": labs, "showPoints": True,
                     "pointMarks": [({"color": _ORANGE} if j == _i else None) for j in range(len(labs))],
                     "series": [{"name": "цена", "values": pts, "color": _BLUE},
                                {"name": "сейчас", "values": here, "color": _ORANGE}]}}]
    sc = scenarios(d, r, today)
    if sc:
        rows = [["%+d б.п." % x["bp"] if x["bp"] else "без изменений",
                 _num(x["price"], 2), ("%+.2f%%" % x["pricePct"]) if x["pricePct"] is not None else "n/a",
                 ("%+.2f%%" % x["totalPct"]) if x["totalPct"] is not None else "n/a"] for x in sc]
        out.append({"type": "table", "title": "Сценарии по ставке (год)",
                    "info": "Арифметика параллельного сдвига доходности — это НЕ ПРОГНОЗ и не "
                            "вероятность. «Цена» — чистая цена сразу после сдвига; «полная "
                            "доходность» добавляет купоны, которые придут за ближайший год. "
                            "Реальная кривая двигается не параллельно.",
                    "data": {"columns": ["сдвиг", "цена, %", "изм. цены", "полная доходность"],
                             "rows": rows}})
    return out


def _floater_panel(d):
    """A floater's recovered coupon rate + the money-market base. The one panel this page type had."""
    cr = coupon_rates(d)
    if not d["flags"]["floater"] or len(cr) < 3:
        return []
    labs = [x["date"] for x in cr]
    ser = [{"type": "line", "name": "ставка купона (восстановлена)",
            "values": [x["pct"] for x in cr], "color": _ORANGE}]
    rus = d.get("rusfar") or []
    if rus:
        by = sorted((x["date"], x["close"]) for x in rus)
        # carry the last known base rate forward to each coupon date (RUSFAR is daily, coupons are
        # not) — but only within _RUSFAR_STALE_D days: past that the line would be FLAT invention,
        # not data (a truncated paged walk, or coupon dates beyond the last print)
        vals = []
        for lab in labs:
            hit = None
            for dd, cl in by:
                if dd <= lab:
                    hit = (dd, cl)
            if hit and (_d(lab) - _d(hit[0])).days <= _RUSFAR_STALE_D:
                vals.append(hit[1])
            else:
                vals.append(None)
        if any(v is not None for v in vals):
            ser.append({"type": "line", "name": "RUSFAR (денежный рынок)", "values": vals, "color": _BLUE})
    return [{"type": "combo", "title": "Ставка купона во времени",
             "units": {"left": "% годовых"},
             "info": "Биржа не публикует ставку купона флоатера (поле приходит пустым) — она "
                     "ВОССТАНОВЛЕНА из рублёвых выплат: купон ÷ непогашенный номинал × 365/период. "
                     "Это производная величина, а не поле биржи."
                     + (" Синяя линия — RUSFAR, ставка денежного рынка: расстояние между линиями и "
                        "есть премия выпуска над базой." if len(ser) > 1 else ""),
             "data": {"labels": labs, "series": ser, "showPoints": True}}]


_CORP_TAIL = ("У флоатеров в колонке доходности стоит «—»: для плавающего купона она не "
              "определена, хотя биржа число публикует. Валютные выпуски помечены своей валютой — "
              "их доходность не сравнима с рублёвой.")


def _corp_title(m, corp):
    if (m.get("sessionCorp") or {}).get("usable", True):
        return "Корпоративные выпуски · оборот ≥ %s за день" % _money(m["minTurnover"])
    # the threshold was NOT applied — the title must not claim it was
    return "Корпоративные выпуски · %d самых оборотистых (порог не применён)" % len(corp)


def _corp_info(m, corp):
    tot = m.get("corpTotal") or 0
    if (m.get("sessionCorp") or {}).get("usable", True):
        head = ("Отфильтровано по ОБОРОТУ: %d выпусков из %d на TQCB прошли порог — у остальных "
                "цена может быть одной случайной сделкой. " % (len(corp), tot))
    else:
        head = ("ПОРОГ ПО ОБОРОТУ НЕ ПРИМЕНЁН: сегодня его не взял ни один выпуск, поэтому "
                "показаны %d самых оборотистых из %d на TQCB — это НЕ отфильтрованная выборка. "
                % (len(corp), tot))
    # _thin_note also covers the not-applied case; here the head already said it, so only the
    # thin-SAMPLE addition is wanted (otherwise the panel says «НЕ ПРИМЕНЁН» twice)
    return head + _CORP_TAIL + (_thin_note(m, "sessionCorp")
                                if (m.get("sessionCorp") or {}).get("usable", True) else "")


def _thin_note(m, key):
    """The disclosure that rides every turnover-filtered panel when the session cannot rank yet."""
    liq = m.get(key) or {}
    if liq.get("thin"):
        return (" ВЫБОРКА МАЛА: порог сегодня взяли лишь %d выпусков из %d (торговались %d) — "
                "оборот накапливается в течение сессии, поэтому утром через фильтр проходит "
                "заметно меньше бумаг, чем к закрытию." % (liq.get("clears", 0), liq.get("n", 0),
                                                           liq.get("traded", 0)))
    if liq.get("usable", True):
        return ""
    return (" ФИЛЬТР ПО ОБОРОТУ НЕ ПРИМЕНЁН: порог сегодня взяли лишь %d выпусков из %d "
            "(торговались %d, максимальный оборот %s) — на таком объёме порог не отличает "
            "несвежую котировку от рынка, а оставил бы выборку из пары бумаг. Показаны цены и "
            "доходности последней завершённой сессии: их публикует биржа, но сегодняшним "
            "объёмом они не подтверждены."
            % (liq.get("clears", 0), liq.get("n", 0), liq.get("traded", 0),
               _money(liq.get("top") or 0)))


def _breakeven_panel(m):
    be = breakevens(m)
    if not be:
        return []
    _n_lk = sum(1 for r in (m.get("ofz") or []) if r.get("linker") and r.get("currency") in _RUB)
    return [{"type": "line", "title": "Вменённая инфляция (номинальные ОФЗ − ОФЗ-ИН)",
             "units": {"left": "% годовых"},
             "info": "Разница между номинальной кривой и реальной доходностью ОФЗ-ИН на той же "
                     "дюрации — то, какую среднюю инфляцию рынок закладывает до погашения. "
                     "ОГОВОРКА: индексация номинала ОФЗ-ИН отстаёт от ИПЦ примерно на 3 месяца, "
                     "поэтому это приближение, а не котируемая инфляция. Сами реальные доходности "
                     "на номинальную кривую не наносятся — сравнима только их РАЗНОСТЬ. "
                     "ЛИКВИДНОСТЬ КАЖДОГО ВЫПУСКА (ОФЗ-ИН торгуются редко, и одна несвежая "
                     "котировка сдвигает всю оценку): %s. Порога по обороту здесь НЕТ намеренно — "
                     "он удалил бы панель совсем; вместо этого тонкость показана, чтобы её было "
                     "видно." % "; ".join(
                         "%s — %s%s" % (x["name"], _money(x.get("valToday") or 0),
                                        (" на %d сделк." % x["numTrades"]) if x.get("numTrades")
                                        else ", сегодня не торговался")
                         for x in be),
             "data": {"labels": ["%.1f г." % x["duration"] for x in be],
                      "showPoints": True,
                      "showPoints": True,
                      # a TERM STRUCTURE, not categories: bars claimed four independent buckets
                      # where the shape across tenors is the whole point (user, 2026-07-25)
                      "series": [{"name": "вменённая инфляция", "color": _UP,
                                  "values": [round(x["breakeven"], 2) for x in be]},
                                 {"name": "номинальная ОФЗ", "color": _BLUE,
                                  "values": [round(x["nominal"], 2) for x in be]},
                                 {"name": "реальная (ОФЗ-ИН)", "color": _ORANGE,
                                  "values": [round(x["real"], 2) for x in be]}]}}]


def fetch_issuer(secid, shortname, fetch=None, _http=None, today=None):
    """Resolve the bond's issuer to the set of ITS securities. {emitentId, prefix, secids}.

    The obvious routes do not work and one of them lies: `securities.json?emitent_id=<id>` SILENTLY
    IGNORES the filter and answers with an unrelated first page, and `/iss/emitents/<id>/securities`
    is a 404. What works is the bond's own description (it carries EMITTER_ID) plus a shortname-prefix
    search whose rows carry `emitent_id` — the search is the heuristic, the ID is what makes it
    CORRECT. Verified live: ГТЛК 1P-01 → EMITTER_ID 5953; q=ГТЛК returns 52 rows, 51 carry that id
    and one (RU000A101D54, id 10497) is a different issuer and is dropped."""
    day = (today or dt.date.today()).isoformat()
    seam = fetch is not None
    get = _getter(fetch, _http)
    sid = (secid or "").upper()
    out = {"emitentId": None, "prefix": None, "secids": set()}
    desc = _pull(sid, "desc", "/securities/%s.json?iss.meta=off&iss.only=description" % sid,
                 get, seam, day)
    rows = mo._rows((desc or {}).get("description") or {})
    eid = None
    for r in rows:
        if (r.get("name") or "").upper() == "EMITTER_ID":
            eid = str(r.get("value") or "").strip()
    if not eid:
        return out
    out["emitentId"] = eid
    pref = re.split(r"[0-9]", (shortname or "").strip())[0].strip()
    if len(pref) < 3:
        pref = (shortname or "")[:6].strip()
    if not pref:
        return out
    out["prefix"] = pref
    # the cache key carries the PREFIX as well as the id: the query IS the prefix, and a second
    # bond of the same issuer with a different shortname would otherwise read a foreign result
    srch = _pull("_ISS%s_%s" % (eid, pref), "search",
                 "/securities.json?iss.meta=off&limit=%d&q=%s&iss.only=securities"
                 "&securities.columns=secid,shortname,type,emitent_id,is_traded,primary_boardid"
                 % (_SEARCH_LIMIT, _urlq(pref)), get, seam, day)
    rows = mo._rows((srch or {}).get("securities") or {})
    for r in rows:
        if str(r.get("emitent_id") or "").strip() == eid and r.get("secid"):
            out["secids"].add(r["secid"])
    # the search is capped at 100 and is NOT paged: a large issuer (Сбер/ВТБ/РЖД have hundreds of
    # issues) is therefore a SUBSET, and the panel must not call it "все выпуски эмитента"
    out["truncated"] = len(rows) >= _SEARCH_LIMIT
    return out


def curve_gate(r, cv):
    """Is this board row a usable point of an issuer's credit curve? Returns None, or WHY not.

    ONE predicate, deliberately shared by the curve and by the group statistics printed beside it.
    Duplicating it is how the two came to disagree: the medians applied only the basis test, so live
    ГТЛК published «разброс 7.05–17.81%» directly under a curve that had thrown that very 7.05%
    print out — the panel and its caption contradicting each other on the same screen."""
    if not r or not r.get("curveComparable") or r.get("ytm") is None:
        return "notComparable"
    t = r.get("durationYears")
    if not t or t * 365.0 < _MIN_SPREAD_DAYS:
        return "short"
    # ONE basis. Live ГТЛК has 1P-16 and 1P-10 quoted to an OFFER at 17.4%/17.1% beside issues quoted
    # to maturity — mixing them is the mislabel this module exists to prevent.
    if r.get("ytmTo") != "MATDATE":              # an ABSENT basis is not a maturity basis
        return "basis"
    # a price nobody traded today is not today's market (live ГТЛК 1P-02: 0 trades, 7.89%)
    if not (r.get("numTrades") or 0):
        return "untraded"
    sov = _curve_at(cv, t) if cv else None
    # a ₽ corporate cannot rationally sit far THROUGH the sovereign curve: live ГТЛК БО-08 printed
    # 7.05% against a 13.78% sovereign — 673 bp through, on ₽4 967 and 3 trades. A broken print, not
    # a credit signal. (fx paper, which legitimately yields less, never gets here — not comparable.)
    # SYMMETRIC: a stale thin print is as likely to sit far ABOVE the curve as below it.
    if sov is not None and not (_THROUGH_CURVE_BP <= (r["ytm"] - sov) * 100.0 <= _THROUGH_UP_BP):
        return "throughCurve"
    return None


def issuer_curve(d, board_rows, secids):
    """The issuer's own credit curve: every comparable issue it has outstanding, duration × yield.

    A single bond has one yield, not a curve — this is the honest per-bond analogue of the КБД, and
    it answers the sharper question: cheap against its OWN issuer, not just against the market.

    Same gates as everywhere else, and the horizon one is not theoretical: live ГТЛК 2P-07 at 0.02 y
    prints 19.96% and ГТЛК БО-08 at 0.10 y prints 7.05% (G −681 bp) — annualisation artefacts that
    would deform the curve they were plotted on."""
    cv = (d or {}).get("curve")
    out = []
    dropped = {"basis": 0, "untraded": 0, "throughCurve": 0, "short": 0}
    for sid in sorted(secids or ()):
        r = (board_rows or {}).get(sid)
        why = curve_gate(r, cv)
        if why:
            if why in dropped:
                dropped[why] += 1
            continue
        t = r["durationYears"]
        sov = _curve_at(cv, t) if cv else None
        out.append({"secid": sid, "name": r.get("name") or sid, "duration": t, "ytm": r["ytm"],
                    "valToday": r.get("valToday"), "gSpreadBp": r.get("gSpreadBp"),
                    "sovereign": sov,
                    "isThis": sid == (d or {}).get("secid")})
    if len(out) < 2:
        return []
    out.sort(key=lambda x: x["duration"])
    out[0]["_dropped"] = dropped          # disclosed on the panel, never silently filtered
    return out


def _why_not_me(d):
    """Why THIS bond is absent from its own issuer's curve — the ACTUAL reason.

    A single blanket sentence («нет сопоставимой доходности») was wrong most of the time and
    contradicted both the exclusion counters beside it and the yield card on the same page: a bond
    is far more often absent because it did not trade today, sits inside the horizon window, or is
    not on the board the curve is built from."""
    f = d.get("flags") or {}
    if f.get("floater"):
        why = "у неё плавающий купон, сопоставимой доходности к погашению нет"
    elif f.get("structured"):
        why = "это структурный выпуск, денежный поток не выводится"
    elif f.get("fx"):
        why = "её доходность — в валюте номинала и с рублёвой кривой не сравнивается"
    elif f.get("linker"):
        why = "у неё индексируемый номинал, доходность реальная, а не номинальная"
    elif (d.get("ytm") or {}).get("toType") == "OFFER":
        why = "её доходность посчитана К ОФЕРТЕ, а кривая строится по доходностям к погашению"
    else:
        why = ("она не прошла те же фильтры, что и остальные исключённые выпуски (см. выше) — "
               "скорее всего сегодня не торговалась или слишком близка к погашению")
    return " САМА ЭТА БУМАГА на кривую не попадает: %s." % why


def _dropped_note(ic):
    """Say WHAT was excluded and why — a silently short curve reads as "this issuer has 4 bonds"."""
    dr = (ic[0] or {}).get("_dropped") or {}
    bits = []
    if dr.get("basis"):
        bits.append("%d считаются к оферте (другая база — на одной кривой их мешать нельзя)" % dr["basis"])
    if dr.get("untraded"):
        bits.append("%d сегодня не торговались" % dr["untraded"])
    if dr.get("throughCurve"):
        bits.append("%d печатают доходность слишком далеко ОТ государственной кривой (ниже неё или "
                    "неправдоподобно выше) — это битый принт, а не премия" % dr["throughCurve"])
    if dr.get("short"):
        bits.append("%d слишком близко к погашению" % dr["short"])
    return (" Исключено: " + "; ".join(bits) + ".") if bits else ""


def _issuer_panel(d):
    ic = d.get("issuerCurve") or []
    if not ic:
        return []
    _me = [x for x in ic if x["isThis"]]
    _prem = [x["ytm"] - x["sovereign"] for x in ic if x.get("sovereign") is not None]
    med = None
    if _prem:
        sp = sorted(_prem)
        med = sp[len(sp) // 2] if len(sp) % 2 else (sp[len(sp) // 2 - 1] + sp[len(sp) // 2]) / 2.0
    return [{"type": "bubble",
             "title": "Кривая доходности эмитента (%s)" % (d.get("issuer") or {}).get("prefix", ""),
             "info": "Все сопоставимые выпуски этого эмитента: по горизонтали дюрация, по вертикали "
                     "доходность. Это и есть аналог КБД для конкретного имени — он отвечает не "
                     "«дорого ли против рынка», а «дорого ли против собственного эмитента». "
                     "Выпусков на кривой: %d.%s Флоатеры, валютные и структурные выпуски того же "
                     "эмитента сюда не попадают (у них нет сопоставимой доходности), как и бумаги "
                     "ближе %d дней до расчётной даты — там доходность становится "
                     "аннуализационным шумом. Кривая строится по доске TQCB — выпуски того же "
                     "эмитента на других досках сюда не попадают.%s%s"
                     % (len(ic),
                        (" Медианная премия к ОФЗ по кривой эмитента: %+d б.п." % round(med * 100))
                        if med is not None else "",
                        _MIN_SPREAD_DAYS,
                        (" ПОИСК ВЫПУСКОВ ОБРЕЗАН на %d бумагах — у крупного эмитента это "
                         "подмножество, а не весь его долг." % _SEARCH_LIMIT)
                        if (d.get("issuer") or {}).get("truncated") else "",
                        _dropped_note(ic)
                        # "this bond is not on the curve" is only meaningful when there IS a this
                        # bond: on the ISSUER-GROUP page the panel has no subject, and the branch
                        # fired unconditionally, explaining the absence of a paper nobody named
                        + ("" if _me or not d.get("secid") else _why_not_me(d))),
             "data": {"points": [{"x": x["duration"], "y": x["ytm"],
                                  "r": (14 if x["isThis"] else 6
                                        + min(14, round(((x["valToday"] or 0) / 1e9) * 6))),
                                  "color": _ORANGE if x["isThis"] else None,
                                  "label": ("● " + x["name"]) if x["isThis"] else x["name"]}
                                 for x in ic],
                      "xUnit": "лет (дюрация)", "yUnit": "% годовых"}}]


def _price_only_panel(d, h, amortising):
    """For a floater / fx / linker / offer-quoted issue: the PRICE, which is a fact, instead of a
    yield we refuse to name and a premium we refuse to compute."""
    vals = [x["close"] for x in h]
    if not any(v is not None for v in vals):
        return []
    return [{"type": "line", "title": "Цена",
             "units": {"left": "% от номинала"},
             "info": "Дневные закрытия основной доски за %s — %s (%d торговых дней). Доходность на "
                     "этой вкладке не показана: у этого выпуска она не сопоставима с рублёвой "
                     "кривой (%s), поэтому и премии к ОФЗ здесь быть не может.%s"
                     % (h[0]["date"], h[-1]["date"], len(h),
                        (d.get("noYtmReason") or "см. карточку доходности").rstrip("."),
                        (" Цена — процент от ТЕКУЩЕГО номинала, а он гасится частями, поэтому через "
                         "даты амортизации ряд не сопоставим." if amortising else "")),
             "data": {"labels": [x["date"] for x in h], "xYear": True,
                      "series": [{"name": "цена, % номинала", "values": vals, "color": _BLUE}]}}]


def _lead(d):
    f = d["flags"]
    who = d.get("name") or d.get("shortName") or d["secid"]
    bits = ["%s — %s." % (who, _kind_label(d))]
    if d["maturity"]:
        bits.append("Погашение %s%s." % (d["maturity"],
                                         (", ближайшая оферта %s" % d["offer"]["date"]) if d["offer"] else ""))
    if f["floater"]:
        bits.append("Купон плавающий, поэтому доходности к погашению на странице нет.")
    if f["fx"]:
        bits.append("Номинал в %s: доходность здесь — валютная и с рублёвой кривой не сравнивается."
                    % d["currency"])
    src = "Данные Московской биржи (ISS), официальные, с задержкой торговой сессии"
    if d.get("zcycMoment"):
        src += "; спреды посчитаны биржей к кривой на %s" % d["zcycMoment"]
    bits.append(src + ". Кредитных рейтингов в данных биржи нет — на странице их не будет.")
    return {"text": " ".join(bits), "meta": "as of %s" % (d.get("asOf") or "?")}


def to_spec(d, today=None, back=None):
    if d.get("unavailable"):
        return {"head": {"leftBig": d.get("secid") or "?", "leftType": "Bond"},
                "title": "Облигация не найдена",
                "lead": {"text": d.get("note") or "Нет данных.", "meta": ""},
                "dataSource": "MOEX ISS", "cards": [], "panels": []}
    head = {"leftBig": d.get("shortName") or d["secid"],
            "leftName": "%s%s" % (d.get("name") or "", (" · " + d["isin"]) if d.get("isin") else ""),
            "leftType": _kind_label(d),
            "rightBig": (_num(d["price"], 2) + "%") if d.get("price") is not None else "",
            "rightSmall": " · ".join(x for x in [
                ("%+.2f%%" % d["changePct"]) if d.get("changePct") else None,
                ("номинал %s%s" % (_cur(d["faceUnit"]), _num(d["face"], 0))) if d.get("face") else None,
            ] if x)}
    r = risk(d, today)
    d = dict(d, _risk=r, _scen=scenarios(d, r, today), _ystats=yield_stats(d))
    # the recovered rate is a FLOATER-only device: on a fixed-coupon bond the rate is published,
    # nothing is "recovered", and the card merely duplicated «Купон» with a misleading subtitle
    _cr = coupon_rates(d) if d["flags"]["floater"] else []
    if _cr:
        # the LAST SET coupon, named as such: publishing it as «текущая ставка» back-filled the very
        # hole the «Купон» card correctly reports as not yet fixed. And the spread must difference
        # the base rate AS OF THAT COUPON'S DATE — against the latest RUSFAR print it silently
        # absorbs a whole coupon period of base-rate drift.
        _last = _cr[-1]
        _rus = d.get("rusfar") or []
        _base = None
        for x in _rus:
            if x["date"] <= _last["date"]:
                _base = x
        d["_frate"] = {"pct": _last["pct"], "date": _last["date"],
                       "base": (_base or {}).get("close"), "baseDate": (_base or {}).get("date"),
                       "spread": (_last["pct"] - _base["close"]) if _base else None}
    over_p = []
    cp = _curve_panel(d)
    if cp:
        over_p.append(cp)
    over_p.extend(_issuer_panel(d))
    over_p.extend(_floater_panel(d))
    over_p.extend(_risk_panels(d, r, today))
    sections = [{"id": "overview", "name": "Обзор", "cards": _issue_cards(d), "panels": over_p}]
    sp = _schedule_panels(d)
    if sp:
        cf = []
        fut = [s for s in (d.get("schedule") or []) if not s.get("past") and s["kind"] == "coupon"]
        if fut:
            known = [s["value"] for s in fut if s["value"] is not None]
            unknown = len(fut) - len(known)
            # a floater's future coupons have no rate yet: summing the knowns and printing the result
            # as "всего" invents a total (all-unknown printed a flat ₽0.00 — the module's own rule)
            if not known:
                sub = "суммы будущих купонов ещё не определены"
            elif unknown:
                sub = "%s%s по %d известным · %d без ставки" % (_cur(d["faceUnit"]), _num(sum(known), 2),
                                                               len(known), unknown)
            else:
                sub = "%s%s всего" % (_cur(d["faceUnit"]), _num(sum(known), 2))
            cf.append({"label": "Купонов впереди", "value": str(len(fut)), "sub": sub,
                       "hint": "Сколько купонных выплат ещё предстоит и их сумма на одну облигацию. "
                               "Купоны с неустановленной ставкой в сумму не входят и посчитаны "
                               "отдельно — ноль вместо них был бы выдумкой."})
        if d.get("face"):
            cf.append({"label": "Номинал", "value": _cur(d["faceUnit"]) + _num(d["face"], 0),
                       "hint": "Текущий номинал одной облигации."})
        if d.get("offer"):
            cf.append({"label": "Ближайшая оферта", "value": d["offer"]["date"],
                       "sub": d["offer"].get("type"),
                       "hint": "Дата предъявления к выкупу."})
        sections.append({"id": "cashflow", "name": "Денежный поток", "cards": cf, "panels": sp})
    hp = _history_panels(d, amortising=d["flags"]["amortising"])
    if hp:
        sections.append({"id": "history", "name": "История", "cards": [], "panels": hp})
    _mk = d.get("market")
    if _mk:
        # LAST, so it never delays or displaces the default read. The engine's tab laziness is
        # RENDER-only, so this data ships in the page — ~30-60 KB, which the engine being shipped
        # ONCE instead of twice more than pays back (measured: merged 400 KB vs 702 KB in two files).
        _mp = _market_panels(_mk)
        if _mp:
            sections.append({"id": "market", "name": "Рынок", "cards": _market_cards(_mk),
                             "panels": _mp})
    spec = {"head": head, "lead": _lead(d),
            "title": "%s · облигация" % (d.get("shortName") or d["secid"]),
            "dataSource": "MOEX ISS (котировки, доходности, КБД, график выплат) — первичный источник биржи",
            "sections": sections, "active": "overview"}
    # the way HOME. Clicking a row on the issuer page is a real navigation to another file, so
    # without this chip the browser's Back button is the only route back (user, 2026-07-25: «как
    # мне вернуться обратно к обзору эмитента?»). Only a page that CAME from a group gets one —
    # a stand-alone issue render has nothing to return to, and a chip to a file that was never
    # written is exactly the dead link the engine's ladder exists to prevent.
    if back and back.get("href") and dashboard.rel_href_ok(back["href"]):
        spec["links"] = [{"text": back.get("text") or "← эмитент", "href": back["href"]}]
    return spec


# ----------------- the MARKET spec -----------------
def _ytm_cell(r):
    """A yield in a table ALWAYS carries what it is computed to. ФСК РС-22 prints -28.89% to an offer
    four days out while its maturity column reads 2027-07-21 — a bare number there is a lie."""
    if r.get("ytm") is None:
        return "—"
    base = "к оферте" if (r.get("ytmTo") or "").startswith("OFFER") else "к погаш."
    return "%s%% %s" % (_num(r["ytm"], 2), base)


def _market_cards(m):
    cv, c = m.get("curve"), []
    if cv:
        for t, lab in ((1.0, "1 год"), (2.0, "2 года"), (5.0, "5 лет"), (10.0, "10 лет")):
            v = _curve_at(cv, t)
            if v is not None:
                c.append({"label": "ОФЗ " + lab, "value": _num(v, 2) + "%",
                          "hint": "Точка кривой бескупонной доходности (КБД), опубликованной биржей."})
        y2, y10 = _curve_at(cv, 2.0), _curve_at(cv, 10.0)
        if y2 is not None and y10 is not None:
            c.append({"label": "Наклон 10л−2г", "value": "%+d б.п." % round((y10 - y2) * 100),
                      "hint": "Разница длинного и короткого конца. Отрицательная (инверсия) обычно "
                              "означает, что рынок ждёт снижения ставок."})
    _be = breakevens(m)
    if _be:
        c.append({"label": "Вменённая инфляция", "value": "%.1f%%" % _be[0]["breakeven"],
                  "sub": "%.1f г. → %.1f%% на %.1f г." % (_be[0]["duration"], _be[-1]["breakeven"],
                                                          _be[-1]["duration"]),
                  "hint": "Номинальная кривая ОФЗ минус реальная доходность ОФЗ-ИН на той же "
                          "дюрации — средняя инфляция, которую рынок закладывает до погашения. "
                          "Приближение: индексация ОФЗ-ИН отстаёт от ИПЦ примерно на 3 месяца."})
    _usable = (m.get("sessionCorp") or {}).get("usable", True)
    c.append({"label": "Выпусков в выборке" if _usable else "Показано выпусков",
              "value": str(len(m.get("corp") or [])),
              "sub": ("из %d на TQCB" % (m.get("corpTotal") or 0)) if _usable
                     else "из %d · порог не применён" % (m.get("corpTotal") or 0),
              "hint": ("Корпоративные выпуски, прошедшие фильтр по обороту (см. заголовок таблицы)."
                       if _usable else
                       "Порог по обороту сегодня не взял ни один выпуск, поэтому он НЕ применён — "
                       "это самые оборотистые бумаги доски, а не отфильтрованная выборка.")})
    return c


def _market_panels(m):
    p, cv = [], m.get("curve")
    if cv:
        # explicit, DISTINCT colours: leaving the historical legs to the palette gave "сегодня" and
        # "1М назад" two near-identical blues, so the one thing the panel compares was unreadable
        _hcol = ["#8a63d2", _ORANGE, "#9aa4b2"]
        ser = [{"name": "сегодня", "values": cv["yields"], "color": _BLUE}]
        for i, leg in enumerate(m.get("curveHist") or []):
            ser.append({"name": "%s назад (%s)" % (leg["label"], leg.get("date") or "?"),
                        "values": leg["yields"], "color": _hcol[i % len(_hcol)]})
        fit = m.get("curveFitPp")
        note = ""
        if m.get("curveHist"):
            note = (" Исторические кривые восстановлены из параметров модели, которые публикует биржа "
                    "(в историческом ответе готовых точек нет); наша реализация воспроизводит "
                    "СЕГОДНЯШНЮЮ опубликованную кривую с расхождением ≤%.3f п.п." % fit)
        elif fit is None or (fit or 0) > _CURVE_TOL_PP:
            note = (" Исторические кривые не показаны: проверка нашей реализации против опубликованной "
                    "кривой не прошла — лучше не рисовать, чем нарисовать неверно.")
        else:
            # the legs were requested but none came back for the day we asked for — say so rather
            # than quietly showing a single line that looks like the whole story
            note = (" Исторические кривые не показаны: биржа не отдала параметры за запрошенные даты "
                    "(а на неизвестный параметр этот эндпоинт отвечает СЕГОДНЯШНЕЙ кривой, которую "
                    "нельзя подписать «месяц назад»).")
        p.append({"type": "line", "title": "Кривая бескупонной доходности ОФЗ (КБД)",
                  "units": {"left": "% годовых"},
                  "info": "Сегодняшняя кривая — ТОЧКИ, опубликованные биржей (%s)." % (cv.get("asOf") or "—") + note,
                  "data": {"labels": ["%g" % t for t in cv["periods"]], "series": ser}})
    p.extend(_breakeven_panel(m))
    sc = m.get("scatter") or []
    if sc:
        p.append({"type": "bubble",
                  "title": "ОФЗ: дюрация × доходность (размер — оборот)",
                  "info": "Каждая точка — рублёвый выпуск ОФЗ, по горизонтали дюрация в годах, по "
                          "вертикали доходность. Показаны %d из %d выпусков доски: остальные "
                          "наторговали меньше %s за день, а на таком обороте «доходность» считается "
                          "от несвежей котировки (ОФЗ 46012 при обороте ₽38 тыс. печатает 2.4%% "
                          "против кривой в 14%%). Валютные ОФЗ сюда не попадают: их доходность — "
                          "в валюте номинала.%s"
                          % (len(sc), len(m.get("ofz") or []), _money(m.get("minScatterTurnover") or 0),
                             _thin_note(m, "sessionOfz")),
                  "data": {"points": [{"x": r["durationYears"], "y": r["ytm"],
                                       "r": 6 + min(20, round(((r["valToday"] or 0) / 1e9) * 8)),
                                       "label": r["name"] or r["secid"]} for r in sc],
                           "xUnit": "лет (дюрация)", "yUnit": "% годовых"}})
    rg = m.get("rgbi") or []
    if len(rg) > 5:
        p.append({"type": "line", "title": "RGBITR — индекс гособлигаций (полной доходности)",
                  "info": "Индекс полной доходности ОФЗ: цена плюс реинвестированные купоны.",
                  "data": {"labels": [x["date"] for x in rg], "xYear": True,
                           "series": [{"name": "RGBITR", "values": [x["close"] for x in rg], "color": _BLUE}]}})
    corp = m.get("corp") or []
    if not corp and (m.get("corpTotal") or 0):
        # the filter removed EVERYTHING: dropping the panel silently reads as "there is no corporate
        # market", when the truth is "nothing cleared the bar we set". Say which.
        p.append({"type": "table",
                  "title": "Корпоративные выпуски · оборот ≥ %s за день" % _money(m["minTurnover"]),
                  "info": "Ни один из %d выпусков доски TQCB не прошёл этот порог оборота — таблица "
                          "пуста не потому, что рынка нет, а потому, что фильтр выставлен выше всех "
                          "сегодняшних оборотов. Понизьте --min-turnover, чтобы увидеть менее "
                          "ликвидные выпуски (их цена тем менее показательна, чем реже сделки)."
                          % (m.get("corpTotal") or 0),
                  "data": {"columns": ["SECID", "выпуск", "погашение", "тип", "доходн. (к чему)",
                                       "дюрация", "G-spread", "вал.", "оборот"], "rows": []}})
    if corp:
        rows = []
        for r in corp[:60]:
            rows.append([r["secid"], r["name"], r["maturity"] or "n/a",
                         _type_short(r["bondType"]),
                         _ytm_cell(r),
                         _num(r["durationYears"], 2) if r["durationYears"] else "n/a",
                         ("%+d" % round(r["gSpreadBp"])) if r["gSpreadBp"] is not None else "—",
                         r["currency"] or "",
                         _money(r["valToday"], r.get("settleCurrency") or "SUR")])
        p.append({"type": "table",
                  "title": _corp_title(m, corp), "info": _corp_info(m, corp),
                  "data": {"columns": ["SECID", "выпуск", "погашение", "тип", "доходн. (к чему)", "дюрация",
                                       "G-spread", "вал.", "оборот"], "rows": rows}})
    return p


def to_market_spec(m):
    return {"head": {"leftBig": "Рынок облигаций", "leftName": "Московская биржа",
                     "leftType": "ОФЗ и корпоративные",
                     "rightBig": (_num(_curve_at(m.get("curve"), 10.0), 2) + "%")
                                 if m.get("curve") else "",
                     "rightSmall": "КБД 10 лет"},
            "lead": {"text": "Кривая бескупонной доходности ОФЗ и срез рынка облигаций Московской "
                             "биржи по данным самой биржи (ISS). Кредитных рейтингов в этих данных "
                             "нет — уровень листинга их не заменяет. Доходности флоатеров и "
                             "структурных выпусков не показываются: для них они не определены.",
                     "meta": "as of %s" % m.get("asOf")},
            "title": "Облигации МосБиржи · кривая и рынок",
            "dataSource": "MOEX ISS — КБД, доски TQOB/TQCB, RGBITR",
            "cards": _market_cards(m), "panels": _market_panels(m)}


def render_html(spec):
    """The ONE interactive engine page. MUST be render_interactive_html, not render_html: a sectioned
    spec rendered through the plain renderer loses its tab bar AND every non-active section's content
    (found in the Preview Pane — the offline suite had happily rendered valid HTML with 2 of 3 tabs
    missing, which is exactly why a dashboard slice is not done until the pixels are checked)."""
    return dashboard.render_interactive_html(spec)


# ----------------- the ISSUER GROUP: the same analysis, one level up -----------------
# A group is not a bond, so what carries over is not the numbers but the QUESTIONS. Yield becomes a
# median and a range (an "average bond" would be a fiction); the coupon schedule becomes a combined
# payment CALENDAR — the one thing a group shows that no single issue can; the sovereign comparison
# becomes the issuer's own curve, which this module already builds.
_GROUP_CAL_MAX = 12       # bondization pulls for the calendar: bounded, and the shortfall disclosed
_GROUP_MEMBERS_MAX = 24   # issue pages pre-rendered beside a group page: bounded, and disclosed
_GROUP_GIVEUP = 3         # consecutive render failures after which we stop trying (a dead ISS)


def resolve_issuer_group(name, fetch=None, _http=None, today=None):
    """Resolve an issuer NAME to its bonds. {emitentId, name, secids, alsoMatched, truncated}.

    A name is not an identifier: live, q=Селигдар returns 19 bonds across TWO issuer ids (15 and 4)
    because the name matches a parent and a neighbour. The dominant id wins — and the others are
    DISCLOSED with their counts rather than silently folded in or dropped."""
    day = (today or dt.date.today()).isoformat()
    seam = fetch is not None
    get = _getter(fetch, _http)
    out = {"emitentId": None, "name": name, "secids": [], "alsoMatched": [], "truncated": False}
    srch = _pull("_GRP" + str(name), "search",
                 "/securities.json?iss.meta=off&limit=%d&q=%s&iss.only=securities"
                 "&securities.columns=secid,shortname,type,emitent_id,is_traded"
                 % (_SEARCH_LIMIT, _urlq(name)), get, seam, day)
    if srch is None:
        # a FAILED search and a search that legitimately matched nothing are different facts, and
        # `_pull` answers None to both. Conflating them made the page state «Выпусков 0» — a
        # fabricated zero of exactly the class this module forbids (review r1 P2).
        out["searchFailed"] = True
        return out
    rows = mo._rows((srch or {}).get("securities") or {})
    out["truncated"] = len(rows) >= _SEARCH_LIMIT
    bonds = [r for r in rows if "bond" in str(r.get("type") or "") and r.get("secid")]
    if not bonds:
        return out
    counts = {}
    for r in bonds:
        eid = str(r.get("emitent_id") or "").strip()
        if eid:
            counts.setdefault(eid, []).append(r["secid"])
    if not counts:
        return out
    top = max(counts, key=lambda k: len(counts[k]))
    out["emitentId"] = top
    out["secids"] = sorted(counts[top])
    out["alsoMatched"] = sorted(({"emitentId": k, "n": len(v)} for k, v in counts.items() if k != top),
                                key=lambda x: -x["n"])
    return out


def _median(xs):
    v = sorted(x for x in xs if x is not None)
    if not v:
        return None
    m = len(v) // 2
    return v[m] if len(v) % 2 else (v[m - 1] + v[m]) / 2.0


def build_group(raw, today=None):
    """The group record: members, the issuer curve, group statistics and the payment calendar."""
    today = today or dt.date.today()
    g = (raw or {}).get("issuerGroup") or {}
    rows = {}
    try:
        rows = {r["secid"]: r for r in _board_rows((raw or {}).get("board"))}
    except Exception:                                          # noqa: BLE001
        rows = {}
    members = [rows[s] for s in (g.get("secids") or []) if s in rows]
    # ONE basis per statistic. `curveComparable` says nothing about YIELDDATETYPE, so without this a
    # −28.89%-to-an-offer print set minYtm and dragged the median that the page HEAD publishes —
    # the same mixing `issuer_curve` refuses two panels away (review r1 P1).
    # the medians are computed over EXACTLY the set the curve above them plots — one predicate, so
    # the two panels cannot contradict each other (review r1 P1, sharpened by the live ГТЛК run)
    _cv = parse_curve((raw or {}).get("zcyc"))
    _cc = [m for m in members if m.get("curveComparable") and m.get("ytm") is not None]
    comparable = [m for m in members if not curve_gate(m, _cv)]
    dropped_basis = len(_cc) - len(comparable)
    curve = issuer_curve({"secid": None, "curve": parse_curve((raw or {}).get("zcyc"))},
                         {m["secid"]: m for m in members}, {m["secid"] for m in members})
    durs = [m["durationYears"] for m in comparable if m.get("durationYears")]
    stats = {"n": len(members), "nComparable": len(comparable),
             "medianYtm": _median([m["ytm"] for m in comparable]),
             "minYtm": min((m["ytm"] for m in comparable), default=None),
             "maxYtm": max((m["ytm"] for m in comparable), default=None),
             "durMin": min(durs, default=None), "durMax": max(durs, default=None),
             "medianGBp": _median([m.get("gSpreadBp") for m in comparable]),
             "droppedBasis": dropped_basis,
             "turnover": sum((m.get("valToday") or 0) for m in members)}
    # the payment CALENDAR: coupon and principal legs kept apart — a redemption is return of capital,
    # not income, the distinction v1.152 was fixed for on the single-issue page
    bz = (raw or {}).get("bondizations") or {}
    # ONE population, three filters, each disclosed: a schedule only counts if the issue is a member
    # this page LISTS, and only if it pays in the same currency as the axis — a $0.79 coupon added
    # to a «₽ на облигацию» total is the v1.150 currency defect at group scale.
    mem_ids = {m["secid"] for m in members}
    fx_ids = {m["secid"] for m in members if m.get("fx")}
    usable = [s for s in bz if s in mem_ids and s not in fx_ids]
    by = {}
    for sid in usable[:_GROUP_CAL_MAX]:
        blk = bz[sid]
        face0 = _original_face(blk, (rows.get(sid) or {}).get("face"))
        sched, _am = _schedule(blk, face0, today)
        for x in sched:
            if x.get("past") or x.get("value") is None:
                continue
            slot = by.setdefault(x["date"], {"date": x["date"], "coupon": 0.0, "principal": 0.0,
                                             "ids": set()})
            if x["kind"] == "coupon":
                slot["coupon"] += x["value"]
            else:
                slot["principal"] += x["value"]
            # an amortising bond pays a coupon leg AND a principal leg on the same date: that is ONE
            # issue paying, not two, and «выпусков» doubled on exactly the dates that matter most
            slot["ids"].add(sid)
    cal = [{"date": by[k]["date"], "coupon": by[k]["coupon"], "principal": by[k]["principal"],
            "n": len(by[k]["ids"])} for k in sorted(by)]
    return {"issuer": g, "members": members, "comparable": comparable, "curve": curve,
            "stats": stats, "calendar": cal,
            "calendarFrom": min(len(usable), _GROUP_CAL_MAX),
            "calendarOf": len(members) - len(fx_ids), "calendarFx": len(fx_ids),
            "boardMissing": not (raw or {}).get("board"), "asOf": today.isoformat(),
            # the market panels want the BUILT record, not the raw blocks — passing the latter made
            # _market_panels return [] and the Рынок tab vanish without a word
            "market": (build_market((raw or {}).get("market"), today)
                       if (raw or {}).get("market") else None)}


def _group_cards(G):
    st, c = G["stats"], []
    if st["medianYtm"] is not None:
        c.append({"label": "Медианная доходность", "value": _num(st["medianYtm"], 2) + "%",
                  "sub": "разброс %s–%s%%" % (_num(st["minYtm"], 2), _num(st["maxYtm"], 2)),
                  "hint": "Медиана доходностей сопоставимых выпусков эмитента. Не «средняя "
                          "облигация» — такой бумаги не существует; разброс показан рядом именно "
                          "поэтому."})
    if st["durMin"] is not None:
        c.append({"label": "Дюрация", "value": "%.1f–%.1f г." % (st["durMin"], st["durMax"]),
                  "hint": "Диапазон дюраций выпусков на кривой эмитента."})
    if st["medianGBp"] is not None:
        c.append({"label": "Медианный G-spread", "value": "%+d б.п." % round(st["medianGBp"]),
                  "hint": "Премия эмитента к кривой ОФЗ по медиане его выпусков — кредитный спред "
                          "ИМЕНИ, который шумит меньше, чем спред одной бумаги."})
    if G.get("boardMissing"):
        # the board IS the membership, so without it the honest value is "unknown" — printing the
        # 0 that falls out of an empty intersection is a fabricated zero (review r1 P1)
        c.append({"label": "Выпусков", "value": "н/д",
                  "sub": "доска биржи не пришла",
                  "hint": "Состав выпусков определяется по доске Московской биржи, а она не "
                          "загрузилась. Это НЕ значит, что выпусков нет — значит, что мы их сейчас "
                          "не видим."})
    else:
        c.append({"label": "Выпусков", "value": str(st["n"]),
                  "sub": ("%d на кривой" % len(G["curve"])) if G["curve"] else "нет сопоставимых",
                  "hint": "Облигаций эмитента в сегодняшнем снимке доски и сколько из них попали на "
                          "кривую (флоатеры, валютные, структурные и бумаги к оферте на неё не "
                          "идут). Выпуски с других досок и снятые с котировки сюда не попадают."})
    if st["turnover"]:
        c.append({"label": "Оборот сегодня", "value": _money(st["turnover"]),
                  "hint": "Суммарный дневной оборот по всем выпускам эмитента."})
    return c


_SECID_RE = re.compile(r"^[A-Za-z0-9]{4,20}$")


def member_filename(page_path, secid):
    """The file an issue's own dashboard is rendered to, BESIDE the group page.

    Returned as a bare basename: it is used both as the write target (joined with the group page's
    directory) and as the relative href in the members table, and the engine refuses any href that
    is not relative. A SECID that is not a plain identifier returns None rather than being
    sanitised — every real MOEX SECID is alphanumeric, so anything else means the input is not
    what we think it is, and quietly rewriting a path is how a traversal becomes a feature."""
    if not _SECID_RE.match(str(secid or "")):
        return None
    stem = os.path.basename(str(page_path or "group.html"))
    stem = stem[:-5] if stem.lower().endswith(".html") else (stem or "group")
    return "%s__%s.html" % (stem, secid)


def _ytm_cell_cur(r):
    """`_ytm_cell` plus the CURRENCY when it is not roubles.

    A foreign-currency issue's yield is a real yield — in its own currency. Printed in the same
    column as the rouble ones it reads as comparable, and it is not (review r1 P1). Only the
    exception is marked: a «RUB» on thirteen of fourteen rows is noise, not information."""
    cell = _ytm_cell(r)
    if cell == "—":
        return cell
    cur = (r.get("currency") or "").upper()
    if cur and cur not in _RUB:
        cell += " (%s)" % cur
    # a LINKER's published yield is a REAL yield — above inflation, not beside a nominal one. Live
    # Селигдар prints SELGOLD001 at 13.06% among nominal 17–18% rows: unmarked it reads as the
    # cheapest paper on the page, when it is a different quantity (found at the pixels).
    if r.get("linker"):
        cell += " · реальн."
    return cell


def _group_members_panel(G, hrefs=None, rendered=None):
    """The list of issues — which the group page did not have at all until v1.153.

    Ordered by MATURITY, not by turnover: for one issuer's own paper the ladder (what matures when)
    is the reading, and it lines up with the payment calendar on the next tab. Every yield carries
    its basis — the v1.150 P0 defect was exactly a bare yield beside a maturity column, and this
    table is the same trap re-armed."""
    ms = G.get("members") or []
    if not ms:
        return []
    ms = sorted(ms, key=lambda m: (m.get("maturity") is None, m.get("maturity") or ""))
    rows, links = [], []
    for m in ms:
        rows.append([m.get("name") or m["secid"], m["secid"], m.get("maturity") or "n/a",
                     _type_short(m.get("bondType")),
                     _ytm_cell_cur(m),
                     _num(m["durationYears"], 2) if m.get("durationYears") else "n/a",
                     ("%+d" % round(m["gSpreadBp"])) if m.get("gSpreadBp") is not None else "—",
                     _money(m.get("valToday"), m.get("settleCurrency") or "SUR")])
        f = (hrefs or {}).get(m["secid"])
        links.append({"href": f, "ask": "Покажи дашборд облигации %s (%s)"
                                        % (m["secid"], m.get("name") or "")} if f else None)
    data = {"columns": ["выпуск", "SECID", "погашение", "тип", "доходн. (к чему)", "дюрация",
                        "G-spread", "оборот"], "rows": rows, "scroll": len(rows) > 14}
    if any(links):
        data["links"], data["linkcol"] = links, 0
    return [{"type": "table", "title": "Выпуски эмитента", "data": data,
             "info": ("Выпуски эмитента из сегодняшнего снимка доски, по сроку погашения. "
                      "Доходность показана ТОЛЬКО с базой, к которой она посчитана: у бумаги к "
                      "оферте это не доходность к погашению, и колонка «погашение» рядом делает "
                      "голое число ложью. У флоатеров и структурных выпусков доходности нет вовсе "
                      "— это не пропуск данных, а отсутствие величины. У валютного выпуска она "
                      "есть, но в СВОЕЙ валюте (помечена ею), а у линкера она РЕАЛЬНАЯ, то есть "
                      "сверх инфляции (помечена «реальн.»); с номинальными рублёвыми в один ряд "
                      "ни та, ни другая не становится — в медианы группы такие бумаги не входят."
                      + ((" Отрендерено %d страниц(ы) из %d: остальные ссылки предложат запросить "
                          "выпуск в чате." % (rendered, len(rows)))
                         if rendered is not None and rendered < len(rows) else "")
                      + (" Название выпуска — ссылка на его собственный дашборд."
                         if any(links) else ""))}]


def _group_panels(G, hrefs=None, rendered=None):
    """The Обзор tab. Panels are assigned to a tab EXPLICITLY (v1.153): the previous split sorted
    them by `type != "table"`, which put the calendar COMBO on both tabs at once (it is not a table,
    so it stayed on Обзор, and its title matched the cashflow filter) and would have swept any new
    table off Обзор silently."""
    p = []
    ic = G["curve"]
    if ic:
        p.extend(_issuer_panel({"issuerCurve": ic,
                                # `truncated` must ride along, or the panel's own "ПОИСК ОБРЕЗАН"
                                # disclosure can never fire on the page that needs it most
                                "issuer": {"prefix": G["issuer"].get("name"),
                                           "truncated": G["issuer"].get("truncated")},
                                "secid": None}))
    p.extend(_group_members_panel(G, hrefs, rendered))
    return p


def _group_cash_panels(G):
    p = []
    cal = G["calendar"]
    if cal:
        p.append({"type": "combo", "title": "Календарь выплат эмитента",
                  "units": {"left": "₽ на облигацию"},
                  "info": ("Совокупные будущие выплаты по %d рублёвым выпускам из %d (остальные не "
                           "тянулись — ограничение в %d графиков на группу). Купоны и погашения "
                           "показаны РАЗДЕЛЬНО: возврат тела — это не доход. Это то, чего страница "
                           "одного выпуска показать не может: где у эмитента пики платежей."
                           % (G["calendarFrom"], G["calendarOf"], _GROUP_CAL_MAX))
                          + ((" %d валютных выпуска(ов) в сумму НЕ включены: ось в рублях, а их "
                              "платежи в другой валюте — складывать их было бы ошибкой, а не "
                              "приближением." % G["calendarFx"]) if G.get("calendarFx") else ""),
                  "data": {"labels": [x["date"] for x in cal], "xYear": True, "series": [
                      {"type": "bar", "name": "купоны", "values": [round(x["coupon"], 2) for x in cal],
                       "color": _UP},
                      {"type": "bar", "name": "погашения", "values": [round(x["principal"], 2) for x in cal],
                       "color": _BLUE}]}})
        rows = [[x["date"], _num(x["coupon"], 2), _num(x["principal"], 2), str(x["n"])] for x in cal]
        p.append({"type": "table", "title": "Календарь: даты и суммы",
                  "info": "По одной строке на дату выплаты; «выпусков» — сколько бумаг платит в этот день.",
                  "data": {"columns": ["дата", "купоны", "погашения", "выпусков"], "rows": rows}})
    return p


def render_one(secid, path, with_market=True, back=None):
    """Render ONE issue's dashboard to `path` — the unit the group page pre-renders."""
    dashboard.write_atomic(path, render_html(to_spec(derive(build(
        gather(secid, with_market=with_market))), back=back)))


def render_members(G, page_path, with_market=True, _render=None):
    """Pre-render every member issue BESIDE the group page, so the table's links resolve.

    A dashboard is a static, self-contained file: nothing in it can run Python, so it cannot render
    a sibling on click. What makes the links real is therefore doing the renders up front — and the
    reason that is cheap is that the expensive part of an issue page is NOT per-bond: the 12 monthly
    ZCYC slices (measured 15.4 s of a 21 s cold render) are keyed globally and are already warm by
    the time the group page exists. Measured on Селигдар: 17.9 s for the first issue, then 0.3–1.2 s
    each. Best-effort per member, like every other optional pull here — one issue that fails must
    not cost the others their pages, and its link simply falls back to the ask ladder."""
    out_dir = os.path.dirname(os.path.abspath(page_path))
    rend = _render or (lambda s, p, **kw: render_one(s, p, **kw))
    back = {"href": os.path.basename(str(page_path)),
            "text": "\u2190 %s \u00b7 \u0432\u0441\u0435 \u0432\u044b\u043f\u0443\u0441\u043a\u0438"
                    % ((G.get("issuer") or {}).get("name") or "\u044d\u043c\u0438\u0442\u0435\u043d\u0442")}
    ok, bad, miss = 0, [], 0
    for m in (G.get("members") or [])[:_GROUP_MEMBERS_MAX]:
        f = member_filename(page_path, m["secid"])
        if not f:
            bad.append(m["secid"])
            continue
        try:
            rend(m["secid"], os.path.join(out_dir, f), with_market=with_market, back=back)
            ok, miss = ok + 1, 0
        except Exception:                                      # noqa: BLE001
            bad.append(m["secid"])
            miss += 1
            # a dead ISS fails every member identically, and each attempt is a full gather with its
            # own paged history walk — grinding through 100 of them helps nobody (review r1 P1)
            if miss >= _GROUP_GIVEUP:
                print("note: %d issue renders failed in a row — giving up on the rest; those links "
                      "fall back to an ask in chat." % miss, file=sys.stderr)
                break
    return ok, bad


def to_group_spec(G, hrefs=None, rendered=None):
    st = G["stats"]
    nm = G["issuer"].get("name") or "эмитент"
    also = G["issuer"].get("alsoMatched") or []
    lead = ("Все облигации эмитента как ОДИН объект анализа: медианы вместо доходности одной бумаги, "
            "собственная кривая эмитента и совокупный календарь выплат. Данные Московской биржи "
            "(ISS). Кредитных рейтингов в них нет.")
    if also:
        lead += (" ВНИМАНИЕ: по этому имени нашлись и другие эмитенты (%s) — их выпуски в группу НЕ "
                 "включены." % ", ".join("id %s: %d бум." % (a["emitentId"], a["n"]) for a in also))
    if G["issuer"].get("truncated"):
        lead += " Поиск обрезан на %d бумагах — у крупного эмитента это подмножество." % _SEARCH_LIMIT
    if G.get("boardMissing"):
        lead += (" ВНИМАНИЕ: доска биржи не загрузилась, а состав выпусков определяется именно по "
                 "ней — список бумаг, медианы и кривая на этой странице отсутствуют не потому, что "
                 "их нет, а потому, что мы их не видим.")
    sections = [{"id": "overview", "name": "Обзор", "cards": _group_cards(G),
                 "panels": _group_panels(G, hrefs, rendered)}]
    _tab = _group_cash_panels(G)
    if _tab:
        sections.append({"id": "cashflow", "name": "Денежный поток", "cards": [], "panels": _tab})
    mk = G.get("market")
    if mk:
        mp = _market_panels(mk)
        if mp:
            sections.append({"id": "market", "name": "Рынок", "cards": _market_cards(mk), "panels": mp})
    return {"head": {"leftBig": nm, "leftName": "облигации эмитента",
                     "leftType": "Группа выпусков",
                     "rightBig": (_num(st["medianYtm"], 2) + "%") if st["medianYtm"] is not None else "",
                     "rightSmall": "медианная доходность"},
            "lead": {"text": lead, "meta": "as of %s" % G.get("asOf")},
            "title": "%s · облигации эмитента" % nm,
            "dataSource": "MOEX ISS — доски, графики выплат, КБД",
            "sections": sections, "active": "overview"}


def render_group_text(G):
    st = G["stats"]
    L = ["%s — облигации эмитента (%d выпусков, %d на кривой)"
         % (G["issuer"].get("name"), st["n"], len(G["curve"]))]
    if st["medianYtm"] is not None:
        L.append("Медианная доходность: %s%% (разброс %s–%s%%)"
                 % (_num(st["medianYtm"], 2), _num(st["minYtm"], 2), _num(st["maxYtm"], 2)))
    if st["durMin"] is not None:
        L.append("Дюрация: %.1f–%.1f г." % (st["durMin"], st["durMax"]))
    if st["medianGBp"] is not None:
        L.append("Медианный G-spread: %+d б.п." % round(st["medianGBp"]))
    if G["calendar"]:
        L.append("Календарь выплат: %d дат по %d выпускам из %d"
                 % (len(G["calendar"]), G["calendarFrom"], G["calendarOf"]))
    for a in (G["issuer"].get("alsoMatched") or []):
        L.append("Внимание: по имени нашёлся и другой эмитент (id %s, %d бумаг) — не включён"
                 % (a["emitentId"], a["n"]))
    L.append("Источник: MOEX ISS, as of %s" % G.get("asOf"))
    return "\n".join(L)


# ----------------- text -----------------
def render_text(d):
    if d.get("unavailable"):
        return "%s: %s" % (d.get("secid") or "?", d.get("note") or "нет данных")
    L = ["%s (%s) — %s" % (d.get("shortName") or d["secid"], d.get("isin") or "?", _kind_label(d))]
    if d.get("price") is not None:
        L.append("Цена: %s%% от номинала%s" % (_num(d["price"], 2),
                                               (" · НКД %s%s" % (_cur(d["coupon"].get("accruedCurrency")),
                                                                                _num(d["coupon"]["accrued"], 2)))
                                               if d["coupon"]["accrued"] is not None else ""))
    if d.get("ytm"):
        L.append("Доходность %s: %s%%%s" % (d["ytm"]["label"], _num(d["ytm"]["value"], 2),
                                            (" (к дате %s)" % d["ytm"]["toDate"]) if d["ytm"].get("toDate") else ""))
    elif d.get("noYtmReason"):
        L.append("Доходность: не считается — " + d["noYtmReason"])
    if d.get("duration"):
        L.append("Дюрация: " + _dur_str(d["duration"]))
    _rk = d.get("risk")
    if _rk and _rk.get("ok"):
        L.append("Мод. дюрация %.2f · выпуклость %.1f (модель воспроизвела цену с точностью %.2f%%)"
                 % (_rk["modified"], _rk["convexity"], _rk["fitPct"]))
        _s3 = [x for x in (d.get("scenarios") or []) if x["bp"] == -300]
        if _s3 and _s3[0]["totalPct"] is not None:
            L.append("Сценарий −300 б.п.: полная доходность за год %+.1f%% (арифметика сдвига, не прогноз)"
                     % _s3[0]["totalPct"])
    _ys, _ys3 = d.get("yieldStats"), d.get("yieldStats3y")
    if _ys:
        L.append("Перцентиль доходности за %.0f г.: %.0f (диапазон окна %.2f–%.2f%%, %d торг. дн.)%s"
                 % (_ys.get("windowDays", 365) / 365.0, _ys["pct"], _ys["min"], _ys["max"], _ys["n"],
                    (" · за 3 г.: %.0f (%d дн.)" % (_ys3["pct"], _ys3["n"]))
                    if _ys3 and _ys3.get("n", 0) > _ys.get("n", 0) else ""))
    _fr = d.get("frate")
    if _fr:
        L.append("Ставка последнего купона (%s): %.2f%%%s"
                 % (_fr["date"], _fr["pct"],
                    (" · спред к RUSFAR %+.2f п.п." % _fr["spread"]) if _fr.get("spread") is not None else ""))
    if d.get("spreads"):
        s = d["spreads"]
        L.append("Спреды к ОФЗ: G %s · Z %s" % (
            ("%+d б.п." % round(s["gBp"])) if s.get("gBp") is not None else "n/a",
            ("%+d б.п." % round(s["zBp"])) if s.get("zBp") is not None else "n/a"))
    if d.get("spreadNote"):
        L.append("Спред к ОФЗ: " + d["spreadNote"])
    cp = d["coupon"]
    if cp.get("unset"):
        L.append("Купон: ставка следующего периода не установлена%s"
                 % ((" · след. выплата " + cp["next"]) if cp["next"] else ""))
    else:
        L.append("Купон: %s%s%s" % (_num(cp["percent"], 2) + "%" if cp["percent"] is not None else "n/a",
                                    (" (%s%s)" % (_cur(d["faceUnit"]), _num(cp["value"], 2)))
                                    if cp["value"] is not None else "",
                                    (" · след. %s" % cp["next"]) if cp["next"] else ""))
    if d.get("offer"):
        L.append("Ближайшая оферта: %s" % d["offer"]["date"])
    if d.get("maturity"):
        L.append("Погашение: %s%s" % (d["maturity"],
                                      (" (через %d дн.)" % d["daysToMaturity"])
                                      if d.get("daysToMaturity") is not None else ""))
    _ic = d.get("issuerCurve") or []
    if _ic:
        _prem = [x["ytm"] - x["sovereign"] for x in _ic if x.get("sovereign") is not None]
        _sp2 = sorted(_prem)
        _med = (_sp2[len(_sp2) // 2] if len(_sp2) % 2
                else (_sp2[len(_sp2) // 2 - 1] + _sp2[len(_sp2) // 2]) / 2.0) if _sp2 else None
        _mine = [x for x in _ic if x["isThis"]]
        L.append("Кривая эмитента: %d выпуска(ов), медианная премия к ОФЗ %s%s"
                 % (len(_ic), ("%+d б.п." % round(_med * 100)) if _med is not None else "n/a",
                    (" · эта бумага %+d б.п." % round((_mine[0]["ytm"] - _mine[0]["sovereign"]) * 100))
                    if _mine and _mine[0].get("sovereign") is not None else
                    " · сама бумага на кривую не попадает"))
    if d.get("market"):
        _mk2 = d["market"]
        L.append("Рынок (вкладка): КБД 10л %s · корпоративных в выборке %d"
                 % (_num(_curve_at(_mk2.get("curve"), 10.0), 2) + "%" if _mk2.get("curve") else "n/a",
                    len(_mk2.get("corp") or [])))
    L.append("Источник: MOEX ISS, as of %s" % (d.get("asOf") or "?"))
    return "\n".join(L)


def render_market_text(m):
    L = ["Рынок облигаций МосБиржи · %s" % m.get("asOf")]
    cv = m.get("curve")
    if cv:
        pts = ", ".join("%gл %s%%" % (t, _num(_curve_at(cv, t), 2)) for t in (1, 2, 5, 10) if _curve_at(cv, t))
        L.append("КБД ОФЗ: " + pts)
    if (m.get("sessionCorp") or {}).get("usable", True):
        L.append("ОФЗ в выборке: %d · корпоративных с оборотом ≥ %s: %d из %d"
                 % (len(m.get("ofz") or []), _money(m["minTurnover"]),
                    len(m.get("corp") or []), m.get("corpTotal") or 0))
    else:
        L.append("ОФЗ в выборке: %d · порог по обороту НЕ применён (сессия не набрала оборот): "
                 "показаны %d самых оборотистых из %d"
                 % (len(m.get("ofz") or []), len(m.get("corp") or []), m.get("corpTotal") or 0))
    if m.get("curveFitPp") is not None:
        L.append("Проверка нашей реализации КБД против опубликованной: ≤%.3f п.п." % m["curveFitPp"])
    L.append("Источник: MOEX ISS. Кредитных рейтингов в данных нет.")
    return "\n".join(L)


# ----------------- raw replay -----------------
_RAW_FILES = (("bond", "bond.json"), ("bondization", "bondization.json"), ("zcyc", "zcyc.json"),
              ("history", "history.json"), ("rusfar", "rusfar.json"),
              ("market", "market.json"), ("board", "board.json"), ("issuer", "issuer.json"),
              ("curves", "curves.json"), ("issuerGroup", "issuer_group.json"),
              ("bondizations", "bondizations.json"),
              ("tqob", "tqob.json"), ("tqcb", "tqcb.json"),
              ("rgbi", "rgbi.json"), ("zcycHist", "zcyc_hist.json"))


def save_raw(raw, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    for key, fn in _RAW_FILES:
        if (raw or {}).get(key) is not None:
            with open(os.path.join(out_dir, fn), "w", encoding="utf-8") as f:
                json.dump(raw[key], f, ensure_ascii=False)
    if (raw or {}).get("secid"):
        with open(os.path.join(out_dir, "secid.txt"), "w", encoding="utf-8") as f:
            f.write(raw["secid"])


def read_from_dir(in_dir):
    """Offline replay. A corrupt/missing file degrades to None — never a crash."""
    out = {}
    for key, fn in _RAW_FILES:
        p = os.path.join(in_dir, fn)
        try:
            with open(p, "r", encoding="utf-8") as f:
                v = json.load(f)
            # well-formed JSON of the WRONG SHAPE (a list) sailed past the ValueError guard and blew
            # up later on `.get` — the promise is "never a crash", not "never a parse error"
            out[key] = v if isinstance(v, dict) else None
        except (OSError, ValueError):
            out[key] = None
    try:
        with open(os.path.join(in_dir, "secid.txt"), "r", encoding="utf-8") as f:
            out["secid"] = f.read().strip()
    except (OSError, UnicodeDecodeError):      # non-UTF-8 bytes are a corrupt file, not a crash
        pass
    return out


# ----------------- CLI -----------------
def main(argv=None):
    ap = argparse.ArgumentParser(description="Russian bonds (MOEX ISS): issue + market dashboards")
    ap.add_argument("secid", nargs="?", help="bond SECID or ISIN (omit with --market)")
    ap.add_argument("--market", action="store_true", help="the market/curve page instead of one issue")
    ap.add_argument("--issuer", metavar="NAME",
                    help="the ISSUER-GROUP page: all of that issuer's bonds as one object of "
                         "analysis (medians, its own curve, the combined payment calendar)")
    ap.add_argument("--json", action="store_true", help="emit the structured record as JSON")
    ap.add_argument("--html", metavar="PATH", help="render the dashboard to PATH")
    ap.add_argument("--emit-spec", metavar="PATH", help="write the dashboard SPEC as JSON")
    ap.add_argument("--save-raw", metavar="DIR", help="save the raw ISS payloads for offline replay")
    ap.add_argument("--from-dir", metavar="DIR", help="rebuild from a --save-raw directory")
    ap.add_argument("--no-market", action="store_true",
                    help="minimal render: skips the Рынок tab AND the analytics that ride its pulls "
                         "— the issuer curve (needs the board) and the История sovereign line "
                         "(needs the monthly ZCYC slices)")
    ap.add_argument("--no-members", action="store_true",
                    help="--issuer: do NOT pre-render each member issue's own dashboard beside the "
                         "group page. The table stays linked either way — an unrendered member's "
                         "link falls back to asking for it in chat (a static page cannot render "
                         "on click; the pre-render is what makes the link resolve)")
    ap.add_argument("--min-turnover", type=float, default=MIN_TURNOVER,
                    help="market page: minimum daily turnover in roubles (default 1e7)")
    a = ap.parse_args(argv)
    if not a.market and not a.secid and not a.issuer:
        ap.error("give a bond SECID, or --market, or --issuer NAME")

    if a.issuer:
        raw = read_from_dir(a.from_dir) if a.from_dir else gather_group(
            a.issuer, with_market=not a.no_market)
        rec = build_group(raw)
        if (rec.get("issuer") or {}).get("searchFailed"):
            print("error: the issuer search failed (network or ISS) — no page written. This is NOT "
                  "'the issuer has no bonds'; retry.", file=sys.stderr)
            return 3
        # links are attached whenever a page is being written, whether or not the member pages get
        # pre-rendered: the engine's ladder HEAD-probes the file and falls back to an ask in chat,
        # so an unrendered member degrades to "ask for it", never to a dead link
        hrefs = {m["secid"]: member_filename(a.html, m["secid"])
                 for m in (rec.get("members") or [])
                 if member_filename(a.html, m["secid"])} if a.html else {}
        spec = to_group_spec(rec, hrefs=hrefs or None)
        text = render_group_text(rec)
    elif a.market:
        raw = read_from_dir(a.from_dir) if a.from_dir else gather_market()
        rec = build_market(raw, min_turnover=a.min_turnover)
        spec = to_market_spec(rec)
        text = render_market_text(rec)
    else:
        raw = (read_from_dir(a.from_dir) if a.from_dir
               else gather(a.secid, with_market=not a.no_market))
        rec = derive(build(raw))
        spec = to_spec(rec)
        text = render_text(rec)
    if a.save_raw:
        save_raw(raw, a.save_raw)
    if a.emit_spec:
        with open(a.emit_spec, "w", encoding="utf-8") as f:
            json.dump(spec, f, ensure_ascii=False)
    if a.html:
        # no `offers`: bonds are the auxiliary side of this skill (the user's own framing) and no
        # installed document fits a single issue or an issuer group. The MEMBER pages keep riding
        # write_atomic via render_one — 14 pre-rendered issues must not print 14 notes; the note
        # belongs to the page that gets PRESENTED.
        dashboard.deliver_html(a.html, render_html(spec))
        print("wrote " + a.html)
        # the group page lands FIRST — it is what gets presented; the member pages follow so the
        # links resolve. --from-dir has only the group's own payloads, so nothing to render from.
        if a.issuer and not a.no_members and not a.from_dir:
            _ok, _bad = render_members(rec, a.html, with_market=not a.no_market)
            print("pre-rendered %d issue page(s) beside it%s"
                  % (_ok, (" · %d failed (%s) — those links fall back to an ask in chat"
                           % (len(_bad), ", ".join(_bad[:4]))) if _bad else ""))
            # the page went out FIRST (it is what gets presented); only if fewer pages landed than
            # the table links does it need the honest count, and that re-render costs no network
            if _ok < len(rec.get("members") or []):
                dashboard.write_atomic(a.html, render_html(
                    to_group_spec(rec, hrefs=hrefs or None, rendered=_ok)))
    if a.json:
        # keys starting with "_" are working state (the parsed 3020-row board), not the record:
        # shipping them turned --json, the documented sourcing path, into 2.7 MB per bond
        print(json.dumps({k: v for k, v in rec.items() if not k.startswith("_")},
                         ensure_ascii=False))
    elif not a.html:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
