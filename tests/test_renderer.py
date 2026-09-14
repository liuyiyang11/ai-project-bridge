from bridge.collectors.presentation_renderer import PresentationRenderer


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

