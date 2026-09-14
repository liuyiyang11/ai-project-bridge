from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional, Set

from ..config import BundleLimits
from ..security import resolve_under


ALLOWED_EXTENSIONS = {".json", ".csv", ".txt", ".log", ".png", ".jpg", ".jpeg", ".webp"}
BLOCKED_EXTENSIONS = {".pth", ".pt", ".ckpt", ".bin", ".safetensors"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


def collect_artifacts(
    project_root: Path,
    artifact_dirs: list[str],
    destination: Path,
    limits: BundleLimits,
    *,
    file_size_bytes: Optional[int] = None,
    extra_extensions: Optional[Set[str]] = None,
) -> list[dict]:
    project_root = Path(project_root).resolve()
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    allowed = ALLOWED_EXTENSIONS | {item.lower() if item.startswith(".") else f".{item.lower()}" for item in (extra_extensions or set())}
    max_file = file_size_bytes if file_size_bytes is not None else limits.max_artifact_file_mb * 1024 * 1024
    max_bundle = limits.max_bundle_mb * 1024 * 1024
    total = 0
    images = 0
    records: list[dict] = []
    files: list[Path] = []
    for relative_dir in artifact_dirs:
        root = resolve_under(project_root, relative_dir)
        if root.is_dir():
            files.extend(path for path in root.rglob("*") if path.is_file())
    files.sort(key=lambda path: (path.stat().st_mtime, str(path)), reverse=True)
    for source in files:
        suffix = source.suffix.lower()
        relative = source.relative_to(project_root)
        relative_text = relative.as_posix()
        lowered_parts = {part.lower() for part in relative.parts}
        if suffix in BLOCKED_EXTENSIONS or "dataset" in lowered_parts or "datasets" in lowered_parts or "checkpoint" in lowered_parts:
            continue
        if suffix not in allowed:
            continue
        size = source.stat().st_size
        if size > max_file or total + size > max_bundle:
            continue
        kind = "image" if suffix in IMAGE_EXTENSIONS else "file"
        if kind == "image" and images >= limits.max_images:
            continue
        target = destination / "artifacts" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        total += size
        images += kind == "image"
        records.append({"source": relative_text, "path": target.relative_to(destination).as_posix(), "bytes": size, "kind": kind})
    return records

