#!/usr/bin/env bash
# v1.125: one-shot IDEMPOTENT install of the neighbor Anthropic plugins (equity-research +
# financial-analysis from the claude-for-financial-services marketplace — the DOCUMENT layer
# the market-data deeper-analysis offers route into), then mirror their skills into every
# local Cowork snapshot (Cowork mounts no user-scope plugins; prewarm_sync's 6h tick keeps
# the mirror fresh afterwards). Cloud Cowork is out of reach from a local machine — the offer
# card there stays the honest "run it on desktop" (SETUP.md §7).
#
#   bash scripts/install_plugins.sh
#
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
if command -v claude >/dev/null 2>&1; then
  claude plugin marketplace add https://github.com/anthropics/financial-services || true
  claude plugin install equity-research@claude-for-financial-services || true
  claude plugin install financial-analysis@claude-for-financial-services || true
else
  echo "warn: 'claude' CLI not on PATH — skipping the plugin install" >&2
  echo "      (the Cowork skill mirror below still runs if a plugin cache already exists)" >&2
fi
bash "$HERE/prewarm_sync.sh" --plugins-only
python3 "$HERE/setup_check.py" --no-net 2>/dev/null | grep -i "neighbor plugins" || true
