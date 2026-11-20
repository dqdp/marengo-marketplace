# Setup — Market Data for ChatGPT Work and Codex

This is the OpenAI package. It installs into the shared agent skill directory and does not use
Claude plugin commands or `~/.claude/skills`.

## Requirements

- ChatGPT desktop app; Windows 11 is the recommended Windows target.
- Python 3.10 or newer.
- Node.js 22.15 or newer when the Databento and Financial Deep Analysis plugins are installed.

The Market Data core and the Financial Deep Analysis MCP offer card need no `pip install` or
`npm install`. Some optional advanced financial workflows create or validate Excel/chart artifacts
with `openpyxl` (and, depending on the selected workflow, the host's spreadsheet/document runtime).
ChatGPT Work and Codex normally provide that artifact runtime. For standalone local DCF validation,
install the plugin's explicit optional requirements only when you need that workflow:

```bash
python3 -m pip install -r ~/plugins/financial-deep-analysis/skills/dcf-model/requirements.txt
```

Native Windows PowerShell:

```powershell
py -3 -X utf8 -m pip install -r "$HOME\plugins\financial-deep-analysis\skills\dcf-model\requirements.txt"
```

## Install the private skill

The private archive contains `ALPHAVANTAGE_API_KEY`, `DATABENTO_API_KEY`, and may contain a warm
cache. Keep it local and never upload, email, commit, or share it.

macOS or Linux:

```bash
mkdir -p ~/.agents/skills
unzip -oq market-data-openai-skill-v<version>-private.zip -d ~/.agents/skills
chmod 600 ~/.agents/skills/market-data/.env
python3 ~/.agents/skills/market-data/scripts/setup_check.py --no-net
```

Windows PowerShell:

```powershell
$SkillParent = Join-Path $HOME ".agents\skills"
$Target = Join-Path $SkillParent "market-data"
$Stage = Join-Path $SkillParent (".market-data-stage-" + [guid]::NewGuid())
New-Item -ItemType Directory -Force $SkillParent | Out-Null
if (Test-Path $Target) { throw "market-data is already installed; use USER_INSTALLATION_RUNBOOK_RU.md to update it" }
try {
  Expand-Archive .\market-data-openai-skill-v<version>-private.zip -DestinationPath $Stage -ErrorAction Stop
  $Prepared = Join-Path $Stage "market-data"
  if (-not (Test-Path $Prepared -PathType Container)) { throw "market-data directory missing in archive" }
  $Sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
  & icacls (Join-Path $Prepared ".env") /inheritance:r /grant:r "*${Sid}:(R,W)"
  if ($LASTEXITCODE -ne 0) { throw "icacls failed for market-data .env" }
  Move-Item $Prepared $Target -ErrorAction Stop
} finally {
  if (Test-Path $Stage) { Remove-Item $Stage -Recurse -Force -ErrorAction SilentlyContinue }
}
py -3 -X utf8 "$HOME\.agents\skills\market-data\scripts\setup_check.py" --no-net
```

The doctor must find both key names without printing their values. MOEX itself is keyless.

## Runtime command rule

Commands in `SKILL.md` use the `<python>` placeholder. Substitute `python3` on macOS, Linux, or
WSL, and `py -3 -X utf8` in native Windows PowerShell. Never type the placeholder literally.

For the full installation of the Market Data skill, Databento MCP, Financial Deep Analysis, and
the Alpha Vantage app, follow `USER_INSTALLATION_RUNBOOK_RU.md` bundled in this skill archive.
Fully quit and restart ChatGPT, then test in a new Work or Codex task.
