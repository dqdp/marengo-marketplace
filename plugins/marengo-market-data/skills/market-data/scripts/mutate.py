#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mutate.py — a mutation runner: does the suite actually CATCH the defect it claims to guard?

Why this exists. Across four review rounds on one module, the reviewers' single most common finding
was not a bug in the code but a **vacuous test** — an assertion that passed whether or not the code
was right (4 in round 1, 9 in round 2). Every time a mutation was run by hand, at least one survived
and exposed one. Doing that by hand does not scale and does not persist.

How it works. Each entry names ONE invariant, the exact source edit that breaks it, and the suite
that must then fail. The runner applies the edit, runs the suite, restores the file, and reports:

  KILLED    the suite failed — the invariant is genuinely pinned
  SURVIVED  the suite still passed — the test for that invariant is vacuous or missing
  STALE     the `find` string is no longer in the source — the catalogue entry needs updating

A SURVIVED line is the finding. A STALE line is maintenance, not a pass.

This is NOT part of the per-run gate (it re-runs whole suites, ~1s each). It belongs to the ship
checklist, beside the review round — cheap enough to run before every ship, and it answers a
question the gate cannot: "would these tests notice if the code were wrong?"

Run:
    python3 scripts/mutate.py                 # everything
    python3 scripts/mutate.py moex_bonds      # one module's entries
    python3 scripts/mutate.py --list
