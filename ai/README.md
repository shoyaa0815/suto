# AI package

The package keeps stable imports at its root and groups implementation by the
reason it changes:

- `executor.py`: public request lifecycle, provider session, and error mapping;
- `execution/request.py`: mode policy, history, identity context, and schemas;
- `execution/loop.py`: bounded model/tool rounds and final-answer handling;
- `execution/limits.py`: elapsed-time, token, tool-call, and shared-budget gates;
- `tooling/assembly.py`: request-scoped tool-handler construction;
- `tooling/events.py`: safe tool audit details, signatures, and callbacks;
- `providers/`: provider protocol and Ollama/OpenAI-compatible adapters;
- `client.py`: provider-facing chat façade;
- `prompting.py` and `response.py`: model-facing input and output rules;
- `config.py`, `models.py`, and `progress.py`: shared contracts and state.

Keep `ai.ask_local_ai`, `ai.execute_local_ai`, `ai.client`, `ai.config`,
`ai.response`, `ai.executor`, and `ai.tool_runtime` compatible. New execution
behavior belongs in the owning subpackage rather than in compatibility façades.
