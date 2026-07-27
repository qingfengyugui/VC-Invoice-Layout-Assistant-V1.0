from __future__ import annotations

import io
import tarfile
from pathlib import Path
from types import SimpleNamespace
from typing import Self
from zipfile import ZipFile

import py7zr
import pytest

from invoice_layout import ingest
from invoice_layout.ingest import _safe_member, discover_inputs
from tests.factories import (
    make_blank_pdf,
    make_color_image,
    make_encrypted_pdf,
    make_vector_pdf,
)

PDF_BYTES = b"%PDF-1.4\n% GEOMETRY-TEST\n%%EOF\n"


class _RarEntry:
    def __init__(
        self,
        filename: str,
        payload: bytes = PDF_BYTES,
        *,
        declared_size: int | None = None,
        directory: bool = False,
        symlink: bool = False,
    ) -> None:
        self.filename = filename
        self.payload = payload
        self.file_size = len(payload) if declared_size is None else declared_size
        self._directory = directory
        self._symlink = symlink

    def isdir(self) -> bool:
        return self._directory

    def is_symlink(self) -> bool:
        return self._symlink


def _mock_rar(
    monkeypatch: pytest.MonkeyPatch,
    entries: list[_RarEntry],
) -> None:
    class Archive:
        def __init__(self, _: Path) -> None:
            pass

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def infolist(self) -> list[_RarEntry]:
            return entries

        def open(self, entry: _RarEntry) -> io.BytesIO:
            return io.BytesIO(entry.payload)

    monkeypatch.setattr(ingest, "rarfile", SimpleNamespace(RarFile=Archive))


def _write_tar(path: Path, members: dict[str, bytes]) -> None:
    with tarfile.open(path, "w") as archive:
        for name, payload in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))


def test_geometry_factories_return_non_financial_source_files(tmp_path: Path) -> None:
    sources = (
        make_blank_pdf(tmp_path)
        + make_color_image(tmp_path)
        + make_vector_pdf(tmp_path)
        + make_encrypted_pdf(tmp_path)
    )

    assert [source.path.name for source in sources] == [
        "blank-test.pdf",
        "color-test.png",
        "vector-test.pdf",
        "encrypted-test.pdf",
    ]
    assert all(source.sha256 and source.archive_member is None for source in sources)


def test_mixed_inputs_expand_and_deduplicate(tmp_path: Path) -> None:
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(PDF_BYTES)
    archive = tmp_path / "batch.zip"
    with ZipFile(archive, "w") as zf:
        zf.writestr("nested/a-copy.pdf", PDF_BYTES)
        zf.writestr("../escape.pdf", PDF_BYTES)

    files, warnings = discover_inputs([pdf, archive], tmp_path / "work")

    assert len(files) == 1
    assert files[0].sha256
    assert any(item.code == "unsafe_archive_member" for item in warnings)
    assert not (tmp_path / "escape.pdf").exists()


def test_unsupported_file_becomes_warning(tmp_path: Path) -> None:
    bad = tmp_path / "notes.exe"
    bad.write_bytes(b"MZ")

    files, warnings = discover_inputs([bad], tmp_path / "work")

    assert files == []
    assert warnings[0].code == "unsupported_format"


def test_directory_input_discovers_supported_files_recursively(tmp_path: Path) -> None:
    source_dir = tmp_path / "sources"
    nested = source_dir / "nested"
    nested.mkdir(parents=True)
    (source_dir / "first.png").write_bytes(b"PNG-TEST")
    (nested / "second.pdf").write_bytes(PDF_BYTES)

    files, warnings = discover_inputs([source_dir], tmp_path / "work")

    assert [source.path.name for source in files] == ["first.png", "second.pdf"]
    assert warnings == []


