from __future__ import annotations

import os
from pathlib import Path

from invoice_layout import runtime


def test_bundled_native_tools_are_found_without_system_path(
    tmp_path: Path, monkeypatch
) -> None:
    native = tmp_path / "native"
    java = native / "java" / "bin" / ("java.exe" if os.name == "nt" else "java")
    seven_zip = native / "bin" / ("7zz.exe" if os.name == "nt" else "7zz")
    java.parent.mkdir(parents=True)
    seven_zip.parent.mkdir(parents=True)
    java.write_bytes(b"JAVA")
    seven_zip.write_bytes(b"7ZIP")
    monkeypatch.setenv("INVOICE_LAYOUT_NATIVE_ROOT", str(native))
    monkeypatch.setenv("PATH", "")

    runtime.configure_native_environment()

    assert runtime.java_executable() == java
    assert runtime.rar_extractor() == seven_zip
    assert os.environ["PATH"].split(os.pathsep)[0] == str(native / "bin")


def test_configured_java_takes_precedence(tmp_path: Path, monkeypatch) -> None:
    configured = tmp_path / "custom-java"
    configured.write_bytes(b"JAVA")
    monkeypatch.setenv("INVOICE_LAYOUT_JAVA", str(configured))

    assert runtime.java_executable() == configured
