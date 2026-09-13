# Developer capability

This package contains Suto's parked coding-specific capability:

- workspace file listing, reading, searching, and approved writes;
- durable coding plans;
- allowlisted verification commands;
- optional Bubblewrap command isolation.

The capability remains covered by tests for future opt-in use. Its internal
`developer` mode is not exposed by the main entrypoint; the public `agent` mode
is reserved for personal-assistant work.

Parked TUI parsing and presentation helpers live in `tui_cli.py`; public TUI
commands belong in `interfaces/tui/commands.py` and must not be added here.
