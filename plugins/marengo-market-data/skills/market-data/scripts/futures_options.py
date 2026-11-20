#!/usr/bin/env python3
"""
futures_options.py — the DETERMINISTIC futures-options data producer (see references/databento.md
"Branch A2" and docs/live-dashboard-spec.md). One command → the whole volatility smile for options
ON a CME future (ES/CL/GC/PA…), following the fixed recipe so it's fast and never improvises:

  option code from the TABLE (PA->PAO…)  →  ONE expiration (nearest quarterly)  →  the FULL
  TRADEABLE ladder of that series (every strike with open interest, never a ±window)  →  price off
  settlements  →  IV via greeks.py (Black-76)  →  a dashboard SPEC  →  the central dashboard.py.

  python3 futures_options.py PA                 # text summary
  python3 futures_options.py PA --html pa.html  # + a self-contained dashboard (dashboard.py)
  python3 futures_options.py CL --json          # the dashboard spec as JSON

Pure stdlib: Databento over HTTP (reusing chart.py's client), IV from greeks.py, render from
dashboard.py. Illiquid metals are settlement-marked (no live ticks) — priced off settlements.
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures
import csv
import datetime as dt
import errno
import io
import json
import math
import os, pathlib
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chart  # HTTP client (get_key / http_get / databento_frontier)  # noqa: E402
import dashboard  # the one renderer: spec -> self-contained --html  # noqa: E402
import greeks  # Black-76 IV  # noqa: E402

DATASET = "GLBX.MDP3"

# Futures root -> option asset (parent = `{asset}.OPT`). Verified live 2026-07-01 for PAO/HXE/EUU;
# the rest are the known distinct-code pattern. Equity-index / livestock share their root.
OPTION_ROOTS = {
    "CL": "LO", "NG": "ON", "HO": "OH", "RB": "OB", "GC": "OG", "SI": "SO", "PL": "PO",
    "PA": "PAO", "HG": "HXE", "ZC": "OZC", "ZS": "OZS", "ZW": "OZW", "ZL": "OZL",
    "6E": "EUU", "6J": "JPU", "ES": "ES", "NQ": "NQ", "LE": "LE",
    # 2026-07-04 GLBX universe audit (docs/databento-audit-2026-07-04.md): every entry verified
    # against the full definition sweep — asset exists, underlying joins back to the root.
    "BZ": "BZO", "MCL": "MCO", "MNG": "MNO", "MGC": "OMG",           # NYMEX/COMEX energy+metals
    "HE": "HE", "GF": "GF", "DC": "DC", "CB": "CB",                  # CME livestock/dairy
    "BTC": "BTC", "ETH": "ETH", "SR3": "SR3", "MES": "EX", "MNQ": "MQE",  # CME crypto/rates/micro-index
    "ZM": "OZM", "KE": "OKE", "ZN": "OZN", "ZB": "OZB", "TN": "OTN",  # CBOT (live socket = license caveat)
}
QCODE = {3: "H", 6: "M", 9: "U", 12: "Z"}  # CME quarterly month codes (Mar/Jun/Sep/Dec)


def _packaged_openai(_root=None):
    """Static build marker; local/Claude checkouts remain on their historical behaviour."""
    root = _root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        with open(os.path.join(root, "market-data-host.json"), encoding="utf-8") as f:
            return json.load(f).get("host") == "openai"
    except (OSError, ValueError, AttributeError):
        return False


def _openai_heavy_env_guard(openai=None, fresh_defs=False):
    """Keep the shipped caches active for an OpenAI --term/--full one-shot.

    Claude/local development keeps honoring both environment kill-switches. `--fresh-defs`
    remains the explicit way to bypass a snapshot read; the snapshot is still left enabled so
    the fresh listing can write through for the next run.
    """
    if openai is None:
        openai = _packaged_openai()
    if not openai:
        return []
    notes = []
    for key, label in (("MARKET_DATA_FUT_DAYCACHE", "same-day statistics cache"),
                       ("MARKET_DATA_FUT_DEFS_SNAPSHOT", "definitions snapshot")):
        if os.environ.get(key, "").strip().lower() in ("0", "off", "no", "false"):
            os.environ.pop(key, None)
            notes.append("OpenAI heavy run kept the %s enabled (ignored %s kill-switch)" % (label, key))
    return notes


# flock/msvcrt answer "held by someone" and "this filesystem cannot lock at all" through the same
# OSError, and conflating them is a permanent false abort: a 9p/FUSE sandbox mount or NFS without
# lockd would report every heavy render as already running, forever.
_LOCK_UNSUPPORTED = {getattr(errno, name) for name in
                     ("ENOLCK", "ENOSYS", "EOPNOTSUPP", "ENOTSUP", "EINVAL", "EBADF")
                     if hasattr(errno, name)}


def _try_lock_exclusive(fd):
    """Take a non-blocking exclusive OS lock on `fd`.

    True  — it is ours.
    False — another process holds it.
    None  — this platform or filesystem offers no locking; the caller proceeds unguarded and says so.
    """
    try:
        import fcntl
        call = lambda: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except ImportError:
        try:
            import msvcrt
        except ImportError:
            return None                 # neither primitive exists on this interpreter
        def call():
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    for _ in range(3):
        try:
            call()
            return True
        except OSError as e:
            if e.errno == errno.EINTR:
                continue                # a signal interrupted the call, not a verdict
            if e.errno in _LOCK_UNSUPPORTED:
                return None
            return False                # EWOULDBLOCK/EAGAIN/EACCES/EDEADLK: genuinely held
    return False                        # only repeated EINTR reaches here — stay conservative


def _unlock(fd):
    try:
        import fcntl
    except ImportError:
        pass
    else:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        return
    try:
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    except (ImportError, OSError):
        pass


class _OutputLease:
    """Exclusive lease for one expensive dashboard output path, held by the OPERATING SYSTEM.

    The lock is an flock (POSIX) / msvcrt byte lock (Windows) on `<out>.lock`, so the KERNEL
    releases it the moment the holder dies — sandbox timeout, SIGTERM, SIGKILL, OOM killer, all
    of them. That is the whole reason to use one.

    The previous design inferred liveness from the lock file's CONTENT — an owner PID, an mtime
    age — and every such scheme has to GUESS whether the writer is still alive, then RECLAIM the
    lock it judged dead. Three review rounds each found a new race in that reclaim (an unlink by
    name removing a rerun's fresh lock; then the same defect one level down in the lock that was
    added to serialise the reclaim), plus a PID probe that cannot run on Windows, where os.kill
    terminates rather than asks. None of that exists here: there is no reclaim, no staleness
    window, no owner token to trust, and no signal handler — dying IS releasing.
    """

    def __init__(self, output_path):
        self.path = os.path.abspath(output_path) + ".lock"
        self.closed = False
        self.unguarded = False
        self._fd = None
        # O_RDWR needs write permission on the FILE, so a stale lock from another user is an
        # OSError, not a refusal — and the caller guards RuntimeError. Say it in the one language
        # that reaches the user instead of a traceback.
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
        except OSError as e:
            raise RuntimeError("cannot prepare the render lock beside %s (%s)"
                               % (os.path.abspath(output_path), e))
        for _ in range(5):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
            except OSError as e:
                raise RuntimeError("cannot open the render lock %s (%s) — remove it or render to "
                                   "another path" % (self.path, e))
            got = _try_lock_exclusive(fd)
            if got is None:
                self._fd = fd           # nothing here can lock: proceed, but do not pretend
                self.unguarded = True
                print("warning: no file locking on this filesystem — a concurrent heavy render of "
                      "%s cannot be detected" % os.path.abspath(output_path), file=sys.stderr)
                return
            if not got:
                os.close(fd)
                raise RuntimeError("a heavy render is already running for %s; wait for it instead "
                                   "of starting a second writer" % os.path.abspath(output_path))
            # We may have locked an inode the previous holder unlinked on its way out, while a
            # third run created a NEW file at this path and locked that one. Locking a deleted
            # inode excludes nobody, so confirm the file we hold is still the file at the path.
            try:
                held = os.fstat(fd).st_ino == os.stat(self.path).st_ino
            except OSError:
                held = False
            if held:
                self._fd = fd
                try:                    # diagnostic only — nothing ever trusts this content
                    os.truncate(fd, 0)
                    os.write(fd, ("%d\n" % os.getpid()).encode("ascii"))
                except OSError:
                    pass
                return
            _unlock(fd)
            os.close(fd)
        raise RuntimeError("a heavy render is already running for %s; wait for it instead of "
                           "starting a second writer" % os.path.abspath(output_path))

    def close(self):
        if self.closed:
            return
        self.closed = True
        fd, self._fd = self._fd, None
        if fd is None:
            return
        if os.name == "nt":
            # Windows refuses to unlink a file that has an open handle (CPython opens without
            # FILE_SHARE_DELETE), so release and close FIRST or the lock file is left behind by
            # every render. A concurrent holder still has ITS handle open, which makes the unlink
            # below fail — which is exactly the outcome we want there.
            _unlock(fd)
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(self.path)
            except OSError:
                pass
            return
        try:
            os.unlink(self.path)        # POSIX: unlink while still HOLDING it, so a waiter that
        except OSError:                 # opened this inode finds the path gone, re-checks identity
            pass                        # and retries the file that replaced it
        _unlock(fd)
        try:
            os.close(fd)
        except OSError:
            pass

    def __del__(self):
        self.close()


def resolve_option_root(root: str) -> str:
    """The option asset code for a futures root (PA->PAO). Unknown -> itself (try `{root}.OPT`)."""
    return OPTION_ROOTS.get(root.upper(), root.upper())


def nearest_quarterly(today: dt.date):
    """TEST-ONLY symbology reference — NOT in the selection path (gather() picks series data-drivenly by
    real expiration; do NOT wire this back in: a computed month/year breaks on the year-width convention
    and metals' expire-month-before roll). The nearest quarterly (H/M/U/Z) that hasn't rolled."""
    y, m = today.year, today.month
    for yy, mm in [(y, 3), (y, 6), (y, 9), (y, 12), (y + 1, 3), (y + 1, 6), (y + 1, 9)]:
        if yy > y or mm > m or (mm == m and today.day <= 14):
            return QCODE[mm], yy
    return "H", y + 1


def series_prefix(root: str, today: dt.date) -> str:
    """TEST-ONLY (see nearest_quarterly) — the raw-symbol series prefix, e.g. PA -> `PAOU6`."""
    mc, yr = nearest_quarterly(today)
    return f"{resolve_option_root(root)}{mc}{yr % 10}"


# ----------------- compute (offline-testable) -----------------
def _iv(price, K, T, F, is_call):
    if not price or price <= 0:
        return None
    try:
        v = greeks.implied_vol(price, K, T, F=F, is_call=is_call, r=0.0)
        return v if (v and 0.01 < v < 3.0) else None
    except Exception:
        return None


def _exp_move(forward, atm_iv_pct, dte):
    """1σ expected move by expiry: forward × ATM IV × √(DTE/365) (IV in PERCENT) -> (±points, ±%).
    Degenerate inputs (no forward / no IV / dte<=0) -> (None, None), never a fake 0. SHARED by
    build_chain (Databento settlement) AND chain_to_an (connector) — review P1 2026-07-13: computing
    it only in build_chain left the DEFAULT connector path with a permanent n/a card."""
    if not forward or atm_iv_pct is None or not dte or dte <= 0:
        return None, None
    em = forward * (atm_iv_pct / 100.0) * math.sqrt(dte / 365.0)
    return em, em / forward * 100


def build_chain(root, forward, contracts, expiration, T, today=None, fwd_src="underlying", option_root=None, under=None, ladder="full"):
    """Assemble the analysis from a normalised contract list (what `gather()` returns):
    `contracts = [{strike, is_call, oi, settlement}]`. DEFAULT = the FULL TRADEABLE ladder (every
    strike with OI on either side). IV is the OTM composite (put for K<=F, call for K>F), in PERCENT."""
    today = today or dt.date.today()
    by = {}  # strike -> {"call": rec, "put": rec}
    for c in contracts:
        by.setdefault(c["strike"], {})[("call" if c["is_call"] else "put")] = c

    def oi(rec, side):
        return int((rec.get(side) or {}).get("oi") or 0)

    strikes = sorted(k for k, r in by.items() if oi(r, "call") > 0 or oi(r, "put") > 0)
    if not strikes:  # nothing has OI (rare) -> fall back to every listed strike, don't return empty
        strikes = sorted(by)
    atm = min(strikes, key=lambda k: abs(k - forward))

    putIV, callIV, callOI, putOI = [], [], [], []
    for k in strikes:
        r = by[k]
        # FULL IV for BOTH legs at every strike (the table shows all; the smile masks to OTM in to_spec).
        # Deep-ITM legs often have no solution -> None -> "n/a" in the table (honest).
        piv = _iv((r.get("put") or {}).get("settlement"), k, T, forward, False)
        civ = _iv((r.get("call") or {}).get("settlement"), k, T, forward, True)
        putIV.append(round(piv * 100, 2) if piv else None)
        callIV.append(round(civ * 100, 2) if civ else None)
        callOI.append(oi(r, "call"))
        putOI.append(oi(r, "put"))

    ai = strikes.index(atm)
    atm_iv = putIV[ai] if putIV[ai] is not None else callIV[ai]  # percent

    def iv_at(target, arr):
        cand = [(abs(k - target), arr[i]) for i, k in enumerate(strikes) if arr[i] is not None]
        return min(cand)[1] if cand else None

    p90, c110 = iv_at(0.90 * forward, putIV), iv_at(1.10 * forward, callIV)
    skew = round(p90 - c110, 1) if (p90 is not None and c110 is not None) else None

    def payout(S):
        return sum(oi(by[k], "call") * max(0, S - k) + oi(by[k], "put") * max(0, k - S) for k in strikes)

    coi, poi = sum(callOI), sum(putOI)
    maxpain = min(strikes, key=payout) if (strikes and (coi or poi)) else None  # meaningless with no OI
    dte = (dt.date.fromisoformat(expiration) - today).days if expiration else round(T * 365)
    # v1.82.0 expected move: the 1σ straddle-implied band by this expiry — arithmetic on the Black-76
    # ATM IV already computed above (atm_iv is PERCENT); shared helper with chain_to_an.
    em, em_pct = _exp_move(forward, atm_iv, dte)
    return {
        "symbol": root.upper(), "optionRoot": (option_root or resolve_option_root(root)).upper(), "expiration": expiration,
        "dte": dte, "forward": forward, "atmStrike": atm, "atmIV": atm_iv, "skew25": skew,
        "expMove": em, "expMovePct": em_pct,
        "pcrOI": (poi / coi if coi else None), "callOItotal": coi, "putOItotal": poi,
        "maxPain": maxpain, "nStrikes": len(strikes), "strikes": strikes,
        "putIV": putIV, "callIV": callIV, "callOI": callOI, "putOI": putOI, "fwdSrc": fwd_src,
        "under": under, "ladder": ladder,   # strike-set provenance: full | coarsened (/term cap) | window
    }


def _num(x, d=None):
    """Adaptive decimals when d is None: 0 for >=100 (palladium 1,220), more for small values (NG
    strike 3.15) — so a $3/MMBtu strike isn't rounded to '3'. Pass d explicitly for counts (OI -> 0)."""
    if x is None:
        return "n/a"
    if d is None:
        ax = abs(x)
        d = 0 if ax >= 100 else (1 if ax >= 10 else (2 if ax >= 1 else 3))
    return f"{x:,.{d}f}"


def _ivfmt(x):
    return "n/a" if x is None else f"{x:.1f}%"


def _greeks_at(an, K, iv_pct, is_call):
    """Black-76 greeks at strike K from an IV already on `an` (percent). None if inputs are degenerate
    (no IV / no forward / expired). greeks.py is the ONLY place we compute greeks — the smile IV in and
    the delta out use the SAME model, so the Δ column is consistent with the OTM smile it sits beside."""
    F, dte = an.get("forward"), an.get("dte")
    if iv_pct is None or F is None or K is None or not dte or dte <= 0 or F <= 0 or K <= 0:
        return None
    try:
        return greeks.black76(F, K, dte / 365.0, iv_pct / 100.0, is_call=is_call)
    except Exception:
        return None


def gex_ladder(an, iv_col):
    """Naive dealer gamma exposure per strike: Γ(K) × (callOI − putOI), in Δ-CONTRACTS PER 1 POINT of
    the underlying (× the contract multiplier for $). Black-76 Γ is call/put-identical (parity), so ONE
    Γ per strike from the SAME OTM-composite IV the smile/Δ column use. Sign = the standard naive
    convention (dealers long calls / short puts) — dealer positioning is NOT observable from OI; the
    panel info states the assumption. Honesty: netOI == 0 -> 0.0 (a real zero); no solvable IV with
    real OI -> None (unknown ≠ 0, the bar is dropped, the table shows n/a)."""
    out = []
    for i, k in enumerate(an["strikes"]):
        net = (an["callOI"][i] or 0) - (an["putOI"][i] or 0)
        if net == 0:
            out.append(0.0)
            continue
        g = _greeks_at(an, k, iv_col[i], True)
        out.append(g.gamma * net if g else None)
    return out


def gamma_flip(an, iv_col):
    """The spot level where naive dealer gamma Σ Γ(K; F=S, σ_K)·netOI_K crosses zero — STICKY-STRIKE
    (each strike keeps its current IV as S moves; a stated modeling assumption, like the dealer sign).
    Swept over strikes + midpoints, linearly interpolated at the sign change; multiple crossings -> the
    one nearest the forward. None (honest) when the book never crosses, every net is flat, or the chain
    is degenerate (dte<=0 / no forward)."""
    F, dte, strikes = an.get("forward"), an.get("dte"), an["strikes"]
    if not strikes or F is None or F <= 0 or not dte or dte <= 0:
        return None
    T = dte / 365.0
    rows = [(k, iv_col[i] / 100.0, (an["callOI"][i] or 0) - (an["putOI"][i] or 0))
            for i, k in enumerate(strikes) if iv_col[i]]
    rows = [r for r in rows if r[2] != 0]
    if not rows:
        return None

    def total(S):
        t = 0.0
        for k, sig, net in rows:
            try:
                t += greeks.black76(S, k, T, sig, is_call=True).gamma * net
            except Exception:
                pass
        return t

    grid = []
    for i, k in enumerate(strikes):
        grid.append(k)
        if i + 1 < len(strikes):
            grid.append((k + strikes[i + 1]) / 2.0)
    tv = [(S, total(S)) for S in grid]
    nz = [(S, t) for S, t in tv if abs(t) > 1e-12]
    if not nz:
        return None
    crossings = [s0 + (s1 - s0) * (-t0) / (t1 - t0)
                 for (s0, t0), (s1, t1) in zip(nz, nz[1:]) if (t0 < 0 < t1) or (t1 < 0 < t0)]
    if not crossings:
        return None
    return round(min(crossings, key=lambda x: abs(x - F)), 4)


def _smile_iv(an, strikes, atm):
    """Per-strike display IV for the smile + table. Use ONLY the OTM leg's quote — put IV for K<=ATM,
    call IV for K>ATM — shown AS-IS, even when thin / low-OI. (OI is NOT quotability: a low-OI strike with
    a live quote in the book is real data, not something to smooth away. And we NEVER substitute the ITM
    leg — its IV is noisy, which is the whole reason the smile is an OTM composite; parity-substituting it
    just reintroduces that noise as outliers.) Where the OTM leg has NO quote, that strike is linearly
    INTERPOLATED — but ONLY when it's bracketed by real quotes on both sides; a true EDGE gap (nothing to
    bracket it on one side) is left None/n/a. We interpolate BETWEEN quotes, never EXTRAPOLATE a wing
    (flat-copy or a linear extension both fabricate wing data we don't have). A bracket MAY span the ATM
    boundary (put quote below, call quote above): the OTM composite is ONE curve in K and both legs are
    its reliable halves (GLBX options are American-style, so parity is approximate — the halves can carry
    a small level offset that such a bridge blends; acceptable, and the point is marked anyway). Returns
    (iv[], interp[]) where interp[i] flags an interpolated (not-quoted) value, so the smile/table mark it."""
    n = len(strikes)
    if atm is None or n == 0:
        return [None] * n, [False] * n
    pIV, cIV = an["putIV"], an["callIV"]

    def _quote(i, k):   # ONLY the OTM leg (put K<ATM, call K>ATM); never the ITM leg (noisy -> outliers).
        # AT the ATM strike NEITHER leg is ITM, so prefer the put but fall back to a REAL call quote
        # (mirrors build_chain's atmIV) — discarding it would make the ATM card and table row disagree.
        if k < atm:
            return pIV[i]
        if k > atm:
            return cIV[i]
        return pIV[i] if pIV[i] is not None else cIV[i]

    q = [_quote(i, k) for i, k in enumerate(strikes)]
    out, interp = list(q), [False] * n
    for i in range(n):
        if out[i] is not None:            # a real quote (thin or not) -> shown as-is, never smoothed
            continue
        lo = next((j for j in range(i - 1, -1, -1) if q[j] is not None), None)
        hi = next((j for j in range(i + 1, n) if q[j] is not None), None)
        if lo is not None and hi is not None:   # fill ONLY a gap BRACKETED by real quotes (interpolate)
            t = (strikes[i] - strikes[lo]) / (strikes[hi] - strikes[lo]) if strikes[hi] != strikes[lo] else 0.5
            out[i] = round(q[lo] + t * (q[hi] - q[lo]), 2)
            interp[i] = True
        # else: an EDGE gap (unbracketed) -> leave None / n/a; we don't extrapolate (that would invent a wing)
    return out, interp


def read_chain_dir(d):
    """Cowork/MCP fallback: load a saved get_futures_options_smile result from a directory.
    The MCP tool returns the SAME Chain JSON as the connector's /smile/:root.json (shared
    snapshot builder). Accepts every envelope a saved tool result arrives in: a bare Chain,
    {structuredContent}, {content:[{text}...]}, {result: json-string}. File = name contains
    'smile' / 'chain' / 'futures_options'."""
    import glob

    def _unwrap(obj):
        if not isinstance(obj, dict):
            return None
        if "strikes" in obj:
            return obj
        sc = obj.get("structuredContent")
        if isinstance(sc, dict) and "strikes" in sc:
            return sc
        res = obj.get("result")
        if isinstance(res, str):
            try:
                r = json.loads(res)
                if isinstance(r, dict) and "strikes" in r:
                    return r
            except ValueError:
                pass
        for c in obj.get("content") or []:
            t = c.get("text") if isinstance(c, dict) else None
            if isinstance(t, str) and '"strikes"' in t:
                try:
                    r = json.loads(t)
                    if isinstance(r, dict) and "strikes" in r:
                        return r
                except ValueError:
                    continue
        return None

    for fp in sorted(glob.glob(os.path.join(d, "*"))):
        nm = os.path.basename(fp).lower()
        if not os.path.isfile(fp) or not any(k in nm for k in ("smile", "chain", "futures_options")):
            continue
        try:
            obj = json.load(open(fp, encoding="utf-8"))
        except (ValueError, OSError):
            continue
        ch = _unwrap(obj)
        if ch:
            return ch
    sys.exit(f"--from-dir {d}: no readable smile Chain (save the get_futures_options_smile result "
             f"to a file whose name contains 'smile'/'chain')")


# The Cowork-bridge mapper: a JS TWIN of chain_to_an + _smile_iv + connector_liveness for the
# blocks that tick (smile, OI bars, headline cards, badge). Greeks cards and the table need
# Black-76 -> they do NOT tick on the bridge (the page says so). Semantics ported verbatim:
# OTM leg only (put K<ATM, call K>ATM, at-ATM put else call); a gap is filled ONLY when
# bracketed by real quotes (linear); an edge gap stays null — never extrapolate a wing.
FO_BRIDGE_JS = r"""function foBridgeDelta(c, sp){
  if(!c || !c.strikes) return null;
  const oi=x=>x?Math.round(Number(x)):0;
  const S=c.strikes, atm=c.atmStrike;
  let idx=[]; for(let i=0;i<S.length;i++){ if(oi((c.callOI||[])[i])>0 || oi((c.putOI||[])[i])>0) idx.push(i); }
  if(!idx.length) idx=S.map((_,i)=>i);
  const K=idx.map(i=>S[i]);
  const pct=v=>v!=null?Math.round(v*10000)/100:null;
  const pIV=idx.map(i=>pct((c.putIV||[])[i])), cIV=idx.map(i=>pct((c.callIV||[])[i]));
  const cOI=idx.map(i=>oi((c.callOI||[])[i])), pOI=idx.map(i=>oi((c.putOI||[])[i]));
  const n=K.length, q=[];
  for(let i=0;i<n;i++){ const k=K[i];
    q.push(atm==null?null:(k<atm?pIV[i]:(k>atm?cIV[i]:(pIV[i]!=null?pIV[i]:cIV[i])))); }
  const out=q.slice(), interp=new Array(n).fill(false);
  for(let i=0;i<n;i++){ if(out[i]!=null) continue;
    let lo=-1, hi=-1;
    for(let j=i-1;j>=0;j--){ if(q[j]!=null){ lo=j; break; } }
    for(let j=i+1;j<n;j++){ if(q[j]!=null){ hi=j; break; } }
    if(lo>=0&&hi>=0){ const t=K[hi]!==K[lo]?(K[i]-K[lo])/(K[hi]-K[lo]):0.5;
      out[i]=Math.round((q[lo]+t*(q[hi]-q[lo]))*100)/100; interp[i]=true; } }
    // else: edge gap -> null; never extrapolate a wing
  const putS=K.map((k,i)=>(atm!=null&&k<=atm)?out[i]:null);
  const callS=K.map((k,i)=>(atm!=null&&k>=atm)?out[i]:null);
  let mode="settlement", detail="";
  if(c.asOf){ const age=(Date.now()-Date.parse(c.asOf))/1000;
    if(isFinite(age)){ if(age>-5&&age<120){ mode="tick"; detail=Math.max(0,Math.round(age))+"s data age"; }
      else if(age>=120){ mode="delayed"; detail=(age>=3600?Math.round(age/3600)+"h":Math.round(age/60)+"m")+" old"; }
      else { mode="delayed"; detail="clock skew"; } } }
  const num=(v,d)=>{ if(v==null) return "n/a"; const ax=Math.abs(v);
    const dd=(d!=null)?d:(ax>=100?0:(ax>=10?1:(ax>=1?2:3)));
    return Number(v).toLocaleString("en-US",{minimumFractionDigits:dd,maximumFractionDigits:dd}); };
  const cards={0:num(c.spot), 1:(c.atmIV!=null?(c.atmIV*100).toFixed(1)+"%":"n/a"),
    2:(c.skew25!=null?((c.skew25>=0?"+":"")+(c.skew25*100).toFixed(1)):"n/a"),
    3:num(c.pcrOI,2), 4:num(c.maxPain), 5:String(K.length)};
  return {badge:{mode:mode, ts:String(c.asOf||"").slice(11,19), detail:detail},
    cards:cards,
    panels:{0:{strikes:K, atm:atm, putIV:putS, callIV:callS, interp:interp, markers:{maxPain:c.maxPain}},
            1:{labels:K, markers:{maxPain:c.maxPain}, series:[{name:"Call OI",color:"#2a78d6",values:cOI},{name:"Put OI",color:"#eb6834",values:pOI}]}}};
}"""


def to_spec(an):
    """The analysis -> a dashboard SPEC for dashboard.py (cards + OTM smile + OI bars + chain table)."""
    strikes = an["strikes"]
    atm = an["atmStrike"]
    # HONEST strike-set label (three states, keyed off provenance — never claim "full ladder" when it isn't):
    #   full      = the direct Databento pull (every OI-bearing strike)   -> "full tradeable ladder"
    #   window    = the connector /smile page (±N strikes around ATM)      -> "±N strikes around ATM"
    #   coarsened = the connector /term pull (a capped near-money grid)    -> "near-money grid · N strikes"
    _lk = an.get("ladder", "full")
    _win = an.get("window")
    if _lk == "window":
        _lad_tab = (f"±{_win} strikes around ATM" if _win else "near-money window")
        _lad_smile = (f"±{_win} window" if _win else "near-money window")
        _strk_hint = (f"A ±{_win}-strike window centred on ATM (the connector's live display window), "
                      "OI-filtered — fewer than the full listed ladder." if _win else
                      "A near-the-money window (the connector's live display window), OI-filtered.")
    elif _lk == "coarsened":
        _lad_tab = "near-money grid"   # count-less: panel titles are STATIC while the switcher swaps data;
        _lad_smile = "near-money grid"  # the per-series count lives on the Strikes card, which does swap
        _strk_hint = ("A coarsened near-the-money grid — the connector caps strikes per series in the term "
                      "pull for a fast load, so this is fewer strikes than the full listed ladder.")
    else:   # full
        _lad_tab = "full tradeable ladder"
        _lad_smile = "full ladder"
        _strk_hint = ("Tradeable strikes shown: every strike with open interest on either side — the full "
                      "ladder, not a fixed window around ATM.")
    # ATM greeks (Black-76 from the ATM IV) — the "magnitude at the money" block. Γ per $1 of underlying;
    # vega per 1 vol point (/100); Θ per calendar day (/365). Per UNIT of underlying — multiply by the
    # contract size for per-contract $. Stable 3-card structure ("n/a" when the ATM IV is unsolvable).
    g = _greeks_at(an, atm, an["atmIV"], True)   # Γ/vega/Θ are call==put; is_call is immaterial here
    iv_pre, _ = _smile_iv(an, strikes, atm)      # the SAME composite IV feeds Δ/Γ/GEX below
    gex = gex_ladder(an, iv_pre)
    flip = gamma_flip(an, iv_pre)
    known = [v for v in gex if v is not None]
    net_gex = sum(known) if known else None
    gex_approx = "≈" if (known and any(v is None for v in gex)) else ""   # holes -> a partial (marked) sum
    cards = [
        # v1.129.1: the sub NAMES the per-series underlying month (a bare 86.8 -> 78.6 swap never
        # said WHOSE forward it became); no under known (connector /smile) -> no sub, never a guess.
        {"label": "Forward", "value": _num(an["forward"]),
         **({"sub": [f"{an['under']} settlement"]} if an.get("under") else {}),
         "hint": "The underlying FUTURES price of THIS series' own month — options on futures price off "
                 "the future, not spot, and a series swap changes the future with it. A 'fwd≈' note in "
                 "the subtitle means it is an estimated fallback (verify the month)."},
        {"label": "ATM IV", "value": _ivfmt(an["atmIV"]),
         "hint": "Implied volatility at the at-the-money strike (Black-76, annualized) — the single number "
                 "the market quotes 'vol' at for this expiry."},
        {"label": "25Δ skew", "value": ("n/a" if an["skew25"] is None else f"{an['skew25']:+.1f}"),
         "hint": "Put IV minus call IV at ~10% OTM (a 25Δ proxy, vol points — moneyness-based, not true "
                 "delta buckets). Positive = puts richer (downside protection bid); negative = call wing bid."},
        {"label": "PCR (OI)", "value": _num(an["pcrOI"], 2),
         "hint": "Put/call ratio by open interest over the whole ladder. >1 = more puts outstanding; hedging "
                 "flows dominate it, so read positioning, not direction."},
        {"label": "Max pain", "value": _num(an["maxPain"]),
         "hint": "The strike minimizing the total intrinsic payout of all open options at expiry — where "
                 "option WRITERS lose least. A pin-gravity heuristic near expiry, not a forecast."},
        {"label": "Strikes", "value": str(an["nStrikes"]), "hint": _strk_hint},
        # v1.82.0 — placed AFTER the fixed 0..5 block: FO_BRIDGE_JS (the Cowork artifact) updates
        # cards 0..5 BY INDEX and the DTE tile must stay LAST; inserting inside 0..5 would desync both.
        {"label": "Exp move", "value": ("n/a" if an.get("expMove") is None else
                                        f"±{_num(an['expMove'])} (±{_num(an['expMovePct'], 1)}%)"),
         "hint": "The 1σ move priced in by this expiry: forward × ATM IV × √(DTE/365) — arithmetic on "
                 "the Black-76 ATM IV shown above. ~68% of outcomes land inside the band if the IV is "
                 "'right'; a straddle breaks even near its edges. In underlying points."},
        {"label": "Γ (ATM)", "value": _num(g.gamma, 3) if g else "n/a",
         "hint": "Gamma at the ATM strike: how much Δ changes per 1-point underlying move, per unit "
                 "(× contract multiplier for per-contract). Peaks at the money and grows into expiry."},
        {"label": "Vega (ATM/1%)", "value": _num(g.vega / 100.0, 4) if g else "n/a",
         "hint": "ATM option price change per 1 vol-point IV move, per underlying unit (× contract "
                 "multiplier for $)."},
        {"label": "Θ (ATM/day)", "value": _num(g.theta / 365.0, 4) if g else "n/a",
         "hint": "ATM daily time decay, per underlying unit (× contract multiplier for $). Accelerates "
                 "as expiry approaches."},
        {"label": "Net GEX (Δ/pt)", "value": ("n/a" if net_gex is None else gex_approx + _num(net_gex)),
         "hint": "Σ Γ×netOI across strikes, in Δ-contracts per 1-pt move — dealer hedge flow under the "
                 "naive assumption (dealers long calls / short puts; real positioning is not observable "
                 "from OI). Positive = dealers dampen moves; negative = amplify. '≈' = strikes without "
                 "solvable IV excluded."},
        {"label": "Γ flip", "value": _num(flip),
         "hint": "The spot level where net dealer gamma crosses zero (sticky-strike sweep: each strike "
                 "keeps its IV as spot moves). Above it hedging dampens moves, below it amplifies — same "
                 "naive positioning assumption as Net GEX."},
        {"label": "DTE", "value": str(an["dte"]),
         "hint": "Calendar days to this series' expiration. Greeks/decay are quoted per calendar day; "
                 "cross-expiration context lives in --term."},
    ]
    # Per-strike IV = the liquidity-anchored smile (real quotes on liquid strikes, interpolated on thin
    # ones — see _smile_iv). The SMILE splits it into two coloured halves; the TABLE shows the same value
    # and marks an interpolated (not-quoted) one with a leading "~".
    iv_col, iv_interp = _smile_iv(an, strikes, atm)
    smile_put = [iv_col[i] if (atm is not None and k <= atm) else None for i, k in enumerate(strikes)]
    smile_call = [iv_col[i] if (atm is not None and k >= atm) else None for i, k in enumerate(strikes)]

    def _ivcell(i):
        s = _ivfmt(iv_col[i])
        return "~" + s if (iv_interp[i] and iv_col[i] is not None) else s

    # Δ column: the OTM leg's delta (put K<=ATM -> negative, call K>ATM -> positive), from the SAME IV.
    # Sign follows the wing; the trader reads the strike off its delta (e.g. the 25Δ put).
    dg = [_greeks_at(an, k, iv_col[i], k > atm) if atm is not None else None for i, k in enumerate(strikes)]
    delta_col = [(_num(x.delta, 2) if x is not None else "n/a") for x in dg]
    gamma_col = [(f"{x.gamma:.4g}" if x is not None else "n/a") for x in dg]
    rows = [[_num(k), _ivcell(i), delta_col[i], gamma_col[i], _num(an["callOI"][i], 0), _num(an["putOI"][i], 0)]
            for i, k in enumerate(strikes)]
    # Heat map: shade each row by distance from ATM (brightest at the money, fading to the wings) so the
    # eye lands on the central strike; the ATM row is also bold. Rank distance -> one step fainter per row.
    ai = strikes.index(atm) if atm in strikes else None

    def _rowstyle(i):
        # Heat concentrated on the ±10-strike core around ATM: a STRONG saturated-but-dark blue (stays
        # dark enough at high alpha to keep light-text contrast) peaks at the money and fades to plain
        # rows past the band, so the illiquid wings stay crisp. ATM steps up + bold as an explicit centre.
        if ai is None or abs(i - ai) > 10:
            return ""
        if i == ai:
            return "background:rgba(74,134,240,0.60);font-weight:600"
        a = 0.46 * (1 - abs(i - ai) / 10)
        return f"background:rgba(74,134,240,{a:.3f})" if a >= 0.001 else ""   # band edge: no-op -> plain

    rowstyle = [_rowstyle(i) for i in range(len(strikes))]
    # Forward provenance (plan B6): a non-underlying forward (continuous front in a roll window can be a
    # DIFFERENT contract month; mid-strike is a guess) is marked IN the artifact, never silent.
    fwd_note = {"continuous": " · fwd≈continuous front (verify month)",
                "mid-strike": " · fwd≈mid strike (no forward data)"}.get(an.get("fwdSrc") or "", "")
    return {
        "about": _about(an),
        "title": f"{an['symbol']} ({an['optionRoot']}) options · {an['expiration']} · {an['dte']} DTE",
        "subtitle": (f"Databento GLBX.MDP3 · settlement · Black-76 · {an['nStrikes']} strikes · "
                     f"not advice · [settlement — no live ticks]{fwd_note}"),
        "cards": cards,
        "panels": [
            {"type": "smile", "title": f"Vol smile (OTM composite · {_lad_smile})", "info":
                "OTM composite: put IV below ATM, call above — the liquid legs (ITM is never "
                "parity-substituted). Grey point = interpolated between neighbours ('~' in the table); "
                "edge gaps stay empty (never extrapolate a wing). Red dashes = max pain.", "data": {
                "strikes": strikes, "atm": atm, "putIV": smile_put, "callIV": smile_call,
                "interp": iv_interp, "markers": {"maxPain": an["maxPain"]}}},
            {"type": "bars", "title": "Open interest by strike", "info":
                "Open contracts per strike — positioning, not volume. Walls = hedging concentration / "
                "pin candidates into expiry; red dashes = max pain. OI ≠ quotability.", "data": {
                "labels": strikes, "markers": {"maxPain": an["maxPain"]},
                "series": [{"name": "Call OI", "color": "#2a78d6", "values": an["callOI"]},
                           {"name": "Put OI", "color": "#eb6834", "values": an["putOI"]}]}},
            {"type": "bars", "title": "Dealer gamma exposure (GEX) by strike", "info":
                "Γ × net OI per strike, in Δ-contracts/pt (× contract multiplier for $). Sign = the naive "
                "dealer assumption (long calls / short puts — real positioning isn't observable). Γ flip = "
                "sticky-strike zero crossing. No-IV strikes excluded (n/a, sum marked ≈).",
             "data": {
                "labels": strikes, "markers": {"gammaFlip": flip},
                "series": [{"name": "GEX (Δ/pt)", "values": gex, "colorBySign": True}]}},
            {"type": "table", "title": f"Chain ({_lad_tab})", "info":
                "One row per tradeable strike. IV = the same OTM composite as the smile ('~' = "
                "interpolated between quoted neighbours; 'n/a' = no reliable quote — never fabricated). "
                "Δ is signed by wing (puts negative below ATM, calls positive above); Γ uses the same IV "
                "and is call≡put by Black-76 parity. Blue heat = proximity to ATM; bold = the ATM row.", "data": {
                "columns": ["Strike", "IV", "Δ (OTM)", "Γ", "Call OI", "Put OI"], "rows": rows,
                "rowstyle": rowstyle}},
        ],
    }


def render_html(an):
    return dashboard.render_interactive_html(to_spec(an))


def _gex_text(an):
    iv_col, _ = _smile_iv(an, an["strikes"], an["atmStrike"])
    gex = gex_ladder(an, iv_col)
    known = [v for v in gex if v is not None]
    net = ("n/a" if not known else ("≈" if any(v is None for v in gex) else "") + _num(sum(known)))
    return (f"net GEX {net} Δ/pt (naive dealer: long calls/short puts) · "
            f"Γ flip {_num(gamma_flip(an, iv_col))} (sticky-strike)")


def render_text(an):
    skew = "n/a" if an["skew25"] is None else f"{an['skew25']:+.1f}"
    fwd_note = {"continuous": " · fwd≈continuous front (verify month)",
                "mid-strike": " · fwd≈mid strike (no forward data)"}.get(an.get("fwdSrc") or "", "")
    L = [f"{an['symbol']} ({an['optionRoot']}) OPTIONS · exp {an['expiration']} ({an['dte']} DTE) · "
         f"fwd {_num(an['forward'])}{fwd_note}",
         f"ATM IV {_ivfmt(an['atmIV'])} · "
         + ("exp move n/a · " if an.get("expMove") is None else
            f"exp move ±{_num(an['expMove'])} (±{_num(an['expMovePct'], 1)}%) · ")
         + f"25Δ skew {skew} · PCR(OI) {_num(an['pcrOI'], 2)} · "
         f"max pain {_num(an['maxPain'])}",
         _gex_text(an),
         f"{an['nStrikes']} tradeable strikes · settlement-priced (illiquid metals have no live ticks)",
         f"{'STRIKE':>8}{'CALL_IV':>9}{'CALL_OI':>9}  |{'PUT_IV':>9}{'PUT_OI':>9}"]
    for i, k in enumerate(an["strikes"]):
        mark = "  <ATM" if k == an["atmStrike"] else ("  <pain" if k == an["maxPain"] else "")
        L.append(f"{_num(k):>8}{_ivfmt(an['callIV'][i]):>9}{_num(an['callOI'][i], 0):>9}  |"
                 f"{_ivfmt(an['putIV'][i]):>9}{_num(an['putOI'][i], 0):>9}{mark}")
    return "\n".join(L)


# ----------------- HTTP (live; not in the offline gate) -----------------
UNDEF_PRICE = 9223372036854775807  # Databento INT64 UNDEF_PRICE sentinel (raw, before /1e9)
UNDEF_QTY = 2147483647             # V2 INT32 UNDEF_STAT_QUANTITY sentinel


def _px(raw):
    """A Databento fixed-point price cell -> float, or None for missing / <=0 / the UNDEF sentinel."""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if not (v > 0) or v >= UNDEF_PRICE:  # rejects 0, negatives, NaN, and INT64_MAX
        return None
    return v / 1e9


def _oi(cell):
    """OI from a (price, quantity) statistics cell -> int; the INT32 sentinel / junk -> 0."""
    if not cell:
        return 0
    try:
        n = int(cell[1])
    except (TypeError, ValueError):
        return 0
    return 0 if (n < 0 or n == UNDEF_QTY) else n


def _daycache_dir():
    """Resolve the direct-path day-cache dir (v1.128 slice A). `MARKET_DATA_FUT_DAYCACHE` = a dir
    override, or 0/off/no/false (case-insensitive, the v1.126 lesson) to disable; default
    ~/.cache/market-data/futures_daycache. None = disabled."""
    v = os.environ.get("MARKET_DATA_FUT_DAYCACHE", "").strip()
    if v.lower() in ("0", "off", "no", "false"):
        return None
    if v.lower() in ("1", "on", "yes", "true"):   # review P3: truthy = DEFAULT-ON, never a "./1" junk dir
        v = ""
    if v:
        return os.path.abspath(os.path.expanduser(v))
    return os.path.join(os.path.expanduser("~"), ".cache", "market-data", "futures_daycache")


def _daycache_path(params):
    """(cache_dir, file path) for a request-params dict — today-prefixed so lookups NEVER serve
    yesterday's file, and the sweep can date-judge leftovers by name alone. Key = the FULL request
    incl. the frontier-CLAMPED end: a frontier advance intra-day changes the key and re-pulls."""
    d = _daycache_dir()
    if not d:
        return None, None
    import hashlib
    h = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:16]
    return d, os.path.join(d, "%s-%s.csv.gz" % (dt.date.today().strftime("%Y%m%d"), h))


