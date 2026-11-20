#!/usr/bin/env python3
"""
smoke.py — budget-aware ENTRYPOINT smoke matrix across instruments and scenarios.

The offline test suites (test_*.py) cover units and seams; THIS runner exercises the
real CLIs the way an agent invokes them, across the instrument classes the skill
routes (futures roots, equity/ETF, cash indices, MOEX, crypto), grouped in tiers by
what they cost:

  offline    no network at all: guards, parse-time validators, ignored-flag warnings,
             --help surfaces, --from/--from-dir fixture renders, greeks math (default)
  keyless    MOEX ISS (public, no key)
  metadata   Databento definition-only listings (--list-expiries, unknown-root errors;
             needs DATABENTO_API_KEY, ~free)
  av         Alpha Vantage REST (metered by the daily quota; capped by --av-budget,
             spaced by --av-pause for the 5/min free tier)
  metered    full Databento chain pulls (statistics; costs real money) — NEVER implied
             by 'all'; must be named explicitly

Run:  python3 scripts/smoke.py                       # offline tier only
      python3 scripts/smoke.py --net keyless,metadata
      python3 scripts/smoke.py --net all --av-budget 3
      python3 scripts/smoke.py --only guard --list   # see what would run
Exit 0 = every selected scenario passed, 1 = a failure (skips don't fail the run).
"""
from __future__ import annotations
import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable or "python3"

sys.path.insert(0, HERE)
import futures_options as _fo  # the guard tables ARE the source of truth — never copy them  # noqa: E402
import options as _op  # noqa: E402

TRACEBACK = r"Traceback \(most recent call last\)"


class Scenario:
    def __init__(self, name, tier, argv, exit_code=0, require=(), forbid=(), av_cost=0,
                 out_file=None, timeout=180):
        self.name, self.tier, self.argv = name, tier, argv
        self.exit_code = exit_code            # int, or "nonzero"
        self.require = list(require)          # regexes over combined stdout+stderr
        self.forbid = list(forbid) + [TRACEBACK]
        self.av_cost = av_cost                # AV request count this scenario spends
        self.out_file = out_file              # artifact that must exist and be non-trivial
        self.timeout = timeout

    def run(self):
        p = subprocess.run([PY] + self.argv, capture_output=True, text=True,
                           timeout=self.timeout, cwd=HERE)
        blob = p.stdout + "\n" + p.stderr
        errs = []
        if self.exit_code == "nonzero":
            if p.returncode == 0:
                errs.append("expected nonzero exit, got 0")
        elif p.returncode != self.exit_code:
            errs.append(f"exit {p.returncode} != {self.exit_code}")
        for rx in self.require:
            if not re.search(rx, blob, re.I):
                errs.append(f"missing /{rx}/")
        for rx in self.forbid:
            if re.search(rx, blob):
                errs.append(f"forbidden /{rx}/ present")
        if self.out_file and not (os.path.exists(self.out_file) and os.path.getsize(self.out_file) > 500):
            errs.append(f"artifact missing/empty: {self.out_file}")
        return errs, blob


def s(name, script, args, **kw):
    return Scenario(name, kw.pop("tier", "offline"), [os.path.join(HERE, script)] + args, **kw)


# ---------------------------------------------------------------- fixtures (offline)

