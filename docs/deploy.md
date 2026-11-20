# Развёртывание

Нужны Docker Engine и Docker Compose версии 2.30 или новее на сервере и на ПК. На сервере направьте домен на адрес сервера и разрешите входящие подключения на порты 80 и 443 для Caddy. На ПК нужен исходящий доступ к дашборду и к Seller API из сети, с которой ваши кабинеты могут пользоваться API.

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

Заполните `PUBLIC_DOMAIN` именем домена без схемы, порта и пути. Задайте `DASHBOARD_USERNAME` без пробелов. Создайте разные случайные значения для `POSTGRES_PASSWORD` и `AGENT_SHARED_SECRET`, например с помощью `openssl rand -hex 32` для каждого. Скопируйте секрет агента на ПК по защищённому каналу.

Сгенерируйте хеш пароля дашборда интерактивно.

```bash
docker run --rm -it caddy:2-alpine caddy hash-password
```

Впишите хеш в одинарных кавычках в `DASHBOARD_PASSWORD_HASH`. Запустите сервисы и откройте `https://ваш-домен/orders` с логином и паролем дашборда.

```bash
docker compose --env-file .env.server -f compose.server.yaml up -d --build
docker compose --env-file .env.server -f compose.server.yaml ps
```

## ПК с агентом

```bash
cp .env.agent.example .env.agent
chmod 600 .env.agent
```

Укажите полный HTTPS-адрес дашборда без пути в `DASHBOARD_URL` и тот же `AGENT_SHARED_SECRET`. Укажите в `OZON_ENV_FILE` путь к отдельному файлу с ключами Ozon. Относительный путь считается от папки проекта. Для существующего файла на один уровень выше подходит `../.env`. Для нового файла используйте [.env.ozon.example](../.env.ozon.example) как образец. Записывайте `OZON_CLIENT_ID_1`, `OZON_API_KEY_1` и аналогичные переменные для второго кабинета без кавычек вокруг значений. Переменные `OZON_CLIENT_NAME_1` и `OZON_CLIENT_NAME_2` можно оставить для локальных заметок. В дашборде используются названия «Кабинет 1» и «Кабинет 2».

```bash
docker compose --env-file .env.agent -f compose.agent.yaml up -d --build
docker compose --env-file .env.agent -f compose.agent.yaml ps
```

В дашборде появится состояние «Агент в сети», затем время обновления каждого кабинета. Если один кабинет временно недоступен, второй продолжит обновляться.

Для диагностики конфигурации Compose используйте `config --quiet` в отдельной копии с фиктивными данными. Обычный `config` может вывести подставленные значения.
