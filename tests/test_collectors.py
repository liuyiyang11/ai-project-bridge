import json

from bridge.collectors.artifact_collector import collect_artifacts
from bridge.collectors.git_collector import collect_git_state
from bridge.config import BundleLimits


def test_git_collector_reports_changed_files_and_writes_diff(tmp_path):
    import subprocess

    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "tracked.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "tracked.txt").write_text("two\n", encoding="utf-8")
    (tmp_path / "new.txt").write_text("new\n", encoding="utf-8")

    result = collect_git_state(tmp_path, tmp_path / "bundle")

    assert set(result["changed_files"]) == {"tracked.txt", "new.txt"}
    assert (tmp_path / "bundle" / "diff.patch").is_file()
    assert (tmp_path / "bundle" / "diff-stat.txt").is_file()


def test_artifact_collector_limits_size_and_rejects_checkpoints(tmp_path):
    artifact_dir = tmp_path / "outputs"
    artifact_dir.mkdir()
    (artifact_dir / "metrics.json").write_text(json.dumps({"a": 1}), encoding="utf-8")
    (artifact_dir / "notes.txt").write_text("ok", encoding="utf-8")
    (artifact_dir / "model.pt").write_bytes(b"checkpoint")
    (artifact_dir / "too-large.log").write_bytes(b"0123456789")
    dest = tmp_path / "bundle"

    records = collect_artifacts(
        tmp_path,
        ["outputs"],
        dest,
        BundleLimits(max_artifact_file_mb=1, max_bundle_mb=1, max_images=2),
        file_size_bytes=9,
    )

    names = {record["source"] for record in records}
    assert "outputs/metrics.json" in names
    assert "outputs/notes.txt" in names
    assert all(not name.endswith(".pt") for name in names)
    assert "outputs/too-large.log" not in names


def test_artifact_collector_caps_images(tmp_path):
    image_dir = tmp_path / "results"
    image_dir.mkdir()
    for index in range(3):
        (image_dir / f"image-{index}.png").write_bytes(b"png")

    records = collect_artifacts(
        tmp_path,
        ["results"],
        tmp_path / "bundle",
        BundleLimits(max_artifact_file_mb=1, max_bundle_mb=1, max_images=2),
    )
    assert len([item for item in records if item["kind"] == "image"]) == 2
