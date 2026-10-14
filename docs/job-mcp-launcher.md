# Confined MCP launcher primitive

`mcp_integration/confined.py` is an internal, unregistered transport primitive.
It is deliberately disconnected from JobRunner, the AI request filter and the
host MCP client. **Jobs still have an empty effective MCP set.** A launcher test
is not production Job authorization or evidence of read-only effect review.

The caller supplies a persisted selection, exact selected name, operator-owned
manifest artifact and reviewed command. Current operator policy must pin the
manifest identity; workspace read permission and `bwrap` are mandatory. Policy,
workspace device/inode and manifest/content identities are checked at startup
and each request. Revocation, drift and invalid requests close the server. There
is no server-requested mount API or dependency auto-discovery. This primitive
does not replace remaining config/executable/dependency/schema/effect/limit
identity gates, per-tool argument mapping, Job state checks or durable journaling.

The explicit canonical artifact must be outside the workspace, operator-owned,
non-symlink and not group/world writable, at most 64 KiB. Its canonical JSON
SHA-256 must match `mount_manifest_identity`:

```json
{"format_version":1,"files":{"/absolute/canonical/file":"sha256-of-file-bytes"}}
```

Only exact regular files are accepted, at most 512 entries, 64 MiB per file and
256 MiB total. They must be inside the pinned workspace or native runtime mount
ceilings (`/usr`, resolved `/bin`, `/lib`, `/lib64`, runtime Python prefix).
Hidden path components, directories, sockets, FIFOs and symlinks are rejected.
Operator review must exclude credentials/private runtime data even in these
ceilings; the launcher never mounts a runtime directory or host environment.
File bytes are frozen in sealed anonymous snapshots and copied into read-only
namespace mounts. No entire workspace mount is added. Native write permission
cannot make any MCP file writable. This intentionally narrow manifest may make
some servers undeployable; an unmet dependency is a blocker.

Bubblewrap namespace probing precedes server startup. Required flags failing at
launch also deny before the server can execute. There is no process fallback.
The process runs in fresh user/PID/network/IPC/UTS namespaces with dropped
capabilities, further user namespace creation disabled, cleared environment and
only synthetic proc/dev. Root/proc/dev are read-only. Private `/tmp` is a 16 MiB
tmpfs, disappears with the namespace, and is the only persistent-file scratch
surface. No host sockets, home, environment sources or scratch directory are
bound. Only stdio and manifest descriptors are inherited by Bubblewrap; mount
descriptors are consumed before server execution.

Fixed conservative transport ceilings currently apply: 30 seconds lifetime/CPU,
1 GiB address space, 64 processes (per-UID rlimit, not a per-Job cgroup quota),
1024 descriptors, 16 MiB file size, 5 seconds per request, 64 KiB per message and
1 MiB cumulative stdout. Stderr is discarded. The serialized JSON-RPC primitive
supports initialization, discovery and calls within one confined lifetime; it
does not support general SDK extensions. Cancellation, timeout, partial startup,
protocol/output failure and context exit kill/await the launcher. PID namespace
teardown owns detached descendants; cleanup failures propagate. There is no
automatic replay or success inference after interruption.

Run local deterministic boundary tests and real proof separately:

```bash
venv/bin/pytest -q tests/mcp/test_confined_launcher.py
SUTO_REQUIRE_MCP_NAMESPACE=1 venv/bin/pytest -q -rs tests/mcp/test_confined_namespace.py
venv/bin/pytest -q -rs
```

The required command **fails** if Bubblewrap or namespaces are unavailable.
Normal full-suite runs may report these integration tests as skipped; those
skips provide no confinement evidence. `.github/workflows/mcp-namespace.yml`
runs the required command on Ubuntu 22.04 with checksum-pinned Bubblewrap 0.11.1,
without test exclusions or namespace
policy overrides. A host that denies user namespaces must report failure and be
replaced with a supported runner; do not relax the launcher or disable the tests.
This checkout has no passing CI evidence until that workflow actually runs.

Fixture manifests enumerate a small test runtime explicitly; test-only dependency
enumeration is never available to a server or production launcher. The fixture
attempts effects at startup, discovery and calls, uses private bounded scratch,
and spawns a detached TERM-ignoring descendant. Real tests assert blocked
workspace/host/runtime writes, protected reads, traversal/symlink escapes,
network and host socket access, startup failure and cancellation at all three
stages. These remain launcher evidence only: durable cancellation/restart/audit,
effect classification and positive queued/scheduled execution gates are open.

## Checkout verification

Started on clean `main` at `7398927`. On 2026-10-14, focused MCP/native
sandbox/workspace/command/permission/worker tests reported **129 passed,
8 skipped**; the full suite reported **909 passed, 8 skipped**. Six skips are
the new real launcher tests, two are existing native Bubblewrap tests. All are
namespace-unavailable skips, not passing isolation evidence. The required
`SUTO_REQUIRE_MCP_NAMESPACE=1` command was also run outside the tool sandbox:
**6 setup errors**, each reporting that host policy prevents namespace creation.
No confinement success is claimed. Supported CI execution is still pending;
the workflow has been prepared but not run or pushed from this checkout.
