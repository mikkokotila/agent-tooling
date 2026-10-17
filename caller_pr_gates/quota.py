"""Cross-process quota admission, durable deadlines and fenced recovery leases.

This database contains request coordination only. It never restores or clears PR
ownership, policy evidence, validation receipts, or delivery leases.
"""
from __future__ import annotations
from contextlib import contextmanager
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
import hashlib
import ctypes
import json
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import time
import uuid
from .errors import diagnostic

def boot_identity():
    path=Path('/proc/sys/kernel/random/boot_id')
    if path.exists(): return path.read_text().strip()
    class Timeval(ctypes.Structure):
        _fields_=[('seconds',ctypes.c_long),('microseconds',ctypes.c_int)]
    value=Timeval();size=ctypes.c_size_t(ctypes.sizeof(value))
    result=ctypes.CDLL(None,use_errno=True).sysctlbyname(b'kern.boottime',ctypes.byref(value),ctypes.byref(size),None,0)
    if result:raise OSError(ctypes.get_errno(),'Boot identity unavailable')
    return str(value.seconds)

class CoolingDown(RuntimeError):
    def __init__(self, account, resource, deadline, status=403):
        self.account=account;self.resource=resource;self.deadline=deadline
        super().__init__(f'GitHub {status}: API rate limit exceeded; shared {resource} cooldown is active')

@dataclass(frozen=True)
class Permit:
    account: str
    resource: str
    epoch: int
    token: str | None = None
    scope: str | None = None
    generations: tuple = ()

