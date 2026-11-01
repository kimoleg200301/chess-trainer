#!/usr/bin/env python3
"""Run the authenticated Stockfish diagnostic without storing account secrets."""
from __future__ import annotations

import argparse
import getpass
import http.cookiejar
import ipaddress
import json
from pathlib import Path
import re
import sys
import time
import warnings
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPCookieProcessor, Request, build_opener


ENDPOINT = "/diagnostics/stockfish-benchmark"
OVERALL_TIMEOUT = 240.0
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class ClientError(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def server_url(value: str) -> str:
    """Permit HTTPS, with plain HTTP only for a local development server."""
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Некорректный адрес сервера.") from exc
    local = parsed.hostname == "localhost"
    if parsed.hostname and not local:
        try:
            local = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            pass
    if (not parsed.hostname or parsed.username is not None or parsed.password is not None
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
            or parsed.scheme not in {"https", "http"}
            or (parsed.scheme == "http" and not local)
            or (port is not None and not 1 <= port <= 65535)):
        raise argparse.ArgumentTypeError("Укажите HTTPS-адрес без пути и реквизитов; HTTP разрешён только для localhost.")
    host = parsed.hostname.lower()
    try:
        host = f"[{host}]" if ":" in host else host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise argparse.ArgumentTypeError("Некорректное имя сервера.") from exc
    suffix = f":{port}" if port and port != {"https": 443, "http": 80}[parsed.scheme] else ""
    return f"{parsed.scheme}://{host}{suffix}"


def benchmark_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value):
        raise argparse.ArgumentTypeError("Некорректный идентификатор benchmark.")
    return value


class RemoteBenchmark:
    def __init__(self, base_url: str):
        self.base_url = base_url
        self.cookies = http.cookiejar.CookieJar()
        self.opener = build_opener(NoRedirect(), HTTPCookieProcessor(self.cookies))
        self.deadline = time.monotonic() + OVERALL_TIMEOUT
        self.logged_in = False

    def call(self, path: str, body: dict | None = None, *, cleanup: bool = False):
        remaining = self.deadline - time.monotonic()
        if not cleanup and remaining <= 0:
            raise ClientError("Истёк срок ожидания результата. Серверный тест может ещё завершаться; его можно запросить с параметром --job-id.")
        request = Request(self.base_url + "/api" + path,
                          data=json.dumps(body).encode() if body is not None else None,
                          headers={"Origin": self.base_url, "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with self.opener.open(request, timeout=3 if cleanup else min(45, remaining)) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            if exc.code == 404 and path.startswith(ENDPOINT):
                message = ("Диагностика или её результат недоступны. На Render выполните Manual Deploy → Deploy latest commit. "
                           "Если сервер уже обновлён, запустите клиент снова: результат мог исчезнуть после перезапуска.")
            elif exc.code == 401:
                message = "Не удалось войти или сессия истекла. Проверьте имя пользователя и пароль тренажёра."
            elif exc.code == 503:
                message = "Движок занят или временно недоступен. Повторите benchmark после завершения текущего анализа."
            elif exc.code in {409, 429}:
                message = "Другой тест ещё выполняется или действует пауза между запусками. Повторите позже."
            elif 300 <= exc.code < 400:
                message = "Сервер перенаправляет запрос. Укажите его окончательный HTTPS-адрес."
            else:
                message = f"Сервер вернул HTTP {exc.code}."
            raise ClientError(message) from None
        except (URLError, TimeoutError, OSError):
            raise ClientError("Не удалось получить ответ сервера. Проверьте адрес и доступность приложения.") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ClientError("Сервер вернул слишком большой ответ.")
        try:
            result = json.loads(raw) if raw else None
        except (ValueError, UnicodeError):
            raise ClientError("Сервер вернул ответ, который не является JSON.") from None
        if result is not None and not isinstance(result, dict):
            raise ClientError("Сервер вернул неожиданный формат ответа.")
        return result

    def run(self, username: str, password: str, job_id: str | None = None) -> tuple[dict, int]:
        self.call("/auth/login", {"username": username, "password": password})
        password = ""
        self.logged_in = True
        # One start request only. Polling and explicit resume never start more CPU work.
        job = self.call(f"{ENDPOINT}/{job_id}") if job_id else self.call(ENDPOINT, {})
        if (not isinstance(job, dict) or not isinstance(job.get("id"), str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", job["id"])):
            raise ClientError("Сервер не вернул идентификатор benchmark.")
        if job_id is not None and job["id"] != job_id:
            raise ClientError("Сервер вернул другой идентификатор benchmark.")
        job_id = job["id"]
        print(f"Stockfish benchmark: {job_id}. Ожидаем завершения трёх запусков…", file=sys.stderr, flush=True)
        last_notice = time.monotonic()
        while job.get("status") == "running":
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise ClientError(f"Истёк срок ожидания benchmark. Получите его результат с параметром --job-id {job_id}.")
            time.sleep(min(2, remaining))
            job = self.call(f"{ENDPOINT}/{job_id}")
            if not isinstance(job, dict) or job.get("id") != job_id:
                raise ClientError("Сервер вернул другой идентификатор benchmark.")
            if time.monotonic() - last_notice >= 15 and job.get("status") == "running":
                print("Тест выполняется; новые запуски не создаются…", file=sys.stderr, flush=True)
                last_notice = time.monotonic()
        if job.get("status") == "failed":
            return {"error": job.get("error") or "Сервер не завершил benchmark.",
                    "partial_report": job.get("result"), "remote_job_id": job_id}, 1
        if job.get("status") != "completed" or not isinstance(job.get("result"), dict):
            raise ClientError("Сервер вернул неизвестное состояние benchmark.")
        return {**job["result"], "measurement_transport": {
            "kind": "http", "server": self.base_url, "job_id": job_id,
        }}, 0

    def close(self):
        try:
            if self.logged_in:
                self.call("/auth/logout", {}, cleanup=True)
        except ClientError:
            print("Не удалось подтвердить выход из сессии. Локальные cookies удалены.", file=sys.stderr)
        finally:
            self.cookies.clear()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the fixed Stockfish benchmark on an authenticated Tactics Lab server.")
    parser.add_argument("--url", required=True, type=server_url, help="HTTPS address, e.g. https://tactics-trial.onrender.com")
    parser.add_argument("--username", default="trainer")
    parser.add_argument("--job-id", type=benchmark_id, help="Read a previously started job without starting another benchmark.")
    parser.add_argument("--output", type=Path, help="Also save the JSON result to this file; credentials and cookies are never saved.")
    args = parser.parse_args(argv)
    client = None
    password = None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            password = getpass.getpass("Пароль аккаунта тренажёра: ")
        client = RemoteBenchmark(args.url)
        report, status = client.run(args.username, password, args.job_id)
        password = None
        rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.write_text(rendered, encoding="utf-8")
            args.output.chmod(0o600)
        print(rendered, end="")
        return status
    except ClientError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except getpass.GetPassWarning:
        print("Для скрытого ввода пароля запустите клиент в интерактивном терминале.", file=sys.stderr)
        return 1
    except OSError:
        print("Не удалось прочитать ввод или сохранить файл результата.", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("Клиент остановлен. Уже запущенный серверный тест ограничен по времени и может завершиться самостоятельно.", file=sys.stderr)
        return 130
    finally:
        password = None
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