def make_fixtures(td):
    """Synthetic but REAL-SHAPED payloads (AV REALTIME_OPTIONS rows, OHLCV csv)."""
    today = dt.date.today()
    # chart --from: plain OHLCV CSV (auto-detected)
    csv_path = os.path.join(td, "series.csv")
    with open(csv_path, "w", encoding="utf-8") as f:
        f.write("date,open,high,low,close,volume\n")
        px = 100.0
        for i in range(60, 0, -1):
            d = today - dt.timedelta(days=i)
            px *= 1.0 + ((i * 7919) % 13 - 6) / 1000.0
            f.write(f"{d.isoformat()},{px:.2f},{px * 1.01:.2f},{px * 0.99:.2f},{px * 1.002:.2f},{1000 + i}\n")
    # options --from-dir: REALTIME_OPTIONS-shaped chain + GLOBAL_QUOTE
    odir = os.path.join(td, "opts")
    os.makedirs(odir, exist_ok=True)
    exp = (today + dt.timedelta(days=14)).isoformat()
    rows = []
    for k in range(90, 111):
        cdelta = max(0.01, min(0.99, 0.5 - (k - 100) * 0.05))
        base = {"symbol": "TST", "expiration": exp, "strike": f"{k}.00", "gamma": "0.01",
                "theta": "-0.05", "vega": "0.1", "volume": "25", "date": today.isoformat()}
        rows.append(dict(base, type="call", implied_volatility=f"{0.15 + 0.002 * abs(k - 100):.5f}",
                         delta=f"{cdelta:.5f}", rho="0.02", open_interest=str(100 + (k % 7) * 30)))
        rows.append(dict(base, type="put", implied_volatility=f"{0.15 + 0.003 * max(0, 100 - k) + 0.001 * abs(k - 100):.5f}",
                         delta=f"{cdelta - 1:.5f}", rho="-0.02", open_interest=str(140 + (k % 5) * 30)))
    json.dump({"endpoint": "Realtime Options", "data": rows},
              open(os.path.join(odir, "TST_realtime_options.json"), "w"))
    json.dump({"Global Quote": {"05. price": "100.40"}},
              open(os.path.join(odir, "TST_global_quote.json"), "w"))
    return csv_path, odir


# ---------------------------------------------------------------- the matrix

