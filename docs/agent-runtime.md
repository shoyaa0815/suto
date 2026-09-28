# Agent runtime migration map (Phases 0–2)

The CLI remains the supported interface. Its request path is
`interfaces/cli/backend.py` → `ai/executor.py` → `ai/execution/loop.py` →
`agent/runtime.py`. The job worker uses the same `ai/executor.py` entry point.
The executor remains the stable compatibility facade for callers and tests.

| Existing module | Phase 1 destination | Decision |
| --- | --- | --- |
| `ai/execution/loop.py` | `agent/runtime.py` | Move the bounded model/tool orchestration into a provider-independent runtime; retain a small adapter for reply language, clarification, reminder completion, progress and audit. |
| `ai/models.py` | `agent/types.py`, `agent/state.py`, `agent/events.py`, `agent/limits.py` | Add request, result, state, event and run-limit contracts; retain `AIExecutionResult` for existing callers. |
| `ai/client.py`, `ai/providers/*` | `llm/base.py`, `llm/types.py`, `llm/router.py`, `ai/providers/*` | Ollama and OpenAI-compatible adapters now implement `Model.generate`; a router binds the configured default provider for each agent run. The legacy `chat()` entry point remains for language correction and document tools. |
| `tools/__init__.py`, `ai/tooling/assembly.py` | `tools/base.py`, `tools/types.py`, `tools/registry.py` | Adapt the selected request-scoped handlers to `ToolRegistry`; retain existing tool implementations and names. |
| `ai/execution/request.py`, `ai/prompting.py` | Future context manager | Reuse current request and context preparation; context budgets and compaction belong to Phase 4. |
| `assistant/conversations/*`, `workflows/storage/*` | Future session store port | Keep existing SQLite history, identity and job persistence. Phase 1 adds no schema migration. |
| `workflows/runtime/*`, `capabilities/developer/*` | Future permission and sandbox ports | Keep job-scoped tool filtering, approvals, workspace containment, command restrictions and Bubblewrap behavior. Centralization belongs to Phase 5. |

The runtime accepts a normalized model and a request-scoped registry. It never
imports concrete providers, tools, SQLite or CLI modules. Tool arguments are
checked against the exposed schema before authorization and execution. The
existing tool names remain stable so stored jobs and prompts continue to work.
There is no new user-facing command or configuration setting.

Phase 2 retains the existing HTTP request and legacy response handling inside
each provider. `generate()` converts that provider's result into `ModelResponse`,
including tool calls, usage and finish reason. The runtime receives only the
router's `Model` interface. There is no new user-facing setting or command.
Later phases can replace the compatibility hooks with
context, session and permission ports without changing the runtime loop.
