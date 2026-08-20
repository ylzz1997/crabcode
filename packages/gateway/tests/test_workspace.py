from pathlib import Path

import pytest
from fastapi import HTTPException

from crabcode_gateway.routes.workspace import _create_directory


def test_create_directory_inside_workspace_root(tmp_path: Path) -> None:
    created = _create_directory(str(tmp_path / "新项目"), (tmp_path,))

    assert created == tmp_path / "新项目"
    assert created.is_dir()


def test_create_directory_rejects_path_outside_workspace_root(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"

    with pytest.raises(HTTPException) as exc_info:
        _create_directory(str(outside), (tmp_path,))

    assert exc_info.value.status_code == 403
    assert not outside.exists()