def test_directory_discovery_rejects_symlink_without_following_external_target(
    tmp_path: Path,
) -> None:
    external = tmp_path / "external.pdf"
    external.write_bytes(PDF_BYTES)
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    (input_dir / "linked.pdf").symlink_to(external)

    files, warnings = discover_inputs([input_dir], tmp_path / "work")

    assert files == []
    assert [warning.code for warning in warnings] == ["unsafe_symlink"]


@pytest.mark.parametrize("member", ["../escape.pdf", "..\\escape.pdf", "/escape.pdf"])
def test_zip_member_traversal_is_never_extracted(tmp_path: Path, member: str) -> None:
    archive = tmp_path / "unsafe.zip"
    with ZipFile(archive, "w") as zf:
        zf.writestr(member, PDF_BYTES)

    files, warnings = discover_inputs([archive], tmp_path / "work")

    assert files == []
    assert [warning.code for warning in warnings] == ["unsafe_archive_member"]
    assert not (tmp_path / "escape.pdf").exists()


def test_tar_member_traversal_is_never_extracted(tmp_path: Path) -> None:
    archive = tmp_path / "unsafe.tar"
    _write_tar(archive, {"../escape.pdf": PDF_BYTES})

    files, warnings = discover_inputs([archive], tmp_path / "work")

    assert files == []
    assert [warning.code for warning in warnings] == ["unsafe_archive_member"]
    assert not (tmp_path / "escape.pdf").exists()


def test_archive_member_metadata_is_preserved(tmp_path: Path) -> None:
    archive = tmp_path / "batch.zip"
    with ZipFile(archive, "w") as zf:
        zf.writestr("nested/geometry.pdf", PDF_BYTES)

    files, warnings = discover_inputs([archive], tmp_path / "work")

    assert warnings == []
    assert files[0].archive_member == "nested/geometry.pdf"


def test_nested_archives_stop_at_maximum_depth(tmp_path: Path) -> None:
    payload = PDF_BYTES
    for depth in range(4):
        archive = tmp_path / f"nested-{depth}.zip"
        with ZipFile(archive, "w") as zf:
            zf.writestr("next.zip" if depth else "geometry.pdf", payload)
        payload = archive.read_bytes()

    outer = tmp_path / "outer.zip"
    outer.write_bytes(payload)
    files, warnings = discover_inputs([outer], tmp_path / "work")

    assert files == []
    assert any(warning.code == "archive_depth_exceeded" for warning in warnings)


def test_archive_file_count_limit_becomes_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("invoice_layout.ingest.MAX_FILES", 1)
    archive = tmp_path / "many.zip"
    with ZipFile(archive, "w") as zf:
        zf.writestr("one.pdf", PDF_BYTES)
        zf.writestr("two.pdf", PDF_BYTES + b"2")

    files, warnings = discover_inputs([archive], tmp_path / "work")

    assert files == []
    assert [warning.code for warning in warnings] == ["archive_limit"]


def test_archive_expanded_byte_limit_becomes_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("invoice_layout.ingest.MAX_BYTES", len(PDF_BYTES) - 1)
    archive = tmp_path / "large.zip"
    with ZipFile(archive, "w") as zf:
        zf.writestr("geometry.pdf", PDF_BYTES)

    files, warnings = discover_inputs([archive], tmp_path / "work")

    assert files == []
    assert [warning.code for warning in warnings] == ["archive_limit"]


def test_malformed_archive_becomes_warning(tmp_path: Path) -> None:
    archive = tmp_path / "bad.zip"
    archive.write_bytes(b"not-an-archive")

    files, warnings = discover_inputs([archive], tmp_path / "work")

    assert files == []
    assert [warning.code for warning in warnings] == ["archive_failed"]


@pytest.mark.parametrize("member", ["NUL.pdf", "CON", "aux ", "LPT1.txt", "COM9."])
def test_windows_device_member_names_are_rejected(member: str) -> None:
    assert not _safe_member(member)


