"""Git publishing for generated artifacts (best-effort, never interactive)."""
from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

_LOCAL_TIMEOUT = 60
_PUSH_TIMEOUT = 300


class ArtifactGitError(RuntimeError):
    """Raised when any git step of artifact publishing fails."""


def _git_env() -> dict[str, str]:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_EDITOR"] = "true"
    return env


def _run_git(args: list[str], cwd: Path, timeout: int) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=timeout,
            env=_git_env(),
        )
    except FileNotFoundError as exc:
        raise ArtifactGitError(f"git not found on PATH: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ArtifactGitError(f"git {' '.join(args)} timed out after {timeout}s") from exc
    if result.returncode != 0:
        raise ArtifactGitError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def publish_artifact_git(output: Path, branch: str = "") -> dict:
    """Commit and push a single artifact file. Returns an outcome dict.

    Steps: verify the file is inside a git work tree, stage only that file,
    commit only if changed, then push (with a single fetch/rebase/push retry
    on non-fast-forward rejection so concurrent writers converge).

    Raises ArtifactGitError on any git failure. Callers treat publish as
    best-effort: warn to stderr and continue with a success exit code.
    """
    output = output.expanduser().resolve()
    try:
        top = _run_git(["rev-parse", "--show-toplevel"], cwd=output.parent, timeout=_LOCAL_TIMEOUT)
    except ArtifactGitError as exc:
        raise ArtifactGitError(f"not inside a git work tree: {exc}") from exc
    root = Path(top).resolve()
    try:
        rel = output.relative_to(root)
    except ValueError as exc:
        raise ArtifactGitError(f"{output} is outside the work tree at {root}") from exc

    _run_git(["add", "--", str(rel)], cwd=root, timeout=_LOCAL_TIMEOUT)
    status = _run_git(["status", "--porcelain", "--", str(rel)], cwd=root, timeout=_LOCAL_TIMEOUT)
    if not status:
        current = _current_branch(root)
        return {
            "committed": False,
            "pushed": False,
            "commit": None,
            "branch": current,
            "output": str(output),
            "reason": "no changes",
        }

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    _run_git(
        ["commit", "-m", f"Update {rel.name} ({stamp})", "--", str(rel)],
        cwd=root,
        timeout=_LOCAL_TIMEOUT,
    )
    commit = _run_git(["rev-parse", "HEAD"], cwd=root, timeout=_LOCAL_TIMEOUT)

    _push_with_retry(root, branch)
    return {
        "committed": True,
        "pushed": True,
        "commit": commit,
        "branch": branch or _current_branch(root),
        "output": str(output),
    }


def _current_branch(root: Path) -> str:
    return _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=root, timeout=_LOCAL_TIMEOUT)


def _push_with_retry(root: Path, branch: str) -> None:
    """Push once; on non-fast-forward rejection, fetch + rebase + push once more."""
    try:
        _push(root, branch)
    except ArtifactGitError as exc:
        if "non-fast-forward" not in str(exc).lower() and "fetch first" not in str(exc).lower():
            raise
        target = branch or _current_branch(root)
        _run_git(["fetch", "origin"], cwd=root, timeout=_LOCAL_TIMEOUT)
        try:
            _run_git(["rebase", f"origin/{target}"], cwd=root, timeout=_LOCAL_TIMEOUT)
        except ArtifactGitError:
            try:
                _run_git(["rebase", "--abort"], cwd=root, timeout=_LOCAL_TIMEOUT)
            except ArtifactGitError:
                pass
            raise ArtifactGitError(
                f"rebase conflict while publishing; aborted, worktree left clean: {exc}"
            ) from exc
        _push(root, branch)


def _push(root: Path, branch: str) -> None:
    if branch:
        _run_git(["push", "origin", branch], cwd=root, timeout=_PUSH_TIMEOUT)
        return
    try:
        _run_git(["push"], cwd=root, timeout=_PUSH_TIMEOUT)
    except ArtifactGitError as exc:
        if "no upstream" in str(exc).lower() or "has no upstream branch" in str(exc).lower():
            current = _current_branch(root)
            _run_git(["push", "-u", "origin", current], cwd=root, timeout=_PUSH_TIMEOUT)
            return
        raise
