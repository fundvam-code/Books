# Развёртывание

Нужны Docker Engine и Docker Compose версии 2.30 или новее на сервере и на ПК. Браузер на ПК открывает дашборд по HTTPS. Агент на этом же ПК забирает задания с сервера, обращается к Seller API с российского IP и отправляет результаты серверу. Веб-приложение, очередь заданий и PostgreSQL работают на сервере.

Для Caddy нужны входящие подключения на порты 80 и 443. Если используете домен, направьте его на публичный адрес сервера. Если используете публичный IPv4 сервера напрямую, Caddy получит для него короткоживущий сертификат Let's Encrypt.

Клонируйте [репозиторий](https://github.com/fundvam-code/Books) отдельно на сервере и на ПК.

```bash
git clone https://github.com/fundvam-code/Books.git marketplace-dashboard
cd marketplace-dashboard
```

## Сервер

```bash
cp .env.server.example .env.server
chmod 600 .env.server
```

Заполните `PUBLIC_DOMAIN` именем домена или публичным IPv4 без схемы, порта и пути. Для IPv4 задайте `DASHBOARD_TLS_MODE=ip`. Задайте `DASHBOARD_USERNAME` без пробелов. Создайте разные случайные значения для `POSTGRES_PASSWORD` и `AGENT_SHARED_SECRET`, например с помощью `openssl rand -hex 32` для каждого. Скопируйте секрет агента на ПК по защищённому каналу.

Сгенерируйте хеш пароля дашборда интерактивно.

```bash
docker run --rm -it caddy:2-alpine caddy hash-password
```

Впишите хеш в одинарных кавычках в `DASHBOARD_PASSWORD_HASH`. Запустите сервисы и откройте `/orders` по HTTPS с логином и паролем дашборда.

```bash
docker compose --env-file .env.server -f compose.server.yaml up -d --build --wait
docker compose --env-file .env.server -f compose.server.yaml ps
```

## ПК с агентом

```bash
cp .env.agent.example .env.agent
chmod 600 .env.agent
```

Укажите полный HTTPS-адрес дашборда без пути в `DASHBOARD_URL` и тот же `AGENT_SHARED_SECRET`. Укажите в `OZON_ENV_FILE` путь к отдельному файлу с ключами Ozon. Относительный путь считается от папки проекта. Для существующего файла на один уровень выше подходит `../.env`. Для нового файла используйте [.env.ozon.example](../.env.ozon.example) как образец. Записывайте `OZON_CLIENT_ID_1` и `OZON_API_KEY_1` для «Золотой антилопы», а `OZON_CLIENT_ID_2` и `OZON_API_KEY_2` для «Зеленой книги». Указывайте значения без кавычек.

```bash
docker compose --env-file .env.agent -f compose.agent.yaml up -d --build
docker compose --env-file .env.agent -f compose.agent.yaml ps
```

В дашборде появится состояние «Агент в сети», затем время обновления каждого кабинета. Если один кабинет временно недоступен, второй продолжит обновляться.

### Прямая печать на MacBook

Добавьте принтер этикеток в настройках macOS. Выполните `lpstat -p -d`, чтобы посмотреть имена очередей. В `.env.agent` задайте `OZON_PRINTER` именем нужной очереди. Значение `default` выбирает системный принтер по умолчанию. Проверьте формат этикетки в настройках этой очереди.

Для доступа к местной очереди печати запустите агент в Терминале macOS. Если агент уже запущен в Docker на этом MacBook, остановите его перед переходом на нативный запуск.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install 'httpx>=0.28,<1'
.venv/bin/python -m agent.run_macos
```

Используйте Python 3.11 или новее. Нативный агент читает те же `.env.agent` и файл ключей Ozon. Окно Терминала должно оставаться открытым, пока агент обслуживает заказы и печать.

Для диагностики конфигурации Compose используйте `config --quiet` в отдельной копии с фиктивными данными. Обычный `config` может вывести подставленные значения.
