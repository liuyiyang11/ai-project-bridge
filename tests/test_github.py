import json
import subprocess

from bridge.github import GhClient


def test_gh_client_lists_only_ai_task_issues_and_parses_labels():
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        payload = [{"number": 4, "title": "Task", "body": "body", "url": "https://github/4", "labels": [{"name": "ai-task"}, {"name": "status:ready"}]}]
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    client = GhClient("C:/Program Files/GitHub CLI/gh.exe", "owner/bridge", run=fake_run)
    issues = client.list_candidate_issues()

    assert issues[0].number == 4
    assert issues[0].labels == {"ai-task", "status:ready"}
    assert calls[0][0][:4] == ["C:/Program Files/GitHub CLI/gh.exe", "issue", "list", "--repo"]
    assert "ai-task" in calls[0][0]
    assert calls[0][1]["shell"] is False


def test_gh_client_status_transition_does_not_execute_issue_text():
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    client = GhClient("gh", "owner/bridge", run=fake_run)
    client.set_status(4, "running")

    command = calls[0]
    assert "status:running" in command
    assert "status:ready" in command
    assert "powershell" not in " ".join(command).lower()


def test_gh_client_creates_draft_pr_with_explicit_branch():
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "https://github.com/owner/demo/pull/9\n", "")

    client = GhClient("gh", "owner/demo", run=fake_run)
    url = client.create_draft_pr("ai/issue-4", "main", "Title", "Body")

    assert url.endswith("/pull/9")
    assert "--draft" in calls[0][0]
    assert "ai/issue-4" in calls[0][0]
    assert calls[0][1]["input"] == "Body"


def test_gh_client_reads_issue_and_comment_author_logins():
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        payload = {
            "number": 4,
            "title": "Task",
            "body": "body",
            "url": "https://github/4",
            "labels": [],
            "author": {"login": "trusted-user"},
            "comments": [{"id": "comment-1", "body": "review", "author": {"login": "reviewer"}}],
        }
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    issue = GhClient("gh", "owner/bridge", run=fake_run).view_issue(4)

    assert issue.author_login == "trusted-user"
    assert issue.comments[0].author_login == "reviewer"
    assert "author" in calls[0][-1]

