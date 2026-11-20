#!/usr/bin/env python3
"""
speccheck.py — universal dashboard-spec invariants, shared by every producer's test suite.

Why this exists. Four review rounds on the bonds module (81 findings, 32 blocking) kept turning up
the SAME defect classes, and each was found late and expensively — by a 24-agent review or by
looking at the rendered pixels, never by the offline suite. They are not bond-specific: they are
properties of the `dashboard.py` spec that any producer can violate, and several are latent in the
core US equity/options/futures paths right now.

Each check below corresponds to a defect that actually shipped:

  UNKNOWN_TYPE     a panel type the engine has no renderer for — silently blank
  EMPTY_PANEL      a titled panel whose data has nothing to draw (v1.152: an История panel with a
                   legend and no line). Set `allowEmpty: True` on a panel that is empty BY DESIGN —
                   the option pages keep a structurally-required panel so combined_spec can zip
                   panels by index across expirations
  PHANTOM_AXIS     a series on axis "y1" in a renderer that never DECLARES a y1 scale. `line` reads
                   s.axis but declares no scale, so Chart.js invents a SECOND LEFT axis while the
                   units promise the series on the right (v1.150 bonds; latent everywhere else)
  AXIS_UNIT_GAP    units.right declared with no y1 series, or a y1 series with no units.right — one
                   of the two is lying about what the reader is looking at
  INVISIBLE_SERIES a line/combo series with 1-2 non-null values and no showPoints/spanGaps: at the
                   engine's pointRadius 0 with no segment to draw, it renders NOTHING while the
                   legend advertises it. THREE separate occurrences in one week
  FORMAT_ROT       a user-visible string carrying None/nan/inf or an unsubstituted %s/{} — the
                   v1.147 `%,.0f` class
  MIXED_CURRENCY   one card showing two different currency symbols: the v1.150 «$28.87 for ₽28.87»
                   defect, which came back in v1.152 in a different panel
  NO_ACTIVE        a sectioned spec without `active` — the engine falls back to section 0, so the
                   page silently opens on the wrong tab

Usage (in a test suite):

    import speccheck
    for name, spec in [("overview", sp), ...]:
        for code, detail in speccheck.violations(spec):
            check("spec invariant %s (%s)" % (code, name), False, detail)

`violations()` NEVER raises: a malformed spec is itself reported, not a crash.
"""
from __future__ import annotations
import json
import re

# renderers that actually DECLARE a second scale (verified against dashboard.py's chart factories)
_Y1_CAPABLE = {"combo"}
# every type the engine can render (canvas blocks + the HTML ones)
KNOWN_TYPES = {"line", "combo", "bars", "bar", "table", "kline", "doughnut", "donut", "heatmap",
               "bubble", "scatter", "smile", "candles", "greeks", "exposure", "note", "text",
               "definition", "quote", "snapshot", "statistics", "rangebar", "ratingbar",
               "sentibar", "delta", "call", "put"}
_CUR = ("₽", "$", "€", "¥", "£")
# WORD-boundary tokens, not substrings: the first run of this checker flagged «Financial Services»
# because "nan" sits inside "Fi-nan-cial". A checker that cries wolf gets switched off.
_ROT_RE = re.compile(r"(?<![A-Za-z])(None|nan|NaN|inf|Infinity|undefined)(?![A-Za-z])|%[sdf](?![\w])|\{\}|\{0\}")
# What makes a line series INVISIBLE is not how few points it has — two ADJACENT points draw a
# perfectly visible segment. It is having no SEGMENT at all: a single point, or points separated by
# nulls (12 monthly values among 279 daily ones). Counting points instead of segments produced a
# flood of false alarms on small fixtures the first time this ran.


def _panels(spec):
    out = list((spec or {}).get("panels") or [])
    for s in (spec or {}).get("sections") or []:
        out.extend(s.get("panels") or [])
    return out


def _cards(spec):
    out = list((spec or {}).get("cards") or [])
    for s in (spec or {}).get("sections") or []:
        out.extend(s.get("cards") or [])
    return out


# these carry their content in the title/info, not in `data` — a "warming" note is a legitimate
# panel with no series (the checker flagged one on its first contact with the options module)
_TEXTUAL = {"note", "text", "definition", "quote", "snapshot", "statistics"}


