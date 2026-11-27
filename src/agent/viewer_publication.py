"""Publish the selected interactive model as one movable HTML file."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import sys
import tempfile


VIEWER_FILENAME = "Sim-BIM.html"


def _install_new(pending: Path, target: Path) -> None:
    if os.name == "nt":
        # Windows rename fails on an existing destination, including on exFAT.
        pending.rename(target)
        return
    try:
        os.link(pending, target)
    except FileExistsError:
        raise
    except OSError:
        if not sys.platform.startswith("linux"):
            raise
        # FAT and some network mounts do not implement hard links.
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        rename = getattr(libc, "renameat2", None)
        if rename is None:
            raise
        at_fdcwd, rename_noreplace = -100, 1
        if rename(at_fdcwd, os.fsencode(pending), at_fdcwd, os.fsencode(target), rename_noreplace):
            err = ctypes.get_errno()
            raise OSError(err, os.strerror(err), target)


def publish_viewer(run: Path, candidate: str, directory: Path, *, expected_sha256: str | None = None) -> Path:
    if not isinstance(candidate, str) or not re.fullmatch(r"candidate_[0-9]+", candidate):
        raise ValueError("Invalid delivered candidate")
    run = run.resolve()
    source = (run / candidate / "viewer.html").resolve()
    if not source.is_relative_to(run):
        raise ValueError("The selected viewer is outside the BIM run")
    directory = directory.resolve()
    target = directory / VIEWER_FILENAME
    pending = None
    try:
        digest = hashlib.sha256()
        with source.open("rb") as reader, tempfile.NamedTemporaryFile(
                mode="wb", prefix=".sim-bim-", dir=directory, delete=False) as writer:
            pending = Path(writer.name)
            while chunk := reader.read(1024 * 1024):
                writer.write(chunk)
                digest.update(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        if expected_sha256 is not None and digest.hexdigest() != expected_sha256:
            raise ValueError("The selected viewer changed after finish")
        try:
            _install_new(pending, target)
        except FileExistsError:
            if target.is_symlink() or not target.is_file():
                raise ValueError("The public viewer path must be a regular file") from None
            with target.open("rb") as existing:
                if hashlib.file_digest(existing, "sha256").digest() == digest.digest():
                    return target
            raise FileExistsError(f"The public viewer already exists with different contents: {target}") from None
        return target
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)


def publish_attested_viewer(run: Path, proof: dict, directory: Path) -> Path:
    if not isinstance(proof, dict) or proof.get("origin") != "domain_tool" or proof.get("tool_name") != "finish_bim":
        raise ValueError("The run has no completed model handoff")
    candidate = proof.get("candidate")
    expected = proof.get("artifact_sha256", {}).get(f"{candidate}/viewer.html")
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("The finished viewer has no bound digest")
    from src.agent.execution.handoff_transaction import handoff_reader
    with handoff_reader(run):
        return publish_viewer(run, candidate, directory, expected_sha256=expected)