_DAYCACHE_NOTED = False


def _daycache_get(params):
    """Today's cached body for these request params, else None. Corrupt file -> unlink + miss."""
    global _DAYCACHE_NOTED
    _, path = _daycache_path(params)
    if not path:
        return None
    import gzip
    import zlib
    try:
        with open(path, "rb") as f:
            body = gzip.decompress(f.read()).decode("utf-8")
    except FileNotFoundError:
        return None
    except (OSError, ValueError, EOFError, zlib.error):
        # review P2: a truncated gzip raises EOFError and a deep bit-flip zlib.error — neither
        # is an OSError; without them here a poisoned file CRASHED every same-day run of that
        # request instead of healing (unlink -> miss -> refetch -> re-cache).
        try:
            os.unlink(path)
        except OSError:
            pass
        return None
    if not _DAYCACHE_NOTED:
        _DAYCACHE_NOTED = True
        print("day-cache: serving repeated same-day Databento pulls from %s "
              "(MARKET_DATA_FUT_DAYCACHE=0 disables)" % os.path.dirname(path), file=sys.stderr)
    return body


def _daycache_put(params, body):
    """Best-effort write-through (mkstemp + atomic replace — concurrent writers race benignly on
    identical content) + a sweep of files dated before yesterday (big roots leave ~25MB/day of
    gzip'd stats; two days retained so midnight-spanning runs keep their morning pulls)."""
    d, path = _daycache_path(params)
    if not path:
        return
    import gzip
    import tempfile
    cutoff = (dt.date.today() - dt.timedelta(days=1)).strftime("%Y%m%d")
    try:
        os.makedirs(d, exist_ok=True)
        for fn in os.listdir(d):
            if fn.endswith(".csv.gz") and fn[:8].isdigit() and fn[:8] < cutoff:
                try:
                    os.unlink(os.path.join(d, fn))
                except OSError:
                    pass
            elif fn.endswith(".tmp"):   # review P3: a SIGKILL between mkstemp and replace orphans
                try:                    # a .tmp forever — sweep >1h-old ones (write_atomic's rule)
                    if os.path.getmtime(os.path.join(d, fn)) < time.time() - 3600:
                        os.unlink(os.path.join(d, fn))
                except OSError:
                    pass
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(gzip.compress(body.encode("utf-8")))
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError:
        pass                                        # a read-only/full disk never kills the pull


