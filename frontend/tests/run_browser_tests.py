"""Run browser regressions against an isolated Vite server or TEST_FRONTEND_URL."""
import argparse
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import ProxyHandler, build_opener

from browser_support import artifact_path

TESTS = Path(__file__).resolve().parent
FRONTEND = TESTS.parent


def wait_for_server(url, server=None):
    # The automatically started server is loopback-only; do not send it through
    # a machine's outbound HTTP proxy.
    local = urlparse(url).hostname in {"127.0.0.1", "localhost", "::1"}
    opener = build_opener(ProxyHandler({})) if local else build_opener()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if server is not None and server.poll() is not None:
            raise RuntimeError("Vite exited before becoming ready; inspect vite.log")
        try:
            with opener.open(url, timeout=1) as response:
                if response.status == 200:
                    return
        except (OSError, URLError):
            pass
        time.sleep(0.1)
    raise RuntimeError(f"Frontend did not become ready at {url}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenarios", nargs="*", help="Optional names, such as routing-browser.py")
    parser.add_argument("--timeout", type=int, default=240, help="Maximum seconds per scenario")
    args = parser.parse_args()
    scenarios = [TESTS / name for name in args.scenarios] if args.scenarios else sorted(TESTS.glob("*-browser.py"))
    if not scenarios or any(path.parent != TESTS or not path.is_file() for path in scenarios):
        parser.error("Choose existing scenario filenames from frontend/tests")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")

    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env["TEST_ARTIFACT_DIR"] = str(Path(artifact_path("vite.log")).parent)
    env.setdefault("COACH_SCREENSHOT_DIR", env["TEST_ARTIFACT_DIR"])
    url = env.get("TEST_FRONTEND_URL")
    server = None
    failed = []
    with Path(artifact_path("vite.log")).open("w") as server_log:
        try:
            if not url:
                with socket.socket() as listener:
                    listener.bind(("127.0.0.1", 0))
                    port = listener.getsockname()[1]
                url = f"http://127.0.0.1:{port}"
                server = subprocess.Popen([
                    "node", str(FRONTEND / "node_modules" / "vite" / "bin" / "vite.js"),
                    "--host", "127.0.0.1", "--port", str(port), "--strictPort",
                ], cwd=FRONTEND, env=env, stdout=server_log, stderr=subprocess.STDOUT)
            wait_for_server(url, server)
            env["TEST_FRONTEND_URL"] = url
            print(f"Browser regressions: {len(scenarios)} scenarios at {url}", flush=True)
            for scenario in scenarios:
                print(f"\nRUN {scenario.name}", flush=True)
                try:
                    result = subprocess.run([sys.executable, str(scenario)], cwd=FRONTEND,
                                            env=env, timeout=args.timeout)
                    if result.returncode:
                        failed.append(scenario.name)
                except subprocess.TimeoutExpired:
                    print(f"TIMEOUT {scenario.name} after {args.timeout}s", file=sys.stderr)
                    failed.append(scenario.name)
        finally:
            if server is not None:
                server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()
    if failed:
        print(f"\nFAILED ({len(failed)}/{len(scenarios)}): {', '.join(failed)}", file=sys.stderr)
        return 1
    print(f"\nPASS: all {len(scenarios)} browser scenarios")
    return 0


if __name__ == "__main__":
    sys.exit(main())
