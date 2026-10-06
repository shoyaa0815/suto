"""Versioned, transactional migrations with a verified pre-upgrade snapshot."""
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .locking import ProcessLock

SCHEMA_VERSION = 22

HARDENING = """
CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE runtime_settings(key TEXT PRIMARY KEY, value INTEGER NOT NULL);
INSERT INTO runtime_settings VALUES ('max_queued_jobs', 1000), ('submissions_per_minute', 120),
 ('daily_token_quota', 10000000), ('workspace_concurrency', 1), ('concurrency', 1), ('retention_days', 0);
CREATE TABLE worker_state(id INTEGER PRIMARY KEY CHECK(id=1), pid INTEGER,
 heartbeat TEXT NOT NULL, state TEXT NOT NULL);
CREATE TABLE job_stats(job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
 queue_seconds REAL NOT NULL DEFAULT 0, run_seconds REAL NOT NULL DEFAULT 0,
 tool_calls INTEGER NOT NULL DEFAULT 0, tool_errors INTEGER NOT NULL DEFAULT 0,
 queued_at TEXT, running_at TEXT);
INSERT INTO job_stats(job_id, queued_at, running_at)
 SELECT id, created_at, CASE WHEN status='running' THEN started_at END FROM jobs;
UPDATE job_stats SET tool_calls=(SELECT COUNT(*) FROM tool_events WHERE job_id=job_stats.job_id),
 tool_errors=(SELECT COUNT(*) FROM tool_events WHERE job_id=job_stats.job_id AND
 (error IS NOT NULL OR status IN ('failed','blocked','timed out','timed_out')));
CREATE TABLE daily_usage(day TEXT PRIMARY KEY, tokens INTEGER NOT NULL DEFAULT 0);
CREATE TABLE structured_logs(id INTEGER PRIMARY KEY AUTOINCREMENT,
 job_id TEXT REFERENCES jobs(id) ON DELETE CASCADE, event_type TEXT NOT NULL,
 detail TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
CREATE INDEX structured_logs_job_idx ON structured_logs(job_id, id);
CREATE TRIGGER job_admission BEFORE INSERT ON jobs BEGIN
 SELECT CASE WHEN (SELECT COUNT(*) FROM jobs WHERE status='queued') >=
 (SELECT value FROM runtime_settings WHERE key='max_queued_jobs')
 THEN RAISE(ABORT, 'job queue quota reached') END;
 SELECT CASE WHEN (SELECT COUNT(*) FROM jobs WHERE julianday(created_at) >= julianday('now','-1 minute')) >=
 (SELECT value FROM runtime_settings WHERE key='submissions_per_minute')
 THEN RAISE(ABORT, 'job submission rate limit reached') END;
END;
CREATE TRIGGER job_created AFTER INSERT ON jobs BEGIN
 INSERT INTO job_stats(job_id, queued_at) VALUES(NEW.id, NEW.created_at);
 INSERT INTO structured_logs(job_id,event_type,created_at) VALUES(NEW.id,'queued',NEW.created_at);
END;
CREATE TRIGGER job_transition AFTER UPDATE OF status ON jobs WHEN OLD.status != NEW.status BEGIN
 INSERT INTO structured_logs(job_id,event_type,detail,created_at)
 VALUES(NEW.id,NEW.status,COALESCE(NEW.error,''),strftime('%Y-%m-%dT%H:%M:%f+00:00','now'));
 UPDATE job_stats SET
 queue_seconds=queue_seconds + CASE WHEN OLD.status='queued' THEN
 MAX(0,(julianday('now')-julianday(queued_at))*86400) ELSE 0 END,
 run_seconds=run_seconds + CASE WHEN OLD.status='running' THEN
 MAX(0,(julianday('now')-julianday(running_at))*86400) ELSE 0 END,
 queued_at=CASE WHEN NEW.status='queued' THEN strftime('%Y-%m-%dT%H:%M:%f+00:00','now') ELSE queued_at END,
 running_at=CASE WHEN NEW.status='running' THEN strftime('%Y-%m-%dT%H:%M:%f+00:00','now') ELSE NULL END
 WHERE job_id=NEW.id;
END;
CREATE TRIGGER job_token_usage AFTER UPDATE OF prompt_tokens,output_tokens ON jobs BEGIN
 INSERT INTO daily_usage(day,tokens) VALUES(date('now'),MAX(0,
 NEW.prompt_tokens+NEW.output_tokens-OLD.prompt_tokens-OLD.output_tokens))
 ON CONFLICT(day) DO UPDATE SET tokens=tokens+excluded.tokens;
END;
CREATE TRIGGER tool_stats AFTER INSERT ON tool_events BEGIN
 UPDATE job_stats SET tool_calls=tool_calls+1,
 tool_errors=tool_errors+CASE WHEN NEW.error IS NOT NULL OR NEW.status IN ('failed','blocked','timed out','timed_out') THEN 1 ELSE 0 END
 WHERE job_id=NEW.job_id;
 INSERT INTO structured_logs(job_id,event_type,detail,created_at)
 VALUES(NEW.job_id,'tool.'||NEW.status,NEW.tool_name||': '||COALESCE(NEW.error,''),NEW.created_at);
END;
CREATE TRIGGER progress_log AFTER INSERT ON job_events BEGIN
 INSERT INTO structured_logs(job_id,event_type,detail,created_at)
 VALUES(NEW.job_id,NEW.event_type,NEW.detail,NEW.created_at);
END;
"""

