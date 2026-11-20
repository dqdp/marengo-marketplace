#!/usr/bin/env python3
"""
moex_fundamentals.py — a SCOPED numbers module for Russian MOEX-listed company fundamentals.

Alpha Vantage has ZERO RU coverage, so the AV `fundamentals.py` role (statements, multiples,
history) has no equivalent for a MOEX name — until now. This module pulls STRUCTURED quarterly +
annual fundamentals for a MOEX company into a clean JSON so (a) a written read can cite OUR numbers
and (b) the deeper-analysis offer (`equity-research:earnings-preview`) consumes our structured
history instead of web-reading it. Design of record: docs/moex-fundamentals-plan.md.

This is a NUMBERS module only — NO dashboard / to_spec this slice (that is the explicit next slice).
It MIRRORS fundamentals.py's shape (gather/build split, --json/--save-raw/--from-dir, a fetch seam)
but sources nothing from AV.

DATA SOURCES (probed live 2026-07-24):
  1. smart-lab.ru/q/<T>/f/q/ (+ /f/y/) — PRIMARY. Keyless server-rendered HTML: per-period Выручка/
     EBITDA/Чистая прибыль/Долг/Активы/Капитализация + P/E, EV/EBITDA, P/S, ROE, dividends, keyed by a
     stable machine-readable `field="…"` attribute on each <tr>. Values are ROUNDED TO ₽bn, space
     thousand-separated, `?`/empty = a genuine hole. Banks (SBER/VTBR) carry Чистая прибыль/ROE but NO
     EBITDA/Выручка → the parser is SECTOR-AWARE (bank shape vs the full corporate shape).
  2. Yahoo query1.finance.yahoo.com/v10/finance/quoteSummary/<T>.ME — keyless JSON CROSS-CHECK /
     fallback (cookie+crumb handshake). A good TTM snapshot, currency-tagged (GMKN reports in USD),
     but per-quarter statements are frozen ≤2021–2023 and YDEX.ME 404s — never the primary.
  3. MOEX ISS — the market wrapper (shares/cap/name via moex_options.fetch_profile; dividends via
     /securities/<T>/dividends.json). First-party price/shares/cap/div even when the scrapes disagree.

HONESTY (hard rules): smart-lab is a SECOND-HAND COMPILATION of the issuers' МСФО/РСБУ reports —
rounded to ₽bn, RELEASE-LAGGED (never same-day, never the UPCOMING quarter), an unofficial scrape.
Every source tag says so; NEVER "primary filing" / "live". ₽ (RUB) throughout; a Yahoo figure carries
its own currency tag. A hole is None, never a fabricated 0. Never hard-fails: smart-lab → Yahoo TTM →
ISS → an honest "unavailable" record (not a crash).
"""
from __future__ import annotations
import argparse
import datetime as dt
import gzip
import html as _htmllib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # Windows-safe
import dashboard  # noqa: E402 — the centralized spec->HTML renderer (the --html dashboard path)
import moex_options as mo  # noqa: E402 — reuse fetch_profile (ISS TQBR: shares/cap/name/currency) + ISS base
from chart import http_get  # noqa: E402

SMARTLAB = "https://smart-lab.ru"
ISS = mo.ISS
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
CACHE_ENV = "MARKET_DATA_MOEXFUND_CACHE"   # a dir override, or 'off'/'0' to disable the per-day cache

# smart-lab is a COMPILATION, rounded, lagged — the tag says so on every surface (never "live"/"primary").
_SL_SOURCE = ("smart-lab.ru (сводка по отчётности МСФО/РСБУ) · округлено до ₽млрд · с лагом публикации · "
              "неофициальный источник, не первичная отчётность")

# smart-lab <tr field="…"> attribute -> our record key. The `field=` attr is the STABLE machine key
# (survives a label re-wording); the Cyrillic label is what the CANARY guards (a layout change that
# drops the labels must break a test, not silently return wrong/empty data).
_SL_FIELDS = {
    "revenue": "revenue", "ebitda": "ebitda", "net_income": "netIncome",
    "net_interest_income": "netInterestIncome", "operating_income": "operatingIncome",
    "assets": "assets", "net_assets": "netAssets", "debt": "debt", "net_debt": "netDebt", "cash": "cash",
    "free_float": "freeFloat", "fcf_yield": "fcfYield", "div_payout_ratio": "divPayoutRatio",
    # v1.148 dashboard: the cost structure (where revenue goes), per-share metrics and the
    # efficiency block — all present on the page and previously DROPPED (23 fields with real data).
    "opex": "opex", "amortization": "amortization", "employment_expenses": "staffCosts",
    "interest_expenses": "interestExpenses", "net_income_ns": "netIncomeAdj",
    "book_value": "bookValue", "bv_share": "bvShare", "fcf_share": "fcfShare",
    "common_share": "sharePrice", "p_fcf": "pFcf", "capex_revenue": "capexToRevenue",
    "employees": "employees", "labour_productivity": "labourProductivity",
    "expenses_per_employee": "costPerEmployee", "cost_to_income": "costToIncome",
    # v1.148 BANK shape: a bank's page carries a COMPLETELY DIFFERENT field set (SBER: 27 populated
    # rows none of the corporate keys cover) — this is the core of banking analysis and was the real
    # reason a bank board looked thin. NOTE two smart-lab TYPOS in the attribute names themselves
    # ("intertest", "cliens") — they are the wire format, so match them verbatim.
    "bank_assets": "bankAssets", "capital": "capital", "loan_portfolio": "loanPortfolio",
    "corporate_loans": "corporateLoans", "retail_loans": "retailLoans",
    "deposits": "deposits", "corporate_deposits": "corporateDeposits",
    "retail_deposits": "retailDeposits", "commission_income": "commissionIncome",
    "net_operating_income": "netOperatingIncome", "provision_for_loan_impairment": "loanProvisions",
    "creation_of_provision_for_loan_impairment": "provisionCharge",
    "net_intertest_margin": "nim", "bank_margin": "bankMargin", "p_b": "pB",
    "core_capital_adequacy_ratio": "car1", "total_capital_adequacy_ratio": "carTotal",
    "cost_of_risk_ratio": "costOfRisk", "share_of_non_performing_loans": "npl",
    "loan_to_deposit_ratio": "ldr", "number_of_cliens": "clientsM",
    "market_cap": "cap", "ev": "ev", "number_of_shares": "sharesM", "eps": "eps",
    "ebitda_margin": "ebitdaMargin", "net_margin": "netMargin", "roe": "roe", "roa": "roa",
    "p_e": "pe", "p_s": "ps", "p_bv": "pbv", "ev_ebitda": "evEbitda", "debt_ebitda": "debtEbitda",
    "fcf": "fcf", "capex": "capex", "ocf": "ocf",
    "dividend": "dividend", "div_yield": "divYield", "dividend_payout": "dividendPayout",
    "currency": "reportCurrency", "date": "reportDate",
}
# UNITS by field family (documented in the record's `units`; the parser needs no per-field table —
# _num strips a trailing "%" universally and a money cell never carries one):
#   money  -> ₽bn, rounded (revenue/ebitda/netIncome/netInterestIncome/operatingIncome/assets/
#             netAssets/debt/netDebt/cash/cap/ev/fcf/capex/ocf/dividendPayout)
#   pct    -> the bare number, "42.3%" -> 42.3 (roe/roa/netMargin/ebitdaMargin/divYield/fcfYield/
#             divPayoutRatio/freeFloat)
#   ratios -> x (pe/ps/pbv/evEbitda/debtEbitda) · sharesM -> MILLIONS of shares
# String (non-numeric) fields — kept verbatim.
_SL_STR = {"currency", "date"}
# The Cyrillic/label anchors the canary checks are PRESENT in a live page (fragility guard).
_CANARY = {"revenue": "Выручка", "net_income": "Чистая прибыль", "market_cap": "Капитализация",
           "roe": "ROE"}


# ----------------- fetch layer (seam-injectable) -----------------
def _default_fetch(url):
    """The real HTTP getter: a browser User-Agent (smart-lab 403s a bare urllib UA), gzip on."""
    return http_get(url, headers=_UA, gzip=True)


def _num(s):
    """A smart-lab cell -> float | None. Strips &nbsp;/space thousand-separators, a trailing %, and a
    unicode minus; `?`/empty/'-' (a genuine hole) -> None (NEVER 0)."""
    if s is None:
        return None
    s = _htmllib.unescape(str(s)).replace("\xa0", " ").strip()
    s = s.replace("−", "-")                 # unicode minus -> ASCII
    if s in ("", "?", "-", "n/a", "—"):
        return None
    s = s.rstrip("%").replace(" ", "")           # drop the % (pct value kept) + thousand-separators
    try:
        return float(s)
    except ValueError:
        return None


def _cells(row_html):
    """The <td> cells of a smart-lab row, EXCLUDING the spacer columns (class chartrow = the chart-icon
    column; ltm_spc = the &nbsp; gap before the LTM column) -> a list of stripped inner-text strings.
    The <th> label cell(s) are naturally skipped (only <td> is read). The result aligns 1:1 with the
    header's period cells (both share the same spacer structure)."""
    out = []
    for m in re.finditer(r"<td([^>]*)>(.*?)</td>", row_html, re.S):
        attrs = m.group(1)
        if "chartrow" in attrs or "ltm_spc" in attrs:
            continue
        txt = re.sub(r"<[^>]+>", " ", m.group(2))
        out.append(re.sub(r"\s+", " ", txt).strip())
    return out


def parse_smartlab(html_text):
    """A smart-lab /f/q/ or /f/y/ page -> {periods:[…], ltmIndex:int|None, fields:{field: [cell,…]},
    labels:{field: cyrillic}, company:str} — or None when the financials table is absent (a junk ticker,
    or a layout the parser no longer recognizes). Purely structural; build() maps it to the record."""
    if not html_text or "simple-little-table financials" not in html_text:
        return None
    t0 = html_text.find("simple-little-table financials")
    tstart = html_text.rfind("<table", 0, t0)
    tend = html_text.find("</table>", t0)
    if tstart < 0 or tend < 0:
        return None
    tbl = html_text[tstart:tend + 8]

    # header row = the <tr> naming the company + "smart-lab.ru"; its non-spacer <td> cells are the
    # period labels (…2025Q1 2025Q2… then an editrow "LTM ?"). company name = the leading <th> text.
    periods, company = [], ""
    for m in re.finditer(r"<tr[^>]*>(.*?)</tr>", tbl, re.S):
        if "smart-lab.ru" not in m.group(1):
            continue
        thm = re.search(r"<th[^>]*>(.*?)</th>", m.group(1), re.S)
        if thm:
            company = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", thm.group(1))).replace("smart-lab.ru", "").strip()
        periods = _cells(m.group(1))
        break
    if not periods:
        return None
    # normalize the trailing LTM label ("LTM ?" / "LTM") and locate it
    ltm_index = None
    for i, p in enumerate(periods):
        if "LTM" in p.upper():
            periods[i] = "LTM"
            ltm_index = i

    fields, labels = {}, {}
    for m in re.finditer(r'<tr field="([^"]+)"[^>]*>(.*?)</tr>', tbl, re.S):
        field, body = m.group(1), m.group(2)
        if field not in _SL_FIELDS:
            continue
        lab = re.search(r"target=\"_blank\">([^<]+)</a>", body) or re.search(r"<th[^>]*>\s*([^<]+?)\s*</th>", body)
        labels[field] = _htmllib.unescape(lab.group(1).strip()) if lab else field
        fields[field] = _cells(body)
    if not fields:
        return None
    return {"periods": periods, "ltmIndex": ltm_index, "fields": fields, "labels": labels, "company": company}


# STOCK (balance-sheet) keys ONLY — the unit-jump healer is restricted to these. A balance-sheet level
# cannot collapse 1000x between two reporting periods and come back, so a cell that far below its row is
# a unit switch. FLOW rows (fcf/ocf/capex/netIncome/dividendPayout/provisionCharge…) CAN legitimately
# collapse toward zero in a bad quarter, and rescaling one of those would fabricate a ₽-trillions figure
# out of a real near-zero — the exact inversion of this module's honesty rule (review P2).
_STOCK_KEYS = {"assets", "netAssets", "bookValue", "bankAssets", "capital", "loanPortfolio",
               "corporateLoans", "retailLoans", "deposits", "corporateDeposits", "retailDeposits",
               "loanProvisions"}


