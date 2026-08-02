import json
from pathlib import Path

import pytest
import yaml

from ch_stand.config import MANAGED_RESOURCE_SUFFIX, ConfigError, load_config

PROFILE_LAYOUTS = {
    "single.yaml": (1, 1, 0),
    "replica-pair.yaml": (1, 2, 3),
    "sharded-replicated-4.yaml": (2, 2, 3),
    "sharded-replicated-8.yaml": (4, 2, 3),
}


@pytest.mark.parametrize(("filename", "layout"), PROFILE_LAYOUTS.items())
def test_bundled_profiles_are_valid(
    profile_directory: Path, tmp_path: Path, filename: str, layout: tuple[int, int, int]
) -> None:
    config = load_config(profile_directory / filename, project_directory=tmp_path)
    shards, replicas, keepers = layout
    assert (
        config.topology.shards,
        config.topology.replicas,
        config.topology.keeper_nodes,
    ) == layout
    assert len(config.nodes) == shards * replicas
    assert len(config.keepers) == keepers
    assert config.clickhouse.version == "25.8.28.1"
    assert config.clickhouse.base_image_name == "clickhouse/clickhouse-server:25.8.28.1"
    assert config.clickhouse.image_name.endswith(MANAGED_RESOURCE_SUFFIX)
    ports = [
        port for node in config.nodes for port in (node.http_port, node.native_port, node.ssh_port)
    ] + [keeper.client_port for keeper in config.keepers]
    assert len(ports) == len(set(ports))


def test_two_by_two_layout_is_deterministic(profile_directory: Path, tmp_path: Path) -> None:
    config = load_config(
        profile_directory / "sharded-replicated-4.yaml", project_directory=tmp_path
    )
    assert [(node.index, node.shard, node.replica) for node in config.nodes] == [
        (1, 1, 1),
        (2, 1, 2),
        (3, 2, 1),
        (4, 2, 2),
    ]
    assert [node.native_port for node in config.nodes] == [19020, 19021, 19022, 19023]
    assert all(node.container_name.endswith(MANAGED_RESOURCE_SUFFIX) for node in config.nodes)


def test_version_override_changes_base_and_diagnostic_image(
    profile_directory: Path, tmp_path: Path
) -> None:
    config = load_config(
        profile_directory / "single.yaml",
        project_directory=tmp_path,
        clickhouse_version="26.3",
    )
    assert config.clickhouse.version == "26.3"
    assert config.clickhouse.base_image_name == "clickhouse/clickhouse-server:26.3"
    assert config.clickhouse.image_name == "ch-stand/clickhouse:26.3-ch-stand-managed"


def _document(profile_directory: Path, filename: str = "single.yaml") -> dict:
    return yaml.safe_load((profile_directory / filename).read_text(encoding="utf-8"))


def _write(tmp_path: Path, document: dict) -> Path:
    path = tmp_path / "stand.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def test_rejects_alpine_version(profile_directory: Path, tmp_path: Path) -> None:
    document = _document(profile_directory)
    document["spec"]["clickhouse"]["version"] = "25.8-alpine"
    with pytest.raises(ConfigError, match="Alpine"):
        load_config(_write(tmp_path, document), project_directory=tmp_path)


def test_rejects_passwords_in_yaml(profile_directory: Path, tmp_path: Path) -> None:
    document = _document(profile_directory)
    document["spec"]["clickhouse"]["password"] = "secret"
    with pytest.raises(ConfigError, match="unknown spec.clickhouse fields: password"):
        load_config(_write(tmp_path, document), project_directory=tmp_path)


def test_multi_node_requires_three_keepers(profile_directory: Path, tmp_path: Path) -> None:
    document = _document(profile_directory, "replica-pair.yaml")
    document["spec"]["topology"]["keeper_nodes"] = 1
    with pytest.raises(ConfigError, match="exactly three"):
        load_config(_write(tmp_path, document), project_directory=tmp_path)


def test_single_rejects_keeper(profile_directory: Path, tmp_path: Path) -> None:
    document = _document(profile_directory)
    document["spec"]["topology"]["keeper_nodes"] = 3
    with pytest.raises(ConfigError, match="1x1"):
        load_config(_write(tmp_path, document), project_directory=tmp_path)


def test_rejects_storage_escape(profile_directory: Path, tmp_path: Path) -> None:
    document = _document(profile_directory)
    document["spec"]["storage"]["root_directory"] = "../outside"
    with pytest.raises(ConfigError, match="relative directory"):
        load_config(_write(tmp_path, document), project_directory=tmp_path)


def test_rejects_overlapping_generated_ports(profile_directory: Path, tmp_path: Path) -> None:
    document = _document(profile_directory)
    document["spec"]["ports"]["native_base"] = document["spec"]["ports"]["http_base"]
    with pytest.raises(ConfigError, match="must not overlap"):
        load_config(_write(tmp_path, document), project_directory=tmp_path)


def test_rejects_invalid_docker_network_name(profile_directory: Path, tmp_path: Path) -> None:
    document = _document(profile_directory)
    document["spec"]["docker"]["network_name"] = "invalid name-ch-stand-managed"
    with pytest.raises(ConfigError, match="docker.network_name"):
        load_config(_write(tmp_path, document), project_directory=tmp_path)


def test_environment_interpolation(profile_directory: Path, tmp_path: Path) -> None:
    document = _document(profile_directory)
    document["spec"]["clickhouse"]["version"] = "${CH_VERSION:-25.8}"
    config = load_config(
        _write(tmp_path, document),
        project_directory=tmp_path,
        environment={"CH_VERSION": "26.3.10.30"},
    )
    assert config.clickhouse.version == "26.3.10.30"


def test_public_document_contains_resolved_nodes_without_secrets(
    profile_directory: Path, tmp_path: Path
) -> None:
    config = load_config(profile_directory / "replica-pair.yaml", project_directory=tmp_path)
    payload = json.dumps(config.public_document())
    assert '"nodes"' in payload
    assert '"keepers"' in payload
    assert "password" not in payload.lower()


def test_json_schema_is_valid_json() -> None:
    path = Path(__file__).parents[1] / "src/ch_stand/schema/ch_stand-v1.schema.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["properties"]["api_version"]["const"] == "ch_stand/v1"
