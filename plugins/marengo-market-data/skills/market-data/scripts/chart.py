"""Render normalized OHLCV data from a configured MCP connector to a local chart."""
from __future__ import annotations

import argparse, csv, datetime as dt, io, math, os, pathlib, re, shutil, subprocess, sys, webbrowser
from decimal import Decimal

UP, DN, MA, AX, GR = "#1D9E75", "#E24B4A", "#378ADD", "#888888", "rgba(128,128,128,0.18)"
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


# ---- resample + render ----

def resample(bars, target_min, cutoff):
    bars = [b for b in bars if b[0] >= cutoff]
    midnight = lambda ts: ts.replace(hour=0, minute=0, second=0, microsecond=0)
    if target_min == 30 * 1440:        # monthly
        keyfn, label_fmt = (lambda ts: midnight(ts).replace(day=1)), "%Y-%m"
    elif target_min == 7 * 1440:       # weekly (bucket to that week's Monday)
        keyfn, label_fmt = (lambda ts: midnight(ts) - dt.timedelta(days=ts.weekday())), "%Y-%m-%d"
    else:
        if target_min <= 0:
            raise ValueError("interval must be positive")
        period = dt.timedelta(minutes=target_min)
        epoch = dt.datetime(1970, 1, 1)
        def keyfn(ts):
            return epoch + ((ts - epoch) // period) * period
        label_fmt = "%Y-%m-%d" if target_min % 1440 == 0 else "%Y-%m-%d %H:%M"
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
        rows.append([k.strftime(label_fmt), b["o"], b["h"], b["l"], b["c"], b["v"]])
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
    visible_decimals = max(-Decimal(str(price)).normalize().as_tuple().exponent
                           for bar in D for price in bar[1:5])
    axis_decimals = -math.floor(math.log10((pmax - pmin) / 5))
    dlt = max(0, visible_decimals, axis_decimals)
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
    raw = pathlib.Path(path).read_text(encoding="utf-8-sig")
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


def main(argv=None):
    ap = argparse.ArgumentParser(description="Render market bars from a CSV or JSON file")
    ap.add_argument("symbol")
    ap.add_argument("--source", choices=["file"], default="file")
    ap.add_argument("--from", dest="from_path", required=True)
    ap.add_argument("--from-format", choices=["csv", "json"])
    ap.add_argument("--source-label", help="provider and time zone for the chart subtitle")
    ap.add_argument("--interval", default="1d", help="target bar interval, e.g. 1h, 2d, 1w or 1mo")
    ap.add_argument("--out", default="chart.svg")
    ap.add_argument("--theme", default="light", choices=["light", "dark"])
    ap.add_argument("--png", action="store_true", help="also write a PNG (cross-platform: rsvg-convert/qlmanage/headless browser)")
    ap.add_argument("--open", action="store_true")
    a = ap.parse_args(argv)
    try:
        minutes = interval_minutes(a.interval)
        if minutes <= 0:
            raise ValueError()
    except (ValueError, OverflowError):
        ap.error("invalid chart interval")
    bars, base = fetch_file(a.from_path, a.from_format)
    D = resample(bars, minutes, dt.datetime.min)
    src = a.source_label or f"local file ({base})"
    subtitle = f"{src} · {len(D)} bars · {D[0][0]} -> {D[-1][0]}"
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
