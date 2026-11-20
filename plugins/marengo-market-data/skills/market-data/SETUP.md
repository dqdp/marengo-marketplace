# Setup — market-data skill (read this first on a new machine)

For current MOEX quotes connect the official BCS MCP at https://be.broker.ru/trade-api-mcp-server/mcp using a read-only Trade API refresh token in the client credential settings. In Codex use the bearer-token environment variable BCS_TOKEN. Never paste the token into a conversation.

For CME contract lookup and quotes, connect Marengo MCP in your client and sign in with the invited account. The Databento key belongs in the Marengo account portal. Use the existing Alpha Vantage connector for equity/ETF quotes. Local API key setup below is for the legacy workflows and analytical scripts.

This skill plots charts and pulls market data through `scripts/chart.py` (and
`scripts/greeks.py`). Both are **pure Python stdlib — no `pip install`** — but a
new machine/account needs three things checked: Python, API keys, and (in some
sandboxes) network egress. Run the one-command doctor, then fix whatever it flags.

```bash
python3 scripts/setup_check.py        # macOS / Linux
py -3 -X utf8 scripts\setup_check.py  # Windows (or: python -X utf8 scripts\setup_check.py)
```

It prints `OK / WARN / FAIL` for Python, the `.env` keys, the PNG converter, and
live network reachability of each data host — with the exact fix under any line
that isn't OK. Everything below is the detail behind those lines.

---

## 1. Install location

Unzip the skill so the host can find it:

```bash
# macOS / Linux
unzip market-data-skill-v<ver>.zip -d ~/.claude/skills/
# Windows (PowerShell)
Expand-Archive market-data-skill-v<ver>.zip -DestinationPath $HOME\.claude\skills\
```

It unpacks to `~/.claude/skills/market-data/` (`SKILL.md` + `references/` +
`scripts/`). **Always invoke the bundled script by its full path** — your working
directory is usually a project, not the skill folder, so a bare
`python3 scripts/chart.py` won't resolve. Use
`python3 "<skill-dir>/scripts/chart.py" …` where `<skill-dir>` is the folder
holding `SKILL.md`. Never hand-roll your own plotting script — see
`references/charting.md`.

## 2. Python

Need **Python 3.7+** (the scripts are stdlib-only). Check: `python3 --version`.

- **macOS**: preinstalled, or `brew install python`.
- **Windows**: install from python.org and tick **"Add python.exe to PATH"**. The
  launcher is `py -3 -X utf8` (or `python -X utf8`), not `python3`.
- **Linux**: `apt install python3` / `dnf install python3`.

## 3. API keys (`.env`)

Charts from **Alpha Vantage** and **Databento** need keys; **MOEX is keyless**.
Create a `.env` file **next to `SKILL.md`** (the script finds it there no matter
your cwd). Copy `.env.example` and fill in:

```
ALPHAVANTAGE_API_KEY=your_av_key      # av-equity, av-crypto
DATABENTO_API_KEY=your_databento_key  # databento (CME futures/options)
```

- The keys are **secrets**: never pass them on the command line or paste them into
  chat. `.env` is gitignored.
- A private skill build (`scripts/package.sh --with-env`) bakes your keys into the
  zip so a sandbox install has them — that archive is **local-only, never shared**.
- Note: the Alpha Vantage key sealed inside the AV **MCP connector** is NOT
  reachable by the script — copy the key into `.env` separately.

## 4. Network egress (the sandbox question)

`chart.py` fetches directly over HTTPS from `alphavantage.co`,
`hist.databento.com`, and `iss.moex.com` — that's what keeps the price series out
of the agent's context. **Some sandboxes block that egress.** The doctor's network
section tells you which hosts are reachable here. If some are blocked:

**A. Local Claude Code session (CLI or Desktop "Code" tab).** The built-in Bash
sandbox has a documented allowlist. Add the hosts to `~/.claude/settings.json`:

```json
{
  "sandbox": {
    "network": {
      "allowedDomains": ["alphavantage.co", "www.alphavantage.co",
                         "hist.databento.com", "iss.moex.com"]
    }
  }
}
```

(The allowlist only takes effect when the Bash sandbox is enabled — `/sandbox`, or
`sandbox.enabled: true`. With the sandbox off, egress is already open.)

**B. Agent-mode / "Home" sessions (a managed full-VM sandbox).** These run in an
Anthropic-managed VM whose egress is enforced by a managed proxy allowlist — your
local `allowedDomains` setting likely does **not** reach it, and there is no
documented user setting to add arbitrary egress domains there. Two options:

1. Build the chart in a **local** session instead (egress open / allowlisted).
2. Use the **`--from` fallback**: pull the series through the MCP connector (the
   connector can reach the host even when direct egress is blocked), save it to a
   file, and render with the same bundled script — no bespoke plotting code:
   ```bash
   python3 "<skill-dir>/scripts/chart.py" INTC --from series.csv --interval 1d \
       --source-label "Alpha Vantage equity (via MCP)" --out chart.svg --png
   ```
   CSV header: `timestamp,open,high,low,close[,volume]` (or JSON). Full playbook in
   `references/charting.md`.

Do **not** disable the sandbox wholesale just to draw a chart — prefer the
allowlist (local) or `--from` (managed VM).

## 5. Displaying the chart

