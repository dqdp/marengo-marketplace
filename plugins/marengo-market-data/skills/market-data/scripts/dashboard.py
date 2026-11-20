#!/usr/bin/env python3
"""
dashboard.py — the CENTRALIZED modular dashboard renderer (see docs/live-dashboard-spec.md).

A dashboard is a declarative SPEC — `{title, subtitle, cards[], panels[{type, data}]}` — and this
module is the ONE renderer that turns any spec into a self-contained responsive HTML page. Analysis
scripts (options/fundamentals/portfolio/futures) become DATA PRODUCERS: they compute numbers, build
a spec, and hand it here. The shell (grid, dark mode, the vendored Chart.js, `</script>`-safe
substitution) and every block type live in ONE place, so a new dashboard = new data + (maybe) a new
block in the registry, never a new bespoke HTML template.

Phase 1: spec -> one-shot self-contained `--html`; blocks cards/smile/bars/line/table.
Phase 2 (this file): `--watch` LIVE-PUSH — a provider seam (DD-3) feeds an SSE server that emits
PER-BLOCK deltas (DD-2); one server per dashboard (DD-4); the provider declares a liveness badge
(DD-5: tick / delayed / settlement + timestamp). The static and live pages SHARE the block registry
and the adaptive-grid JS, so they never drift.

Pure stdlib. The vendored `chart.umd.min.js` is inlined so the page renders in egress-blocked viewers
(e.g. the in-app preview) — never an external `<script src>`.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import sys
import tempfile
import time

# Block types drawn with Chart.js on a <canvas> (the registry lives in the page JS). Anything else
# (e.g. "table") is rendered server-side so it shows without JS.
CANVAS_BLOCKS = {"smile", "bars", "line", "combo", "candles", "doughnut", "scatter", "donut", "heatmap", "greeks", "exposure", "bubble"}


def _chartjs_inline() -> str:
    """The vendored Chart.js UMD, read __file__-relative and inlined so --html is self-contained."""
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chart.umd.min.js")
    with open(p, encoding="utf-8") as fh:
        return fh.read()


def _klinecharts_inline() -> str:
    """Vendored KLineChart v9.8 UMD (self-hosted, zero-dep) — inlined only when a `kline` block
    is present, so non-price dashboards don't carry it."""
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "klinecharts.min.js")
    with open(p, encoding="utf-8") as fh:
        return fh.read()


def _has_kline(spec) -> bool:
    """True when ANY board the page can show carries a kline panel: the flattened top level,
    any SECTION (a tab click builds it client-side — the lib must already be inline), and the
    v1.127 EMBEDDED sibling board (the Cowork board swap renders it without navigation)."""
    if not isinstance(spec, dict):
        return False
    if any(p.get("type") == "kline" for p in (spec.get("panels") or [])):
        return True
    for s in (spec.get("sections") or []):
        if any(p.get("type") == "kline" for p in (s.get("panels") or [])):
            return True
    sib = spec.get("sibling")
    return _has_kline(sib.get("spec")) if isinstance(sib, dict) else False


def esc(s) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def esc_attr(s) -> str:
    """esc() PLUS the double-quote — safe to interpolate inside a double-quoted HTML attribute
    (esc() alone leaves `"`, which would break out of style="…"/other attrs)."""
    return esc(s).replace('"', "&quot;")


def _sub_html(spec) -> str:
    """Subtitle inner HTML: an optional clickable site link (http-only) then the text."""
    sub = esc(spec.get("subtitle", ""))
    st = spec.get("subtitleSite") or {}
    url, txt = str(st.get("url") or ""), str(st.get("text") or "")
    if url.startswith(("http://", "https://")) and txt:
        link = f'<a class="sublink" href="{esc_attr(url)}" target="_blank" rel="noopener">{esc(txt)}</a>'
        return f"{link} \u00b7 {sub}" if sub else link
    return sub


def _about_is_top(spec) -> bool:
    """v1.97: `about.top` promotes the identity paragraph to a PLAIN full-width tile right
    under the header — FLAT pages only. Sectioned pages keep the §6.6 Overview-tile behavior:
    the client tab-swap twin fills #aboutSlot and would diverge from a top slot."""
    return bool((spec.get("about") or {}).get("top")) and not spec.get("sections")


def _about_top_html(spec) -> str:
    """The about.top tile: full-width, no chevron, no 'About' heading — the text IS the point
    (what this instrument is must be readable before any number). Cue rides as a caption."""
    if not _about_is_top(spec):
        return ""
    ab = spec.get("about") or {}
    txt = str(ab.get("text") or "")
    if not txt:
        return ""
    cue = ab.get("cue") or ""
    return (f'<div class="panel abt abttop"><div class="txt">{esc(txt)}</div>'
            + (f'<div class="cap abtsrc">{esc(cue)}</div>' if cue else "") + "</div>")


def _lead_html(spec, live=False) -> str:
    """v1.110: the page-level DESCRIPTION block — a full-width identity paragraph directly under the
    header and ABOVE the tab bar, so it stays visible across tab swaps (it is NOT section content,
    unlike the old §6.6 About tile). Shared verbatim by the fundamentals and options dashboards (same
    company blurb; the only header difference is the leftSmall 'Fundamentals'/'Options'). The
    interactive page clamps the paragraph to ~4 lines with a 'show more' toggle; the no-JS page
    renders it whole. A meta line (site · FY end · as-of) trails the text. Falls back to the ETF
    about.top tile when no `lead` is set (that flat page keeps its identity tile)."""
    ld = spec.get("lead") or {}
    txt = str(ld.get("text") or "")
    site = str(ld.get("site") or "")
    dom = site.split("://", 1)[-1].rstrip("/") if site else ""
    if dom.startswith("www."):
        dom = dom[4:]                       # not str.removeprefix — that's 3.9+; keep 3.7-compatible
    link = (f'<a href="{esc_attr(site)}" target="_blank" rel="noopener">{esc(dom)}</a>'
            if site.startswith(("http://", "https://")) else "")   # full scheme, not a bare "http" prefix (review P3)
    meta_txt = str(ld.get("meta") or "")
    sep = " · " if (link and meta_txt) else ""
    meta_inner = link + sep + esc(meta_txt)
    if not txt and not meta_inner:
        return _about_top_html(spec)        # nothing to lead with -> the ETF about.top path (or empty)
    meta = f'<div class="cap leadmeta">{meta_inner}</div>' if meta_inner else ""
    if not txt:                             # meta-only (a description-less name) — no paragraph, no toggle
        return f'<div class="lead">{meta}</div>'
    if not live:                            # no-JS page: the whole paragraph, never clamped (nothing could expand it)
        return f'<div class="lead"><div class="txt">{esc(txt)}</div>{meta}</div>'
    return (f'<div class="lead"><div class="txt clamp" id="leadtx">{esc(txt)}</div>'
            f'<button class="leadmore" id="leadmore" onclick="tgLead()">show more</button>{meta}</div>')


def _about_html(spec, live=False) -> str:
    """Company 'About' strip between the cards and the grid. Live: collapsed to ONE clamped
    line (chevron expands); static no-JS page renders the full paragraph. Returns '' when the
    about is promoted to the TOP slot (v1.97) — never two About blocks."""
    if _about_is_top(spec):
        return ""
    ab = spec.get("about") or {}
    cue = ab.get("cue") or "Alpha Vantage profile \u00b7 may lag the business"
    txt = str(ab.get("text") or "")
    if not txt:
        return ""
    site = str(ab.get("site") or "")
    dom = site.split("://", 1)[-1].rstrip("/") if site else ""
    if dom.startswith("www."):
        dom = dom[4:]                       # not str.removeprefix — that's 3.9+; keep 3.7-compatible
    link = (f' \u00b7 <a href="{esc_attr(site)}" target="_blank" rel="noopener">{esc(dom)}</a>'
            if site.startswith("http") else "")
    meta = f'<span class="cap">{esc(str(ab.get("meta") or ""))}{link}</span>'
    if not live:
        return f'<div class="panel abt"><h3>About {meta}</h3><div class="txt">{esc(txt)}</div></div>'
    return (f'<div class="panel abt" id="abt"><h3><button class="collapse" onclick="tgAbt()" '   # expanded by default (like every other tile); the chevron collapses it
            f'title="expand / collapse">\u25be</button>About {meta}'
            f'<span class="cap abtsrc">{esc(cue)}</span></h3>'
            f'<div class="txt" id="abtx">{esc(txt)}</div></div>')


ASK_TITLE = "copy a question about this block for the AI chat"


def _cards_html(cards, live=False) -> str:
    """Server-render the metric cards (visible without JS). In live mode each value gets an id
    (`cv{i}`) so a per-card delta can patch just that number, and the row gets `id="met"`."""
    mid = ' id="met"' if live else ""
    if not cards:
        return f'<div class="metrics"{mid}></div>' if live else ""
    parts = []
    for i, c in enumerate(cards):
        vid = f' id="cv{i}"' if live else ""
        hint = c.get("hint") or ""
        tip = f' data-tip="{esc_attr(hint)}"' if hint else ""
        exp = c.get("scope") == "expiry"   # a per-expiration tile: accent border + the expiration it refers to
        cls = "m exp" if exp else "m"
        if c.get("wide"):                  # a double-width tile (e.g. the session clock)
            cls += " wide"
        # v1.130: sub lines and the scope caption COMPOSE (a scoped card can carry both — the options
        # PCR card's "vol X.XX" + its expiration stamp). Subs first; the scope caption stays LAST and
        # keeps its switcher-patchable id.
        xcap = ""
        if c.get("sub"):                   # generic caption line(s)
            _subs = c["sub"] if isinstance(c["sub"], list) else [c["sub"]]
            xcap = "".join(f'<div class="mx">{esc(str(s))}</div>' for s in _subs)
        if exp and c.get("scopeLabel"):    # the switcher patches this one by id
            _mxid = f' id="mx{i}"' if live else ""   # quoted attr (twin of the JS id="mx"+i); nested-f-string quote clash on 3.7-3.11 otherwise
            xcap += f'<div class="mx"{_mxid}>{esc(str(c.get("scopeLabel", "")))}</div>'
        cd = c.get("countdown")            # a LIVE session countdown: JS ticks it from the embedded schedule
        if cd:
            _sch = json.dumps(cd.get("schedule") or [], separators=(",", ":"))
            # phaseFromSchedule (h2, v1.115): a NON-equity session tile (CME Globex) derives its phase
            # from its OWN schedule client-side — data-pfs opts OUT of the v1.109 equity-ET rewrite
            _pfs = ' data-pfs="1"' if cd.get("phaseFromSchedule") else ""
            xcap += f'<div class="mx sess-cd"{_pfs} data-cd="{esc_attr(_sch)}">{esc(str(cd.get("text", "")))}</div>'
        ask = f'<button class="ask" title="%s">\u2726</button>' % ASK_TITLE if live else ""
        vcls = "v sm" if c.get("small") else "v"   # small = one-line compact value (session tiles)
        parts.append(f'<div class="{cls}"{tip}><div class="l">{esc(c.get("label", ""))}</div>'
                     f'<div class="{vcls}"{vid}>{esc(c.get("value", ""))}</div>{xcap}{ask}</div>')
    return f'<div class="metrics"{mid}>{"".join(parts)}</div>'


def _table_legend_html(data) -> str:
    """The visible key under a table — separate so a SCROLLY table can place it OUTSIDE the
    scroll box (inside, it hid under the rows and read as 'no legend' — user 2026-07-15)."""
    sep = "&nbsp;&nbsp;\u00b7&nbsp;&nbsp;"   # no backslash inside f-string exprs — 3.9-compatible
    leg = ""
    for l in (data.get("legend") or []):
        leg += (f'<b style="color:{esc_attr(str(l.get("color", "")))}">{esc(str(l.get("sym", "")))}</b> '
                f'{esc(str(l.get("label", "")))}' + sep)
    return f'<div class="tbl-leg">{leg[:-len(sep)]}</div>' if leg else ""


def rel_href_ok(h) -> bool:
    """A table/row link may ONLY be a RELATIVE file beside this page.

    Row links are producer DATA, and a page that navigates to an arbitrary host because a row was
    clicked is a hole — so the same rule the header chips use (`_links_html`) is applied here, in
    ONE place, with a JS twin (`relOK`) so the interactive re-render cannot produce a link the
    server refused: no scheme (`javascript:`, `https:`), no `//host`, no absolute path."""
    h = str(h or "")
    return bool(h) and not h.startswith(("/", "\\")) and h[:2] != "//" \
        and ":" not in h.split("/")[0] and "\\" not in h


def _table_html(data, with_legend=True) -> str:
    cols = data.get("columns", []) or []
    rows = data.get("rows", []) or []
    styles = data.get("rowstyle") or []   # optional per-row CSS (e.g. the ATM heat map)
    cstyles = data.get("cellstyle") or []  # optional per-CELL CSS (e.g. compare's bold-green best-in-row)
    hints = data.get("colhints") or {}    # v1.105: {column label: hover hint} — the .htip tooltip on the th
    links = data.get("links") or []       # v1.153: per-ROW {href, ask} — the sibling-dashboard chip, in a row
    lcol = int(data.get("linkcol") or 0)
    head = "".join((f'<th><span class="htip" data-tip="{esc_attr(str(hints[c]))}">{esc(c)}</span></th>'
                    if hints.get(c) else f"<th>{esc(c)}</th>") for c in cols)

    def _row(i, r):
        cs = cstyles[i] if i < len(cstyles) else []
        lk = links[i] if i < len(links) else None
        if lk and not rel_href_ok((lk or {}).get("href")):
            lk = None

        def _td(j, v):
            s = cs[j] if j < len(cs) else ""
            inner = esc(v)
            if lk and j == lcol:
                inner = (f'<a class="tlink" href="{esc_attr(str(lk.get("href")))}" '
                         f'data-ask="{esc_attr(str(lk.get("ask") or ""))}">{inner}</a>')
            return f'<td style="{esc_attr(s)}">{inner}</td>' if s else f"<td>{inner}</td>"
        cells = "".join(_td(j, v) for j, v in enumerate(r))
        st = styles[i] if i < len(styles) else ""
        return (f'<tr style="{esc_attr(st)}">' if st else "<tr>") + cells + "</tr>"

    body = "".join(_row(i, r) for i, r in enumerate(rows))
    leg = _table_legend_html(data) if with_legend else ""   # v1.107.1: a scrolly table renders it OUTSIDE the box
    big = ' class="big"' if data.get("big") else ""   # headline tables (e.g. the vol term strip) read at 14px
    return f"<table{big}><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>{leg}"




def _klbar_html(i, p) -> str:
    """Server first-paint toolbar for a kline panel (the interactive page never runs renderShell,
    so the buttons must be baked): D/W/M frame switch + indicator toggles (MA default on)."""
    frames = list((p.get("data") or {}).get("frames") or {})
    on = set((p.get("data") or {}).get("on") or ["MA"])
    fb = "".join(f'<button data-fr="{fr}" class="{"on" if fr == (p["data"].get("frame")) else ""}" '
                 f'onclick="klFrame({i},\'{fr}\')">{fr.upper()}</button>' for fr in frames)
    ib = "".join(f'<button data-ind="{n}" class="{"on" if n in on else ""}" '
                 f'onclick="klInd({i},\'{n}\')">{n}</button>'
                 for n in (p.get("indicators") or ["MA", "MACD", "RSI", "BOLL", "VOL"]))
    return (f'<div class="klbar"><span class="klgrp klframes">{fb}</span>'
            f'<span class="klgrp klinds">{ib}'
            f'<button class="klscale" onclick="klScale({i})" title="log / linear scale">log</button>'
            f'<button class="klgear" onclick="klParamsToggle({i})" title="indicator parameters">\u2699</button>'
            f'{_kltv_html(p)}</span></div>'
            f'<div class="klparams" id="klp{i}" style="display:none"></div>')


def _kltv_html(p) -> str:
    tv = str((p.get("data") or {}).get("tvUrl") or "")
    return (f'<a class="kltv" href="{esc_attr(tv)}" target="_blank" rel="noopener" '
            f'title="open this chart on TradingView">TradingView \u2197</a>'
            if tv.startswith(("http://", "https://")) else "")


def _heatmap_html(d) -> str:
    """Heat grid — pure HTML on BOTH pages (no canvas). Two shapes:
    square (labels + n×n matrix, diverging −1..1 corr ramp, values signed) — the portfolio corr block;
    asymmetric (rows + cols + matrix, ramp:"seq" = min→max sequential, values raw) — the IV surface."""
    d = d or {}
    rows_l = [str(x) for x in (d.get("rows") or d.get("labels") or [])]
    cols_l = [str(x) for x in (d.get("cols") or d.get("labels") or [])]
    mx = d.get("matrix") or []
    seq = (d.get("ramp") == "seq")
    logc = seq and d.get("colorScale") == "log"   # opt-in log colour: spread the low/mid band under outliers
    cfmt = d.get("cellFmt")   # "int" -> compact integers (OI surface: 1234 -> "1.2k"), else 1-dp
    if not rows_l or not cols_l or len(mx) != len(rows_l):
        return '<div class="cap">no matrix</div>'
    finite = [float(v) for r in mx for v in (r or []) if isinstance(v, (int, float)) and math.isfinite(v)]   # exclude NaN/inf: else one stray NaN poisons vlo/vhi and paints the WHOLE surface red (twin of the JS !isNaN fin filter)
    vlo, vhi = (min(finite), max(finite)) if finite else (0.0, 1.0)
    _pos = [v for v in finite if v > 0]
    _plo = min(_pos) if _pos else 1.0            # log domain floor = smallest POSITIVE cell (0/neg -> neutral)
    _llo, _lhi = math.log(_plo), math.log(max(vhi, _plo))

    def _col(v):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return "transparent", "#8a8f98"
        if not math.isfinite(v):   # NaN/inf -> a colourless hole (mirror the JS col(); the cell text is already "n/a")
            return "transparent", "#8a8f98"
        if seq:   # sequential: neutral -> red, min..max of THIS matrix (IVs, not correlations)
            if logc:
                t = 0.0 if (v <= 0 or _lhi <= _llo) else (math.log(v) - _llo) / (_lhi - _llo)
            else:
                t = 0.0 if vhi <= vlo else (v - vlo) / (vhi - vlo)
            t = max(0.0, min(1.0, t))
            r, g, bl = 246 + (226 - 246) * t, 247 + (75 - 247) * t, 249 + (74 - 249) * t
            return f"rgb({int(r)},{int(g)},{int(bl)})", ("#1f2328" if t < 0.6 else "#fff")
        v = max(-1.0, min(1.0, v))
        if v >= 0:
            r, g, bl = 246 + (29 - 246) * v, 247 + (158 - 247) * v, 249 + (117 - 249) * v
        else:
            t = -v
            r, g, bl = 246 + (226 - 246) * t, 247 + (75 - 247) * t, 249 + (74 - 249) * t
        ink = "#1f2328" if abs(v) < 0.6 else "#fff"
        return f"rgb({int(r)},{int(g)},{int(bl)})", ink

    nc = len(cols_l)

    def _fmt(v):
        try:
            fv = float(v)
        except (TypeError, ValueError):
            return "n/a"
        if not math.isfinite(fv):   # a NaN/inf slipping out of a producer's OI/IV math would crash floor(nan+0.5)
            return "n/a"            # -> the whole --html/--term render dies; render a hole instead (twin of the JS !isFinite)
        if cfmt == "int":
            # floor(x+0.5) in IEEE doubles — the JS twin computes the SAME expression, so the two render
            # bit-identically (round()/toFixed disagree on exact .5 ties: 1250 -> "1.2k" vs "1.3k").
            return f"{math.floor(fv/100 + 0.5)/10:.1f}k" if abs(fv) >= 1000 else str(math.floor(fv + 0.5))
        return f"{fv:.1f}" if seq else f"{fv:+.1f}"

    # FILL columns: `auto` row-label + N equal `minmax(0,1fr)` data cells that share the panel width. No side
    # gutters when the panel is wide (1-col), no clip/scroll when it's narrow (2-col) — the `0` min lets a cell
    # shrink below content rather than overflow (short numbers never actually clip). Twin: hmHTML() in JS.
    out = [f'<div class="hmwrap" style="overflow-x:auto">'
           f'<div class="hm" style="grid-template-columns:auto repeat({nc},minmax(0,1fr))">',
           '<div class="hml"></div>']
    out += [f'<div class="hml">{esc(x)}</div>' for x in cols_l]
    for i2 in range(len(rows_l)):
        out.append(f'<div class="hml" style="text-align:right">{esc(rows_l[i2])}</div>')
        for j in range(nc):
            v = (mx[i2] or [None] * nc)[j] if j < len(mx[i2] or []) else None
            bg, ink = _col(v)
            out.append(f'<div style="background:{bg};color:{ink}">{esc(_fmt(v))}</div>')
    out.append("</div></div>")
    return "".join(out)


def add_hm_color_toggle(panel):
    """Attach an on-chart [linear | log] colour-scale toggle to a SEQ heatmap panel: two data views that
    SHARE the matrix (linear = default; log spreads the low/mid band when a few outliers dominate). Sets the
    panel's shown `data` to the linear view. Returns the panel (chainable)."""
    base = panel.get("data") or {}
    lin = dict(base)
    lin.pop("colorScale", None)
    log = dict(base, colorScale="log")
    panel["data"] = lin
    panel["variants"] = {"label": "colour", "index": 0,
                         "options": [{"name": "linear", "data": lin}, {"name": "log", "data": log}]}
    return panel


def _ratingbar_html(d, i=0, live=False) -> str:
    """Analyst-view block (server-rendered; same markup client-side): big target + rating split bar.
    live adds an 'estimates' details toggle (consensus rows) — the static page stays no-JS."""
    d = d or {}
    tgt, px = d.get("target"), d.get("price")
    up = f' <span class="cap">{(tgt / px - 1) * 100:+.0f}% vs {_fmt_num(px)}</span>' if (tgt and px) else ""
    segs, total = [], 0
    for k, c in (("strongBuy", "#0F6E56"), ("buy", "#7fc8a9"), ("hold", "#EFB366"),
                 ("sell", "#e08a8a"), ("strongSell", "#C0392B")):
        v = int(d.get(k) or 0); total += v
        if v:
            segs.append((k, v, c))
    bar = "".join(f'<span style="flex:{v};background:{c}"></span>' for _, v, c in segs) if total else ""
    lab = " · ".join(f"{v} {k.replace('strongBuy', 'strong-buy').replace('strongSell', 'strong-sell')}"
                     for k, v, _ in segs)
    dtl = ""
    stamp = ""
    if live and d.get("estAsOf") and (d.get("est") or []):
        # v1.107: LKG-cached estimates are ALWAYS stamped — cache never impersonates a live pull
        stamp = ('<div class="cap">estimates from the last good pull ' + esc(str(d["estAsOf"]))
                 + ' \u2014 this pull returned none</div>')
    if live and not (d.get("est") or []):
        # v1.105.1: an empty estimates feed must SAY so (MU live: AV's EARNINGS_ESTIMATES
        # flipped to [] intra-day and the box silently vanished) — transient, a re-render retries
        dtl = ('<div class="cap">per-period estimates unavailable in this pull '
               '(the provider returned none) — re-render to retry</div>')
    if live and (d.get("est") or []):
        rows = []
        for r in d["est"]:
            rng = (f"{_fmt_num(r.get('lo'))}\u2013{_fmt_num(r.get('hi'))}"
                   if r.get("lo") is not None and r.get("hi") is not None else "\u2014")
            d30, d7 = r.get("d30"), r.get("d7")
            rtip = (f' title="${_fmt_num(r.get("revLoB"))}B\u2013${_fmt_num(r.get("revHiB"))}B'
                    + (f' \u00b7 {int(r.get("revN"))} analysts' if r.get("revN") else "") + '"'
                    if r.get("revLoB") is not None and r.get("revHiB") is not None else "")
            # NB: no backslash escapes inside f-string {...} — PEP 701 is 3.12+; stock macOS is 3.9
            em = "\u2014"
            d7s = em if d7 is None else f"{d7:+.2f}"
            d30s = em if d30 is None else f"{d30:+.2f}"
            revs = em if r.get("revB") is None else "$" + _fmt_num(r.get("revB")) + "B"
            rows.append(f"<tr><td>{esc(str(r.get('label') or ''))}</td><td>{_fmt_num(r.get('eps'))}</td>"
                        f"<td>{rng}</td><td>{d7s}</td><td>{d30s}</td>"
                        f"<td>{int(r.get('up30') or 0)}/{int(r.get('down30') or 0)}</td>"
                        f"<td>{int(r.get('n') or 0)}</td>"
                        f"<td{rtip}>{revs}</td></tr>")
        # EXPANDED by default (user 2026-07-13) \u2014 tgD collapses on first click (glyph flips to \u25b8)
        dtl = (f'<button class="dtl" onclick="tgD(\'dtlr{i}\',this)">\u25be estimates</button>'
               f'<div class="dtlbox" id="dtlr{i}"><table>'
               f'<thead><tr><th></th><th>EPS est</th><th>low\u2013high</th><th>\u03947d</th><th>\u039430d</th>'
               f'<th>revis \u2191/\u2193</th><th>analysts</th><th>revenue</th></tr></thead>{"".join(rows)}</table>'
               '<div class="dtlleg">'
               '<div class="cap"><b>EPS est</b> \u2014 analyst-consensus earnings per share for the period</div>'
               '<div class="cap"><b>low\u2013high</b> \u2014 full spread of individual analyst estimates</div>'
               '<div class="cap"><b>\u03947d / \u039430d</b> \u2014 consensus drift vs 7 / 30 days ago; + means estimates are being raised</div>'
               '<div class="cap"><b>revis \u2191/\u2193</b> \u2014 analysts who raised / cut their EPS estimate in the trailing 30 days</div>'
               '<div class="cap"><b>analysts</b> \u2014 how many analysts submit an EPS estimate for this period</div>'
               '<div class="cap"><b>revenue</b> \u2014 consensus revenue; hover it for the low\u2013high range and its analyst count</div>'
               '</div></div>')
    return (f'<div class="rate"><div class="big">Target {_fmt_num(tgt)}{up}</div>'
            f'<div class="splitbar">{bar}</div><div class="cap">{esc(lab)}</div>{stamp}{dtl}</div>')


def _sentibar_html(d, i=0, live=False) -> str:
    d = d or {}
    n, avg = int(d.get("n") or 0), d.get("avg")
    if not n:
        if int(d.get("nAll") or 0):
            # news EXISTS, it just failed the relevance gate — 0-of-N with the ungated mean,
            # never the "no news data" lie (review 2026-07-04)
            ref = (f'<div class="cap">full feed {d["avgAll"]:+.2f} \u00b7 {int(d["nAll"])} articles</div>'
                   if d.get("avgAll") is not None else "")
            gate = (f'pass relevance \u2265 {d.get("relmin")}' if d.get("relmin") else "relevant")
            return (f'<div class="rate"><div class="big">0 of {int(d["nAll"])} <span class="cap">{gate}</span></div>'
                    f'{ref}</div>')
        return '<div class="cap">no news data</div>'
    bull, neu, bear = int(d.get("bull") or 0), int(d.get("neutral") or 0), int(d.get("bear") or 0)
    bar = "".join(f'<span style="flex:{v};background:{c}"></span>'
                  for v, c in ((bull, "#1D9E75"), (neu, "#bbb"), (bear, "#C0392B")) if v)
    dtl = ""
    if live and (d.get("articles") or []):
        rows = []
        for a in d["articles"]:
            lab = str(a.get("label") or "")
            col = ("rgba(29,158,117,.22)" if "Bullish" in lab else
                   "rgba(192,57,43,.22)" if "Bearish" in lab else "rgba(128,128,128,.22)")
            url = str(a.get("url") or "")
            t = esc(str(a.get("title") or ""))
            # v1.106 (user 2026-07-15, reversing the 2026-07-04 hover-only call): the AV summary
            # is a VISIBLE muted line under the headline — the tab is the news' own room now
            link = (f'<a href="{esc_attr(url)}" target="_blank" rel="noopener">{t}</a>'
                    if url.startswith(("http://", "https://")) else f"<span>{t}</span>")
            summ = (f'<div class="cap nsum">{esc(str(a.get("sum")))}</div>' if a.get("sum") else "")
            sc = a.get("score")
            meta = " \u00b7 ".join(x for x in (str(a.get("d") or ""), str(a.get("src") or "")) if x)
            rows.append(f'<div class="nrow"><span class="chip" style="background:{col}">{esc(lab)}</span> {link}'
                        + summ
                        + f'<div class="cap">{esc(meta)}' + ("" if sc is None else f" \u00b7 {sc:+.2f}") + "</div></div>")
        hint = ('<div class="cap">\u2197 opens here in the built-in browser \u00b7 '
                'use the pane\u2019s \u2190 to come back</div>')
        # EXPANDED by default (user 2026-07-13) \u2014 tgD collapses on first click (glyph flips to \u25b8)
        dtl = (f'<button class="dtl" onclick="tgD(\'dtln{i}\',this)">\u25be headlines</button>'
               f'<div class="dtlbox" id="dtln{i}">{hint}{"".join(rows)}</div>')
    ref = ""
    if d.get("avgAll") is not None:
        ref = (f'<div class="cap">full feed {d["avgAll"]:+.2f} \u00b7 {int(d.get("nAll") or 0)} articles</div>')
    return (f'<div class="rate"><div class="big">{avg:+.2f} <span class="cap">{esc(d.get("label", ""))}</span></div>'
            f'<div class="splitbar">{bar}</div>'
            f'<div class="cap">{bull} bull \u00b7 {neu} neu \u00b7 {bear} bear ({n} articles'
            + (f" \u00b7 relevance \u2265 {d.get('relmin')}" if d.get("relmin") else "") + f')</div>{ref}{dtl}</div>')


