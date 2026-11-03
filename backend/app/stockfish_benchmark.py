"""Portable, bounded runner for Stockfish's built-in single-thread benchmark.

This module deliberately has no database or web-framework dependencies. The
same command and parser are used by the authenticated diagnostic and the CLI.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import statistics
import subprocess
import sys
import time


STANDARD_PROFILE = {
    "id": "stockfish-bench-32mb-1thread-depth13-v1",
    "name": "Stockfish built-in bench",
    "hash_mib": 32,
    "threads": 1,
    "limit": 13,
    "limit_type": "depth",
    "positions": "default",
    "multipv": 1,
    "command_arguments": ["bench", "32", "1", "13", "default", "depth"],
}
POSITIONS_FILE = Path(__file__).with_name("benchmark_positions.txt")
PROFILE = {
    "id": "tactics-stockfish-nnue-multipv2-depth13-v2",
    "name": "Fixed diagnostic NNUE / MultiPV 2 workload via Stockfish bench",
    "hash_mib": 32,
    "threads": 1,
    "limit": 13,
    "limit_type": "depth",
    "positions": "benchmark_positions.txt",
    "position_count": 8,
    "positions_sha256": hashlib.sha256(POSITIONS_FILE.read_bytes()).hexdigest(),
    "multipv": 2,
    "nnue": True,
    "evaluation_type": "NNUE",
    "command_arguments": ["bench", "32", "1", "13", "benchmark_positions.txt", "depth"],
}
PROFILES = {"app": PROFILE, "standard": STANDARD_PROFILE}
DEFAULT_REPETITIONS = 3
MAX_REPETITIONS = 3
DEFAULT_TIMEOUT = 60.0


class BenchmarkError(RuntimeError):
    """Safe error text, with completed measurements retained when available."""

    def __init__(self, message: str, *, code: str = "benchmark_failed", partial_report: dict | None = None):
        super().__init__(message)
        self.code = code
        self.partial_report = partial_report


def parse_bench_output(stdout: str, stderr: str) -> dict:
    """Read official bench totals, not the many intermediate UCI nps values."""
    output = stdout + "\n" + stderr
    name = re.search(r"^(Stockfish[^\r\n]*?)(?: by [^\r\n]*)?$", output, re.MULTILINE)
    patterns = {
        "engine_time_ms": r"^Total time \(ms\)\s*:\s*(\d+)\s*$",
        "nodes": r"^Nodes searched\s*:\s*(\d+)\s*$",
        "nps": r"^Nodes/second\s*:\s*(\d+)\s*$",
    }
    values = {}
    for field, pattern in patterns.items():
        matches = re.findall(pattern, output, re.MULTILINE)
        if len(matches) != 1 or int(matches[0]) <= 0:
            raise BenchmarkError("Stockfish не вернул полную итоговую сводку bench.", code="invalid_output")
        values[field] = int(matches[0])
    if name is None:
        raise BenchmarkError("Не удалось определить версию Stockfish.", code="invalid_output")
    positions = re.findall(r"^Position:\s*(\d+)/(\d+)\b", output, re.MULTILINE)
    if positions and (int(positions[-1][0]) != int(positions[-1][1])
                      or len(positions) != int(positions[-1][1])):
        raise BenchmarkError("Stockfish не завершил все позиции bench.", code="invalid_output")
    modes = []
    if "classical evaluation enabled" in output:
        modes.append("classical")
    if "NNUE evaluation using" in output or "NNUE evaluation enabled" in output:
        modes.append("NNUE")
    return {"engine_name": name.group(1).strip(), **values,
            "position_count": int(positions[-1][1]) if positions else None,
            "evaluation_modes": modes,
            "nnue_networks": sorted(set(re.findall(r"NNUE evaluation using\s+(\S+)", output)))}


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None


def hardware_metadata(cgroup_root: Path = Path("/sys/fs/cgroup")) -> dict:
    """Report scheduling limits separately from the host's visible CPU count."""
    quota_cores = None
    quota = _read_text(cgroup_root / "cpu.max")
    if quota:
        fields = quota.split()
        if len(fields) == 2 and fields[0] != "max":
            try:
                amount, period = map(int, fields)
                quota_cores = amount / period if amount > 0 and period > 0 else None
            except ValueError:
                pass
    else:
        for root in (cgroup_root / "cpu", cgroup_root / "cpu,cpuacct", cgroup_root):
            amount, period = _read_text(root / "cpu.cfs_quota_us"), _read_text(root / "cpu.cfs_period_us")
            try:
                if amount is not None and period is not None and int(amount) > 0 and int(period) > 0:
                    quota_cores = int(amount) / int(period)
                    break
            except ValueError:
                pass
    memory_limit = None
    for path in (cgroup_root / "memory.max", cgroup_root / "memory" / "memory.limit_in_bytes",
                 cgroup_root / "memory.limit_in_bytes"):
        raw = _read_text(path)
        if raw == "max":
            break
        try:
            if raw is not None:
                value = int(raw)
                memory_limit = value if 0 < value < 2**60 else None
                break
        except ValueError:
            pass
    try:
        affinity_count = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        affinity_count = None
    cpu_model = None
    cpuinfo = _read_text(Path("/proc/cpuinfo"))
    if cpuinfo:
        match = re.search(r"^(?:model name|Hardware)\s*:\s*(.+)$", cpuinfo, re.MULTILINE)
        if match:
            cpu_model = match.group(1).strip()[:200]
    return {"system": platform.system(), "architecture": platform.machine(),
            "cpu_model": cpu_model, "visible_logical_cpus": os.cpu_count(),
            "process_affinity_cpus": affinity_count, "cpu_quota_cores": quota_cores,
            "memory_limit_bytes": memory_limit}


