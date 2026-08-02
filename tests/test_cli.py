import json
from pathlib import Path

from ch_stand.cli import main


def test_validate_and_show_do_not_require_docker(
    profile_directory: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    profile = profile_directory / "replica-pair.yaml"
    assert main(["--json", "-c", str(profile), "validate"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["clickhouse_nodes"] == 2
    assert result["keeper_nodes"] == 3
    assert main(["--json", "-c", str(profile), "show"]) == 0
    show = capsys.readouterr().out
    assert "password" not in show.lower()


def test_init_materializes_profiles_without_a_config(tmp_path: Path, capsys) -> None:
    assert main(["--json", "init", "--directory", str(tmp_path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["project_directory"] == str(tmp_path)
    assert (tmp_path / "configs/single.yaml").is_file()


def test_machine_capabilities_use_a_versioned_envelope(capsys) -> None:
    assert main(["--machine", "--request-id", "test-1", "--component-capabilities"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["contract_version"] == "pg_play/component/v1"
    assert result["component"] == "ch_stand"
    assert result["request_id"] == "test-1"
    assert result["result"]["config_schemas"] == ["ch_stand/v1"]


def test_machine_mode_rejects_arbitrary_sql(capsys) -> None:
    code = main(["--machine", "--request-id", "test-2", "sql", "SELECT 1"])
    result = json.loads(capsys.readouterr().out)
    assert code == 4
    assert result["status"] == "failed"
    assert result["error"]["code"] == "unsupported"


def test_json_wraps_sql_and_log_text(
    profile_directory: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)

    class Manager:
        @staticmethod
        def sql(statement, *, node_name=None):
            return f"sql:{node_name}:{statement}"

        @staticmethod
        def logs(node_name, *, tail):
            return f"logs:{node_name}:{tail}\n"

    monkeypatch.setattr("ch_stand.cli._manager", lambda _config: Manager())
    profile = profile_directory / "single.yaml"

    assert main(["--json", "-c", str(profile), "sql", "SELECT 1", "--node", "node1"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "node": "node1",
        "output": "sql:node1:SELECT 1",
    }

    assert main(["--json", "-c", str(profile), "logs", "--node", "node1", "--tail", "5"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "node": "node1",
        "output": "logs:node1:5\n",
    }
