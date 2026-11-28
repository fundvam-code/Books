# Запуск на macOS

Понадобятся Docker Desktop, Python 3.12 или новее, подключённый принтер macOS и доступ к API подключаемых маркетплейсов.

## Настройка

В корне нового клона выполните:

```bash
umask 077
cp .env.macos.example .env.macos
cp .env.agent.example .env.agent
python3 -m venv .venv
.venv/bin/python -m pip install 'httpx>=0.28,<1'
lpstat -p -d
```

- В `.env.macos` заполните `POSTGRES_PASSWORD` и `AGENT_SHARED_SECRET` независимыми секретами. Для генерации каждого используйте `openssl rand -hex 32`. Для каталога мест хранения можно указать `BOOK_STORAGE_SHEET_ID` и `BOOK_STORAGE_SHEET_GID`.
- В `.env.agent` укажите тот же `AGENT_SHARED_SECRET`. По умолчанию `OZON_PRINTER=default` использует принтер macOS по умолчанию. При смене порта в `.env.macos` поменяйте порт и в `DASHBOARD_URL`.
- Для Ozon скопируйте `.env.ozon.example` в `.env.ozon` и заполните Client ID и API-ключ каждого используемого кабинета. Для Wildberries скопируйте `.env.wb.example` в `.env.wb` и заполните токен Marketplace. `WB_CONTENT_TOKEN` используется для карточек товаров, а `WB_WRITE_TOKEN` с правами записи нужен для сборки.
- При обновлении действующей установки используйте текущую `.env.macos` и прежнее значение `COMPOSE_PROJECT_NAME`. По этому имени Compose находит рабочие тома базы, этикеток и сертификата.

## Запуск

```bash
mac() { docker compose --env-file .env.macos -f compose.macos.yaml "$@"; }
mac up -d --build --wait
mkdir -p data
mac exec -T caddy cat /data/caddy/pki/authorities/local/root.crt > data/macos-caddy-root.crt
curl --fail --cacert data/macos-caddy-root.crt https://127.0.0.1:8443/health
.venv/bin/python -m agent.run_macos
```

Добавьте `data/macos-caddy-root.crt` в связку ключей входа с доверием для SSL. Откройте `https://127.0.0.1:8443/orders`. Агент должен появиться в статусе «в сети».

Для автозапуска остановите ручной процесс агента и установите пользовательский LaunchAgent:

```bash
agent_plist="$(.venv/bin/python scripts/install-macos-agent.py)"
plutil -lint "$agent_plist"
launchctl bootstrap "gui/$(id -u)" "$agent_plist"
launchctl print "gui/$(id -u)/com.fundvam.books.agent"
```

Журнал агента находится в `data/macos-agent.log`. После обновления его кода или ключей выполните `launchctl kickstart -k "gui/$(id -u)/com.fundvam.books.agent"`, когда агент не занят сборкой или печатью. Для непрерывной обработки Mac должен быть включён, а пользователь — находиться в системе.

## Обновления и резервные копии

Для обновления выполните `git pull --ff-only`, затем `mac up -d --build --wait`. Для согласованной копии дождитесь завершения активных заданий и остановите дашборд, оставив PostgreSQL запущенным:

```bash
mac stop dashboard
scripts/dashboard-data backup "$HOME/books-backup-ДАТА"
mac up -d --wait dashboard
```

Копия включает базу и PDF. Храните её отдельно от Git. Для восстановления задайте новое имя проекта Compose, соберите образ дашборда и запустите только базу. Если рабочий проект продолжает работать, экспортируйте также свободный порт, например `DASHBOARD_HTTPS_PORT=8444`.

```bash
export COMPOSE_PROJECT_NAME=books-restore
mac build dashboard
mac up -d --wait database
scripts/dashboard-data restore "$HOME/books-backup-ДАТА"
mac up -d --wait
```

После восстановления проверьте журнал печати и незавершённые задачи до подключения агента. Новый проект выпускает свой сертификат Caddy.
