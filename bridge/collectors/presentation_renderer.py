from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..github import find_executable


@dataclass(frozen=True)
class RenderCapabilities:
    powerpoint_com: bool
    libreoffice: bool
    summary: str


class PresentationRenderer:
    def detect_capabilities(self) -> RenderCapabilities:
        powerpoint = False
        if os.name == "nt":
            powerpoint_path = find_executable("POWERPNT") or find_executable("POWERPNT.EXE")
            try:
                import win32com.client  # type: ignore  # noqa: F401

                powerpoint = bool(powerpoint_path)
            except ImportError:
                powerpoint = False
        libreoffice = bool(find_executable("soffice") or find_executable("libreoffice"))
        available = []
        if powerpoint:
            available.append("PowerPoint COM")
        if libreoffice:
            available.append("LibreOffice")
        summary = "available: " + ", ".join(available) if available else "no PowerPoint COM or LibreOffice renderer detected"
        return RenderCapabilities(powerpoint, libreoffice, summary)

    def render(self, pptx_path: Path, output_dir: Path) -> dict:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        final_pptx = output_dir / "final.pptx"
        shutil.copy2(pptx_path, final_pptx)
        capabilities = self.detect_capabilities()
        pdf_path: Path | None = None
        errors: list[str] = []
        if capabilities.powerpoint_com:
            try:
                pdf_path = self._render_powerpoint(final_pptx, output_dir / "final.pdf")
            except Exception as exc:  # COM errors vary by installed Office version.
                errors.append(f"PowerPoint COM failed: {exc}")
        if pdf_path is None and capabilities.libreoffice:
            try:
                pdf_path = self._render_libreoffice(final_pptx, output_dir)
            except Exception as exc:
                errors.append(f"LibreOffice failed: {exc}")
        return {
            "final_pptx": "final.pptx",
            "final_pdf": pdf_path.name if pdf_path and pdf_path.is_file() else None,
            "slides_png": [],
            "contact_sheet": None,
            "capabilities": capabilities.summary,
            "errors": errors,
        }

    @staticmethod
    def _render_powerpoint(pptx_path: Path, pdf_path: Path) -> Path:
        import pythoncom  # type: ignore
        import win32com.client  # type: ignore

        pythoncom.CoInitialize()
        app = win32com.client.DispatchEx("PowerPoint.Application")
        presentation = None
        try:
            presentation = app.Presentations.Open(str(pptx_path), WithWindow=False)
            presentation.SaveAs(str(pdf_path), 32)
        finally:
            if presentation is not None:
                presentation.Close()
            app.Quit()
            pythoncom.CoUninitialize()
        return pdf_path

    @staticmethod
    def _render_libreoffice(pptx_path: Path, output_dir: Path) -> Path | None:
        executable = find_executable("soffice") or find_executable("libreoffice")
        if not executable:
            return None
        result = subprocess.run([executable, "--headless", "--convert-to", "pdf", "--outdir", str(output_dir), str(pptx_path)], capture_output=True, text=True, encoding="utf-8", errors="replace", shell=False, check=False)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "LibreOffice conversion failed")
        candidate = output_dir / f"{pptx_path.stem}.pdf"
        target = output_dir / "final.pdf"
        if candidate.is_file() and candidate != target:
            candidate.replace(target)
        return target if target.is_file() else None