def _make_fetch(dataset=DATASET, env_file=None, daycache=True):
    """The REAL Databento Historical fetch closure (Basic-auth header, frontier clamp, transient retry) —
    shared by gather() and --list-expiries so both hit the API identically. v1.128: responses ride a
    same-day on-disk DAY-CACHE (_daycache_*) — the connector's day-cache equivalent for the direct
    path, so --list-expiries -> --full -> a re-run in one session pays the multi-minute whole-root
    defs/stats bill ONCE (the CL cloud-session post-mortem); errors are never cached.
    `daycache=False` (re-review P3): the settlement --watch poll path — with the v1.129 DATE-level
    key an evening poll would otherwise serve the morning body until midnight (the settle
    publication no longer steps the key), so the poll bypasses the cache both ways."""
    auth_b64 = base64.b64encode(f"{chart.get_key('DATABENTO_API_KEY', env_file)}:".encode()).decode()
    hdr = {"Authorization": "Basic " + auth_b64}
    frontier = {}

    def fetch(schema, symbols, stype_in, start, endd):
        if schema not in frontier:
            try:                                    # tolerate a flaky metadata call (retry-then-None)
                frontier[schema] = chart.databento_frontier(dataset, schema, auth_b64)
            except Exception:
                frontier[schema] = None
        e = min(endd, frontier[schema]) if frontier[schema] else endd  # clamp to available_end (422 guard)
        # map_symbols (v1.129 F5, probe-verified 2026-07-23): the sync CSV gains a trailing `symbol`
        # column — the batched settle pull groups by it; named-field parsers ignore it elsewhere.
        p = {"dataset": dataset, "symbols": symbols, "schema": schema, "stype_in": stype_in,
             "stype_out": "instrument_id", "encoding": "csv", "map_symbols": "true",
             "start": start, "end": e}
        url = "https://hist.databento.com/v0/timeseries.get_range?" + urllib.parse.urlencode(p)
        # v1.129 F1 (the cloud CL forensics): the cache KEY is DATE-level — `end` in the key is the
        # UNCLAMPED request end, while the clamp stays in the URL. GLBX available_end steps ~hourly
        # per schema, and with the clamp in the key one step between two same-day runs invalidated
        # the whole cache (the cloud retry re-paid the 2.5-min defs pull). Same-day semantics:
        # settlement analytics gain nothing from an intra-day frontier advance — the data is EOD;
        # the date prefix in the filename still expires everything at midnight.
        pkey = dict(p, end=endd)
        body = _daycache_get(pkey) if daycache else None
        if body is None:
            # chart.http_get is the SINGLE retry authority (transient 5xx/network/read-timeout with backoff;
            # a 4xx raises at once). A local range(3) loop here used to COMPOUND with it — up to 9 requests
            # per call — and, worse, never caught a bare read TimeoutError (an OSError, not a URLError), the
            # exact high-variance OH.OPT flake this path hits (review 2026-07-15).
            body = chart.http_get(url, hdr, gzip=True)   # v1.128: the futhist twin rule — these are
            # the same payload class as futhist's statistics pulls (a plain LO stream ran >7 min live)
            if daycache:
                _daycache_put(pkey, body)
        return list(csv.DictReader(io.StringIO(body)))
    return fetch


def _defs_snapshot_enabled():
    """The v1.129 F4 kill-switch: MARKET_DATA_FUT_DEFS_SNAPSHOT ∈ 0/off/no/false disables the
    defs snapshot (reader AND write-through); default ON. Tests pin 0 — the snapshot is an
    ambient channel through the real futures store (the day-cache hermeticity rule)."""
    return os.environ.get("MARKET_DATA_FUT_DEFS_SNAPSHOT", "").strip().lower() \
        not in ("0", "off", "no", "false")


def _defs_ttl_days():
    try:
        return max(1, int(os.environ.get("MARKET_DATA_FUT_DEFS_TTL_DAYS", "5")))
    except ValueError:
        return 5


def _defs_snapshot_path(opt):
    """<futures-store>/defs/<OPT>.json.gz — resolved per call via futhist's ladder (env-sensitive,
    triggers the shipped store.tgz unpack), so the snapshot RIDES THE ZIP like the vol store: a
    fresh cloud VM starts with a days-old listing instead of the ~2.5-minute whole-root defs pull
    (THE dominant cold cost in the CL cloud forensics 2026-07-23)."""
    import futhist   # lazy: futhist imports this module at its top — module-level would be circular
    return os.path.join(futhist._resolve_store_dir(), "defs", "%s.json.gz" % opt)


def _defs_snapshot_load(opt, today):
    """(series dict, asOf) from a fresh-enough snapshot, else None. A definitions listing is
    slow-moving (new series ~monthly, new strikes near the money ~daily and near-zero-OI when
    born), so a ≤TTL-days-old listing is analytically sound; expired series drop out via the
    exp_ns filter regardless. Corrupt/stale/future-dated -> None (the live path heals it)."""
    if not _defs_snapshot_enabled():
        return None
    import gzip
    try:
        try:
            with open(_defs_snapshot_path(opt), "rb") as f:
                raw = f.read()
        except OSError:
            import opthist  # lazy: the history module already owns the hardened tar reader
            raw = opthist._load_packaged_store_bytes(
                "futures/defs/%s.json.gz" % opt.upper(), max_member=32 * 1024 * 1024
            )
        if raw is None:
            return None
        snap = json.loads(gzip.decompress(raw).decode("utf-8"))
        age = (today - dt.date.fromisoformat(snap["asOf"])).days
        if age > _defs_ttl_days() or age < 0:
            return None
        series = {}
        for s in snap["series"]:
            series[(s["stem"], s["exp_raw"])] = {
                "stem": s["stem"], "exp_ns": s["exp_ns"], "mc": s["mc"], "yd": s["yd"],
                "under": s["under"], "lad": {i: (k, bool(c)) for i, k, c in s["lad"]}}
        return series, snap["asOf"]
    except Exception:
        return None


def _defs_snapshot_save(opt, today, series):
    """Best-effort write-through after a LIVE listing (mkstemp + atomic replace). The store leaf
    rides prewarm_sync's rsync and package.sh's store.tgz, which is what carries it into cloud."""
    if not _defs_snapshot_enabled():
        return
    import gzip
    import tempfile
    path = _defs_snapshot_path(opt)
    try:
        payload = {"asOf": today.isoformat(), "root": opt, "series": [
            {"stem": v["stem"], "exp_raw": k[1], "exp_ns": v["exp_ns"], "mc": v["mc"],
             "yd": v["yd"], "under": v["under"],
             "lad": [[i, kk, cc] for i, (kk, cc) in v["lad"].items()]}
            for k, v in series.items()]}
        os.makedirs(os.path.dirname(path), exist_ok=True)
        for fn in os.listdir(os.path.dirname(path)):   # re-review P3: defs/ SHIPS (store.tgz +
            if fn.endswith(".tmp"):                    # prewarm_sync rsync) — sweep >1h .tmp orphans
                try:                                   # or they propagate into every zip/snapshot
                    fp = os.path.join(os.path.dirname(path), fn)
                    if os.path.getmtime(fp) < time.time() - 3600:
                        os.unlink(fp)
                except OSError:
                    pass
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(gzip.compress(json.dumps(payload).encode("utf-8")))
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError:
        pass                                        # a read-only store never kills the listing


def _list_series(root, fetch, today, option_root=None, fresh=False):
    """The DEFINITION listing only (cheap metadata — NO market-data pulls): {(stem, exp): series}.
    Keyed on (stem, expiration) because stem uniqueness is an exchange CONVENTION, not an invariant —
    two listings sharing a stem must never merge. Raises RuntimeError (actionable, lists known roots)
    when the parent listing itself is rejected — an unknown/mistyped root or an unmapped option code.
    v1.129 F4: a fresh-enough defs SNAPSHOT (see _defs_snapshot_load) serves the listing with ZERO
    network; a LIVE listing write-throughs the snapshot. `fresh=True` (--fresh-defs) forces live."""
    opt = (option_root or resolve_option_root(root)).upper()
    if not fresh:
        snap = _defs_snapshot_load(opt, today)
        if snap is not None:
            series, as_of = snap
            print(f"note: {opt}.OPT definition listing served from the local defs snapshot "
                  f"(asOf {as_of}, TTL {_defs_ttl_days()}d — strikes/series listed since then are "
                  f"absent; --fresh-defs forces a live pull)", file=sys.stderr)
            return series
    if option_root is None and root.upper() not in OPTION_ROOTS:
        print(f"warning: root '{root.upper()}' is not in the option-root table — probing '{opt}.OPT' as a "
              f"guess (pass --option-root CODE if the option asset differs, e.g. Brent BZ -> BZO)",
              file=sys.stderr)
    end = (today + dt.timedelta(days=1)).isoformat()
    stem_re = re.compile(rf"^{re.escape(opt)}([FGHJKMNQUVXZ])(\d{{1,2}})$")
    series = {}
    try:
        # Closed-day lookback (the Python twin of the connector's dfe5591 fix): a Saturday after a
        # Friday holiday leaves [today-1, today+1) with NO definition records. A per-day walk is the
        # obvious fix but each parent pull costs ~40-60s server-side on big roots (GC: 37k defs, and
        # an EMPTY closed-day pull is just as slow) — so fall back to ONE wide 7-day window instead:
        # it spans any exchange holiday run, and duplicate records across days collapse in the parser
        # (same instrument_ids overwrite into the same series entries).
        rows = fetch("definition", opt + ".OPT", "parent",
                     (today - dt.timedelta(days=1)).isoformat(), end)
        if not rows:
            rows = fetch("definition", opt + ".OPT", "parent",
                         (today - dt.timedelta(days=7)).isoformat(), end)
    except urllib.error.HTTPError as e:
        if e.code in (400, 422):
            raise RuntimeError(
                f"error: no Databento options listing for '{root.upper()}' (HTTP {e.code} on {opt}.OPT — "
                f"unknown/mistyped root, or an unmapped option code; try --option-root). Known roots: "
                f"{', '.join(sorted(OPTION_ROOTS))}") from None
        if e.code in (401, 403):
            raise RuntimeError(f"error: Databento rejected credentials/entitlement for {opt}.OPT (HTTP {e.code})") from None
        if e.code == 429:
            raise RuntimeError(f"error: Databento throttled the definition request for {opt}.OPT (HTTP 429)") from None
        if e.code >= 500:
            raise RuntimeError(f"error: Databento definition service failed for {opt}.OPT (HTTP {e.code}); retry later") from None
        raise RuntimeError(f"error: Databento rejected the definition request for {opt}.OPT (HTTP {e.code})") from None
    for d in rows or []:
        rs, cls = d.get("raw_symbol", ""), d.get("instrument_class")
        if cls not in ("C", "P") or " " not in rs:
            continue
        m = stem_re.match(rs.split(" ", 1)[0])
        if not m:                       # weekly / EOM / not this root's {opt}{month}{year} series
            continue
        exp_raw = str(d.get("expiration") or "")
        ent = series.setdefault((m.group(0), exp_raw),
                                {"stem": m.group(0), "exp_ns": None, "lad": {}, "mc": m.group(1),
                                 "yd": m.group(2), "under": None})
        try:
            ent["lad"][d["instrument_id"]] = (float(d["strike_price"]) / 1e9, cls == "C")
        except (TypeError, ValueError, KeyError):
            continue
        if ent["exp_ns"] is None:
            try:
                ent["exp_ns"] = int(exp_raw)
            except (TypeError, ValueError):
                pass
        if not ent["under"]:            # the EXACT underlying future symbol (authoritative)
            ent["under"] = (d.get("underlying") or "").strip() or None
    if series:
        _defs_snapshot_save(opt, today, series)   # v1.129 F4: write-through (best-effort)
    return series


