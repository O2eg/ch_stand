"""Stable machine envelope and component capability document."""

from __future__ import annotations

from typing import Any

from ch_stand import __version__

CONTRACT_VERSION = "ch_play/component/v1"
CAPABILITY_SCHEMA_VERSION = "ch_play/capabilities/v1"
COMPONENT = "ch_stand"

EXIT_CODES = {
    "success": 0,
    "validation_error": 2,
    "precondition_failed": 3,
    "unsupported": 4,
    "partial": 5,
    "execution_error": 6,
    "cancelled": 7,
    "ownership_error": 8,
}


def static_capabilities() -> dict[str, Any]:
    read_only = {
        command: {
            "mutates_target": False,
            "machine_output": True,
            "accepts_plan_hash": False,
        }
        for command in (
            "capabilities",
            "active",
            "validate",
            "show",
            "plan",
            "status",
            "health",
        )
    }
    mutating = {
        command: {
            "mutates_target": True,
            "machine_output": True,
            "accepts_plan_hash": command == "apply",
        }
        for command in ("up", "apply", "stop", "down")
    }
    return {
        "capability_schema_version": CAPABILITY_SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "component": COMPONENT,
        "component_version": __version__,
        "machine_interface": {
            "machine_flag": "--machine",
            "request_id_option": "--request-id",
            "capabilities_option": "--component-capabilities",
        },
        "commands": {**read_only, **mutating},
        "config_schemas": ["ch_stand/v1"],
        "states": [
            "planned",
            "running",
            "succeeded",
            "partial",
            "failed",
            "cancelled",
            "skipped",
            "blocked",
        ],
        "exit_codes": EXIT_CODES,
        "machine_unsupported_commands": ["connection --show-password", "sql", "ssh"],
        "secret_policy": {
            "machine_output_redacts_secrets": True,
            "password_requires_explicit_human_flag": True,
        },
    }


def envelope(
    command: str,
    status: str,
    *,
    request_id: str | None,
    result: Any = None,
    artifacts: list[dict[str, Any]] | None = None,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "contract_version": CONTRACT_VERSION,
        "component": COMPONENT,
        "component_version": __version__,
        "command": command,
        "request_id": request_id,
        "status": status,
        "result": result,
        "artifacts": artifacts or [],
        "warnings": [],
        "error": error,
    }
