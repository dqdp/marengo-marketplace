# Ранбук установки Market Data для ChatGPT Work и Codex

**Актуально на:** 6 августа 2026 года
**Поддерживаемая конфигурация первой версии:** macOS и Windows 11, ChatGPT Desktop / Codex,
локальная установка. Нативный Windows-сценарий работает через PowerShell; WSL не обязателен.
Актуальная Windows 10 — best effort, а не целевая проверенная платформа.
**Важно:** полный локальный комплект пока не равен hosted/cloud-версии. Локальный
Databento MCP в облаке не запускается; статус облака описан отдельно ниже.

## 1. Что устанавливается

| Компонент | Для чего нужен | Содержит ключи |
|---|---|---|
| `market-data` private skill | Рыночные данные, фундаментальный и опционный дашборды, кэш, MOEX | **Да:** `ALPHAVANTAGE_API_KEY` и `DATABENTO_API_KEY` в `.env` |
| `databento-market-data` plugin | 19 локальных MCP-инструментов Databento для фьючерсов и опционов | Нет |
| `financial-deep-analysis` plugin | 13 углублённых финансовых workflows, PDF-отчёты и интерактивные действия | Нет |
| официальный Alpha Vantage MCP | Дополнительные точечные запросы Alpha Vantage в ChatGPT Work | Ключ вводится на OAuth-странице Alpha Vantage, но не включается в наш plugin |

Текущие проверенные версии:

- `market-data` — `1.165.0`;
- `databento-market-data` — `1.3.1`;
- `financial-deep-analysis` — `0.2.0+codex.20260806151822`.

Один `financial-deep-analysis` plugin устанавливает все вложенные skills сразу.
Устанавливать каждый финансовый skill по отдельности не нужно.

## 2. Что должно быть у пользователя

Нужны:

1. Актуальное приложение ChatGPT Desktop с доступом к ChatGPT Work или Codex.
2. Python 3.10 или новее: `python3 --version` на macOS либо `py -3 -X utf8 --version` на Windows.
3. Node.js 22.15 или новее: `node --version`.
4. Команда Codex: `codex --version`.
5. Личные ключи Alpha Vantage и Databento, уже включённые поставщиком в
   **приватный** архив `market-data`.

Базовая установка Market Data, Databento MCP и интерактивной плитки Financial Deep Analysis не
требует `pip install` или `npm install`. Некоторые углублённые финансовые workflows используют
optional dependencies для создания и проверки Excel/графических артефактов — в частности,
`openpyxl`. В ChatGPT Work и Codex обычно используется встроенный runtime документов и таблиц.
Для самостоятельного запуска DCF-валидатора из Terminal установите его явно описанные зависимости:

```bash
python3 -m pip install -r ~/plugins/financial-deep-analysis/skills/dcf-model/requirements.txt
```

Windows PowerShell:

```powershell
py -3 -X utf8 -m pip install -r "$HOME\plugins\financial-deep-analysis\skills\dcf-model\requirements.txt"
```

Это необязательно для обычного дашборда и плиток; установка нужна только для соответствующего
локального advanced-workflow.

Пользователь получает три файла:

```text
market-data-openai-skill-v1.165.0-private.zip
databento-chatgpt-plugin.zip
financial-deep-analysis-plugin-v0.2.0+codex.20260806151822.zip
```

Приватный архив `market-data` содержит секреты и прогретый кэш. Его нельзя
отправлять в публичный репозиторий, общий чат, почту без шифрования или публичное
облачное хранилище. Два plugin-архива ключей не содержат.

## 3. Установка файлов

Откройте Terminal на macOS либо PowerShell на Windows и перейдите в папку, где лежат три архива.

macOS:

```bash
cd "/путь/к/папке-с-архивами"
```

Windows PowerShell:

```powershell
Set-Location "C:\путь\к\папке-с-архивами"
```

### 3.1. Market Data skill

```bash
mkdir -p ~/.agents/skills
unzip -oq ./market-data-openai-skill-v1.165.0-private.zip -d ~/.agents/skills
chmod 600 ~/.agents/skills/market-data/.env
```

Windows PowerShell:

```powershell
$SkillParent = Join-Path $HOME ".agents\skills"
$Target = Join-Path $SkillParent "market-data"
$Stage = Join-Path $SkillParent (".market-data-stage-" + [guid]::NewGuid())
New-Item -ItemType Directory -Force $SkillParent | Out-Null
if (Test-Path $Target) { throw "market-data is already installed; use section 9 to update it" }
try {
  Expand-Archive .\market-data-openai-skill-v1.165.0-private.zip -DestinationPath $Stage -ErrorAction Stop
  $Prepared = Join-Path $Stage "market-data"
  if (-not (Test-Path $Prepared -PathType Container)) { throw "market-data directory missing in archive" }
  $Sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
  & icacls (Join-Path $Prepared ".env") /inheritance:r /grant:r "*${Sid}:(R,W)"
  if ($LASTEXITCODE -ne 0) { throw "icacls failed for market-data .env" }
  Move-Item $Prepared $Target -ErrorAction Stop
} finally {
  if (Test-Path $Stage) { Remove-Item $Stage -Recurse -Force -ErrorAction SilentlyContinue }
}
```

`icacls` убирает унаследованный широкий доступ к файлу с ключами и оставляет чтение/запись
текущему пользователю. Не ослабляйте ACL всей домашней папки.

После распаковки должен существовать файл:

```text
~/.agents/skills/market-data/SKILL.md
```

Ключи хранятся только здесь:

```text
~/.agents/skills/market-data/.env
```

Не копируйте `.env` в папки коннекторов или plugins.

### 3.2. Financial Deep Analysis

```bash
mkdir -p ~/plugins
unzip -oq ./financial-deep-analysis-plugin-v0.2.0+codex.20260806151822.zip -d ~/plugins
```

Windows PowerShell:

```powershell
New-Item -ItemType Directory -Force "$HOME\plugins" | Out-Null
Expand-Archive .\financial-deep-analysis-plugin-v0.2.0+codex.20260806151822.zip -DestinationPath "$HOME\plugins" -Force
```

После распаковки должна существовать папка:

```text
~/plugins/financial-deep-analysis
```

### 3.3. Databento MCP

Архив Databento не содержит внешнюю корневую папку, поэтому каталог назначения
создаётся заранее:

```bash
mkdir -p ~/plugins/databento-market-data
unzip -oq ./databento-chatgpt-plugin.zip -d ~/plugins/databento-market-data
```

Windows PowerShell:

```powershell
New-Item -ItemType Directory -Force "$HOME\plugins\databento-market-data" | Out-Null
Expand-Archive .\databento-chatgpt-plugin.zip -DestinationPath "$HOME\plugins\databento-market-data" -Force
```

После распаковки должны существовать:

```text
~/plugins/databento-market-data/.codex-plugin/plugin.json
~/plugins/databento-market-data/.mcp.json
```

Databento plugin при запуске читает только `DATABENTO_API_KEY` из
`~/.agents/skills/market-data/.env`. Сам plugin остаётся без ключа.

## 4. Добавление plugins в личный marketplace

Создайте каталог:

```bash
mkdir -p ~/.agents/plugins
```

Windows PowerShell:

```powershell
New-Item -ItemType Directory -Force "$HOME\.agents\plugins" | Out-Null
```

Файл marketplace должен находиться здесь:

```text
~/.agents/plugins/marketplace.json
```

Если файла ещё нет, создайте его со следующим содержимым:

```json
{
  "name": "personal",
  "interface": {
    "displayName": "Personal"
  },
  "plugins": [
    {
      "name": "databento-market-data",
      "source": {
        "source": "local",
        "path": "./plugins/databento-market-data"
      },
      "policy": {
        "installation": "AVAILABLE",
        "authentication": "ON_INSTALL"
      },
      "category": "Data & Analytics"
    },
    {
      "name": "financial-deep-analysis",
      "source": {
        "source": "local",
        "path": "./plugins/financial-deep-analysis"
      },
      "policy": {
        "installation": "AVAILABLE",
        "authentication": "ON_INSTALL"
      },
      "category": "Data & Analytics"
    }
  ]
}
```

Если `marketplace.json` уже существует, не заменяйте его целиком. Добавьте два
объекта из примера в существующий массив `plugins`, сохранив остальные записи.

Проверьте, что Codex видит marketplace и оба plugins:

```bash
codex plugin marketplace list
codex plugin list
```

В списке `personal` должны появиться:

```text
databento-market-data@personal
financial-deep-analysis@personal
```