def _rangebar_html(d, i=0, live=False) -> str:
    """A 'where in its range' horizontal bar: a track from min..max, filled to the current value, with a
    median tick \u2014 the visual twin of the IV-rank card. Pure HTML (server + client mirror rangebarHTML)."""
    d = d or {}
    r0 = lambda v: int(v + 0.5)   # round HALF-UP (non-neg values) \u2014 bit-identical to the client's Math.round twin
    if d.get("bars") is not None:   # MULTI-tenor: ONE shared rank track (left=cheap/min \u2192 right=rich/max) with a
        unit = d.get("unit", "")    # COLOURED dot per tenor at its rank%, + a one-line colour legend. Each tenor's
        cols = ("#35b8a0", "#5a9ff0", "#e8b055", "#c88ce8", "#e07a5a")   # own range/median/pctile ride the tooltip.
        ticks, dots, legs = [], [], []
        for j, b in enumerate([x for x in d["bars"] if x.get("cur") is not None and x.get("min") is not None
                               and x.get("max") is not None and x["max"] > x["min"]]):
            c = cols[j % len(cols)]
            span = b["max"] - b["min"]
            cp = max(0.0, min(100.0, (b["cur"] - b["min"]) / span * 100))
            med = b.get("median")
            rk = (" \u00b7 rank %d%%" % r0(b["rank"])) if b.get("rank") is not None else ""
            pc = (" \u00b7 %d%% pctile" % r0(b["pct"])) if b.get("pct") is not None else ""
            mt = (" \u00b7 median %d%s" % (r0(med), unit)) if med is not None else ""
            tip = "%s \u00b7 IV %d%s%s%s%s \u00b7 range %d\u2013%d%s" % (b.get("label", ""), r0(b["cur"]), unit, rk, pc, mt,
                                                               r0(b["min"]), r0(b["max"]), unit)
            if med is not None:   # the median NOTCH for this tenor, coloured to match its dot, at the median's rank
                ticks.append('<span class="rb-tick" style="left:%.1f%%;background:%s"></span>'
                             % (max(0.0, min(100.0, (med - b["min"]) / span * 100)), c))
            dots.append('<span class="rb-dot" style="left:%.1f%%;background:%s" title="%s"></span>' % (cp, c, esc_attr(tip)))
            legs.append('<span class="rb-lg"><i style="background:%s"></i>%s %d%s%s</span>' % (
                c, esc(str(b.get("label", ""))), r0(b["cur"]), unit,
                (" \u00b7 r%d" % r0(b["rank"])) if b.get("rank") is not None else ""))
        if not dots:
            return '<div class="rbar rbar-multi"><div class="cap" style="padding:10px 4px">not enough history for a range yet</div></div>'
        return ('<div class="rbar rbar-multi"><div class="rb-track">%s%s</div><div class="rb-leg">%s</div></div>'
                % ("".join(ticks), "".join(dots), "".join(legs)))
    lo, hi, cur, med, unit = d.get("min"), d.get("max"), d.get("cur"), d.get("median"), d.get("unit", "")
    if lo is None or hi is None or cur is None or hi <= lo:
        return '<div class="cap" style="padding:10px 4px">not enough history for a range yet</div>'
    cp = max(0.0, min(100.0, (cur - lo) / (hi - lo) * 100))
    sub = []
    if d.get("rank") is not None:
        sub.append("IV rank %d%%" % r0(d["rank"]))
    if d.get("pct") is not None:
        sub.append("%d%% pctile" % r0(d["pct"]))
    subhtml = (' <span class="cap">' + esc(" \u00b7 ".join(sub)) + '</span>') if sub else ""
    med_tick = ('<span class="rb-med" style="left:%.1f%%"></span>' % max(0.0, min(100.0, (med - lo) / (hi - lo) * 100))
                if med is not None else "")
    mid = ('<span>med %d%s</span>' % (r0(med), unit)) if med is not None else "<span></span>"
    return ('<div class="rbar"><div class="rb-head"><b>%d%s</b> now%s</div>'
            '<div class="rb-track"><span class="rb-fill" style="width:%.1f%%"></span>%s'
            '<span class="rb-now" style="left:%.1f%%"></span></div>'
            '<div class="rb-scale"><span>%d%s</span>%s<span>%d%s</span></div></div>') % (
        r0(cur), unit, subhtml, cp, med_tick, cp, r0(lo), unit, mid, r0(hi), unit)


# ================= server-side SVG chart renderers (NO JavaScript) =================
# The one-shot `--html` draws its charts as inline <svg> so the dashboard renders in ANY viewer —
# Cowork's present_files, an email, a static file — not only a JS-capable Preview Pane. (The LIVE
# `--watch` page still uses Chart.js: it needs JS for the SSE updates anyway.) A viewBox + width:100%
# scales each chart to its grid cell; colours are theme-agnostic (transparent bg, mid-grey axes).
PALETTE_SVG = ["#1D9E75", "#378ADD", "#EFB366", "#0F6E56", "#C0392B", "#9b8cff", "#e08a8a", "#9FE1CB"]
AXIS_C, GRID_C = "#8a8f98", "rgba(128,128,128,.18)"
PUT_C, CALL_C, PAIN_C = "#eb6834", "#2a78d6", "#C0392B"


def _fmt_lab(x):
    return _fmt_num(x) if isinstance(x, (int, float)) else str(x)


def _fmt_num(x, nd=None):
    """Adaptive number format: fewer decimals for large magnitudes (1220 -> '1,220'), more for small
    (3.15 -> '3.15') — so a $3/MMBtu NG strike isn't rounded to '3' like a $1220 palladium strike."""
    if x is None:
        return ""
    if nd is None:
        ax = abs(x)
        nd = 0 if ax >= 100 else (1 if ax >= 10 else (2 if ax >= 1 else 3))
    return f"{x:,.{nd}f}"


def _axis_fmt(lo, hi, n=5):
    """ONE decimal width PER AXIS (style review H1: per-tick adaptivity mixed '216/54.0/0.000'
    on one axis and duplicated labels on narrow spans). Decimals come from the TICK STEP —
    enough to keep n ticks distinct, zero when the step is >= 1."""
    import math
    step = abs(hi - lo) / max(1, n - 1)
    if step >= 1 or step <= 0:
        nd = 0 if step > 0 or abs(hi) >= 100 else (1 if abs(hi) >= 10 else 2)
    else:
        nd = min(4, max(0, -math.floor(math.log10(step))))
    return lambda v: f"{v:,.{nd}f}"


def _svg_open(w, h):
    return (f'<svg class="chart" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" '
            f'preserveAspectRatio="xMidYMid meet" role="img">')


def _empty_svg(w, h):
    return _svg_open(w, h) + f'<text x="{w/2:.0f}" y="{h/2:.0f}" text-anchor="middle" font-size="12" fill="{AXIS_C}">no data</text></svg>'


def _yaxis(s, ymin, ymax, L, R, T, PH, w, unit=""):
    span = (ymax - ymin) or 1
    fmt = _axis_fmt(ymin, ymax)   # ONE decimal width per axis (style review H1)

    def Y(v):
        return T + (ymax - v) / span * PH
    for g in range(5):
        v = ymin + span * g / 4.0
        y = Y(v)
        s.append(f'<line x1="{L}" y1="{y:.1f}" x2="{w-R}" y2="{y:.1f}" stroke="{GRID_C}" stroke-width="1"/>')
        s.append(f'<text x="{L-5}" y="{y+3.5:.1f}" text-anchor="end" font-size="10" fill="{AXIS_C}">{fmt(v)}{unit}</text>')
    return Y


def _xticks(s, labels, X, y, fmt):
    n = len(labels)
    step = max(1, round(n / 8))
    idxs = list(range(0, n, step))
    if n and (n - 1) not in idxs:
        idxs.append(n - 1)
    for i in idxs:
        s.append(f'<text x="{X(i):.1f}" y="{y}" text-anchor="middle" font-size="10" fill="{AXIS_C}">{esc(fmt(labels[i]))}</text>')


def _legend(s, items, w, T):
    widths = [20 + len(str(lbl)) * 6 for lbl, _ in items]
    x = (w - (sum(widths) + 10 * (len(items) - 1))) / 2
    for (label, color), wd in zip(items, widths):
        s.append(f'<rect x="{x:.0f}" y="{T-19:.0f}" width="10" height="10" rx="2" fill="{color}"/>')
        s.append(f'<text x="{x+14:.0f}" y="{T-10:.0f}" font-size="10" fill="{AXIS_C}">{esc(label)}</text>')
        x += wd + 10


def _vmarker(s, xp, T, PH, color, label, at="top"):
    s.append(f'<line x1="{xp:.1f}" y1="{T:.1f}" x2="{xp:.1f}" y2="{T+PH:.1f}" stroke="{color}" stroke-width="1" stroke-dasharray="3 3"/>')
    if label:  # ATM sits above the plot, max-pain below it — so the two labels never overlap
        ly = T - 1 if at == "top" else T + PH + 11
        s.append(f'<text x="{xp:.1f}" y="{ly:.1f}" text-anchor="middle" font-size="9" fill="{color}">{esc(label)}</text>')


def _polyline(series, X, Y, color, interp=None, dash=None):
    """Line segments that BREAK on None (OTM masking), PLUS a marker dot at every point so the
    individual strikes are visible (not just a bare line). An INTERPOLATED (not-quoted) point is drawn
    in grey (same radius — colour-only, matching the live chart) so a model-filled strike is obvious,
    never mistaken for a real quote. `dash` = the borderDash twin (M6): [4,3] -> stroke-dasharray."""
    out, seg = [], []
    da = (' stroke-dasharray="%s"' % esc_attr(" ".join(str(int(x)) for x in dash))) if dash else ""

    def flush():
        if len(seg) >= 2:
            out.append('<path d="M' + " L".join(f"{x:.1f} {y:.1f}" for x, y, _ in seg) + f'" fill="none" stroke="{color}" stroke-width="2"{da}/>')
        for x, y, ip in seg:
            out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="1.9" fill="{"#8a8f98" if ip else color}"/>')
    for i, v in enumerate(series):
        if v is None:
            flush()
            seg = []
        else:
            seg.append((X(i), Y(v), bool(interp[i]) if (interp and i < len(interp)) else False))
    flush()
    return "".join(out)


def _svg_smile(d, w=700, h=300):
    strikes, put, call = d.get("strikes") or [], d.get("putIV") or [], d.get("callIV") or []
    n = len(strikes)
    if n == 0:
        return _empty_svg(w, h)
    L, R, T, B = 46, 10, 30, 26
    PW, PH = w - L - R, h - T - B
    vals = [v for v in put + call if v is not None] or [0, 1]
    ymin, ymax = min(vals), max(vals)
    pad = (ymax - ymin) * 0.12 or 1
    ymin, ymax = max(0, ymin - pad), ymax + pad
    s = [_svg_open(w, h)]

    def X(i):
        return L + (i + 0.5) / n * PW
    Y = _yaxis(s, ymin, ymax, L, R, T, PH, w, unit="%")
    _xticks(s, strikes, X, h - 8, _fmt_num)
    atm = d.get("atm")
    if atm in strikes:
        _vmarker(s, X(strikes.index(atm)), T, PH, AXIS_C, f"ATM {_fmt_num(atm)}")
    mp = (d.get("markers") or {}).get("maxPain")
    if mp in strikes and mp != atm:
        _vmarker(s, X(strikes.index(mp)), T, PH, PAIN_C, "max pain", at="bottom")
    interp = d.get("interp")
    s.append(_polyline(put, X, Y, PUT_C, interp))
    s.append(_polyline(call, X, Y, CALL_C, interp))
    _legend(s, [("Put IV (OTM)", PUT_C), ("Call IV (OTM)", CALL_C)], w, T)
    s.append("</svg>")
    return "".join(s)


def _svg_bars(d, w=700, h=300):
    # DELIBERATE VARIANCE (v1.131, the M7 pattern): this no-JS static twin renders `stack`-grouped
    # series as plain side-by-side bars — user-facing --html always rides the Chart.js engine, which
    # owns the stacked "book with caps" view; no CLI surface feeds stacked specs through here.
    labels, series = d.get("labels") or [], d.get("series") or []
    n, m = len(labels), len(series)
    if n == 0 or m == 0:
        return _empty_svg(w, h)
    L, R, T, B = 42, 10, 30, 26
    PW, PH = w - L - R, h - T - B
    vals = [v for sr in series for v in (sr.get("values") or []) if v]
    vmin = min([0.0] + [v for v in vals if v < 0])          # baseline 0 always in range; negatives extend it
    vmax = max([v for v in vals if v > 0], default=0.0)
    if vmax <= vmin:
        vmax = vmin + 1
    s = [_svg_open(w, h)]
    Y = _yaxis(s, vmin, vmax, L, R, T, PH, w)

    def Xc(i):
        return L + (i + 0.5) / n * PW
    gw = (PW / n) * 0.8
    bw = gw / m
    base = Y(0.0)
    for i in range(n):
        for j, sr in enumerate(series):
            vv = sr.get("values") or []
            v = vv[i] if i < len(vv) else 0
            if not v:
                continue
            x = Xc(i) - gw / 2 + j * bw
            y = Y(v)
            top, hgt = (y, base - y) if v > 0 else (base, y - base)   # negatives hang below the baseline
            fill = (("#1D9E75" if v > 0 else "#C0392B") if sr.get("colorBySign")   # == the JS twin (review M1)
                    else (sr.get("color") or PALETTE_SVG[j % len(PALETTE_SVG)]))
            s.append(f'<rect x="{x:.1f}" y="{top:.1f}" width="{max(0.6, bw*0.9):.1f}" height="{max(0.4, hgt):.1f}" fill="{fill}"/>')
    _xticks(s, labels, Xc, h - 8, _fmt_lab)
    mp = (d.get("markers") or {}).get("maxPain")
    if mp in labels:
        _vmarker(s, Xc(labels.index(mp)), T, PH, PAIN_C, "max pain", at="bottom")
    gf = (d.get("markers") or {}).get("gammaFlip")
    try:                                       # the flip is a LEVEL, not a category — place it fractionally
        N = [float(x) for x in labels]
        if gf is not None and len(N) >= 2 and N[0] <= float(gf) <= N[-1]:
            jx = next(j for j in range(len(N) - 1) if N[j] <= float(gf) <= N[j + 1])
            t = (float(gf) - N[jx]) / ((N[jx + 1] - N[jx]) or 1)
            _vmarker(s, Xc(jx) + t * (Xc(jx + 1) - Xc(jx)), T, PH, "#378ADD", "Γ flip", at="top")
    except (TypeError, ValueError, StopIteration):
        pass
    _legend(s, [(sr.get("name", ""), sr.get("color") or PALETTE_SVG[j % len(PALETTE_SVG)]) for j, sr in enumerate(series)], w, T)
    s.append("</svg>")
    return "".join(s)


def _svg_line(d, w=700, h=300):
    labels, series = d.get("labels") or [], d.get("series") or []
    n = len(labels)
    if n == 0 or not series:
        return _empty_svg(w, h)
    L, R, T, B = 46, 10, 30, 26
    PW, PH = w - L - R, h - T - B
    allv = [v for sr in series for v in (sr.get("values") or []) if v is not None] or [0, 1]
    ymin, ymax = min(allv), max(allv)
    pad = (ymax - ymin) * 0.1 or 1
    ymin, ymax = ymin - pad, ymax + pad
    s = [_svg_open(w, h)]

    def X(i):
        return L + (i + 0.5) / n * PW
    Y = _yaxis(s, ymin, ymax, L, R, T, PH, w)
    _xticks(s, labels, X, h - 8, lambda x: str(x))
    for j, sr in enumerate(series):   # M6: honor per-series color/dash like the JS twin
        s.append(_polyline(sr.get("values") or [], X, Y,
                           sr.get("color") or PALETTE_SVG[j % len(PALETTE_SVG)], dash=sr.get("dash")))
    _legend(s, [(sr.get("name", ""), sr.get("color") or PALETTE_SVG[j % len(PALETTE_SVG)])
                for j, sr in enumerate(series)], w, T)
    s.append("</svg>")
    return "".join(s)




def _svg_combo(d, w=700, h=300):
    """Bars + line(s), optional right axis (series axis=="y1") — the Ф7 E1 block."""
    labels = d.get("labels") or []
    series = d.get("series") or []
    bars = [x for x in series if x.get("type", "bar") == "bar"]
    lines = [x for x in series if x.get("type") == "line"]
    n = len(labels)
    if n == 0 or not series:
        return _empty_svg(w, h)
    L, R, T, B = 46, 46, 30, 26
    PW, PH = w - L - R, h - T - B
    lv = [v for sr in bars for v in (sr.get("values") or []) if v is not None]
    lv += [v for sr in lines if sr.get("axis") != "y1" for v in (sr.get("values") or []) if v is not None]
    ymin, ymax = min(lv + [0]), max(lv + [1])
    Y = _yaxis(s0 := [_svg_open(w, h)], ymin, ymax, L, R, T, PH, w)
    s = s0

    def X(i):
        return L + (i + 0.5) / n * PW
    gw = (PW / n) * 0.72
    bw = gw / max(1, len(bars))
    z = Y(0)
    for j, sr in enumerate(bars):
        vals = sr.get("values") or []
        col = sr.get("color") or PALETTE_SVG[j % len(PALETTE_SVG)]
        for i in range(min(n, len(vals))):
            v = vals[i]
            if v is None:
                continue
            y = Y(v)
            s.append(f'<rect x="{X(i)-gw/2+j*bw:.1f}" y="{min(y, z):.1f}" width="{max(0.6, bw*0.9):.1f}" '
                     f'height="{max(0.4, abs(y-z)):.1f}" rx="2" fill="{col}"/>')
    r1 = [x for x in lines if x.get("axis") == "y1"]
    if r1:
        rv = [v for sr in r1 for v in (sr.get("values") or []) if v is not None] or [0, 1]
        rmin, rmax = min(rv), max(rv)
        span = (rmax - rmin) or 1
        rfmt = _axis_fmt(rmin, rmax)
        rsuf = "%" if d.get("y1pct") is not False else ""   # JS-twin parity (style review H2):
        for g in range(5):                                   # a shares axis is NOT percents
            v = rmin + span * g / 4.0
            y = T + (rmax - v) / span * PH
            s.append(f'<text x="{w-R+5}" y="{y+3.5:.1f}" font-size="10" fill="{AXIS_C}">{rfmt(v)}{rsuf}</text>')

        def YR(v):
            return T + (rmax - v) / span * PH
    for sr in lines:
        vals = sr.get("values") or []
        yy = YR if sr.get("axis") == "y1" and r1 else Y
        col = sr.get("color") or "#378ADD"
        pts = [(X(i), yy(v)) for i, v in enumerate(vals[:n]) if v is not None]
        da = (' stroke-dasharray="%s"' % esc_attr(" ".join(str(int(x)) for x in sr["dash"]))
              if sr.get("dash") else "")   # M6: borderDash twin
        if len(pts) >= 2:
            s.append('<path d="M' + " L".join(f"{x:.1f} {y:.1f}" for x, y in pts)
                     + f'" fill="none" stroke="{col}" stroke-width="2"{da}/>')
        for x, y in pts:
            s.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="1.9" fill="{col}"/>')
    _xticks(s, labels, X, h - 8, _fmt_lab)
    u = d.get("units") or {}
    if u.get("left"):
        s.append(f'<text x="2" y="12" font-size="9" fill="{AXIS_C}">{esc(u["left"])}</text>')
    if u.get("right"):
        s.append(f'<text x="{w-2}" y="12" text-anchor="end" font-size="9" fill="{AXIS_C}">{esc(u["right"])}</text>')
    _legend(s, [(sr.get("name", ""), sr.get("color") or ("#378ADD" if sr.get("type") == "line"
                 else PALETTE_SVG[i % len(PALETTE_SVG)])) for i, sr in enumerate(series)], w, T)
    s.append("</svg>")
    return "".join(s)



def _svg_candles(d, w=700, h=300):
    f0 = (d.get("frames") or {}).get(d.get("frame") or "m") or {}
    labels, o, hi, lo, c = (f0.get(k) or [] for k in ("labels", "o", "h", "l", "c"))
    n = len(labels)
    if not n:
        return _empty_svg(w, h)
    L, R, T, B = 46, 10, 24, 26
    PW, PH = w - L - R, h - T - B
    vals = [v for v in hi + lo if v is not None]
    ymin, ymax = min(vals), max(vals)
    s = [_svg_open(w, h)]
    Y = _yaxis(s, ymin, ymax, L, R, T, PH, w)

    def X(i):
        return L + (i + 0.5) / n * PW
    bw = max(1.2, (PW / n) * 0.6)
    for i in range(n):
        if None in (o[i], hi[i], lo[i], c[i]):
            continue
        col = "#1D9E75" if c[i] >= o[i] else "#C0392B"
        x = X(i)
        s.append(f'<line x1="{x:.1f}" y1="{Y(hi[i]):.1f}" x2="{x:.1f}" y2="{Y(lo[i]):.1f}" stroke="{col}" stroke-width="1"/>')
        yb, yt = Y(min(o[i], c[i])), Y(max(o[i], c[i]))
        s.append(f'<rect x="{x-bw/2:.1f}" y="{yt:.1f}" width="{bw:.1f}" height="{max(0.8, yb-yt):.1f}" fill="{col}"/>')
    _xticks(s, labels, X, h - 8, str)
    s.append("</svg>")
    return "".join(s)

SVG_RENDERERS = {"smile": _svg_smile, "bars": _svg_bars, "line": _svg_line, "combo": _svg_combo, "candles": _svg_candles, "kline": _svg_candles}


def _svg_panel(p) -> str:
    """A canvas-type block -> inline SVG for the static page. Unknown types degrade to a note."""
    fn = SVG_RENDERERS.get(p.get("type"))
    if fn:
        try:
            return fn(p.get("data", {}) or {})
        except Exception as e:  # a bad block must not blank the whole dashboard
            return f'<div style="color:{AXIS_C};font-size:12px;padding:8px">chart error: {esc(type(e).__name__)}</div>'
    return f'<div style="color:{AXIS_C};font-size:12px;padding:8px">no renderer for block: {esc(p.get("type"))}</div>'


def _panels_html(panels, live=False, livepanels=False) -> str:
    """STATIC page: chart blocks -> inline <svg> (no JS, renders anywhere); table -> <table>, full
    width. LIVE page: chart blocks -> <canvas id=p{i}> (Chart.js), each panel `id="panel{i}"` and
    tables in a `.tbl` wrapper so an SSE delta can re-render one. `livepanels` bakes the per-panel live
    dot + age counter (the JS twin's `liveHere`) so a mid-watch RELOAD — which never reruns renderShell —
    still shows liveness (agent-review P1: the reload served a spec with zero dots/counters)."""
    out = []
    for i, p in enumerate(panels or []):
        t = p.get("type", "")
        title = esc(p.get("title", ""))
        cls = "panel"
        if p.get("full"):
            cls = "panel full"          # explicit wide panel (Ф7 E5)
        if p.get("newRow"):
            cls += " rowstart"          # force column 1 -> the summary PAIR always shares a row;
                                        # an odd chart count leaves the gap opposite a big chart
        if t == "kline" and live:
            inner = (_klbar_html(i, p) + f'<div class="kline" id="k{i}"></div>'
                     + f'<div class="klgrip" onpointerdown="klResizeStart(event,{i})" title="drag to resize the chart"></div>')
        elif t in ("ratingbar", "sentibar"):
            inner = (_ratingbar_html(p.get("data"), i, live) if t == "ratingbar"
                     else _sentibar_html(p.get("data"), i, live))
        elif t == "rangebar":
            inner = _rangebar_html(p.get("data"), i, live)   # pure HTML — identical on static & live
        elif t == "heatmap":
            inner = _heatmap_html(p.get("data"))          # pure HTML — identical on static & live
            if "full" not in cls:
                cls += " wide"   # a NON-full heatmap is content-wide -> the grid keeps 2-col only when there's
                                 # real room and stacks it to 1-col SOONER (layout()'s hasWide threshold)
        elif t == "table":
            _pd = p.get("data", {}) or {}
            _sc = " scrolly" if _pd.get("scroll") else ""   # v1.103 bounded scroll box
            tbl = _table_html(_pd, with_legend=not _sc)
            # v1.107.1: the scrolly box hides its tail — the legend lives OUTSIDE it
            inner = (f'<div class="tbl{_sc}">{tbl}</div>' + (_table_legend_html(_pd) if _sc else "")
                     if live else _table_html(_pd))
            # a table spans all grid columns — on the LIVE page too (baked server-side: a
            # mid-watch reload patches in place and never reruns renderShell, so the class
            # must be here). v1.126: COMPOSE, don't overwrite — the JS twin builds
            # 'panel'+full+wide+rowstart, so a newRow table kept its rowstart there while the
            # server dropped it (a reload would shift the layout).
            if "full" not in cls:
                cls = cls.replace("panel", "panel full", 1)
        elif t == "note":
            _dots = '<span class="dots"><i></i><i></i><i></i></span>' if p.get("loading") else ""
            inner = f'<div class="note-panel">{esc(p.get("text", ""))}{_dots}</div>'   # inline callout (no canvas); loading -> animated three dots
            if "full" not in cls:
                cls += " full"          # a note spans the row (it stands in for a full-width panel)
        elif live:
            # v1.99: a HORIZONTAL bar chart sizes its wrapper to the row count (24px/row) — the
            # fixed 230px squeezed 20 rows flat when the panel row-stretched next to a tall pair
            d = p.get("data") or {}
            if p.get("type") == "bars" and d.get("horizontal"):
                h = max(140, min(640, 24 * len(d.get("labels") or []) + 64))
                inner = f'<div class="cw" style="height:{h}px"><canvas id="p{i}"></canvas></div>'
            else:
                inner = f'<div class="cw"><canvas id="p{i}"></canvas></div>'
        else:
            inner = _svg_panel(p)       # server-side SVG — no JavaScript needed
        pid = f' id="panel{i}"' if live else ""
        collapse = (f'<button class="collapse" onclick="collapsePanel({i})" onmouseenter="rowHov({i},1)"'
                    f' onmouseleave="rowHov({i},0)" title="collapse / expand this row">\u25be</button>'
                    if live else "")   # on EVERY tile; hover pre-highlights the row it will fold
        zoom = ""
        if live and t == "smile":
            zoom = (f'<span class="zoom"><button onclick="zoomSmile({i},1)" title="zoom in on ATM">+</button>'
                    f'<button onclick="zoomSmile({i},-1)" title="zoom out">−</button>'
                    f'<button onclick="zoomSmile({i},0)" title="reset">↺</button></span>')
        elif live and t == "candles":
            frames = ((p.get("data") or {}).get("frames") or {})
            fbtn = "".join(f'<button onclick="setFrame({i},\'{fr}\')" title="{lab}">{fr.upper()}</button>'
                           for fr, lab in (("d", "daily"), ("w", "weekly"), ("m", "monthly")) if fr in frames)
            zoom = ('<span class="zoom">' + fbtn
                    + f'<button onclick="zoomR({i},1)" title="zoom in (fewer periods)">+</button>'
                    f'<button onclick="zoomR({i},-1)" title="zoom out (more periods)">−</button>'
                    f'<button onclick="zoomR({i},0)" title="reset">↺</button></span>')
        elif live and p.get("zoom") and t in CANVAS_BLOCKS:   # first paint carries the buttons:
            zoom = (f'<span class="zoom"><button onclick="zoomR({i},1)" title="zoom in (fewer periods)">+</button>'
                    f'<button onclick="zoomR({i},-1)" title="zoom out (more periods)">−</button>'
                    f'<button onclick="zoomR({i},0)" title="reset">↺</button></span>')   # interactive has no
                                                              # SSE snapshot to renderShell() them in
        vtog = ""
        vr = p.get("variants")
        if live and vr and vr.get("options"):   # a per-panel toggle: swap the panel between precomputed data views
            ix = vr.get("index", 0)
            vtog = ('<span class="vtoggle">' + "".join(
                f'<button onclick="switchVariant({i},{k})"{" class=on" if k == ix else ""}>'
                f'{esc(o.get("name", ""))}</button>' for k, o in enumerate(vr["options"])) + '</span>')
        title_html = (f'<span class="htip" data-tip="{esc_attr(p["info"])}">{title}</span>'
                      if p.get("info") else title)   # hover the TITLE for the explainer (CSS-only -> static pages too)
        pdot = '<span class="panlive-dot" title="live"></span> ' if livepanels else ""   # twin of panelsHTML's
        pcnt = f'<span class="panlive" id="pl{i}"></span>' if livepanels else ""         # liveHere pdot/pcnt (order matches)
        ask = f'<button class="ask" title="%s">\u2726</button>' % ASK_TITLE if live else ""
        out.append(f'<div class="{cls}"{pid}><h3>{collapse}{pcnt}{zoom}{vtog}{pdot}{title_html}{ask}</h3>{inner}</div>')
    return "".join(out)


