#!/usr/bin/env python3
"""Create local configuration; never print generated credentials."""
import argparse
import os
import re
import secrets
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--username', default='trainer')
parser.add_argument('--domain', default='')
parser.add_argument('--port', type=int, default=8080)
args = parser.parse_args()
if not re.fullmatch(r'[A-Za-z0-9_.-]{3,64}', args.username):
    parser.error('Username: 3–64 letters, digits, underscore, dot or hyphen.')
if args.domain and not re.fullmatch(r'[A-Za-z0-9.-]+', args.domain):
    parser.error('Domain must be a plain hostname, without scheme or path.')
if not 1024 <= args.port <= 65535:
    parser.error('Port must be between 1024 and 65535.')
root = Path(__file__).resolve().parents[1]
path = root / '.env'
origin = f'https://{args.domain}' if args.domain else f'http://localhost:{args.port},http://127.0.0.1:{args.port}'
content = (
    f'POSTGRES_PASSWORD={secrets.token_urlsafe(32)}\n'
    f'AUTH_USERNAME={args.username}\n'
    f'AUTH_PASSWORD={secrets.token_urlsafe(24)}\n'
    f'APP_PORT={args.port}\n'
    f'COOKIE_SECURE={str(bool(args.domain)).lower()}\n'
    f'ALLOWED_ORIGINS={origin}\n'
    f'APP_DOMAIN={args.domain}\n'
)
try:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
except FileExistsError:
    parser.exit(1, '.env already exists; left unchanged.\n')
with os.fdopen(fd, 'w') as file:
    file.write(content)
print(f'Configuration saved to {path}. Read AUTH_PASSWORD there to sign in. Keep this file private.')