def _executable(engine_path: str) -> Path:
    found = shutil.which(engine_path)
    if found is None:
        raise BenchmarkError("Исполняемый файл Stockfish не найден или недоступен.", code="engine_unavailable")
    return Path(found).resolve()


def _binary_hash(path: Path) -> str | None:
    try:
        with path.open("rb") as handle:
            digest = hashlib.sha256()
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
            return digest.hexdigest()
    except OSError:
        return None


def _summary(runs: list[dict]) -> dict:
    nodes = {run["nodes"] for run in runs}
    names = {run["engine_name"] for run in runs}
    positions = {run["position_count"] for run in runs}
    networks = {tuple(run["nnue_networks"]) for run in runs}
    total_time = sum(run["engine_time_ms"] for run in runs)
    rates = [run["nps"] for run in runs]
    return {
        "completed_runs": len(runs),
        "checksum_consistent": len(nodes) == 1 and len(names) == 1 and len(positions) == 1 and len(networks) == 1,
        "bench_signature_nodes": next(iter(nodes)) if len(nodes) == 1 else None,
        "median_nps": statistics.median(rates) if rates else None,
        "min_nps": min(rates) if rates else None,
        "max_nps": max(rates) if rates else None,
        "weighted_nps": round(sum(run["nodes"] for run in runs) * 1000 / total_time) if total_time else None,
        "total_engine_time_ms": total_time,
        "total_wall_time_ms": round(sum(run["wall_time_ms"] for run in runs), 2),
    }


def run_benchmark(engine_path: str, repetitions: int = DEFAULT_REPETITIONS,
                  timeout_per_run: float = DEFAULT_TIMEOUT, *, profile: str = "app") -> dict:
    if profile not in PROFILES:
        raise ValueError("Неизвестный профиль benchmark.")
    if isinstance(repetitions, bool) or not isinstance(repetitions, int) or not 1 <= repetitions <= MAX_REPETITIONS:
        raise ValueError(f"Число запусков должно быть от 1 до {MAX_REPETITIONS}.")
    if (isinstance(timeout_per_run, bool) or not isinstance(timeout_per_run, (int, float))
            or not math.isfinite(timeout_per_run) or not 0 < timeout_per_run <= DEFAULT_TIMEOUT):
        raise ValueError(f"Предел времени одного запуска должен быть от 0 до {DEFAULT_TIMEOUT:g} секунд.")
    executable = _executable(engine_path)
    selected_profile = PROFILES[profile]
    arguments = list(selected_profile["command_arguments"])
    if profile == "app":
        arguments[4] = str(POSITIONS_FILE)
    report = {
        "profile": {**selected_profile, "command_arguments": list(selected_profile["command_arguments"])},
        "started_at": datetime.now(timezone.utc).isoformat(),
        "timeout_per_run_seconds": timeout_per_run, "requested_runs": repetitions,
        "binary_sha256": _binary_hash(executable), "hardware": hardware_metadata(), "runs": [],
    }
    for index in range(repetitions):
        started = time.perf_counter()
        try:
            result = subprocess.run([str(executable), *arguments],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout_per_run, check=False)
            wall_ms = round((time.perf_counter() - started) * 1000, 2)
            if result.returncode != 0:
                raise BenchmarkError(f"Stockfish завершил bench с кодом {result.returncode}.", code="engine_exit")
            measurement = parse_bench_output(result.stdout, result.stderr)
            if profile == "app" and (measurement["position_count"] != selected_profile["position_count"]
                                      or measurement["evaluation_modes"] != ["NNUE"]
                                      or not re.search(r"\bmultipv 2\b", result.stdout)):
                raise BenchmarkError("Stockfish не подтвердил заданный профиль NNUE / MultiPV 2.", code="profile_mismatch")
        except subprocess.TimeoutExpired:
            error = BenchmarkError("Stockfish не завершил один запуск bench за отведённое время.", code="timeout")
        except OSError:
            error = BenchmarkError("Не удалось запустить Stockfish для измерения.", code="engine_unavailable")
        except BenchmarkError as exc:
            error = exc
        else:
            report["runs"].append({"run": index + 1, **measurement, "wall_time_ms": wall_ms})
            continue
        report["summary"] = _summary(report["runs"])
        report["failed_run"] = index + 1
        error.partial_report = report
        raise error
    report["summary"] = _summary(report["runs"])
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    if not report["summary"]["checksum_consistent"]:
        raise BenchmarkError("Контрольное число узлов различается между запусками; результаты нельзя считать воспроизводимыми.",
                             code="inconsistent_checksum", partial_report=report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stockfish bench: 32 MiB hash, one thread, depth 13; app profile uses NNUE / MultiPV 2.")
    parser.add_argument("--engine", default=os.environ.get("STOCKFISH_PATH", "/usr/games/stockfish"))
    parser.add_argument("--repetitions", type=int, choices=range(1, MAX_REPETITIONS + 1), default=DEFAULT_REPETITIONS)
    parser.add_argument("--profile", choices=PROFILES, default="app", help="app: fixed NNUE / MultiPV 2 workload; standard: official default positions.")
    parser.add_argument("--output", type=Path, help="Also save the JSON report to this file.")
    args = parser.parse_args(argv)
    status = 0
    try:
        report = run_benchmark(args.engine, repetitions=args.repetitions, profile=args.profile)
    except BenchmarkError as exc:
        report = {"error": {"code": exc.code, "detail": str(exc)}, "partial_report": exc.partial_report}
        status = 1
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        try:
            args.output.write_text(rendered + "\n", encoding="utf-8")
        except OSError:
            print("Не удалось сохранить JSON-файл результата.", file=sys.stderr)
            status = 1
    print(rendered)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
