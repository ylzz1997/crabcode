"""Git review reads real temporary repositories without staging or modifying them."""
import os
import subprocess

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from crabcode_gateway.routes import git_review
from crabcode_gateway.schemas import WorkspaceInfo


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.STDOUT).decode().strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "review@example.test")
    git(root, "config", "user.name", "Review test")
    (root / "file.txt").write_text("base\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "initial")
    return root


@pytest.fixture
def client(tmp_path):
    app = FastAPI()
    app.state.workspace_info = WorkspaceInfo(startup_cwd=str(tmp_path), home=str(tmp_path), browse_roots=[str(tmp_path)])
    app.include_router(git_review.router)
    with TestClient(app) as value:
        yield value


def diff(client, repo, scope, **kwargs):
    response = client.get("/workspace/git/diff", params={"path": str(repo), "scope": scope, **kwargs})
    assert response.status_code == 200, response.text
    return response.json()


def test_distinct_index_worktree_and_uncommitted_scopes(client, repo):
    (repo / "file.txt").write_text("staged\n")
    git(repo, "add", "file.txt")
    (repo / "file.txt").write_text("working\n")
    (repo / "new file.txt").write_text("untracked\n")
    index_before = (repo / ".git/index").read_bytes()
    status_before = git(repo, "status", "--porcelain=v1")
    staged = diff(client, repo, "staged")
    unstaged = diff(client, repo, "unstaged")
    uncommitted = diff(client, repo, "uncommitted")
    assert len(staged["files"]) == 1
    assert "-base\n+staged" in staged["files"][0]["diff"]
    assert "-staged\n+working" in unstaged["files"][0]["diff"]
    assert "-base\n+working" in uncommitted["files"][0]["diff"]
    assert unstaged["files"][1]["path"] == "new file.txt"
    assert uncommitted["added"] == 2 and uncommitted["removed"] == 1
    assert (repo / ".git/index").read_bytes() == index_before
    assert git(repo, "status", "--porcelain=v1") == status_before


def test_commit_root_and_branch_common_ancestor(client, repo):
    first = git(repo, "rev-parse", "HEAD")
    assert diff(client, repo, "commit", ref=first)["files"][0]["action"] == "create"
    git(repo, "checkout", "-b", "feature")
    (repo / "feature.txt").write_text("feature\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "feature change")
    feature = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "main")
    (repo / "main-only.txt").write_text("main\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "main change")
    git(repo, "checkout", "feature")
    (repo / "dirty.txt").write_text("not committed\n")
    branch = diff(client, repo, "branch", ref="main")
    assert branch["base"] == first and branch["target"] == feature
    assert [file["path"] for file in branch["files"]] == ["feature.txt"]
    commit = diff(client, repo, "commit", ref=feature)
    assert [file["path"] for file in commit["files"]] == ["feature.txt"]
    info = client.get("/workspace/git/info", params={"path": str(repo)}).json()
    assert info["available"] and info["branch"] == "feature" and info["default_base"] == "main"
    assert info["commits"][0] == {"id": feature, "subject": "feature change"}


def test_no_repo_unborn_and_invalid_reference(client, tmp_path):
    assert not client.get("/workspace/git/info", params={"path": str(tmp_path)}).json()["available"]
    git(tmp_path, "init", "-b", "main")
    info = client.get("/workspace/git/info", params={"path": str(tmp_path)}).json()
    assert info["available"] and info["head"] is None and info["commits"] == []
    (tmp_path / "first.txt").write_text("staged\n")
    git(tmp_path, "add", ".")
    (tmp_path / "first.txt").write_text("working\n")
    assert "+staged" in diff(client, tmp_path, "staged")["files"][0]["diff"]
    assert "+working" in diff(client, tmp_path, "uncommitted")["files"][0]["diff"]
    response = client.get("/workspace/git/diff", params={"path": str(tmp_path), "scope": "commit", "ref": "--output=bad"})
    assert response.status_code == 400
    assert not (tmp_path / "bad").exists()


def test_paths_binary_deleted_and_no_external_diff(client, repo):
    name = "空 格\t换行\n*.txt"
    (repo / name).write_text("base\n")
    (repo / "binary.bin").write_bytes(b"\0before")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "special files")
    (repo / name).write_text("changed\n")
    (repo / "binary.bin").write_bytes(b"\0after")
    (repo / "file.txt").unlink()
    marker = repo / "external-ran"
    git(repo, "config", "diff.external", f"touch {marker}")
    result = diff(client, repo, "uncommitted")
    files = {file["path"]: file for file in result["files"]}
    assert "+changed" in files[name]["diff"]
    assert files["file.txt"]["action"] == "delete"
    assert "二进制" in files["binary.bin"]["note"]
    assert not marker.exists()


def test_untracked_symlink_does_not_read_target(client, repo, tmp_path):
    target = tmp_path / "outside.txt"
    target.write_text("PRIVATE CONTENT\n")
    os.symlink(target, repo / "link.txt")
    result = diff(client, repo, "uncommitted")
    assert "PRIVATE CONTENT" not in str(result)
    assert str(target) in result["files"][0]["diff"]


def test_bounds_and_scope_validation(client, repo, tmp_path, monkeypatch):
    response = client.get("/workspace/git/info", params={"path": str(tmp_path.parent)})
    assert response.status_code == 403
    response = client.get("/workspace/git/diff", params={"path": str(repo), "scope": "invalid"})
    assert response.status_code == 422
    (repo / "one.txt").write_text("one\n")
    (repo / "two.txt").write_text("two\n")
    monkeypatch.setattr(git_review, "MAX_FILES", 1)
    result = diff(client, repo, "unstaged")
    assert result["truncated"] and result["total_files"] == 2 and len(result["files"]) == 1


def test_untracked_patches_share_response_budget(client, repo, monkeypatch):
    for name in ("one.txt", "two.txt", "three.txt"):
        (repo / name).write_text("x" * 600 + "\n")
    monkeypatch.setattr(git_review, "MAX_OUTPUT_BYTES", 1000)
    result = diff(client, repo, "uncommitted")
    assert sum(len((file["diff"] or "").encode()) for file in result["files"]) <= 1000
    assert result["added"] == 3
    assert sum(file["diff"] is None for file in result["files"]) == 2
