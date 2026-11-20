#!/usr/bin/env python3
"""
chart.py — fetch a market series and render it to an SVG, end to end.

The whole point: the price series is fetched, resampled, and drawn entirely
inside this script. It NEVER passes through the agent's context, so plotting a
chart costs one command + one summary line instead of dumping/re-emitting the
data. Pure stdlib (urllib), no pip installs.

Sources (pick with --source):
  databento   futures / options-on-futures (CME). Needs DATABENTO_API_KEY.
  av-crypto   crypto via Alpha Vantage CRYPTO_INTRADAY. Needs ALPHAVANTAGE_API_KEY.
  av-equity   equities/ETFs via Alpha Vantage TIME_SERIES_*. Needs ALPHAVANTAGE_API_KEY.

Keys are read from the environment, or a .env file (cwd or --env-file). Never
pass keys on the command line or paste them into chat.

Examples:
  python3 chart.py BTC  --source av-crypto  --interval 4h --days 14 --out btc.svg --open
  python3 chart.py AAPL --source av-equity  --interval 1d --days 90 --out aapl.svg
  python3 chart.py ES.v.0 --source databento --interval 4h --days 14 --stype continuous --open
"""
from __future__ import annotations
import argparse, base64, csv, datetime as dt, io, os, pathlib, re, shutil, subprocess, sys, urllib.parse, urllib.request, webbrowser

UP, DN, MA, AX, GR = "#1D9E75", "#E24B4A", "#378ADD", "#888888", "rgba(128,128,128,0.18)"
PRICE_SCALE = 1e9  # Databento ohlcv prices are 1e9-scaled integers