ADVANCED = """
ALTER TABLE jobs ADD COLUMN parent_id TEXT REFERENCES jobs(id);
ALTER TABLE jobs ADD COLUMN options TEXT NOT NULL DEFAULT '{}';
CREATE INDEX jobs_parent_idx ON jobs(parent_id);
CREATE TABLE knowledge_files(workspace TEXT NOT NULL, path TEXT NOT NULL,
 sha256 TEXT NOT NULL, PRIMARY KEY(workspace,path));
CREATE VIRTUAL TABLE knowledge_chunks USING fts5(workspace UNINDEXED, path UNINDEXED,
 line_start UNINDEXED, line_end UNINDEXED, sha256 UNINDEXED, content, tokenize='unicode61');
CREATE TABLE notifications(id INTEGER PRIMARY KEY AUTOINCREMENT,
 job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
 status TEXT NOT NULL, created_at TEXT NOT NULL, read_at TEXT);
CREATE TRIGGER job_notification AFTER UPDATE OF status ON jobs
 WHEN OLD.status != NEW.status AND NEW.status IN
 ('completed','failed','blocked','waiting_approval','interrupted','cancelled') BEGIN
 INSERT INTO notifications(job_id,status,created_at)
 VALUES(NEW.id,NEW.status,strftime('%Y-%m-%dT%H:%M:%f+00:00','now'));
END;
"""

PERSONAL_ASSISTANT = """
CREATE TABLE users(
 id TEXT PRIMARY KEY, display_name TEXT NOT NULL, timezone TEXT NOT NULL,
 locale TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE channel_identities(
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 channel TEXT NOT NULL, external_id TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(channel,external_id));
CREATE INDEX channel_identities_user_idx ON channel_identities(user_id);
CREATE TABLE user_preferences(
 user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 key TEXT NOT NULL, value TEXT NOT NULL, updated_at TEXT NOT NULL,
 PRIMARY KEY(user_id,key));
CREATE TABLE conversations(
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 channel TEXT NOT NULL, external_thread_id TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(user_id,channel,external_thread_id));
CREATE INDEX conversations_user_updated_idx ON conversations(user_id,updated_at);
CREATE TABLE messages(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
 role TEXT NOT NULL CHECK(role IN ('user','assistant')),
 content TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX messages_conversation_idx ON messages(conversation_id,id);
CREATE TABLE assistant_tasks(
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 title TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL CHECK(status IN ('open','completed','cancelled')),
 due_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, completed_at TEXT);
CREATE INDEX assistant_tasks_user_status_idx ON assistant_tasks(user_id,status,due_at);
CREATE TABLE reminders(
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 title TEXT NOT NULL, remind_at TEXT NOT NULL, timezone TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('scheduled','delivered','cancelled')),
 channel_identity_id TEXT REFERENCES channel_identities(id) ON DELETE SET NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX reminders_due_idx ON reminders(status,remind_at);
CREATE INDEX reminders_user_idx ON reminders(user_id,status,remind_at);
"""

DAILY_BRIEFING = """
CREATE TABLE briefing_deliveries(
 user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 local_date TEXT NOT NULL, delivered_at TEXT NOT NULL,
 PRIMARY KEY(user_id,local_date));
"""