def _has_content(p):
    """Does this panel actually have something to draw?"""
    if p.get("type") in _TEXTUAL:
        return bool(p.get("title") or p.get("info") or p.get("data"))
    d = p.get("data")
    if d is None:
        return False
    if not isinstance(d, dict):
        return bool(d)
    t = p.get("type")
    if t == "table":
        return bool(d.get("rows"))
    if t in ("heatmap",):
        return bool(d.get("matrix") or d.get("rows"))
    if t in ("bubble", "scatter"):
        return bool(d.get("points"))
    if t in ("doughnut", "donut"):
        return bool(d.get("values"))
    if t == "kline":
        return bool(d.get("frames") or d.get("bars") or d.get("candles") or d.get("o") or d.get("c"))
    ser = d.get("series")
    if ser is not None:
        return any(any(v is not None for v in (s.get("values") or [])) for s in ser)
    # unknown shape: anything non-empty counts (never a false alarm on a bespoke block)
    return bool(d)


def _nonnull(vals):
    return sum(1 for v in (vals or []) if v is not None)


def _segments(vals):
    """Adjacent non-null pairs — the segments Chart.js can actually stroke."""
    v = list(vals or [])
    return sum(1 for a, b in zip(v, v[1:]) if a is not None and b is not None)


def violations(spec, strict_currency=True):
    """[(CODE, detail)] — empty when the spec is sane. Never raises."""
    out = []
    try:
        if not isinstance(spec, dict):
            return [("MALFORMED", "spec is %s, not a dict" % type(spec).__name__)]
        secs = spec.get("sections")
        if secs and len(secs) > 1 and not spec.get("active"):
            out.append(("NO_ACTIVE", "%d sections and no `active` — the page opens on section 0"
                        % len(secs)))
        for p in _panels(spec):
            if not isinstance(p, dict):
                out.append(("MALFORMED", "panel is %s" % type(p).__name__))
                continue
            t, title = p.get("type"), (p.get("title") or "")[:60]
            if t not in KNOWN_TYPES:
                out.append(("UNKNOWN_TYPE", "%r in panel %r" % (t, title)))
            # `allowEmpty` is the producer DECLARING that an empty panel is structural — the option
            # pages zip panels by INDEX across expirations, so a conditionally-dropped panel would
            # misalign the switcher. Declaring it keeps the intent visible instead of silently
            # exempting a whole panel type (this checker nearly talked me into re-breaking it).
            if not _has_content(p) and not p.get("allowEmpty"):
                out.append(("EMPTY_PANEL", "%r (%s) ships with nothing to draw" % (title, t)))
            d = p.get("data") if isinstance(p.get("data"), dict) else {}
            ser = [s for s in (d.get("series") or []) if isinstance(s, dict)]
            y1 = [s for s in ser if s.get("axis") == "y1"]
            if y1 and t not in _Y1_CAPABLE:
                out.append(("PHANTOM_AXIS",
                            "%r (%s): series %r sits on y1, but this renderer declares no y1 scale "
                            "— Chart.js will invent a second LEFT axis"
                            % (title, t, [s.get("name") for s in y1][:2])))
            units = p.get("units") if isinstance(p.get("units"), dict) else {}
            if units.get("right") and not y1 and t in _Y1_CAPABLE:
                out.append(("AXIS_UNIT_GAP",
                            "%r: units.right=%r but no series on y1" % (title, units["right"])))
            # a y1 series with no units.right is FINE while the axis labels itself: combo suffixes
            # the right ticks with '%' unless y1pct is False. It is a gap only when that suffix is
            # off — then the axis carries bare numbers in an unnamed unit (the ₽1000-as-"1000%" case)
            if y1 and not units.get("right") and t in _Y1_CAPABLE and d.get("y1pct") is False:
                out.append(("AXIS_UNIT_GAP",
                            "%r: a series on y1 with y1pct off and no units.right — the right axis "
                            "carries bare numbers in an unnamed unit" % title))
            if t in ("line", "combo"):
                marked = bool(d.get("showPoints")) or bool(d.get("pointMarks"))
                # The defect is a legend entry that draws NOTHING beside series that DO draw — the
                # bond's own dot on the curve, the "сейчас" marker, the monthly sovereign leg among
                # daily yields. A one-point series on a one-point chart is a minimal FIXTURE, not a
                # defect, so the rule fires only when something else in the same panel is drawn.
                drawn = any(_segments(x.get("values")) for x in ser if x.get("type") != "bar")
                for s in ser:
                    if s.get("type") == "bar":
                        continue
                    n, seg = _nonnull(s.get("values")), _segments(s.get("values"))
                    if n and not seg and drawn and not marked and not s.get("spanGaps"):
                        out.append(("INVISIBLE_SERIES",
                                    "%r: series %r has %d non-null point(s) but NO adjacent pair, "
                                    "so no segment is drawn; without showPoints/spanGaps it renders "
                                    "as nothing" % (title, s.get("name"), n)))
        for c in _cards(spec):
            if not isinstance(c, dict):
                out.append(("MALFORMED", "card is %s" % type(c).__name__))
                continue
            lab = str(c.get("label") or "")[:40]
            shown = " ".join(str(c.get(k) or "") for k in ("value", "sub"))
            _m = _ROT_RE.search(shown)
            if _m:
                out.append(("FORMAT_ROT", "card %r shows %r in %r" % (lab, _m.group(0), shown[:60])))
            if strict_currency:
                seen = {sym for sym in _CUR if sym in shown}
                if len(seen) > 1:
                    out.append(("MIXED_CURRENCY",
                                "card %r mixes %s in %r" % (lab, "/".join(sorted(seen)), shown[:60])))
    except Exception as e:                                     # noqa: BLE001 — a checker must not crash a suite
        out.append(("CHECKER_ERROR", repr(e)))
    return out


