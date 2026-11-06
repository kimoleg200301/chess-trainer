# Основной рабочий сервер

Тренажёр работает на ПК с Windows, Docker Desktop и Ubuntu в WSL2. Mac используется для разработки и доступа через WireGuard; для работы уже развёрнутого сайта Mac может быть выключен.

| Параметр | Значение |
| --- | --- |
| Основной сайт, только через WireGuard | http://10.66.0.2:8080 |
| SSH через WireGuard | `ssh -p 22 root@10.66.0.2` |
| Каталог на сервере | `/opt/chess-trainer` |
| Ветка на сервере | `server-wireguard`, отслеживает `origin/main` |
| Репозиторий для разработки на Mac | `/Users/kim/IdeaProjects/chess-trainer` |
| Окружение | Ubuntu 24.04.3, WSL2 с systemd; Docker Desktop, x86_64 |
| Проект Compose | `chess-trainer`; сервисы `frontend`, `backend`, `db` |
| Конфигурация | `.env`, `compose.yml`, серверный `compose.wireguard.yml` |
| Основная база | PostgreSQL 17, БД `tactics`, роль `trainer`, том `chess-trainer_pgdata` |

Backend, база и Stockfish работают на новом ПК. Frontend отдаётся его nginx и отображается браузером. Данные PostgreSQL находятся в томе Docker Desktop, а не в каталоге исходников. Образы приложения собираются на сервере из опубликованного кода GitHub. Другой проект `trueface-cv` и его PostgreSQL 16 обслуживаются отдельно; их контейнеры, тома и порт `5432` не меняй.

## Доступ и серверная конфигурация

Путь запроса: устройство с WireGuard → Windows `10.66.0.2:8080` (интерфейс `client1`) → Windows `127.0.0.1:18080` → frontend Docker Desktop → FastAPI. WireGuard не заменяет вход в аккаунт приложения. HTTP используется внутри зашифрованного туннеля.

Серверный `compose.wireguard.yml` фиксирует публикацию frontend как `127.0.0.1:18080:80`. Windows portproxy слушает только `10.66.0.2:8080`; правило firewall ограничено этим адресом и интерфейсом `client1`. Динамический IP WSL в маршруте не используется. Проброс портов роутера не нужен. Сохраняй закрытый доступ: не публикуй приложение на `0.0.0.0`, LAN или IPv6 wildcard; PostgreSQL вообще не публикует порт хоста.

В `.env` заданы `COMPOSE_PROJECT_NAME=chess-trainer`, `COMPOSE_FILE=compose.yml:compose.wireguard.yml`, `APP_BIND_ADDRESS=127.0.0.1`, `APP_PORT=18080`, `COOKIE_SECURE=false`. `ALLOWED_ORIGINS` содержит только `http://10.66.0.2:8080`, `http://localhost:18080` и `http://127.0.0.1:18080`. Origin-проверка дополняет сетевые ограничения.

Override запускает Alembic и Uvicorn без `app.bootstrap` и задаёт пустой `AUTH_PASSWORD` для backend. Значение `AUTH_PASSWORD=disabled` в `.env` — предохранитель Compose, а не пароль входа. Аккаунты и их хеши паролей уже восстановлены из PostgreSQL.

Файлы `.env`, `compose.wireguard.yml`, `DEPLOYMENT.txt` принадлежат установленному экземпляру. Последние два могут быть untracked; изучай и сохраняй их при обновлении. Не запускай `scripts/configure.py`, `app.bootstrap`, `docker compose down -v` или удаление томов на рабочем экземпляре. Не добавляй секреты, дампы и закрытые архивы в Git.

Всегда указывай проект и оба Compose-файла явно. Не устанавливай второй Docker Engine в Ubuntu и не останавливай общий Docker Desktop для обычного обновления тренажёра.

## Обновление приложения

Разрабатывай и проверяй код на Mac, публикуй выбранный коммит в GitHub `main`, затем обновляй сервер. Выполняй блоки последовательно в одной Bash-сессии сервера; при ошибке остановись. Для изменений только документации достаточно проверки диффа и fast-forward: бэкап, сборка и перезапуск приложения не нужны.

