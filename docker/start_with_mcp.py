"""Run the bundled MCP servers alongside the LiteLLM proxy in one container.

The services are supervised together so the container exits when any one stops.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import Final

MCP_ARTIFACT_DIR: Final = Path("/app/mcp/slack-mcp/site-packages")
MCP_APP: Final = "slack_self_mcp.mcp_server:app"
MCP_HOST: Final = "0.0.0.0"
MCP_PORT: Final = "8000"
FIGMA_ARTIFACT_DIR: Final = Path("/app/mcp/figma-mcp")
FIGMA_APP: Final = FIGMA_ARTIFACT_DIR / "dist/bin.js"
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


def stop(processes: list[subprocess.Popen[bytes]], signum: int = signal.SIGTERM) -> None:
    for process in processes:
        if process.poll() is None:
            with suppress(ProcessLookupError):
                process.send_signal(signum)

    deadline: Final = time.monotonic() + SHUTDOWN_GRACE_SECONDS
    for process in processes:
        try:
            process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            sys.stderr.write("start-with-mcp: a child did not stop in time; killing it\n")
            with suppress(ProcessLookupError):
                process.kill()
            process.wait()


def exit_status(child_code: int) -> int:
    """Report a signal-terminated child the way a shell does, as 128 + signal."""
    return child_code if child_code >= 0 else 128 - child_code


def supervise(processes: list[subprocess.Popen[bytes]], signals: list[int]) -> int:
    """Return the exit code of the first child to stop."""
    while not signals:
        for process in processes:
            exit_code: Final = process.poll()
            if signals:
                return 128 + signals[0]
            if exit_code is not None:
                sys.stderr.write(f"start-with-mcp: child exited with status {exit_status(exit_code)}; check its logs\n")
                return exit_status(exit_code)
        time.sleep(POLL_SECONDS)
    return 128 + signals[0]


def main(argv: list[str] | None = None) -> int:
    passthrough: Final = list(sys.argv[1:] if argv is None else argv)
    if not (
        FIGMA_APP.is_file()
        and (FIGMA_ARTIFACT_DIR / "package.json").is_file()
        and (FIGMA_ARTIFACT_DIR / "node_modules").is_dir()
    ):
        sys.stderr.write(f"start-with-mcp: missing Figma artifact in {FIGMA_ARTIFACT_DIR}; rebuild the image\n")
        return 1

    children: Final[list[subprocess.Popen[bytes]]] = []
    signals: Final[list[int]] = []

    def forward(signum: int, _frame: object) -> None:
        if not signals:
            signals.append(signum)

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, forward)

    try:
        if not signals:
            mcp_server: Final = start_mcp_server()
            if mcp_server is not None:
                children.append(mcp_server)
        if not signals:
            figma_env: Final = {
                key: value for key, value in os.environ.items()
                if key not in (
                    "FIGMA_API_KEY", "FIGMA_OAUTH_TOKEN", "FIGMA_CLIENT_ID", "FIGMA_CLIENT_SECRET"
                ) and not (key == "NODE_ENV" and value == "cli")
            }
            children.append(subprocess.Popen([
                "node", str(FIGMA_APP), "--host", "127.0.0.1", "--port", "3333",
                "--skip-image-downloads",
            ], env=figma_env, cwd=FIGMA_ARTIFACT_DIR))
        if not signals:
            children.append(subprocess.Popen([sys.executable, str(MERGE_AND_START), *passthrough]))
        return supervise(children, signals)
    except OSError as exc:
        sys.stderr.write(f"start-with-mcp: failed to start a child: {exc}\n")
        return 128 + signals[0] if signals else 1
    finally:
        stop(children, signals[0] if signals else signal.SIGTERM)


if __name__ == "__main__":
    raise SystemExit(main())
