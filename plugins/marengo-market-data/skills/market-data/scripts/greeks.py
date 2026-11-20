#!/usr/bin/env python3
"""
greeks.py — option pricing, greeks, and implied volatility.

Two models, one interface:
  - Black-Scholes(-Merton): options on a SPOT underlying (equities, ETFs, FX, crypto).
    Supports a continuous dividend / carry yield q.
  - Black-76: options on a FUTURE (CME/NYMEX/COMEX futures options via Databento).
    Use the futures price F as the underlying; no dividend term.

Why this exists: Databento returns NO greeks/IV for futures options, so compute
them here. (Alpha Vantage already returns native greeks/IV for equity/ETF options
via REALTIME_OPTIONS, so you usually do NOT need this for that branch.)

Conventions:
  - Rates/vols/yields are decimals per year (0.05 = 5%, 0.20 = 20%).
  - T is time to expiry in YEARS (calendar). For short-dated options, calendar
    time with r≈0 is fine; pass a real r/q if you have them.
  - theta is returned PER YEAR; divide by 365 for per-calendar-day.
  - vega/rho are per 1.00 (i.e. per 100 vol points / per 100% rate). Divide by
    100 for "per 1 vol point" / "per 1% rate".
  - Black-76 delta is w.r.t. the FUTURE price.

CLI examples:
  # implied vol + greeks of a futures call from its market price (Black-76)
  python3 greeks.py iv --model b76 --type call --F 7467 --K 7575 --T 0.0282 --price 24.75

  # price + greeks at a known vol (Black-Scholes, equity with 0.5% div yield)
  python3 greeks.py price --model bs --type put --S 282.5 --K 280 --T 0.05 --sigma 0.26 --q 0.005

  # solve the strike for a target delta (e.g. 25-delta call), Black-76
  python3 greeks.py strike --model b76 --type call --F 7467 --T 0.0282 --sigma 0.13 --target-delta 0.25
"""

from __future__ import annotations
import argparse
import math
from dataclasses import dataclass, asdict

SQRT2 = math.sqrt(2.0)
SQRT_2PI = math.sqrt(2.0 * math.pi)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / SQRT2))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / SQRT_2PI


# --------------------------------------------------------------------------- #
# Core: a unified d1/d2 using a "forward" F and discount factor DF.
# Black-Scholes spot maps to this with F = S*exp((r-q)T), DF = exp(-rT).
# Black-76 maps with F = futures price, DF = exp(-rT).
# --------------------------------------------------------------------------- #
def _d1_d2(F: float, K: float, T: float, sigma: float):
    if F <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        raise ValueError("F, K, T, sigma must be positive")
    v = sigma * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sigma * sigma * T) / v
    return d1, d1 - v


@dataclass
class Greeks:
    price: float
    delta: float
    gamma: float
    vega: float      # per 1.00 vol (per 100 vol pts); /100 for per vol point
    theta: float     # per YEAR; /365 for per calendar day
    rho: float       # per 1.00 rate; /100 for per 1% rate
    d1: float
    d2: float
    iv: float | None = None


def _price_and_greeks(F, K, T, sigma, r, is_call, *, spot=None, q=0.0, model="b76"):
    """
    F   : forward/futures price used in d1/d2.
    spot: the actual underlying spot S (Black-Scholes only) — delta/gamma are
          reported w.r.t. spot for the 'bs' model and w.r.t. F for 'b76'.
    """
    DF = math.exp(-r * T)
    d1, d2 = _d1_d2(F, K, T, sigma)
    Nd1, Nd2 = _norm_cdf(d1), _norm_cdf(d2)
    nd1 = _norm_pdf(d1)
    sign = 1.0 if is_call else -1.0

    # Undiscounted-forward price, then discount.
    price = DF * (sign * (F * (_norm_cdf(sign * d1)) - K * (_norm_cdf(sign * d2))))

    vega = DF * F * nd1 * math.sqrt(T)          # per 1.00 vol
    if model == "bs":
        S = spot if spot is not None else F
        # For BS, F = S*exp((r-q)T) -> delta w.r.t spot:
        delta = sign * math.exp(-q * T) * _norm_cdf(sign * d1)
        gamma = math.exp(-q * T) * nd1 / (S * sigma * math.sqrt(T))
        theta = (
            -(S * math.exp(-q * T) * nd1 * sigma) / (2 * math.sqrt(T))
            - sign * r * K * DF * _norm_cdf(sign * d2)
            + sign * q * S * math.exp(-q * T) * _norm_cdf(sign * d1)
        )
        rho = sign * K * T * DF * _norm_cdf(sign * d2)
        vega = S * math.exp(-q * T) * nd1 * math.sqrt(T)
    else:  # black-76 (futures)
        delta = sign * DF * _norm_cdf(sign * d1)
        gamma = DF * nd1 / (F * sigma * math.sqrt(T))
        # Black-76 time decay (per year): theta = dPrice/dt = -dPrice/dT.
        # With F,K fixed, only DF and d1/d2 depend on T, giving
        #   theta = -(F*DF*n(d1)*sigma)/(2*sqrt(T)) + r*price.
        theta = -(F * DF * nd1 * sigma) / (2 * math.sqrt(T)) + r * price
        rho = -T * price  # b76: dPrice/dr = -T * price (since only DF depends on r)

    return Greeks(price=price, delta=delta, gamma=gamma, vega=vega,
                  theta=theta, rho=rho, d1=d1, d2=d2)


