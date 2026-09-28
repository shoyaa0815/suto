"""Workspace command isolation; accepts no permission or approval state."""

import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SandboxPolicy:
    backend: str
    workspace: Path
    writable: bool = False


class Sandbox:
    def __init__(self, policy: SandboxPolicy) -> None:
        self.policy = policy

    def command(self, command: list[str], temp_dir: str, timeout: int) -> list[str]:
        policy = self.policy
        if policy.backend == "process":
            return command
        if policy.backend != "bwrap":
            raise PermissionError("unknown sandbox backend")
        bwrap = shutil.which("bwrap", path="/usr/bin:/bin")
        prlimit = shutil.which("prlimit", path="/usr/bin:/bin")
        if not bwrap or not prlimit or sys.platform != "linux":
            raise PermissionError("bwrap sandbox requires Linux, bubblewrap and prlimit; no fallback")
        if policy.workspace == Path("/") or policy.workspace in (
            Path("/usr"), Path("/etc"), Path("/proc"), Path("/dev")
        ):
            raise PermissionError("sandbox workspace cannot be a system root")
        argv = [bwrap, "--unshare-all", "--new-session", "--die-with-parent", "--cap-drop", "ALL"]
        for root in ("/usr", "/bin", "/lib", "/lib64"):
            if Path(root).exists():
                argv += ["--ro-bind", root, root]
        prefix = Path(sys.prefix).resolve()
        if not prefix.is_relative_to("/usr") and not prefix.is_relative_to(policy.workspace):
            argv += ["--ro-bind", str(prefix), str(prefix)]
        # The namespace's /tmp is private; only the command's temporary directory is bound in.
        argv += [
            "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",  # nosec B108
            "--bind", temp_dir, temp_dir,
            "--bind" if policy.writable else "--ro-bind",
            str(policy.workspace), str(policy.workspace),
        ]
        for name in (".env", ".git", ".ssh", ".aws"):
            target = policy.workspace / name
            if target.is_symlink():
                raise PermissionError(f"sandbox rejects symlinked protected path: {name}")
            if target.is_dir():
                argv += ["--tmpfs", str(target)]
            elif target.is_file():
                argv += ["--ro-bind", "/dev/null", str(target)]
        # Limits apply inside the namespace, before workspace code executes.
        argv += [
            "--chdir", str(policy.workspace), "--", prlimit,
            "--as=1073741824", f"--cpu={timeout}", "--nproc=64",
            "--nofile=256", "--fsize=16777216", "--", *command,
        ]
        return argv
