from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from bridge.store import task_store as task_store_module
from bridge.store.task_store import TaskStore


def _create(store: TaskStore, task_id: str = "task-atomic"):
    return store.create_task(
        task_id,
        project="demo",
        task_type="code",
        instruction="create a complete snapshot",
        acceptance=["all seven files exist"],
        model="gpt-test",
        reasoning_effort="low",
        metadata={"nested": {"kind": "smoke"}},
    )


def _assert_complete_or_absent(store: TaskStore, task_id: str):
    directory = store.task_dir(task_id)
    if not directory.exists():
        return
    assert directory.is_dir()
    assert {item.name for item in directory.iterdir()} == set(TaskStore._FILES)
    assert all((directory / name).is_file() for name in TaskStore._FILES)


@pytest.mark.parametrize("fault", ["pre-serialization", "staging-mkdir", "task.yaml", "state.json", "task.json", "events.jsonl", "result.json", "fsync", "rename"])
def test_atomic_create_faults_leave_complete_snapshot_or_no_final_directory(tmp_path, monkeypatch, fault):
    store = TaskStore(tmp_path / "中文 path with spaces" / ".bridge")
    task_id = f"task-fault-{fault.replace('.', '-') }"

    if fault == "pre-serialization":
        def fail(*args, **kwargs):
            raise UnicodeEncodeError("utf-8", "surrogate\ud800", 9, 10, "fault injection")

        monkeypatch.setattr(store, "_json_bytes", fail)
    elif fault == "staging-mkdir":
        def fail(*args, **kwargs):
            raise OSError("staging mkdir fault")

        monkeypatch.setattr(task_store_module.tempfile, "mkdtemp", fail)
    elif fault in {"task.yaml", "state.json", "task.json", "events.jsonl", "result.json"}:
        original = store._write_staged_file

        def fail(path, data):
            if path.name == fault:
                raise OSError(f"{fault} write fault")
            return original(path, data)

        monkeypatch.setattr(store, "_write_staged_file", fail)
    elif fault == "fsync":
        monkeypatch.setattr(task_store_module.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("fsync fault")))
    elif fault == "rename":
        monkeypatch.setattr(task_store_module.os, "rename", lambda source, destination: (_ for _ in ()).throw(OSError("rename fault")))

    with pytest.raises(Exception):
        _create(store, task_id)

    _assert_complete_or_absent(store, task_id)
    assert not store.task_dir(task_id).exists()
    tasks_root = store.root / "tasks"
    if tasks_root.exists():
        assert not any(item.name == f".task-{task_id}.claim" for item in tasks_root.iterdir())


def test_atomic_create_serializes_all_durable_bytes_before_filesystem_mutation(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / ".bridge")
    original_mkdtemp = task_store_module.tempfile.mkdtemp
    observed = []

    def observe(*args, **kwargs):
        observed.append((store.root / "tasks").exists())
        return original_mkdtemp(*args, **kwargs)

    monkeypatch.setattr(task_store_module.tempfile, "mkdtemp", observe)
    _create(store)

    assert observed == [True]
    assert store.get_task("task-atomic")["state"] == "QUEUED"


def test_initialize_creates_complete_legacy_snapshot_and_preserves_raw_yaml(tmp_path):
    store = TaskStore(tmp_path / ".bridge")
    task_yaml = "version: 1\ntask_type: code\nproject: demo\n"

    store.initialize("legacy-complete", task_yaml, {"status": "ready", "project": "demo"})

    directory = store.task_dir("legacy-complete")
    assert {item.name for item in directory.iterdir()} == set(TaskStore._FILES)
    assert task_yaml.encode("utf-8") == (directory / "task.yaml").read_bytes()
    assert store.get_task("legacy-complete")["status"] == "ready"


def test_initialize_rejects_lone_surrogate_in_task_yaml_before_filesystem_mutation(tmp_path):
    store = TaskStore(tmp_path / ".bridge")

    with pytest.raises(ValueError, match="invalid Unicode"):
        store.initialize("legacy-yaml-surrogate", "bad\ud800", {"status": "ready"})

    assert not store.task_dir("legacy-yaml-surrogate").exists()
    assert not (store.root / "tasks").exists()


def test_initialize_rejects_lone_surrogate_in_state_before_filesystem_mutation(tmp_path):
    store = TaskStore(tmp_path / ".bridge")

    with pytest.raises(ValueError, match="invalid Unicode"):
        store.initialize("legacy-state-surrogate", "task", {"status": "ready", "metadata": "bad\ud800"})

    assert not store.task_dir("legacy-state-surrogate").exists()
    assert not (store.root / "tasks").exists()


