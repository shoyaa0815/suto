"""Model-facing schemas and guidance for workspace tools."""

PROMPT = """- Workspace tools are available only for automation jobs.
- Use list_workspace_files to inspect the workspace before assuming its layout.
- Use search_workspace to locate relevant text, then read_workspace_file for
  the exact files needed.
- apply_workspace_patch is available only for jobs explicitly created with
  write permission. Read an existing file first and pass its sha256 as
  expected_sha256. The exact diff requires user approval before the tool
  replaces the complete file content atomically.
- Never claim to have run a command unless run_workspace_command returned its
  result. File deletion is unavailable.
- All paths must be relative to the workspace. Never try to escape it with
  parent paths or external symlinks."""

LIST_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_workspace_files",
        "description": "List files and directories inside the job workspace.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative directory path. Defaults to the workspace root.",
                },
                "recursive": {
                    "type": "boolean",
                    "description": "List descendants recursively. Defaults to false.",
                },
            },
        },
    },
}

READ_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_workspace_file",
        "description": "Read one UTF-8 text file inside the job workspace.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path relative to the workspace.",
                },
            },
            "required": ["path"],
        },
    },
}

SEARCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_workspace",
        "description": "Search text files inside the job workspace.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Text to find, matched case-insensitively.",
                },
                "path": {
                    "type": "string",
                    "description": "Relative file or directory to search. Defaults to root.",
                },
            },
            "required": ["query"],
        },
    },
}

PATCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "apply_workspace_patch",
        "description": (
            "Atomically replace a text file or create a new text file in the "
            "workspace. Requires explicit job write permission."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path relative to the workspace.",
                },
                "content": {
                    "type": "string",
                    "description": "Complete new UTF-8 file content.",
                },
                "expected_sha256": {
                    "type": "string",
                    "description": (
                        "Current sha256 returned by read_workspace_file. "
                        "Required when modifying an existing file."
                    ),
                },
            },
            "required": ["path", "content"],
        },
    },
}