def series_lines(series, today=None):
    """--list-expiries formatter: one line per listed series — the validate-before-invoke pre-check an
    agent runs to confirm the root and pick --expiry deterministically."""
    today = today or dt.date.today()
    out = []
    for ent in sorted(series.values(), key=lambda v: (v["exp_ns"] or (1 << 62), v["stem"])):
        if ent["exp_ns"]:
            d = dt.datetime.fromtimestamp(ent["exp_ns"] / 1e9, dt.timezone.utc).date()
            exps, dte = d.isoformat(), (d - today).days
        else:
            exps, dte = "?", "?"
        kind = "quarterly" if ent["mc"] in "HMUZ" else "serial"
        nk = len({k for k, _ in ent["lad"].values()})
        out.append(f"{ent['stem']} · {exps} · {dte} DTE · {nk} strikes · {kind} · underlying {ent['under'] or '?'}")
    return out


# --- parse-time validators + ignored-flag warnings (docs/command-layer-plan.md §0.5) ---
def _arg_date(s):
    try:
        dt.date.fromisoformat(s)
        return s
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{s}' is not YYYY-MM-DD") from None


def _arg_watch(s):
    v = int(s)
    if not 2 <= v <= 3600:
        raise argparse.ArgumentTypeError("--watch seconds must be within [2, 3600]")
    return v


def _arg_port(s):
    v = int(s)
    if not 1024 <= v <= 65535:
        raise argparse.ArgumentTypeError("port must be within [1024, 65535]")
    return v


_URL_DEFAULT = os.environ.get("SMILE_WEB_URL", "http://127.0.0.1:8768")


def _warn_ignored(a):
    """Flags that don't apply to the chosen mode — warn instead of silently swallowing them."""
    w = []
    if a.watch is None:
        if a.port != 8788:
            w.append("warning: --port is ignored without --watch")
        if getattr(a, "open", False):
            w.append("warning: --open is ignored without --watch")
    if a.source == "databento" and a.url != _URL_DEFAULT:
        w.append("warning: --url is ignored with --source databento (it's the connector's address)")
    if getattr(a, "liquidity", False) and not getattr(a, "list_series", False):
        w.append("warning: --liquidity only applies to --list-series — ignored")
    if getattr(a, "list_series", False):   # §0.5: the listing ignores chain-shaping flags — say so
        if a.expiry:
            w.append("warning: --expiry is ignored with --list-series (it LISTS the series; pick one after)")
        if a.option_root:
            w.append("warning: --option-root is ignored with --list-series (the connector resolves the root)")
        if a.source == "databento":
            w.append("warning: --list-series always uses the connector — with --source databento use "
                     "--list-expiries (the Databento-direct listing)")
    return w


def _pick_target(series, expiry, today, root):
    """The ONE series-selection rule (shared by gather and gather_term): --expiry = nearest listed
    series within ±7 DATE days (tie -> quarterly, bigger ladder, stem); default = nearest FUTURE
    quarterly (fallbacks: any future, any). Raises with the full listing when out of tolerance."""
    today_ns = int(dt.datetime.combine(today, dt.time(), dt.timezone.utc).timestamp() * 1e9)
    def _prefer(keys):  # deterministic tie-break: quarterly first, then the bigger distinct strike ladder, then stem
        return min(keys, key=lambda k: (series[k]["mc"] not in "HMUZ",
                                        -len({K for K, _ in series[k]["lad"].values()}),
                                        series[k]["stem"]))

    if expiry:  # an explicit expiration date -> the listed series expiring nearest it, WITHIN TOLERANCE
        try:
            want = dt.date.fromisoformat(expiry)
        except ValueError:
            raise ValueError(f"--expiry '{expiry}' is not a date (use YYYY-MM-DD)") from None
        gaps = {}
        for k, v in series.items():   # whole DATE days: the help text and --list-expiries speak in
            d = (dt.datetime.fromtimestamp(v["exp_ns"] / 1e9, dt.timezone.utc).date()
                 if v["exp_ns"] else None)   # dates, and real expirations carry ~20:00Z times
            gaps[k] = abs((d - want).days) if d else (1 << 31)
        best = min(gaps.values())
        if best > 7:    # no listed series near the asked date -> error WITH the full series listing
            nk = min(gaps, key=gaps.get)
            nd = (dt.datetime.fromtimestamp(series[nk]["exp_ns"] / 1e9, dt.timezone.utc).date().isoformat()
                  if series[nk]["exp_ns"] else "?")
            lines = [ln + ("   <- nearest" if ln.startswith(f"{series[nk]['stem']} · {nd}") else "")
                     for ln in series_lines(series, today)]
            raise RuntimeError(f"error: no listed {root.upper()} option series within 7 days of {expiry}. "
                               f"Listed series (pick one with --expiry, or drop --expiry for the nearest "
                               f"quarterly):\n  " + "\n  ".join(lines))
        target = _prefer([k for k, g in gaps.items() if g == best])
    else:       # default: nearest FUTURE expiration among quarterly months (fallbacks: any future, any)
        future = [k for k, v in series.items() if v["exp_ns"] and v["exp_ns"] >= today_ns]
        quarterly = [k for k in future if series[k]["mc"] in "HMUZ"]
        pool = quarterly or future or list(series)
        near = min((series[k]["exp_ns"] or (1 << 62)) for k in pool)
        target = _prefer([k for k in pool if (series[k]["exp_ns"] or (1 << 62)) == near])
    return target


def gather(root, expiry=None, dataset=DATASET, env_file=None, today=None, fetch=None,
           option_root=None, echo=False, fresh_defs=False, daycache=True):
    """Pull the recipe's data from Databento Historical and normalise to `build_chain`'s inputs.
    The target series is chosen from the DEFINITION LISTING by nearest FUTURE expiration among the
    quarterly (H/M/U/Z) months — data-driven, so it's immune to the year-width convention (1-digit
    2026 `PAOU6` vs 2-digit 2027 `PAOH27`) AND to metals' options-expire-the-month-before roll (we key
    off the real expiration, never a computed contract-month string). Market data (settlements/OI) is
    then pulled for ONLY that series' strikes. `fetch(schema,symbols,stype_in,start,end)->list[dict]`
    is injectable for tests. Returns (forward, contracts, expiration, T, fwd_src) — fwd_src names the
    forward's provenance ('underlying' | 'continuous' | 'mid-strike' | 'none') so a fallback forward is
    MARKED downstream, never silently presented as settlement-grade.
    `--expiry` semantics: the nearest listed series within ±7 days, else an ERROR listing the real
    expirations (a warned wrong series is still a wrong series); an exact-date tie prefers the
    QUARTERLY stem, then the bigger ladder — deterministic, never dict order."""
    if fetch is None:
        dashboard.check_no_net("Route: get_futures_options_smile via the Databento MCP connector, "
                               "save the Chain JSON, render with --from-dir DIR (see references Cowork playbook).")
    today = today or dt.date.today()
    look = (today - dt.timedelta(days=7)).isoformat()   # sparse settlements: reach past long weekends
    end = (today + dt.timedelta(days=1)).isoformat()

    if fetch is None:                                   # the real Databento Historical client
        fetch = _make_fetch(dataset, env_file, daycache=daycache)

    series = _list_series(root, fetch, today, option_root, fresh=fresh_defs)
    if not series:
        return None, [], None, 0.1, "none", None   # 6-tuple: callers unpack `under` too (v1.69.0)

    today_ns = int(dt.datetime(today.year, today.month, today.day, tzinfo=dt.timezone.utc).timestamp() * 1e9)

    target = _pick_target(series, expiry, today, root)

    tgt = series[target]
    lad, exp_ns = tgt["lad"], tgt["exp_ns"]
    if echo:    # the chosen series is always visible on the CLI -> a mis-pick can't stay silent
        _d = dt.datetime.fromtimestamp(exp_ns / 1e9, dt.timezone.utc).date() if exp_ns else None
        print(f"series: {tgt['stem']} · exp {_d.isoformat() if _d else '?'} · "
              f"{(_d - today).days if _d else '?'} DTE · {len({k for k, _ in lad.values()})} strikes · "
              f"{'quarterly' if tgt['mc'] in 'HMUZ' else 'serial'} · underlying {tgt['under'] or '?'}",
              file=sys.stderr)

    # forward = the underlying future's settlement. Use the EXACT `underlying` symbol from the option
    # definition — do NOT construct it, because the future's year width can differ from the option's
    # (NG option `ONU6` -> future `NGU26`, not `NGU6`, which 422s "symbology_invalid_request"). Fall
    # back to the liquid front continuous `{root}.v.0`; never let a forward miss crash the chain.
    def _fwd_settle(sym, stype):
        try:
            rows = fetch("statistics", sym, stype, look, end)
        except Exception:
            return None
        ps = [p for p in (_px(r.get("price")) for r in rows if r.get("stat_type") == "3") if p is not None]
        return ps[-1] if ps else None

    fwd_src = "underlying"
    forward = _fwd_settle(tgt["under"], "raw_symbol") if tgt.get("under") else None
    if forward is None:                                 # fallback: the volume-continuous front — which in a
        forward = _fwd_settle(f"{root.upper()}.v.0", "continuous")   # roll window can be ANOTHER month ->
        if forward is not None:                                      # tracked + marked, never silent
            fwd_src = "continuous"

    # MARKET DATA (settlement 3 / lowest-offer 7 / highest-bid 8 / OI 9) for ONLY that series' strikes.
    ids, stat = list(lad), {}
    failed_ids = set()
    for i in range(0, len(ids), 120):                   # chunk instrument_ids to dodge URL 414s
        chunk = ids[i:i + 120]
        try:
            rows = fetch("statistics", ",".join(chunk), "instrument_id", look, end)
        except Exception:                               # a hard failure on one chunk drops its strikes,
            failed_ids.update(chunk)                    # not the whole chain (transient errors retry above)
            continue
        for r in rows:
            stat.setdefault(r.get("instrument_id"), {})[r.get("stat_type")] = (r.get("price"), r.get("quantity"))
    if ids and not stat:
        raise RuntimeError(f"error: Databento statistics returned no market data for {tgt['stem']} "
                           f"({len(ids)} contracts); not rendering an all-n/a chain")
    if failed_ids:
        print(f"warning: Databento statistics failed for {len(failed_ids)} contracts in {tgt['stem']} — "
              "dropping that chunk", file=sys.stderr)

    contracts = []
    for iid, (K, is_call) in lad.items():
        if iid in failed_ids and iid not in stat:
            continue
        s = stat.get(iid, {})
        px = _px(s["3"][0]) if "3" in s else None
        if px is None and "7" in s and "8" in s:        # fall back to the bid/offer mid
            lo, hi = _px(s["7"][0]), _px(s["8"][0])
            px = (lo + hi) / 2 if (lo is not None and hi is not None) else None
        contracts.append({"strike": K, "is_call": is_call, "oi": _oi(s.get("9")), "settlement": px})

    expiration = (dt.datetime.fromtimestamp(exp_ns / 1e9, dt.timezone.utc).date().isoformat()
                  if exp_ns else None)
    T = (max(1, exp_ns / 1e9 - today_ns / 1e9) / 86400 / 365 if exp_ns else 0.1)
    if forward is None and contracts:                   # last resort: middle listed strike — marked
        ks = sorted({c["strike"] for c in contracts})
        forward = ks[len(ks) // 2]
        fwd_src = "mid-strike"
    return forward, contracts, expiration, T, fwd_src, tgt.get("under")


# ----------------- consume the connector (PREFERRED, keyless) -----------------
# The databento-mcp-server connector already computes the smile (Black-76 IV, OI, max-pain, deltas)
# and exposes it at GET /smile/:root.json — socket-backed (live) when the connector runs --live. The
# skill just POLLS that localhost endpoint: NO Databento key here (the connector holds it), and NO
# second data pipeline. This is the single scheme; the direct `gather()` below is the no-connector
# settlement fallback.
def fetch_connector_chain(base_url, root, expiry=None, window=None, timeout=90):
    q = {}
    if expiry:
        q["expiry"] = expiry
    if window:
        q["window"] = window
    url = base_url.rstrip("/") + "/smile/" + urllib.parse.quote(root)
    url += ".json" + (("?" + urllib.parse.urlencode(q)) if q else "")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:   # keyless localhost — no auth header
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 503:   # connector is still seeding the live buffer (cold start) -> keep retrying, honestly
            raise dashboard.Warming("warming up the live feed…") from None
        raise


def fetch_connector_term(base_url, root, timeout=600):
    """GET the connector's day-cached cross-expiration payload (/term/:root.json). The FIRST pull of
    a trading day runs the slow Databento side (minutes on big roots — the connector logs it); every
    later pull is an instant cache hit. Keyless localhost, like /smile."""
    url = base_url.rstrip("/") + "/term/" + urllib.parse.quote(root) + ".json"
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def connector_term_cached(base_url, root, timeout=8):
    """Probe the connector's /term day-cache WITHOUT triggering a pull (instant). True/False =
    ready-now / cold; None = the connector can't answer the probe (old build / unreachable)."""
    url = base_url.rstrip("/") + "/term/" + urllib.parse.quote(root) + ".json?probe=1"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            v = json.loads(r.read().decode("utf-8"))
        # distinguish key-ABSENT (old build with no probe support) from an explicit cached:false —
        # an old connector serves the full payload on ?probe=1 (no "cached" key) -> None, not "cold".
        return bool(v["cached"]) if isinstance(v, dict) and "cached" in v else None
    except Exception:
        return None


def fetch_connector_series(base_url, root, liquidity=False, timeout=None):
    """The FIRST-QUESTION listing: GET /series/:root.json — what option-expiration series exist, which
    is nearest, and (with ?liquidity=1) which carries the most OI. Served INSTANTLY from the connector's
    long-lived defs catalog (no daily snapshot re-pull); ?liquidity=1 adds a whole-root OI pull the
    connector day-caches — the FIRST such pull of a trading day runs the slow Databento side (tens of
    seconds on a wide root), so it gets the long term-style timeout. Keyless localhost, like /smile."""
    url = base_url.rstrip("/") + "/series/" + urllib.parse.quote(root) + ".json"
    if liquidity:
        url += "?liquidity=1"
    if timeout is None:
        timeout = 600 if liquidity else 30   # bare catalog listing is instant; OI adds a whole-root pull
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def render_series_listing(payload):
    """Format the /series payload as the 'first question' an agent presents BEFORE the heavy chain pull:
    every listed expiration with its DTE / kind / strike count (+ OI when liquidity was pulled), the
    nearest future series and the most-liquid one marked, and a prompt naming the default (nearest) and
    how to pick one (--expiry) or see them all (--term). Honest: the most-OI mark appears only when the
    connector actually supplied OI (liquidity=1) — it is never guessed from strike counts."""
    root = payload.get("root", "?")
    opt = payload.get("optionsRoot", "?")
    series = payload.get("series", []) or []
    nearest = payload.get("nearest")
    most = payload.get("mostLiquid")
    have_oi = any("oi" in s for s in series)
    L = [f"{root} ({opt}) — {payload.get('count', len(series))} option-expiration series · "
         f"asOf {payload.get('asOf', '?')} (connector defs catalog)",
         f"{'STEM':>9}{'EXPIRY':>13}{'DTE':>6}{'KIND':>11}{'STRIKES':>9}" + (f"{'OI':>11}" if have_oi else "")]
    for s in series:
        kind = "quarterly" if s.get("quarterly") else "serial"
        line = (f"{s.get('stem', '?'):>9}{s.get('expiration', '?'):>13}{s.get('dte', '?'):>6}"
                f"{kind:>11}{s.get('strikes', '?'):>9}")
        if have_oi:
            oi = s.get("oi")
            line += f"{('{:,}'.format(int(oi)) if oi is not None else 'n/a'):>11}"
        marks = []
        if s.get("stem") == nearest:
            marks.append("← nearest")
        if most and s.get("stem") == most:
            marks.append("★ most OI")
        if marks:
            line += "   " + " · ".join(marks)
        L.append(line)
    dflt = nearest or (series[0].get("stem") if series else "?")
    L.append("")
    L.append(f"first question — which expiration? default = nearest ({dflt}); "
             f"--expiry <YYYY-MM-DD> analyzes one series, --term shows the cross-expiration view of all.")
    return "\n".join(L)


def _term_shim(payload):
    """The connector term payload -> a gather_term-compatible fetch closure. gather_term stays
    UNCHANGED (all term math + honesty rules live there and are already offline-tested); this just
    replays the payload as definition/statistics rows — the exact shape the direct path parses."""
    defs, stats, funders = [], {}, {}
    iid = 0
    for ser in payload.get("series") or []:
        exp = ser.get("expiration")
        try:
            exp_ns = int(dt.datetime.fromisoformat(exp + "T20:00:00+00:00").timestamp() * 1e9)
        except (TypeError, ValueError):
            continue
        if ser.get("under") and ser.get("fwdSettle") is not None:
            funders[ser["under"]] = float(ser["fwdSettle"])
        for row in ser.get("strikes") or []:
            k = row.get("k")
            if k is None:
                continue
            # normalize float dust at INGESTION (live 2026-07-16: a payload k of 4.099999999 printed
            # verbatim on the smile x-axis) — round 6dp covers every GLBX tick; int(round(...)) keeps
            # the ns fixed-point exact (int() would truncate 4.1e9 - epsilon back to ...999)
            k = round(float(k), 6)
            for cls, s_key, o_key in (("C", "cSettle", "cOi"), ("P", "pSettle", "pOi")):
                iid += 1
                defs.append({"raw_symbol": f"{ser.get('stem', '?')} {cls}{k}", "instrument_id": str(iid),
                             "strike_price": str(int(round(float(k) * 1e9))), "instrument_class": cls,
                             "expiration": str(exp_ns), "underlying": ser.get("under") or ""})
                rows = []
                if row.get(s_key) is not None:
                    rows.append({"instrument_id": str(iid), "stat_type": "3",
                                 "price": str(int(float(row[s_key]) * 1e9)), "quantity": ""})
                if row.get(o_key) is not None:   # a real 0 round-trips as 0; only null/absent stays unknown
                    rows.append({"instrument_id": str(iid), "stat_type": "9",
                                 "price": "", "quantity": str(int(row[o_key]))})
                if rows:
                    stats[str(iid)] = rows

    def fetch(schema, symbols, stype_in, start, end):
        if schema == "definition":
            return defs
        if schema == "statistics" and stype_in == "instrument_id":   # v1.128: the id-scoped protocol
            return [r for i in symbols.split(",") for r in stats.get(i, [])]
        if schema == "statistics" and stype_in == "raw_symbol":
            # v1.129 F5: serve the BATCHED shape (comma-joined symbols, map_symbols column)
            return [{"instrument_id": "0", "stat_type": "3",
                     "price": str(int(funders[s] * 1e9)), "quantity": "", "symbol": s}
                    for s in symbols.split(",") if s in funders]
        return []
    return fetch


# Curated CONTRACT reference for the About strip (name · venue · unit · style/quirk). Facts only,
# majors only — an unmapped root gets an honest generic line, never invented specs. The "month
# BEFORE" quirk = COMEX metals option series expire in the month before the future's delivery month.
ROOT_SPECS = {
    "ES": ("E-mini S&P 500 futures", "CME", "$50 × index pt", "American-style"),
    "MES": ("Micro E-mini S&P 500 futures", "CME", "$5 × index pt", "American-style"),
    "NQ": ("E-mini Nasdaq-100 futures", "CME", "$20 × index pt", "American-style"),
    "MNQ": ("Micro E-mini Nasdaq-100 futures", "CME", "$2 × index pt", "American-style"),
    "CL": ("WTI Crude Oil futures", "NYMEX", "1,000 bbl", "American-style"),
    "MCL": ("Micro WTI Crude Oil futures", "NYMEX", "100 bbl", "American-style"),
    "NG": ("Henry Hub Natural Gas futures", "NYMEX", "10,000 MMBtu", "American-style"),
    "HO": ("NY Harbor ULSD (Heating Oil) futures", "NYMEX", "42,000 gal", "American-style"),
    "RB": ("RBOB Gasoline futures", "NYMEX", "42,000 gal", "American-style"),
    "MNG": ("Micro Henry Hub Natural Gas futures", "NYMEX", "1,000 MMBtu", "American-style"),
    "GC": ("Gold futures", "COMEX", "100 troy oz",
           "American-style; series expire the month BEFORE the future's delivery month"),
    "MGC": ("Micro Gold futures", "COMEX", "10 troy oz",
            "American-style; series expire the month BEFORE the future's delivery month"),
    "SI": ("Silver futures", "COMEX", "5,000 troy oz",
           "American-style; series expire the month BEFORE the future's delivery month"),
    "HG": ("Copper futures", "COMEX", "25,000 lb",
           "American-style; series expire the month BEFORE the future's delivery month"),
    "ZC": ("Corn futures", "CBOT", "5,000 bu", "American-style"),
    "ZS": ("Soybean futures", "CBOT", "5,000 bu", "American-style"),
    "ZW": ("Chicago SRW Wheat futures", "CBOT", "5,000 bu", "American-style"),
    "KE": ("KC HRW Wheat futures", "CBOT", "5,000 bu", "American-style"),
    "ZM": ("Soybean Meal futures", "CBOT", "100 short tons", "American-style"),
    "ZN": ("10-Year T-Note futures", "CBOT", "$100,000 face", "American-style"),
    "ZB": ("30-Year T-Bond futures", "CBOT", "$100,000 face", "American-style"),
    "TN": ("Ultra 10-Year T-Note futures", "CBOT", "$100,000 face", "American-style"),
    "SR3": ("3-Month SOFR futures", "CME", "$2,500 × IMM index pt", "American-style"),
    "6E": ("Euro FX futures", "CME", "€125,000", "European-style"),
    "BTC": ("Bitcoin futures", "CME", "5 BTC", ""),
    "ETH": ("Ether futures", "CME", "50 ETH", ""),
    "HE": ("Lean Hog futures", "CME", "40,000 lb", "American-style"),
    "GF": ("Feeder Cattle futures", "CME", "50,000 lb", "American-style"),
    "BZ": ("Brent Crude Last Day futures", "NYMEX", "1,000 bbl", ""),
    "PL": ("Platinum futures", "NYMEX", "50 troy oz",
           "American-style; series expire the month BEFORE the future's delivery month"),
    "PA": ("Palladium futures", "NYMEX", "100 troy oz",
           "American-style; series expire the month BEFORE the future's delivery month"),
}


def _about(an):
    """The About strip: underlying + option specifics. Curated registry facts for mapped roots; an
    honest generic line otherwise. 'exercises into <sym>' is DATA (the definition's own underlying)."""
    entry = ROOT_SPECS.get(an.get("symbol"))
    under = an.get("under")
    ex = f" · exercises into its own underlying future {under}" if under else ""
    if entry:
        name, venue, unit, style = entry
        style_s = f" · {style}" if style else ""
        txt = (f"{name} · {venue} · contract unit {unit}{style_s} · options root "
               f"{an.get('optionRoot', '?')}{ex}. Curated reference specs — verify contract details with CME.")
    else:
        txt = (f"{an.get('symbol', '?')} options on futures · GLBX.MDP3 (CME group) · options root "
               f"{an.get('optionRoot', '?')}{ex}. No curated spec entry for this root — only "
               f"definition-derived facts shown; verify contract details with CME.")
    return {"text": txt, "cue": "curated specs + definition data · verify with CME"}


TERM_BUCKETS = [0.80, 0.85, 0.90, 0.95, 1.00, 1.05, 1.10, 1.15, 1.20]   # moneyness cols (±2.5% tol)


def _contracts_from_stats(lad, stat):
    """A series' ladder + the parent statistics -> build_chain contracts (settlement-else-mid, OI)."""
    out = []
    for iid, (K, is_call) in lad.items():
        st = stat.get(str(iid), {})
        px = _px(st["3"][0]) if "3" in st else None
        if px is None and "7" in st and "8" in st:
            lo, hi = _px(st["7"][0]), _px(st["8"][0])
            px = (lo + hi) / 2 if (lo is not None and hi is not None) else None
        out.append({"strike": K, "is_call": is_call, "oi": _oi(st.get("9")), "settlement": px})
    return out


_STATS_ID_CHUNK = 500   # the connector's ed67f30 chunk — ~4KB of ids per URL, far under 414 territory


def _stats_by_id(fetch, ids, today, end):
    """Statistics for the KEPT series' instrument ids ONLY (v1.128 slice B — the connector's
    ed67f30 lesson brought skill-side): the old whole-root parent pull paid Databento's server
    prep for every weekly/EOM listing and the unused far monthlies (LO.OPT ≈ 36MB/day = minutes
    per pull, the CL cloud-session post-mortem); the kept ladders are a fraction of that.
    ADAPTIVE window: (today-1 .. end) first — a normal trading day's settlements live there,
    including the next-morning OI publication (the +1-day ts_event slop, v1.114) — widened to
    today-4 ONLY when the narrow window carries no settlements (holiday runs; the _list_series
    closed-day fallback twin). Known drift vs the old whole-root -4d window (review 2026-07-23):
    the widen is chain-WIDE — a lone id whose last stats published 2-4 days ago while the rest
    of the chain settled yesterday stays a hole where the old window priced it. Conservative
    ("holes stay holes") and rare (CME publishes settlements daily for open instruments). Chunks fan out ×8 concurrently; a failed chunk drops its strikes
    with a warning (the gather() rule); ALL chunks failing raises — never an all-n/a term build.
    Each id lives in exactly one chunk and rows stay chronological within it, so the
    latest-day-wins merge below is unaffected by chunk completion order."""
    chunks = [ids[i:i + _STATS_ID_CHUNK] for i in range(0, len(ids), _STATS_ID_CHUNK)]

    def _wave(start):
        def _pull(ch):
            try:
                return fetch("statistics", ",".join(ch), "instrument_id", start, end) or []
            except Exception:
                return None                          # a hard failure drops the chunk, not the build
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, max(1, len(chunks)))) as ex:
            res = list(ex.map(_pull, chunks))
        rows = [r for rr in res if rr is not None for r in rr]
        nfail = sum(len(ch) for ch, rr in zip(chunks, res) if rr is None)
        return rows, nfail, (bool(res) and all(rr is None for rr in res))
    rows, nfail, allfail = _wave((today - dt.timedelta(days=1)).isoformat())
    if not any(r.get("stat_type") == "3" for r in rows):
        rows, nfail, allfail = _wave((today - dt.timedelta(days=4)).isoformat())
    if allfail and ids:
        raise RuntimeError(f"error: Databento statistics failed for all {len(chunks)} id chunks "
                           f"({len(ids)} contracts); not rendering an all-n/a term build")
    if nfail:
        print(f"warning: Databento statistics failed for {nfail} contracts — dropping those strikes",
              file=sys.stderr)
    stat = {}
    for r in rows:
        stat.setdefault(str(r.get("instrument_id")), {})[r.get("stat_type")] = (r.get("price"), r.get("quantity"))
    return stat


