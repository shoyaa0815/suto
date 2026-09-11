"""Optional Linux namespace isolation for approved workspace commands."""
import shutil
import sys
from pathlib import Path


def sandbox_command(context, command: list[str], temp_dir: str, timeout: int) -> list[str]:
    if context.sandbox == 'process':
        return command
    if context.sandbox != 'bwrap':
        raise PermissionError('unknown sandbox backend')
    bwrap = shutil.which('bwrap', path='/usr/bin:/bin')
    prlimit = shutil.which('prlimit', path='/usr/bin:/bin')
    if not bwrap or not prlimit or sys.platform != 'linux':
        raise PermissionError('bwrap sandbox requires Linux, bubblewrap and prlimit; no fallback')
    if context.workspace == Path('/') or context.workspace in (Path('/usr'), Path('/etc'), Path('/proc'), Path('/dev')):
        raise PermissionError('sandbox workspace cannot be a system root')
    argv = [bwrap, '--unshare-all', '--new-session', '--die-with-parent', '--cap-drop', 'ALL']
    for root in ('/usr', '/bin', '/lib', '/lib64'):
        if Path(root).exists():
            argv += ['--ro-bind', root, root]
    prefix = Path(sys.prefix).resolve()
    if not prefix.is_relative_to('/usr') and not prefix.is_relative_to(context.workspace):
        argv += ['--ro-bind', str(prefix), str(prefix)]
    # This /tmp is a new in-memory filesystem inside the isolated namespace.
    argv += ['--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp',  # nosec B108
             '--bind', temp_dir, temp_dir,
             '--bind' if 'apply_workspace_patch' in context.allowed_tools else '--ro-bind',
             str(context.workspace), str(context.workspace)]
    for name in ('.env', '.git', '.ssh', '.aws'):
        target = context.workspace / name
        if target.is_symlink():
            raise PermissionError(f'sandbox rejects symlinked protected path: {name}')
        if target.is_dir():
            argv += ['--tmpfs', str(target)]
        elif target.is_file():
            argv += ['--ro-bind', '/dev/null', str(target)]
    # Apply limits after namespace setup, before executing any workspace code.
    # Applying RLIMIT_NPROC before bwrap also counts unrelated host processes.
    argv += ['--chdir', str(context.workspace), '--', prlimit,
             '--as=1073741824', f'--cpu={timeout}', '--nproc=64',
             '--nofile=256', '--fsize=16777216', '--', *command]
    return argv
