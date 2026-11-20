#!/usr/bin/env bash
# Nightly prewarm + Cowork-snapshot store sync (v1.93.0 — the MSOS Cowork post-mortem fix).
#
# The settlement vol store is canonical at <installed skill>/store/options — the opthist
# resolution ladder (MARKET_DATA_STORE env -> <skill>/store/options -> ~/.cache) finds it
# there, and the repo-dev copy reaches the SAME data through a symlinked store/options.
# Cowork mounts ONLY the per-session skill snapshot into its sandbox (the VM home is
# throwaway), so a store outside the skill dir never reaches it. This wrapper:
#   1. merges universe.seen queued by Cowork sessions BACK into the canonical store
#      (a ticker first analyzed in Cowork joins the nightly universe),
#   2. runs the prewarm (writes the canonical store via the ladder),
#   3. rsyncs the warmed store into every Cowork skill snapshot (~32MB local — cheap).
#
# LaunchAgent com.market-data.optcache-prewarm runs THIS instead of options.py directly.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"          # <skill>/scripts
SKILL="$(dirname "$HERE")"
STORE="$SKILL/store/options"
FSTORE="$SKILL/store/futures"        # slice (f): the futures vol-history store rides the same sync
PY="${PREWARM_PYTHON:-python3}"
SNAPS="${PREWARM_SNAPS:-$HOME/Library/Application Support/Claude/local-agent-mode-sessions/skills-plugin}"
PLUGCACHE="${PREWARM_PLUGINS:-$HOME/.claude/plugins/cache/claude-for-financial-services}"

mirror_plugins() {
  # v1.125: mirror the neighbor Anthropic plugins' SKILLS (equity-research + financial-analysis —
  # the document layer the deeper-analysis offers route into) into every local Cowork snapshot,
  # beside market-data. Cowork mounts NO user-scope plugins, so this sync is the ONLY channel
  # their skills reach a local Cowork session (the /plugin:command slash form still resolves only
  # on desktop; in Cowork the agent invokes the mirrored skill via the Skill tool). Cloud Cowork
  # is unreachable from a local machine — SETUP.md §7 says so and the offer card there stays the
  # honest "run it on desktop". Best-effort and tiny (<1MB for both plugins).
  for plug in equity-research financial-analysis; do
    latest="$(ls -d "$PLUGCACHE/$plug"/*/ 2>/dev/null | sort -V | tail -1)"
    [ -n "$latest" ] && [ -d "${latest}skills" ] || continue
    for sk in "${latest}skills"/*/; do
      [ -f "${sk}SKILL.md" ] || continue
      name="$(basename "$sk")"
      for snapd in "$SNAPS"/*/*/skills; do
        [ -d "$snapd" ] || continue
        # NEVER clobber a pre-existing skill of the same name that we didn't mirror (the
        # standard Cowork set already ships a `skill-creator`; financial-analysis has one too).
        # The marker lives BESIDE the dir, OUT of rsync --delete's reach (review P2 2026-07-23:
        # an in-dir marker + a crash between rsync and touch orphaned the mirror forever —
        # every later run read it as a user skill and skipped it silently). The legacy in-dir
        # marker is still honored once for re-adoption; --delete then strips it naturally.
        if [ -d "$snapd/$name" ] && [ ! -f "$snapd/.mirrored-$name" ] \
           && [ ! -f "$snapd/$name/.mirrored-from-plugin" ]; then
          continue
        fi
        touch "$snapd/.mirrored-$name" || continue   # claim FIRST — the crash window closes
        mkdir -p "$snapd/$name"
        rsync -a --delete "$sk" "$snapd/$name/" || true
      done
    done
  done
}

# --plugins-only: just the Cowork skill mirror (install_plugins.sh calls this right after a
# plugin install so Cowork picks the skills up without waiting for the next 6h tick)
if [ "${1:-}" = "--plugins-only" ]; then
  mirror_plugins
  exit 0
