from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..config import BundleLimits
from ..github import find_executable


@dataclass(frozen=True)
class RenderCapabilities:
    powerpoint_com: bool
    libreoffice: bool
    pdf_to_png: bool
    summary: str


class PresentationRenderer:
    def detect_capabilities(self) -> RenderCapabilities:
        powerpoint = self._powerpoint_com_available()
        libreoffice = bool(find_executable("soffice") or find_executable("libreoffice"))
        pdf_to_png = self._pdf_to_png_available()
        summary = "; ".join(
            [
                f"PowerPoint COM {'available' if powerpoint else 'unavailable'}",
                f"LibreOffice {'available' if libreoffice else 'unavailable'}",
                f"PDF-to-PNG renderer {'available' if pdf_to_png else 'unavailable'}",
            ]
        )
        return RenderCapabilities(powerpoint, libreoffice, pdf_to_png, summary)

    @staticmethod
    def _powerpoint_com_available() -> bool:
        """Probe COM in an isolated process, because Office RPC failures can be fatal to Python."""
        if os.name != "nt":
            return False
        try:
            result = subprocess.run(
                [sys.executable, "-m", "bridge.collectors.presentation_renderer", "--probe-powerpoint-com"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0 and (result.stdout or "").strip() == "available"

    @staticmethod
    def _probe_powerpoint_com_in_process() -> bool:
        """Run inside the isolated probe process; never call this from the Bridge worker."""
        try:
            import pythoncom  # type: ignore
            import win32com.client  # type: ignore
        except ImportError:
            return False

        initialized = False
        app = None
        try:
            pythoncom.CoInitialize()
            initialized = True
            app = win32com.client.DispatchEx("PowerPoint.Application")
            return app is not None
        except Exception:
            return False
        finally:
            if app is not None:
                try:
                    app.Quit()
                except Exception:
                    pass
            if initialized:
                try:
                    pythoncom.CoUninitialize()
                except Exception:
                    pass

    @staticmethod
    def _import_pdf_renderer():
        try:
            import pymupdf  # type: ignore

            return pymupdf
        except ImportError:
            import fitz  # type: ignore

            return fitz

    @staticmethod
    def _pdf_to_png_available() -> bool:
        try:
            PresentationRenderer._import_pdf_renderer()
        except ImportError:
            return False
        return True

    def render(self, pptx_path: Path, output_dir: Path, *, limits: Optional[BundleLimits] = None) -> dict:
        limits = limits or BundleLimits()
        output_dir = Path(output_dir)
        if output_dir.exists():
            shutil.rmtree(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        final_pptx = output_dir / "final.pptx"
        shutil.copy2(pptx_path, final_pptx)
        capabilities = self.detect_capabilities()
        pdf_path: Optional[Path] = None
        slides_png: list[str] = []
        contact_sheet: Optional[str] = None
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
        if pdf_path is not None and pdf_path.is_file() and capabilities.pdf_to_png:
            try:
                slides_png, contact_sheet = self._render_pdf_pages(pdf_path, output_dir, limits)
            except Exception as exc:
                errors.append(f"PDF to PNG failed: {exc}")
        elif pdf_path is not None and pdf_path.is_file():
            errors.append("PDF to PNG renderer is unavailable; install the presentation optional dependencies")
        return {
            "final_pptx": "final.pptx",
            "final_pdf": pdf_path.name if pdf_path and pdf_path.is_file() else None,
            "slides_png": slides_png,
            "contact_sheet": contact_sheet,
            "capabilities": capabilities.summary,
            "errors": errors,
        }

    @staticmethod
    def _render_powerpoint(pptx_path: Path, pdf_path: Path) -> Path:
        import pythoncom  # type: ignore
        import win32com.client  # type: ignore

        initialized = False
        app = None
        presentation = None
        try:
            pythoncom.CoInitialize()
            initialized = True
            app = win32com.client.DispatchEx("PowerPoint.Application")
            presentation = app.Presentations.Open(str(pptx_path), WithWindow=False)
            presentation.SaveAs(str(pdf_path), 32)
        finally:
            if presentation is not None:
                presentation.Close()
            if app is not None:
                app.Quit()
            if initialized:
                pythoncom.CoUninitialize()
        return pdf_path

    @staticmethod
    def _render_libreoffice(pptx_path: Path, output_dir: Path) -> Optional[Path]:
        executable = find_executable("soffice") or find_executable("libreoffice")
        if not executable:
            return None
        result = subprocess.run(
            [executable, "--headless", "--convert-to", "pdf", "--outdir", str(output_dir), str(pptx_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "LibreOffice conversion failed")
        candidate = output_dir / f"{pptx_path.stem}.pdf"
        target = output_dir / "final.pdf"
        if candidate.is_file() and candidate != target:
            candidate.replace(target)
        return target if target.is_file() else None

    @staticmethod
    def _render_pdf_pages(pdf_path: Path, output_dir: Path, limits: BundleLimits) -> tuple[list[str], Optional[str]]:
        fitz = PresentationRenderer._import_pdf_renderer()

        if limits.max_images <= 0:
            return [], None
        slides_dir = output_dir / "slides_png"
        slides_dir.mkdir(parents=True, exist_ok=True)
        max_slide_images = max(0, limits.max_images - 1)
        max_file = limits.max_artifact_file_mb * 1024 * 1024
        slide_paths: list[Path] = []
        document = fitz.open(str(pdf_path))
        try:
            for index, page in enumerate(document):
                if index >= max_slide_images:
                    break
                pixmap = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
                path = slides_dir / f"slide_{index + 1:03d}.png"
                pixmap.save(str(path))
                if path.stat().st_size <= max_file:
                    slide_paths.append(path)
                else:
                    path.unlink(missing_ok=True)
        finally:
            document.close()

        slide_names = [path.relative_to(output_dir).as_posix() for path in slide_paths]
        if not slide_paths:
            return slide_names, None
        contact = PresentationRenderer._make_contact_sheet(slide_paths, output_dir / "contact_sheet.png")
        if contact is not None and contact.stat().st_size > max_file:
            contact.unlink(missing_ok=True)
            contact = None
        return slide_names, contact.relative_to(output_dir).as_posix() if contact else None

    @staticmethod
    def _make_contact_sheet(slide_paths: list[Path], contact_path: Path) -> Optional[Path]:
        try:
            from PIL import Image  # type: ignore
        except ImportError:
            return None

        thumbnail_width = 320
        thumbnail_height = 180
        gutter = 16
        columns = 4
        rows = (len(slide_paths) + columns - 1) // columns
        canvas = Image.new(
            "RGB",
            (columns * thumbnail_width + (columns + 1) * gutter, rows * thumbnail_height + (rows + 1) * gutter),
            "white",
        )
        resampling = getattr(Image, "Resampling", Image)
        for index, slide_path in enumerate(slide_paths):
            with Image.open(slide_path) as image:
                thumbnail = image.convert("RGB")
                thumbnail.thumbnail((thumbnail_width, thumbnail_height), resampling.LANCZOS)
                column = index % columns
                row = index // columns
                x = gutter + column * (thumbnail_width + gutter) + (thumbnail_width - thumbnail.width) // 2
                y = gutter + row * (thumbnail_height + gutter) + (thumbnail_height - thumbnail.height) // 2
                canvas.paste(thumbnail, (x, y))
        canvas.save(contact_path, format="PNG", optimize=True)
        return contact_path


if __name__ == "__main__" and len(sys.argv) == 2 and sys.argv[1] == "--probe-powerpoint-com":
    raise SystemExit(0 if PresentationRenderer._probe_powerpoint_com_in_process() else 1)