REMINDER_DELIVERY = """
CREATE TABLE delivery_targets(
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 platform TEXT NOT NULL, destination_id TEXT NOT NULL,
 destination_type TEXT NOT NULL CHECK(destination_type IN ('dm','guild_channel')),
 display_name TEXT NOT NULL, guild_id TEXT, requester_id TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(user_id,platform,destination_id));
CREATE INDEX delivery_targets_user_idx ON delivery_targets(user_id,platform);
ALTER TABLE reminders ADD COLUMN delivery_target_id TEXT
 REFERENCES delivery_targets(id) ON DELETE SET NULL;
CREATE INDEX reminders_delivery_target_idx ON reminders(delivery_target_id,status,remind_at);
CREATE TABLE reminder_deliveries(
 reminder_id TEXT PRIMARY KEY REFERENCES reminders(id) ON DELETE CASCADE,
 status TEXT NOT NULL CHECK(status IN ('pending','sending','retrying','delivered','failed')),
 attempt_count INTEGER NOT NULL DEFAULT 0, retry_at TEXT,
 last_error TEXT, updated_at TEXT NOT NULL);
CREATE INDEX reminder_deliveries_status_idx
 ON reminder_deliveries(status,retry_at,updated_at);
"""

DISCORD_BRIEFING_DELIVERY = """
CREATE TABLE external_briefing_deliveries(
 user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 local_date TEXT NOT NULL,
 delivery_target_id TEXT NOT NULL REFERENCES delivery_targets(id) ON DELETE CASCADE,
 scheduled_time TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','sending','retrying','delivered','failed')),
 attempt_count INTEGER NOT NULL DEFAULT 0, retry_at TEXT,
 last_error TEXT, updated_at TEXT NOT NULL,
 PRIMARY KEY(user_id,local_date));
CREATE INDEX external_briefing_deliveries_status_idx
 ON external_briefing_deliveries(status,retry_at,updated_at);
"""

# Versions 7 and 8 preserve compatibility with databases created while the
# retired briefing feature was changing. These tables have no active runtime.
VERSION_7_COMPATIBILITY = "SELECT 1;"

LEGACY_BRIEFING_SCHEMA = """
CREATE TABLE IF NOT EXISTS briefing_deliveries(
 user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 local_date TEXT NOT NULL, delivered_at TEXT NOT NULL,
 PRIMARY KEY(user_id,local_date));
CREATE TABLE IF NOT EXISTS external_briefing_deliveries(
 user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 local_date TEXT NOT NULL,
 delivery_target_id TEXT NOT NULL REFERENCES delivery_targets(id) ON DELETE CASCADE,
 scheduled_time TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','sending','retrying','delivered','failed')),
 attempt_count INTEGER NOT NULL DEFAULT 0, retry_at TEXT,
 last_error TEXT, updated_at TEXT NOT NULL,
 PRIMARY KEY(user_id,local_date));
CREATE INDEX IF NOT EXISTS external_briefing_deliveries_status_idx
 ON external_briefing_deliveries(status,retry_at,updated_at);
"""

REMOVE_SEMANTIC_MEMORY = """
DROP TABLE IF EXISTS memories;
DROP TABLE IF EXISTS workspace_features;
"""

THREE_TIER_MEMORY = """
CREATE TABLE IF NOT EXISTS session_summaries(
 conversation_id TEXT PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
 user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 summary TEXT NOT NULL,
 updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS session_summaries_user_idx ON session_summaries(user_id);
CREATE TABLE IF NOT EXISTS assistant_memories(
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 category TEXT NOT NULL DEFAULT 'general', content TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS assistant_memories_user_idx ON assistant_memories(user_id, category);
CREATE VIRTUAL TABLE IF NOT EXISTS assistant_memories_fts USING fts5(
 memory_id UNINDEXED, user_id UNINDEXED, category UNINDEXED, content, tokenize='unicode61');
CREATE TRIGGER IF NOT EXISTS assistant_memories_ai AFTER INSERT ON assistant_memories BEGIN
 INSERT INTO assistant_memories_fts(memory_id, user_id, category, content)
 VALUES (NEW.id, NEW.user_id, NEW.category, NEW.content);
END;
CREATE TRIGGER IF NOT EXISTS assistant_memories_ad AFTER DELETE ON assistant_memories BEGIN
 DELETE FROM assistant_memories_fts WHERE memory_id = OLD.id;
END;
CREATE TRIGGER IF NOT EXISTS assistant_memories_au AFTER UPDATE ON assistant_memories BEGIN
 DELETE FROM assistant_memories_fts WHERE memory_id = OLD.id;
 INSERT INTO assistant_memories_fts(memory_id, user_id, category, content)
 VALUES (NEW.id, NEW.user_id, NEW.category, NEW.content);
END;
"""

AGENT_ONLY_MODE = """
UPDATE jobs SET mode = 'agent' WHERE mode IN ('chat', 'home', 'developer');
"""

SESSION_COMPACTION = """
ALTER TABLE session_summaries ADD COLUMN compacted_through_message_id INTEGER NOT NULL DEFAULT 0;
"""

