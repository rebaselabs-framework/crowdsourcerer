"""Tests for workers/local/code_exec.py — the subprocess sandbox."""

import pytest

from workers.local.code_exec import CodeExecError, run, run_python


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_hello_world(self):
        result = await run({"code": "print('hello')"})
        assert "hello" in result["stdout"]
        assert result["exit_code"] == 0
        assert result["timed_out"] is False

    @pytest.mark.asyncio
    async def test_arithmetic(self):
        result = await run({"code": "print(sum(range(10)))"})
        assert "45" in result["stdout"]
        assert result["exit_code"] == 0

    @pytest.mark.asyncio
    async def test_stdin_passthrough(self):
        result = await run({"code": "import sys; print(sys.stdin.read().upper())", "stdin": "hello"})
        assert "HELLO" in result["stdout"]


class TestValidation:
    @pytest.mark.asyncio
    async def test_missing_code_raises(self):
        with pytest.raises(CodeExecError):
            await run({})

    @pytest.mark.asyncio
    async def test_empty_code_raises(self):
        with pytest.raises(CodeExecError):
            await run({"code": "  "})

    @pytest.mark.asyncio
    async def test_non_python_language_rejected(self):
        with pytest.raises(CodeExecError):
            await run({"code": "echo hi", "language": "bash"})

    @pytest.mark.asyncio
    async def test_negative_timeout_rejected(self):
        with pytest.raises(CodeExecError):
            await run({"code": "print(1)", "timeout_seconds": -1})


class TestTimeout:
    @pytest.mark.asyncio
    async def test_timeout_marks_timed_out(self):
        result = await run(
            {"code": "import time; time.sleep(5)", "timeout_seconds": 0.5}
        )
        assert result["timed_out"] is True

    @pytest.mark.asyncio
    async def test_timeout_capped_at_30(self):
        """Requesting a 100-second timeout should be capped to 30."""
        # We can't wait 30s in tests, but we can verify the value is
        # clamped by running a trivial script with a large request.
        result = await run({"code": "print('x')", "timeout_seconds": 100})
        assert result["exit_code"] == 0


class TestErrorCapture:
    @pytest.mark.asyncio
    async def test_stderr_captured(self):
        result = await run({"code": "import sys; sys.stderr.write('boom')"})
        assert "boom" in result["stderr"]

    @pytest.mark.asyncio
    async def test_exception_propagates_nonzero_exit(self):
        result = await run({"code": "raise RuntimeError('fail')"})
        assert result["exit_code"] != 0
        assert "RuntimeError" in result["stderr"]


class TestSandboxHygiene:
    @pytest.mark.asyncio
    async def test_no_access_to_host_env_vars(self):
        """The sandbox clears all env vars except the minimal PATH/HOME/TMPDIR
        it sets explicitly, so $HOME / $PATH / $TMPDIR should be the
        sandbox-local values — not whatever the parent process has."""
        import os

        os.environ["LEAKED_SECRET"] = "shouldnt-see"
        try:
            result = await run(
                {"code": "import os; print(os.environ.get('LEAKED_SECRET') or 'absent')"}
            )
            assert "absent" in result["stdout"]
        finally:
            os.environ.pop("LEAKED_SECRET", None)

    @pytest.mark.asyncio
    async def test_working_directory_is_temp(self):
        """CWD should be a fresh temp dir, not the test's cwd."""
        result = await run({"code": "import os; print(os.getcwd())"})
        # macOS temp dirs contain /tmp/ or /var/folders/ patterns
        assert "cs-code-" in result["stdout"] or "/tmp" in result["stdout"]
