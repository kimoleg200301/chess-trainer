#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
umask 077
mkdir -p backups
backup_file="backups/tactics-$(date -u +%Y%m%dT%H%M%SZ).dump"
docker compose exec -T db pg_dump -U trainer -d tactics --format=custom > "$backup_file"
printf 'Backup saved: %s\n' "$backup_file"
