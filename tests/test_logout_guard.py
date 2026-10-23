import asyncio
import contextlib
import io
import os
import subprocess
from unittest.mock import Mock

import pytest

from editor.config import ConfigError
from editor.logout_guard import GUARD_PATH, guard_available, logout_guard
from editor.worker import Worker, RestartRequested
from editor.portal import Health


def fake_guard():
    proc = Mock(stdout=io.BytesIO(b"ready\n"))
    proc.poll.return_value = None
    return proc


def ready(read, write, error, timeout):
    return read, [], []


def test_dedicated_guard_lifetime_and_command(tmp_path):
    proc = fake_guard()
    popen = Mock(return_value=proc)
    with logout_guard(tmp_path, "dedicated", popen=popen, select_ready=ready):
        proc.terminate.assert_not_called()
        popen.assert_called_once_with([str(tmp_path / GUARD_PATH), str(os.getpid())],
                                      stdin=subprocess.DEVNULL, stdout=subprocess.PIPE)
    proc.terminate.assert_called_once()
    proc.wait.assert_called_once_with(timeout=5)
    assert proc.stdout.closed


def test_personal_computer_never_starts_guard(tmp_path):
    popen = Mock()
    with logout_guard(tmp_path, "personal", popen=popen):
        pass
    popen.assert_not_called()


@pytest.mark.parametrize("response,readable,exited", [(b"", True, 1), (b"wrong\n", True, None),
                                                     (b"ready\n", False, None), (b"ready\n", True, 1)])
def test_failed_start_does_not_run_worker_and_cleans_up(tmp_path, response, readable, exited):
    proc = fake_guard()
    proc.stdout = io.BytesIO(response)
    proc.poll.return_value = exited
    with pytest.raises(ConfigError, match="couldn't start"):
        with logout_guard(tmp_path, "dedicated", popen=lambda *a, **kw: proc,
                          select_ready=lambda *a: ([proc.stdout] if readable else [], [], [])):
            pytest.fail("Unprotected worker started")
    assert proc.stdout.closed
    assert proc.terminate.called == (exited is None)


def test_missing_guard_explains_repair(tmp_path):
    with pytest.raises(ConfigError, match="Repair"):
        with logout_guard(tmp_path, "dedicated", popen=Mock(side_effect=FileNotFoundError)):
            pytest.fail("Unprotected worker started")


@pytest.mark.parametrize("result", [FileNotFoundError(), subprocess.TimeoutExpired("guard", 15),
                                    subprocess.CompletedProcess([], 1, b"ready\n"),
                                    subprocess.CompletedProcess([], 0, b"wrong\n")])
def test_health_probe_rejects_failed_launch(tmp_path, monkeypatch, result):
    run = Mock(side_effect=result) if isinstance(result, Exception) else Mock(return_value=result)
    monkeypatch.setattr("editor.logout_guard.subprocess.run", run)
    assert not guard_available(tmp_path)


def test_exception_releases_guard_without_relabeling_worker_error(tmp_path):
    proc = fake_guard()
    proc.wait.side_effect = [subprocess.TimeoutExpired("guard", 5), 0]
    with pytest.raises(OSError, match="worker error"):
        with logout_guard(tmp_path, "dedicated", popen=lambda *a, **kw: proc, select_ready=ready):
            raise OSError("worker error")
    proc.kill.assert_called_once()
    assert proc.stdout.closed


@pytest.mark.parametrize("failure", [RestartRequested("repair"), asyncio.CancelledError()])
async def test_worker_releases_guard_on_restart_or_cancellation(failure):
    events = []

    @contextlib.contextmanager
    def guard():
        events.append("protected")
        try:
            yield
        finally:
            events.append("released")

    worker = Worker(portal=None, version="test", doctor=lambda: Health(True), run_job=None,
                    session_guard=guard)

    async def serve():
        assert events == ["protected"]
        raise failure

    worker._serve = serve
    with pytest.raises(type(failure)):
        await worker.serve()
    assert events == ["protected", "released"]
