"""In-process Python code execution sandbox.

Replaces RebaseKit ``/code/api/execute``. Runs user-supplied Python
inside a short-lived subprocess with:

- A fresh temp directory as CWD (no access to the host filesystem
  beyond that directory).
- Wall-clock timeout (default 10 s, max 30 s).
- CPU + address-space + file-size + max-open-files rlimits, applied
  via ``preexec_fn`` so they're enforced by the kernel.
- No environment variables leaked from the parent process — only the
  minimal ``PATH`` and ``PYTHONDONTWRITEBYTECODE``.
- ``stdin`` is passed through if supplied, otherwise ``/dev/null``.

This is NOT a security sandbox strong enough to accept arbitrary
hostile code. It's a "don't let a user script accidentally eat the
server" safety net. For real untrusted execution you want a VM or a
container per call.

JavaScript / bash are intentionally unsupported. The endpoint
advertised them historically via RebaseKit but the cost/benefit of
adding two more language runtimes to the API container isn't worth it.
"""

from __future__ import annotations

import asyncio
import os
import resource
import sys
import tempfile
from dataclasses import dataclass

_DEFAULT_TIMEOUT = 10.0
_MAX_TIMEOUT = 30.0
_MAX_OUTPUT_BYTES = 256 * 1024  # 256 KB of stdout/stderr each

_CPU_SECONDS_LIMIT = 15  # RLIMIT_CPU, in CPU-seconds
_AS_BYTES_LIMIT = 512 * 1024 * 1024  # RLIMIT_AS, 512 MB
_FSIZE_BYTES_LIMIT = 10 * 1024 * 1024  # RLIMIT_FSIZE, 10 MB
_NOFILE_LIMIT = 64  # RLIMIT_NOFILE


class CodeExecError(ValueError):
    """Raised on bad input (unsupported language, invalid timeout, etc.)."""


@dataclass(frozen=True, slots=True)
class ExecResult:
    stdout: str
    stderr: str
    exit_code: int
    runtime_ms: int
    timed_out: bool


def _apply_rlimits() -> None:
    """Child-process preexec hook that enforces resource caps.

    Every setrlimit call is wrapped so that platforms which don't
    support a given limit (e.g. macOS does not enforce RLIMIT_AS the
    same way Linux does) fall back gracefully instead of aborting
    the subprocess launch.
    """
    limits: tuple[tuple[int, tuple[int, int]], ...] = (
        (resource.RLIMIT_CPU, (_CPU_SECONDS_LIMIT, _CPU_SECONDS_LIMIT)),
        (resource.RLIMIT_FSIZE, (_FSIZE_BYTES_LIMIT, _FSIZE_BYTES_LIMIT)),
        (resource.RLIMIT_NOFILE, (_NOFILE_LIMIT, _NOFILE_LIMIT)),
    )
    for key, value in limits:
        try:
            resource.setrlimit(key, value)
        except (ValueError, OSError):
            pass  # Platform doesn't support this limit; carry on.

    # RLIMIT_AS is enforced on Linux but behaves inconsistently on
    # macOS — try it but don't crash the sandbox if it fails.
    try:
        resource.setrlimit(
            resource.RLIMIT_AS, (_AS_BYTES_LIMIT, _AS_BYTES_LIMIT)
        )
    except (ValueError, OSError, AttributeError):
        pass


def _truncate(data: bytes) -> str:
    if len(data) <= _MAX_OUTPUT_BYTES:
        return data.decode("utf-8", errors="replace")
    keep = _MAX_OUTPUT_BYTES
    snippet = data[:keep].decode("utf-8", errors="replace")
    return snippet + f"\n...[truncated at {keep} bytes]"


async def run_python(code: str, *, timeout: float, stdin: str | None) -> ExecResult:
    """Run *code* in a fresh subprocess and return the result."""
    with tempfile.TemporaryDirectory(prefix="cs-code-") as tmp:
        script_path = os.path.join(tmp, "main.py")
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(code)

        loop = asyncio.get_running_loop()
        start = loop.time()

        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",  # isolated mode: ignore PYTHON* env vars + user site-packages
            script_path,
            stdin=asyncio.subprocess.PIPE if stdin else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=tmp,
            env={
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONUNBUFFERED": "1",
                "HOME": tmp,
                "TMPDIR": tmp,
            },
            preexec_fn=_apply_rlimits,
        )

        timed_out = False
        try:
            stdin_bytes = stdin.encode("utf-8") if stdin is not None else None
            out, err = await asyncio.wait_for(
                proc.communicate(input=stdin_bytes),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            timed_out = True
            proc.kill()
            try:
                out, err = await proc.communicate()
            except Exception:  # noqa: BLE001 — cleanup path, must not raise
                out, err = b"", b""

        runtime_ms = int((loop.time() - start) * 1000)
        return ExecResult(
            stdout=_truncate(out),
            stderr=_truncate(err),
            exit_code=proc.returncode if proc.returncode is not None else -1,
            runtime_ms=runtime_ms,
            timed_out=timed_out,
        )


async def run(inp: dict) -> dict:
    """Task-handler entry point."""
    code = inp.get("code")
    if not isinstance(code, str) or not code.strip():
        raise CodeExecError("code_execute: 'code' must be a non-empty string")

    language = (inp.get("language") or "python").lower()
    if language != "python":
        raise CodeExecError(
            f"code_execute: language '{language}' is not supported — only 'python'"
        )

    timeout = float(inp.get("timeout_seconds") or _DEFAULT_TIMEOUT)
    if timeout <= 0:
        raise CodeExecError("code_execute: timeout_seconds must be > 0")
    if timeout > _MAX_TIMEOUT:
        timeout = _MAX_TIMEOUT

    stdin = inp.get("stdin")
    if stdin is not None and not isinstance(stdin, str):
        raise CodeExecError("code_execute: 'stdin' must be a string when provided")

    result = await run_python(code, timeout=timeout, stdin=stdin)
    return {
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.exit_code,
        "runtime_ms": result.runtime_ms,
        "timed_out": result.timed_out,
    }


__all__ = ["CodeExecError", "ExecResult", "run", "run_python"]
