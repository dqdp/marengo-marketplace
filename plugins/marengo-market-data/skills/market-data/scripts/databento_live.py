#!/usr/bin/env python3
"""
databento_live.py — futures-options LIVE backend (reducer) for the options.py SSE
front-end. Pure stdlib + greeks.py; **NO `databento` package**. The DBN/CRAM Live socket
lives in the databento MCP extension and feeds NORMALIZED records to this reducer over a
local side-channel stream (see CLAUDE.md "Next Slice"). TDD step 2 of the futures-live slice.

This turns a stream of normalized Databento records into the SAME chain dict that
options.py's `_diff` + renderer consume, so the live front-end is reused unchanged. IV and
greeks are computed locally via Black-76 (greeks.py), warm-started per strike from the
previous chain — so a per-tick re-solve is cheap (see greeks.implied_vol(guess=...)).

Normalized record shapes (a DBN decoder produces these; prices already /1e9, UNDEF -> None):
  {"type":"definition","instrument_id":int,"instrument_class":"C"|"P"|"F","strike":float|None,
   "expiration":"YYYY-MM-DD","underlying":str}
  {"type":"quote","instrument_id":int,"bid":float|None,"ask":float|None,"ts":str|None}
  {"type":"statistics","instrument_id":int,"stat_type":"open_interest","value":float}
"""
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import greeks as _g  # noqa: E402


def new_state():
    """The chain buffer: instrument definitions, latest quotes, open interest, the future id."""
    return {"defs": {}, "quotes": {}, "oi": {}, "future_id": None, "last_ts": None}


def _mid(bid, ask):
    if bid is not None and ask is not None:
        return (bid + ask) / 2.0
    return bid if bid is not None else ask  # one-sided fallback; None if both UNDEF


def apply_tick(state, rec):
    """Fold one normalized record into the buffer (mutates + returns state)."""
    t = rec.get("type")
    iid = rec.get("instrument_id")
    if t == "definition":
        state["defs"][iid] = {"class": rec.get("instrument_class"), "strike": rec.get("strike"),
                              "expiration": rec.get("expiration"), "underlying": rec.get("underlying")}
        if rec.get("instrument_class") == "F":
            state["future_id"] = iid
    elif t == "quote":
        m = _mid(rec.get("bid"), rec.get("ask"))
        prev = state["quotes"].get(iid)
        ts = rec.get("ts")
        # v1.126: the stream is NOT assumed strictly ordered — a late-arriving OLDER tick (both
        # stamped) must not overwrite a newer quote nor regress last_ts; an equal ts (the same
        # event re-sent) stays an idempotent overwrite. A BOOK CLEAR leaves its stamp in
        # state["cleared"] (re-review P3: popping the quote alone forgot the ordering memory —
        # a late stale tick would have resurrected the cleared level).
        known_ts = (prev or {}).get("ts") or state.get("cleared", {}).get(iid)
        if ts and known_ts and ts < known_ts:
            return state
        if m is not None:
            state["quotes"][iid] = {"bid": rec.get("bid"), "ask": rec.get("ask"), "mid": m, "ts": ts}
            state.get("cleared", {}).pop(iid, None)
            if ts and (not state.get("last_ts") or ts >= state["last_ts"]):
                state["last_ts"] = ts
        elif prev is not None and "bid" in rec and "ask" in rec \
                and rec["bid"] is None and rec["ask"] is None:
            # both sides UNDEF with the keys PRESENT = a book clear — the previous quote must not
            # stay live forever (v1.126); a malformed/keyless record still just drops.
            state["quotes"].pop(iid, None)
            if ts:
                state.setdefault("cleared", {})[iid] = ts
    elif t == "statistics" and rec.get("stat_type") == "open_interest":
        state["oi"][iid] = rec.get("value")
    return state


def forward(state):
    """The underlying future's mid (the Black-76 forward), or None if not seen yet."""
    fid = state.get("future_id")
    q = state["quotes"].get(fid) if fid is not None else None
    return q["mid"] if q else None


