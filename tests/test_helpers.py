"""Unit tests for helpers: ManifestLock, log_provenance, _checksum, update_schema.

Author: Samuel Ahuno (ekwame001@gmail.com)
Date: 2026-04-15
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from multiprocessing import Process, Barrier
from pathlib import Path

import pytest

import casetrack


# ── _checksum ──────────────────────────────────────────────────────────────────

def test_checksum_matches_md5(tmp_path: Path):
    f = tmp_path / "f.bin"
    payload = b"hello world" * 1000
    f.write_bytes(payload)
    assert casetrack._checksum(str(f)) == hashlib.md5(payload).hexdigest()


def test_checksum_differs_when_content_changes(tmp_path: Path):
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("alpha")
    b.write_text("beta")
    assert casetrack._checksum(str(a)) != casetrack._checksum(str(b))


# ── log_provenance ─────────────────────────────────────────────────────────────

def test_log_provenance_appends_jsonl(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("USER", "tester")
    monkeypatch.setenv("HOSTNAME", "test-host")
    monkeypatch.delenv("SLURM_JOB_ID", raising=False)
    monkeypatch.delenv("SLURM_ARRAY_TASK_ID", raising=False)

    manifest = tmp_path / "manifest.tsv"
    casetrack.log_provenance(str(manifest), {"action": "init", "n_samples": 3})
    casetrack.log_provenance(str(manifest), {"action": "append", "analysis": "modkit"})

    log_path = str(manifest) + casetrack.PROVENANCE_SUFFIX
    assert os.path.exists(log_path)

    lines = Path(log_path).read_text().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    second = json.loads(lines[1])
    assert first["action"] == "init"
    assert first["user"] == "tester"
    assert first["hostname"] == "test-host"
    assert first["slurm_job_id"] is None
    assert first["slurm_array_task_id"] is None
    assert "timestamp" in first
    assert second["analysis"] == "modkit"


def test_log_provenance_captures_slurm_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("USER", "tester")
    monkeypatch.setenv("SLURM_JOB_ID", "123456")
    monkeypatch.setenv("SLURM_ARRAY_TASK_ID", "7")

    manifest = tmp_path / "manifest.tsv"
    casetrack.log_provenance(str(manifest), {"action": "append"})
    entry = json.loads(Path(str(manifest) + casetrack.PROVENANCE_SUFFIX).read_text().strip())
    assert entry["slurm_job_id"] == "123456"
    assert entry["slurm_array_task_id"] == "7"


# ── update_schema ──────────────────────────────────────────────────────────────

def test_update_schema_creates_and_updates(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("USER", "schema-user")
    manifest = tmp_path / "manifest.tsv"

    casetrack.update_schema(str(manifest), "modkit", ["modkit_mean", "modkit_done"])
    schema_path = str(manifest) + casetrack.SCHEMA_SUFFIX
    assert os.path.exists(schema_path)

    schema = json.loads(Path(schema_path).read_text())
    assert schema["modkit"]["columns"] == ["modkit_mean", "modkit_done"]
    assert schema["modkit"]["added_by"] == "schema-user"
    assert "added" in schema["modkit"]

    # A second analysis should coexist; not clobber the first.
    casetrack.update_schema(str(manifest), "tldr", ["tldr_count", "tldr_done"])
    schema = json.loads(Path(schema_path).read_text())
    assert set(schema) == {"modkit", "tldr"}
    assert schema["tldr"]["columns"] == ["tldr_count", "tldr_done"]


def test_update_schema_replaces_same_analysis(tmp_path: Path):
    manifest = tmp_path / "manifest.tsv"
    casetrack.update_schema(str(manifest), "modkit", ["a", "modkit_done"])
    casetrack.update_schema(str(manifest), "modkit", ["b", "c", "modkit_done"])
    schema = json.loads(Path(str(manifest) + casetrack.SCHEMA_SUFFIX).read_text())
    assert schema["modkit"]["columns"] == ["b", "c", "modkit_done"]


# ── ManifestLock ───────────────────────────────────────────────────────────────

def test_manifest_lock_creates_lockfile(tmp_path: Path):
    manifest = tmp_path / "manifest.tsv"
    manifest.touch()
    with casetrack.ManifestLock(str(manifest)):
        assert (tmp_path / "manifest.tsv.lock").exists()
    # Lock file remains (fcntl releases the advisory lock); that is expected.
    assert (tmp_path / "manifest.tsv.lock").exists()


def _hold_lock_worker(lock_path: str, barrier_ready: str, release_flag: str, hold_seconds: float):
    """Acquire lock, signal ready, spin until release flag appears."""
    with casetrack.ManifestLock(lock_path):
        Path(barrier_ready).touch()
        start = time.time()
        while not Path(release_flag).exists() and time.time() - start < hold_seconds:
            time.sleep(0.02)


def test_manifest_lock_blocks_concurrent_acquire(tmp_path: Path):
    """Second acquisition must wait for the first to release."""
    manifest = tmp_path / "manifest.tsv"
    manifest.touch()
    ready = tmp_path / "holder_ready"
    release = tmp_path / "release_now"

    holder = Process(target=_hold_lock_worker, args=(str(manifest), str(ready), str(release), 5.0))
    holder.start()
    try:
        # Wait until the child process has the lock.
        for _ in range(200):
            if ready.exists():
                break
            time.sleep(0.01)
        assert ready.exists(), "lock holder never signalled"

        # Try to acquire non-blockingly: must fail while holder has it.
        import fcntl
        f = open(str(manifest) + casetrack.LOCK_SUFFIX, "w")
        with pytest.raises(BlockingIOError):
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        f.close()

        # Tell holder to release.
        release.touch()
        holder.join(timeout=5)
        assert not holder.is_alive()

        # Now acquisition should succeed.
        with casetrack.ManifestLock(str(manifest)):
            pass
    finally:
        if holder.is_alive():
            holder.terminate()
            holder.join()