### 1. Проверь текущий код и целевой коммит

```bash
set -euo pipefail
export LC_ALL=C
cd /opt/chess-trainer
dc() { docker compose -p chess-trainer --env-file .env -f compose.yml -f compose.wireguard.yml "$@"; }
git status --short --branch
git diff --quiet
git diff --cached --quiet
CHESS_PREVIOUS_SHA="$(git rev-parse HEAD)"
git fetch origin
CHESS_TARGET_SHA="$(git rev-parse origin/main)"
git merge-base --is-ancestor "$CHESS_PREVIOUS_SHA" "$CHESS_TARGET_SHA"
git log --oneline HEAD.."$CHESS_TARGET_SHA"
git diff --stat HEAD "$CHESS_TARGET_SHA"
```

Изучи входящий дифф, особенно миграции, Dockerfile и Compose. Ожидаемые untracked-файлы сервера не мешают проверке `git diff`; остальные локальные файлы также проверь. При разошедшейся истории или изменённых отслеживаемых файлах разберись с причиной. Не применяй `reset --hard` и `git clean` к серверному каталогу.

### 2. Сохрани данные и конфигурацию

```bash
umask 077
mkdir -p backups
CHESS_STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
CHESS_BACKUP="backups/update-${CHESS_STAMP}"
mkdir "$CHESS_BACKUP"
printf '%s\n' "$CHESS_PREVIOUS_SHA" > "$CHESS_BACKUP/code.sha"
cp .env compose.wireguard.yml "$CHESS_BACKUP/"
if [ -f DEPLOYMENT.txt ]; then cp DEPLOYMENT.txt "$CHESS_BACKUP/"; fi
dc exec -T db pg_dump -U trainer -d tactics --format=custom > "$CHESS_BACKUP/tactics.dump.partial"
test -s "$CHESS_BACKUP/tactics.dump.partial"
dc exec -T db pg_restore --list < "$CHESS_BACKUP/tactics.dump.partial" > "$CHESS_BACKUP/tactics.toc"
mv "$CHESS_BACKUP/tactics.dump.partial" "$CHESS_BACKUP/tactics.dump"
(cd "$CHESS_BACKUP" && sha256sum tactics.dump .env compose.wireguard.yml code.sha tactics.toc > SHA256SUMS)
printf 'Backup: /opt/chess-trainer/%s\n' "$CHESS_BACKUP"
```

До обновления сохрани этот каталог вне сервера. Например, в отдельном Терминале Mac с WireGuard подставь фактическое имя каталога из вывода:

```bash
(
set -e
umask 077
CHESS_BACKUP_NAME='update-YYYYMMDDTHHMMSSZ'
mkdir -p /Users/kim/chess-backups
scp -r -P 22 "root@10.66.0.2:/opt/chess-trainer/backups/${CHESS_BACKUP_NAME}" /Users/kim/chess-backups/
(cd "/Users/kim/chess-backups/${CHESS_BACKUP_NAME}" && shasum -a 256 -c SHA256SUMS)
)
```

Дамп и `.env` содержат закрытые данные. Копия только внутри Docker Desktop или на том же ПК не защищает от его потери. Проверяй восстановление резервных копий в отдельную базу; не используй рабочую базу для проверки восстановления.

### 3. Обнови и проверь приложение

После проверки внешней копии продолжай в исходной Bash-сессии сервера:

```bash
git merge --ff-only "$CHESS_TARGET_SHA"
test "$(git rev-parse HEAD)" = "$CHESS_TARGET_SHA"
dc config --quiet
dc build backend frontend
dc up -d --wait --wait-timeout 240
dc exec -T frontend nginx -t
dc exec -T frontend nginx -s reload
dc exec -T backend python -c 'import json,urllib.request; h=json.load(urllib.request.urlopen("http://127.0.0.1:8000/api/health",timeout=10)); assert h.get("status")=="ok" and h.get("database")=="ok" and h.get("engine_available") is True, h; print(json.dumps(h))'
dc ps
git rev-parse HEAD
```

