"""Bundled profiles, schema, and diagnostic-image build assets."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from ch_stand.errors import ChStandError

PACKAGE_ROOT = Path(__file__).resolve().parent
PROFILE_FILES = (
    "single.yaml",
    "replica-pair.yaml",
    "sharded-replicated-4.yaml",
    "sharded-replicated-8.yaml",
)
SCHEMA_FILES = ("ch_stand-v1.schema.json",)
DOCKER_FILES = ("Dockerfile", "ch-stand-entrypoint.sh")


def _ensure_directory(path: Path, *, label: str) -> None:
    if path.is_symlink():
        raise ChStandError(f"{label} must not be a symbolic link: {path}")
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o755)
    except OSError as exc:
        raise ChStandError(f"cannot create {label} {path}: {exc}") from exc
    if not path.is_dir():
        raise ChStandError(f"{label} is not a directory: {path}")


def docker_build_context() -> Path:
    docker_directory = PACKAGE_ROOT / "docker"
    missing = [name for name in DOCKER_FILES if not (docker_directory / name).is_file()]
    if not (PACKAGE_ROOT / ".dockerignore").is_file():
        missing.append("../.dockerignore")
    if missing:
        raise ChStandError("installed ch-stand package lacks Docker assets: " + ", ".join(missing))
    return PACKAGE_ROOT


def _write_asset(source: Path, target: Path, *, force: bool) -> str:
    payload = source.read_bytes()
    existed = target.exists()
    if target.is_symlink():
        raise ChStandError(f"refusing to replace a symbolic link: {target}")
    if existed:
        if not target.is_file():
            raise ChStandError(f"project asset target is not a regular file: {target}")
        if target.read_bytes() == payload:
            return "unchanged"
        if not force:
            raise ChStandError(
                f"project asset already exists with different content: {target}; "
                "use init --force to replace it"
            )
    descriptor, temporary_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
        temporary.replace(target)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise
    return "replaced" if existed else "created"


def initialize_project(directory: str | Path, *, force: bool = False) -> dict[str, object]:
    project_directory = Path(directory).expanduser().resolve()
    _ensure_directory(project_directory, label="project directory")
    files: list[dict[str, str]] = []
    for subdirectory, names in (("configs", PROFILE_FILES), ("schema", SCHEMA_FILES)):
        target_directory = project_directory / subdirectory
        _ensure_directory(target_directory, label="project asset directory")
        if project_directory not in target_directory.resolve().parents:
            raise ChStandError(f"project asset directory escaped the project: {target_directory}")
        for name in names:
            source = PACKAGE_ROOT / subdirectory / name
            if not source.is_file():
                raise ChStandError(f"installed ch-stand package lacks asset: {source}")
            target = target_directory / name
            try:
                state = _write_asset(source, target, force=force)
            except OSError as exc:
                raise ChStandError(f"cannot write project asset {target}: {exc}") from exc
            files.append({"path": str(target), "state": state})
    return {"project_directory": str(project_directory), "force": force, "files": files}