Установите их:

```bash
codex plugin add databento-market-data@personal
codex plugin add financial-deep-analysis@personal
```

Ещё раз выполните `codex plugin list`. Оба компонента должны иметь статус
`installed, enabled`.

## 5. Подключение официального Alpha Vantage MCP

Это отдельное, дополнительное подключение. Оно удобно для точечных фактов и
резервного получения данных через MCP, но **не заменяет** ключ Alpha Vantage в
приватном `market-data` skill.

Рекомендуемый безопасный способ — официальный remote MCP с OAuth:

1. Откройте ChatGPT → **Settings** → **Apps**.
2. В **Advanced settings** включите **Developer mode**. В некоторых версиях
   приложения переключатель находится в **Settings → Security and login**.
3. Вернитесь в **Apps** и нажмите **Create app**.
4. Название: `Alpha Vantage MCP`.
5. MCP Server URL:

   ```text
   https://mcp.alphavantage.co/mcp
   ```

6. Authentication: **OAuth**.
7. На открывшейся странице Alpha Vantage введите личный API key и подтвердите
   доступ.

Не используйте устаревший вариант с `?apikey=...` в URL. Он оставляет секрет в
адресе подключения и официально помечен Alpha Vantage как legacy/deprecated.

### Почему Alpha Vantage настраивается дважды

- OAuth даёт доступ официальному MCP внутри ChatGPT Work.
- `~/.agents/skills/market-data/.env` даёт доступ нашим локальным Python-скриптам,
  которые строят быстрые дашборды и используют кэш.

Эти хранилища учётных данных независимы. Если OAuth работает, но дашборд сообщает
об отсутствующем ключе, нужно исправить `.env`, а не переподключать MCP.

В Codex отдельный Alpha Vantage MCP для базового сценария не обязателен:
`market-data` получает данные напрямую по ключу из `.env`. Не добавляйте ключ в
`~/.codex/config.toml` в рамках этого комплекта.

Актуальные первоисточники:

