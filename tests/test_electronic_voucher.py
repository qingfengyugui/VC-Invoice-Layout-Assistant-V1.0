"""Behavior tests for fixed-layout and metadata-only electronic vouchers."""

from __future__ import annotations

import hashlib
import subprocess
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from invoice_layout import electronic_voucher, normalize
from invoice_layout.electronic_voucher import convert_ofd_to_pdf, parse_voucher_xml
from invoice_layout.ingest import discover_inputs
from invoice_layout.models import SourceFile
from invoice_layout.normalize import normalize_sources
from tests.factories import test_settings as settings_for_tests


def test_ingest_accepts_ofd_and_xml(tmp_path: Path) -> None:
    ofd = tmp_path / "flight.ofd"
    ofd.write_bytes(b"OFD-TEST")
    xml = tmp_path / "flight.xml"
    xml.write_text("<Invoice><InvoiceNo>TEST-001</InvoiceNo></Invoice>", "utf-8")

    files, warnings = discover_inputs([ofd, xml], tmp_path / "work")

    assert {file.path.suffix for file in files} == {".ofd", ".xml"}
    xml_source = next(file for file in files if file.path.suffix == ".xml")
    assert xml_source.metadata == (("InvoiceNo", "TEST-001"),)
    assert warnings == []


def test_xml_never_becomes_generated_page(tmp_path: Path) -> None:
    path = tmp_path / "flight.xml"
    path.write_text("<Invoice><InvoiceNo>TEST-001</InvoiceNo></Invoice>", "utf-8")
    source = _source(path, "application/xml")

    pages, warnings = normalize_sources([source], settings_for_tests(tmp_path))

    assert pages == []
    assert [warning.code for warning in warnings] == ["xml_requires_layout_companion"]


def test_normalize_xml_keeps_ingested_metadata_without_creating_a_page(tmp_path: Path) -> None:
    path = tmp_path / "flight.xml"
    path.write_text("<Invoice><InvoiceNo>TEST-001</InvoiceNo></Invoice>", "utf-8")
    sources, ingest_warnings = discover_inputs([path], tmp_path / "work")

    pages, warnings = normalize_sources(sources, settings_for_tests(tmp_path))

    assert ingest_warnings == []
    assert sources[0].metadata == (("InvoiceNo", "TEST-001"),)
    assert pages == []
    assert [warning.code for warning in warnings] == ["xml_requires_layout_companion"]


def test_parse_voucher_xml_returns_namespace_local_scalar_fields(tmp_path: Path) -> None:
    path = tmp_path / "voucher.xml"
    path.write_text(
        "<v:Invoice xmlns:v='urn:test'><v:InvoiceNo> TEST-001 </v:InvoiceNo>"
        "<v:Passenger>Ada</v:Passenger><v:Details><v:Ignored>nested</v:Ignored>"
        "</v:Details></v:Invoice>",
        "utf-8",
    )

    fields = parse_voucher_xml(path)

    assert fields == {"InvoiceNo": "TEST-001", "Passenger": "Ada", "Ignored": "nested"}


def test_parse_voucher_xml_rejects_unsafe_entities(tmp_path: Path) -> None:
    path = tmp_path / "unsafe.xml"
    path.write_text(
        "<!DOCTYPE invoice [<!ENTITY expand 'unsafe'>]>"
        "<Invoice><InvoiceNo>&expand;</InvoiceNo></Invoice>",
        "utf-8",
    )

    with pytest.raises(ValueError, match="unsafe XML"):
        parse_voucher_xml(path)


def test_parse_voucher_xml_enforces_input_size_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "large.xml"
    path.write_bytes(b"<Invoice>oversized</Invoice>")
    monkeypatch.setattr(electronic_voucher, "MAX_XML_BYTES", 8)

    with pytest.raises(ValueError, match="20 MiB limit"):
        parse_voucher_xml(path)


