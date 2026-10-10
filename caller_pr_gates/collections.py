"""Explicit-profile collection sharing; no TTL can promote old policy evidence."""
from __future__ import annotations
import asyncio, contextvars, hashlib, json, os, time, uuid, inspect
from .quota import Coordinator
from pathlib import Path

context = contextvars.ContextVar('github_collection', default={})

class Collections:
    def __init__(self, coordinator, account):
        self.coordinator=coordinator;self.account=account
        with coordinator.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS collections (
                account TEXT,key TEXT,token TEXT,phase TEXT,boundary REAL,started REAL,
                lease REAL,boot TEXT,payload TEXT,error TEXT, PRIMARY KEY(account,key));
                CREATE TABLE IF NOT EXISTS immutable_evidence (
                account TEXT,key TEXT,payload TEXT,observed REAL,PRIMARY KEY(account,key));''')

    async def run(self, profile, target, producer, *, boundary=None, operation=None):
        boundary=time.time() if boundary is None else boundary
        key=hashlib.sha256(json.dumps([profile,target],sort_keys=True).encode()).hexdigest()
        joined=None
        caller=next((f'{Path(f.filename).stem}:{f.function}' for f in inspect.stack(context=0) if f.filename.endswith(('/hook.py','/cli.py','/monitor.py','/supervisor.py'))),profile)
        metadata=dict(operation=operation or os.environ.get('CALLER_GATE_OPERATION') or caller,session=os.environ.get('CALLER_GATE_SESSION') or os.environ.get('CODEX_THREAD_ID') or os.environ.get('CLAUDE_SESSION_ID'),
                      process=os.getpid(),target=target,account=self.account,resource='collection',reason=profile)
        while True:
            with self.coordinator.db() as db:
                db.execute('BEGIN IMMEDIATE')
                row=db.execute('SELECT * FROM collections WHERE account=? AND key=?',(self.account,key)).fetchone()
                active=bool(row and row['phase'] in {'pending','reading'} and row['boot']==self.coordinator.boot and row['lease']>time.monotonic())
                if row and row['phase']=='result' and (row['token']==joined or row['started']>=boundary):
                    if row['error']:
                        raise RuntimeError(row['error'])
                    db.commit()
                    self.coordinator.audit(**metadata,outcome='reused',requests=0)
                    return json.loads(row['payload'])
                if active:
                    if row['phase']=='pending':
                        joined=row['token']
                        db.execute('UPDATE collections SET boundary=MAX(boundary,?) WHERE account=? AND key=?',(boundary,self.account,key))
                    leader=False
                else:
                    joined=uuid.uuid4().hex;leader=True
                    db.execute('INSERT OR REPLACE INTO collections VALUES(?,?,?,?,?,NULL,?,?,NULL,NULL)',
                        (self.account,key,joined,'pending',boundary,time.monotonic()+75,self.coordinator.boot))
            if not leader:
                await asyncio.sleep(.01);continue
            try:
                # Only callers admitted before the first read can share a strict
                # fresh collection. This batching window is NOT an evidence TTL.
                await asyncio.sleep(.03)
                with self.coordinator.db() as db:
                    db.execute('BEGIN IMMEDIATE')
                    row=db.execute('SELECT * FROM collections WHERE account=? AND key=?',(self.account,key)).fetchone()
                    if row['token']!=joined:continue
                    started=time.time()
                    if started<row['boundary']:raise RuntimeError('Collection clock moved behind freshness boundary')
                    db.execute("UPDATE collections SET phase='reading',started=? WHERE account=? AND key=? AND token=?",(started,self.account,key,joined))
                metadata.update(collection=joined,profile=profile,boundary=started)
                marker=context.set(metadata)
                try:payload=await producer()
                finally:context.reset(marker)
                encoded=json.dumps(payload,sort_keys=True)
                with self.coordinator.db() as db:
                    cursor=db.execute("UPDATE collections SET phase='result',payload=?,error=NULL WHERE account=? AND key=? AND token=? AND lease>? AND boot=?",
                        (encoded,self.account,key,joined,time.monotonic(),self.coordinator.boot))
                    if cursor.rowcount!=1:raise RuntimeError('Collection lease expired; discard late evidence')
                    db.execute('DELETE FROM collections WHERE phase=\'result\' AND rowid IN (SELECT rowid FROM collections ORDER BY started DESC LIMIT -1 OFFSET 1000)')
                with self.coordinator.db() as db:
                    count=db.execute("SELECT COALESCE(SUM(json_extract(fields,'$.requests')),0) FROM attribution WHERE json_extract(fields,'$.collection')=?",(joined,)).fetchone()[0]
                self.coordinator.audit(**metadata,outcome='fresh',requests=0,http_requests=count)
                return payload
            except BaseException as exc:
                from .errors import redact
                with self.coordinator.db() as db:
                    db.execute("UPDATE collections SET phase='result',payload=NULL,error=? WHERE account=? AND key=? AND token=?",
                        (redact(str(exc)),self.account,key,joined))
                self.coordinator.audit(**metadata,outcome='failed',requests=0)
                raise

    def immutable(self,key):
        with self.coordinator.db() as db:
            row=db.execute('SELECT payload FROM immutable_evidence WHERE account=? AND key=?',(self.account,key)).fetchone()
            return json.loads(row['payload']) if row else None

    def remember(self,key,payload):
        with self.coordinator.db() as db:
            db.execute('INSERT OR REPLACE INTO immutable_evidence VALUES(?,?,?,?)',(self.account,key,json.dumps(payload),time.time()))
            db.execute('DELETE FROM immutable_evidence WHERE rowid IN (SELECT rowid FROM immutable_evidence ORDER BY observed DESC LIMIT -1 OFFSET 1000)')
