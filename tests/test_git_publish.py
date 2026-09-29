"""Tests for artifact git publishing."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from nexus.git_publish import ArtifactGitError, publish_artifact_git

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not on PATH"
)


def _git(*args: str, cwd: Path) -> str:
    env = dict(os.environ)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=60,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _make_repo(path: Path, remote: Path | None = None) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-b", "main", cwd=path)
    _git("config", "user.email", "test@example.com", cwd=path)
    _git("config", "user.name", "Test", cwd=path)
    if remote is not None:
        _git("remote", "add", "origin", str(remote), cwd=path)
    return path


@pytest.fixture
def bare_remote(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git("init", "--bare", "-b", "main", cwd=remote)
    return remote


def _remote_files(remote: Path) -> list[str]:
    out = _git("--git-dir", str(remote), "ls-tree", "-r", "--name-only", "main", cwd=remote.parent)
    return out.splitlines() if out else []


class TestPublishArtifactGit:
    def test_first_publish_commits_and_pushes(self, tmp_path, bare_remote):
        repo = _make_repo(tmp_path / "work", bare_remote)
        artifact = repo / "dashboard.html"
        artifact.write_text("<html>v1</html>", encoding="utf-8")

        result = publish_artifact_git(artifact)

        assert result["committed"] is True
        assert result["pushed"] is True
        assert result["commit"]
        assert result["branch"] == "main"
        assert "dashboard.html" in _remote_files(bare_remote)
        log = _git("log", "-1", "--format=%s", cwd=repo)
        assert log.startswith("Update dashboard.html (")

    def test_no_change_is_noop(self, tmp_path, bare_remote):
        repo = _make_repo(tmp_path / "work", bare_remote)
        artifact = repo / "dashboard.html"
        artifact.write_text("<html>v1</html>", encoding="utf-8")
        publish_artifact_git(artifact)

        result = publish_artifact_git(artifact)

        assert result["committed"] is False
        assert result["pushed"] is False
        assert result["commit"] is None
        assert result["reason"] == "no changes"

    def test_changed_content_republishes(self, tmp_path, bare_remote):
        repo = _make_repo(tmp_path / "work", bare_remote)
        artifact = repo / "dashboard.html"
        artifact.write_text("<html>v1</html>", encoding="utf-8")
        first = publish_artifact_git(artifact)
        artifact.write_text("<html>v2</html>", encoding="utf-8")

        second = publish_artifact_git(artifact)

        assert second["committed"] is True
        assert second["pushed"] is True
        assert second["commit"] != first["commit"]

    def test_only_artifact_is_committed(self, tmp_path, bare_remote):
        repo = _make_repo(tmp_path / "work", bare_remote)
        artifact = repo / "dashboard.html"
        artifact.write_text("<html>v1</html>", encoding="utf-8")
        (repo / "notes.txt").write_text("do not commit", encoding="utf-8")

        publish_artifact_git(artifact)

        assert "notes.txt" not in _remote_files(bare_remote)
        assert (repo / "notes.txt").exists()

    def test_outside_repo_raises(self, tmp_path):
        artifact = tmp_path / "lonely.html"
        artifact.write_text("<html/>", encoding="utf-8")
        with pytest.raises(ArtifactGitError):
            publish_artifact_git(artifact)

    def test_broken_remote_raises(self, tmp_path):
        repo = _make_repo(tmp_path / "work")
        artifact = repo / "dashboard.html"
        artifact.write_text("<html/>", encoding="utf-8")
        _git("remote", "add", "origin", "https://invalid.example.com/nope.git", cwd=repo)
        with pytest.raises(ArtifactGitError):
            publish_artifact_git(artifact, branch="main")

    def test_explicit_branch(self, tmp_path, bare_remote):
        repo = _make_repo(tmp_path / "work", bare_remote)
        artifact = repo / "dashboard.html"
        artifact.write_text("<html/>", encoding="utf-8")

        result = publish_artifact_git(artifact, branch="main")

        assert result["pushed"] is True
        assert result["branch"] == "main"

    def test_concurrent_writers_converge(self, tmp_path, bare_remote):
        """Two clones publishing different files: both land on the remote."""
        repo_a = _make_repo(tmp_path / "a", bare_remote)
        repo_b = _make_repo(tmp_path / "b", bare_remote)
        file_a = repo_a / "dash-a.html"
        file_b = repo_b / "dash-b.html"
        file_a.write_text("<html>a</html>", encoding="utf-8")
        file_b.write_text("<html>b</html>", encoding="utf-8")

        result_a = publish_artifact_git(file_a)
        result_b = publish_artifact_git(file_b)  # exercises fetch/rebase/push retry

        assert result_a["pushed"] is True
        assert result_b["pushed"] is True
        remote_files = _remote_files(bare_remote)
        assert "dash-a.html" in remote_files
        assert "dash-b.html" in remote_files