CORE_TRACE_AND_MESSAGES = """
CREATE TABLE messages_v13(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
 role TEXT NOT NULL CHECK(role IN ('system','user','assistant','tool')),
 content TEXT NOT NULL, created_at TEXT NOT NULL,
 metadata TEXT NOT NULL DEFAULT '{}');
INSERT INTO messages_v13(id,conversation_id,role,content,created_at)
 SELECT id,conversation_id,role,content,created_at FROM messages;
DROP TABLE messages;
ALTER TABLE messages_v13 RENAME TO messages;
CREATE INDEX messages_conversation_idx ON messages(conversation_id,id);
CREATE TABLE run_events(
 event_id TEXT PRIMARY KEY,
 run_id TEXT NOT NULL,
 job_id TEXT REFERENCES jobs(id) ON DELETE CASCADE,
 session_id TEXT REFERENCES conversations(id) ON DELETE CASCADE,
 parent_run_id TEXT,
 tool_call_id TEXT,
 event_type TEXT NOT NULL,
 data TEXT NOT NULL DEFAULT '{}',
 created_at TEXT NOT NULL);
CREATE INDEX run_events_run_idx ON run_events(run_id,created_at);
CREATE INDEX run_events_job_idx ON run_events(job_id,created_at);
ALTER TABLE job_events ADD COLUMN run_id TEXT;
ALTER TABLE tool_events ADD COLUMN run_id TEXT;
ALTER TABLE tool_events ADD COLUMN tool_call_id TEXT;
"""

RUN_LIFECYCLE = """
CREATE TABLE agent_runs(
 id TEXT PRIMARY KEY,
 session_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
 parent_run_id TEXT,
 status TEXT NOT NULL,
 created_at TEXT NOT NULL,
 started_at TEXT,
 completed_at TEXT,
 final_text TEXT,
 error TEXT,
 usage_json TEXT NOT NULL DEFAULT '{}',
 owner_pid INTEGER NOT NULL,
 owner_start TEXT NOT NULL
);
CREATE UNIQUE INDEX agent_runs_active_session_idx ON agent_runs(session_id)
 WHERE parent_run_id IS NULL AND status IN ('queued','running');
CREATE TABLE session_skills(
 session_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
 skill_name TEXT NOT NULL,
 position INTEGER NOT NULL,
 PRIMARY KEY(session_id, skill_name),
 UNIQUE(session_id, position)
);
"""

JOB_ATTEMPTS = """
ALTER TABLE jobs ADD COLUMN attempt_id TEXT;
CREATE TABLE job_attempts(
 id TEXT PRIMARY KEY,
 job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
 ordinal INTEGER NOT NULL CHECK(ordinal > 0),
 status TEXT NOT NULL,
 started_at TEXT NOT NULL,
 finished_at TEXT,
 UNIQUE(job_id, ordinal)
);
CREATE INDEX job_attempts_job_idx ON job_attempts(job_id, ordinal);
CREATE TRIGGER job_attempt_status AFTER UPDATE OF status ON jobs
 WHEN OLD.status IN ('running', 'waiting_approval', 'waiting_children')
 AND OLD.status != NEW.status AND NEW.attempt_id IS NOT NULL
 AND NEW.status NOT IN ('queued', 'running') BEGIN
 UPDATE job_attempts
 SET status=NEW.status,
     finished_at=COALESCE(NEW.finished_at, strftime('%Y-%m-%dT%H:%M:%f+00:00','now'))
 WHERE id=NEW.attempt_id;
END;
"""

AUTOMATION_RUNS = """
CREATE TABLE automation_run_parameters(
 job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
 automation_version_id TEXT NOT NULL REFERENCES automation_versions(id),
 parameters TEXT NOT NULL
);
"""

SCHEDULE_OCCURRENCE_IDENTITY = """
CREATE TRIGGER IF NOT EXISTS schedule_occurrence_job_identity
BEFORE INSERT ON trigger_history
WHEN NEW.job_id IS NOT NULL AND EXISTS (
 SELECT 1 FROM trigger_history
 WHERE schedule_id = NEW.schedule_id AND scheduled_for = NEW.scheduled_for
   AND job_id IS NOT NULL AND job_id != NEW.job_id
)
BEGIN
 SELECT RAISE(ABORT, 'schedule occurrence already has a job');
END;
CREATE TRIGGER IF NOT EXISTS schedule_occurrence_job_identity_update
BEFORE UPDATE OF schedule_id, scheduled_for, job_id ON trigger_history
WHEN NEW.job_id IS NOT NULL AND EXISTS (
 SELECT 1 FROM trigger_history
 WHERE id != OLD.id AND schedule_id = NEW.schedule_id
   AND scheduled_for = NEW.scheduled_for
   AND job_id IS NOT NULL AND job_id != NEW.job_id
)
BEGIN
 SELECT RAISE(ABORT, 'schedule occurrence already has a job');
END;
"""

