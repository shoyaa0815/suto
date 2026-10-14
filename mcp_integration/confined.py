"""Internal offline launcher primitive; deliberately not wired to Job execution.

Manifest artifacts are supplied by the operator, never by an MCP response.
This transport is serialized and bounded. It does not grant tool eligibility,
classify effects, or implement the durable call journal required for Jobs.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import signal
import stat
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from sandbox import Sandbox, SandboxPolicy

from .job_policy import identity, load_job_mcp_policy, revalidate_selection


class ConfinementDenied(PermissionError):
    def __init__(self):
        super().__init__("MCP confinement unavailable or invalid; no fallback")


def _regular(path: Path) -> bool:
    return path.is_absolute() and path.resolve() == path and path.is_file()


def load_manifest(path: Path, expected: str) -> dict:
    """Read an explicit operator-owned artifact, pinned by current policy."""
    from .job_policy import MAX_POLICY_BYTES, _unique_pairs

    try:
        if not _regular(path):
            raise ConfinementDenied()
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            details = os.fstat(stream.fileno())
            if (details.st_uid != os.geteuid() or details.st_mode & 0o022
                    or not stat.S_ISREG(details.st_mode)):
                raise ConfinementDenied()
            raw = stream.read(MAX_POLICY_BYTES + 1)
        if len(raw) > MAX_POLICY_BYTES:
            raise ConfinementDenied()
        manifest = json.loads(raw, object_pairs_hook=_unique_pairs)
        if (identity(manifest) != expected or not isinstance(manifest, dict)
                or set(manifest) != {"format_version", "files"}
                or type(manifest["format_version"]) is not int
                or manifest["format_version"] != 1
                or not isinstance(manifest["files"], dict)
                or not 1 <= len(manifest["files"]) <= 512):
            raise ConfinementDenied()
        return manifest
    except (OSError, ValueError, TypeError, RecursionError):
        raise ConfinementDenied() from None


class ConfinedMCPLauncher:
    """One server lifetime, with policy/workspace revalidation at each request.

    Callers must separately validate executable/config/dependency, limits,
    schema/effect identities and journal calls before enabling production Jobs.
    No environment input or writable mount is accepted by this API.
    """

    def __init__(self, context, options: dict, tool: str, manifest: Path,
                 command: tuple[str, ...]):
        self.context = context
        # Detach from caller-owned mutable containers.
        self.options = json.loads(json.dumps(options))
        self.tool = tool
        self.manifest_path = manifest
        self.command = tuple(command)
        self.workspace_stat = context.workspace.stat()
        self.process = None
        self._lock = asyncio.Lock()
        self._sequence = 0
        self._output_bytes = 0
        self._expiry = None

    def _validate(self) -> dict:
        try:
            context = self.context
            if context.sandbox != "bwrap" or context.workspace == Path("/tmp"):
                raise ConfinementDenied()
            context.require_tool("read_workspace_file")
            current = context.workspace.stat()
            if (current.st_dev, current.st_ino) != (
                    self.workspace_stat.st_dev, self.workspace_stat.st_ino):
                raise ConfinementDenied()
            revalidate_selection(self.options, str(context.workspace))
            pin = self.options.get("mcp_selection", {})
            if self.tool not in pin.get("tools", []):
                raise ConfinementDenied()
            entry = load_job_mcp_policy()["tools"][self.tool]
            if self.manifest_path.is_relative_to(context.workspace):
                raise ConfinementDenied()
            return load_manifest(self.manifest_path, entry["mount_manifest_identity"])
        except Exception:
            raise ConfinementDenied() from None

    def _open_files(self, manifest: dict) -> list[tuple[int, str]]:
        opened = []
        total = 0
        try:
            workspace = self.context.workspace
            # Same runtime ceilings as native commands, narrowed to exact files.
            roots = [Path(p).resolve() for p in ("/usr", "/bin", "/lib", "/lib64", sys.prefix)]
            for source, digest in manifest["files"].items():
                path = Path(source)
                in_workspace = path.is_relative_to(workspace)
                if (str(path) != source or not _regular(path) or not isinstance(digest, str)
                        or any(part.startswith(".") for part in path.parts)
                        or not (in_workspace or any(path.is_relative_to(root) for root in roots))
                        or path.is_relative_to("/tmp") and not in_workspace):
                    raise ConfinementDenied()
                fd = os.memfd_create("suto-mcp-mount", os.MFD_ALLOW_SEALING)
                opened.append((fd, source))
                source_fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(source_fd, "rb") as stream:
                    details = os.fstat(stream.fileno())
                    if not stat.S_ISREG(details.st_mode) or details.st_size > 67_108_864:
                        raise ConfinementDenied()
                    content = stream.read(67_108_865)
                total += len(content)
                if total > 268_435_456:
                    raise ConfinementDenied()
                if len(content) > 67_108_864 or hashlib.sha256(content).hexdigest() != digest:
                    raise ConfinementDenied()
                # Frozen bytes prevent in-place writes/rebinding after validation.
                with os.fdopen(os.dup(fd), "wb") as snapshot:
                    snapshot.write(content)
                os.fchmod(fd, details.st_mode & 0o555)
                os.lseek(fd, 0, os.SEEK_SET)
                fcntl.fcntl(fd, fcntl.F_ADD_SEALS, fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW
                            | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL)
            if (not self.command or self.command[0] not in manifest["files"]
                    or any(not isinstance(arg, str) or "\0" in arg for arg in self.command)):
                raise ConfinementDenied()
            return opened
        except BaseException as error:
            for fd, _ in opened:
                os.close(fd)
            if isinstance(error, Exception):
                raise ConfinementDenied() from None
            raise

    @asynccontextmanager
    async def running(self):
        """Own startup through cleanup, including failure and cancellation."""
        opened = []
        try:
            if self.process is not None:
                raise ConfinementDenied()
            manifest = self._validate()
            capability = Sandbox(SandboxPolicy("bwrap", self.context.workspace)).capability()
            if capability.status != "available":
                raise ConfinementDenied()
            opened = self._open_files(manifest)
            # prlimit limits the trusted launcher before any untrusted code runs.
            argv = ["/usr/bin/prlimit", "--as=1073741824", "--cpu=30",
                    "--nproc=64", "--nofile=1024", "--fsize=16777216", "--",
                    "/usr/bin/bwrap", "--unshare-all", "--unshare-user", "--disable-userns",
                    "--new-session", "--die-with-parent", "--cap-drop", "ALL", "--clearenv"]
            # Create private filesystem topology before binding any files. A
            # workspace under /tmp must not be hidden by a later scratch mount,
            # nor inherit writable scratch directories around its file mounts.
            argv += ["--proc", "/proc", "--remount-ro", "/proc",
                     "--dev", "/dev", "--remount-ro", "/dev", "--perms", "0700",
                     "--size", "16777216", "--tmpfs", "/tmp",
                     "--size", "1048576", "--tmpfs", str(self.context.workspace)]
            for fd, destination in opened:
                argv += ["--perms", format(os.fstat(fd).st_mode & 0o555, "o"),
                         "--ro-bind-data", str(fd), destination]
            argv += ["--remount-ro", str(self.context.workspace), "--remount-ro", "/",
                     "--setenv", "HOME", "/tmp", "--setenv", "TMPDIR", "/tmp",
                     "--setenv", "PYTHONDONTWRITEBYTECODE", "1",
                     "--chdir", str(self.context.workspace), "--", *self.command]
            spawn = asyncio.create_task(asyncio.create_subprocess_exec(
                *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, env={}, close_fds=True,
                pass_fds=tuple(fd for fd, _ in opened), start_new_session=True,
                limit=65_536))
            try:
                self.process = await asyncio.shield(spawn)
            except asyncio.CancelledError:
                self.process = await spawn
                raise
            for fd, _ in opened:
                os.close(fd)
            opened = []
            self._expiry = asyncio.get_running_loop().call_later(30, self._kill)
            initialized = await self.request("initialize", {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "suto-confined-fixture", "version": "1"},
            })
            capabilities = initialized.get("capabilities")
            if (initialized.get("protocolVersion") != "2024-11-05"
                    or not isinstance(capabilities, dict)
                    or not isinstance(capabilities.get("tools"), dict)):
                raise ConfinementDenied()
            await self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            yield self
        except OSError:
            raise ConfinementDenied() from None
        finally:
            for fd, _ in opened:
                os.close(fd)
            # Shield cleanup from task cancellation; do not return before wait.
            cleanup = asyncio.create_task(self.close())
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
                raise

    def _kill(self):
        if self.process is not None and self.process.returncode is None:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    async def close(self):
        if self._expiry is not None:
            self._expiry.cancel()
        self._kill()
        if self.process is not None:
            await asyncio.wait_for(self.process.wait(), 5)

    async def _send(self, message):
        data = (json.dumps(message) + "\n").encode()
        if len(data) > 65_536 or self.process is None or self.process.returncode is not None:
            raise ConfinementDenied()
        self.process.stdin.write(data)
        await self.process.stdin.drain()

    async def request(self, method: str, params: dict | None = None) -> dict:
        """Serialized bounded JSON-RPC, not a Job authorization API."""
        async with self._lock:
            try:
                async with asyncio.timeout(5):
                    # Detect mount content/identity drift before every dispatch.
                    opened = self._open_files(self._validate())
                    for fd, _ in opened:
                        os.close(fd)
                    self._sequence += 1
                    await self._send({"jsonrpc": "2.0", "id": self._sequence,
                                      "method": method, "params": params or {}})
                    while True:
                        response = await self._receive()
                        if response.get("id") == self._sequence:
                            if "error" in response or not isinstance(response.get("result"), dict):
                                raise ConfinementDenied()
                            return response["result"]
            except asyncio.CancelledError:
                await self.close()
                raise
            except Exception:
                await self.close()
                raise ConfinementDenied() from None

    async def _receive(self):
        line = await self.process.stdout.readline()
        self._output_bytes += len(line)
        if not line or self._output_bytes > 1_048_576:
            raise ConfinementDenied()
        return json.loads(line)
