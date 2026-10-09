"""Verified local backups. Stop writers before backup; restore to a new directory."""

import argparse
import hashlib
import json
import shutil
import tarfile
import tempfile
from contextlib import ExitStack
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any


def checksum(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def manifest_path(archive: Path) -> Path:
    return archive.with_suffix(archive.suffix + ".manifest.json")


def verify(archive: Path) -> dict[str, Any]:
    manifest: dict[str, Any] = json.loads(manifest_path(archive).read_text())
    if checksum(archive) != manifest["archive_sha256"]:
        raise ValueError("Backup archive checksum mismatch")
    seen = set()
    with tarfile.open(archive, "r:gz") as bundle:
        for member in bundle.getmembers():
            name = PurePosixPath(member.name)
            if (
                not member.isfile()
                or name.is_absolute()
                or PureWindowsPath(member.name).drive
                or "\\" in member.name
                or ":" in member.name
                or ".." in name.parts
                or any(
                    PureWindowsPath(part).is_reserved() or part.endswith((".", " "))
                    for part in name.parts
                )
                or member.name in seen
                or member.name not in manifest["files"]
            ):
                raise ValueError("Unsafe or unexpected backup member")
            seen.add(member.name)
            stream = bundle.extractfile(member)
            if stream is None or (
                hashlib.file_digest(stream, "sha256").hexdigest() != manifest["files"][member.name]
            ):
                raise ValueError("Backup content verification failed")
    if seen != set(manifest["files"]):
        raise ValueError("Backup file inventory mismatch")
    return manifest


def backup(root: Path, destination: Path) -> dict[str, Any]:
    # Only backup creation needs the POSIX application locks. Verification and
    # restore use the standard library and can run on Windows without installing xasset.
    from xasset.store.writer import atomic_path, write_json, writer_lock

    root, destination = root.resolve(), destination.resolve()
    if not root.is_dir():
        raise ValueError("Data directory does not exist")
    if (
        destination.is_relative_to(root)
        or destination.exists()
        or manifest_path(destination).exists()
    ):
        raise ValueError("Choose a new backup path outside the data directory")
    destination.parent.mkdir(parents=True, exist_ok=True)
    hashes = {}
    with ExitStack() as locks:
        # Fail if the monitor or a map is active, and block file/database publication.
        locks.enter_context(writer_lock(root / "monitor/daemon"))
        for path in sorted((root / "maps").glob("*/registration.json")):
            locks.enter_context(writer_lock(path.parent))
        locks.enter_context(writer_lock(root / "live"))
        locks.enter_context(writer_lock(root))
        entries = sorted(root.rglob("*"))
        if any(path.is_symlink() for path in entries):
            raise ValueError("Refuse symlinks in the data backup")
        paths = [
            path
            for path in entries
            if path.is_file()
            and path.name != ".writer.lock"
            and not path.name.startswith(".pending-")
        ]
        with atomic_path(destination) as temporary:
            with tarfile.open(temporary, "w:gz") as archive:
                for path in paths:
                    relative = str(path.relative_to(root))
                    hashes[relative] = checksum(path)
                    archive.add(path, arcname=relative, recursive=False)
    manifest = {"files": hashes, "archive_sha256": checksum(destination)}
    write_json(manifest_path(destination), manifest)
    verify(destination)
    return {
        "path": str(destination),
        "files": len(hashes),
        "verified": True,
        "archive_sha256": manifest["archive_sha256"],
    }


def restore(archive: Path, destination: Path) -> dict[str, Any]:
    if destination.exists():
        raise ValueError("Restore requires a new empty destination path")
    manifest = verify(archive)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".restore-", dir=destination.parent) as temporary:
        root = Path(temporary) / "data"
        root.mkdir()
        with tarfile.open(archive, "r:gz") as bundle:
            for member in bundle.getmembers():
                target = root / member.name
                target.parent.mkdir(parents=True, exist_ok=True)
                stream = bundle.extractfile(member)
                assert stream is not None
                with target.open("wb") as output:
                    shutil.copyfileobj(stream, output)
                if checksum(target) != manifest["files"][member.name]:
                    raise ValueError("Restored file checksum mismatch")
        root.rename(destination)
    return {"path": str(destination), "files": len(manifest["files"]), "verified": True}


def restore_checkpoint(checkpoint: Path, destination: Path) -> dict[str, Any]:
    """Reassemble and verify a checked-in checkpoint without shell-specific commands."""
    if destination.exists():
        raise ValueError("Restore requires a new empty destination path")
    manifests = list(checkpoint.glob("*.tar.gz.manifest.json"))
    if len(manifests) != 1:
        raise ValueError("Expected exactly one backup manifest in the checkpoint")
    archive_name = manifests[0].name.removesuffix(".manifest.json")
    hashes = {}
    for line in (checkpoint / "SHA256SUMS").read_text().splitlines():
        expected, name = line.split("  ", 1)
        if name.startswith(archive_name + ".part-"):
            if Path(name).name != name or "\\" in name or ":" in name:
                raise ValueError("Invalid checkpoint part name")
            hashes[name] = expected
    names = sorted(hashes)
    if not names or names != [f"{archive_name}.part-{i:02d}" for i in range(len(names))]:
        raise ValueError("Checkpoint parts must be numbered consecutively from zero")
    with tempfile.TemporaryDirectory(prefix="xasset-checkpoint-") as temporary:
        archive = Path(temporary) / archive_name
        with archive.open("wb") as output:
            for name in names:
                digest = hashlib.sha256()
                with (checkpoint / name).open("rb") as stream:
                    while chunk := stream.read(1024 * 1024):
                        digest.update(chunk)
                        output.write(chunk)
                if digest.hexdigest() != hashes[name]:
                    raise ValueError(f"Checkpoint part checksum mismatch: {name}")
        shutil.copyfile(manifests[0], manifest_path(archive))
        return restore(archive, destination)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--data-dir", type=Path, default=Path("data"))
    create.add_argument("archive", type=Path)
    validate = commands.add_parser("verify")
    validate.add_argument("archive", type=Path)
    recover = commands.add_parser("restore")
    recover.add_argument("archive", type=Path)
    recover.add_argument("destination", type=Path)
    checkpoint = commands.add_parser("restore-checkpoint")
    checkpoint.add_argument("checkpoint", type=Path)
    checkpoint.add_argument("destination", type=Path)
    args = parser.parse_args()
    if args.command == "create":
        result = backup(args.data_dir, args.archive)
    elif args.command == "restore":
        result = restore(args.archive, args.destination)
    elif args.command == "restore-checkpoint":
        result = restore_checkpoint(args.checkpoint, args.destination)
    else:
        result = {"verified": bool(verify(args.archive))}
    print(json.dumps(result, indent=2))
