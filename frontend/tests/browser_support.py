"""Portable browser and artifact settings shared by regression scenarios."""
import os
from pathlib import Path


def chromium_options() -> dict:
    # Omit executable_path to use the browser installed by Playwright in CI.
    executable = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
    return {"executable_path": executable} if executable else {}


def artifact_path(name: str) -> str:
    directory = Path(os.environ.get(
        "TEST_ARTIFACT_DIR", str(Path(__file__).resolve().parents[1] / "test-results" / "browser"))).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    return str(directory / name)
