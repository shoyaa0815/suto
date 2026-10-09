# Runtime dependency audit

## Personal Memory separation update — 2026-10-09

Implemented from clean `main` at `8058463`. The dependency tables and test
counts below are historical audit evidence; this update supersedes their
Personal Memory runtime edges and the statement that nothing was removed.

- `JobStore` now composes
  [`SessionSummaryStore`](../assistant/conversations/summaries.py), with its
  model in `assistant.conversations.models`. The original summary CRUD SQL,
  `/runs` ownership/history and `SessionStore.compact` transaction are preserved.
  `assistant.memory.models.SessionSummary` re-exports the same class.
- Personal memory CRUD is detached from `JobStore`. Memory tool schemas,
  handlers, aliases and guidance are absent from mode/tool registration and
  request assembly. Request preparation no longer calls `MemoryRetriever` or
  injects personal memory records, even for legacy `AssistantContext` requests.
  The memory-specific executor failure branch is consequently removed.
- `ContextManager`, `Retriever`, `RetrievalResult`, `RetrievalError`, attachment
  retrieval and workspace `KnowledgeStore`/index/search are retained. The
  retrieval facade lazily supports the legacy `MemoryRetriever` import without
  loading personal memory on the runtime startup path.
- `MemoryStore` and explicit legacy service/tool/retriever imports remain for
  compatibility, outside runtime composition/registration. Its mixed
  `clear_user_memories` behavior is unchanged and unavailable on `JobStore`.
  No tables, FTS triggers, schema versions, migrations or user data are removed.
  Private extensions and persisted external tool references have not been
  inventoried; this is no claim that dormant legacy modules can now be deleted.
- Personal tasks/reminders, Job/Automation/Schedule semantics, permissions,
  approvals, sandbox and MCP policy are outside this change. Jobs still receive
  no MCP tools.

Regression evidence is in
[`test_memory_separation.py`](../tests/automation/test_memory_separation.py)
(cold imports, absent CRUD/registration, all eight memory names rejected by the
real JobRunner/executor, successful workspace indexing/search and unchanged
legacy rows), [`test_sessions.py`](../tests/assistant/test_sessions.py)
(compaction/restart and old-schema memory/FTS preservation),
[`test_api.py`](../tests/interfaces/test_api.py)
(`/runs` summary continuation, persisted result/SSE and restart), and
[`test_ai_core.py`](../tests/ai/test_ai_core.py)
(a broken legacy memory index cannot block session execution).

Validation used temporary SQLite databases and deterministic model fakes:

| Check | Result |
| --- | --- |
| Focused memory/session/AI/mode/workspace/worker/API/run-persistence/CLI tests (`venv/bin/pytest -q -rs --tb=short` with explicit paths) | **150 passed in 13.75s**, no skips |
| Full suite (`venv/bin/pytest -q -rs`) | **835 passed, 2 skipped in 50.44s** |
| Diff review and `git diff --check` | Passed; no schema/migration, permission or sandbox edits |

The local API socket probe was denied inside the command sandbox; focused and
full integration checks ran outside it. Both full-suite skips are the existing
real Bubblewrap tests at `tests/tools/test_sandbox.py:62` and `:104`: host policy
prevents namespace creation. Real namespace isolation remains unverified here.

## Historical audit baseline

Job authorization/tool/helper follow-up audited on 2026-10-08 against latest
commit `2bccb9a865ecfd9731795c9690ebf93f74ef60b1` (`docs: clarify shared runtime
dependencies and MCP audit gaps`), with a clean initial `git status --short`.
[spec.md](../spec.md) is the governing runtime refactor contract. This audit
preserves the completed Automation design and documents current dependencies;
it does not implement the target architecture.

## Scope and method

Read the repository instructions, `spec.md`, `README.md`,
[operations](operations.md), [agent runtime](agent-runtime.md),
[Skills](skills.md), [AI package](../ai/README.md), and
[developer capability](../capabilities/developer/README.md) documentation.
Enumerated Python modules and inspected imports with Python AST, then followed
call sites, tool registration/filtering, SQL relationships, lifecycle handlers,
and corresponding tests. The component tables cover all nine requested
directories; supporting packages are included where those paths depend on them.
Package `__init__.py` files follow their exports' classifications, with mixed
facades classified Shared. The original inventory was recorded against
`10bbc73d540b67624d9d1529d7a6f6c41893675e`. The preceding follow-up at
`2bccb9a` covered assistant dependencies and MCP reachability. This round
directly rechecks only the Needs Review items for Job MCP authorization, generic
native tools and operational helpers: submission, stored permissions/options,
Worker/Runner, executor selection, AgentRuntime authorization, concrete side
effects and the tests supporting those boundaries. It also checks which helper
implementations must survive Personal Assistant removal. Other classifications
remain the earlier inventory, not a new whole-repository implementation audit.
Remaining Shared labels below are resolved retention decisions with known
callers, not unanswered removal questions.

Evidence below refers to repository code and deterministic tests. No live LLM,
external service, private configuration, user Skill directory, or user database
was inspected. Test-created temporary databases are verification fixtures.
Absence of a repository call site is not proof that an installed extension or
external Python caller does not use an exported API.

| Classification | Meaning in this audit |
| --- | --- |
| Core Automation | Executes or safeguards the Job/Automation lifecycle required by the contract. Keep. |
| Shared | Used by runtime and assistant adapters, or currently required by common imports, store composition, configuration, or API contracts. Preserve until callers are separated. |
| Personal Assistant Only | Its observed behavior serves chat, personal memory/tasks/reminders, or voice; any import coupling still needs removal before deleting its module. |
| Optional | Advanced behavior the contract explicitly permits disabling. Keep existing persistence and lifecycle dependencies until isolated. |
| Unknown / Needs Review | Repository evidence does not justify a removal decision or establish the required execution path. Keep and record the unresolved question. |

## Current dependency map

```mermaid
flowchart TD
    Main[main.py] --> CLI[CLI backend and command registry]
    Main --> API[Local runs and durable Runtime API]
    Main --> Worker[Standalone automation worker]
    Main --> Voice[Voice adapter]
    Main --> Web[Dashboard and settings]
    CLI --> Services[Job / Automation / Schedule / Approval services]
    Services --> Store[JobStore and SQLite]
    API --> Services
    Worker --> Scheduler[Scheduler]
    Scheduler --> Store
    Worker --> Runner[JobRunner]
    Runner --> Store
    Runner --> Executor[execute_local_ai]
    CLI --> Executor
    API --> Executor
    Voice --> Executor
    Executor --> Prep[Request preparation and tool assembly]
    Prep --> Context[ContextManager and Skill instructions]
    Prep --> Personal[Conditional personal context and memory]
    Prep --> MCP[MCP manager and allowlists]
    Executor --> Loop[ModelToolLoop and runtime hooks]
    Loop --> Runtime[AgentRuntime]
    Runtime --> LLM[LLM router and provider adapters]
    Runtime --> Registry[ToolRegistry / ToolExecutor]
    Runtime --> Permission[PermissionEngine]
    Registry --> Workspace[Job workspace / plan / command tools]
    Workspace --> Approval[ExecutionContext and durable exact-action approvals]
    Approval --> Store
    Workspace --> Sandbox[Process or Bubblewrap sandbox]
    Loop --> Store
    Store --> Inbox[Job notification trigger and persistent inbox]
    Inbox --> Delivery[CLI notify_cli]
    CLI --> Reminder[Separate personal reminder loop]
    Store --> Mixins[Identity / Conversation / Memory / Task stores]
    API --> Sessions[Sessions and identity]
    Voice --> Sessions
    CLI --> Sessions
    Sessions --> Store
```

This is the observed graph, not a proposed redesign. Personal prompt retrieval
and personal tool handlers are conditional; their imports are not conditional.
The Worker itself does not import CLI, conversation, or reminder modules.
Its production owner is now `application/worker.py`, dispatched by `main.py worker`; the CLI owns notification presentation and personal reminder delivery.
The MCP edge represents executor integration; production Job authorization is
restricted as detailed below, so this graph does not assert Job MCP success.

### End-to-end paths and evidence

