#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
treasury.py — the US TREASURY CURVE dashboard (Alpha Vantage, keyed).

Design of record: docs/us-treasury-curve-plan.md.

What this page is. Every US equity, future and option the skill covers is priced off this curve,
and the skill had no view of it. It is also the honest US analogue of the МОЕХ КБД page: the curve
today against a month and a year ago, the 10y−2y slope that names an inversion, the tenors over
time with the effective fed funds rate beside them, and the realized real rate.

What this page is NOT. Alpha Vantage publishes **no per-issue/CUSIP data at all** — there is no US
equivalent of `moex_bonds.py <SECID>`, and no amount of probing will produce one. A curve page is
possible; a bond page is not.

THE TRAP THIS MODULE EXISTS TO SURVIVE (probed live 2026-07-25). `TREASURY_YIELD` accepts any
`maturity` string and answers an unsupported one — `1year`, `6month`, `20year`, even `banana` —
with the **10-YEAR series**: HTTP 200, no error field, 16 843 rows. The only field that betrays it
is `name`. A page that trusts its own request therefore draws six copies of the 10-year and labels
them 3m…30y: a fabricated curve that looks entirely plausible. So every leg re-checks the `name` it
got back, and a mismatch DROPS the tenor and says so. (Fourth endpoint in this skill to answer a
question it was not asked, after ISS's `from`/`till`, `emitent_id` and `params.dates`.)
"""
from __future__ import annotations
import argparse
import datetime as dt
import gzip
import json
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dashboard  # noqa: E402
from chart import get_key, http_get  # noqa: E402

CACHE_ENV = "MARKET_DATA_TREASURY_CACHE"
AV = "https://www.alphavantage.co/query?"

# the SIX tenors Alpha Vantage actually publishes. There is no 1y, no 3y, no 20y — anything else
# silently returns the 10-year, so this table is also the whitelist.
TENORS = [("3month", 0.25, "3 мес.", "3-Month"),
          ("2year", 2.0, "2 года", "2-Year"),
          ("5year", 5.0, "5 лет", "5-Year"),
          ("7year", 7.0, "7 лет", "7-Year"),
          ("10year", 10.0, "10 лет", "10-Year"),
          ("30year", 30.0, "30 лет", "30-Year")]
_BY_KEY = {k: (yrs, lab, nm) for k, yrs, lab, nm in TENORS}
_BLUE, _ORANGE, _GREY, _GREEN = "#2a78d6", "#eb6834", "#8a8f98", "#35b8a0"
_HIST_YEARS = 5
_LEG_SLACK_DAYS = 10      # how stale a single tenor may be inside one historical leg


# ----------------- cache (AV's free tier is 25 calls/day; this page costs 8 cold) -----------------
def _cache_dir():
    v = os.environ.get(CACHE_ENV, "").strip()
    if v.lower() in ("0", "off", "false", "no"):
        return None
    if v:
        return os.path.abspath(os.path.expanduser(v))
    return os.path.join(os.path.expanduser("~"), ".cache", "market-data", "treasury")


def _cache_name(name, day):
    safe = "".join(c for c in str(name).upper() if c.isalnum() or c in "._-") or "_"
    return "%s.%s.json.gz" % (safe, day)


def _cache_get(name, day):
    d = _cache_dir()
    if not d:
        return None
    try:
        with gzip.open(os.path.join(d, _cache_name(name, day)), "rt", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:                                          # noqa: BLE001
        return None


def _cache_put(name, day, payload):
    d = _cache_dir()
    if not d:
        return
    tmp = None
    try:
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, _cache_name(name, day))
        # a UNIQUE temp name: two concurrent runs sharing one `<name>.tmp` interleave their gzip
        # writes and os.replace then promotes the corrupt blob into the live cache, where the
        # swallowed exception hides it (review r1 P3)
        tmp = "%s.%d.tmp" % (p, os.getpid())
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, p)
    except Exception:                                          # noqa: BLE001
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _sweep_cache(day):
    """Drop other days' files and orphaned temps. Without this every day of use left ~8 more
    compressed full histories behind, forever (review r1 P2)."""
    d = _cache_dir()
    if not d or not os.path.isdir(d):
        return
    try:
        for fn in os.listdir(d):
            if fn.endswith(".tmp") or (fn.endswith(".json.gz") and (".%s." % day) not in fn):
                try:
                    os.unlink(os.path.join(d, fn))
                except OSError:
                    pass
    except OSError:
        pass


def is_throttled(payload):
    """Alpha Vantage answers a rate-limited request with `{"Information": "…25 requests per day…"}`
    — HTTP 200 and a perfectly truthy dict. Treated as data it caches as data, and one throttled
    minute then poisons the WHOLE DAY: the page cannot recover even after the limit clears
    (review r1 P1). The other AV modules here already guard it; this one did not."""
    if not isinstance(payload, dict):          # --from-dir can hand us any well-formed JSON
        return False
    for k in ("Note", "Information", "Error Message"):
        if str(payload.get(k, "")).strip():
            return True
    return False


class FetchError(RuntimeError):
    """A pull failed for a reason the module reports rather than throws."""


def _fetch_av(function, maturity=None, interval="daily"):
    p = {"function": function, "interval": interval,
         "apikey": get_key("ALPHAVANTAGE_API_KEY", None)}
    if maturity:
        p["maturity"] = maturity
    try:
        txt = http_get(AV + urllib.parse.urlencode(p))
    except Exception as e:                                     # noqa: BLE001
        # http_get re-raises HTTPError on any 4xx and URLError/TimeoutError after its retries.
        # Unwrapped, ONE flaky call out of eight killed a run in which the other seven tenors had
        # already been fetched and cached (review r1 P1).
        raise FetchError("%s: %s" % (function, e))
    try:
        return json.loads(txt)
    except Exception:                                          # noqa: BLE001
        return None


# ----------------- parsing -----------------
def parse_series(payload, maturity=None):
    """Oldest-first [(date, percent)], holes dropped — or None if this is not the series we asked for.

    Two guards, both earned by live data:
      * `name` must match the maturity requested. AV answers ANY unsupported maturity with the
        10-year series and no error, so trusting the request means drawing the wrong line under the
        right label.
      * `value: "."` (719 of 16 843 daily rows) is a HOLE. Parsed as a float it becomes 0.0 and
        drags every average, every min and every historical leg through the floor.
    """
    if not isinstance(payload, dict) or is_throttled(payload):
        return None
    if maturity:
        want = (_BY_KEY.get(maturity) or (None, None, None))[2]
        if not want or not str(payload.get("name") or "").startswith(want):
            return None
    out = []
    for row in (payload.get("data") or []):
        v = str(row.get("value") or "").strip()
        if not v or v == ".":
            continue
        try:
            out.append((str(row.get("date")), float(v)))
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda x: x[0])
    return out


def _at_or_before(series, target):
    """The last observation on or before `target` — the trading-day walk-back, done by lookup
    rather than by guessing which days are holidays."""
    prev = None
    for d, v in series:
        if d <= target:
            prev = (d, v)
        else:
            break
    return prev


# ----------------- gather -----------------
def gather(fetch=None, today=None, years=_HIST_YEARS):
    """Eight calls cold (six tenors + fed funds + CPI), each day-cached under its own key."""
    day = (today or dt.date.today()).isoformat()
    if fetch is None:
        # the repo convention: fast-fail with a routing hint instead of eight full retry ladders
        # against a sandbox that blocks egress (review r1 P2)
        dashboard.check_no_net("treasury.py needs Alpha Vantage (alphavantage.co)")
    get = fetch or _fetch_av
    out, throttled = {}, False
    _sweep_cache(day)
    for name, fn, mat, kw in ([("TY_" + k, "TREASURY_YIELD", k, {"interval": "daily"})
                               for k, _y, _l, _n in TENORS]
                              + [("FEDFUNDS", "FEDERAL_FUNDS_RATE", None, {"interval": "daily"}),
                                 ("CPI", "CPI", None, {"interval": "monthly"})]):
        key = mat or name
        blk = _cache_get(name, day)
        if blk is None:
            if throttled:
                # once the provider has said "rate limit", the next seven calls only deepen it
                out[key] = None
                continue
            try:
                blk = get(function=fn, maturity=mat, **kw) if mat else get(function=fn, **kw)
            except Exception as e:                             # noqa: BLE001
                # ONE flaky call out of eight must not kill a run whose other seven legs already
                # landed — and an injected seam may raise anything at all, not just FetchError
                print("note: %s pull failed (%r) — that leg is absent, not guessed."
                      % (mat or fn, e), file=sys.stderr)
                blk = None
            if is_throttled(blk):
                print("note: Alpha Vantage throttled (free tier = 25 req/day · 5/min) — the "
                      "remaining series are skipped and NOTHING is cached, so a later run "
                      "recovers.", file=sys.stderr)
                throttled, blk = True, None
            elif blk:
                _cache_put(name, day, blk)
        out[key] = blk
    out["_years"] = years
    out["_throttled"] = throttled
    return out


def save_raw(raw, path):
    os.makedirs(path, exist_ok=True)
    for k, v in (raw or {}).items():
        if k.startswith("_"):
            continue
        with open(os.path.join(path, "%s.json" % k), "w", encoding="utf-8") as fh:
            json.dump(v, fh, ensure_ascii=False)
    return path


def read_from_dir(path):
    """Offline replay. A missing directory, an unreadable entry or a corrupt file degrades to
    None — never a crash (the repo's standing contract, stated on moex_bonds.read_from_dir)."""
    out = {}
    try:
        names = sorted(os.listdir(path))
    except OSError:
        return out
    for f in names:
        if not f.endswith(".json"):
            continue
        try:
            with open(os.path.join(path, f), encoding="utf-8") as fh:
                out[f[:-5]] = json.load(fh)
        except (OSError, ValueError):
            out[f[:-5]] = None
    return out


# ----------------- build -----------------
def build(raw, today=None):
    raw = raw or {}
    series, dropped = {}, []
    for key, _yrs, lab, _nm in TENORS:
        s = parse_series(raw.get(key), key)
        if s:
            series[key] = s
        else:
            # either the pull failed, or AV handed back a DIFFERENT tenor than the one requested
            blk = raw.get(key)
            # WHY it dropped is a fact about provenance, and this module's whole premise is not to
            # lie about provenance: a throttled pull, a failed pull, a wrong-shaped payload and a
            # genuine silent substitution are four different things (review r1 P2). `.get` is also
            # not safe on whatever --from-dir handed us — a well-formed list would AttributeError.
            got = (str(blk.get("name") or "").split(" Treasury")[0]
                   if isinstance(blk, dict) else "")
            # substitution is decided by whether the NAME matches, not by whether a name exists:
            # a correctly-named series that is all holes is EMPTY, not substituted
            _want = _BY_KEY[key][2]
            why = ("throttle" if is_throttled(blk) else
                   "missing" if blk is None else
                   "shape" if not isinstance(blk, dict) else
                   "empty" if got.startswith(_want) else "substituted")
            got = "" if why != "substituted" else got
            dropped.append({"tenor": key, "label": lab, "got": got or None, "why": why})
    as_of = min((s[-1][0] for s in series.values()), default=None)
    curve = {}
    for key, s in series.items():
        pt = _at_or_before(s, as_of) if as_of else None
        if pt:
            curve[key] = pt[1]
    pts = sorted(((_BY_KEY[k][0], v) for k, v in curve.items()), key=lambda x: x[0])

    # historical legs: walk back to a real session and REPORT the date we got, never the one we
    # wanted (the ISS lesson — a leg labelled «1Y назад» that is actually today is a fabrication)
    legs = {}
    if as_of:
        base = dt.date.fromisoformat(as_of)
        for lab, days in (("1M", 30), ("1Y", 365)):
            want = (base - dt.timedelta(days=days)).isoformat()
            leg, dates = {}, {}
            for key, s in series.items():
                if s[0][0] > want:            # this tenor does not reach back that far
                    continue
                pt = _at_or_before(s, want)
                # each tenor walks back INDEPENDENTLY, so a tenor whose publication paused (the
                # 30-year did, 2002-2006) could contribute a point months older than the label —
                # points of materially different dates drawn as one dated curve, the very
                # fabrication this module refuses. Bounded, and the spread is disclosed.
                if pt and (dt.date.fromisoformat(want) - dt.date.fromisoformat(pt[0])).days <= _LEG_SLACK_DAYS:
                    leg[key] = pt[1]
                    dates[key] = pt[0]
            if len(leg) >= 2 and dates:
                got_date, oldest = max(dates.values()), min(dates.values())
                legs[lab] = {"date": got_date, "curve": leg,
                             "oldest": oldest,
                             "spreadDays": (dt.date.fromisoformat(got_date)
                                            - dt.date.fromisoformat(oldest)).days,
                             "points": sorted(((_BY_KEY[k][0], v) for k, v in leg.items()),
                                              key=lambda x: x[0])}

    # the slope, and how long it has held its sign — pure arithmetic on two published series
    slope, run, inverted = None, None, None
    s10, s2 = series.get("10year"), series.get("2year")
    hist = []
    if s10 and s2:
        m2 = dict(s2)
        hist = [(d, round((v - m2[d]) * 100, 1)) for d, v in s10 if d in m2]
        # the headline verdict must come from the date the CURVE is drawn for. Reading the last row
        # of its own series instead let one lagging tenor put «+34 б.п. — нормальный наклон» above
        # a curve that was actually inverted at as_of (review r1 P2, reproduced).
        hist = [x for x in hist if not as_of or x[0] <= as_of]
        if hist:
            slope = hist[-1][1]
            inverted = slope < 0
            run = 0
            for _d, v in reversed(hist):
                if (v < 0) == inverted:
                    run += 1
                else:
                    break

    ff = parse_series(raw.get("FEDFUNDS"))
    ff_at = _at_or_before(ff, as_of) if (ff and as_of) else None
    cpi = parse_series(raw.get("CPI"))
    cpi_yoy = cpi_as_of = None
    if cpi and len(cpi) >= 13:
        # YoY from the CPI INDEX. AV also publishes an `INFLATION` series — it ends 2024-01-01
        # while CPI is current, so using it would print a two-year-old number as today's.
        cpi_as_of, last = cpi[-1]
        prev = _at_or_before(cpi[:-1], (dt.date.fromisoformat(cpi_as_of)
                                        - dt.timedelta(days=350)).isoformat())
        if prev and prev[1]:
            cpi_yoy = round((last / prev[1] - 1.0) * 100.0, 2)
    real10y = round(curve["10year"] - cpi_yoy, 2) if (cpi_yoy is not None
                                                      and "10year" in curve) else None
    yrs = int(raw.get("_years") or _HIST_YEARS)
    cut = (dt.date.fromisoformat(as_of) - dt.timedelta(days=365 * yrs)).isoformat() if as_of else ""
    return {"asOf": as_of, "curve": curve, "curvePoints": pts, "dropped": dropped, "legs": legs,
            "slope": slope, "inverted": inverted, "slopeRun": run,
            "fedFunds": (ff_at[1] if ff_at else None),
            "fedFundsAsOf": (ff_at[0] if ff_at else None),
            "cpiYoY": cpi_yoy, "cpiAsOf": cpi_as_of, "real10y": real10y, "years": yrs,
            # the plotted windows are trimmed HERE so `--json` never carries 60 years of dailies
            "_hist": {k: [x for x in s if x[0] >= cut] for k, s in series.items()},
            "_slopeHist": [x for x in hist if x[0] >= cut],
            "_ff": [x for x in (ff or []) if x[0] >= cut],
            "_cpi": cpi or []}


# ----------------- presentation -----------------
def _num(v, n=2):
    return "n/a" if v is None else ("%%.%df" % n) % v


def _cards(d):
    c = []
    for key, lab in (("10year", "10 лет"), ("2year", "2 года"), ("3month", "3 мес.")):
        if key in d["curve"]:
            c.append({"label": "UST " + lab, "value": _num(d["curve"][key]) + "%",
                      "hint": "Ставка постоянной срочности, публикуемая Казначейством США "
                              "(constant maturity). Это не доходность конкретного выпуска — "
                              "по отдельным выпускам данных у провайдера нет вовсе."})
    if d["slope"] is not None:
        c.append({"label": "Наклон 10л−2г", "value": "%+d б.п." % round(d["slope"]),
                  "sub": ("инверсия · %d сессий подряд" % d["slopeRun"]) if d["inverted"]
                         else ("нормальный наклон · %d сессий" % d["slopeRun"]),
                  "hint": "Разница десятилетней и двухлетней ставок. Отрицательная (инверсия) "
                          "исторически предшествовала рецессиям — это арифметика двух "
                          "опубликованных рядов, а не модель и не прогноз."})
    if d["fedFunds"] is not None:
        c.append({"label": "Ставка ФРС", "value": _num(d["fedFunds"]) + "%",
                  "hint": "Эффективная ставка по федеральным фондам (фактическая, не целевой "
                          "диапазон). Показывает, где стоит политика относительно кривой."})
    if d["fedFunds"] is None:
        c.append({"label": "Ставка ФРС", "value": "н/д",
                  "hint": "Ряд ставки по федеральным фондам сегодня не пришёл. Это НЕ значит, что "
                          "раздела не бывает — значит, что данных за этот прогон нет."})
    if d["real10y"] is None:
        c.append({"label": "Реальная 10л", "value": "н/д",
                  "sub": "нет ряда CPI" if d["cpiYoY"] is None else "нет 10-летнего срока",
                  "hint": "Реальная ставка не посчитана: не хватает одной из двух её частей — "
                          "номинальной десятилетней ставки или индекса CPI."})
    if d["real10y"] is not None:
        c.append({"label": "Реальная 10л", "value": _num(d["real10y"]) + "%",
                  "sub": "номинал %s%% − CPI %s%%" % (_num(d["curve"]["10year"]),
                                                      _num(d["cpiYoY"])),
                  "hint": "ФАКТИЧЕСКАЯ реальная ставка: номинальная минус уже случившаяся годовая "
                          "инфляция. Это НЕ вменённая инфляция и не breakeven — рядов TIPS "
                          "провайдер не публикует, поэтому ожидания рынка отсюда не видны."})
    return c


_WHY_RU = {
    "substituted": "провайдер вернул ДРУГОЙ ряд вместо запрошенного (молчаливая подмена срока) — "
                   "рисовать его под этой подписью было бы выдумкой",
    "throttle": "провайдер ответил отказом по лимиту запросов — данных за сегодня просто нет",
    "missing": "запрос не удался (сеть или провайдер)",
    "shape": "пришёл ответ неожиданной формы",
    "empty": "ряд пришёл пустым или целиком из пропусков",
}


def _dropped_note(d):
    """WHY a tenor is missing is itself a fact about provenance, and reporting every cause as the
    provider's silent substitution would be this module telling a specific lie about where its data
    came from — on the module whose entire premise is not to (review r1 P2)."""
    if not d["dropped"]:
        return ""
    by = {}
    for x in d["dropped"]:
        by.setdefault(x.get("why") or "missing", []).append(x["label"])
    parts = ["%s — %s" % (", ".join(v), _WHY_RU.get(k, k)) for k, v in sorted(by.items())]
    got = [x for x in d["dropped"] if x.get("why") == "substituted" and x.get("got")]
    tail = (" (вместо них пришёл ряд «%s»)"
            % ", ".join(sorted({x["got"] for x in got}))) if got else ""
    return " ВНИМАНИЕ, сроки на графике отсутствуют: " + "; ".join(parts) + tail + "."


def _curve_panel(d):
    if len(d["curvePoints"]) < 2:
        return []
    labs = [_BY_KEY[k][1] for k, _y, _l, _n in
            [(t[0], t[1], t[2], t[3]) for t in TENORS] if k in d["curve"]]
    order = [k for k, _y, _l, _n in TENORS if k in d["curve"]]
    ser = [{"name": "сегодня (%s)" % d["asOf"], "values": [d["curve"][k] for k in order],
            "color": _BLUE, "showPoints": True}]
    for lab, col in (("1M", _GREEN), ("1Y", _GREY)):
        leg = d["legs"].get(lab)
        if leg:
            _sp = leg.get("spreadDays") or 0
            ser.append({"name": "%s назад (%s%s)" % ("месяц" if lab == "1M" else "год", leg["date"],
                                                     (", часть сроков до %s" % leg["oldest"])
                                                     if _sp else ""),
                        "values": [leg["curve"].get(k) for k in order], "color": col,
                        "showPoints": True, "spanGaps": True})
    note = _dropped_note(d)
    return [{"type": "line", "title": "Кривая доходности US Treasuries",
             "units": {"left": "% годовых"},
             "info": ("Ставки постоянной срочности на %s против месяца и года назад. Точек ровно "
                      "шесть — это все сроки, которые публикует провайдер; 1 год, 3 и 20 лет он не "
                      "отдаёт, и ничего между узлами здесь не достроено. Каждая историческая нога "
                      "подписана ДАТОЙ, которую реально удалось получить: ближайшей торговой "
                      "сессией не позже целевой." % d["asOf"]) + note,
             "data": {"labels": labs, "series": ser}}]


def _slope_panel(d):
    h = d.get("_slopeHist") or []
    if len(h) < 10:
        return []
    return [{"type": "line", "title": "Наклон кривой 10л−2г",
             "units": {"left": "б.п."},
             "info": "Десятилетняя ставка минус двухлетняя. Ниже нуля — инверсия: короткие деньги "
                     "дороже длинных. Это разность двух опубликованных рядов, без модели; "
                     "исторически инверсия предшествовала рецессиям, но сама по себе она их не "
                     "предсказывает и не датирует. Окно — %d лет." % d["years"],
             "data": {"labels": [x[0] for x in h], "xYear": True,
                      "series": [{"name": "10л − 2г", "values": [x[1] for x in h],
                                  "color": _BLUE}]}}]


def _hist_panel(d):
    hist = d.get("_hist") or {}
    keys = [k for k in ("3month", "2year", "10year", "30year") if hist.get(k)]
    if not keys:
        return []
    dates = sorted({x[0] for k in keys for x in hist[k]})
    ser = []
    for k, col in zip(keys, (_GREY, _GREEN, _BLUE, _ORANGE)):
        m = dict(hist[k])
        ser.append({"name": "UST " + _BY_KEY[k][1], "values": [m.get(x) for x in dates],
                    "color": col, "spanGaps": True})
    ff = dict(d.get("_ff") or [])
    if ff:
        ser.append({"name": "Ставка ФРС", "values": [ff.get(x) for x in dates],
                    "color": "#c88ce8", "spanGaps": True, "dashed": True})
    return [{"type": "line", "title": "Ставки во времени", "units": {"left": "% годовых"},
             "info": "Сроки кривой за %d лет со ставкой ФРС рядом. Именно сопоставление с "
                     "политикой отвечает на вопрос, которого сами сроки не отвечают: рынок идёт "
                     "впереди ФРС или позади неё." % d["years"],
             "data": {"labels": dates, "xYear": True, "series": ser}}]


def _real_panel(d):
    hist = (d.get("_hist") or {}).get("10year") or []
    cpi = d.get("_cpi") or []
    if not hist or len(cpi) < 13:
        return []
    idx = dict(cpi)
    rows, dates = [], []
    for dd, v in hist:
        y = dt.date.fromisoformat(dd)
        try:
            prev_m = dt.date(y.year - 1, y.month, 1).isoformat()
        except ValueError:                                     # noqa: PERF203
            continue
        cur_m = dt.date(y.year, y.month, 1).isoformat()
        if cur_m in idx and prev_m in idx and idx[prev_m]:
            dates.append(dd)
            rows.append(round(v - (idx[cur_m] / idx[prev_m] - 1.0) * 100.0, 2))
    if len(rows) < 10:
        return []
    return [{"type": "line", "title": "Реальная ставка 10 лет", "units": {"left": "% годовых"},
             "info": "Номинальная десятилетняя ставка минус ФАКТИЧЕСКАЯ годовая инфляция по CPI. "
                     "Это не breakeven и не вменённая инфляция: рядов TIPS провайдер не публикует, "
                     "поэтому ожидания рынка отсюда не видны — только то, что уже случилось. CPI "
                     "выходит с задержкой около месяца (последний %s), кривая — вчерашняя, так что "
                     "правый край сопоставляет данные разных дат."
                     % (d.get("cpiAsOf") or "н/д"),
             "data": {"labels": dates, "xYear": True,
                      "series": [{"name": "реальная 10л", "values": rows, "color": _GREEN}]}}]


def _head_big(d):
    """The headline number, and WHICH tenor it is. Hardcoding the 10-year printed «n/a%» as the
    page's biggest number whenever that one tenor was the one the provider dropped (review r1 P2)."""
    for key, _y, lab, _n in reversed(TENORS):
        if key == "10year" and key in d["curve"]:
            return _num(d["curve"][key]) + "%", "10 лет"
    for key, _y, lab, _n in TENORS:
        if key in d["curve"]:
            return _num(d["curve"][key]) + "%", lab
    return "", "нет данных"


def to_spec(d):
    lead = ("Кривая доходности казначейских облигаций США: ставки постоянной срочности, наклон "
            "10л−2г и реальная ставка. Данные Alpha Vantage (первоисточник — Казначейство США). "
            "По ОТДЕЛЬНЫМ выпускам (CUSIP) провайдер не публикует ничего — поэтому здесь кривая "
            "рынка, а не разбор конкретной бумаги.")
    if d["dropped"]:
        lead += (" ВНИМАНИЕ: %d срок(а) исключены — провайдер молча вернул другой ряд вместо "
                 "запрошенного." % len(d["dropped"]))
    sections = [{"id": "overview", "name": "Обзор", "cards": _cards(d),
                 "panels": _curve_panel(d) + _slope_panel(d)},
                {"id": "history", "name": "История", "cards": [],
                 "panels": _hist_panel(d) + _real_panel(d)}]
    return {"head": {"leftBig": "US Treasuries", "leftName": "кривая доходности",
                     "leftType": "Ставки США",
                     "rightBig": _head_big(d)[0], "rightSmall": _head_big(d)[1]},
            "lead": {"text": lead, "meta": "as of %s" % (d["asOf"] or "н/д")},
            "title": "US Treasuries · кривая доходности",
            "dataSource": "Alpha Vantage — TREASURY_YIELD, FEDERAL_FUNDS_RATE, CPI",
            "sections": sections, "active": "overview"}


def render_html(spec):
    return dashboard.render_interactive_html(spec)


def render_text(d):
    L = ["US Treasuries — кривая доходности на %s" % (d["asOf"] or "н/д")]
    if d["curvePoints"]:
        L.append("Кривая: " + " · ".join(
            "%s %s%%" % (_BY_KEY[k][1], _num(d["curve"][k]))
            for k, _y, _l, _n in TENORS if k in d["curve"]))
    if d["slope"] is not None:
        L.append("Наклон 10л−2г: %+d б.п. — %s (%d сессий подряд)"
                 % (round(d["slope"]), "ИНВЕРСИЯ" if d["inverted"] else "нормальный наклон",
                    d["slopeRun"]))
    for lab, ru in (("1M", "месяц"), ("1Y", "год")):
        leg = d["legs"].get(lab)
        if leg and "10year" in leg["curve"] and "10year" in d["curve"]:
            L.append("10 лет против %s назад (%s): %+d б.п."
                     % (ru, leg["date"], round((d["curve"]["10year"]
                                                - leg["curve"]["10year"]) * 100)))
    if d["fedFunds"] is not None:
        L.append("Ставка ФРС (эффективная): %s%%" % _num(d["fedFunds"]))
    if d["real10y"] is not None:
        L.append("Реальная 10л: %s%% (номинал %s%% минус фактический CPI %s%% на %s) — "
                 "не breakeven: рядов TIPS у провайдера нет"
                 % (_num(d["real10y"]), _num(d["curve"]["10year"]), _num(d["cpiYoY"]),
                    d.get("cpiAsOf") or "н/д"))
    for x in d["dropped"]:
        L.append("Внимание: срок %s исключён — провайдер вернул ряд «%s» вместо запрошенного"
                 % (x["label"], x.get("got") or "неизвестный"))
    L.append("Источник: Alpha Vantage (Казначейство США), as of %s" % (d["asOf"] or "н/д"))
    return "\n".join(L)


def main(argv=None, _fetch=None):
    ap = argparse.ArgumentParser(description="US Treasury yield curve (Alpha Vantage)")
    ap.add_argument("--html", metavar="PATH", help="render the dashboard to PATH")
    ap.add_argument("--json", action="store_true", help="emit the structured record as JSON")
    ap.add_argument("--emit-spec", metavar="PATH", help="write the dashboard SPEC as JSON")
    ap.add_argument("--save-raw", metavar="DIR", help="save the raw payloads for offline replay")
    ap.add_argument("--from-dir", metavar="DIR", help="rebuild from a --save-raw directory")
    ap.add_argument("--years", type=int, default=_HIST_YEARS,
                    help="history window for the time panels (default 5)")
    a = ap.parse_args(argv)
    if not 1 <= a.years <= 60:
        # unbounded, a large value overflowed the date arithmetic with a raw OverflowError, a
        # negative one silently emptied the История tab and 0 silently meant "the default"
        ap.error("--years must be between 1 and 60 (the provider's own history starts 1962)")
    try:
        raw = read_from_dir(a.from_dir) if a.from_dir else gather(years=a.years, fetch=_fetch)
    except FetchError as e:
        print("error: could not reach Alpha Vantage (%s) — nothing is drawn rather than a guessed "
              "curve." % e, file=sys.stderr)
        return 4
    if a.from_dir:
        raw["_years"] = a.years
    rec = build(raw)
    if not rec["curve"]:
        print("error: no usable tenor came back — nothing is drawn rather than a guessed curve.",
              file=sys.stderr)
        return 3
    spec = to_spec(rec)
    if a.save_raw:
        save_raw(raw, a.save_raw)
    if a.emit_spec:
        with open(a.emit_spec, "w", encoding="utf-8") as fh:
            json.dump(spec, fh, ensure_ascii=False)
    if a.html:
        # no `offers`: the curve is macro CONTEXT, and SKILL.md's palette is company- and
        # commodity-shaped — a rates page earns no deeper document, so the note stops at
        # present + read rather than manufacturing a card that would not fit.
        dashboard.deliver_html(a.html, render_html(spec))
        print("wrote " + a.html)
    if a.json:
        print(json.dumps({k: v for k, v in rec.items() if not k.startswith("_")},
                         ensure_ascii=False))
    elif not a.html:
        print(render_text(rec))
    return 0


if __name__ == "__main__":
    sys.exit(main())
