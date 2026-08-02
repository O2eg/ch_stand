from pathlib import Path

import pytest

from ch_stand.assets import PROFILE_FILES, docker_build_context, initialize_project
from ch_stand.errors import ChStandError


def test_docker_build_context_contains_diagnostic_assets() -> None:
    context = docker_build_context()
    dockerfile = (context / "docker/Dockerfile").read_text(encoding="utf-8")
    entrypoint = (context / "docker/ch-stand-entrypoint.sh").read_text(encoding="utf-8")
    for package in ("linux-tools-generic", "bpftrace", "gdb", "strace", "openssh-server"):
        assert package in dockerfile
    assert "perf version" in dockerfile
    assert "clickhouse keeper" in entrypoint
    assert "/usr/sbin/sshd" in entrypoint


def test_project_initialization_is_idempotent_and_protects_edits(tmp_path: Path) -> None:
    first = initialize_project(tmp_path)
    assert len(first["files"]) == len(PROFILE_FILES) + 1
    assert {item["state"] for item in first["files"]} == {"created"}
    second = initialize_project(tmp_path)
    assert {item["state"] for item in second["files"]} == {"unchanged"}
    profile = tmp_path / "configs/single.yaml"
    profile.write_text(profile.read_text(encoding="utf-8") + "# local edit\n", encoding="utf-8")
    with pytest.raises(ChStandError, match="init --force"):
        initialize_project(tmp_path)
    forced = initialize_project(tmp_path, force=True)
    assert any(item["state"] == "replaced" for item in forced["files"])


def test_project_initialization_refuses_symlink_targets(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "configs").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ChStandError, match="symbolic link"):
        initialize_project(tmp_path)