| Path | Observed execution, persistence, failure handling, and result |
| --- | --- |
| Job submission | [`commands._run`](../interfaces/cli/commands.py#L234) or [`POST /jobs`](../interfaces/api/runtime.py#L88) → [`JobService.submit`](../application/automation.py#L51) validates prompt/secrets/workspace → `JobStore.create_job` persists a queued job → optional worker wake. API services have no in-process worker; the independent worker polls durable state. CLI/API return queued state. [`test_external_job_service_worker_runtime_and_persisted_result`](../tests/interfaces/test_runtime_api.py#L59) verifies submission, runtime tool execution, persisted result and notification after reopening storage. |
| Saved automation | [`AutomationService`](../application/automation.py#L220) → definition validation → `create_automation` / `revise_automation` / `create_automation_job` → immutable version records, rendered parameters, workspace and permission snapshot. [`JobRunner`](../workflows/runtime/runner.py#L208) resolves the job's pinned automation and Skill versions. Covered by `test_reusable_automations.py` and `test_scheduled_automation_execution.py` under `tests/automation/`. |
| Schedule and retry | [`ScheduleService`](../application/automation.py#L136) → [`Scheduler.create/create_automation/tick`](../workflows/runtime/scheduler.py#L170) → [`fire_schedule`](../workflows/storage/store.py#L2197) or `retry_trigger`. Occurrence identity, materialized job, parameters, trigger history and schedule advancement commit atomically. Retry uses the same logical job and a new claimed attempt. UTC/ZoneInfo and stored schedule timezone govern timing. Tests cover pinning, explicit upgrade, missed runs, rollback, concurrent ticks and crash boundaries in `tests/automation/test_scheduled_automation_execution.py`. |
| Worker and recovery | [`AutomationWorker.start`](../workflows/runtime/worker.py#L113) acquires the database's advisory lock before recovery, heartbeat, child reconciliation, scheduler tick, retention and atomic job claim. [`claim_next_job`](../workflows/storage/store.py#L772) enforces quotas/concurrency and commits the attempt record. Runner cancellation persists interruption unless already cancelled; worker shutdown cancels/awaits tasks and releases ownership. `tests/automation/test_automation.py` and `test_hardening.py` cover cancellation, safe resume, locks, backup, migration rollback and restart. |
| AI execution | [`JobRunner.run`](../workflows/runtime/runner.py#L85) builds a pinned `ExecutionContext`, budgets and durable callbacks, then passes an `AgentRequest` to `execute_local_ai` without `AssistantContext`, conversation history or session ID. Executor → `prepare_request` → `build_runtime_tools` → `ModelToolLoop` → `AgentRuntime` → provider/tool rounds. Completion checks budget, children, verification after writes and durable plan status before committing a job result. Transient retries stop after side effects. Covered by `tests/ai/test_ai_workspace.py`, `tests/automation/test_automation.py`, and `tests/automation/test_scheduled_automation_execution.py`. |
| Durable job approval | Workspace write/command handler → [`ExecutionContext.require_approval`](../workflows/runtime/context.py#L114) → [`JobRunner.require_approval`](../workflows/runtime/runner.py#L156) → [`request_or_consume_approval`](../workflows/storage/store.py#L1304). Unapproved exact action persists a request and moves the job to waiting approval; no action is executed. CLI `ApprovalService.decide` validates the request through the store, optionally wakes the worker, and the next execution consumes one matching, unexpired approval. Denial, changed actions, expiry and cancellation invalidate/block execution. Covered by `tests/automation/test_approvals.py` and the scheduled-job approval test. |
| Notification | Committed job status transition → [`job_notification` trigger](../workflows/storage/migrations.py#L392) → `notifications` → [`OperationsStore` queries/ack](../workflows/storage/operations.py#L153) → optional [`notify_cli`](../interfaces/cli/operations.py#L88), enabled by `SUTO_NOTIFY_CLI`. Delivery uses fixed safe summaries, suppresses obsolete approval messages, acknowledges after output, and retries acknowledgement without reprinting in the same session. Inbox creation does not require a live CLI. Covered by `tests/automation/test_result_notifications.py` and `tests/interfaces/test_cli_live_notifications.py`, including transaction rollback and offline/restart delivery. |

## Component classification

### Assistant, agent, and application

| Component | Classification | Dependency evidence and decision |
| --- | --- | --- |
| `assistant/context.py`, assistant facade | Shared | Executor/request/assembly and CLI/API/voice still import `AssistantContext`; JobRunner supplies none. This is a known import contract, not required personal context for Jobs. `DeliveryTargetContext` is Personal Assistant Only data. See the assistant call-site closure below before removing either export. |
| `assistant/identity/{models,store}.py` | Shared | JobStore imports/inherits IdentityStore at [store.py:10](../workflows/storage/store.py#L10) and [store.py:80](../workflows/storage/store.py#L80). API startup [server.py:163](../interfaces/api/server.py#L163), CLI and voice resolve/apply identity for isolated legacy sessions. Schedule defaults now come from RuntimeSettings, not `context.user.timezone`; [commands.py:425](../interfaces/cli/commands.py#L425) and [configuration regression](../tests/automation/test_runtime_configuration.py#L128) establish that separation. Preserve identity for those adapters; personal preferences are a separate removal candidate. |
| `assistant/conversations/{models,store}.py` | Shared | JobStore composition plus SessionStore and legacy `/runs` still require conversation ownership/history. API [find_run](../interfaces/api/server.py#L280) checks both user and interface before reads/cancellation/SSE. Durable `/jobs` uses separate job/attempt state and no session ID. Keep the module until legacy adapters are separated; see assistant closure and persistence boundaries. |
| `assistant/memory/{service,tools}.py`; personal memory records | Personal Assistant Only | [`prepare_request`](../ai/execution/request.py#L196) retrieves user memory only with `AssistantContext`; [`assembly`](../ai/tooling/assembly.py#L53) builds memory handlers only for that context. Production `JobRunner` supplies neither. Imported through shared AI and tools facades; detach those registrations/imports before module removal. `tests/assistant/test_memory.py` verifies isolation and retrieval; `tests/ai/test_ai_core.py` verifies safe retrieval failure. |
| `assistant/memory/{models,store}.py` as complete modules | Shared | JobStore inherits MemoryStore; its summary records/CRUD are still read by [personal prompt context](../ai/execution/request.py#L139), while [SessionStore.compact](../sessions/store.py#L31) updates the summary table atomically with its cursor. Personal memory CRUD is separate behavior; `clear_user_memories` also deletes summaries at [store.py:172](../assistant/memory/store.py#L172). Whole-module or whole-table removal is unsafe while legacy sessions remain. |
| `assistant/tasks/{models,store,tools}.py` | Personal Assistant Only | CLI task/reminder commands and personal delivery use these APIs; AI/tool facades import schemas and reminder-completion checks. `TaskStore` remains a `JobStore` base class. No Worker/Scheduler business call to personal task/reminder methods was found. `tests/assistant/test_assistant.py` and `tests/interfaces/test_cli.py` cover them. Unlink composition and imports before removing Python modules; keep legacy tables/migrations. |
| Retired briefing behavior | Personal Assistant Only | No active briefing module or worker exists. [`migrations.py:201`](../workflows/storage/migrations.py#L201) explicitly retains historical briefing tables for compatibility; `test_version_seven_database_retains_legacy_briefing_schema` in `tests/automation/test_hardening.py` enforces that. Those migration statements are not removable dead code. |
| `agent/runtime.py`, `types.py`, `state.py`, `events.py`, `limits.py` | Core Automation | Provider-neutral bounded execution, tool schema validation before permissions, strict authorization, safe events and terminal results. Shared executor calls this runtime for jobs as well as chat. No concrete CLI/assistant/database import in `runtime.py`. `tests/agent/test_runtime.py`, `test_model_adapter.py` and `tests/automation/test_scheduled_automation_execution.py` provide boundary evidence. |
| `agent/subagents.py` | Optional | [`ModelToolLoop`](../ai/execution/loop.py#L276) installs `SubAgentManager` only when `agent.delegate` is selected. It intersects parent tools/permissions/Skills and budgets; tests in `tests/agent/test_subagents.py` cover denial/cancellation. Its unconditional import is still a shared executor dependency. Do not confuse it with durable child jobs below. |
| `application/automation.py`: JobService, ScheduleService, AutomationService, ApprovalService | Core Automation | No assistant import or personal-store business call. Services use JobStore workflow methods, checkpoints, definitions and optional worker signals. Runtime API [mounts these services](../interfaces/api/server.py#L347) without a worker; independent [run_worker](../application/worker.py#L14) supplies execution. ScheduleService takes RuntimeSettings timezone/workspace defaults. |
| `application/worker.py`, `application/runtime_configuration.py`, `interfaces/api/runtime.py` | Core Automation | Independent worker lifecycle, runtime-only configuration and durable service-backed HTTP routes now have production callers. Tests: `tests/automation/test_worker_runtime.py`, `tests/automation/test_runtime_configuration.py`, `tests/interfaces/test_runtime_api.py`. Preserve these when detaching assistant adapters. |
| `application/configuration.py` | Shared | Profile/voice parsing and settings editor coexist with `read_configuration`, which [load_runtime_settings](../application/runtime_configuration.py#L135) uses to read runtime fields only. Keep configuration-file validation, atomic writes and legacy timezone fallback; voice/profile behavior can be separated without removing the common reader. Tests: `tests/application/test_configuration.py`, `test_runtime_configuration.py`. |
| `application/settings.py` | Shared | Validated environment readers supply provider and job limits; imported by `ai/config.py`, `workflows/runtime/context.py`, and runner. Not merely dashboard settings. |
| `application/modes.py` | Shared | Both jobs and interactive AI call `get_mode_policy('agent')`; default policy text and personal-tool sets are mixed with runtime filtering. Preserve the single-mode contract and safe filtering while later removing assistant wording/sets. `CLARIFICATIONS_ENABLED=False` parks clarification pending a working persistent interface path. |
| `application/language.py` | Shared | Job `prepare_request` chooses reply language; prompting and final response correction also call it. It is not only the chat language UI. Keep its current dependency until runtime output behavior is explicitly separated. |
| `application/skill_proposals.py` | Optional | CLI `/skill-proposal` calls this service, backed by `SkillProposalStore`; Worker/Scheduler do not invoke detection or publication. Tests: `tests/automation/test_skill_proposal_foundation.py`, `test_skill_proposal_approval.py`, `test_repeated_workflow_detection.py`. |

### Interfaces

| Component | Classification | Dependency evidence and decision |
| --- | --- | --- |
| `main.py` entry dispatch | Shared | CLI, settings, API and voice coexist with [standalone worker dispatch](../main.py#L38). Imports selected interfaces lazily. Preserve worker/configuration dispatch while retiring personal surfaces only when separately authorized. `tests/interfaces/test_main.py` covers it. |
| `interfaces/cli/backend.py` | Shared | [run_session](../interfaces/cli/backend.py#L99) still creates identity/session and starts job notifications/personal reminders. It no longer starts or stops AutomationWorker; [finally](../interfaces/cli/backend.py#L379) only cancels CLI background tasks. [Worker independence regression](../tests/automation/test_worker_runtime.py#L118) covers continued execution after CLI exit and database cancellation. Preserve command dispatch and inbox presentation; chat/session setup remains a conditional removal candidate. |
| `interfaces/cli/commands.py` registry/context | Shared | Public job, automation, schedule and approval handlers coexist with personal commands and chat Skill UX. Job handlers depend on services and runtime timezone; the registry is the only current public workflow command route. Remove handlers individually, not the registry. Tests: `test_cli_jobs.py`, `test_cli_schedules.py`, `test_cli_skill_proposals.py` under `tests/interfaces/`. |
| CLI `_task`, `_reminder`, `_clear`, `_reset`; `reminder_input.py` | Personal Assistant Only | Explicit handlers at [`commands.py:594`](../interfaces/cli/commands.py#L594) onwards call personal persistence; none executes jobs. Candidates after retaining the command dispatch and runtime configuration needed by workflow handlers. `_reset` calls a database-wide conversation reset; deleting its surface must not delete stored data during refactor. |
| `interfaces/cli/app.py`, `output.py`, `progress.py` | Shared | CLI input and output also display command/job/notification/approval results. [`capabilities/developer/cli.py`](../capabilities/developer/cli.py#L15) imports output/progress. Preserve an operational CLI and usable approval/delivery path before changing presentation. |
| `interfaces/cli/history.py`, `language.py`; chat fallback in backend | Personal Assistant Only | Prompt display/history and interactive language continuity feed the chat request path. Backend calls sessions/AI on non-command messages at line 199 onwards. `app.py` still imports `HistoryWindow`; replace that caller before removing history. Language implementation in `application/` remains Shared. |
| `interfaces/cli/skill_catalog.py`; `/skills`, `/skill activate/deactivate`, `/<skill> <message>` | Personal Assistant Only | CLI discovery/session selection passes registry Skills into chat. Backend binds `SkillSelection` to its conversation; [`handle_command`](../interfaces/cli/commands.py#L823) resolves one-request skill invocations. This is distinct from pinned SQLite automation Skills. Preserve loader/registry and automation Skill execution. |
| `interfaces/cli/operations.py`: `notify_cli` and inbox summaries | Core Automation | Actual delivery path for durable job result/approval/retry notifications. Backend starts it optionally. Covered by live notification tests; notification acknowledgements must continue to reflect confirmed delivery. |
| `interfaces/cli/operations.py`: personal reminder helpers/loop | Personal Assistant Only | `notify_personal_reminders` and `print_due_reminders` at [`operations.py:132`](../interfaces/cli/operations.py#L132) use `TaskStore.claim_due_reminders`, independent of Scheduler/jobs/inbox. Remove only this loop and its personal helpers/callers. |
| `interfaces/cli/operations.py`: `handle_operations` | Unknown / Needs Review | Implements health/backup/limits/logs/knowledge/subtasks/notifications, but no call from production `run_session` or public command registry was found. `test_operator_cli_validation_and_preview` in `tests/automation/test_hardening.py` calls it directly. Current `/notifications` is a helper implementation, not a registered public command. Keep runtime operations APIs; decide the intended public route before removing or exposing the wrapper. |
| `interfaces/api/server.py` | Shared | One local guarded app mounts both durable [Runtime API services](../interfaces/api/server.py#L347) and legacy session `/runs`. [create_app](../interfaces/api/server.py#L157) still recovers agent runs, resolves identity and constructs SessionService before serving either family. `/jobs` is sessionless but API startup is still coupled to assistant identity imports/storage. Preserve local guard, `/runs` ownership/cancellation/SSE and durable routes until explicitly separated. `tests/interfaces/test_api.py` and `test_runtime_api.py` cover both. |
| `interfaces/voice/*` | Personal Assistant Only | `main.py voice` → factory/controller → capture/STT → shared executor → sanitized TTS/playback. No Worker/Scheduler caller and no automation worker started. [`test_runtime_imports_no_voice_types`](../tests/interfaces/test_voice.py#L344) guards runtime separation. All voice-local modules are candidates with dispatch/config/docs cleanup; shared sessions, broker, providers and persistence are not voice-specific. |
| `interfaces/web/history.py`; dashboard history UI in `static/*` | Personal Assistant Only | Read-only local CLI conversation viewer. `tests/interfaces/test_web.py` verifies history isolation, pagination and no database creation for an absent database. Candidate after removing corresponding routes/UI. |
| `interfaces/web/{server,settings}.py`; settings UI in `static/*` | Shared | History and settings share server/assets. Runtime configuration now has an independent [loader](../application/runtime_configuration.py#L135) and file/environment settings; the editor shares validated configuration/atomic writes. Retain runtime settings and host/origin/session protections while retiring history UI. Tests: `tests/interfaces/test_web.py`, `tests/application/test_runtime_configuration.py`. |

### Workflows, Skills, tools, permissions, and sandbox

| Component | Classification | Dependency evidence and decision |
| --- | --- | --- |
| `workflows/models.py`, `errors.py` | Core Automation | Job/attempt/result/approval/schedule/automation/Skill contracts and structured safe error codes used throughout services, storage, runner and CLI. Optional proposal contracts share `models.py`; no whole-module removal. |
| `workflows/runtime/{worker,scheduler,runner,context,options,checkpoints}.py` | Core Automation | Lock/claim/recovery, timezone schedules, budgets, tool permissions, exact approvals, workspace identity/hashes and result transitions. See end-to-end map. Checkpoint validation is shared with JobService resume and must precede resumed side effects. |
| `workflows/storage/store.py` | Shared | Core workflow persistence facade also inherits all four assistant stores, RunStore, OperationsStore, SubtaskStore, KnowledgeStore and SkillProposalStore. Deleting any imported base class breaks jobs at import time even when its methods are not called. Preserve transaction/connection safety and workflow APIs. |
| `workflows/storage/{migrations,locking}.py` | Core Automation | Schema version 23, pre-upgrade online backup, version transactions, newer/partial-schema refusal and migration/worker advisory locks. Historical assistant tables remain compatibility obligations. `tests/automation/test_hardening.py` and migration/recovery tests validate these paths. |
| `workflows/storage/{operations,redaction}.py` | Shared | Runtime settings, quotas, diagnostics/metrics, logs, backup, retention and inbox are core; cleanup also redacts optional rejected proposals. Redaction is reused by assistant, API, voice and tool audit. Keep unread-notification and resumable-job safeguards. |
| `workflows/storage/runs.py` | Shared | `agent_runs` ownership/recovery and session Skill persistence serve CLI/API/voice; `JobStore` inherits the mixin. Job attempts use separate job/attempt state, while job runtime trace helpers live in `store.py`. `tests/agent/test_run_persistence.py` covers process ownership, restart and session reservations. |
| `workflows/storage/knowledge.py` | Core Automation | Opt-in workspace index/search through `tools.advanced`, with workspace scoping, symlink rejection, redaction, quotas, citations and staleness checks. It is not personal memory. `tests/automation/test_advanced.py` verifies index integrity and privacy. |
| `workflows/storage/subtasks.py`; subtask portion of `tools/advanced.py` | Optional | Durable child jobs require `options.subtasks`; the Worker nonetheless always calls `reconcile_children`, and Runner always calls `reserved_budget` and `wait_for_children`. Parent cancellation, shared quotas and restart state depend on these methods even with the feature unexposed. Preserve handling of existing child jobs before isolating it. See `tests/automation/test_advanced.py`. |
| `workflows/storage/skill_proposals.py` | Optional | Detection/publication is explicit and not executed by worker ticks. However JobStore inherits it and `OperationsStore.cleanup` always queries proposal tables. Publication atomically creates a versioned Skill with provenance checks. Optional exposure does not imply safe module/table deletion. |
| `workflows/library/{definitions,bundles}.py` | Core Automation | Definition/parameter/prompt/Skill/workspace validation plus validated export/import; called by services and storage. Keep version pinning and secret rejection. Tests: `tests/automation/test_reusable_automations.py`, `test_public_automation.py`. |
| `skills/{types,loader}.py`; `SkillRegistry` and `builtin_registry` in `registry.py` | Shared | AI preparation always resolves a registry; active Skill allowlists intersect tools/MCP and context builder renders instructions. Builtins are reusable instruction documents, not executable handlers. `tests/agent/test_skills.py` proves they cannot grant permissions or bypass registration. Keep the framework. |
| `SkillSelection` in `skills/registry.py` | Shared | Session activation is chat UX, but voice uses this class and API uses the same `session_skills` persistence through RunStore. It shares a module with core registry discovery. Remove session selection only after all adapter consumers are retired or separated. |
| SQLite automation Skills (`workflows.models.Skill/SkillVersion`, store APIs) | Core Automation | `automation_version_skills` pins version IDs; Runner retrieves and passes their instructions into ContextManager as `skill_instructions`, without CLI selection. Tests: `tests/automation/test_reusable_automations.py`, `test_scheduled_automation_execution.py`. Never delete these when retiring chat Skills. |
| `tools/{base,types,registry,executor}.py` | Core Automation | Registered handlers, schemas, normalized results and grant-required execution underpin AgentRuntime. Tests in `tests/agent/test_runtime.py` cover invalid arguments before permission checks, strict grants and failure results. |
| `tools/__init__.py` | Shared | Global registration/schema/guidance facade eagerly imports personal task/memory, workspace and native domain tools. `get_tools` builds request subsets; importing only `tools.registry` still executes package initialization. Remove personal entries and imports coherently, not the facade. |
| `tools/advanced.py`: retrieval portion | Core Automation | Job option `retrieval` selects `index_project/search_project`, and handlers require `ExecutionContext` authorization before KnowledgeStore access. Subtask functions in the same module are Optional. |
| `tools/delegation.py` | Optional | Schema/name for `agent.delegate`; tools facade registers it and the loop installs its request-scoped handler. Keep imported schema until optional exposure is separated. |
| `tools/clarification.py` | Shared | Parsed clarification contract/hooks remain in the shared loop, although current mode parks exposure. Missing-input clarification is an explicit safeguard, not removable general chat behavior. Tests: `tests/ai/test_ai_core.py` for parked clarification; durable planning tool tests cover a separate retained capability. |
| `tools/datetime_tool.py`, `tools/search/*` | Shared | Native datetime/web/search/research handlers are registered and available to interactive adapters. Job `_allowed_tools` currently returns only authorized job-scoped names; ordinary Runner does not select these native assistant names. Search/fetch is also reused by `tools/web/operations.py`; keep URL/DNS/redirect/size/timeout controls. Tests: `tests/tools/test_datetime_tool.py`, `test_search.py`, `test_search_pipeline.py`, `test_web.py`. Lack of default job exposure is not grounds to remove tool capabilities required by the contract. |
| `tools/file_reader/*` | Shared | Attachment tools, document cache/index/retrieval/extraction/tokenization/summarization are eagerly imported through tools and assembled by AI. CLI/API do not currently supply job attachments, but executor supports them; these APIs have direct tests in `tests/tools/test_file_reader.py`. Keep until attachment support is explicitly decided; do not conflate document retrieval with personal memory. |
| `tools/{filesystem,git,python,terminal,web}/*` domain facades | Unknown / Needs Review | `tools.ALL_TOOLS/ALL_TOOL_SCHEMAS` exports namespaced and alias tools; `tests/tools/test_domain_registry.py` calls their dispatch directly. Neither the current interactive allowlist nor Runner's job allowlist selects those facade names. Generic filesystem/shell/code implementations do not use the job's exact-action approval/containment/sandbox chain. Their external use/required future surface is not established by repository call sites. Preserve existing disabled exposure; neither delete nor enable them in this audit. |
| `permissions/{models,policy,engine,approvals}.py` | Core Automation | Fail-closed policy and exact approval values used by runtime and `ExecutionContext`. `PermissionPolicy` defaults to deny; executor explicitly permits only the selected registry. `tests/agent/test_permissions.py` and `tests/automation/test_approvals.py` verify boundaries. |
| `permissions/broker.py` | Shared | Live run-scoped approval handshake used by AgentRuntime/PermissionEngine and CLI/voice; pending entries expire/cancel and are memory-only. API supplies no interactive submission path and fails closed. It is distinct from durable job approval storage and cannot be deleted as voice-only UI. `tests/agent/test_interactive_approvals.py` verifies it. |
| `sandbox/{runtime,__init__}.py` | Core Automation | `SandboxPolicy` selects process/Bubblewrap command isolation after job authorization. Bubblewrap capability failure has no process fallback. Namespace/mount/resource controls and process backend behavior are tested by `tests/tools/test_sandbox.py`. |
| `capabilities/developer/workspace/*`, `command.py`, `sandbox.py`, `planning.py` | Core Automation | Job-specific read/search/list, approved atomic patches, contained verification commands, process-tree cleanup and durable plan steps. Despite the package's “parked” wording, these are called by shared tool assembly for real jobs. Tests: `tests/tools/test_workspace_tools.py`, `test_command_tools.py`, `test_planning_tools.py`. |
| `capabilities/developer/cli.py` | Shared | Public backend imports its compatibility parsing/presentation exports; tests call the parked parsers and job presentation helpers directly. Some operations are parked, but module-wide deletion would break CLI imports; determine unused symbols individually later. |

### Supporting packages that cannot be omitted

| Component | Classification | Evidence |
| --- | --- | --- |
| `ai/executor.py`, `execution/*`, `tooling/*`, `client.py`, `config.py`, `models.py`, `prompting.py`, `progress.py`, `response.py`, `tool_runtime.py` | Shared | Stable job/interactive executor path owns provider sessions, MCP cleanup, request policy, safe errors, budgets and observability. Personal branches/imports in request/assembly/loop must be removed individually. Final language enforcement also runs for jobs. `ai/models.py` imports the optional interactive `Plan` contract. |
| `ai/providers/*`, `llm/*` | Core Automation | Ollama/OpenAI-compatible protocol adapters, normalized Model request/result and router used by shared loop. `ai/providers/model.py` also supplies a compatibility adapter; tests in `tests/agent/test_model_adapter.py` exercise that interface. Keep provider behavior and configuration. |
| `context/{builder,budget}.py`, context facade | Shared | ContextManager builds all job prompts, including pinned automation Skill instructions; generic context budgets/retrieval records are not solely personal memory. [`builder.skills`](../context/builder.py#L85) keeps guidance subordinate to permissions. |
| `context/compactor.py`, `sessions/{service,store}.py` | Shared | Current API/CLI/voice use persisted conversations, bounded history and summaries. Worker omits session context, but these modules are still imported by the executor hook path and API. They are candidates only after those adapter paths are separated. |
| `retrieval/memory.py` | Personal Assistant Only | MemoryRetriever wraps per-user PersistentMemory and is called only with personal context in preparation. Its imports propagate through the retrieval package facade and shared executor. Remove that adapter/import, not generic retrieval result/error contracts. |
| `retrieval/base.py`, retrieval facade | Shared | `ContextManager` imports RetrievalResult and executor catches RetrievalError. The facade also eagerly imports MemoryRetriever. Keep base contracts while detaching personal retrieval. |
| `mcp_integration/*` | Core Automation | Required integration infrastructure per spec: trusted config, filtered startup/discovery, schema adapter, ToolRegistry/permissions and cleanup. Default production Jobs currently exclude all MCP names before startup; successful executor tests are contextless. See the MCP closure below. Preserve `mcp` and deny-by-default boundaries; stdio servers are trusted host processes outside the Job command sandbox. |
| `planning/*` | Optional | Interactive tool-free Planner/Replanner runs only without ExecutionContext at [`loop.py:227`](../ai/execution/loop.py#L227); invalid plans fall back. Imports/result contracts remain shared. This is distinct from required durable job plan tools and checkpoints. `tests/agent/test_planning.py` covers it. |

## Follow-up closure: assistant call sites and removal boundaries

The distinction is business use versus import/composition. None of the four
assistant mixins is called for personal business behavior by JobService,
AutomationService, ScheduleService, durable ApprovalService or the headless
Worker. All four remain mandatory imports/base classes of JobStore today.
Removing their files without detaching that composition would break even
sessionless submission. Known Shared dependencies are therefore closed as
retention decisions; they do not become safe deletions merely because the
worker can execute without personal rows.

| Question closed | Exact retained flow and verification | Classification / candidate decision |
| --- | --- | --- |
| Does a real Job need an assistant identity or conversation? | [run_worker](../application/worker.py#L14) → JobStore/JobRunner → [AgentRequest](../workflows/runtime/runner.py#L275), with no AssistantContext/history/session ID. [Headless test](../tests/automation/test_worker_runtime.py#L79) makes identity/conversation/agent-run creation raise, then [asserts committed results, notifications and zero personal rows](../tests/automation/test_worker_runtime.py#L93). [Configuration test](../tests/automation/test_runtime_configuration.py#L44) repeats session rejection across CLI/API submission and headless execution. | Job lifecycle is Core Automation; assistant mixins remain Shared composition only on this path. No whole-JobStore or assistant-directory removal. |
| Why retain identity/conversation APIs after adding `/jobs`? | [API startup](../interfaces/api/server.py#L157) still calls identity resolution/profile application and builds SessionService. Legacy `/runs` [resumes/checks ownership](../interfaces/api/server.py#L181), persists agent runs/session Skills, appends history and supplies AssistantContext; [find_run](../interfaces/api/server.py#L280) enforces user/interface ownership for result/cancel/SSE. [API isolation test](../tests/interfaces/test_api.py#L242) rejects foreign sessions; [run persistence tests](../tests/agent/test_run_persistence.py#L24) cover restart, reservations and orphan recovery. | IdentityStore/ConversationStore/models are Shared until the legacy adapters and startup are separated. Personal history/clear/reset behavior is a conditional candidate, preserving ownership and compatibility data. |
| Is profile timezone still a schedule dependency? | [CLI schedule](../interfaces/cli/commands.py#L425) reads `service.runtime.timezone`; [ScheduleService](../application/automation.py#L143) uses RuntimeSettings. [Runtime loader](../application/runtime_configuration.py#L135) accepts legacy file `profile.timezone` as fallback without a user DB lookup. [Cross-entry test](../tests/automation/test_runtime_configuration.py#L128) deliberately gives CLI user a conflicting timezone and verifies runtime defaults. | Runtime configuration is Core Automation; persisted user timezone is only legacy adapter behavior here. Preserve legacy config fallback, explicit schedule timezone and pinned snapshots. Personal profile/preferences cannot justify deleting the common parser. |
| Why retain MemoryStore summary methods? | [personal prompt adapter](../ai/execution/request.py#L139) reads `get_session_summary`; [SessionStore.compact](../sessions/store.py#L31) updates summary/cursor in one transaction. [Compaction/restart test](../tests/assistant/test_sessions.py#L19) preserves raw history; [cross-user test](../tests/assistant/test_sessions.py#L73) rejects append/compaction with unchanged state. [Summary CRUD test](../tests/assistant/test_memory.py#L31) verifies exported mixin methods. | Complete models/store modules remain Shared. `MemoryItem` and long-term personal CRUD are Personal Assistant Only candidates after separating imports and mixed `clear_user_memories` semantics; summary/session behavior is still retained for legacy API/CLI/voice. |
| Can personal prompt memory and task/reminder behavior be detached? | [prepare_request](../ai/execution/request.py#L196) only retrieves personal memory with AssistantContext; [assembly](../ai/tooling/assembly.py#L53) similarly builds personal handlers. [Final reminder check](../ai/execution/loop.py#L149) is context-gated. Their imports are unconditional. CLI [personal reminder loop](../interfaces/cli/backend.py#L155) is separate from job `notify_cli`. Personal CRUD/isolation tests live in `tests/assistant/test_memory.py` and `test_assistant.py`; job tests use workspace tools instead. | Personal Assistant Only behavior remains a safe *conditional* removal candidate: detach these branches/imports/tool schemas and TaskStore composition together. Preserve workspace knowledge, schedules, job inbox, exact approvals and legacy schema. Do not delete a mixed facade first. |
| Does job observability require session persistence? | [hooks.on_event](../ai/execution/loop.py#L31) selects ExecutionContext.plan_store when AssistantContext is absent; [add_run_event](../workflows/storage/store.py#L844) persists Job IDs with nullable session IDs. [run_events schema](../workflows/storage/migrations.py#L275) has both job and conversation FKs; [agent_runs/session_skills](../workflows/storage/migrations.py#L293) are separate session tables. | Job event path is Core Automation; common trace table is Shared. Never remove `run_events` with chat traces. RunStore's session methods remain Shared by composition and legacy adapters, not a substitute for Job attempts. |

## Follow-up closure: production Job MCP and delegation

Production reachability is now established: default queued and scheduled Jobs
cannot select an MCP tool. The unresolved question is the required authorization
contract, not whether contextless executor tests prove Job support.

| Boundary | Source and test evidence | Decision |
| --- | --- | --- |
| Public submission / stored options | [CLI `_run`](../interfaces/cli/commands.py#L234) and [REST validator](../interfaces/api/runtime.py#L88) → [JobService.submit](../application/automation.py#L51) accept workspace/write/command fields, with no arbitrary tool allowlist. REST rejects extra keys, including the [tested `allowed_tools` payload](../tests/interfaces/test_runtime_api.py#L132). [validate_options](../workflows/runtime/options.py#L6) accepts retrieval/subtasks/sandbox/budget options only; automation definitions use validated options too. | Submission/validation is Core Automation. No supported public Job MCP authorization surface found. Keep Needs Review for the intended contract. |
| Scheduler / Worker / Runner | [Worker owner](../application/worker.py#L18) constructs ordinary JobRunner; [scheduler](../workflows/runtime/scheduler.py#L255) materializes Jobs through storage. [Runner allowlist](../workflows/runtime/runner.py#L180) contains workspace/plan/command/retrieval/subtask names, then [passes its context](../workflows/runtime/runner.py#L287) to the executor. Pinned SQLite Skill text is passed as `skill_instructions`; it cannot expand permissions. | This excludes MCP for both queued and scheduled Jobs, even with host MCP configuration. Do not broaden allowlists or claim successful Job MCP execution in this audit. |
| Pre-start exclusion and retained execution substrate | [eligible_mcp_config](../ai/execution/request.py#L36) intersects configured tool names with ExecutionContext and active Skills before [startup](../ai/executor.py#L126). [Manager](../mcp_integration/manager.py#L32) handles discovery/failure cleanup; [request preparation](../ai/execution/request.py#L175) and [loop registry/permissions](../ai/execution/loop.py#L223) apply tool/Skill/permission checks. [SDK call](../mcp_integration/client.py#L66), [adapter](../mcp_integration/adapter.py#L76) and [executor cleanup](../ai/executor.py#L254) complete the path when a permitted tool reaches it. | MCP package/configuration/SDK/permissions/cleanup are Core Automation infrastructure required by spec. Shared executor remains Shared. A trusted stdio server runs on the host, outside the Job command sandbox. |
| Missing or invalid MCP configuration | [load_mcp_config](../mcp_integration/config.py#L98) returns an empty config when the path setting is absent/empty; it does not search the working directory. An explicit missing/unsafe/malformed file fails validation. [Executor](../ai/executor.py#L114) loads config **before** Job filtering and returns `failed` with `invalid MCP configuration` on ValueError. A selected server with no allowlist is not started; connection/discovery failures close its client and expose none of its tools, while other valid servers can remain available. | Core Automation trust boundary. Source order means invalid explicit host config also fails requests for Jobs that would exclude MCP; exclusion does not bypass config validation. Config tests and the contextless invalid-config executor test prove the constituent behavior, but there is no dedicated queued-Job invalid-config test. |
| Tool permission versus approval/containment/sandbox | [ModelToolLoop](../ai/execution/loop.py#L263) constructs an explicit `allow` policy for selected native/MCP handlers. [AgentRuntime](../agent/runtime.py#L219) resolves and validates arguments, checks PermissionEngine and the request hook, then obtains a [ToolExecutor grant](../tools/executor.py#L59). However [MCPTool.execute](../mcp_integration/adapter.py#L76) directly calls `client.call_tool`; it receives no ExecutionContext, calls no `require_approval`/`safe_path`/`sandbox_command`, and [stdio startup](../mcp_integration/client.py#L30) does not use Sandbox. | Tool-level schema/permission enforcement is Core Automation. It does **not** establish durable exact-action approval or Job workspace/namespace confinement for MCP actions. A caller-injected context naming an MCP tool is an internal selection path, not proof of a public Job authorization contract. Default Job exclusion prevents reaching that path today. |
| What existing MCP tests prove | [Job allowlist test](../tests/mcp/test_integration.py#L288) constructs contexts directly and checks filtering/preparation. [Successful executor test](../tests/mcp/test_integration.py#L372) supplies no ExecutionContext. [Runtime denial/schema test](../tests/mcp/test_integration.py#L249) uses a direct registry. [Cancellation test](../tests/mcp/test_integration.py#L434) verifies cleanup with fake clients; local stdio tests exercise the SDK separately. [Runtime API denial test](../tests/interfaces/test_runtime_api.py#L312) verifies default Jobs cannot call MCP. | These prove retained adapter behavior and default denial, not successful submission → claim → MCP side effect. Missing evidence: that positive production path under an approved authorization design. |
| Bounded delegation versus durable child Jobs | [request selection](../ai/execution/request.py#L86) and [conditional handler](../ai/execution/loop.py#L276) require `agent.delegate` selection; Runner does not select it. [Subagent tests](../tests/agent/test_subagents.py#L279) directly inject authorization or execute without Job context. [Durable subtasks](../workflows/runtime/runner.py#L188) are independently opt-in; Worker reconciliation and Runner child budgets remain unconditional. | Bounded delegation is Optional, not a required MCP integration. Its classification is closed; no default Job delegation success is claimed. Preserve existing durable-child recovery/budget compatibility. |

Chat MCP consumers can be candidates with chat retirement, but retaining the
MCP integration is mandatory. Neither `mcp_integration/*` nor the `mcp` package
is a safe removal candidate. Personal memory/task handlers have no business
role in MCP startup/discovery/call/cleanup; their eager import coupling must
still be detached before removal. No live private MCP configuration or user
extensions were inspected.

**Still Needs Review — Job MCP authorization contract.** Retain the Core
Automation integration, but do not interpret host `allow_tools` or the internal
context-construction test as a durable authorization grant. Missing evidence is
an approved Job/Automation contract identifying who may select MCP tools, how
state-changing arguments map to durable approvals, and what workspace/sandbox
guarantees apply to trusted host servers. No production submission → claim →
allowed MCP call test, or corresponding denied/expired/changed-action MCP
approval and sandbox test, establishes those guarantees. This is a current
capability/contract gap relative to `spec.md`, not evidence of a reachable
default-Job permission bypass. No permission was broadened to close it.

2026-10-08 implementation gate review: [Job MCP contract](job-mcp-contract.md)
traces the current enforcement points and specifies conditional authorization,
approval and confinement requirements. It stops before implementation pending
server eligibility, startup confinement and mutation/recovery decisions. The
default Job denial and the missing positive production evidence remain unchanged.

## Follow-up closure: native tools and operational helpers

Jobs use the context-bound `capabilities/developer` tools. The generic domain
facades and operator command wrapper are separate contracts, even though they
are exported by modules that also contain required runtime code.

| Item | Classification | Actual callers, enforcement and removal consequence |
| --- | --- | --- |
| Job workspace reads/writes, plan and verification-command tools | Core Automation | JobService validates the workspace; [Runner checkpoint validation](../workflows/runtime/checkpoints.py#L11) precedes execution/resume. Runner constructs ExecutionContext with read/plan tools, adds writes/commands only from stored flags, and adds retrieval/subtasks only from validated options. [Request filtering](../ai/execution/request.py#L86) intersects job-scoped names; [assembly](../ai/tooling/assembly.py#L66) installs context-bound handlers. These cannot be removed with personal tool registrations. |
| Workspace and exact-action enforcement | Core Automation | [ExecutionContext](../workflows/runtime/context.py#L70) resolves/pins the workspace and checks tool permission plus workspace identity. [safe_path](../capabilities/developer/workspace/paths.py#L25) resolves paths and rejects escapes, including external symlinks. [Patch handler](../capabilities/developer/workspace/writing.py#L22) requires write permission, size/hash/change-budget checks and approval for path/before/after hashes before atomic replacement. [Command handler](../capabilities/developer/command.py#L269) requires command permission, validates executable/arguments/contained paths, and requires approval for command/timeout/sandbox before process creation. ApprovalService/store and these guards have no personal task/reminder business dependency. |
| Durable approvals and selected command sandbox | Core Automation | Runner's approval callback → [request_or_consume_approval](../workflows/storage/store.py#L1304) uses `BEGIN IMMEDIATE`, a running-job check, digest matching, expiry/invalidation and single consumption. Pending approval returns `waiting_approval` before the side effect; denial/cancellation blocks it. [sandbox adapter](../capabilities/developer/sandbox.py#L7) chooses write/read-only mounts from Job permissions; [Sandbox.command](../sandbox/runtime.py#L54) either keeps `process` behavior or requires available Bubblewrap with no fallback. Command environment, bounded output, timeout/cancellation and process-group cleanup live in `command.py`, not the generic terminal/Python runners. |
| Missing policy/context dependencies | Core Automation | PermissionPolicy defaults to deny; [PermissionEngine](../permissions/engine.py#L18) denies missing/throwing/malformed policies, and Runtime/ToolExecutor require strict grants. No ExecutionContext removes Job workspace/plan/command/advanced exposure. With a context but no plan store, preparation removes plan/advanced tools; without a command-event callback it removes commands. A missing approval callback rejects state changes. The store/callback exposure rules are source-backed; existing tests do not separately exercise every missing-store/callback combination. |
| `tools/{filesystem,terminal,python,git}/*`, dotted names and underscore aliases | Needs Review | [get_tools](../tools/__init__.py#L192) exports these for an explicit caller, but neither mode selection nor Job filtering selects them, even if a context names a generic tool. No production caller of their concrete operations was found beyond facade registration. [Filesystem default](../tools/filesystem/__init__.py#L7) has no root; optional `FilesystemOperations(root_dir=...)` confinement is not a Job approval path. [TerminalRunner](../tools/terminal/runner.py#L42) uses `shell=True`; [PythonRunner](../tools/python/runner.py#L33) uses host `python -c`; [GitOperations](../tools/git/operations.py#L20) accepts a repository path and can commit. None supplies the context/approval/sandbox chain above. Do not enable these as replacements for Job tools or declare them Personal Assistant Only. Required external Python/extension compatibility and the intended public surface remain unestablished. |
| Generic `web.search/web.fetch` facade and shared search/fetch implementation | Facade: Needs Review; internals: Shared | Facade names have the same production exclusion as other generic domains. [WebOperations](../tools/web/operations.py#L7) delegates to shared `tools.search` implementations with URL/DNS/redirect/timeout/size safeguards; their native names remain used by interactive selection. They do not gain Job exposure from being registered. Preserve the shared fetch/search guards when reviewing facade retirement; no live external extension inventory was inspected. |
| `tools/__init__.py`, shared executor/import contracts | Shared | AgentRuntime imports ToolRegistry/ToolExecutor through a package whose initialization eagerly imports domain, personal-task and personal-memory registrations. Removing personal modules first breaks Job startup even when those handlers are unselected. Detach personal imports/entries while retaining `get_tools`, schemas and context-bound assembly in any later authorized removal. |
| Operations settings/quotas, heartbeat, diagnostics/metrics and logs | Core Automation behavior; OperationsStore remains Shared | [Worker](../workflows/runtime/worker.py#L120) calls heartbeat/settings; [Runner](../workflows/runtime/runner.py#L110) reads daily-token settings; [claim](../workflows/storage/store.py#L772) uses persisted concurrency/quota state; [JobService.worker_ready](../application/automation.py#L26) uses diagnostics. [OperationsStore](../workflows/storage/operations.py#L37) validates settings and logs mutations. These are trusted lifecycle/service calls, not model tools that need enabling through `handle_operations`. Removing them as personal helpers breaks job admission/execution/readiness/observability. |
| Retention and backup helpers | Core Automation behavior; OperationsStore remains Shared | Worker calls `cleanup(retention, dry_run=False)` independently of CLI. Cleanup preserves resumable jobs, checkpoints/approval history and unread notifications, and serializes deletion plus optional rejected-proposal redaction atomically. Backup uses [backup_database](../workflows/storage/migrations.py) and is also used by migration safeguards; no Job model tool exposes arbitrary backup/cleanup paths. Keep underlying storage/migration APIs. Retention's unconditional proposal-table query remains a known Optional-feature compatibility dependency, not personal memory. |
| Project index/search helpers | Core Automation | `options.retrieval` → `index_project/search_project` → [context permission check](../tools/advanced.py#L44) → [KnowledgeStore](../workflows/storage/knowledge.py#L51) with `context.workspace`, not a model-supplied root. Index/search use per-workspace state, no-follow file opening, exclusion/redaction/quotas and hash checks that omit stale/newly symlinked results. They mutate derived index state without write approval under the existing retrieval opt-in contract; they do not modify workspace files. Removing personal memory must retain this index and its tables. |
| Durable child helpers / bounded agent delegation | Optional | `create_subtask/subtask_results` are Job opt-ins, distinct from unselected `agent.delegate`. [SubtaskStore](../workflows/storage/subtasks.py#L30) checks the running parent's opt-in, write/command ceiling, budgets and idempotency; children inherit workspace/sandbox and cannot nest. Worker reconciliation and Runner child-budget/results methods remain unconditional compatibility dependencies. No new delegation scope was audited or authorized. |
| `handle_operations` exported operator wrapper | Needs Review | Repository search finds only its definition and direct hardening-test callers. [public command registry](../interfaces/cli/commands.py#L801) and backend never dispatch it; it is not a Job tool. Its shlex/argument validation, cleanup preview and error redaction do not establish Job authorization: it directly invokes trusted store APIs, with no ExecutionContext/approval broker. [operations documentation](operations.md#local-notifications-and-verification) advertises `/notifications` despite that absent route. Missing: intended supported routing/extension compatibility or an explicit retirement decision. Do not expose it or remove its backing APIs based on the direct test. |
| Durable inbox and `notify_cli` | Core Automation | Job transitions atomically create inbox rows. [backend](../interfaces/cli/backend.py#L148) separately wires `notify_cli` when configured; it needs no personal identity/reminder context. Safe status summaries, stale-approval suppression and output-before-ack/retry behavior must survive removal of reminder helpers. CLI delivery can be disabled without losing durable inbox state. Removing the whole `operations.py` would break that import/delivery path. |
| Personal reminder formatting/listing/claim/delivery helpers | Personal Assistant Only | `print_due_reminders`, `print_pending_reminders`, `_reminder_time`, `notify_personal_reminders` use user-scoped TaskStore reminders. Backend/commands call them separately from the Job inbox. They are conditional removal candidates after detaching those callers; they provide no Job permission, approval, scheduling or sandbox enforcement. |

`process` is the persisted default when sandbox configuration is omitted. It
provides command allowlisting/process-group handling, **not** filesystem or
network namespace isolation. `bwrap` is an explicit command isolation backend;
its capability failure prevents execution. Neither backend wraps generic host
handlers or MCP server startup automatically. This distinction is the existing
contract documented in [operations](operations.md#stronger-command-sandbox),
not a new relaxation. The shown legacy `/run --sandbox` example does not itself
establish current public CLI option reachability; the public `_run` path above
accepts workspace/write/command fields. Stored/pinned automation options and
direct JobStore options are separate configuration paths.

### Allow/deny evidence and its limits

The following are existing tests, with no new tests or changed assertions.
Focused verification covers the selected files listed below; Skill, structured
error/workspace-rebinding and other supporting tests also ran in the full suite.

| Boundary | Existing tests and what they establish |
| --- | --- |
| Selected native Job tools can execute | `test_external_job_service_worker_runtime_and_persisted_result` in [runtime API tests](../tests/interfaces/test_runtime_api.py#L59) follows HTTP → service → independent Worker/ordinary JobRunner → AgentRuntime → workspace read and asserts reopened result/attempt/tool-event/inbox state. [Headless worker test](../tests/automation/test_worker_runtime.py#L33) also executes queued and pinned scheduled reads while identity/conversation/session-run creation raises. |
| Default Job denial and durable approval | [API tests](../tests/interfaces/test_runtime_api.py#L307) block ungranted writes, commands and an invented MCP call with no changes, command events or approval rows. The MCP denial test configures no allowed MCP server; it proves unavailable-tool denial, not a discovered MCP tool's approval behavior. The write test covers approve/deny/cancel, reopened approval state, no preapproval file and single consumption; its approved write still blocks final completion without verification. [Approval tests](../tests/automation/test_approvals.py#L44) separately cover exactness, changed actions, expiry, cancellation and single use. |
| Missing/invalid policy and grants | [runtime tests](../tests/agent/test_runtime.py#L49) exercise absent policy, PermissionEngine(None), deny, require-approval without a working approval path, unknown rule, malformed decisions and non-boolean hooks; no handler runs. The executor rejects missing/forged grants. [permission tests](../tests/agent/test_permissions.py#L9) require exact approval types and literal True callbacks, rejecting False/None. |
| No approval path / workspace boundaries | [approval tests](../tests/automation/test_approvals.py#L183) prove a permitted write without a callback creates no file and a pending command starts no process. [workspace tests](../tests/tools/test_workspace_tools.py#L39) reject traversal/external symlinks, unauthorized writes, stale hashes and exhausted change budgets. [workspace-rebinding command test](../tests/automation/test_error_codes.py#L437) replaces the root before/during approval and asserts SANDBOX_VIOLATION and zero process creation. These are specific boundary tests, not proof of every possible concurrent filesystem race. |
| Sandbox/configuration and scheduled permissions | [sandbox tests](../tests/tools/test_sandbox.py#L17) prove invalid backend rejection, explicit/default persistence and no fallback for missing/host-denied Bubblewrap. [scheduled approval test](../tests/automation/test_scheduled_automation_execution.py#L305) uses an injected executor to check preserved process mode, write permission, command denial and pending/consumed approvals; it does not execute real Bubblewrap. Command tests cover allowlisted/path validation, actual process audit, timeout and cancellation. The two real namespace tests remain skipped on this host. |
| MCP allow/deny/config/cleanup | [MCP tests](../tests/mcp/test_integration.py) cover empty allowlist/no startup, no config/no directory auto-discovery, unsafe/missing/invalid config, missing environment references, schema rejection before calls, direct permission denial, Skill/job intersections, server partial failures and cleanup/cancellation. Successful real stdio and contextless executor calls prove transport/adapter behavior only. No queued/scheduled allowed MCP path or MCP durable-approval/sandbox proof was found. |
| Generic native facade contract | [domain tests](../tests/tools/test_domain_registry.py#L14) prove exports, aliases and direct filesystem/terminal/Python dispatch. [Skill tests](../tests/agent/test_skills.py#L123) prove a Skill naming `file_write` cannot add it to selected tools. Generic Job exclusion is established by Runner/request source; no dedicated production Job test tries every generic domain name/alias. Direct facade tests do not prove Job permissions/approvals/sandbox or justify exposure/removal. |
| Operational storage and delivery | [hardening tests](../tests/automation/test_hardening.py#L96) cover consistent/no-overwrite backup, quotas/concurrency, metrics/logs/inbox persistence and direct helper validation/preview. [retention tests](../tests/automation/test_retention_cleanup.py) assert dry-run/expiry boundaries, unread preservation, serialization/restart/idempotency and rollback across notifications/proposal redaction. [advanced tests](../tests/automation/test_advanced.py) cover workspace index isolation/staleness/redaction/quota rollback and child permission/budget inheritance. [live notification tests](../tests/interfaces/test_cli_live_notifications.py) cover stale approvals, output failure and acknowledgement retry/offline restart. Direct helper tests do not prove public command routing. |

Closed retention decisions: Job tool/approval/sandbox enforcement, workspace
retrieval and operational storage/delivery are Core Automation; common facades,
redaction and mixed imports remain Shared; reminder-only helpers are Personal
Assistant Only; child/delegation behavior is Optional with retained lifecycle
dependencies. Generic facade removal and operator-wrapper public contracts
remain Needs Review because source/direct tests cannot establish external
compatibility or the intended supported surface. No full mixed-module deletion
is certified by this scoped audit.

## Direct versus transitive personal-assistant dependencies

| Audited consumer | Direct business use | Transitive coupling that must be preserved or detached |
| --- | --- | --- |
| Worker | No personal task/reminder/chat/memory call. Independent `application.worker.run_worker` owns Scheduler/Runner/locks/recovery. | JobStore assistant mixins and shared AI/tools imports remain. Headless regression rejects identity/conversation/run creation and asserts zero rows in those tables. |
| Scheduler | No personal reminder or memory use. ScheduleService supplies RuntimeSettings timezone or an explicit timezone. | JobStore composition remains; legacy `profile.timezone` is a configuration-file fallback, not a persisted-user lookup. |
| AgentRuntime | No assistant module, personal session store, CLI, voice, concrete provider or SQLite import. | Importing its ToolRegistry/ToolExecutor reaches eager `tools/__init__.py`; executor hooks/preparation import assistant/sessions. Its runtime loop remains provider-independent. |
| JobService | No personal store method calls. | JobStore inheritance and checkpoint helpers; optional Worker reference. |
| AutomationService | No personal store method calls. Uses definition/version/Skill/workspace persistence. | Same JobStore composition and library/storage validation. |
| Durable ApprovalService | No personal task, reminder, conversation or identity requirement in its mutation path. Actor is supplied as CLI text. | JobStore, workflow models, ExecutionContext → PermissionEngine → runner callback. Shared permission package also exports live ApprovalBroker. |
| Job Notifications | Job IDs and committed status transitions; no personal reminder/user identity or delivery-target FK. | Inbox queries belong to JobStore/OperationsStore; current automatic delivery is CLI-owned and shares a module with personal reminders. |

Worker lifecycle is independent of CLI sessions. API startup still creates an
assistant identity for legacy routes, but neither durable API requests nor the
headless worker need conversation execution. The remaining coupling is known
store composition/imports and legacy adapter contracts, not a CLI-owned worker.

## Persistence boundaries

| Data | Classification and preservation requirement |
| --- | --- |
| `jobs`, `job_attempts`, `job_events`, `tool_events`, `change_events`, `command_events`, `job_steps`, `job_stats`, `daily_usage` | Core Automation. Preserve results/errors, quotas, usage, verification/checkpoints and parent/child state. |
| `automations`, `automation_versions`, `automation_run_parameters`, `automation_version_skills`, `skills`, `skill_versions` | Core Automation. Pinned Skill versions and rendered run parameters are unrelated to chat session selection. |
| `schedules`, `schedule_automation_snapshots`, `trigger_history` | Core Automation. Preserve immutable snapshots, occurrence identity, retry history, missed-run handling and explicit upgrades. |
| `approval_requests`, `approval_events`, `notifications` | Core Automation. Job FKs, exact approval digests/consumption/expiry and atomic notification creation remain. Notifications are not `reminder_deliveries`. |
| `runtime_settings`, `worker_state`, `structured_logs`, `schema_migrations` | Core Automation. Preserve ownership/heartbeat, limits, observability, migrations, WAL/foreign-key handling, online backups and recovery locks. |
| `knowledge_files`, `knowledge_chunks` and FTS backing tables | Core Automation workspace retrieval. Do not remove as personal memory. |
| `run_events` | Shared with core job observability. `job_id` references jobs; nullable `session_id` references conversations, both with cascade deletion. Deleting conversations can delete attached session traces; keep the table and job trace path. |
| `agent_runs`, `session_skills`, `users`, `channel_identities`, `conversations`, `messages`, `session_summaries` | Shared current CLI/API/voice identity/session/run contracts. `agent_runs` and `session_skills` depend on conversations; conversations depend on users. A whole chat-table removal is not safe while `/runs` still uses this ownership model. |
| `assistant_memories`, `assistant_memories_fts`, `assistant_tasks`, `reminders`, `reminder_deliveries`, `delivery_targets`, personal `user_preferences` | Personal behavior candidates. Existing SQL/migration relationships still require preservation during this audit. `session_summaries` in MemoryStore is separately Shared. |
| `briefing_deliveries`, `external_briefing_deliveries` | Retired personal behavior; historical compatibility schema is explicitly retained and tested. No active briefing runtime found. |
| `skill_draft_proposals`, `skill_proposal_events` | Optional feature data. Cleanup/publication/provenance/deduplication still depend on these tables. Preserve reviewed/pending records and generated core Skill versions. |

Sources: base schema in [`JobStore._initialize`](../workflows/storage/store.py#L117),
incremental schema in [`migrations.py`](../workflows/storage/migrations.py),
run ownership in [`RunStore`](../workflows/storage/runs.py), and
[retention queries](../workflows/storage/operations.py#L109).
No schema or migration change is proposed here. No existing database data is
declared safe to drop based solely on a Python component classification.

## Removal candidates and prerequisites

“Candidate” means removable behavior under the governing contract once the
listed callers are detached and retained paths verified. It does not mean an
unconditional file deletion is safe in the current tree. Nothing was removed.

| Candidate | Evidence of separation | Required retained boundary before removal |
| --- | --- | --- |
| General chat fallback, `/clear`, `/reset`, chat display/history | Backend chat branch and commands are separate from JobService submissions; Runner receives no history. | Preserve CLI job/automation/schedule/approval commands, runtime timezone, independent worker ownership and inbox delivery; preserve current API session/run ownership until separately addressed. |
| Personal task/reminder commands, tools and delivery loop | Worker/Scheduler use jobs/schedules; personal loop uses TaskStore, not job notifications. | Detach backend reminder task, command handlers, AI reminder finalization, mode/tool schema registrations and TaskStore inheritance. Keep scheduler, job notification delivery and shared permission/approval behavior. Leave legacy data/schema intact. |
| Personal memory CRUD/tools/retriever/prompt injection | Runner omits AssistantContext; retrieval and handler installation are context-gated. | Detach request/assembly/tools/retrieval-facade imports and registrations; retain ContextManager, generic retrieval contracts, workspace knowledge index and currently shared session summary methods. |
| Voice-local modules, STT/TTS/microphone/playback/presentation | Only the voice entry route and voice tests call them; no automation caller. | Remove voice dispatch and voice-specific config/UI/documentation together when authorized. Preserve shared executor, LLM HTTP provider, sessions/run records and ApprovalBroker. |
| Dashboard chat viewer routes/history/assets | Web history is read-only conversation viewing; no job worker path. | Preserve settings server/editor/config parser and required configuration access; split mixed assets/routes before deleting complete files. |
| Chat Skill command routing and session-only selection | SQLite automation Skill versions flow independently through Runner → ContextManager. | Preserve Skill loader/registry/tool intersections/MCP restrictions and pinned automation Skill tables/APIs. API/voice also persist session selections; handle those callers before deleting selection code. |

Optional Skill proposal/detection/delegation features are **not removal
candidates for this round**. Their exposure can be separated later without
redesigning core Automation. Durable subtask compatibility is especially
important because Worker/Runner call child lifecycle/budget methods regardless
of whether a new job opts in.

### Third-party dependency retention

| Dependency or host program | Actual import/use | Audit decision |
| --- | --- | --- |
| `aiohttp` | Providers/executor/search, API/web and voice speech | Shared; voice/dashboard removal cannot remove runtime HTTP support. |
| `python-dotenv` | `main.py` configuration bootstrap | Shared; preserve provider/MCP/runtime environment configuration. |
| `PyYAML` | Configuration, Skill loader and MCP configuration | Shared; preserve. |
| `mcp` | `mcp_integration/client.py` stdio SDK | Core Automation integration; preserve. |
| `lingua-language-detector` | `application/language.py`, used by job request/final response | Shared current runtime behavior; preserve. |
| `prompt-toolkit`, `rich` | CLI input/history/output; voice also uses PromptSession | Shared current CLI path; do not remove without retaining command/approval/notification rendering. `rich` is in `requirements.txt` but absent from `pyproject.toml` dependencies; packaging parity is a recorded issue, not changed here. |
| `python-docx`, `openpyxl`, `pypdf` | Eager imports in `tools/file_reader/extraction.py` | Shared import requirement today. Document-tool removal/optional loading needs explicit review; removing packages now breaks tools/runtime imports. |
| `bwrap`, `prlimit` | `sandbox/runtime.py` capability and command construction | Keep host isolation support. Available executable does not prove namespace permission. |
| `arecord`, `aplay` | `interfaces/voice/audio.py` ALSA capture/playback | Voice-only host-program requirements; candidates with voice removal. No dedicated STT/TTS Python distribution appears in either dependency manifest; voice speech uses `aiohttp`. |

## Risks and Unknown / Needs Review register

| Item | Established evidence | Unresolved question / consequence |
| --- | --- | --- |
| Resolved: standalone worker ownership — Core Automation | [main worker route](../main.py#L38) → [application owner](../application/worker.py#L14); CLI no longer owns worker shutdown. [Worker tests](../tests/automation/test_worker_runtime.py#L118) exercise CLI exit, cancellation, approvals, restart and locks. | Closed as a dependency question. Preserve independent owner and real CLI inbox delivery. |
| High: mixed store and eager imports | JobStore inherits assistant/optional mixins; tools/retrieval facades eagerly import personal behavior. | Dependency is known. File/directory deletion based on product labels would break startup. Remove behavior only with import/composition changes in an authorized implementation round. |
| Resolved: assistant ownership versus durable API — Shared | API mounts durable services while startup and legacy `/runs` still use identity/session storage; [server.py:157](../interfaces/api/server.py#L157), [server.py:280](../interfaces/api/server.py#L280), [server.py:347](../interfaces/api/server.py#L347). | Closed: known retention boundary. Durable API availability does not make whole assistant stores safe to delete. |
| High: Needs Review — Job MCP authorization contract (Core Automation retained) | Production Runner excludes MCP before server startup; API/options reject arbitrary tool names. Selected MCP adapters perform a direct host SDK call after runtime schema/permission checks, without the Job's exact-action approval/workspace/sandbox chain. See MCP closure above. | Missing: approved selection/action-approval/confinement semantics and a positive production submission → claim → MCP call/cleanup test with deny/expiry/changed-action/sandbox coverage. Current exclusion is established; no reachable default-Job bypass is demonstrated. Keep infrastructure and default denial. |
| Resolved: bounded delegation versus durable children — Optional | `agent.delegate` has the same default Job exclusion but is explicitly Optional; [loop.py:276](../ai/execution/loop.py#L276) installs it only when selected. Durable `create_subtask` is separately selected by [runner.py:188](../workflows/runtime/runner.py#L188). | Closed classification, not a claim of default Job delegation support. Preserve unconditional child budget/reconciliation/recovery dependencies. |
| Medium: optional features remain import/storage dependencies | Proposals are explicit; cleanup still queries their tables. Subtasks opt in, but worker reconciliation/runner reservations are unconditional. | Dependency is known. Hiding tools/commands is distinct from deleting compatibility handlers/tables; existing durable children and proposal publication must remain valid. |
| Medium: Needs Review — generic native domain tool removal contract | [mode policy](../application/modes.py#L10) and [Job filtering](../ai/execution/request.py#L86) exclude dotted/alias facade names. Direct handlers have no Job approval/sandbox chain; default generic filesystem operations have no root. [domain tests](../tests/tools/test_domain_registry.py#L14) verify exports/direct dispatch, not Job safety. | Missing: required external Python/extension compatibility and intended public surface; no production deny test enumerates every generic name/alias. Keep unexposed. Preserve Shared registration/import contracts and safe web/search internals if wrappers are later retired. |
| Medium: Needs Review — operational helper public contract | [handle_operations](../interfaces/cli/operations.py#L15) has only [direct test callers](../tests/automation/test_hardening.py#L268); registry/backend do not dispatch it. Worker/Runner/services separately call Core Automation settings/quotas/retention/diagnostics; Job retrieval uses KnowledgeStore; CLI wires `notify_cli` independently. | Core backing APIs and Personal Assistant Only reminder helpers are classified; only wrapper routing/external compatibility or retirement remains unresolved. Direct tests and advertised `/notifications` do not prove a supported command route. Removing the whole mixed operations module breaks retained delivery. |
| Medium: Needs Review — installed extension/data references | Audit covers tracked source/tests, not private Skill files, MCP commands or a user's database contents. | Missing: an authorized inventory of installed extensions, persisted tool/config references and external Python consumers. Static repository evidence cannot certify their removal. Keep compatibility; no schema/data removal is proposed. |
| Medium: namespace verification limit | Both real Bubblewrap integration tests skip because host namespace creation is denied, even outside the command sandbox. | Filesystem/network namespace isolation and descendant behavior on a supported deployment host remain unverified here. Do not count mocked/process checks as proof of real namespace isolation. |

There is no unexplained local personal-assistant call on the traced job,
schedule, durable approval or job notification path. The unresolved execution
and external-contract questions above are explicitly retained as review items;
this audit does not certify all target-runtime integrations or unconditional
removal safety.

## Verification and conclusion

Python in the existing repository virtual environment: **3.14.4**.
No implementation/test/config/spec/schema/migration file was edited. No user
database was opened. Tests used temporary storage and deterministic model/HTTP
fakes, with local aiohttp servers and a test stdio MCP server where applicable.
Only this documentation file is updated.

Focused Job authorization/tool/helper verification command:

```bash
venv/bin/pytest -q -rs --tb=short tests/mcp/test_integration.py tests/agent/test_runtime.py tests/agent/test_permissions.py tests/tools/test_domain_registry.py tests/tools/test_workspace_tools.py tests/tools/test_command_tools.py tests/tools/test_sandbox.py tests/ai/test_ai_workspace.py tests/automation/test_approvals.py tests/automation/test_advanced.py tests/automation/test_hardening.py tests/automation/test_retention_cleanup.py tests/automation/test_scheduled_automation_execution.py tests/automation/test_worker_runtime.py tests/interfaces/test_runtime_api.py tests/interfaces/test_cli_live_notifications.py
```

| Check | Actual result |
| --- | --- |
| Focused command inside sandbox | **Incomplete; interrupted with exit 130** after reporting a failure and ceasing progress. No final pytest summary/traceback was obtained, so no pass/failure total or definitive cause is claimed for this attempt. |
| Same focused command outside sandbox | **198 passed, 2 skipped in 21.74s**, exit 0; no code/test changes or exclusions. These tests overlap the full suite, not extra unique coverage. |
| `venv/bin/pytest -q -rs` outside sandbox | **828 passed, 2 skipped in 48.98s**, exit 0. Both skips are real Bubblewrap tests at `tests/tools/test_sandbox.py:62` and `:104`: host policy prevents namespace creation. Real namespace isolation remains unverified on this host. |
| `git diff -- docs/runtime-dependency-audit.md`, `git diff --check` | Reviewed documentation diff; whitespace check passed. Changed-file inventory contains only this document. |

The scoped review confirms Core Automation retention for Job tool permissions,
durable approvals, workspace validation, selected command sandbox, workspace
retrieval and operational storage/inbox delivery. Shared facades/imports must
survive or be separated before removing Personal Assistant Only handlers;
Optional child helpers still have mandatory compatibility callers. Job MCP
authorization and generic/operator-wrapper public/removal contracts remain
Needs Review with the exact missing decisions/tests recorded above. The current
Job MCP exclusion is proven; successful contextless MCP calls do not establish
Job approval or confinement. Full tests pass with the disclosed namespace skips.
No implementation, tests, spec, schema, migrations or permissions were changed,
and no unconditional mixed-module or existing-data removal is certified.
