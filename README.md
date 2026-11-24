# Marengo Market Data

Публичный каталог скилла для ChatGPT Work и Claude Cowork. Обе поставки собираются из одного коммита приватного Market Data Skill. Версия, исходный коммит и контрольные суммы файлов хранятся в [release.json](release.json).

[Инструкция для Windows](docs/WINDOWS_SETUP.md) содержит обновление уже установленного плагина и первое подключение. В настольном ChatGPT откройте **Marengo Data** из этого каталога. Его [нативная конфигурация OAuth](chatgpt/connectors/marengo-data/.mcp.json) входит в плагин. В Claude Cowork добавьте Marengo по HTTPS.

[Предыдущая Word-инструкция к личному плагину 0.1.4](docs/Marengo-Windows-Setup-1.169.2.docx?raw=true)

## Первое подключение

Клиентская редакция с обычными ценовыми графиками находится в `editions/price-charts` приватного `dqdp/market_data_skill`. Основа графиков и расчёта греков — версия 1.36.0, коммит `753c19b`. Версия 1.169.3 добавляет маршрутизацию котировок опционов США через OPRA и сохраняет цепочки Alpha Vantage. Каталог включает только маршрутизатор, справочник Alpha Vantage, построение ценового SVG и расчёт греков.

Клиент скачивает скилл и обновления из [публичного каталога](https://github.com/dqdp/marengo-marketplace) по HTTPS. Для чтения каталога не нужны аккаунт GitHub, приглашение, SSH-ключ или доступ к приватным репозиториям. Для рыночных данных пользователь входит в собственную приглашённую учётную запись Marengo.

**ChatGPT Work на компьютере.** Добавьте публичный каталог под пользователем настольного ChatGPT. Команды для новой установки и уже настроенного каталога приведены в разделе [Windows и Linux](#настольный-chatgpt-work-на-windows-и-linux).

**Claude Cowork.** Откройте **Cowork → Customize → Plugins → Add → Add marketplace → Add from a repository**, введите `dqdp/marengo-marketplace` и добавьте **Marengo Market Data** из этого каталога. Для последующих версий сохраняйте подключение репозитория. [Инструкция Anthropic](https://support.claude.com/en/articles/13837440-use-plugins-in-claude)

### Настольный ChatGPT Work на Windows и Linux

Под пользователем ОС, который запускает ChatGPT, добавьте публичный HTTPS-каталог из ветки `main`. На Windows откройте обычный PowerShell, на Linux — терминал того же пользователя.

```powershell
codex plugin marketplace add https://github.com/dqdp/marengo-marketplace.git --ref main
codex plugin add marengo-data@marengo
codex plugin add marengo-market-data@marengo
codex plugin list --marketplace marengo --json
```

Ожидаются включённые Marengo Data 0.1.6 и Marengo Market Data 1.169.3 или более новые версии. Если `marengo` уже зарегистрирован, проверьте источник и действуйте по разделу 4 инструкции. Для обновления каталога выполните `codex plugin marketplace upgrade marengo`, примените доступное обновление плагина и сверьте установленную версию. Полностью закройте и откройте настольный ChatGPT и проверьте **Work → Plugins → Marengo → Marengo Market Data** в новом чате. [Документация OpenAI](https://developers.openai.com/plugins/build/plugins#add-a-marketplace-from-the-cli)

При переходе с вручную загруженного скилла выключите прежнюю копию после установки из каталога. Если прежний ZIP распаковывали в личную папку skills, перенесите только старую папку market-data за пределы каталогов обнаружения.

**Подключение данных на чистом компьютере**

1. В настольном ChatGPT после установки **Marengo Data** откройте настройки MCP и пройдите OAuth для `marengo`. Для личной копии 0.1.4 из Plugin Creator и уже подключённого Git-каталога шаги приведены в [разделе 2 инструкции](docs/WINDOWS_SETUP.md#2-marengo-data-в-настольном-chatgpt-на-windows). В Claude Cowork следуйте разделу 3.

2. Для MOEX выпустите в БКС Trade API refresh-токен с правом «Только для чтения». В настольном ChatGPT откройте Настройки → Интеграции → Плагины → Добавить сервер MCP. Выберите HTTP и адрес `https://be.broker.ru/trade-api-mcp-server/mcp`. В Claude Desktop откройте Settings → Connectors → Add custom connector и задайте тот же адрес. Для интерфейса с секцией Request headers выберите No sign-in. Укажите в настройках подключения заголовок `Authorization` со значением `Bearer <refresh-токен>`. Если секция заголовков недоступна, используйте способ установки для своей ОС из официальной инструкции. Откройте новую сессию Work или Cowork и проверьте доступность инструментов БКС. [Инструкция БКС для обоих клиентов](https://trade-api.bcs.ru/mcp/)
3. Подключите существующий Alpha Vantage MCP с прежней авторизацией. В новой сессии запросите котировку SPY через Alpha Vantage, ценовой график CME через Marengo и bid/ask SBER через БКС. Для каждой проверки сверьте источник, инструмент и время котировки. Вне торговой сессии зафиксируйте время последнего обновления.

 После установки проверьте маршруты данных в новой сессии: CME через Marengo, текущие котировки MOEX через БКС, акции США и ETF через Alpha Vantage.

## Публикация обновления

1. Повысьте `version` в `editions/price-charts/SKILL.md`, проверьте изменение и отправьте исходный коммит в GitHub.
2. Оператор с доступом к обоим репозиториям запускает из копии каталога `python3 tools/propose.py ПОЛНЫЙ_SHA`. Используются обычные git/SSH и gh оператора. Команда собирает обе версии в отдельной временной папке и открывает PR в каталог. Повтор возвращает существующий PR или статус уже опубликованного SHA.
3. Проверьте CI и изменения, затем объедините PR в `main`. Обновите каталог на клиентском устройстве и сверьте версию плагина с release.json.
4. В каждом клиенте откройте новую сессию, проверьте установленную версию и запросите котировку по известному инструменту. Для возврата к прежней логике выпустите исправление с новым номером версии.

Локальная проверка каталога выполняется командой `python3 tools/release.py`. Для сверки с исходниками используйте `python3 tools/release.py --check-source /path/to/market_data_skill`.
