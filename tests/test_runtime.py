import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from docker.errors import ImageNotFound, NotFound

from ch_stand.config import load_config
from ch_stand.errors import DockerRuntimeError
from ch_stand.runtime import StandManager
from ch_stand.runtime_common import (
    CONFIG_HASH_LABEL,
    INSTANCE_LABEL,
    MANAGED_LABEL,
    PROJECT_LABEL,
    RESOURCE_KIND_LABEL,
)


def test_plan_transitions_are_deterministic(profile_directory: Path, tmp_path: Path) -> None:
    config = load_config(profile_directory / "single.yaml", project_directory=tmp_path)
    manager = StandManager(config, client=object())
    initial = manager.plan()
    assert initial["required_action"] == "up"
    assert initial["plan_hash"].startswith("sha256:")

    manager._record_applied_state()
    unchanged = manager.plan()
    assert unchanged["required_action"] == "none"
    assert unchanged["applied_state_hash"] == config.config_hash

    changed_config = replace(
        config,
        diagnostics=replace(config.diagnostics, perf=False),
    )
    restart = StandManager(changed_config, client=object()).plan()
    assert restart["required_action"] == "restart"
    assert restart["can_apply"] is True

    version_config = config.with_clickhouse_version("26.3")
    blocked = StandManager(version_config, client=object()).plan()
    assert blocked["required_action"] == "blocked"
    assert blocked["can_apply"] is False


def test_storage_rejects_symlinked_node_directory(profile_directory: Path, tmp_path: Path) -> None:
    config = load_config(profile_directory / "single.yaml", project_directory=tmp_path)
    manager = StandManager(config, client=object())
    config.storage_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (config.storage_root / config.nodes[0].hostname).symlink_to(outside, target_is_directory=True)
    with pytest.raises(DockerRuntimeError, match="must not be a symlink"):
        manager._node_storage(config.nodes[0])


def test_client_command_is_an_argv_and_does_not_use_a_shell(
    profile_directory: Path, tmp_path: Path
) -> None:
    config = load_config(profile_directory / "single.yaml", project_directory=tmp_path)
    manager = StandManager(config, client=object())
    statement = "SELECT '$HOME; $(id)'"
    command = manager._client_command("secret", statement)
    assert isinstance(command, list)
    assert command[-1] == statement
    assert "sh" not in command[:1]


def test_applied_state_contains_no_password(profile_directory: Path, tmp_path: Path) -> None:
    config = load_config(profile_directory / "replica-pair.yaml", project_directory=tmp_path)
    manager = StandManager(config, client=object())
    manager._record_applied_state()
    payload = manager._applied_state_path().read_text(encoding="utf-8")
    assert "password" not in payload.lower()
    assert json.loads(payload)["topology"]["replicas"] == 2


def test_perf_container_options_are_explicit(profile_directory: Path, tmp_path: Path) -> None:
    config = load_config(profile_directory / "single.yaml", project_directory=tmp_path)
    manager = StandManager(config, client=object())
    options = manager._container_options(config.resources.server)
    assert options["cap_add"] == ["PERFMON", "SYS_PTRACE"]
    assert options["security_opt"] == ["seccomp=unconfined"]
    assert options["ulimits"][0]["Name"] == "nofile"


def test_explicit_image_build_overrides_never_pull_policy(
    profile_directory: Path, tmp_path: Path
) -> None:
    config = load_config(profile_directory / "single.yaml", project_directory=tmp_path)
    config = replace(config, docker=replace(config.docker, pull_policy="never"))

    class MissingImages:
        def __init__(self) -> None:
            self.build_options = None

        def get(self, _name):
            raise ImageNotFound("missing")

        def build(self, **options):
            self.build_options = options

    images = MissingImages()
    manager = StandManager(config, client=SimpleNamespace(images=images))
    manager._ensure_image(force=True)
    assert images.build_options is not None
    assert images.build_options["pull"] is True