def test_parse_voucher_xml_checks_size_before_reading(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "large.xml"
    path.write_bytes(b"<Invoice>oversized</Invoice>")
    monkeypatch.setattr(electronic_voucher, "MAX_XML_BYTES", 8)

    def unexpected_read(_: Path) -> bytes:
        raise AssertionError("oversized XML must not be read")

    monkeypatch.setattr(Path, "read_bytes", unexpected_read)

    with pytest.raises(ValueError, match="20 MiB limit"):
        parse_voucher_xml(path)


def test_parse_voucher_xml_limits_read_after_file_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "replaced.xml"
    path.write_bytes(b"<Invoice>oversized</Invoice>")
    monkeypatch.setattr(electronic_voucher, "MAX_XML_BYTES", 8)
    original_stat = Path.stat

    def stale_stat(candidate: Path, *args: object, **kwargs: object) -> object:
        if candidate == path:
            return SimpleNamespace(st_size=0)
        return original_stat(candidate, *args, **kwargs)

    def unbounded_read(_: Path) -> bytes:
        raise AssertionError("XML parser must use a bounded stream read")

    monkeypatch.setattr(Path, "stat", stale_stat)
    monkeypatch.setattr(Path, "read_bytes", unbounded_read)

    with pytest.raises(ValueError, match="20 MiB limit"):
        parse_voucher_xml(path)


def test_convert_ofd_to_pdf_uses_safe_argument_array_and_preserves_converter_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "flight;unsafe.ofd"
    source.write_bytes(b"OFD-TEST")
    renderer = tmp_path / "renderer.jar"
    renderer.write_bytes(b"JAR-TEST")
    output = tmp_path / "converted" / "flight.pdf"
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        Path(command[-1]).write_bytes(b"%PDF-CONVERTER-MARKER")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(electronic_voucher.subprocess, "run", fake_run)
    monkeypatch.setattr(electronic_voucher, "java_executable", lambda: Path("java"))

    result = convert_ofd_to_pdf(source, output, renderer)

    assert result == output
    assert output.read_bytes() == b"%PDF-CONVERTER-MARKER"
    command, kwargs = calls[0]
    assert command[:4] == ["java", "-jar", str(renderer), str(source)]
    assert Path(command[4]).parent == output.parent
    assert Path(command[4]).suffix == ".pdf"
    assert Path(command[4]) != output
    assert kwargs == {"check": True, "capture_output": True, "text": True, "timeout": 120}


def test_convert_ofd_to_pdf_rejects_stale_output_when_converter_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "flight.ofd"
    source.write_bytes(b"OFD-TEST")
    renderer = tmp_path / "renderer.jar"
    renderer.write_bytes(b"JAR-TEST")
    output = tmp_path / "flight.pdf"
    output.write_bytes(b"%PDF-STALE")
    monkeypatch.setattr(
        electronic_voucher.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, "", ""),
    )
    monkeypatch.setattr(electronic_voucher, "java_executable", lambda: Path("java"))

    with pytest.raises(electronic_voucher.OFDConversionError, match="did not create"):
        convert_ofd_to_pdf(source, output, renderer)
    assert output.read_bytes() == b"%PDF-STALE"


def test_convert_ofd_to_pdf_fails_before_output_when_java_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "flight.ofd"
    source.write_bytes(b"OFD-TEST")
    renderer = tmp_path / "renderer.jar"
    renderer.write_bytes(b"JAR-TEST")
    output = tmp_path / "flight.pdf"
    monkeypatch.setattr(electronic_voucher, "java_executable", lambda: None)

    with pytest.raises(electronic_voucher.OFDConversionError, match="Java runtime"):
        convert_ofd_to_pdf(source, output, renderer)

    assert not output.exists()


def test_convert_ofd_to_pdf_maps_atomic_replace_failure_to_conversion_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "flight.ofd"
    source.write_bytes(b"OFD-TEST")
    renderer = tmp_path / "renderer.jar"
    renderer.write_bytes(b"JAR-TEST")
    output = tmp_path / "flight.pdf"
    output.write_bytes(b"%PDF-STALE")

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        Path(command[-1]).write_bytes(b"%PDF-NEW")
        return subprocess.CompletedProcess(command, 0, "", "")

    def denied_replace(_: Path, *args: object, **kwargs: object) -> Path:
        raise PermissionError("denied")

    monkeypatch.setattr(electronic_voucher.subprocess, "run", fake_run)
    monkeypatch.setattr(electronic_voucher, "java_executable", lambda: Path("java"))
    monkeypatch.setattr(Path, "replace", denied_replace)

    with pytest.raises(electronic_voucher.OFDConversionError, match="PermissionError"):
        convert_ofd_to_pdf(source, output, renderer)
    assert output.read_bytes() == b"%PDF-STALE"