# ---- Shared page JS (used by BOTH the static and the live templates so they never drift) ----
# The block registry: panel.type -> (canvas, data) -> a Chart. New block = new entry here.
SHARED_JS = r"""const PALETTE=["#1D9E75","#378ADD","#EFB366","#0F6E56","#C0392B","#9b8cff","#e08a8a","#9FE1CB"];
const ORANGE="#eb6834", BLUE="#2a78d6", GRY="#8a8f98", RED="#C0392B";
// v1.133 THEME toggle (footer, right). Default = unset data-theme -> follow the app (prefers-color-
// scheme, via the v1.132.2 CSS companion). A click sets an explicit data-theme + localStorage; the
// charts are theme-neutral (v1.88 contract) so nothing needs a rebuild — the CSS flips instantly.
function mdTheme(){ var a=document.documentElement.getAttribute("data-theme");
  if(a==="light"||a==="dark") return a;
  return (window.matchMedia&&window.matchMedia("(prefers-color-scheme: dark)").matches)?"dark":"light"; }
function syncThemeBtn(){ var b=document.getElementById("themetog"); if(!b) return; var c=mdTheme();
  b.textContent="◐ "+(c==="dark"?"Dark":"Light");   // ◐ + the CURRENT mode; a click flips it
  b.setAttribute("aria-label", c==="dark"?"Switch to light theme":"Switch to dark theme"); }
function toggleTheme(){ var n=(mdTheme()==="dark")?"light":"dark";
  document.documentElement.setAttribute("data-theme",n);
  try{ localStorage.setItem("md-theme",n); }catch(e){} syncThemeBtn(); }
function initTheme(){ try{ var t=localStorage.getItem("md-theme");
    if(t==="light"||t==="dark") document.documentElement.setAttribute("data-theme",t); }catch(e){}
  syncThemeBtn();
  try{ var mq=window.matchMedia&&window.matchMedia("(prefers-color-scheme: dark)");   // while on AUTO,
    if(mq){ var h=function(){ var s=null; try{ s=localStorage.getItem("md-theme"); }catch(e){}   // re-label
      if(s!=="light"&&s!=="dark") syncThemeBtn(); };                                              // if the app flips
      mq.addEventListener?mq.addEventListener("change",h):(mq.addListener&&mq.addListener(h)); } }catch(e){} }
Chart.defaults.locale="en-US";   // style review H4: default ticks rendered "1,06" by viewer locale
Chart.defaults.color=GRY;        // style review M3: themed tick/legend ink (was Chart.js #666)
function baseOpts(extra){ return Object.assign({responsive:true, maintainAspectRatio:false, animation:false,
  scales:{x:{grid:{color:"rgba(128,128,128,.12)"}}, y:{grid:{color:"rgba(128,128,128,.12)"}}},
  plugins:{legend:{labels:{boxWidth:12}}}}, extra||{}); }   // animation:false -> live data snaps in place, no redraw
// v1.107.2: a colorBySign series' legend swatch — half green / half red (Chart.js would
// otherwise show the FIRST bar's colour: an all-time-miss first quarter made the cube red).
let _signCv=null;
function signCube(){ if(_signCv) return _signCv; const c=document.createElement("canvas"); c.width=c.height=12;
  const x=c.getContext("2d");   // diagonal split (user 2026-07-15): green upper-left / red lower-right
  x.fillStyle="#1D9E75"; x.beginPath(); x.moveTo(0,0); x.lineTo(12,0); x.lineTo(0,12); x.closePath(); x.fill();
  x.fillStyle=RED; x.beginPath(); x.moveTo(12,0); x.lineTo(12,12); x.lineTo(0,12); x.closePath(); x.fill();
  _signCv=c; return c; }
function qTicks(){ return {autoSkip:false, maxRotation:45, minRotation:0, font:{size:10},
  // v1.108: quarter-adaptive — >12 visible points -> deduped YEAR-only ticks (like the annual
  // neighbours); zoomed in (<=12) -> the full "24Q1" labels. Re-evaluates on every zoom patch.
  callback:function(v,i,tk){ const l=String(this.getLabelForValue(v));
    if(((this.chart.data.labels)||[]).length>12){ const y=l.split("Q")[0];
      const p=(i>0&&tk[i-1]!=null)?String(this.getLabelForValue(tk[i-1].value)).split("Q")[0]:null;
      return p===y?"":("20"+y); } return l; }}; }
function signLegend(S){ if(!(S||[]).some(s=>s&&s.colorBySign)) return null;
  return {labels:{usePointStyle:true, boxWidth:12, generateLabels:(ch)=>{
    const df=Chart.defaults.plugins.legend.labels.generateLabels; const items=df?df(ch):[];
    items.forEach(it=>{ const ds=ch.data.datasets[it.datasetIndex]||{};
      it.pointStyle = ds.$sign ? signCube() : "rectRounded"; }); return items; }}}; }
// A vertical marker (ATM / max pain) at a category index; getIdx()<0 hides it.
let _vid=0;
function vline(getIdx, color, label, bottom){ return {id:"vl"+(_vid++), afterDraw:(ch)=>{
  const i=getIdx(ch); if(i==null||i<0) return;
  const x=ch.scales.x.getPixelForValue(i), A=ch.chartArea, c=ch.ctx;
  c.save(); c.strokeStyle=color; c.setLineDash([3,3]); c.beginPath(); c.moveTo(x,A.top); c.lineTo(x,A.bottom); c.stroke();
  const txt=(typeof label==="function")?label(ch):label;
  if(txt){ c.fillStyle=color; c.font="10px sans-serif"; c.textAlign="center"; c.fillText(txt, x, bottom?A.bottom+11:A.top-2); } c.restore(); }}; }
// Interpolated (not-quoted) smile points are marked by COLOUR, not size: a grey dot (same radius) plus a
// colour GRADIENT on the two adjoining segments (the curve fades to grey into/out of the point), and the
// hover tooltip says "interpolated". So a model-filled thin strike is obvious without an oversized dot.
function smilePts(interp, base){ const v=interp||[];
  return { pointBackgroundColor: v.length ? v.map(f=>f?GRY:base) : base, pointRadius:2.5, pointBorderWidth:0 }; }
function smileSeg(base){ return { borderColor:(ctx)=>{ const iv=ctx.chart.$interp||[], a=iv[ctx.p0DataIndex], b=iv[ctx.p1DataIndex];
  if((!a&&!b)||!ctx.p0||!ctx.p1) return undefined;   // both endpoints quoted -> normal leg colour
  const g=ctx.chart.ctx.createLinearGradient(ctx.p0.x, ctx.p0.y, ctx.p1.x, ctx.p1.y);
  g.addColorStop(0, a?GRY:base); g.addColorStop(1, b?GRY:base); return g; } }; }   // fade to grey at the interpolated end
Chart.register({id:"unitlabels", afterDraw:(ch)=>{ const u=ch.$units; if(!u) return; const c=ch.ctx;
  c.save(); c.font="9px -apple-system,Segoe UI,sans-serif"; c.fillStyle=GRY;
  if(u.left){ c.textAlign="left"; c.fillText(u.left, 2, 12); }
  if(u.right){ c.textAlign="right"; c.fillText(u.right, ch.width-2, 12); } c.restore(); }});
const RENDERERS = {
  // Markers read LIVE state off the chart ($atm/$mp), not a build-time closure, so a data patch (new
  // ATM as the forward moves, or a settling strike count) moves them without a full rebuild.
  smile: (cv, d) => { const plugins=[
      vline((ch)=>(ch.$atm!=null?(ch.data.labels||[]).indexOf(ch.$atm):-1), GRY, (ch)=>"ATM "+ch.$atm),            // top
      vline((ch)=>{ const mp=ch.$mp; return (mp!=null&&mp!==ch.$atm)?(ch.data.labels||[]).indexOf(mp):-1; }, RED, "max pain", true)];  // bottom, hidden when == ATM
    const tip={callbacks:{label:(ctx)=>{ const ip=(ctx.chart.$interp||[])[ctx.dataIndex];
      return ctx.dataset.label+": "+ctx.formattedValue+"%"+(ip?"  · interpolated":""); }}};
    const ch=new Chart(cv, {type:"line", data:{labels:d.strikes, datasets:[
      {label:"Put IV (OTM)", data:d.putIV, borderColor:ORANGE, backgroundColor:ORANGE, spanGaps:false, pointHoverRadius:4, borderWidth:2, ...smilePts(d.interp, ORANGE), segment:smileSeg(ORANGE)},
      {label:"Call IV (OTM)", data:d.callIV, borderColor:BLUE, backgroundColor:BLUE, spanGaps:false, pointHoverRadius:4, borderWidth:2, ...smilePts(d.interp, BLUE), segment:smileSeg(BLUE)}]},
      options:baseOpts({scales:{y:{ticks:{callback:v=>(Math.round(v*100)/100)+"%"}}}, plugins:{legend:{labels:{boxWidth:12}}, tooltip:tip}}), plugins});
    ch.$atm=d.atm; ch.$mp=(d.markers||{}).maxPain; ch.$interp=d.interp; return ch; },
  combo: (cv, d) => { const S=d.series||[]; const GREEN="#1D9E75";
    const ds=S.map((s,i)=>{ if((s.type||"bar")==="line") return {type:"line",label:s.name,data:s.values,
        borderColor:s.color||BLUE,backgroundColor:s.color||BLUE,yAxisID:s.axis||"y",pointRadius:2,borderWidth:2,order:0};
      const bg = s.colorBySign ? (s.values||[]).map(v=>(v||0)>=0?GREEN:RED) : (s.color||PALETTE[i%PALETTE.length]);
      return {type:"bar",label:s.name,data:s.values,backgroundColor:bg,$sign:!!s.colorBySign,order:i+1}; });
    const SL=signLegend(S);   // v1.107.2 half-and-half legend cube for sign-coloured bars
    const hasY1=S.some(s=>s.axis==="y1");
    const sc={x:{grid:{display:false}},y:{grid:{color:"rgba(128,128,128,.12)"}}};
    if(d.xQuarter) sc.x.ticks=qTicks();   // v1.108 quarter-adaptive axis
    if(hasY1) sc.y1={position:"right",grid:{display:false},
      ticks: d.y1pct===false ? {} : {callback:v=>(Math.round(v*100)/100)+"%"}};   // round: autogen ticks carry float dust (2.6000000000000001)
    return new Chart(cv,{data:{labels:d.labels,datasets:ds},
      options:baseOpts(Object.assign({scales:sc}, SL?{plugins:{legend:SL}}:{}))}); },
  candles: (cv, d) => { const f=(d.frames||{})[d.frame]||{}; const GREEN="#1D9E75";
    // Two grouped bar datasets draw side-by-side (the "split candle" bug); instead ONE transparent
    // dataset fixes the category x + [low,high] y-range, and a plugin paints real candles. The
    // frame rides in plugin OPTIONS (opts.f) so it's present at the FIRST draw inside new Chart().
    const drawer={id:"candledraw", afterDatasetsDraw:(ch,a,opts)=>{ const F=opts&&opts.f; if(!F) return;
      const ys=ch.scales.y, c=ch.ctx, meta=ch.getDatasetMeta(0), n=(F.labels||[]).length; if(!n||!meta.data) return;
      c.save(); c.lineWidth=1;
      for(let i=0;i<n;i++){ const el=meta.data[i]; if(!el||F.o[i]==null) continue;
        const x=el.x, cw=Math.max(1,(el.width||6)*0.7);
        const up=F.c[i]>=F.o[i], col=up?GREEN:RED; c.strokeStyle=col; c.fillStyle=col;
        c.beginPath(); c.moveTo(x,ys.getPixelForValue(F.h[i])); c.lineTo(x,ys.getPixelForValue(F.l[i])); c.stroke();
        const yo=ys.getPixelForValue(F.o[i]), yc=ys.getPixelForValue(F.c[i]);
        c.fillRect(x-cw/2, Math.min(yo,yc), cw, Math.max(1,Math.abs(yc-yo))); } c.restore(); }};
    return new Chart(cv,{type:"bar",data:{labels:f.labels,datasets:[
      {data:(f.l||[]).map((_,i)=>[f.l[i],f.h[i]]),backgroundColor:"rgba(0,0,0,0)",categoryPercentage:1,barPercentage:1}]},
      options:baseOpts({scales:{x:{grid:{display:false},ticks:{maxTicksLimit:12,font:{size:9}}},
        y:{grid:{color:"rgba(128,128,128,.12)"}}},
        plugins:{legend:{display:false},candledraw:{f:f},tooltip:{callbacks:{title:(t)=>f.labels[t[0].dataIndex],
          label:(ctx)=>{ const i=ctx.dataIndex; return ["O "+f.o[i],"H "+f.h[i],"L "+f.l[i],"C "+f.c[i]]; }}}}}),
      plugins:[drawer]}); },
  doughnut: (cv, d) => new Chart(cv,{type:"doughnut",data:{labels:d.labels,
    datasets:[{data:d.values,backgroundColor:(d.labels||[]).map((_,i)=>(d.colors&&d.colors[i])||PALETTE[i%PALETTE.length]),borderWidth:0}]},
    options:{responsive:true,maintainAspectRatio:false,animation:false,cutout:"60%",
      plugins:{legend:{position:"right",labels:{boxWidth:10,font:{size:10}}}}}}),
  bars: (cv, d) => { const plugins=[
      vline((ch)=>(ch.$mp!=null?(ch.data.labels||[]).indexOf(ch.$mp):-1), RED, "max pain", true),
      vline((ch)=>{ const g=ch.$gf, N=(ch.data.labels||[]).map(Number);   // fractional index: the flip is a level
        if(g==null||N.length<2||!isFinite(g)||g<N[0]||g>N[N.length-1]) return -1;
        for(let j=0;j<N.length-1;j++){ if(g>=N[j]&&g<=N[j+1]) return j+(g-N[j])/((N[j+1]-N[j])||1); } return -1; },
        "#378ADD", "Γ flip")];
    const HZ=!!d.horizontal;   // v1.98: horizontal rows (indexAxis y) — a short row list pairs with a tall neighbour
    const SL=signLegend(d.series);
    // v1.131 stack groups: series with the same s.stack pile up (base + build cap + unwind ghost —
    // the merged OI·Δ panel); distinct groups sit side by side. legendHide keeps a second group's
    // duplicate cap entries out of the legend (one "+ built"/"− unwound" pair speaks for both sides).
    const LH=(d.series||[]).map(s=>!!(s&&s.legendHide));
    const STK=(d.series||[]).some(s=>s&&s.stack);
    const LEG=SL||(LH.some(Boolean)?{labels:{boxWidth:12,filter:(it)=>!LH[it.datasetIndex]}}:null);   // boxWidth: keep the 12px swatch (Object.assign would drop the default) — review P3
    const ch=new Chart(cv, {type:"bar", data:{labels:d.labels,
      datasets:(d.series||[]).map((s,i)=>({label:s.name, data:s.values, $sign:!!s.colorBySign,
        stack:s.stack||undefined,
        backgroundColor: s.colorBySign ? (s.values||[]).map(v=>(v||0)>=0?"#1D9E75":RED) : (s.color||PALETTE[i%PALETTE.length])}))},
      options:baseOpts(Object.assign(HZ?{indexAxis:"y", scales:{x:{beginAtZero:true, grid:{color:"rgba(128,128,128,.12)"}},
        y:{grid:{display:false}, ticks:{autoSkip:false, font:{size:10}}}}}
        :{scales:Object.assign({y:Object.assign({beginAtZero:true},STK?{stacked:true}:{})}, d.xQuarter?{x:{grid:{display:false},ticks:qTicks()}}:{})},
        LEG?{plugins:{legend:LEG}}:{})), plugins});
    ch.$mp=(d.markers||{}).maxPain; ch.$gf=(d.markers||{}).gammaFlip; return ch; },
  line: (cv, d) => { const _xok=Array.isArray(d.xnum)&&d.series&&d.series[0]&&d.xnum.length===(d.series[0].values||[]).length
      &&(d.xticks||[]).length>0&&new Set(d.xnum).size>=2;   // need labels + ≥2 distinct x, else fall back to the category axis
    const XN=_xok?d.xnum:null;
    // XN present -> a PROPORTIONAL numeric x-axis (e.g. sqrt-time term structure): points sit at their real
    // x; explicit d.xticks {v,t} label round tenors. Absent -> the category axis (byte-identical to before).
    const SP=!!d.showPoints, LB=d.pointLabels||d.labels||[], PM=d.pointMarks||[];   // opt-in: a visible marker at every data point
    // (each = one expiration on a term chart) with an emphasised dot at a marked one (◆ quarterly / ★ most-OI).
    const datasets=(d.series||[]).map((s,i)=>{ const base=s.color||PALETTE[i%PALETTE.length];
      return {label:s.name,
      data: XN?(s.values||[]).map((v,j)=>({x:XN[j], y:v})):s.values,
      borderColor:base, backgroundColor:base,
      borderDash:s.dash||undefined, fill:s.fill?{target:"origin"}:false,
      pointRadius: SP?(s.values||[]).map((_,j)=>(PM[j]?4.5:2.6)):0, pointHoverRadius: SP?6:0,
      pointBackgroundColor: SP?(s.values||[]).map((_,j)=>((PM[j]&&PM[j].color)||base)):base, pointBorderWidth:0,
      // a SPARSE series (e.g. monthly observations beside a daily one) is otherwise drawn as
      // nothing at all: isolated non-null points with pointRadius 0 and no segment between them.
      spanGaps:!!s.spanGaps,
      borderWidth:2, tension:.1, yAxisID:s.axis||"y"}; });
    const tipP = (SP&&LB.length)?{tooltip:{callbacks:{title:(it)=>{const j=(it&&it[0])?it[0].dataIndex:-1; return LB[j]||"";}}}}:{};   // name the expiration on hover
    // keep the DEFAULT legend (square swatch + readable text) but strip the borderDash so a dashed series
    // doesn't render a broken/dashed box. Same for every line chart (not just the point ones).
    const legLabels = {boxWidth:12, generateLabels:(ch)=>{ const df=Chart.defaults.plugins.legend.labels.generateLabels;
      const items=df?df(ch):[]; items.forEach(it=>{ it.lineDash=[]; }); return items; }};
    const yLog = d.ylog?{type:"logarithmic"}:{};   // opt-in log Y (multiplicative term shapes read symmetrically)
    let opts;
    if(XN){ const T=d.xticks||[];
      opts=baseOpts({scales:{x:{type:"linear", min:Math.min.apply(null,XN), max:Math.max.apply(null,XN),
        grid:{color:"rgba(128,128,128,.12)"},
        afterBuildTicks: T.length?((ax)=>{ax.ticks=T.map(t=>({value:t.v}));}):undefined,
        ticks:{maxRotation:0, font:{size:10}, callback:(v)=>{const m=T.find(t=>Math.abs(t.v-v)<1e-6); return m?m.t:"";}}},
        y:Object.assign({grid:{color:"rgba(128,128,128,.12)"}}, yLog)},
        plugins:Object.assign({legend:{labels:legLabels}}, tipP)}); }
    else { // v1.98 d.xYear: YEAR-only category ticks (a 20y monthly axis printed month noise).
      // v1.154: the ticks are CHOSEN (one per year, at the year's first label) instead of being
      // filtered by a neighbour comparison. The old dedup compared a tick's year to the year of
      // the previous DATA POINT, not of the previous rendered tick, so a year was labelled only
      // when a surviving tick happened to fall on the first trading day of that year — measured
      // live on a 1248-point 5-year rates chart, only 2021 and 2022 ever appeared and the yield
      // inversion could not be dated. Shared by every xYear panel in the skill.
      const yrIdx=[]; if(d.xYear&&d.labels){let py=null;
        d.labels.forEach((L,i)=>{const y=String(L).slice(0,4); if(y!==py){yrIdx.push(i); py=y;}});}
      const xt = d.xall?{autoSkip:false,maxRotation:45,minRotation:45,font:{size:10}}
        :(d.xYear?{maxRotation:0,font:{size:10},autoSkip:true,
          callback:function(v){return String(this.getLabelForValue(v)).slice(0,4);}}:null);
      const xAx = xt?{grid:{color:"rgba(128,128,128,.12)"},ticks:xt}:null;
      if(xAx&&d.xYear&&!d.xall&&yrIdx.length) xAx.afterBuildTicks=(ax)=>{ax.ticks=yrIdx.map(i=>({value:i}));};   // xall wins: its own label callback would print full labels on year-only ticks
      opts=baseOpts(Object.assign((xt||d.ylog)?{scales:Object.assign(
        xAx?{x:xAx}:{},
        {y:Object.assign({grid:{color:"rgba(128,128,128,.12)"}},yLog)})}:{},
        {plugins:Object.assign({legend:{labels:legLabels}}, tipP)})); }
    return new Chart(cv, {type:"line", data:{labels:XN?undefined:d.labels, datasets}, options:opts,
    plugins:(d.earnIdx&&d.earnIdx.length)?[{id:"earn"+(_vid++),afterDraw:(ch)=>{const A=ch.chartArea,c=ch.ctx,X=ch.scales.x;c.save();c.strokeStyle="rgba(226,75,74,.5)";c.setLineDash([4,3]);c.lineWidth=1;c.font="9px sans-serif";c.fillStyle="rgba(226,75,74,.95)";d.earnIdx.forEach(i=>{const x=X.getPixelForValue(i);if(x>=A.left&&x<=A.right){c.beginPath();c.moveTo(x,A.top);c.lineTo(x,A.bottom);c.stroke();c.fillText("E",x+2,A.top+9);}});c.restore();}}]:[]}); },
  bubble: (cv, d) => { const pts=(d.points||[]).map((p,i)=>({x:p.x, y:p.y, r:Math.max(5, Math.min(26, p.r||8)),
      label:p.label, backgroundColor:p.color||PALETTE[i%PALETTE.length]}));   // explicit per-point
      // colour wins over the palette — the same `s.color||PALETTE[…]` convention every other
      // renderer here already follows; without it a point cannot be singled out except by size
    const ds=pts.map(p=>({label:p.label, data:[p], backgroundColor:p.backgroundColor, borderWidth:0}));
    if(d.benchmark) ds.push({label:d.benchmark.label||"benchmark", data:[{x:d.benchmark.x, y:d.benchmark.y, r:6,
      label:d.benchmark.label}], backgroundColor:GRY, pointStyle:"rectRot", borderWidth:0});
    return new Chart(cv, {type:"bubble", data:{datasets:ds},
      options:baseOpts({plugins:{legend:{display:false},
        tooltip:{callbacks:{label:(c)=>{const p=c.raw||{};
          return (p.label||c.dataset.label)+": "+(+p.x).toFixed(1)+(d.xUnit||"% vol")+", "+(+p.y).toFixed(1)+(d.yUnit||"% ret");}}}}})}); },
};"""

# Adaptive grid — JS-authoritative. A @media/@container query FREEZES in an embedded side panel once
# Chart.js locks the canvas sizes (the grid stays 2-col even when the panel is narrow — the recurring
# bug). So MEASURE the real rendered width and set 1 or 2 columns directly, then re-fit the charts — on
# every resize (ResizeObserver + window) and a few delayed passes. Exposes window.__relayout so the
# live page can re-fit after a rebuild.
GRID_JS = r"""(function(){
  const grid=document.querySelector(".grid"), host=grid.parentElement||document.body; let last="";
  const KLSZ={};
  function layout(){
    const w=Math.round(grid.getBoundingClientRect().width);
    const th=grid.querySelector(".panel.wide")?900:680;   // a tab with wide content (heatmap surfaces) needs more room before 2-col -> it stacks to 1-col sooner
    const cols=w>=th?"1fr 1fr":"1fr"; if(cols!==last){ grid.style.gridTemplateColumns=cols; last=cols; }
    // v1.99.1: a panel ALONE in its 2-col row (trailing odd panel, or a chart right before a
    // full-width table) stretches to both columns — never a dead half-row cell
    const kids=[...grid.children].filter(el=>el.classList&&el.classList.contains("panel"));
    kids.forEach(el=>{ el.style.gridColumn=""; });
    if(cols!=="1fr"){ let slot=0;
      kids.forEach((el,idx)=>{ if(el.classList.contains("full")){ slot=0; return; }
        if(slot===0){ const nx=kids[idx+1];
          // v1.126: a following .rowstart panel wraps to column 1 (CSS grid-column-start) —
          // the current panel is ALONE in its row exactly like before a .full one
          if(!nx||nx.classList.contains("full")||nx.classList.contains("rowstart")){ el.style.gridColumn="1/-1"; return; }
          slot=1; } else slot=0; }); }
    // The CARD row was still on @media, i.e. on the VIEWPORT — the exact trap the panel grid was
    // moved off. In an embedded viewer the container can shrink while the viewport does not (expand
    // to fullscreen, then restore), and the cards stayed at 5 tracks inside a 450px column: tiny
    // tiles that no resize could fix. Measure the ROW and set the tracks; an inline style beats the
    // media query, so the no-JS static page keeps its @media fallback untouched.
    document.querySelectorAll(".metrics").forEach(mr=>{
      const mw=Math.round(mr.getBoundingClientRect().width);
      const n=mw>=940?5:mw>=690?4:mw>=450?3:2, v="repeat("+n+",minmax(0,1fr))";
      if(mr.style.gridTemplateColumns!==v) mr.style.gridTemplateColumns=v; });
    CHARTS.forEach(c=>{ try{ c&&c.resize(); }catch(e){} });
    if(typeof KL!=="undefined") Object.keys(KL).forEach(k=>{ try{ const el=document.getElementById("k"+k);
      KL[k]&&KL[k].resize(); if(el){ const r=el.getBoundingClientRect(); KLSZ[k]=[Math.round(r.width),Math.round(r.height)]; } }catch(e){} });  // KLineChart isn't in CHARTS -> resize it too, or it renders at the pre-layout width and "slides"
    document.querySelectorAll(".m[data-tip], .htip[data-tip]").forEach(el=>{ const r=el.getBoundingClientRect();
      el.classList.toggle("tipL", r.left + Math.min(340, innerWidth*0.8) > innerWidth - 12); });
  }
  // An embedded file pane can finish attaching without delivering ResizeObserver/window.resize
  // to the child document (observed in Codex: Chart.js stays at 0px until the user drags the pane).
  // Keep a cheap watchdog: it only calls the real layout when a VISIBLE chart disagrees with its
  // container, so healthy dashboards do two dimension reads per chart/second and zero redraws.
  function chartNeedsFit(c){
    const cv=c&&c.canvas, box=cv&&cv.parentElement; if(!box) return false;
    const r=box.getBoundingClientRect(), w=Math.round(r.width), h=Math.round(r.height);
    if(w<8||h<8) return false;   // genuinely hidden tab/pane — wait until it is visible
    return !c.width||!c.height||Math.abs(Math.round(c.width)-w)>2||Math.abs(Math.round(c.height)-h)>2;
  }
  function klineNeedsFit(k){
    const el=document.getElementById("k"+k); if(!el) return false;
    const r=el.getBoundingClientRect(), w=Math.round(r.width), h=Math.round(r.height); if(w<8||h<8) return false;
    const cv=el.querySelector("canvas"), cr=cv&&cv.getBoundingClientRect();
    const prev=KLSZ[k];
    return !cr||cr.width<8||cr.height<8||!prev||Math.abs(prev[0]-w)>2||Math.abs(prev[1]-h)>2;
  }
  function recoverMissedResize(){
    const bad=CHARTS.some(chartNeedsFit);
    const kbad=typeof KL!=="undefined"&&Object.keys(KL).some(klineNeedsFit);
    if(bad||kbad) layout();
  }
  window.__relayout=layout;
  try{ const ro=new ResizeObserver(layout); ro.observe(host); ro.observe(grid); }catch(e){}
  addEventListener("resize", layout);
  try{ visualViewport.addEventListener("resize", layout); }catch(e){}   // embedded panes: window.resize can stay silent
  addEventListener("pageshow", layout);
  document.addEventListener("visibilitychange", ()=>{ if(!document.hidden) layout(); });
  [0,150,500,1500].forEach(t=>setTimeout(layout,t));
  setInterval(recoverMissedResize,1000);   // indefinite because a restored/background task may attach minutes later
  // External links (user 2026-07-13): the NEW-TAB ladder. Try window.open first — a real
  // browser opens a new tab, and so will the pane the day its policy allows page-initiated
  // tabs. TODAY the pane returns null from open() even in a real user gesture WITHOUT
  // noopener (probed 2026-07-13) — then fall back to a location.href assignment, the one
  // navigation the pane does allow (its own ← returns to the dashboard). No menu.
  async function copyTxt(s){ let ok=false;
    try{ await navigator.clipboard.writeText(s); ok=true; }catch(_){}
    if(!ok){ try{ const ta=document.createElement("textarea"); ta.value=s;
      ta.style.cssText="position:fixed;opacity:0"; document.body.appendChild(ta);
      ta.focus(); ta.select(); ok=document.execCommand("copy"); ta.remove(); }catch(_){} }
    return ok; }
  const LOCAL_RE=/^(localhost|127\.|\[::1\])/;
  document.addEventListener("click", async function(e){
    const a=e.target.closest && e.target.closest('a[href^="http"]');
    if(!a){
      // v1.126.1: SIBLING chips are RELATIVE links — real navigation works ONLY on a
      // localhost origin (the Preview Pane's http.server serves the sibling file). The
      // Cowork file-preview viewer serves the page from claudeusercontent.com at a SIGNED
      // url: a relative href resolves against THAT origin -> the app's "Open external link"
      // dialog -> 404 (live QCOM screenshots, 2026-07-23 — the viewer never serves sibling
      // basenames). There the chip copies an ASK instead — paste it in chat and the agent
      // presents the sibling dashboard.
      // v1.153: TABLE row links (a.tlink) ride the same ladder. A pending sibling CHIP is visible
      // immediately with a loading label, but is probed on EVERY origin until its file exists;
      // a member row is likewise listed whether or not that issue was pre-rendered. Without the
      // probe, an unfinished sibling/member would 404 the Preview Pane on click.
      const rl=e.target.closest && e.target.closest("a.hdrlink,a.tlink");
      if(!rl) return;
      const isT=rl.classList && rl.classList.contains("tlink");
      const isPending=rl.classList && rl.classList.contains("pending");
      const isAskOnly=rl.classList && rl.classList.contains("askonly");
      if(!isT && !isPending && !isAskOnly && LOCAL_RE.test(location.hostname||"")) return;
      e.preventDefault(); e.stopPropagation();
      const f=rl.getAttribute("href")||"";
      if((isPending||isAskOnly)&&!LOCAL_RE.test(location.hostname||"")){
        const ask="Show the dashboard file "+f+" from this session's outputs";
        const ok=await copyTxt(ask); askToast(ok?"Ask copied — paste it in chat":"Ask in chat: "+ask); return; }
      // click-time PROBE: if this viewer actually serves the sibling (both files presented
      // and same-path hosted), navigate; else never hand the host a dead relative URL.
      let alive=false;
      try{ const r=await fetch(f,{method:"HEAD"}); alive=!!(r&&r.ok); }catch(_){}
      if(alive){ try{ window.location.href=f; }catch(_){} return; }
      if(isPending){ askToast("Dashboard is still rendering — try again shortly"); return; }
      const ask=(isT&&rl.getAttribute("data-ask"))||("Show the dashboard file "+f+" from this session's outputs");
      const ok=await copyTxt(ask);
      askToast(ok?"Ask copied — paste it in chat":"Ask in chat: "+ask);
      return;
    }
    e.preventDefault(); e.stopPropagation();
    let w=null;
    try{ w=window.open(a.href,"_blank"); }catch(_){}
    if(w) return;
    // location.href is PROBED-safe only on a localhost origin (the Claude Code pane serves the
    // page from a local http.server; its own ← returns here). In any OTHER embedded viewer
    // (file://, the Cowork file-preview partition) the same assignment blanks the page while
    // the host shows its external-open dialog (the AVGO white-page bug, 2026-07-18) — there
    // the clipboard is the honest channel (same ladder as ask-AI).
    if(LOCAL_RE.test(location.hostname||"")){
      try{ window.location.href=a.href; }catch(_){}
      return;
    }
    const ok=await copyTxt(a.href);
    askToast(ok?"Link copied — open it in your browser":"Could not copy — the link is: "+a.href);
  }, true);
  // v1.90.0 ask-AI: a tiny per-block button copies a composed question to the clipboard (the
  // pane page cannot inject into the agent chat — the clipboard is the honest channel). The
  // question is read from the block's OWN DOM (no per-producer wiring); writeText needs a real
  // gesture (probed 2026-07-14), execCommand is the fallback.
  function askToast(msg){ let t=document.querySelector(".asktoast");
    if(!t){ t=document.createElement("div"); t.className="asktoast"; document.body.appendChild(t); }
    t.textContent=msg; t.classList.add("on"); clearTimeout(t._tm); t._tm=setTimeout(()=>t.classList.remove("on"),2200); }
  document.addEventListener("click", async function(e){
    const b=e.target.closest && e.target.closest("button.ask"); if(!b) return;
    e.preventDefault(); e.stopPropagation();
    let q="";
    const m=b.closest(".m");
    if(m){ const l=m.querySelector(".l"), v=m.querySelector(".v");
      q=((l?l.textContent.trim():"")+(v?" = "+v.textContent.trim():""));
      const mx=[...m.querySelectorAll(".mx")].map(x=>x.textContent.trim()).filter(Boolean).join(" · ");
      if(mx) q+=" ("+mx+")";
    } else { const p=b.closest(".panel"); const h=p&&p.querySelector("h3");
      if(h){ const c=h.cloneNode(true); c.querySelectorAll("button,.zoom,.vtoggle,.panlive,.panlive-dot").forEach(x=>x.remove());
        q=c.textContent.replace(/\s+/g," ").trim(); } }
    if(!q) return;
    const text='Explain "'+q+'" on the '+document.title+' dashboard \u2014 what does it mean here and what is the takeaway?';
    let ok=false;
    try{ await navigator.clipboard.writeText(text); ok=true; }catch(_){}
    if(!ok){ try{ const ta=document.createElement("textarea"); ta.value=text;
      ta.style.cssText="position:fixed;opacity:0"; document.body.appendChild(ta);
      ta.focus(); ta.select(); ok=document.execCommand("copy"); ta.remove(); }catch(_){} }
    askToast(ok?"Question copied \u2014 paste it to the AI chat":"Copy failed \u2014 copy the block text manually");
  }, true);
})();"""

