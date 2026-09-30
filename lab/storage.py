"""Small SQLite journal plus local encrypted credentials."""
from __future__ import annotations
import json
import os
import sqlite3
import time
from pathlib import Path
from cryptography.fernet import Fernet
from .okx import OkxCredentials

class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        self.path = self.directory / 'lab.sqlite3'
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, ts REAL, kind TEXT, message TEXT)')
        os.chmod(self.path, 0o600)

    def connect(self):
        return sqlite3.connect(self.path)

    def get(self, key, default=None):
        with self.connect() as db:
            row = db.execute('SELECT value FROM state WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO state VALUES (?,?)', (key, json.dumps(value, allow_nan=False)))

    def event(self, kind, message):
        with self.connect() as db:
            db.execute('INSERT INTO events(ts,kind,message) VALUES(?,?,?)', (time.time(), kind, message))
            db.execute('DELETE FROM events WHERE id < (SELECT MAX(id)-3000 FROM events)')

    def events(self):
        with self.connect() as db:
            rows = db.execute('SELECT ts,kind,message FROM events ORDER BY id DESC LIMIT 60').fetchall()
        return [dict(zip(('ts','kind','message'), r)) for r in rows]

    def credentials(self, mode="demo"):
        assert mode in {'demo', 'live'}
        path = self.directory / f'{mode}.enc'
        if not path.exists():
            return OkxCredentials(simulated=mode == "demo")
        return OkxCredentials(**json.loads(self._fernet().decrypt(path.read_bytes())))

    def save_credentials(self, creds, mode="demo"):
        assert mode in {"demo", "live"}
        value = {'api_key': creds.api_key, 'api_secret': creds.api_secret, 'passphrase': creds.passphrase, 'simulated': mode == 'demo'}
        self._write_encrypted(mode, value)

    def secret(self, name):
        assert name in {'openrouter', 'typesafe'}
        path = self.directory / f'{name}.enc'
        return json.loads(self._fernet().decrypt(path.read_bytes())) if path.exists() else ''

    def has_secret(self, name):
        return (self.directory / f'{name}.enc').exists()

    def save_secret(self, name, value):
        assert name in {'openrouter', 'typesafe'}
        self._write_encrypted(name, value)

    def _fernet(self):
        key_path = self.directory / 'local.key'
        if not key_path.exists():
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as f:
                f.write(Fernet.generate_key())
        return Fernet(key_path.read_bytes())

    def _write_encrypted(self, name, value):
        encrypted = self._fernet().encrypt(json.dumps(value).encode())
        temp = self.directory / f'{name}.enc.tmp'
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'wb') as f:
            f.write(encrypted)
        temp.replace(self.directory / f'{name}.enc')
