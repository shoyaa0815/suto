"""Durable, one-level delegation with carved-out budgets and explicit joins."""
import json
from dataclasses import asdict
from datetime import UTC, datetime
from uuid import uuid4

from ..runtime.options import job_limits, validate_options
from .redaction import redact_text


class SubtaskStore:
    def children(self, parent_id: str) -> list:
        with self._connect() as db:
            return [self._to_job(row) for row in db.execute(
                'SELECT * FROM jobs WHERE parent_id=? ORDER BY created_at,id', (parent_id,))]

    def reserved_budget(self, parent_id: str) -> dict:
        reserved = {'max_tokens': 0, 'max_tool_calls': 0, 'max_elapsed_seconds': 0, 'max_changed_files': 0}
        for child in self.children(parent_id):
            limits = asdict(job_limits(child))
            for key in reserved:
                reserved[key] += limits[key]
        return reserved

    def create_subtask(self, parent_id: str, prompt: str, *, key: str,
                       max_tokens: int = 10000, max_tool_calls: int = 8,
                       max_elapsed_seconds: int = 120, max_changed_files: int = 1,
                       allow_write: bool = False, allow_command: bool = False):
        if not isinstance(key, str) or not key.strip() or len(key) > 100:
            raise ValueError('subtask requires a stable key (1-100 characters)')
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 16000:
            raise ValueError('subtask prompt must contain 1-16000 characters')
        if type(allow_write) is not bool or type(allow_command) is not bool:
            raise ValueError('subtask permissions must be booleans')
        limits = validate_options(dict(max_tokens=max_tokens, max_tool_calls=max_tool_calls,
            max_elapsed_seconds=max_elapsed_seconds, max_changed_files=max_changed_files))
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            parent = self._to_job(db.execute('SELECT * FROM jobs WHERE id=?', (parent_id,)).fetchone())
            if not parent or parent.status != 'running' or parent.parent_id or not parent.options.get('subtasks'):
                raise PermissionError('subtasks are unavailable for this job')
            if (allow_write and not parent.allow_write) or (allow_command and not parent.allow_command):
                raise PermissionError('child permissions cannot exceed the parent')
            existing = db.execute('SELECT * FROM jobs WHERE parent_id=? AND source_ref=?', (parent_id, key)).fetchone()
            options = {**limits, 'sandbox': parent.options.get('sandbox', 'process'),
                       'memory': parent.options.get('memory', False),
                       'retrieval': parent.options.get('retrieval', False)}
            if existing:
                child = self._to_job(existing)
                if (child.prompt, child.allow_write, child.allow_command, child.options) != (
                        redact_text(prompt), allow_write, allow_command, options):
                    raise ValueError('subtask key already names a different action')
                return child
            children = [self._to_job(row) for row in db.execute('SELECT * FROM jobs WHERE parent_id=?', (parent_id,))]
            if len(children) >= 8:
                raise ValueError('at most 8 subtasks per job')
            parent_limits = asdict(job_limits(parent))
            stats = db.execute('SELECT * FROM job_stats WHERE job_id=?', (parent_id,)).fetchone()
            used = {'max_tokens': parent.total_tokens,
                    'max_tool_calls': stats['tool_calls'] + 1,
                    'max_elapsed_seconds': stats['run_seconds'] + max(0, (
                        datetime.now(UTC) - datetime.fromisoformat(stats['running_at'])).total_seconds()),
                    'max_changed_files': db.execute('SELECT COUNT(DISTINCT path) FROM change_events WHERE job_id=?', (parent_id,)).fetchone()[0]}
            for name, amount in limits.items():
                reserved = sum(asdict(job_limits(child))[name] for child in children)
                if amount + reserved + used[name] >= parent_limits[name]:
                    raise ValueError(f'insufficient parent budget: {name}')
            job_id = f'job_{uuid4().hex[:8]}'
            db.execute('INSERT INTO jobs(id,prompt,mode,status,source,source_ref,workspace,allow_write,allow_command,'
                       'created_at,parent_id,options) VALUES(?,?,?,\'queued\',\'subtask\',?,?,?,?,?,?,?)',
                       (job_id, redact_text(prompt), parent.mode, key, parent.workspace, int(allow_write),
                        int(allow_command), datetime.now(UTC).isoformat(), parent_id, json.dumps(options, sort_keys=True)))
            self._log(db, parent_id, 'subtask.created', job_id)
        return self.get_job(job_id)

    def wait_for_children(self, parent_id: str) -> bool:
        with self._connect() as db:
            cursor = db.execute("UPDATE jobs SET status='waiting_children' WHERE id=? AND status='running' "
                "AND EXISTS(SELECT 1 FROM jobs WHERE parent_id=? AND status!='completed')", (parent_id, parent_id))
            return cursor.rowcount == 1

    def reconcile_children(self) -> None:
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            parents = db.execute("SELECT id FROM jobs WHERE status='waiting_children'").fetchall()
            for parent in parents:
                children = db.execute('SELECT id,status FROM jobs WHERE parent_id=?', (parent['id'],)).fetchall()
                failed = [row['id'] for row in children if row['status'] in ('failed','blocked','cancelled','interrupted')]
                if failed:
                    db.execute("UPDATE jobs SET status='blocked',error=?,finished_at=? WHERE id=?",
                        ('subtasks need attention: ' + ', '.join(failed), datetime.now(UTC).isoformat(), parent['id']))
                elif children and all(row['status'] == 'completed' for row in children):
                    db.execute("UPDATE jobs SET status='queued',error=NULL,finished_at=NULL WHERE id=?", (parent['id'],))
            orphaned = [row[0] for row in db.execute("SELECT child.id FROM jobs AS child JOIN jobs AS parent "
                "ON child.parent_id=parent.id WHERE parent.status IN ('failed','blocked','cancelled') "
                "AND child.status IN ('queued','running','waiting_approval','interrupted')")]
        for child_id in orphaned:
            self.cancel_job(child_id)

    def children_summary(self, parent_id: str) -> str:
        return '\n'.join(f'{job.id} [{job.status}] key={job.source_ref}: {(job.result or job.error or "pending")[:4000]}'
                         for job in self.children(parent_id))[:16000]
