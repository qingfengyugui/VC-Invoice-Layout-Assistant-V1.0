from __future__ import annotations

from pathlib import Path

from tools.build_portable_bundle import build_portable_bundle


def test_portable_bundle_contains_executable_native_tools_and_all_skills(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    executable = tmp_path / "invoice-layout.exe"
    executable.write_bytes(b"EXE")
    java_home = tmp_path / "jdk"
    jlink = java_home / "bin" / "jlink.exe"
    jlink.parent.mkdir(parents=True)
    jlink.write_bytes(b"JLINK")
    seven_zip = tmp_path / "7z.exe"
    seven_zip.write_bytes(b"7ZIP")
    seven_zip.with_name("7z.dll").write_bytes(b"DLL")
    seven_zip_license = tmp_path / "License.txt"
    seven_zip_license.write_text("7-Zip license", encoding="utf-8")
    (project / "LICENSE").parent.mkdir(parents=True, exist_ok=True)
    (project / "LICENSE").write_text("license", encoding="utf-8")
    (project / "THIRD_PARTY_NOTICES.md").write_text("notices", encoding="utf-8")
    for platform in ("codex", "claude-code", "openclaw", "workbuddy", "qoder", "qclaw"):
        skill = project / "platforms" / platform / "invoice-layout-agent"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(platform, encoding="utf-8")
    (project / "platforms" / "compatibility.json").write_text(
        "{}", encoding="utf-8"
    )
    output = tmp_path / "bundle"
    calls: list[list[str]] = []

    def fake_run(command: list[str], *, check: bool) -> None:
        calls.append(command)
        java_output = Path(command[command.index("--output") + 1])
        (java_output / "bin").mkdir(parents=True)
        (java_output / "bin" / "java.exe").write_bytes(b"JAVA")

    built = build_portable_bundle(
        project,
        executable=executable,
        java_home=java_home,
        seven_zip=seven_zip,
        seven_zip_license=seven_zip_license,
        output=output,
        runner=fake_run,
    )

    assert built == output
    assert (output / "invoice-layout.exe").read_bytes() == b"EXE"
    assert (output / "native" / "java" / "bin" / "java.exe").is_file()
    assert (output / "native" / "bin" / "7z.exe").read_bytes() == b"7ZIP"
    assert (output / "native" / "bin" / "7z.dll").read_bytes() == b"DLL"
    assert (output / "licenses" / "7zip" / "License.txt").is_file()
    assert all(
        (output / "platforms" / platform / "invoice-layout-agent" / "SKILL.md").is_file()
        for platform in ("codex", "claude-code", "openclaw", "workbuddy", "qoder", "qclaw")
    )
    assert "java.base,java.desktop,java.management,java.naming,java.sql" in calls[0]


def test_portable_bundle_refuses_existing_destination(tmp_path: Path) -> None:
    output = tmp_path / "bundle"
    output.mkdir()

    try:
        build_portable_bundle(
            tmp_path,
            executable=tmp_path / "missing",
            java_home=tmp_path / "missing-jdk",
            seven_zip=tmp_path / "missing-7z",
            output=output,
        )
    except FileExistsError:
        pass
    else:
        raise AssertionError("existing bundle destination must be refused")