The script writes an `.svg` (opens in any browser, any OS). For an inline PNG add
`--png`; it auto-selects a converter:

- **rsvg-convert** — fast, any OS (`brew/apt/choco install librsvg`). Recommended off macOS.
- **qlmanage + sips** — macOS built-in (no install).
- **headless Chrome/Edge** — last-resort fallback (~1–3s).

Chart *generation* needs none of these — only the optional PNG preview does.
**Strongly recommend installing `rsvg-convert`** (`brew install librsvg` /
`choco install rsvg-convert` / `apt install librsvg2-bin`): it is fast (~0.1s),
cross-platform, and — unlike `qlmanage` — works under the Bash sandbox (no Apple
Events), so it never leaves a hung background task.

### Showing a chart or dashboard to the user (not just to the agent)
A rendered image does NOT appear in the user's chat just because the agent `Read` it
(the client shows a file link). Pick by environment:
- **`--open`** opens the SVG in the user's **external default browser**, automatically
  — the fast way to show a rich dashboard. Works only when the script runs on the
  user's **local** machine with a display. Under the sandbox it is blocked on macOS
  (browser launch = Apple Events → error -600); run that one command unsandboxed. In
  **agent-mode / Home (remote VM)** it opens a browser inside the container — the user
  sees nothing, so there use an inline widget + the SVG file instead.
  - **Windows via WSL2**: `webbrowser` may look for a Linux browser. Fall back to
    `cmd.exe /c start "" "<path.svg>"` to open the Windows host browser.
- **Inline widget** (in clients that render them): build a SMALL one from `--json` —
  never re-emit the full dashboard SVG through a widget (it streams for minutes).

## 6. Verify end to end

A keyless smoke test that needs no `.env` and proves the full path works:

```bash
python3 "<skill-dir>/scripts/chart.py" IMOEX --source moex --interval 1d --days 30 --out /tmp/smoke.svg
```

You should get a one-line summary (`… N 1d bars … last close …`) and a `smoke.svg`.
If keys are set, repeat with `PPLT --source av-equity` and `PL.v.0 --source databento`.

## 7. Neighbor plugins (the deeper-analysis document layer)

When this skill answers an "analyze X", it OFFERS a deeper written report (on OpenAI, an
earnings-analysis offer produces a standalone PDF as the primary artifact and may retain DOCX as
its editable source; comps/DCF remain models, and Claude keeps the original DOCX behavior). Those
documents are produced by two
**separate Anthropic plugins** — `equity-research` and `financial-analysis` — that the offer
routes into (`/equity-research:earnings`, `/financial-analysis:comps`, …). **The skill zip does
not install them** (it can't run the plugin CLI), so a fresh machine adds them once — one command
(v1.125, idempotent — installs both plugins AND mirrors their skills into Cowork, see below):

```bash
bash ~/.claude/skills/market-data/scripts/install_plugins.sh
```

(Manual fallback: `claude plugin marketplace add https://github.com/anthropics/financial-services`,
then `claude plugin install equity-research@claude-for-financial-services` and
`claude plugin install financial-analysis@claude-for-financial-services`.)

They are **optional** — the data + dashboard + written read all work without them; only the
one-click deeper-report offers need them. `setup_check.py` flags them if missing.

**Cowork (v1.125):** Cowork mounts no user-scope plugins, so the `/equity-research:*` slash
commands resolve only on desktop. But `install_plugins.sh` (and every 6h `prewarm_sync.sh` tick
afterwards) **mirrors the plugins' SKILLS into every local Cowork snapshot** beside market-data —
a local Cowork agent can then produce the documents by invoking the mirrored skill via the Skill
tool (`earnings-analysis`, `comps-analysis`, `dcf-model`, …; a pre-existing same-name Cowork
skill is never clobbered — mirrors are claimed by a `.mirrored-<name>` marker beside the dir).
Uninstalling a plugin (or a skill dropped in a version bump) leaves its mirrored copies in the
snapshots — stale but harmless; delete the marker-claimed dirs by hand if needed. **Cloud**
Cowork is unreachable from a local machine — there the offer card stays the honest "run this
on desktop/local for the full report".

**Cloud skill freshness (v1.126.1):** cloud-Cowork sessions run the skill copy PUBLISHED to
claude.ai — no local install/sync updates it. After any ship that changes Cowork behavior,
re-upload the SHAREABLE zip (`bash scripts/package.sh`, the non---with-env build) to claude.ai
→ Settings → Skills, or the next cloud session runs the stale snapshot (the QCOM session
2026-07-23: no offer card + a dead sibling chip = a pre-v1.123.3 cloud copy). NOTE 2026-07-23:
the desktop app now syncs a NATIVE plugin mount into sessions (`rpm/` manifest — equity-research
+ financial-analysis listed "installedBy: user") — probe a fresh Cowork session (`ls /mnt`,
does `/equity-research:earnings` resolve?) before assuming the plugins are still absent there.

---

**Quick checklist:** Python 3.7+ · skill at `~/.claude/skills/market-data/` · `.env`
beside `SKILL.md` (AV + Databento; MOEX keyless) · `python3 scripts/setup_check.py`
all green · egress allowlisted (local) or `--from` ready (managed VM) · neighbor plugins
installed for the deeper-report offers (§7, optional).