def build_scenarios(td):
    csv_path, odir = make_fixtures(td)
    svg_out = os.path.join(td, "from.svg")
    S = []

    # --- classify-first guards: EVERY known futures root must be refused by options.py
    for root in sorted(_fo.OPTION_ROOTS):
        S.append(s(f"guard: options.py refuses futures root {root}", "options.py", [root],
                   exit_code=1, require=[r"FUTURES root", r"futures_options\.py", r"--equity"]))
    # …and every no-GLBX namesake root (ICE softs etc.): honest refusal, NO futures route offered
    for root, (fut, venue, eq) in sorted(_op.NAMESAKE_ROOTS.items()):
        S.append(s(f"guard: options.py refuses namesake root {root} ({eq})", "options.py", [root],
                   exit_code=1, require=[r"FUTURES root", re.escape(eq.split()[0]), r"--equity"],
                   forbid=[r"futures_options\.py"]))
    # cash indices: proxy suggestion; VIX = explicitly no proxy
    for idx, proxy in sorted(_op.INDEX_PROXY.items() if hasattr(_op, "INDEX_PROXY") else
                             _fo.INDEX_PROXY.items()):
        if proxy:
            S.append(s(f"guard: options.py index {idx} -> proxy {proxy}", "options.py", [idx],
                       exit_code=1, require=[rf"\b{proxy}\b", "proxy"]))
        else:
            S.append(s(f"guard: options.py {idx} has NO proxy", "options.py", [idx],
                       exit_code=1, require=["no clean", "futures"], forbid=[r"-> python3.*options\.py [A-Z]"]))

    # --- parse-time validators die at argparse (exit 2), never mid-run
    V = [("chart --interval banana", "chart.py", ["AAPL", "--source", "av-equity", "--interval", "banana"], ["interval"]),
         ("chart --days 0", "chart.py", ["AAPL", "--source", "av-equity", "--days", "0"], ["positive"]),
         ("chart --days -3", "chart.py", ["AAPL", "--source", "av-equity", "--days", "-3"], ["positive"]),
         ("options --watch 0 (below floor; 1s is legal — 2 req/poll vs 600/min)", "options.py",
          ["AAPL", "--watch", "0"], [r"\[1, 3600\]"]),
         ("options --watch 3601 (above cap)", "options.py", ["AAPL", "--watch", "3601"], [r"\[1, 3600\]"]),
         ("options --expiry 2026-13-40", "options.py", ["AAPL", "--expiry", "2026-13-40"], ["YYYY-MM-DD"]),
         ("options --expiry 07/17/2026", "options.py", ["AAPL", "--expiry", "07/17/2026"], ["YYYY-MM-DD"]),
         ("options --window -1", "options.py", ["AAPL", "--window", "-1"], ["positive"]),
         ("options --port 80 (privileged)", "options.py", ["AAPL", "--port", "80"], ["port"]),
         ("futures --watch -5 (the busy-spin killer)", "futures_options.py", ["CL", "--watch", "-5"], [r"\[2, 3600\]"]),
         ("futures --port 70000", "futures_options.py", ["CL", "--port", "70000"], ["port"]),
         ("futures --expiry 17-07-2026", "futures_options.py", ["CL", "--expiry", "17-07-2026"], ["YYYY-MM-DD"]),
         ("portfolio --days -1", "portfolio.py", ["--holdings", "NVDA:100", "--days", "-1"], ["positive"]),
         ]
    for name, script, args, req in V:
        S.append(s("validate: " + name, script, args, exit_code=2, require=req))
    S.append(s("validate: portfolio --holdings NVDA:abc", "portfolio.py",
               ["--holdings", "NVDA:abc"], exit_code="nonzero", require=["TICKER:NUMBER"]))
    # --list-series (the first question) is connector-only: a dead connector must exit CLEAN,
    # pointing at the Databento-direct sibling — never a traceback. Keyless, so offline-safe.
    S.append(s("first-question: --list-series + dead connector -> clean error naming --list-expiries",
               "futures_options.py", ["PA", "--list-series", "--url", "http://127.0.0.1:9"],
               exit_code="nonzero", require=["list-expiries"], forbid=["Traceback"]))
    S.append(s("first-question: --liquidity without --list-series warns (never silent)",
               "futures_options.py", ["PA", "--liquidity", "--from-dir", td],
               exit_code="nonzero", require=[r"warning: --liquidity only applies"]))

    # --- greeks.py math + the --dte surface (pure compute, no network)
    S.append(s("greeks: b76 IV via --dte (golden path)", "greeks.py",
               ["iv", "--model", "b76", "--type", "call", "--F", "7467", "--K", "7575",
                "--dte", "21", "--price", "24.75"], require=[r"iv\s*:\s*0\.09"]))
    S.append(s("greeks: --dte 365 == --T 1 (same IV)", "greeks.py",
               ["iv", "--model", "b76", "--type", "call", "--F", "100", "--K", "100",
                "--dte", "365", "--price", "8.0"], require=[r"iv\s*:\s*0\.20"]))
    S.append(s("greeks: --T and --dte together refused", "greeks.py",
               ["iv", "--model", "b76", "--type", "call", "--F", "100", "--K", "100",
                "--T", "0.1", "--dte", "30", "--price", "2"], exit_code=2, require=["mutually exclusive"]))
    S.append(s("greeks: neither --T nor --dte refused", "greeks.py",
               ["iv", "--model", "b76", "--type", "call", "--F", "100", "--K", "100",
                "--price", "2"], exit_code=2, require=["--T", "--dte"]))
    # designed contract: no-solution is a REPORT, not an error (deep-ITM trades at intrinsic)
    S.append(s("greeks: below-intrinsic price -> honest no-solution (exit 0)", "greeks.py",
               ["iv", "--model", "b76", "--type", "call", "--F", "100", "--K", "90",
                "--dte", "1", "--price", "5"], require=["no solution", "intrinsic"]))
    S.append(s("greeks: bs put price sanity", "greeks.py",
               ["price", "--model", "bs", "--type", "put", "--S", "100", "--K", "100",
                "--dte", "30", "--sigma", "0.2"], require=[r"price\s*:\s*2\.2"]))

    # --- offline artifact paths: --from / --from-dir render without network
    S.append(s("offline render: chart --from CSV -> SVG", "chart.py",
               ["TST", "--from", csv_path, "--out", svg_out],
               require=["bars"], out_file=svg_out))
    S.append(s("offline error: chart --from missing file", "chart.py",
               ["TST", "--from", os.path.join(td, "nope.csv")],
               exit_code="nonzero", require=["not found"]))
    S.append(s("offline render: options --from-dir -> json", "options.py",
               ["TST", "--from-dir", odir, "--json"], require=[r'"symbol":\s*"TST"', r'"strikes"']))
    S.append(s("offline render: options --full --from-dir -> combined tabbed spec (keyless)", "options.py",
               ["TST", "--full", "--from-dir", odir, "--json"],   # cache-state-agnostic: assert the 4-section shape
               require=[r'"sections"', r'"overview"', r'"smile"', r'"term"', r'"history"', r'"cardSeries"']))
    S.append(s("offline warn: options --port without --watch", "options.py",
               ["TST", "--from-dir", odir, "--json", "--port", "9001"],
               require=[r"warning: --port is ignored without --watch"]))
    S.append(s("offline warn: chart --dataset on file source", "chart.py",
               ["TST", "--from", csv_path, "--dataset", "XX.YY", "--out", svg_out],
               require=[r"warning: --dataset is ignored"]))
    S.append(s("offline warn: chart --market on file source", "chart.py",
               ["TST", "--from", csv_path, "--market", "EUR", "--out", svg_out],
               require=[r"warning: --market is ignored"]))
    S.append(s("offline warn: chart --moex-market on file source", "chart.py",
               ["TST", "--from", csv_path, "--moex-market", "shares", "--out", svg_out],
               require=[r"warning: --moex-market is ignored"]))

    # --- every CLI has a working --help (the agent's first probe)
    for script in ["chart.py", "options.py", "futures_options.py", "fundamentals.py",
                   "portfolio.py", "greeks.py", "dashboard.py", "setup_check.py"]:
        S.append(s(f"help: {script} --help", script, ["--help"], require=["usage"]))
    S.append(s("doctor: setup_check --no-net", "setup_check.py", ["--no-net"], require=["Ready"]))

    # --- keyless network: MOEX ISS across instrument types
    moex_out = os.path.join(td, "moex_%s.svg")
    for sym, extra in [("SBER", []), ("GAZP", []), ("LKOH", [])]:
        S.append(s(f"moex: chart {sym} 1d", "chart.py",
                   [sym, "--source", "moex", "--interval", "1d", "--days", "60",
                    "--out", moex_out % sym],
                   tier="keyless", require=["bars"], out_file=moex_out % sym))
    S.append(s("moex: unknown secid fails clean", "chart.py",
               ["ZZZZZZ", "--source", "moex", "--days", "30"],
               tier="keyless", exit_code="nonzero"))

    # --- Databento metadata-only (definition listings; needs the key, ~free)
    for root in ["NG", "CL", "ES", "GC"]:
        S.append(s(f"databento-meta: {root} --list-expiries", "futures_options.py",
                   [root, "--list-expiries"],
                   tier="metadata", require=[r"\d{4}-\d{2}-\d{2}", "DTE", "strikes", "underlying"]))
    S.append(s("databento-meta: quarterly flag present (NG)", "futures_options.py",
               ["NG", "--list-expiries"], tier="metadata", require=["quarterly"]))
    S.append(s("databento-meta: BZ (Brent) lists via BZO since v1.44", "futures_options.py",
               ["BZ", "--list-expiries"],
               tier="metadata", require=[r"\d{4}-\d{2}-\d{2}", "underlying"]))
    S.append(s("databento-meta: unknown root XYZ fails with known-roots list", "futures_options.py",
               ["XYZ", "--source", "databento"],
               tier="metadata", exit_code="nonzero",
               require=["option-root table", "Known roots", "--option-root"]))
    S.append(s("databento-meta: far --expiry lists all series (NG +3y)", "futures_options.py",
               ["NG", "--source", "databento", "--expiry",
                (dt.date.today() + dt.timedelta(days=1095)).isoformat()],
               tier="metadata", exit_code="nonzero", require=["listed series|expirations|series"]))
    S.append(s("databento-meta: warn --url with --source databento", "futures_options.py",
               ["NG", "--list-expiries", "--source", "databento", "--url", "http://127.0.0.1:9999"],
               tier="metadata", require=[r"warning: --url is ignored"]))
    S.append(s("databento-meta: warn --port without --watch", "futures_options.py",
               ["NG", "--list-expiries", "--port", "9009"],
               tier="metadata", require=[r"warning: --port is ignored without --watch"]))

    # --- Alpha Vantage (metered by quota; ordered by information value; av_cost = requests)
    aapl_svg = os.path.join(td, "aapl.svg")
    btc_svg = os.path.join(td, "btc.svg")
    S.append(s("av: options AAPL --json (chain sanity)", "options.py",
               ["AAPL", "--json"], tier="av", av_cost=2,
               require=[r'"symbol":\s*"AAPL"', r'"strikes":\s*\[']))
    S.append(s("av: chart AAPL daily --adjusted (subtitle provenance)", "chart.py",
               ["AAPL", "--source", "av-equity", "--interval", "1d", "--days", "200",
                "--adjusted", "--out", aapl_svg], tier="av", av_cost=1,
               require=["bars"], out_file=aapl_svg))
    S.append(s("av: options CL --equity = Colgate (the namesake escape)", "options.py",
               ["CL", "--equity", "--json"], tier="av", av_cost=2,
               require=[r'"symbol":\s*"CL"']))
    S.append(s("av: chart BTC av-crypto (bare asset + --market)", "chart.py",
               ["BTC", "--source", "av-crypto", "--market", "USD", "--interval", "1d",
                "--days", "30", "--out", btc_svg], tier="av", av_cost=1,
               require=["bars"], out_file=btc_svg))
    S.append(s("av: options AAPL --list-expiries", "options.py",
               ["AAPL", "--list-expiries"], tier="av", av_cost=1,
               require=[r"\d{4}-\d{2}-\d{2}", "DTE"]))
    S.append(s("av: fundamentals NVDA --json (6-call fan-out)", "fundamentals.py",
               ["NVDA", "--json"], tier="av", av_cost=7, require=["NVDA"]))
    S.append(s("av: portfolio 2 holdings + benchmark", "portfolio.py",
               ["--holdings", "NVDA:10,AAPL:10", "--days", "180"], tier="av", av_cost=4,
               require=["NVDA", "AAPL"]))

    # --- full metered Databento chain (explicit-only; statistics pulls cost money)
    S.append(s("metered: futures_options ES settlement chain", "futures_options.py",
               ["ES", "--source", "databento", "--json"], tier="metered", timeout=420,
               require=[r'"panels"', "series:"]))
    S.append(s("metered: futures_options NG settlement chain", "futures_options.py",
               ["NG", "--source", "databento", "--json"], tier="metered", timeout=420,
               require=[r'"panels"', "series:"]))
    return S