def build_chain(symbol, state, expiration, T, *, window=20, r=0.0, prev=None):
    """Reduce the buffer into an options.py-compatible chain dict for one expiration. IV is
    solved per strike from the quote mid via Black-76, warm-started from `prev` (the previous
    chain) so unchanged strikes re-solve in one iteration. Mirrors options.build()'s keys."""
    F = forward(state)
    if F is None or F <= 0:
        raise ValueError("no future quote yet (forward unknown)")

    calls, puts = {}, {}   # strike -> (instrument_id, mid)
    for iid, d in state["defs"].items():
        if d["class"] not in ("C", "P") or d["expiration"] != expiration or d["strike"] is None:
            continue
        q = state["quotes"].get(iid)
        if q:
            (calls if d["class"] == "C" else puts)[float(d["strike"])] = (iid, q["mid"])
    strikes = sorted(set(calls) | set(puts))
    if not strikes:
        raise ValueError("no option quotes yet")

    seed_c, seed_p = {}, {}  # warm-start seeds from the previous chain, per strike
    if prev:
        for j, K in enumerate(prev.get("strikes", [])):
            seed_c[K], seed_p[K] = prev["callIV"][j], prev["putIV"][j]

    def solve(K, mid, is_call, seed):
        iv = _g.implied_vol(mid, K, T, F=F, r=r, is_call=is_call, model="b76", guess=seed)
        d = _g.black76(F, K, T, iv, r=r, is_call=is_call).delta if iv else None
        return iv, d

    civ, piv, cdl, pdl, coi_l, poi_l = {}, {}, {}, {}, {}, {}
    for K in strikes:
        if K in calls:
            iid, mid = calls[K]
            civ[K], cdl[K] = solve(K, mid, True, seed_c.get(K))
            coi_l[K] = state["oi"].get(iid)
        if K in puts:
            iid, mid = puts[K]
            piv[K], pdl[K] = solve(K, mid, False, seed_p.get(K))
            poi_l[K] = state["oi"].get(iid)

    atm = min(strikes, key=lambda k: abs(k - F))
    ai = strikes.index(atm)
    win = strikes[max(0, ai - window): min(len(strikes), ai + window + 1)]

    atm_civ, atm_piv = civ.get(atm), piv.get(atm)
    atm_iv = next((x for x in [(atm_civ + atm_piv) / 2 if (atm_civ is not None and atm_piv is not None) else None,
                               atm_civ, atm_piv] if x is not None), None)

    # 25-delta skew over the whole expiration
    c25 = min(((K, cdl[K]) for K in calls if cdl.get(K) is not None and 0 < cdl[K] < 1),
              key=lambda kd: abs(kd[1] - 0.25), default=None)
    p25 = min(((K, pdl[K]) for K in puts if pdl.get(K) is not None and -1 < pdl[K] < 0),
              key=lambda kd: abs(kd[1] + 0.25), default=None)
    call25IV = civ.get(c25[0]) if c25 else None
    put25IV = piv.get(p25[0]) if p25 else None
    skew25 = (put25IV - call25IV) if (call25IV is not None and put25IV is not None) else None

    coi = sum(v for v in coi_l.values() if v) or 0
    poi = sum(v for v in poi_l.values() if v) or 0
    pcr_oi = poi / coi if coi else None

    def payout(Kp):
        cp = sum((coi_l.get(K) or 0) * (Kp - K) for K in calls if K < Kp)
        pp = sum((poi_l.get(K) or 0) * (K - Kp) for K in puts if K > Kp)
        return cp + pp
    max_pain = min(strikes, key=payout) if (coi or poi) else atm

    exps = sorted({d["expiration"] for d in state["defs"].values() if d["class"] in ("C", "P")})
    return {
        "symbol": symbol, "expiration": expiration, "dte": max(0, round(T * 365)),
        "asOf": state.get("last_ts"), "spot": F, "spotEstimated": False,
        "atmStrike": atm, "atmIV": atm_iv, "skew25": skew25, "call25IV": call25IV, "put25IV": put25IV,
        "call25Strike": c25[0] if c25 else None, "put25Strike": p25[0] if p25 else None,
        "pcrOI": pcr_oi, "pcrVol": None, "callOItotal": coi, "putOItotal": poi,
        "maxPain": max_pain, "nContracts": len(calls) + len(puts),
        "nExpirations": len(exps), "expirations": exps, "window": window, "strikes": win,
        "callIV": [civ.get(K) for K in win], "putIV": [piv.get(K) for K in win],
        "callOI": [coi_l.get(K) for K in win], "putOI": [poi_l.get(K) for K in win],
        "callDelta": [cdl.get(K) for K in win], "putDelta": [pdl.get(K) for K in win],
        "callVol": [None for _ in win], "putVol": [None for _ in win],
    }