def test_normalize_ofd_preserves_converter_generated_pdf_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_path = tmp_path / "flight.ofd"
    source_path.write_bytes(b"OFD-TEST")
    source = _source(source_path, "application/ofd")
    renderer = tmp_path / "renderer.jar"
    renderer.write_bytes(b"JAR-TEST")

    def fake_convert(ofd: Path, output: Path, supplied_renderer: Path) -> Path:
        assert (ofd, supplied_renderer) == (source_path, renderer)
        output.parent.mkdir(parents=True, exist_ok=True)
        from reportlab.pdfgen.canvas import Canvas

        canvas = Canvas(str(output), pagesize=(72, 72))
        canvas.drawString(12, 36, "CONVERTER-MARKER")
        canvas.save()
        return output

    monkeypatch.setattr(normalize, "_ofd_renderer_path", lambda: renderer)
    monkeypatch.setattr(normalize, "convert_ofd_to_pdf", fake_convert)

    pages, warnings = normalize_sources([source], settings_for_tests(tmp_path))

    from pypdf import PdfReader

    assert warnings == []
    assert pages[0].source_path == source_path
    assert "CONVERTER-MARKER" in PdfReader(pages[0].page_pdf).pages[0].extract_text()


def test_normalize_ofd_conversion_failure_becomes_source_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_path = tmp_path / "flight.ofd"
    source_path.write_bytes(b"OFD-TEST")
    source = _source(source_path, "application/ofd")

    def failing_convert(_: Path, __: Path, ___: Path) -> Path:
        raise electronic_voucher.OFDConversionError("CalledProcessError")

    monkeypatch.setattr(normalize, "convert_ofd_to_pdf", failing_convert)

    pages, warnings = normalize_sources([source], settings_for_tests(tmp_path))

    assert pages == []
    assert [warning.code for warning in warnings] == ["ofd_conversion_failed"]


def test_ofd_renderer_path_allows_explicit_deployment_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    renderer = tmp_path / "ofd-renderer.jar"
    renderer.write_bytes(b"JAR")
    monkeypatch.setenv("INVOICE_LAYOUT_OFD_RENDERER", str(renderer))

    assert normalize._ofd_renderer_path() == renderer


def test_ofd_renderer_path_prefers_environment_override_over_packaged_jar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "site-packages" / "invoice_layout"
    package.mkdir(parents=True)
    monkeypatch.setattr(normalize, "__file__", str(package / "normalize.py"))
    packaged_renderer = package / "bin" / "ofd-renderer.jar"
    packaged_renderer.parent.mkdir()
    packaged_renderer.write_bytes(b"JAR")
    configured_renderer = tmp_path / "configured.jar"
    configured_renderer.write_bytes(b"CONFIGURED-JAR")
    monkeypatch.setenv("INVOICE_LAYOUT_OFD_RENDERER", str(configured_renderer))

    assert normalize._ofd_renderer_path() == configured_renderer


def test_ofd_renderer_path_rejects_missing_environment_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    missing_renderer = tmp_path / "missing.jar"
    monkeypatch.setenv("INVOICE_LAYOUT_OFD_RENDERER", str(missing_renderer))

    with pytest.raises(electronic_voucher.OFDConversionError, match="unavailable"):
        normalize._ofd_renderer_path()


def test_ofd_renderer_path_falls_back_to_development_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    package = tmp_path / "project" / "src" / "invoice_layout"
    package.mkdir(parents=True)
    monkeypatch.setattr(normalize, "__file__", str(package / "normalize.py"))
    monkeypatch.delenv("INVOICE_LAYOUT_OFD_RENDERER", raising=False)

    assert normalize._ofd_renderer_path() == tmp_path / "project" / "tools" / "ofd-renderer" / "target" / "ofd-renderer.jar"