# The shared style block (both templates). Live adds a sticky liveness bar on top (see LIVE_STYLE).
BASE_STYLE = r"""  :root { color-scheme: light dark; }
  .panel.full { grid-column:1/-1; }   /* a table spans the full grid row — static AND live */
  .m { position:relative; cursor:default; }
  .m[data-tip] { cursor:help; }
  .m[data-tip]:hover::after { content:attr(data-tip); position:absolute; left:0; top:100%;
    margin-top:6px; z-index:20; width:min(340px,80vw); white-space:normal;
    background:#111; color:#eee; font-size:11px; line-height:1.45; padding:8px 10px;
    border-radius:8px; box-shadow:0 4px 14px rgba(0,0,0,.35); pointer-events:none; }
  .m[data-tip]:hover { z-index:21; }
  .m.tipL[data-tip]:hover::after { left:auto; right:0; }   /* right-edge cards: anchor the hint right so it never clips at the viewport */
  .htip[data-tip] { position:relative; cursor:help; border-bottom:1px dotted #6a6f76; }
  .htip[data-tip]:hover::after { content:attr(data-tip); position:absolute; left:0; top:100%;
    margin-top:6px; z-index:20; width:min(320px,80vw); white-space:normal; font-weight:400;
    background:#111; color:#eee; font-size:12px; line-height:1.5; padding:8px 11px;
    border-radius:8px; box-shadow:0 4px 14px rgba(0,0,0,.35); pointer-events:none; }
  .htip[data-tip]:hover { z-index:21; }
  .htip.tipL[data-tip]:hover::after { left:auto; right:0; }
  .klgrip { position:relative; height:9px; margin:-2px 0 -8px; border-radius:0 0 6px 6px; cursor:ns-resize;
    touch-action:none; background:rgba(128,128,128,.10); }   /* touch-action: a touch drag must resize, not scroll-then-cancel */
  .klgrip::after { content:""; position:absolute; left:50%; top:50%; transform:translate(-50%,-50%);
    width:40px; height:3px; border-radius:2px; background:rgba(128,128,128,.55); }
  .klgrip:hover { background:rgba(55,138,221,.28); }
  .klgrip:hover::after { background:rgba(55,138,221,.9); }
  .rate .big { font-size:20px; font-weight:700; margin:4px 0 8px; }
  .splitbar { display:flex; height:12px; border-radius:6px; overflow:hidden; margin:6px 0; }
  .rbar { padding:6px 4px 2px; }
  .rbar-multi { padding:10px 6px 6px; }   /* multi-tenor IV-rank: ONE shared rank track + a coloured dot per tenor + a one-line legend */
  .rbar-multi .rb-track { height:14px; }
  .rb-tick { position:absolute; top:50%; transform:translate(-1px,-50%); width:2px; height:18px; border-radius:1px; opacity:.85; }   /* the median NOTCH, coloured per tenor */
  .rb-dot { position:absolute; top:50%; transform:translate(-50%,-50%); width:13px; height:13px; border-radius:50%;
    border:2px solid #fff; box-shadow:0 1px 3px rgba(0,0,0,.45); }
  .rb-leg { display:flex; flex-wrap:wrap; gap:6px 16px; margin-top:11px; font-size:12px; color:#8a8f98; }   /* scheme-neutral grey (was dark-only #c8ccd2 -> invisible in light mode) */
  .rb-lg { display:inline-flex; align-items:center; gap:5px; }
  .rb-lg i { width:9px; height:9px; border-radius:50%; display:inline-block; flex:0 0 auto; }
  .rbar .rb-head { font-size:13px; color:#8a8f98; margin-bottom:10px; }   /* scheme-neutral (was #c8ccd2) */
  .rbar .rb-head b { font-size:18px; }   /* inherit the theme body colour (dark on light, light on dark) — was a dark-only #e8e8e8 */
  .rbar .rb-track { position:relative; height:12px; border-radius:6px; margin:4px 2px 6px;
    background:linear-gradient(90deg, rgba(29,158,117,.28), rgba(239,179,102,.30), rgba(192,57,43,.34)); }
  .rbar .rb-fill { position:absolute; left:0; top:0; height:100%; border-radius:6px 0 0 6px;
    background:rgba(255,255,255,.06); }
  .rbar .rb-med { position:absolute; top:-3px; width:2px; height:18px; background:#8a8f98; transform:translateX(-1px); }
  .rbar .rb-now { position:absolute; top:50%; width:13px; height:13px; border-radius:50%;
    background:#e8e8e8; border:2px solid #111; transform:translate(-50%,-50%); box-shadow:0 0 0 1px rgba(255,255,255,.35); }
  .rbar .rb-scale { display:flex; justify-content:space-between; font-size:11px; color:#8a8f98; }
  .rbar .rb-scale .rb-mid, .rbar .rb-scale span:nth-child(2) { color:#a7abb2; }
  .cap { color:#8a8f98; font-size:11px; }
  .hdr-flex { display:flex; justify-content:space-between; align-items:flex-start; gap:12px; }
  .hdr-big { font-size:24px; font-weight:700; }
  .hdr-small { color:#8a8f98; font-size:15px; font-weight:500; }   /* the company name was unreadably small (user 2026-07-04) */
  .hdr-left { display:flex; flex-wrap:wrap; align-items:baseline; column-gap:8px; row-gap:4px;
    min-width:0; flex:1 1 auto; }   /* legacy header row (futures / compare / single-series / ETF) — unchanged */
  .hdr-left .hdr-big, .hdr-left .hdr-small { flex:0 0 auto; }
  /* v1.110 unified stacked header (fundamentals + options --full): ticker+name on one line, board TYPE under */
  .hdr-left.col { flex-direction:column; flex-wrap:nowrap; align-items:flex-start; gap:2px; }
  .hdr-idline { display:flex; flex-wrap:wrap; align-items:baseline; column-gap:8px; row-gap:4px; min-width:0; }
  .hdr-idline .hdr-big, .hdr-name { flex:0 0 auto; }
  .hdr-name { color:#8a8f98; font-size:15px; font-weight:500; }   /* the ticker's расшифровка (company name) — shown on BOTH boards (user 2026-07-15) */
  .hdr-type { font-size:12.5px; font-weight:700; color:#9aa0a8; }   /* board type UNDER the ticker: smaller but bold (user 2026-07-15) */
  .hdr-priceline { display:flex; align-items:center; justify-content:flex-end; gap:20px; }   /* sibling-board link pins next to the price on the right (gap 2x — user 2026-07-15) */
  .hdr-priceline .hdrlinks { margin-left:0; transform:translateY(-5px); }   /* link sits 5px above the price */
  .hdrtags { display:inline-flex; flex-wrap:wrap; gap:4px 6px; align-self:center; justify-content:center;
    flex:1 1 0; min-width:150px; margin-left:28px; }  /* TAG CLOUD: CENTERED in its own area (user 2026-07-08 —
                                       no longer glued to the name) + a big margin so it sits well off the title;
                                       basis 0 keeps it beside the name (wraps INSIDE itself), min-width drops it
                                       BELOW the name when the row is too tight — never over the price */
  .hdrlinks { margin-left:8px; }
  .hdrlink { border:1px solid #2a6db0; border-radius:5px; padding:2px 9px; margin-right:6px;
             color:#5a9ff0; text-decoration:none; font-size:12px; white-space:nowrap; }
  .hdrlink:hover { background:rgba(90,159,240,.12); }
  .hdrlink.pending { opacity:.62; border-style:dashed; cursor:progress; }  /* v1.159.3: visible immediately while sibling background-renders */
  .hdrlink.pending::after { content:" · loading"; font-size:10px; color:#8a8f98; }
  .hdrlink.askonly { border-style:dashed; }
  .hdrlink.askonly::after { content:" · ask in chat"; font-size:10px; color:#8a8f98; }
  .abttop { margin:12px 0 2px; }      /* v1.97: identity tile under the header — full-width, plain */
  .abttop .abtsrc { display:block; text-align:right; margin-top:6px; }
  .hdrtag { border:1px solid #4a4f56; border-radius:5px; padding:2px 9px;
    font-size:12px; color:#8a8f98; white-space:normal; overflow-wrap:anywhere;
    max-width:100%; }   /* STANDARD grey meta-pill (no gold — user 2026-07-08), just slightly larger; wraps inside itself, never over the price */
  .hdr-right { text-align:right; }
  .panel.rowstart { grid-column-start:1; }
  .klbar { display:flex; gap:16px; align-items:flex-start; margin:2px 0 8px; }
  .klframes { display:inline-flex; flex-wrap:wrap; gap:4px; flex:0 0 auto; }   /* frames: pinned left */
  .klinds { display:flex; flex-wrap:wrap; gap:4px; flex:1 1 auto; justify-content:flex-end;
            align-content:flex-start; }   /* controls cloud: takes the rest, right-aligned, wraps DOWN on the right */
  .klbar button { background:transparent; border:1px solid #555; color:#8a8f98; border-radius:6px;
    height:22px; padding:0 9px; font-size:11px; cursor:pointer; }
  .klbar button.on { color:inherit; border-color:#1D9E75; background:rgba(29,158,117,.16); }
  .kline { height:220px; }   /* minimal by default (main pane only); klFit grows it per open sub-pane */
  .klgear, .klscale { background:transparent; border:1px solid #555; color:#8a8f98; border-radius:6px;
    height:22px; font-size:11px; cursor:pointer; padding:0 6px; }
  .klbar .klgear { font-size:17px; line-height:1; padding:0 8px; }   /* beat `.klbar button`; the ⚙ glyph was a tiny dot */
  .klgear:hover, .klscale:hover { color:inherit; border-color:#888; }
  .klscale.on, .klgear.on { color:inherit; border-color:#1D9E75; background:rgba(29,158,117,.16); }
  .klparams { display:flex; gap:12px; flex-wrap:wrap; align-items:center; margin:2px 0 8px; }
  .klpi { display:inline-flex; align-items:center; gap:4px; }
  .klpi label { color:#8a8f98; font-size:11px; }
  .klpi input { background:rgba(128,128,128,.15); border:1px solid #777; color:inherit; border-radius:5px;
    height:20px; padding:0 6px; font-size:11px; width:78px; }
  h3 .collapse { background:transparent; border:0; color:#8a8f98; cursor:pointer;
    display:inline-flex; align-items:center; justify-content:center; width:20px; height:20px;
    padding:0; margin:0 4px 0 -4px; font-size:15px; line-height:1; vertical-align:middle;
    border-radius:5px; }   /* fixed square in EVERY state: rotate() spins around its center, no shift */
  h3 .collapse:hover { color:inherit; background:rgba(128,128,128,.18); }
  .panel.collapsed h3 .collapse { transform:rotate(-90deg); }
  .panel { transition:box-shadow .12s; }
  .panel.rowhov { box-shadow:inset 0 0 0 1px rgba(29,158,117,.55); }   /* hover a chevron -> outline the row it folds */
  .dtl { background:transparent; border:1px solid #6a6f76; color:#8a8f98; border-radius:6px;
    height:24px; font-size:12px; font-weight:600; cursor:pointer; padding:0 12px; margin-top:10px; }
  .dtl:hover { color:inherit; border-color:#1D9E75; background:rgba(29,158,117,.12); }
  .dtlbox { margin-top:8px; font-size:12px; line-height:1.55; max-height:405px; overflow:auto; }   /* +35% (user 2026-07-13) */
  .dtlbox table { width:100%; border-collapse:collapse; font-size:12px; }
  .rate .cap { font-size:12px; }
  .dtlbox th, .dtlbox td { text-align:right; padding:3px 6px; }
  .dtlbox th:first-child, .dtlbox td:first-child { text-align:left; }
  .dtlbox thead th { color:#8a8f98; font-weight:500; border-bottom:1px solid rgba(128,128,128,.2); }
  .nrow { margin:8px 0; }
  .nrow a { color:#378ADD; text-decoration:none; }
  .nrow a:hover { text-decoration:underline; }
  .nrow a::after { content:"\2197"; font-size:9px; margin-left:2px; color:#8a8f98; }
  .chip { display:inline-block; border-radius:4px; padding:0 5px; font-size:10px; }
  .dtlleg { margin-top:10px; border-top:1px solid rgba(128,128,128,.2); padding-top:8px; }
  .dtlleg .cap { margin:3px 0; }
  .dtlleg b { color:inherit; font-weight:600; }
  .hm { display:grid; gap:2px; }   /* data columns are minmax(0,1fr) — they fill the panel evenly (no gutters, no clip) */
  .hm div { text-align:center; font-size:10px; padding:2px 4px; border-radius:3px; line-height:1.2; }
  .hm .hml { background:none; color:#8a8f98; font-weight:600; white-space:nowrap; }   /* one-line row/col label -> short rows */
  .abt { margin:0 0 14px; font-size:13px; }
  .abt .txt { line-height:1.55; margin-top:2px; }
  .abt h3 .cap { font-weight:400; }
  .abtsrc { float:right; font-style:italic; opacity:.75; }
  .abt a { color:#8a8f98; }
  .abt.off .txt { white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .abt.off h3 .collapse { transform:rotate(-90deg); }
  /* v1.110: the page-level description block — under the header, above the tabs, persists across tab swaps */
  .lead { margin:10px 0 6px; font-size:13px; }
  .lead .txt { line-height:1.55; }
  .lead .txt.clamp { display:-webkit-box; -webkit-line-clamp:4; -webkit-box-orient:vertical; overflow:hidden; }
  .lead .txt.clamp.open { -webkit-line-clamp:initial; overflow:visible; }
  .lead .leadmore { display:none; background:none; border:none; padding:2px 0; margin-top:1px; cursor:pointer;
    color:#378ADD; font-size:12px; font-weight:500; }
  .lead .leadmore:hover { text-decoration:underline; }
  .lead .leadmeta { margin-top:6px; }
  .lead .leadmeta a { color:#8a8f98; text-decoration:none; }
  .lead .leadmeta a:hover { text-decoration:underline; }
  .panel.collapsed > *:not(h3) { display:none !important; }
  body { margin:0; font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif; background:#fff; color:#1f2328; }
  .wrap { max-width:1400px; margin:0 auto; padding:16px; }
  .hdr { font-size:20px; font-weight:700; margin-bottom:14px; }   /* a bit more air between the header and the tile cloud (user 2026-07-08) */
  .sub { color:#8a8f98; font-size:12px; margin:2px 0 14px; }
  .sub a { color:#378ADD; text-decoration:none; }
  .sub a:hover { text-decoration:underline; }
  /* EXPLICIT column counts per width (2 -> 3 -> 4): a lone tile stays ONE track (never stretches), and the
     reflow is predictable — no auto-fill guessing that collapses straight to 1 column on a medium width. */
  .metrics { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px; margin-bottom:16px; }
  @media (min-width:450px){ .metrics { grid-template-columns:repeat(3,minmax(0,1fr)); } }
  @media (min-width:690px){ .metrics { grid-template-columns:repeat(4,minmax(0,1fr)); } }
  @media (min-width:940px){ .metrics { grid-template-columns:repeat(5,minmax(0,1fr)); } }
  .m { background:#eef1f5; border-radius:8px; padding:10px 12px; box-shadow:0 0 0 1px rgba(15,23,42,.07); }   /* on a #fff page #f6f7f9 was ~1.08:1 (invisible); deeper fill + a hairline ring gives a crisp card edge */
  .m .l { font-size:12px; color:#8a8f98; } .m .v { font-size:20px; font-weight:600; }
  .m .v.sm { font-size:14px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .m.exp { border-left:3px solid rgba(55,138,221,.7); padding-left:9px; }   /* a per-expiration tile (vs global) */
  .m.wide { grid-column:span 2; }   /* a double-width tile fills TWO tracks (no dead gap; grid keeps it flush) */
  .m .mx { font-size:10px; color:#6f86a8; margin-top:3px; line-height:1.35; }   /* caption line(s): expiration, or session hours (wrap, don't clip) */
  .m .mx.sess-cd { color:#2ec77e; font-weight:600; font-size:11.5px; }   /* the LIVE session countdown */
  /* Default 1 column. The static page overrides this with a CSS auto-fit rule (safe: no canvas to
     freeze); the live page keeps 1fr and sets the count from JS (the side-panel canvas-freeze fix). */
  .grid { display:grid; grid-template-columns:1fr; gap:14px; }
  .panel { background:#eef1f5; border-radius:10px; padding:12px 14px; min-width:0; box-shadow:0 0 0 1px rgba(15,23,42,.07); }   /* same card-edge fix as .m: separate the panel from the #fff page */
  .panel h3 { margin:0 0 8px; font-size:13px; font-weight:600; }
  .cw { position:relative; width:100%; height:230px; min-width:0; }
  canvas { max-width:100%; }
  table { width:100%; border-collapse:collapse; font-size:12px; }
  table.big { font-size:14px; }
  table.big th, table.big td { padding:6px 8px; }
  th,td { text-align:right; padding:3px 6px; border-bottom:1px solid rgba(128,128,128,.2); }
  th:first-child,td:first-child { text-align:left; }
  /* v1.153: a row link into a sibling dashboard. Underlined on hover only — a table of 14 issues
     with 14 permanently-blue cells reads as a link farm, not as data. */
  a.tlink { color:inherit; text-decoration:none; border-bottom:1px dotted rgba(90,159,240,.55); cursor:pointer; }
  a.tlink:hover { color:#2a6db0; border-bottom-color:#2a6db0; }
  :root[data-theme="dark"] a.tlink:hover { color:#7ab4f5; border-bottom-color:#7ab4f5; }
  .foot { color:#8a8f98; font-size:11px; margin-top:14px; display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
  .foot .foottext { flex:1 1 auto; min-width:0; }
  .themetog { flex:0 0 auto; margin-left:auto; background:rgba(128,128,128,.15); color:inherit;
    border:1px solid rgba(128,128,128,.4); border-radius:6px; padding:3px 10px; font:inherit; font-size:11px;
    cursor:pointer; white-space:nowrap; }   /* bottom-row THEME toggle, right-aligned (margin-left:auto) */
  .themetog:hover { background:rgba(128,128,128,.30); }
  .note-panel { color:#9aa0a8; font-size:13px; line-height:1.55; padding:14px 14px 16px; white-space:pre-wrap; }   /* an inline 'note' callout panel — SHARED (a note is reachable from the static _panels_html too, unlike .warming) */
  .dots { display:inline-block; margin-left:6px; white-space:nowrap; vertical-align:middle; }   /* animated three-dots "loading" indicator — SHARED */
  .dots i { display:inline-block; width:5px; height:5px; margin-left:4px; border-radius:50%; background:currentColor; opacity:.2; animation:dotblink 1.2s infinite; }
  .dots i:nth-child(2) { animation-delay:.2s; } .dots i:nth-child(3) { animation-delay:.4s; }
  @keyframes dotblink { 0%,80%,100% { opacity:.2; } 40% { opacity:1; } }
  .tbl.scrolly { max-height:420px; overflow-y:auto; }   /* v1.103: a long event list scrolls; the page doesn't */
  .tbl.scrolly thead th { position:sticky; top:0; background:#eef1f5; z-index:1; }   /* must match the panel fill so rows scroll UNDER it */
  @media (prefers-color-scheme: dark) {
    body { background:#1f1f1f; color:#e8e8e8; } .m,.panel { background:#2a2a2a; box-shadow:0 0 0 1px rgba(255,255,255,.06); }
    .tbl.scrolly thead th { background:#2a2a2a; }
  }
  /* v1.132.2: FOLLOW THE HOST APP THEME on every surface. prefers-color-scheme (above) is the default
     signal — the Preview Pane conveys the Claude app theme through it. A host that instead STAMPS
     data-theme on the root (the Artifact / Cowork-viewer convention) wins in BOTH directions here:
     the attribute selectors (specificity 0,2,x) outrank the plain element/class rules the media query
     sets (0,0,x), so an explicit data-theme="light" forces light even under a dark OS, and vice versa.
     There is NO toggle — the page never sets data-theme itself; it only obeys one the host sets. The
     charts carry theme-neutral inks (the v1.88 style contract), so no JS theme branch is needed. */
  :root[data-theme="dark"] { color-scheme:dark; }
  :root[data-theme="dark"] body { background:#1f1f1f; color:#e8e8e8; }
  :root[data-theme="dark"] .m, :root[data-theme="dark"] .panel { background:#2a2a2a; box-shadow:0 0 0 1px rgba(255,255,255,.06); }
  :root[data-theme="dark"] .tbl.scrolly thead th { background:#2a2a2a; }
  :root[data-theme="light"] { color-scheme:light; }
  :root[data-theme="light"] body { background:#fff; color:#1f2328; }
  :root[data-theme="light"] .m, :root[data-theme="light"] .panel { background:#eef1f5; box-shadow:0 0 0 1px rgba(15,23,42,.07); }
  :root[data-theme="light"] .tbl.scrolly thead th { background:#eef1f5; }"""

# Static page = NO JavaScript: charts are inline SVG, the grid reflows via a CSS auto-fit rule (there's
# no Chart.js locking canvas sizes, so the side-panel freeze that forced JS on the LIVE page doesn't
# apply here). This renders in ANY viewer — Cowork present_files, email, a static file. Every value is
# esc()'d, so a data string with "</script>" or "<" can't inject.
STATIC_STYLE = r"""  .grid { grid-template-columns:repeat(auto-fit,minmax(300px,1fr)); }
  .chart { width:100%; height:auto; display:block; }"""

HTML_TEMPLATE = (r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
""" + BASE_STYLE + "\n" + STATIC_STYLE + r"""
</style></head>
<body><div class="wrap">
  __HEAD__
  <div class="sub">__SUBTITLE__</div>
  __ABOUTTOP__
  __CARDS__
  __ABOUT__
  <div class="grid">__PANELS__</div>
  <div class="foot">__FOOT__</div>
</div></body></html>""")


def _active_section(spec):
    """The active section dict for a sectioned spec, or None for a plain (single-section) spec.
    Falls back to the first section when spec['active'] doesn't name one."""
    secs = spec.get("sections")
    if not secs:
        return None
    aid = spec.get("active")
    for s in secs:
        if s.get("id") == aid:
            return s
    return secs[0]


def _with_active(spec):
    """For a sectioned spec (`spec['sections']=[{id,name,cards,panels,switcher?,cardSeries?,state?}]`),
    return a SHALLOW COPY whose top-level cards/panels/switcher/cardSeries are the ACTIVE section's —
    so the existing server renderers + client JS build the active section at first paint, while
    `sections[]` rides along in the embedded SPEC for the client-side tab swap (mountTabs/showSection).
    A plain spec (no `sections`) is returned unchanged. Only the active section is server-rendered;
    the others are lazy (built client-side on their first tab click)."""
    sec = _active_section(spec)
    if sec is None:
        return spec
    m = dict(spec)
    m["active"] = sec.get("id")
    m["cards"] = sec.get("cards", [])
    m["panels"] = sec.get("panels", [])
    # about/switcher/cardSeries are SECTION-scoped on a sectioned page: the flattened top-level
    # carries only the ACTIVE section's (a non-overview tab has no About — §6.6: About is an
    # Overview tile, not page chrome). A section without them -> None (page-level about ignored).
    for k in ("switcher", "cardSeries", "about"):
        m[k] = sec.get(k) if k in sec else None
    m["subtitle"] = sec.get("subtitle") or spec.get("subtitle") or ""   # per-tab as-of; never None -> no literal "None"
    # v1.84 review P1: stash the PAGE subtitle so the CLIENT tab swap can fall back to it exactly
    # like this server twin does (a subtitle-less tab used to clear #sub forever — the as-of date,
    # the non-USD disclosure and 'not advice' silently vanished after the first tab click).
    m["_pageSub"] = spec.get("subtitle") or ""
    return m


def _livepanels(spec):
    """True when the ACTIVE tab IS the streaming section — then bake the per-panel live dot/counter server-side
    (a mid-watch reload boots via buildAll, never renderShell, so the JS-only spans must be baked here)."""
    return bool(spec.get("liveSection") and spec.get("active") == spec.get("liveSection"))


_SKILL_VER = None


def skill_version(_path=None):
    """The skill version from SKILL.md's `version:` frontmatter (skill root = the scripts dir's
    parent, the same self-location as .env). '' when unreadable — the footer then drops the
    stamp rather than faking one. Cached: one file read per process (`_path` bypasses the
    cache — test seam only)."""
    global _SKILL_VER
    if _SKILL_VER is not None and _path is None:
        return _SKILL_VER
    ver = ""
    try:
        p = _path or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "SKILL.md")
        with open(p, encoding="utf-8") as f:
            for ln in f:
                m = re.match(r"version:\s*([0-9][0-9A-Za-z.\-]*)\s*$", ln.strip())
                if m:
                    ver = m.group(1)
                    break
    except (OSError, UnicodeDecodeError):   # unreadable OR non-UTF8 — never kill a render over the stamp
        pass
    if _path is None:
        _SKILL_VER = ver
    return ver


def _foot(mid="", source="", live=False):
    """Footer line for every render family: version stamp · engine · [transport ·] [source ·] liability.
    The stamp answers "which code drew this page?" from the page itself (the 2026-07-14 MSOS
    Cowork post-mortem: a cold-store page was mistaken for an old-version page). `source` (v1.110:
    the provider name, e.g. 'Alpha Vantage') moved OUT of the header chips into this bottom line.
    v1.133: `live` pages (they carry the JS) also get a right-aligned light/dark THEME toggle here —
    the bottom-most row, right side (user request); it sets data-theme (obeyed by the v1.132.2 CSS
    companion) + persists to localStorage. Default is unset = follow the app theme; no toggle on the
    no-JS static page (a button with no handler would be dead)."""
    v = skill_version()
    txt = ("market-data v%s · " % esc(v) if v else "") + "dashboard.py · " \
        + (mid + " · " if mid else "") \
        + '<span id="ftsrc">' + (esc(str(source)) + " · " if source else "") + "</span>" \
        + "data analysis, not investment advice"   # ftsrc: a served page boots on the loading spec (no
        # source) — the SSE snapshot handler fills the span from SPEC.dataSource (review P2 sibling)
    tog = ('<button id="themetog" class="themetog" type="button" onclick="toggleTheme()" '
           'title="Toggle light / dark theme (default: follow the app)"></button>') if live else ""
    return '<span class="foottext">' + txt + '</span>' + tog


def render_html(spec) -> str:
    """A dashboard spec -> a self-contained NO-JS HTML page (inline SVG charts; renders anywhere)."""
    spec = _with_active(spec)
    parts = {
        # v1.126: the static page renders the REAL head (ticker/name/type/price) — pre-fix the
        # template had no __HEAD__ slot, so a head-bearing spec's whole header vanished on the
        # no-JS path (and a 205KB klinecharts inline was computed for a template that never
        # used it — dropped: a no-JS page can't run it).
        "__HEAD__": _head_html(spec, live=False),
        "__TITLE__": esc(spec.get("title", "dashboard")),
        "__SUBTITLE__": _sub_html(spec),
        "__CARDS__": _cards_html(spec.get("cards", [])),
        "__ABOUTTOP__": _lead_html(spec, live=False),
        "__ABOUT__": _about_html(spec),
        "__PANELS__": _panels_html(spec.get("panels", [])),
        "__FOOT__": _foot(source=spec.get("dataSource", "")),   # static (no JS): no toggle (a dead button)
    }
    # Single pass: each placeholder is replaced exactly once; the replacement text is NOT re-scanned,
    # so data can't inject or clobber a placeholder (a function repl is also literal — no backref magic).
    return re.sub(r"__(?:HEAD|TITLE|SUBTITLE|CARDS|ABOUTTOP|ABOUT|PANELS|FOOT)__", lambda m: parts[m.group(0)], HTML_TEMPLATE)


# ============================ Phase 2: live-push (SSE) ============================
def _now() -> str:
    return dt.datetime.now().strftime("%H:%M:%S")


# --- per-block diff (DD-2): send only the blocks that changed; a STRUCTURE change -> full snapshot
# (the client rebuilds, because the charts' axes/labels moved and the built Chart objects are stale).
def _panel_shape(p):
    d = p.get("data") or {}
    t = p.get("type")
    if t == "smile":
        return (t, p.get("title"), (tuple(d.get("strikes") or []), d.get("atm")))
    if t in ("bars", "line"):
        return (t, p.get("title"), tuple(d.get("labels") or []))
    if t == "table":
        return (t, p.get("title"), tuple(d.get("columns") or []))
    # Unknown / registry-pending canvas type: be conservative — key the WHOLE data, so ANY change forces
    # a full snapshot. (A delta could otherwise silently patch a stale-axis chart the day such a type
    # gains a renderer whose axis isn't captured above.)
    return (t, p.get("title"), json.dumps(d, sort_keys=True, default=str))


def _structure(spec):
    cards = tuple(c.get("label") for c in (spec.get("cards") or []))
    panels = tuple(_panel_shape(p) for p in (spec.get("panels") or []))
    return (cards, panels)


def diff_spec(prev, cur):
    """Diff two specs into the minimal live payload. Returns a full 'snapshot' when the structure
    (card labels, panel types/titles, or a chart's axis) changed — the page must rebuild — else a
    'delta' carrying ONLY the cards whose value changed and the panels whose data changed."""
    if prev is None or _structure(prev) != _structure(cur):
        return {"type": "snapshot", "spec": cur}
    pc, cc = prev.get("cards") or [], cur.get("cards") or []
    cards = {i: c.get("value") for i, c in enumerate(cc) if i >= len(pc) or c.get("value") != pc[i].get("value")}
    pp, cp = prev.get("panels") or [], cur.get("panels") or []
    panels = {i: p.get("data") for i, p in enumerate(cp) if i >= len(pp) or p.get("data") != pp[i].get("data")}
    out = {"type": "delta", "cards": cards, "panels": panels}
    # title/subtitle aren't part of _structure (so they don't force a rebuild), but they DO change (e.g. an
    # "as of {asOf}" subtitle each poll) — carry them so the header re-syncs on the delta path too.
    if cur.get("title") != prev.get("title"):
        out["title"] = cur.get("title")
    if cur.get("subtitle") != prev.get("subtitle"):
        out["subtitle"] = cur.get("subtitle")
    if cur.get("head") != prev.get("head"):   # a head-based page (--full): the live spot rides in head.rightBig
        out["head"] = cur.get("head")
    return out


def _delta_event(ev, badge):
    """Build the SSE 'delta' payload from a diff_spec delta — the changed cards + panels PLUS the header
    extras (head/title/subtitle) the client re-syncs on the delta path (applyDelta reads d.head/d.title/
    d.subtitle). Returns None when NOTHING changed (a no-op poll only ticks the badge). Carrying `head`
    is what keeps the --full live spot moving — it rides in head.rightBig; dropping it (the old inline
    emitter sent only cards/panels) froze the header at its first-snapshot value."""
    extra = {k: ev[k] for k in ("head", "title", "subtitle") if k in ev}
    if not (ev.get("cards") or ev.get("panels") or extra):
        return None
    return {"cards": ev.get("cards") or {}, "panels": ev.get("panels") or {}, "badge": badge, **extra}


def _sse(event, data) -> str:
    """One Server-Sent-Event frame. '<'/'>' are escaped so a payload can't break the page/stream."""
    payload = json.dumps(data, separators=(",", ":")).replace("<", "\\u003c").replace(">", "\\u003e")
    return f"event: {event}\ndata: {payload}\n\n"


# --- liveness badge (DD-5): the PROVIDER declares its data mode + a timestamp; the badge shows it.
LIVENESS = {
    "tick": ("● LIVE", "live-tick"),            # a real push socket — sub-second updates
    "delayed": ("● DELAYED", "live-delayed"),   # server polls a REST source every interval
    "settlement": ("● SETTLEMENT", "live-settle"),  # illiquid/marked — settlements, no live ticks
}


def badge_html(liveness) -> str:
    liveness = liveness or {}
    label, cls = LIVENESS.get(liveness.get("mode"), ("● " + str(liveness.get("mode", "?")), "live-settle"))
    detail, ts = liveness.get("detail") or "", liveness.get("ts") or ""
    txt = label + (" · " + detail if detail else "") + (" · " + ts if ts else "")
    return f'<div class="livebar {cls}" id="livebar">{esc(txt)}</div>'


