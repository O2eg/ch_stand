import hashlib
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path

from ch_stand.config import load_config
from ch_stand.render import keeper_config_xml, server_config_xml, users_config_xml


def test_cluster_config_has_shards_replicas_macros_and_keeper_quorum(
    profile_directory: Path, tmp_path: Path
) -> None:
    config = load_config(
        profile_directory / "sharded-replicated-4.yaml", project_directory=tmp_path
    )
    node = config.nodes[2]
    root = ET.fromstring(server_config_xml(config, node))
    cluster = root.find(f"./remote_servers/{config.clickhouse.cluster_name}")
    assert cluster is not None
    shards = cluster.findall("shard")
    assert len(shards) == 2
    assert [len(shard.findall("replica")) for shard in shards] == [2, 2]
    assert all(shard.findtext("internal_replication") == "true" for shard in shards)
    assert root.findtext("./macros/shard") == "02"
    assert root.findtext("./macros/replica") == node.hostname
    assert len(root.findall("./zookeeper/node")) == 3
    assert root.findtext("default_replica_path") == (
        "/clickhouse/tables/{shard}/{database}/{table}"
    )
    password = root.find(
        f"./remote_servers/{config.clickhouse.cluster_name}/shard/replica/password"
    )
    assert password is not None
    assert password.attrib == {"from_env": "CH_STAND_PASSWORD"}


def test_single_config_has_cluster_but_no_keeper(profile_directory: Path, tmp_path: Path) -> None:
    config = load_config(profile_directory / "single.yaml", project_directory=tmp_path)
    root = ET.fromstring(server_config_xml(config, config.nodes[0]))
    assert root.find(f"./remote_servers/{config.clickhouse.cluster_name}") is not None
    assert root.find("zookeeper") is None
    assert root.find("default_replica_path") is None


def test_users_config_hashes_password_and_enables_diagnostics(
    profile_directory: Path, tmp_path: Path
) -> None:
    config = load_config(profile_directory / "single.yaml", project_directory=tmp_path)
    payload = users_config_xml(config, "private-value")
    assert "private-value" not in payload
    root = ET.fromstring(payload)
    user = root.find(f"./users/{config.clickhouse.user}")
    assert user is not None
    assert user.attrib == {"replace": "replace"}
    password_hash = user.findtext("password_sha256_hex")
    assert password_hash == hashlib.sha256(b"private-value").hexdigest()
    assert root.findtext("./profiles/default/allow_introspection_functions") == "1"
    assert root.findtext("./profiles/default/max_threads") == "4"


def test_custom_user_removes_bundled_default_user(profile_directory: Path, tmp_path: Path) -> None:
    config = load_config(profile_directory / "single.yaml", project_directory=tmp_path)
    config = replace(config, clickhouse=replace(config.clickhouse, user="developer"))
    root = ET.fromstring(users_config_xml(config, "private-value"))
    default_user = root.find("./users/default")
    custom_user = root.find("./users/developer")
    assert default_user is not None
    assert default_user.attrib == {"remove": "remove"}
    assert custom_user is not None
    assert custom_user.attrib == {"replace": "replace"}


def test_keeper_config_contains_same_three_member_raft_group(
    profile_directory: Path, tmp_path: Path
) -> None:
    config = load_config(profile_directory / "replica-pair.yaml", project_directory=tmp_path)
    for keeper in config.keepers:
        root = ET.fromstring(keeper_config_xml(config, keeper))
        assert root.findtext("./keeper_server/server_id") == str(keeper.index)
        servers = root.findall("./keeper_server/raft_configuration/server")
        assert [server.findtext("id") for server in servers] == ["1", "2", "3"]
        assert [server.findtext("hostname") for server in servers] == [
            item.hostname for item in config.keepers
        ]
