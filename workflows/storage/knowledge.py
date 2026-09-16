"""Bounded, incremental keyword retrieval for workspace project files."""
import hashlib
import os
import re
import stat
from pathlib import Path

from .redaction import redact_text

EXCLUDED_DIRS = {'.git', '.hg', '.svn', '.ssh', '.aws', '.codex', '.agents', 'node_modules',
                 'venv', '.venv', '__pycache__', 'data', 'dist', 'build', '.pytest_cache'}
TEXT_SUFFIXES = {'.py', '.md', '.txt', '.toml', '.json', '.yaml', '.yml', '.js', '.jsx', '.ts',
                 '.tsx', '.html', '.css', '.rs', '.go', '.java', '.c', '.h', '.cpp', '.sql', '.sh'}
MAX_FILE_BYTES = 200000
MAX_INDEX_BYTES = 20_000_000
MAX_INDEX_FILES = 5000
MAX_CHUNKS = 20000


def workspace_key(path) -> str:
    result = Path(path).expanduser().resolve()
    if not result.is_dir():
        raise ValueError('workspace must be an existing directory')
    return str(result)


def safe_document(workspace: str, relative: str) -> bytes:
    """Open every path component without following symlinks (including races)."""
    parts = Path(relative).parts
    if not parts or Path(relative).is_absolute() or any(part in ('.', '..') for part in parts):
        raise PermissionError('invalid document path')
    fd = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        doc_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(doc_fd, 'rb') as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_FILE_BYTES:
                raise ValueError('document is not a bounded regular file')
            data = stream.read(MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES or b'\x00' in data:
                raise ValueError('document is oversized or binary')
            return data
    finally:
        os.close(fd)


class KnowledgeStore:
    def index_workspace(self, workspace) -> dict:
        key = workspace_key(workspace)
        seen = set()
        total_bytes = updated = skipped = 0
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for directory, dirs, files in os.walk(key, followlinks=False):
                dirs[:] = sorted(name for name in dirs if name not in EXCLUDED_DIRS and not name.startswith('.')
                                 and not Path(directory, name).is_symlink())
                for name in sorted(files):
                    path = Path(directory, name)
                    if (name.startswith('.') or path.suffix.lower() not in TEXT_SUFFIXES or
                            any(word in name.casefold() for word in ('secret', 'credential', 'private_key'))):
                        continue
                    relative = path.relative_to(key).as_posix()
                    try:
                        data = safe_document(key, relative)
                        content = data.decode('utf-8')
                    except (OSError, ValueError, UnicodeError):
                        skipped += 1
                        continue
                    seen.add(relative)
                    total_bytes += len(data)
                    if len(seen) > MAX_INDEX_FILES or total_bytes > MAX_INDEX_BYTES:
                        raise ValueError('project index quota exceeded; previous index preserved')
                    digest = hashlib.sha256(data).hexdigest()
                    previous = db.execute('SELECT sha256 FROM knowledge_files WHERE workspace=? AND path=?', (key, relative)).fetchone()
                    if previous and previous[0] == digest:
                        continue
                    db.execute('DELETE FROM knowledge_chunks WHERE workspace=? AND path=?', (key, relative))
                    lines = content.splitlines()
                    # Bound both line length and chunk length without losing line citations.
                    for offset in range(0, len(lines), 40):
                        chunk = '\n'.join(lines[offset:offset + 40])
                        for start in range(0, len(chunk), 4000):
                            db.execute('INSERT INTO knowledge_chunks VALUES(?,?,?,?,?,?)',
                                (key, relative, offset + 1, min(offset + 40, len(lines)), digest,
                                 redact_text(chunk[start:start + 4000])))
                    db.execute('INSERT INTO knowledge_files VALUES(?,?,?) ON CONFLICT(workspace,path) '
                               'DO UPDATE SET sha256=excluded.sha256', (key, relative, digest))
                    updated += 1
            previous_paths = [row[0] for row in db.execute('SELECT path FROM knowledge_files WHERE workspace=?', (key,))]
            removed = set(previous_paths) - seen
            for relative in removed:
                db.execute('DELETE FROM knowledge_chunks WHERE workspace=? AND path=?', (key, relative))
                db.execute('DELETE FROM knowledge_files WHERE workspace=? AND path=?', (key, relative))
            count = db.execute('SELECT COUNT(*) FROM knowledge_chunks WHERE workspace=?', (key,)).fetchone()[0]
            if count > MAX_CHUNKS:
                raise ValueError('project chunk quota exceeded; previous index preserved')
            self._log(db, None, 'knowledge.indexed', f'{key}: {len(seen)} files, {count} chunks')
        return {'files': len(seen), 'updated': updated, 'removed': len(removed), 'skipped': skipped, 'chunks': count}

    def search_knowledge(self, workspace, query: str, limit: int = 5) -> list[dict]:
        key = workspace_key(workspace)
        if not isinstance(query, str) or len(query) > 1000:
            raise ValueError('query must be a string of at most 1000 characters')
        tokens = re.findall(r'\w+', query, re.UNICODE)[:20]
        if not tokens:
            return []
        expression = ' OR '.join('"' + token + '"' for token in tokens)
        with self._connect() as db:
            rows = db.execute('SELECT path,line_start,line_end,sha256,content FROM knowledge_chunks '
                              'WHERE knowledge_chunks MATCH ? AND workspace=? ORDER BY rank LIMIT 50',
                              (expression, key)).fetchall()
        results, hashes = [], {}
        for row in rows:
            if row['path'] not in hashes:
                try:
                    hashes[row['path']] = hashlib.sha256(safe_document(key, row['path'])).hexdigest()
                except (OSError, ValueError):
                    hashes[row['path']] = None
            if hashes[row['path']] != row['sha256']:
                continue  # Never serve stale, deleted or newly symlinked files.
            results.append(dict(row))
            if len(results) >= min(max(int(limit), 1), 10):
                break
        return results

    def clear_knowledge(self, workspace) -> None:
        key = workspace_key(workspace)
        with self._connect() as db:
            db.execute('DELETE FROM knowledge_chunks WHERE workspace=?', (key,))
            db.execute('DELETE FROM knowledge_files WHERE workspace=?', (key,))
            self._log(db, None, 'knowledge.cleared', key)