SCHEDULED_AUTOMATION_SNAPSHOTS = """
CREATE TABLE schedule_automation_snapshots (
 schedule_id TEXT PRIMARY KEY REFERENCES schedules(id) ON DELETE CASCADE,
 automation_name TEXT NOT NULL,
 automation_version_id TEXT NOT NULL REFERENCES automation_versions(id) ON DELETE RESTRICT,
 automation_version INTEGER NOT NULL CHECK(automation_version > 0),
 parameters TEXT NOT NULL CHECK(json_valid(parameters) AND json_type(parameters) = 'object'),
 skill_version_ids TEXT NOT NULL CHECK(json_valid(skill_version_ids) AND json_type(skill_version_ids) = 'array'),
 workspace TEXT NOT NULL,
 allow_write INTEGER NOT NULL CHECK(allow_write IN (0, 1)),
 allow_command INTEGER NOT NULL CHECK(allow_command IN (0, 1)),
 options TEXT NOT NULL CHECK(json_valid(options) AND json_type(options) = 'object')
);
CREATE TRIGGER schedule_automation_snapshot_immutable
BEFORE UPDATE ON schedule_automation_snapshots BEGIN
 SELECT RAISE(ABORT, 'scheduled automation snapshot is immutable');
END;
"""

JOB_RESULT_NOTIFICATIONS = """
ALTER TABLE notifications ADD COLUMN kind TEXT NOT NULL DEFAULT '';
UPDATE notifications SET kind = CASE status
 WHEN 'waiting_approval' THEN 'approval_required'
 ELSE status END;
CREATE INDEX notifications_unread_idx ON notifications(read_at, id);
DROP TRIGGER job_notification;
CREATE TRIGGER job_notification AFTER UPDATE OF status ON jobs
WHEN OLD.status != NEW.status AND NEW.status IN
 ('completed','failed','blocked','waiting_approval','interrupted','cancelled') BEGIN
 INSERT INTO notifications(job_id,status,created_at,kind)
 VALUES(NEW.id,NEW.status,strftime('%Y-%m-%dT%H:%M:%f+00:00','now'),
  CASE
   WHEN NEW.status='waiting_approval' THEN 'approval_required'
   WHEN NEW.status='failed' AND NEW.source='schedule' AND EXISTS (
    SELECT 1 FROM trigger_history AS history
    JOIN schedules ON schedules.id=history.schedule_id
    WHERE history.job_id=NEW.id AND history.status='created'
      AND history.attempt > schedules.retry_limit
      AND (schedules.retry_limit > 0 OR NEW.retry_count > 0)
   ) THEN 'retry_exhausted'
   ELSE NEW.status
  END);
END;
"""

SKILL_DRAFT_PROPOSALS = """
CREATE TABLE skill_draft_proposals(
 id TEXT PRIMARY KEY,
 name TEXT NOT NULL,
 instructions TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','approved','rejected','deleted')),
 source_job_ids TEXT NOT NULL CHECK(json_valid(source_job_ids) AND json_type(source_job_ids)='array'),
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 CHECK(status != 'deleted' OR (name='' AND instructions='' AND source_job_ids='[]'))
);
CREATE INDEX skill_draft_proposals_status_idx ON skill_draft_proposals(status,created_at);
CREATE TABLE skill_proposal_events(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 proposal_id TEXT NOT NULL REFERENCES skill_draft_proposals(id) ON DELETE CASCADE,
 status TEXT NOT NULL CHECK(status IN ('pending','approved','rejected','deleted')),
 created_at TEXT NOT NULL
);
CREATE INDEX skill_proposal_events_proposal_idx ON skill_proposal_events(proposal_id,id);
CREATE TRIGGER skill_draft_proposals_initial_status BEFORE INSERT ON skill_draft_proposals
WHEN NEW.status!='pending'
BEGIN SELECT RAISE(ABORT, 'skill proposal must start pending'); END;
CREATE TRIGGER skill_draft_proposals_transition BEFORE UPDATE ON skill_draft_proposals
WHEN NOT (
 (OLD.status='pending' AND NEW.status IN ('approved','rejected','deleted')) OR
 (OLD.status IN ('approved','rejected') AND NEW.status='deleted')
) OR NEW.id!=OLD.id OR NEW.created_at!=OLD.created_at OR
 (NEW.status!='deleted' AND (
  NEW.name!=OLD.name OR NEW.instructions!=OLD.instructions OR
  NEW.source_job_ids!=OLD.source_job_ids
 )) OR (NEW.status='deleted' AND (
  NEW.name!='' OR NEW.instructions!='' OR NEW.source_job_ids!='[]'
 ))
BEGIN SELECT RAISE(ABORT, 'invalid skill proposal transition'); END;
CREATE TRIGGER skill_draft_proposals_created AFTER INSERT ON skill_draft_proposals
BEGIN INSERT INTO skill_proposal_events(proposal_id,status,created_at)
 VALUES(NEW.id,NEW.status,NEW.created_at); END;
CREATE TRIGGER skill_draft_proposals_changed AFTER UPDATE ON skill_draft_proposals
BEGIN INSERT INTO skill_proposal_events(proposal_id,status,created_at)
 VALUES(NEW.id,NEW.status,NEW.updated_at); END;
"""

