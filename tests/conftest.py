from pathlib import Path

import pytest

from ch_stand.assets import PACKAGE_ROOT


@pytest.fixture
def profile_directory() -> Path:
    return PACKAGE_ROOT / "configs"
