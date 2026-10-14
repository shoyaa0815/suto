"""Real namespace evidence only. Required CI turns unavailability into failure."""

import asyncio
import hashlib
import json
import os
import socket
import subprocess
from pathlib import Path

import pytest

from mcp_integration.confined import ConfinementDenied
from sandbox import Sandbox, SandboxPolicy
from tests.mcp.test_confined_launcher import make_launcher


@pytest.fixture
def namespace(tmp_path):
    capability = Sandbox(SandboxPolicy("bwrap", tmp_path)).capability()
    if capability.status != "available":
        if os.environ.get("SUTO_REQUIRE_MCP_NAMESPACE") == "1":
            pytest.fail("required real namespace proof unavailable: " + capability.reason)
        pytest.skip("real namespace proof unavailable: " + capability.reason)


def runtime_files():
    # Test-only operator review artifact generation. The production launcher
    # cannot discover dependencies or add any of these mounts itself.
    executable = Path("/usr/bin/python3").resolve()
    probe = """
import json,os,pathlib,signal,socket,subprocess,sys
print(json.dumps(sorted({str(pathlib.Path(m.__file__).resolve()) for m in list(sys.modules.values())
                        if getattr(m, '__file__', None)})))
"""
    paths = {executable, *(Path(p) for p in json.loads(subprocess.check_output(
        [str(executable), "-S", "-c", probe], text=True, env={})))}
    for path in list(paths):
        if path == executable or path.suffix == ".so":
            output = subprocess.check_output(["/usr/bin/ldd", str(path)], text=True, env={})
            for token in output.split():
                if token.startswith("/"):
                    paths.add(Path(token).resolve())
    return executable, paths


def fixture_launcher(tmp_path, monkeypatch, port, socket_path, block="none"):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    script = workspace / "fixture.py"
    script.write_bytes(Path(__file__).with_name("confined_fixture.py").read_bytes())
    (workspace / "input.txt").write_text("input")
    secret = tmp_path / "secret"
    secret.write_text("protected")
    (workspace / "escape").symlink_to(secret)
    executable, paths = runtime_files()
    paths.update({script, workspace / "input.txt"})
    files = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    launcher = make_launcher(tmp_path, monkeypatch, files, (
        str(executable), "-S", str(script), str(workspace), str(secret),
        str(socket_path), str(port), block))
    monkeypatch.setenv("SUTO_PRIVATE_SECRET", "must-not-reach-server")
    return launcher


def namespace_members(namespace):
    result = []
    for path in Path("/proc").glob("[0-9]*/ns/pid"):
        try:
            if os.readlink(path) == namespace:
                result.append(path)
        except OSError:
            pass
    return result


@pytest.fixture
def listeners(tmp_path):
    with socket.socket() as tcp, socket.socket(socket.AF_UNIX) as unix:
        tcp.bind(("127.0.0.1", 0))
        tcp.listen()
        path = tmp_path / "host.sock"
        unix.bind(str(path))
        unix.listen()
        yield tcp.getsockname()[1], path


async def test_real_startup_discovery_calls_and_cleanup(namespace, listeners, tmp_path, monkeypatch):
    launcher = fixture_launcher(tmp_path, monkeypatch, *listeners)
    spawn = asyncio.create_subprocess_exec
    launches = []
    async def track_spawn(*args, **kwargs):
        launches.append(args)
        return await spawn(*args, **kwargs)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", track_spawn)
    receive = launcher._receive
    startup = []
    async def track():
        response = await receive()
        if "checks" in response.get("result", {}):
            startup.append(response["result"]["checks"])
        return response
    monkeypatch.setattr(launcher, "_receive", track)
    async with launcher.running():
        assert all(startup[0].values()), startup[0]
        discovery = await launcher.request("tools/list")
        assert all(discovery["checks"].values()), discovery["checks"]
        result = await launcher.request("tools/call", {"name": "read", "arguments": {}})
        assert all(result["checks"].values()), result["checks"]
        ns = result["namespace"]
        assert len(namespace_members(ns)) >= 3  # PID 1 + server + detached descendant
        assert result["content"][0]["text"] == "input"
    assert launcher.process.returncode is not None
    assert len(launches) == 1
    assert not namespace_members(ns)
    assert (tmp_path / "secret").read_text() == "protected"
    assert (launcher.context.workspace / "input.txt").read_text() == "input"
    assert not (launcher.context.workspace / "index").exists()
    assert not (tmp_path / "private").exists()


@pytest.mark.parametrize("stage", ["initialize", "tools/list", "tools/call"])
async def test_real_cancellation_kills_detached_descendants(namespace, listeners, tmp_path, monkeypatch, stage):
    launcher = fixture_launcher(tmp_path, monkeypatch, *listeners, block=stage)
    ready = asyncio.Event()
    observed = {}
    receive = launcher._receive
    async def track():
        response = await receive()
        if response.get("method") == "notifications/ready":
            observed.update(response["params"])
            ready.set()
        return response
    monkeypatch.setattr(launcher, "_receive", track)
    async def run():
        async with launcher.running():
            await launcher.request("tools/list")
            await launcher.request("tools/call", {"name": "read"})
    task = asyncio.create_task(run())
    await asyncio.wait_for(ready.wait(), 10)
    assert all(observed["checks"].values())
    assert len(namespace_members(observed["namespace"])) >= 3
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert launcher.process.returncode is not None
    assert not namespace_members(observed["namespace"])
    with pytest.raises(ConfinementDenied):
        await launcher.request("tools/call")


async def test_real_missing_dependency_has_no_host_retry(namespace, listeners, tmp_path, monkeypatch):
    launcher = fixture_launcher(tmp_path, monkeypatch, *listeners)
    # Remove the fixture from the exact manifest and repin as an operator would.
    manifest = json.loads(launcher.manifest_path.read_text())
    del manifest["files"][str(launcher.context.workspace / "fixture.py")]
    from mcp_integration.job_policy import identity, pin_selection
    launcher.manifest_path.write_text(json.dumps(manifest))
    policy_path = Path(os.environ["SUTO_JOB_MCP_POLICY"])
    policy = json.loads(policy_path.read_text())
    policy["tools"][launcher.tool]["mount_manifest_identity"] = identity(manifest)
    policy_path.write_text(json.dumps(policy))
    launcher.options = {"mcp_selection": pin_selection([launcher.tool], str(launcher.context.workspace))}
    with pytest.raises(ConfinementDenied):
        async with launcher.running():
            pytest.fail("missing dependency must block")
    assert launcher.process.returncode != 0
    assert (tmp_path / "secret").read_text() == "protected"


async def test_real_timeout_cleans_partial_startup(namespace, listeners, tmp_path, monkeypatch):
    launcher = fixture_launcher(tmp_path, monkeypatch, *listeners, block="initialize")
    observed = {}
    receive = launcher._receive
    async def track():
        response = await receive()
        if response.get("method") == "notifications/ready":
            observed.update(response["params"])
        return response
    monkeypatch.setattr(launcher, "_receive", track)
    with pytest.raises(ConfinementDenied):
        async with launcher.running():
            pytest.fail("startup timeout cannot succeed")
    assert all(observed["checks"].values())
    assert launcher.process.returncode is not None
    assert not namespace_members(observed["namespace"])
