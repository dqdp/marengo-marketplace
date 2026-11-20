#!/usr/bin/env python3
"""
setup_check.py — onboarding doctor for the market-data skill.

Verifies, on THIS machine, everything chart.py / greeks.py need, and prints a
concrete fix for anything missing. Read-only and offline-safe (the network probe
is a few tiny HEAD-style GETs you can skip with --no-net). Pure stdlib, no installs.

  python3 scripts/setup_check.py            # full check (incl. network reachability)
  python3 scripts/setup_check.py --no-net   # skip the network probe

Exit code 0 = ready (warnings allowed), 1 = a hard blocker was found.
See SETUP.md for the full platform-by-platform walkthrough.
"""
from __future__ import annotations
import argparse, os, platform, shutil, subprocess, sys, tempfile, urllib.request
from xml.sax.saxutils import escape as _xesc   # escape skill paths (may contain & < >) in plist / Task XML

HOSTS = {  # source -> (probe URL, what it powers)
    "alphavantage.co": ("https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol=IBM",
                         "av-equity / av-crypto charts"),
    "hist.databento.com": ("https://hist.databento.com/v0/metadata.get_dataset_range?dataset=GLBX.MDP3",
                           "databento (CME futures) charts"),
    "iss.moex.com": ("https://iss.moex.com/iss/index.json", "moex (Russian markets) charts — keyless"),
}
GREEN, YEL, RED, OFF = "\033[32m", "\033[33m", "\033[31m", "\033[0m"
if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
    GREEN = YEL = RED = OFF = ""

hard_fail = False


def line(level, msg, fix=""):
    global hard_fail
    tag = {"ok": f"{GREEN}OK  {OFF}", "warn": f"{YEL}WARN{OFF}", "fail": f"{RED}FAIL{OFF}"}[level]
    if level == "fail":
        hard_fail = True
    print(f"  {tag}  {msg}")
    if fix:
        for ln in fix.strip("\n").split("\n"):
            print(f"        {ln}")


def skill_dir():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def env_path():
    here = skill_dir()
    for p in (os.path.join(here, ".env"), os.path.join(os.getcwd(), ".env")):
        if os.path.exists(p):
            return p
    return None


def read_env_keys(path):
    keys = set()
    try:
        for ln in open(path, encoding="utf-8"):
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1)
                if v.strip():
                    keys.add(k.strip())
    except OSError:
        pass
    return keys


def av_key_in_file():
    """True if ALPHAVANTAGE_API_KEY is persisted in a .env the SCHEDULED job can read. The
    launchd/schtasks prewarm runs WITHOUT the interactive shell environment, so a key that lives
    only in `export ALPHAVANTAGE_API_KEY=...` is invisible to it — the nightly run then dies
    'key not found' and the cache silently never warms. Only a .env beside SKILL.md (or cwd)
    reaches the job (options.get_key reads it by absolute path)."""
    p = env_path()
    return bool(p and "ALPHAVANTAGE_API_KEY" in read_env_keys(p))


def _warn_prewarm_key():
    """Warn (once, at install / in the doctor) when the scheduled prewarm won't be able to
    authenticate because the AV key is env-only, not in a .env the job can read."""
    if not av_key_in_file():
        line("warn", "ALPHAVANTAGE_API_KEY not in a .env — the scheduled prewarm can't authenticate",
             "The nightly job runs without your shell environment, so an env-only key is invisible\n"
             "to it (it would exit 'key not found'). Persist it: put ALPHAVANTAGE_API_KEY=<key> in a\n"
             ".env beside SKILL.md.")


def check_python():
    v = sys.version_info
    if v < (3, 7):
        line("fail", f"Python {v.major}.{v.minor} — too old (need 3.7+)",
             "Install Python 3.8+ from python.org (Windows: check 'Add to PATH').")
    else:
        line("ok", f"Python {v.major}.{v.minor}.{v.micro} on {platform.system()}")


