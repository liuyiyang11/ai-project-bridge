# AI Project Bridge V0.1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a safe, testable Windows 11 local bridge that dispatches validated GitHub Issue tasks to isolated worktrees, configured deterministic commands, and an injectable Codex runner.

**Architecture:** A pure validation/config/state core sits below a `gh` subprocess adapter and a dispatcher. Code and presentation tasks use generated Git worktrees and Codex JSONL sessions; experiment review tasks use only registered argv commands and bounded artifact collection. Fakes make the full lifecycle testable without network or Codex usage.

**Tech Stack:** Python 3.11+, PyYAML, Pydantic, pytest, Git CLI, GitHub CLI (`gh`), Codex CLI, optional PowerPoint COM and LibreOffice.

---

### Task 1: Repository skeleton, metadata, and security-first tests

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `AGENTS.md`, `bridge/__init__.py`, `bridge/__main__.py`
- Test: `tests/test_task_parser.py`, `tests/test_security.py`, `tests/test_config.py`

- [ ] Write tests first for the marker/YAML parser, unknown task type, unregistered project, traversal, absolute path, arbitrary command, and strict config loading.
- [ ] Run `python -m pytest tests/test_task_parser.py tests/test_security.py tests/test_config.py -q`; confirm failures are missing-module failures.
- [ ] Implement the smallest models and validators using Pydantic and PyYAML.
- [ ] Rerun the targeted tests and confirm they pass.

### Task 2: Persistent task store and event/result files

**Files:**
- Create: `bridge/task_store.py`
- Test: `tests/test_task_store.py`

- [ ] Write failing tests for initial state, atomic JSON persistence, JSONL event append, and duplicate/rework idempotency.
- [ ] Run the targeted tests and observe the expected missing implementation failures.
- [ ] Implement `TaskStore` with `.bridge/tasks/<issue>` files and explicit processed comment ids.
- [ ] Run the targeted tests and confirm they pass.

### Task 3: GitHub and subprocess boundary adapters

**Files:**
- Create: `bridge/github.py`, `bridge/commands.py`
- Test: `tests/test_github.py`, `tests/test_commands.py`

- [ ] Write tests proving GitHub commands use argv without shell, status transitions are label-only, and arbitrary commands are rejected.
- [ ] Implement typed GitHub issue/comment/PR adapters around `gh` and config command execution around registered argv.
- [ ] Verify targeted tests.

### Task 4: Git worktree and Codex runner

**Files:**
- Create: `bridge/worktree.py`, `bridge/codex/__init__.py`, `bridge/codex/runner.py`
- Test: `tests/test_worktree.py`, `tests/test_codex_runner.py`

- [ ] Write tests for generated branch names, main/master push protection, worktree create/reuse/cleanup, JSONL capture, final message, and missing Codex binary.
- [ ] Implement Git operations with `shell=False`, generated `ai/issue-N` branches, and the observed Codex CLI arguments.
- [ ] Verify targeted tests with a temporary demo git repository.

### Task 5: Collectors and renderer capability reporting

**Files:**
- Create: `bridge/collectors/__init__.py`, `bridge/collectors/git_collector.py`, `bridge/collectors/artifact_collector.py`, `bridge/collectors/presentation_renderer.py`
- Test: `tests/test_collectors.py`, `tests/test_renderer.py`

- [ ] Write failing tests for changed-file collection, checkpoint rejection, per-file/total/image limits, and renderer capability detection.
- [ ] Implement bounded collectors and optional PowerPoint COM/LibreOffice reporting.
- [ ] Verify targeted tests.

### Task 6: Executors and dispatcher lifecycle

**Files:**
- Create: `bridge/executors/__init__.py`, `bridge/executors/code.py`, `bridge/executors/presentation.py`, `bridge/executors/experiment_review.py`, `bridge/dispatcher.py`
- Test: `tests/test_dispatcher.py`, `tests/test_executors.py`

- [ ] Write fake-GitHub/fake-runner tests for ready->running->review, failed tasks, duplicate Issue scans, rework comment idempotency, and deterministic experiment review.
- [ ] Implement the executor interface and dispatcher transitions, PR body generation, and review bundle manifests.
- [ ] Verify full test coverage for the fake end-to-end lifecycle.

### Task 7: CLI, examples, schema, scripts, and documentation

**Files:**
- Create: `bridge/cli.py`, `bridge/config.example.yaml`, `config.local.yaml.example`, `schemas/task.schema.json`, `examples/*.md`, `scripts/*.ps1`, `README.md`
- Test: `tests/test_cli.py`

- [ ] Write tests for doctor output with missing gh, show-task, retry, and setup-github label planning.
- [ ] Implement `doctor`, `run`, `run-once`, `show-task`, `retry`, and `setup-github` commands.
- [ ] Add Windows PowerShell helpers and end-user documentation.
- [ ] Run the CLI tests.

### Task 8: Fresh verification and demo repository

**Files:**
- Modify: `README.md` only if verification discovers inaccurate commands.

- [ ] Run `python -m bridge doctor` and record the actual missing `gh` result.
- [ ] Run the complete `pytest` suite.
- [ ] Create a temporary demo repository, execute the fake runner lifecycle, verify result files, and clean up only the temporary demo directory.
- [ ] Review `git status`, the final file tree, and all requirement gaps before reporting results.

