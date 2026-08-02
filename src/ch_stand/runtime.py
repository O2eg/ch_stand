"""Docker SDK lifecycle for declarative ClickHouse stands."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import docker
from docker.errors import APIError, BuildError, DockerException, ImageNotFound, NotFound
from docker.types import Ulimit

from ch_stand.assets import docker_build_context
from ch_stand.config import MANAGED_RESOURCE_SUFFIX, KeeperSpec, NodeSpec, StandConfig
from ch_stand.credentials import (
    clean_credentials,
    credential_paths,
    credentials_status,
    ensure_credentials,
    read_credentials,
)
from ch_stand.errors import DockerRuntimeError, PreconditionError
from ch_stand.render import keeper_config_xml, server_config_xml, users_config_xml, write_text
from ch_stand.runtime_common import (
    ACTIVE_LOCK_NETWORK,
    CONFIG_HASH_LABEL,
    IMAGE_BASE_LABEL,
    IMAGE_SCHEMA_LABEL,
    IMAGE_SCHEMA_VERSION,
    INSTANCE_LABEL,
    MANAGED_LABEL,
    NODE_LABEL,
    PROJECT_LABEL,
    REPLICA_LABEL,
    RESOURCE_KIND_LABEL,
    SHARD_LABEL,
    VERSION_LABEL,
    discover_active_stand,
    resource_labels,
    sql_literal,
)

__all__ = ["StandManager", "discover_active_stand"]


@dataclass(frozen=True)
class NodeStoragePaths:
    root: Path
    data: Path
    log: Path
    config: Path
    server_config: Path
    users_config: Path


@dataclass(frozen=True)
class KeeperStoragePaths:
    root: Path
    data: Path
    log: Path
    config: Path
    keeper_config: Path


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _connection_address(address: str) -> str:
    if address == "0.0.0.0":
        return "127.0.0.1"
    if address == "::":
        return "::1"
    return address


class StandManager:
    def __init__(
        self,
        config: StandConfig,
        *,
        client: Any | None = None,
        output: Callable[[str], None] = print,
    ) -> None:
        self.config = config
        self.client = client or docker.from_env()
        self.output = output

    def _ping(self) -> None:
        try:
            self.client.ping()
        except DockerException as exc:
            raise DockerRuntimeError(f"cannot connect to Docker: {exc}") from exc

    def _labels(
        self,
        *,
        resource_kind: str,
        node: NodeSpec | None = None,
    ) -> dict[str, str]:
        labels = {
            **self.config.docker.labels,
            MANAGED_LABEL: "true",
            PROJECT_LABEL: self.config.metadata.name,
            INSTANCE_LABEL: self.config.instance_id,
            CONFIG_HASH_LABEL: self.config.config_hash,
            RESOURCE_KIND_LABEL: resource_kind,
            VERSION_LABEL: self.config.clickhouse.version,
        }
        if node is not None:
            labels.update(
                {
                    NODE_LABEL: node.hostname,
                    SHARD_LABEL: str(node.shard),
                    REPLICA_LABEL: str(node.replica),
                }
            )
        return labels

    def _assert_owned(
        self,
        resource: Any,
        *,
        label: str,
        expected_kind: str,
        check_hash: bool,
    ) -> None:
        labels = resource_labels(resource)
        expected = {
            MANAGED_LABEL: "true",
            PROJECT_LABEL: self.config.metadata.name,
            INSTANCE_LABEL: self.config.instance_id,
            RESOURCE_KIND_LABEL: expected_kind,
        }
        if check_hash:
            expected[CONFIG_HASH_LABEL] = self.config.config_hash
        mismatched = [key for key, value in expected.items() if labels.get(key) != value]
        if mismatched:
            raise DockerRuntimeError(
                f"{label} is not owned by this resolved ch-stand configuration: "
                + ", ".join(mismatched)
            )

    def _find_container(self, name: str) -> Any | None:
        try:
            return self.client.containers.get(name)
        except NotFound:
            return None

    def _find_network(self, name: str) -> Any | None:
        try:
            return self.client.networks.get(name)
        except NotFound:
            return None

    def _find_active_lock(self) -> Any | None:
        return self._find_network(ACTIVE_LOCK_NETWORK)

    def _acquire_active_lock(self) -> bool:
        lock = self._find_active_lock()
        if lock is not None:
            labels = resource_labels(lock)
            if (
                labels.get(MANAGED_LABEL) == "true"
                and labels.get(RESOURCE_KIND_LABEL) == "active-lock"
                and labels.get(PROJECT_LABEL) == self.config.metadata.name
                and labels.get(INSTANCE_LABEL) == self.config.instance_id
            ):
                return False
            raise DockerRuntimeError(
                f"another ch-stand project is active: {labels.get(PROJECT_LABEL) or '<unknown>'}"
            )
        try:
            self.client.networks.create(
                ACTIVE_LOCK_NETWORK,
                driver="bridge",
                labels=self._labels(resource_kind="active-lock"),
                check_duplicate=True,
            )
        except APIError as exc:
            lock = self._find_active_lock()
            if lock is not None:
                labels = resource_labels(lock)
                raise DockerRuntimeError(
                    f"another ch-stand project acquired the active lease: "
                    f"{labels.get(PROJECT_LABEL) or '<unknown>'}"
                ) from exc
            raise DockerRuntimeError(f"cannot acquire the active ch-stand lease: {exc}") from exc
        return True

    def _release_active_lock(self) -> None:
        lock = self._find_active_lock()
        if lock is None:
            return
        labels = resource_labels(lock)
        if (
            labels.get(MANAGED_LABEL) != "true"
            or labels.get(RESOURCE_KIND_LABEL) != "active-lock"
            or labels.get(PROJECT_LABEL) != self.config.metadata.name
            or labels.get(INSTANCE_LABEL) != self.config.instance_id
        ):
            raise DockerRuntimeError("refusing to release an active lease owned by another stand")
        lock.remove()

    def _configured_container_names(self) -> tuple[str, ...]:
        return tuple(node.container_name for node in self.config.nodes) + tuple(
            keeper.container_name for keeper in self.config.keepers
        )

    def _shutdown_containers(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            ("server", node.container_name) for node in reversed(self.config.nodes)
        ) + tuple(("keeper", keeper.container_name) for keeper in reversed(self.config.keepers))

    def _has_running_containers(self) -> bool:
        for name in self._configured_container_names():
            container = self._find_container(name)
            if container is None:
                continue
            container.reload()
            if container.status not in {"created", "exited", "dead"}:
                return True
        return False

    def _ensure_image(self, *, force: bool = False) -> None:
        image_name = self.config.clickhouse.image_name
        expected = {
            IMAGE_SCHEMA_LABEL: IMAGE_SCHEMA_VERSION,
            IMAGE_BASE_LABEL: self.config.clickhouse.base_image_name,
            MANAGED_LABEL: "true",
            RESOURCE_KIND_LABEL: "managed-image",
            VERSION_LABEL: self.config.clickhouse.version,
        }
        rebuild = force or self.config.docker.pull_policy == "always"
        try:
            image = self.client.images.get(image_name)
            labels = (image.attrs.get("Config") or {}).get("Labels") or {}
            if any(labels.get(key) != value for key, value in expected.items()):
                if self.config.docker.pull_policy == "never" and not force:
                    raise DockerRuntimeError(
                        f"diagnostic image {image_name} has an incompatible image schema"
                    )
                rebuild = True
        except ImageNotFound:
            if self.config.docker.pull_policy == "never" and not force:
                raise DockerRuntimeError(
                    f"diagnostic image {image_name} is absent and pull_policy is never"
                ) from None
            rebuild = True
        if not rebuild:
            return
        context = docker_build_context()
        self.output(f"BUILD image={image_name} base={self.config.clickhouse.base_image_name}")
        try:
            self.client.images.build(
                path=str(context),
                dockerfile="docker/Dockerfile",
                tag=image_name,
                buildargs={
                    "CLICKHOUSE_BASE_IMAGE": self.config.clickhouse.base_image_name,
                    "CLICKHOUSE_VERSION": self.config.clickhouse.version,
                },
                pull=force or self.config.docker.pull_policy == "always",
                rm=True,
                forcerm=True,
            )
        except BuildError as exc:
            details = "\n".join(
                str(item.get("stream") or item.get("error") or "").strip()
                for item in exc.build_log
                if item.get("stream") or item.get("error")
            )
            raise DockerRuntimeError(
                f"cannot build diagnostic ClickHouse image {image_name}: {exc}\n{details}"
            ) from exc
        except (APIError, OSError) as exc:
            raise DockerRuntimeError(
                f"cannot build diagnostic ClickHouse image {image_name}: {exc}"
            ) from exc

    def image_status(self) -> dict[str, Any]:
        self._ping()
        try:
            image = self.client.images.get(self.config.clickhouse.image_name)
        except ImageNotFound:
            return {
                "image": self.config.clickhouse.image_name,
                "present": False,
                "compatible": False,
            }
        labels = (image.attrs.get("Config") or {}).get("Labels") or {}
        expected = {
            IMAGE_SCHEMA_LABEL: IMAGE_SCHEMA_VERSION,
            IMAGE_BASE_LABEL: self.config.clickhouse.base_image_name,
            MANAGED_LABEL: "true",
            RESOURCE_KIND_LABEL: "managed-image",
            VERSION_LABEL: self.config.clickhouse.version,
        }
        return {
            "image": self.config.clickhouse.image_name,
            "base_image": self.config.clickhouse.base_image_name,
            "present": True,
            "compatible": all(labels.get(key) == value for key, value in expected.items()),
            "id": image.short_id,
            "size_bytes": int(image.attrs.get("Size", 0) or 0),
            "labels": labels,
            "expected_labels": expected,
        }

    def image_build(self) -> dict[str, Any]:
        self._ping()
        self._ensure_image(force=True)
        return self.image_status()

    def _storage_root(self) -> Path:
        project = self.config.project_directory.resolve()
        requested = self.config.project_directory / self.config.storage.root_directory
        if requested.is_symlink():
            raise DockerRuntimeError(f"storage root must not be a symlink: {requested}")
        root = requested.resolve()
        if root == project or project not in root.parents:
            raise DockerRuntimeError("storage root escaped the project directory")
        return root

    def _safe_child(self, root: Path, name: str) -> Path:
        requested = root / name
        if requested.is_symlink():
            raise DockerRuntimeError(
                f"managed storage directory must not be a symlink: {requested}"
            )
        resolved = requested.resolve()
        if root not in resolved.parents:
            raise DockerRuntimeError(f"managed storage escaped its root: {requested}")
        return resolved

    def _node_storage(self, node: NodeSpec) -> NodeStoragePaths:
        root = self._safe_child(self._storage_root(), node.hostname)
        data = self._safe_child(root, "data")
        log = self._safe_child(root, "log")
        config = self._safe_child(root, "config")
        return NodeStoragePaths(
            root=root,
            data=data,
            log=log,
            config=config,
            server_config=config / "ch-stand.xml",
            users_config=config / "ch-stand-users.xml",
        )

    def _keeper_storage(self, keeper: KeeperSpec) -> KeeperStoragePaths:
        root = self._safe_child(self._storage_root(), keeper.hostname)
        data = self._safe_child(root, "data")
        log = self._safe_child(root, "log")
        config = self._safe_child(root, "config")
        return KeeperStoragePaths(
            root=root,
            data=data,
            log=log,
            config=config,
            keeper_config=config / "keeper_config.xml",
        )

    def _applied_state_path(self) -> Path:
        path = self._storage_root() / ".ch-stand-applied.json"
        if path.is_symlink():
            raise DockerRuntimeError(f"applied state must not be a symlink: {path}")
        return path

    def _prepare_storage(self, password: str) -> None:
        root = self._storage_root()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for node in self.config.nodes:
            paths = self._node_storage(node)
            for directory in (paths.root, paths.data, paths.log, paths.config):
                directory.mkdir(parents=True, exist_ok=True, mode=0o750)
            write_text(paths.server_config, server_config_xml(self.config, node))
            write_text(paths.users_config, users_config_xml(self.config, password))
        for keeper in self.config.keepers:
            paths = self._keeper_storage(keeper)
            for directory in (paths.root, paths.data, paths.log, paths.config):
                directory.mkdir(parents=True, exist_ok=True, mode=0o750)
            write_text(paths.keeper_config, keeper_config_xml(self.config, keeper))

    def storage_init(self) -> dict[str, Any]:
        credentials = ensure_credentials(
            self.config.project_directory, user=self.config.clickhouse.user
        )
        self._prepare_storage(credentials.password)
        return self.storage_status()

    @staticmethod
    def _directory_usage(path: Path) -> tuple[int, int, list[str]]:
        total = 0
        files = 0
        errors: list[str] = []
        if not path.exists():
            return total, files, errors

        def record_error(exc: OSError) -> None:
            errors.append(str(exc))

        for root, _, names in os.walk(path, onerror=record_error):
            for name in names:
                item = Path(root) / name
                try:
                    total += item.stat().st_size
                    files += 1
                except OSError as exc:
                    errors.append(f"{item}: {exc}")
        return total, files, errors

    def storage_status(self) -> dict[str, Any]:
        root = self._storage_root()
        entries = []
        for role, item, paths in (
            *[("server", node.hostname, self._node_storage(node)) for node in self.config.nodes],
            *[
                ("keeper", keeper.hostname, self._keeper_storage(keeper))
                for keeper in self.config.keepers
            ],
        ):
            total, files, errors = self._directory_usage(paths.root)
            entries.append(
                {
                    "role": role,
                    "name": item,
                    "path": str(paths.root),
                    "present": paths.root.exists(),
                    "bytes": total,
                    "files": files,
                    "errors": errors,
                }
            )
        disk_target = root if root.exists() else root.parent
        while not disk_target.exists() and disk_target != disk_target.parent:
            disk_target = disk_target.parent
        disk = shutil.disk_usage(disk_target)
        return {
            "project": self.config.metadata.name,
            "root": str(root),
            "entries": entries,
            "total_bytes": sum(item["bytes"] for item in entries),
            "filesystem": {
                "path": str(disk_target),
                "total_bytes": disk.total,
                "used_bytes": disk.used,
                "free_bytes": disk.free,
            },
        }

    def _containers_exist(self) -> list[str]:
        return [name for name in self._configured_container_names() if self._find_container(name)]

    def storage_clean(self) -> dict[str, Any]:
        self._ping()
        blockers = self._containers_exist()
        if blockers:
            raise DockerRuntimeError(
                "cannot clean storage while stand containers exist: " + ", ".join(blockers)
            )
        root = self._storage_root()
        if not root.exists():
            return {"root": str(root), "cleared": False}
        try:
            shutil.rmtree(root)
        except PermissionError:
            try:
                self.client.containers.run(
                    self.config.clickhouse.image_name,
                    entrypoint=["/bin/sh", "-c"],
                    command=["find /ch-stand-storage -mindepth 1 -delete"],
                    remove=True,
                    user="0:0",
                    volumes={str(root): {"bind": "/ch-stand-storage", "mode": "rw"}},
                    labels=self._labels(resource_kind="storage-helper"),
                )
            except (DockerException, ImageNotFound) as exc:
                raise DockerRuntimeError(
                    f"cannot clear root-owned ClickHouse storage {root}: {exc}"
                ) from exc
            shutil.rmtree(root)
        return {"root": str(root), "cleared": True}

    def _ensure_network(self) -> Any:
        network = self._find_network(self.config.docker.network_name)
        if network is not None:
            self._assert_owned(
                network,
                label="stand network",
                expected_kind="network",
                check_hash=True,
            )
            return network
        try:
            return self.client.networks.create(
                self.config.docker.network_name,
                driver="bridge",
                labels=self._labels(resource_kind="network"),
                check_duplicate=True,
            )
        except APIError as exc:
            raise DockerRuntimeError(f"cannot create stand network: {exc}") from exc

    def _container_options(self, resource: Any) -> dict[str, Any]:
        options: dict[str, Any] = {
            "nano_cpus": int(resource.cpu_limit * 1_000_000_000),
            "mem_limit": resource.memory_limit,
            "shm_size": resource.shm_size,
            "ulimits": [Ulimit(name="nofile", soft=262144, hard=262144)],
            "init": True,
        }
        if self.config.diagnostics.perf:
            options["cap_add"] = ["PERFMON", "SYS_PTRACE"]
            options["security_opt"] = ["seccomp=unconfined"]
        return options

    def _ensure_keeper_container(self, keeper: KeeperSpec, network: Any) -> Any:
        existing = self._find_container(keeper.container_name)
        if existing is not None:
            self._assert_owned(
                existing,
                label=f"Keeper container {keeper.container_name}",
                expected_kind="keeper",
                check_hash=True,
            )
            existing.reload()
            if existing.status != "running":
                existing.start()
            return existing
        paths = self._keeper_storage(keeper)
        credentials = credential_paths(self.config.project_directory)
        try:
            container = self.client.containers.create(
                self.config.clickhouse.image_name,
                name=keeper.container_name,
                hostname=keeper.hostname,
                network=network.name,
                labels=self._labels(resource_kind="keeper"),
                environment={"CH_STAND_ROLE": "keeper"},
                ports={"9181/tcp": (self.config.ports.bind_address, keeper.client_port)},
                volumes={
                    str(paths.data): {"bind": "/var/lib/clickhouse", "mode": "rw"},
                    str(paths.log): {"bind": "/var/log/clickhouse-keeper", "mode": "rw"},
                    str(paths.keeper_config): {
                        "bind": "/etc/clickhouse-keeper/keeper_config.xml",
                        "mode": "ro",
                    },
                    str(credentials.ssh): {"bind": "/ch-stand-ssh-source", "mode": "ro"},
                },
                **self._container_options(self.config.resources.keeper),
            )
            self.output(f"START keeper={keeper.hostname} port={keeper.client_port}")
            container.start()
            return container
        except APIError as exc:
            raise DockerRuntimeError(
                f"cannot create Keeper container {keeper.hostname}: {exc}"
            ) from exc

    def _ensure_server_container(self, node: NodeSpec, network: Any, password: str) -> Any:
        existing = self._find_container(node.container_name)
        if existing is not None:
            self._assert_owned(
                existing,
                label=f"ClickHouse container {node.container_name}",
                expected_kind="server",
                check_hash=True,
            )
            existing.reload()
            if existing.status != "running":
                existing.start()
            return existing
        paths = self._node_storage(node)
        credentials = credential_paths(self.config.project_directory)
        try:
            container = self.client.containers.create(
                self.config.clickhouse.image_name,
                name=node.container_name,
                hostname=node.hostname,
                network=network.name,
                labels=self._labels(resource_kind="server", node=node),
                environment={
                    "CH_STAND_ROLE": "server",
                    "CH_STAND_PASSWORD": password,
                    "CLICKHOUSE_SKIP_USER_SETUP": "1",
                },
                ports={
                    "8123/tcp": (self.config.ports.bind_address, node.http_port),
                    "9000/tcp": (self.config.ports.bind_address, node.native_port),
                    "22/tcp": (self.config.ports.bind_address, node.ssh_port),
                },
                volumes={
                    str(paths.data): {"bind": "/var/lib/clickhouse", "mode": "rw"},
                    str(paths.log): {"bind": "/var/log/clickhouse-server", "mode": "rw"},
                    str(paths.server_config): {
                        "bind": "/etc/clickhouse-server/config.d/ch-stand.xml",
                        "mode": "ro",
                    },
                    str(paths.users_config): {
                        "bind": "/etc/clickhouse-server/users.d/ch-stand-users.xml",
                        "mode": "ro",
                    },
                    str(credentials.ssh): {"bind": "/ch-stand-ssh-source", "mode": "ro"},
                },
                **self._container_options(self.config.resources.server),
            )
            self.output(
                f"START node={node.hostname} shard={node.shard} replica={node.replica} "
                f"native_port={node.native_port}"
            )
            container.start()
            return container
        except APIError as exc:
            raise DockerRuntimeError(
                f"cannot create ClickHouse container {node.hostname}: {exc}"
            ) from exc

    @staticmethod
    def _exec(container: Any, command: list[str], *, check: bool = True) -> str:
        result = container.exec_run(command)
        output = (
            result.output.decode("utf-8", errors="replace")
            if isinstance(result.output, bytes)
            else str(result.output)
        )
        if check and result.exit_code != 0:
            raise DockerRuntimeError(
                f"command failed in {container.name} with exit {result.exit_code}: {output.strip()}"
            )
        return output.strip()

    def _client_command(self, password: str, query: str, *, json_format: bool = False) -> list[str]:
        command = [
            "clickhouse-client",
            "--user",
            self.config.clickhouse.user,
            "--password",
            password,
            "--database",
            self.config.clickhouse.database,
            "--multiquery",
            "--query",
            query,
        ]
        if json_format:
            command.extend(["--format", "JSON"])
        return command

    def _wait_for_tcp(self, port: int, timeout_seconds: float) -> None:
        deadline = time.monotonic() + timeout_seconds
        address = _connection_address(self.config.ports.bind_address)
        last_error = "not ready"
        while time.monotonic() < deadline:
            try:
                with socket.create_connection((address, port), timeout=1):
                    return
            except OSError as exc:
                last_error = str(exc)
                time.sleep(0.5)
        raise DockerRuntimeError(
            f"TCP endpoint {address}:{port} did not become ready: {last_error}"
        )

    def _wait_for_server(self, container: Any, password: str, timeout_seconds: float) -> None:
        deadline = time.monotonic() + timeout_seconds
        last_error = "not ready"
        while time.monotonic() < deadline:
            container.reload()
            if container.status in {"exited", "dead"}:
                logs = container.logs(tail=100).decode("utf-8", errors="replace")
                raise DockerRuntimeError(
                    f"ClickHouse container {container.name} stopped during startup:\n{logs}"
                )
            try:
                output = self._exec(
                    container,
                    self._client_command(password, "SELECT 1"),
                )
                if output == "1":
                    return
                last_error = output
            except DockerRuntimeError as exc:
                last_error = str(exc)
            time.sleep(1)
        raise DockerRuntimeError(
            f"ClickHouse container {container.name} did not become ready: {last_error}"
        )

    def _node_container(self, node: NodeSpec, *, check_hash: bool = False) -> Any:
        container = self._find_container(node.container_name)
        if container is None:
            raise DockerRuntimeError(
                f"ClickHouse node does not exist: {node.hostname}; run up first"
            )
        self._assert_owned(
            container,
            label=f"ClickHouse container {node.container_name}",
            expected_kind="server",
            check_hash=check_hash,
        )
        return container

    def _node_by_name(self, name: str | None) -> NodeSpec:
        if name is None:
            return self.config.nodes[0]
        for node in self.config.nodes:
            if name in {node.hostname, str(node.index), f"node{node.index}"}:
                return node
        valid = ", ".join(node.hostname for node in self.config.nodes)
        raise DockerRuntimeError(f"unknown ClickHouse node {name}; choose one of: {valid}")

    def _assert_current_stand_active(self) -> None:
        lock = self._find_active_lock()
        if lock is None:
            raise DockerRuntimeError("no active ch-stand stand; run up first")
        self._assert_owned(
            lock,
            label="active stand lease",
            expected_kind="active-lock",
            check_hash=False,
        )

    def _identity_signature(self) -> str:
        identity = {
            "project": self.config.metadata.name,
            "version": self.config.clickhouse.version,
            "base_image": self.config.clickhouse.base_image_name,
            "cluster_name": self.config.clickhouse.cluster_name,
            "topology": asdict(self.config.topology),
            "network": self.config.docker.network_name,
            "storage": self.config.storage.root_directory,
            "nodes": [node.container_name for node in self.config.nodes],
            "keepers": [keeper.container_name for keeper in self.config.keepers],
        }
        return hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def _read_applied_state(self) -> dict[str, Any] | None:
        path = self._applied_state_path()
        if not path.is_file():
            return None
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DockerRuntimeError(f"cannot read applied state {path}: {exc}") from exc
        if not isinstance(state, dict):
            raise DockerRuntimeError(f"applied state is invalid: {path}")
        return state

    def _record_applied_state(self) -> None:
        path = self._applied_state_path()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        document = {
            "state_version": 1,
            "config_hash": self.config.config_hash,
            "identity_signature": self._identity_signature(),
            "clickhouse_version": self.config.clickhouse.version,
            "topology": asdict(self.config.topology),
            "document": self.config.public_document(),
        }
        write_text(path, json.dumps(document, indent=2, sort_keys=True) + "\n", mode=0o600)

    def plan(self) -> dict[str, Any]:
        state = self._read_applied_state()
        result: dict[str, Any] = {
            "project": self.config.metadata.name,
            "required_action": "none",
            "can_apply": True,
            "desired_state_hash": self.config.config_hash,
            "applied_state_hash": state.get("config_hash") if state else None,
        }
        if state is None:
            result.update(required_action="up", reason="the stand has no applied state")
        elif state.get("config_hash") == self.config.config_hash:
            result["reason"] = "the desired and applied configurations match"
        elif state.get("identity_signature") != self._identity_signature():
            result.update(
                required_action="blocked",
                can_apply=False,
                reason=(
                    "ClickHouse version, topology, cluster identity, image, network, or storage "
                    "changed; use recreate --clear-data or the old config to run down"
                ),
            )
        else:
            result.update(
                required_action="restart",
                reason="ports, resources, diagnostics, labels, or profile settings changed",
            )
        result["plan_hash"] = _canonical_hash(
            {
                "project": result["project"],
                "required_action": result["required_action"],
                "desired_state_hash": result["desired_state_hash"],
                "applied_state_hash": result["applied_state_hash"],
            }
        )
        return result

    def _assert_up_configuration_is_applied(self) -> None:
        state = self._read_applied_state()
        if state is not None and state.get("config_hash") != self.config.config_hash:
            raise DockerRuntimeError(
                "the YAML differs from the applied stand; run plan and apply --restart, or "
                "recreate --clear-data for an identity change"
            )

    def up(self, *, timeout_seconds: float = 240.0) -> dict[str, Any]:
        self._ping()
        self._assert_up_configuration_is_applied()
        return self._activate(timeout_seconds=timeout_seconds)

    def _activate(self, *, timeout_seconds: float) -> dict[str, Any]:
        acquired = self._acquire_active_lock()
        try:
            result = self._up_locked(timeout_seconds=timeout_seconds)
            self._record_applied_state()
            return result
        except Exception:
            if acquired and not self._has_running_containers():
                self._release_active_lock()
            raise

    def _up_locked(self, *, timeout_seconds: float) -> dict[str, Any]:
        credentials = ensure_credentials(
            self.config.project_directory, user=self.config.clickhouse.user
        )
        self._ensure_image()
        self._prepare_storage(credentials.password)
        network = self._ensure_network()
        keepers = [self._ensure_keeper_container(keeper, network) for keeper in self.config.keepers]
        for keeper in self.config.keepers:
            self._wait_for_tcp(keeper.client_port, timeout_seconds)
        servers = [
            self._ensure_server_container(node, network, credentials.password)
            for node in self.config.nodes
        ]
        for container in servers:
            self._wait_for_server(container, credentials.password, timeout_seconds)
        expected = self.config.topology.node_count
        count = self._exec(
            servers[0],
            self._client_command(
                credentials.password,
                "SELECT count() FROM system.clusters WHERE cluster = "
                + sql_literal(self.config.clickhouse.cluster_name),
            ),
        )
        if count != str(expected):
            raise DockerRuntimeError(
                f"system.clusters reports {count} nodes for {self.config.clickhouse.cluster_name}, "
                f"expected {expected}"
            )
        if keepers:
            self._exec(
                servers[0],
                self._client_command(
                    credentials.password,
                    "SELECT count() FROM system.zookeeper WHERE path = '/'",
                ),
            )
        self.output(
            f"READY project={self.config.metadata.name} topology={self.config.topology.label} "
            f"nodes={expected} keepers={len(keepers)}"
        )
        return {
            "project": self.config.metadata.name,
            "clickhouse_version": self.config.clickhouse.version,
            "image": self.config.clickhouse.image_name,
            "cluster": self.config.clickhouse.cluster_name,
            "topology": self.config.topology.label,
            "nodes": [asdict(node) for node in self.config.nodes],
            "keepers": [asdict(keeper) for keeper in self.config.keepers],
        }

    def apply(self, *, plan_hash: str, timeout_seconds: float = 240.0) -> dict[str, Any]:
        current = self.plan()
        if current["plan_hash"] != plan_hash:
            raise PreconditionError(
                f"stale plan: expected {plan_hash}, current plan is {current['plan_hash']}"
            )
        if not current["can_apply"]:
            raise PreconditionError(current["reason"])
        if current["required_action"] == "none":
            return {"project": self.config.metadata.name, "action": "none"}
        if current["required_action"] == "up":
            return {"action": "up", "stand": self.up(timeout_seconds=timeout_seconds)}
        self.down(clear_data=False)
        return {"action": "restart", "stand": self._activate(timeout_seconds=timeout_seconds)}

    def status(self) -> dict[str, Any]:
        self._ping()
        rows = []
        for role, name in (
            *[("server", node.container_name) for node in self.config.nodes],
            *[("keeper", keeper.container_name) for keeper in self.config.keepers],
        ):
            container = self._find_container(name)
            if container is None:
                rows.append({"role": role, "container": name, "state": "absent"})
                continue
            self._assert_owned(
                container,
                label=f"{role} container {name}",
                expected_kind=role,
                check_hash=False,
            )
            container.reload()
            labels = resource_labels(container)
            rows.append(
                {
                    "role": role,
                    "container": name,
                    "state": container.status,
                    "image": (container.attrs.get("Config") or {}).get("Image"),
                    "config_hash": labels.get(CONFIG_HASH_LABEL),
                    "started_at": (container.attrs.get("State") or {}).get("StartedAt"),
                }
            )
        active = discover_active_stand(client=self.client)
        return {
            "project": self.config.metadata.name,
            "active": active.get("project") == self.config.metadata.name,
            "topology": self.config.topology.label,
            "containers": rows,
        }

    def sql(self, statement: str, *, node_name: str | None = None) -> str:
        self._ping()
        self._assert_current_stand_active()
        node = self._node_by_name(node_name)
        container = self._node_container(node)
        credentials = read_credentials(
            self.config.project_directory, expected_user=self.config.clickhouse.user
        )
        return self._exec(
            container,
            self._client_command(credentials.password, statement),
        )

    def cluster_status(self) -> dict[str, Any]:
        query = (
            "SELECT cluster, shard_num, replica_num, host_name, host_address, port, "
            "is_local, errors_count, slowdowns_count FROM system.clusters WHERE cluster = "
            + sql_literal(self.config.clickhouse.cluster_name)
            + " ORDER BY shard_num, replica_num FORMAT JSON"
        )
        payload = self.sql(query)
        try:
            document = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise DockerRuntimeError(f"cannot decode system.clusters output: {exc}") from exc
        return {
            "cluster": self.config.clickhouse.cluster_name,
            "expected_nodes": self.config.topology.node_count,
            "rows": document.get("data", []),
        }

    def keeper_status(self) -> dict[str, Any]:
        if not self.config.keepers:
            return {"configured": False, "keepers": []}
        self._ping()
        self._assert_current_stand_active()
        container = self._node_container(self.config.nodes[0])
        rows = []
        for keeper in self.config.keepers:
            command = [
                "sh",
                "-c",
                f"printf 'mntr\\n' | nc -w 2 {keeper.hostname} 9181",
            ]
            output = self._exec(container, command, check=False)
            values = {}
            for line in output.splitlines():
                key, separator, value = line.partition("\t")
                if separator:
                    values[key] = value
            rows.append(
                {
                    "keeper": keeper.hostname,
                    "host_port": keeper.client_port,
                    "state": values.get("zk_server_state", "unavailable"),
                    "version": values.get("zk_version"),
                    "metrics": values,
                }
            )
        return {"configured": True, "keepers": rows}

    def health(self) -> dict[str, Any]:
        self._ping()
        checks: list[dict[str, Any]] = []

        lock = self._find_active_lock()
        if lock is None:
            checks.append(
                {
                    "id": "stand.active_lock",
                    "status": "unavailable",
                    "detail": "this stand does not own the active lease",
                }
            )
        else:
            try:
                self._assert_owned(
                    lock,
                    label="active stand lease",
                    expected_kind="active-lock",
                    check_hash=True,
                )
            except DockerRuntimeError as exc:
                checks.append(
                    {"id": "stand.active_lock", "status": "unavailable", "detail": str(exc)}
                )
            else:
                checks.append(
                    {
                        "id": "stand.active_lock",
                        "status": "healthy",
                        "detail": "this stand owns the active lease",
                    }
                )

        try:
            state = self._read_applied_state()
        except DockerRuntimeError as exc:
            checks.append(
                {"id": "stand.configuration", "status": "unavailable", "detail": str(exc)}
            )
        else:
            matches = state is not None and state.get("config_hash") == self.config.config_hash
            checks.append(
                {
                    "id": "stand.configuration",
                    "status": "healthy" if matches else "unavailable",
                    "detail": "configuration matches"
                    if matches
                    else "applied configuration is absent or differs from YAML",
                }
            )

        credentials = None
        try:
            credentials = read_credentials(
                self.config.project_directory, expected_user=self.config.clickhouse.user
            )
        except DockerRuntimeError as exc:
            checks.append({"id": "credentials", "status": "unavailable", "detail": str(exc)})
        for node in self.config.nodes:
            container = self._find_container(node.container_name)
            if container is None:
                checks.append(
                    {"id": f"node.{node.hostname}", "status": "unavailable", "detail": "absent"}
                )
                continue
            try:
                self._assert_owned(
                    container,
                    label=f"ClickHouse container {node.container_name}",
                    expected_kind="server",
                    check_hash=True,
                )
            except DockerRuntimeError as exc:
                checks.append(
                    {"id": f"node.{node.hostname}", "status": "unavailable", "detail": str(exc)}
                )
                continue
            container.reload()
            if container.status != "running" or credentials is None:
                checks.append(
                    {
                        "id": f"node.{node.hostname}",
                        "status": "unavailable",
                        "detail": container.status,
                    }
                )
                continue
            try:
                version = self._exec(
                    container,
                    self._client_command(credentials.password, "SELECT version()"),
                )
                checks.append(
                    {"id": f"node.{node.hostname}", "status": "healthy", "version": version}
                )
            except DockerRuntimeError as exc:
                checks.append(
                    {"id": f"node.{node.hostname}", "status": "unavailable", "detail": str(exc)}
                )
        keeper_containers_ready = True
        keeper_container_states: dict[str, str] = {}
        for keeper in self.config.keepers:
            container = self._find_container(keeper.container_name)
            state = "absent"
            if container is not None:
                try:
                    self._assert_owned(
                        container,
                        label=f"Keeper container {keeper.container_name}",
                        expected_kind="keeper",
                        check_hash=True,
                    )
                except DockerRuntimeError as exc:
                    state = str(exc)
                else:
                    container.reload()
                    state = container.status
            keeper_containers_ready = keeper_containers_ready and state == "running"
            keeper_container_states[keeper.hostname] = state

        keeper_states: dict[str, str] = {}
        if self.config.keepers and keeper_containers_ready:
            try:
                keeper_states = {
                    item["keeper"]: item["state"] for item in self.keeper_status()["keepers"]
                }
            except DockerRuntimeError:
                keeper_states = {}

        for keeper in self.config.keepers:
            process_state = keeper_container_states[keeper.hostname]
            quorum_state = keeper_states.get(keeper.hostname, "unavailable")
            healthy = process_state == "running" and quorum_state in {"leader", "follower"}
            checks.append(
                {
                    "id": f"keeper.{keeper.hostname}",
                    "status": "healthy" if healthy else "unavailable",
                    "detail": {"container": process_state, "quorum": quorum_state},
                }
            )
        if self.config.keepers:
            states = list(keeper_states.values())
            quorum_healthy = states.count("leader") == 1 and states.count("follower") == (
                len(self.config.keepers) - 1
            )
            checks.append(
                {
                    "id": "keeper.quorum",
                    "status": "healthy" if quorum_healthy else "unavailable",
                    "detail": keeper_states,
                }
            )
        level = "healthy" if all(item["status"] == "healthy" for item in checks) else "unavailable"
        return {
            "project": self.config.metadata.name,
            "status": level,
            "exit_code": 0 if level == "healthy" else 2,
            "checks": checks,
        }

    def logs(self, node_name: str | None = None, *, tail: int = 200) -> str:
        self._ping()
        node = self._node_by_name(node_name)
        container = self._node_container(node)
        output = container.logs(tail=tail)
        return (
            output.decode("utf-8", errors="replace") if isinstance(output, bytes) else str(output)
        )

    def connection_info(
        self, node_name: str | None = None, *, show_password: bool = False
    ) -> dict[str, Any]:
        node = self._node_by_name(node_name)
        result = {
            "node": node.hostname,
            "host": _connection_address(self.config.ports.bind_address),
            "http_port": node.http_port,
            "native_port": node.native_port,
            "ssh_port": node.ssh_port,
            "database": self.config.clickhouse.database,
            "user": self.config.clickhouse.user,
        }
        if show_password:
            result["password"] = read_credentials(
                self.config.project_directory, expected_user=self.config.clickhouse.user
            ).password
        else:
            result["password"] = "<redacted>"
        return result

    def run_ssh(self, node_name: str | None = None) -> int:
        executable = shutil.which("ssh")
        if executable is None:
            raise DockerRuntimeError("ssh is not installed on the host or is not in PATH")
        self._ping()
        self._assert_current_stand_active()
        node = self._node_by_name(node_name)
        container = self._node_container(node, check_hash=True)
        container.reload()
        if container.status != "running":
            raise DockerRuntimeError(f"ClickHouse container {container.name} is not running")
        credentials = read_credentials(
            self.config.project_directory, expected_user=self.config.clickhouse.user
        )
        return subprocess.run(
            [
                executable,
                "-i",
                str(credentials.ssh_private),
                "-p",
                str(node.ssh_port),
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "UserKnownHostsFile=/dev/null",
                f"root@{_connection_address(self.config.ports.bind_address)}",
            ],
            check=False,
        ).returncode

    def stop(self, *, timeout_seconds: int = 30) -> None:
        self._ping()
        for role, name in self._shutdown_containers():
            container = self._find_container(name)
            if container is None:
                continue
            self._assert_owned(
                container,
                label=f"{role} container {name}",
                expected_kind=role,
                check_hash=False,
            )
            container.reload()
            if container.status not in {"created", "exited", "dead"}:
                self.output(f"STOP container={name}")
                container.stop(timeout=timeout_seconds)
        self._release_active_lock()

    def down(
        self,
        *,
        clear_data: bool = False,
        timeout_seconds: int = 30,
        release_lock: bool = True,
    ) -> None:
        self._ping()
        if clear_data:
            self._acquire_active_lock()
        for role, name in self._shutdown_containers():
            container = self._find_container(name)
            if container is None:
                continue
            self._assert_owned(
                container,
                label=f"{role} container {name}",
                expected_kind=role,
                check_hash=False,
            )
            container.reload()
            if container.status not in {"created", "exited", "dead"}:
                self.output(f"STOP container={name}")
                container.stop(timeout=timeout_seconds)
            self.output(f"REMOVE container={name}")
            container.remove(force=False, v=True)
        network = self._find_network(self.config.docker.network_name)
        if network is not None:
            self._assert_owned(
                network,
                label="stand network",
                expected_kind="network",
                check_hash=False,
            )
            network.remove()
        if clear_data:
            self.storage_clean()
        if release_lock:
            self._release_active_lock()

    def recreate(
        self,
        *,
        clear_data: bool,
        timeout_seconds: float = 240.0,
        stop_timeout_seconds: int = 30,
    ) -> dict[str, Any]:
        if not clear_data:
            raise DockerRuntimeError("recreate requires --clear-data to make data loss explicit")
        self.down(clear_data=True, timeout_seconds=stop_timeout_seconds)
        return self._activate(timeout_seconds=timeout_seconds)

    @staticmethod
    def _port_available(address: str, port: int) -> tuple[bool, str]:
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            sock.bind((address, port))
        except OSError as exc:
            return False, str(exc)
        finally:
            sock.close()
        return True, "available"

    def doctor(self) -> dict[str, Any]:
        self._ping()
        info = self.client.info()
        checks: list[dict[str, Any]] = [
            {
                "id": "docker.daemon",
                "status": "healthy",
                "detail": f"Docker {info.get('ServerVersion', 'unknown')}",
            }
        ]
        for node in self.config.nodes:
            container = self._find_container(node.container_name)
            for service, port in (
                ("http", node.http_port),
                ("native", node.native_port),
                ("ssh", node.ssh_port),
            ):
                if container is None:
                    available, detail = self._port_available(self.config.ports.bind_address, port)
                else:
                    try:
                        self._assert_owned(
                            container,
                            label=f"ClickHouse container {node.container_name}",
                            expected_kind="server",
                            check_hash=False,
                        )
                    except DockerRuntimeError as exc:
                        available, detail = False, str(exc)
                    else:
                        available, detail = True, "reserved by the owned container"
                checks.append(
                    {
                        "id": f"node.{node.hostname}.port.{service}",
                        "status": "healthy" if available else "unavailable",
                        "detail": f"{self.config.ports.bind_address}:{port}: {detail}",
                    }
                )
        for keeper in self.config.keepers:
            container = self._find_container(keeper.container_name)
            if container is None:
                available, detail = self._port_available(
                    self.config.ports.bind_address, keeper.client_port
                )
            else:
                try:
                    self._assert_owned(
                        container,
                        label=f"Keeper container {keeper.container_name}",
                        expected_kind="keeper",
                        check_hash=False,
                    )
                except DockerRuntimeError as exc:
                    available, detail = False, str(exc)
                else:
                    available, detail = True, "reserved by the owned container"
            checks.append(
                {
                    "id": f"keeper.{keeper.hostname}.port",
                    "status": "healthy" if available else "unavailable",
                    "detail": detail,
                }
            )
        credential_report = credentials_status(self.config.project_directory)
        credential_ready = credential_report["state"] in {"absent", "ready"}
        checks.append(
            {
                "id": "credentials",
                "status": "healthy" if credential_ready else "unavailable",
                "detail": credential_report,
            }
        )
        paranoid_path = Path("/proc/sys/kernel/perf_event_paranoid")
        paranoid = (
            paranoid_path.read_text(encoding="utf-8").strip() if paranoid_path.is_file() else None
        )
        checks.append(
            {
                "id": "diagnostics.perf",
                "status": "healthy",
                "detail": {
                    "enabled": self.config.diagnostics.perf,
                    "host_perf_event_paranoid": paranoid,
                    "capabilities": ["PERFMON", "SYS_PTRACE"]
                    if self.config.diagnostics.perf
                    else [],
                    "note": (
                        "host kernel policy can still restrict perf even when container "
                        "capabilities are present"
                    ),
                },
            }
        )
        level = "healthy" if all(item["status"] == "healthy" for item in checks) else "unavailable"
        return {
            "project": self.config.metadata.name,
            "status": level,
            "exit_code": 0 if level == "healthy" else 2,
            "checks": checks,
        }

    def capabilities(self) -> dict[str, Any]:
        return {
            "component": "ch_stand",
            "component_version": __import__("ch_stand").__version__,
            "config_schema": "ch_stand/v1",
            "clickhouse_version": self.config.clickhouse.version,
            "base_image": self.config.clickhouse.base_image_name,
            "diagnostic_image": self.config.clickhouse.image_name,
            "topology": {
                **asdict(self.config.topology),
                "node_count": self.config.topology.node_count,
                "label": self.config.topology.label,
            },
            "interfaces": ["http", "native", "ssh"],
            "diagnostic_tools": [
                "perf",
                "bpftrace",
                "gdb",
                "strace",
                "lsof",
                "lshw",
                "sysstat",
                "fio",
                "stress-ng",
                "iotop",
                "tcpdump",
                "ethtool",
                "numactl",
            ],
            "keeper": {"enabled": bool(self.config.keepers), "nodes": len(self.config.keepers)},
            "supports": {
                "replicated_merge_tree_defaults": bool(self.config.keepers),
                "distributed_ddl": bool(self.config.keepers),
                "distributed_tables": True,
                "perf_capabilities": self.config.diagnostics.perf,
            },
        }

    def cleanup_status(self) -> dict[str, Any]:
        return {
            "project": self.config.metadata.name,
            "containers": self.status()["containers"],
            "storage": self.storage_status(),
            "credentials": credentials_status(self.config.project_directory),
            "image": self.image_status(),
        }

    def cleanup(
        self,
        *,
        remove_containers: bool,
        remove_storage: bool,
        remove_image: bool,
        remove_credentials: bool,
        stop_timeout_seconds: int = 30,
    ) -> dict[str, Any]:
        if not any((remove_containers, remove_storage, remove_image, remove_credentials)):
            raise DockerRuntimeError("cleanup requires at least one explicit scope")
        self._ping()
        if (
            (remove_storage or remove_credentials)
            and not remove_containers
            and self._containers_exist()
        ):
            raise DockerRuntimeError(
                "storage or shared credentials cannot be removed while stand containers exist"
            )
        result: dict[str, Any] = {"project": self.config.metadata.name}
        if remove_containers:
            self.down(clear_data=remove_storage, timeout_seconds=stop_timeout_seconds)
            result["containers"] = "removed"
            if remove_storage:
                result["storage"] = "removed"
        elif remove_storage:
            result["storage"] = self.storage_clean()
        if remove_credentials:
            result["credentials_removed"] = clean_credentials(self.config.project_directory)
        if remove_image:
            try:
                image = self.client.images.get(self.config.clickhouse.image_name)
            except ImageNotFound:
                result["image_removed"] = False
            else:
                labels = (image.attrs.get("Config") or {}).get("Labels") or {}
                if (
                    labels.get(MANAGED_LABEL) != "true"
                    or labels.get(RESOURCE_KIND_LABEL) != "managed-image"
                    or not any(tag.endswith(MANAGED_RESOURCE_SUFFIX) for tag in (image.tags or []))
                ):
                    raise DockerRuntimeError("refusing to remove an image not owned by ch-stand")
                self.client.images.remove(
                    self.config.clickhouse.image_name, force=False, noprune=False
                )
                result["image_removed"] = True
        return result
