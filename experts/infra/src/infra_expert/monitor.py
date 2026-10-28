"""Manage one local remote-branch monitor for the Infra dashboard."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from subprocess import Popen
from threading import RLock
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

router = APIRouter(prefix="/monitor")


class ConnectionRequest(BaseModel):
    """A GitHub repository and one explicit branch; credentials stay in Git's credential store."""

    repository: str
    branch: str = "main"  # DEFAULT: user can select any existing branch.

    @field_validator("repository")
    @classmethod
    def validate_repository(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not re.fullmatch(
            r"(?:https://github\.com/|git@github\.com:)[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value
        ):
            raise ValueError("Use https://github.com/owner/repo or git@github.com:owner/repo.git")
        return value

    @field_validator("branch")
    @classmethod
    def validate_branch(cls, value: str) -> str:
        value = value.strip()
        result = subprocess.run(
            ["git", "check-ref-format", f"refs/heads/{value}"], capture_output=True, timeout=10
        )
        if result.returncode or not value or value.startswith("-"):
            raise ValueError("Enter a valid Git branch name")
        return value


class MonitorStatus(BaseModel):
    """Current connection and last successful or failed poll."""

    connection_id: str | None = None
    repository: str | None = None
    branch: str | None = None
    state: Literal["disconnected", "connecting", "connected", "error"] = "disconnected"
    checked_at: datetime | None = None
    error: str | None = None
    interval_seconds: int = 5  # DEFAULT: matches the local monitor polling interval.


class Heartbeat(BaseModel):
    connection_id: str
    state: Literal["connected", "error"]
    error: str | None = Field(default=None, max_length=300)


class MonitorController:
    """Own the monitor process; switching repositories stops the previous process first."""

    def __init__(self) -> None:
        self.process: subprocess.Popen | None = None
        self.status = MonitorStatus()
        self.lock = RLock()

    def stop(self) -> None:
        with self.lock:
            if self.process and self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
            self.process = None
            self.status = MonitorStatus()

    def start(self, request: ConnectionRequest) -> MonitorStatus:
        with self.lock:
            self.stop()
            root = Path(__file__).resolve().parents[4]
            script = root / "scripts" / "infra_monitor.py"
            if not script.is_file():
                raise HTTPException(503, "Remote monitoring requires the local workspace checkout.")
            connection_id = str(uuid4())
            # DEFAULT: this feature runs with one local Infra API worker on its configured port.
            port = int(os.environ.get("EXPERT_PORT", "8104"))
            command = [
                sys.executable,
                str(script),
                "--repo",
                request.repository,
                "--branch",
                request.branch,
                "--connection-id",
                connection_id,
                "--api-url",
                f"http://127.0.0.1:{port}/monitor/predict",
            ]
            try:
                self.process = Popen(
                    command,
                    cwd=root,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
            except OSError as exc:
                raise HTTPException(503, "Could not start the local Git monitor.") from exc
            self.status = MonitorStatus(
                connection_id=connection_id,
                repository=request.repository,
                branch=request.branch,
                state="connecting",
            )
            return self.status

    def current(self) -> MonitorStatus:
        with self.lock:
            if self.process and self.process.poll() is not None:
                self.status.state = "error"
                self.status.error = "Monitor stopped. Check Git installation and reconnect."
            elif (
                self.status.checked_at
                and (datetime.now(UTC) - self.status.checked_at).total_seconds() > 90
            ):
                self.status.state = "error"
                self.status.error = "Monitor has not reported recently. Check the connection."
            return self.status.model_copy()


controller = MonitorController()


@router.post("/connect", response_model=MonitorStatus)
def connect(request: ConnectionRequest) -> MonitorStatus:
    """Replace the current monitor with the selected GitHub repository and branch."""
    return controller.start(request)


@router.post("/disconnect", response_model=MonitorStatus)
def disconnect() -> MonitorStatus:
    """Stop monitoring the current repository."""
    controller.stop()
    return controller.current()


@router.get("/status", response_model=MonitorStatus)
def status() -> MonitorStatus:
    """Return the current monitor state to the dashboard."""
    return controller.current()


@router.post("/heartbeat", response_model=MonitorStatus)
def heartbeat(update: Heartbeat) -> MonitorStatus:
    """Accept status only from the current monitor process."""
    with controller.lock:
        if update.connection_id != controller.status.connection_id:
            raise HTTPException(409, "This monitor is no longer active.")
        controller.status.state = update.state
        controller.status.error = update.error
        controller.status.checked_at = datetime.now(UTC)
        return controller.current()
