# Job MCP authorization and confinement contract

Review date: 2026-10-08. Source baseline: `a07ea4184b092f3065044ed35a6b5f47d5ec3390`.
The initial worktree was clean. Read alongside [spec.md](../spec.md) and the
[runtime dependency audit](runtime-dependency-audit.md#follow-up-closure-production-job-mcp-and-delegation).
This review preserves Personal Assistant features, existing security controls,
and the completed Automation design.

**Decision: initial Job MCP support is read-only. Implementation remains blocked.**
Only explicitly named, independently verified read-only server/tool pairs may
become eligible under the policy below. Mutation and unknown effects are denied,
even when a Job has native write/command permission or an approval. The initial
subset is local stdio, offline and without credentials; children inherit no MCP
grant. No actual server/tool pair is approved or enabled by this document. The
current effective set remains empty. This is an implementation and test contract,
not an implemented configuration format or authorization grant. Adding names to
the Runner allowlist alone would bypass its gates.

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

## Initial read-only authorization policy

The effective set must be the intersection of an operator-approved **Job MCP
policy**, a validated and persisted **Job selection**, the matching **server
configuration**, supported discovered **tool schemas**, and any **Skill
restrictions**. Each entry must identify exactly `mcp.<server>.<tool>`; no
wildcards, implicit selection of every configured server, or grants from model
output, tool annotations, descriptions or Skill text. The current effective set
is empty because the Job policy/selection do not exist.

The operator owns the reviewed policy and eligible identities. An authorized Job
submitter may select only a subset through a future validated service boundary;
host configuration alone cannot grant it. Every selected entry must satisfy all
of the following before becoming usable:

1. **Trusted grant and identity.** The operator policy names the eligible server
   and original tool, allowed workspaces, executable/dependency identity,
   arguments, environment sources, schema and effect-metadata identities,
   read-only review evidence, resource limits and confinement profile (including
   the exact mount manifest). Job submission may request a subset but
   cannot supply a command, credentials or a broader policy. The Job service must
   validate selection against operator policy and submitter permissions before
   persisting the grant. Persist its identity with the Job
   and pinned Automation/schedule snapshot. Revalidate against current policy
   before launch and each call: revocation denies; changed identity requires a
   fresh grant rather than silently applying broader configuration. Missing
   selections grant nothing. Duplicate or ambiguous server/tool identities deny;
   tool-name aliases cannot merge grants. Children receive no MCP grant.
2. **Launch permission and confinement.** Validate the pinned workspace before
   startup and again before calls. Confinement must cover startup, initialization,
   discovery, tool execution and descendants, with an approved working directory,
   runtime mounts, workspace access, private temporary storage, environment,
   network restrictions and resource/time/output limits. Initial support requires
   working Job namespace isolation (`bwrap` on the current host architecture),
   read-only workspace/runtime mounts, no network, no host sockets or credentials,
   and only bounded private ephemeral scratch writes. The MCP profile must be at
   least as restrictive as the Job's permissions and sandbox; native write/command
   grants do not make MCP mounts writable or authorize a general command tool.
   A Job selecting `process` cannot launch MCP under this policy: it must select
   supported isolation through the validated Job boundary or remain blocked.
   `process`, `cwd`, server promises, or JSON path checks alone cannot
   supply namespace confinement for arbitrary executable servers. Unavailable
   required isolation is a blocker: deny execution with no host fallback. Runtime
   mounts must come only from the operator-reviewed, pinned manifest within the
   Job's existing mount rules. Never add mounts from server requests, discovery,
   returned paths, model output or dependency auto-discovery. If the server needs
   a mount not already permitted, reject it and record a deployment blocker;
   do not widen native command allowlists, mounts, symlink rules or confinement.
3. **Verified read-only effects and complete metadata.** Require an exact tool
   name, supported input schema, explicit boolean `readOnlyHint: true`, and a
   trusted policy review bound to the executable/dependencies, schema and effect
   metadata. A server's hint or description is untrusted evidence and cannot
   itself grant eligibility. Review must establish effects for every permitted
   argument shape over startup, discovery, calls, background work and cleanup.
   Missing/false/non-boolean read-only hints, contradictory metadata, metadata
   drift or an unreviewed argument/effect mapping deny. Do not infer read-only
   from a name such as `get`, an absent hint, or successful transport tests.
   Reject workspace writes, persistent caches/index updates, command-capability
   tools, external mutations, destructive effects and effects that cannot be
   determined. Read-only means no persistent workspace/host/external mutation;
   bounded private scratch and Suto-owned audit/result persistence are the only
   permitted bookkeeping writes. A tool with optional write parameters is
   ineligible until a reviewed schema/adapter excludes all such paths. Any
   unexpected side-effect attempt is a policy violation, not read-only success.
4. **Job permission and workspace checks at each call.** Retain AgentRuntime
   schema/permission checks and ToolExecutor grants, and enforce the Job's
   `ExecutionContext.require_tool` boundary for the exact MCP name. Validate the
   pinned workspace/checkpoints before launch and revalidate workspace identity,
   Job state, current policy and arguments immediately before dispatch. A reviewed
   per-tool mapping must apply existing contained-path validation to every path
   argument; opaque or unmappable resource selectors deny. Traversal, outside
   symlinks, root replacement and broader server roots cannot bypass Job rules.
   Read-only calls need a valid read grant, not mutation approval. Neither an
   approval nor native write/command permission overrides read-only eligibility.
5. **Cancellation and uncertain outcomes.** Cancellation or timeout prevents new
   dispatches and terminates/awaits the server and descendants, including partial
   startup/discovery failure. Do not finalize cancelled Jobs as successful. A
   call interrupted after dispatch has an unknown outcome until confirmed;
   cleanup or an SDK error does not prove completion or rollback. Persist the
   interruption, reconcile outstanding calls after restart, and revalidate all
   gates before an existing Job retry/resume path repeats an eligible read. A
   repeated read may return newer data; do not mark a lost result as success or
   claim exactly-once reads. Suspected mutation blocks further MCP dispatch and
   automatic replay pending investigation, even for a tool labelled read-only.
6. **Fail-closed completeness.** Missing/malformed policy, unknown selection,
   mismatched server/schema/effect-metadata identity, absent environment sources,
   unsupported schemas, invalid workspace or unavailable confinement denies
   before the relevant process/call. Validate policy/config/launch prerequisites
   before any server starts; discovery happens only inside confinement. Missing
   or ambiguous discovered metadata denies before tool registration/dispatch and
   closes the selected server. A requested tool that fails discovery must produce
   an explicit Job failure/block, not a silently successful reduced-capability run.
   Unselected servers never start. Audit failures and unknown call outcomes
   cannot become success. Sanitize errors and audit metadata. Server output and
   returned content remain untrusted data, never additional authorization.

## Required call results and audit metadata

Use the durable Job attempt/event/result path, with a unique call correlation ID.
Before dispatch, commit the authorized call intent; if that write fails, do not
call the server. Record the terminal outcome separately so restart can identify
an intent without a confirmed result. Required records are:

- Job/attempt/call IDs, Automation/version and selection identity when present;
  exact server/original tool and exposed name; policy/config/executable/dependency,
  schema/effect-metadata and read-only review identities.
- Pinned workspace identity, sandbox profile and mount-manifest identity,
  permission/eligibility decision and a stable sanitized reason for any denial.
  Record launch/discovery failures and cleanup status even when no call is sent.
- Validated-argument correlation through a privacy-safe digest or protected
  reference, UTC timestamps, duration, dispatch state, bounded result size and
  outcome: confirmed success, tool error, transport error, denied, cancelled,
  timeout or unknown. Distinguish server-reported failure from transport failure.
- A bounded, sanitized result or protected artifact reference for confirmed
  success, plus safe result metadata needed to inspect it. Record truncation and
  cleanup/descendant termination failures explicitly. Required cleanup must be
  confirmed before successful Job completion.

Do not put credentials, raw environment values, raw private arguments/results,
stdout/stderr or sensitive paths into general logs/errors/prompts. A digest must
not expose low-entropy secrets; use protected references or keyed digests where
needed. Returned content remains untrusted and subject to existing disclosure
rules. If outcome persistence fails after dispatch, the Job cannot report
success; recovery must treat the outstanding intent as unknown. Audit writes
must preserve existing atomic Job transitions, isolation, retention and backup
semantics. No new storage design or schema is authorized here.

No new configuration keys, public commands, database fields or action types are
introduced by this document. Existing host MCP consumers retain their current
behavior; this contract applies to prospective durable Job support only.

## Remaining blockers before enabling read-only Job MCP

| Blocker | Required closure |
| --- | --- |
| Reviewed eligible identities | No actual server/tool set, immutable runtime identity, read-only review or permitted mount manifest is supplied. Name and review each exact pair; default remains empty. |
| Job grant and validation boundary | Public submission/options do not accept MCP selection. Add validated subset selection and pinned Job/Automation/schedule identities, revocation checks and per-call Job permission/workspace enforcement before exposure. |
| Confined launcher and supported deployment | Current stdio startup is a host process. Implement startup/discovery/calls/descendant confinement using the Job sandbox without additional server-requested mounts. If an eligible server cannot start safely with permitted mounts, it stays blocked. Real Bubblewrap tests were skipped because this host denies namespace creation; actual confinement must be proven on a supported host with no fallback. |
| Effect metadata and durable audit/recovery | Current adapter retains schema/name but no read-only effect classification; result metadata names only server/tool. Add reviewed metadata validation and the required durable intent/outcome/cleanup records, including unknown outcomes and audit failure handling. |
| Production-boundary evidence | Positive authorized queued/scheduled execution, read-only eligibility denials, real confinement, cancellation/restart and audit/privacy assertions below are missing. Contextless SDK success is insufficient. |

These are implementation/deployment/evidence blockers under a chosen read-only
policy, not permission to relax it. This documentation update enables no Job MCP
path and changes no native or non-Job MCP behavior.

Existing JSON Job options and version snapshots may accommodate a future bounded
selection without schema changes, but they currently reject MCP options. No
schema/migration change is justified or made here. If durable effect tracking
requires new storage later, explain that requirement and add migration tests
before enabling effects.

## Gates for future mutation support

Mutation remains out of scope until a separate contract and implementation pass
all of these gates; read-only grants must never silently become mutable:

1. Define reviewed per-tool effect adapters, bounded targets and preconditions.
   Preserve native write/command permissions, contained paths, hash/change budgets,
   committed change/command audit, checkpoints and completion verification.
   Define equivalent target scoping and confirmation for any external effect.
2. Use the Job's durable `require_approval` path, not the live ApprovalBroker.
   Bind the canonical approval digest to Job/action, exact server/tool,
   policy/config/schema/effect identities, validated arguments, workspace,
   confinement/limits and target preconditions (file before/after hashes where
   applicable). Keep previews safe. Pending approval permits no proposed effect;
   deny, expiry, cancellation, changed action/policy or missing callback blocks.
   Consume an exact approval once transactionally and recheck state immediately
   before dispatch. Grant only authority for that action; a persistent writable
   server or background task cannot inherit approval for arbitrary later effects.
3. Durably journal intent, approval consumption, dispatch and confirmed outcome.
   Define crash recovery for every gap, including approval consumed before send,
   send before acknowledgement, and effect committed before result persistence.
   Approval consumption and a remote effect cannot be assumed one transaction.
4. Provide a stable per-logical-action idempotency key honored by the effect
   endpoint, or a reliable reconciliation protocol that determines committed,
   uncommitted and unknown outcomes before any redispatch. Reuse the same identity
   across attempts/resume; never blindly retry an uncertain effect. If neither
   mechanism is available, that mutable tool remains disabled. Define conflict,
   partial-effect and operator recovery handling; compensation is a separate
   authorized action, not assumed rollback.
5. Prove cancellation/timeout/crash stops new dispatch, cleans up descendants,
   preserves cancelled state and records possible committed effects. Prove
   restart/retry/approval re-entry cannot duplicate a mutation or convert an
   unknown outcome into success. Use temporary storage, deterministic faults and
   real confinement coverage; preserve migration/backup/recovery safeguards if
   new durable effect storage is required.

## Acceptance evidence required for implementation

Use deterministic model fakes, a local stdio fixture and temporary SQLite stores.
The positive path must use public submission → ordinary Worker claim → ordinary
JobRunner → shared executor → AgentRuntime → confined real MCP transport →
confirmed read-only result → cleanup, then reopen storage and assert
attempts/result/audit and unchanged workspace/protected host state.
Do not inject an MCP allowlist directly into a test ExecutionContext as evidence
of public Job authorization. Verify pinned Automation/scheduled Jobs too.

| Criterion | Required production-boundary evidence still missing |
| --- | --- |
| Authorized execution | Operator-reviewed exact server/tool and validated Job selection execute an offline read with explicit read-only metadata. Confirm bounded output, committed audit and unchanged persistent state for queued and pinned scheduled Jobs. Unselected servers never launch. |
| Selection/permission denial | Unselected names, wildcards, ambiguous names, forged selections, wider Skills/child grants, revoked/changed identities and missing/denied Job permissions cause no forbidden startup/call. Native write/command permissions and even an approval cannot admit a mutable MCP tool. |
| Read-only classification | Missing/false/malformed read-only hint, contradictory or changed metadata, unknown effects, persistent cache writes, external/command mutations and optional write arguments deny registration/dispatch. A claimed read-only tool that attempts an effect is blocked by confinement and fails with a policy violation. |
| Mount authority | A selected server requests extra runtime mounts during initialization/discovery/calls or returns paths suggesting them: mount manifest remains unchanged, no host relaunch occurs, and an unmet runtime requirement blocks. Test startup that needs a forbidden dependency mount. |
| Cancellation/restart | Events coordinate cancellation during startup, discovery and calls; assert terminated descendants, no later dispatch and durable cancelled state. Faults before/after dispatch and before result commit produce explicit outstanding/unknown records. Reopened storage permits only revalidated read retries; suspected mutation blocks replay. |
| Confinement | Real namespace tests exercise startup and calls attempting workspace/host writes, persistent index updates, traversal, external symlinks, workspace rebinding, protected-file access, network/socket access and descendant survival; assert denied effects. Test process-only/missing/host-denied sandbox with zero MCP server launch and no fallback. Skips do not satisfy this gate. |
| Invalid inputs | Missing/ambiguous policy/config/effect review/server/env, changed or unsupported schemas, unmappable paths, malformed selection and partial discovery failure block without false success or private diagnostic leakage; assert cleanup. |
| Audit/results | Reopen storage and correlate intents, decisions, outcomes and cleanup for success, tool/transport errors, denials, timeout and cancellation. Audit failure before send causes zero calls; failure after send prevents success and survives restart as unknown. Assert bounded output, truncation markers and no secrets/private content in general logs/errors. |
| Future mutation gate only | Before separate mutation enablement, test pending/approve/deny/expiry/changed action or identities, missing callback, single consumption and zero preapproval effects. Inject cancellation/crash at every journal/dispatch/effect/result gap; assert reconciliation/idempotency prevents duplicates, and unsupported recovery keeps the tool disabled. |

Existing tests establish default Job denial, direct MCP transport/filtering and
cleanup, native durable approvals and native command sandbox behavior. They do
**not** prove successful authorized Job MCP execution, MCP durable approvals or
MCP namespace confinement. This review intentionally adds no runtime/test code
and claims none of that missing positive evidence.

## Historical verification of the retained baseline

The following results were recorded by the preceding contract review against
`2f6422ead11cd714bdf2d47e16160fdd62e64435`; they were not rerun for this
documentation-only policy update. No tests or assertions were changed:

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
| Previous review's diff and whitespace check | Only this contract and its link in the dependency audit changed; `git diff --check` passed. |

Both skips are the real Bubblewrap tests at `tests/tools/test_sandbox.py:62` and
`:104`: host policy prevents namespace creation, including outside the execution
sandbox. Namespace confinement remains unverified on this host. Passing the
retained suite does not supply the missing positive Job MCP evidence above.
No runtime, Personal Assistant, spec, schema or migration changes were made.

This policy update changes only `docs/job-mcp-contract.md`. Its verification is
document review, changed-file scope inspection, `git diff` and
`git diff --check`; runtime tests are not rerun because no executable behavior
changes. Historical passing tests do not certify this prospective policy as
implemented or enable any MCP server/tool for Jobs.
