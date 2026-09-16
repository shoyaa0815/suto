# Developer capability

This package contains Suto's parked coding-specific capability:

- workspace file listing, reading, searching, and approved writes;
- durable coding plans;
- allowlisted verification commands;
- optional Bubblewrap command isolation.

Workspace tools live in `workspace/`: `schemas.py` owns model-facing contracts,
`reading.py` owns listing/read/search behavior, `writing.py` owns approved atomic
writes, and `paths.py` centralizes path-containment and hashing safeguards. Import
the public API from `capabilities.developer.workspace`.

The capability remains covered by tests for future opt-in use. Its internal
`developer` mode is not exposed by the main entrypoint; the public `agent` mode
is reserved for personal-assistant work.

Parked CLI parsing and presentation helpers live in `cli.py`; public CLI
commands belong in `interfaces/cli/commands.py` and must not be added here.