# --- the RECORD (what `--json` emits and the written read sources from) ---------------------------
# Twice in one module a raw payload leaked into the record and nobody noticed until a reviewer
# measured it: the TQCB board carried twice (2.7 MB per bond) and then 12 ZCYC slices retained whole
# (34.2 MB, of which one row per slice was ever read). Both were invisible to every test, because a
# record that is 1000x too big still has the right keys.
RECORD_BUDGET_KB = 200        # a structured record for ONE instrument; series are fine, blobs are not


def record_violations(rec, budget_kb=RECORD_BUDGET_KB, _path=""):
    """[(CODE, detail)] for a producer RECORD. Never raises."""
    out = []
    try:
        if not isinstance(rec, dict):
            return [("MALFORMED", "record is %s" % type(rec).__name__)]
        pub = {k: v for k, v in rec.items() if not str(k).startswith("_")}
        kb = len(json.dumps(pub, ensure_ascii=False, default=str)) / 1024.0
        if kb > budget_kb:
            big = sorted(((len(json.dumps(v, ensure_ascii=False, default=str)), k)
                          for k, v in pub.items()), reverse=True)[:3]
            out.append(("RECORD_BLOAT",
                        "%.0f KB > %d KB budget; heaviest: %s"
                        % (kb, budget_kb, ", ".join("%s %.0f KB" % (k, n / 1024.0) for n, k in big))))

        def _walk(v, path, depth=0):
            if depth > 4 or len(out) > 12:
                return
            if isinstance(v, dict):
                # an UNPARSED exchange payload: the shape every ISS/AV block has
                if "columns" in v and "data" in v and isinstance(v.get("data"), list):
                    out.append(("RAW_LEAK",
                                "%s is an unparsed {columns,data} payload (%d rows) — the record "
                                "should carry the PARSED values, not the wire format"
                                % (path or "<root>", len(v["data"]))))
                    return
                for k, x in v.items():
                    if not str(k).startswith("_"):
                        _walk(x, "%s.%s" % (path, k) if path else str(k), depth + 1)
            elif isinstance(v, list) and v:
                _walk(v[0], "%s[0]" % path, depth + 1)
        _walk(pub, "")
    except Exception as e:                                     # noqa: BLE001
        out.append(("CHECKER_ERROR", repr(e)))
    return out


def assert_record(rec, name, check, budget_kb=RECORD_BUDGET_KB):
    v = record_violations(rec, budget_kb)
    check("record invariants: %s" % name, not v,
          "; ".join("%s — %s" % (c, d) for c, d in v[:3]))
    return v


def assert_sane(spec, name, check, strict_currency=True):
    """Convenience for the suites: reports each violation through the suite's own `check`."""
    v = violations(spec, strict_currency=strict_currency)
    check("spec invariants: %s" % name, not v,
          "; ".join("%s — %s" % (c, d) for c, d in v[:4]))
    return v


def _cli():
    import json
    import sys
    bad = 0
    for path in sys.argv[1:]:
        with open(path, encoding="utf-8") as f:
            spec = json.load(f)
        v = violations(spec)
        print("%s: %s" % (path, "OK" if not v else "%d violation(s)" % len(v)))
        for c, d in v:
            print("   %-16s %s" % (c, d))
        bad += len(v)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(_cli())