def test_initialize_write_fault_leaves_no_final_directory_or_transaction_debris(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / ".bridge")
    original = store._write_staged_file

    def fail(path, data):
        if path.name == "state.json":
            raise OSError("legacy state write fault")
        return original(path, data)

    monkeypatch.setattr(store, "_write_staged_file", fail)

    with pytest.raises(OSError, match="legacy state write fault"):
        store.initialize("legacy-write-fault", "task", {"status": "ready"})

    assert not store.task_dir("legacy-write-fault").exists()
    tasks_root = store.root / "tasks"
    if tasks_root.exists():
        assert not any(item.name.startswith(".task-legacy-write-fault-") for item in tasks_root.iterdir())
        assert not (tasks_root / ".task-legacy-write-fault.claim").exists()


def test_initialize_rename_fault_leaves_no_final_directory_or_transaction_debris(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / ".bridge")
    monkeypatch.setattr(
        task_store_module.os,
        "rename",
        lambda source, destination: (_ for _ in ()).throw(OSError("legacy rename fault")),
    )

    with pytest.raises(OSError, match="legacy rename fault"):
        store.initialize("legacy-rename-fault", "task", {"status": "ready"})

    assert not store.task_dir("legacy-rename-fault").exists()
    tasks_root = store.root / "tasks"
    if tasks_root.exists():
        assert not any(item.name.startswith(".task-legacy-rename-fault-") for item in tasks_root.iterdir())
        assert not (tasks_root / ".task-legacy-rename-fault.claim").exists()


def test_initialize_fails_closed_for_existing_complete_and_ghost_directories(tmp_path):
    store = TaskStore(tmp_path / ".bridge")
    store.initialize("legacy-existing", "original", {"status": "ready"})
    original_yaml = store.task_path("legacy-existing", "task.yaml").read_bytes()
    original_state = store.task_path("legacy-existing", "state.json").read_bytes()

    with pytest.raises(ValueError, match="task already exists"):
        store.initialize("legacy-existing", "replacement", {"status": "failed"})

    assert store.task_path("legacy-existing", "task.yaml").read_bytes() == original_yaml
    assert store.task_path("legacy-existing", "state.json").read_bytes() == original_state

    ghost = store.task_dir("legacy-ghost")
    ghost.mkdir(parents=True)
    (ghost / "task.yaml").write_bytes(b"ghost")

    with pytest.raises(ValueError, match="task already exists"):
        store.initialize("legacy-ghost", "replacement", {"status": "ready"})

    assert (ghost / "task.yaml").read_bytes() == b"ghost"
    assert not (ghost / "state.json").exists()
    assert not (ghost / "task.json").exists()


def test_claim_owner_cleans_only_same_task_staging(tmp_path):
    store = TaskStore(tmp_path / ".bridge")
    tasks_root = store.root / "tasks"
    tasks_root.mkdir(parents=True)
    owned_stale_stage = tasks_root / ".task-legacy-owned-old.tmp"
    owned_stale_stage.mkdir()
    other_stale_stage = tasks_root / ".task-other-old.tmp"
    other_stale_stage.mkdir()
    old = time.time() - (2 * 24 * 60 * 60)
    os.utime(owned_stale_stage, (old, old))
    os.utime(other_stale_stage, (old, old))

    store.initialize("legacy-owned", "task", {"status": "ready"})

    assert not owned_stale_stage.exists()
    assert other_stale_stage.exists()


def test_duplicate_complete_legacy_partial_and_claim_are_never_overwritten(tmp_path):
    store = TaskStore(tmp_path / ".bridge")
    _create(store, "task-complete")
    with pytest.raises(ValueError, match="task already exists"):
        _create(store, "task-complete")

    partial = store.task_dir("task-legacy")
    partial.mkdir(parents=True)
    (partial / "task.yaml").write_text("legacy", encoding="utf-8")
    with pytest.raises(ValueError, match="task already exists"):
        _create(store, "task-legacy")
    assert (partial / "task.yaml").read_text(encoding="utf-8") == "legacy"
    assert store.is_incomplete_task("task-legacy") is True

    claim = store.root / "tasks" / ".task-task-claimed.claim"
    claim.write_text(json.dumps({"pid": os.getpid(), "owner": "other"}), encoding="ascii")
    with pytest.raises(ValueError, match="already in progress"):
        _create(store, "task-claimed")
    assert claim.is_file()