- [официальный Alpha Vantage MCP](https://github.com/alphavantage/alpha_vantage_mcp);
- [установка и использование plugins в ChatGPT и Codex](https://learn.chatgpt.com/docs/plugins);
- [регистрация MCP в ChatGPT developer mode](https://developers.openai.com/plugins/build/plugins#create-and-test-a-plugin-locally-with-an-mcp-server).

## 6. Перезапуск и активация

После установки:

1. macOS: полностью завершите ChatGPT Desktop через **Cmd+Q**. Windows: полностью завершите
   ChatGPT через значок в системном трее (**Quit/Выход**); простого закрытия окна недостаточно.
2. Запустите приложение снова.
3. Выберите **ChatGPT → Work** или **Codex**.
4. Откройте **Plugins** и убедитесь, что `Databento Market Data` и
   `Financial Deep Analysis` включены.
5. Создайте **новую задачу/новый чат**. Уже открытая задача может хранить старый
   снимок skills и plugins.

Plugins не работают в обычном режиме Chat. Нужен ChatGPT Work или Codex.

## 7. Проверка установки

### 7.1. Market Data и ключи

```bash
python3 ~/.agents/skills/market-data/scripts/setup_check.py --no-net
```

Windows PowerShell:

```powershell
py -3 -X utf8 "$HOME\.agents\skills\market-data\scripts\setup_check.py" --no-net
```

Проверка должна увидеть `ALPHAVANTAGE_API_KEY` и `DATABENTO_API_KEY`. Она не
должна печатать значения ключей.

Затем в новой задаче попросите:

```text
Проанализируй Coca-Cola (KO). Сначала покажи ссылку на фундаментальный дашборд,
затем действия для Initial Coverage и контекстного Earnings report.
```

Ожидаемый результат:

- сначала появляется заметная ссылка на фундаментальный дашборд;
- фундаментальный и опционный дашборды ссылаются друг на друга;
- показываются два действия углублённого анализа;
- на общей плитке можно выбрать `Русский` или `English`.

### 7.2. Databento MCP

```text
Используй Databento Market Data и покажи информацию о текущей сессии без платной выгрузки.
```

Ожидаемый результат: вызывается безопасный инструмент `get_session_info`, без
запроса повторно вставить API key в connector.

### 7.3. Alpha Vantage MCP

```text
Используй Alpha Vantage MCP и перечисли три доступные функции.
```

Ожидаемый результат: MCP показывает функции наподобие `TIME_SERIES_DAILY` и
`TIME_SERIES_INTRADAY`. Если ChatGPT просит подтверждение первого вызова,
подтвердите его.

### 7.4. Financial Deep Analysis

```text
Подготовь Initial Coverage по Microsoft на русском языке с итоговым PDF.
```

Ожидаемый результат: запускается `initiating-coverage`, а не второй обычный
market-data дашборд; итоговым артефактом является самостоятельный отчёт/PDF.

## 8. Частые проблемы

### Skill не найден

- Проверьте `~/.agents/skills/market-data/SKILL.md`.
- Полностью перезапустите приложение через Cmd+Q на macOS или Quit в системном трее Windows.
- Начните новую задачу.

### Plugins не видны

- Выполните `codex plugin marketplace list` и убедитесь, что есть `personal` с
  корнем домашней папки.
- Выполните `codex plugin list` и найдите оба plugins.
- Проверьте пути в `~/.agents/plugins/marketplace.json`.
- Повторите две команды `codex plugin add ...@personal`.

### Databento просит ключ или не запускается

- Проверьте Node.js: нужна версия 22.15 или новее.
- Запустите `setup_check.py --no-net`.
- Проверьте наличие `DATABENTO_API_KEY` в
  `~/.agents/skills/market-data/.env`, не печатая значение ключа в чат.
- Не добавляйте ключ в `.mcp.json`: launcher должен прочитать его из private skill.

### Alpha Vantage MCP работает, а дашборд — нет

Это разные каналы авторизации. OAuth подключает MCP, а дашборду нужен
`ALPHAVANTAGE_API_KEY` в `~/.agents/skills/market-data/.env`.

### Нет плиток углублённого анализа

- Проверьте, что `financial-deep-analysis` имеет статус `installed, enabled`.
- Начните новую задачу после установки или обновления.
- Убедитесь, что выбран режим Work или Codex, а не обычный Chat.

### Дашборд не открылся автоматически

Автоматическое открытие боковой панели не гарантируется оболочкой. Контракт первой
версии — быстро показать заметную ссылку на self-contained HTML. Нажмите ссылку;
остальные действия могут дорисоваться после неё.

## 9. Обновление

Не распаковывайте новую версию поверх старой: такой способ оставляет удалённые файлы. Перед
заменой полностью завершите ChatGPT: на macOS через Cmd+Q, на Windows через **Quit/Выход** в
системном трее. Команды ниже распаковывают архив рядом с целевым каталогом, проверяют его и
переключают только точную директорию компонента. Резервные копии хранятся вне каталогов,
сканируемых Codex, в `~/.market-data-backups/`; при ошибке переключения старая версия
восстанавливается автоматически.

macOS:

```bash
BACKUP_ROOT="$HOME/.market-data-backups/$(date +%Y%m%d%H%M%S)-$$"
mkdir -p "$BACKUP_ROOT" || exit 1

restore_backup() {
  target="$1"; backup="$2"
  [ -z "$backup" ] || [ -e "$target" ] || mv "$backup" "$target"
}

cleanup_stage() {
  stage="$1"; parent="$2"
  case "$stage" in
    "$parent"/.market-data-stage.*) rm -rf -- "$stage" ;;
    *) echo "refusing unsafe staging cleanup: $stage" >&2; return 1 ;;
  esac
}

preserve_market_data_store() {
  current="$1"; prepared="$2"
  [ -d "$current/store" ] || return 0
  mkdir -p "$prepared/store" || return 1
  cp -a "$current/store/." "$prepared/store/" || return 1
}

switch_prepared() {
  source="$1"; target="$2"; backup=""
  if [ -e "$target" ]; then
    backup="$(mktemp -d "$BACKUP_ROOT/$(basename "$target").previous.XXXXXX")" || return 1
    rmdir "$backup" || return 1
    mv "$target" "$backup" || return 1
  fi
  trap 'restore_backup "$target" "$backup"; exit 130' HUP INT TERM
  if ! mv "$source" "$target"; then
    restore_backup "$target" "$backup"
    trap - HUP INT TERM
    return 1
  fi
  trap - HUP INT TERM
}

replace_archive_dir() {
  archive="$1"; member="$2"; target="$3"
  parent="$(dirname "$target")"; mkdir -p "$parent" || return 1
  stage="$(mktemp -d "$parent/.market-data-stage.XXXXXX")" || return 1
  unzip -oq "$archive" -d "$stage" || { cleanup_stage "$stage" "$parent"; return 1; }
  [ -d "$stage/$member" ] || { echo "missing $member in $archive" >&2; cleanup_stage "$stage" "$parent"; return 1; }
  if [ "$member" = "market-data" ]; then
    preserve_market_data_store "$target" "$stage/$member" || { cleanup_stage "$stage" "$parent"; return 1; }
    [ -f "$stage/$member/.env" ] || { echo "market-data .env missing" >&2; cleanup_stage "$stage" "$parent"; return 1; }
    chmod 600 "$stage/$member/.env" || { cleanup_stage "$stage" "$parent"; return 1; }
  fi
  switch_prepared "$stage/$member" "$target" || { cleanup_stage "$stage" "$parent"; return 1; }
  cleanup_stage "$stage" "$parent"
}

replace_flat_archive() {
  archive="$1"; target="$2"
  parent="$(dirname "$target")"; mkdir -p "$parent" || return 1
  stage="$(mktemp -d "$parent/.market-data-stage.XXXXXX")" || return 1
  unzip -oq "$archive" -d "$stage" || { cleanup_stage "$stage" "$parent"; return 1; }
  [ -f "$stage/.codex-plugin/plugin.json" ] || { echo "plugin manifest missing" >&2; cleanup_stage "$stage" "$parent"; return 1; }
  switch_prepared "$stage" "$target" || { cleanup_stage "$stage" "$parent"; return 1; }
  cleanup_stage "$stage" "$parent"
}

replace_archive_dir ./market-data-openai-skill-v1.165.0-private.zip market-data \
  "$HOME/.agents/skills/market-data" || exit 1
replace_archive_dir ./financial-deep-analysis-plugin-v0.2.0+codex.20260806151822.zip \
  financial-deep-analysis "$HOME/plugins/financial-deep-analysis" || exit 1
replace_flat_archive ./databento-chatgpt-plugin.zip "$HOME/plugins/databento-market-data" || exit 1
```

Windows PowerShell:

```powershell
$ErrorActionPreference = "Stop"
$BackupRoot = Join-Path $HOME (".market-data-backups\" + (Get-Date -Format yyyyMMddHHmmss) + "-" + [guid]::NewGuid())
New-Item -ItemType Directory -Force $BackupRoot | Out-Null

function Restore-Backup($Target, $Backup) {
  if ($Backup -and -not (Test-Path $Target) -and (Test-Path $Backup)) {
    Move-Item $Backup $Target -ErrorAction Stop
  }
}

function Protect-MarketDataEnv($SkillRoot) {
  $envFile = Join-Path $SkillRoot ".env"
  if (-not (Test-Path $envFile -PathType Leaf)) { throw "market-data .env missing" }
  $sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
  & icacls $envFile /inheritance:r /grant:r "*${sid}:(R,W)"
  if ($LASTEXITCODE -ne 0) { throw "icacls failed for market-data .env" }
}

function Preserve-MarketDataStore($Current, $Prepared) {
  $oldStore = Join-Path $Current "store"
  if (-not (Test-Path $oldStore -PathType Container)) { return }
  $newStore = Join-Path $Prepared "store"
  New-Item -ItemType Directory -Force $newStore | Out-Null
  Get-ChildItem -LiteralPath $oldStore -Force |
    Copy-Item -Destination $newStore -Recurse -Force -ErrorAction Stop
}

function Switch-PreparedDirectory($Source, $Target) {
  $backup = $null
  if (Test-Path $Target) {
    $backup = Join-Path $BackupRoot ((Split-Path $Target -Leaf) + ".previous." + [guid]::NewGuid())
    Move-Item $Target $backup -ErrorAction Stop
  }
  try {
    Move-Item $Source $Target -ErrorAction Stop
  } catch {
    Restore-Backup $Target $backup
    throw
  }
}

function Install-VersionedDirectory($Archive, $Member, $Target) {
  $parent = Split-Path $Target
  New-Item -ItemType Directory -Force $parent | Out-Null
  $stage = Join-Path $parent (".market-data-stage-" + [guid]::NewGuid())
  try {
    Expand-Archive $Archive -DestinationPath $stage -ErrorAction Stop
    $source = Join-Path $stage $Member
    if (-not (Test-Path $source -PathType Container)) { throw "Missing $Member in $Archive" }
    if ($Member -eq "market-data") {
      Preserve-MarketDataStore $Target $source
      Protect-MarketDataEnv $source
    }
    Switch-PreparedDirectory $source $Target
  } finally {
    if (Test-Path $stage) { Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue }
  }
}

function Install-FlatDirectory($Archive, $Target) {
  $parent = Split-Path $Target
  New-Item -ItemType Directory -Force $parent | Out-Null
  $stage = Join-Path $parent (".market-data-stage-" + [guid]::NewGuid())
  try {
    Expand-Archive $Archive -DestinationPath $stage -ErrorAction Stop
    if (-not (Test-Path (Join-Path $stage ".codex-plugin\plugin.json"))) {
      throw "Plugin manifest missing in $Archive"
    }
    Switch-PreparedDirectory $stage $Target
  } finally {
    if (Test-Path $stage) { Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue }
  }
}

Install-VersionedDirectory .\market-data-openai-skill-v1.165.0-private.zip market-data `
  "$HOME\.agents\skills\market-data"
Install-VersionedDirectory `
  .\financial-deep-analysis-plugin-v0.2.0+codex.20260806151822.zip `
  financial-deep-analysis "$HOME\plugins\financial-deep-analysis"
Install-FlatDirectory .\databento-chatgpt-plugin.zip "$HOME\plugins\databento-market-data"
```

После безопасной замены повторите регистрацию plugins:

```bash
codex plugin add databento-market-data@personal
codex plugin add financial-deep-analysis@personal
```

Не удаляйте домашние каталоги рекурсивно. Проверив новую версию, резервные копии из созданного
подкаталога `~/.market-data-backups/` можно заархивировать или удалить по одной. После обновления
запустите ChatGPT заново и начните новую задачу.

При обновлении накопленный `market-data/store/` копируется в подготовленную новую версию до
переключения. При совпадении файлов действующая локальная история имеет приоритет над содержимым
нового архива; `store.tgz` остаётся неизменяемым резервным seed-слоем.

При замене ключей:

1. обновите приватный `market-data` архив или установленный `.env`;
2. установите для `.env` права `chmod 600` на macOS или повторите команду `icacls` на Windows;
3. для Alpha Vantage OAuth отдельно отключите и заново подключите приложение,
   если менялся Alpha Vantage key.

## 10. Поддержка облака

| Поверхность | Статус |
|---|---|
| ChatGPT Desktop → Work | Полный локальный комплект поддержан |
| ChatGPT Desktop → Codex | Полный локальный комплект поддержан |
| ChatGPT Desktop → Work/Codex на Windows 11 | Целевая нативная конфигурация через PowerShell |
| ChatGPT Desktop на актуальной Windows 10 | Best effort; нужен современный ConPTY |
| Codex CLI | Skills и локальные plugins поддержаны; начать новую сессию после установки |
| ChatGPT Work в браузере/облаке | Alpha Vantage OAuth поддержан; локальные архивы сами туда не переносятся |
| Hosted Databento MCP | Пока не готов для пользователя: требуется развёрнутый HTTPS endpoint и подтверждённая регистрация/auth |

Не обещайте пользователю полный Databento в hosted/cloud, пока не пройдены
регистрация, OAuth/auth-проверка и реальный вызов `get_session_info` из облачного
ChatGPT Work.

## 11. Итоговый чек-лист

- [ ] Private skill распакован в `~/.agents/skills/market-data`.
- [ ] `.env` имеет права `600`, оба ключа обнаружены doctor-проверкой.
- [ ] Databento plugin распакован в `~/plugins/databento-market-data`.
- [ ] Financial plugin распакован в `~/plugins/financial-deep-analysis`.
- [ ] В `codex plugin list` оба plugins имеют статус `installed, enabled`.
- [ ] Официальный Alpha Vantage MCP подключён по OAuth без ключа в URL.
- [ ] ChatGPT Desktop полностью перезапущен, создана новая задача.
- [ ] Пройдены четыре пользовательских smoke-проверки из раздела 7.