fi
# any OTHER arg is a mistake — never let a typo ("--plugin-only") fall through into the
# METERED full prewarm (review P3 2026-07-23)
if [ $# -gt 0 ]; then
  echo "prewarm_sync: unknown arg: $1 (only --plugins-only is recognized; no args = the full run)" >&2
  exit 2
fi

# Refuse to run against an EMPTY canonical store when ~/.cache still holds the real one —
# a fresh store would send the prewarm re-backfilling the whole universe (thousands of
# metered calls). Migration is one-time: rsync ~/.cache/market-data/options/ -> $STORE/.
LEGACY="$HOME/.cache/market-data/options"
if [ ! -s "$STORE/universe.seen" ] && [ -s "$LEGACY/universe.seen" ]; then
  echo "prewarm_sync: canonical store $STORE is empty but $LEGACY is not — migrate first:" >&2
  echo "  mkdir -p '$STORE' && rsync -a '$LEGACY/' '$STORE/'" >&2
  exit 1
fi
mkdir -p "$STORE"
# A DANGLING store/options symlink (repo checkout after the installed copy moved) makes mkdir -p
# no-op without creating anything — refuse rather than fork the store or ship a dead link.
[ -d "$STORE" ] || { echo "prewarm_sync: $STORE is not a directory (dangling symlink?)" >&2; exit 1; }

merge_seen() {
  # merge Cowork-queued tickers back into the canonical universe (best-effort)
  for seen in "$SNAPS"/*/*/skills/market-data/store/options/universe.seen; do
    [ -f "$seen" ] || continue
    [ "$seen" -ef "$STORE/universe.seen" ] && continue   # never merge the canonical file into itself
    cat "$seen" "$STORE/universe.seen" 2>/dev/null | tr -d ' \r' | grep -E '^[A-Z0-9.-]+$' \
      | sort -u > "$STORE/universe.seen.tmp" && mv "$STORE/universe.seen.tmp" "$STORE/universe.seen"
  done
}

# 1) merge BEFORE the prewarm so today's pass covers Cowork-queued names
merge_seen

# 2) the actual prewarm (--daily-once: a complete pass stamps the day; same-day reruns no-op)
"$PY" "$HERE/options.py" --prewarm --daily-once
rc=$?

# 2b) slice (f): the FUTURES accrete — the tier-1 set (21 roots since v1.129; MARKET_DATA_FUT_PREWARM
#     overrides). Warmed roots cost ~one open month of pulls (covered-month skip + settle-span
#     narrowing); a COLD root runs its full first backfill here — that IS the designed overnight
#     initial fill. Best-effort: a futures failure never blocks the options sync below.
#     v1.166: the same call ALSO runs the DEFS-ONLY tier — a definition snapshot (no statistics)
#     for every other OPTION_ROOTS root, because the snapshot is what makes a dashboard fast
#     (measured 2026-08-07: PL with one 44s, ZL without 200s) and it is 5-200 KB per root. It runs
#     even when --daily-once skips the accrete; MARKET_DATA_FUT_DEFS_PREWARM= turns it off.
"$PY" "$HERE/futhist.py" --prewarm --daily-once || rc=$?

# 3) re-merge (a Cowork session may have queued names DURING the long prewarm — step 4's
#    --delete would otherwise clobber them out of the snapshot before they were ever seen),
merge_seen

# 4) ship the warmed store into every Cowork skill snapshot. Source is $STORE/ (trailing
#    slash DEREFERENCES a symlinked store/options — the repo layout), never $SKILL/store/:
#    rsync -a copies a symlink AS a symlink and --delete would replace each snapshot's real
#    store with a host-absolute link that dangles inside the Cowork VM.
for snap in "$SNAPS"/*/*/skills/market-data; do
  [ -d "$snap" ] || continue
  mkdir -p "$snap/store/options"
  rsync -a --delete "$STORE/" "$snap/store/options/" || true
  # slice (f): the futures store too (same trailing-slash rule — dereference, never ship a symlink)
  if [ -d "$FSTORE" ]; then
    mkdir -p "$snap/store/futures"
    rsync -a --delete "$FSTORE/" "$snap/store/futures/" || true
  fi
done

# 5) v1.125: keep the neighbor-plugin skills mirrored into Cowork (see mirror_plugins above)
mirror_plugins
exit $rc
