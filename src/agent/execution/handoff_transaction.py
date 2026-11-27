"""Serialize handoff readers/writers and restore the previously signed bundle on a failed finish."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from uuid import uuid4

import portalocker


LOCK_FILE = ".sim-inspection.lock"
_JOURNAL = ".sim-finish-rollback"


def _paths(run, candidate):
    if candidate != "seed" and not re.fullmatch(r"candidate_[0-9]+", candidate):
        raise ValueError("Invalid handoff candidate")
    folder = run / candidate
    if not folder.is_dir() or folder.is_symlink() or folder.resolve().parent != run.resolve():
        raise ValueError("Invalid handoff candidate directory")
    paths = [run / name for name in ("delivery.json", "delivery.html", "delivery_selection.json")]
    return paths + [folder / name for name in (
        "viewer.html", "sim_inspection.json", "sim_inspection_draft.json", "mesh_review.json")]


def _retire(journal):
    # A crash during cleanup cannot turn a committed handoff into a pending rollback.
    trash = journal.with_name(".sim-finish-completed-" + uuid4().hex)
    journal.rename(trash)
    shutil.rmtree(trash, ignore_errors=True)


def _restore(run):
    journal = run / _JOURNAL
    if not journal.exists():
        return
    record = json.loads((journal / "previous.json").read_bytes())
    for index, path in enumerate(_paths(run, record["candidate"])):
        if index not in record["saved"]:
            path.unlink(missing_ok=True)
            continue
        # Keep the backup intact until every member has been restored. Recovery
        # is repeatable even after another process interruption during rollback.
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            staged = Path(stream.name)
        try:
            shutil.copyfile(journal / str(index), staged)
            os.replace(staged, path)
        finally:
            staged.unlink(missing_ok=True)
    _retire(journal)


@contextmanager
def handoff_writer(run):
    with portalocker.Lock(run / LOCK_FILE, timeout=120):
        _restore(run)
        yield


@contextmanager
def handoff_reader(run):
    lock = run / LOCK_FILE
    if not lock.exists():
        # Historical read-only workspaces have no writer or lock file.
        yield
        return
    with portalocker.Lock(lock, mode="rb", timeout=120, flags=portalocker.LOCK_SH | portalocker.LOCK_NB):
        if (run / _JOURNAL).exists():
            raise ValueError("Interrupted finish requires recovery before reading this handoff")
        yield


@contextmanager
def finish_transaction(run, candidate):
    """Durably save only files this finish may replace. All consumers validate source and viewer digests."""
    with handoff_writer(run):
        paths = _paths(run, candidate)
        temporary = run / (".sim-finish-prepare-" + uuid4().hex)
        temporary.mkdir()
        journal = run / _JOURNAL
        try:
            saved = []
            for index, path in enumerate(paths):
                if path.exists():
                    shutil.copyfile(path, temporary / str(index))
                    saved.append(index)
            (temporary / "previous.json").write_text(json.dumps({"candidate": candidate, "saved": saved}))
            temporary.rename(journal)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        try:
            yield
        except BaseException:
            _restore(run)
            raise
        else:
            _retire(journal)
