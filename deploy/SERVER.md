# Основной рабочий сервер

Эта инструкция применяется к установленному тренажёру и его рабочим данным.

| Параметр | Значение |
| --- | --- |
| Основной сайт, только через Tailscale | http://100.105.139.68:8080 |
| SSH через Tailscale | `ssh -p 2222 root@100.105.139.68` |
| Каталог и ветка | `/opt/chess-trainer`, `main` |
| Система | Ubuntu 24.04, WSL2, без systemd |
| Сервисы Compose | `frontend`, `backend`, `db`; PostgreSQL 17, Stockfish |
| Конфигурация | `.env`, `compose.yml` и серверный `compose.wsl.yml` |

Основная PostgreSQL находится в томе `pgdata` этого Compose-проекта. Все новые партии, аккаунты, задачи, отчёты и статистика сохраняются здесь. Исходники обновляются через GitHub; образы приложения собираются на сервере.

## Адреса и конфигурация

Подключи устройство к разрешённой сети Tailscale, открой `http://100.105.139.68:8080` и войди в аккаунт тренажёра. Подключение к Tailscale не заменяет пароль приложения.

Путь запроса: устройство в tailnet → Tailscale Serve на `100.105.139.68:8080` → WSL `127.0.0.1:8080` → nginx → FastAPI. В `.env` используются `APP_BIND_ADDRESS=127.0.0.1` и `APP_PORT=8080`. В `compose.wsl.yml` адрес закреплён непосредственно в публикации `127.0.0.1:${APP_PORT:-8080}:80`; смена `APP_BIND_ADDRESS` не должна открывать этот override наружу. Docker не должен публиковать сайт на `0.0.0.0`, LAN-адресе WSL или IPv6 wildcard. Проверяй итоговую конфигурацию обоих Compose-файлов, а не только значения `.env`.

`ALLOWED_ORIGINS` содержит `http://100.105.139.68:8080`, `http://localhost:8080` и `http://127.0.0.1:8080`. Публичные и LAN origins удалены; их не нужно сохранять или возвращать из старой конфигурации. Проверка Origin дополняет ограничение сетевого доступа и сама по себе не закрывает публичный сайт.

Серверный `compose.wsl.yml` публикует frontend на WSL `127.0.0.1:8080`, запускает миграции и API без bootstrap. Он задаёт пустой `AUTH_PASSWORD` для backend, сохраняя пароли и сессии существующих аккаунтов в PostgreSQL. Значение `AUTH_PASSWORD=disabled` в `.env` — предохранитель Compose, а не пароль пользователя.

Используется отдельный Tailscale daemon с сокетом `/run/tailscale-chess/tailscaled.sock`. Состояние маршрутов можно посмотреть командой `tailscale --socket=/run/tailscale-chess/tailscaled.sock serve status`. Serve работает только внутри tailnet; Funnel для этого сайта не включай. Не сбрасывай конфигурацию Serve целиком: она также обеспечивает SSH на TCP `2222`.

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

С другого разрешённого устройства с включённым Tailscale проверь `http://100.105.139.68:8080/api/health` и страницу входа. Затем отдельно проверь закрытый доступ: без Tailscale приложение не должно открываться по публичному IP через мобильную сеть и по LAN-адресу Windows через домашнюю сеть, включая прежние веб-порты `8080` и `18080`. На самом сервере health через `127.0.0.1:8080` должен продолжать работать. Проверь, что frontend не слушает WSL eth0 и что Funnel не включён.

Для проверки входа, тренировки, анализа Stockfish и сохранения результатов используй отдельный тестовый аккаунт; затем удали только созданные им данные. Зафиксируй установленный SHA, пути резервных копий, результаты разрешённого доступа и проверок закрытых адресов.

## Откат

Используй сохранённый предыдущий SHA с текущей политикой доступа только через Tailscale. Сверь старую конфигурацию перед восстановлением: loopback bind и ограниченный `ALLOWED_ORIGINS` должны сохраниться, Windows-проброс сайта и публичные адреса возвращать нельзя. До запуска старого кода проверь совместимость с текущей схемой PostgreSQL: миграции уже могли изменить её. Старый дамп не восстанавливай поверх новых пользовательских данных автоматически. При необходимости восстановления сначала сохрани текущее состояние и проверь процедуру на отдельной базе. Переключение кода не должно удалять том `pgdata`.

## Автозапуск и ограничения

- Linux запускает Docker и Compose через `/usr/local/sbin/chess-trainer-boot`; `systemctl` здесь не используется.
- После переключения проверены health через Tailscale и loopback, отсутствие ответа приложения на прежних публичных и Windows LAN-портах, а также отказ соединения с WSL eth0 на `8080`. У отдельного Tailscale daemon сохранены Serve-маршруты `2222` и `8080` на loopback; у второго daemon Serve не настроен. Funnel не включён.
- Windows должен запускать `Ubuntu-SSH` при входе владельца дистрибутива без создания веб-пробросов. Прежние правила Windows для веб-портов и задача `ChessTrainer-WSL-Forward-18080` подлежат отдельной проверке и очистке; не применяй старый helper публичного доступа. Установка замены этой задачи пока не подтверждена.
- При отдельном запуске WSL его Linux boot hook восстанавливает Compose и Tailscale. Полная перезагрузка Windows и срабатывание задачи по событию входа ещё не проверялись. До входа пользователя доступ не гарантирован; сон и выключение компьютера останавливают сайт.
- Браузер использует HTTP внутри сети Tailscale; между узлами действует шифрование Tailscale. Публичный IP и веб-проброс на роутере для этого режима не используются.
- Обычное обновление приложения не требует перезапуска Windows, WSL, SSH или Tailscale. Не включай публичный доступ при обновлении или откате.