# --- provider seam (DD-3): a data producer the live server drives. Duck-typed on these 4 members.
class Provider:
    """initial()->spec (first paint), poll()->spec|None (next; None=unchanged), liveness()->badge,
    .interval (seconds between polls). Subclass or use CallableProvider."""
    interval = 10.0

    def initial(self):
        raise NotImplementedError

    def poll(self):
        return None

    def liveness(self):
        return {"mode": "settlement", "ts": "", "detail": ""}


class CallableProvider(Provider):
    """Wrap a `produce()->spec` callable (fetch + compute + to_spec). `poll()` re-produces; the
    server diffs. `initial()` caches so a page reload/reconnect doesn't re-fetch."""

    def __init__(self, produce, interval=10.0, mode="settlement", detail=""):
        self.produce, self.interval, self.mode, self.detail = produce, interval, mode, detail
        self._spec, self._ts = None, ""

    def initial(self):
        if self._spec is None:
            self._spec, self._ts = self.produce(), _now()
        return self._spec

    def poll(self):
        self._spec, self._ts = self.produce(), _now()
        return self._spec

    def liveness(self):
        return {"mode": self.mode, "ts": self._ts, "detail": self.detail}


LIVE_STYLE = r"""  button.ask { position:absolute; top:4px; right:6px; background:none; border:none;
    cursor:pointer; color:inherit; font-size:10px; line-height:1; padding:2px 3px; opacity:0;
    transition:opacity .15s; }
  .m:hover .ask, .panel:hover .ask { opacity:.35; }
  button.ask:hover { opacity:.9; }
  .panel { position:relative; }
  .panel h3 button.ask { top:10px; }
  .asktoast { position:fixed; bottom:18px; left:50%; transform:translate(-50%,8px); z-index:99;
    background:rgba(20,23,28,.92); color:#e8e8e8; border-radius:8px; padding:8px 14px;
    font-size:12px; opacity:0; pointer-events:none; transition:opacity .2s, transform .2s; }
  .asktoast.on { opacity:1; transform:translate(-50%,0); }
  .livebar { position:sticky; top:0; z-index:9; color:#fff; padding:6px 14px;
    font:600 12px -apple-system,Segoe UI,sans-serif; transition:filter .7s ease-out; }
  .live-tick { background:#0F6E56; } .live-delayed { background:#B7791F; } .live-settle { background:#5b616e; }
  .zoom { float:right; font-weight:400; }
  .zoom button { background:rgba(128,128,128,.2); color:inherit; border:0; border-radius:4px;
    width:22px; height:18px; margin-left:3px; cursor:pointer; font-size:12px; line-height:1; }
  .zoom button:hover { background:rgba(128,128,128,.4); }
  .vtoggle { float:right; font-weight:400; margin-left:8px; }   /* per-panel data-view toggle (e.g. greek normalization) */
  .vtoggle button { background:rgba(128,128,128,.18); color:#9aa0a8; border:0; border-radius:4px;
    height:18px; margin-left:3px; padding:0 8px; cursor:pointer; font-size:10.5px; line-height:1; }
  .vtoggle button:hover { background:rgba(128,128,128,.34); color:inherit; }
  .vtoggle button.on { background:rgba(55,138,221,.35); color:inherit; }
  .kltv { border:1px solid #4a4f56; border-radius:6px; padding:0 9px; height:22px; display:inline-flex;
    align-items:center; font-size:11px; color:#8a8f98; text-decoration:none; white-space:nowrap; }
  .kltv:hover { color:#e8e8e8; border-color:#378ADD; }
  .swbar { display:flex; align-items:center; gap:8px; flex-wrap:wrap; margin:2px 0 10px;
    padding:8px 12px; background:rgba(128,128,128,.10); border:1px solid #2a2f36; border-radius:8px; }
  .swbar label { font:600 12px -apple-system,Segoe UI,sans-serif; color:#8a8f98; }
  .swbar select { background:rgba(128,128,128,.15); color:inherit; border:1px solid #777; border-radius:6px;
    padding:4px 8px; font-size:13px; cursor:pointer; max-width:100%; }
  .swbar .swhint { color:#8a8f98; font-size:11px; }
  .swbar .swleg { color:#8a8f98; font-size:11px; margin-left:auto; }
  .swbar .swleg b { font-weight:700; }
  .tbl-leg { color:#8a8f98; font-size:11px; margin:8px 2px 2px; }
  .tbl-leg b { font-weight:700; }
  .tabbar { display:flex; gap:2px; border-bottom:1px solid #2a2f37; margin:2px 0 12px; flex-wrap:wrap; }
  .tabbar .tab { background:none; border:none; border-bottom:2px solid transparent; color:#8a8f98;
    font:500 14px -apple-system,Segoe UI,sans-serif; padding:8px 15px; cursor:pointer; }
  .tabbar .tab:hover { color:inherit; }
  .tabbar .tab.on { color:inherit; font-weight:600; border-bottom-color:#378ADD; }
  .tabbar .tab .tablive { display:inline-block; width:7px; height:7px; border-radius:50%; background:#2ec77e;
    vertical-align:middle; position:relative; top:-1px; margin-right:6px; animation:tabpulse 2.4s ease-in-out infinite; }
  h3 .panlive-dot { display:inline-block; width:7px; height:7px; border-radius:50%; background:currentColor;
    color:#2ec77e; vertical-align:middle; position:relative; top:-1px; margin-right:3px;
    animation:tabpulse 2.4s ease-in-out infinite; }   /* leading live dot — a CSS circle; ONLY the dot blinks (slow) */
  h3 .panlive-dot.live-delayed { color:#EFB366; } h3 .panlive-dot.live-settle { color:#8a8f98; }
  h3 .panlive { float:right; font-size:11px; font-weight:600; color:#2ec77e; margin-left:8px; background:none; }   /* age counter — green, STATIC (only the dot blinks) */
  @keyframes tabpulse { 0%,100%{opacity:1} 50%{opacity:.15} }   /* clearer blink for the live dots */
  .warming { color:#8a8f98; font-size:13px; padding:24px 8px; text-align:center; }"""

# The live page bakes the initial spec (first paint + a no-JS fallback of the same DOM), then an
# EventSource takes over: 'snapshot' rebuilds the shell, 'delta' patches only changed cards/panels,
# 'poll' refreshes the liveness badge. It reuses the SAME block registry (SHARED_JS) and adaptive
# grid (GRID_JS) as the static page — one source of truth.
_NET_REACHABLE = None  # memoized egress-probe result for =auto (probe once per process, not per call)


def _probe_timeout_exc(e):
    """Is this probe failure a TIMEOUT (ambiguous: slow vendor OR silent-drop firewall), as
    opposed to an instant refusal (connection refused / DNS failure — a real wall answers at
    once)? Checks the exception AND a URLError's wrapped .reason; py3.9-safe (socket.timeout
    is not yet TimeoutError there)."""
    import socket as _s
    if isinstance(e, (_s.timeout, TimeoutError)):
        return True
    reason = getattr(e, "reason", None)
    if isinstance(reason, (_s.timeout, TimeoutError)):
        return True
    # str fallback is deliberately COST-BOUNDED: a false positive here buys one extra 10s
    # attempt and the verdict still lands False — never a wrong "reachable".
    return "timed out" in str(e).lower()


def net_reachable(timeout=3.0, retry_timeout=10.0, _opener=None):
    """Fast egress litmus for MARKET_DATA_NO_NET=auto: does this process have outbound HTTPS?
    A quick GET to Alpha Vantage — ANY HTTP response (even a 4xx/5xx: the host answered) proves
    reachability; a REFUSED connection / DNS failure means the sandbox blocked egress (fail at
    once). A TIMEOUT is ambiguous — a slow-but-alive vendor is indistinguishable from a
    silent-drop firewall at 3s (the WDC false-negative 2026-07-18: AV answered direct fetches
    seconds after the probe called it "blocked") — so a timeout gets ONE retry on a longer
    window before concluding blocked; worst case ~13s, still far under the ~60s direct-fetch
    burn this probe exists to avoid. `_opener` is a seam so the offline gate injects a fake
    and never touches the network."""
    import urllib.request as _u
    import urllib.error as _ue
    opener = _opener or _u.urlopen
    for t in (timeout, retry_timeout):
        try:
            r = opener("https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol=IBM&apikey=demo",
                       timeout=t)
            try:
                r.close()
            except Exception:
                pass
            return True
        except _ue.HTTPError:
            return True        # the host answered (401/403/429/5xx) -> egress works
        except Exception as e:
            if not _probe_timeout_exc(e):
                return False   # refused / DNS / proxy wall -> egress blocked, no retry
    return False               # timed out on BOTH windows -> treat as blocked


def no_net_blocked(probe=None):
    """Decide whether direct HTTP is disabled, honoring MARKET_DATA_NO_NET (case-insensitive):
      unset / '' / '0' / 'false' / 'no' / 'off'  -> NOT blocked  (fetch directly — the normal
                                                     local path AND the open-egress Cowork case)
      'auto'                                      -> PROBE egress; blocked only if unreachable
                                                     (the Cowork self-router — no more guessing)
      any other value ('1' / 'true' / …)          -> blocked      (explicit force-off; no probe)
    `probe` is an injected ()->bool egress test for the offline gate; production memoizes ONE
    real probe per process so --full (gather + term) doesn't pay for two round-trips."""
    global _NET_REACHABLE
    val = (os.environ.get("MARKET_DATA_NO_NET") or "").strip().lower()
    if val in ("", "0", "false", "no", "off"):
        return False
    if val == "auto":
        if probe is not None:
            return not probe()
        if _NET_REACHABLE is None:
            _NET_REACHABLE = net_reachable()
        return not _NET_REACHABLE
    return True


# v1.125.1: LOCAL agent-mode mounts /sessions; the CLOUD Cowork VM does NOT (the INTC session
# false-negative: a v1.124.0 render took the desktop spawn+pending path, so the options chip
# stayed hidden forever behind the viewer-dead HEAD-poll). Any marker hit means Cowork.
_COWORK_MARKERS = ("/sessions", "/mnt/user-data", "/mnt/outputs")


def in_cowork(_probe=None, _isdir=None):
    """True inside a Cowork / agent-mode sandbox — LOCAL (the `/sessions` mount) or CLOUD (the
    `/mnt/*` workspace mounts; see _COWORK_MARKERS). MARKET_DATA_COWORK overrides detection for
    odd sandboxes (=1 force-on, =0 force-off; empty = unset). `_probe`/`_isdir` are test seams.
    Callers use this to switch fragile background/HEAD-poll machinery (dead in the Cowork
    file-preview viewer) to eager synchronous work."""
    if _probe is not None:
        return _probe
    env = os.environ.get("MARKET_DATA_COWORK")
    if env and env.strip():   # empty reads as unset — markers decide, never a silent force-off
        # v1.126: case/space-insensitive — "FALSE"/"No "/"off" silently forced Cowork mode ON
        return env.strip().lower() not in ("0", "false", "no", "off")
    isdir = _isdir or os.path.isdir
    return any(isdir(p) for p in _COWORK_MARKERS)


def check_no_net(route_hint, probe=None, _cowork=None):
    """Cowork/agent-mode sandboxes MAY block egress. MARKET_DATA_NO_NET gates a script's network
    entry so it fast-fails with its own routing hint instead of ~60s of timeouts: `=1` forces it
    off; `=auto` probes and fails ONLY when egress is genuinely blocked (see no_net_blocked).
    In a LOCAL run (`=auto`, no Cowork /sessions mount) the exit ALSO says to retry without the
    env var first — a failed probe there may be a slow vendor or the local Bash sandbox, and the
    Cowork MCP route is the wrong first move (the WDC session burned two calls on it). `_cowork`
    is a test seam over the /sessions mount check."""
    if no_net_blocked(probe):
        val = (os.environ.get("MARKET_DATA_NO_NET") or "").strip().lower()
        cw = in_cowork(_cowork)
        note = ""
        if val == "auto" and not cw:
            note = ("Local run + =auto: the failed probe may mean a SLOW vendor or the local "
                    "Bash sandbox, not blocked egress — first retry without MARKET_DATA_NO_NET "
                    "(or unsandboxed; costs the ~60s timeout ladder if egress truly IS blocked); "
                    "the MCP route is the fallback. ")
        sys.exit("error: MARKET_DATA_NO_NET — direct HTTP disabled. " + note + route_hint)


POLL_DEFAULT_S = 2   # the ONE live-poll default (options --watch, bridge artifacts; user: 2s, 2026-07-05)

# Cowork-artifact transport (probed 2026-07-05): the side-panel artifact exposes
# window.cowork.callMcpTool(name, args) -> {content, structuredContent, isError}; MCP tools
# execute HOST-side, so the page can poll live data (e.g. get_futures_options_smile reads the
# connector's tick buffer) with no egress and no server. Deltas ride the SAME applyDelta
# machinery as SSE — a producer-supplied mapper turns the tool payload into a delta.
BRIDGE_JS = r"""__MAPPERJS__
const BRIDGE=__BRIDGECFG__;
let bridgeInFlight=false;
async function bridgeTick(){
  if(bridgeInFlight) return;                    // a call slower than the interval must not stack / repaint out of order
  if(!(window.cowork&&window.cowork.callMcpTool)){
    BADGE={mode:"delayed",detail:"bridge unavailable \u2014 open this page as a Cowork artifact",ts:""};
    renderBadge(); return; }
  bridgeInFlight=true;
  try{
    const r=await window.cowork.callMcpTool(BRIDGE.tool, BRIDGE.args||{});
    let payload=r&&r.structuredContent;
    if(payload==null){ const t=r&&r.content&&r.content[0]&&r.content[0].text; payload=t?JSON.parse(t):null; }
    if(!payload||(r&&r.isError)) throw new Error(String((payload&&payload.error)||"tool error").slice(0,120));
    const map=window[BRIDGE.mapper]; if(!map) throw new Error("no mapper "+BRIDGE.mapper);
    const d=map(payload, SPEC);
    if(d) applyDelta(d);                       // same per-block patch path as SSE deltas
  }catch(e){ BADGE={mode:"delayed",detail:String(e&&e.message||e).slice(0,100),ts:""}; renderBadge(); }
  finally{ bridgeInFlight=false; }
}
setInterval(bridgeTick, (BRIDGE.intervalMs||__POLLMS__)); bridgeTick();""".replace("__POLLMS__", str(POLL_DEFAULT_S * 1000))

SSE_JS = r"""const es=new EventSource("/stream");
es.addEventListener("snapshot", e=>{ const p=JSON.parse(e.data); const ns=p.spec; if(p.badge) BADGE=p.badge; lastUpdate=Date.now(); PANEL_TS={}; PANEL_BASE=Date.now();   // fresh page -> every panel counts from the snapshot
  if(SPEC.liveSection && SPEC.active!==SPEC.liveSection){   // off the live tab: refresh the stored section, DON'T yank
    const cur=(SPEC.sections||[]).find(x=>x.id===SPEC.liveSection), nw=(ns.sections||[]).find(x=>x.id===SPEC.liveSection);
    if(cur&&nw){ cur.cards=nw.cards; cur.panels=nw.panels; } renderBadge(); return; }
  if(CHARTS.length && structSig(ns)===structSig(SPEC)){   // SAME structure as what's already drawn -> patch,
    SPEC=ns; setHead(); (SPEC.cards||[]).forEach((c,i)=>setCard(i,c.value));   // don't rebuild (kills the startup redraw)
    (SPEC.panels||[]).forEach((pn,i)=>renderPanel(i, pn.data)); }
  else { SPEC=ns; renderShell(); }
  mountTabs(); mountSwitcher();   // a snapshot can introduce sections/switcher (the live page boots on a plain loading spec)
  const lsl=document.getElementById("leadSlot");   // v1.112: the lead + footer source arrive WITH the real spec —
  if(lsl){ const nh=leadHTML(SPEC.lead); if(lsl.__lead!==nh){ lsl.__lead=nh; lsl.innerHTML=nh; initLead(); } }   // (guarded: an unchanged lead keeps its open/closed state)
  const fsr=document.getElementById("ftsrc"); if(fsr&&SPEC.dataSource) fsr.textContent=String(SPEC.dataSource)+" · ";
  renderBadge(); });
es.addEventListener("delta", e=>applyDelta(JSON.parse(e.data)));
es.addEventListener("sections", e=>{ const p=JSON.parse(e.data); const upd=p.sections||[]; if(p.badge) BADGE=p.badge;   // a background load (cold-name vol history) finished: swap the loading sections for the real ones
  upd.forEach(ns=>{ const S=SPEC.sections||[]; const i=S.findIndex(s=>s.id===ns.id); if(i>=0) S[i]=ns; });   // replace Overview/History in place; other tabs are untouched
  if(upd.some(ns=>ns.id===SPEC.active)) showSection(SPEC.active);   // viewing an updated tab -> re-render it now (no reload); others fill in on their next click
  renderBadge(); });
es.addEventListener("poll", e=>{ const p=JSON.parse(e.data); if(p.badge) BADGE=p.badge; lastUpdate=Date.now(); renderBadge(); });
es.onerror=()=>{ if(BADGE) BADGE=Object.assign({}, BADGE, {detail:"reconnecting…"}); renderBadge(); };"""

