"""Atomic local ClickHouse password and SSH identity management."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ch_stand.errors import DockerRuntimeError

CREDENTIALS_VERSION = 1
CREDENTIALS_DIRECTORY = ".ch_stand/credentials"


@dataclass(frozen=True)
class CredentialPaths:
    root: Path
    database: Path
    ssh: Path
    ssh_private: Path
    ssh_public: Path


@dataclass(frozen=True)
class Credentials:
    user: str
    password: str
    ssh_private: Path
    ssh_public: Path


def credential_paths(project_directory: Path) -> CredentialPaths:
    project = project_directory.resolve()
    requested = project / CREDENTIALS_DIRECTORY
    if requested.is_symlink():
        raise DockerRuntimeError(f"credential directory must not be a symlink: {requested}")
    root = requested.resolve()
    if project not in root.parents:
        raise DockerRuntimeError(f"credential directory escaped the project: {requested}")
    ssh = root / "ssh"
    return CredentialPaths(
        root=root,
        database=root / "clickhouse.json",
        ssh=ssh,
        ssh_private=ssh / "ch_stand_test",
        ssh_public=ssh / "ch_stand_test.pub",
    )


def _run(command: list[str]) -> None:
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise DockerRuntimeError(f"required executable is missing: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        raise DockerRuntimeError(f"cannot generate SSH credentials: {detail}") from exc


def _validate_file(path: Path, *, private: bool) -> None:
    if path.is_symlink() or not path.is_file():
        raise DockerRuntimeError(f"credential file is missing or invalid: {path}")
    mode = path.stat().st_mode & 0o777
    expected = 0o600 if private else 0o644
    if mode != expected:
        raise DockerRuntimeError(
            f"credential file {path} has mode {oct(mode)} instead of {oct(expected)}"
        )


def read_credentials(project_directory: Path, *, expected_user: str | None = None) -> Credentials:
    paths = credential_paths(project_directory)
    _validate_file(paths.database, private=True)
    _validate_file(paths.ssh_private, private=True)
    _validate_file(paths.ssh_public, private=False)
    try:
        document = json.loads(paths.database.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DockerRuntimeError(f"cannot read ClickHouse credentials: {exc}") from exc
    if not isinstance(document, dict) or set(document) != {"version", "user", "password"}:
        raise DockerRuntimeError("ClickHouse credential file has an invalid structure")
    if document["version"] != CREDENTIALS_VERSION:
        raise DockerRuntimeError("ClickHouse credential file has an unsupported version")
    user = document["user"]
    password = document["password"]
    if not isinstance(user, str) or not user or not isinstance(password, str) or not password:
        raise DockerRuntimeError("ClickHouse credential file contains invalid values")
    if expected_user is not None and user != expected_user:
        raise DockerRuntimeError(
            f"generated credentials belong to ClickHouse user {user}, expected {expected_user}"
        )
    return Credentials(
        user=user,
        password=password,
        ssh_private=paths.ssh_private,
        ssh_public=paths.ssh_public,
    )


def ensure_credentials(project_directory: Path, *, user: str) -> Credentials:
    paths = credential_paths(project_directory)
    if paths.root.exists():
        return read_credentials(project_directory, expected_user=user)
    parent = paths.root.parent
    if parent.is_symlink():
        raise DockerRuntimeError(f"credential parent must not be a symlink: {parent}")
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(parent, 0o700)
    temporary = Path(tempfile.mkdtemp(dir=parent, prefix=".credentials."))
    try:
        ssh_directory = temporary / "ssh"
        ssh_directory.mkdir(mode=0o700)
        private = ssh_directory / "ch_stand_test"
        _run(
            [
                "ssh-keygen",
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-C",
                "ch-stand local diagnostic key",
                "-f",
                str(private),
            ]
        )
        os.chmod(private, 0o600)
        os.chmod(private.with_suffix(".pub"), 0o644)
        database = temporary / "clickhouse.json"
        database.write_text(
            json.dumps(
                {
                    "version": CREDENTIALS_VERSION,
                    "user": user,
                    "password": secrets.token_urlsafe(32),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        os.chmod(database, 0o600)
        temporary.replace(paths.root)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return read_credentials(project_directory, expected_user=user)


def credentials_status(project_directory: Path) -> dict[str, Any]:
    paths = credential_paths(project_directory)
    if not paths.root.exists():
        return {
            "path": str(paths.root),
            "state": "absent",
            "ssh_keygen": shutil.which("ssh-keygen"),
        }
    try:
        credentials = read_credentials(project_directory)
    except DockerRuntimeError as exc:
        return {"path": str(paths.root), "state": "invalid", "error": str(exc)}
    return {
        "path": str(paths.root),
        "state": "ready",
        "user": credentials.user,
        "database_file": str(paths.database),
        "ssh_private": str(paths.ssh_private),
        "ssh_public": str(paths.ssh_public),
    }


def clean_credentials(project_directory: Path) -> bool:
    paths = credential_paths(project_directory)
    if not paths.root.exists():
        return False
    shutil.rmtree(paths.root)
    return True
