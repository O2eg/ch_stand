"""Strict declarative configuration for ClickHouse Docker stands."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import re
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

import yaml

from ch_stand.errors import ConfigError

API_VERSION = "ch_stand/v1"
KIND = "ClickHouseStand"
MANAGED_RESOURCE_SUFFIX = "-ch-stand-managed"
DEFAULT_CLICKHOUSE_VERSION = "25.8.28.1"
DEFAULT_DATABASE = "default"
DEFAULT_USER = "default"
MAX_CLICKHOUSE_NODES = 32

_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
_SETTING_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_VERSION_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_NETWORK_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_MEMORY_RE = re.compile(r"^[1-9][0-9]*(?:[bkmg])?$", re.IGNORECASE)
_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*?))?\}")

MANAGED_PROFILE_SETTINGS = frozenset({"access_management", "readonly"})


@dataclass(frozen=True)
class MetadataConfig:
    name: str


@dataclass(frozen=True)
class ClickHouseConfig:
    version: str
    image: str | None
    database: str
    user: str
    cluster_name: str
    settings: dict[str, str]

    @property
    def base_image_name(self) -> str:
        return self.image or f"clickhouse/clickhouse-server:{self.version}"

    @property
    def image_name(self) -> str:
        if self.image:
            digest = hashlib.sha256(self.image.encode("utf-8")).hexdigest()[:12]
            return f"ch-stand/clickhouse:custom-{digest}{MANAGED_RESOURCE_SUFFIX}"
        return f"ch-stand/clickhouse:{self.version}{MANAGED_RESOURCE_SUFFIX}"


@dataclass(frozen=True)
class TopologyConfig:
    shards: int
    replicas: int
    keeper_nodes: int

    @property
    def node_count(self) -> int:
        return self.shards * self.replicas

    @property
    def replicated(self) -> bool:
        return self.replicas > 1

    @property
    def label(self) -> str:
        return f"{self.shards}x{self.replicas}"


@dataclass(frozen=True)
class DockerConfig:
    pull_policy: str
    network_name: str
    labels: dict[str, str]


@dataclass(frozen=True)
class StorageConfig:
    root_directory: str


@dataclass(frozen=True)
class PortsConfig:
    bind_address: str
    http_base: int
    native_base: int
    ssh_base: int
    keeper_base: int


@dataclass(frozen=True)
class ResourceConfig:
    cpu_limit: float
    memory_limit: str
    shm_size: str


@dataclass(frozen=True)
class ResourcesConfig:
    server: ResourceConfig
    keeper: ResourceConfig


@dataclass(frozen=True)
class DiagnosticsConfig:
    perf: bool


@dataclass(frozen=True)
class NodeSpec:
    index: int
    shard: int
    replica: int
    container_name: str
    hostname: str
    http_port: int
    native_port: int
    ssh_port: int


@dataclass(frozen=True)
class KeeperSpec:
    index: int
    container_name: str
    hostname: str
    client_port: int


@dataclass(frozen=True)
class StandConfig:
    source: Path
    project_directory: Path
    api_version: str
    kind: str
    metadata: MetadataConfig
    clickhouse: ClickHouseConfig
    topology: TopologyConfig
    docker: DockerConfig
    storage: StorageConfig
    ports: PortsConfig
    resources: ResourcesConfig
    diagnostics: DiagnosticsConfig

    @property
    def instance_id(self) -> str:
        return hashlib.sha256(str(self.project_directory).encode("utf-8")).hexdigest()

    @property
    def config_hash(self) -> str:
        document = asdict(self)
        document.pop("source", None)
        document.pop("project_directory", None)
        payload = json.dumps(document, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @property
    def storage_root(self) -> Path:
        return (self.project_directory / self.storage.root_directory).resolve()

    @property
    def nodes(self) -> tuple[NodeSpec, ...]:
        result: list[NodeSpec] = []
        index = 0
        for shard in range(1, self.topology.shards + 1):
            for replica in range(1, self.topology.replicas + 1):
                index += 1
                logical_name = f"{self.metadata.name}-s{shard:02d}r{replica:02d}"
                result.append(
                    NodeSpec(
                        index=index,
                        shard=shard,
                        replica=replica,
                        container_name=f"{logical_name}{MANAGED_RESOURCE_SUFFIX}",
                        hostname=logical_name,
                        http_port=self.ports.http_base + index - 1,
                        native_port=self.ports.native_base + index - 1,
                        ssh_port=self.ports.ssh_base + index - 1,
                    )
                )
        return tuple(result)

    @property
    def keepers(self) -> tuple[KeeperSpec, ...]:
        return tuple(
            KeeperSpec(
                index=index,
                container_name=(
                    f"{self.metadata.name}-keeper-{index:02d}{MANAGED_RESOURCE_SUFFIX}"
                ),
                hostname=f"{self.metadata.name}-keeper-{index:02d}",
                client_port=self.ports.keeper_base + index - 1,
            )
            for index in range(1, self.topology.keeper_nodes + 1)
        )

    def with_clickhouse_version(self, version: str | None) -> StandConfig:
        if version is None:
            return self
        return replace(self, clickhouse=replace(self.clickhouse, version=_version(version)))

    def public_document(self) -> dict[str, Any]:
        document = asdict(self)
        document["source"] = str(self.source)
        document["project_directory"] = str(self.project_directory)
        document["resolved_storage_root"] = str(self.storage_root)
        document["resolved_base_image"] = self.clickhouse.base_image_name
        document["resolved_image"] = self.clickhouse.image_name
        document["instance_id"] = self.instance_id
        document["config_hash"] = self.config_hash
        document["nodes"] = [asdict(node) for node in self.nodes]
        document["keepers"] = [asdict(keeper) for keeper in self.keepers]
        return document


def _interpolate_environment(text: str, environment: dict[str, str]) -> str:
    def replace_match(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        if name in environment:
            return environment[name]
        if default is not None:
            return default
        raise ConfigError(f"environment variable {name} is required")

    return _ENV_RE.sub(replace_match, text)


def _interpolate_document(value: Any, environment: dict[str, str]) -> Any:
    if isinstance(value, str):
        return _interpolate_environment(value, environment)
    if isinstance(value, list):
        return [_interpolate_document(item, environment) for item in value]
    if isinstance(value, dict):
        return {key: _interpolate_document(item, environment) for key, item in value.items()}
    return value


def _mapping(value: Any, label: str, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{label} must be a mapping")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ConfigError(f"unknown {label} fields: {', '.join(unknown)}")
    return value


def _required(mapping: dict[str, Any], key: str, label: str) -> Any:
    if key not in mapping:
        raise ConfigError(f"{label}.{key} is required")
    return mapping[key]


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{label} must be a non-empty string")
    return value.strip()


def _name(value: Any, label: str) -> str:
    result = _text(value, label)
    if not _NAME_RE.fullmatch(result):
        raise ConfigError(f"{label} must match {_NAME_RE.pattern}")
    return result


def _identifier(value: Any, label: str) -> str:
    result = _text(value, label)
    if not _IDENTIFIER_RE.fullmatch(result):
        raise ConfigError(f"{label} must be a ClickHouse identifier")
    return result


def _version(value: Any) -> str:
    result = _text(value, "clickhouse.version").lower()
    if not _VERSION_RE.fullmatch(result):
        raise ConfigError("clickhouse.version must be a Docker branch or full-version tag")
    if result.endswith("-alpine"):
        raise ConfigError(
            "Alpine ClickHouse tags are unsupported by the diagnostic image; use an Ubuntu tag"
        )
    return result


def _image(value: Any) -> str | None:
    if value is None:
        return None
    result = _text(value, "clickhouse.image")
    if any(character.isspace() for character in result):
        raise ConfigError("clickhouse.image must not contain whitespace")
    if result.endswith("-alpine"):
        raise ConfigError("clickhouse.image must use an apt-based Ubuntu image")
    return result


def _positive_int(value: Any, label: str, *, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(f"{label} must be a positive integer")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{label} must be at most {maximum}")
    return value


def _non_negative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigError(f"{label} must be a non-negative integer")
    return value


def _port(value: Any, label: str) -> int:
    result = _positive_int(value, label)
    if result > 65535:
        raise ConfigError(f"{label} must be between 1 and 65535")
    return result


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{label} must be a boolean")
    return value


def _positive_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{label} must be a positive number")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ConfigError(f"{label} must be a positive finite number")
    return result


def _memory(value: Any, label: str) -> str:
    result = _text(value, label)
    if not _MEMORY_RE.fullmatch(result):
        raise ConfigError(f"{label} must be bytes or an integer with b, k, m, or g suffix")
    return result.lower()


def _settings(value: Any) -> dict[str, str]:
    mapping = _mapping(
        value, "clickhouse.settings", set(value) if isinstance(value, dict) else set()
    )
    result: dict[str, str] = {}
    for name, raw in mapping.items():
        if not isinstance(name, str) or not _SETTING_RE.fullmatch(name):
            raise ConfigError(f"invalid ClickHouse profile setting name: {name!r}")
        if name in MANAGED_PROFILE_SETTINGS:
            raise ConfigError(f"ClickHouse profile setting {name} is managed by ch-stand")
        if raw is None or isinstance(raw, (dict, list)):
            raise ConfigError(f"ClickHouse profile setting {name} must be a scalar")
        if isinstance(raw, bool):
            result[name] = "1" if raw else "0"
        elif isinstance(raw, (str, int, float)):
            result[name] = str(raw)
        else:
            raise ConfigError(f"ClickHouse profile setting {name} must be a scalar")
    return result


def _labels(value: Any) -> dict[str, str]:
    mapping = _mapping(value, "docker.labels", set(value) if isinstance(value, dict) else set())
    result: dict[str, str] = {}
    for key, raw in mapping.items():
        if not isinstance(key, str) or not key.strip():
            raise ConfigError("docker label names must be non-empty strings")
        result[key] = _text(raw, f"docker.labels.{key}")
    return result


def _relative_directory(value: Any, label: str) -> str:
    result = Path(_text(value, label))
    if result.is_absolute() or result == Path(".") or ".." in result.parts:
        raise ConfigError(f"{label} must be a relative directory without '..'")
    return str(result)


def _resource(value: Any, label: str, defaults: dict[str, Any]) -> ResourceConfig:
    mapping = _mapping(value, label, {"cpu_limit", "memory_limit", "shm_size"})
    return ResourceConfig(
        cpu_limit=_positive_number(
            mapping.get("cpu_limit", defaults["cpu_limit"]), f"{label}.cpu_limit"
        ),
        memory_limit=_memory(
            mapping.get("memory_limit", defaults["memory_limit"]), f"{label}.memory_limit"
        ),
        shm_size=_memory(mapping.get("shm_size", defaults["shm_size"]), f"{label}.shm_size"),
    )


def load_config(
    path: str | Path,
    *,
    environment: dict[str, str] | None = None,
    clickhouse_version: str | None = None,
    project_directory: str | Path | None = None,
) -> StandConfig:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise ConfigError(f"configuration file does not exist: {source}")
    try:
        raw_document = yaml.safe_load(source.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"cannot parse YAML configuration: {exc}") from exc
    document = _interpolate_document(
        raw_document, dict(os.environ if environment is None else environment)
    )
    root = _mapping(document, "document", {"api_version", "kind", "metadata", "spec"})
    api_version = _text(_required(root, "api_version", "document"), "api_version")
    kind = _text(_required(root, "kind", "document"), "kind")
    if api_version != API_VERSION:
        raise ConfigError(f"api_version must be {API_VERSION}")
    if kind != KIND:
        raise ConfigError(f"kind must be {KIND}")

    metadata_raw = _mapping(_required(root, "metadata", "document"), "metadata", {"name"})
    stand_name = _name(_required(metadata_raw, "name", "metadata"), "metadata.name")
    spec = _mapping(
        _required(root, "spec", "document"),
        "spec",
        {"clickhouse", "topology", "docker", "storage", "ports", "resources", "diagnostics"},
    )

    clickhouse_raw = _mapping(
        _required(spec, "clickhouse", "spec"),
        "spec.clickhouse",
        {"version", "image", "database", "user", "cluster_name", "settings"},
    )
    clickhouse = ClickHouseConfig(
        version=_version(clickhouse_raw.get("version", DEFAULT_CLICKHOUSE_VERSION)),
        image=_image(clickhouse_raw.get("image")),
        database=_identifier(
            clickhouse_raw.get("database", DEFAULT_DATABASE), "clickhouse.database"
        ),
        user=_identifier(clickhouse_raw.get("user", DEFAULT_USER), "clickhouse.user"),
        cluster_name=_identifier(
            clickhouse_raw.get("cluster_name", stand_name.replace("-", "_")),
            "clickhouse.cluster_name",
        ),
        settings=_settings(clickhouse_raw.get("settings", {})),
    )

    topology_raw = _mapping(
        _required(spec, "topology", "spec"),
        "spec.topology",
        {"shards", "replicas", "keeper_nodes"},
    )
    topology = TopologyConfig(
        shards=_positive_int(
            _required(topology_raw, "shards", "topology"), "topology.shards", maximum=16
        ),
        replicas=_positive_int(
            _required(topology_raw, "replicas", "topology"), "topology.replicas", maximum=4
        ),
        keeper_nodes=_non_negative_int(
            topology_raw.get("keeper_nodes", 0), "topology.keeper_nodes"
        ),
    )
    if topology.node_count > MAX_CLICKHOUSE_NODES:
        raise ConfigError(f"topology may contain at most {MAX_CLICKHOUSE_NODES} ClickHouse nodes")
    if topology.node_count == 1 and topology.keeper_nodes != 0:
        raise ConfigError("the 1x1 topology must not start ClickHouse Keeper")
    if topology.node_count > 1 and topology.keeper_nodes != 3:
        raise ConfigError("multi-node topologies require exactly three ClickHouse Keeper nodes")

    docker_raw = _mapping(
        spec.get("docker", {}), "spec.docker", {"pull_policy", "network_name", "labels"}
    )
    pull_policy = _text(docker_raw.get("pull_policy", "missing"), "docker.pull_policy")
    if pull_policy not in {"always", "missing", "never"}:
        raise ConfigError("docker.pull_policy must be always, missing, or never")
    network_name = _text(
        docker_raw.get("network_name", f"{stand_name}-network{MANAGED_RESOURCE_SUFFIX}"),
        "docker.network_name",
    )
    if (
        not _NETWORK_RE.fullmatch(network_name)
        or not network_name.endswith(MANAGED_RESOURCE_SUFFIX)
        or len(network_name) > 63
    ):
        raise ConfigError(
            "docker.network_name must be a lowercase Docker name ending with "
            f"{MANAGED_RESOURCE_SUFFIX}"
        )
    docker = DockerConfig(
        pull_policy=pull_policy,
        network_name=network_name,
        labels=_labels(docker_raw.get("labels", {})),
    )

    storage_raw = _mapping(spec.get("storage", {}), "spec.storage", {"root_directory"})
    storage = StorageConfig(
        root_directory=_relative_directory(
            storage_raw.get("root_directory", f".ch_stand/{stand_name}"),
            "storage.root_directory",
        )
    )

    ports_raw = _mapping(
        spec.get("ports", {}),
        "spec.ports",
        {"bind_address", "http_base", "native_base", "ssh_base", "keeper_base"},
    )
    bind_address = _text(ports_raw.get("bind_address", "127.0.0.1"), "ports.bind_address")
    try:
        ipaddress.ip_address(bind_address)
    except ValueError as exc:
        raise ConfigError("ports.bind_address must be an IPv4 or IPv6 address") from exc
    ports = PortsConfig(
        bind_address=bind_address,
        http_base=_port(ports_raw.get("http_base", 18123), "ports.http_base"),
        native_base=_port(ports_raw.get("native_base", 19000), "ports.native_base"),
        ssh_base=_port(ports_raw.get("ssh_base", 12220), "ports.ssh_base"),
        keeper_base=_port(ports_raw.get("keeper_base", 19181), "ports.keeper_base"),
    )

    resources_raw = _mapping(spec.get("resources", {}), "spec.resources", {"server", "keeper"})
    resources = ResourcesConfig(
        server=_resource(
            resources_raw.get("server", {}),
            "resources.server",
            {"cpu_limit": 1.0, "memory_limit": "2g", "shm_size": "256m"},
        ),
        keeper=_resource(
            resources_raw.get("keeper", {}),
            "resources.keeper",
            {"cpu_limit": 0.5, "memory_limit": "512m", "shm_size": "128m"},
        ),
    )

    diagnostics_raw = _mapping(spec.get("diagnostics", {}), "spec.diagnostics", {"perf"})
    diagnostics = DiagnosticsConfig(
        perf=_boolean(diagnostics_raw.get("perf", True), "diagnostics.perf")
    )

    resolved_project = Path(
        Path.cwd() if project_directory is None else project_directory
    ).resolve()
    storage_root = (resolved_project / storage.root_directory).resolve()
    if storage_root == resolved_project or resolved_project not in storage_root.parents:
        raise ConfigError("storage.root_directory must resolve inside the project directory")

    config = StandConfig(
        source=source,
        project_directory=resolved_project,
        api_version=api_version,
        kind=kind,
        metadata=MetadataConfig(name=stand_name),
        clickhouse=clickhouse,
        topology=topology,
        docker=docker,
        storage=storage,
        ports=ports,
        resources=resources,
        diagnostics=diagnostics,
    ).with_clickhouse_version(clickhouse_version)

    published_ports = [
        port for node in config.nodes for port in (node.http_port, node.native_port, node.ssh_port)
    ] + [keeper.client_port for keeper in config.keepers]
    if any(port > 65535 for port in published_ports):
        raise ConfigError("generated host ports exceed 65535")
    if len(published_ports) != len(set(published_ports)):
        raise ConfigError("generated ClickHouse, SSH, and Keeper host ports must not overlap")
    if any(len(node.container_name) > 63 for node in config.nodes):
        raise ConfigError("metadata.name is too long for generated managed container names")
    return config