def test_same_task_id_concurrent_creators_have_one_winner(tmp_path):
    root = tmp_path / ".bridge"
    stores = [TaskStore(root), TaskStore(root)]
    barrier = threading.Barrier(2)

    def attempt(store):
        barrier.wait(timeout=2)
        try:
            _create(store, "task-concurrent")
            return "winner"
        except ValueError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, stores))

    assert sorted(result == "winner" for result in results) == [False, True]
    _assert_complete_or_absent(stores[0], "task-concurrent")
    assert stores[0].get_task("task-concurrent")["state"] == "QUEUED"


def test_pid_is_alive_current_process_is_true():
    assert TaskStore._pid_is_alive(os.getpid()) is True


def test_live_owner_claim_and_staging_are_not_reclaimed(tmp_path):
    store = TaskStore(tmp_path / ".bridge")
    tasks_root = store.root / "tasks"
    tasks_root.mkdir(parents=True)
    live_stage = tasks_root / ".task-task-live-random.tmp"
    live_stage.mkdir()
    (live_stage / "partial").write_text("partial", encoding="utf-8")
    live_claim = tasks_root / ".task-task-live.claim"
    live_claim.write_text(json.dumps({"pid": os.getpid(), "owner": "live"}), encoding="ascii")
    old = time.time() - 3600
    os.utime(live_stage, (old, old))
    os.utime(live_claim, (old, old))

    removed = store.cleanup_stale_staging(max_age_seconds=0)

    assert removed == []
    assert live_stage.exists()
    assert live_claim.exists()


def test_staging_and_claim_cleanup_reclaims_proven_dead_owner(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / ".bridge")
    tasks_root = store.root / "tasks"
    tasks_root.mkdir(parents=True)
    stale_stage = tasks_root / ".task-task-stale-random.tmp"
    stale_stage.mkdir()
    (stale_stage / "orphan").write_text("orphan", encoding="utf-8")
    stale_claim = tasks_root / ".task-task-stale.claim"
    stale_claim.write_text(json.dumps({"pid": 12345678, "owner": "dead"}), encoding="ascii")
    active_claim = tasks_root / ".task-task-active.claim"
    active_claim.write_text(json.dumps({"pid": os.getpid(), "owner": "live"}), encoding="ascii")
    partial = tasks_root / "task-partial"
    partial.mkdir()
    (partial / "task.yaml").write_text("partial", encoding="utf-8")
    old = time.time() - 3600
    os.utime(stale_stage, (old, old))
    os.utime(stale_claim, (old, old))
    os.utime(active_claim, (old, old))
    monkeypatch.setattr(TaskStore, "_pid_is_alive", staticmethod(lambda pid: pid == os.getpid()))

    removed = store.cleanup_stale_staging(max_age_seconds=0)

    assert set(removed) == {stale_stage.name, stale_claim.name}
    assert not stale_stage.exists()
    assert not stale_claim.exists()
    assert active_claim.exists()
    assert partial.exists()
    assert store.list_task_ids() == ["task-partial"]


@pytest.mark.parametrize("conservative_result", [True, None], ids=["access-denied", "unknown"])
def test_unknown_or_access_denied_owner_is_not_reclaimed(tmp_path, monkeypatch, conservative_result):
    store = TaskStore(tmp_path / ".bridge")
    tasks_root = store.root / "tasks"
    tasks_root.mkdir(parents=True)
    stage = tasks_root / ".task-task-uncertain-random.tmp"
    stage.mkdir()
    claim = tasks_root / ".task-task-uncertain.claim"
    claim.write_text(json.dumps({"pid": 12345678, "owner": "uncertain"}), encoding="ascii")
    old = time.time() - 3600
    os.utime(stage, (old, old))
    os.utime(claim, (old, old))
    monkeypatch.setattr(TaskStore, "_pid_is_alive", staticmethod(lambda pid: conservative_result))

    removed = store.cleanup_stale_staging(max_age_seconds=0)

    assert removed == []
    assert stage.exists()
    assert claim.exists()


def test_staging_is_hidden_from_listing_and_recovery(tmp_path):
    from .test_supervisor_async import make_supervisor

    store = TaskStore(tmp_path / ".bridge")
    staging = store.root / "tasks" / ".task-task-hidden-random.tmp"
    staging.mkdir(parents=True)
    (staging / "task.json").write_text("{}", encoding="utf-8")

    supervisor, queue, _ = make_supervisor(tmp_path, store=store)

    assert store.list_task_ids() == []
    assert queue.submitted_task_ids == []
    assert staging.exists()
    supervisor.close()