def test_java_wrapper_uses_the_ofdrw_path_overload() -> None:
    wrapper = Path("tools/ofd-renderer/src/main/java/app/OFDRenderer.java").read_text("utf-8")

    assert "ConvertHelper.toPdf(input, output);" in wrapper
    assert "ConvertHelper.toPdf(input.toString(), output.toString());" not in wrapper


def test_shaded_renderer_removes_dependency_signature_files() -> None:
    pom = Path("tools/ofd-renderer/pom.xml").read_text("utf-8")

    assert "META-INF/*.SF" in pom
    assert "META-INF/*.DSA" in pom
    assert "META-INF/*.RSA" in pom


def test_build_helper_runs_maven_and_copies_the_shaded_renderer(tmp_path: Path) -> None:
    from tools.build_ofd_renderer import build_ofd_renderer

    project_root = tmp_path / "project"
    renderer_dir = project_root / "tools" / "ofd-renderer"
    renderer_dir.mkdir(parents=True)
    calls: list[tuple[list[str], Path, bool]] = []

    def fake_run(command: list[str], *, cwd: Path, check: bool) -> None:
        calls.append((command, cwd, check))
        artifact = cwd / "target" / "ofd-renderer.jar"
        artifact.parent.mkdir()
        artifact.write_bytes(b"SHADED-OFDRENDERER")

    packaged = build_ofd_renderer(project_root, runner=fake_run)

    assert calls == [(["mvn", "-q", "-DskipTests", "clean", "package"], renderer_dir, True)]
    assert packaged == project_root / "src" / "invoice_layout" / "bin" / "ofd-renderer.jar"
    assert packaged.read_bytes() == b"SHADED-OFDRENDERER"


def test_build_helper_reuses_packaged_renderer_without_maven(tmp_path: Path) -> None:
    from tools.build_ofd_renderer import build_ofd_renderer

    project_root = tmp_path / "project"
    packaged = project_root / "src" / "invoice_layout" / "bin" / "ofd-renderer.jar"
    packaged.parent.mkdir(parents=True)
    packaged.write_bytes(b"VERIFIED-PACKAGED-OFDRENDERER")

    def unexpected_run(*args: object, **kwargs: object) -> None:
        raise AssertionError("Maven must not run when the packaged renderer exists")

    assert build_ofd_renderer(project_root, runner=unexpected_run) == packaged
    assert packaged.read_bytes() == b"VERIFIED-PACKAGED-OFDRENDERER"


def test_build_helper_force_rebuilds_packaged_renderer(tmp_path: Path) -> None:
    from tools.build_ofd_renderer import build_ofd_renderer

    project_root = tmp_path / "project"
    packaged = project_root / "src" / "invoice_layout" / "bin" / "ofd-renderer.jar"
    packaged.parent.mkdir(parents=True)
    packaged.write_bytes(b"OLD")

    def fake_run(command: list[str], *, cwd: Path, check: bool) -> None:
        artifact = cwd / "target" / "ofd-renderer.jar"
        artifact.parent.mkdir(parents=True)
        artifact.write_bytes(b"REBUILT")

    assert build_ofd_renderer(project_root, runner=fake_run, force=True) == packaged
    assert packaged.read_bytes() == b"REBUILT"


def test_wheel_build_contract_includes_the_renderer_and_custom_hook() -> None:
    configuration = tomllib.loads(Path("pyproject.toml").read_text("utf-8"))
    wheel = configuration["tool"]["hatch"]["build"]["targets"]["wheel"]
    custom_hook = configuration["tool"]["hatch"]["build"]["hooks"]["custom"]

    assert wheel["force-include"] == {"src/invoice_layout/bin/ofd-renderer.jar": "invoice_layout/bin/ofd-renderer.jar"}
    assert custom_hook == {"path": "tools/hatch_build.py"}


def _source(path: Path, media_type: str) -> SourceFile:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return SourceFile(id=digest[:16], path=path, sha256=digest, media_type=media_type)
