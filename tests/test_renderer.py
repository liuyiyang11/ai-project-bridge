import sys
import types

import pytest

from bridge.collectors.presentation_renderer import BundleLimits, PresentationRenderer, RenderCapabilities


def test_renderer_reports_capabilities_without_external_renderer(monkeypatch, tmp_path):
    monkeypatch.setattr("bridge.collectors.presentation_renderer.find_executable", lambda name: None)
    renderer = PresentationRenderer()
    capabilities = renderer.detect_capabilities()

    assert capabilities.powerpoint_com is False
    assert capabilities.libreoffice is False
    assert capabilities.summary


def test_renderer_always_preserves_final_pptx(tmp_path, monkeypatch):
    monkeypatch.setattr("bridge.collectors.presentation_renderer.find_executable", lambda name: None)
    source = tmp_path / "source.pptx"
    source.write_bytes(b"pptx")
    output = tmp_path / "rendered"

    result = PresentationRenderer().render(source, output)

    assert (output / "final.pptx").read_bytes() == b"pptx"
    assert result["final_pptx"] == "final.pptx"
    assert result["final_pdf"] is None


def test_powerpoint_detection_does_not_require_powerpnt_on_path(monkeypatch):
    class FakeApplication:
        def __init__(self):
            self.quit_called = False

        def Quit(self):
            self.quit_called = True

    app = FakeApplication()
    pythoncom = types.ModuleType("pythoncom")
    pythoncom.CoInitialize = lambda: None
    pythoncom.CoUninitialize = lambda: None
    client = types.ModuleType("win32com.client")
    client.DispatchEx = lambda name: app
    win32com = types.ModuleType("win32com")
    win32com.client = client
    monkeypatch.setattr("bridge.collectors.presentation_renderer.os.name", "nt")
    monkeypatch.setitem(sys.modules, "pythoncom", pythoncom)
    monkeypatch.setitem(sys.modules, "win32com", win32com)
    monkeypatch.setitem(sys.modules, "win32com.client", client)

    assert PresentationRenderer._probe_powerpoint_com_in_process() is True
    assert app.quit_called is True


def test_renderer_exports_slides_and_contact_sheet_from_pdf(tmp_path, monkeypatch):
    fitz = pytest.importorskip("pymupdf")
    pytest.importorskip("PIL")
    source = tmp_path / "source.pptx"
    source.write_bytes(b"pptx")

    def fake_powerpoint(pptx_path, pdf_path):
        document = fitz.open()
        for _ in range(3):
            document.new_page(width=640, height=360)
        document.save(str(pdf_path))
        document.close()
        return pdf_path

    monkeypatch.setattr(PresentationRenderer, "_powerpoint_com_available", staticmethod(lambda: True))
    monkeypatch.setattr(PresentationRenderer, "_pdf_to_png_available", staticmethod(lambda: True))
    monkeypatch.setattr(PresentationRenderer, "_render_powerpoint", staticmethod(fake_powerpoint))
    monkeypatch.setattr("bridge.collectors.presentation_renderer.find_executable", lambda name: None)

    result = PresentationRenderer().render(source, tmp_path / "rendered", limits=BundleLimits(max_images=4))

    assert result["final_pdf"] == "final.pdf"
    assert result["slides_png"] == ["slides_png/slide_001.png", "slides_png/slide_002.png", "slides_png/slide_003.png"]
    assert result["contact_sheet"] == "contact_sheet.png"
    assert all((tmp_path / "rendered" / name).is_file() for name in result["slides_png"] + [result["contact_sheet"]])

