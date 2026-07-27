"""Safe discovery of supported source files and archive contents."""

from __future__ import annotations

import hashlib
import importlib
import mimetypes
import shutil
import tarfile
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from tempfile import mkdtemp
from threading import Lock
from typing import Any, BinaryIO, Protocol

py7zr: Any
_Py7zIO: Any
_WriterFactory: Any
rarfile: Any
try:  # py7zr is an optional runtime dependency in minimal developer installs.
    py7zr = importlib.import_module("py7zr")
    _Py7zIO = py7zr.Py7zIO
    _WriterFactory = py7zr.WriterFactory
except ImportError:  # pragma: no cover - covered when optional dependency is absent
    py7zr = None
    _Py7zIO = object
    _WriterFactory = object

try:  # rarfile still needs one supported local decompression backend.
    rarfile = importlib.import_module("rarfile")
except ImportError:  # pragma: no cover - covered when optional dependency is absent
    rarfile = None

from .electronic_voucher import parse_voucher_xml
from .models import SourceFile, WarningItem
from .runtime import configure_native_environment

SUPPORTED = {".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".heic", ".ofd", ".xml"}
MAX_FILES = 10_000
MAX_BYTES = 2 * 1024**3
MAX_DEPTH = 3

_COPY_CHUNK_SIZE = 1024 * 1024
_MEDIA_TYPES = {
    ".heic": "image/heic",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".pdf": "application/pdf",
    ".ofd": "application/ofd",
    ".xml": "application/xml",
    ".png": "image/png",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".webp": "image/webp",
}
_WINDOWS_RESERVED_NAMES = {"con", "prn", "aux", "nul", *(f"com{number}" for number in range(1, 10)), *(f"lpt{number}" for number in range(1, 10))}


class _ArchiveLimitError(ValueError):
    """An archive exceeds a declared resource limit."""


class _UnsafeArchiveMemberError(ValueError):
    """An archive member has a path that cannot be safely materialized."""


class _ReadableBytes(Protocol):
    def read(self, size: int = -1) -> bytes: ...


@dataclass
class _ExpansionBudget:
    """One resource budget shared by every archive expanded in a batch."""

    remaining_entries: int
    remaining_bytes: int
    _lock: Lock = field(default_factory=Lock, repr=False)

    def consume_entry(self) -> None:
        with self._lock:
            if self.remaining_entries <= 0:
                raise _ArchiveLimitError("archive expansion limit exceeded")
            self.remaining_entries -= 1

    def consume_bytes(self, amount: int) -> None:
        with self._lock:
            if amount < 0 or amount > self.remaining_bytes:
                raise _ArchiveLimitError("archive expansion limit exceeded")
            self.remaining_bytes -= amount


def _safe_member(name: str) -> bool:
    """Return whether an archive member is a portable relative file path."""
    if not name or "\x00" in name:
        return False
    normalized = name.replace("\\", "/")
    posix_path = PurePosixPath(normalized)
    windows_path = PureWindowsPath(normalized)
    if posix_path.is_absolute() or windows_path.is_absolute() or windows_path.drive:
        return False
    for part in posix_path.parts:
        normalized_part = part.rstrip(" .")
        device_stem = normalized_part.split(".", maxsplit=1)[0].casefold()
        if (
            part in {"", ".", ".."}
            or ":" in part
            or part != normalized_part
            or device_stem in _WINDOWS_RESERVED_NAMES
        ):
            return False
    return True


def _member_key(member: str) -> str:
    """Return the Windows-normalized key used to reject destination collisions."""
    return "/".join(part.rstrip(" .").casefold() for part in PurePosixPath(member.replace("\\", "/")).parts)


def _warning(code: str, path: Path, message: str) -> WarningItem:
    return WarningItem(
        code=code,
        source_page_ids=(),
        output_page=None,
        message=f"{path}: {message}",
        action="review source file",
        severity="warning",
    )


def _archive_kind(path: Path) -> str | None:
    name = path.name.lower()
    if name.endswith(".zip"):
        return "zip"
    if name.endswith(".7z"):
        return "7z"
    if name.endswith(".rar"):
        return "rar"
    if name.endswith((".tar", ".tar.gz", ".tgz")):
        return "tar"
    return None


def _target(root: Path, member: str, destinations: set[str] | None = None) -> Path:
    """Build a member target while preventing zip-slip and symlink escapes."""
    if not _safe_member(member):
        raise _UnsafeArchiveMemberError(f"unsafe archive member: {member}")
    if destinations is not None:
        key = _member_key(member)
        if key in destinations:
            raise _UnsafeArchiveMemberError(f"archive member collides on Windows: {member}")
        destinations.add(key)

    root = root.resolve()
    target = root.joinpath(*PurePosixPath(member.replace("\\", "/")).parts)
    target_parent = target.parent
    target_parent.mkdir(parents=True, exist_ok=True)
    resolved_parent = target_parent.resolve()
    if resolved_parent != root and root not in resolved_parent.parents:
        raise _UnsafeArchiveMemberError(f"archive member escapes root: {member}")

    resolved_target = target.resolve(strict=False)
    if resolved_target != root and root not in resolved_target.parents:
        raise _UnsafeArchiveMemberError(f"archive member escapes root: {member}")
    return target


def _copy_limited(source: _ReadableBytes, destination: Path, remaining: int) -> int:
    """Copy one member without writing more than its remaining byte budget."""
    written = 0
    with destination.open("wb") as output:
        while chunk := source.read(_COPY_CHUNK_SIZE):
            written += len(chunk)
            if written > remaining:
                raise _ArchiveLimitError("archive expansion limit exceeded")
            output.write(chunk)
    return written


def _materialized_size(path: Path) -> int:
    """Return an extracted regular file's size without following a symlink."""
    if path.is_symlink() or not path.is_file():
        return 0
    return path.stat().st_size


def _remove_failed_output(path: Path) -> None:
    """Remove one failed archive member while keeping removal inside its work root."""
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


class _LimitedFileWriter(_Py7zIO):
    """A file-backed py7zr writer that reserves bytes before every disk write."""

    def __init__(self, path: Path, budget: _ExpansionBudget) -> None:
        self._path = path
        self._budget = budget
        self._output: BinaryIO | None = None
        self.written = 0
        self._lock = Lock()

    def write(self, data: bytes | bytearray) -> int:
        with self._lock:
            self._budget.consume_bytes(len(data))
            if self._output is None:
                self._output = self._path.open("w+b")
            self._output.seek(0, 2)
            written = self._output.write(data)
            self._output.flush()
            self.written += written
            return written

    def read(self, size: int | None = None) -> bytes:
        with self._lock:
            if self._output is None:
                return b""
            self._output.flush()
            return self._output.read(-1 if size is None else size)

    def seek(self, offset: int, whence: int = 0) -> int:
        with self._lock:
            if self._output is None:
                return 0
            return self._output.seek(offset, whence)

    def flush(self) -> None:
        if self._output is not None:
            self._output.flush()

    def size(self) -> int:
        return self.written

    def close(self) -> None:
        if self._output is not None:
            self._output.close()
            self._output = None


class _LimitedWriterFactory(_WriterFactory):
    """Create exactly one path-validated streaming writer for a 7z member."""

    def __init__(self, expected_member: str, output: Path, budget: _ExpansionBudget) -> None:
        self._expected_member = expected_member.replace("\\", "/")
        self._output = output
        self._budget = budget
        self.writer: _LimitedFileWriter | None = None

    def create(self, filename: str) -> _LimitedFileWriter:
        supplied_path = Path(filename)
        expected_path = self._output.resolve()
        matches_member = filename.replace("\\", "/") == self._expected_member
        matches_target = supplied_path.is_absolute() and supplied_path.resolve() == expected_path
        if (not matches_member and not matches_target) or self.writer is not None:
            raise _UnsafeArchiveMemberError(f"unexpected 7z archive member: {filename}")
        self.writer = _LimitedFileWriter(self._output, self._budget)
        return self.writer

    def close(self) -> None:
        if self.writer is not None:
            self.writer.close()


def _expand_zip(path: Path, root: Path, budget: _ExpansionBudget) -> list[Path]:
    outputs: list[Path] = []
    destinations: set[str] = set()
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            budget.consume_entry()
            if info.is_dir():
                continue
            if info.file_size > budget.remaining_bytes:
                raise _ArchiveLimitError("archive expansion limit exceeded")
            output = _target(root, info.filename, destinations)
            with archive.open(info) as source:
                written = _copy_limited(source, output, budget.remaining_bytes)
            budget.consume_bytes(written)
            outputs.append(output)
    return outputs


def _expand_tar(path: Path, root: Path, budget: _ExpansionBudget) -> list[Path]:
    outputs: list[Path] = []
    destinations: set[str] = set()
    with tarfile.open(path) as archive:
        for info in archive:
            budget.consume_entry()
            if not info.isfile():
                continue
            if info.size > budget.remaining_bytes:
                raise _ArchiveLimitError("archive expansion limit exceeded")
            source = archive.extractfile(info)
            if source is None:
                continue
            output = _target(root, info.name, destinations)
            with source:
                written = _copy_limited(source, output, budget.remaining_bytes)
            budget.consume_bytes(written)
            outputs.append(output)
    return outputs


def _expand_7z(path: Path, root: Path, budget: _ExpansionBudget) -> list[Path]:
    if py7zr is None:
        raise RuntimeError("py7zr is not installed")
    with py7zr.SevenZipFile(path) as archive:
        destinations: set[str] = set()
        outputs: list[Path] = []
        for entry in archive.list():
            budget.consume_entry()
            if entry.is_directory:
                continue
            declared_size = entry.uncompressed
            if not isinstance(declared_size, int) or declared_size < 0 or declared_size > budget.remaining_bytes:
                raise _ArchiveLimitError("archive expansion limit exceeded")
            output = _target(root, entry.filename, destinations)
            factory = _LimitedWriterFactory(entry.filename, output, budget)
            try:
                archive.extract(path=root, targets=[entry.filename], factory=factory)
                factory.close()
                _target(root, entry.filename)
                actual_size = _materialized_size(output)
            except Exception:
                factory.close()
                _remove_failed_output(output)
                raise
            if factory.writer is None or not output.is_file() or actual_size != factory.writer.written or actual_size > declared_size:
                _remove_failed_output(output)
                raise _ArchiveLimitError("archive expansion limit exceeded")
            outputs.append(_target(root, entry.filename))
    return outputs


def _rar_member_is_link(info: Any) -> bool:
    checker = getattr(info, "is_symlink", None)
    if callable(checker) and checker():
        return True
    return getattr(info, "file_redir", None) is not None


def _expand_rar(path: Path, root: Path, budget: _ExpansionBudget) -> list[Path]:
    """Stream path-validated RAR members through the shared expansion budget."""
    if rarfile is None:
        raise RuntimeError("rarfile is not installed")
    configure_native_environment()
    destinations: set[str] = set()
    outputs: list[Path] = []
    with rarfile.RarFile(path) as archive:
        for info in archive.infolist():
            budget.consume_entry()
            if info.isdir():
                continue
            if _rar_member_is_link(info):
                raise _UnsafeArchiveMemberError(
                    f"unsafe archive member: {info.filename}"
                )
            declared_size = getattr(info, "file_size", None)
            if (
                not isinstance(declared_size, int)
                or declared_size < 0
                or declared_size > budget.remaining_bytes
            ):
                raise _ArchiveLimitError("archive expansion limit exceeded")
            output = _target(root, info.filename, destinations)
            try:
                with archive.open(info) as source:
                    written = _copy_limited(
                        source,
                        output,
                        min(declared_size, budget.remaining_bytes),
                    )
                if written != declared_size:
                    raise ValueError("RAR member size does not match its metadata")
            except Exception:
                _remove_failed_output(output)
                raise
            budget.consume_bytes(written)
            outputs.append(output)
    return outputs


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(_COPY_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _is_within(path: Path, directory: Path) -> bool:
    resolved_path = path.resolve()
    resolved_directory = directory.resolve()
    return resolved_path == resolved_directory or resolved_directory in resolved_path.parents


def _source_files(path: Path, work_dir: Path) -> tuple[list[Path], list[Path]]:
    files: list[Path] = []
    symlinks: list[Path] = []
    for candidate in path.rglob("*"):
        if candidate.is_symlink():
            symlinks.append(candidate)
        elif candidate.is_file() and not _is_within(candidate, work_dir):
            files.append(candidate.resolve())
    sort_key = lambda candidate: candidate.as_posix().lower()
    return sorted(files, key=sort_key), sorted(symlinks, key=sort_key)


def _media_type(path: Path) -> str:
    return _MEDIA_TYPES.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def discover_inputs(paths: Sequence[Path], work_dir: Path) -> tuple[list[SourceFile], list[WarningItem]]:
    """Discover supported inputs, safely expand archives, and deduplicate by SHA-256."""
    work_dir.mkdir(parents=True, exist_ok=True)
    work_dir = work_dir.resolve()
    budget = _ExpansionBudget(remaining_entries=MAX_FILES, remaining_bytes=MAX_BYTES)
    queue: list[tuple[Path, str | None, int]] = []
    warnings: list[WarningItem] = []
    for item in paths:
        if item.is_symlink():
            warnings.append(
                _warning(
                    "unsafe_symlink",
                    item.absolute(),
                    "symbolic links are not followed",
                )
            )
            continue
        resolved = item.resolve()
        if not resolved.exists():
            warnings.append(_warning("missing_input", resolved, "input path does not exist"))
            continue
        if resolved.is_dir():
            source_files, symlinks = _source_files(resolved, work_dir)
            queue.extend((source, None, 0) for source in source_files)
            warnings.extend(
                _warning(
                    "unsafe_symlink",
                    symlink.absolute(),
                    "symbolic links are not followed",
                )
                for symlink in symlinks
            )
        elif resolved.is_file():
            queue.append((resolved, None, 0))
        else:
            warnings.append(_warning("unsupported_format", resolved, "input is not a regular file or directory"))

    materialized: list[tuple[Path, str | None]] = []
    index = 0
    while index < len(queue):
        path, member, depth = queue[index]
        index += 1
        kind = _archive_kind(path)
        if kind is None:
            if path.suffix.lower() in SUPPORTED:
                materialized.append((path, member))
            else:
                warnings.append(_warning("unsupported_format", path, "format is not supported"))
            continue
        if depth >= MAX_DEPTH:
            warnings.append(_warning("archive_depth_exceeded", path, "nested archive depth exceeded"))
            continue

        destination = Path(mkdtemp(prefix="archive-", dir=work_dir))
        try:
            expanded = {
                "zip": _expand_zip,
                "tar": _expand_tar,
                "7z": _expand_7z,
                "rar": _expand_rar,
            }[kind](path, destination, budget)
        except _UnsafeArchiveMemberError as error:
            warnings.append(_warning("unsafe_archive_member", path, str(error)))
        except _ArchiveLimitError as error:
            warnings.append(_warning("archive_limit", path, str(error)))
        except Exception as error:  # noqa: BLE001 - archive backends expose disparate error types.
            warnings.append(_warning("archive_failed", path, type(error).__name__))
        else:
            queue.extend(
                (expanded_path, expanded_path.relative_to(destination).as_posix(), depth + 1)
                for expanded_path in expanded
            )

    seen: dict[str, SourceFile] = {}
    for path, member in materialized:
        digest = _hash_file(path)
        if digest not in seen:
            metadata: tuple[tuple[str, str], ...] = ()
            if path.suffix.lower() == ".xml":
                try:
                    metadata = tuple(parse_voucher_xml(path).items())
                except ValueError as error:
                    warnings.append(_warning("xml_metadata_invalid", path, str(error)))
            seen[digest] = SourceFile(
                id=digest[:16],
                path=path,
                sha256=digest,
                media_type=_media_type(path),
                archive_member=member,
                metadata=metadata,
            )
    return list(seen.values()), warnings
