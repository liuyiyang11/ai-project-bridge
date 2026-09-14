from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Optional

from ..collectors.presentation_renderer import PresentationRenderer
from ..security import resolve_under
from .code import CodeExecutor, _value
from . import ExecutionContext


class PresentationExecutor(CodeExecutor):
    def __init__(self, runner=None, manager_factory=None, renderer: Optional[PresentationRenderer] = None, *, publish: bool = True, session_manager: Any = None):
        super().__init__(runner, manager_factory, publish=publish, session_manager=session_manager)
        self.renderer = renderer or PresentationRenderer()

    def execute(self, context: ExecutionContext, rework_instruction: Optional[str] = None) -> dict:
        prompt = self._presentation_prompt(context, rework_instruction)
        return self._execute_codex(context, prompt, rework_instruction)

    def _prepare_review_artifacts(self, context: ExecutionContext, worktree: Path, bundle_dir: Path, codex_result: Any) -> dict:
        pptx_files = sorted(worktree.rglob("*.pptx"), key=lambda path: path.stat().st_mtime, reverse=True)
        if not pptx_files:
            raise RuntimeError("presentation task completed without a .pptx file")
        render_dir = bundle_dir / "presentation"
        if render_dir.exists():
            shutil.rmtree(render_dir)
        render_dir.mkdir(parents=True, exist_ok=True)
        if isinstance(self.renderer, PresentationRenderer):
            render_result = self.renderer.render(pptx_files[0], render_dir, limits=context.config.limits)
        else:
            # Keep the small renderer seam used by fake runners and tests.
            render_result = self.renderer.render(pptx_files[0], render_dir)

        published = self._publish_rendered_files(context, worktree, render_dir, render_result)
        return {
            "presentation": render_result,
            "artifact_list": [{"path": f"presentation/{name}", "kind": "presentation"} for name in published],
        }

    @staticmethod
    def _rendered_names(render_result: dict) -> list[str]:
        names: list[str] = []
        for key in ("final_pdf", "contact_sheet", "final_pptx"):
            value = render_result.get(key)
            if isinstance(value, str) and value:
                names.append(value)
        slides = render_result.get("slides_png")
        if isinstance(slides, list):
            names.extend(value for value in slides if isinstance(value, str) and value)
        return list(dict.fromkeys(names))

    def _publish_rendered_files(self, context: ExecutionContext, worktree: Path, render_dir: Path, render_result: dict) -> list[str]:
        target = worktree / "review_bundle" / "presentation"
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)
        max_file = context.config.limits.max_artifact_file_mb * 1024 * 1024
        max_bundle = context.config.limits.max_bundle_mb * 1024 * 1024
        total = 0
        images = 0
        published: list[str] = []
        for name in self._rendered_names(render_result):
            source = resolve_under(render_dir, name)
            if not source.is_file():
                continue
            size = source.stat().st_size
            is_image = source.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
            if size > max_file or total + size > max_bundle:
                continue
            if is_image and images >= context.config.limits.max_images:
                continue
            destination = target / Path(name)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            total += size
            images += int(is_image)
            published.append(Path(name).as_posix())
        return published

    def _presentation_prompt(self, context: ExecutionContext, rework_instruction: Optional[str]) -> str:
        task = context.task
        root = context.project.root
        files = []
        for name in (task.brief, task.slides_spec):
            if name:
                path = resolve_under(root, name)
                if not path.is_file():
                    raise RuntimeError(f"presentation input does not exist: {name}")
                files.append(f"Read project-relative file {name}.")
        if task.assets_dir:
            asset_path = resolve_under(root, task.assets_dir)
            if not asset_path.is_dir():
                raise RuntimeError(f"presentation assets directory does not exist: {task.assets_dir}")
            files.append(f"Use existing assets under {task.assets_dir}; do not download unknown binaries.")
        if task.template:
            template = resolve_under(root, task.template)
            if not template.is_file():
                raise RuntimeError(f"presentation template does not exist: {task.template}")
            files.append(f"Use template {task.template}.")
        base = super()._prompt(context, rework_instruction)
        return base + "\n\nPresentation requirements:\n- Keep 16:9.\n- " + "\n- ".join(files)

