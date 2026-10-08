# Job MCP authorization and confinement contract

Review date: 2026-10-08. Source baseline: `2f6422ead11cd714bdf2d47e16160fdd62e64435`.
The initial worktree was clean. Read alongside [spec.md](../spec.md) and the
[runtime dependency audit](runtime-dependency-audit.md#follow-up-closure-production-job-mcp-and-delegation).
This review preserves Personal Assistant features, existing security controls,
and the completed Automation design.

**Disposition: stop before implementation.** No MCP server/tool is authorized
for production Jobs today. The conditional contract below specifies the gates
required for future support; it is not an implemented configuration format or
an authorization grant. Server eligibility, startup authority, and the handling
of state-changing effects need decisions that the repository does not supply.
Adding names to the Runner allowlist would bypass those missing gates.

## Current production flow and enforcement

| Boundary | Execution and enforcement | MCP consequence |
| --- | --- | --- |
| Submission | CLI `commands._run` / REST `submit_job` → [JobService.submit](../application/automation.py) → `validate_workspace` → `JobStore.create_job`. REST accepts prompt/workspace/write/command fields only. [validate_options](../workflows/runtime/options.py) rejects unknown options. | There is no public Job MCP selection or policy field. Host configuration cannot grant a Job permission. |
| Automation and schedule | Validated definitions and immutable versions preserve workspace, write/command flags, options and Skill versions. Scheduler materializes queued Jobs from pinned snapshots. | Pinned Skill instructions are prompt data, not MCP authorization. |
| Claim and cancellation | [AutomationWorker.start](../workflows/runtime/worker.py) acquires the database lock, recovers attempts, ticks the scheduler and atomically claims Jobs before `JobRunner.run`. Cancellation commits cancelled state and cancels active tasks; independent workers observe stored cancellation. | Any future MCP launch/call must observe Job cancellation and own cleanup through this lifecycle. |
| Job context | [JobRunner.run](../workflows/runtime/runner.py) checks workspace checkpoints, assembles native tools from stored permissions/options, and creates `ExecutionContext` with durable approval, budget and audit callbacks. | The allowlist contains no MCP names. Queued and scheduled Jobs both exclude MCP. |
| Before server startup | [execute_local_ai](../ai/executor.py) loads explicit host configuration, then [eligible_mcp_config](../ai/execution/request.py) intersects host tool names, Job allowlist and active Skill restrictions before `MCPManager.start`. | For ordinary Jobs the intersection is empty, so no MCP server starts. Invalid explicit configuration still fails the request because loading precedes filtering. |
| Startup/discovery | [MCPManager](../mcp_integration/manager.py) starts selected clients, adapts schemas, filters exposed tools, and closes failed clients. [StdioMCPClient.connect](../mcp_integration/client.py) passes command/args/environment to the SDK. | Startup is a host process, without Job workspace, sandbox or approval callbacks. Startup/discovery themselves can have effects. Failed servers expose no tools; other servers may remain available. |
| Tool authorization | [ModelToolLoop](../ai/execution/loop.py) registers selected tools and constructs explicit tool-name `allow` rules. [AgentRuntime](../agent/runtime.py) validates arguments, checks PermissionEngine and the authorization hook, then obtains a [ToolExecutor](../tools/executor.py) grant. Missing/invalid permission decisions deny. | This authorizes a tool name and validated arguments; it does not enforce MCP action effects or confinement. The live ApprovalBroker is separate from durable Job approval. |
| Tool side effect | [MCPTool.execute](../mcp_integration/adapter.py) directly calls `client.call_tool`. Errors are sanitized. Executor cleanup closes the manager in `finally`. | No `ExecutionContext.require_tool`, durable exact-action approval, contained path validation or sandbox is applied by the MCP adapter. |
| Native safeguards to preserve | [ExecutionContext](../workflows/runtime/context.py), [workspace paths](../capabilities/developer/workspace/paths.py), approved patch/command handlers and [Sandbox](../sandbox/runtime.py) enforce native Job permissions, workspace identity, exact approvals and selected command isolation. | These safeguards do not automatically cover MCP startup or calls. `process` has no filesystem/network namespace isolation; `bwrap` is opt-in for commands and fails without fallback. |
| Result/retry/recovery | Runner commits result/error/attempt state and checks write verification. Retry guards inspect native change/command events; checkpoints validate recorded file hashes. Durable status transitions create notifications. | Arbitrary MCP effects are not recorded in those native event/checkpoint contracts. A successful or ambiguous MCP call cannot safely inherit their retry/completion assumptions. |

[Host MCP configuration](../mcp_integration/config.py) currently requires an
explicit absolute, regular, non-symlink configuration file, bounded size and
non-group/world-writable permissions on POSIX. It accepts stdio, an absolute
command, string arguments, environment-variable references and explicit tool
names. It rejects unknown keys and malformed values. These are configuration
guards, not immutable executable/dependency identity, per-Job capabilities,
action classification or namespace guarantees. No private MCP configuration,
credentials, installed extensions or user database was inspected.

## Conditional authorization contract

The effective set must be the intersection of an operator-approved **Job MCP
policy**, a validated and persisted **Job selection**, the matching **server
configuration**, supported discovered **tool schemas**, and any **Skill
restrictions**. Each entry must identify exactly `mcp.<server>.<tool>`; no
wildcards, implicit selection of every configured server, or grants from model
output, tool annotations, descriptions or Skill text. The current effective set
is empty because the Job policy/selection do not exist.

Every selected entry must satisfy all of the following before becoming usable:

1. **Trusted grant and identity.** The operator policy names the eligible server
   and original tool, allowed workspaces, executable/dependency identity,
   arguments, environment sources, schema identity, action class, resource
   limits and confinement profile. Job submission may request a subset but
   cannot supply a command, credentials or a broader policy. A defined authority
   must grant this selection at submission. Persist its identity with the Job
   and pinned Automation/schedule snapshot. Revalidate against current policy
   before launch and each call: revocation denies; changed identity requires a
   fresh grant rather than silently applying broader configuration. Children
   receive no MCP grant unless an explicit bounded inheritance rule is added.
2. **Launch permission and confinement.** Validate the pinned workspace before
   startup and again before calls. Confinement must cover startup, initialization,
   discovery, tool execution and descendants, with an approved working directory,
   runtime mounts, workspace access, private temporary storage, environment,
   network restrictions and resource/time/output limits. A tool allowlist cannot
   contain a server that already has unrestricted write/network authority before
   approval. `process`, `cwd`, server promises, or JSON path checks alone cannot
   supply namespace confinement for arbitrary executable servers. Unavailable
   required isolation denies execution with no host fallback. Never widen the
   existing native command allowlist or sandbox mounts to accommodate a server.
3. **Explicit effect policy.** A reviewed, confined read-only tool may use a read
   rule only when its full lifetime cannot mutate the workspace or external
   state. Workspace mutation must stay within existing write permissions,
   path/hash/change-budget checks, committed change audit and verification.
   Command execution must preserve command permissions and executable/argument
   restrictions. Unknown effects, destructive actions, ambiguous parameter
   mappings and unconfined external effects deny. An arbitrary MCP tool must not
   be relabelled `read` or `command` to bypass these safeguards.
4. **Durable exact-action approval.** For any admitted state-changing operation,
   use the Job's durable `require_approval` path before granting the relevant
   side-effect authority. The canonical digest must bind server/tool identity,
   policy/config/schema identity, validated arguments, workspace, confinement,
   limits and relevant target preconditions (including before/after hashes for
   file changes). Persist only safe summaries/previews, not credentials or raw
   private arguments. Pending approval pauses without the proposed effect;
   rejection, expiry, cancellation, changed action or changed policy denies or
   requires fresh approval. Consumption is exact, single-use and transactional.
   Recheck Job state, policy and workspace immediately before dispatch. Approval
   must not allow other calls or writable background activity by a live server.
5. **Cancellation and uncertain outcomes.** Cancellation or timeout prevents new
   dispatches and terminates/awaits the server and descendants, including partial
   startup/discovery failure. Do not finalize cancelled Jobs as successful. A
   remote or already committed effect cannot be assumed undone. Record dispatch
   and outcome durably enough that retry/restart/approval re-entry cannot repeat
   an uncertain mutation. Until a safe reconciliation/idempotency contract exists,
   such actions remain disabled; cleanup or an SDK error does not prove rollback.
6. **Fail-closed completeness.** Missing/malformed policy, unknown selection,
   mismatched server or schema identity, absent environment sources, unsupported
   schemas, invalid workspace or unavailable confinement denies before the
   relevant process/call. A requested tool that fails discovery must produce an
   explicit Job failure/block, not a silently successful reduced-capability run.
   Unselected servers never start. Audit failures and ambiguous mutation outcomes
   cannot become success. Sanitize errors and audit metadata. Server output and
   returned content remain untrusted data, never additional authorization.

No new configuration keys, public commands, database fields or action types are
introduced by this document. Existing host MCP consumers retain their current
behavior; this contract applies to prospective durable Job support only.

## Decisions required before enabling a production path

| Decision | Repository gap and required choice |
| --- | --- |
| Initial eligible server/tool effects | Choose the actual supported subset: an initial offline, read-only server/tool set, or state-changing/credentialled/external tools. Name the reviewed servers and schemas and the authority that grants Job selection. The repo supplies no such approved set. Recommended first scope: explicit offline, read-only tools with no credentials and no inheritance to children. This recommendation is not an active grant. |
| Startup authority and deployment confinement | Decide whether Job MCP must require working Bubblewrap and which immutable runtime/dependency mounts are permitted. Recommended: require namespace isolation, read-only workspace and no network for the initial subset, failing on unsupported hosts. Supporting other platforms or external-service tools requires a separately reviewed confinement design; treating trusted host startup as confined would relax the requested controls. |
| Mutations, approvals and recovery | If writes/commands/external effects are required, define per-tool effect adapters and when authority is granted, how native audit/checkpoint/verification requirements are preserved, and how uncertain effects are reconciled after cancellation/crash. Persistent arbitrary server writes cannot satisfy exact-action approval simply by approving one JSON call. Decide the replay/idempotency contract before such tools are eligible. |

Even the recommended read-only subset requires a Job policy/selection boundary,
configuration identity pinning, a context-aware server launcher and real
confinement coverage. General mutation support additionally spans approvals,
audit, retry/recovery and completion verification. These changes cross the
current MCP transport and Job lifecycle boundaries; they cannot be implemented
safely as a small allowlist/adapter switch with repository evidence alone.

Existing JSON Job options and version snapshots may accommodate a future bounded
selection without schema changes, but they currently reject MCP options. No
schema/migration change is justified or made here. If durable effect tracking
requires new storage later, explain that requirement and add migration tests
before enabling effects.

## Acceptance evidence required for implementation

Use deterministic model fakes, a local stdio fixture and temporary SQLite stores.
The positive path must use public submission → ordinary Worker claim → ordinary
JobRunner → shared executor → AgentRuntime → real MCP transport → confirmed
effect/result → cleanup, then reopen storage and assert attempts/result/audit.
Do not inject an MCP allowlist directly into a test ExecutionContext as evidence
of public Job authorization. Verify pinned Automation/scheduled Jobs too.

| Criterion | Required production-boundary evidence still missing |
| --- | --- |
| Authorized execution | Approved exact server/tool executes; confirmed output and committed state match. Unselected servers are never launched. |
| Denial | Model-requested unselected servers/tools, forged selections, wider Skills/child grants and configuration changes cause no forbidden startup/call. |
| Approval | If mutable tools are admitted: pending/approve/deny/expiry/changed arguments/config/schema, single consumption and missing callback; assert no effect before approval. Read-only-only eligibility must explicitly reject mutable tools. |
| Cancellation/restart | Events coordinate cancellation during startup, discovery, approval wait and calls; assert terminated descendants and durable cancelled state. Crash/restart and ambiguous effects do not cause automatic duplicate mutation. |
| Confinement | Real namespace tests exercise startup and calls attempting traversal, external symlinks, workspace rebinding, protected-file access, outside writes, network access and descendant survival; assert denied effects. Test missing/host-denied sandbox with zero server launch and no fallback. |
| Invalid inputs | Missing policy/config/server/env, changed or unsupported schemas, malformed selection and partial discovery failure block without false success or private diagnostic leakage. |

Existing tests establish default Job denial, direct MCP transport/filtering and
cleanup, native durable approvals and native command sandbox behavior. They do
**not** prove successful authorized Job MCP execution, MCP durable approvals or
MCP namespace confinement. This review intentionally adds no runtime/test code
and claims none of that missing positive evidence.

## Verification of the retained baseline

Commands run with the existing virtual environment; no tests or assertions were
changed:

```bash
venv/bin/pytest -q -rs --tb=short tests/mcp/test_integration.py tests/agent/test_runtime.py tests/agent/test_permissions.py tests/automation/test_approvals.py tests/automation/test_worker_runtime.py tests/interfaces/test_runtime_api.py tests/tools/test_workspace_tools.py tests/tools/test_command_tools.py tests/tools/test_sandbox.py
venv/bin/pytest -q -rs
git diff --check
```

| Check | Result |
| --- | --- |
| Focused command inside execution sandbox | 97 passed, 29 failed, 2 skipped in 10.85s. All failures were Runtime API tests denied local socket creation (`PermissionError: Operation not permitted`). |
| Same focused command outside execution sandbox | 126 passed, 2 skipped in 10.92s. No test exclusions or code changes. |
| Full suite outside execution sandbox | 828 passed, 2 skipped in 49.35s. |
| Final diff review and whitespace check | Only this contract and its link in the dependency audit changed; `git diff --check` passed. |

Both skips are the real Bubblewrap tests at `tests/tools/test_sandbox.py:62` and
`:104`: host policy prevents namespace creation, including outside the execution
sandbox. Namespace confinement remains unverified on this host. Passing the
retained suite does not supply the missing positive Job MCP evidence above.
No runtime, Personal Assistant, spec, schema or migration changes were made.
