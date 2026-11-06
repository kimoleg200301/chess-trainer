# Обновление основного сервера

## Окружение

- Репозиторий: `https://github.com/kimoleg200301/chess-trainer`, ветка `main`.
- SSH через WireGuard: `ssh -p 22 root@10.66.0.2`.
- Основной сайт: `http://10.66.0.2:8080`, доступ через WireGuard. Вход в аккаунт приложения сохраняется.
- Каталог: `/opt/chess-trainer`; ветка `server-wireguard` отслеживает `origin/main`. Compose-проект `chess-trainer`, рабочая PostgreSQL 17 в томе `chess-trainer_pgdata`.
- Ubuntu 24.04 в WSL2 с systemd; Docker daemon принадлежит Windows Docker Desktop. Windows WireGuard `client1` → `10.66.0.2:8080` → Windows loopback `127.0.0.1:18080` → frontend.
- Mac M3: `/Users/kim/IdeaProjects/chess-trainer`, используется для разработки; локальный экземпляр и его БД должны быть отдельными от сервера.
- Полный порядок обновления, бэкап и автозапуск описаны в [deploy/SERVER.md](deploy/SERVER.md).

## Правила

- Готовь изменения в рабочем репозитории, публикуй проверенный коммит в GitHub и обновляй основной сервер из `main`. Образы приложения собираются на сервере; GHCR в этом процессе не используется.
- Сохраняй серверные `.env`, `compose.wireguard.yml`, `DEPLOYMENT.txt`, настройки доступа и том PostgreSQL. Изучи локальные изменения до обновления; не затирай их. Серверные `compose.wireguard.yml` и `DEPLOYMENT.txt` ожидаемо могут быть untracked.
- Всегда используй `docker compose -p chess-trainer --env-file .env -f compose.yml -f compose.wireguard.yml`: override отключает bootstrap восстановленной базы. `AUTH_PASSWORD=disabled` — предохранитель, а не пароль входа; backend получает пустой `AUTH_PASSWORD`, аккаунты хранятся в PostgreSQL.
- Сохраняй frontend bind `127.0.0.1:18080:80`, `APP_PORT=18080` и `ALLOWED_ORIGINS=http://10.66.0.2:8080,http://localhost:18080,http://127.0.0.1:18080`. Windows portproxy должен слушать только WireGuard `10.66.0.2:8080`. Не публикуй сайт на `0.0.0.0`/LAN, PostgreSQL на host-порту или публичные origins.
- На Docker Desktop также работает TrueFace с отдельной PostgreSQL 16. Не меняй его контейнеры, тома и порт `5432`; не заменяй Docker daemon и не выполняй глобальную очистку Docker.
- Не запускай `app.bootstrap`, `scripts/configure.py`, `docker compose down -v` и удаление томов при обычном обновлении.
- Не добавляй в Git секреты, `.env` и дампы. Бэкапы и проверку сохранности данных выполняй на основной базе этого сервера.

## Порядок обновления

1. Проверь рабочее дерево, текущий SHA и изменения до выбранного коммита `origin/main`.
2. Перед изменением работающего приложения сохрани полный дамп PostgreSQL и серверную конфигурацию. Проверь размер и читаемость дампа через `pg_restore --list`, сохрани его копию вне сервера.
3. Обнови код через fast-forward, проверь Compose, собери образы и запусти контейнеры по [инструкции](deploy/SERVER.md). При ошибке остановись. После пересоздания backend перечитай конфигурацию nginx.
4. Проверь health внутри backend, на Windows loopback и с другого устройства через WireGuard, а также отсутствие доступа к приложению по публичному и LAN-адресам. Вход, тренировку и анализ проверяй отдельным тестовым аккаунтом, сохраняя личную статистику пользователя; удали тестовые данные после проверки.
5. Сообщи установленный SHA, путь бэкапа, проверки и ограничения. Для изменений только документации пересборка и перезапуск приложения не нужны.

- Откат выполняется на этом же сервере с учётом совместимости схемы БД и сохранением доступа через WireGuard. Не восстанавливай старый дамп поверх новых пользовательских данных автоматически.
- Прежний Tailscale-сервер сохранён для отката, но не получает новые данные автоматически. Не обновляй его при обычном обслуживании основного сервера; статус переноса и оставшиеся проверки — в [deploy/SERVER.md](deploy/SERVER.md).
- При обычном обновлении не перезапускай Windows/WSL, SSH, Docker Desktop или WireGuard. `restart: unless-stopped` действует после запуска Docker Desktop; systemd в Ubuntu не запускает Windows Docker Desktop. Windows firewall/portproxy требуют администратора Windows, root в WSL этих прав не даёт.