def test_shutdown_order_stops_servers_before_keepers(
    profile_directory: Path, tmp_path: Path
) -> None:
    config = load_config(profile_directory / "replica-pair.yaml", project_directory=tmp_path)
    manager = StandManager(config, client=object())
    assert manager._shutdown_containers() == (
        ("server", config.nodes[1].container_name),
        ("server", config.nodes[0].container_name),
        ("keeper", config.keepers[2].container_name),
        ("keeper", config.keepers[1].container_name),
        ("keeper", config.keepers[0].container_name),
    )


def test_stop_refuses_foreign_container(profile_directory: Path, tmp_path: Path) -> None:
    config = load_config(profile_directory / "single.yaml", project_directory=tmp_path)
    foreign = MagicMock()
    foreign.attrs = {"Config": {"Labels": {}}}
    client = MagicMock()
    client.containers.get.return_value = foreign
    manager = StandManager(config, client=client)

    with pytest.raises(DockerRuntimeError, match="not owned"):
        manager.stop()

    foreign.stop.assert_not_called()


def test_health_rejects_foreign_container(profile_directory: Path, tmp_path: Path) -> None:
    config = load_config(profile_directory / "single.yaml", project_directory=tmp_path)
    foreign = MagicMock()
    foreign.attrs = {"Config": {"Labels": {}}}
    foreign.status = "running"
    client = MagicMock()
    client.containers.get.return_value = foreign
    lock = MagicMock()
    lock.attrs = {
        "Labels": {
            MANAGED_LABEL: "true",
            PROJECT_LABEL: config.metadata.name,
            INSTANCE_LABEL: config.instance_id,
            RESOURCE_KIND_LABEL: "active-lock",
            CONFIG_HASH_LABEL: config.config_hash,
        }
    }
    client.networks.get.return_value = lock
    manager = StandManager(config, client=client)
    manager._record_applied_state()

    with patch(
        "ch_stand.runtime.read_credentials", return_value=SimpleNamespace(password="secret")
    ):
        result = manager.health()

    node_check = next(item for item in result["checks"] if item["id"].startswith("node."))
    assert node_check["status"] == "unavailable"
    foreign.exec_run.assert_not_called()


def test_doctor_treats_disabled_perf_as_supported_configuration(
    profile_directory: Path, tmp_path: Path
) -> None:
    config = load_config(profile_directory / "single.yaml", project_directory=tmp_path)
    config = replace(config, diagnostics=replace(config.diagnostics, perf=False))
    client = MagicMock()
    client.info.return_value = {"ServerVersion": "test"}
    client.containers.get.side_effect = NotFound("missing")
    manager = StandManager(config, client=client)

    with patch.object(manager, "_port_available", return_value=(True, "available")):
        result = manager.doctor()

    perf = next(item for item in result["checks"] if item["id"] == "diagnostics.perf")
    assert perf["status"] == "healthy"
    assert result["status"] == "healthy"


def test_directory_usage_reports_walk_permission_errors(
    profile_directory: Path, tmp_path: Path
) -> None:
    config = load_config(profile_directory / "single.yaml", project_directory=tmp_path)
    manager = StandManager(config, client=object())
    root = tmp_path / "storage"
    root.mkdir()

    def denied_walk(_path, *, onerror):
        onerror(PermissionError("permission denied"))
        return []

    with patch("ch_stand.runtime.os.walk", side_effect=denied_walk):
        _total, _files, errors = manager._directory_usage(root)

    assert errors
    assert "permission denied" in errors[0]


def test_ssh_reports_missing_host_client(profile_directory: Path, tmp_path: Path) -> None:
    config = load_config(profile_directory / "single.yaml", project_directory=tmp_path)
    manager = StandManager(config, client=object())
    with patch("ch_stand.runtime.shutil.which", return_value=None):
        with pytest.raises(DockerRuntimeError, match="ssh is not installed"):
            manager.run_ssh(None)
