from __future__ import annotations

import subprocess
from pathlib import Path


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", shell=False, check=False)
    if result.returncode != 0:
        return ""
    return result.stdout


def collect_git_state(cwd: Path, bundle_dir: Path) -> dict:
    bundle_dir.mkdir(parents=True, exist_ok=True)
    status = _git(cwd, "status", "--porcelain=v1", "--untracked-files=all")
    changed_files = []
    for line in status.splitlines():
        if len(line) >= 4:
            name = line[3:]
            if " -> " in name:
                name = name.split(" -> ", 1)[1]
            changed_files.append(name)
    diff = _git(cwd, "diff", "--no-ext-diff")
    diff_stat = _git(cwd, "diff", "--stat")
    (bundle_dir / "diff.patch").write_text(diff, encoding="utf-8")
    (bundle_dir / "diff-stat.txt").write_text(diff_stat, encoding="utf-8")
    return {
        "status": status,
        "changed_files": changed_files,
        "diff_stat": diff_stat.strip(),
        "diff_patch": "diff.patch",
    }
