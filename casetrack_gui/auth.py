"""HTTP Basic authentication for the casetrack operator GUI.

Single-operator threat model: the GUI runs on a login node behind an
``ssh -L`` tunnel and is only reachable from the operator's laptop. This
module adds a thin browser-native auth layer so that another user with
shell access on the same login node cannot connect to localhost:8765 and
read or mutate the cohort.

Credentials are kept in memory only — there is no on-disk hash. The
operator passes the password via env (``CASETRACK_GUI_PASSWORD``) or a
file (``--password-file``); the CLI wires it into ``AuthConfig`` and the
app injects a ``Depends`` on every protected route. ``/healthz`` stays
open so liveness probes still work.

Author: Samuel Ahuno (ekwame001@gmail.com)
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials


_security = HTTPBasic(realm="casetrack")


@dataclass(frozen=True)
class AuthConfig:
    """Resolved auth credentials for the running GUI process.

    ``username`` and ``password`` are kept as plaintext in memory only —
    they never touch disk. If you need persistence, set the env vars in
    your shell rc (or a sourced secrets file) and let the CLI resolve
    them at startup.
    """
    username: str
    password: str

    def make_dependency(self):
        # Snapshot expected creds as bytes once, so the closure doesn't
        # re-encode on every request.
        expected_user = self.username.encode("utf-8")
        expected_pass = self.password.encode("utf-8")

        def _verify(creds: HTTPBasicCredentials = Depends(_security)) -> str:
            ok_user = secrets.compare_digest(creds.username.encode("utf-8"), expected_user)
            ok_pass = secrets.compare_digest(creds.password.encode("utf-8"), expected_pass)
            if not (ok_user and ok_pass):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid credentials",
                    headers={"WWW-Authenticate": 'Basic realm="casetrack"'},
                )
            return creds.username

        return _verify