def gather_term(root, expiry=None, dataset=DATASET, env_file=None, today=None, fetch=None,
                option_root=None, max_series=10, max_days=400, fresh_defs=False):
    """Cross-EXPIRATION term structure from the SAME parent-level pulls the single-series path uses:
    ONE definition parent listing (all series — already how --list-expiries works), id-scoped
    statistics for the KEPT series' ladders (v1.128 — the whole-root parent pull is gone; see
    _stats_by_id), plus one tiny settlement pull per DISTINCT underlying
    future (each series prices off its OWN month — CL Aug options off CLQ, Dec off CLZ; keying
    moneyness off the front would warp the whole surface, so forwards are strictly per-series, from
    the definition's exact `underlying` symbol). Honesty: a bucket with no strike within ±half-bucket
    of ITS series' forward stays None; an unsolvable IV stays None; a series with no forward is
    DROPPED with a stderr warning (a surface row keyed to a guessed forward would be fabrication)."""
    if fetch is None:
        dashboard.check_no_net("Route: get_futures_options_smile per expiry via the Databento MCP "
                               "connector, or run this on a networked host.")
        fetch = _make_fetch(dataset, env_file)
        print(f"note: direct --term pulls the {root.upper()} definition listing parent-level "
              f"(minutes on big roots, high variance on closed days) + id-scoped statistics for the "
              f"kept series; same-day repeats ride the local day-cache. The connector's /term "
              f"day-cache is the other fast path (--source connector/auto).", file=sys.stderr)
    today = today or dt.date.today()
    series = _list_series(root, fetch, today, option_root, fresh=fresh_defs)
    if not series:
        raise RuntimeError(f"error: no listed option series for {root.upper()}")
    today_ns = int(dt.datetime.combine(today, dt.time(), dt.timezone.utc).timestamp() * 1e9)
    horizon_ns = today_ns + int(max_days * 86400 * 1e9)
    future = sorted((v for v in series.values()
                     if v["exp_ns"] and today_ns <= v["exp_ns"] <= horizon_ns),
                    key=lambda v: v["exp_ns"])[:max_series]
    if not future:
        raise RuntimeError(f"error: no future {root.upper()} option series within {max_days} days")

    look = (today - dt.timedelta(days=4)).isoformat()    # the per-symbol settle window (tiny pulls)
    end = (today + dt.timedelta(days=1)).isoformat()
    opt = (option_root or resolve_option_root(root)).upper()   # payload provenance (no stats pull rides it now)
    tkey = _pick_target(series, expiry, today, root)     # the combined page's series — SAME rule as gather
    tgt = series[tkey]
    keep = sorted(future + ([tgt] if tgt not in future else []), key=lambda v: v["exp_ns"])
    stat = _stats_by_id(fetch, [str(iid) for v in keep for iid in v["lad"]], today, end)

    def _fwd(sym):
        try:
            rows = fetch("statistics", sym, "raw_symbol", look, end)
        except Exception:
            return None
        ps = [p for p in (_px(r.get("price")) for r in rows if r.get("stat_type") == "3") if p is not None]
        return ps[-1] if ps else None
    unders = []
    for v in keep:
        u = v.get("under")
        if u and u not in unders:
            unders.append(u)
    # v1.129 F5: ONE batched settlement pull for ALL underlyings — the ~10 per-symbol pulls each
    # paid Databento's fixed per-request prep (~100s live-cloud in the CL forensics). The
    # map_symbols `symbol` column attributes rows per future (the stream is ts-ordered, so the
    # LAST stat-3 per symbol = the latest settle — same rule as _fwd). A failed batch or a
    # payload whose stat-3 rows lack the column (shape drift) falls back to the concurrent
    # per-symbol pulls; a symbol with no settlement in the batch stays absent -> its series is
    # dropped with the same warning as before (never keyed to a guessed forward).
    fwd_by_under = None
    if unders:
        try:
            rows = fetch("statistics", ",".join(unders), "raw_symbol", look, end)
        except Exception:
            rows = None
        if rows is not None:
            st3 = [r for r in rows if r.get("stat_type") == "3"]
            if all(r.get("symbol") for r in st3):
                fwd_by_under = {}
                for r in st3:
                    px = _px(r.get("price"))
                    if px is not None:
                        fwd_by_under[r["symbol"]] = px
    if fwd_by_under is None:
        # The per-underlying settlement pulls are independent -> concurrent (the pre-F5 path,
        # kept as the fallback; same pattern as the fundamentals/portfolio fan-out).
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, max(1, len(unders)))) as ex:
            fwd_by_under = dict(zip(unders, ex.map(_fwd, unders)))

    out, matrix, oimatrix = [], [], []
    # `keep` already carries the TARGET even when it lies beyond the max_series/max_days window (an
    # explicit --expiry on a far series) — its forward AND its stats are pulled above; without this
    # the combined page silently re-anchored to the FRONT series (gate regression, 2026-07-06).
    # Sorted: term curve stays monotonic in expiration.
    for v in keep:
        F = fwd_by_under.get(v.get("under"))
        exp_d = dt.datetime.fromtimestamp(v["exp_ns"] / 1e9, dt.timezone.utc).date()
        if F is None:
            print(f"warning: no settlement for {v['stem']}'s underlying {v.get('under') or '?'} — "
                  f"series dropped from the term structure (never keyed to a guessed forward)", file=sys.stderr)
            continue
        T = max(1.0, (exp_d - today).days) / 365.0
        by_k, oi_by_k = {}, {}
        for iid, (K, is_call) in v["lad"].items():
            st = stat.get(str(iid), {})
            px = _px(st["3"][0]) if "3" in st else None
            if px is None and "7" in st and "8" in st:
                lo, hi = _px(st["7"][0]), _px(st["8"][0])
                px = (lo + hi) / 2 if (lo is not None and hi is not None) else None
            by_k.setdefault(K, {})["call" if is_call else "put"] = px
            # OI for the OI surface: a MISSING stat-9 record means UNKNOWN (None), never 0 — the panel
            # info promises "0 = a real datum"; only a present record yields a number (junk -> 0 via _oi).
            oi_by_k.setdefault(K, {})["call" if is_call else "put"] = (_oi(st["9"]) if "9" in st else None)
        strikes = sorted(by_k)
        if not strikes:
            continue

        def _leg_iv(K):   # the OTM-leg rule (put below F, call above) for the surface buckets
            # v1.125: NO ITM substitution — the surface info promises "OTM-leg IV" and the module
            # rule (see _smile_iv) is that the ITM leg's IV is noisy and is NEVER substituted:
            # a strike with no OTM settlement stays a HOLE ("holes stay holes"), and _iv_near /
            # the term-row skew25 built on it can no longer ride an ITM wing unmarked.
            px = by_k[K].get("put") if K <= F else by_k[K].get("call")
            piv = _iv(px, K, T, F, K > F)
            return round(piv * 100, 2) if piv else None
        # ATM IV/strike + the tradeable count come from build_chain on THIS series' contracts — the SAME
        # OI-filtered reducer that builds the switcher's chain card — so the Series-table's atmIV /
        # atmStrike / nStrikes match the card EXACTLY. (skew25 below stays on the wider full-grid
        # _iv_near for better wing coverage, so it may differ from the card's 25Δ skew by a hair.)
        # The pre-v1.111 rule picked the strike geometrically nearest the forward across ALL listed
        # strikes, so it went n/a whenever that strike was a DEAD listing (no settlement, OI 0) while a
        # tradeable strike a tick away priced fine — the emptied HO term curve (2026-07-15).
        contracts = _contracts_from_stats(v["lad"], stat)
        _an = build_chain(root, F, contracts, exp_d.isoformat(), T, today=today,
                          option_root=option_root, under=v.get("under"))
        atm_k, atm_iv = _an["atmStrike"], _an["atmIV"]

        def _iv_near(target):
            cand = [(abs(k - target), _leg_iv(k)) for k in strikes]
            cand = [(g, v_) for g, v_ in cand if v_ is not None]
            return min(cand)[1] if cand else None
        p90, c110 = _iv_near(0.90 * F), _iv_near(1.10 * F)
        skew = round(p90 - c110, 1) if (p90 is not None and c110 is not None) else None
        out.append({"expiration": exp_d.isoformat(), "dte": (exp_d - today).days, "stem": v["stem"],
                    "quarterly": v["mc"] in "HMUZ", "fwd": F, "atmStrike": atm_k,
                    # nStrikes = the TRADEABLE count build_chain kept (OI>0), matching the chain card —
                    # not len(near-money grid), which counted dead listings (HO term row said 33 while
                    # the card said 7). One density number, consistent with the ATM-IV/skew above it.
                    "atmIV": atm_iv, "skew25": skew, "nStrikes": _an["nStrikes"],
                    # full per-series chain inputs so the switcher can build EVERY series' page (not
                    # just the target) — greeks stay Black-76 in Python, computed once at build time.
                    # `contracts` is the SAME object build_chain just consumed for atmIV (no re-pull).
                    "under": v.get("under"), "T": T, "contracts": contracts})
        row, oi_row = [], []
        for b in TERM_BUCKETS:
            kstar = b * F
            k = min(strikes, key=lambda x: abs(x - kstar))
            if abs(k - kstar) > 0.025 * F:   # no listed strike within ±2.5% of the bucket -> a hole (both surfaces)
                row.append(None)
                oi_row.append(None)
                continue
            v_ = _leg_iv(k)
            row.append(round(v_, 1) if v_ is not None else None)
            c_oi, p_oi = oi_by_k.get(k, {}).get("call"), oi_by_k.get(k, {}).get("put")
            # both legs UNKNOWN -> a hole (None); any known leg -> the honest sum (0 = a real datum)
            oi_row.append(None if (c_oi is None and p_oi is None) else (c_oi or 0) + (p_oi or 0))
        matrix.append(row)
        oimatrix.append(oi_row)
    if not out:
        raise RuntimeError(f"error: no {root.upper()} series with a usable forward — term structure "
                           f"would be all-fabricated; not rendering")
    tF = fwd_by_under.get(tgt.get("under"))
    if tF is None:
        raise RuntimeError(f"error: no settlement for the target series {tgt['stem']}'s underlying "
                           f"{tgt.get('under') or '?'} — cannot price the chain")
    t_exp = dt.datetime.fromtimestamp(tgt["exp_ns"] / 1e9, dt.timezone.utc).date()
    target = {"stem": tgt["stem"], "expiration": t_exp.isoformat(), "under": tgt.get("under"),
              "forward": tF, "fwdSrc": "underlying", "T": max(1.0, (t_exp - today).days) / 365.0,
              "contracts": _contracts_from_stats(tgt["lad"], stat)}
    return {"symbol": root.upper(), "optionRoot": opt, "series": out, "target": target,
            "surface": {"cols": [f"{round(b*100)}%" for b in TERM_BUCKETS], "matrix": matrix},
            "oiSurface": {"cols": [f"{round(b*100)}%" for b in TERM_BUCKETS], "matrix": oimatrix}}


