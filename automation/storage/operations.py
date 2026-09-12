"""Operator-facing health, metrics, retention, and durable local notifications."""
import json
import os
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .migrations import SCHEMA_VERSION, backup_database
from .redaction import redact_text

_RETENTION_QUERIES = {
    table: (
        # The table comes only from the fixed tuple below, never from input.
        f"SELECT COUNT(*) FROM {table} WHERE created_at < ? AND job_id IN "  # nosec B608
        "(SELECT id FROM jobs WHERE status IN ('completed','failed','cancelled') "
        "AND finished_at < ?)",
        f"DELETE FROM {table} WHERE created_at < ? AND job_id IN "  # nosec B608
        "(SELECT id FROM jobs WHERE status IN ('completed','failed','cancelled') "
        "AND finished_at < ?)",
    )
    for table in (
        'job_events',
        'tool_events',
        'command_events',
        'change_events',
        'notifications',
    )
}
_RETENTION_QUERIES['structured_logs'] = (
    "SELECT COUNT(*) FROM structured_logs WHERE created_at < ? AND "
    "(job_id IS NULL OR job_id IN (SELECT id FROM jobs WHERE status IN "
    "('completed','failed','cancelled') AND finished_at < ?))",
    "DELETE FROM structured_logs WHERE created_at < ? AND "
    "(job_id IS NULL OR job_id IN (SELECT id FROM jobs WHERE status IN "
    "('completed','failed','cancelled') AND finished_at < ?))",
)


class OperationsStore:
    def settings(self) -> dict:
        with self._connect() as db:
            return dict(db.execute('SELECT key,value FROM runtime_settings').fetchall())

    def configure(self, **values: int) -> dict:
        bounds = {'concurrency': (1, 16), 'workspace_concurrency': (1, 16),
                  'max_queued_jobs': (1, 100000), 'submissions_per_minute': (1, 100000),
                  'daily_token_quota': (1, 1000000000), 'retention_days': (0, 3650)}
        for key, value in values.items():
            if key not in bounds or type(value) is not int or not bounds[key][0] <= value <= bounds[key][1]:
                raise ValueError(f'invalid runtime setting: {key}')
        with self._connect() as db:
            db.executemany('UPDATE runtime_settings SET value=? WHERE key=?',
                           [(v, k) for k, v in values.items()])
            self._log(db, None, 'settings.updated', json.dumps(values, sort_keys=True))
        return self.settings()

    @staticmethod
    def _log(db, job_id, event_type, detail='') -> None:
        db.execute('INSERT INTO structured_logs(job_id,event_type,detail,created_at) VALUES(?,?,?,?)',
                   (job_id, event_type, redact_text(detail)[:16000], datetime.now(UTC).isoformat()))

    def logs(self, job_id: str | None = None, limit: int = 100) -> list[dict]:
        with self._connect() as db:
            rows = db.execute('SELECT id AS event_id, job_id, event_type, detail, created_at '
                              'FROM structured_logs WHERE (? IS NULL OR job_id=?) ORDER BY id DESC LIMIT ?',
                              (job_id, job_id, min(max(int(limit), 1), 1000))).fetchall()
        return [dict(row) for row in reversed(rows)]

    def metrics(self) -> dict:
        with self._connect() as db:
            states = dict(db.execute('SELECT status,COUNT(*) FROM jobs GROUP BY status').fetchall())
            stats = dict(db.execute('SELECT COALESCE(SUM(queue_seconds),0) AS queue_seconds, '
                'COALESCE(SUM(run_seconds),0) AS run_seconds, COALESCE(SUM(tool_calls),0) AS tool_calls, '
                'COALESCE(SUM(tool_errors),0) AS tool_errors FROM job_stats').fetchone())
            stats['tokens'] = db.execute('SELECT COALESCE(SUM(prompt_tokens+output_tokens),0) FROM jobs').fetchone()[0]
            stats['tokens_today'] = db.execute("SELECT COALESCE(SUM(tokens),0) FROM daily_usage WHERE day=date('now')").fetchone()[0]
        finished = sum(states.get(key, 0) for key in ('completed', 'failed', 'blocked'))
        return {'jobs': states, 'success_rate': states.get('completed', 0) / finished if finished else None,
                'success_rate_denominator': 'completed + failed + blocked', **stats}

    def heartbeat(self, state: str) -> None:
        with self._connect() as db:
            db.execute('INSERT INTO worker_state VALUES(1,?,?,?) ON CONFLICT(id) DO UPDATE '
                       'SET pid=excluded.pid,heartbeat=excluded.heartbeat,state=excluded.state',
                       (os.getpid(), datetime.now(UTC).isoformat(), state))

    def diagnostics(self) -> dict:
        with self._connect() as db:
            integrity = db.execute('PRAGMA quick_check').fetchone()[0]
            version = db.execute('PRAGMA user_version').fetchone()[0]
            worker = db.execute('SELECT * FROM worker_state WHERE id=1').fetchone()
            foreign_keys = len(db.execute('PRAGMA foreign_key_check').fetchall())
        worker = dict(worker) if worker else None
        healthy_worker = bool(worker and worker['state'] == 'running' and
            (datetime.now(UTC) - datetime.fromisoformat(worker['heartbeat'])).total_seconds() < 30)
        database_ok = integrity == 'ok' and version == SCHEMA_VERSION and not foreign_keys
        return {'ok': database_ok, 'ready': database_ok and healthy_worker,
                'integrity': integrity, 'schema_version': version, 'foreign_key_errors': foreign_keys,
                'worker_alive': healthy_worker, 'worker': worker, 'settings': self.settings(),
                'capabilities': {'bwrap_installed': bool(shutil.which('bwrap', path='/usr/bin:/bin')),
                                 'prlimit_installed': bool(shutil.which('prlimit', path='/usr/bin:/bin')),
                                 'embedding_model_configured': bool(os.environ.get('SUTO_EMBED_MODEL', '').strip())},
                'database_bytes': self.path.stat().st_size, 'metrics': self.metrics()}

    def backup(self, destination: str | Path) -> Path:
        result = backup_database(self.path, Path(destination))
        with self._connect() as db:
            self._log(db, None, 'database.backup', str(result))
        return result

    def cleanup(self, days: int = 30, *, dry_run: bool = True) -> dict:
        if type(days) is not int or days < 1:
            raise ValueError('retention must be at least one day')
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        counts = {}
        # Keep resumable jobs, checkpoints, approval history and summary metrics.
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for table, (count_query, delete_query) in _RETENTION_QUERIES.items():
                counts[table] = db.execute(count_query, (cutoff, cutoff)).fetchone()[0]
                if not dry_run:
                    db.execute(delete_query, (cutoff, cutoff))
            if not dry_run:
                self._log(db, None, 'retention.cleanup', json.dumps({'days': days, 'deleted': counts}))
        return {'dry_run': dry_run, 'older_than_days': days, 'rows': counts}

    def notifications(self, *, unread_only: bool = True, limit: int = 100) -> list[dict]:
        with self._connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM notifications WHERE (?=0 OR read_at IS NULL) '
                'ORDER BY id DESC LIMIT ?', (int(unread_only), min(max(int(limit), 1), 500)))]

    def acknowledge_notification(self, event_id: int) -> bool:
        with self._connect() as db:
            cursor = db.execute('UPDATE notifications SET read_at=? WHERE id=? AND read_at IS NULL',
                                (datetime.now(UTC).isoformat(), event_id))
            return cursor.rowcount == 1
