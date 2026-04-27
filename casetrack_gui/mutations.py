"""Run casetrack CLI mutations as subprocesses.

The web layer never executes ``INSERT/UPDATE`` directly. All mutating
actions (censor, uncensor, add-metadata) shell out to the ``casetrack``
console script so the SQLite WAL writer, provenance.jsonl audit, and
qc_events invariants stay single-headed.

Returns a :class:`MutationResult` with stdout/stderr the GUI surfaces in
a flash banner. We do NOT swallow non-zero exits — the caller decides.

Author: Samuel Ahuno (ekwame001@gmail.com)
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass

CASETRACK_BIN_ENV = "CASETRACK_BIN"


@dataclass
class MutationResult:
    ok: bool
    cmd: list[str]
    stdout: str
    stderr: str
    returncode: int


def _resolve_bin() -> str:
    import os
    override = os.environ.get(CASETRACK_BIN_ENV)
    if override:
        return override
    found = shutil.which("casetrack")
    if found:
        return found
    # Fallback: invoke the module via python — works when the package is
    # installed in -e mode but the console_script wasn't refreshed.
    return "python3 -m casetrack"


def run(args: list[str], *, timeout: float = 30.0) -> MutationResult:
    """Run ``casetrack <args...>`` and capture output. Never raises on
    non-zero exit — the GUI shows the failure to the operator instead."""
    bin_cmd = _resolve_bin().split()
    cmd = bin_cmd + list(args)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as e:
        return MutationResult(ok=False, cmd=cmd, stdout="", stderr=str(e), returncode=127)
    except subprocess.TimeoutExpired as e:
        return MutationResult(
            ok=False, cmd=cmd, stdout=e.stdout or "", stderr=f"timeout after {timeout}s",
            returncode=124,
        )
    return MutationResult(
        ok=proc.returncode == 0,
        cmd=cmd,
        stdout=proc.stdout,
        stderr=proc.stderr,
        returncode=proc.returncode,
    )


def censor(project_dir: str, level: str, entity_id: str, kind: str, reason: str) -> MutationResult:
    return run([
        "censor",
        "--project-dir", project_dir,
        "--level", level,
        "--id", entity_id,
        "--kind", kind,
        "--reason", reason,
    ])


def uncensor(
    project_dir: str,
    *,
    event_id: int | None = None,
    level: str | None = None,
    entity_id: str | None = None,
    reason: str,
    ethics_override: bool = False,
) -> MutationResult:
    args = ["uncensor", "--project-dir", project_dir, "--reason", reason]
    if event_id is not None:
        args += ["--event-id", str(event_id)]
    elif level and entity_id:
        args += ["--level", level, "--id", entity_id]
    if ethics_override:
        args += ["--ethics-override", "--yes"]
    return run(args)
