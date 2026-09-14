#!/usr/bin/python3
"""Validate and extract the one reviewed Hermes GitHub source export."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import sys
import tarfile


COMMIT = "29112bef099274229cadff79cdff7bf7b99c4b77"
GIT_TREE_OID = "daaffc303ae437041b7f76be17c5f61b14f2ce99"
ARCHIVE_SHA256 = "76b99a8be9b77d66833c3cfe2b35c6d6f6a58e4ff9637ef8effcfc1f420ab35a"
CANONICAL_TREE_SHA256 = "7ecc4e3f45d65d6ab33907f9220c06daa66b118af2a0fbbf5df3f2e019776ad1"
TOP_LEVEL = f"hermes-agent-{COMMIT}"
COMPRESSED_SIZE = 69_442_866
EXPANDED_SIZE = 167_028_103
MEMBERS = 11_994
REGULAR_FILES = 10_925
DIRECTORIES = 1_069
MAX_FILE_SIZE = 3_871_968
DIRECTORY_TREE_SHA256 = "52e732aac8179dc8f2b00aa0c19662ed328b277afc7c43acd11482cdbb129508"
EXPECTED_PAX = {"comment": COMMIT}
CRITICAL_FILES = {
    "pyproject.toml": (
        34_002,
        "c70c8b52f6cc08a4e65f0fc1713c26814fd4f19811bc7e01de645009b2a76600",
    ),
    "scripts/install.sh": (
        167_838,
        "85ef536d455e51ab67aa74d79272efd49fe717597dbaadfd3cca179a905f4706",
    ),
    "uv.lock": (
        722_856,
        "383cd8f98ec23dc3fe4cf63759ec73be5a869cc953f068b4e79ec4e8ed00287d",
    ),
}
RECEIPT_NAME = ".atlas-source.json"


class ArchiveError(RuntimeError):
    pass


def fail(message: str) -> None:
    raise ArchiveError(message)


def stable_fields(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def open_parent(root_fd: int, parts: tuple[str, ...]) -> int:
    descriptor = os.dup(root_fd)
    try:
        for part in parts:
            child = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            fail("an extracted file could not be written")
        view = view[written:]


def canonical_tree_digest(
    entries: list[tuple[PurePosixPath, int, int, str]],
) -> str:
    digest = hashlib.sha256()
    for path, mode, size, file_sha256 in sorted(
        entries, key=lambda item: item[0].as_posix()
    ):
        digest.update(
            f"{path.as_posix()}\0{mode:o}\0{size}\0{file_sha256}\n".encode(
                "utf-8"
            )
        )
    return digest.hexdigest()


def directory_tree_digest(paths: list[PurePosixPath]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: item.as_posix()):
        digest.update(f"{path.as_posix()}\n".encode("utf-8"))
    return digest.hexdigest()


def read_regular_at(
    parent_fd: int,
    name: str,
    expected_owner: tuple[int, int],
    *,
    maximum: int,
) -> tuple[bytes, int, str]:
    before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    descriptor = os.open(
        name,
        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        dir_fd=parent_fd,
    )
    try:
        opened = os.fstat(descriptor)
        mode = stat.S_IMODE(opened.st_mode)
        if (
            not stat.S_ISREG(opened.st_mode)
            or stable_fields(before) != stable_fields(opened)
            or opened.st_nlink != 1
            or (opened.st_uid, opened.st_gid) != expected_owner
            or mode not in {0o640, 0o644, 0o750, 0o755}
            or opened.st_size > maximum
        ):
            fail("an installed Hermes file has unsafe metadata")
        chunks: list[bytes] = []
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, maximum + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            digest.update(chunk)
            total += len(chunk)
            if total > maximum:
                fail("an installed Hermes file exceeds its bound")
        after = os.fstat(descriptor)
        if total != opened.st_size or stable_fields(after) != stable_fields(opened):
            fail("an installed Hermes file changed while verified")
        return b"".join(chunks), mode, digest.hexdigest()
    finally:
        os.close(descriptor)


def validate_installed_component(name: str) -> None:
    encoded = name.encode("utf-8", errors="strict")
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or len(encoded) > 255
        or any(byte < 32 or byte == 127 for byte in encoded)
    ):
        fail("an installed Hermes path component is unsafe")


def verify_generated_egg_info(parent_fd: int, expected_owner: tuple[int, int]) -> None:
    """Bound non-executable setuptools metadata separately from the source inventory."""
    descriptor = os.open("hermes_agent.egg-info", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=parent_fd)
    try:
        before = os.fstat(descriptor)
        if (before.st_uid, before.st_gid) != expected_owner or stat.S_IMODE(before.st_mode) not in {0o700, 0o750, 0o755}:
            fail("the generated Hermes package metadata directory is unsafe")
        expected = {"PKG-INFO", "dependency_links.txt", "entry_points.txt", "requires.txt", "top_level.txt", "SOURCES.txt"}
        if {entry.name for entry in os.scandir(descriptor)} != expected:
            fail("the generated Hermes package metadata inventory differs")
        for name in sorted(expected):
            raw, mode, _digest = read_regular_at(descriptor, name, expected_owner, maximum=256 * 1024)
            if mode & 0o111:
                fail("generated Hermes package metadata is executable")
            if name == "PKG-INFO" and (b"Name: hermes-agent" not in raw.splitlines() or b"Version: 0.21.0" not in raw.splitlines()):
                fail("the generated Hermes package identity differs")
        if stable_fields(os.fstat(descriptor)) != stable_fields(before):
            fail("generated Hermes package metadata changed while verified")
    finally:
        os.close(descriptor)


def verify_installed(destination: Path) -> None:
    try:
        before_root = os.lstat(destination)
        root_fd = os.open(
            destination,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
    except OSError as exc:
        raise ArchiveError("the installed Hermes tree is unavailable") from exc
    try:
        root_info = os.fstat(root_fd)
        root_mode = stat.S_IMODE(root_info.st_mode)
        if (
            not stat.S_ISDIR(root_info.st_mode)
            or stat.S_ISLNK(before_root.st_mode)
            or stable_fields(before_root) != stable_fields(root_info)
            or root_mode not in {0o700, 0o750, 0o755}
            or root_mode & 0o022
        ):
            fail("the installed Hermes root metadata is unsafe")
        expected_owner = (root_info.st_uid, root_info.st_gid)
        entries: list[tuple[PurePosixPath, int, int, str]] = []
        directories: list[PurePosixPath] = []
        regular_count = 0
        expanded_size = 0
        maximum_size = 0
        receipt_seen = False

        def walk(directory_fd: int, relative_root: PurePosixPath) -> None:
            nonlocal regular_count, expanded_size, maximum_size, receipt_seen
            before_directory = os.fstat(directory_fd)
            try:
                children = sorted(os.scandir(directory_fd), key=lambda item: item.name)
            except OSError as exc:
                raise ArchiveError("an installed Hermes directory is unreadable") from exc
            for child in children:
                validate_installed_component(child.name)
                relative = relative_root / child.name
                relative_name = relative.as_posix()
                metadata = child.stat(follow_symlinks=False)
                if relative_root == PurePosixPath(".") and child.name == "hermes_agent.egg-info":
                    verify_generated_egg_info(directory_fd, expected_owner)
                    continue
                if relative_root == PurePosixPath(".") and child.name == "venv":
                    if (
                        not stat.S_ISDIR(metadata.st_mode)
                        or child.is_symlink()
                        or (metadata.st_uid, metadata.st_gid) != expected_owner
                        or stat.S_IMODE(metadata.st_mode) not in {0o700, 0o750, 0o755}
                        or stat.S_IMODE(metadata.st_mode) & 0o022
                    ):
                        fail("the managed Hermes virtual environment is unsafe")
                    continue
                if relative_root == PurePosixPath(".") and child.name in {
                    RECEIPT_NAME,
                    ".install_method",
                }:
                    raw, _mode, _digest = read_regular_at(
                        directory_fd,
                        child.name,
                        expected_owner,
                        maximum=4096,
                    )
                    if child.name == RECEIPT_NAME:
                        expected_receipt = json.dumps(
                            {
                                "archive_sha256": ARCHIVE_SHA256,
                                "canonical_tree_sha256": CANONICAL_TREE_SHA256,
                                "commit": COMMIT,
                                "git_tree_oid": GIT_TREE_OID,
                                "schema_version": 1,
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("ascii") + b"\n"
                        if raw != expected_receipt:
                            fail("the Hermes provenance receipt differs from review")
                        receipt_seen = True
                    elif raw != b"unknown\n":
                        fail("the managed Hermes installation method differs from review")
                    continue
                if stat.S_ISDIR(metadata.st_mode) and not child.is_symlink():
                    mode = stat.S_IMODE(metadata.st_mode)
                    if (
                        (metadata.st_uid, metadata.st_gid) != expected_owner
                        or mode not in {0o700, 0o750, 0o755}
                        or mode & 0o022
                    ):
                        fail("an installed Hermes directory has unsafe metadata")
                    directories.append(relative)
                    child_fd = os.open(
                        child.name,
                        os.O_RDONLY
                        | os.O_DIRECTORY
                        | os.O_CLOEXEC
                        | os.O_NOFOLLOW,
                        dir_fd=directory_fd,
                    )
                    try:
                        walk(child_fd, relative)
                    finally:
                        os.close(child_fd)
                    continue
                if not stat.S_ISREG(metadata.st_mode) or child.is_symlink():
                    fail("the installed Hermes tree contains a special file")
                _raw, observed_mode, file_sha256 = read_regular_at(
                    directory_fd,
                    child.name,
                    expected_owner,
                    maximum=MAX_FILE_SIZE,
                )
                size = metadata.st_size
                normalized_mode = 0o755 if observed_mode & 0o111 else 0o644
                regular_count += 1
                expanded_size += size
                maximum_size = max(maximum_size, size)
                if expanded_size > EXPANDED_SIZE:
                    fail("the installed Hermes tree exceeds its size bound")
                entries.append((relative, normalized_mode, size, file_sha256))
            after_directory = os.fstat(directory_fd)
            if stable_fields(after_directory) != stable_fields(before_directory):
                fail("an installed Hermes directory changed while verified")

        walk(root_fd, PurePosixPath("."))
        if (
            not receipt_seen
            or regular_count != REGULAR_FILES
            or len(directories) != DIRECTORIES - 1
            or expanded_size != EXPANDED_SIZE
            or maximum_size != MAX_FILE_SIZE
            or canonical_tree_digest(entries) != CANONICAL_TREE_SHA256
            or directory_tree_digest(directories) != DIRECTORY_TREE_SHA256
        ):
            fail("the installed Hermes source inventory differs from review")
        after_root = os.fstat(root_fd)
        after_path = os.lstat(destination)
        if (
            stable_fields(after_root) != stable_fields(root_info)
            or stable_fields(after_path) != stable_fields(root_info)
        ):
            fail("the installed Hermes tree changed while verified")
    finally:
        os.close(root_fd)


def validate_destination(destination: Path) -> int:
    try:
        before = os.lstat(destination)
        descriptor = os.open(
            destination,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        opened = os.fstat(descriptor)
    except OSError as exc:
        fail(f"the extraction destination is unavailable: {exc}")
    if (
        not stat.S_ISDIR(opened.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or before.st_dev != opened.st_dev
        or before.st_ino != opened.st_ino
        or opened.st_uid != 0
        or opened.st_gid != 0
        or stat.S_IMODE(opened.st_mode) != 0o700
    ):
        os.close(descriptor)
        fail("the extraction destination metadata is unsafe")
    try:
        if os.listdir(descriptor):
            os.close(descriptor)
            fail("the extraction destination is not empty")
    except BaseException:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise
    return descriptor


def validate_member_name(name: str, seen: set[str]) -> PurePosixPath:
    try:
        encoded = name.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ArchiveError("an archive path is not valid UTF-8") from exc
    path = PurePosixPath(name)
    if (
        not name
        or len(encoded) > 4096
        or name in seen
        or path.is_absolute()
        or path.as_posix() != name
        or "\\" in name
        or any(part in {"", ".", ".."} for part in path.parts)
        or any(len(part.encode("utf-8")) > 255 for part in path.parts)
        or any(byte < 32 or byte == 127 for byte in encoded)
    ):
        fail("an archive path is unsafe or duplicated")
    seen.add(name)
    return path


def extract(archive_path: Path, destination: Path) -> None:
    try:
        before_path = os.lstat(archive_path)
        archive_fd = os.open(
            archive_path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
        )
    except OSError as exc:
        raise ArchiveError("the vendored archive is unavailable") from exc
    root_fd = -1
    try:
        opened = os.fstat(archive_fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_ISLNK(before_path.st_mode)
            or stable_fields(before_path) != stable_fields(opened)
            or opened.st_uid != 0
            or opened.st_gid != 0
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) != 0o444
            or opened.st_size != COMPRESSED_SIZE
        ):
            fail("the vendored archive metadata differs from review")
        archive_hash = hashlib.sha256()
        while True:
            chunk = os.read(archive_fd, 1024 * 1024)
            if not chunk:
                break
            archive_hash.update(chunk)
        if archive_hash.hexdigest() != ARCHIVE_SHA256:
            fail("the vendored archive checksum differs from review")
        os.lseek(archive_fd, 0, os.SEEK_SET)
        root_fd = validate_destination(destination)

        seen_archive: set[str] = set()
        seen_exported: dict[str, bool] = {}
        entries: list[tuple[PurePosixPath, int, int, str]] = []
        member_count = 0
        regular_count = 0
        directory_count = 0
        expanded_size = 0
        maximum_size = 0
        critical_observed: dict[str, tuple[int, str]] = {}

        with os.fdopen(os.dup(archive_fd), "rb") as archive_stream:
            with tarfile.open(fileobj=archive_stream, mode="r:gz") as archive:
                if archive.pax_headers != EXPECTED_PAX:
                    fail("the archive provenance header differs from review")
                for member in archive:
                    member_count += 1
                    if member_count > MEMBERS:
                        fail("the archive has too many members")
                    path = validate_member_name(member.name, seen_archive)
                    if (
                        member.pax_headers != EXPECTED_PAX
                        or member.uid != 0
                        or member.gid != 0
                        or member.uname != "root"
                        or member.gname != "root"
                        or member.sparse
                    ):
                        fail("an archive member has unsafe provenance or metadata")
                    if member.name == TOP_LEVEL:
                        if not member.isdir() or member.mode != 0o775 or member.size != 0:
                            fail("the archive root differs from review")
                        directory_count += 1
                        continue
                    prefix = f"{TOP_LEVEL}/"
                    if not member.name.startswith(prefix):
                        fail("an archive member is outside the reviewed root")
                    relative = PurePosixPath(member.name.removeprefix(prefix))
                    relative_name = relative.as_posix()
                    if relative_name == RECEIPT_NAME:
                        fail("the archive collides with the managed provenance receipt")
                    ancestors = tuple(
                        PurePosixPath(*relative.parts[:index]).as_posix()
                        for index in range(1, len(relative.parts))
                    )
                    if (
                        relative_name in seen_exported
                        or any(seen_exported.get(parent) is False for parent in ancestors)
                        or any(parent not in seen_exported for parent in ancestors)
                        or (
                            member.isfile()
                            and any(
                                existing.startswith(f"{relative_name}/")
                                for existing in seen_exported
                            )
                        )
                    ):
                        fail("archive paths collide or have a missing parent")
                    parent_fd = open_parent(root_fd, relative.parts[:-1])
                    try:
                        if member.isdir():
                            if member.mode != 0o775 or member.size != 0:
                                fail("an archive directory differs from review")
                            os.mkdir(relative.name, 0o755, dir_fd=parent_fd)
                            seen_exported[relative_name] = True
                            directory_count += 1
                            continue
                        if (
                            not member.isfile()
                            or member.mode not in {0o664, 0o775}
                            or member.size > MAX_FILE_SIZE
                            or expanded_size + member.size > EXPANDED_SIZE
                        ):
                            fail("an archive file is unsupported or oversized")
                        source = archive.extractfile(member)
                        if source is None:
                            fail("an archive file is unreadable")
                        normalized_mode = 0o755 if member.mode & 0o111 else 0o644
                        output_fd = os.open(
                            relative.name,
                            os.O_WRONLY
                            | os.O_CREAT
                            | os.O_EXCL
                            | os.O_CLOEXEC
                            | os.O_NOFOLLOW,
                            normalized_mode,
                            dir_fd=parent_fd,
                        )
                        try:
                            file_hash = hashlib.sha256()
                            observed_size = 0
                            while True:
                                chunk = source.read(1024 * 1024)
                                if not chunk:
                                    break
                                observed_size += len(chunk)
                                if observed_size > member.size:
                                    fail("an archive file exceeds its declared size")
                                file_hash.update(chunk)
                                write_all(output_fd, chunk)
                            if observed_size != member.size:
                                fail("an archive file is truncated")
                            os.fchmod(output_fd, normalized_mode)
                            os.fsync(output_fd)
                        finally:
                            os.close(output_fd)
                        file_sha256 = file_hash.hexdigest()
                        regular_count += 1
                        expanded_size += observed_size
                        maximum_size = max(maximum_size, observed_size)
                        seen_exported[relative_name] = False
                        entries.append(
                            (relative, normalized_mode, observed_size, file_sha256)
                        )
                        if relative_name in CRITICAL_FILES:
                            critical_observed[relative_name] = (
                                observed_size,
                                file_sha256,
                            )
                    finally:
                        os.close(parent_fd)

        if (
            member_count != MEMBERS
            or regular_count != REGULAR_FILES
            or directory_count != DIRECTORIES
            or expanded_size != EXPANDED_SIZE
            or maximum_size != MAX_FILE_SIZE
            or canonical_tree_digest(entries) != CANONICAL_TREE_SHA256
            or critical_observed != CRITICAL_FILES
        ):
            fail("the exported Hermes source tree differs from review")

        receipt = json.dumps(
            {
                "archive_sha256": ARCHIVE_SHA256,
                "canonical_tree_sha256": CANONICAL_TREE_SHA256,
                "commit": COMMIT,
                "git_tree_oid": GIT_TREE_OID,
                "schema_version": 1,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii") + b"\n"
        receipt_fd = os.open(
            RECEIPT_NAME,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o644,
            dir_fd=root_fd,
        )
        try:
            write_all(receipt_fd, receipt)
            os.fchmod(receipt_fd, 0o644)
            os.fsync(receipt_fd)
        finally:
            os.close(receipt_fd)
        for directory_name in sorted(
            (
                name
                for name, is_directory in seen_exported.items()
                if is_directory
            ),
            key=lambda name: len(PurePosixPath(name).parts),
            reverse=True,
        ):
            directory_fd = open_parent(
                root_fd, tuple(PurePosixPath(directory_name).parts)
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        os.fsync(root_fd)

        after_fd = os.fstat(archive_fd)
        after_path = os.lstat(archive_path)
        if (
            stable_fields(after_fd) != stable_fields(opened)
            or stable_fields(after_path) != stable_fields(opened)
        ):
            fail("the vendored archive changed while it was extracted")
    finally:
        if root_fd >= 0:
            os.close(root_fd)
        os.close(archive_fd)


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--verify-installed":
        operation = lambda: verify_installed(Path(sys.argv[2]))
    elif len(sys.argv) == 3:
        operation = lambda: extract(Path(sys.argv[1]), Path(sys.argv[2]))
    else:
        print(
            "usage: extract-hermes-source.py ARCHIVE DESTINATION\n"
            "       extract-hermes-source.py --verify-installed DESTINATION",
            file=sys.stderr,
        )
        return 64
    try:
        operation()
    except (ArchiveError, OSError, tarfile.TarError, EOFError) as exc:
        print(f"Hermes source extraction failed: {exc}", file=sys.stderr)
        return 65
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
