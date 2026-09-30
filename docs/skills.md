# Skills

A skill is a local `SKILL.md` document that supplies instructions and optional
tool guidance. It contains no executable handlers. Tools remain registered in
`ToolRegistry`, and every call still passes through the existing permission,
approval, and sandbox path.

Each skill lives in its own directory. `SkillRegistry.discover(root)` loads
`root/*/SKILL.md` in sorted order and rejects duplicate names. The two builtins
are under `skills/builtin/`. Other callers can supply a registry to
`execute_local_ai(..., skill_registry=registry)` and select names with
`active_skills=(...)` or `AgentRequest.active_skills`.

## `SKILL.md` format

```markdown
---
name: research
description: Gather and compare evidence.
recommended_tools: [search_web]
allowed_tools: [search_web, fetch_url]
configuration:
  depth: 2
metadata:
  owner: local
---
Gather relevant information, compare evidence, and summarize findings.
```

The opening and closing `---` lines delimit YAML metadata; the remaining Markdown
body is the required instruction text. `name` and `description` are required.
Names use lowercase letters, digits, `_`, and `-`. Tool names may also contain
`.`. `recommended_tools` and `allowed_tools` are optional lists of names;
`configuration` and `metadata` are optional JSON-compatible mappings. Unknown
fields, duplicates, malformed YAML, empty instructions, and files over 30,000
characters are rejected.

`recommended_tools` adds guidance only for tools already available to the
request. `allowed_tools` intersects the existing tool policy; an omitted list
places no extra restriction, while `[]` allows no tools. With multiple active
skills, all allow lists intersect. Neither field grants a tool or a permission.

Use `/skills` to discover available skills, `/skill activate <name>` and
`/skill deactivate <name>` in the CLI. The CLI also loads user skills from
`~/.suto/skills/<name>/SKILL.md` when it starts. Create the directory and files
yourself; the directory name must match the skill's `name`. Restart Suto after
adding or editing a skill. If the directory is missing, only builtins are
available. Invalid files, symlinks, duplicate names, and names that conflict
with CLI commands are skipped with a warning.

Run `/<name> <message>` to use a skill for one request, for example
`/research compare these sources`. The skill name is removed from the message
sent to the model, and this invocation does not change the saved selection.
Skills already activated for the session also apply to that request.
Public CLI commands take precedence over skill names. A skill invocation without
a message prints usage and sends no AI request.

Selections made with `/skill activate` are stored with the conversation and
restored when that session resumes. Each request resolves selected names,
filters available tools, and gives the active instructions to
`ContextManager`. Existing versioned automation skill instructions enter the
same context path, while their storage and pinning behavior remains unchanged.