def get_key(name: str, env_file: str | None) -> str:
    if os.environ.get(name):
        return os.environ[name]
    here = os.path.dirname(os.path.abspath(__file__))
    # search: --env-file, then .env in the cwd, next to the script, and the skill
    # root (so a .env dropped beside SKILL.md works no matter where it's run from)
    for path in ([env_file] if env_file else []) + [
            ".env", os.path.join(here, ".env"), os.path.join(here, os.pardir, ".env")]:
        if path and os.path.exists(path):
            for line in open(path):
                line = line.strip()
                if line.startswith(name + "="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    sys.exit(f"error: {name} not set. Set it as an environment variable, or put a .env "
             f"file (KEY=value lines) in the skill folder next to SKILL.md, or pass --env-file.")


def http_get(url: str, headers: dict | None = None) -> str:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", "replace")


def interval_minutes(s: str) -> int:
    s = s.strip().lower()
    if s in ("1w", "w", "week", "1week"):
        return 7 * 1440
    if s in ("1mo", "mo", "1month", "month", "1mon"):
        return 30 * 1440  # bucketed as a calendar month in resample()
    if s.endswith("h"):
        return int(float(s[:-1]) * 60)
    if s.endswith("d"):
        return int(float(s[:-1]) * 1440)
    if s.endswith("m") or s.endswith("min"):
        return int(s.rstrip("min").rstrip("m"))
    return int(s)


# ---- fetch adapters: each returns [(datetime_utc, o,h,l,c,v), ...] oldest-first ----

def databento_frontier(dataset, schema, auth):
    """Per-schema available `end` — REST get_range needs an explicit end (no end
    returns only ~1 day), and an end past the frontier 422s. So we clamp to it."""
    import json
    url = "https://hist.databento.com/v0/metadata.get_dataset_range?" + urllib.parse.urlencode({"dataset": dataset})
    info = json.loads(http_get(url, {"Authorization": "Basic " + auth}))
    return info.get("schema", {}).get(schema, {}).get("end") or info.get("end")


def fetch_databento(symbol, stype, dataset, start, end, target_min, env_file):
    key = get_key("DATABENTO_API_KEY", env_file)
    schema = "ohlcv-1d" if target_min >= 1440 else "ohlcv-1h"
    auth = base64.b64encode(f"{key}:".encode()).decode()
    if not end:
        if schema == "ohlcv-1d":
            # daily frontier is today 00:00Z, so end=today needs no metadata round-trip
            end = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
        else:
            end = databento_frontier(dataset, schema, auth)
    params = {"dataset": dataset, "symbols": symbol, "schema": schema,
              "stype_in": stype, "stype_out": "instrument_id",
              "encoding": "csv", "start": start, "end": end}
    url = "https://hist.databento.com/v0/timeseries.get_range?" + urllib.parse.urlencode(params)
    text = http_get(url, {"Authorization": "Basic " + auth})
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        ts = dt.datetime.fromtimestamp(int(row["ts_event"]) / 1e9, dt.timezone.utc).replace(tzinfo=None)
        out.append((ts, float(row["open"]) / PRICE_SCALE, float(row["high"]) / PRICE_SCALE,
                    float(row["low"]) / PRICE_SCALE, float(row["close"]) / PRICE_SCALE, float(row["volume"])))
    return out, schema


def _av(params, env_file):
    key = get_key("ALPHAVANTAGE_API_KEY", env_file)
    params = dict(params, apikey=key, datatype="csv")
    text = http_get("https://www.alphavantage.co/query?" + urllib.parse.urlencode(params))
    if text.lstrip().startswith("{"):
        sys.exit("error: Alpha Vantage returned JSON (likely a note/error, not data): " + text[:200])
    return text


def fetch_av_crypto(symbol, market, target_min, env_file):
    base = "60min" if target_min < 1440 else "60min"
    text = _av({"function": "CRYPTO_INTRADAY", "symbol": symbol, "market": market,
                "interval": base, "outputsize": "full"}, env_file)
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        ts = dt.datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S")
        out.append((ts, float(row["open"]), float(row["high"]), float(row["low"]),
                    float(row["close"]), float(row.get("volume", 0))))
    out.sort(key=lambda r: r[0])
    return out, "crypto-60min"


def fetch_av_equity(symbol, target_min, env_file):
    if target_min >= 1440:
        text = _av({"function": "TIME_SERIES_DAILY", "symbol": symbol,
                    "outputsize": "full", "entitlement": "realtime"}, env_file)
        tcol = "timestamp"
        fmt = "%Y-%m-%d"
    else:
        text = _av({"function": "TIME_SERIES_INTRADAY", "symbol": symbol, "interval": "60min",
                    "outputsize": "full", "entitlement": "realtime", "extended_hours": "false"}, env_file)
        tcol = "timestamp"
        fmt = "%Y-%m-%d %H:%M:%S"
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        ts = dt.datetime.strptime(row[tcol], fmt)
        out.append((ts, float(row["open"]), float(row["high"]), float(row["low"]),
                    float(row["close"]), float(row.get("volume", 0))))
    out.sort(key=lambda r: r[0])
    return out, ("equity-1d" if target_min >= 1440 else "equity-60min")


# Friendly --moex-market -> ISS (engine, market). Covers the whole exchange.
MOEX_MARKETS = {
    "index": ("stock", "index"), "shares": ("stock", "shares"),
    "bonds": ("stock", "bonds"), "currency": ("currency", "selt"),
    "futures": ("futures", "forts"), "options": ("futures", "options"),
}


def _moex_resolve(symbol):
    """Auto-detect (engine, market) for any MOEX security from its primary board,
    so shares/FX/bonds/futures all work without specifying the segment."""
    import json
    b = json.loads(http_get(
        f"https://iss.moex.com/iss/securities/{symbol}.json?iss.meta=off&iss.only=boards")).get("boards", {})
    ci = {n: i for i, n in enumerate(b.get("columns", []))}
    rows = b.get("data", [])
    if not rows or "engine" not in ci or "market" not in ci:
        raise SystemExit(f"error: MOEX ISS does not know '{symbol}' (check the code, or pass --moex-market)")
    pick = (next((r for r in rows if r[ci.get("is_primary", -1)] == 1), None)
            or next((r for r in rows if r[ci.get("is_traded", -1)] == 1), None) or rows[0])
    return pick[ci["engine"]], pick[ci["market"]]


def fetch_moex(symbol, start, end, target_min, market=None):
    """Moscow Exchange via the public ISS API (no key). Indices, shares, bonds,
    FX and FORTS futures/options — engine/market auto-resolved from the security
    unless --moex-market is given. Volume = rouble turnover (`value`)."""
    import json
    engine, mkt = MOEX_MARKETS[market] if market else _moex_resolve(symbol)
    # Use ISS-native candle intervals so long weekly/monthly pulls don't page
    # through thousands of daily bars: 31=monthly, 7=weekly, 24=daily, 60/10 intraday.
    iss = (31 if target_min >= 28 * 1440 else 7 if target_min >= 7 * 1440
           else 24 if target_min >= 1440
           else next(c for c in (60, 10, 1) if target_min % c == 0 and target_min >= c))
    till = end or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    base = (f"https://iss.moex.com/iss/engines/{engine}/markets/{mkt}/securities/{symbol}/candles.json"
            f"?interval={iss}&from={start}&till={till}")
    # ISS pages candles at ~500/response — page via `start` until exhausted, else
    # long ranges silently truncate to the first 500 bars from `from` (stale!).
    out, start_at = [], 0
    while True:
        resp = json.loads(http_get(f"{base}&start={start_at}"))
        c = resp.get("candles", {})
        col = {n: i for i, n in enumerate(c.get("columns", []))}
        data = c.get("data", [])
        if not data:
            break
        for r in data:
            ts = dt.datetime.strptime(r[col["begin"]][:19], "%Y-%m-%d %H:%M:%S")
            vol = r[col["value"]] if col.get("value") is not None and r[col["value"]] else (r[col["volume"]] if "volume" in col else 0)
            out.append((ts, r[col["open"]], r[col["high"]], r[col["low"]], r[col["close"]], vol or 0))
        cur = resp.get("candles.cursor", {})
        cc = {n: i for i, n in enumerate(cur.get("columns", []))}
        if cur.get("data") and all(k in cc for k in ("INDEX", "TOTAL", "PAGESIZE")):
            cr = cur["data"][0]
            nxt = cr[cc["INDEX"]] + cr[cc["PAGESIZE"]]
            if nxt >= cr[cc["TOTAL"]]:
                break
            start_at = nxt
        else:  # no cursor metadata: stop when a short page comes back
            if len(data) < 500:
                break
            start_at += len(data)
        if start_at > 500000:  # safety backstop
            break
    if not out:
        raise SystemExit(f"error: MOEX ISS returned no {mkt} candles for {symbol} (check code / --moex-market / interval)")
    out.sort(key=lambda x: x[0])
    return out, f"moex-{mkt}-{iss}"


# ---- resample + render ----

def resample(bars, target_min, cutoff):
    bars = [b for b in bars if b[0] >= cutoff]
    midnight = lambda ts: ts.replace(hour=0, minute=0, second=0, microsecond=0)
    if target_min >= 28 * 1440:        # monthly
        keyfn, label_fmt = (lambda ts: midnight(ts).replace(day=1)), "%Y-%m"
    elif target_min >= 7 * 1440:       # weekly (bucket to that week's Monday)
        keyfn, label_fmt = (lambda ts: midnight(ts) - dt.timedelta(days=ts.weekday())), "%Y-%m-%d"
    elif target_min >= 1440:           # daily
        keyfn, label_fmt = midnight, "%Y-%m-%d"
    else:                              # intraday — bucket into target_min-minute bins
        def keyfn(ts):
            b = ((ts.hour * 60 + ts.minute) // target_min) * target_min
            return ts.replace(hour=b // 60, minute=b % 60, second=0, microsecond=0)
        label_fmt = "%Y-%m-%d %H:%M"
    buckets, order = {}, []
    for ts, o, h, l, c, v in bars:
        k = keyfn(ts)
        if k not in buckets:
            buckets[k] = {"o": o, "h": h, "l": l, "c": c, "v": v, "first": ts, "last": ts}
            order.append(k)
        else:
            b = buckets[k]
            b["h"], b["l"], b["v"] = max(b["h"], h), min(b["l"], l), b["v"] + v
            if ts < b["first"]:
                b["o"], b["first"] = o, ts
            if ts > b["last"]:
                b["c"], b["last"] = c, ts
    rows = []
    for k in order:
        b = buckets[k]
        rows.append([k.strftime(label_fmt), round(b["o"], 2), round(b["h"], 2), round(b["l"], 2), round(b["c"], 2), int(b["v"])])
    return rows


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_svg(D, title, subtitle, theme="light"):
    W, H, L, R, T, PB, VT, VB = 760, 440, 14, 74, 44, 322, 340, 424  # price axis on the RIGHT
    dark = theme == "dark"
    if dark:
        BG, TITLE, SUB, AXc, GRc = "#16171a", "#ececec", "#8a8d93", "#7c7f86", "rgba(255,255,255,0.09)"
    else:
        BG, TITLE, SUB, AXc, GRc = None, "#3a3a3a", "#9a9a9a", AX, GR
    n = len(D)
    lo = min(b[3] for b in D); hi = max(b[2] for b in D); pad = (hi - lo) * 0.06 or 1
    pmin, pmax = lo - pad, hi + pad
    if lo >= 0:
        pmin = max(0, pmin)  # prices are non-negative; don't pad the axis below zero
    dlt = 0 if pmax >= 100 else (1 if pmax >= 10 else (2 if pmax >= 1 else 4))  # axis decimals by price scale
    pf = lambda v: f"{v:,.{dlt}f}"
    vmax = max((b[5] for b in D), default=0) or 1
    st = (W - L - R) / n; cw = max(1.5, st * 0.62)
    px = lambda i: L + (i + 0.5) * st
    py = lambda p: T + (pmax - p) / (pmax - pmin) * (PB - T)
    vy = lambda v: VB - (v / vmax) * (VB - VT)
    chg = (D[-1][4] - D[0][1]) / D[0][1] * 100 if D[0][1] else 0.0
    ccol = UP if chg >= 0 else DN; ctxt = f"{'+' if chg >= 0 else ''}{chg:.1f}%"
    s = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" font-family="-apple-system,Segoe UI,Helvetica,sans-serif" role="img">',
         f'<title>{esc(title)} {ctxt}</title>']
    if BG:
        s.append(f'<rect x="0" y="0" width="{W}" height="{H}" fill="{BG}"/>')  # +1 rect only in dark theme
    s.append(f'<text x="{L}" y="22" font-size="17" font-weight="600" fill="{TITLE}">{esc(title)}</text>')
    s.append(f'<text x="{W-R}" y="22" text-anchor="end" font-size="15" font-weight="600" fill="{ccol}">{ctxt}</text>')
    s.append(f'<text x="{L}" y="37" font-size="11" fill="{SUB}">{esc(subtitle)}</text>')
    for g in range(5):
        p = pmin + (pmax - pmin) * g / 4; y = py(p)
        s.append(f'<line x1="{L}" y1="{y:.1f}" x2="{W-R}" y2="{y:.1f}" stroke="{GRc}" stroke-width="1"/>')
        s.append(f'<text x="{W-R+6}" y="{y+4:.1f}" font-size="11" fill="{AXc}">{pf(p)}</text>')
    multiyear = D[0][0][:4] != D[-1][0][:4]  # show the year on multi-year spans
    ticks = sorted({0, n//4, n//2, 3*n//4, n-1})
    for i in ticks:
        anc = "start" if i == 0 else ("end" if i == n-1 else "middle")
        tx = L if i == 0 else (W-R if i == n-1 else px(i))
        lab = D[i][0][:7] if multiyear else D[i][0][5:]  # YYYY-MM vs MM-DD[ HH]
        s.append(f'<text x="{tx:.1f}" y="{PB+16:.0f}" text-anchor="{anc}" font-size="11" fill="{AXc}">{esc(lab)}</text>')
    for i, b in enumerate(D):
        up = b[4] >= b[1]; c = UP if up else DN; x = px(i)
        s.append(f'<line x1="{x:.1f}" y1="{py(b[2]):.1f}" x2="{x:.1f}" y2="{py(b[3]):.1f}" stroke="{c}" stroke-width="1"/>')
        yo, yc = py(b[1]), py(b[4]); top = min(yo, yc); hgt = max(1.0, abs(yo - yc))
        s.append(f'<rect x="{x-cw/2:.1f}" y="{top:.1f}" width="{cw:.1f}" height="{hgt:.1f}" fill="{c}" rx="0.6"/>')
        if b[5]:
            s.append(f'<rect x="{x-cw/2:.1f}" y="{vy(b[5]):.1f}" width="{cw:.1f}" height="{VB-vy(b[5]):.1f}" fill="{c}" opacity="0.45"/>')
    pts = []
    for i in range(19, n):
        pts.append((px(i), py(sum(D[j][4] for j in range(i-19, i+1)) / 20)))
    if pts:
        s.append('<path d="M' + " L".join(f"{x:.1f} {y:.1f}" for x, y in pts) + f'" fill="none" stroke="{MA}" stroke-width="1.6"/>')
    # last-price guide + right-axis label (no extra rects, keeps the test's counts)
    last = D[-1]; lc = last[4]; lcol = UP if last[4] >= last[1] else DN; yl = py(lc)
    s.append(f'<line x1="{L}" y1="{yl:.1f}" x2="{W-R}" y2="{yl:.1f}" stroke="{lcol}" stroke-width="1" stroke-dasharray="3 3" opacity="0.85"/>')
    s.append(f'<text x="{W-R+6}" y="{yl+4:.1f}" font-size="11" font-weight="600" fill="{lcol}">{lc:,.{max(2, dlt)}f}</text>')
    s.append("</svg>")
    return "\n".join(s)


def _find(*candidates):
    for c in candidates:
        if os.path.sep in c or (os.altsep and os.altsep in c):
            if os.path.exists(c):
                return c
        else:
            p = shutil.which(c)
            if p:
                return p
    return None


def rasterize(svg_path, png_path, out_w=1000):
    """SVG -> PNG, aspect-correct, trying converters in order of speed:
    rsvg-convert (fast, any OS) -> qlmanage+sips (macOS) -> headless browser (last
    resort). Returns the converter name, or raises if none is available."""
    vb = re.search(r'viewBox="[\d.]+ [\d.]+ ([\d.]+) ([\d.]+)"', open(svg_path).read())
    vw, vh = (float(vb.group(1)), float(vb.group(2))) if vb else (760.0, 440.0)
    out_h = round(out_w * vh / vw)
    dn = subprocess.DEVNULL

    rsvg = _find("rsvg-convert")
    if rsvg:
        subprocess.run([rsvg, "-w", str(out_w), svg_path, "-o", png_path], check=True)
        return "rsvg-convert"

    ql, sips = _find("qlmanage"), _find("sips")
    if ql and sips:
        d = os.path.dirname(os.path.abspath(png_path)) or "."
        subprocess.run([ql, "-t", "-s", str(out_w), "-o", d, svg_path], stdout=dn, stderr=dn, check=False)
        square = os.path.join(d, os.path.basename(svg_path) + ".png")
        if os.path.exists(square):
            subprocess.run([sips, "-c", str(out_h), str(out_w), square, "--out", png_path], stdout=dn, stderr=dn, check=True)
            os.remove(square)
            return "qlmanage+sips"

    browser = _find("chromium", "chromium-browser", "google-chrome", "google-chrome-stable",
                    "msedge", "microsoft-edge", "microsoft-edge-stable",
                    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                    r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    if browser:
        subprocess.run([browser, "--headless=new", f"--screenshot={png_path}",
                        f"--window-size={out_w},{out_h}", "--hide-scrollbars",
                        "--default-background-color=FFFFFFFF", "file://" + os.path.abspath(svg_path)],
                       stdout=dn, stderr=dn, check=False)
        if os.path.exists(png_path):
            return "headless-browser"

    raise SystemExit("error: no SVG->PNG converter found. Install rsvg-convert (recommended), "
                     "or use --open to view the SVG in a browser.")


def _parse_ts(s):
    """Parse a timestamp from a local file: ISO date/datetime, or an epoch integer
    (s/ms/us/ns — Databento ts_event is ns)."""
    s = str(s).strip()
    if s.isdigit():
        n = int(s)
        for thresh, div in ((10**16, 1e9), (10**13, 1e6), (10**11, 1e3)):
            if n >= thresh:
                return dt.datetime.utcfromtimestamp(n / div)
        return dt.datetime.utcfromtimestamp(n)
    s = s.replace("T", " ").replace("Z", "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s, fmt)
        except ValueError:
            pass
    raise SystemExit(f"error: unrecognized timestamp '{s}' (use YYYY-MM-DD[ HH:MM:SS] or an epoch int)")


def fetch_file(path, fmt=None):
    """Render from a locally-saved OHLCV series instead of fetching over HTTP.
    For locked-down sandboxes where direct egress to the data host is blocked but an
    MCP connector CAN return the series: save it to CSV/JSON, then chart it here —
    so you reuse this renderer (consistent style, resampling, provenance) instead of
    writing a one-off plotting script. CSV needs a header with a timestamp/date column
    plus open,high,low,close (volume optional); JSON accepts a list of objects or
    [ts,o,h,l,c,v] rows (or {"bars"/"data"/"candles"/"values": [...]}). Prices must be
    real numbers already (not Databento 1e9-scaled integers)."""
    fmt = fmt or ("json" if path.lower().endswith(".json") else "csv")
    raw = open(path).read()
    out = []
    if fmt == "json":
        import json
        obj = json.loads(raw)
        if isinstance(obj, dict):
            for k in ("bars", "data", "candles", "values", "results"):
                if isinstance(obj.get(k), list):
                    obj = obj[k]
                    break
        for r in obj:
            if isinstance(r, dict):
                g = {k.lower(): v for k, v in r.items()}
                ts = (g.get("timestamp") or g.get("time") or g.get("date")
                      or g.get("datetime") or g.get("t"))
                o, h, l, c = g["open"], g["high"], g["low"], g["close"]
                v = g.get("volume", g.get("v", 0))
            else:
                ts, o, h, l, c = r[0], r[1], r[2], r[3], r[4]
                v = r[5] if len(r) > 5 else 0
            out.append((_parse_ts(ts), float(o), float(h), float(l), float(c), float(v or 0)))
    else:
        rdr = csv.DictReader(io.StringIO(raw))
        cols = {(c or "").lower(): c for c in (rdr.fieldnames or [])}
        tcol = next((cols[k] for k in ("timestamp", "time", "date", "datetime") if k in cols), None)
        if not tcol or any(k not in cols for k in ("open", "high", "low", "close")):
            sys.exit("error: CSV needs a timestamp/date column plus open,high,low,close (volume optional)")
        vcol = cols.get("volume")
        for row in rdr:
            out.append((_parse_ts(row[tcol]),
                        float(row[cols["open"]]), float(row[cols["high"]]),
                        float(row[cols["low"]]), float(row[cols["close"]]),
                        float(row[vcol]) if vcol and row.get(vcol) not in (None, "") else 0.0))
    if not out:
        sys.exit("error: no rows parsed from " + path)
    out.sort(key=lambda r: r[0])
    return out, fmt


SOURCE_TZ = {  # bar-timestamp timezone per source: (hours east of UTC, label)
    "databento": (0, "UTC"),
    "av-crypto": (0, "UTC"),
    "av-equity": (-4, "ET"),   # US Eastern (approx; daily charts are tz-insensitive)
    "moex": (3, "MSK"),        # Moscow is permanent UTC+3 (no DST)
    "file": (0, ""),           # local file: timestamps taken as-is, no tz relabel
}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Fetch a market series and render it to SVG")
    ap.add_argument("symbol")
    ap.add_argument("--source", choices=["databento", "av-crypto", "av-equity", "moex", "file"])
    ap.add_argument("--from", dest="from_path",
                    help="render from a local OHLCV file (CSV or JSON) instead of fetching — "
                         "for egress-blocked sandboxes; implies --source file")
    ap.add_argument("--from-format", choices=["csv", "json"], help="override file format (else by extension)")
    ap.add_argument("--source-label", help="provenance text for the subtitle in --from mode "
                    "(e.g. 'Alpha Vantage equity via MCP')")
    ap.add_argument("--interval", default="1d", help="target bar size, e.g. 1h 4h 1d")
    ap.add_argument("--days", type=float, default=14, help="lookback in days (fractional ok, e.g. 0.1 for ~2.4h)")
    ap.add_argument("--market", default="USD", help="quote market for av-crypto")
    ap.add_argument("--dataset", default="GLBX.MDP3")
    ap.add_argument("--stype", default="continuous", help="databento stype_in")
    ap.add_argument("--moex-market", choices=["index", "shares", "bonds", "currency", "futures", "options"],
                    help="MOEX segment override (default: auto-resolve from the security)")
    ap.add_argument("--start"); ap.add_argument("--end")
    ap.add_argument("--out", default="chart.svg")
    ap.add_argument("--theme", default="light", choices=["light", "dark"])
    ap.add_argument("--png", action="store_true", help="also write a PNG (cross-platform: rsvg-convert/qlmanage/headless browser)")
    ap.add_argument("--env-file")
    ap.add_argument("--open", action="store_true")
    a = ap.parse_args(argv)
    if a.from_path and not a.source:
        a.source = "file"
    if not a.source:
        ap.error("--source is required (or pass --from PATH to render a local file)")
    if a.source == "file" and not a.from_path:
        ap.error("--source file requires --from PATH")

    tmin = interval_minutes(a.interval)
    tz_off, tz_lab = SOURCE_TZ[a.source]  # bars are stamped in this tz; size the window in it too
    now_local = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(hours=tz_off)
    cutoff = now_local - dt.timedelta(days=a.days)
    start = a.start or cutoff.strftime("%Y-%m-%d")

    if a.source == "databento":
        bars, base = fetch_databento(a.symbol, a.stype, a.dataset, start, a.end, tmin, a.env_file)
    elif a.source == "av-crypto":
        bars, base = fetch_av_crypto(a.symbol, a.market, tmin, a.env_file)
    elif a.source == "av-equity":
        bars, base = fetch_av_equity(a.symbol, tmin, a.env_file)
    elif a.source == "file":
        bars, base = fetch_file(a.from_path, a.from_format)
        # the file IS the intended window (already scoped by the caller) — keep every
        # row, only resampling to --interval; honour --start/--days only if given.
        if not a.start:
            cutoff = dt.datetime.min
    else:  # moex
        bars, base = fetch_moex(a.symbol, start, a.end, tmin, a.moex_market)
    if not bars:
        sys.exit("error: no data returned")

    D = resample(bars, tmin, cutoff)
    if not D:
        sys.exit("error: no bars after the cutoff window")

    if a.source == "file":
        src = a.source_label or f"local file ({base})"
        subtitle = f"{src} · {len(D)} bars · {D[0][0]} -> {D[-1][0]} {tz_lab}".strip()
    else:
        src = {"databento": f"Databento {a.dataset}", "av-crypto": "Alpha Vantage crypto",
               "av-equity": "Alpha Vantage equity", "moex": "MOEX ISS"}[a.source]
        subtitle = f"{src} · {base}->{a.interval} · {len(D)} bars · {D[0][0]} -> {D[-1][0]} {tz_lab}"
    title = f"{a.symbol} · {a.interval}"
    open(a.out, "w", encoding="utf-8").write(render_svg(D, title, subtitle, a.theme))
    print(f"{a.out}  |  {len(D)} {a.interval} bars  |  {D[0][0]} -> {D[-1][0]}  |  last close {D[-1][4]:,}")
    if a.png:
        png_path = (a.out[:-4] if a.out.endswith(".svg") else a.out) + ".png"
        via = rasterize(a.out, png_path)
        print(f"{png_path}  |  via {via}")
    if a.open:
        # cross-platform: webbrowser opens the SVG in the default browser on
        # macOS / Windows / Linux. Build the file URI with pathlib.as_uri() so it
        # is valid on Windows too (file:///C:/... with forward slashes, not
        # "file://C:\..." which browsers reject).
        webbrowser.open(pathlib.Path(os.path.abspath(a.out)).as_uri())


if __name__ == "__main__":
    main()