"""
from __future__ import annotations
import os
import signal
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# (module, suite, why, find, replace)
#   module — the FILE that gets mutated (without .py)
#   suite  — the test suite that must fail (without test_ / .py)
# Every entry below was verified by hand at least once: it killed its suite when introduced.
CATALOGUE = [
    # ---- moex_bonds: honesty gates that four review rounds were spent establishing ----
    ("moex_bonds", "moex_bonds", "the principal must not count as coupon income",
     'coupon_1y = sum(cf for t, cf, k in cfs if t <= 1.0 and k == "coupon")',
     'coupon_1y = sum(cf for t, cf, k in cfs if t <= 1.0)'),
    ("moex_bonds", "moex_bonds", "a yield percentile needs a MATDATE basis",
     'if ytm.get("toType") != "MATDATE" or not ytm.get("basisKnown", True):',
     'if False:'),
    ("moex_bonds", "moex_bonds", "yield-to-offer is refused by TYPE, before the price oracle",
     'if (d["ytm"] or {}).get("toType") == "OFFER":',
     'if False:'),
    ("moex_bonds", "moex_bonds", "the price oracle gates the risk analytics",
     '"ok": fit is not None and fit <= _RISK_TOL_PCT}', '"ok": True}'),
    ("moex_bonds", "moex_bonds", "face and settlement currency are never summed",
     'if (d.get("settleCurrency") or d.get("faceUnit")) != d.get("faceUnit"):\n        return []',
     'if False:\n        return []'),
    ("moex_bonds", "moex_bonds", "a floater's rate divides by the OUTSTANDING face",
     'face = s.get("faceAfter") or d.get("faceOriginal") or d.get("face")',
     'face = d.get("faceOriginal") or d.get("face")'),
    ("moex_bonds", "moex_bonds", "the issuer search is filtered by emitent_id",
     'if str(r.get("emitent_id") or "").strip() == eid and r.get("secid"):',
     'if r.get("secid"):'),
    ("moex_bonds", "moex_bonds", "the sovereign leg uses THAT DAY's duration",
     'dur = row.get("durationYears") or cur_dur', 'dur = cur_dur'),
    ("moex_bonds", "moex_bonds", "the history pull is board-scoped",
     'seg = ("/boards/%s" % board) if board else ""', 'seg = ""'),
    ("moex_bonds", "moex_bonds", "unknown payments are not called identical",
     'return None          # not "level"', 'return False          # not "level"'),

    # ---- the shared engine: these affect every dashboard ----
    ("dashboard", "dashboard", "bubble honours an explicit per-point colour",
     'backgroundColor:p.color||PALETTE[i%PALETTE.length]', 'backgroundColor:PALETTE[i%PALETTE.length]'),
    ("dashboard", "dashboard", "line honours a per-series spanGaps",
     'spanGaps:!!s.spanGaps,', ''),
    ("dashboard", "dashboard", "the card row is measured, not @media'd",
     'mw>=940?5:mw>=690?4:mw>=450?3:2', '5'),

    # ---- speccheck itself: a checker nobody tests is a checker nobody trusts ----
    ("speccheck", "speccheck", "invisibility is judged by SEGMENTS, not by point count",
     'n, seg = _nonnull(s.get("values")), _segments(s.get("values"))\n                    if n and not seg',
     'n, seg = _nonnull(s.get("values")), _segments(s.get("values"))\n                    if n < 3'),
    ("speccheck", "speccheck", "format rot matches whole words, not substrings",
     '(?<![A-Za-z])(None|nan|NaN|inf|Infinity|undefined)(?![A-Za-z])',
     '(None|nan|NaN|inf|Infinity|undefined)'),
    ("speccheck", "speccheck", "text panels are exempt from EMPTY_PANEL",
     'if p.get("type") in _TEXTUAL:', 'if False:'),

    # ---- core modules: NOT yet verified to be caught. A SURVIVED line here is a real finding
    #      about the core suites, which is exactly why they are in the catalogue. ----
    ("moex_options", "moex_options", "a structurally-empty panel DECLARES itself (allowEmpty)",
     '{"type": "bars", "title": "Turnover (vol/OI) by strike", "allowEmpty": True,',
     '{"type": "bars", "title": "Turnover (vol/OI) by strike",'),
    ("options", "options", "the GEX panel declares allowEmpty (it is index-structural)",
     '{"type": "bars", "title": "Dealer gamma exposure (GEX) by strike", "allowEmpty": True,',
     '{"type": "bars", "title": "Dealer gamma exposure (GEX) by strike",'),
    ("moex_bonds", "moex_bonds", "the ZCYC slice keeps ONE parsed row, not the intraday block",
     'out[dd] = p          # the ONE parsed row, not the 22 000-row intraday block',
     'out[dd] = (blk or {}).get("params")'),
    ("speccheck", "speccheck", "an unparsed {columns,data} payload in a record is caught",
     'if "columns" in v and "data" in v and isinstance(v.get("data"), list):',
     'if False:'),
    ("fundamentals", "fundamentals", "the insider panel names its right axis",
     '"units": {"left": "$M", "right": "buyers"},', '"units": {"left": "$M"},'),

    # ---- v1.153: linked table rows (the group page -> an issue's own dashboard) ----
    ("dashboard", "dashboard", "a table row link must be RELATIVE (no scheme, no //host)",
     'if lk and not rel_href_ok((lk or {}).get("href")):\n            lk = None',
     'if False:\n            lk = None'),
    ("moex_bonds", "moex_bonds", "the members table never prints a yield without its basis",
     '                     _ytm_cell_cur(m),',
     '                     (_num(m["ytm"], 2) if m.get("ytm") is not None else "—"),'),

    # ---- review round 1 on the group page: 7 blocking findings, each pinned here ----
    ("moex_bonds", "moex_bonds", "the calendar aggregates only issues the page LISTS",
     'usable = [s for s in bz if s in mem_ids and s not in fx_ids]',
     'usable = [s for s in bz]'),
    ("moex_bonds", "moex_bonds", "a foreign-currency issue never joins the rouble calendar",
     'fx_ids = {m["secid"] for m in members if m.get("fx")}', 'fx_ids = set()'),
    ("moex_bonds", "moex_bonds", "a calendar row counts ISSUES, not cash-flow legs",
     '"n": len(by[k]["ids"])', '"n": 2 * len(by[k]["ids"])'),
    ("moex_bonds", "moex_bonds", "no board means an UNKNOWN population, never a printed 0",
     'if G.get("boardMissing"):', 'if False:'),
    ("moex_bonds", "moex_bonds", "the group medians use the CURVE's own predicate, not a looser one",
     'comparable = [m for m in members if not curve_gate(m, _cv)]',
     'comparable = list(_cc)'),
    ("moex_bonds", "moex_bonds", "the shared curve predicate keeps ONE yield basis",
     'if r.get("ytmTo") != "MATDATE":              # an ABSENT basis is not a maturity basis',
     'if False:'),
    ("moex_bonds", "moex_bonds", "a foreign-currency yield is marked with its currency",
     'if cur and cur not in _RUB:\n        cell += " (%s)" % cur', 'if False:\n        pass'),
    ("moex_bonds", "moex_bonds", "a linker's yield is marked REAL, not left beside nominal ones",
     'if r.get("linker"):\n        cell += " \u00b7 \u0440\u0435\u0430\u043b\u044c\u043d."',
     'if False:\n        pass'),
    ("moex_bonds", "moex_bonds", "the member pre-render is bounded",
     'for m in (G.get("members") or [])[:_GROUP_MEMBERS_MAX]:',
     'for m in (G.get("members") or []):'),
    ("moex_bonds", "moex_bonds", "a dead provider makes the pre-render GIVE UP",
     'if miss >= _GROUP_GIVEUP:', 'if False:'),
    ("moex_bonds", "moex_bonds", "a FAILED issuer search is not an empty one",
     '    if srch is None:\n', '    if False:\n'),
    ("moex_bonds", "moex_bonds", "a lossy cache name is disambiguated by digest",
     'safe = "%s-%s" % (safe, hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8])',
     'safe = safe'),
    # mutate.py itself CANNOT be catalogued here. Its `find` strings would also match the CATALOGUE
    # literal above, so the replace rewrites the entry instead of the code (that is precisely why an
    # attempted self-entry reported SURVIVED), and a mutated copy sweeps its own sidecar back at
    # startup anyway. Its invariants are pinned directly by test_speccheck.py.
    ("moex_bonds", "moex_bonds", "group panels are assigned to a tab EXPLICITLY, not by type",
     '"panels": _group_panels(G, hrefs, rendered)}]',
     '"panels": [x for x in _group_panels(G, hrefs, rendered) if x.get("type") != "table"]}]'),
    ("moex_bonds", "moex_bonds", "an issue page rendered from a group carries the way back",
     'if back and back.get("href") and dashboard.rel_href_ok(back["href"]):', 'if False:'),
    ("moex_bonds", "moex_bonds", "member_filename refuses a SECID that is not an identifier",
     'if not _SECID_RE.match(str(secid or "")):', 'if False:'),
    ("moex_bonds", "moex_bonds", "pre-rendering one member is best-effort, not all-or-nothing",
     '        except Exception:                                      # noqa: BLE001\n'
     '            bad.append(m["secid"])',
     '        except Exception:                                      # noqa: BLE001\n'
     '            raise'),

    # ---- v1.154: the US Treasury curve. The headline trap is a provider that answers ANY
    #      unsupported maturity with the 10-year series and no error at all. ----
    ("treasury", "treasury", "a leg whose `name` disagrees with the maturity asked for is REFUSED",
     'if not want or not str(payload.get("name") or "").startswith(want):', 'if False:'),
    # NOTE: "a `.` row is a hole" has no catalogue entry on purpose. It is enforced twice and
    # independently — the explicit `v == "."` guard, and the try/except around float() — so no
    # SINGLE-line mutation can break it (the attempt reported SURVIVED because the guard makes the
    # except unreachable for that input). Redundant defence, not a vacuous test; pinned in the suite.
    ("treasury", "treasury", "the page is dated by its DATA, not by the clock",
     'as_of = min((s[-1][0] for s in series.values()), default=None)',
     'as_of = (today or dt.date.today()).isoformat()'),
    ("treasury", "treasury", "a historical leg reports the date it GOT, not the one it wanted",
     'legs[lab] = {"date": got_date, "curve": leg,', 'legs[lab] = {"date": want, "curve": leg,'),
    # NOTE: "parse_series refuses a throttle envelope" has no entry — it is caught twice over (the
    # `name` check fails for a tenor, and the envelope carries no `data` at all), so no single-line
    # mutation expresses it. The invariant that MATTERS is that a throttled answer never reaches the
    # day cache, and that one is pinned below.
    ("treasury", "treasury", "a throttled answer is never written to the day cache",
     'if is_throttled(blk):', 'if False:'),
    ("treasury", "treasury", "the slope verdict is taken at the page's own as_of",
     'hist = [x for x in hist if not as_of or x[0] <= as_of]', 'hist = list(hist)'),
    ("treasury", "treasury", "a stale tenor cannot join a historical leg silently",
     'if pt and (dt.date.fromisoformat(want) - dt.date.fromisoformat(pt[0])).days <= _LEG_SLACK_DAYS:',
     'if pt:'),
    ("treasury", "treasury", "a dropped tenor reports its REAL reason, not always substitution",
     '"empty" if got.startswith(_want) else "substituted")',
     '"substituted" if got else "empty")'),
    ("treasury", "treasury", "the headline number falls back when the 10-year is the missing one",
     'for key, _y, lab, _n in TENORS:\n        if key in d["curve"]:\n            return _num(d["curve"][key]) + "%", lab',
     'for key, _y, lab, _n in TENORS:\n        if False:\n            return _num(d["curve"][key]) + "%", lab'),
    ("dashboard", "dashboard", "xYear picks one tick per YEAR instead of deduping neighbours",
     'if(xAx&&d.xYear&&!d.xall&&yrIdx.length) xAx.afterBuildTicks=',
     ''),

    # ---- v1.157: the closing-step note. The whole slice exists because PROSE instructions did
    #      not fire (ZW + PA, 2026-07-30, on a current v1.154.0 page), so the tests that pin the
    #      note must themselves be non-vacuous. ----
    ("dashboard", "dashboard", "the closing note goes to STDERR, never stdout",
     'file=stream if stream is not None else sys.stderr)',
     'file=stream if stream is not None else sys.stdout)'),
    # stdout is the producer's text report and IS redirected to a log on the futures golden path —
    # a note there would land in the very file it exists to pull the agent out of.
    ("dashboard", "dashboard", "the note demands PRESENT before the written read",
     '"  1. PRESENT %s NOW (Cowork present_files / the file card) — before the written"',
     '"  1. Ship %s NOW (the Cowork file card) — before the written"'),
    ("dashboard", "dashboard", "the note carries the producer's own offer command",
     'cmds = " | ".join(offers)', 'cmds = ""'),
    ("dashboard", "dashboard", "deliver_html writes atomically, never through a plain open(w)",
     'write_atomic(path, html)', 'open(path, "w", encoding="utf-8").write(html)'),
    ("dashboard", "dashboard", "the OpenAI package suppresses legacy Claude closing steps",
     'if resolved_host != "openai":', 'if True:'),
    # NOTE: "--skill-available is named" has NO entry on purpose. The flag appears twice in the
    # note (the invocation line AND the sentence explaining what its absence costs), so no
    # single-line mutation can express it — the same redundant-defence case as the treasury
    # `.`-hole. It is pinned in the suite, not here.
    ("treasury", "dashboard", "a producer cannot write its page behind deliver_html's back",
     'dashboard.deliver_html(a.html, render_html(spec))',
     'dashboard.write_atomic(a.html, render_html(spec))'),
    # treasury is the probe because it has exactly ONE write site: the source-level guard checks
    # only that `deliver_html(` appears at all, so a producer with two sites would survive losing
    # one. That coarseness is deliberate (the guard cannot know a producer's site count) — this
    # entry proves the guard fires at all, which is what stops a NEW read path shipping unwired.

    # ---- v1.158: the review of the ChatGPT/Codex distribution merge (PR #9) ----
    ("offer", "dashboard", "--lang reaches the prompt the button actually sends",
     '    return prompt + LANGUAGE_INSTRUCTIONS[lang] if lang else prompt',
     '    return prompt'),
    ("offer", "dashboard", "--lang rides the TYPED command too, not only the one-click prompt",
     '    print(dashboard.offer_widget(in_language(a.command, a.lang), a.label, ok, note=a.note))',
     '    print(dashboard.offer_widget(a.command, a.label, ok, note=a.note))'),
    ("futures_options", "futures_options", "the lease is a real OS lock, not a file that exists",
     '        try:\n            call()\n            return True',
     '        try:\n            return True'),
    ("futures_options", "futures_options", "a lock held on an unlinked inode is not exclusivity",
     '                held = os.fstat(fd).st_ino == os.stat(self.path).st_ino',
     '                held = True'),
    ("futures_options", "futures_options", "a filesystem that CANNOT lock is not 'someone holds it'",
     '            if e.errno in _LOCK_UNSUPPORTED:\n                return None',
     '            if False:\n                return None'),
    ("futures_options", "futures_options", "an unwritable lock path is a clean error, not an OSError",
     '            except OSError as e:\n                raise RuntimeError("cannot open the render lock',
     '            except FileExistsError as e:\n                raise RuntimeError("cannot open the render lock'),
    ("opthist", "opthist", "an archive that cannot be PLACED reports its cause",
     '            if not placed:                  # it IS in the archive and still did not arrive',
     '            if False:                       # it IS in the archive and still did not arrive'),
    ("test_speccheck", "speccheck", "the sweep tests never touch the repo's own scripts/",
     '_r = _msp.run([sys.executable, _isomut, "--sweep"], capture_output=True, text=True)',
     '_r = _msp.run([sys.executable, _mut.__file__, "--sweep"], capture_output=True, text=True)'),
    ("futures_options", "futures_options", "the heavy render releases its lease in a finally",
     '        finally:\n            if _lease is not None:\n                _lease.close()',
     '        finally:\n            pass'),
    ("opthist", "opthist", "an archive without the requested leaf is not unpacked TWICE",
     '            if not carries_leaf:\n                return None',
     '            if not carries_leaf:\n                pass'),
    ("opthist", "opthist", "the unpack note never fires from INSIDE the two-stage loop",
     '        except Exception as e:\n            reason = "%s: %s" % (type(e).__name__, e)',
     '        except Exception as e:\n            sys.stderr.write("market-data: store.tgz unpack '
     'failed () — bundled history remains readable directly\\n")\n'
     '            reason = "%s: %s" % (type(e).__name__, e)'),
    # The packaged companion ships to users; its two entrypoints earn the same check as the repo's.
    ("packaging/openai/financial-deep-analysis/mcp/offer_server.py", "openai_port",
     "one malformed byte on stdin is a parse error, not a dead server",
     '((sys.stdin, "replace"), (sys.stdout, "backslashreplace"))',
     '((sys.stdin, "strict"), (sys.stdout, "backslashreplace"))'),
    ("packaging/openai/financial-deep-analysis/mcp/python_stdio_launcher.cjs", "openai_port",
     "a Python candidate is PROBED before it is handed the server",
     '  const probe = probePython3(command, prefix);\n  if (!probe.ok) {',
     '  const probe = probePython3(command, prefix);\n  if (false) {'),

    # ---- round 2 of the same review: four of these fixes had defects of their own ----
    # NOTE: "a lone surrogate is written, not fatal" has NO entry. Two independent defences carry
    # it — the backslashreplace stream AND the ensure_ascii=True retry at the write — so breaking
    # either one alone leaves the behaviour intact and no single-line mutation expresses it. Same
    # redundant-defence case as `--skill-available` above; it is pinned in the suite instead.
    ("packaging/openai/financial-deep-analysis/mcp/offer_server.py", "openai_port",
     "a parse failure that is not JSONDecodeError is answered, never fatal",
     '        except Exception:               # JSONDecodeError, and whatever else a parser can raise',
     '        except json.JSONDecodeError:'),
    ("packaging/openai/financial-deep-analysis/mcp/offer_server.py", "openai_port",
     "a dispatch failure records its cause on stderr",
     '                traceback.print_exc(file=sys.stderr)', '                pass'),
    ("packaging/openai/financial-deep-analysis/mcp/offer_server.py", "openai_port",
     "one request's dispatch failure is answered, never fatal",
     '            except Exception as error:  # one request\'s failure is answered, never fatal',
     '            except json.JSONDecodeError as error:'),
    ("packaging/openai/financial-deep-analysis/mcp/python_stdio_launcher.cjs", "openai_port",
     "a probe failure reports its OWN reason, not a generic one",
     '    return { ok: false, reason: `is Python ${version ? version[1] : "2"}; 3.9 or newer is required` };',
     '    return { ok: false, reason: "not found" };'),
    # ---- futhist: the v1.166 two-tier prewarm (the snapshot is the speed lever, not history) ----
    ("futhist", "futhist", "the defs-only tier excludes the roots the accrete tier already refreshes "
                           "— a duplicate whole-root listing is this tier's entire cost, twice",
     '        if opt in seen:\n            continue',
     '        if False:\n            continue'),
    ("futhist", "futhist", "--daily-once's early return still refreshes the defs tier (the stamp "
                           "guards a YEAR of statistics, not a 5-day-TTL listing)",
     '            _defs_tier(roots, today, defs_refresh_fn, defs_roots_env)\n            return {}',
     '            return {}'),
    # ---- package.sh: the defs snapshots are the ONE store artifact a SHAREABLE build may carry ----
    ("scripts/package.sh", "package",
     "the shareable defs bundle takes only files NAMED like a snapshot (never a stray note)",
     '      [A-Z0-9][A-Z0-9]*.json.gz) ;;               # an option code — never notes.md, ._junk, *.bak\n      *) continue ;;',
     '      *) ;;'),
    ("scripts/package.sh", "package",
     "a SYMLINK in the snapshot dir is never payload (it can point anywhere in the tree)",
     '    [ -L "$f" ] && continue                       # a link out of the tree is never payload',
     '    :'),
    # NOTE: the sweep's missing-directory guard has NO entry either, for the reason recorded
    # above — a `find` string aimed at mutate.py matches the CATALOGUE literal first and rewrites
    # the entry instead of the code (it reported SURVIVED, exactly as that note predicts).
    # test_speccheck.py runs the real sweep in a tree without packaging/ instead.
]


_SUF = ".mutorig"      # sidecar holding the pristine source while its file is mutated


def _target_path(mod):
    """A catalogue target: a scripts/ module by NAME, or a repo-relative PATH.

    The packaged OpenAI companion's MCP server and its node launcher are catalogue targets too,
    and neither is an importable module in scripts/ — an invariant that ships to users deserves
    the same mutation check as one that stays in the repo.
    """
    if "/" in mod or os.path.splitext(mod)[1]:
        return os.path.abspath(os.path.join(os.path.dirname(HERE), mod))
    return os.path.join(HERE, "%s.py" % mod)


def _sweep(loud=True):
    """Restore any source left mutated by a KILLED run.

    The `finally` that restores each file does not survive SIGKILL/SIGTERM, and it was not
    hypothetical: a run killed by a command timeout left `moex_bonds.py` carrying `if False:` in
    place of a path-traversal guard, which then read as a py3.9-only test failure — and could have
    been committed. So the pristine source is parked in a sidecar BEFORE the edit and swept back
    here on the next start, whatever killed the last one."""
    n = 0
    directories = sorted({os.path.dirname(_target_path(mod)) for mod, *_rest in CATALOGUE})
    for directory in directories:
        if not os.path.isdir(directory):
            # A catalogue target's directory need not exist in every tree: the INSTALLED skill
            # ships scripts/ but no packaging/. Sweeping is crash recovery and runs first in
            # main() — it must never be the thing that crashes, least of all before restoring
            # the sidecars in the directories that DO exist.
            continue
        for f in sorted(os.listdir(directory)):
            if not f.endswith(_SUF):
                continue
            src, orig = os.path.join(directory, f[:-len(_SUF)]), os.path.join(directory, f)
            with open(orig, encoding="utf-8") as fh:
                body = fh.read()
            if not body.strip():
                # an EMPTY sidecar cannot be the pristine source; restoring it would destroy the
                # module this tool exists to protect. Leave both files alone and say so.
                print("  WARN      %s%s is empty — NOT restoring; check the file by hand"
                      % (f[:-len(_SUF)], _SUF))
                continue
            open(src, "w", encoding="utf-8").write(body)
            os.unlink(orig)
            n += 1
            if loud:
                print("  RESTORED  %s — a previous run was killed mid-mutation" % f[:-len(_SUF)])
    return n


def _run(suite):
    r = subprocess.run([sys.executable, os.path.join(HERE, "test_%s.py" % suite)],
                       capture_output=True, text=True, cwd=HERE)
    return r.returncode == 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    _sweep()
    for _sig in ("SIGTERM", "SIGINT", "SIGHUP"):
        if hasattr(signal, _sig):                                  # Windows lacks SIGHUP
            signal.signal(getattr(signal, _sig),
                          lambda *_a: (_sweep(loud=False), sys.exit(130)))
    if "--sweep" in argv:
        return 0
    if "--list" in argv:
        for m, s, why, _f, _r in CATALOGUE:
            print("  %-16s %-16s %s" % (m, s, why))
        return 0
    only = [a for a in argv if not a.startswith("-")]
    sel = [e for e in CATALOGUE if not only or e[0] in only or e[1] in only]
    if not sel:
        print("nothing selected; try --list", file=sys.stderr)
        return 2
    killed = survived = stale = 0
    for mod, suite, why, find, repl in sel:
        path = _target_path(mod)
        try:
            src = open(path, encoding="utf-8").read()
        except OSError as e:
            # A target's file need not exist in every tree — the installed skill ships mutate.py
            # but no packaging/. That is the catalogue being unusable HERE, which is what STALE
            # already means; it is not a reason for the gate to die on its first entry.
            print("  STALE     %-14s %s" % (mod, why))
            print("            %s" % e)
            stale += 1
            continue
        if find not in src:
            print("  STALE     %-14s %s" % (mod, why))
            print("            the catalogue's `find` is gone from the source — update the entry")
            stale += 1
            continue
        try:
            # atomically, so a kill BETWEEN truncate and write cannot leave a 0-byte "pristine" copy
            _t = path + _SUF + ".part"
            open(_t, "w", encoding="utf-8").write(src)
            os.replace(_t, path + _SUF)                           # survives a kill; swept next run
            open(path, "w", encoding="utf-8").write(src.replace(find, repl, 1))
            ok = _run(suite)
        finally:
            open(path, "w", encoding="utf-8").write(src)          # always restore
            if os.path.exists(path + _SUF):
                os.unlink(path + _SUF)
        if ok:
            print("  SURVIVED  %-14s %s" % (mod, why))
            print("            test_%s.py still PASSES with this invariant broken — the test for it "
                  "is vacuous or missing" % suite)
            survived += 1
        else:
            print("  killed    %-14s %s" % (mod, why))
            killed += 1
    print("\n%d killed · %d SURVIVED · %d stale (of %d)" % (killed, survived, stale, len(sel)))
    return 1 if (survived or stale) else 0


if __name__ == "__main__":
    raise SystemExit(main())