def check_keys():
    p = env_path()
    if not p:
        line("warn", "no .env found (skill folder or cwd)",
             "MOEX works without any key. For Alpha Vantage / Databento charts, create\n"
             ".env next to SKILL.md with: ALPHAVANTAGE_API_KEY=... and DATABENTO_API_KEY=...\n"
             "(.env.example shows the format). Never put keys on the CLI or in chat.")
        return
    keys = read_env_keys(p)
    rel = os.path.relpath(p)
    line("ok", f".env found ({rel})")
    for k, who in (("ALPHAVANTAGE_API_KEY", "av-equity / av-crypto"), ("DATABENTO_API_KEY", "databento")):
        if k in keys:
            line("ok", f"{k} set — {who} enabled")
        else:
            line("warn", f"{k} missing — {who} charts will error until set",
                 f"Add {k}=<your key> to {rel}")
    line("ok", "MOEX needs no key (keyless public ISS API)")


def check_network(skip):
    if skip:
        line("warn", "network probe skipped (--no-net)")
        return
    print("  ---- network egress (reachable vs sandbox-blocked) ----")
    blocked = []
    for host, (url, who) in HOSTS.items():
        try:
            req = urllib.request.Request(url, method="GET", headers={"User-Agent": "market-data-setup-check"})
            with urllib.request.urlopen(req, timeout=8) as r:
                r.read(64)
            line("ok", f"{host} reachable — {who}")
        except urllib.error.HTTPError as e:
            # an HTTP status (401/403/etc.) still means the host is REACHABLE
            line("ok", f"{host} reachable (HTTP {e.code}) — {who}")
        except Exception as e:  # noqa: BLE001 — URLError, timeout, DNS, etc. = blocked
            blocked.append(host)
            line("warn", f"{host} NOT reachable ({type(e).__name__}) — {who}")
    if blocked:
        line("warn", f"{len(blocked)} host(s) blocked — likely a sandbox egress policy",
             "If this is a LOCAL Claude Code session: add the hosts to\n"
             "  sandbox.network.allowedDomains in ~/.claude/settings.json\n"
             "If this is agent-mode / Home (a managed full-VM sandbox), that setting may not\n"
             "apply — either run charts in a local session, or use chart.py --from (pull the\n"
             "series via the MCP connector, save CSV/JSON, render locally). See SETUP.md.")


def check_png():
    # SVG always works; PNG is only for inline preview.
    if shutil.which("rsvg-convert"):
        line("ok", "PNG preview: rsvg-convert found (fast, any OS)")
    elif platform.system() == "Darwin" and shutil.which("qlmanage"):
        line("ok", "PNG preview: qlmanage+sips (macOS built-in)")
    elif any(shutil.which(b) for b in ("chromium", "google-chrome", "chrome", "msedge")):
        line("ok", "PNG preview: headless Chrome/Edge (slower fallback)")
    else:
        line("warn", "no SVG->PNG converter — SVG still renders; --png/inline preview won't",
             "Optional: install rsvg-convert (brew/apt/choco install librsvg) or just open the .svg\n"
             "with --open. Chart generation itself needs none of these.")


_FIN_PLUGINS = ("equity-research", "financial-analysis")


def _openai_package(_root=None):
    import json
    marker = os.path.join(_root or skill_dir(), "market-data-host.json")
    try:
        with open(marker, encoding="utf-8") as handle:
            return json.load(handle).get("host") == "openai"
    except Exception:
        return False


def _openai_companion_installed(_cache=None):
    cache = _cache or os.path.expanduser(
        "~/.codex/plugins/cache/personal/financial-deep-analysis"
    )
    direct = os.path.join(cache, ".codex-plugin", "plugin.json")
    if os.path.isfile(direct):
        return True
    try:
        versions = os.listdir(cache)
    except OSError:
        return False
    return any(
        os.path.isfile(os.path.join(cache, version, ".codex-plugin", "plugin.json"))
        for version in versions
    )


