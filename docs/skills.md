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

Use `/skills` to discover builtins, `/skill activate <name>` and
`/skill deactivate <name>` in the CLI. Selection lasts for that CLI run, including
subsequent requests; it is not stored across restarts. Each request resolves
selected names, filters available tools, and gives the active instructions to
`ContextManager`. Existing versioned automation skill instructions enter the
same context path, while their storage and pinning behavior remains unchanged.