def _heal_unit_jumps(quarters, ltm, sym="", which="quarterly"):
    """smart-lab sometimes switches a row's UNIT mid-row: SBER's «Активы банка, млрд руб» reads
    60 107 · 61 469 · 64 474 · 68 815 · **69.0** · **69.0** — the two newest cells are printed in
    TRILLIONS while the header still says billions, so a naive parse reports ₽69 bn for a ₽69 TRILLION
    balance sheet (a 1000x error; caught live 2026-07-24). Heal CONSERVATIVELY: only for money rows, only
    when the row genuinely lives in the thousands-of-₽bn (median >= 1000), and only for cells that sit
    >100x BELOW that median — a real business never drops 100x for one quarter and recovers. Anything
    ambiguous is left ALONE (an unhealed cell is a visible outlier; a wrongly-scaled one is invisible),
    and every heal is announced on stderr so it is never silent."""
    healed = []
    for key in _STOCK_KEYS:
        vals = [q.get(key) for q in quarters] + [ltm.get(key)]
        known = sorted(v for v in vals if v is not None)
        if len(known) < 3:
            continue
        # Reference = the row's MAXIMUM, not its median. smart-lab flips the NEWEST cells first, so once
        # half a rolling 5+LTM window is flipped the MEDIAN itself becomes a trillion value, the row
        # silently fails the `>= 1000` gate and a 1000x-understated balance sheet ships with no warning
        # (review P2). A flipped cell is never the max, so the max stays a correctly-printed ₽bn figure
        # for as long as ONE correct cell remains. (If EVERY cell is flipped there is no jump left to
        # detect — the row is then internally consistent and nothing here can tell; documented limit.)
        ref = known[-1]
        if ref < 1000:                      # rows in the ordinary ₽bn range are never trillion-printed
            continue
        for rec in list(quarters) + [ltm]:
            v = rec.get(key)
            # Heal ONLY when the ×1000 reading lands back in the row's own range (within 3x of the
            # reference): that is what a trillions-printed cell looks like. A merely SMALL value whose
            # ×1000 would overshoot the row is left ALONE — an unhealed outlier is visible, a wrongly
            # scaled one is invisible.
            if v is not None and 0 < v and (ref / 3.0) <= v * 1000.0 <= (ref * 3.0):
                rec[key] = v * 1000.0
                healed.append(key)
    if healed:
        print("note: %s %s — smart-lab printed %s in TRILLIONS while the row header says ₽bn; rescaled "
              "to ₽bn (%d cell(s))" % (sym or "?", which, "/".join(sorted(set(healed))), len(healed)),
              file=sys.stderr)
    return quarters, ltm


def _rows_from_parsed(parsed):
    """parse_smartlab output -> (quarters, ltm): quarters = [{period, <recordkey>: value,…}] oldest→newest
    (excluding the LTM column); ltm = the LTM column's dict (or {}). Money/ratio cells go through _num;
    currency/date stay strings. A cell missing for a period is simply absent from that period's dict."""
    periods, ltm_i, srcfields = parsed["periods"], parsed["ltmIndex"], parsed["fields"]
    n = len(periods)
    quarters, ltm = [], {}
    for i in range(n):
        rec = {"period": periods[i]}
        for field, cells in srcfields.items():
            if i >= len(cells):
                continue
            key = _SL_FIELDS[field]
            raw = cells[i]
            val = raw.strip() or None if field in _SL_STR else _num(raw)
            if val is not None:
                rec[key] = val
        if i == ltm_i:
            ltm = {k: v for k, v in rec.items() if k != "period"}
        else:
            quarters.append(rec)
    return quarters, ltm


# ----------------- Yahoo cross-check (keyless, fallback only) -----------------
# financialData is Yahoo's LIVE TTM snapshot (probed 2026-07-24: GAZP totalRevenue matched smart-lab's
# LTM revenue to the decimal). The per-statement modules (incomeStatementHistory*) are FROZEN ≤2021–2023
# for RU names — their net-income sums are stale garbage, so we DERIVE TTM net income from the live
# snapshot (revenue × net margin) rather than trusting the frozen statements.
_YQ_MODULES = "financialData,defaultKeyStatistics,assetProfile"


