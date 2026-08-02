"""Human and machine command-line execution."""

from __future__ import annotations

import contextlib
import io
import json
import sys
from collections.abc import Sequence
from typing import Any

import docker

from ch_stand.assets import initialize_project
from ch_stand.cli_parser import parser
from ch_stand.config import StandConfig, load_config
from ch_stand.errors import ChStandError, DockerRuntimeError, PreconditionError
from ch_stand.orchestration import EXIT_CODES, envelope, static_capabilities
from ch_stand.runtime import StandManager, discover_active_stand


def _emit(value: Any, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, indent=2, sort_keys=True))
    elif isinstance(value, str):
        print(value, end="" if value.endswith("\n") else "\n")
    else:
        print(json.dumps(value, indent=2, sort_keys=True))


def _manager(config: StandConfig) -> StandManager:
    return StandManager(config)


def _execute(args: Any, config: StandConfig) -> int:
    if args.command == "validate":
        _emit(
            {
                "valid": True,
                "config": str(config.source),
                "project": config.metadata.name,
                "clickhouse_version": config.clickhouse.version,
                "base_image": config.clickhouse.base_image_name,
                "topology": config.topology.label,
                "clickhouse_nodes": config.topology.node_count,
                "keeper_nodes": config.topology.keeper_nodes,
            },
            as_json=args.json,
        )
        return 0
    if args.command == "show":
        _emit(config.public_document(), as_json=args.json)
        return 0

    manager = _manager(config)
    if args.command == "plan":
        result = manager.plan()
        _emit(result, as_json=args.json)
        return 2 if result["required_action"] == "blocked" else 0
    if args.command == "up":
        _emit(manager.up(timeout_seconds=args.timeout), as_json=args.json)
    elif args.command == "apply":
        _emit(
            manager.apply(plan_hash=args.plan_hash, timeout_seconds=args.timeout),
            as_json=args.json,
        )
    elif args.command == "status":
        _emit(manager.status(), as_json=args.json)
    elif args.command == "health":
        result = manager.health()
        _emit(result, as_json=args.json)
        return int(result["exit_code"])
    elif args.command == "cluster":
        _emit(manager.cluster_status(), as_json=args.json)
    elif args.command == "keeper":
        _emit(manager.keeper_status(), as_json=args.json)
    elif args.command == "sql":
        output = manager.sql(args.statement, node_name=args.node)
        _emit(
            {"node": args.node or config.nodes[0].hostname, "output": output}
            if args.json
            else output,
            as_json=args.json,
        )
    elif args.command == "logs":
        output = manager.logs(args.node, tail=args.tail)
        _emit(
            {"node": args.node or config.nodes[0].hostname, "output": output}
            if args.json
            else output,
            as_json=args.json,
        )
    elif args.command == "connection":
        _emit(
            manager.connection_info(args.node, show_password=args.show_password),
            as_json=args.json,
        )
    elif args.command == "ssh":
        return manager.run_ssh(args.node)
    elif args.command == "doctor":
        result = manager.doctor()
        _emit(result, as_json=args.json)
        return int(result["exit_code"])
    elif args.command == "capabilities":
        _emit(
            {"contract": static_capabilities(), "target": manager.capabilities()},
            as_json=args.json,
        )
    elif args.command == "image":
        result = manager.image_build() if args.image_command == "build" else manager.image_status()
        _emit(result, as_json=args.json)
    elif args.command == "storage":
        if args.storage_command == "init":
            result = manager.storage_init()
        elif args.storage_command == "status":
            result = manager.storage_status()
        else:
            result = manager.storage_clean()
        _emit(result, as_json=args.json)
    elif args.command == "stop":
        manager.stop(timeout_seconds=args.timeout)
        _emit({"project": config.metadata.name, "state": "stopped"}, as_json=args.json)
    elif args.command == "restart":
        manager.stop(timeout_seconds=args.stop_timeout)
        _emit(manager.up(timeout_seconds=args.timeout), as_json=args.json)
    elif args.command == "down":
        if args.clear_data and not args.force:
            raise DockerRuntimeError("down --clear-data also requires --force")
        manager.down(clear_data=args.clear_data, timeout_seconds=args.timeout)
        _emit(
            {"project": config.metadata.name, "state": "removed", "data_cleared": args.clear_data},
            as_json=args.json,
        )
    elif args.command == "recreate":
        _emit(
            manager.recreate(
                clear_data=args.clear_data,
                timeout_seconds=args.timeout,
                stop_timeout_seconds=args.stop_timeout,
            ),
            as_json=args.json,
        )
    elif args.command == "cleanup":
        if args.cleanup_command == "status":
            result = manager.cleanup_status()
        else:
            result = manager.cleanup(
                remove_containers=args.containers or args.all,
                remove_storage=args.storage or args.all,
                remove_image=args.image or args.all,
                remove_credentials=args.credentials or args.all,
                stop_timeout_seconds=args.stop_timeout,
            )
        _emit(result, as_json=args.json)
    else:  # pragma: no cover
        raise AssertionError(f"unhandled command: {args.command}")
    return 0


