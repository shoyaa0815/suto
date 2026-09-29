from pathlib import Path

import pytest

from permissions import Approval, PermissionEngine, PermissionPolicy
from sandbox import Sandbox, SandboxCapability, SandboxPolicy


def test_permission_decisions_and_exact_approval():
    engine = PermissionEngine(PermissionPolicy({
        "file.read": "allow",
        "file.write": "require_approval",
        "shell.dangerous": "deny",
    }))
    assert engine.decide("file.read").allowed
    assert engine.decide("file.write").requires_confirmation
    assert not engine.decide("unknown").allowed
    calls = []
    request = Approval("file.write", {"path": "a.py", "sha256": "abc"}, "write a.py", "diff")

    with pytest.raises(PermissionError, match="approval is unavailable"):
        engine.require("file.write", approval=request)
    with pytest.raises(PermissionError, match="approval is unavailable"):
        engine.require("file.write", approval=Approval("file.read", {}, "", ""), approval_callback=calls.append)
    with pytest.raises(PermissionError, match="not allowed"):
        engine.require("shell.dangerous", approval=request, approval_callback=calls.append)
    assert calls == []

    engine.require("file.read")
    engine.require("file.write", approval=request, approval_callback=lambda approval: calls.append(approval) or True)
    assert calls == [request]
    with pytest.raises(PermissionError, match="approval denied"):
        engine.require("file.write", approval=request, approval_callback=lambda _: False)
    with pytest.raises(PermissionError, match="approval denied"):
        engine.require("file.write", approval=request, approval_callback=lambda _: None)


def test_invalid_policy_fails_closed():
    engine = PermissionEngine(PermissionPolicy({"file.write": "unknown_rule"}))
    with pytest.raises(PermissionError, match="invalid permission policy"):
        engine.require("file.write")


def test_sandbox_policy_is_independent_of_permission_decision(tmp_path, monkeypatch):
    monkeypatch.setattr("sandbox.runtime.shutil.which", lambda name, **_: f"/usr/bin/{name}")
    monkeypatch.setattr("sandbox.runtime.sys.platform", "linux")
    monkeypatch.setattr(Sandbox, "capability", lambda self: SandboxCapability("available", "test"))
    command = ["/usr/bin/git", "status"]
    temp_dir = str(tmp_path / "temp")
    for writable, binding in ((False, "--ro-bind"), (True, "--bind")):
        policy = SandboxPolicy("bwrap", Path(tmp_path), writable=writable)
        argv = Sandbox(policy).command(command, temp_dir, 10)
        assert any(
            argv[index:index + 3] == [binding, str(tmp_path), str(tmp_path)]
            for index in range(len(argv) - 2)
        )
        assert argv[-2:] == command[-2:]
    assert Sandbox(SandboxPolicy("process", tmp_path)).command(command, str(tmp_path), 10) == command
