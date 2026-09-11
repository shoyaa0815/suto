import asyncio
import shutil
import subprocess
import sys
import tempfile
from dataclasses import replace

import pytest

from automation.context import COMMAND_TOOLS, WRITE_WORKSPACE_TOOLS, ExecutionContext
from automation.sandbox import sandbox_command
from tools.command import build_command_tools


def test_missing_namespace_backend_fails_closed(tmp_path, monkeypatch):
    context = ExecutionContext('sandbox', tmp_path, allowed_tools=COMMAND_TOOLS, sandbox='bwrap')
    monkeypatch.setattr(shutil, 'which', lambda *args, **kwargs: None)
    with pytest.raises(PermissionError, match='no fallback'):
        sandbox_command(context, ['true'], str(tmp_path), 10)


def test_sandbox_runtime_boundary(tmp_path):
    if not shutil.which('bwrap'):
        pytest.skip('bubblewrap is not installed')
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    (workspace / 'input.txt').write_text('workspace data')
    (workspace / '.env').write_text('SECRET=hidden')
    outside = tmp_path / 'host-secret.txt'
    outside.write_text('outside secret')
    context = ExecutionContext('sandbox', workspace, allowed_tools=COMMAND_TOOLS, sandbox='bwrap')
    script = """
import json, os, pathlib, resource, socket, subprocess, sys
assert pathlib.Path('input.txt').read_text() == 'workspace data'
try:
    assert pathlib.Path('.env').read_text() == ''
except PermissionError:
    pass
assert not pathlib.Path(sys.argv[1]).exists()
assert all(name == 'lo' for _, name in socket.if_nameindex())
assert resource.getrlimit(resource.RLIMIT_AS)[0] == 1073741824
assert subprocess.run([sys.executable, '-c', 'pass']).returncode == 0
try:
    pathlib.Path('new.txt').write_text('not allowed')
except OSError:
    pass
else:
    raise AssertionError('read-only workspace was writable')
print('isolated')
"""
    with tempfile.TemporaryDirectory(prefix='suto-sandbox-test-') as temp:
        command = sandbox_command(context, [sys.executable, '-c', script, str(outside)], temp, 10)
        result = subprocess.run(command, capture_output=True, text=True, timeout=15, env={'PATH': '/usr/bin:/bin'})
        if 'No permissions to create' in result.stderr or 'Operation not permitted' in result.stderr:
            pytest.skip('host execution policy prevents namespace creation; run this test outside the tool sandbox')
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == 'isolated'
        writable = replace(context, allowed_tools=COMMAND_TOOLS | WRITE_WORKSPACE_TOOLS)
        result = subprocess.run(sandbox_command(writable, [sys.executable, '-c',
            "from pathlib import Path; Path('new.txt').write_text('allowed')"], temp, 10),
            capture_output=True, text=True, timeout=15, env={'PATH': '/usr/bin:/bin'})
        assert result.returncode == 0, result.stderr
        assert (workspace / 'new.txt').read_text() == 'allowed'


async def test_approved_bwrap_command_and_timeout_kill_descendants(tmp_path):
    if not shutil.which('bwrap'):
        pytest.skip('bubblewrap is not installed')
    context = ExecutionContext('sandbox', tmp_path, allowed_tools=COMMAND_TOOLS | WRITE_WORKSPACE_TOOLS,
                               sandbox='bwrap', approval_callback=lambda *args: None)
    events = []
    command = build_command_tools(context, events.append)['run_workspace_command']
    (tmp_path / 'test_simple.py').write_text('def test_simple():\n    assert 2 + 2 == 4\n')
    result = await command(['pytest', '-q', 'test_simple.py'])
    if 'No permissions to create' in result or 'Operation not permitted' in result:
        pytest.skip('host execution policy prevents namespace creation')
    assert '1 passed' in result and events[-1]['exit_code'] == 0
    (tmp_path / 'test_slow.py').write_text(
        'import subprocess, sys, time\n'
        'def test_slow():\n'
        '    subprocess.Popen([sys.executable, "-c", '
        '"import time; from pathlib import Path; time.sleep(1.4); Path(\'escaped.txt\').write_text(\'alive\')"])\n'
        '    time.sleep(10)\n')
    result = await command(['pytest', '-q', 'test_slow.py'], timeout_seconds=1)
    assert 'status: timed_out' in result
    await asyncio.sleep(.8)
    assert not (tmp_path / 'escaped.txt').exists()