def check_plugins(_path=None, _root=None, _openai_cache=None):
    if _openai_package(_root):
        if _openai_companion_installed(_openai_cache):
            line(
                "ok",
                "financial-deep-analysis companion plugin present "
                "(OpenAI deeper-analysis offers)",
            )
        else:
            line(
                "warn",
                "financial-deep-analysis companion plugin missing — "
                "deeper-analysis offers will not be clickable",
                "Install the separately packaged public plugin:\n"
                "  codex plugin add financial-deep-analysis@personal\n"
                "Then start a new ChatGPT Work or Codex task.",
            )
        return

    # The neighbor Anthropic plugins (equity-research + financial-analysis, from the
    # claude-for-financial-services marketplace) are the DOCUMENT layer the market-data OFFER workflow
    # routes into (/equity-research:earnings, /financial-analysis:comps, ...). They install SEPARATELY —
    # the skill zip can't run the plugin CLI — so a fresh machine won't have them. Optional (the skill
    # works without them), but flag it so the deeper-analysis offers actually resolve.
    import json
    installed = set()
    try:
        with open(_path or os.path.expanduser("~/.claude/plugins/installed_plugins.json"), encoding="utf-8") as f:
            for key, recs in (json.load(f).get("plugins") or {}).items():
                # v1.125: a non-str key must not abort the whole scan (false "missing"), and a
                # registry key with an EMPTY record list is a stale entry, not an install
                if isinstance(key, str) and recs:
                    installed.add(key.split("@", 1)[0])
    except Exception:
        pass
    missing = [p for p in _FIN_PLUGINS if p not in installed]
    if not missing:
        line("ok", "neighbor plugins present — equity-research + financial-analysis (deeper-analysis offers)")
    else:
        line("warn", "neighbor plugins missing (%s) — deeper-analysis document offers won't resolve" % ", ".join(missing),
             "Optional — the DOCUMENT layer the offer workflow routes into. One idempotent command\n"
             "(installs both plugins + mirrors their skills into local Cowork snapshots):\n"
             "  bash %s/install_plugins.sh" % os.path.dirname(os.path.abspath(__file__)))


def check_shadowed_refs(_root=None):
    # v1.125 (install hygiene): `unzip -o` reinstalls never DELETE — pre-v1.6x layouts left the
    # reference playbooks at the SKILL ROOT (options.md, fundamentals.md, …) beside the current
    # references/ copies, and an agent that opens the root copy follows a months-old playbook
    # (found live 2026-07-23: 7 stale root .md + a v1.57 CLAUDE.md in the installed skill).
    root = _root or skill_dir()
    refdir = os.path.join(root, "references")
    try:
        refs = {f for f in os.listdir(refdir) if f.endswith(".md")}
    except OSError:
        return                      # no references/ (broken install) — other checks flag that
    try:
        stale = sorted(f for f in os.listdir(root)
                       if f.endswith(".md") and f in refs and os.path.isfile(os.path.join(root, f)))
    except OSError:
        return
    if stale:
        line("warn", "stale unzip leftovers shadow references/ — " + ", ".join(stale),
             "These root-level copies predate the references/ layout and mislead agents.\n"
             "Delete them:  rm " + " ".join('"%s"' % os.path.join(root, f) for f in stale))
    else:
        line("ok", "no root-level .md shadows references/")


def check_tzdata():
    # The equity session tile + intraday timestamps convert through the IANA tz America/New_York.
    # Bare Windows ships NO tz database, so the skill degrades to a fixed UTC-5 offset (labels read
    # 'UTC-05:00', DST may drift 1h). macOS/Linux bundle the db; on Windows `pip install tzdata` restores it.
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo("America/New_York")
        line("ok", "IANA tz database present — session/intraday show correct ET (EDT/EST) labels")
    except Exception:
        line("warn", "no IANA tz database — ET labels fall back to a fixed UTC-5 (may drift 1h under DST)",
             "The skill still works — the session tile renders, only the tz label + DST are approximate.\n"
             "Optional on Windows: pip install tzdata  — restores correct EDT/EST labels and DST.")


def check_scripts():
    here = os.path.dirname(os.path.abspath(__file__))
    # options.py / futures_options.py import dashboard + opthist at MODULE TOP, so a missing engine file fails
    # every options run while the rest of the doctor still prints OK — check them too.
    for name in ("chart.py", "fundamentals.py", "portfolio.py", "options.py", "greeks.py", "databento_live.py",
                 "dashboard.py", "opthist.py", "futures_options.py", "moex_options.py", "moexopthist.py",
                 "moex_fundamentals.py", "moex_bonds.py", "speccheck.py",
                 "treasury.py"):
        if os.path.exists(os.path.join(here, name)):
            line("ok", f"scripts/{name} present")
        else:
            line("fail", f"scripts/{name} MISSING — incomplete install",
                 "Re-unzip the skill so scripts/ is complete.")
    # options history / prewarm seeds its universe from this file (opthist.load_universe, __file__-relative);
    # missing -> the nightly prewarm silently degrades to accreted-only (Overview/History warm slower).
    if os.path.exists(os.path.join(here, "universe_options.txt")):
        line("ok", "scripts/universe_options.txt present (nightly options-prewarm seed)")
    else:
        line("warn", "scripts/universe_options.txt missing — options prewarm degrades to accreted-only",
             "Re-unzip the skill (package.sh must copy scripts/*.txt).")
    # The --html dashboards inline this vendored Chart.js (no CDN → self-contained). Missing it
    # doesn't affect text/--svg/--json, but --html would error at render time.
    for lib, what in (("chart.umd.min.js", "--html dashboards"),
                      ("klinecharts.min.js", "the fundamentals price candles")):
        if os.path.exists(os.path.join(here, lib)):
            line("ok", f"scripts/{lib} present (inlined into --html — self-contained, no CDN)")
        else:
            line("warn", f"scripts/{lib} MISSING — {what} will error (other modes fine)",
                 "Re-unzip the skill so scripts/ is complete.")