def black_scholes(S, K, T, sigma, r=0.0, q=0.0, is_call=True) -> Greeks:
    """Options on spot S with continuous yield q."""
    F = S * math.exp((r - q) * T)
    return _price_and_greeks(F, K, T, sigma, r, is_call, spot=S, q=q, model="bs")


def black76(F, K, T, sigma, r=0.0, is_call=True) -> Greeks:
    """Options on a future F (CME/NYMEX/COMEX futures options)."""
    return _price_and_greeks(F, K, T, sigma, r, is_call, spot=None, q=0.0, model="b76")


def implied_vol(price, K, T, *, F=None, S=None, r=0.0, q=0.0, is_call=True,
                model="b76", tol=1e-8, max_iter=100, guess=None, return_iters=False):
    """
    Solve implied vol from a market price. Newton step with bisection fallback,
    bracketed on [1e-6, 5.0]. Returns sigma (decimal) or None if no solution
    (e.g. price below intrinsic).

    Warm start: pass `guess` (e.g. the previous tick's IV) to seed the solver instead of
    the cold 0.2 — it converges in fewer Newton steps, which is what makes a per-tick
    re-solve cheap on a live chain. An out-of-bracket guess is ignored (falls back to 0.2).
    With `return_iters=True` returns `(sigma, n_iterations)` (the no-solution / above-bound
    short-circuits return `(None, 0)` / `(hi, 0)`) so the live backend can profile and cap
    `max_iter` low.
    """
    lo, hi = 1e-6, 5.0

    def _ret(sig, n):
        return (sig, n) if return_iters else sig

    def price_at(sig):
        if model == "bs":
            return black_scholes(S, K, T, sig, r=r, q=q, is_call=is_call).price
        return black76(F, K, T, sig, r=r, is_call=is_call).price

    # No-arbitrage bounds check.
    if price <= price_at(lo):
        return _ret(None, 0)
    if price >= price_at(hi):
        return _ret(hi, 0)

    sig = guess if (guess is not None and lo < guess < hi) else 0.2
    for i in range(1, max_iter + 1):
        g = (black_scholes(S, K, T, sig, r=r, q=q, is_call=is_call) if model == "bs"
             else black76(F, K, T, sig, r=r, is_call=is_call))
        diff = g.price - price
        if abs(diff) < tol:
            return _ret(sig, i)
        vega = g.vega if g.vega > 1e-12 else 1e-12
        step = diff / vega
        new = sig - step
        if not (lo < new < hi):           # Newton left the bracket -> bisection
            if diff > 0:
                hi = sig
            else:
                lo = sig
            new = 0.5 * (lo + hi)
        else:
            if diff > 0:
                hi = sig
            else:
                lo = sig
        sig = new
    return _ret(sig, max_iter)


def forward_from_parity(call_price, put_price, K, T, r=0.0):
    """Implied forward from put-call parity: C - P = DF*(F - K) -> F = K + (C-P)/DF."""
    DF = math.exp(-r * T)
    return K + (call_price - put_price) / DF