Перечитывание nginx обновляет upstream после пересоздания backend. Просмотри `dc logs --tail=80 backend frontend`, не публикуя секреты или персональные данные. Внутренняя проверка API не проверяет Windows forwarding: отдельно на Mac с WireGuard выполни:

```bash
curl --fail --silent --show-error --max-time 15 http://10.66.0.2:8080/api/health
```

Ожидаются `status=ok`, `database=ok`, `engine_available=true`. Проверь страницу входа, тренировку, анализ Stockfish и сохранение результатов отдельным тестовым аккаунтом; затем удали только его тестовые данные. На устройстве в LAN без VPN проверь отсутствие доступа через LAN-адрес Windows на портах `8080` и `18080`; с внешней сети без VPN — через публичный адрес. Отказ с устройства, у которого вообще нет маршрута к проверяемой LAN, не доказывает закрытость сервиса.

При необходимости проверь `http://127.0.0.1:18080/api/health` в Windows. Доступность этого порта на loopback конкретного WSL-дистрибутива отдельно не подтверждена, поэтому она не является единственной проверкой установки. Зафиксируй SHA, путь бэкапа и результаты проверок.

## Автозапуск и резервное копирование

- У всех трёх сервисов есть `restart: unless-stopped`: контейнеры возобновляют работу после запуска Docker Desktop, если не были явно остановлены. systemd в Ubuntu не запускает Windows Docker Desktop.
- В Windows настрой запуск Docker Desktop при входе его пользователя и автоматический запуск нужного WireGuard-туннеля. SSH-доступ к WSL также должен восстанавливаться штатным механизмом этого ПК. Не заменяй общую настройку Docker или других проектов.
- Windows portproxy и firewall сохраняются после перезагрузки. Подготовленный `Enable-ChessTrainerWireGuard.ps1` уже успешно выполнен с правами администратора Windows; повторный запуск нужен только для проверки/восстановления этой конфигурации, а не для каждого обновления приложения.
- Автозапуск после полной перезагрузки пока не проверен. Проверь его в согласованное время, имея доступ к Windows на случай потери SSH. Не перезагружай удалённый ПК автоматически ради проверки. До входа пользователя доступ не гарантирован; сон и выключение ПК останавливают приложение.
- Регулярное резервное копирование ещё не настроено. Настрой ежедневный полный `pg_dump` по схеме выше и перенос копии в отдельное хранилище; контролируй результат, срок хранения и пробное восстановление. Предварительный бэкап перед обновлением остаётся обязательным.

## Статус переноса и откат

6 ноября 2026 года установлены код `7fecc367d316b2804711a94cb114bd366e8b3b2b` и снимок PostgreSQL от **04:27 UTC**. Полные данные 16 таблиц проверены до запуска приложения. Единственное учтённое различие — доказанно эквивалентная запись одного CHECK-ограничения после `pg_restore`; остальные данные и структура проверялись строго. Отчёты установки находятся в `/opt/chess-trainer/backups/migration-20261106T045001Z`.

По выводу установки подтверждены восстановление базы, health API и реальный поиск Stockfish. Windows helper подтвердил локальный health через forwarding; пользователь подтвердил открытие сайта с Mac через WireGuard. Полная пользовательская проверка входа, тренировок и анализа, проверки запрета LAN/публичного доступа и тест перезагрузки пока отдельно не подтверждены.

Прежний сервер `100.105.139.68:2222` сохранён через Tailscale как резерв перехода. Это независимая база: изменения на нём после снимка автоматически не переносятся, а новые записи основного сервера туда не реплицируются. Используй новый сайт для дальнейшей работы; не обновляй старый сервер в рамках обычного релиза. До его отключения проверь, не осталось ли на нём неперенесённых записей после 04:27 UTC.

Для отката на основном сервере используй сохранённый предыдущий SHA после проверки совместимости с текущей схемой БД. Сохраняй `compose.wireguard.yml`, проект `chess-trainer`, том и закрытый маршрут WireGuard. Старый дамп не восстанавливай поверх новых пользовательских данных автоматически: сначала сохрани текущее состояние и проверь восстановление отдельно. Возврат к прежнему ПК также требует отдельного согласования данных, чтобы не потерять записи нового сервера.
