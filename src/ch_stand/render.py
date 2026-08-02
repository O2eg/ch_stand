"""Render generated ClickHouse and Keeper configuration fragments."""

from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from pathlib import Path

from ch_stand.config import KeeperSpec, NodeSpec, StandConfig


def _element(
    parent: ET.Element, name: str, value: object | None = None, **attributes: str
) -> ET.Element:
    child = ET.SubElement(parent, name, attributes)
    if value is not None:
        child.text = str(value)
    return child


def _xml(root: ET.Element) -> str:
    ET.indent(root, space="  ")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        + ET.tostring(root, encoding="unicode", short_empty_elements=True)
        + "\n"
    )


def server_config_xml(config: StandConfig, node: NodeSpec) -> str:
    root = ET.Element("clickhouse")
    _element(
        root, "display_name", f"{config.metadata.name} shard {node.shard} replica {node.replica}"
    )
    _element(root, "listen_host", "0.0.0.0")
    _element(root, "http_port", 8123)
    _element(root, "tcp_port", 9000)
    _element(root, "interserver_http_port", 9009)

    logger = _element(root, "logger")
    _element(logger, "level", "information")
    _element(logger, "log", "/var/log/clickhouse-server/clickhouse-server.log")
    _element(logger, "errorlog", "/var/log/clickhouse-server/clickhouse-server.err.log")
    _element(logger, "size", "256M")
    _element(logger, "count", 5)
    _element(logger, "console", 1)

    remote_servers = _element(root, "remote_servers")
    cluster = _element(remote_servers, config.clickhouse.cluster_name)
    for shard_number in range(1, config.topology.shards + 1):
        shard = _element(cluster, "shard")
        _element(shard, "internal_replication", "true" if config.topology.replicated else "false")
        for replica in (item for item in config.nodes if item.shard == shard_number):
            replica_element = _element(shard, "replica")
            _element(replica_element, "host", replica.hostname)
            _element(replica_element, "port", 9000)
            _element(replica_element, "user", config.clickhouse.user)
            _element(replica_element, "password", None, from_env="CH_STAND_PASSWORD")

    macros = _element(root, "macros")
    _element(macros, "shard", f"{node.shard:02d}")
    _element(macros, "replica", node.hostname)
    _element(macros, "cluster", config.clickhouse.cluster_name)

    if config.keepers:
        zookeeper = _element(root, "zookeeper")
        for keeper in config.keepers:
            keeper_node = _element(zookeeper, "node")
            _element(keeper_node, "host", keeper.hostname)
            _element(keeper_node, "port", 9181)
        distributed_ddl = _element(root, "distributed_ddl")
        _element(distributed_ddl, "path", "/clickhouse/task_queue/ddl")
        _element(
            root,
            "default_replica_path",
            "/clickhouse/tables/{shard}/{database}/{table}",
        )
        _element(root, "default_replica_name", "{replica}")

    part_log = _element(root, "part_log")
    _element(part_log, "database", "system")
    _element(part_log, "table", "part_log")
    _element(part_log, "partition_by", "toYYYYMM(event_date)")
    _element(part_log, "flush_interval_milliseconds", 500)
    return _xml(root)


def users_config_xml(config: StandConfig, password: str) -> str:
    root = ET.Element("clickhouse")
    profiles = _element(root, "profiles")
    profile = _element(profiles, "default")
    defaults = {
        "allow_introspection_functions": "1",
        "log_queries": "1",
        "log_query_threads": "1",
        "use_uncompressed_cache": "0",
    }
    defaults.update(config.clickhouse.settings)
    for name, value in sorted(defaults.items()):
        _element(profile, name, value)

    users = _element(root, "users")
    if config.clickhouse.user != "default":
        _element(users, "default", remove="remove")
    user = _element(users, config.clickhouse.user, replace="replace")
    _element(
        user,
        "password_sha256_hex",
        hashlib.sha256(password.encode("utf-8")).hexdigest(),
    )
    networks = _element(user, "networks")
    _element(networks, "ip", "::/0")
    _element(user, "profile", "default")
    _element(user, "quota", "default")
    _element(user, "access_management", 1)
    return _xml(root)


def keeper_config_xml(config: StandConfig, keeper: KeeperSpec) -> str:
    root = ET.Element("clickhouse")
    logger = _element(root, "logger")
    _element(logger, "level", "information")
    _element(logger, "log", "/var/log/clickhouse-keeper/clickhouse-keeper.log")
    _element(logger, "errorlog", "/var/log/clickhouse-keeper/clickhouse-keeper.err.log")
    _element(logger, "size", "256M")
    _element(logger, "count", 5)
    _element(logger, "console", 1)
    _element(root, "listen_host", "0.0.0.0")
    keeper_server = _element(root, "keeper_server")
    _element(keeper_server, "tcp_port", 9181)
    _element(keeper_server, "server_id", keeper.index)
    _element(keeper_server, "log_storage_path", "/var/lib/clickhouse/coordination/log")
    _element(
        keeper_server,
        "snapshot_storage_path",
        "/var/lib/clickhouse/coordination/snapshots",
    )
    _element(keeper_server, "four_letter_word_white_list", "ruok,mntr,stat,srvr,conf")
    settings = _element(keeper_server, "coordination_settings")
    _element(settings, "operation_timeout_ms", 10000)
    _element(settings, "session_timeout_ms", 30000)
    _element(settings, "raft_logs_level", "information")
    raft = _element(keeper_server, "raft_configuration")
    for member in config.keepers:
        server = _element(raft, "server")
        _element(server, "id", member.index)
        _element(server, "hostname", member.hostname)
        _element(server, "port", 9234)
    return _xml(root)


def write_text(path: Path, payload: str, *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise RuntimeError(f"generated configuration target must not be a symlink: {path}")
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.chmod(mode)
    temporary.replace(path)
