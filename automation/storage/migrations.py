"""Versioned, transactional migrations with a verified pre-upgrade snapshot."""
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .locking import ProcessLock

SCHEMA_VERSION = 2

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
CREATE TABLE workspace_features(workspace TEXT PRIMARY KEY, memory_enabled INTEGER NOT NULL DEFAULT 0);
CREATE TABLE memories(id TEXT PRIMARY KEY, workspace TEXT NOT NULL, content TEXT NOT NULL,
 embedding TEXT NOT NULL, model TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX memories_workspace_idx ON memories(workspace);
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
            for number, script in ((1, HARDENING), (2, ADVANCED)):
                if number <= version:
                    continue
                with store._connect() as connection:
                    connection.executescript('BEGIN IMMEDIATE;\n' + script)
                    connection.execute('INSERT INTO schema_migrations VALUES (?,?)',
                                       (number, datetime.now(UTC).isoformat()))
                    connection.execute(f'PRAGMA user_version = {number}')
            with store._connect() as connection:
                connection.execute('PRAGMA journal_mode=WAL')
    finally:
        lock.release()