def _command_name(args: Any) -> str:
    parts = [args.command]
    for name in (
        "cluster_command",
        "keeper_command",
        "image_command",
        "storage_command",
        "cleanup_command",
    ):
        value = getattr(args, name, None)
        if value:
            parts.append(value)
    return " ".join(parts)


def _machine_supported(args: Any) -> bool:
    if args.command in {"sql", "ssh"}:
        return False
    return not (args.command == "connection" and args.show_password)


def _emit_machine(
    args: Any,
    *,
    result: Any = None,
    status: str = "succeeded",
    error: dict[str, Any] | None = None,
    config: StandConfig | None = None,
) -> None:
    artifacts = []
    if config is not None:
        artifacts.append(
            {
                "kind": "StandConfiguration",
                "schema_version": "ch_stand/v1",
                "hash": config.config_hash,
                "path": str(config.source),
            }
        )
    _emit(
        envelope(
            _command_name(args),
            status,
            request_id=args.request_id,
            result=result,
            artifacts=artifacts,
            error=error,
        ),
        as_json=True,
    )


def _run_machine(args: Any) -> int:
    if not _machine_supported(args):
        _emit_machine(
            args,
            status="failed",
            error={"code": "unsupported", "message": "command is unavailable in machine mode"},
        )
        return EXIT_CODES["unsupported"]
    config = None
    try:
        args.json = True
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            if args.command == "active":
                result = discover_active_stand()
                exit_code = 0 if result["active"] else 1
                print(json.dumps(result))
            elif args.command == "init":
                print(json.dumps(initialize_project(args.directory, force=args.force)))
                exit_code = 0
            elif args.command == "capabilities" and args.config is None:
                print(json.dumps(static_capabilities()))
                exit_code = 0
            else:
                if args.config is None:
                    raise DockerRuntimeError(f"--config is required for {args.command}")
                config = load_config(args.config, clickhouse_version=args.clickhouse_version)
                exit_code = _execute(args, config)
        text = output.getvalue().strip()
        try:
            result = json.loads(text) if text else None
        except json.JSONDecodeError:
            result = {"stdout": text}
        status = "succeeded" if exit_code == 0 else "partial"
        if args.command == "plan" and isinstance(result, dict):
            if result.get("required_action") == "blocked":
                status = "blocked"
        _emit_machine(args, result=result, status=status, config=config)
        if status == "blocked":
            return EXIT_CODES["precondition_failed"]
        return EXIT_CODES["partial"] if exit_code == 1 else exit_code
    except PreconditionError as exc:
        _emit_machine(
            args,
            status="blocked",
            error={"code": "precondition_failed", "message": str(exc)},
            config=config,
        )
        return EXIT_CODES["precondition_failed"]
    except (ChStandError, docker.errors.DockerException) as exc:
        _emit_machine(
            args,
            status="failed",
            error={"code": "execution_error", "message": str(exc)},
            config=config,
        )
        return EXIT_CODES["execution_error"]
    except KeyboardInterrupt:
        _emit_machine(
            args,
            status="cancelled",
            error={"code": "cancelled", "message": "interrupted"},
            config=config,
        )
        return EXIT_CODES["cancelled"]


def main(argv: Sequence[str] | None = None) -> int:
    argument_parser = parser()
    args = argument_parser.parse_args(argv)
    if args.component_capabilities:
        args.command = "capabilities"
    elif args.command is None:
        argument_parser.error("a command is required")
    if args.machine:
        return _run_machine(args)
    try:
        if args.command == "active":
            result = discover_active_stand()
            _emit(result, as_json=args.json)
            return 0 if result["active"] else 1
        if args.command == "init":
            _emit(initialize_project(args.directory, force=args.force), as_json=args.json)
            return 0
        if args.command == "capabilities" and args.config is None:
            _emit(static_capabilities(), as_json=args.json)
            return 0
        if args.config is None:
            raise DockerRuntimeError(f"--config is required for {args.command}")
        config = load_config(args.config, clickhouse_version=args.clickhouse_version)
        return _execute(args, config)
    except (ChStandError, docker.errors.DockerException) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("ERROR: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
