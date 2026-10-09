# Job MCP selection metadata

Selection implements only the authorization preparation in
[the security contract](job-mcp-contract.md). **Job MCP execution remains disabled,
including for valid selections.** No actual server/tool is approved by this change.
No launcher, mounts, networking, credentials or sandbox permissions are added.
Host MCP configuration for consumers without a Job context is unchanged.

## Operator policy

Set `SUTO_JOB_MCP_POLICY` explicitly to an absolute, canonical path to a regular
JSON file owned by the runtime operator. Symlinks, group/world-writable files,
files over 64 KiB, duplicate JSON keys and unknown fields are rejected. Store it
outside every eligible Job workspace. There is no working-directory discovery,
model/Skill authoring endpoint, or fallback to `SUTO_MCP_CONFIG`. Both submitting
processes and the standalone worker must use the same operator configuration.
The existing local CLI/API caller is the authorized submitter; this change adds
no account model or permission to expose the API remotely.

The default allowlist is empty. An explicit empty policy is:

```json
{"format_version": 1, "tools": {}}
```

`tools` maps exact `mcp.<server>.<original-tool>` names to reviewed entries;
maximum 64 entries. Names are case-sensitive, server names cannot contain dots,
and duplicate names, aliases, whitespace normalization and wildcards grant
nothing. Each entry requires exactly:

| Field | Required value |
| --- | --- |
| `effect` | Literal `read_only`; mutation and unknown effects are rejected |
| `workspaces` | Nonempty array of distinct, absolute canonical workspace roots; exact match, no descendant or wildcard grants |
| `config_identity` | SHA-256 of the reviewed stdio configuration, including command, arguments and environment-source restrictions |
| `executable_identity` | SHA-256 of the reviewed executable identity |
| `dependency_identity` | SHA-256 of the reviewed dependency identity |
| `schema_identity` | SHA-256 of the supported tool schema |
| `effect_metadata_identity` | SHA-256 of the reviewed effect metadata |
| `review_identity` | SHA-256 of independent read-only review evidence |
| `mount_manifest_identity` | SHA-256 of the exact operator-reviewed permitted manifest |
| `confinement_identity` | SHA-256 of the reviewed local stdio/offline/no-credentials/read-only confinement profile |
| `resource_limits_identity` | SHA-256 of reviewed time, output and resource limits |

Identity values must be 64 lowercase hexadecimal characters. They are references
to operator review artifacts, not proof that a server, schema or deployment
satisfies those artifacts. This stage does not discover tools, verify live
executables/dependencies, resolve environment values or apply manifests. Those
checks and every other execution gate in the contract remain blockers. Do not
include secrets or raw environment values in policy artifacts or identities.

## Service selection and persistence

`JobService.submit(..., mcp_tools=[...])`,
`ScheduleService.create(..., mcp_tools=[...])`, and Automation definition files
accepted by `AutomationService.create/update` select only subsets of operator
policy. Definitions use the optional `mcp_tools` array. Omission or `[]` grants
nothing; null, duplicate, unknown, malformed and unauthorized selections deny
before persistence. Callers cannot submit policy identities, executable commands,
confinement settings or grants. Skill instructions remain untrusted prompt data.

`POST /jobs` accepts the same optional `mcp_tools` array and calls `JobService`.
There is no new CLI flag: saved definitions can be submitted through the existing
`/automation create/update` commands. Exported bundles preserve selection requests;
selected bundles cannot be imported as grants. Submit their definitions through
`AutomationService` for fresh validation.

Services create `options.mcp_selection` with sorted names, exact workspace,
`policy_identity` and `selection_identity`. Both identities use SHA-256 over
canonical JSON (sorted keys, compact separators, ASCII escaping). The policy
identity covers the entire policy, so even an unrelated policy edit requires a
fresh selection. The selection digest detects inconsistent metadata; it is not a
signature and does not replace trusted storage or operator control.

Jobs preserve the pin on retry/restart. Automation versions store the pin;
automation runs and scheduled automation snapshots copy it unchanged. Definition
updates cannot silently change already queued Jobs or scheduled snapshots.
Explicit schedule upgrade uses the target version's freshly validated pin.
Children inherit no selection and have no API to request one. SQLite triggers
reject replacement/removal of persisted selections and updates to version or
prompt-schedule options. Scheduled automation snapshots retain their existing
immutability trigger and are compared to the pinned version at materialization.

Migration 24 adds JSON `options` only to `automation_versions` and `schedules`:
inspection found these had no appropriate metadata storage. Existing Jobs and
scheduled automation snapshots already have options. Legacy rows receive `{}`;
no grants are invented and no old tables/data are removed. Existing transactional
migration, locking and verified pre-upgrade backup behavior is retained.

## Revalidation and denial

Current policy is read again when persisting selections, running an automation,
creating/upgrading/materializing an automated schedule, resuming a Job, and at
Runner entry. Unknown/revoked names, changed policy/artifact identities, changed
workspace bindings and inconsistent pins deny. Submission failures persist
nothing. A denied schedule materialization rolls back; the scheduler records a
sanitized skipped occurrence with no Job, advances its timing, and continues
unrelated schedules. Inspect schedule history for the denial. It never substitutes
a smaller tool set or a new policy identity. Regrant by submitting a fresh Job,
updating the Automation and explicitly upgrading its schedule, or creating a new
prompt schedule.

A claimed Job with selection is committed as `blocked` with `PERMISSION_DENIED`,
even if its selection still validates, before calling the AI executor. MCP names
are also excluded at request filtering/preparation for every Job context,
including forged context allowlists and Skill/model requests. The effective set
is always empty. Required pre-launch/per-call live-identity checks, confinement,
durable MCP intent/outcome/recovery and positive production execution evidence
remain unimplemented and cannot be bypassed by selection metadata.

Regression evidence: `tests/mcp/test_job_policy.py` exercises services, reopened
SQLite state, version pinning, upgrade, revocation, tampering, child denial,
migration rollback/backup, model/Skill denial and API → Worker → blocked result.
Startup, subprocess and tool-call spies must remain untouched in these tests.