class Coordinator:
    def __init__(self, directory=None, *, wall=None, monotonic=None, boot=None, lease_seconds=30):
        directory=Path(directory or os.environ.get('CALLER_PR_GATES_STATE_DIR') or Path.home()/'.local/state/caller-pr-gates')
        directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.path=directory/'github-coordination.sqlite3'
        self.wall=wall or time.time;self.mono=monotonic or time.monotonic
        self.boot=boot or boot_identity();self.lease_seconds=lease_seconds
        with self.db() as db:
            if db.execute('PRAGMA user_version').fetchone()[0] >= 1:
                self.path.chmod(0o600)
                return
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''CREATE TABLE IF NOT EXISTS cooldowns (
              account TEXT,resource TEXT,epoch INTEGER NOT NULL DEFAULT 0,
              deadline REAL NOT NULL,mono_deadline REAL NOT NULL,boot TEXT,
              duration REAL NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,status INTEGER,
              lease_token TEXT,lease_wall REAL NOT NULL DEFAULT 0,lease_mono REAL NOT NULL DEFAULT 0,
              PRIMARY KEY(account,resource));
              CREATE TABLE IF NOT EXISTS attribution (
              id INTEGER PRIMARY KEY,at REAL,fields TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS representations (
              account TEXT,key TEXT,etag TEXT,payload TEXT,observed REAL,
              PRIMARY KEY(account,key));''')
            db.execute('PRAGMA user_version=1')
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        db=sqlite3.connect(self.path,timeout=10);db.row_factory=sqlite3.Row
        db.execute('PRAGMA busy_timeout=10000');db.execute('PRAGMA synchronous=FULL')
        try:
            with db:yield db
        finally:db.close()

    def remaining(self,row):
        if row['boot']==self.boot:
            return max(row['deadline']-self.wall(),row['mono_deadline']-self.mono(),0)
        # Reboot/clock uncertainty: preserve at least the recorded duration once.
        return max(row['deadline']-self.wall(),row['duration'],0)

    def check(self,account,resources):
        """Avoid launching a collection whose required resource is cooling down."""
        with self.db() as db:
            rows=list(db.execute('SELECT * FROM cooldowns WHERE account=?',(account,)))
            for row in rows:
                if row['resource'] not in set(resources)|{'*'}:continue
                if row['boot']!=self.boot or self.remaining(row)>0 or row['lease_token'] and (row['lease_wall']>self.wall() or row['lease_mono']>self.mono()):
                    raise CoolingDown(account,row['resource'],max(row['deadline'],row['lease_wall']),row['status'])

    def admit(self,account,resource):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            rows=list(db.execute('SELECT * FROM cooldowns WHERE account=? AND resource IN (?,?)',(account,resource,'*')))
            for row in rows:
                if row['boot']!=self.boot:
                    duration=self.remaining(row)
                    db.execute('UPDATE cooldowns SET boot=?,deadline=?,mono_deadline=? WHERE account=? AND resource=?',
                               (self.boot,self.wall()+duration,self.mono()+duration,account,row['resource']))
                    db.commit()
                    raise CoolingDown(account,row['resource'],self.wall()+duration,row['status'])
                if self.remaining(row)>0 or (row['lease_token'] and (row['lease_wall']>self.wall() or row['lease_mono']>self.mono())):
                    raise CoolingDown(account,row['resource'],max(row['deadline'],row['lease_wall']),row['status'])
            # Expired throttles still require one serialized recovery request.
            pending=[r for r in rows if r['duration']>0]
            generations=tuple(sorted((r['resource'],r['epoch']) for r in rows))
            if pending:
                row=next((r for r in pending if r['resource']=='*'),pending[0]);token=uuid.uuid4().hex
                db.execute('UPDATE cooldowns SET lease_token=?,lease_wall=?,lease_mono=? WHERE account=? AND resource=?',
                    (token,self.wall()+self.lease_seconds,self.mono()+self.lease_seconds,account,row['resource']))
                return Permit(account,resource,row['epoch'],token,row['resource'],generations)
            return Permit(account,resource,max((r['epoch'] for r in rows),default=0),generations=generations)

    def throttle(self,permit,error):
        data=diagnostic(error)
        if data['kind']!='rate_limited':return False
        headers=data['rate_limit_headers'];scope='*' if data['quota_kind']=='secondary' else headers.get('x-ratelimit-resource') or permit.resource
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT * FROM cooldowns WHERE account=? AND resource=?',(permit.account,scope)).fetchone()
            # A late probe cannot alter newer coordination, even its lease.
            if permit.token:
                leased=db.execute('SELECT * FROM cooldowns WHERE account=? AND resource=?',(permit.account,permit.scope)).fetchone()
                if (not leased or leased['epoch']!=permit.epoch or leased['lease_token']!=permit.token
                    or leased['lease_wall']<=self.wall() and leased['lease_mono']<=self.mono()):return False
            attempts=(old['attempts'] if old else 0)+1;duration=min(60*2**min(attempts-1,6),3600)
            deadlines=[]
            try:
                reset=float(headers.get('x-ratelimit-reset',''))
                if math.isfinite(reset):deadlines.append(max(0,reset-self.wall()))
            except (ValueError,TypeError):pass
            try:
                retry=headers.get('retry-after','');delay=float(retry)
                if math.isfinite(delay):deadlines.append(max(0,delay))
            except (ValueError,TypeError):
                try:deadlines.append(max(0,parsedate_to_datetime(retry).timestamp()-self.wall()))
                except (ValueError,TypeError,OverflowError):pass
            if deadlines:duration=max(deadlines)+1
            if old:duration=max(duration,self.remaining(old))
            epoch=(old['epoch'] if old else 0)+1
            db.execute('''INSERT INTO cooldowns VALUES(?,?,?,?,?,?,?,?,?,NULL,0,0)
                ON CONFLICT(account,resource) DO UPDATE SET epoch=excluded.epoch,deadline=excluded.deadline,
                mono_deadline=excluded.mono_deadline,boot=excluded.boot,duration=excluded.duration,
                attempts=excluded.attempts,status=excluded.status,lease_token=NULL,lease_wall=0,lease_mono=0''',
                (permit.account,scope,epoch,self.wall()+duration,self.mono()+duration,self.boot,duration,attempts,data['http_status'] or 403))
            return True

    def finish(self,permit,*,success):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            generations=tuple(sorted((r['resource'],r['epoch']) for r in db.execute('SELECT resource,epoch FROM cooldowns WHERE account=? AND resource IN (?,?)',(permit.account,permit.resource,'*'))))
            if generations!=permit.generations:return False
            if not permit.token:return True
            row=db.execute('SELECT * FROM cooldowns WHERE account=? AND resource=?',(permit.account,permit.scope)).fetchone()
            if not row or row['epoch']!=permit.epoch or row['lease_token']!=permit.token:return False
            if row['lease_wall']<=self.wall() and row['lease_mono']<=self.mono():return False
            duration=0 if success else min(60*2**min(row['attempts'],6),3600)
            db.execute('UPDATE cooldowns SET deadline=?,mono_deadline=?,duration=?,lease_token=NULL,lease_wall=0,lease_mono=0 WHERE account=? AND resource=?',
                (self.wall()+duration,self.mono()+duration,duration,permit.account,permit.scope))
            return True

    def audit(self,**fields):
        # Whitelist caller-supplied metadata; no query bodies, headers or tokens.
        allowed={'operation','session','process','target','account','resource','reason','outcome','requests','status','collection','profile','boundary','http_requests'}
        value={k:v for k,v in fields.items() if k in allowed}
        with self.db() as db:
            db.execute('INSERT INTO attribution(at,fields) VALUES(?,?)',(self.wall(),json.dumps(value,sort_keys=True)))
            db.execute('DELETE FROM attribution WHERE id <= (SELECT MAX(id)-20000 FROM attribution)')

def account_key(host,token):
    return hashlib.sha256((host.lower()+'\0'+token).encode()).hexdigest()

def resource_for(path):
    if path.rstrip('/')=='/graphql':return 'graphql'
    if path.startswith('/search/'):return 'search'
    return 'core'
