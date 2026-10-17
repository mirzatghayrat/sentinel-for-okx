"""Agent proposals: the only thing an agent can write, kept apart from the desk's own journal.

An agent (through the MCP server) may file a trial proposal or ask for a pause. Nothing here trades.
The desk reads the queue: pause requests are carried out because pausing only reduces risk; trial
proposals wait for the person to approve them in the web console, or, if the person switched it on,
demo proposals start automatically. Live proposals always need the person and the live confirmation.
Every proposal is re-validated by the desk before anything runs.
"""
from __future__ import annotations
import json
import sqlite3
import time
import uuid
from pathlib import Path

KINDS = {'trial', 'pause'}
TTL = 24 * 3600          # an undecided proposal expires after a day
MAX_PENDING = 5
MAX_PER_DAY = 20


def clean_text(text, limit=500):
    """Agent text is shown to the person as data: printable, single-spaced, bounded."""
    text = ' '.join(str(text or '').split())
    return ''.join(ch for ch in text if ch.isprintable())[:limit]


class Proposals:
    def __init__(self, directory, *, create=True):
        self.path = Path(directory) / 'proposals.sqlite3'
        if create:
            if not self.path.parent.exists():
                raise ValueError('Sentinel 数据目录不存在；请先启动一次 Sentinel')
            with self._db() as db:
                db.execute('CREATE TABLE IF NOT EXISTS proposals (id TEXT PRIMARY KEY, created REAL, kind TEXT, mode TEXT,'
                           ' status TEXT, payload TEXT, note TEXT, decided REAL, result TEXT)')
            self.path.chmod(0o600)

    def _db(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.execute('PRAGMA journal_mode=WAL')
        return db

    def add(self, kind, mode, payload, note):
        assert kind in KINDS and mode in {'demo', 'live'}
        now = time.time()
        with self._db() as db:
            pending = db.execute("SELECT COUNT(*) FROM proposals WHERE status='pending'").fetchone()[0]
            today = db.execute('SELECT COUNT(*) FROM proposals WHERE created > ?', (now - 86400,)).fetchone()[0]
            if pending >= MAX_PENDING:
                raise ValueError(f'已有 {pending} 个待处理提案；请等用户处理后再提交')
            if today >= MAX_PER_DAY:
                raise ValueError('24 小时内提案次数已达上限')
            pid = uuid.uuid4().hex[:12]
            db.execute('INSERT INTO proposals VALUES (?,?,?,?,?,?,?,?,?)',
                       (pid, now, kind, mode, 'pending', json.dumps(payload, allow_nan=False), clean_text(note), None, None))
        return self.get(pid)

    def list(self, limit=30, status=None):
        if not self.path.exists():
            return []
        sql, args = 'SELECT * FROM proposals', ()
        if status:
            sql, args = sql + ' WHERE status=?', (status,)
        with self._db() as db:
            rows = db.execute(sql + ' ORDER BY created DESC LIMIT ?', (*args, limit)).fetchall()
        return [self._row(r) for r in rows]

    def get(self, pid):
        if not self.path.exists():
            return None
        with self._db() as db:
            row = db.execute('SELECT * FROM proposals WHERE id=?', (pid,)).fetchone()
        return self._row(row) if row else None

    def decide(self, pid, status, result=''):
        """Move a pending proposal to its final status; returns False if it was no longer pending."""
        with self._db() as db:
            cur = db.execute("UPDATE proposals SET status=?, decided=?, result=? WHERE id=? AND status='pending'",
                             (status, time.time(), clean_text(result, 300), pid))
            return cur.rowcount == 1

    def expire(self):
        with self._db() as db:
            db.execute("UPDATE proposals SET status='expired', decided=? WHERE status='pending' AND created < ?",
                       (time.time(), time.time() - TTL))

    @staticmethod
    def _row(r):
        keys = ('id', 'created', 'kind', 'mode', 'status', 'payload', 'note', 'decided', 'result')
        row = dict(zip(keys, r))
        row['payload'] = json.loads(row['payload'])
        return row
