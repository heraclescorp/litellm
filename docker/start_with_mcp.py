"""Run the bundled MCP server alongside the LiteLLM proxy in one container.

The image entrypoint is a single process, and ``merge_and_start.py`` replaces itself with
the proxy via ``execvp``, so nothing would keep a second process alive. This script starts
both as children and returns the exit code of whichever stops first, after stopping the
other. The container then exits, so the platform restarts both together instead of leaving
a pod that is up but missing the MCP server.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Final

MCP_ARTIFACT_DIR: Final = Path("/app/mcp/slack-mcp/site-packages")
MCP_APP: Final = "slack_self_mcp.mcp_server:app"
MCP_HOST: Final = "0.0.0.0"
MCP_PORT: Final = "8000"
MERGE_AND_START: Final = Path("/app/docker/merge_and_start.py")
POLL_SECONDS: Final = 0.2
SHUTDOWN_GRACE_SECONDS: Final = 30.0


def mcp_command() -> list[str]:
    return [sys.executable, "-m", "uvicorn", MCP_APP, "--host", MCP_HOST, "--port", MCP_PORT]


def mcp_env() -> dict[str, str]:
    """The artifact is a relocatable dependency tree, so it is reached through PYTHONPATH."""
    inherited: Final = os.environ.get("PYTHONPATH")
    artifact_path: Final = f"{MCP_ARTIFACT_DIR}{os.pathsep}{inherited}" if inherited else str(MCP_ARTIFACT_DIR)
    return {**os.environ, "PYTHONPATH": artifact_path}


def start_mcp_server() -> subprocess.Popen[bytes] | None:
    if not MCP_ARTIFACT_DIR.is_dir():
        sys.stderr.write(f"start-with-mcp: {MCP_ARTIFACT_DIR} is absent, starting without the MCP server\n")
        return None
    return subprocess.Popen(mcp_command(), env=mcp_env())


def stop(processes: list[subprocess.Popen[bytes]]) -> None:
    for process in processes:
        if process.poll() is None:
            process.terminate()

    deadline: Final = time.monotonic() + SHUTDOWN_GRACE_SECONDS
    for process in processes:
        try:
            process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def exit_status(child_code: int) -> int:
    """Report a signal-terminated child the way a shell does, as 128 + signal."""
    return child_code if child_code >= 0 else 128 - child_code


def supervise(processes: list[subprocess.Popen[bytes]]) -> int:
    """Return the exit code of the first child to stop, after stopping the rest."""

    def forward(signum: int, _frame: object) -> None:
        for process in processes:
            if process.poll() is None:
                process.send_signal(signum)

    # The container sends its stop signal to this process alone, so both children need it.
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, forward)

    try:
        while True:
            for process in processes:
                exit_code: Final = process.poll()
                if exit_code is not None:
                    return exit_status(exit_code)
            time.sleep(POLL_SECONDS)
    finally:
        stop(processes)


def main(argv: list[str] | None = None) -> int:
    passthrough: Final = list(sys.argv[1:] if argv is None else argv)
    mcp_server: Final = start_mcp_server()
    proxy: Final = subprocess.Popen([sys.executable, str(MERGE_AND_START), *passthrough])
    children: Final = [child for child in (mcp_server, proxy) if child is not None]
    return supervise(children)


if __name__ == "__main__":
    raise SystemExit(main())
