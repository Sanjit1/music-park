from __future__ import annotations

from pathlib import Path

from music_mcn.refresh import run_validation_best_effort, run_validation_subprocess


def test_run_validation_subprocess_invokes_validator_script(monkeypatch):
    calls = []

    class Result:
        returncode = 0

    def fake_run(args, check=False):
        calls.append((args, check))
        return Result()

    monkeypatch.setattr("music_mcn.refresh.subprocess.run", fake_run)

    exit_code = run_validation_subprocess(Path("/tmp/graph"))

    assert exit_code == 0
    assert calls
    args, check = calls[0]
    assert check is False
    assert Path(args[0]).name.startswith("python")
    assert Path(args[1]).as_posix().endswith("scripts/validate_graph.py")
    assert args[2:] == ["--graph", "/tmp/graph"]


def test_run_validation_best_effort_skips_killed_validator(monkeypatch):
    monkeypatch.setattr("music_mcn.refresh.run_validation_subprocess", lambda _graph_dir: 137)

    exit_code = run_validation_best_effort(Path("/tmp/graph"))

    assert exit_code == 0


def test_run_validation_best_effort_keeps_real_failures(monkeypatch):
    monkeypatch.setattr("music_mcn.refresh.run_validation_subprocess", lambda _graph_dir: 1)

    exit_code = run_validation_best_effort(Path("/tmp/graph"))

    assert exit_code == 1