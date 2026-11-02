# Основной рабочий сервер

Эта инструкция применяется к установленному тренажёру и его рабочим данным.

| Параметр | Значение |
| --- | --- |
| Основной сайт | http://178.88.83.132:18080 |
| Доступ через Tailscale | http://100.105.139.68:8080 |
| SSH через Tailscale | `ssh -p 2222 root@100.105.139.68` |
| Каталог и ветка | `/opt/chess-trainer`, `main` |
| Система | Ubuntu 24.04, WSL2, без systemd |
| Сервисы Compose | `frontend`, `backend`, `db`; PostgreSQL 17, Stockfish |
| Конфигурация | `.env`, `compose.yml` и серверный `compose.wsl.yml` |

Основная PostgreSQL находится в томе `pgdata` этого Compose-проекта. Все новые партии, аккаунты, задачи, отчёты и статистика сохраняются здесь. Исходники обновляются через GitHub; образы приложения собираются на сервере.

## Адреса и конфигурация

Путь запроса: внешний TCP `18080` → Windows `192.168.1.66:18080` → текущий IP WSL, порт `8080` → nginx → FastAPI. В `.env` внутренний `APP_PORT` остаётся `8080`. Разрешённые `ALLOWED_ORIGINS` включают `http://178.88.83.132:18080` и используемые адреса Tailscale. Сохраняй прежние разрешённые адреса при добавлении нового.

Серверный `compose.wsl.yml` публикует frontend на WSL `0.0.0.0:8080`, запускает миграции и API без bootstrap. Он задаёт пустой `AUTH_PASSWORD` для backend, сохраняя пароли и сессии существующих аккаунтов в PostgreSQL. Значение `AUTH_PASSWORD=disabled` в `.env` — предохранитель Compose, а не пароль пользователя.

Файлы `.env`, `compose.wsl.yml` и `DEPLOYMENT.txt` принадлежат установленному экземпляру. Последние два могут быть untracked. Не затирай их при обновлении. Не запускай `scripts/configure.py`, `app.bootstrap`, `docker compose down -v` и удаление томов на рабочем экземпляре. Секреты, конфигурацию с паролями и дампы не коммить.

## Обновление приложения

Подготовь и проверь изменения в рабочем репозитории, опубликуй выбранный коммит в `main`. На сервере выполняй блоки последовательно; при ошибке остановись. Команды ниже предполагают одну Bash-сессию. Если изменена только документация, достаточно проверить дифф и обновить Git через fast-forward; сборка, перезапуск и дамп БД для этого не нужны.

### 1. Проверь текущий код и целевой коммит

```bash
set -euo pipefail
cd /opt/chess-trainer
dc() { docker compose --env-file .env -f compose.yml -f compose.wsl.yml "$@"; }
git status --short
git diff --quiet
git diff --cached --quiet
CHESS_PREVIOUS_SHA="$(git rev-parse HEAD)"
git fetch origin
CHESS_TARGET_SHA="$(git rev-parse origin/main)"
git log --oneline HEAD.."$CHESS_TARGET_SHA"
git diff --stat HEAD "$CHESS_TARGET_SHA"
```

Проверки `git diff` останавливают обновление при изменениях отслеживаемых файлов; ожидаемые untracked-файлы сервера им не мешают. Изучи остальные локальные файлы и содержимое входящего диффа, особенно миграции, Dockerfile и Compose. Если история разошлась или есть незнакомые изменения, разберись с ними до обновления. Не применяй `reset --hard` и `git clean` к серверному каталогу.

### 2. Сохрани данные и конфигурацию

```bash
umask 077
mkdir -p backups
CHESS_STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
CHESS_DUMP="backups/tactics-${CHESS_STAMP}.dump"
printf '%s\n' "$CHESS_PREVIOUS_SHA" > "backups/code-${CHESS_STAMP}.txt"
cp .env "backups/env-${CHESS_STAMP}"
cp compose.wsl.yml "backups/compose.wsl-${CHESS_STAMP}.yml"
dc exec -T db pg_dump -U trainer -d tactics --format=custom > "$CHESS_DUMP"
test -s "$CHESS_DUMP"
dc exec -T db pg_restore --list < "$CHESS_DUMP" > "${CHESS_DUMP}.toc"
sha256sum "$CHESS_DUMP"
printf 'Backup: /opt/chess-trainer/%s\n' "$CHESS_DUMP"
```

До продолжения скопируй дамп и конфигурацию в защищённое хранилище вне сервера и проверь контрольную сумму копии. Для передачи подходит `scp -P 2222` через Tailscale с фактическими путями созданных файлов. Дамп и `.env` содержат закрытые данные. Обычный независимый бэкап также можно сделать командой `COMPOSE_FILE=compose.yml:compose.wsl.yml bash scripts/backup.sh`.

### 3. Обнови и проверь приложение

```bash
git merge --ff-only "$CHESS_TARGET_SHA"
test "$(git rev-parse HEAD)" = "$CHESS_TARGET_SHA"
dc config --quiet
dc build
dc up -d --wait --wait-timeout 180
dc exec -T frontend nginx -t
dc exec -T frontend nginx -s reload
dc ps
curl --fail --silent --show-error --max-time 15 http://127.0.0.1:8080/api/health
git rev-parse HEAD
```

Перечитывание nginx обновляет адрес upstream после пересоздания backend. В health ожидаются `status=ok`, `database=ok`, `engine_available=true`. Просмотри `dc logs --tail=80 backend frontend`, не публикуя секреты и персональные данные из журналов.

С устройства вне домашней сети проверь `http://178.88.83.132:18080/api/health` и страницу входа. Проверка внешнего адреса с самого сервера может зависеть от поддержки NAT loopback роутером. Для проверки входа, тренировки, анализа Stockfish и сохранения результатов используй отдельный тестовый аккаунт; затем удали только созданные им данные. Зафиксируй установленный SHA, пути резервных копий и результаты проверок.

## Откат

Используй сохранённый предыдущий SHA и серверную конфигурацию. До запуска старого кода проверь совместимость с текущей схемой PostgreSQL: миграции уже могли изменить её. Старый дамп не восстанавливай поверх новых пользовательских данных автоматически. При необходимости восстановления сначала сохрани текущее состояние и проверь процедуру на отдельной базе. Переключение кода не должно удалять том `pgdata`.

## Автозапуск и ограничения

- Linux запускает Docker и Compose через `/usr/local/sbin/chess-trainer-boot`; `systemctl` здесь не используется.
- В Windows зарегистрирована задача `ChessTrainer-WSL-Forward-18080` при входе текущего пользователя. Она запускает `Ubuntu-SSH`, определяет IP WSL и обновляет проброс `18080 → 8080`.
- Установленный helper: `%ProgramData%\ChessTrainer-WslForward\Enable-ChessTrainerForward-18080.ps1`. При отдельном перезапуске WSL, когда пользователь уже вошёл в Windows, запусти задачу из PowerShell от администратора: `Start-ScheduledTask -TaskName 'ChessTrainer-WSL-Forward-18080'` и проверь доступность сайта после её завершения.
- Регистрация задачи и локальная/внешняя доступность проверены. Полная перезагрузка Windows и срабатывание по событию входа ещё не проверялись. До входа пользователя доступ не гарантирован; сон и выключение компьютера останавливают сайт.
- Основной адрес сейчас использует HTTP. HTTPS требует отдельной настройки домена и TLS. При смене публичного IP обнови адрес и `ALLOWED_ORIGINS`; закрепи LAN-адрес Windows в DHCP.
- Обычное обновление приложения не требует перезапуска Windows, WSL, SSH, Tailscale или изменения правил роутера.