def test_archive_file_budget_applies_to_the_whole_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("invoice_layout.ingest.MAX_BYTES", len(PDF_BYTES))
    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"
    for archive in (first, second):
        with ZipFile(archive, "w") as zf:
            zf.writestr("geometry.pdf", PDF_BYTES)

    files, warnings = discover_inputs([first, second], tmp_path / "work")

    assert len(files) == 1
    assert any(warning.code == "archive_limit" for warning in warnings)


def test_archive_entry_budget_counts_directories(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("invoice_layout.ingest.MAX_FILES", 1)
    archive = tmp_path / "directory-flood.zip"
    with ZipFile(archive, "w") as zf:
        zf.writestr("nested/", b"")
        zf.writestr("nested/geometry.pdf", PDF_BYTES)

    files, warnings = discover_inputs([archive], tmp_path / "work")

    assert files == []
    assert [warning.code for warning in warnings] == ["archive_limit"]


def test_7z_actual_size_cannot_exceed_declared_size(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Entry:
        filename = "geometry.pdf"
        is_directory = False
        uncompressed = 4

    class Archive:
        def __init__(self, _: Path) -> None:
            pass

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def list(self) -> list[Entry]:
            return [Entry()]

        def extractall(self, path: Path) -> None:
            (path / Entry.filename).write_bytes(b"12345")

        def extract(self, *, path: Path, targets: list[str], factory: object) -> None:
            assert targets == [Entry.filename]
            writer = factory.create(Entry.filename)  # type: ignore[attr-defined]
            writer.write(b"12345")  # type: ignore[attr-defined]

    monkeypatch.setattr(ingest, "py7zr", SimpleNamespace(SevenZipFile=Archive))
    archive = tmp_path / "oversized.7z"
    archive.write_bytes(b"7z-test")

    files, warnings = discover_inputs([archive], tmp_path / "work")

    assert files == []
    assert [warning.code for warning in warnings] == ["archive_limit"]


def test_oversized_7z_member_exhausts_the_shared_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Entry:
        filename = "geometry.pdf"
        is_directory = False
        uncompressed = 4

    class Archive:
        def __init__(self, _: Path) -> None:
            pass

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def list(self) -> list[Entry]:
            return [Entry()]

        def extract(self, *, path: Path, targets: list[str], factory: object) -> None:
            assert targets == [Entry.filename]
            writer = factory.create(Entry.filename)  # type: ignore[attr-defined]
            writer.write(b"12345")  # type: ignore[attr-defined]

    monkeypatch.setattr("invoice_layout.ingest.MAX_BYTES", 5)
    monkeypatch.setattr(ingest, "py7zr", SimpleNamespace(SevenZipFile=Archive))
    oversized = tmp_path / "oversized.7z"
    oversized.write_bytes(b"7z-test")
    following = tmp_path / "following.zip"
    with ZipFile(following, "w") as zf:
        zf.writestr("next.pdf", b"1234")

    files, warnings = discover_inputs([oversized, following], tmp_path / "work")

    assert files == []
    assert [warning.code for warning in warnings] == ["archive_limit", "archive_limit"]
    assert not list((tmp_path / "work").rglob("geometry.pdf"))


def test_7z_writer_never_writes_beyond_shared_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    observed_sizes: list[int] = []

    class Entry:
        filename = "geometry.pdf"
        is_directory = False
        uncompressed = 4

    class Archive:
        def __init__(self, _: Path) -> None:
            pass

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def list(self) -> list[Entry]:
            return [Entry()]

        def extract(self, *, path: Path, targets: list[str], factory: object) -> None:
            assert targets == [Entry.filename]
            writer = factory.create(Entry.filename)  # type: ignore[attr-defined]
            writer.write(b"1234")  # type: ignore[attr-defined]
            observed_sizes.append((path / Entry.filename).stat().st_size)
            writer.write(b"5")  # type: ignore[attr-defined]

    monkeypatch.setattr("invoice_layout.ingest.MAX_BYTES", 4)
    monkeypatch.setattr(ingest, "py7zr", SimpleNamespace(SevenZipFile=Archive))
    oversized = tmp_path / "bounded.7z"
    oversized.write_bytes(b"7z-test")
    following = tmp_path / "following.zip"
    with ZipFile(following, "w") as zf:
        zf.writestr("next.pdf", b"1")

    files, warnings = discover_inputs([oversized, following], tmp_path / "work")

    assert observed_sizes == [4]
    assert files == []
    assert [warning.code for warning in warnings] == ["archive_limit", "archive_limit"]
    assert not list((tmp_path / "work").rglob("geometry.pdf"))


def test_real_py7zr_archive_uses_the_streaming_factory(tmp_path: Path) -> None:
    assert tuple(map(int, py7zr.__version__.split(".")[:2])) >= (1, 1)
    archive = tmp_path / "real.7z"
    with py7zr.SevenZipFile(archive, "w") as writer:
        writer.writestr(PDF_BYTES, "geometry.pdf")

    files, warnings = discover_inputs([archive], tmp_path / "work")

    assert warnings == []
    assert len(files) == 1
    assert files[0].archive_member == "geometry.pdf"
    assert files[0].path.read_bytes() == PDF_BYTES


def test_rar_member_is_streamed_through_shared_safety_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_rar(monkeypatch, [_RarEntry("trip/geometry.pdf")])
    archive = tmp_path / "batch.rar"
    archive.write_bytes(b"RAR-TEST")

    files, warnings = discover_inputs([archive], tmp_path / "work")

    assert warnings == []
    assert len(files) == 1
    assert files[0].archive_member == "trip/geometry.pdf"
    assert files[0].path.read_bytes() == PDF_BYTES


@pytest.mark.parametrize("member", ["../escape.pdf", "..\\escape.pdf", "/escape.pdf"])
def test_rar_member_traversal_is_never_extracted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    member: str,
) -> None:
    _mock_rar(monkeypatch, [_RarEntry(member)])
    archive = tmp_path / "unsafe.rar"
    archive.write_bytes(b"RAR-TEST")

    files, warnings = discover_inputs([archive], tmp_path / "work")

    assert files == []
    assert [warning.code for warning in warnings] == ["unsafe_archive_member"]
    assert not (tmp_path / "escape.pdf").exists()


def test_rar_symlink_member_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_rar(monkeypatch, [_RarEntry("link.pdf", symlink=True)])
    archive = tmp_path / "symlink.rar"
    archive.write_bytes(b"RAR-TEST")

    files, warnings = discover_inputs([archive], tmp_path / "work")

    assert files == []
    assert [warning.code for warning in warnings] == ["unsafe_archive_member"]


def test_rar_declared_size_cannot_exceed_shared_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ingest, "MAX_BYTES", 4)
    _mock_rar(monkeypatch, [_RarEntry("large.pdf", declared_size=5)])
    archive = tmp_path / "large.rar"
    archive.write_bytes(b"RAR-TEST")

    files, warnings = discover_inputs([archive], tmp_path / "work")

    assert files == []
    assert [warning.code for warning in warnings] == ["archive_limit"]


def test_rar_stream_never_writes_beyond_shared_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ingest, "MAX_BYTES", 4)
    _mock_rar(
        monkeypatch,
        [_RarEntry("bounded.pdf", b"12345", declared_size=4)],
    )
    archive = tmp_path / "bounded.rar"
    archive.write_bytes(b"RAR-TEST")

    files, warnings = discover_inputs([archive], tmp_path / "work")

    assert files == []
    assert [warning.code for warning in warnings] == ["archive_limit"]
    assert not list((tmp_path / "work").rglob("bounded.pdf"))


def test_missing_rar_backend_is_an_explicit_archive_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ingest, "rarfile", None, raising=False)
    archive = tmp_path / "missing-backend.rar"
    archive.write_bytes(b"RAR-TEST")

    files, warnings = discover_inputs([archive], tmp_path / "work")

    assert files == []
    assert [warning.code for warning in warnings] == ["archive_failed"]