def to_term_spec(T):
    """The term analysis -> a dashboard SPEC (ATM-IV/skew curve + IV surface + per-series table)."""
    S = T["series"]
    front = S[0]
    slope = (round(S[-1]["atmIV"] - front["atmIV"], 1)
             if (len(S) > 1 and S[-1]["atmIV"] is not None and front["atmIV"] is not None) else None)
    rows = [[r["expiration"], str(r["dte"]), r["stem"] + ("" if r["quarterly"] else " ·serial"),
             _num(r["fwd"]), _ivfmt(r["atmIV"]),
             (f"{r['skew25']:+.1f}" if r["skew25"] is not None else "n/a"), str(r["nStrikes"])] for r in S]
    return {
        "title": f"{T['symbol']} ({T['optionRoot']}) options · term structure · {len(S)} series",
        "subtitle": (f"Databento GLBX.MDP3 · settlement · Black-76 · per-series forwards · "
                     f"not advice · [settlement — no live ticks]"),
        "cards": [
            {"label": "Front ATM IV", "value": _ivfmt(front["atmIV"]),
             "hint": "ATM implied vol of the nearest expiration — the short end of the curve."},
            {"label": "Term slope", "value": (f"{slope:+.1f}" if slope is not None else "n/a"),
             "hint": "Farthest ATM IV minus front, vol points. Positive = contango (normal); negative = "
                     "inverted — near-dated stress prices above the back."},
            {"label": "Series", "value": str(len(S)),
             "hint": "Listed expirations shown (nearest first, capped; weeklies excluded by the "
                     "root's stem pattern)."},
            {"label": "Front DTE", "value": str(front["dte"]),
             "hint": "Calendar days to the nearest expiration."},
            {"label": "Front fwd", "value": _num(front["fwd"]),
             "hint": "The front series' OWN underlying future settlement — every series prices off its "
                     "own month, so each row's moneyness is keyed to its own forward."},
        ],
        "panels": [
            {"type": "line", "title": "ATM IV & 25Δ skew by expiration", "full": True, "info":
                "The vol term structure from settlements (Black-76 per series). Solid = ATM IV; dashed = "
                "the ~10%-OTM skew proxy in vol points. Upward = contango; a front bump = event risk.",
             "data": {"labels": [r["expiration"][2:] for r in S], "series": [
                 {"name": "ATM IV %", "values": [r["atmIV"] for r in S]},
                 {"name": "25Δ skew (vol pts)", "values": [r["skew25"] for r in S], "dash": [6, 4]}]}},
            {"type": "heatmap", "title": "IV surface (moneyness × expiry)", "info":
                "OTM-leg IV at the nearest listed strike per 5% moneyness bucket — keyed to EACH series' "
                "own forward (GLBX months differ; the front's price would warp the rows). n/a = no strike/"
                "settlement within tolerance — holes stay holes, nothing is interpolated across series. "
                "Hotter = richer vol.",
             "data": {"rows": [f"{r['stem']} · {r['dte']}d" for r in S],
                      "cols": T["surface"]["cols"], "matrix": T["surface"]["matrix"], "ramp": "seq"}},
            {"type": "heatmap", "title": "Open interest by moneyness × expiry", "info":
                "Total open interest (call+put) at the nearest listed strike per 5% moneyness bucket — "
                "keyed to EACH series' own forward, the OI twin of the IV surface above. Hotter = more open "
                "contracts (where positioning concentrates across the curve). 0 = a real datum (no OI at "
                "that bucket); n/a = no listed strike within tolerance — holes stay holes.",
             "data": {"rows": [f"{r['stem']} · {r['dte']}d" for r in S],
                      "cols": T["oiSurface"]["cols"], "matrix": T["oiSurface"]["matrix"],
                      "ramp": "seq", "cellFmt": "int"}},
            {"type": "table", "title": "Series", "info":
                "One row per listed expiration (definition-driven). Fwd = that series' own underlying "
                "settlement. n/a = no solvable value — never filled.",
             "data": {"columns": ["Expiry", "DTE", "Stem", "Fwd", "ATM IV", "25Δ skew", "Strikes"],
                      "rows": rows}},
        ],
    }


def combined_spec(specs, T, target_idx=0):
    """A SWITCHABLE combined --term page: the chosen series' chain (smile/OI/GEX/table) + an
    EXPIRATION SWITCHER that swaps WHICH series those charts show — every series' page is pre-built
    here (Black-76 greeks computed once), so the swap is client-side with zero network — plus the
    cross-expiration term panels (curve/surface/series-table) appended below as the fixed overview."""
    S = T["series"]
    tsp = to_term_spec(T)
    chain_n = len(specs[target_idx]["panels"])   # the per-series chain panels (smile/OI/GEX/table)
    panels = []
    for j in range(chain_n):
        p = dict(specs[target_idx]["panels"][j])
        p["series"] = [specs[i]["panels"][j]["data"] for i in range(len(specs))]   # every series' data
        panels.append(p)
    panels += tsp["panels"]                       # term panels stay fixed (no .series)
    # PERMANENT cross-expiration tiles lead every series' card set — they frame the GENERAL page and do
    # NOT swap with the switcher (constant across series); the per-series chain tiles follow and DO swap.
    perm = [c for c in (next((x for x in tsp["cards"] if x["label"] == lab), None)
                        for lab in ("Series", "Front ATM IV", "Term slope")) if c]
    card_series = [perm + list(specs[i]["cards"]) for i in range(len(specs))]
    out = dict(specs[target_idx])
    out["title"] = tsp["title"]                   # GENERAL page = all-series title, not the target series'
    out["panels"] = panels
    out["cards"] = card_series[target_idx]
    out["cardSeries"] = card_series
    suffix = f" · +term ({len(S)} series, switchable)"
    # v1.129.1 series honesty (user: «под видом экспирации меняется базовый актив»): a futures
    # series swap changes the UNDERLYING month too (LOU6 -> CLU6, LOZ6 -> CLZ6 — 1:1 by CME
    # construction), so the control is labeled "Series" and every option NAMES its future.
    out["switcher"] = {"label": "Series", "index": target_idx,
                       "options": [(f"{s['stem']} → {s['under']} · {s['dte']} DTE" if s.get("under")
                                    else f"{s['stem']} · {s['dte']} DTE") for s in S],
                       # per-series subtitles so strike counts / fwd notes track the selection
                       "subtitles": [specs[i].get("subtitle", "") + suffix for i in range(len(specs))],
                       "hint": "switch which SERIES the chain charts (smile / OI / GEX / table) show — "
                               "each series is options on its OWN futures month (the arrow names it), "
                               "so the forward changes with the pick; the term panels below stay as "
                               "the cross-expiration overview"}
    out["subtitle"] = specs[target_idx].get("subtitle", "") + suffix
    return out


_EIA_WEEKDAY = {"CL": 2, "HO": 2, "RB": 2, "BZ": 2, "MCL": 2,   # petroleum status: Wednesdays 10:30 ET
                "NG": 3, "MNG": 3}                              # natgas storage: Thursdays 10:30 ET


def eia_events(root, hist, today=None, now_et=None):
    """The futures analog of the equity earnings overlay (h2, user 2026-07-16): EIA weekly report
    dates for ENERGY roots — schedule-derived (a fixed weekday; holiday shifts are NOT modelled and
    the tile hint says so), zero network. `past` spans the store window (or ~6 months bare), `next`
    is the next report day. Non-energy roots -> None — a calendar is never fabricated."""
    wd = _EIA_WEEKDAY.get((root or "").upper())
    if wd is None:
        return None
    today = today or dt.date.today()
    try:
        start = dt.date.fromisoformat(hist[0]["date"]) if hist else today - dt.timedelta(days=180)
    except (KeyError, ValueError, TypeError):
        start = today - dt.timedelta(days=180)
    past, d = [], start
    while d <= today:
        if d.weekday() == wd:
            past.append(d.isoformat())
        d += dt.timedelta(days=1)
    nxt = None
    if today.weekday() == wd:
        # v1.126: on the report day itself, BEFORE the ~10:30 ET release "next" IS today —
        # it pointed a week out all morning. `now_et` is the test seam. NB: opthist._et_zone
        # never raises (a tz-less platform gets its fixed UTC-5 fallback, so under EDT this
        # check runs up to 1h behind — 10:30–11:30 EDT may still read "today", the same
        # documented approximation as the Session tile); the except guards only a broken
        # opthist import.
        try:
            import opthist as _oh
            _n = now_et if now_et is not None else dt.datetime.now(_oh._et_zone())
            if (_n.hour, _n.minute) < (10, 30):
                nxt = today
        except Exception:
            nxt = None
    if nxt is None:
        nxt = today + dt.timedelta(days=1)
        while nxt.weekday() != wd:
            nxt += dt.timedelta(days=1)
    return {"past": past, "next": nxt.isoformat()}


_COMPLEX = {"CL": "energy", "BZ": "energy", "NG": "natural gas", "HO": "energy", "RB": "energy",
            "GC": "precious metals", "SI": "precious metals", "PL": "precious metals",
            "PA": "precious metals", "HG": "copper / industrial metals",
            "ZC": "grains", "ZW": "grains", "KE": "grains", "ZS": "grains / oilseeds",
            "ZL": "grains / oilseeds", "ZM": "grains / oilseeds",
            "LE": "livestock", "HE": "livestock", "GF": "livestock"}


def offer_commands(root):
    """The deeper-analysis commands that FIT a commodity, for the closing-step note (v1.157.0).
    SKILL.md's palette: a commodity story -> the sector landscape, "how do I play it" -> a screen,
    a calendar question (WASDE, OPEC+, FOMC) -> the catalyst calendar. Company-shaped documents
    (dcf / comps / earnings*) NEVER fit a commodity and are deliberately absent. The complex names
    the sector report's scope; an unmapped root gets the bare command rather than a guessed
    complex — naming the wrong complex is worse than naming none."""
    cx = _COMPLEX.get((root or "").upper())
    return [("/equity-research:sector " + cx) if cx else "/equity-research:sector",
            "/equity-research:screen", "/equity-research:catalysts"]


_SESSION_EXCLUDED = {"ZC", "ZS", "ZW", "KE", "ZM", "ZL", "LE", "HE", "GF", "DC", "CB"}
# grains/livestock/dairy trade DIFFERENT (shorter, split) sessions — no tile rather than a faked
# 23h one; their honest schedules are a documented follow-up.


def globex_session(root, now=None):
    """The CME Globex session tile for the ~23h family (energy/metals/index/FX/rates/crypto):
    Sun–Fri, 18:00 ET open -> next-day 17:00 ET close, with the daily 17:00–18:00 maintenance
    break. Returns the current phase + an 8-boundary open/close schedule the engine live-ticks
    (`countdown.phaseFromSchedule` — the client derives Open/Closed from the tile's OWN schedule,
    never from the equity ET clock). Holidays are not checked (weekends are). Excluded families
    and a missing tz database -> None (the tile is omitted, never faked)."""
    if (root or "").upper() in _SESSION_EXCLUDED:
        return None
    try:
        from zoneinfo import ZoneInfo
        etz = ZoneInfo("America/New_York")
    except Exception:
        return None
    now = now or dt.datetime.now().astimezone()
    if now.tzinfo is None:
        now = now.astimezone()
    exch = now.astimezone(etz)
    midnight = exch.replace(hour=0, minute=0, second=0, microsecond=0)
    sched = []
    for dday in range(-1, 9):
        day = midnight + dt.timedelta(days=dday)
        if day.weekday() <= 4:                       # Mon–Fri: the 17:00 ET close
            b = day.replace(hour=17)
            if b > exch:
                sched.append({"at": int(b.timestamp() * 1000), "label": "closes"})
        if day.weekday() <= 3 or day.weekday() == 6:  # Sun–Thu: the 18:00 ET open
            b = day.replace(hour=18)
            if b > exch:
                sched.append({"at": int(b.timestamp() * 1000), "label": "opens"})
    sched.sort(key=lambda x: x["at"])
    sched = sched[:8]
    nxt = sched[0] if sched else None
    if nxt and nxt["label"] == "closes":
        status = "Open"
    elif exch.weekday() == 5 or (exch.weekday() == 6 and exch.hour < 18) \
            or (exch.weekday() == 4 and exch.hour >= 17):
        status = "Closed · weekend"
    else:
        status = "Closed · daily break"

    def _dur(secs):
        secs = max(0, int(secs))
        d_, h, m = secs // 86400, secs % 86400 // 3600, secs % 3600 // 60
        return (f"{d_}d {h}h" if d_ else (f"{h}h {m:02d}m" if h else f"{m}m"))
    cd_text = (nxt["label"] + " in " + _dur(nxt["at"] / 1000 - exch.timestamp())) if nxt else ""
    return {"status": status, "schedule": sched, "countdownText": cd_text,
            "hoursEt": "Sun–Fri 18:00–17:00 ET (daily 17:00–18:00 break)"}


_FUT_HIST_SUB = "Databento GLBX.MDP3 statistics settlement history · Black-76 IV (greeks.py, computed)"
_FUT_SKEW_HINT = ("putIV − callIV at the ~10%-OTM moneyness PROXY of the nearest expiration, vol points. "
                  "NEGATIVE = calls richer (upside bid) — common for energy, unlike the equity puts-bid prior.")
_FUT_CHAIN_NOTE = "(The Chain/Term tabs are today's SETTLEMENTS too — nothing on this page is live.)"


