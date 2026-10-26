"""Persistent cache, request pacing and local rolling request budget."""
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time
from email.utils import parsedate_to_datetime


class BudgetError(Exception):
    pass


def retry_after(value, now=None):
    """Return Retry-After seconds for either HTTP format."""
    now = time.time() if now is None else now
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        try:
            return max(0, parsedate_to_datetime(value).timestamp() - now)
        except (TypeError, ValueError, OverflowError):
            return 0


class Store:
    def __init__(self, path=None, clock=time.time, sleep=time.sleep):
        self.clock, self.sleep = clock, sleep
        self.lock = threading.RLock()
        if path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path) if path else ':memory:', timeout=30, check_same_thread=False)
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, saved REAL, value TEXT);
            CREATE TABLE IF NOT EXISTS gates (scope TEXT PRIMARY KEY, next REAL, interval REAL);
            CREATE TABLE IF NOT EXISTS calls (scope TEXT, stamp REAL);
            CREATE INDEX IF NOT EXISTS calls_scope_stamp ON calls(scope, stamp);
            CREATE INDEX IF NOT EXISTS calls_stamp ON calls(stamp);
        ''')
        self.db.commit()
        self.hits = 0
        self.requests = 0

    @staticmethod
    def fingerprint(key):
        return hashlib.sha256(key.encode()).hexdigest()

    def read(self, key, ttl):
        if ttl <= 0:
            return None
        with self.lock:
            row = self.db.execute('SELECT saved,value FROM cache WHERE key=?', (key,)).fetchone()
            if not row or not 0 <= self.clock() - row[0] < ttl:
                return None
            try:
                value = json.loads(row[1])
            except ValueError:
                return None
            self.hits += 1
            return value

    def write(self, key, value):
        with self.lock, self.db:
            self.db.execute('INSERT OR REPLACE INTO cache VALUES (?,?,?)',
                            (key, self.clock(), json.dumps(value, ensure_ascii=False)))

    def acquire(self, scope, interval, budget=90000):
        """Serialize request starts across workers and processes using this DB."""
        while True:
            with self.lock:
                try:
                    self.db.execute('BEGIN IMMEDIATE')
                    now = self.clock()
                    self.db.execute('DELETE FROM calls WHERE stamp <= ?', (now - 86400,))
                    count = self.db.execute('SELECT COUNT(*) FROM calls WHERE scope=?', (scope,)).fetchone()[0]
                    if count >= budget:
                        raise BudgetError('Local rolling 24-hour request budget reached; try later')
                    row = self.db.execute('SELECT next,interval FROM gates WHERE scope=?', (scope,)).fetchone()
                    delay = max(0, row[0] - now) if row else 0
                    if delay > 120:
                        raise BudgetError('Steam request cooldown active; retry later')
                    if not delay:
                        spacing = max(interval, row[1]) if row else interval
                        self.db.execute('INSERT OR REPLACE INTO gates VALUES (?,?,?)', (scope, now + spacing, spacing))
                        self.db.execute('INSERT INTO calls VALUES (?,?)', (scope, now))
                        self.requests += 1
                    self.db.commit()
                except BaseException:
                    self.db.rollback()
                    raise
            if not delay:
                return
            # Recheck after sleeping so a 429 extends already-waiting workers.
            self.sleep(min(delay, 1))

    def cooldown(self, scope, seconds, interval):
        with self.lock:
            try:
                self.db.execute('BEGIN IMMEDIATE')
                row = self.db.execute('SELECT next,interval FROM gates WHERE scope=?', (scope,)).fetchone()
                spacing = min(4, max(interval, row[1] if row else 0) * 2)
                until = max(self.clock() + seconds, row[0] if row else 0)
                self.db.execute('INSERT OR REPLACE INTO gates VALUES (?,?,?)', (scope, until, spacing))
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    def close(self):
        with self.lock:
            self.db.close()