LIVE_TEMPLATE = (r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__ __TSUF__</title>
<style>
""" + BASE_STYLE + "\n" + LIVE_STYLE + r"""
</style></head>
<body>
__LIVEBAR__
<div class="wrap">
  __HEAD__
  <div class="sub" id="sub">__SUBTITLE__</div>
  <div id="leadSlot">__ABOUTTOP__</div>
  __CARDS__
  <div id="aboutSlot">__ABOUT__</div>
  <div class="grid" id="grid">__PANELS__</div>
  <div class="foot">__FOOT__</div>
</div>
<script>__CHARTJS__</script>
<script>__KLINEJS__</script>
<script>
let SPEC = __SPEC__;
let BADGE = __BADGE__;
""" + SHARED_JS + r"""
let CHARTS=[];
let lastUpdate=Date.now();
let PANEL_TS={};   // per-panel last-update stamp (a delta carries ONLY the panels that changed) -> each panel's live counter resets on ITS own update, not the page's
let PANEL_BASE=Date.now();   // fallback for a panel that hasn't changed since the last snapshot (STABLE — not lastUpdate, which resets every poll and would fake a uniform update)
const esc=s=>String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
const escA=s=>esc(s).replace(/"/g,"&quot;");   // attribute sink (style/title) — quotes too (v1.126)
// v1.153 twin of the server rel_href_ok — a row link is producer DATA; relative files only.
function relOK(h){ h=String(h||""); return !!h && h.charAt(0)!=="/" && h.slice(0,2)!=="//"
  && h.split("/")[0].indexOf(":")<0 && h.indexOf("\\")<0; }
function tableHTML(d,noLeg){ const cols=d.columns||[], rows=d.rows||[], st=d.rowstyle||[], cst=d.cellstyle||[], leg=d.legend||[];
  const sty=s=>esc(s).replace(/"/g,"&quot;"), hints=d.colhints||{};   // v1.105 colhints twin
  const lks=d.links||[], lcol=d.linkcol|0;
  const h=cols.map(c=>hints[c]?'<th><span class="htip" data-tip="'+sty(String(hints[c]))+'">'+esc(c)+"</span></th>":"<th>"+esc(c)+"</th>").join("");
  const b=rows.map((r,i)=>{ const cs=cst[i]||[]; let lk=lks[i]||null; if(lk&&!relOK(lk.href)) lk=null;
    return "<tr"+(st[i]?' style="'+sty(st[i])+'"':"")+">"+r.map((v,j)=>{
      let inner=esc(v);
      if(lk&&j===lcol) inner='<a class="tlink" href="'+sty(String(lk.href))+'" data-ask="'+sty(String(lk.ask||""))+'">'+inner+"</a>";
      return "<td"+(cs[j]?' style="'+sty(cs[j])+'"':"")+">"+inner+"</td>"; }).join("")+"</tr>"; }).join("");
  const lh=leg.length?'<div class="tbl-leg">'+leg.map(l=>'<b style="color:'+sty(l.color||"")+'">'+esc(l.sym||"")+"</b> "+esc(l.label||"")).join("&nbsp;&nbsp;·&nbsp;&nbsp;")+"</div>":"";
  return "<table"+(d.big?' class="big"':"")+"><thead><tr>"+h+"</tr></thead><tbody>"+b+"</tbody></table>"+(noLeg?"":lh); }
function tableLegHTML(d){ const leg=(d||{}).legend||[]; if(!leg.length) return "";   // v1.107.1: outside-the-box legend twin
  return '<div class="tbl-leg">'+leg.map(l=>'<b style="color:'+esc(String(l.color||"")).replace(/"/g,"&quot;")+'">'+esc(l.sym||"")+"</b> "+esc(l.label||"")).join("&nbsp;&nbsp;\u00b7&nbsp;&nbsp;")+"</div>"; }
const ASKB='<button class="ask" title="copy a question about this block for the AI chat">\u2726</button>';
function cardsHTML(sp){ return (sp.cards||[]).map((c,i)=>{ const h=c.hint?esc(c.hint).replace(/"/g,"&quot;"):"";
  const exp=(c.scope==="expiry");
  let xc="";
  if(c.sub){ const S=Array.isArray(c.sub)?c.sub:[c.sub]; xc=S.map(s=>'<div class="mx">'+esc(s)+"</div>").join(""); }
  if(exp&&c.scopeLabel) xc+='<div class="mx" id="mx'+i+'">'+esc(c.scopeLabel)+"</div>";   // v1.130: subs compose, caption last (twin of the server)
  if(c.countdown){ const sch=esc(JSON.stringify(c.countdown.schedule||[])).replace(/"/g,"&quot;");
    const pfs=c.countdown.phaseFromSchedule?' data-pfs="1"':"";   // twin of the server _pfs
    xc+='<div class="mx sess-cd"'+pfs+' data-cd="'+sch+'">'+esc(c.countdown.text||"")+"</div>"; }
  const cls=(exp?"m exp":"m")+(c.wide?" wide":"");
  return '<div class="'+cls+'"'+(c.hint?' data-tip="'+h+'"':'')+'><div class="l">'+esc(c.label||"")+'</div><div class="'+(c.small?"v sm":"v")+'" id="cv'+i+'">'+esc(c.value||"")+'</div>'+xc+ASKB+"</div>"; }).join(""); }
function rangebarHTML(d,i){ d=d||{};
  if(d.bars!=null){ const u=d.unit||"", cols=["#35b8a0","#5a9ff0","#e8b055","#c88ce8","#e07a5a"];   // ONE track + coloured dots + median notches per tenor — twin of the server
    let ticks="",dots="",legs="",j=0;
    for(const b of d.bars){ if(b.cur==null||b.min==null||b.max==null||b.max<=b.min) continue;
      const c=cols[j%cols.length]; j++; const span=b.max-b.min;
      const cp=Math.max(0,Math.min(100,(b.cur-b.min)/span*100));
      const rk=b.rank!=null?" · rank "+Math.round(b.rank)+"%":"", pc=b.pct!=null?" · "+Math.round(b.pct)+"% pctile":"", mt=b.median!=null?" · median "+Math.round(b.median)+u:"";
      const tip=(b.label||"")+" · IV "+Math.round(b.cur)+u+rk+pc+mt+" · range "+Math.round(b.min)+"–"+Math.round(b.max)+u;   // esc THEN quote-replace at the attr (below) = esc_attr order; the old pre-replace double-escaped a "
      if(b.median!=null) ticks+='<span class="rb-tick" style="left:'+Math.max(0,Math.min(100,(b.median-b.min)/span*100)).toFixed(1)+'%;background:'+c+'"></span>';
      dots+='<span class="rb-dot" style="left:'+cp.toFixed(1)+'%;background:'+c+'" title="'+esc(tip).replace(/"/g,"&quot;")+'"></span>';
      legs+='<span class="rb-lg"><i style="background:'+c+'"></i>'+esc(String(b.label||""))+" "+Math.round(b.cur)+u+(b.rank!=null?" · r"+Math.round(b.rank):"")+'</span>'; }
    if(!dots) return '<div class="rbar rbar-multi"><div class="cap" style="padding:10px 4px">not enough history for a range yet</div></div>';
    return '<div class="rbar rbar-multi"><div class="rb-track">'+ticks+dots+'</div><div class="rb-leg">'+legs+'</div></div>'; }
  const lo=d.min,hi=d.max,cur=d.cur,med=d.median,u=d.unit||"";
  if(lo==null||hi==null||cur==null||hi<=lo) return '<div class="cap" style="padding:10px 4px">not enough history for a range yet</div>';
  const clamp=v=>Math.max(0,Math.min(100,(v-lo)/(hi-lo)*100)), cp=clamp(cur);
  const sub=[]; if(d.rank!=null)sub.push("IV rank "+Math.round(d.rank)+"%"); if(d.pct!=null)sub.push(Math.round(d.pct)+"% pctile");
  const subh=sub.length?' <span class="cap">'+esc(sub.join(" · "))+'</span>':"";
  const medt=med!=null?'<span class="rb-med" style="left:'+clamp(med).toFixed(1)+'%"></span>':"";
  const mid=med!=null?'<span>med '+Math.round(med)+u+'</span>':"<span></span>";
  return '<div class="rbar"><div class="rb-head"><b>'+Math.round(cur)+u+'</b> now'+subh+'</div>'
    +'<div class="rb-track"><span class="rb-fill" style="width:'+cp.toFixed(1)+'%"></span>'+medt
    +'<span class="rb-now" style="left:'+cp.toFixed(1)+'%"></span></div>'
    +'<div class="rb-scale"><span>'+Math.round(lo)+u+'</span>'+mid+'<span>'+Math.round(hi)+u+'</span></div></div>'; }
function fmtNum(x,nd){ if(x==null||x===""||!isFinite(+x)) return ""; x=+x;   // _fmt_num twin (v1.126):
  if(nd==null){const ax=Math.abs(x); nd=ax>=100?0:(ax>=10?1:(ax>=1?2:3));}   // adaptive decimals.
  // NUMERIC PARITY with Python's f"{x:,.Nf}" (re-review P2: the Intl formatter diverged on ~1.2%
  // of real prices — it rounds the shortest-decimal form half-expand; Python rounds the binary
  // double half-even). toFixed IS correctly rounded on the double, so the ONLY gap is an EXACT
  // decimal tie — possible only for a dyadic x with denominator 2^(nd+1) (bit test below is
  // exact: *2^k never rounds) — where toFixed goes half-up and Python half-even. Verified
  // node-vs-CPython: 0 mismatches / 40,011-value sweep incl. 286.5/64.25/1.125/-287.5 ties.
  const m=Math.pow(10,nd), t2=x*Math.pow(2,nd+1);
  let v=x;
  if(t2%1===0&&(t2/2)%1!==0) v=2*Math.round(x*m/2)/m;   // exact tie -> half-even like Python
  let s=v.toFixed(nd);
  const neg=s.charAt(0)==="-"; if(neg) s=s.slice(1);
  const dot=s.indexOf("."), ip=dot<0?s:s.slice(0,dot), fp=dot<0?"":s.slice(dot);
  return (neg?"-":"")+ip.replace(/\B(?=(\d{3})+(?!\d))/g,",")+fp; }   // en-US comma grouping
function ratebarHTML(d,i){ d=d||{}; const segs=[["strongBuy","#0F6E56"],["buy","#7fc8a9"],["hold","#EFB366"],["sell","#e08a8a"],["strongSell","#C0392B"]]
    .map(([k,c])=>[k,+(d[k]||0),c]).filter(x=>x[1]>0);
  const bar=segs.map(([k,v,c])=>'<span style="flex:'+v+';background:'+c+'"></span>').join("");
  const up=(d.target&&d.price)?(()=>{const u=(d.target/d.price-1)*100;
    return ' <span class="cap">'+(u>=0?"+":"")+u.toFixed(0)+"% vs "+fmtNum(+d.price)+'</span>';})():"";
  const lab=segs.map(([k,v])=>v+" "+k.replace("strongBuy","strong-buy").replace("strongSell","strong-sell")).join(" · ");
  let dtl="";
  if(!(d.est||[]).length) dtl='<div class="cap">per-period estimates unavailable in this pull (the provider returned none) — re-render to retry</div>';   // v1.105.1 honest hole twin
  if((d.est||[]).length){ const sgn=v=>v!=null?((v>=0?"+":"")+(+v).toFixed(2)):"—";
    const rows=d.est.map(r=>{ const rt=(r.revLoB!=null&&r.revHiB!=null)?' title="$'+fmtNum(r.revLoB)+'B–$'+fmtNum(r.revHiB)+'B'+(r.revN?' · '+r.revN+' analysts':'')+'"':"";
      return "<tr><td>"+esc(r.label||"")+"</td><td>"+fmtNum(r.eps)+"</td><td>"+(r.lo!=null&&r.hi!=null?fmtNum(r.lo)+"–"+fmtNum(r.hi):"—")+"</td><td>"+sgn(r.d7)+"</td><td>"+sgn(r.d30)+"</td><td>"+(r.up30||0)+"/"+(r.down30||0)+"</td><td>"+(r.n||0)+"</td><td"+rt+">"+(r.revB!=null?"$"+fmtNum(r.revB)+"B":"—")+"</td></tr>"; }).join("");   // fmtNum: the _fmt_num twin (v1.126 — raw floats like $102.345678901B were the SHIPPED look)
    dtl='<button class="dtl" onclick="tgD(\'dtlr'+i+'\',this)">▾ estimates</button><div class="dtlbox" id="dtlr'+i+'"><table><thead><tr><th></th><th>EPS est</th><th>low–high</th><th>Δ7d</th><th>Δ30d</th><th>revis ↑/↓</th><th>analysts</th><th>revenue</th></tr></thead>'+rows+'</table><div class="dtlleg"><div class="cap"><b>EPS est</b> — analyst-consensus earnings per share for the period</div><div class="cap"><b>low–high</b> — full spread of individual analyst estimates</div><div class="cap"><b>Δ7d / Δ30d</b> — consensus drift vs 7 / 30 days ago; + means estimates are being raised</div><div class="cap"><b>revis ↑/↓</b> — analysts who raised / cut their EPS estimate in the trailing 30 days</div><div class="cap"><b>analysts</b> — how many analysts submit an EPS estimate for this period</div><div class="cap"><b>revenue</b> — consensus revenue; hover it for the low–high range and its analyst count</div></div></div>'; }
  const stamp=(d.estAsOf&&(d.est||[]).length)?'<div class="cap">estimates from the last good pull '+esc(String(d.estAsOf))+' \u2014 this pull returned none</div>':"";   // v1.107 LKG stamp twin
  return '<div class="rate"><div class="big">Target '+fmtNum(d.target)+up+'</div><div class="splitbar">'+bar+'</div><div class="cap">'+esc(lab)+'</div>'+stamp+dtl+'</div>'; }
function sentibarHTML(d,i){ d=d||{};
  if(!d.n){ if(d.nAll) return '<div class="rate"><div class="big">0 of '+d.nAll+' <span class="cap">'+(d.relmin?"pass relevance ≥ "+d.relmin:"relevant")+'</span></div>'+(d.avgAll!=null?'<div class="cap">full feed '+(d.avgAll>=0?"+":"")+(+d.avgAll).toFixed(2)+" · "+d.nAll+" articles</div>":"")+"</div>";
    return '<div class="cap">no news data</div>'; }
  const bar=[[d.bull,"#1D9E75"],[d.neutral,"#bbb"],[d.bear,"#C0392B"]].filter(x=>x[0]>0)
    .map(([v,c])=>'<span style="flex:'+v+';background:'+c+'"></span>').join("");
  let dtl="";
  if((d.articles||[]).length){ const rows=d.articles.map(a=>{ const lab=String(a.label||"");
    const col=lab.indexOf("Bullish")>=0?"rgba(29,158,117,.22)":(lab.indexOf("Bearish")>=0?"rgba(192,57,43,.22)":"rgba(128,128,128,.22)");
    const url=String(a.url||""); const t=esc(a.title||"");
    const link=url.search(/^https?:\/\//)===0?'<a href="'+esc(url).replace(/"/g,"&quot;")+'" target="_blank" rel="noopener">'+t+"</a>":"<span>"+t+"</span>";
    const summ=a.sum?'<div class="cap nsum">'+esc(a.sum)+"</div>":"";   // v1.106: visible summary twin
    return '<div class="nrow"><span class="chip" style="background:'+col+'">'+esc(lab)+"</span> "+link+summ
      +'<div class="cap">'+esc([a.d,a.src].filter(Boolean).join(" · "))+(a.score!=null?" · "+(a.score>=0?"+":"")+(+a.score).toFixed(2):"")+"</div></div>"; }).join("");
    dtl='<button class="dtl" onclick="tgD(\'dtln'+i+'\',this)">▾ headlines</button><div class="dtlbox" id="dtln'+i+'"><div class="cap">↗ opens here in the built-in browser · use the pane’s ← to come back</div>'+rows+"</div>"; }
  return '<div class="rate"><div class="big">'+(d.avg>=0?"+":"")+(+d.avg).toFixed(2)+' <span class="cap">'+esc(d.label||"")+'</span></div><div class="splitbar">'+bar+'</div><div class="cap">'+d.bull+' bull · '+d.neutral+' neu · '+d.bear+' bear ('+d.n+' articles'+(d.relmin?" · relevance ≥ "+d.relmin:"")+')</div>'+(d.avgAll!=null?'<div class="cap">full feed '+(d.avgAll>=0?"+":"")+(+d.avgAll).toFixed(2)+" · "+(d.nAll||0)+" articles</div>":"")+dtl+'</div>'; }
function hmHTML(d){ d=d||{}; const rowsL=(d.rows||d.labels||[]).map(String), colsL=(d.cols||d.labels||[]).map(String),
    mx=d.matrix||[], seq=(d.ramp==="seq"), cfmt=d.cellFmt, logc=(seq&&d.colorScale==="log");
  if(!rowsL.length||!colsL.length||mx.length!==rowsL.length) return '<div class="cap">no matrix</div>';
  const fin=mx.flatMap(r=>(r||[]).filter(v=>v!=null&&isFinite(+v)).map(Number));   // isFinite: drop NaN AND inf from the colour domain (twin of the server math.isfinite)
  const vlo=fin.length?Math.min(...fin):0, vhi=fin.length?Math.max(...fin):1;
  const pos=fin.filter(v=>v>0), plo=pos.length?Math.min(...pos):1, llo=Math.log(plo), lhi=Math.log(Math.max(vhi,plo));
  const col=v=>{ if(v==null||!isFinite(+v)) return ["transparent","#8a8f98"];   // null/NaN/inf -> no colour (mirrors the server _col)
    if(seq){ let t=logc?((+v<=0||lhi<=llo)?0:(Math.log(+v)-llo)/(lhi-llo)):(vhi<=vlo?0:(+v-vlo)/(vhi-vlo));
      t=Math.max(0,Math.min(1,t)); const r=246+(226-246)*t, g=247+(75-247)*t, b=249+(74-249)*t;
      return ["rgb("+(r|0)+","+(g|0)+","+(b|0)+")", t<0.6?"#1f2328":"#fff"]; }
    v=Math.max(-1,Math.min(1,+v)); let r,g,b;
    if(v>=0){ r=246+(29-246)*v; g=247+(158-247)*v; b=249+(117-249)*v; }
    else { const t=-v; r=246+(226-246)*t; g=247+(75-247)*t; b=249+(74-249)*t; }
    return ["rgb("+(r|0)+","+(g|0)+","+(b|0)+")", Math.abs(v)<0.6?"#1f2328":"#fff"]; };
  const fmt=v=>(v==null||!isFinite(+v))?"n/a":(cfmt==="int"?(Math.abs(+v)>=1000?(Math.floor(+v/100+0.5)/10).toFixed(1)+"k":String(Math.floor(+v+0.5))):(seq?(+v).toFixed(1):((v>=0?"+":"")+(+v).toFixed(1))));   // !isFinite: NaN/inf -> "n/a" (twin of the server math.isfinite guard)
  let h='<div class="hmwrap" style="overflow-x:auto"><div class="hm" style="grid-template-columns:auto repeat('+colsL.length+',minmax(0,1fr))"><div class="hml"></div>';   // FILL — twin of the server _heatmap_html
  h+=colsL.map(x=>'<div class="hml">'+esc(x)+"</div>").join("");
  for(let i2=0;i2<rowsL.length;i2++){ h+='<div class="hml" style="text-align:right">'+esc(rowsL[i2])+"</div>";
    for(let j=0;j<colsL.length;j++){ const v=(mx[i2]||[])[j]; const [bg,ink]=col(v);
      h+='<div style="background:'+bg+';color:'+ink+'">'+fmt(v)+"</div>"; } }
  return h+"</div></div>"; }
function klBar(i,p){ const fr=Object.keys((p.data||{}).frames||{});
  const fb=fr.map(f=>'<button data-fr="'+f+'" class="'+(f===p.data.frame?"on":"")+'" onclick="klFrame('+i+",'"+f+"'"+')">'+f.toUpperCase()+'</button>').join("");
  const inds=(p.indicators||["MA","MACD","RSI","BOLL","VOL"]);
  const def=(p.data.on||["MA"]);
  const ib=inds.map(n=>'<button data-ind="'+n+'" class="'+(def.indexOf(n)>=0?"on":"")+'" onclick="klInd('+i+",'"+n+"'"+')">'+n+'</button>').join("");
  return '<div class="klbar"><span class="klgrp klframes">'+fb+'</span>'
       + '<span class="klgrp klinds">'+ib
       + '<button class="klscale" onclick="klScale('+i+')" title="log / linear scale">log</button>'
       + '<button class="klgear" onclick="klParamsToggle('+i+')" title="indicator parameters">\u2699</button>'
       + (String((p.data||{}).tvUrl||"").search(/^https?:\/\//)===0 ? '<a class="kltv" href="'+esc(p.data.tvUrl).replace(/"/g,"&quot;")+'" target="_blank" rel="noopener" title="open this chart on TradingView">TradingView ↗</a>' : "")
       + '</span></div>'
       + '<div class="klparams" id="klp'+i+'" style="display:none"></div>'; }
function panelsHTML(sp){ return (sp.panels||[]).map((p,i)=>{
  let inner;
  if(p.type==="table"){ const sc=!!(p.data&&p.data.scroll);
    inner='<div class="tbl'+(sc?" scrolly":"")+'">'+tableHTML(p.data||{},sc)+'</div>'+(sc?tableLegHTML(p.data||{}):""); }
  else if(p.type==="ratingbar") inner=ratebarHTML(p.data,i);
  else if(p.type==="sentibar") inner=sentibarHTML(p.data,i);
  else if(p.type==="kline") inner=klBar(i,p)+'<div class="kline" id="k'+i+'"></div><div class="klgrip" onpointerdown="klResizeStart(event,'+i+')" title="drag to resize the chart"></div>';
  else if(p.type==="heatmap") inner=hmHTML(p.data);
  else if(p.type==="rangebar") inner=rangebarHTML(p.data,i);
  else if(p.type==="note") inner='<div class="note-panel">'+esc(p.text||"")+(p.loading?'<span class="dots"><i></i><i></i><i></i></span>':"")+'</div>';   // inline callout — no canvas (buildPanel skips it: no #p{i}); loading -> animated dots
  else if(p.type==="bars"&&p.data&&p.data.horizontal){ // v1.99 twin: row-proportional height (24px/row)
    const hh=Math.max(140,Math.min(640,24*((p.data.labels||[]).length)+64));
    inner='<div class="cw" style="height:'+hh+'px"><canvas id="p'+i+'"></canvas></div>'; }
  else inner='<div class="cw"><canvas id="p'+i+'"></canvas></div>';
  let zoom = "";
  if(p.type==="smile") zoom='<span class="zoom"><button onclick="zoomSmile('+i+',1)" title="zoom in on ATM">+</button><button onclick="zoomSmile('+i+',-1)" title="zoom out">−</button><button onclick="zoomSmile('+i+',0)" title="reset">↺</button></span>';
  else if(p.zoom) zoom='<span class="zoom"><button onclick="zoomR('+i+',1)" title="zoom in (fewer periods)">+</button><button onclick="zoomR('+i+',-1)" title="zoom out (more periods)">−</button><button onclick="zoomR('+i+',0)" title="reset">↺</button></span>';
  let vtog="";
  if(p.variants&&(p.variants.options||[]).length){ const ix=p.variants.index||0;
    vtog='<span class="vtoggle">'+p.variants.options.map((o,k)=>'<button onclick="switchVariant('+i+','+k+')"'+(k===ix?" class=on":"")+'>'+esc(o.name||"")+"</button>").join("")+"</span>"; }
  const titleH = p.info ? '<span class="htip" data-tip="'+esc(p.info).replace(/"/g,"&quot;")+'">'+esc(p.title||"")+'</span>' : esc(p.title||"");
  const full = (p.type==="table"||p.type==="note"||p.full) ? " full" : "";   // a note spans the row (twin of _panels_html)
  const wide = (p.type==="heatmap"&&!p.full) ? " wide" : "";   // content-wide -> grid stacks it to 1-col sooner (layout hasWide)
  const rowstart = p.newRow ? " rowstart" : "";
  const collapse = '<button class="collapse" onclick="collapsePanel('+i+')" onmouseenter="rowHov('+i+',1)" onmouseleave="rowHov('+i+',0)" title="collapse / expand this row">▾</button>';
  const liveHere=(SPEC.liveSection&&SPEC.active===SPEC.liveSection);   // streaming panel: dot leads the title, counter at the right edge
  const pdot=liveHere?'<span class="panlive-dot" title="live"></span> ':"";
  const pcnt=liveHere?'<span class="panlive" id="pl'+i+'"></span>':"";
  return '<div class="panel'+full+wide+rowstart+'" id="panel'+i+'"><h3>'+collapse+pcnt+zoom+vtog+pdot+titleH+ASKB+'</h3>'+inner+'</div>'; }).join(""); }
function switchVariant(i,k){ const p=SPEC.panels[i]; if(!p||!p.variants||!(p.variants.options||[])[k]) return;
  const opt=p.variants.options[k];
  p.variants.index=k; p.data=opt.data;   // swap the panel's data view (axis type / colour scale can differ)
  if(opt.title!=null){ p.title=opt.title;   // v1.132: a window variant carries its own title/info (the baseline
    const t=document.querySelector("#panel"+i+" h3 .htip");   // date differs per window) -> update the h3 caption + tip
    if(t){ t.textContent=opt.title; if(opt.info!=null){ p.info=opt.info; t.setAttribute("data-tip",opt.info); } } }
  const box=document.querySelector("#panel"+i+" .vtoggle");
  if(box) [...box.children].forEach((b,j)=>b.className=(j===k?"on":""));
  const ch=CHARTS[i]; try{ ch&&ch.destroy(); }catch(e){} CHARTS[i]=null;
  if(document.getElementById("p"+i)) buildPanel(i);   // a canvas block (line/bars): full rebuild (axis type)
  else renderPanel(i, p.data);                        // an HTML block (heatmap/table): re-render its markup
  if(window.__relayout) window.__relayout(); }
function collapsePanel(i){ const el=document.getElementById("panel"+i); if(!el) return;
  const t=el.offsetTop, all=[...document.querySelectorAll(".grid > .panel")];
  const grp=[el, ...all.filter(e=>e!==el&&Math.abs(e.offsetTop-t)<4)];   // row-mates share a top
  const doCollapse=!el.classList.contains("collapsed");
  grp.forEach(e=>e.classList.toggle("collapsed", doCollapse));
  if(!doCollapse){ Object.keys(KL).forEach(k=>{ const ke=document.getElementById("k"+k);
    if(ke&&grp.some(g=>g.contains(ke))) setTimeout(()=>{try{klFit(+k);}catch(e){}},0); }); }   // re-fit to the deterministic height (sub-panes keep their size)
  if(window.__relayout) setTimeout(window.__relayout,0); }
function tgD(id,btn){ const e=document.getElementById(id); if(!e) return; const off=e.style.display==="none";
  e.style.display=off?"":"none"; if(btn) btn.textContent=(off?"▾":"▸")+btn.textContent.slice(1); }
function tgAbt(){ const p=document.getElementById("abt"); if(p) p.classList.toggle("off"); }
function tgLead(){ const t=document.getElementById("leadtx"), b=document.getElementById("leadmore");   // expand/collapse the page description
  if(!t) return; const open=t.classList.toggle("open"); if(b) b.textContent=open?"show less":"show more";
  if(!open) initLead(); }   // re-measure on collapse — a widened window must not leave a dead "show more" (review P3)
function initLead(){ const t=document.getElementById("leadtx"), b=document.getElementById("leadmore");   // reveal the toggle ONLY when the clamped text overflows 4 lines (re-checked on load/resize — timing/width-robust)
  if(!t||!b||t.classList.contains("open")) return;   // leave an expanded block alone
  b.style.display=(t.scrollHeight>t.clientHeight+2)?"inline":"none"; }
function leadHTML(ld){ ld=ld||{}; const txt=String(ld.text||"");   // twin of the server _lead_html (live): an SSE-served
  // page boots on the loading spec and gets the real one as a snapshot — without this twin the lead never appeared (review P2)
  let site=String(ld.site||""), dom=site?site.split("://").pop().replace(/\/+$/,""):""; if(dom.indexOf("www.")===0) dom=dom.slice(4);
  const link=/^https?:\/\//.test(site)?'<a href="'+esc(site).replace(/"/g,"&quot;")+'" target="_blank" rel="noopener">'+esc(dom)+'</a>':"";
  const metaTxt=String(ld.meta||""); const sep=(link&&metaTxt)?" · ":""; const inner=link+sep+esc(metaTxt);
  if(!txt&&!inner) return "";   // no about.top fallback client-side: the ETF flat page is never SSE-served
  const meta=inner?'<div class="cap leadmeta">'+inner+'</div>':"";
  if(!txt) return '<div class="lead">'+meta+'</div>';
  return '<div class="lead"><div class="txt clamp" id="leadtx">'+esc(txt)+'</div><button class="leadmore" id="leadmore" onclick="tgLead()">show more</button>'+meta+'</div>'; }
function aboutHTML(ab){ ab=ab||{}; const txt=String(ab.text||""); if(!txt) return "";   // twin of the server _about_html (live)
  const cue=ab.cue||"";   // live pages ALWAYS carry a spec cue; NO hardcoded AV default here (it would leak the AV string onto CME/other pages — the server keeps the default for the static path)
  let site=String(ab.site||""), dom=site?site.split("://").pop().replace(/\/+$/,""):""; if(dom.indexOf("www.")===0) dom=dom.slice(4);
  const link=/^https?:/.test(site)?' · <a href="'+esc(site).replace(/"/g,"&quot;")+'" target="_blank" rel="noopener">'+esc(dom)+'</a>':"";
  const meta='<span class="cap">'+esc(String(ab.meta||""))+link+'</span>';
  return '<div class="panel abt" id="abt"><h3><button class="collapse" onclick="tgAbt()" title="expand / collapse">▾</button>About '+meta+'<span class="cap abtsrc">'+esc(cue)+'</span></h3><div class="txt" id="abtx">'+esc(txt)+'</div></div>'; }
function rowHov(i,on){ const all=[...document.querySelectorAll(".grid > .panel")];
  if(!on){ all.forEach(e=>e.classList.remove("rowhov")); return; }     // leave clears everywhere (layout may have shifted)
  const el=document.getElementById("panel"+i); if(!el) return;
  const t=el.offsetTop;
  [el, ...all.filter(e=>e!==el&&Math.abs(e.offsetTop-t)<4)].forEach(e=>e.classList.add("rowhov")); }
let _zoomN={};
function setFrame(i,fr){ const p=SPEC.panels[i]; if(!p||!p.data||!p.data.frames) return;
  p.data.frame=fr; p.zoom=(p.data.frames[fr]||{}).zoom||p.zoom; delete _zoomN[i];
  const ch=CHARTS[i], d=viewOf(p,i);
  if(ch&&ch.data){ try{ patchChart(ch,p.type,d); ch.update("none"); return; }catch(e){} }
  buildPanel(i); }
function viewOf(p,i){ let d=p.data||{};
  if(d.frames){ const f=d.frames[d.frame]||{}; const z=p.zoom||f.zoom, len=(f.labels||[]).length;
    const n=Math.max(1,Math.min(len,(_zoomN[i]==null?((z||{}).def||len):_zoomN[i]))), k=-n;
    const nf={}; ["labels","o","h","l","c"].forEach(kk=>{ nf[kk]=(f[kk]||[]).slice(k); });
    return Object.assign({},d,{frames:Object.assign({},d.frames,{[d.frame]:nf})}); }
  const z=p.zoom; if(!z) return d;
  const len=(d.labels||[]).length, n=Math.max(1,Math.min(len,_zoomN[i]==null?(z.def||len):_zoomN[i])), k=-n;
  const nd=Object.assign({},d); nd.labels=(d.labels||[]).slice(k);
  if(d.series) nd.series=(d.series||[]).map(s=>Object.assign({},s,{values:(s.values||[]).slice(k)}));
  return nd; }
function zoomR(i,dir){ const p=SPEC.panels[i]; if(!p) return;
  const d=p.data||{}, f=d.frames?(d.frames[d.frame]||{}):null;
  const z=p.zoom||(f&&f.zoom); if(!z) return;
  const len=f?(f.labels||[]).length:((d.labels||[]).length);
  const cur=_zoomN[i]==null?(z.def||len):_zoomN[i];
  _zoomN[i] = dir===0 ? (z.def||len) : Math.max(z.min||1, Math.min(len, cur + (dir>0?-(z.step||1):(z.step||1))));
  const ch=CHARTS[i], vd=viewOf(p,i);
  if(ch&&ch.data){ try{ patchChart(ch,p.type,vd); ch.update("none"); return; }catch(e){} }
  buildPanel(i); }
let _zoomHalf={};
function zoomSmile(i, dir){ const ch=CHARTS[i], d=SPEC.panels[i]&&SPEC.panels[i].data; if(!ch||!d) return;
  const n=(d.strikes||[]).length, ai=(d.strikes||[]).indexOf(d.atm), c=ai>=0?ai:Math.floor(n/2);
  if(dir===0){ _zoomHalf[i]=null; } else { let h=_zoomHalf[i]==null?Math.floor(n/2):_zoomHalf[i];
    _zoomHalf[i]=Math.max(3, Math.min(Math.floor(n/2), h + (dir>0?-4:4))); }
  const h=_zoomHalf[i];
  ch.options.scales.x.min = (h==null)? undefined : d.strikes[Math.max(0,c-h)];
  ch.options.scales.x.max = (h==null)? undefined : d.strikes[Math.min(n-1,c+h)];
  ch.update(); }
let KL={}, KLIND={};
const KL_MAIN={MA:1,EMA:1,BOLL:1,SAR:1};            // overlay on the candle pane; others get a sub-pane
const KL_PARAMS={MA:[20,50],EMA:[20,50]};           // clean default: TWO moving averages, not four
const KL_STYLE={grid:{horizontal:{color:"rgba(128,128,128,.10)"},vertical:{color:"rgba(128,128,128,.10)"}},
  candle:{bar:{upColor:"#1D9E75",downColor:"#C0392B",upBorderColor:"#1D9E75",downBorderColor:"#C0392B",
    upWickColor:"#1D9E75",downWickColor:"#C0392B"},priceMark:{high:{color:"#8a8f98"},low:{color:"#8a8f98"}}},
  xAxis:{axisLine:{color:"rgba(128,128,128,.3)"}},yAxis:{axisLine:{color:"rgba(128,128,128,.3)"}},
  separator:{color:"rgba(128,128,128,.16)",fill:false},   // pane divider: muted grey like the grip (was bright white)
  indicator:{lastValueMark:{show:false}}};
function klRows(f){ return (f&&f.ts||[]).map((t,j)=>({timestamp:t,open:f.o[j],high:f.h[j],low:f.l[j],close:f.c[j],volume:(f.v&&f.v[j])||0})); }
function klBuild(i){ const p=SPEC.panels[i], el=document.getElementById("k"+i); if(!el||typeof klinecharts==="undefined") return;
  try{ klinecharts.dispose(el); }catch(e){}                       // idempotent: a re-build must not stack instances
  const c=klinecharts.init(el); KL[i]=c; c.setStyles(KL_STYLE);
  if(p.data.pricePrec!=null){ try{ c.setPriceVolumePrecision(p.data.pricePrec, 0); }catch(e){} }  // fewer axis decimals
  const f=(p.data.frames||{})[p.data.frame]||{}; c.applyNewData(klRows(f));
  try{ c.setOffsetRightDistance(16); }catch(e){}   // resting gap to the axis (was 32; -50% per user 2026-07-04)
  if(p.data.barSpace) c.setBarSpace(p.data.barSpace);
  KLIND[i]={};
  (p.data.on||["MA"]).forEach(n=>klAdd(i,n));         // default indicators (clean: just MA)
  // delayed right-edge snap: after a zoom/scroll settles (800ms), if a gap opened past the
  // last candle, glide it back to the right edge — deliberate lag, not a fight with the user.
  // v9 semantics (probed): from/to are CLAMPED to data; the whitespace lives in realTo>len.
  // gap0 = the chart's own right-offset in slots at build time, so the resting position
  // never re-triggers the snap.
  const snap=()=>{ clearTimeout(window["klSnapT"+i]); window["klSnapT"+i]=setTimeout(()=>{
    try{ if(c.__drag) return;                       // never yank mid-gesture (fast zoom/drag overshoot)
      const vr=c.getVisibleRange&&c.getVisibleRange(); if(!vr) return;
      const len=(((p.data.frames||{})[p.data.frame]||{}).ts||[]).length; if(!len) return;
      if((vr.to||0) < len-2) return;                // last candle isn't near the right -> browsing history, leave it
      let bs=8; try{ const g=c.getBarSpace&&c.getBarSpace(); bs=(g&&g.bar)||g||8; }catch(_){}
      const rest=16/(bs||8);                        // resting whitespace in BARS (scale-aware, matches the 16px offset)
      if((vr.realTo||0)-(len-1) > rest+1.5) c.scrollToRealTime(120);
    }catch(e){} }, 400); };
  try{ c.subscribeAction("onScroll",snap); c.subscribeAction("onZoom",snap); }catch(e){}
  try{ el.addEventListener("pointerdown",()=>{c.__drag=true;});
       window.addEventListener("pointerup",()=>{ if(c.__drag){ c.__drag=false; snap(); } }); }catch(e){}
  try{ new ResizeObserver(()=>{ try{ c.resize(); }catch(_){} }).observe(el); }catch(e){}   // reflow to the container's real width (no stale right gap)
  klFit(i);
}
function klBase(i){ // auto height: short by default, +110 per OPEN sub-pane (VOL/MACD/RSI, not the MA/EMA/BOLL overlays)
  const sub=Object.keys(KLIND[i]||{}).filter(n=>!KL_MAIN[n]).length; return 220+sub*110; }   // minimal base (user 2026-07-04)
function klFit(i){ const el=document.getElementById("k"+i), c=KL[i]; if(!el||!c) return;
  el.style.height=Math.max(220, klBase(i)+(el.__hAdj||0))+"px";   // __hAdj = manual drag offset (small bounds)
  try{ c.resize(); }catch(e){}
  if(window.__relayout) setTimeout(window.__relayout,0); }
function klResizeStart(e,i){ e.preventDefault(); const el=document.getElementById("k"+i); if(!el) return;
  try{ e.target.setPointerCapture(e.pointerId); }catch(_){}          // keep the stream even if the pointer leaves the pane
  const y0=e.clientY, a0=el.__hAdj||0; document.body.style.userSelect="none";
  const mv=ev=>{ el.__hAdj=Math.max(-140, Math.min(280, a0+(ev.clientY-y0))); klFit(i); };
  const up=()=>{ document.removeEventListener("pointermove",mv); document.removeEventListener("pointerup",up);
    document.removeEventListener("pointercancel",up); document.body.style.userSelect=""; };
  document.addEventListener("pointermove",mv); document.addEventListener("pointerup",up);
  document.addEventListener("pointercancel",up); }                   // a cancelled gesture must not leave a sticky drag
function klScale(i){ const c=KL[i]; if(!c) return;
  const cur=(c.getStyles&&c.getStyles().yAxis||{}).type||"normal";
  const nx=cur==="log"?"normal":"log";
  c.setStyles({yAxis:{type:nx}});   // BOLL's negative DN band is removed, so log needs no guard
  const b=document.querySelector("#panel"+i+" .klscale"); if(b) b.classList.toggle("on", nx==="log"); }
function klAdd(i,name){ const c=KL[i]; if(!c) return;
  const spec={name:name,calcParams:KL_PARAMS[name]};
  if(name==="BOLL") spec.figures=[{key:"up",title:"UP: ",type:"line"},{key:"mid",title:"MID: ",type:"line"}];
  const sub={name:name}; if(name==="VOL") sub.calcParams=[];      // drop VOL's MA5/10/20 lines (noise)
  const pid = KL_MAIN[name] ? c.createIndicator(spec, true, {id:"candle_pane"})   // STACK: false wipes pane-mates
                            : c.createIndicator(sub, false, {height:100, minHeight:60, dragEnabled:true});
  KLIND[i][name]=pid; }
function klInd(i,name){ const c=KL[i]; if(!c) return; KLIND[i]=KLIND[i]||{};
  const btn=document.querySelector("#panel"+i+" .klinds button[data-ind='"+name+"']");
  if(KLIND[i][name]!=null){ c.removeIndicator(KLIND[i][name], name); delete KLIND[i][name]; btn&&btn.classList.remove("on"); }
  else { klAdd(i,name); btn&&btn.classList.add("on"); }
  klFit(i);                                            // sub-pane opened/closed -> re-fit the height
  const box=document.getElementById("klp"+i); if(box&&box.style.display==="flex") klParamRow(i); }
function klParams(name){ return (typeof KL_PARAMS!=="undefined"&&KL_PARAMS[name])||null; }
function klSetParam(i,name,val){ const c=KL[i]; if(!c) return;
  const arr=String(val).split(",").map(x=>parseInt(x.trim(),10)).filter(x=>!isNaN(x)&&x>0);
  if(!arr.length) return; const pid=(KLIND[i]||{})[name]; if(pid==null) return;
  try{ c.overrideIndicator({name:name,calcParams:arr}, pid); }catch(e){ console.error("param",e); } }
function klParamRow(i){ const box=document.getElementById("klp"+i); if(!box) return;
  const on=Object.keys(KLIND[i]||{});
  box.innerHTML = on.length ? on.map(n=>{
    const c=KL[i], cur=(c&&c.getIndicatorByPaneId&&(function(){try{const m=c.getIndicatorByPaneId(KLIND[i][n]);const ind=m&&(m.get?m.get(n):m[n]);return ind&&ind.calcParams?ind.calcParams.join(","):"";}catch(e){return "";}})())||"";
    return '<span class="klpi"><label>'+n+'</label><input value="'+cur+'" size="8" onchange="klSetParam('+i+",'"+n+"',this.value)\" placeholder=\"periods\"></span>";
  }).join("") : '<span class="lbl">no active indicators</span>'; }
function klParamsToggle(i){ const box=document.getElementById("klp"+i); if(!box) return;
  const show=box.style.display==="none"||!box.style.display; box.style.display=show?"flex":"none"; if(show) klParamRow(i);
  const b=document.querySelector("#panel"+i+" .klgear"); if(b) b.classList.toggle("on", show); }   // fill the gear while its row is open
function klFrame(i,fr){ const p=SPEC.panels[i]; if(!p||!(p.data.frames||{})[fr]) return; p.data.frame=fr;
  const c=KL[i]; if(c){ c.applyNewData(klRows(p.data.frames[fr])); }
  document.querySelectorAll("#panel"+i+" .klframes button").forEach(b=>b.classList.toggle("on", b.dataset.fr===fr)); }
function buildPanel(i){ const p=SPEC.panels[i];
  if(p.type==="kline"){ klBuild(i); return; }
  const cv=document.getElementById("p"+i); if(!cv) return;
  const fn=RENDERERS[p.type];
  if(fn){ try{ CHARTS[i]=fn(cv, viewOf(p,i)); const u=p.units||(p.data||{}).units; if(u) CHARTS[i].$units=u; }catch(e){ console.error("block",p.type,e); } }
  else { cv.parentElement.innerHTML='<div style="color:#8a8f98;font-size:12px;padding:8px">no renderer for block: '+esc(p.type)+'</div>'; } }
function buildAll(){ CHARTS.forEach(c=>{ try{ c&&c.destroy(); }catch(e){} }); CHARTS=[];
  Object.keys(KL).forEach(k=>{ try{ klinecharts.dispose(document.getElementById("k"+k)); }catch(e){} }); KL={}; KLIND={};
  (SPEC.panels||[]).forEach((p,i)=>{ CHARTS[i]=null; buildPanel(i); });
  if(window.__relayout) window.__relayout(); }
function renderHead(){ const el=document.getElementById("hdr"); if(!el) return; const h=SPEC.head;
  const lA=s=>esc(s).replace(/"/g,"&quot;");
  const swp=(SPEC.sibling&&SPEC.sibling.spec&&SPEC.sibling.label)?'<button class="hdrlink" onclick="swapBoard()">'+esc(SPEC.sibling.label)+"</button>":"";   // v1.127 embedded-board swap (server _links_html twin)
  const lnks=swp+(SPEC.links||[]).filter(l=>l&&l.href&&l.text&&l.href.split("/")[0].indexOf(":")<0&&l.href.slice(0,2)!=="//").map(l=>'<a class="hdrlink'+(l.pending?" pending":(l.askOnly?" askonly":""))+'" href="'+lA(l.href)+'">'+esc(l.text)+"</a>").join("");   // relative-only, the server _links_html twin (review P3; v1.125: no //host refs)
  const lnk=lnks?'<span class="hdrlinks">'+lnks+"</span>":"";
  if(!h){ el.className="hdr"; el.innerHTML=esc(SPEC.title||"")+lnk; return; }   // rebuild the flex header from SPEC.head
  el.className="hdr hdr-flex";
  const pills=(h.tags||[]).filter(Boolean).map(x=>'<span class="hdrtag">'+esc(x)+"</span>").join("");
  const tags=pills?'<span class="hdrtags">'+pills+"</span>":"";
  if(h.leftType||h.leftName){   // v1.110 unified header: ticker+name / board type under / link by the price (twin of _head_html)
    const nm=h.leftName?' <span class="hdr-name">'+esc(h.leftName)+"</span>":"";
    const tp=h.leftType?'<div class="hdr-type">'+esc(h.leftType)+"</div>":"";
    const rs=h.rightSmall?'<div class="hdr-small">'+esc(h.rightSmall)+"</div>":"";
    el.innerHTML='<div class="hdr-left col"><div class="hdr-idline"><span class="hdr-big">'+esc(h.leftBig||"")+"</span>"+nm+'</div>'+tp+'</div>'
      +'<div class="hdr-right"><div class="hdr-priceline">'+lnk+'<span class="hdr-big">'+esc(h.rightBig||"")+"</span></div>"+rs+"</div>";
    return; }
  el.innerHTML='<div class="hdr-left"><span class="hdr-big">'+esc(h.leftBig||"")+'</span> <span class="hdr-small">'+esc(h.leftSmall||"")+"</span>"+tags+lnk+'</div><div class="hdr-right"><div class="hdr-big">'+esc(h.rightBig||"")+'</div><div class="hdr-small">'+esc(h.rightSmall||"")+"</div></div>"; }
function setHead(){ const h=document.getElementById("hdr");
  if(h) renderHead();                                                     // BOTH branches live in renderHead (head/plain-title, links incl. — review P2-2: the old else nuked the chips)
  const s=document.getElementById("sub"); if(s&&SPEC.subtitle!=null){ const st=SPEC.subtitleSite||{}; const u=String(st.url||""); const base=esc(SPEC.subtitle);
    s.innerHTML=(u.search(/^https?:\/\//)===0&&st.text) ? '<a class="sublink" href="'+esc(u).replace(/"/g,"&quot;")+'" target="_blank" rel="noopener">'+esc(st.text)+"</a>"+(base?" · "+base:"") : base; } }
function renderShell(){ const met=document.getElementById("met"); if(met) met.innerHTML=cardsHTML(SPEC);
  setHead(); document.getElementById("grid").innerHTML=panelsHTML(SPEC); buildAll(); }
function aboutTopHTML(sp){ sp=sp||{}; const ab=sp.about||{};   // twin of the server _about_top_html
  if(!ab.top||((sp.sections||[]).length>1)) return "";          // (sectioned pages ignore about.top)
  const txt=String(ab.text||""); if(!txt) return "";
  return '<div class="panel abt abttop"><div class="txt">'+esc(txt)+'</div>'
    +(ab.cue?'<div class="cap abtsrc">'+esc(String(ab.cue))+'</div>':"")+'</div>'; }
function swapBoard(){ const sb=SPEC.sibling; if(!sb||!sb.spec) return;   // v1.127: the EMBEDDED sibling
  // board (Cowork: no file-to-file navigation exists in the viewer) — swap SPEC wholesale and
  // re-render IN PLACE. The new board gets a back-reference to the old one, so the chip toggles
  // between the two forever; buildAll() destroys/rebuilds every chart and kline.
  const prev=SPEC, ns=sb.spec;
  ns.sibling={label:(sb.backLabel||"← back"), backLabel:sb.label, spec:prev};
  SPEC=ns;
  const ob=document.querySelector(".tabbar"); if(ob&&ob.parentNode) ob.parentNode.removeChild(ob);   // the new board mounts its OWN tabs
  const ow=document.querySelector(".swbar"); if(ow&&ow.parentNode) ow.parentNode.removeChild(ow);    // review P2-1: and its own switcher
  if((SPEC.sections||[]).length>1){ setHead(); mountTabs(); showSection(SPEC.active); }
  // ^ review P2-2: showSection OWNS the warming/loading placeholders, the swbar and the about
  //   slot — renderShell alone painted a BLANK grid when the active tab was still warming.
  else { renderShell(); mountSwitcher();
    const asl=document.getElementById("aboutSlot"); if(asl) asl.innerHTML=(SPEC.about&&!SPEC.about.top?aboutHTML(SPEC.about):"");
    const sub=document.getElementById("sub"); if(sub) sub.textContent=(SPEC.subtitle!=null?String(SPEC.subtitle):""); }
  const lsl=document.getElementById("leadSlot"); if(lsl){ const nh=leadHTML(SPEC.lead)||aboutTopHTML(SPEC);   // review P2-3:
    lsl.__lead=nh; lsl.innerHTML=nh; initLead(); }   // the ETF identity tile survives the round-trip
  const fsr=document.getElementById("ftsrc"); if(fsr) fsr.textContent=(SPEC.dataSource?String(SPEC.dataSource)+" · ":"");
  try{ if(SPEC.title) document.title=SPEC.title; }catch(e){} }
function patchChart(ch, type, d){  // update data IN PLACE — a live patch repaints instantly (animation off), no rebuild
  if(type==="bubble"){ const pts=(d.points||[]);
    const nb=(ch.data.datasets||[]).filter(ds=>ds.pointStyle!=="rectRot").length;   // benchmark ds excluded
    if(pts.length!==nb) throw new Error("bubble count changed");   // let renderPanel rebuild (points are per-dataset)
    ch.data.datasets.forEach((ds,di)=>{ const p=pts[di]; if(p&&ds.data&&ds.data[0]){ ds.data[0].x=p.x; ds.data[0].y=p.y; ds.data[0].r=Math.max(5,Math.min(26,p.r||8)); } });
    return; }
  if(type==="smile"){ if(d.strikes) ch.data.labels=d.strikes;
    ch.$atm=d.atm; ch.$mp=(d.markers||{}).maxPain; ch.$interp=d.interp;   // keep live ATM / max-pain / interp in sync
    ch.data.datasets[0].data=(d.putIV||[]).slice(); Object.assign(ch.data.datasets[0], smilePts(d.interp, ORANGE));
    ch.data.datasets[1].data=(d.callIV||[]).slice(); Object.assign(ch.data.datasets[1], smilePts(d.interp, BLUE)); }
  else if(type==="combo"){ const S=d.series||[]; if(d.labels) ch.data.labels=d.labels;
    S.forEach((s,i)=>{ const dd=ch.data.datasets[i]; if(!dd) return; dd.data=(s.values||[]).slice();
      if(s.colorBySign) dd.backgroundColor=dd.data.map(v=>(v||0)>=0?"#1D9E75":RED); }); }
  else if(type==="candles"){ const f=(d.frames||{})[d.frame]||{};
    ch.data.labels=f.labels; ch.data.datasets[0].data=(f.l||[]).map((_,i)=>[f.l[i],f.h[i]]);
    ch.options.plugins.candledraw={f:f}; }
  else if(type==="doughnut"){ if(d.labels) ch.data.labels=d.labels; ch.data.datasets[0].data=(d.values||[]).slice(); }
  else if(type==="bars"||type==="line"){ const S=d.series||[];
    if(type==="bars"){ ch.$mp=(d.markers||{}).maxPain; ch.$gf=(d.markers||{}).gammaFlip; }   // keep max-pain/Γ-flip vlines live
    if(type==="bars") S.forEach((s,i)=>{ const dd=ch.data.datasets[i];
      if(dd&&s.colorBySign) dd.backgroundColor=(s.values||[]).map(v=>(v||0)>=0?"#1D9E75":RED); });
    if(S.length!==ch.data.datasets.length) throw new Error("series-count changed");  // -> renderPanel rebuilds (colors/labels intact)
    if(d.labels) ch.data.labels=d.labels;
    S.forEach(function(s,j){ ch.data.datasets[j].data=(s.values||[]).slice(); }); } }
function renderPanel(i, data){ if(!SPEC.panels[i]) return; const p=SPEC.panels[i]; p.data=data;
  if(p.type==="table"){ const el=document.querySelector("#panel"+i+" .tbl"); if(el) el.innerHTML=tableHTML(data,!!(data&&data.scroll)); return; }
  if(p.type==="ratingbar"||p.type==="sentibar"){ const el=document.getElementById("panel"+i);
    if(el) el.querySelector(".rate,.cap").outerHTML = (p.type==="ratingbar"?ratebarHTML(data,i):sentibarHTML(data,i)); return; }
  if(p.type==="rangebar"){ const el=document.getElementById("panel"+i);
    if(el){ const g=el.querySelector(".rbar,.cap"); if(g) g.outerHTML=rangebarHTML(data,i); } return; }
  if(p.type==="heatmap"){ const el=document.getElementById("panel"+i);
    if(el){ const g=el.querySelector(".hmwrap,.cap"); if(g) g.outerHTML=hmHTML(data); } return; }   // .hmwrap (not .hm): hmHTML returns the FULL wrapper, so replacing the inner .hm would NEST a new .hmwrap on every [linear|log] toggle
  const ch=CHARTS[i], vd=p.zoom?viewOf(p,i):data;
  if(ch && ch.data && RENDERERS[p.type]){ try{ patchChart(ch, p.type, vd); ch.update("none"); return; }catch(e){} }  // silent patch: no redraw animation
  try{ ch&&ch.destroy(); }catch(e){} buildPanel(i);          // fallback: rebuild if no chart / patch failed
  if(window.__relayout) window.__relayout(); }
function setCard(i, v){ const el=document.getElementById("cv"+i); if(el) el.textContent=v; }
const BMODE={tick:["● LIVE","live-tick"],delayed:["● DELAYED","live-delayed"],settlement:["● SETTLEMENT","live-settle"]};
function renderBadge(){ const b=BADGE||{}, m=BMODE[b.mode]||["● "+(b.mode||"?"),"live-settle"], age=Math.round((Date.now()-lastUpdate)/1000);
  const bar=document.getElementById("livebar");
  if(bar){ if(SPEC.liveSection||SPEC.noLiveBar){ bar.style.display="none"; }   // --full: no page-wide bar — hidden from the FIRST paint (loading spec carries noLiveBar) through the sectioned snapshot (liveSection)
    else { bar.className="livebar "+m[1]; bar.textContent=m[0]+(b.detail?" · "+b.detail:"")+(b.ts?" · "+b.ts:"")+" · "+age+"s ago"; } }
  const now=Date.now();   // per-panel live indicator (--full): a green dot LEADS the title, the age counter sits at the right edge
  document.querySelectorAll(".panlive-dot").forEach(d=>{ d.className="panlive-dot "+m[1]; });   // colour the leading dot by mode
  document.querySelectorAll(".panlive").forEach(p=>{ const i=+String(p.id||"pl").slice(2); const ts=(PANEL_TS[i]!=null?PANEL_TS[i]:PANEL_BASE);
    const a=Math.round((now-ts)/1000);   // age since THIS panel last changed (unchanged panels count up from the snapshot, independently)
    p.textContent=a+"s"; p.title=m[0]+(b.detail?" · "+b.detail:"")+" · this panel updated "+a+"s ago"; }); }
function applyDelta(d){ if(d.badge) BADGE=d.badge; lastUpdate=Date.now();
  { const t=Date.now(); for(const k in (d.panels||{})) PANEL_TS[+k]=t; }   // stamp ONLY the panels this delta actually changed
  if(d.title!=null){ SPEC.title=d.title; } if(d.subtitle!=null){ SPEC.subtitle=d.subtitle; }
  if(d.head){ SPEC.head=d.head; }                  // live spot in the header (--full)
  if(d.title!=null||d.subtitle!=null||d.head) setHead();   // header re-syncs on the delta path (e.g. "as of …" subtitle, live spot)
  const _lv=SPEC.liveSection;   // a sectioned live page (--full --watch): the stream feeds ONLY that section
  if(_lv && SPEC.active!==_lv){ // viewer is on another tab -> stash into the stored section (fresh on return), no DOM
    const s=(SPEC.sections||[]).find(x=>x.id===_lv);
    if(s){ for(const k in (d.panels||{})){ if((s.panels||[])[+k]) s.panels[+k].data=d.panels[k]; }
           for(const k in (d.cards||{})){ if((s.cards||[])[+k]) s.cards[+k].value=d.cards[k]; } }
    renderBadge(); return; }
  const moved=Object.keys(d.cards||{}).length+Object.keys(d.panels||{}).length;
  for(const k in (d.cards||{})) setCard(+k, d.cards[k]);
  for(const k in (d.panels||{})) renderPanel(+k, d.panels[k]);
  renderBadge();
  if(moved){ const bar=document.getElementById("livebar");  // flash the bar so a real update is visible
    if(bar){ bar.style.filter="brightness(1.9)"; setTimeout(function(){ bar.style.filter=""; }, 60); } } }
function structSig(sp){ return JSON.stringify([(sp.cards||[]).map(c=>c.label),  // NB: no data length —
  (sp.panels||[]).map(p=>[p.type, p.title])]); }   // a settling strike count patches in place, not a rebuild
// --- expiration switcher (--term): the per-series chain data is PRE-EMBEDDED (panel.series[] +
// SPEC.cardSeries[]); the <select> swaps which series the smile/OI/GEX/table + cards show, entirely
// client-side (greeks are Python-computed once at build; no network, no recompute). Term panels
// (curve/surface/series-table) have no .series and stay fixed as the cross-expiration overview.
function mountSwitcher(){ const sw=SPEC.switcher; if(!sw||!(sw.options||[]).length) return;
  const grid=document.getElementById("grid"); if(!grid||document.getElementById("swsel")) return;
  const bar=document.createElement("div"); bar.className="swbar";
  const M=sw.marks||[];   // per-option {sym,color} — ◆ nearest quarterly · ★ most OI
  const opts=sw.options.map((o,i)=>{ const m=M[i]; const c=(m&&m.color)?' style="color:'+escA(String(m.color))+'"':"";
    const g=(m&&m.sym)?"  "+m.sym:""; return '<option value="'+i+'"'+c+'>'+esc(o+g)+"</option>"; }).join("");
  const leg=(sw.legend||[]).map(l=>'<b style="color:'+escA(String(l.color||""))+'">'+esc(l.sym)+"</b> "+esc(l.label)).join("  ·  ");   // v1.126: colors ride escA like the table-legend twins
  bar.innerHTML='<label>'+esc(sw.label||"Series")+'</label><select id="swsel">'+opts+"</select>"
    +(sw.hint?'<span class="swhint">'+esc(sw.hint)+"</span>":"")
    +(leg?'<span class="swleg">'+leg+"</span>":"");
  grid.parentNode.insertBefore(bar, grid);
  const sel=document.getElementById("swsel"); sel.value=String(sw.index||0);
  sel.addEventListener("change", function(){ switchSeries(+this.value); }); }
function switchSeries(idx){ const sw=SPEC.switcher; if(!sw) return; sw.index=idx;
  if(SPEC.liveSection && sw.expiries && sw.expiries[idx]){   // --watch: re-pin the LIVE stream to the picked series
    try{ fetch("/pin?expiry="+encodeURIComponent(sw.expiries[idx])); }catch(e){} }
  const _now=Date.now();
  (SPEC.panels||[]).forEach(function(p,i){ if(p.series && p.series[idx]!=null){
    if(p.type==="smile"&&CHARTS[i]){ _zoomHalf[i]=null;   // a zoom window centred on the OLD series' ATM is stale
      delete CHARTS[i].options.scales.x.min; delete CHARTS[i].options.scales.x.max; }
    if(p.seriesIsVariants && p.variants){   // v1.132: series[idx] IS this expiration's window options-set —
      p.variants.options = p.series[idx];   // re-point; KEEP the chosen window index (baseline date is one per
      const k=Math.min(p.variants.index||0, p.variants.options.length-1); p.variants.index=k;   // symbol, so
      renderPanel(i, p.variants.options[k].data); }   // the title/vtoggle don't change on an expiration swap — only data
    else renderPanel(i, p.series[idx]);
    PANEL_TS[i]=_now; } });   // the shown data just changed -> reset THIS panel's live counter now
  if(SPEC.cardSeries && SPEC.cardSeries[idx]){ SPEC.cards=SPEC.cardSeries[idx];
    const met=document.getElementById("met"); if(met) met.innerHTML=cardsHTML(SPEC); }
  if(sw.subtitles && sw.subtitles[idx]!=null){ const s=document.getElementById("sub"); if(s) s.textContent=sw.subtitles[idx]; }
  lastUpdate=_now; renderBadge(); }   // repaint the counters immediately (0s on the switched panels)
// --- section tabs (combined --full page): SPEC.sections[] each carry their own cards/panels/
// switcher; the tab bar swaps which section is MOUNTED, client-side (same idea as the series
// switcher above). Only the active section is built at load; a tab click builds its section on
// demand. A section with state:"warming" shows a placeholder until its data arrives (cold history).
function _findSection(id){ const S=SPEC.sections||[]; return S.find(s=>s.id===id)||S[0]; }
function activateSection(id){ const s=_findSection(id); if(!s) return null; SPEC.active=s.id;
  SPEC.panels=s.panels||[]; SPEC.switcher=s.switcher||null; SPEC.cardSeries=s.cardSeries||null;
  const si=(s.switcher&&s.switcher.index)||0;
  SPEC.cards=(s.cardSeries&&s.cardSeries[si])||s.cards||[];   // cards track the persisted switcher index (survive a tab round-trip)
  SPEC.subtitle=(s.switcher&&s.switcher.subtitles&&s.switcher.subtitles[si]!=null)?s.switcher.subtitles[si]
               :(s.subtitle!=null?s.subtitle:(SPEC._pageSub||""));   // per-tab as-of; subtitle-less tab -> the PAGE subtitle (server-twin parity, review P1)
  return s; }
function _warmHTML(s){ return '<div class="warming">'+esc((s&&s.warmingText)||"warming up… this panel fills in once the data arrives")+"</div>"; }
function _loadingHTML(s){ return '<div class="note-panel">'+esc((s&&s.loadingText)||"Loading…")+'<span class="dots"><i></i><i></i><i></i></span></div>'; }   // in-flight: text + animated three dots (background backfill streams the real data in)
function showSection(id){ const s=activateSection(id); if(!s) return;
  document.querySelectorAll(".tabbar .tab").forEach(b=>b.classList.toggle("on", b.dataset.sec===s.id));
  const asl=document.getElementById("aboutSlot"); if(asl) asl.innerHTML=(s.about?aboutHTML(s.about):"");  // About is Overview-only (§6.6); FILL it from the section — a live reload never renderShells the tab in (agent-review P1)
  const ob=document.querySelector(".swbar"); if(ob) ob.remove();          // drop the previous section's switcher
  if(s.state==="warming"||s.state==="loading"){ const met=document.getElementById("met"); if(met) met.innerHTML=cardsHTML(SPEC);
    setHead(); CHARTS.forEach(c=>{ try{ c&&c.destroy(); }catch(e){} }); CHARTS=[];
    document.getElementById("grid").innerHTML=(s.state==="loading"?_loadingHTML(s):_warmHTML(s)); }   // loading -> blinking dots; warming -> static placeholder
  else { renderShell(); mountSwitcher();
    // re-pick 1/2-col for the new tab (its wide content may need the higher threshold). ONE sync pass
    // can measure the freshly-inserted grid before the browser reflows the newly-built Chart.js canvases
    // + a wide heatmap's minmax(0,1fr) tracks — leaving that heatmap cramped until the FIRST interaction
    // forces a relayout (the linear->log toggle "expands" it). Deferred idempotent passes settle it with
    // no user action (mirrors the boot-time [0,150,500,1500] ladder); a no-op when already correct. (v1.143)
    if(window.__relayout){ window.__relayout(); [60,300].forEach(t=>setTimeout(window.__relayout,t)); } } }
function mountTabs(){ const S=SPEC.sections; if(!S||S.length<2) return;
  const a=_findSection(SPEC.active); if(a){ a.cards=SPEC.cards; a.panels=SPEC.panels; a.switcher=SPEC.switcher; a.cardSeries=SPEC.cardSeries; }
  // ^ JSON-embedding split the flattened active-section copies from sections[]; re-alias so switchSeries
  //   mutations (index/panel data) persist when you tab away and back (Phase-0 review P1).
  if(document.querySelector(".tabbar")) return;
  const bar=document.createElement("nav"); bar.className="tabbar";
  bar.innerHTML=S.map(s=>{ const lv=(SPEC.liveSection&&s.id===SPEC.liveSection)?'<span class="tablive" title="this tab streams live"></span>':"";
    return '<button class="tab'+(s.id===SPEC.active?" on":"")+'" data-sec="'+esc(s.id).replace(/"/g,"&quot;")+'">'+lv+esc(s.name||s.id)+"</button>"; }).join("");
  const anchor=document.getElementById("met")||document.getElementById("about")||document.getElementById("grid");
  if(anchor&&anchor.parentNode) anchor.parentNode.insertBefore(bar, anchor);
  bar.querySelectorAll(".tab").forEach(b=>b.addEventListener("click", function(){ showSection(this.dataset.sec); })); }
function fmtDur(ms){ if(ms<=0) return "now"; let s=Math.floor(ms/1000);
  const d=Math.floor(s/86400); s-=d*86400; const h=Math.floor(s/3600); s-=h*3600; const m=Math.floor(s/60); s-=m*60;
  if(d) return d+"d "+h+"h"; if(h) return h+"h "+m+"m"; if(m) return m+"m "+s+"s"; return s+"s"; }
// twin of opthist._SESS_ET / _SESS_STATUS — recompute the ET phase from the wall clock (Intl,
// DST-correct) so a saved (non-SSE) page keeps the STATUS word live too, never a frozen phase
// beside a still-ticking countdown (v1.109 review). No Intl tz support -> keep the server value.
function etSessionStatus(){ try{
  const p=new Intl.DateTimeFormat("en-US",{timeZone:"America/New_York",weekday:"short",hour:"2-digit",minute:"2-digit",hour12:false}).formatToParts(new Date());
  const g=t=>{ const o=p.find(x=>x.type===t); return o?o.value:""; };
  if(g("weekday")==="Sat"||g("weekday")==="Sun") return "Closed · weekend";
  let h=parseInt(g("hour"),10); if(h===24) h=0; const m=h*60+parseInt(g("minute"),10);
  if(m>=240&&m<570) return "Pre-market"; if(m>=570&&m<960) return "Open · regular";
  if(m>=960&&m<1200) return "After-hours"; return "Closed";
}catch(e){ return null; } }
function tickSessions(){ const now=Date.now(), st=etSessionStatus();   // LIVE session: tick the countdown AND keep the phase word current
  document.querySelectorAll(".sess-cd").forEach(el=>{ let sch; try{ sch=JSON.parse(el.getAttribute("data-cd")||"[]"); }catch(e){ return; }
    const nb=(sch||[]).find(b=>b.at>now); el.textContent=nb?(nb.label+" in "+fmtDur(nb.at-now)):"";
    const v=el.parentElement&&el.parentElement.querySelector(".v");
    if(el.getAttribute("data-pfs")){   // h2: the phase follows the tile's OWN schedule (never the equity ET clock);
      if(v&&nb){ const open=nb.label==="closes";   // rewrite only on a real open/closed DISAGREEMENT — the server's
        if(open&&v.textContent.indexOf("Open")!==0) v.textContent="Open";   // richer "Closed · weekend" text survives
        else if(!open&&v.textContent.indexOf("Open")===0) v.textContent="Closed"; } }
    else if(st){ if(v&&v.textContent!==st) v.textContent=st; } }); }
function pollPending(){  // probe only where sibling files can resolve; always settle eventually
  if(typeof fetch!=="function"||!(SPEC.links||[]).some(l=>l&&l.pending&&l.href)) return;
  const local=/^(localhost|127\.|\[::1\])/.test(location.hostname||"");
  const settlePending=(P,askOnly)=>{ P.forEach(l=>{ l.pending=false; if(askOnly) l.askOnly=true; }); renderHead(); };
  if(!local){ settlePending((SPEC.links||[]).filter(l=>l&&l.pending),true); return; }
  const waits=[2000,3000,5000,8000,13000,21000,30000], maxAttempts=24; let wi=0, attempt=0;
  function tick(){ const P=(SPEC.links||[]).filter(l=>l&&l.pending&&l.href);   // re-read: an SSE snapshot swaps SPEC wholesale
    if(!P.length) return;
    Promise.all(P.map(l=>fetch(l.href,{method:"HEAD"}).then(r=>!!r.ok).catch(()=>false)))
      .then(oks=>{ let hit=false; oks.forEach((ok,i)=>{ if(ok&&P[i].pending){ P[i].pending=false; delete P[i].askOnly; hit=true; } });
        if(hit) renderHead();                            // SPEC mutated first — tab swaps keep the revealed chip
        const pending=(SPEC.links||[]).filter(l=>l&&l.pending);
        if(pending.length){ if(attempt++<maxAttempts) setTimeout(tick,waits[Math.min(wi++,waits.length-1)]);
          else settlePending(pending,true); } }); }
  tick(); }
setInterval(renderBadge,1000); setInterval(tickSessions,1000);
buildAll(); renderBadge(); mountSwitcher(); mountTabs(); tickSessions(); pollPending(); initLead(); initTheme();
window.addEventListener("load", initLead); window.addEventListener("resize", initLead);   // re-measure the lead clamp after fonts/layout settle and on width changes
__SSEJS__
""" + GRID_JS + r"""
</script></body></html>""")




def _links_html(spec, live=False) -> str:
    """Header cross-link chips (v1.87): spec['links'] = [{text, href}] — RELATIVE hrefs to sibling
    dashboard files (aapl.html ⇄ aapl_options.html). Relative same-origin navigation is the one
    kind the embedded pane allows (pre-check 2026-07-13); producers emit a link only when the
    sibling file exists (dashboard.sibling_links) — never a dead chip. v1.94: an entry with
    `pending: True` (the sibling is being background-rendered right now) renders HIDDEN on JS
    pages (`live=True`) — the client HEAD-poll reveals it when the file lands — and is DROPPED
    from the no-JS static page, where nothing could ever reveal it. v1.127: an EMBEDDED sibling
    (spec['sibling']) renders a SWAP chip FIRST — a client-side board switch, no navigation."""
    out = ""
    _sb = spec.get("sibling")
    if live and isinstance(_sb, dict) and _sb.get("spec") and _sb.get("label"):
        out += f'<button class="hdrlink" onclick="swapBoard()">{esc(str(_sb["label"]))}</button>'
    for l in (spec.get("links") or []):
        href, text = str(l.get("href") or ""), str(l.get("text") or "")
        if href and (":" in href.split("/", 1)[0] or href.startswith("//")):   # RELATIVE only (review P3:
            # no javascript:/http: schemes; v1.125 also no protocol-relative //host network-path refs)
            continue
        pending = bool(l.get("pending"))
        if pending and not live:
            continue
        if href and text:
            cls = "hdrlink pending" if pending else ("hdrlink askonly" if l.get("askOnly") else "hdrlink")
            out += f'<a class="{cls}" href="{esc_attr(href)}">{esc(text)}</a>'
    return f'<span class="hdrlinks">{out}</span>' if out else ""


def spawn_bg(argv):
    """Detached background render (v1.94): fire `argv` (full command, interpreter included) with
    io -> DEVNULL and its own session, so it outlives the parent CLI. The child env carries
    MARKET_DATA_NO_SIBLING=1 — a background sibling render must never spawn siblings of its own
    (belt: the existence check already breaks the cycle; braces: this survives a race where both
    directions start before either file lands). Returns the Popen or None; NEVER raises — a
    failed spawn just means no pending chip."""
    try:
        import subprocess
        env = dict(os.environ, MARKET_DATA_NO_SIBLING="1")
        # start_new_session is a silent no-op on Windows — detach needs the creationflags twin
        # (DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP), else closing the console kills the child
        kw = {"creationflags": 0x00000208} if os.name == "nt" else {"start_new_session": True}
        return subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                stdin=subprocess.DEVNULL, env=env, **kw)
    except Exception:
        return None


def render_sync(argv):
    """Synchronous sibling render (v1.123.2, the Cowork fix): run `argv` to completion, io -> DEVNULL,
    child env carries MARKET_DATA_NO_SIBLING=1 (no grandchildren — belt against recursion). Returns True
    on exit 0. Used in Cowork, where spawn_bg's detached child AND the page's HEAD-poll are both dead —
    rendering the sibling inline makes its chip a REAL link on first paint. NEVER raises."""
    try:
        import subprocess
        env = dict(os.environ, MARKET_DATA_NO_SIBLING="1")
        # timeout: the inline render BLOCKS the parent (that's the point — a real chip on first paint), so
        # a wedged child must not hang the page forever. 180s >> a normal ~5s render; TimeoutExpired is an
        # Exception -> caught below -> False -> no chip (review P3).
        r = subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           stdin=subprocess.DEVNULL, env=env, timeout=180)
        return r.returncode == 0
    except Exception:
        return False


def offer_widget(command, label, plugin_ok, note=None, host="claude"):
    """A tiny show_widget offer-card for the deeper-analysis OFFER (v1.123.3, concern 2). The agent renders
    it via mcp__visualize__show_widget in Cowork/agent-mode — there are NO spawn_task chips there (v1.118.1),
    so a clickable card is the only one-click surface. On Claude, when the document plugin is installed
    (`plugin_ok`), the button calls the global `sendPrompt(command)`. On OpenAI, this function intentionally
    emits no button because command stdout cannot create a ChatGPT component; the companion MCP App owns that
    card. Everything is escaped (JS string via json, HTML via html.escape)."""
    import html as _html, json as _json
    # json.dumps does NOT neutralize the <script> context: "</script>" AND a "<!--<script>" with no "</"
    # (the HTML "script data double escaped" state) both break out. Escape EVERY "<"/">" to their JS
    # \uXXXX form — still the same chars INSIDE the JS string (sendPrompt gets the exact command), but the
    # HTML parser never sees a raw "<". (json's ensure_ascii already \u-escapes U+2028/U+2029.)
    cmd_js = _json.dumps(command).replace("<", "\\u003C").replace(">", "\\u003E")
    cmd_h, lbl_h = _html.escape(command), _html.escape(label)
    head = (command.split() or [""])[0]   # v1.125: namespace from the COMMAND token only — a colon
    ns = head.lstrip("/").split(":", 1)[0] if (head.startswith("/") and ":" in head) else ""
    # in the ARGS ("/earnings AAPL 10:30") must not garble the plugin name
    if plugin_ok and host != "openai":
        body = ('<button id="mdgo" class="go">%s</button>'
                '<div class="sub">one click sends it to chat · or type <code>%s</code></div>'
                '<script>var _c=document.getElementById("mdgo");if(_c)_c.onclick=function(){'
                'if(window.sendPrompt)sendPrompt(%s);};</script>' % (lbl_h, cmd_h, cmd_js))
    elif host == "openai" and plugin_ok:
        body = ('<div class="sub">The companion plugin is available. Its native MCP App, not this '
                'command output, renders the clickable follow-up card:</div>'
                '<div class="cmd"><code>%s</code></div>' % cmd_h)
    elif host == "openai":
        body = ('<div class="sub">Needs the <b>financial-deep-analysis</b> companion plugin '
                'in this task. Install or enable it, then start a '
                '<b>new ChatGPT Work or Codex task</b>:</div>'
                '<div class="cmd"><code>%s</code></div>' % cmd_h)
    else:
        plug = ("the <b>%s</b>" % _html.escape(ns)) if ns else "its"
        body = ('<div class="sub">Needs %s plugin — not in this session '
                '(cloud Cowork mounts no user plugins). Run it on <b>desktop</b>:</div>'
                '<div class="cmd"><code>%s</code></div>' % (plug, cmd_h))
    extra = ('<div class="note">%s</div>' % _html.escape(note)) if note else ""
    return (
        '<style>.mdoffer{font:14px/1.5 -apple-system,system-ui,sans-serif;max-width:520px}'
        '.mdoffer .ttl{font-weight:600;margin-bottom:4px}.mdoffer .go{font:inherit;font-weight:600;'
        'padding:8px 14px;border:0;border-radius:8px;background:#2a78d6;color:#fff;cursor:pointer}'
        '.mdoffer .sub{color:#667;margin-top:8px}.mdoffer code{background:#eef1f5;padding:2px 6px;'
        'border-radius:5px;font-size:13px}.mdoffer .cmd{margin-top:6px}.mdoffer .note{color:#889;'
        'margin-top:8px;font-size:13px}@media(prefers-color-scheme:dark){.mdoffer code{background:#2a2a2a}'
        '.mdoffer .sub,.mdoffer .note{color:#99a}}</style>'
        '<div class="mdoffer"><div class="ttl">Deeper analysis available</div>%s%s</div>' % (body, extra))


def write_atomic(path, text):
    """Write-then-rename (v1.94.2): a sibling file's EXISTENCE is a protocol signal now — the
    HEAD-poll reveals the chip the moment the path exists, and sibling_links existence-gates on
    it — so the file must never be observable truncated. A plain open(w) exists 0-byte for the
    whole render; a child killed mid-write would poison the gate FOREVER (chip onto a blank page,
    no retry — the file exists). os.replace is atomic on POSIX and same-volume Windows."""
    # v1.125: a UNIQUE tmp per writer — a fixed path+".tmp" raced two concurrent renders of the
    # SAME file (a spawn_bg background sibling + a user re-render, the concurrency this feature
    # introduced): writer A could os.replace B's half-written tmp into place, re-creating the
    # torn-file window. mkstemp in the target dir keeps the rename same-volume atomic.
    d = os.path.dirname(os.path.abspath(path)) or "."
    try:
        # best-effort sweep of SIGKILL-orphaned tmp siblings (unique names accumulate where the
        # old fixed name self-overwrote; anything >1h old is a dead writer — v1.126)
        _base = os.path.basename(path) + "."
        _now = time.time()
        for _fn in os.listdir(d):
            if _fn.startswith(_base) and _fn.endswith(".tmp") and _now - os.path.getmtime(os.path.join(d, _fn)) > 3600:
                os.unlink(os.path.join(d, _fn))
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        # mkstemp creates 0600 and os.replace carries that onto the target (review P3 2026-07-23:
        # a 0644 render silently became owner-only) — keep the existing file's mode, else the
        # conventional umask-honoring default a plain open() would have produced
        try:
            os.chmod(tmp, os.stat(path).st_mode & 0o777)
        except OSError:
            um = os.umask(0)
            os.umask(um)
            os.chmod(tmp, 0o666 & ~um)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


_CLOSING_HDR = "next — the render is NOT the answer; closing steps, in this order:"


def closing_note(path, offers=(), skill_dir=None):
    """The closing-step reminder every producer emits to STDERR at the write (v1.157.0).

    WHY a printed note and not another paragraph of doctrine: the rules were already written, in
    bold, and current — the ZW (wheat) and PA (palladium) Cowork sessions 2026-07-30 both drew a
    v1.154.0-stamped page, then stopped: no written read until the user kicked the session, and the
    deeper-analysis document named in prose with no card. Same class as CAT / GMKN / ROSN / NVTK /
    V-vs-MA / MSOS / ORCL / INTC / QCOM before them — nine recurrences against instructions living
    ~400 lines away in SKILL.md and the reference, read at session start and out of view by the time
    the render lands. This note trades distance for proximity: it arrives IN the tool result, at the
    moment the file exists, next to the decision about what to say.

    The futures path needed it most, and mechanically rather than as a discipline problem: the
    documented `--full ... > root_full.log 2>&1` merges BOTH streams into the log, so a SUCCESSFUL
    run handed the agent an empty tool result — no numbers, no confirmation, not even the filename.
    Presenting the file was the only visible work left. (That golden path drops the `2>&1` in the
    same slice, which is what lets this note be seen at all.)

    STDERR by design: stdout carries the producer's own text report and IS redirected to a log on
    the futures path — a note there would land in the very file it exists to pull the agent out of.

    `offers` = the asset-class-appropriate deeper-analysis commands, picked by the CALLER (a
    producer knows its asset class; it cannot know the user's context). Empty is meaningful, not
    lazy: plain non-thematic ETF pages, the Treasury curve and portfolio pages earn NO offer per
    SKILL.md, and a note nagging for one there would manufacture a wrong card."""
    sdir = skill_dir or os.path.dirname(os.path.abspath(__file__))
    lines = [_CLOSING_HDR,
             "  1. PRESENT %s NOW (Cowork present_files / the file card) — before the written"
             " read, before any web lookup. 'rendered' is not 'presented'." % path,
             "  2. THEN the written read: 3–6 sentences answering the question actually asked,"
             " sourced from this page's own numbers (the reference's checklist)."]
    if offers:
        cmds = " | ".join(offers)
        lines += ["  3. THEN the deeper-analysis OFFER — pick ONE by context (%s), then render the"
                  " card, never prose alone:" % cmds,
                  '       python3 "%s" "<command>" "<button label>" --skill-available <skill>'
                  % os.path.join(sdir, "offer.py"),
                  "     Pass --skill-available <skill> when the mapped document skill IS in THIS"
                  " session's skill list (cloud Cowork mounts them natively) — WITHOUT it the card"
                  " renders with no button at all, which reads as 'only a mention in the text'.",
                  "     Add --lang ru|en when the deliverable should be written in the language of"
                  " THIS conversation — the document skill has no other way to learn it, and the"
                  " default is English.",
                  "     Then ALSO close with one plain-text line naming the command (the visualize"
                  " widget can hang unrendered — the text line is the guarantee the offer is seen)."]
    return "\n".join(lines)


def _host_contract(_root=None):
    """Return the packaged host marker, or empty for Claude/local development."""
    root = _root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        with open(os.path.join(root, "market-data-host.json"), encoding="utf-8") as f:
            return str((json.load(f) or {}).get("host") or "").strip().lower()
    except (OSError, ValueError, AttributeError):
        return ""


def deliver_html(path, html, offers=(), skill_dir=None, stream=None, host=None):
    """Write a dashboard page and emit the host-appropriate closing step.

    Every producer's `--html` path goes through here, and `test_dashboard.py` asserts that at the
    SOURCE level, because the defect is a MISSING call: nothing at runtime can observe a wiring
    that was never written. That guard is the point. Twice now a NEW read path has shipped without
    the closing step travelling with it (moex_fundamentals -> the NVTK session closed with neither
    card; the futures offer rule landed in v1.128 and the ZW/PA sessions still closed card-less), so
    the reminder belongs where the file is written, not in a checklist a new producer must remember
    to copy. OpenAI packages use their native MCP offer/link contract, so the legacy Claude/Cowork
    command reminder is intentionally silent there. Atomic via write_atomic (the sibling-EXISTENCE
    protocol: a page must never be observable truncated). Returns `path` so a caller can keep its
    own `wrote …` line."""
    write_atomic(path, html)
    resolved_host = str(host).strip().lower() if host is not None else _host_contract()
    if resolved_host != "openai":
        print(closing_note(path, offers=offers, skill_dir=skill_dir),
              file=stream if stream is not None else sys.stderr)
    return path


def write_reverse_sibling(sib, own_spec, html_path):
    """v1.127: the embedded pair's SECOND half. `sib` = the {label, backLabel, fname, spec} dict
    a producer's *_links helper returned after embedding the sibling into ITS page; this
    re-renders the sibling FILE with `own_spec` embedded the other way — so EACH presented file
    is self-contained and one chip click swaps boards in place (the Cowork viewer cannot
    navigate between files at all). Links are stripped from both halves (dead there); best-effort
    — a failure leaves the sibling file as its own plain render."""
    try:
        own = dict(own_spec)
        own.pop("links", None)
        own.pop("sibling", None)
        rev = dict(sib["spec"])
        rev.pop("links", None)
        rev["sibling"] = {"label": sib.get("backLabel") or "back",
                          "backLabel": sib.get("label") or "back", "spec": own}
        p = os.path.join(os.path.dirname(os.path.abspath(html_path)), sib["fname"])
        write_atomic(p, render_interactive_html(rev))
        return p
    except Exception as e:  # noqa: BLE001 — the primary page is already written; never kill it over this
        print(f"warning: reverse sibling embed failed ({str(e)[:80]}) — the sibling file keeps its plain render",
              file=sys.stderr)
        return None


def sibling_basename(value, default):
    """Return a safe same-directory HTML sibling basename.

    Cross-dashboard navigation is intentionally relative/same-origin. Internal parent->child
    hand-offs may carry a descriptive OpenAI filename, but never a directory, URL, or non-HTML
    target. Falling back preserves the historical <sym>.html convention for direct CLI use.
    """
    raw = str(value or "")
    if (not raw or "/" in raw or "\\" in raw or ":" in raw
            or raw in (".", "..") or not raw.lower().endswith(".html")):
        return default
    return raw


def _path_identity(path, _platform=sys.platform):
    """Canonical comparison key for an output path on the current platform."""
    value = os.path.normcase(os.path.realpath(os.path.abspath(path)))
    # normcase is a no-op on macOS even though the default APFS volume is case-insensitive.
    return value.casefold() if _platform in ("darwin", "win32") else value


def _paths_same(left, right, _samefile=os.path.samefile, _platform=sys.platform):
    """Filesystem identity for existing paths; conservative case-safe fallback for new outputs."""
    try:
        return bool(_samefile(left, right))
    except OSError:
        return _path_identity(left, _platform) == _path_identity(right, _platform)


def sibling_target(out_path, value, default, collision_default):
    """Resolve a safe sibling basename and absolute path without reusing the parent output.

    A caller may give the parent a conventional sibling name (for example, render the
    fundamentals page as ``aapl_options.html``).  In that case the historical default would
    make the child overwrite the page that spawned it.  Fall back to a distinct deterministic
    name, using realpath + normcase so the same guard works on case-insensitive Windows paths.
    """
    fname = sibling_basename(value, default)
    directory = os.path.dirname(os.path.abspath(out_path))
    sibling = os.path.join(directory, fname)
    if _paths_same(sibling, out_path):
        fname = sibling_basename(None, collision_default)
        sibling = os.path.join(directory, fname)
    return fname, sibling


def sibling_links(out_path, candidates):
    """[(filename, label), ...] -> spec['links'] entries for files that actually EXIST next to
    out_path (no dead links — a 404 inside the embedded pane has no back affordance). Never
    links a file to itself."""
    out = []
    d = os.path.dirname(os.path.abspath(out_path)) if out_path else ""
    for fname, label in candidates:
        sib = os.path.join(d, fname) if d else ""
        if sib and os.path.exists(sib) and not _paths_same(sib, out_path):
            out.append({"text": label, "href": fname})
    return out


def cap_str(cap_b, cur="$"):
    """Market-cap display string from BILLIONS — the ONE rule for the v1.110 unified header's
    change/cap line, shared by BOTH boards (options and fundamentals import it, so they cannot
    drift on formatting; v1.113). Compact $X.XXT at a trillion ('$3,190B' is unreadable),
    integer $B in the mid range, $M below $1B (num(x,0) used to round a $402M small-cap to
    'cap $0B'). Thresholds gate on the ROUNDED display value (999.5B -> '$1.00T', 0.9995B ->
    '$1B'); None/0/NaN/inf/sub-$0.5M -> '' (the caller OMITS the segment — never a fake $0).
    `cur` is the currency symbol (v1.138: MOEX passes '₽'; default '$' keeps every $ caller identical)."""
    if not cap_b or cap_b < 0 or not math.isfinite(cap_b):
        return ""
    if cap_b >= 999.5:
        return f"{cur}{cap_b / 1000:,.2f}T"
    if cap_b >= 0.9995:
        return f"{cur}{cap_b:,.0f}B"
    s = f"{cap_b * 1000:,.0f}"   # gate on the STRING: format() banker-rounds the exact .5 down to "0"
    return f"{cur}{s}M" if s != "0" else ""


def _head_html(spec, live=True) -> str:
    """Header: spec.head -> a two-column flex row. LEFT stacks the ticker + its name (leftBig +
    leftName) with the board type (leftType) UNDER it, smaller-but-bold; RIGHT pins the cross-link
    chip next to the price (rightBig), with change/cap (rightSmall) below (v1.110). Back-compat: a
    head with only {leftSmall, tags} renders the small text inline + the pills, no type row. Falls
    back to the plain title line when there is no head. Twin of the JS renderHead.
    live=False (the v1.126 static render_html path): pending chips are DROPPED — nothing on a
    no-JS page could ever reveal them."""
    h = spec.get("head")
    lnk = _links_html(spec, live=live)
    if not h:
        return f'<div class="hdr" id="hdr">{esc(spec.get("title", "dashboard"))}{lnk}</div>'
    _pills = "".join(f'<span class="hdrtag">{esc(str(x))}</span>' for x in (h.get("tags") or []) if x)
    tags = f'<span class="hdrtags">{_pills}</span>' if _pills else ""
    if h.get("leftType") or h.get("leftName"):   # v1.110 unified header: ticker+name / type-under / link-by-price
        nm = f' <span class="hdr-name">{esc(h.get("leftName") or "")}</span>' if h.get("leftName") else ""
        tp = f'<div class="hdr-type">{esc(h.get("leftType", ""))}</div>' if h.get("leftType") else ""
        rs = f'<div class="hdr-small">{esc(h.get("rightSmall", ""))}</div>' if h.get("rightSmall") else ""
        return ('<div class="hdr hdr-flex" id="hdr">'
                f'<div class="hdr-left col"><div class="hdr-idline"><span class="hdr-big">{esc(h.get("leftBig", ""))}</span>{nm}</div>{tp}</div>'
                f'<div class="hdr-right"><div class="hdr-priceline">{lnk}<span class="hdr-big">{esc(h.get("rightBig", ""))}</span></div>{rs}</div></div>')
    # legacy header (portfolio / compare / single-series — the remaining leftSmall users;
    # futures moved to the stacked head in v1.115, ETF in v1.121): big + leftSmall inline + tags + links | price
    return ('<div class="hdr hdr-flex" id="hdr">'
            f'<div class="hdr-left"><span class="hdr-big">{esc(h.get("leftBig", ""))}</span> '
            f'<span class="hdr-small">{esc(h.get("leftSmall", ""))}</span>{tags}{lnk}</div>'
            f'<div class="hdr-right"><div class="hdr-big">{esc(h.get("rightBig", ""))}</div>'
            f'<div class="hdr-small">{esc(h.get("rightSmall", ""))}</div></div></div>')

def render_live_html(spec, liveness=None) -> str:
    """A spec (+ the provider's liveness badge) -> the live page. Self-contained; the initial spec is
    baked for first paint, then SSE deltas take over. Served by `serve()` at `/`."""
    spec = _with_active(spec)
    liveness = liveness or {"mode": "settlement", "ts": "", "detail": ""}
    parts = {
        "__CHARTJS__": _chartjs_inline(),
        "__HEAD__": _head_html(spec),
        "__KLINEJS__": _klinecharts_inline() if (_has_kline(spec) or spec.get("_preloadKline")) else "",
        "__TITLE__": esc(spec.get("title", "dashboard")),
        "__TSUF__": "· live",
        "__SUBTITLE__": _sub_html(spec),
        # a SECTIONED live page (--full --watch) drops the page-wide green bar — only SOME tabs are live;
        # the live status shows as a per-panel "● Ns ago" counter on the streaming section's panels instead.
        "__LIVEBAR__": "" if (spec.get("liveSection") or spec.get("noLiveBar")) else badge_html(liveness),
        "__CARDS__": _cards_html(spec.get("cards", []), live=True),
        "__ABOUTTOP__": _lead_html(spec, live=True),
        "__ABOUT__": _about_html(spec, live=True),
        "__PANELS__": _panels_html(spec.get("panels", []), live=True, livepanels=_livepanels(spec)),
        "__SPEC__": json.dumps(spec, separators=(",", ":")).replace("<", "\\u003c").replace(">", "\\u003e"),
        "__BADGE__": json.dumps(liveness, separators=(",", ":")).replace("<", "\\u003c").replace(">", "\\u003e"),
        "__SSEJS__": SSE_JS,
        "__FOOT__": _foot("live via SSE per-block deltas", source=spec.get("dataSource", ""), live=True),
    }
    return re.sub(r"__(?:CHARTJS|KLINEJS|HEAD|TITLE|TSUF|SUBTITLE|LIVEBAR|CARDS|ABOUTTOP|ABOUT|PANELS|SPEC|BADGE|SSEJS|FOOT)__",
                  lambda m: parts[m.group(0)], LIVE_TEMPLATE)


def render_interactive_html(spec) -> str:
    """The Ф7 E7 page: the SAME Chart.js page as live (renderers, zoom, tooltips, grid) with
    SSE OFF — no EventSource, no liveness bar. For file dashboards that want interactivity
    (fundamentals --html); truly self-contained."""
    spec = _with_active(spec)
    sib = spec.get("sibling")
    if isinstance(sib, dict) and isinstance(sib.get("spec"), dict):
        # v1.127: the EMBEDDED SIBLING BOARD — in the Cowork viewer file-to-file navigation is
        # impossible (the page is served from claudeusercontent.com at a signed URL; relative
        # hrefs 404 — the QCOM/MA sessions), so the sibling dashboard rides INSIDE this page
        # and the header chip swaps boards client-side (swapBoard: zero navigation, instant).
        # Flatten it exactly like the top spec; strip any nested sibling — the client builds
        # the back-reference itself, so depth is always exactly two boards.
        s2 = _with_active(sib["spec"])
        s2.pop("sibling", None)
        spec = dict(spec, sibling=dict(sib, spec=s2))
    parts = {
        "__CHARTJS__": _chartjs_inline(),
        "__HEAD__": _head_html(spec),
        "__KLINEJS__": _klinecharts_inline() if _has_kline(spec) else "",
        "__TITLE__": esc(spec.get("title", "dashboard")),
        "__TSUF__": "",
        "__SUBTITLE__": _sub_html(spec),
        "__LIVEBAR__": "",
        "__CARDS__": _cards_html(spec.get("cards", []), live=True),
        "__ABOUTTOP__": _lead_html(spec, live=True),
        "__ABOUT__": _about_html(spec, live=True),
        "__PANELS__": _panels_html(spec.get("panels", []), live=True, livepanels=_livepanels(spec)),
        "__SPEC__": json.dumps(spec, separators=(",", ":")).replace("<", "\\u003c").replace(">", "\\u003e"),
        "__BADGE__": "null",
        "__SSEJS__": "",
        "__FOOT__": _foot(source=spec.get("dataSource", ""), live=True),
    }
    return re.sub(r"__(?:CHARTJS|KLINEJS|HEAD|TITLE|TSUF|SUBTITLE|LIVEBAR|CARDS|ABOUTTOP|ABOUT|PANELS|SPEC|BADGE|SSEJS|FOOT)__",
                  lambda m: parts[m.group(0)], LIVE_TEMPLATE)


def render_bridge_html(spec, bridge, liveness=None, mapper_js="") -> str:
    """The Cowork-artifact LIVE page: the live template with the SSE slot replaced by a
    window.cowork.callMcpTool poll loop. `bridge` = {tool, args, intervalMs, mapper};
    `mapper_js` defines window[mapper](payload, SPEC) -> applyDelta-shaped delta
    ({badge, cards:{i:v}, panels:{i:data}}). Self-contained; degrades honestly outside Cowork."""
    spec = _with_active(spec)
    liveness = liveness or {"mode": "delayed", "ts": "", "detail": "waiting for the first bridge tick\u2026"}
    bjs = (BRIDGE_JS
           .replace("__MAPPERJS__", mapper_js or "")
           .replace("__BRIDGECFG__", json.dumps(bridge, separators=(",", ": "))
                    .replace("<", "\\u003c").replace(">", "\\u003e")))
    parts = {
        "__CHARTJS__": _chartjs_inline(),
        "__HEAD__": _head_html(spec),
        "__KLINEJS__": _klinecharts_inline() if _has_kline(spec) else "",
        "__TITLE__": esc(spec.get("title", "dashboard")),
        "__TSUF__": "· live",
        "__SUBTITLE__": _sub_html(spec),
        # a SECTIONED live page (--full --watch) drops the page-wide green bar — only SOME tabs are live;
        # the live status shows as a per-panel "● Ns ago" counter on the streaming section's panels instead.
        "__LIVEBAR__": "" if (spec.get("liveSection") or spec.get("noLiveBar")) else badge_html(liveness),
        "__CARDS__": _cards_html(spec.get("cards", []), live=True),
        "__ABOUTTOP__": _lead_html(spec, live=True),
        "__ABOUT__": _about_html(spec, live=True),
        "__PANELS__": _panels_html(spec.get("panels", []), live=True, livepanels=_livepanels(spec)),
        "__SPEC__": json.dumps(spec, separators=(",", ":")).replace("<", "\\u003c").replace(">", "\\u003e"),
        "__BADGE__": json.dumps(liveness, separators=(",", ":")).replace("<", "\\u003c").replace(">", "\\u003e"),
        "__SSEJS__": bjs,
        "__FOOT__": _foot("live via the Cowork MCP bridge", source=spec.get("dataSource", ""), live=True),
    }
    return re.sub(r"__(?:CHARTJS|KLINEJS|HEAD|TITLE|TSUF|SUBTITLE|LIVEBAR|CARDS|ABOUTTOP|ABOUT|PANELS|SPEC|BADGE|SSEJS|FOOT)__",
                  lambda m: parts[m.group(0)], LIVE_TEMPLATE)


def _safe_liveness(provider):
    try:
        b = dict(provider.liveness() or {})
    except Exception:
        b = {}
    b.setdefault("mode", "settlement")
    b.setdefault("ts", _now())
    b.setdefault("detail", "")
    return b


def _is_loopback(host) -> bool:
    return str(host) in ("127.0.0.1", "::1", "localhost")


class Warming(RuntimeError):
    """A provider raises this from initial()/poll() to say: COLD START — the upstream is still seeding,
    this is NOT an error, keep retrying. serve() shows its message on the badge (no scary 'error:'), so
    the cold-start retry is deterministic + honest (no need to hand-warm the feed from outside).
    The `warming` attr is the CONTRACT (duck-typed by `_badge_detail`), so a producer that can't import
    dashboard can still raise its own exception with `.warming = True`."""
    warming = True


def _badge_detail(e) -> str:
    """Badge detail for a poll-loop exception: a warming signal (duck-typed via `.warming`) shows its
    message plainly — a cold start is not a failure — while anything else keeps the loud error prefix.
    Bounded + never empty, so a warming feed can't masquerade as a healthy idle one."""
    if getattr(e, "warming", False):
        return (str(e) or "warming up…")[:60]
    return f"error: {type(e).__name__}: {str(e)[:60]}"


def _loading_spec() -> dict:
    return {"title": "Loading…", "subtitle": "warming up the live feed — the panel fills in on the first update",
            # A later SSE snapshot may introduce a kline panel. Preload its vendored runtime now:
            # scripts cannot be injected reliably after the live shell has already booted.
            "_preloadKline": True, "cards": [], "panels": []}


def serve(provider, port=8787, host="127.0.0.1", open_page=False):
    """Run ONE live dashboard server (DD-4) for `provider`: a background thread polls it every
    `provider.interval`s into shared state; the SSE `/stream` endpoint pushes per-block deltas (or a
    full snapshot on a structure change) plus the liveness badge; `/` serves the live page. The seam
    is provider-shaped, so a settlement re-fetch today and a Databento tick socket later both drop in
    unchanged. LOOPBACK-ONLY (the page mints metered data with no auth). Ctrl-C stops it."""
    import http.server
    import threading
    import time
    import webbrowser

    if not _is_loopback(host):  # refuse to expose an unauthenticated, metered page on the network
        raise ValueError(f"serve() refuses a non-loopback host ({host!r}); bind 127.0.0.1 only")

    # Seed with a LOADING placeholder and bind IMMEDIATELY — the first (possibly slow, e.g. a Databento
    # Live socket handshake) fetch runs in the poll thread AFTER bind, so the panel opens instantly and
    # fills in on the first update instead of showing a connection error while it fetches.
    _load = _loading_spec()
    if getattr(provider, "no_livebar", False):   # a sectioned (--full) provider: no page-wide bar, from the FIRST paint
        _load["noLiveBar"] = True
    state = {"spec": _load, "badge": dict(_safe_liveness(provider), detail="loading…"),
             "v": 0, "clients": 0, "secupd": None, "secv": 0}
    lock = threading.Lock()
    stop = threading.Event()

    # Background section push (DD-6): a provider (e.g. a cold --full doing a vol-history backfill in its
    # own thread) can hand the server a list of updated SECTIONS mid-serve; each SSE client emits a
    # `sections` event on the version bump, so Overview/History fill in live without a reload or a poll.
    def push_sections(sections):
        with lock:
            state["secupd"] = sections
            state["secv"] += 1
    if hasattr(provider, "set_push"):
        provider.set_push(push_sections)

    def poll():
        first = True
        while not stop.is_set():
            with lock:
                watching = state["clients"] > 0
            if not watching:            # no browser connected -> PAUSE polling, so an upstream metered
                stop.wait(1.0)          # socket can idle out (closing the dashboard stops the data load)
                continue
            t0 = time.time()
            try:
                sp = provider.initial() if first else provider.poll()
                with lock:
                    if sp is not None:
                        state["spec"] = sp
                    state["badge"] = _safe_liveness(provider)
                    state["v"] += 1
                first = False   # v1.126: only after a SUCCESSFUL initial()/poll() — a stateful
                # provider whose initial() raised must RETRY initial() next cycle, never jump to poll()
            except Exception as e:  # transient — keep serving; surface it on the badge
                b = _safe_liveness(provider)
                b["detail"] = _badge_detail(e)
                with lock:
                    state["badge"] = b
                    state["v"] += 1
                if getattr(e, "fatal", False):  # entitlement/dead chain: NEVER re-poll a metered key —
                    print(f"  FATAL: {e} — polling stopped (the page keeps the error)")
                    return                      # the server stays up, the badge keeps the message
            stop.wait(max(1.0, float(getattr(provider, "interval", 10.0)) - (time.time() - t0)))

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
          try:
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                with lock:
                    body = render_live_html(state["spec"], state["badge"]).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            elif path == "/pin":   # a sectioned live page (--full --watch): re-pin the streamed series
                import urllib.parse
                exp = (urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
                       .get("expiry") or [""])[0]
                if exp and hasattr(provider, "set_pin"):
                    provider.set_pin(exp)   # the poll thread picks it up on the next tick
                self.send_response(204)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
            elif path == "/stream":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                with lock:
                    state["clients"] += 1     # a viewer connected -> polling resumes
                    # A client that connects AFTER a background push (but before the next poll refreshes
                    # state["spec"]) still needs the pushed sections: if one is pending, start one version
                    # behind so the first loop iteration re-emits it (idempotent client-side replace). Since
                    # secupd is never cleared, this catch-up also fires on every LATER reconnect whose HTML is
                    # already warm — a harmless no-op re-apply (no clean way to tell "already in HTML" apart).
                    last_secv = state["secv"] - 1 if state["secupd"] is not None else state["secv"]
                last, last_v = None, -1
                try:
                    while not stop.is_set():
                        with lock:
                            spec, badge, v = state["spec"], state["badge"], state["v"]
                            secupd, secv = state["secupd"], state["secv"]
                        wrote = False
                        if v != last_v:
                            ev = diff_spec(last, spec)
                            if ev["type"] == "snapshot":
                                self.wfile.write(_sse("snapshot", {"spec": spec, "badge": badge}).encode())
                            else:
                                de = _delta_event(ev, badge)   # carries head/title/subtitle too (the live spot)
                                if de is not None:              # None = nothing changed; the poll below still ticks
                                    self.wfile.write(_sse("delta", de).encode())
                            self.wfile.write(_sse("poll", {"badge": badge}).encode())  # resets the "Ns ago"
                            last, last_v = spec, v
                            wrote = True
                        if secv != last_secv:   # a background load finished -> stream the updated sections in (no reload)
                            # v1.125: deliberately AFTER the snapshot/delta block — the client's snapshot
                            # handler replaces SPEC wholesale, so a catch-up sections frame written before
                            # the connect-time snapshot was clobbered by it (secupd is one-shot, never
                            # re-bumped: the 'warming' tab then stuck for the whole session).
                            self.wfile.write(_sse("sections", {"sections": secupd, "badge": badge}).encode())
                            last_secv = secv
                            wrote = True
                        if not wrote:
                            self.wfile.write(b": hb\n\n")  # heartbeat: warm socket + detect disconnect
                        self.wfile.flush()
                        stop.wait(0.5)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
                finally:
                    with lock:
                        state["clients"] -= 1  # viewer gone -> if it was the last, polling pauses
            else:
                self.send_error(404)
          except (BrokenPipeError, ConnectionResetError, OSError):
            return  # client hung up mid-response (reload / navigate away) — not an error

    class _QuietServer(http.server.ThreadingHTTPServer):
        def handle_error(self, request, client_address):
            # v1.126: a browser aborting its EventSource RSTs the socket mid-read — routine
            # (raised in handle_one_request's readline, OUTSIDE do_GET's guards) and it printed
            # a full stdlib traceback into the --watch console. Real errors still print.
            et = sys.exc_info()[0]
            if et and issubclass(et, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
                return
            super().handle_error(request, client_address)

    httpd = _QuietServer((host, port), Handler)  # bind FIRST (instant loading page)…
    threading.Thread(target=poll, daemon=True).start()              # …then fetch in the background
    url = f"http://{host}:{port}"
    print(f"live dashboard on {url}  ·  poll {getattr(provider, 'interval', 10)}s · SSE per-block deltas")
    print(f"  serve it in the Preview Pane (point the dev server at port {port}) or open the URL; Ctrl-C to stop.")
    if open_page:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        stop.set()
        httpd.shutdown()


def main() -> None:
    ap = argparse.ArgumentParser(description="Render a dashboard spec (JSON) to a self-contained HTML page.")
    ap.add_argument("--spec", help="path to a spec JSON file (default: read stdin)")
    ap.add_argument("--html", help="write the HTML here (default: stdout)")
    ap.add_argument("--watch", type=int, nargs="?", const=10, default=None, metavar="SECONDS",
                    help="LIVE: serve --spec via SSE, re-reading the file every N seconds (default 10)")
    ap.add_argument("--port", type=int, default=8787, help="port for --watch (default 8787)")
    ap.add_argument("--mode", default="settlement", choices=list(LIVENESS),
                    help="liveness badge mode for --watch (default settlement)")
    ap.add_argument("--open", action="store_true", help="open the --watch page in the default browser")
    a = ap.parse_args()

    if a.watch is not None:
        if not a.spec:
            ap.error("--watch needs --spec (a JSON file to serve and poll)")
        path = a.spec
        prov = CallableProvider(lambda: json.loads(open(path, encoding="utf-8").read()),
                                interval=a.watch, mode=a.mode, detail=os.path.basename(path))
        serve(prov, port=a.port, open_page=a.open)
        return

    raw = open(a.spec, encoding="utf-8").read() if a.spec else sys.stdin.read()
    html = render_html(json.loads(raw))
    if a.html:
        with open(a.html, "w", encoding="utf-8") as fh:
            fh.write(html)
        print(f"wrote {a.html} ({len(html)} bytes)")
    else:
        sys.stdout.write(html)


if __name__ == "__main__":
    main()
