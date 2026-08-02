"""Argument parser for ch-stand."""

from __future__ import annotations

import argparse
import math
from pathlib import Path

from ch_stand import __version__


def _positive_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return result


def _non_negative_int(value: str) -> int:
    result = int(value)
    if result < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return result


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="ch-stand",
        description="Deploy and inspect declarative ClickHouse stands through the Docker SDK.",
    )
    result.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    result.add_argument("-c", "--config", type=Path, help="path to a ch_stand/v1 YAML file")
    result.add_argument(
        "--ch-version",
        "--clickhouse-version",
        dest="clickhouse_version",
        help="override spec.clickhouse.version without editing YAML",
    )
    result.add_argument("--json", action="store_true", help="print machine-readable JSON")
    result.add_argument("--machine", action="store_true", help=argparse.SUPPRESS)
    result.add_argument("--request-id", help=argparse.SUPPRESS)
    result.add_argument("--component-capabilities", action="store_true", help=argparse.SUPPRESS)

    commands = result.add_subparsers(dest="command")
    commands.add_parser("active", help="discover the active ch-stand lease without a config")
    init = commands.add_parser("init", help="write editable profiles and JSON Schema")
    init.add_argument("--directory", type=Path, default=Path("."))
    init.add_argument("--force", action="store_true")
    commands.add_parser("validate", help="validate and resolve the configuration")
    commands.add_parser("show", help="show resolved configuration without secrets")
    commands.add_parser("plan", help="show the required action without changing the stand")

    up = commands.add_parser("up", help="build, create, and start the stand")
    up.add_argument("--timeout", type=_positive_float, default=240.0)

    apply_command = commands.add_parser("apply", help="apply a reviewed restart plan")
    apply_command.add_argument("--restart", action="store_true", required=True)
    apply_command.add_argument("--plan-hash", required=True)
    apply_command.add_argument("--timeout", type=_positive_float, default=240.0)

    commands.add_parser("status", help="show managed container states")
    commands.add_parser("health", help="check ClickHouse and Keeper readiness")

    cluster = commands.add_parser("cluster", help="inspect ClickHouse cluster topology")
    cluster_commands = cluster.add_subparsers(dest="cluster_command", required=True)
    cluster_commands.add_parser("status", help="query system.clusters")

    keeper = commands.add_parser("keeper", help="inspect ClickHouse Keeper quorum")
    keeper_commands = keeper.add_subparsers(dest="keeper_command", required=True)
    keeper_commands.add_parser("status", help="query Keeper mntr endpoints")

    sql = commands.add_parser("sql", help="run SQL inside a ClickHouse server container")
    sql.add_argument("statement")
    sql.add_argument("--node", help="generated hostname, nodeN, or one-based node index")

    logs = commands.add_parser("logs", help="read ClickHouse container stdout/stderr")
    logs.add_argument("--node", help="generated hostname, nodeN, or one-based node index")
    logs.add_argument("--tail", type=_non_negative_int, default=200)

    connection = commands.add_parser("connection", help="show host connection details")
    connection.add_argument("--node")
    connection.add_argument("--show-password", action="store_true")

    ssh = commands.add_parser("ssh", help="open an SSH session to a ClickHouse node")
    ssh.add_argument("--node")

    commands.add_parser("doctor", help="run read-only Docker, port, credential, and perf checks")
    commands.add_parser("capabilities", help="show target and component capabilities")

    image = commands.add_parser("image", help="build or inspect the diagnostic image")
    image_commands = image.add_subparsers(dest="image_command", required=True)
    image_commands.add_parser("status", help="inspect the resolved diagnostic image")
    image_commands.add_parser("build", help="force a diagnostic image rebuild")

    storage = commands.add_parser("storage", help="manage bind-mounted stand storage")
    storage_commands = storage.add_subparsers(dest="storage_command", required=True)
    storage_commands.add_parser("init", help="create credentials, config, and storage directories")
    storage_commands.add_parser("status", help="show storage paths, sizes, and free space")
    storage_clean = storage_commands.add_parser("clean", help="permanently clear stand storage")
    storage_clean.add_argument("--force", action="store_true", required=True)

    stop = commands.add_parser("stop", help="stop containers and preserve all data")
    stop.add_argument("--timeout", type=_non_negative_int, default=30)

    restart = commands.add_parser("restart", help="restart an unchanged stand")
    restart.add_argument("--timeout", type=_positive_float, default=240.0)
    restart.add_argument("--stop-timeout", type=_non_negative_int, default=30)

    down = commands.add_parser("down", help="remove containers and the managed network")
    down.add_argument("--clear-data", action="store_true")
    down.add_argument("--force", action="store_true")
    down.add_argument("--timeout", type=_non_negative_int, default=30)

    recreate = commands.add_parser(
        "recreate", help="remove all stand data and create a fresh cluster"
    )
    recreate.add_argument("--clear-data", action="store_true", required=True)
    recreate.add_argument("--timeout", type=_positive_float, default=240.0)
    recreate.add_argument("--stop-timeout", type=_non_negative_int, default=30)

    cleanup = commands.add_parser("cleanup", help="report or remove owned resources")
    cleanup_commands = cleanup.add_subparsers(dest="cleanup_command", required=True)
    cleanup_commands.add_parser("status", help="show managed resource sizes and states")
    cleanup_run = cleanup_commands.add_parser("run", help="remove explicitly selected scopes")
    cleanup_run.add_argument("--containers", action="store_true")
    cleanup_run.add_argument("--storage", action="store_true")
    cleanup_run.add_argument("--image", action="store_true")
    cleanup_run.add_argument("--credentials", action="store_true")
    cleanup_run.add_argument("--all", action="store_true")
    cleanup_run.add_argument("--force", action="store_true", required=True)
    cleanup_run.add_argument("--stop-timeout", type=_non_negative_int, default=30)
    return result