def strike_for_delta(target_delta, T, sigma, *, F=None, S=None, r=0.0, q=0.0,
                     is_call=True, model="b76"):
    """Bisection on strike to hit a target |delta|."""
    base = F if model != "bs" else S
    lo, hi = (base * 0.2, base * 3.0)
    target = abs(target_delta)
    for _ in range(200):
        K = 0.5 * (lo + hi)
        g = (black_scholes(S, K, T, sigma, r=r, q=q, is_call=is_call) if model == "bs"
             else black76(F, K, T, sigma, r=r, is_call=is_call))
        d = abs(g.delta)
        # For calls, delta decreases as K rises; for puts |delta| increases as K rises.
        if is_call:
            if d > target:
                lo = K
            else:
                hi = K
        else:
            if d > target:
                hi = K
            else:
                lo = K
    return 0.5 * (lo + hi)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _add_common(p):
    def _positive_float(s):
        v = float(s)
        if not math.isfinite(v) or v <= 0:
            raise argparse.ArgumentTypeError("must be positive")
        return v

    def _positive_int(s):
        v = int(s)
        if v <= 0:
            raise argparse.ArgumentTypeError("must be a positive integer")
        return v

    p.add_argument("--model", choices=["bs", "b76"], default="b76")
    p.add_argument("--type", dest="kind", choices=["call", "put"], default="call")
    p.add_argument("--F", type=float, help="future/forward price (b76)")
    p.add_argument("--S", type=float, help="spot price (bs)")
    p.add_argument("--K", type=float)
    p.add_argument("--T", type=_positive_float, help="time to expiry in YEARS (or use --dte)")
    p.add_argument("--dte", type=_positive_int, help="days to expiry (= --T dte/365) — the safe alternative when "
                                                     "you have a day count; mutually exclusive with --T")
    p.add_argument("--r", type=float, default=0.0)
    p.add_argument("--q", type=float, default=0.0, help="dividend/carry yield (bs)")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Black-Scholes / Black-76 greeks & IV")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser("price", help="price + greeks at a known sigma")
    _add_common(pp)
    pp.add_argument("--sigma", type=float, required=True)

    pi = sub.add_parser("iv", help="implied vol + greeks from a market price")
    _add_common(pi)
    pi.add_argument("--price", type=float, required=True)

    ps = sub.add_parser("strike", help="strike for a target delta")
    _add_common(ps)
    ps.add_argument("--sigma", type=float, required=True)
    ps.add_argument("--target-delta", type=float, required=True)

    a = ap.parse_args(argv)
    if a.T is not None and a.dte is not None:
        ap.error("--T and --dte are mutually exclusive (years vs days for the same thing)")
    if a.T is None:
        if a.dte is None:
            ap.error("need --T YEARS or --dte DAYS")
        a.T = a.dte / 365.0
    # v1.126: model/underlier mismatch died with a raw TypeError from _d1_d2 — argparse-loud instead
    if getattr(a, "model", None) == "b76" and getattr(a, "F", None) is None:
        ap.error("--model b76 needs --F (the futures/forward price)")
    if getattr(a, "model", None) == "bs" and getattr(a, "S", None) is None:
        ap.error("--model bs needs --S (the spot price)")
    is_call = a.kind == "call"

    if a.cmd == "price":
        g = (black_scholes(a.S, a.K, a.T, a.sigma, r=a.r, q=a.q, is_call=is_call)
             if a.model == "bs"
             else black76(a.F, a.K, a.T, a.sigma, r=a.r, is_call=is_call))
        out = asdict(g)
    elif a.cmd == "iv":
        sig = implied_vol(a.price, a.K, a.T, F=a.F, S=a.S, r=a.r, q=a.q,
                          is_call=is_call, model=a.model)
        if sig is None:
            print("no solution (price below intrinsic?)")
            return
        g = (black_scholes(a.S, a.K, a.T, sig, r=a.r, q=a.q, is_call=is_call)
             if a.model == "bs"
             else black76(a.F, a.K, a.T, sig, r=a.r, is_call=is_call))
        g.iv = sig
        out = asdict(g)
    elif a.cmd == "strike":
        K = strike_for_delta(a.target_delta, a.T, a.sigma, F=a.F, S=a.S, r=a.r,
                             q=a.q, is_call=is_call, model=a.model)
        out = {"strike": K, "target_delta": a.target_delta}

    width = max(len(k) for k in out)
    for k, v in out.items():
        if isinstance(v, float):
            print(f"{k:<{width}} : {v:.6f}")
        else:
            print(f"{k:<{width}} : {v}")


if __name__ == "__main__":
    main()