REPEATED_WORKFLOW_PROPOSALS = """
ALTER TABLE skill_draft_proposals ADD COLUMN detection_key TEXT;
CREATE UNIQUE INDEX skill_draft_proposals_detection_key_idx
 ON skill_draft_proposals(detection_key) WHERE detection_key IS NOT NULL;
DROP TRIGGER skill_draft_proposals_transition;
CREATE TRIGGER skill_draft_proposals_transition BEFORE UPDATE ON skill_draft_proposals
WHEN NOT (
 (OLD.status='pending' AND NEW.status IN ('approved','rejected','deleted')) OR
 (OLD.status IN ('approved','rejected') AND NEW.status='deleted')
) OR NEW.id!=OLD.id OR NEW.created_at!=OLD.created_at OR
 NEW.detection_key IS NOT OLD.detection_key OR
 (NEW.status!='deleted' AND (
  NEW.name!=OLD.name OR NEW.instructions!=OLD.instructions OR
  NEW.source_job_ids!=OLD.source_job_ids
 )) OR (NEW.status='deleted' AND (
  NEW.name!='' OR NEW.instructions!='' OR NEW.source_job_ids!='[]'
 ))
BEGIN SELECT RAISE(ABORT, 'invalid skill proposal transition'); END;
"""

PROPOSAL_RETENTION = """
ALTER TABLE skill_draft_proposals ADD COLUMN redacted INTEGER NOT NULL DEFAULT 0
 CHECK(redacted IN (0,1));
DROP TRIGGER skill_draft_proposals_transition;
CREATE TRIGGER skill_draft_proposals_transition BEFORE UPDATE ON skill_draft_proposals
WHEN NOT (
 (OLD.status='pending' AND NEW.status IN ('approved','rejected','deleted')) OR
 (OLD.status IN ('approved','rejected') AND NEW.status='deleted') OR
 (OLD.status='rejected' AND NEW.status='rejected' AND OLD.redacted=0 AND
  NEW.redacted=1 AND NEW.name='' AND NEW.instructions='' AND NEW.source_job_ids='[]')
) OR NEW.id!=OLD.id OR NEW.created_at!=OLD.created_at OR
 NEW.detection_key IS NOT OLD.detection_key OR
 (OLD.redacted=1 AND NEW.redacted!=1) OR
 (NEW.status!='deleted' AND NEW.redacted=0 AND (
  NEW.name!=OLD.name OR NEW.instructions!=OLD.instructions OR
  NEW.source_job_ids!=OLD.source_job_ids
 )) OR (NEW.status='deleted' AND (
  NEW.name!='' OR NEW.instructions!='' OR NEW.source_job_ids!='[]'
 )) OR (NEW.redacted=1 AND (
  NEW.name!='' OR NEW.instructions!='' OR NEW.source_job_ids!='[]' OR
  NEW.status NOT IN ('rejected','deleted')
 ))
BEGIN SELECT RAISE(ABORT, 'invalid skill proposal transition'); END;
DROP TRIGGER skill_draft_proposals_changed;
CREATE TRIGGER skill_draft_proposals_changed AFTER UPDATE ON skill_draft_proposals
WHEN OLD.status!=NEW.status
BEGIN INSERT INTO skill_proposal_events(proposal_id,status,created_at)
 VALUES(NEW.id,NEW.status,NEW.updated_at); END;
"""