PREWARM_LABEL = "com.market-data.optcache-prewarm"
PREWARM_TASK = "MarketDataOptCachePrewarm"
WINDOWS_TASK_TIMEOUT_S = 30


def _prewarm_argv():
    utf8 = ["-X", "utf8"] if platform.system() == "Windows" else []
    return [sys.executable, *utf8, os.path.join(skill_dir(), "scripts", "options.py"), "--prewarm"]


def _plist_path():
    return os.path.expanduser("~/Library/LaunchAgents/%s.plist" % PREWARM_LABEL)


def _prewarm_log():
    return os.path.expanduser("~/.cache/market-data/prewarm.log")


def _win_task_xml(argv, hour):
    args = " ".join('"%s"' % _xesc(a) for a in argv[1:])
    return ('<?xml version="1.0" encoding="UTF-16"?>\n'
            '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
            '  <Triggers><CalendarTrigger>\n'
            '    <StartBoundary>2026-01-01T%02d:00:00</StartBoundary><Enabled>true</Enabled>\n'
            '    <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>\n'
            '  </CalendarTrigger></Triggers>\n'
            '  <Settings><StartWhenAvailable>true</StartWhenAvailable>\n'
            '    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n'
            '    <ExecutionTimeLimit>PT30M</ExecutionTimeLimit>\n'
            '    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries></Settings>\n'
            '  <Actions><Exec><Command>%s</Command><Arguments>%s</Arguments></Exec></Actions>\n'
            '</Task>\n' % (hour, _xesc(argv[0]), args))


def _run_schtasks(args):
    """Run one Task Scheduler command without letting Windows onboarding hang forever."""
    try:
        return subprocess.run(
            ["schtasks"] + list(args),
            capture_output=True,
            text=True,
            timeout=WINDOWS_TASK_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None


def install_prewarm(hour=4):
    """Schedule the nightly `options.py --prewarm` — launchd (macOS) / Task Scheduler (Windows).
    Both catch up a missed run on wake (StartCalendarInterval / StartWhenAvailable)."""
    argv, sysname = _prewarm_argv(), platform.system()
    if sysname == "Darwin":
        os.makedirs(os.path.dirname(_plist_path()), exist_ok=True)
        os.makedirs(os.path.dirname(_prewarm_log()), exist_ok=True)
        pa = "".join("    <string>%s</string>\n" % _xesc(a) for a in argv)
        plist = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                 '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                 '<plist version="1.0"><dict>\n'
                 '  <key>Label</key><string>%s</string>\n'
                 '  <key>ProgramArguments</key><array>\n%s  </array>\n'
                 '  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>%d</integer>'
                 '<key>Minute</key><integer>0</integer></dict>\n'
                 '  <key>StandardOutPath</key><string>%s</string>\n'
                 '  <key>StandardErrorPath</key><string>%s</string>\n'
                 '  <key>RunAtLoad</key><false/>\n</dict></plist>\n'
                 % (PREWARM_LABEL, pa, hour, _prewarm_log(), _prewarm_log()))
        with open(_plist_path(), "w") as f:
            f.write(plist)
        subprocess.run(["launchctl", "unload", _plist_path()], capture_output=True)
        r = subprocess.run(["launchctl", "load", _plist_path()], capture_output=True, text=True)
        if r.returncode == 0:
            line("ok", "prewarm scheduled via launchd — %02d:00 daily (catches up on wake)" % hour,
                 "plist: %s\nlog:   %s\nremove: python3 scripts/setup_check.py --uninstall-prewarm" % (_plist_path(), _prewarm_log()))
            _warn_prewarm_key()
        else:
            line("fail", "launchctl load failed", (r.stderr or "").strip()[:200])
    elif sysname == "Windows":
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(suffix=".xml")
            with os.fdopen(fd, "w", encoding="utf-16") as f:
                f.write(_win_task_xml(argv, hour))
            r = _run_schtasks(["/create", "/tn", PREWARM_TASK, "/xml", tmp, "/f"])
        finally:
            if tmp:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        if r is None:
            line("fail", "schtasks /create timed out or is unavailable")
        elif r.returncode == 0:
            line("ok", "prewarm scheduled via Task Scheduler — %02d:00 daily, %s (runs missed on wake)" % (hour, PREWARM_TASK),
                 "remove: python scripts\\setup_check.py --uninstall-prewarm")
            _warn_prewarm_key()
        else:
            line("fail", "schtasks /create failed", (r.stderr or r.stdout or "").strip()[:200])
    else:
        line("warn", "auto-scheduling is macOS (launchd) / Windows (Task Scheduler) only",
             "run nightly yourself (cron): %s" % " ".join(argv))