def build_full_spec(root, TT, specs, target_idx, hist, as_of=None):
    """The COMBINED futures page [Overview · Chain · Term · History] — slice (e) of
    docs/futures-full-plan.md, the equity options.build_full mirror on the same sections engine.
    Chain = the switchable per-expiration drill (combined_spec — EXACTLY the --term page's chain
    half); Term = the cross-expiration overview (to_term_spec); Overview/History = opthist's
    builders over the futhist store records (the ONE-CONTRACT lever) with the FUTURES provider
    strings — Databento/Black-76, and never a "LIVE" claim (every tab is settlement-priced).
    A COLD store renders honest 'warming' placeholders naming the backfill command and leads on
    Chain — never a blank tab, never an in-render backfill.
    PAGE FURNITURE = the v1.110 equity parity (slice h1, user 2026-07-16): the stacked header
    (root + curated product name + 'Options on futures' + the UNDERLYING settlement price with
    change% vs the prior stored settle), the page-level LEAD block (curated contract identity +
    an 'as of · settlement' meta) instead of the About strip, NO subtitles anywhere (the #sub
    line must not change on tab/expiration switches; provenance lives in the footer dataSource
    and the lead meta)."""
    import opthist   # lazy: futures_options must stay importable by futhist (which imports opthist itself)
    combined = combined_spec(specs, TT, target_idx)
    n_chain = len(specs[target_idx]["panels"])
    chain = {"id": "smile", "name": "Chain", "cards": combined["cards"],
             "panels": combined["panels"][:n_chain], "switcher": dict(combined["switcher"]),
             "cardSeries": combined["cardSeries"]}
    chain["switcher"]["hint"] = "chain (smile · OI · GEX · ladder) for the picked expiration"
    tsp = to_term_spec(TT)
    term = {"id": "term", "name": "Term", "cards": tsp["cards"], "panels": tsp["panels"]}
    for c in term["cards"]:      # h1 honesty: the window cap must never read as "all listed"
        if c.get("label") == "Series":
            c["hint"] = ((c.get("hint") or "") + " The count is the nearest series within the term "
                         "window — NOT necessarily all the exchange lists; --list-series shows every "
                         "expiration.").strip()
    eia = eia_events(root, hist)                  # energy roots only — never a fabricated calendar
    if hist:
        overview = opthist.build_overview(hist, earnings=eia, skew_hint=_FUT_SKEW_HINT,
                                          chain_note=_FUT_CHAIN_NOTE, event_label="EIA report")
        history = opthist.build_history_section(root.upper(), hist, earnings=eia,
                                                subtitle=_FUT_HIST_SUB, skew_hint=_FUT_SKEW_HINT,
                                                event_label="EIA report")
        settle = hist[-1].get("date")
        _sc = {"label": "Settlement store", "value": settle or "n/a",
               "hint": ("Overview & History come from the futhist settlement store as of %s — daily "
                        "Databento statistics reduced through the SAME build_chain as the Chain tab "
                        "(Black-76, computed IV). Fill/refresh: futhist.py %s --backfill."
                        % (settle, root.upper()))}
        history["cards"] = [_sc] + (history.get("cards") or [])
        if history.get("cardSeries"):
            history["cardSeries"] = [[_sc] + c for c in history["cardSeries"]]
        active = "overview"
    else:
        warm = ("Volatility history needs the futures settlement store — warming up. Fill it with:  "
                "futhist.py %s --backfill --years 1  (the nightly accrete keeps it fresh after)."
                % root.upper())
        # h4-A (user 2026-07-17, the equity v1.78 parity): the COLD Overview renders IMMEDIATELY,
        # LIVE-SYNTHESIZED from the term pull's own data — only the history-derived pieces (the rank
        # tile, the CM-IV chart) are honestly 'warming' and arrive after the backfill.
        S = TT["series"]
        front_s = S[0] if S else {}
        usable = [s for s in S if s.get("atmIV") is not None]
        if len(usable) >= 2:
            f2, b2 = usable[0]["atmIV"], usable[-1]["atmIV"]     # PERCENT here (gather_term units)
            slope_lbl = "flat" if abs(f2 - b2) < 0.05 else ("front-rich" if f2 > b2 else "back-rich")
            slope_sub = ["%.1f%% → %.1f%%" % (f2, b2)]
        else:
            slope_lbl, slope_sub = "n/a", None
        tc = TT["target"].get("contracts") or []
        coi = sum((c.get("oi") or 0) for c in tc if c.get("is_call"))
        poi = sum((c.get("oi") or 0) for c in tc if not c.get("is_call"))
        cold_cards = [
            {"label": "IV rank · 30d", "value": "warming",
             "hint": "Needs the settlement history store — fill it:  futhist.py %s --backfill "
                     "--years 1. Until then the cheap/rich read is shape-relative (the term "
                     "structure), not history-relative." % root.upper()},
            {"label": "ATM IV · front", "value": (_ivfmt(front_s.get("atmIV"))), "scope": "expiry",
             "scopeLabel": ("nearest · %dd DTE" % front_s["dte"]) if front_s.get("dte") is not None
                           else "nearest expiry",
             "hint": "At-the-money implied vol of the nearest expiration (today's settlements)."},
            {"label": "25Δ skew · front",
             "value": ("%+.1f" % front_s["skew25"]) if front_s.get("skew25") is not None else "n/a",
             "scope": "expiry",
             "scopeLabel": ("nearest · %dd DTE" % front_s["dte"]) if front_s.get("dte") is not None
                           else "nearest expiry",
             "hint": _FUT_SKEW_HINT},
            {"label": "Term slope · settlement", "value": slope_lbl, "sub": slope_sub,
             "hint": "Nearest vs farthest ATM IV in today's term pull. Front-rich = a near-term "
                     "event / backwardation; back-rich = contango. " + _FUT_CHAIN_NOTE},
            {"label": "PCR · OI", "value": ("%.2f" % (poi / coi)) if coi else "n/a",
             "scope": "expiry", "scopeLabel": "target series",
             "hint": "Put/call open interest of the target series (today's pull)."},
        ]
        eia = eia_events(root, [])
        if eia and eia.get("next"):
            cold_cards.append({"label": "Next EIA report", "value": eia["next"][5:],
                               "hint": "Schedule-derived (fixed weekday; holiday shifts not "
                                       "modelled). The report lands 10:30 ET."})
        overview = {"id": "overview", "name": "Overview", "cards": cold_cards, "panels": []}
        history = {"id": "history", "name": "History", "state": "warming", "cards": [], "panels": [],
                   "warmingText": warm}
        active = "overview"
    sections = [overview, chain, term, history]
    for s in sections:                # h1: ONE quiet #sub line — no section/switcher subtitle may swap it
        s.pop("subtitle", None)
        if s.get("switcher"):
            s["switcher"].pop("subtitles", None)
    opt = (TT.get("optionRoot") or resolve_option_root(root)).upper()
    # h1 header: the v1.110 stacked head — the UNDERLYING settlement price on the right, honestly
    # labelled (never presented as live), with change% vs the prior stored settle when the target
    # is the FRONT series (the store's spot IS the front settle; any other target would compare
    # apples to oranges).
    F = TT["target"].get("forward")
    entry = ROOT_SPECS.get(root.upper())
    name = (entry[0] if entry else "%s futures" % root.upper())
    chg_s = ""
    # v1.126: as_of may arrive TIMESTAMP-form from a connector payload ("2026-07-17T20:15:00Z") —
    # normalize to the DATE for the same-day guard and the header text, or the guard misses the
    # same trading day (a fake +0.00%) and the raw timestamp leaks into the header.
    as_of = str(as_of)[:10] if as_of else as_of
    if hist and F:
        prev = next((r for r in reversed(hist) if r.get("date") != as_of), None)
        if prev:
            # h2: STEM-match the target series in the prior record (futhist carries fwd/stem since
            # v1.115) — an honest change% for ANY target; fall back to the record's front spot only
            # when the target IS the front (pre-h2 stores lack the extras)
            tstem = TT["target"].get("stem")
            prev_fwd = next((se.get("fwd") for se in prev.get("series", [])
                             if se.get("stem") == tstem and se.get("fwd")), None)
            if prev_fwd is None and target_idx == 0:
                prev_fwd = prev.get("spot")
            if prev_fwd:
                chg_s = "%+.2f%% · " % ((F / prev_fwd - 1) * 100)
    right_small = chg_s + "settlement" + (" %s" % as_of if as_of else "")
    # h1 feedback (user): the header OWNS the product name and the Contract TILE owns the specs —
    # the lead carries only what neither has: the exercise fact + the verify caveat. v1.129.1: the
    # lead is series-AGNOSTIC — it used to pin the TARGET's month ("…future CLU6"), which went
    # stale the moment the Series switcher swapped; the per-series Forward card names the month.
    ex = ("Each series exercises into its own futures month — the Series picker and the "
          "Forward card name it. ")
    if entry:
        lead_text = ex + "Curated reference specs — verify contract details with CME."
        style_short = (entry[3].split("-", 1)[0] if entry[3] else "American")
        _contract = {"label": "Contract", "value": f"{style_short} · {entry[2]}",
                     "sub": [f"{entry[1]} · options root {opt}"],
                     "hint": (f"{entry[0]} options · {entry[1]} · contract unit {entry[2]}"
                              + (f" · {entry[3]}" if entry[3] else "")
                              + f" · options root {opt}. Curated reference specs — verify "
                                f"contract details with CME.")}
    else:
        lead_text = (ex + "No curated spec entry for this root — definition-derived facts only; "
                          "verify contract details with CME.")
        _contract = {"label": "Contract", "value": "Options on futures",
                     "sub": [f"GLBX.MDP3 · options root {opt}"],
                     "hint": ("No curated spec entry for this root — only definition-derived "
                              "facts shown; verify contract details with CME.")}
    if not overview.get("state"):             # the warm Overview leads with the identity tile
        overview["cards"] = [_contract] + (overview.get("cards") or [])
        sess = globex_session(root)
        if sess:                              # h2: the Globex session tile, right after Contract
            overview["cards"].insert(1, {
                "label": "Session · CME Globex", "value": sess["status"], "small": True,
                "countdown": {"schedule": sess["schedule"], "text": sess["countdownText"],
                              "phaseFromSchedule": True},
                "hint": ("%s. The countdown ticks live in your browser; the phase follows the tile's "
                         "own schedule. Holidays are not checked (weekends are)." % sess["hoursEt"])})
    lead_meta = ("as of %s · " % as_of if as_of else "") + \
        "settlement data — nothing on this page is a live tick"
    return {"title": "%s (%s) options" % (root.upper(), opt),
            "head": {"leftBig": root.upper(), "leftName": name.replace(" futures", ""),
                     "leftType": "Options on futures",
                     "rightBig": "$" + _num(F), "rightSmall": right_small},
            "lead": {"text": lead_text, "meta": lead_meta, "site": ""},
            "dataSource": "Databento GLBX.MDP3",
            "sections": sections, "active": active}


def render_term_text(T):
    S = T["series"]
    L = [f"{T['symbol']} ({T['optionRoot']}) OPTIONS TERM STRUCTURE · {len(S)} series · per-series forwards",
         f"{'EXPIRY':>12}{'DTE':>6}{'STEM':>9}{'FWD':>10}{'ATM_IV':>9}{'SKEW':>7}{'STRIKES':>9}"]
    for r in S:
        sk = f"{r['skew25']:+.1f}" if r["skew25"] is not None else "n/a"   # hoisted: nested f-quotes break 3.9
        L.append(f"{r['expiration']:>12}{r['dte']:>6}{r['stem']:>9}{_num(r['fwd']):>10}"
                 f"{_ivfmt(r['atmIV']):>9}{sk:>7}{r['nStrikes']:>9}")
    return "\n".join(L)


def chain_to_an(chain, root):
    """A connector Chain (decimal IVs, both legs, None-for-no-OI) -> the analysis dict `to_spec` reads.
    OTM-masks (put for K<=ATM, call for K>ATM), scales IV to percent, keeps the tradeable ladder."""
    s = chain.get("strikes") or []
    ci, pi = chain.get("callIV") or [], chain.get("putIV") or []
    co, po = chain.get("callOI") or [], chain.get("putOI") or []
    atm = chain.get("atmStrike")

    def _g(arr, i):   # tolerate a ragged snapshot (a parallel array shorter than strikes) -> missing = None
        return arr[i] if i < len(arr) else None

    def oi(x):
        return int(x) if x else 0

    idx = [i for i in range(len(s)) if oi(_g(co, i)) > 0 or oi(_g(po, i)) > 0] or list(range(len(s)))
    strikes, putIV, callIV, callOI, putOI = [], [], [], [], []
    for i in idx:
        pv, cv = _g(pi, i), _g(ci, i)   # FULL both legs (table shows all; smile masks in to_spec)
        strikes.append(s[i])
        putIV.append(round(pv * 100, 2) if pv is not None else None)
        callIV.append(round(cv * 100, 2) if cv is not None else None)
        callOI.append(oi(_g(co, i)))
        putOI.append(oi(_g(po, i)))
    pct = lambda v: (v * 100 if v is not None else None)  # noqa: E731
    # v1.82.0 (review P1): the connector chain carries every expected-move input (spot/atmIV/dte) —
    # compute it here too, or the DEFAULT futures path shows a permanent n/a beside a live ATM IV.
    _em, _em_pct = _exp_move(chain.get("spot"), pct(chain.get("atmIV")), chain.get("dte"))
    return {
        "symbol": root.upper(), "optionRoot": chain.get("symbol") or resolve_option_root(root),
        "expiration": chain.get("expiration"), "dte": chain.get("dte"), "forward": chain.get("spot"),
        "expMove": _em, "expMovePct": _em_pct,
        "atmStrike": atm, "atmIV": pct(chain.get("atmIV")),
        "skew25": (round(chain["skew25"] * 100, 1) if chain.get("skew25") is not None else None),
        "pcrOI": chain.get("pcrOI"), "maxPain": chain.get("maxPain"), "nStrikes": len(strikes),
        "strikes": strikes, "putIV": putIV, "callIV": callIV, "callOI": callOI, "putOI": putOI,
        "fwdSrc": "spot",   # the connector's live spot — its provenance is the liveness badge's job
        # /smile serves a ±window centred on ATM (default 20) — NOT the full ladder; honest label in to_spec
        "ladder": "window", "window": chain.get("window"),
    }


def _asof_age_s(asof):
    """Seconds since the chain's asOf timestamp — the honest freshness signal (None if unparseable)."""
    if not asof:
        return None
    try:
        t = dt.datetime.fromisoformat(str(asof).replace("Z", "+00:00"))
        return (dt.datetime.now(dt.timezone.utc) - t).total_seconds()
    except (ValueError, TypeError):
        return None


_SKEW_TOL_S = 5  # absorb minor clock skew, but reject an absurdly-future asOf as "not verified fresh"


def connector_liveness(chain):
    """Honest badge from the connector Chain's asOf: fresh (<2 min) = tick-LIVE, else delayed/settlement.
    Never claim live when the data is stale (hard safety rule). A FUTURE asOf beyond a small skew
    tolerance (clock skew / a bad timestamp) is NOT verified-fresh -> delayed, and the 'Ns ago' counter
    is clamped to >=0 so a skewed clock can't print a negative age."""
    age = _asof_age_s(chain.get("asOf"))
    hm = str(chain.get("asOf") or "")[11:19]
    if age is None or age < -_SKEW_TOL_S:
        return {"mode": "delayed", "detail": "connector", "label": "via connector · poll-rate"}
    if age < 120:  # fresh -> tick/LIVE; the "Ns ago" counter is rendered client-side and ticks live
        shown = max(0, int(age))
        return {"mode": "tick", "detail": "connector", "age": shown,
                "label": f"● LIVE (connector) · asOf {hm}Z · {shown}s ago"}
    lab = f"{int(age // 3600)}h" if age >= 3600 else f"{int(age // 60)}m"
    return {"mode": "delayed", "detail": f"connector · {lab} old",
            "label": f"via connector · asOf {hm}Z · {lab} old (not live)"}


def connector_spec(base_url, root, expiry=None, window=None):
    chain = fetch_connector_chain(base_url, root, expiry, window)
    an = chain_to_an(chain, root)
    spec = to_spec(an)
    spec["subtitle"] = (f"Databento GLBX.MDP3 · Black-76 · {an['nStrikes']} strikes · not advice · "
                        f"[{connector_liveness(chain)['label']}]")
    return spec


# ----------------- live (--watch): the provider seam into dashboard.serve -----------------
def _live_spec(root, env_file=None, expiry=None, option_root=None):
    # daycache=False (re-review P3): every poll must see tonight's settles the moment they publish —
    # the DATE-level cache key would otherwise pin the morning body until midnight.
    forward, contracts, expiration, T, fwd_src, under = gather(root, expiry=expiry, env_file=env_file,
                                                               option_root=option_root, daycache=False)
    if not contracts:
        raise RuntimeError(f"no listed options for {root}")
    return to_spec(build_chain(root, forward, contracts, expiration, T, fwd_src=fwd_src,
                               option_root=option_root, under=under))


class _ConnectorProvider(dashboard.Provider):
    """Polls the connector's /smile/:root.json each tick; the liveness badge is RECOMPUTED from the
    chain's asOf every poll, so it flips to green ● LIVE (mode=tick) exactly when the data is fresh
    (connector --live streaming) and to delayed/settlement when it's stale — never claims live falsely."""

    def __init__(self, base_url, root, expiry=None, interval=10.0):
        self.base_url, self.root, self.expiry, self.interval = base_url, root, expiry, interval
        self._last_asof = None

    def _fetch(self):
        chain = fetch_connector_chain(self.base_url, self.root, self.expiry)
        an = chain_to_an(chain, self.root)
        spec = to_spec(an)
        spec["subtitle"] = (f"Databento GLBX.MDP3 · Black-76 · {an['nStrikes']} strikes · not advice · "
                            f"via connector")   # liveness is the green bar's job on the live page (no dup)
        self._last_asof = chain.get("asOf")
        return spec

    def initial(self):
        return self._fetch()

    def poll(self):
        return self._fetch()

    def liveness(self):
        # Re-derive freshness from the LAST seen asOf on every call (not frozen at fetch time): if polls
        # start failing, dashboard.serve still calls liveness(), the asOf ages past 120s, and the badge
        # drops out of ● LIVE into delayed instead of lying green over a dead feed.
        L = connector_liveness({"asOf": self._last_asof})
        return {"mode": L["mode"], "ts": str(self._last_asof or "")[11:19], "detail": L["detail"]}


class _FullConnectorProvider(dashboard.Provider):
    """h3 (--full --watch): the LIVE combined page — the equity make_full_provider mirror. The full
    [Overview · Chain · Term · History] spec builds ONCE (connector term day-cache + the futhist
    store), then each poll re-fetches ONLY the PINNED series from /smile/:root.json and splices its
    fresh chain into the cached page (cards by label — the cross-series trio survives; panels by
    index + the per-series switcher slot). `set_pin` follows the client's expiration pick; the
    liveness badge re-derives from the last smile asOf every call (● LIVE only on a genuinely fresh
    connector feed — settlement wording otherwise, in the header too). Polls return a fresh deep
    copy (the equity alias-free P0 rule: a shared object would freeze diff_spec at first paint)."""

    def __init__(self, base_url, root, expiry=None, interval=10.0, option_root=None):
        self.base_url, self.root, self.interval = base_url, root, interval
        self.option_root = option_root
        self._expiry = expiry                     # the CLI-requested target (a date), build-time only
        self._st = {"base": None, "pin": None, "expiries": []}
        self._last_asof = None

    def _build(self):
        import futhist   # lazy — futhist imports THIS module at its top (cycle guard)
        payload = fetch_connector_term(self.base_url, self.root)
        TT = gather_term(self.root, expiry=self._expiry, option_root=self.option_root,
                         fetch=_term_shim(payload))
        S = TT["series"]
        ans = [build_chain(self.root, s["fwd"], s["contracts"], s["expiration"], s["T"],
                           fwd_src="underlying", option_root=self.option_root,
                           under=s.get("under"), ladder="coarsened") for s in S]
        specs = [to_spec(a_i) for a_i in ans]
        target_idx = next((i for i, s in enumerate(S)
                           if s["stem"] == TT["target"]["stem"]
                           and s["expiration"] == TT["target"]["expiration"]), 0)
        hist = futhist.load_store(self.root)
        spec = build_full_spec(self.root, TT, specs, target_idx, hist,
                               as_of=payload.get("asOf"))
        spec["active"] = "smile"                  # live leads on the streaming tab (equity parity)
        spec["liveSection"] = "smile"
        self._st.update(base=spec, pin=S[target_idx]["expiration"],
                        expiries=[s["expiration"] for s in S])
        self._last_asof = payload.get("asOf")
        return json.loads(json.dumps(dashboard._with_active(spec)))

    def initial(self):
        return self._build()

    def poll(self):
        if self._st["base"] is None:
            return self._build()
        chain = fetch_connector_chain(self.base_url, self.root, self._st["pin"])
        self._last_asof = chain.get("asOf") or self._last_asof
        an = chain_to_an(chain, self.root)
        fresh = to_spec(an)
        base = self._st["base"]
        sec = next(s for s in base["sections"] if s["id"] == "smile")
        pin = chain.get("expiration") or self._st["pin"]
        _ix = self._st["expiries"].index(pin) if pin in self._st["expiries"] else None
        sw = sec.get("switcher") or {}
        if _ix is not None:
            sw["index"] = _ix                     # the dropdown follows the live re-pin
        fby = {c["label"]: c for c in fresh["cards"]}
        for c in sec["cards"]:                    # refresh matching tiles; the trio has no twin -> stays
            fc = fby.get(c["label"])
            if fc:
                c["value"] = fc["value"]
                if fc.get("scopeLabel"):
                    c["scopeLabel"] = fc["scopeLabel"]
        if _ix is not None and isinstance(sec.get("cardSeries"), list) and _ix < len(sec["cardSeries"]):
            sec["cardSeries"][_ix] = [dict(c) for c in sec["cards"]]
        for i, p in enumerate(fresh["panels"]):   # splice the shown chain panels + the per-series slot
            if i < len(sec["panels"]):
                sec["panels"][i]["data"] = p["data"]
                _ser = sec["panels"][i].get("series")
                if _ix is not None and isinstance(_ser, list) and _ix < len(_ser):
                    _ser[_ix] = p["data"]
        if base.get("head") and an.get("forward") is not None:
            L = connector_liveness(chain)         # header honesty follows the SAME liveness the badge shows
            base["head"]["rightBig"] = "$" + _num(an["forward"])
            base["head"]["rightSmall"] = ("live · connector feed" if L["mode"] == "tick"
                                          else "settlement · connector")
        return json.loads(json.dumps(dashboard._with_active(base)))

    def liveness(self):
        L = connector_liveness({"asOf": self._last_asof})
        return {"mode": L["mode"], "ts": str(self._last_asof or "")[11:19], "detail": L["detail"]}

    def set_pin(self, exp):
        self._st["pin"] = exp                     # the next poll re-polls this series


