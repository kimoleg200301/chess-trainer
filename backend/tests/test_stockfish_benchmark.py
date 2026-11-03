"""The portable benchmark needs neither PostgreSQL nor an application session."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from app import stockfish_benchmark as benchmark


@pytest.fixture(scope="session", autouse=True)
def database():
    yield


@pytest.fixture(autouse=True)
def clean_training():
    yield


def output(*, nodes=100_000, milliseconds=1000, positions=8, app=True, engine="Stockfish 15.1"):
    stdout = engine + " by the Stockfish developers (see AUTHORS file)\n"
    if app:
        stdout += "info string NNUE evaluation using nn-test.nnue enabled\n"
    else:
        stdout += "info string classical evaluation enabled\ninfo string NNUE evaluation using nn-test.nnue enabled\n"
    stdout += "info depth 13 multipv 2 score cp 42 nodes 998 nps 998000 time 1 pv e2e4\n"
    stderr = "".join(f"Position: {index}/{positions} (test FEN)\n" for index in range(1, positions + 1))
    stderr += f"Total time (ms) : {milliseconds}\nNodes searched  : {nodes}\nNodes/second    : {nodes * 1000 // milliseconds}\n"
    return SimpleNamespace(returncode=0, stdout=stdout, stderr=stderr)


@pytest.fixture
def executable(tmp_path, monkeypatch):
    path = tmp_path / "stockfish"
    path.write_bytes(b"dummy executable for mocked runner")
    path.chmod(0o700)
    monkeypatch.setattr(benchmark, "hardware_metadata", lambda: {"cpu_quota_cores": 0.1})
    return path


def test_parser_uses_bench_totals_not_intermediate_uci_nps():
    result = output()
    parsed = benchmark.parse_bench_output(result.stdout, result.stderr)
    assert parsed == {"engine_name": "Stockfish 15.1", "engine_time_ms": 1000, "nodes": 100_000,
                      "nps": 100_000, "position_count": 8, "evaluation_modes": ["NNUE"],
                      "nnue_networks": ["nn-test.nnue"]}


@pytest.mark.parametrize("invalid", ["missing-summary", "zero-time", "incomplete-positions", "duplicate-totals"])
def test_parser_refuses_partial_or_ambiguous_results(invalid):
    result = output()
    if invalid == "missing-summary":
        result.stderr = "Position: 1/8 (test FEN)\n"
    elif invalid == "zero-time":
        result.stderr = result.stderr.replace("(ms) : 1000", "(ms) : 0")
    elif invalid == "incomplete-positions":
        result.stderr = result.stderr.replace("Position: 8/8 (test FEN)\n", "")
    else:
        result.stderr += "Nodes searched : 100000\n"
    with pytest.raises(benchmark.BenchmarkError, match="Stockfish") as error:
        benchmark.parse_bench_output(result.stdout, result.stderr)
    assert error.value.code == "invalid_output"


def test_app_profile_records_reproducibility_and_weighted_rates(executable, monkeypatch):
    calls = []
    responses = iter([output(milliseconds=1000), output(milliseconds=2000), output(milliseconds=4000)])
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return next(responses)
    monkeypatch.setattr(benchmark.subprocess, "run", run)
    result = benchmark.run_benchmark(str(executable))
    assert len(calls) == 3
    assert all(command == [str(executable), "bench", "32", "1", "13", str(benchmark.POSITIONS_FILE), "depth"]
               for command, _ in calls)
    assert all(options["timeout"] == 60 and options["stdin"] == subprocess.DEVNULL
               and options["check"] is False for _, options in calls)
    assert result["profile"]["multipv"] == 2 and result["profile"]["nnue"] is True
    assert result["profile"]["positions_sha256"] == hashlib.sha256(benchmark.POSITIONS_FILE.read_bytes()).hexdigest()
    assert result["binary_sha256"] == hashlib.sha256(executable.read_bytes()).hexdigest()
    assert result["hardware"] == {"cpu_quota_cores": 0.1}
    assert [run["run"] for run in result["runs"]] == [1, 2, 3]
    summary = result["summary"]
    assert summary["checksum_consistent"] and summary["bench_signature_nodes"] == 100_000
    assert summary["median_nps"] == 50_000 and summary["min_nps"] == 25_000 and summary["max_nps"] == 100_000
    assert summary["weighted_nps"] == round(300_000 * 1000 / 7000)
    json.dumps(result)


def test_standard_profile_keeps_official_default_command(executable, monkeypatch):
    calls = []
    def run(command, **_kwargs):
        calls.append(command)
        return output(positions=48, app=False)
    monkeypatch.setattr(benchmark.subprocess, "run", run)
    result = benchmark.run_benchmark(str(executable), repetitions=1, profile="standard")
    assert calls == [[str(executable), "bench", "32", "1", "13", "default", "depth"]]
    assert result["profile"]["multipv"] == 1
    assert result["runs"][0]["evaluation_modes"] == ["classical", "NNUE"]


@pytest.mark.parametrize("problem", ["positions", "classical", "multipv"])
def test_app_profile_requires_its_positions_and_engine_options(executable, monkeypatch, problem):
    response = output(positions=7 if problem == "positions" else 8, app=problem != "classical")
    if problem == "multipv":
        response.stdout = response.stdout.replace("multipv 2", "multipv 1")
    monkeypatch.setattr(benchmark.subprocess, "run", lambda *_args, **_kwargs: response)
    with pytest.raises(benchmark.BenchmarkError) as error:
        benchmark.run_benchmark(str(executable), repetitions=1)
    assert error.value.code == "profile_mismatch"
    assert error.value.partial_report["failed_run"] == 1


def test_timeout_preserves_prior_runs_and_never_publishes_partial_run_as_result(executable, monkeypatch):
    calls = []
    def run(command, **_kwargs):
        calls.append(command)
        if len(calls) == 2:
            raise subprocess.TimeoutExpired(command, 60, output="sensitive process text must not be exposed")
        return output()
    monkeypatch.setattr(benchmark.subprocess, "run", run)
    with pytest.raises(benchmark.BenchmarkError) as error:
        benchmark.run_benchmark(str(executable))
    assert error.value.code == "timeout"
    assert len(error.value.partial_report["runs"]) == 1
    assert error.value.partial_report["failed_run"] == 2
    assert "sensitive" not in str(error.value)
    assert len(calls) == 2


def test_nonzero_exit_is_not_misreported_as_success(executable, monkeypatch):
    response = output()
    response.returncode = 7
    monkeypatch.setattr(benchmark.subprocess, "run", lambda *_args, **_kwargs: response)
    with pytest.raises(benchmark.BenchmarkError) as error:
        benchmark.run_benchmark(str(executable))
    assert error.value.code == "engine_exit"
    assert error.value.partial_report["runs"] == []


def test_checksum_change_fails_comparability_even_if_each_run_completed(executable, monkeypatch):
    responses = iter([output(), output(nodes=120_000), output()])
    monkeypatch.setattr(benchmark.subprocess, "run", lambda *_args, **_kwargs: next(responses))
    with pytest.raises(benchmark.BenchmarkError) as error:
        benchmark.run_benchmark(str(executable))
    assert error.value.code == "inconsistent_checksum"
    summary = error.value.partial_report["summary"]
    assert summary["completed_runs"] == 3 and summary["checksum_consistent"] is False
    assert summary["bench_signature_nodes"] is None


@pytest.mark.parametrize("kwargs", [{"repetitions": True}, {"repetitions": 0}, {"repetitions": 4},
    {"timeout_per_run": float("nan")}, {"timeout_per_run": 61}, {"timeout_per_run": 0}, {"profile": "custom"}])
def test_invalid_runner_parameters_do_not_start_process(monkeypatch, kwargs):
    monkeypatch.setattr(benchmark.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("must not execute"))
    with pytest.raises(ValueError):
        benchmark.run_benchmark("missing-stockfish", **kwargs)


def test_cgroup_v2_limits_are_separate_from_host_cpu_count(tmp_path):
    (tmp_path / "cpu.max").write_text("10000 100000")
    (tmp_path / "memory.max").write_text(str(512 * 1024 * 1024))
    data = benchmark.hardware_metadata(tmp_path)
    assert data["cpu_quota_cores"] == 0.1
    assert data["memory_limit_bytes"] == 512 * 1024 * 1024
    assert "visible_logical_cpus" in data and "process_affinity_cpus" in data


def test_cgroup_unlimited_and_legacy_values(tmp_path):
    (tmp_path / "cpu.max").write_text("max 100000")
    (tmp_path / "memory.max").write_text("max")
    assert benchmark.hardware_metadata(tmp_path)["cpu_quota_cores"] is None
    assert benchmark.hardware_metadata(tmp_path)["memory_limit_bytes"] is None
    (tmp_path / "cpu.max").unlink()
    (tmp_path / "memory.max").unlink()
    (tmp_path / "cpu").mkdir()
    (tmp_path / "cpu" / "cpu.cfs_quota_us").write_text("50000")
    (tmp_path / "cpu" / "cpu.cfs_period_us").write_text("100000")
    (tmp_path / "memory.limit_in_bytes").write_text(str(2**63))
    data = benchmark.hardware_metadata(tmp_path)
    assert data["cpu_quota_cores"] == 0.5 and data["memory_limit_bytes"] is None


def test_cli_saves_json_without_database_environment(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(benchmark, "run_benchmark", lambda engine_path, **kwargs: {"engine": engine_path, **kwargs})
    target = tmp_path / "result.json"
    assert benchmark.main(["--engine", "stockfish", "--profile", "standard", "--repetitions", "1", "--output", str(target)]) == 0
    saved = json.loads(target.read_text())
    assert saved == {"engine": "stockfish", "profile": "standard", "repetitions": 1}
    assert json.loads(capsys.readouterr().out) == saved


def test_cli_failure_is_nonzero_and_machine_readable(monkeypatch, capsys):
    def fail(*_args, **_kwargs):
        raise benchmark.BenchmarkError("Заданный движок недоступен.", code="engine_unavailable")
    monkeypatch.setattr(benchmark, "run_benchmark", fail)
    assert benchmark.main(["--engine", "missing-engine"]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "engine_unavailable"


def test_real_stockfish_honors_nnue_multipv_and_bundled_positions():
    engine = os.environ.get("TEST_STOCKFISH_PATH") or shutil.which("stockfish")
    if not engine and Path("/usr/games/stockfish").exists():
        engine = "/usr/games/stockfish"
    if not engine:
        pytest.skip("Stockfish is not available for the real benchmark smoke test")
    result = benchmark.run_benchmark(engine, repetitions=1)
    measured = result["runs"][0]
    assert measured["position_count"] == 8 and measured["evaluation_modes"] == ["NNUE"]
    assert measured["nnue_networks"] and measured["nodes"] > 0 and measured["nps"] > 0
    assert result["profile"]["multipv"] == 2
