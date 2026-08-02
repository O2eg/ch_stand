import json
import os
from pathlib import Path

import pytest

from ch_stand.credentials import credential_paths, ensure_credentials, read_credentials
from ch_stand.errors import DockerRuntimeError


def test_credentials_are_generated_once_with_private_modes(tmp_path: Path) -> None:
    first = ensure_credentials(tmp_path, user="default")
    second = ensure_credentials(tmp_path, user="default")
    assert first.password == second.password
    assert len(first.password) >= 32
    assert first.ssh_private.stat().st_mode & 0o777 == 0o600
    assert first.ssh_public.stat().st_mode & 0o777 == 0o644
    paths = credential_paths(tmp_path)
    assert paths.database.stat().st_mode & 0o777 == 0o600
    document = json.loads(paths.database.read_text(encoding="utf-8"))
    assert document["user"] == "default"


def test_credentials_reject_a_different_clickhouse_user(tmp_path: Path) -> None:
    ensure_credentials(tmp_path, user="default")
    with pytest.raises(DockerRuntimeError, match="expected analyst"):
        read_credentials(tmp_path, expected_user="analyst")


def test_partial_credentials_are_rejected(tmp_path: Path) -> None:
    paths = credential_paths(tmp_path)
    paths.root.mkdir(parents=True)
    paths.database.write_text("{}", encoding="utf-8")
    os.chmod(paths.database, 0o600)
    with pytest.raises(DockerRuntimeError, match="missing or invalid"):
        ensure_credentials(tmp_path, user="default")


def test_credentials_root_must_not_be_a_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / ".ch_stand"
    root.mkdir()
    (root / "credentials").symlink_to(outside, target_is_directory=True)
    with pytest.raises(DockerRuntimeError, match="must not be a symlink"):
        ensure_credentials(tmp_path, user="default")