def make_full_provider(root, url, expiry=None, interval=10.0, option_root=None):
    """The h3 live combined-page provider (--full --watch) — connector-only: the /smile poll is the
    keyless cheap path and the term day-cache makes the one-time build fast on a warmed day."""
    return _FullConnectorProvider(url, root, expiry=expiry, interval=interval,
                                  option_root=option_root)


def make_provider(root, interval=60.0, env_file=None, expiry=None, source="databento", url=None,
                  produce=None, option_root=None):
    """A dashboard provider (DD-3) for one futures-options smile. `source="connector"` POLLS the
    databento-mcp-server smile endpoint (keyless; green ● LIVE when the connector runs --live) — the
    single scheme; `source="databento"` is the direct settlement fallback. `produce` is injectable for
    tests (skip the network)."""
    if produce is not None:
        opt = (option_root or resolve_option_root(root)).upper()
        return dashboard.CallableProvider(produce, interval=interval, mode="settlement",
                                          detail=f"{opt} · polled")
    if source == "connector":
        return _ConnectorProvider(url, root, expiry, interval=interval)
    opt = (option_root or resolve_option_root(root)).upper()
    return dashboard.CallableProvider(lambda: _live_spec(root, env_file, expiry, option_root),
                                      interval=interval, mode="settlement",
                                      detail=f"{opt} · Databento settlement · polled")


def _spec_text(spec):
    return spec.get("title", "") + "\n" + " · ".join(
        f"{c['label']} {c['value']}" for c in spec.get("cards", []))


def _connector_up(base_url, timeout=2.5):
    """Is the connector reachable? (Any HTTP response — even 404 — means it's listening.)"""
    try:
        urllib.request.urlopen(base_url.rstrip("/") + "/", timeout=timeout)
        return True
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False


def main(argv=None):
    ap = argparse.ArgumentParser(description="Futures-options volatility smile (Databento, Black-76).")
    ap.add_argument("root", help="futures ROOT only, e.g. PA, CL, GC, ES — never an option code (PAO) "
                                 "or a dated contract (PAOU6/ESU6); the script resolves those itself")
    ap.add_argument("--source", choices=["auto", "connector", "databento"], default="auto",
                    help="auto (default) = use the connector if it's running (keyless), else direct; "
                         "connector = POLL the databento-mcp-server smile endpoint (keyless, live if it "
                         "runs --live); databento = direct settlement fetch (needs a key). Agents: pin "
                         "the source explicitly — auto flips by environment state")
    ap.add_argument("--url", default=_URL_DEFAULT,
                    help="connector base URL (default http://127.0.0.1:8768, or $SMILE_WEB_URL)")
    ap.add_argument("--option-root", help="override the option asset code for an unmapped root "
                                          "(e.g. Brent: BZ --option-root BZO)")
    ap.add_argument("--fresh-defs", action="store_true",
                    help="force a LIVE definition listing, bypassing the local defs snapshot "
                         "(v1.129: a ≤%dd-old snapshot from the futures store serves the listing "
                         "with zero network by default; newly-listed strikes/series need this flag "
                         "before tonight's refresh)" % _defs_ttl_days())
    ap.add_argument("--full", action="store_true",
                    help="the COMBINED tabbed page [Overview · Chain · Term · History]: the --term "
                         "chain+term drill PLUS the retrospective Overview/History from the futhist "
                         "settlement store (cold store -> honest 'warming' tabs; fill with "
                         "futhist.py ROOT --backfill). Add --watch N for the LIVE page — the Chain "
                         "tab streams via the connector /smile poll (h3).")
    ap.add_argument("--term", action="store_true",
                    help="the chain page (default or --expiry series) + CROSS-expiration analytics: "
                         "ATM-IV/skew term curve + moneyness×expiry IV surface (per-series forwards) + a "
                         "series table. SOURCE connector-first — the connector's day-cached /term makes "
                         "repeat runs instant (--source databento = the direct path: gzip'd, id-scoped "
                         "stats, same-day repeats ride a local day-cache — the first run of a day is "
                         "still the slow one); combines "
                         "with --expiry; one-shot — --watch/--artifact do not apply")
    ap.add_argument("--list-expiries", action="store_true",
                    help="print the LISTED series (stem · expiration · DTE · strikes · kind · underlying) "
                         "from the Databento definition listing and exit — no market-data schema, but the "
                         "whole-root defs pull itself runs ~40-60s+ on big roots (same-day repeats ride "
                         "the day-cache); needs the Databento key even with --source connector")
    ap.add_argument("--list-series", action="store_true",
                    help="the FIRST QUESTION: print the option-expiration series from the connector's "
                         "keyless /series (nearest + strike count, instant from the long-lived defs "
                         "catalog) and exit — present this and ask which expiration BEFORE the heavy chain "
                         "pull; add --liquidity to also rank by open interest (most-liquid series)")
    ap.add_argument("--liquidity", action="store_true",
                    help="with --list-series: also pull open interest so the most-liquid series is marked "
                         "(one whole-root OI pull, connector day-cached after the first)")
    ap.add_argument("--html", help="write a self-contained dashboard here (via dashboard.py)")
    ap.add_argument("--json", action="store_true", help="print the dashboard spec as JSON")
    ap.add_argument("--watch", type=_arg_watch, nargs="?", const=60, default=None, metavar="SECONDS",
                    help="LIVE panel: serve a self-updating dashboard, re-fetching every N seconds (default 60)")
    ap.add_argument("--port", type=_arg_port, default=8788, help="port for --watch live server (default 8788)")
    ap.add_argument("--open", action="store_true", help="open the --watch page in the default browser")
    ap.add_argument("--expiry", type=_arg_date,
                    help="expiration YYYY-MM-DD -> the listed series within ±7 days of it, else an error "
                         "listing the real expirations (default: nearest quarterly series)")
    ap.add_argument("--env-file", help="path to a .env with DATABENTO_API_KEY (direct source only)")
    ap.add_argument("--from-dir", help="Cowork/MCP fallback: build from a saved get_futures_options_smile "
                                       "result in this directory (filename contains 'smile'/'chain'); no network")
    ap.add_argument("--artifact", metavar="PATH",
                    help="write a Cowork LIVE page: the dashboard polls get_futures_options_smile via "
                         "window.cowork.callMcpTool (smile/OI/cards tick; greeks/table stay as rendered). "
                         "Open it with create_artifact in a Cowork session")
    a = ap.parse_args(argv)

    for w in _warn_ignored(a):                           # §0.5: never swallow an inapplicable flag silently
        print(w, file=sys.stderr)

    if a.list_series:                                    # FIRST QUESTION: keyless connector /series listing
        if not _connector_up(a.url):
            sys.exit(f"error: the connector is not reachable at {a.url} — start it (connector-web) to list "
                     f"series, or use --list-expiries for the Databento-direct definition listing")
        try:
            payload = fetch_connector_series(a.url, a.root, liquidity=a.liquidity)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as e:
            sys.exit(f"error: connector /series fetch failed ({e}) — retry, or use --list-expiries "
                     f"(Databento-direct definition listing)")
        if not (payload.get("series") or []):
            sys.exit(f"(no listed option series for {a.root.upper()} on the connector)")
        print(render_series_listing(payload))
        return

    if a.list_expiries:                                  # validate-before-invoke: definition listing ONLY
        try:
            lines = series_lines(_list_series(a.root, _make_fetch(DATASET, a.env_file),
                                              dt.date.today(), a.option_root, fresh=a.fresh_defs))
        except RuntimeError as e:
            sys.exit(str(e))
        print("\n".join(lines) if lines else f"(no listed option series for {a.root.upper()})")
        return

    if a.term or a.full:
        mode = "--full" if a.full else "--term"
        if a.from_dir:
            sys.exit(f"error: {mode} needs the Databento historical path (saved chains hold ONE series); "
                     f"drop --from-dir")
        if a.full and a.watch is not None:
            # h3: the LIVE combined page — connector-only (the keyless /smile poll + the term day-cache)
            if not _connector_up(a.url):
                sys.exit(f"error: --full --watch needs the running connector at {a.url} (the keyless "
                         f"/smile poll) — start connector-web, or drop --watch for the one-shot page")
            print(f"serving the LIVE {a.root.upper()} combined page — the Chain tab streams "
                  f"(poll every {a.watch}s); a cold term day-cache makes the FIRST build slow "
                  f"(the loading page shows until it lands)", file=sys.stderr)
            dashboard.serve(make_full_provider(a.root, a.url, expiry=a.expiry, interval=a.watch,
                                               option_root=a.option_root),
                            port=a.port, open_page=a.open)
            return
        for note in _openai_heavy_env_guard(fresh_defs=a.fresh_defs):
            print("note: " + note, file=sys.stderr)
        _lease = None
        if a.html:
            try:
                _lease = _OutputLease(a.html)
            except RuntimeError as e:
                sys.exit("error: %s" % e)
        try:
            if _packaged_openai():
                print("OpenAI one-shot heavy render: keep this process alive for up to 900s; capture this "
                      "first run's log and do not rerun it. The output lease blocks concurrent writers.",
                      file=sys.stderr)
            for flag, name in ((a.watch, "--watch"), (a.artifact, "--artifact")):
                if flag is not None:
                    print(f"warning: {name} does not apply to {mode} (one-shot) — ignored", file=sys.stderr)
            # Source: connector-FIRST (its /term/:root.json is day-cached — repeat runs are instant;
            # the first run of a trading day is the same slow Databento side either way). Explicit
            # --source databento keeps the direct path.
            tsource = a.source
            if tsource == "auto":
                tsource = "connector" if _connector_up(a.url) else "databento"
            term_note = ""
            try:   # COMBINED page: the chosen series (--expiry or nearest quarterly) + the term analytics
                if tsource == "connector":
                    if not _connector_up(a.url):
                        sys.exit(f"error: --source connector pinned but the connector at {a.url} is not "
                                 f"reachable — start it (or use --source databento)")
                    cached = connector_term_cached(a.url, a.root)
                    if cached is True:
                        print(f"{a.root.upper()} term structure is already in the connector's day-cache "
                              f"— rendering now, no wait.", file=sys.stderr)
                    elif cached is False:
                        print(f"warming the {a.root.upper()} term structure: first request of the trading day "
                              f"pulls it from Databento (minutes on big roots like GC/ES/CL; the connector "
                              f"day-caches it, so every later request today is instant)…", file=sys.stderr)
                    try:
                        payload = fetch_connector_term(a.url, a.root)
                    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as e:
                        sys.exit(f"error: connector /term fetch failed ({e}) — the first pull of a trading "
                                 f"day runs the slow Databento side; retry, or use --source databento")
                    TT = gather_term(a.root, expiry=a.expiry, option_root=a.option_root,
                                     fetch=_term_shim(payload))
                    term_note = f" · via connector day-cache (asOf {payload.get('asOf', '?')})"
                else:
                    TT = gather_term(a.root, expiry=a.expiry, env_file=a.env_file,
                                     option_root=a.option_root, fresh_defs=a.fresh_defs)
            except (RuntimeError, ValueError) as e:   # same pulls either way (parent-level superset)
                sys.exit(str(e))
            # Build EVERY series' chain page (not just the target) so the expiration switcher can swap
            # them client-side — greeks stay Black-76 in Python, computed once here.
            S = TT["series"]
            # Strike-set provenance for the honest chain label: the connector /term pull is a coarsened
            # near-money grid (term-data.ts caps strikes/series); the direct Databento path is the full ladder.
            term_ladder = "coarsened" if tsource == "connector" else "full"
            ans = [build_chain(a.root, s["fwd"], s["contracts"], s["expiration"], s["T"],
                               fwd_src="underlying", option_root=a.option_root, under=s.get("under"),
                               ladder=term_ladder) for s in S]
            specs = [to_spec(a_i) for a_i in ans]
            # (stem, expiration) — stem uniqueness is an exchange convention, not an invariant (see _list_series)
            target_idx = next((i for i, s in enumerate(S)
                               if s["stem"] == TT["target"]["stem"]
                               and s["expiration"] == TT["target"]["expiration"]), None)
            if target_idx is None:   # e.g. the target series had no usable strikes — never show the wrong series silently
                sys.exit(f"error: the requested series {TT['target']['stem']} ({TT['target']['expiration']}) "
                         f"has no chartable strikes on the term page — pick another --expiry, or run without --term")
            an = ans[target_idx]   # the default series feeds the text output
            hist = None
            if a.full:
                import futhist    # lazy — futhist imports THIS module at its top (cycle guard)
                hist = futhist.load_store(a.root)
                if not hist:
                    print(f"note: the {a.root.upper()} futures vol-history store is COLD — Overview/History "
                          f"render as 'warming'. Fill them:  python3 scripts/futhist.py {a.root.upper()} "
                          f"--backfill --years 1", file=sys.stderr)
                # h1: NO subtitles on the --full page (the term_note/day-cache provenance goes to stderr
                # only; the page carries as-of in the lead meta and the source in the footer)
                _asof = None
                if tsource == "connector":
                    try:
                        _asof = payload.get("asOf")
                    except NameError:
                        _asof = None
                spec = build_full_spec(a.root, TT, specs, target_idx, hist,
                                       as_of=_asof or dt.date.today().isoformat())
            else:
                spec = combined_spec(specs, TT, target_idx)
                if term_note:
                    spec["subtitle"] = spec.get("subtitle", "") + term_note
                    sw = spec.get("switcher") or {}
                    sw["subtitles"] = [s + term_note for s in sw.get("subtitles", [])]   # the note survives a switch
            if a.json:
                print(json.dumps(spec, separators=(",", ":")))
            else:
                note_line = ("\n" + term_note.strip(" ·")) if term_note else ""
                hist_line = ""
                if a.full:
                    hist_line = ("\n\nHISTORY · %d sessions · %s..%s (futhist store)"
                                 % (len(hist), hist[0].get("date", "n/a"),
                                    hist[-1].get("date", "n/a"))) if hist else \
                                "\n\nHISTORY · store cold — futhist.py %s --backfill" % a.root.upper()
                print(render_text(an) + note_line + "\n\n" + render_term_text(TT) + hist_line)
            if a.html:
                dashboard.deliver_html(a.html, dashboard.render_interactive_html(spec),
                                       offers=offer_commands(a.root))
                print(f"\nwrote {a.html}", file=sys.stderr)
        finally:
            if _lease is not None:
                _lease.close()
        return

    if a.from_dir or a.artifact:
        # Cowork path: initial spec from a saved chain (or a live fetch when no dir given),
        # then either a static render or the bridge-live artifact page.
        if a.from_dir:
            chain = read_chain_dir(a.from_dir)
        else:
            chain = fetch_connector_chain(a.url, a.root, expiry=a.expiry) \
                if _connector_up(a.url) else None  # a.expiry is a validated STRING (_arg_date), never a date
            if chain is None:
                sys.exit("error: --artifact needs --from-dir (a saved smile result) or a running connector")
        an = chain_to_an(chain, a.root)
        spec = to_spec(an)
        if a.artifact:
            # the bridge JS updates cards 0..5 by index — greeks/Exp move/table stay as first rendered
            spec["subtitle"] += " · greeks/exp-move/table as of initial render"
            bridge = {"tool": "get_futures_options_smile",
                      "args": {"root": a.root.upper(),
                               "expiry": chain.get("expiration") or a.expiry or "nearest",
                               "window": 20},
                      "intervalMs": int((a.watch or dashboard.POLL_DEFAULT_S) * 1000), "mapper": "foBridgeDelta"}
            page = dashboard.render_bridge_html(spec, bridge, liveness=connector_liveness(chain),
                                                mapper_js=FO_BRIDGE_JS)
            pathlib.Path(a.artifact).write_text(page, encoding="utf-8")
            print(f"{a.artifact}  |  {a.root.upper()} bridge-live dashboard (Cowork artifact)", file=sys.stderr)
            return
        if a.html:
            pathlib.Path(a.html).write_text(dashboard.render_interactive_html(spec), encoding="utf-8")
            print(f"{a.html}  |  {a.root.upper()} dashboard (from saved chain)", file=sys.stderr)
            return
        if a.json:
            print(json.dumps(spec, indent=1))
            return
        print(render_text(an))
        return

    source = a.source
    if source == "auto":                                 # keyless connector if it's up, else direct
        source = "connector" if _connector_up(a.url) else "databento"
        print(f"source=auto → {source}" + (f" ({a.url})" if source == "connector" else " (direct fetch)"),
              file=sys.stderr)
    if a.option_root and source == "connector":
        sys.exit("error: --option-root is only supported with --source databento; the connector exposes roots only")

    if a.watch is not None:  # live panel — one server per dashboard (DD-4)
        dashboard.serve(make_provider(a.root, interval=a.watch, env_file=a.env_file, expiry=a.expiry,
                                      source=source, url=a.url, option_root=a.option_root),
                        port=a.port, open_page=a.open)
        return

    if source == "connector":                            # PREFERRED: consume the connector (keyless)
        try:
            spec, an = connector_spec(a.url, a.root, a.expiry), None
        except dashboard.Warming as e:  # cold start on the one-shot path: a message, not a traceback
            sys.exit(f"{e} — the connector is still seeding this series; retry in a few seconds")
        except urllib.error.HTTPError as e:   # non-503 HTTP: unknown root / connector-side error
            sys.exit(f"error: connector at {a.url} returned HTTP {e.code} for {a.root} — check the root "
                     f"(the connector exposes mapped roots only), or use --source databento")
        except OSError as e:                  # connector down / refused / timeout — a mainline mode
            sys.exit(f"error: connector not reachable at {a.url} ({getattr(e, 'reason', e)}) — start the "
                     f"databento connector, or use --source databento for the settlement chain")
    else:                                                # fallback: direct settlement fetch (needs key)
        try:
            forward, contracts, expiration, T, fwd_src, under = gather(a.root, expiry=a.expiry,
                                                                        env_file=a.env_file,
                                                                        option_root=a.option_root, echo=True,
                                                                        fresh_defs=a.fresh_defs)
        except (ValueError, RuntimeError) as e:          # bad --expiry / unknown root: a message, not a trace
            sys.exit(str(e))
        if not contracts:
            sys.exit(f"error: no listed options for {a.root} (check the root / entitlement)")
        fwd_warn = {"continuous": "warning: forward is fwd≈continuous front (verify month)",
                    "mid-strike": "warning: forward is fwd≈mid strike (no forward data)"}.get(fwd_src)
        if fwd_warn:
            print(fwd_warn, file=sys.stderr)
        an = build_chain(a.root, forward, contracts, expiration, T, fwd_src=fwd_src, option_root=a.option_root,
                         under=under)
        spec = to_spec(an)

    if a.json:
        print(json.dumps(spec, separators=(",", ":")))
        return
    print(render_text(an) if an is not None else _spec_text(spec))
    if a.html:
        dashboard.deliver_html(a.html, dashboard.render_interactive_html(spec),
                               offers=offer_commands(a.root))
        print(f"\nwrote {a.html}")


if __name__ == "__main__":
    main()
