"""Shared Docker ownership constants and helpers."""

from __future__ import annotations

from typing import Any

import docker

from ch_stand.config import MANAGED_RESOURCE_SUFFIX
from ch_stand.errors import DockerRuntimeError

ACTIVE_LOCK_NETWORK = f"ch-stand-active-lock{MANAGED_RESOURCE_SUFFIX}"
MANAGED_LABEL = "io.ch-stand.managed"
PROJECT_LABEL = "io.ch-stand.project"
INSTANCE_LABEL = "io.ch-stand.instance"
CONFIG_HASH_LABEL = "io.ch-stand.config-hash"
RESOURCE_KIND_LABEL = "io.ch-stand.resource-kind"
NODE_LABEL = "io.ch-stand.node"
SHARD_LABEL = "io.ch-stand.shard"
REPLICA_LABEL = "io.ch-stand.replica"
VERSION_LABEL = "io.ch-stand.clickhouse-version"

IMAGE_SCHEMA_LABEL = "io.ch-stand.image-schema"
IMAGE_BASE_LABEL = "io.ch-stand.base-image"
IMAGE_SCHEMA_VERSION = "1"


def resource_labels(resource: Any) -> dict[str, str]:
    attrs = getattr(resource, "attrs", {}) or {}
    return attrs.get("Labels") or (attrs.get("Config") or {}).get("Labels") or {}


def discover_active_stand(*, client: Any | None = None) -> dict[str, Any]:
    docker_client = client or docker.from_env()
    try:
        networks = docker_client.networks.list(
            names=[ACTIVE_LOCK_NETWORK],
            filters={
                "label": [
                    f"{MANAGED_LABEL}=true",
                    f"{RESOURCE_KIND_LABEL}=active-lock",
                ]
            },
        )
    except docker.errors.DockerException as exc:
        raise DockerRuntimeError(f"cannot discover active ch-stand lease: {exc}") from exc
    exact = [network for network in networks if network.name == ACTIVE_LOCK_NETWORK]
    if not exact:
        return {"active": False, "lock_network": ACTIVE_LOCK_NETWORK}
    if len(exact) != 1:
        raise DockerRuntimeError("multiple active ch-stand lock networks were discovered")
    labels = resource_labels(exact[0])
    return {
        "active": True,
        "lock_network": ACTIVE_LOCK_NETWORK,
        "project": labels.get(PROJECT_LABEL),
        "instance_id": labels.get(INSTANCE_LABEL),
        "config_hash": labels.get(CONFIG_HASH_LABEL),
        "clickhouse_version": labels.get(VERSION_LABEL),
    }


def sql_literal(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