def _default_yahoo_fetch(sym):
    """Real cookie+crumb handshake -> the quoteSummary JSON text, or None on ANY failure (a 404 for a
    de-listed/reorganized name like YDEX.ME, a rate-limit, a blocked IP). Kept OFF the generic `fetch`
    seam because it needs a cookie jar across three requests; tests inject a stub instead."""
    try:
        import http.cookiejar
        import urllib.parse
        import urllib.request
        cj = http.cookiejar.CookieJar()
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
        op.addheaders = list(_UA.items())
        for seed in ("https://fc.yahoo.com", "https://finance.yahoo.com"):
            try:
                op.open(seed, timeout=10).read()
                if len(cj):
                    break
            except Exception:
                continue
        crumb = op.open("https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=10).read().decode("utf-8", "replace")
        if not crumb or "{" in crumb or " " in crumb.strip() or len(crumb) > 40:
            return None   # "Too Many Requests" / "Invalid Cookie" JSON — a real crumb is a short opaque token
        url = ("https://query1.finance.yahoo.com/v10/finance/quoteSummary/%s.ME?modules=%s&crumb=%s"
               % (sym.upper(), _YQ_MODULES, urllib.parse.quote(crumb)))
        return op.open(url, timeout=15).read().decode("utf-8", "replace")
    except Exception:
        return None


def _yraw(d, *path):
    """Walk a quoteSummary dotted path to a {'raw':…} leaf -> its .raw float, or None."""
    for k in path:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    if isinstance(d, dict):
        d = d.get("raw")
    return d if isinstance(d, (int, float)) else None


def parse_yahoo(json_text):
    """quoteSummary JSON -> a currency-TAGGED live-TTM snapshot dict, or None. Money is converted to
    ₽/$bn (Yahoo reports absolute units). Everything comes from the LIVE financialData/defaultKeyStatistics
    blocks (NOT the frozen ≤2021–2023 statement modules); TTM net income is DERIVED as revenue × net
    margin (both live), since Yahoo carries no live netIncome scalar for RU names. currency =
    financialData.financialCurrency (GMKN → 'USD' — the caller must NOT mix a USD figure into a ₽ series;
    every value here is tagged with this currency)."""
    try:
        d = json.loads(json_text) if isinstance(json_text, str) else json_text
    except (ValueError, TypeError):
        return None
    res = (((d or {}).get("quoteSummary") or {}).get("result")) or None
    if not res:
        return None
    r = res[0]
    fd, ks = r.get("financialData") or {}, r.get("defaultKeyStatistics") or {}
    cur = fd.get("financialCurrency") or "RUB"

    def _b(v):
        return round(v / 1e9, 3) if v is not None else None

    def _pc(m):
        return round(m * 100, 2) if m is not None else None
    ap = r.get("assetProfile") or {}
    rev, margin = _yraw(fd, "totalRevenue"), _yraw(fd, "profitMargins")
    ni = (rev * margin) if (rev is not None and margin is not None) else None   # derived TTM (live snapshot)
    out = {
        "currency": cur,
        "revenueB": _b(rev),
        "ebitdaB": _b(_yraw(fd, "ebitda")),
        "netIncomeB": _b(ni),
        "netMargin": _pc(margin),
        "roe": _pc(_yraw(fd, "returnOnEquity")),
        "cashB": _b(_yraw(fd, "totalCash")),
        "debtB": _b(_yraw(fd, "totalDebt")),
        "evB": _b(_yraw(ks, "enterpriseValue")),
        "shares": _yraw(ks, "sharesOutstanding"),
        "eps": _yraw(ks, "trailingEps"),
        # the business DESCRIPTION + sector/industry (assetProfile). This is what the page lead should
        # OPEN with — the AV board leads with the same thing (OVERVIEW.Description). Yahoo writes it in
        # English and may be unreachable (a blocked IP / a .ME 404), so it is best-effort: absent -> the
        # lead falls back to the ISS identity facts, never to a wall of caveats.
        "description": (ap.get("longBusinessSummary") or "").strip() or None,
        "sector": ap.get("sector") or None,
        "industry": ap.get("industry") or None,
        "website": ap.get("website") or None,
    }
    return out if any(v is not None for k, v in out.items() if k != "currency") else None


def fetch_iss_description(sym, fetch=None):
    """MOEX ISS security master -> {legalName, isin, listLevel, typeName} — first-party IDENTITY facts
    (the issuer's legal name, ISIN, listing tier, security type). ISS carries NO business description,
    so this is what the page lead falls back to when Yahoo's assetProfile is unreachable. Best-effort:
    any failure -> {}."""
    fetch = fetch or _default_fetch
    try:
        d = json.loads(fetch(ISS + "/securities/%s.json?iss.meta=off&iss.only=description" % sym.upper()))
        vals = {}
        for r in mo._rows(d.get("description", {})):   # shape access rides INSIDE the guard: a valid-JSON
            if r.get("name"):                          # non-object body must not raise out of gather()
                vals[r["name"]] = r.get("value")
    except Exception:
        return {}
    lvl = vals.get("LISTLEVEL")
    # ISS quotes the trading name INSIDE the legal name ('"Газпром" (ПАО) ао') — strip('"') only ate the
    # leading one and left a stray quote in the lead, so drop them all.
    return {"legalName": (vals.get("NAME") or "").replace('"', "").strip() or None,
            "isin": vals.get("ISIN"), "listLevel": (str(lvl) if lvl not in (None, "") else None),
            "typeName": vals.get("TYPENAME")}


# ----------------- ISS dividends (the market wrapper's first-party piece) -----------------
def fetch_dividends(sym, fetch=None):
    """MOEX ISS dividend history -> [{date, value, currency}] oldest→newest (best-effort; any failure
    -> []). fetch(url)->text is the generic seam. Clean keyless JSON: secid/isin/registryclosedate/
    value/currencyid."""
    fetch = fetch or _default_fetch
    try:
        d = json.loads(fetch(ISS + "/securities/%s/dividends.json?iss.meta=off" % sym.upper()))
    except Exception:
        return []
    out = []
    for r in mo._rows(d.get("dividends", {})):
        val, day = mo._ff(r.get("value")), r.get("registryclosedate")
        if day and val is not None:
            out.append({"date": day, "value": val, "currency": r.get("currencyid")})
    out.sort(key=lambda x: x["date"])
    return out


# ----------------- per-day disk cache (best-effort; the released-quarter data is stable, the LTM
# column moves, so key by DAY — a same-day re-run is free, a next-day run re-pulls) -----------------
def _cache_dir():
    v = os.environ.get(CACHE_ENV, "").strip()
    if v.lower() in ("0", "off", "false", "no"):
        return None
    if v:
        return os.path.abspath(os.path.expanduser(v))
    base = os.path.join(os.path.expanduser("~"), ".cache", "market-data", "moexfund")
    return base


def _cache_path(sym, kind, day):
    return os.path.join(_cache_dir(), "%s.%s.%s.html.gz" % (sym.upper(), day, kind))


def _cache_get(sym, kind, day):
    d = _cache_dir()
    if not d:
        return None
    import zlib
    p = _cache_path(sym, kind, day)
    try:
        with gzip.open(p, "rt", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None
    except (OSError, EOFError, ValueError, zlib.error):
        # a corrupt gzip BODY raises zlib.error (NOT an OSError subclass — the v1.128 opthist class),
        # which would crash the cache read (called outside a try). Best-effort: DELETE the poisoned file
        # so it can't crash-loop every same-day run, then treat it as a miss (re-fetch, ISS is truth).
        try:
            os.unlink(p)
        except OSError:
            pass
        return None


def _cache_put(sym, kind, day, text):
    d = _cache_dir()
    if not d or not text:
        return
    import tempfile
    tmp = None
    try:
        os.makedirs(d, exist_ok=True)
        # sweep this symbol's stale-day files (the released data is stable, but keep the cache tidy)
        for fn in os.listdir(d):
            if fn.startswith(sym.upper() + ".") and fn.endswith(".html.gz") and (".%s." % day) not in fn:
                try:
                    os.unlink(os.path.join(d, fn))
                except OSError:
                    pass
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        os.close(fd)
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, _cache_path(sym, kind, day))
        tmp = None
    except OSError:
        pass
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _smartlab_get(sym, kind, fetch, use_cache=True, day=None):
    """Fetch one smart-lab page ('q' or 'y'), consulting the per-day cache on the DEFAULT fetch path.
    A 404 (junk ticker) or any network error -> None (build degrades to Yahoo/ISS). When a `fetch` seam
    is injected (tests) the cache is bypassed (the seam is the source of truth)."""
    day = day or dt.date.today().isoformat()
    seam = fetch is not None
    fetch = fetch or _default_fetch
    if use_cache and not seam:
        hit = _cache_get(sym, kind, day)
        if hit is not None:
            return hit
    url = "%s/q/%s/f/%s/" % (SMARTLAB, sym.upper(), kind)
    try:
        text = fetch(url)
    except Exception:
        return None
    if use_cache and not seam and text:
        _cache_put(sym, kind, day, text)
    return text


# ================= DASHBOARD layer (v1.148) =================
# Design of record: docs/moex-fundamentals-dashboard-plan.md. The NUMBERS path (gather/build) stays
# untouched and fast; the dashboard needs three EXTRA best-effort sources — ISS daily candles (kline +
# 52w/DMA/drawdown/returns), the IMOEX series (Beta), and the MOEX vol store (the vol strip). Every one
# degrades on failure: a price/vol miss drops its panel, never the page (the "never hard-fails" rule).
_IDX_BETA = "IMOEX"          # the RU market proxy Beta is measured against
_PRICE_TIMEOUT_S = 90        # hard ceiling on the ISS price pulls (paging a 20y series is ~2-3s)
_BETA_DAYS = 1100            # ~3y — the conventional beta window (2-5y); bounds the IMOEX pull
_MIN_PRICE_DAYS = 30         # below this a "52w range"/DMA/CAGR read would be noise, not signal


def _rows_of(res):
    """chart.fetch_moex returns a TUPLE `(rows, source_tag)` — not the rows (live catch 2026-07-24: a
    synthetic fixture that returned bare rows hid this until the first real render). Accept either
    shape so the seam and the live getter agree; anything else -> []."""
    if isinstance(res, tuple) and len(res) == 2 and isinstance(res[0], list):
        return res[0]
    return res if isinstance(res, list) else []


_PRICE_START = "1997-01-01"   # precedes every MOEX equity listing -> ISS returns the FULL listed history


def _fetch_prices(sym, days=None, fetch=None):
    """ISS daily OHLCV via chart.fetch_moex (the SAME keyless getter chart.py --source moex uses) ->
    [(ts, o, h, l, c, vol)] rows. `days=None` pulls the FULL listed history (the default): the board
    advertises "full listing depth" and calls its drawdown an all-time figure, and a truncated window
    would make BOTH claims false — a 2-year window put GAZP's max drawdown at a fraction of its real
    -79.9% (the 2008 crash), published as if it were all-time (review P0). The cost is small and
    concurrent: ~5 200 bars in ~2s. `fetch(sym, start, end, target_min)` is the test seam.
    Any failure -> [] AND a stderr note (the reader must learn WHY the price panels vanished)."""
    start = _PRICE_START if not days else (dt.date.today() - dt.timedelta(days=days)).isoformat()
    try:
        if fetch is not None:
            return _rows_of(fetch(sym, start, None, 1440))
        import chart
        return _rows_of(chart.fetch_moex(sym, start, None, 1440))
    except (Exception, SystemExit) as e:
        # review P2 (silent-swallow): losing the chart, 52w range, vs-200d MA, drawdown AND beta with
        # nothing said anywhere left the reader guessing — say it once, on stderr, and degrade.
        print("note: %s — MOEX ISS price history unavailable (%s); the price chart, 52w range, "
              "vs-200d MA, drawdown and beta panels are omitted" % (sym, str(e)[:80]), file=sys.stderr)
        return []


def _closes_of(days):
    return [r[4] for r in days if r[4]]


def _price_stats(days):
    """Derived price context from the ISS series — 52w hi/lo + position, vs the 200d MA, max drawdown
    (+ the monthly drawdown series for the panel), trailing returns and the since-first-bar CAGR.
    Under _MIN_PRICE_DAYS sessions -> None (an honest absence, never a fabricated range)."""
    if not days or len(days) < _MIN_PRICE_DAYS:
        return None
    c = _closes_of(days)
    if len(c) < _MIN_PRICE_DAYS:
        return None
    last = c[-1]
    w52 = c[-252:] if len(c) >= 252 else c
    hi, lo = max(w52), min(w52)
    pos = ((last - lo) / (hi - lo) * 100.0) if hi > lo else None
    dma200 = (sum(c[-200:]) / 200.0) if len(c) >= 200 else None
    # drawdown from the running peak (monthly-resampled for a readable panel on a long history).
    # Walk the FILTERED (date, close) pairs so the drawdown index and the plotted month always refer to
    # the SAME session: indexing dd (built over filtered closes) with the position in the UNFILTERED
    # days list shifted every later month once any bar had a missing/zero close (review P2).
    pairs = [(r[0], r[4]) for r in days if r[4]]
    peak, max_dd, dd_lbl, dd_val, seen = pairs[0][1], 0.0, [], [], set()
    for ts, x in pairs:                          # ONE pass: running peak, the deepest drawdown, monthly points
        peak = max(peak, x)
        val = (x / peak - 1.0) * 100.0 if peak else 0.0
        max_dd = min(max_dd, val)
        key = ts.strftime("%Y-%m")
        if key not in seen:                      # one point per month (first session)
            seen.add(key)
            dd_lbl.append(key)
            dd_val.append(round(val, 2))

    def _ret(n):
        return round((last / c[-n] - 1.0) * 100.0, 2) if len(c) > n and c[-n] else None
    yrs = max((days[-1][0] - days[0][0]).days / 365.25, 1e-9)
    cagr = (round(((last / c[0]) ** (1.0 / yrs) - 1.0) * 100.0, 2) if (c[0] and last and yrs > 0.5) else None)
    return {"last": last, "hi52": hi, "lo52": lo, "pos52": (round(pos, 1) if pos is not None else None),
            "dma200": dma200, "dma200Pct": (round((last / dma200 - 1.0) * 100.0, 2) if dma200 else None),
            "maxDD": round(max_dd, 2), "ddSeries": dd_val, "ddLabels": dd_lbl,
            "firstDate": pairs[0][0].strftime("%Y-%m-%d"),   # the series start — the panel must NAME it
            "ret": {"1M": _ret(21), "3M": _ret(63), "6M": _ret(126), "1Y": _ret(252), "3Y": _ret(756)},
            "cagr": cagr}


def _beta(days, idx_days):
    """Beta of the share vs the RU market proxy (IMOEX) on overlapping daily returns — cov/var. Too
    little overlap (<60 common sessions) -> None (never a meaningless beta)."""
    if not days or not idx_days:
        return None
    a = {r[0].date(): r[4] for r in days if r[4]}
    b = {r[0].date(): r[4] for r in idx_days if r[4]}
    common = sorted(set(a) & set(b))
    if len(common) < 60:
        return None
    ra, rb = [], []
    for p, q in zip(common, common[1:]):
        if a[p] and b[p]:
            ra.append(a[q] / a[p] - 1.0)
            rb.append(b[q] / b[p] - 1.0)
    n = len(ra)
    if n < 30:
        return None
    ma, mb = sum(ra) / n, sum(rb) / n
    var = sum((x - mb) ** 2 for x in rb)
    if var <= 0:
        return None
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    return round(cov / var, 2)


def _kline_frames(days):
    """D/W/M OHLCV frames for the engine's kline panel, from the ISS rows (the fundamentals.py frame
    SHAPE, fed from MOEX candles instead of AV). Empty series -> None."""
    if not days:
        return None
    rows = [(r[0].strftime("%Y-%m-%d"), r[1], r[2], r[3], r[4], r[5]) for r in days if r[4]]
    if not rows:
        return None

    def _ms(d_):
        return int(dt.datetime.fromisoformat(d_ + "T00:00:00+00:00").timestamp() * 1000)

    def _bucket(key):
        agg, first = {}, {}
        for d_, o_, h_, l_, c_, v_ in rows:
            kk = key(d_)
            if kk not in agg:
                agg[kk] = [o_, h_, l_, c_, v_]
                first[kk] = d_
            else:
                a = agg[kk]
                a[1] = max(a[1], h_)
                a[2] = min(a[2], l_)
                a[3] = c_
                a[4] += v_
        ks = sorted(agg)
        return {"labels": ks, "ts": [_ms(first[k]) for k in ks],
                "o": [agg[k][0] for k in ks], "h": [agg[k][1] for k in ks],
                "l": [agg[k][2] for k in ks], "c": [agg[k][3] for k in ks], "v": [agg[k][4] for k in ks]}

    def _iso_week(d_):
        y, wk, _ = dt.date.fromisoformat(d_).isocalendar()
        return "%d-%02d" % (y, wk)
    fr_d = _bucket(lambda d_: d_)
    fr_w = _bucket(_iso_week)
    fr_m = _bucket(lambda d_: d_[:7])
    prec = 2 if (rows[-1][4] or 0) < 100 else (1 if (rows[-1][4] or 0) < 1000 else 0)
    return {"frame": "m" if len(fr_m["ts"]) >= 8 else "d", "pricePrec": prec,
            "frames": {"d": fr_d, "w": fr_w, "m": fr_m}}


def _adjust_closes(days, dividends):
    """Dividend-ADJUSTED close series {date: close} from raw ISS candles + the ISS dividend history.

    MOEX ISS publishes RAW closes, but `opthist.realized_at` requires ADJUSTED ones: on RU names paying
    8-15% yields the ex-dividend gap otherwise enters the return series as a genuine one-day crash and
    inflates realized vol for the whole trailing window — enough to flip the sign of the vol strip's
    "options priced RICH vs CHEAP" verdict (review P1). Standard back-adjustment: every close BEFORE an
    ex-date is scaled by (P - v)/P, where P is the last close before it, so the payout stops looking
    like a price move while every real move is preserved.

    APPROXIMATION (documented, not hidden): ISS gives the REGISTRY-CLOSE date, and the ex-date sits a
    session or two earlier, so the gap is removed from a date that can be off by a day or two — good
    enough to kill an 11% artefact, not a total-return index. No dividends -> the raw closes."""
    closes = {r[0].strftime("%Y-%m-%d"): r[4] for r in days if r[4]}
    if not dividends:
        return closes
    dates = sorted(closes)
    dvs = sorted(((str(x.get("date") or "")[:10], x.get("value") or 0.0) for x in dividends
                  if x.get("value") and _parse_iso(x.get("date"))), reverse=True)
    out, cum, di = {}, 1.0, 0
    for d_ in reversed(dates):                     # newest -> oldest, accumulating the factor
        while di < len(dvs) and dvs[di][0] > d_:   # crossed an ex-date: older bars carry the extra factor
            _prev = [x for x in dates if x < dvs[di][0]]
            if _prev:
                p = closes[_prev[-1]]
                if p and p > dvs[di][1] > 0:
                    cum *= (p - dvs[di][1]) / p
            di += 1
        out[d_] = closes[d_] * cum
    return out


def _vol_snapshot(sym, closes, load=None):
    """The options-vol strip context from the MOEX vol store (moexopthist — warm for the liquid names),
    reduced through the SAME opthist helpers the AV path uses (the records match byte-for-byte). Cold /
    non-optionable / no store -> None: the strip is simply OMITTED (there is no options history to show,
    and a 'warming' promise would be a lie on a name that may have no chain at all)."""
    try:
        import moexopthist as mh
        import opthist
    except Exception:
        return None
    try:
        hist = (load or mh.load_store)(sym)
    except Exception:
        hist = []
    if not hist:
        return None
    last = hist[-1]
    # opthist.realized_at takes an ITERABLE OF (date, close) PAIRS — iterating a dict yields bare
    # keys, which raised ValueError and (behind a broad except) silently blanked Realized HV + VRP on
    # every render. Normalize once here (live catch 2026-07-24).
    _cl = list(closes.items()) if isinstance(closes, dict) else list(closes or [])
    tenors = []
    for t in opthist.CM_TENORS:
        iv = last.get("cm%d" % t)
        try:
            rk = opthist.iv_rank_at(hist, t)
        except Exception:
            rk = None
        w = {30: 20, 60: 40, 90: 60}.get(t, 20)
        hv = None
        if _cl:
            try:
                hv = opthist.realized_at(_cl, last.get("date"), window=w)
            except Exception:
                hv = None
        tenors.append({"t": t, "iv": iv, "hv": hv,
                       "rank": (rk or {}).get("rank") if isinstance(rk, dict) else rk,
                       "pct": (rk or {}).get("pct") if isinstance(rk, dict) else None,
                       "vrp": (round((iv - hv) * 100, 1) if (iv is not None and hv is not None) else None)})
    if not any(t["iv"] is not None for t in tenors):
        return None
    return {"asOf": last.get("date"), "tenors": tenors}


def gather_dashboard(sym, fetch=None, yahoo_fetch=None, price_fetch=None, vol_load=None,
                     save_dir=None, want_annual=True, want_yahoo=True):
    """The --html path: the NUMBERS record (gather) + the three best-effort dashboard extras (prices,
    the IMOEX series for Beta, the vol store), fetched CONCURRENTLY. Each extra degrades on its own —
    a dead ISS candle endpoint costs the kline/price panels, never the statements page."""
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        # The SHARE is pulled at FULL listed depth (days=None): the drawdown panel calls its peak an
        # all-time high and the lead advertises full listing depth, so a truncated window would make both
        # claims false (review P0). The INDEX is only ever used for BETA, whose conventional estimation
        # window is 2-5 years — pulling IMOEX back to 1997 added ~14s of paging for statistically
        # irrelevant history (measured 2026-07-24: 22.6s -> 8s by bounding it).
        f_px = ex.submit(_fetch_prices, sym, None, price_fetch)
        f_ix = ex.submit(_fetch_prices, _IDX_BETA, _BETA_DAYS, price_fetch)
        d = gather(sym, fetch=fetch, yahoo_fetch=yahoo_fetch, save_dir=save_dir,
                   want_annual=want_annual, want_yahoo=want_yahoo)
        # a hung ISS pull must not block the render forever (review P2): bound the wait, then degrade
        def _res(fut, what):
            try:
                return fut.result(timeout=_PRICE_TIMEOUT_S)
            except Exception as e:
                print("note: %s — the %s pull did not finish in %ds (%s); its panels are omitted"
                      % (sym, what, _PRICE_TIMEOUT_S, str(e)[:60]), file=sys.stderr)
                return []
        days, idx = _res(f_px, "price history"), _res(f_ix, "index (beta)")

    def _safe(what, fn, *a):
        """The 'a price miss drops its panel, never the page' contract has to hold for the DERIVE layer
        too, not just the fetch: ISS can return a row shape these helpers don't expect, and an exception
        here would kill the whole --html render, statements included (review P3)."""
        try:
            return fn(*a)
        except Exception as e:
            print("note: %s — could not derive %s from the ISS series (%s); that panel is omitted"
                  % (sym, what, str(e)[:70]), file=sys.stderr)
            return None
    d["priceStats"] = _safe("price statistics", _price_stats, days)
    d["priceOHLC"] = _safe("the price chart", _kline_frames, days)
    d["beta"] = _safe("beta", _beta, days, idx)
    # realized vol needs ADJUSTED closes (opthist's contract): feed the dividend-adjusted series so an
    # ex-div gap is not counted as a crash (review P1)
    d["volSnap"] = _safe("the vol strip", lambda: _vol_snapshot(
        sym, _adjust_closes(days, d.get("dividends")), load=vol_load))
    return d


# ----------------- gather / build -----------------
RAW_NAMES = {"sl_q": "smartlab_quarterly.html", "sl_y": "smartlab_annual.html",
             "yahoo": "yahoo_quotesummary.json", "profile": "iss_profile.json",
             "div": "iss_dividends.json", "desc": "iss_description.json"}


def gather(sym, fetch=None, yahoo_fetch=None, save_dir=None, want_annual=True, want_yahoo=True):
    """Pull the sources and assemble a `raw` dict, then build(). `fetch(url)->text` is the generic HTTP
    seam (smart-lab + ISS); `yahoo_fetch(sym)->text|None` is the Yahoo seam (its cookie handshake can't
    ride the url seam). Both default to the real network; both are injected in the offline suite. All
    sources are best-effort — a failure of one degrades, never crashes."""
    sym = sym.upper()
    day = dt.date.today().isoformat()
    sl_q = _smartlab_get(sym, "q", fetch, day=day)
    sl_y = _smartlab_get(sym, "y", fetch, day=day) if want_annual else None
    yj = None
    if want_yahoo:
        yj = (yahoo_fetch or _default_yahoo_fetch)(sym)
    # ISS market wrapper: reuse moex_options.fetch_profile (TQBR shares/cap/name/currency) through the
    # SAME url seam (fetch_profile speaks an ISS-PATH seam, so bridge it); dividends via fetch_dividends.
    iss_seam = (lambda p: fetch(ISS + p)) if fetch is not None else None
    profile = mo.fetch_profile(sym, fetch=iss_seam)
    div = fetch_dividends(sym, fetch=fetch)
    desc = fetch_iss_description(sym, fetch=fetch)   # identity facts for the page lead (best-effort)
    raw = {"sl_q_html": sl_q, "sl_y_html": sl_y, "yahoo_json": yj, "profile": profile, "div_json": div,
           "iss_desc": desc}
    if save_dir:
        _save_raw(save_dir, raw)
    return build(sym, raw)


def _save_raw(save_dir, raw):
    """Dump the raw payloads so earnings-preview / an offline --from-dir replay can read them. The raw
    smart-lab HTML + Yahoo JSON go as-is; the ISS profile (a thin processed dict) + dividend list are
    JSON. Best-effort — a write failure warns and never wastes the fetch."""
    try:
        os.makedirs(save_dir, exist_ok=True)
        _writes = {"sl_q": raw.get("sl_q_html"), "sl_y": raw.get("sl_y_html")}
        for key, text in _writes.items():
            if text:
                _atomic_write(os.path.join(save_dir, RAW_NAMES[key]), text)
        if raw.get("yahoo_json"):
            _atomic_write(os.path.join(save_dir, RAW_NAMES["yahoo"]), raw["yahoo_json"])
        if raw.get("profile"):
            _atomic_write(os.path.join(save_dir, RAW_NAMES["profile"]), json.dumps(raw["profile"]))
        if raw.get("div_json"):
            _atomic_write(os.path.join(save_dir, RAW_NAMES["div"]), json.dumps(raw["div_json"]))
        if raw.get("iss_desc"):
            _atomic_write(os.path.join(save_dir, RAW_NAMES["desc"]), json.dumps(raw["iss_desc"]))
    except OSError as e:
        print("warning: --save-raw could not write %s (%s); continuing" % (save_dir, e), file=sys.stderr)


def _atomic_write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def read_from_dir(sym, d):
    """Rebuild from a --save-raw directory (offline replay, 0 network). Any missing file -> that source
    is simply absent (build degrades)."""
    def _read(name):
        try:
            with open(os.path.join(d, name), encoding="utf-8") as f:
                return f.read()
        except OSError:
            return None

    def _read_json(name, default):
        # a corrupt/truncated/hand-edited replay file must DEGRADE (source absent), not crash the
        # offline rebuild — the "never hard-fails" contract. The smart-lab HTML / Yahoo JSON already
        # degrade in their parsers; these two direct json.loads did not (review P2).
        txt = _read(name)
        if not txt:
            return default
        try:
            return json.loads(txt)
        except (ValueError, TypeError):
            return default
    raw = {"sl_q_html": _read(RAW_NAMES["sl_q"]), "sl_y_html": _read(RAW_NAMES["sl_y"]),
           "yahoo_json": _read(RAW_NAMES["yahoo"]),
           "profile": _read_json(RAW_NAMES["profile"], {}),
           "div_json": _read_json(RAW_NAMES["div"], []),
           "iss_desc": _read_json(RAW_NAMES["desc"], {})}
    return build(sym, raw)


def _is_bank(parsed):
    """Sector shape from smart-lab: a bank reports Чистая прибыль/ROE but NO Выручка/EBITDA (SBER/VTBR).
    Detect by the absence of BOTH revenue and ebitda field rows (a corp always lists revenue)."""
    if not parsed:
        return False
    f = parsed["fields"]
    return "revenue" not in f and "ebitda" not in f


def build(sym, raw):
    """Assemble the structured record from the raw sources. None = an honest hole; a total miss returns
    an `unavailable` record (never a crash, never fabricated zeros)."""
    sym = sym.upper()
    parsed_q = parse_smartlab(raw.get("sl_q_html"))
    parsed_y = parse_smartlab(raw.get("sl_y_html"))
    yahoo = parse_yahoo(raw.get("yahoo_json")) if raw.get("yahoo_json") else None
    profile = raw.get("profile") or {}
    div = raw.get("div_json") or []
    idesc = raw.get("iss_desc") or {}

    quarters, ltm = (_rows_from_parsed(parsed_q) if parsed_q else ([], {}))
    annual, _ay = (_rows_from_parsed(parsed_y) if parsed_y else ([], {}))
    # heal smart-lab's mid-row unit switches (₽bn header, trillions in the newest cells) before anything
    # downstream reads the numbers — see _heal_unit_jumps
    quarters, ltm = _heal_unit_jumps(quarters, ltm, sym, "quarterly")
    annual, _ay = _heal_unit_jumps(annual, _ay, sym, "annual")
    bank = _is_bank(parsed_q) or (_is_bank(parsed_y) if parsed_y else False)

    name = (profile.get("name") or (parsed_q and parsed_q["company"]) or (parsed_y and parsed_y["company"]) or "")
    spot, shares, cur = profile.get("spot"), profile.get("sharesOut"), profile.get("currency")
    # first-party cap in ₽bn — GATE on a rouble listing (mirror moex_options.py: a non-SUR/RUB board
    # quotes spot in a foreign currency, so spot×shares in ₽ would be a wrong-unit figure; review P3).
    cap_rub = cur in (None, "", "SUR", "RUB")
    cap_b = (spot * shares / 1e9) if (spot and shares and cap_rub) else None

    # fetch_profile returns an all-None dict (truthy!) for an unknown ISS ticker — real ISS data means a
    # name/spot/shares actually resolved, else a junk ticker would render an empty corp record, not "unavailable".
    iss_ok = bool(profile.get("name") or profile.get("spot") or profile.get("sharesOut"))
    prov = {"smartlab": bool(parsed_q or parsed_y), "yahooTTM": bool(yahoo), "iss": iss_ok}
    # honest cross-check: smart-lab LTM vs Yahoo live TTM, only when SAME currency (Yahoo tags USD for
    # GMKN — never cross-check across currencies). Prefer REVENUE (Yahoo's totalRevenue is a live TTM that
    # matched GAZP's smart-lab LTM to the decimal); fall back to the derived net income (banks list no
    # revenue). A ratio inside 0.7–1.43 (±~40%) reads as consistent — the two are compiled independently.
    cross = None
    if yahoo and (yahoo.get("currency") in ("RUB", None)) and ltm:
        for slk, yk, label in (("revenue", "revenueB", "revenue"), ("netIncome", "netIncomeB", "net income")):
            sl, yv = ltm.get(slk), yahoo.get(yk)
            if sl and yv:
                ratio = yv / sl
                cross = {"metric": label, "smartlabLtmB": sl, "yahooTtmB": yv, "ratio": round(ratio, 3),
                         "agree": 0.7 <= ratio <= 1.43}
                break

    if not prov["smartlab"] and not prov["yahooTTM"] and not prov["iss"]:
        return {"symbol": sym, "unavailable": True, "sectorShape": "unknown", "currency": "RUB",
                "asOf": dt.date.today().isoformat(), "provenance": prov,
                "note": ("fundamentals unavailable for %s — RU coverage is scrape-based (smart-lab primary, "
                         "Yahoo fallback, MOEX ISS) and all sources were unreachable or carried no data. "
                         "This is not a crash; re-try later." % sym),
                "source": _SL_SOURCE}

    d = {
        "symbol": sym, "name": name, "unavailable": False,
        "sectorShape": "bank" if bank else "corp",
        "currency": "RUB",
        "asOf": dt.date.today().isoformat(),   # WHEN this was pulled — not a report date
        "units": {"money": "RUB bn (rounded)", "pct": "percent", "ratios": "x",
                  "sharesM": "millions of shares (smart-lab per-quarter)",
                  "sharesOut": "absolute share count (ISS ISSUESIZE)"},
        "source": _SL_SOURCE,
        "provenance": prov,
        "quarters": quarters, "ltm": ltm, "annual": annual,
        "reportCurrency": (ltm.get("reportCurrency") if ltm else None),
        "spot": spot, "sharesOut": shares, "marketCapB": (round(cap_b, 2) if cap_b else None),
        "issCurrency": cur,
        "dividends": div,
        "identity": idesc,          # ISS first-party identity (legal name / ISIN / listing tier / type)
        "yahooTTM": yahoo,           # currency-tagged; a cross-check, never merged into the ₽ series
        "crossCheck": cross,
        "note": _read_note(sym, bank, prov),
    }
    return d


def _read_note(sym, bank, prov):
    """The one-line human honesty note carried on the record + printed in the text footer."""
    shape = ("bank shape (net income / ROE; banks report no EBITDA/revenue line)" if bank
             else "corporate shape (revenue / EBITDA / net income + multiples)")
    src = []
    if prov["smartlab"]:
        src.append("smart-lab (compiled МСФО/РСБУ, ₽bn, release-lagged)")
    if prov["yahooTTM"]:
        src.append("Yahoo TTM (currency-tagged cross-check)")
    if prov["iss"]:
        src.append("MOEX ISS (price/shares/cap/dividends, first-party)")
    return "%s — %s. Sources: %s. History + current multiples only; NO forward guidance/consensus " \
           "(not in any keyless source)." % (sym, shape, "; ".join(src) or "none")


# ================= to_spec — the dashboard (v1.148) =================
# Palette is the AV company board's, BYTE-IDENTICAL, so the two boards read as one system (the
# dashboard.py style contract). Revenue/net-income are two SHADES OF GREEN on purpose: the margin
# LINE takes its colour from the engine palette (a blue), which would otherwise collide with a blue
# net-income bar and read as one series (caught in the Preview Pane, 2026-07-24).
_UP, _NI, _AMBER, _RED = "#9FE1CB", "#0F6E56", "#EFB366", "#E24B4A"


def _series(rows, key):
    """A per-period value list for `key` (None where the period has no value — an honest hole)."""
    return [r.get(key) for r in rows]


def _has(vals):
    return any(v is not None for v in vals)


def _card(label, value, hint, sub=None):
    c = {"label": label, "value": value, "hint": hint}
    if sub:
        c["sub"] = sub
    return c


def _pv(v, d=1, suf=""):
    return "n/a" if v is None else ("%.*f%s" % (d, v, suf))


def _bnv(v):
    """A ₽bn money value for a card — the SIGN leads the currency symbol ("-₽5 879.1 bn", never
    "₽-5 879.1 bn", which reads as a typo; caught in the Preview Pane on GAZP's net debt)."""
    if v is None:
        return "n/a"
    return ("-₽%s bn" % _mny(abs(v))) if v < 0 else ("₽%s bn" % _mny(v))


def _periods(rows):
    return [r.get("period", "?") for r in rows]


def _parse_iso(s):
    """An ISS date string -> date, or None. ISS dates are EXTERNAL input: a malformed or placeholder
    value ('0000-00-00') must not raise out of to_spec and kill the whole dashboard (review P3)."""
    try:
        return dt.date.fromisoformat(str(s)[:10])
    except (ValueError, TypeError):
        return None


def _latest(d, key):
    """(value, period) for the newest period that actually reports `key` — LTM first, then the
    quarters newest-first, then the annual rows. Some smart-lab rows are ANNUAL-ONLY (headcount,
    labour productivity, cost per employee are printed once a year, in the reporting quarter), so
    reading the LTM column alone silently dropped them. The period rides along so the card can SAY
    which period it is quoting instead of implying it is current."""
    ltm = d.get("ltm") or {}
    if ltm.get(key) is not None:
        return ltm[key], "LTM"
    for rows in (list(reversed(d.get("quarters") or [])), list(reversed(d.get("annual") or []))):
        for r in rows:
            if r.get(key) is not None:
                return r[key], r.get("period", "?")
    return None, None


def _lead_dash(d):
    """The page-level lead (persists across tab swaps). It must READ AS A DESCRIPTION OF THE COMPANY
    first — the AV board opens with the issuer's business summary, and a lead that opens with sourcing
    caveats instead tells the reader nothing about what they are looking at (user 2026-07-24: «описание
    компании должно быть нормальным»). Order: what the company IS (Yahoo assetProfile's business summary
    when reachable, else the ISS identity facts: legal name / type / listing tier / ISIN) + what it is
    WORTH (cap, free float, sector shape) — THEN one compact sourcing sentence (compilation, ₽bn,
    lagged, ~5q/5y depth, no forward consensus), which the meta line echoes."""
    ident = d.get("identity") or {}
    yt = d.get("yahooTTM") or {}
    nm = ("%s (%s)" % (d["name"], d["symbol"])) if d.get("name") else d["symbol"]
    bits = []
    # 1. what the company IS — the real business description when Yahoo gave one, kept SHORT (the lead
    # is a caption, not a prospectus; the reader wants a couple of sentences, user 2026-07-24)
    desc = yt.get("description")
    if desc:
        _d1 = " ".join(desc.split())
        if len(_d1) > 320:
            _cut = _d1[:320]
            _dot = _cut.rfind(". ")            # prefer a sentence boundary over a mid-clause ellipsis
            _d1 = (_cut[:_dot + 1] if _dot > 160 else _cut.rsplit(" ", 1)[0] + "…")
        bits.append("%s — %s" % (nm, _d1))
    else:
        _who = [x for x in (ident.get("legalName"), ident.get("typeName")) if x]
        bits.append("%s — %s%s listed on the Moscow Exchange."
                    % (nm, (", ".join(_who) + ", " if _who else ""),
                       "a bank" if d["sectorShape"] == "bank" else "a Russian company"))
    # 2. identity facts ONLY — never what a card or the header already shows. Market cap lives in the
    # header (rightSmall) and free float is a Financials card, so repeating them here was pure noise
    # (user 2026-07-24: «дублируют часть карточек, например маркет кап»).
    facts = []
    if yt.get("sector") or yt.get("industry"):
        facts.append(" / ".join(x for x in (yt.get("sector"), yt.get("industry")) if x))
    if ident.get("listLevel"):
        facts.append("listing tier %s" % ident["listLevel"])
    if ident.get("isin"):
        facts.append(ident["isin"])
    if facts:
        bits.append(" · ".join(facts) + ".")
    # 3. ONE compact sourcing sentence — every honesty element, no padding
    bits.append("Statements: smart-lab's compilation of МСФО/РСБУ reports — ₽bn, release-lagged, ~5 "
                "quarters / 5 years deep (not a primary filing). Price and vol: MOEX ISS, full listing "
                "depth. No analyst estimates — no keyless source has them.%s"
                % (" Bank shape: no EBITDA or corporate revenue line."
                   if d["sectorShape"] == "bank" else ""))
    return {"text": " ".join(bits),
            "meta": ("as of %s · statements are release-lagged" % d.get("asOf", "?"))}


def _overview_cards(d):
    ltm, ps = d.get("ltm") or {}, d.get("priceStats") or {}
    qs = d.get("quarters") or []
    yoy = None
    if len(qs) >= 5 and qs[-1].get("revenue") and qs[-5].get("revenue"):
        yoy = (qs[-1]["revenue"] / qs[-5]["revenue"] - 1.0) * 100.0
    cards = []
    if d["sectorShape"] != "bank":
        cards.append(_card("Rev YoY", _pv(yoy, 1, "%"),
                           "Latest quarter revenue growth year over year (smart-lab, ₽bn)."))
        cards.append(_card("EBITDA margin", _pv(ltm.get("ebitdaMargin"), 1, "%"),
                           "EBITDA margin over the trailing twelve months."))
    cards.append(_card("Net margin", _pv(ltm.get("netMargin"), 1, "%"), "Net margin over the trailing twelve months."))
    if d["sectorShape"] == "bank":
        # "net cash" (cash − debt) is MEANINGLESS for a bank — deposits ARE its liabilities. The headline
        # synthesis metrics of a bank are the margin it earns and the capital buffer behind it.
        cards.append(_card("NIM", _pv(ltm.get("nim"), 2, "%"),
                           "Net interest margin — the core profitability metric of a bank (smart-lab)."))
        if ltm.get("carTotal") is not None or ltm.get("car1") is not None:
            cards.append(_card("Capital adequacy", _pv(ltm.get("carTotal") or ltm.get("car1"), 1, "%"),
                               "Total capital adequacy ratio — the regulatory buffer against risk-weighted "
                               "assets (CBR minimum ~8%)."))
    else:
        _cash, _debt = ltm.get("cash"), ltm.get("debt")
        cards.append(_card("Net cash", _bnv((_cash - _debt) if (_cash is not None and _debt is not None) else None),
                           "Cash minus debt (₽bn). Negative = net debt."))
    cards.append(_card("P/E", _pv(ltm.get("pe"), 2), "Price / earnings, TRAILING twelve months (never forward — no keyless consensus)."))
    if d["sectorShape"] != "bank":
        cards.append(_card("EV/EBITDA", _pv(ltm.get("evEbitda"), 2), "Enterprise value / EBITDA over the trailing twelve months."))
    cards.append(_card("ROE", _pv(ltm.get("roe"), 1, "%"), "Return on equity over the trailing twelve months."))
    cards.append(_card("Div yield", _pv(ltm.get("divYield"), 1, "%"),
                       "Dividend yield (smart-lab); the payment history is on the Dividends tab."))
    if ps.get("pos52") is not None:
        cards.append(_card("52w range", "%.0f%%" % ps["pos52"],
                           "Where the price sits in its 52-week range: 0%% = the low, 100%% = the high "
                           "(₽%s - ₽%s). Source: MOEX ISS." % (_rat(ps.get("lo52"), 2), _rat(ps.get("hi52"), 2))))
    if ps.get("dma200Pct") is not None:
        cards.append(_card("vs 200d MA", "%+.1f%%" % ps["dma200Pct"],
                           "Price vs its 200-day moving average (MOEX ISS)."))
    return cards


def _fin_cards(d):
    ltm = d.get("ltm") or {}
    if d["sectorShape"] == "bank":
        # The BANKING metric set — a bank is analysed on margin, capital adequacy, risk cost and asset
        # quality, none of which exist on a corporate page (and none of which the AV US board breaks out).
        cards = [_card("NIM", _pv(ltm.get("nim"), 2, "%"),
                       "Net interest margin — interest income minus interest expense over earning assets. "
                       "The core profitability metric of a bank (smart-lab)."),
                 _card("P/BV", _pv(ltm.get("pbv") or ltm.get("pB"), 2),
                       "Price / book value — the headline valuation multiple for a bank."),
                 _card("ROE", _pv(ltm.get("roe"), 1, "%"), "Return on equity over the trailing twelve months."),
                 _card("Cost/income", _pv(ltm.get("costToIncome"), 1, "%"),
                       "Operating costs as a share of operating income — the standard bank efficiency "
                       "ratio. Lower is better; below ~40% is strong.")]
        if ltm.get("carTotal") is not None or ltm.get("car1") is not None:
            cards.append(_card("Capital adequacy", _pv(ltm.get("carTotal") or ltm.get("car1"), 1, "%"),
                               "Total capital adequacy ratio (core ratio %s) — the regulatory buffer "
                               "against risk-weighted assets. The CBR minimum is ~8%%; a thin ratio caps "
                               "both growth and dividends." % _pv(ltm.get("car1"), 1, "%"),
                               sub=(["core %s" % _pv(ltm.get("car1"), 1, "%")] if ltm.get("car1") is not None else None)))
        if ltm.get("costOfRisk") is not None:
            cards.append(_card("Cost of risk", _pv(ltm.get("costOfRisk"), 2, "%"),
                               "Provision charges as a share of the loan book — what credit risk actually "
                               "costs the bank each period. Rising CoR eats the margin."))
        if ltm.get("npl") is not None:
            cards.append(_card("NPL share", _pv(ltm.get("npl"), 2, "%"),
                               "Non-performing loans as a share of the portfolio — asset quality. Compare "
                               "against the provisioning coverage on the asset-quality panel."))
        if ltm.get("ldr") is not None:
            cards.append(_card("Loans/deposits", _pv(ltm.get("ldr"), 0, "%"),
                               "Loan-to-deposit ratio — funding balance. Above 100% means the loan book "
                               "outgrew its deposit funding and leans on wholesale money."))
        if ltm.get("clientsM") is not None:
            cards.append(_card("Clients", _pv(ltm.get("clientsM"), 1) + "M",
                               "Active clients, millions (smart-lab) — the franchise's scale."))
        cards.append(_card("ROA", _pv(ltm.get("roa"), 1, "%"), "Return on assets over the trailing twelve months."))
        _bt, _btp = _latest(d, "bankAssets")
        if _bt is not None:
            cards.append(_card("Bank assets", _bnv(_bt),
                               "Total assets on the balance sheet (₽bn, %s) — a bank's balance sheet IS its "
                               "business, unlike a corporate's." % _btp))
    else:
        cards = [_card("P/S", _pv(ltm.get("ps"), 2), "Market cap / revenue over the trailing twelve months."),
                 _card("ROA", _pv(ltm.get("roa"), 1, "%"), "Return on assets over the trailing twelve months."),
                 _card("P/BV", _pv(ltm.get("pbv"), 2), "Price / book value."),
                 _card("FCF yield", _pv(ltm.get("fcfYield"), 1, "%"),
                       "Free cash flow yield (FCF / market cap)."),
                 _card("Debt/EBITDA", _pv(ltm.get("debtEbitda"), 2),
                       "Leverage: debt to EBITDA. Above ~3x is generally considered high.")]
    # ---- shared tail (both shapes): market beta, float, capital intensity, headcount ----
    if d.get("beta") is not None:
        cards.append(_card("Beta", _pv(d["beta"], 2),
                           "Beta vs the %s index on daily returns (MOEX ISS). >1 = more volatile than "
                           "the market." % _IDX_BETA))
    ff = ltm.get("freeFloat")
    if ff is not None:
        cards.append(_card("Free float", _pv(ff, 0, "%"), "Share of stock in free float (smart-lab)."))
    # EFFICIENCY block — RU-only detail the AV board has no equivalent for (these rows were parsed and
    # dropped before v1.148). Headcount is an absolute count, productivity is ₽m of revenue per employee.
    if ltm.get("capexToRevenue") is not None:
        cards.append(_card("Capex/revenue", _pv(ltm.get("capexToRevenue"), 0, "%"),
                           "Capital intensity: capex as a share of revenue — how much of every rouble of "
                           "sales is reinvested just to keep the business running (smart-lab)."))
    # headcount / productivity are ANNUAL-ONLY rows -> take the latest reporting period and SAY which
    _emp, _empp = _latest(d, "employees")
    if _emp is not None:
        cards.append(_card("Employees", format(_emp, ",.0f").replace(",", " "),
                           "Headcount as of %s (smart-lab reports it once a year, so this is the latest "
                           "published figure, not a current one)." % _empp,
                           sub=[_empp] if _empp != "LTM" else None))
    _lp, _lpp = _latest(d, "labourProductivity")
    if _lp is not None:
        cards.append(_card("Revenue/employee", _pv(_lp, 2),
                           "Labour productivity as smart-lab reports it (₽m of revenue per employee), "
                           "latest published period %s." % _lpp,
                           sub=[_lpp] if _lpp != "LTM" else None))
    return cards   # (a bank's Cost/income already rides its own metric set above — never duplicated)


def _combo(title, labels, bars, line=None, info="", units="₽B"):
    """A bars+line combo panel; a series with no data at all is dropped, and the panel itself is None
    when nothing survives (never an empty chart)."""
    ser = [{"type": "bar", "name": n, "values": v, "color": c} for n, v, c in bars if _has(v)]
    if not ser:
        return None
    if line and _has(line[1]):
        ser.append({"type": "line", "name": line[0], "values": line[1], "axis": "y1"})
    return {"type": "combo", "title": title, "units": {"left": units}, "info": info,
            "data": {"labels": labels, "series": ser}}


def _fin_panels(d):
    """The statement panels — annual when the annual page parsed (5y reads better), else quarterly.
    Bank vs corp shape decides WHICH panels exist (a bank never gets a fabricated EBITDA/FCF panel)."""
    ann, qs = d.get("annual") or [], d.get("quarters") or []
    rows = ann if len(ann) >= 3 else qs
    span = "annual" if rows is ann and ann else "quarterly"
    lbl = _periods(rows)
    out = []
    if d["sectorShape"] == "bank":
        # A bank is a BALANCE SHEET business: income mix, the loan book, funding, capital and asset
        # quality — the full set smart-lab publishes for banks (27 fields the corporate keys don't cover).
        p = _combo("Income mix (₽B): net interest + fees · ROE", lbl,
                   [("net interest income", _series(rows, "netInterestIncome"), _UP),
                    ("commission income", _series(rows, "commissionIncome"), _AMBER),
                    ("net income", _series(rows, "netIncome"), _NI)],
                   ("ROE %", _series(rows, "roe")),
                   "Where a bank earns (%s): net interest income vs fee/commission income, with net "
                   "income and ROE. A rising fee share is earnings that do NOT depend on the rate cycle. "
                   "Banks report no corporate revenue/EBITDA line — those panels are absent BY DESIGN "
                   "here, not lost." % span)
        if p:
            out.append(p)
        p = _combo("Balance sheet (₽B): assets · loans · deposits · capital", lbl,
                   [("bank assets", _series(rows, "bankAssets"), _NI),
                    ("loan portfolio", _series(rows, "loanPortfolio"), _UP),
                    ("deposits", _series(rows, "deposits"), _AMBER),
                    ("capital", _series(rows, "capital"), _RED)],
                   None,
                   "The bank's balance sheet (%s): total assets, the loan book it earns on, the deposits "
                   "funding it and the equity capital absorbing losses." % span)
        if p:
            out.append(p)
        p = _combo("Loan book (₽B): corporate vs retail", lbl,
                   [("corporate loans", _series(rows, "corporateLoans"), _NI),
                    ("retail loans", _series(rows, "retailLoans"), _UP)],
                   ("NIM %", _series(rows, "nim")),
                   "Loan mix (%s) with net interest margin on the right axis. Retail usually carries a "
                   "wider margin and a higher risk cost than corporate lending — the mix shift explains "
                   "margin moves." % span)
        if p:
            out.append(p)
        p = _combo("Funding (₽B): corporate vs retail deposits", lbl,
                   [("corporate deposits", _series(rows, "corporateDeposits"), _NI),
                    ("retail deposits", _series(rows, "retailDeposits"), _UP)],
                   ("loans / deposits %", _series(rows, "ldr")),
                   "Deposit funding by client type (%s) with the loan-to-deposit ratio: retail deposits "
                   "are the stickier, cheaper funding base." % span)
        if p:
            out.append(p)
        # asset quality — a pure-ratio panel (NPL / cost of risk / capital adequacy)
        _aq = [("NPL share %", _series(rows, "npl")), ("cost of risk %", _series(rows, "costOfRisk")),
               ("capital adequacy %", _series(rows, "carTotal")), ("core capital %", _series(rows, "car1"))]
        _aqs = [{"name": n, "values": v} for n, v in _aq if _has(v)]
        if _aqs:
            out.append({"type": "line", "title": "Asset quality & capital (%%, %s)" % span,
                        "units": {"left": "%"},
                        "info": "Credit risk and the capital buffer (%s): the non-performing share of the "
                                "loan book, what provisioning costs each period (cost of risk), and the "
                                "regulatory capital adequacy ratios. Rising NPL with a falling capital "
                                "ratio is the stress combination to watch." % span,
                        "data": {"labels": lbl, "series": _aqs}})
        p = _combo("Provisions (₽B): stock vs period charge", lbl,
                   [("provisions on balance", _series(rows, "loanProvisions"), _NI),
                    ("charge for the period", _series(rows, "provisionCharge"), _RED)],
                   None,
                   "Loan-loss provisions (%s): the accumulated buffer on the balance sheet vs what was "
                   "charged to profit in the period. The charge is what actually hits earnings." % span)
        if p:
            out.append(p)
    else:
        p = _combo("Revenue & net income (₽B) · margin", lbl,
                   [("revenue", _series(rows, "revenue"), _UP),
                    ("net income", _series(rows, "netIncome"), _NI)],
                   ("net margin %", _series(rows, "netMargin")),
                   "Revenue vs net income (%s); the gap between the bars is the cost of running the "
                   "business, and a rising margin means it is shrinking. Source: the smart-lab "
                   "compilation (₽bn, release-lagged)." % span)
        if p:
            out.append(p)
        p = _combo("EBITDA (₽B) · margin", lbl,
                   [("EBITDA", _series(rows, "ebitda"), _AMBER)],
                   ("EBITDA margin %", _series(rows, "ebitdaMargin")),
                   "EBITDA and EBITDA margin (%s) — operating profitability before interest, tax, "
                   "depreciation and amortisation." % span)
        if p:
            out.append(p)
        p = _combo("Free cash flow (₽B): OCF − capex", lbl,
                   [("operating cash flow", _series(rows, "ocf"), _UP),
                    ("capex", _series(rows, "capex"), _AMBER),
                    ("FCF", _series(rows, "fcf"), _NI)],
                   None,
                   "Operating cash flow vs capital expenditure (%s); FCF is what is left for "
                   "dividends, debt repayment and buybacks." % span)
        if p:
            out.append(p)
    p = _combo("Balance (₽B): assets, equity, debt", lbl,
               [("assets", _series(rows, "assets"), _NI),
                ("net assets (equity)", _series(rows, "netAssets"), _UP),
                ("debt", _series(rows, "debt"), _RED)],
               None,
               "Assets vs shareholders' equity and debt (%s) — balance-sheet structure and "
               "leverage." % span)
    if p:
        out.append(p)
    # WHERE THE REVENUE GOES — the cost structure (opex / staff / amortisation / interest). These rows
    # sit on the same smart-lab page and were previously dropped (the "board looks thin" catch, user
    # 2026-07-24): 23 populated fields were parsed away. The US board has no direct twin (AV doesn't
    # break costs out), so this is RU-only detail.
    p = _combo("Cost structure (₽B): opex · staff · amortisation · interest", lbl,
               [("operating expenses", _series(rows, "opex"), _AMBER),
                ("staff costs", _series(rows, "staffCosts"), _NI),
                ("amortisation", _series(rows, "amortization"), _UP),
                ("interest expense", _series(rows, "interestExpenses"), _RED)],
               None,
               "Where the revenue goes (%s): total operating expenses with the staff, amortisation and "
               "interest lines broken out (smart-lab). Rising interest expense against flat operating "
               "costs is a leverage story, not an operating one." % span)
    if p:
        out.append(p)
    # PER-SHARE economics — what one share owns and earns (EPS / book value / FCF per share).
    p = _combo("Per share (₽): EPS · book value · FCF", lbl,
               [("EPS", _series(rows, "eps"), _NI),
                ("book value / share", _series(rows, "bvShare"), _UP),
                ("FCF / share", _series(rows, "fcfShare"), _AMBER)],
               None,
               "Per-share economics (%s): earnings, book value and free cash flow per ordinary share "
               "(smart-lab, ₽). Compare EPS against the share price on the Overview chart, and book "
               "value against P/BV." % span, units="₽/share")
    if p:
        out.append(p)
    # multiples trend (line): the valuation history in one panel
    mult = [("P/E", _series(rows, "pe")), ("P/S", _series(rows, "ps")),
            ("EV/EBITDA", _series(rows, "evEbitda")), ("P/BV", _series(rows, "pbv")),
            ("P/FCF", _series(rows, "pFcf"))]
    mser = [{"name": n, "values": v} for n, v in mult if _has(v)]
    if mser:
        out.append({"type": "line", "title": "Valuation multiples (%s)" % span,
                    "info": "How the market valued the company each period (TRAILING multiples from the "
                            "smart-lab compilation). No forward multiples — analyst consensus is not "
                            "available in any keyless source.",
                    "data": {"labels": lbl, "series": mser}})
    return out


def _div_section(d):
    """The RU replacement for the US Ownership tab: the ISS dividend history (first-party) + the
    share-count trend (a buyback/issuance proxy). Dropped entirely when neither exists."""
    dv, ltm = d.get("dividends") or [], d.get("ltm") or {}
    rows = d.get("annual") or d.get("quarters") or []
    panels, cards = [], []
    if dv:
        # Group by (year, CURRENCY): ISS pays some issuers in USD (probed 2026-07-24: RUAL/POLY/QIWI are
        # USD while GAZP is RUB), so summing across currencies into one ₽-labelled bar was a real
        # corruption (review P1). One SERIES PER CURRENCY, each labelled with its own unit.
        by_cur = {}
        for x in dv:
            _y, _c = str(x.get("date") or "")[:4], (x.get("currency") or "RUB")
            if len(_y) == 4 and _y.isdigit():          # ignore an unparseable ISS date (review P3)
                by_cur.setdefault(_c, {})
                by_cur[_c][_y] = by_cur[_c].get(_y, 0.0) + (x.get("value") or 0.0)
        by_year = by_cur.get("RUB") or (list(by_cur.values())[0] if by_cur else {})
        # Fill the GAP years with an explicit 0 so a SUSPENSION is visible instead of the chart simply
        # stopping at the last payment (GAZP ended at 2022 in a 2026 session — indistinguishable from
        # "our data ran out"; user catch 2026-07-24). ISS is the complete first-party record, so a zero
        # for a COMPLETED year is genuinely sourced. The CURRENT year is NOT padded: most RU registers
        # close in May-July, so "no entry yet" is unknowable, not a cancellation — zero-filling it
        # published a company that simply has not paid YET as having cancelled its dividend (review P1).
        _yrs_all = sorted(set().union(*[set(m) for m in by_cur.values()])) if by_cur else []
        _last_complete = dt.date.today().year - 1
        _end = max(_last_complete, int(_yrs_all[-1]) if _yrs_all else _last_complete)   # keep future-dated entries
        yrs = ["%d" % y for y in range(int(_yrs_all[0]), _end + 1)] if _yrs_all else []
        _cur_main = "RUB" if "RUB" in by_cur else (sorted(by_cur)[0] if by_cur else "RUB")
        _csym = {"RUB": "₽", "SUR": "₽"}.get(_cur_main, _cur_main + " ")
        _ser, _gap = [], []
        for _c in sorted(by_cur, key=lambda k: (k != _cur_main, k)):
            m = by_cur[_c]
            _sym = {"RUB": "₽", "SUR": "₽"}.get(_c, _c + " ")
            # a year the ISSUER never paid in THIS currency is 0 only for completed years; the current
            # year stays None (an honest unknown) unless a payment is already on record
            _v = [(round(m[y], 2) if y in m else (None if int(y) >= dt.date.today().year else 0.0))
                  for y in yrs]
            _ser.append({"name": "dividend %s/share" % _sym.strip(), "color": _UP if _c == _cur_main else _AMBER,
                         "values": _v})
            _gap += [y for y in yrs if y not in m and int(y) < dt.date.today().year]
        _multi = len(by_cur) > 1
        panels.append({"type": "bars",
                       "title": "Dividends per share by year (%s) — MOEX ISS" % (
                           "mixed currencies" if _multi else _csym.strip()),
                       "units": {"left": _csym.strip()},
                       "info": "Dividend history per share by registry-close date (MOEX ISS — the "
                               "first-party source, not a compilation), summed per year. A ZERO year is a "
                               "real zero: ISS holds the complete record, so no entry for a COMPLETED year "
                               "means no payment was made. The CURRENT year is left EMPTY, not zeroed — "
                               "most RU registers close mid-year, so 'nothing yet' is not a cancellation."
                               + (" This issuer has paid in MORE THAN ONE currency (%s) — one series per "
                                  "currency, never summed together." % ", ".join(sorted(by_cur)) if _multi else "")
                               + ((" No payment on record for: %s." % ", ".join(sorted(set(_gap)))) if _gap else ""),
                       "data": {"labels": yrs, "xYear": True, "series": _ser}})
        last = dv[-1]
        _lsym = {"RUB": "₽", "SUR": "₽"}.get(last.get("currency") or "RUB", (last.get("currency") or "") + " ")
        _ld = _parse_iso(str(last.get("date") or "")[:10])
        _ago = ((dt.date.today() - _ld).days / 365.25) if _ld else None
        _when = ("%.1f years ago" % _ago) if (_ago is not None and _ago >= 0) else (
            "scheduled ahead" if _ago is not None else "date unavailable")
        cards.append(_card("Last dividend", "%s%s" % (_lsym, _rat(last.get("value"), 2)),
                           "Registry close %s (MOEX ISS) — %s. A completed year with no entry means no "
                           "payment was made; the current year may simply not have closed its register yet."
                           # a missing registry date printed as the literal "None" (speccheck FORMAT_ROT)
                           % (last.get("date") or "date not published", _when),
                           # str(None)[:10] printed the literal "None · date unavailable" in a
                           # user-facing card (speccheck FORMAT_ROT); with no date the sub is the
                           # explanation alone
                           sub=(([("%s · %s" % (str(last.get("date"))[:10], _when))
                                  if last.get("date") else _when])
                                if (_ago is None or _ago >= 1.5 or _ago < 0) else None)))
    if ltm.get("divYield") is not None:
        cards.append(_card("Div yield", _pv(ltm.get("divYield"), 1, "%"), "Dividend yield (smart-lab)."))
    if ltm.get("divPayoutRatio") is not None:
        cards.append(_card("Payout", _pv(ltm.get("divPayoutRatio"), 0, "%"),
                           "Share of profit paid out as dividends (smart-lab)."))
    # Capital returned — the AV board's "Cap return ($B): bbs + divs · shares" mirror: the CASH paid
    # out (smart-lab's Див.выплата, ₽bn) as bars with the share count as a right-axis LINE for context.
    # A share count alone was a dead panel (GAZP: five identical bars — it never issues or buys back;
    # user catch 2026-07-24), whereas the payout is the actual shareholder-return story. NOTE: RU has no
    # keyless ₽ BUYBACK figure, so unlike the AV twin this panel is dividends-only — stated in the info.
    sh, pay = _series(rows, "sharesM"), _series(rows, "dividendPayout")
    if _has(pay) or _has(sh):
        ser = []
        if _has(pay):
            ser.append({"type": "bar", "name": "dividends paid", "values": pay, "color": _UP})
        if _has(sh):
            ser.append({"type": "line", "name": "shares, M", "values": sh, "axis": "y1"})
        panels.append({"type": "combo", "title": "Capital returned (₽B): dividends · share count",
                       "units": {"left": "₽B", "right": "M shares"},
                       # the right axis is a SHARE COUNT — without this the engine suffixes it "%"
                       "y1pct": False,
                       "info": "Cash actually paid out to shareholders per period (smart-lab «Див.выплата», "
                               "₽bn) with the ordinary-share count as the right-axis line: a falling count = "
                               "buybacks, a rising one = dilution. The source carries NO ₽ buyback figure "
                               "(unlike a US board, which splits buybacks vs dividends), so the bars are "
                               "dividends only — a flat share line simply means no issuance or buybacks.",
                       "data": {"labels": _periods(rows), "series": ser}})
    if not panels:
        return None
    return {"id": "dividends", "name": "Dividends", "cards": cards, "panels": panels}


def to_spec(d):
    """The record -> a dashboard.py SPEC: the v1.110 unified header, the page lead (provenance +
    depth), and the tabs [Overview · Financials · Dividends?]. NEVER emits an analyst/insider/13F
    surface — those are structurally unavailable keyless, so the board omits them rather than
    faking an empty tab (docs/moex-fundamentals-dashboard-plan.md § Data parity)."""
    if d.get("unavailable"):
        return {"head": {"leftBig": d["symbol"], "leftName": "", "leftType": "Fundamentals"},
                "title": "%s — fundamentals unavailable" % d["symbol"],
                "lead": {"text": d.get("note", ""), "meta": d.get("asOf", "")},
                "dataSource": _SL_SOURCE, "cards": [], "panels": []}
    ps = d.get("priceStats") or {}
    _cur = _cur_sym(d.get("issCurrency"))
    head = {"leftBig": d["symbol"], "leftName": d.get("name") or "", "leftType": "Fundamentals",
            "rightBig": ((_cur + _rat(d.get("spot"), 2)) if d.get("spot") else ""),
            "rightSmall": _right_small_dash(d)}

    over = list(_overview_cards(d))
    p_over = []
    if d.get("priceOHLC"):
        p_over.append({"type": "kline", "full": True, "collapsible": True,
                       "title": "%s price chart (MOEX ISS)" % d["symbol"],
                       "indicators": ["MA", "EMA", "MACD", "RSI", "BOLL", "VOL"],
                       "data": dict(d["priceOHLC"], on=["VOL"], barSpace=8)})
    vp = _vol_strip_panel(d)
    if vp:
        p_over.append(vp)
    # the headline statement chart repeats on Overview (the synthesis view)
    _fin = _fin_panels(d)
    if _fin:
        p_over.append(_fin[0])
    if ps.get("ddSeries"):
        _fd = ps.get("firstDate") or "?"
        p_over.append({"type": "line", "title": "Drawdown from peak (%%) — since %s (MOEX ISS)" % _fd[:7],
                       "units": {"left": "%"},
                       "info": "How far below its running peak the share traded (monthly), measured over "
                               "the FULL ISS series — which starts %s, so the peak is an all-time high "
                               "only as far back as MOEX publishes candles for this listing. Deepest "
                               "drawdown in that window: %.1f%%. Raw (not dividend-adjusted) closes."
                               % (_fd, ps["maxDD"]),
                       "data": {"labels": ps["ddLabels"], "xYear": True,
                                "series": [{"name": "drawdown %", "color": _RED, "values": ps["ddSeries"]}]}})
    sections = [{"id": "overview", "name": "Overview", "cards": over, "panels": p_over},
                {"id": "financials", "name": "Financials", "cards": _fin_cards(d), "panels": _fin}]
    ds = _div_section(d)
    if ds:
        sections.append(ds)
    return {"head": head, "lead": _lead_dash(d),
            "title": "%s fundamentals" % d["symbol"],
            "dataSource": ("smart-lab.ru (statements — a release-lagged compilation) · MOEX ISS "
                           "(price/shares/cap/dividends)"
                           + (" · Yahoo TTM (cross-check)" if d.get("yahooTTM") else "")),
            "sections": sections, "active": "overview"}


def _right_small_dash(d):
    """The header change/cap line — cap is RUB-gated exactly like the numbers path (a non-rouble
    listing shows no ₽ cap)."""
    parts = []
    if d.get("marketCapB"):
        cap = dashboard.cap_str(d["marketCapB"], cur="₽")
        if cap:
            parts.append("cap " + cap)
    ret = ((d.get("priceStats") or {}).get("ret") or {}).get("1Y")
    if ret is not None:
        parts.append("1Y %+.1f%%" % ret)
    return " · ".join(parts)


def _vol_strip_panel(d):
    """The options-vol term strip (30/60/90 CM IV · rank · realized · VRP) from the MOEX vol store —
    the AV board's strip, fed by moexopthist. Absent when the store is cold/the name has no chain."""
    vs = d.get("volSnap")
    if not vs or not vs.get("tenors"):
        return None
    rows = []
    for t in vs["tenors"]:
        w = {30: 20, 60: 40, 90: 60}.get(t["t"], 20)
        rows.append(["%dd" % t["t"],
                     ("%.1f%%" % (t["iv"] * 100)) if t.get("iv") is not None else "n/a",
                     ("%.0f%%" % t["rank"]) if t.get("rank") is not None else "n/a",
                     ("%.1f%% (%dd)" % (t["hv"] * 100, w)) if t.get("hv") is not None else "n/a",
                     ("%+.1f" % t["vrp"]) if t.get("vrp") is not None else "n/a"])
    return {"type": "table", "title": "Vol · term structure — settlement %s" % (vs.get("asOf") or "?"),
            "info": "Constant-maturity ATM IV by tenor from the LOCAL MOEX settlement store "
                    "(moexopthist; IV is RECOMPUTED Black-Scholes on the share — MOEX publishes none in "
                    "history). The SHAPE is the read: front-rich = near-term risk/event, contango = the "
                    "calm-market normal. Realized HV is computed on DIVIDEND-ADJUSTED ISS closes (the "
                    "payout is backed out of the series using the ISS dividend history, so an ex-div gap "
                    "is not counted as a crash; ISS dates the record close, so the adjustment can sit a "
                    "session or two off). Live chain: moex_options.py %s --full." % d["symbol"],
            "data": {"columns": ["Tenor", "ATM IV", "IV rank", "Realized HV", "VRP"], "rows": rows,
                     "big": True,
                     "colhints": {"ATM IV": "at-the-money implied volatility interpolated to a constant "
                                            "maturity",
                                  "IV rank": "where today's IV sits in the store's min-max range",
                                  "Realized HV": "realized volatility over the paired trailing window "
                                                 "(30↔20d, 60↔40d, 90↔60d)",
                                  "VRP": "IV − realized, in points: positive = options priced RICH vs the "
                                         "movement the share delivered"},
                     "cellstyle": [["", "font-weight:700", "", "", ""] for _ in rows]}}


def render_html(spec):
    """The ONE interactive engine page (M7: user-facing --html is always the dashboard.py renderer)."""
    return dashboard.render_interactive_html(spec)


# ----------------- text -----------------
def _mny(v):
    return "n/a" if v is None else format(v, ",.1f").replace(",", " ")   # ₽bn with space thousand-sep


def _rat(v, d=2):
    return "n/a" if v is None else ("%.*f" % (d, v))


def _pct(v):
    return "n/a" if v is None else ("%.1f%%" % v)


def _cur_sym(cur):
    """Spot/price prefix by the ISS listing currency — ₽ for a rouble board, else the currency code
    (a non-SUR/RUB MOEX listing quotes in a foreign currency; don't mislabel it ₽; review P3)."""
    return "₽" if cur in (None, "", "SUR", "RUB") else (cur + " ")


def render_text(d):
    L = []
    if d.get("unavailable"):
        return d.get("note", "fundamentals unavailable for %s" % d["symbol"])
    nm = ("%s (%s)" % (d["name"], d["symbol"])) if d.get("name") else d["symbol"]
    L.append("%s — RU FUNDAMENTALS · %s · ₽" % (nm, d["sectorShape"]))
    cap = ("₽%s bn" % _mny(d["marketCapB"])) if d.get("marketCapB") else "n/a"
    _csym = _cur_sym(d.get("issCurrency"))
    L.append("market cap %s  ·  shares %s  ·  spot %s"
             % (cap, format(d["sharesOut"], ",.0f").replace(",", " ") if d.get("sharesOut") else "n/a",
                ("%s%s" % (_csym, _rat(d["spot"], 2))) if d.get("spot") else "n/a"))
    L.append("")
    ltm = d.get("ltm") or {}
    if d["sectorShape"] == "bank":
        L.append("LTM  net income ₽%s bn  ·  ROE %s  ·  P/E %s  ·  P/BV %s"
                 % (_mny(ltm.get("netIncome")), _pct(ltm.get("roe")), _rat(ltm.get("pe")), _rat(ltm.get("pbv"))))
    else:
        L.append("LTM  revenue ₽%s bn  ·  EBITDA ₽%s bn  ·  net income ₽%s bn"
                 % (_mny(ltm.get("revenue")), _mny(ltm.get("ebitda")), _mny(ltm.get("netIncome"))))
        L.append("LTM  P/E %s  ·  EV/EBITDA %s  ·  P/S %s  ·  ROE %s  ·  net margin %s"
                 % (_rat(ltm.get("pe")), _rat(ltm.get("evEbitda")), _rat(ltm.get("ps")),
                    _pct(ltm.get("roe")), _pct(ltm.get("netMargin"))))
    L.append("")
    qs = d.get("quarters") or []
    if qs:
        if d["sectorShape"] == "bank":
            L.append("%-8s %12s %8s" % ("PERIOD", "NET_INC(bn)", "ROE"))
            for q in qs:
                L.append("%-8s %12s %8s" % (q.get("period", "?"), _mny(q.get("netIncome")), _pct(q.get("roe"))))
        else:
            L.append("%-8s %11s %10s %11s" % ("PERIOD", "REV(bn)", "EBITDA", "NET_INC"))
            for q in qs:
                L.append("%-8s %11s %10s %11s"
                         % (q.get("period", "?"), _mny(q.get("revenue")), _mny(q.get("ebitda")), _mny(q.get("netIncome"))))
    yt = d.get("yahooTTM")
    if yt:
        L.append("")
        L.append("Yahoo TTM cross-check (%s): revenue %s · net income %s · net margin %s · ROE %s"
                 % (yt.get("currency"), _mny(yt.get("revenueB")), _mny(yt.get("netIncomeB")),
                    _pct(yt.get("netMargin")), _pct(yt.get("roe"))))
        cc = d.get("crossCheck")
        if cc:
            L.append("  %s cross-check: smart-lab ₽%s bn vs Yahoo ₽%s bn — %s"
                     % (cc["metric"], _mny(cc["smartlabLtmB"]), _mny(cc["yahooTtmB"]),
                        "consistent" if cc["agree"] else "DIVERGENT (check units/parse)"))
    dv = d.get("dividends") or []
    if dv:
        last = dv[-1]
        L.append("")
        L.append("last dividend (ISS): %s %s%s" % (last["date"], _rat(last["value"], 2), " " + (last.get("currency") or "")))
    L.append("")
    L.append("source: %s · MOEX ISS (price/shares/cap/dividends) · data analysis, not investment advice" % _SL_SOURCE)
    return "\n".join(L)


# ----------------- reciprocal dashboard link -----------------
def _opt_links(html_path, sym, bg=False, _spawn=None, options_fname=None):
    """Create/link the MOEX --full options sibling; embed it synchronously in Cowork."""
    fname, sib = dashboard.sibling_target(
        html_path,
        options_fname,
        sym.lower() + "_options.html",
        sym.lower() + "_options_sibling.html",
    )
    label = "%s options ↗" % sym
    links = dashboard.sibling_links(html_path, [(fname, label)])
    if not bg or os.environ.get("MARKET_DATA_NO_SIBLING"):
        return links, None
    cw = dashboard.in_cowork()
    if links and not cw:
        return links, None
    here = os.path.dirname(os.path.abspath(__file__))
    parent_fname = dashboard.sibling_basename(os.path.basename(html_path), sym.lower() + ".html")
    argv = [sys.executable, os.path.join(here, "moex_options.py"), sym, "--full", "--html", sib,
            "--fundamentals-link", parent_fname]
    if cw:
        emit = os.path.join(os.path.dirname(sib), ".%s.%d.spec.json" % (fname[:-5], os.getpid()))
        ok_child = (_spawn or dashboard.render_sync)(argv + ["--emit-spec", emit])
        sspec = None
        if ok_child:
            try:
                with open(emit, encoding="utf-8") as f:
                    sspec = json.load(f)
            except (OSError, ValueError):
                pass
        try:
            os.unlink(emit)
        except OSError:
            pass
        if sspec is not None:
            return [], {"label": "%s options" % sym, "backLabel": "%s fundamentals" % sym,
                        "fname": fname, "spec": sspec}
        if ok_child:
            return dashboard.sibling_links(html_path, [(fname, label)]) or [], None
        return [], None
    if (_spawn or dashboard.spawn_bg)(argv):
        print("note: rendering %s in the background — the '%s' chip appears when it lands" % (sib, label),
              file=sys.stderr)
        return [{"text": label, "href": fname, "pending": True}], None
    return [], None


# ----------------- CLI -----------------
def main(argv=None):
    ap = argparse.ArgumentParser(description="Russian MOEX-listed company fundamentals (smart-lab primary, "
                                             "Yahoo TTM cross-check, MOEX ISS market wrapper). Numbers module — "
                                             "no dashboard this slice.")
    ap.add_argument("symbol")
    ap.add_argument("--json", action="store_true", help="emit the structured record as compact JSON")
    ap.add_argument("--html", metavar="PATH",
                    help="write the self-contained RESPONSIVE dashboard (tabs: Overview · Financials · "
                         "Dividends) — pulls the extra price/vol sources (MOEX ISS candles + the vol store)")
    ap.add_argument("--summary-out", metavar="PATH",
                    help="also write the compact text summary from this SAME gather")
    ap.add_argument("--emit-spec", metavar="PATH", default=None,
                    help="also dump the dashboard SPEC as JSON (the sibling-embed seam)")
    ap.add_argument("--options-link", help=argparse.SUPPRESS)
    ap.add_argument("--save-raw", metavar="DIR",
                    help="save the raw smart-lab HTML + Yahoo JSON + ISS payloads to DIR while fetching; "
                         "re-render later with --from-dir DIR (offline, 0 network)")
    ap.add_argument("--from-dir", help="rebuild from a --save-raw directory (offline replay)")
    ap.add_argument("--no-yahoo", action="store_true", help="skip the Yahoo cross-check fetch")
    ap.add_argument("--no-annual", action="store_true", help="skip the /f/y/ annual page fetch")
    a = ap.parse_args(argv)
    sym = a.symbol.upper()
    _dash = bool(a.html or a.emit_spec)
    if a.from_dir:
        d = read_from_dir(sym, a.from_dir)
        if a.save_raw:
            print("warning: --save-raw is ignored with --from-dir (nothing was fetched)", file=sys.stderr)
        if _dash:   # offline replay carries no price/vol payloads — the board renders without those panels
            print("note: --from-dir has no price/vol sources saved — the chart and vol panels are omitted",
                  file=sys.stderr)
    elif _dash:
        # the dashboard path additionally pulls prices (kline/52w/DMA/drawdown/beta) + the vol store
        d = gather_dashboard(sym, save_dir=a.save_raw, want_annual=not a.no_annual,
                             want_yahoo=not a.no_yahoo)
    else:
        d = gather(sym, save_dir=a.save_raw, want_annual=not a.no_annual, want_yahoo=not a.no_yahoo)
    base_spec = to_spec(d) if _dash else None
    if a.emit_spec:
        dashboard.write_atomic(a.emit_spec, json.dumps(base_spec, ensure_ascii=False))
    if a.summary_out:
        dashboard.write_atomic(a.summary_out, render_text(d) + "\n")
    if a.html:
        if not a.from_dir and not os.environ.get("MARKET_DATA_NO_SIBLING"):
            dashboard.write_atomic(a.html, render_html(base_spec))
        links, sibling = _opt_links(a.html, sym, bg=not a.from_dir, options_fname=a.options_link)
        spec = dict(base_spec)
        if links:
            spec["links"] = links
        if sibling:
            spec["sibling"] = sibling
        # the NVTK session 2026-07-24 closed with NEITHER card — a NEW read path shipped and the
        # closing step did not travel with it. :sector is a COMPLEMENT, not a default second card
        # (user 2026-07-24): it earns a place only for a genuine commodity producer.
        dashboard.deliver_html(a.html, render_html(spec),
                               offers=["/equity-research:earnings %s <Qn FYnn>" % sym,
                                       "/equity-research:earnings-preview " + sym,
                                       "/financial-analysis:dcf " + sym,
                                       "/equity-research:sector <complex> — SECOND card only for a "
                                       "metals/oil/mining producer, never for a bank or retailer"])
        print("%s  |  %s fundamentals dashboard (MOEX)" % (a.html, sym))
        if sibling:
            dashboard.write_reverse_sibling(sibling, base_spec, a.html)
    elif a.json:
        print(json.dumps(d, separators=(",", ":"), ensure_ascii=False))
    else:
        print(render_text(d))


if __name__ == "__main__":
    main()