def uninstall_prewarm():
    sysname = platform.system()
    if sysname == "Darwin":
        subprocess.run(["launchctl", "unload", _plist_path()], capture_output=True)
        try:
            os.remove(_plist_path())
            line("ok", "prewarm launchd job removed")
        except OSError:
            line("warn", "no prewarm launchd job found")
    elif sysname == "Windows":
        r = _run_schtasks(["/delete", "/tn", PREWARM_TASK, "/f"])
        if r is None:
            line("warn", "schtasks /delete timed out or is unavailable")
        else:
            line("ok" if r.returncode == 0 else "warn",
                 "prewarm task removed" if r.returncode == 0 else "no prewarm task found")
    else:
        line("warn", "nothing to remove on this platform")


def check_prewarm():
    print("  ---- prewarm schedule (nightly cache refresh) ----")
    sysname = platform.system()
    if sysname == "Darwin":
        if os.path.exists(_plist_path()):
            line("ok", "prewarm scheduled (launchd %s)" % PREWARM_LABEL)
            _warn_prewarm_key()
        else:
            line("warn", "prewarm not scheduled (optional)", "install: python3 scripts/setup_check.py --install-prewarm")
    elif sysname == "Windows":
        r = _run_schtasks(["/query", "/tn", PREWARM_TASK])
        if r is None:
            line("warn", "Task Scheduler query timed out or is unavailable")
        elif r.returncode == 0:
            line("ok", "prewarm scheduled (Task Scheduler %s)" % PREWARM_TASK)
            _warn_prewarm_key()
        else:
            line("warn", "prewarm not scheduled (optional)", "install: python scripts\\setup_check.py --install-prewarm")
    else:
        line("warn", "prewarm auto-scheduling not supported on %s" % sysname)


def main():
    ap = argparse.ArgumentParser(description="Onboarding doctor for the market-data skill")
    ap.add_argument("--no-net", action="store_true", help="skip the network reachability probe")
    ap.add_argument("--install-prewarm", action="store_true", help="schedule the nightly options cache prewarm (launchd/Task Scheduler)")
    ap.add_argument("--uninstall-prewarm", action="store_true", help="remove the scheduled prewarm job")
    a = ap.parse_args()

    if a.install_prewarm:
        install_prewarm()
        sys.exit(1 if hard_fail else 0)
    if a.uninstall_prewarm:
        uninstall_prewarm()
        sys.exit(0)

    print(f"\nmarket-data skill — setup check\n  skill dir: {skill_dir()}\n")
    check_python()
    check_scripts()
    check_keys()
    check_png()
    check_tzdata()
    check_shadowed_refs()
    check_plugins()
    check_network(a.no_net)
    check_prewarm()

    print()
    if hard_fail:
        print(f"{RED}A hard blocker was found — fix the FAIL line(s) above.{OFF}\n")
        sys.exit(1)
    print(f"{GREEN}Ready.{OFF} WARN lines are optional/source-specific — see SETUP.md for details.\n")
    sys.exit(0)


if __name__ == "__main__":
    main()
