from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..collectors.presentation_renderer import PresentationRenderer
from ..security import resolve_under
from .code import CodeExecutor, _value
from . import ExecutionContext


class PresentationExecutor(CodeExecutor):
    def __init__(self, runner=None, manager_factory=None, renderer: Optional[PresentationRenderer] = None, *, publish: bool = True):
        super().__init__(runner, manager_factory, publish=publish)
        self.renderer = renderer or PresentationRenderer()

    def execute(self, context: ExecutionContext, rework_instruction: Optional[str] = None) -> dict:
        prompt = self._presentation_prompt(context, rework_instruction)
        result = self._execute_codex(context, prompt, rework_instruction)
        state = context.store.load_state(context.issue.number)
        worktree = Path(state["worktree_path"])
        pptx_files = sorted(worktree.rglob("*.pptx"), key=lambda path: path.stat().st_mtime, reverse=True)
        if not pptx_files:
            raise RuntimeError("presentation task completed without a .pptx file")
        render_dir = context.task_dir / "review_bundle" / "presentation"
        render_result = self.renderer.render(pptx_files[0], render_dir)
        result["presentation"] = render_result
        result["artifact_list"] = [{"path": f"presentation/{key}", "kind": "presentation"} for key in ("final_pptx", "final_pdf") if render_result.get(key)]
        return result

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

