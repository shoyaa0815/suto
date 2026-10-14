import asyncio
import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from mcp_integration.confined import ConfinedMCPLauncher, ConfinementDenied, load_manifest
from mcp_integration.job_policy import IDENTITIES, identity, pin_selection
from sandbox import SandboxCapability
from workflows.runtime.context import ExecutionContext


def make_launcher(tmp_path, monkeypatch, files=None, command=None):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    (workspace / "input.txt").write_text("input")
    (workspace / ".env").write_text("private")
    manifest = {"format_version": 1, "files": files or {
        str(workspace / "input.txt"): hashlib.sha256(b"input").hexdigest()}}
    artifact = tmp_path / "manifest.json"
    artifact.write_text(json.dumps(manifest))
    artifact.chmod(0o600)
    entry = {key: identity(key) for key in IDENTITIES}
    entry.update(effect="read_only", workspaces=[str(workspace)],
                 mount_manifest_identity=identity(manifest))
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"format_version": 1, "tools": {"mcp.fixture.read": entry}}))
    policy.chmod(0o600)
    monkeypatch.setenv("SUTO_JOB_MCP_POLICY", str(policy))
    options = {"mcp_selection": pin_selection(["mcp.fixture.read"], str(workspace))}
    return ConfinedMCPLauncher(ExecutionContext("test", workspace, sandbox="bwrap"),
                              options, "mcp.fixture.read", artifact,
                              command or (str(workspace / "input.txt"),))


@pytest.mark.parametrize("failure", ["process", "permission", "revoked", "rebound",
                                     "manifest", "missing", "host-denied"])
async def test_denial_before_server_spawn(tmp_path, monkeypatch, failure):
    launcher = make_launcher(tmp_path, monkeypatch)
    calls = []
    async def forbidden(*args, **kwargs):
        calls.append(args)
        raise AssertionError("must never spawn")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    monkeypatch.setattr("mcp_integration.confined.Sandbox.capability",
                        lambda _: SandboxCapability(
                            "unavailable" if failure == "host-denied" else "available",
                            "unit boundary fake only"))
    if failure == "process":
        launcher.context = replace(launcher.context, sandbox="process")
    elif failure == "permission":
        launcher.context = replace(launcher.context, allowed_tools=frozenset())
    elif failure == "revoked":
        Path(os.environ["SUTO_JOB_MCP_POLICY"]).write_text('{"format_version":1,"tools":{}}')
    elif failure == "rebound":
        launcher.context.workspace.rename(tmp_path / "old")
        launcher.context.workspace.mkdir()
    elif failure == "manifest":
        launcher.manifest_path.write_text('{}')
    elif failure == "missing":
        launcher.manifest_path.unlink()
    with pytest.raises(ConfinementDenied, match="no fallback"):
        async with launcher.running():
            pytest.fail("unexpected launch")
    assert calls == [] and launcher.process is None


@pytest.mark.parametrize("target", ["directory", "secret", "symlink", "outside", "fifo", "changed"])
def test_manifest_cannot_expand_native_mount_rules(tmp_path, monkeypatch, target):
    launcher = make_launcher(tmp_path, monkeypatch)
    path = launcher.context.workspace / "input.txt"
    if target == "directory":
        path = launcher.context.workspace
    elif target == "secret":
        path = launcher.context.workspace / ".env"
    elif target == "symlink":
        link = launcher.context.workspace / "link"
        link.symlink_to(path)
        path = link
    elif target == "outside":
        path = tmp_path / "secret"
        path.write_text("input")
    elif target == "fifo":
        path = launcher.context.workspace / "fifo"
        os.mkfifo(path)
    elif target == "changed":
        path.write_text("changed")
    with pytest.raises(ConfinementDenied):
        launcher._open_files({"files": {str(path): hashlib.sha256(b"input").hexdigest()}})


def test_snapshots_are_sealed_and_manifest_permissions_checked(tmp_path, monkeypatch):
    launcher = make_launcher(tmp_path, monkeypatch)
    files = launcher._open_files(launcher._validate())
    try:
        fd, path = files[0]
        Path(path).write_text("replacement")
        assert os.read(fd, 20) == b"input"
        with pytest.raises(OSError):
            os.write(fd, b"mutation")
    finally:
        for fd, _ in files:
            os.close(fd)
    launcher.manifest_path.chmod(0o666)
    expected = identity(json.loads(launcher.manifest_path.read_text()))
    with pytest.raises(ConfinementDenied):
        load_manifest(launcher.manifest_path, expected)


async def test_cancel_during_spawn_still_awaits_and_cleans_process(tmp_path, monkeypatch):
    launcher = make_launcher(tmp_path, monkeypatch)
    monkeypatch.setattr("mcp_integration.confined.Sandbox.capability",
                        lambda _: SandboxCapability("available", "unit fake only"))
    entered, release, waited = asyncio.Event(), asyncio.Event(), asyncio.Event()
    class Process:
        pid = 123456789
        returncode = None
        async def wait(self):
            self.returncode = -9
            waited.set()
    async def spawn(*args, **kwargs):
        assert "--unshare-user" in args and "--disable-userns" in args
        assert args.index("--unshare-user") < args.index("--disable-userns")
        entered.set()
        await release.wait()
        return Process()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    killed = []
    monkeypatch.setattr(os, "killpg", lambda *args: killed.append(args))
    async def run():
        async with launcher.running():
            pytest.fail("cancelled startup")
    task = asyncio.create_task(run())
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert waited.is_set() and killed


@pytest.mark.parametrize("failure", ["eof", "malformed", "oversize", "server-error", "revoked", "mount-drift"])
async def test_transport_failure_closes_before_any_later_dispatch(tmp_path, monkeypatch, failure):
    launcher = make_launcher(tmp_path, monkeypatch)
    sent, killed = [], []
    class Input:
        def write(self, data):
            sent.append(data)
        async def drain(self):
            pass
    class Output:
        async def readline(self):
            return {"eof": b"", "malformed": b"private diagnostic\n",
                    "oversize": b"x" * 1_048_577,
                    "server-error": b'{"id":1,"error":{"message":"private"}}\n'}[failure]
    class Process:
        returncode = None
        pid = 123456789
        stdin, stdout = Input(), Output()
        async def wait(self):
            self.returncode = -9
    launcher.process = Process()
    monkeypatch.setattr(os, "killpg", lambda *args: killed.append(args))
    if failure == "revoked":
        Path(os.environ["SUTO_JOB_MCP_POLICY"]).write_text('{"format_version":1,"tools":{}}')
    elif failure == "mount-drift":
        (launcher.context.workspace / "input.txt").write_text("changed")
    with pytest.raises(ConfinementDenied, match="no fallback") as error:
        await launcher.request("tools/list")
    assert "private" not in str(error.value)
    assert launcher.process.returncode == -9 and killed
    assert len(sent) == (0 if failure in {"revoked", "mount-drift"} else 1)
    with pytest.raises(ConfinementDenied):
        await launcher.request("tools/call")
    assert len(sent) <= 1
