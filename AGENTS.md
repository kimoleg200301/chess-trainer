# Обновление рабочего сервера

## Окружение

- Репозиторий: `https://github.com/kimoleg200301/chess-trainer`, ветка `main`.
- SSH через Tailscale: `root@100.105.139.68`, порт `2222`.
- Каталог на сервере: `/opt/chess-trainer`; сайт: `http://100.105.139.68:8080`.
- Ubuntu 24.04 в WSL2 без systemd. Используй Docker Compose; для восстановления запуска — `/usr/local/sbin/chess-trainer-boot`.

## Правила

- Готовь изменения в рабочем репозитории. Для развёртывания используй проверенный коммит, опубликованный в GitHub.
- Сохраняй серверные `.env`, `compose.wsl.yml`, настройки автозапуска и том PostgreSQL. Изучи локальные изменения до обновления; не затирай их. Серверный `compose.wsl.yml` ожидаемо может быть untracked.
- Всегда используй `compose.yml` вместе с `compose.wsl.yml`: override отключает bootstrap восстановленной базы. `AUTH_PASSWORD=disabled` — предохранитель, а не пароль входа; аккаунты хранятся в PostgreSQL.
- Не запускай `app.bootstrap`, `scripts/configure.py`, `docker compose down -v` и удаление томов при обычном обновлении.
- Не добавляй в Git секреты, `.env` и дампы. Сохраняй работающий Render для отката.

## Порядок обновления

Выполняй команды на сервере последовательно; при ошибке останови обновление.
Перед `pull` проверь локальные изменения, запиши текущий SHA и путь созданного дампа.
Прежде чем продолжить после backup, проверь ненулевой размер и читаемость дампа через `pg_restore --list` в контейнере `db`, сохрани копию вне сервера.

```bash
cd /opt/chess-trainer
git status --short
git rev-parse HEAD
COMPOSE_FILE=compose.yml:compose.wsl.yml bash scripts/backup.sh
git pull --ff-only origin main
docker compose --env-file .env -f compose.yml -f compose.wsl.yml config --quiet
docker compose --env-file .env -f compose.yml -f compose.wsl.yml build
docker compose --env-file .env -f compose.yml -f compose.wsl.yml up -d --wait --wait-timeout 180
docker compose --env-file .env -f compose.yml -f compose.wsl.yml ps
curl --fail --silent --show-error --max-time 15 http://127.0.0.1:8080/api/health
```

- Образы приложения собираются на сервере, GHCR не используется.
- После запуска проверь health, журналы и затронутые функции; вход, тренировку и анализ проверяй отдельным тестовым аккаунтом, сохраняя личную статистику пользователя.
- Откат к предыдущему коду после миграций БД требует проверки совместимости схемы. Не восстанавливай старый дамп поверх новых пользовательских данных автоматически.
- В результате сообщи установленный SHA, путь бэкапа, выполненные проверки и ограничения. При обычном обновлении приложения не перезапускай Windows/WSL, SSH и Tailscale.
