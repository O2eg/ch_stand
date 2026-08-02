import re
from importlib import resources
from pathlib import Path

import ch_stand

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_distribution_metadata_matches_package() -> None:
    data = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = data["project"]
    assert project["name"] == "ch-stand"
    assert project["version"] == ch_stand.__version__
    assert project["requires-python"] == ">=3.10"
    assert project["license"] == "MIT"
    assert project["license-files"] == ["LICENSE"]
    assert project["authors"] == [{"name": "O2eg", "email": "oleg.ispu@yandex.ru"}]
    assert project["urls"] == {
        "Homepage": "https://o2eg.com/",
        "Repository": "https://github.com/O2eg/ch_stand",
        "Issues": "https://github.com/O2eg/ch_stand/issues",
    }
    assert data["tool"]["setuptools"]["packages"]["find"]["namespaces"] is False

    manifest = (PROJECT_ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    assert "include pyproject.toml" in manifest
    assert "recursive-include tests *.py *.sh" in manifest

    publish = (PROJECT_ROOT / ".github/workflows/publish.yml").read_text(encoding="utf-8")
    assert 'tags:\n      - "v*"' in publish
    assert "GITHUB_REF_NAME" in publish
    assert "pypa/gh-action-pypi-publish@" in publish
    assert "https://pypi.org/p/ch-stand" in publish


def test_runtime_assets_are_packaged() -> None:
    package = resources.files("ch_stand")
    expected = (
        ".dockerignore",
        "configs/single.yaml",
        "configs/replica-pair.yaml",
        "configs/sharded-replicated-4.yaml",
        "configs/sharded-replicated-8.yaml",
        "docker/Dockerfile",
        "docker/ch-stand-entrypoint.sh",
        "schema/ch_stand-v1.schema.json",
    )
    for relative_path in expected:
        assert package.joinpath(relative_path).is_file(), relative_path


def test_pyproject_version_matches_package() -> None:
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version = "([^"]+)"$', pyproject, flags=re.MULTILINE)
    assert match is not None
    assert match.group(1) == ch_stand.__version__


def test_readme_uses_absolute_links_for_pypi() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    links = re.findall(r"\[[^]]+\]\(([^)]+)\)", readme)
    relative_links = [link for link in links if "://" not in link and not link.startswith("#")]
    assert relative_links == []