def backup_database(source: Path, destination: Path) -> Path:
    source, destination = source.resolve(), destination.expanduser().absolute()
    if destination.resolve() == source:
        raise ValueError('backup must not replace the live database')
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        with closing(sqlite3.connect(f'{source.as_uri()}?mode=ro', uri=True)) as src:
            with closing(sqlite3.connect(destination)) as dst:
                src.backup(dst)
                if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise RuntimeError('backup integrity check failed')
        with destination.open('rb') as stream:
            os.fsync(stream.fileno())
    except BaseException:
        destination.unlink()  # Only the exclusive, newly created incomplete backup.
        raise
    return destination


def initialize_database(store) -> None:
    # Initialization is serialized separately from the worker lifecycle lock.
    lock = ProcessLock(store.path.with_suffix(store.path.suffix + '.migration.lock'))
    if not lock.acquire():
        raise RuntimeError('database migration already in progress; retry startup')
    try:
        with store._connect() as connection:
            version = connection.execute('PRAGMA user_version').fetchone()[0]
            existing = connection.execute("SELECT 1 FROM sqlite_master WHERE name='jobs'").fetchone()
        if version > SCHEMA_VERSION:
            raise RuntimeError(f'database schema {version} is newer than supported {SCHEMA_VERSION}')
        if version == SCHEMA_VERSION:
            return
        with ProcessLock(store.path.with_suffix(store.path.suffix + '.worker.lock')):
            if existing:
                backup_database(store.path, store.path.parent / 'backups' /
                    f'{store.path.name}.v{version}.{uuid4().hex}.db')
            if version == 0:
                store._initialize()
            for number, script in (
                (1, HARDENING),
                (2, ADVANCED),
                (3, PERSONAL_ASSISTANT),
                (4, DAILY_BRIEFING),
                (5, REMINDER_DELIVERY),
                (6, DISCORD_BRIEFING_DELIVERY),
                (7, VERSION_7_COMPATIBILITY),
                (8, LEGACY_BRIEFING_SCHEMA),
                (9, REMOVE_SEMANTIC_MEMORY),
                (10, THREE_TIER_MEMORY),
                (11, AGENT_ONLY_MODE),
                (12, SESSION_COMPACTION),
                (13, CORE_TRACE_AND_MESSAGES),
                (14, RUN_LIFECYCLE),
                (15, JOB_ATTEMPTS),
                (16, AUTOMATION_RUNS),
                (17, SCHEDULE_OCCURRENCE_IDENTITY),
                (18, SCHEDULED_AUTOMATION_SNAPSHOTS),
                (19, JOB_RESULT_NOTIFICATIONS),
                (20, SKILL_DRAFT_PROPOSALS),
                (21, REPEATED_WORKFLOW_PROPOSALS),
                (22, PROPOSAL_RETENTION),
            ):
                if number > SCHEMA_VERSION:
                    break
                if number <= version:
                    continue
                with store._connect() as connection:
                    # A restored database can have the column while its version
                    # marker still reflects an earlier snapshot.
                    migration_script = script
                    if number == 12 and any(
                        row[1] == 'compacted_through_message_id'
                        for row in connection.execute('PRAGMA table_info(session_summaries)')
                    ):
                        migration_script = ''
                    if number == 13:
                        message_columns = {row[1] for row in connection.execute('PRAGMA table_info(messages)')}
                        job_event_columns = {row[1] for row in connection.execute('PRAGMA table_info(job_events)')}
                        tool_event_columns = {row[1] for row in connection.execute('PRAGMA table_info(tool_events)')}
                        markers = (
                            'metadata' in message_columns,
                            connection.execute("SELECT 1 FROM sqlite_master WHERE name='run_events'").fetchone() is not None,
                            'run_id' in job_event_columns,
                            'run_id' in tool_event_columns,
                            'tool_call_id' in tool_event_columns,
                        )
                        if all(markers):
                            migration_script = ''
                        elif any(markers):
                            raise RuntimeError('database has a partial core trace migration; restore a verified backup')
                    if number == 14:
                        markers = tuple(
                            connection.execute(
                                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                (name,),
                            ).fetchone() is not None
                            for name in ('agent_runs', 'session_skills')
                        )
                        if all(markers):
                            migration_script = ''
                        elif any(markers):
                            raise RuntimeError('database has a partial run lifecycle migration; restore a verified backup')
                    if number == 15:
                        markers = (
                            'attempt_id' in {
                                row[1] for row in connection.execute('PRAGMA table_info(jobs)')
                            },
                            connection.execute(
                                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='job_attempts'"
                            ).fetchone() is not None,
                            connection.execute(
                                "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='job_attempt_status'"
                            ).fetchone() is not None,
                        )
                        if all(markers):
                            migration_script = ''
                        elif any(markers):
                            raise RuntimeError('database has a partial job attempt migration; restore a verified backup')
                    if number == 16 and connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='automation_run_parameters'"
                    ).fetchone() is not None:
                        migration_script = ''
                    if number == 17 and connection.execute(
                        "SELECT 1 FROM trigger_history WHERE job_id IS NOT NULL "
                        "GROUP BY schedule_id, scheduled_for "
                        "HAVING COUNT(DISTINCT job_id) > 1 LIMIT 1"
                    ).fetchone() is not None:
                        raise RuntimeError('schedule occurrence has conflicting jobs; restore a verified backup')
                    if number == 18:
                        markers = (
                            connection.execute(
                                "SELECT 1 FROM sqlite_master WHERE type='table' "
                                "AND name='schedule_automation_snapshots'"
                            ).fetchone() is not None,
                            connection.execute(
                                "SELECT 1 FROM sqlite_master WHERE type='trigger' "
                                "AND name='schedule_automation_snapshot_immutable'"
                            ).fetchone() is not None,
                        )
                        if all(markers):
                            migration_script = ''
                        elif any(markers):
                            raise RuntimeError('database has a partial scheduled automation migration; restore a verified backup')
                    if number == 19:
                        trigger = connection.execute(
                            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='job_notification'"
                        ).fetchone()
                        markers = (
                            'kind' in {row[1] for row in connection.execute('PRAGMA table_info(notifications)')},
                            connection.execute(
                                "SELECT 1 FROM sqlite_master WHERE type='index' "
                                "AND name='notifications_unread_idx'"
                            ).fetchone() is not None,
                            trigger is not None and 'retry_exhausted' in trigger[0],
                        )
                        if all(markers):
                            migration_script = ''
                        elif any(markers):
                            raise RuntimeError('database has a partial job notification migration; restore a verified backup')
                    if number == 20:
                        markers = tuple(
                            connection.execute(
                                "SELECT 1 FROM sqlite_master WHERE type=? AND name=?",
                                (kind, name),
                            ).fetchone() is not None
                            for kind, name in (
                                ('table', 'skill_draft_proposals'),
                                ('table', 'skill_proposal_events'),
                                ('index', 'skill_draft_proposals_status_idx'),
                                ('index', 'skill_proposal_events_proposal_idx'),
                                ('trigger', 'skill_draft_proposals_initial_status'),
                                ('trigger', 'skill_draft_proposals_transition'),
                                ('trigger', 'skill_draft_proposals_created'),
                                ('trigger', 'skill_draft_proposals_changed'),
                            )
                        )
                        if all(markers):
                            migration_script = ''
                        elif any(markers):
                            raise RuntimeError('database has a partial skill proposal migration; restore a verified backup')
                    if number == 21:
                        columns = {row[1] for row in connection.execute(
                            'PRAGMA table_info(skill_draft_proposals)')}
                        index = connection.execute(
                            "SELECT 1 FROM sqlite_master WHERE type='index' "
                            "AND name='skill_draft_proposals_detection_key_idx'"
                        ).fetchone()
                        trigger = connection.execute(
                            "SELECT sql FROM sqlite_master WHERE type='trigger' "
                            "AND name='skill_draft_proposals_transition'"
                        ).fetchone()
                        if 'detection_key' in columns and index and trigger and 'detection_key' in trigger[0]:
                            migration_script = ''
                        elif 'detection_key' in columns or index:
                            raise RuntimeError('database has a partial repeated workflow proposal migration; restore a verified backup')
                    if number == 22:
                        columns = {row[1] for row in connection.execute(
                            'PRAGMA table_info(skill_draft_proposals)')}
                        transition = connection.execute(
                            "SELECT sql FROM sqlite_master WHERE type='trigger' "
                            "AND name='skill_draft_proposals_transition'"
                        ).fetchone()
                        changed = connection.execute(
                            "SELECT sql FROM sqlite_master WHERE type='trigger' "
                            "AND name='skill_draft_proposals_changed'"
                        ).fetchone()
                        if ('redacted' in columns and transition and 'OLD.redacted' in transition[0]
                                and changed and 'OLD.status!=NEW.status' in changed[0]):
                            migration_script = ''
                        elif 'redacted' in columns:
                            raise RuntimeError('database has a partial proposal retention migration; restore a verified backup')
                    connection.executescript('BEGIN IMMEDIATE;\n' + migration_script)
                    connection.execute('INSERT OR REPLACE INTO schema_migrations VALUES (?,?)',
                                       (number, datetime.now(UTC).isoformat()))
                    connection.execute(f'PRAGMA user_version = {number}')
            with store._connect() as connection:
                connection.execute('PRAGMA journal_mode=WAL')
    finally:
        lock.release()
