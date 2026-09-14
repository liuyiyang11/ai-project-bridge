import subprocess

import pytest

from bridge.worktree import WorktreeError, WorktreeManager


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    (repo / "README.md").write_text("demo\n", encoding="utf-8")
    git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.com", "add", ".")
    git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "init")
    return repo


def test_worktree_create_branch_and_cleanup(tmp_path):
    repo = make_repo(tmp_path)
    manager = WorktreeManager(repo, tmp_path / "worktrees")

    info = manager.prepare("demo", 12)
    assert info.branch == "ai/issue-12"
    assert info.path.is_dir()
    assert manager.default_branch() == "main"

    (info.path / "changed.txt").write_text("change\n", encoding="utf-8")
    assert "changed.txt" in manager.changed_files(info.path)
    manager.cleanup(info)
    assert not info.path.exists()


@pytest.mark.parametrize("branch", ["main", "master"])
def test_main_and_master_are_never_publishable(tmp_path, branch):
    repo = make_repo(tmp_path)
    manager = WorktreeManager(repo, tmp_path / "worktrees")
    with pytest.raises(WorktreeError, match="protected"):
        manager.assert_publishable_branch(branch)


def test_default_branch_never_falls_back_to_current_feature_branch(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "feature/audit")
    (repo / "README.md").write_text("demo\n", encoding="utf-8")
    git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.com", "add", ".")
    git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "init")

    with pytest.raises(WorktreeError, match="default branch"):
        WorktreeManager(repo, tmp_path / "worktrees").default_branch()


def test_push_uses_configured_remote(tmp_path):
    repo = make_repo(tmp_path)
    manager = WorktreeManager(repo, tmp_path / "worktrees", remote="upstream", base_branch="main")
    calls = []

    def fake_git(args, cwd=None, check=True):
        calls.append(args)
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    manager._git = fake_git
    manager.push(repo, "ai/issue-9")

    assert calls == [["push", "--set-upstream", "upstream", "ai/issue-9"]]