# ---------------------------------------------------------------- runner

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--net", default="offline",
                    help="comma list of tiers: offline,keyless,metadata,av,metered — or 'all' "
                         "(= offline+keyless+metadata+av; metered must be named explicitly)")
    ap.add_argument("--av-budget", type=int, default=3, help="max AV requests to spend (default 3)")
    ap.add_argument("--av-pause", type=float, default=13.0,
                    help="seconds between AV scenarios (free tier = 5/min; 0 for premium keys)")
    ap.add_argument("--only", help="run only scenarios whose name contains this substring")
    ap.add_argument("--list", action="store_true", help="print the selected scenarios and exit")
    a = ap.parse_args(argv)

    tiers = {"offline", "keyless", "metadata", "av"} if a.net == "all" else {t.strip() for t in a.net.split(",")}
    bad = tiers - {"offline", "keyless", "metadata", "av", "metered"}
    if bad:
        ap.error(f"unknown tier(s): {', '.join(sorted(bad))}")

    td = tempfile.mkdtemp(prefix="smoke_")
    scen = [x for x in build_scenarios(td) if x.tier in tiers]
    if a.only:
        scen = [x for x in scen if a.only.lower() in x.name.lower()]
    if a.list:
        for x in scen:
            cost = f"  (av_cost={x.av_cost})" if x.av_cost else ""
            print(f"{x.tier:9} {x.name}{cost}")
        print(f"-- {len(scen)} scenarios")
        return 0

    passed, failed, skipped, spent = 0, [], 0, 0
    for x in scen:
        if x.tier == "av":
            if spent + x.av_cost > a.av_budget:
                print(f"  SKIP  {x.name}  (av budget {a.av_budget} would be exceeded)")
                skipped += 1
                continue
            if spent and a.av_pause:
                time.sleep(a.av_pause)
            spent += x.av_cost
        try:
            errs, blob = x.run()
        except subprocess.TimeoutExpired:
            errs, blob = [f"timeout after {x.timeout}s"], ""
        if errs:
            failed.append(x.name)
            print(f"  FAIL  {x.name}  — {'; '.join(errs)}")
            tail = "\n".join(blob.strip().splitlines()[-4:])
            if tail:
                print("        " + tail.replace("\n", "\n        "))
        else:
            passed += 1
            print(f"  PASS  {x.name}")

    print("\n" + "=" * 60)
    note = f" · {skipped} skipped (av budget)" if skipped else ""
    print(f"{passed} passed, {len(failed)} failed{note}  [tiers: {', '.join(sorted(tiers))}; av spent ≈{spent}]")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
