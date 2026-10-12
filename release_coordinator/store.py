"""Short SQLite transactions; one fenced lease per release scope."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid


class Conflict(ValueError):
    pass


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


class Store:
    def __init__(self, root, clock=time.time):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.path = self.root / 'state.sqlite3'
        self.clock = clock
        with self.tx() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, scope TEXT NOT NULL, config TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, project TEXT NOT NULL, state TEXT NOT NULL,
                sha TEXT, validation TEXT, created REAL NOT NULL, updated REAL NOT NULL, ready_at REAL,
                urgent INTEGER NOT NULL DEFAULT 0, separate INTEGER NOT NULL DEFAULT 0, batch TEXT);
            CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY, project TEXT NOT NULL, scope TEXT NOT NULL,
                state TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL, data TEXT NOT NULL, error TEXT);
            CREATE TABLE IF NOT EXISTS leases(scope TEXT PRIMARY KEY, owner TEXT NOT NULL, epoch INTEGER NOT NULL, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS steps(batch TEXT NOT NULL, phase TEXT NOT NULL, state TEXT NOT NULL,
                result TEXT NOT NULL, updated REAL NOT NULL, PRIMARY KEY(batch,phase));
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, batch TEXT, kind TEXT NOT NULL,
                data TEXT NOT NULL, created REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS task_project ON tasks(project,state,batch);
            CREATE INDEX IF NOT EXISTS batch_scope ON batches(scope,state);
            ''')
        self.path.chmod(0o600)

    @contextlib.contextmanager
    def tx(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            if db.in_transaction:
                db.commit()
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _event(db, batch, kind, data, now):
        db.execute('INSERT INTO events(batch,kind,data,created) VALUES(?,?,?,?)', (batch,kind,encode(data),now))

    def project(self, key):
        with self.tx() as db:
            row = db.execute('SELECT config FROM projects WHERE id=?', (key,)).fetchone()
            if not row:
                raise ValueError('Unknown project: ' + key)
            return json.loads(row[0])

    def add_project(self, config):
        config = dict(config)
        import re
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}',str(config.get('id',''))):raise ValueError('Invalid project ID')
        for key in ('id', 'repository', 'remote', 'target', 'environment', 'adapter'):
            if not config.get(key):
                raise ValueError('Missing project field: ' + key)
        repo = Path(config['repository']).expanduser().resolve()
        if not repo.is_dir():
            raise ValueError('Repository directory does not exist')
        config['repository'] = str(repo)
        if not isinstance(config['adapter'], list) or not all(isinstance(x, str) for x in config['adapter']):
            raise ValueError('adapter must be a command argument array')
        phases = config.setdefault('phases', ['build', 'publish', 'verify'])
        if not phases or len(set(phases)) != len(phases) or any(not x.replace('_','').isalnum() for x in phases):
            raise ValueError('Invalid phase names')
        identity = [config['remote'].rstrip('/').removesuffix('.git').lower(), config['target'], config['environment']]
        config['scope'] = config.get('lock_scope') or hashlib.sha256(encode(identity).encode()).hexdigest()
        config.setdefault('branch', 'main')
        config.setdefault('required_checks', [])
        with self.tx() as db:
            old = db.execute('SELECT config FROM projects WHERE id=?', (config['id'],)).fetchone()
            if old and old[0] != encode(config):
                active = db.execute("SELECT 1 FROM batches WHERE project=? AND state!='complete'", (config['id'],)).fetchone()
                unfinished=db.execute("SELECT 1 FROM tasks WHERE project=? AND state NOT IN ('complete','withdrawn')",(config['id'],)).fetchone()
                if active or unfinished:
                    raise Conflict('Cannot change configuration while a batch is unresolved')
            db.execute('INSERT OR REPLACE INTO projects VALUES(?,?,?)', (config['id'],config['scope'],encode(config)))
        return config

    def register(self, project, task_id=None, urgent=False, separate=False):
        self.project(project)
        task_id = task_id or str(uuid.uuid4())
        now = self.clock()
        with self.tx() as db:
            old = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if old:
                if old['project'] != project:
                    raise Conflict('Task ID belongs to another project')
                return dict(old)
            db.execute('INSERT INTO tasks(id,project,state,created,updated,urgent,separate) VALUES(?,?,?,?,?,?,?)',
                       (task_id,project,'active',now,now,int(urgent),int(separate)))
            value=dict(db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone())
        value['validation']=json.loads(value['validation']) if value['validation'] else None
        return value

    def task(self, task_id):
        with self.tx() as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if not row:
                raise ValueError('Unknown task')
            result = dict(row)
            result['validation'] = json.loads(result['validation']) if result['validation'] else None
            return result

    def ready(self, task_id, sha, validation):
        import re
        if not re.fullmatch('[0-9a-f]{40}', sha):
            raise ValueError('Ready requires a full commit SHA')
        checks = validation.get('checks', [])
        if not checks or any(c.get('status') != 'passed' or not c.get('name') for c in checks):
            raise ValueError('Ready requires named, passed validation checks')
        task = self.task(task_id)
        required = set(self.project(task['project'])['required_checks'])
        if required - {c['name'] for c in checks}:
            raise ValueError('Missing required checks: ' + ', '.join(sorted(required - {c['name'] for c in checks})))
        now = self.clock()
        with self.tx() as db:
            task = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if task['batch']:
                if task['sha'] == sha and task['validation'] == encode(validation):
                    return self._task_in(db, task_id)
                raise Conflict('A batched task is immutable; register a new task')
            # Repeated ready calls do not restart the collection window.
            ready_at = task['ready_at'] if task['state'] == 'ready' and task['sha'] == sha else now
            db.execute("UPDATE tasks SET state='ready',sha=?,validation=?,ready_at=?,updated=? WHERE id=?",
                       (sha,encode(validation),ready_at,now,task_id))
            return self._task_in(db, task_id)

    @staticmethod
    def _task_in(db, task_id):
        value=dict(db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone())
        value['validation']=json.loads(value['validation']) if value['validation'] else None
        return value

    def touch(self, task_id, state=None):
        if state not in (None, 'active', 'closing', 'blocked', 'withdrawn'):
            raise ValueError('Invalid task state')
        with self.tx() as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if not row or row['batch']:
                raise Conflict('Unknown or already batched task')
            if state and row['state']=='ready' and state!='withdrawn':
                raise Conflict('Withdraw the ready task before changing its validated commit')
            db.execute('UPDATE tasks SET state=?,updated=? WHERE id=?', (state or row['state'],self.clock(),task_id))
            return self._task_in(db,task_id)

    def due(self, project):
        now = self.clock()
        config = self.project(project)
        with self.tx() as db:
            if db.execute("SELECT 1 FROM batches WHERE scope=? AND state!='complete'", (config['scope'],)).fetchone():
                return None
            tasks = [dict(r) for r in db.execute("SELECT * FROM tasks WHERE project=? AND state='ready' AND batch IS NULL ORDER BY ready_at,rowid", (project,))]
            if not tasks:
                return None
            peers = [dict(r) for r in db.execute("SELECT * FROM tasks WHERE project=? AND state IN ('active','closing') AND batch IS NULL AND updated>?", (project,now-300))]
            tasks=self._select(tasks)
            first = min(t['ready_at'] for t in tasks)
            delay = 180 if any(t['state']=='closing' for t in peers) else 90
            deadline = now if not peers or any(t['urgent'] or t['separate'] for t in tasks) else first+delay
            return {'tasks':tasks,'deadline':deadline,'due':now>=deadline}

    @staticmethod
    def _select(tasks):
        if tasks[0]['separate']:return tasks[:1]
        return [task for task in tasks if not task['separate']]

    def claim(self, scope, owner, ttl=60):
        now = self.clock()
        with self.tx() as db:
            row = db.execute('SELECT * FROM leases WHERE scope=?', (scope,)).fetchone()
            if row and row['expires']>now and row['owner']!=owner:
                return None
            epoch = row['epoch'] if row and row['owner']==owner and row['expires']>now else (row['epoch']+1 if row else 1)
            db.execute('INSERT OR REPLACE INTO leases VALUES(?,?,?,?)', (scope,owner,epoch,now+ttl))
            return epoch

    def heartbeat(self, scope, owner, epoch, ttl=60):
        with self.tx() as db:
            self._guard(db,scope,owner,epoch)
            db.execute('UPDATE leases SET expires=? WHERE scope=?', (self.clock()+ttl,scope))

    def _guard(self, db, scope, owner, epoch):
        row = db.execute('SELECT * FROM leases WHERE scope=?', (scope,)).fetchone()
        if not row or row['owner']!=owner or row['epoch']!=epoch or row['expires']<=self.clock():
            raise Conflict('Lease lost; old owner is fenced')

    def guard(self, scope, owner, epoch):
        with self.tx() as db:
            self._guard(db,scope,owner,epoch)

    def release(self, scope, owner, epoch):
        with self.tx() as db:
            db.execute('UPDATE leases SET expires=0 WHERE scope=? AND owner=? AND epoch=?', (scope,owner,epoch))

    def create_batch(self, project, owner, epoch):
        candidate = self.due(project)
        if not candidate or not candidate['due']:
            return None
        config = self.project(project)
        now = self.clock(); batch_id=str(uuid.uuid4())
        with self.tx() as db:
            self._guard(db,config['scope'],owner,epoch)
            if db.execute("SELECT 1 FROM batches WHERE scope=? AND state!='complete'", (config['scope'],)).fetchone():
                return None
            # Re-read inside the transaction so ready/withdrawn mutations cannot race selection.
            tasks=[dict(r) for r in db.execute("SELECT * FROM tasks WHERE project=? AND state='ready' AND batch IS NULL ORDER BY ready_at,rowid", (project,))]
            if not tasks:
                return None
            tasks=self._select(tasks)
            peers=list(db.execute("SELECT state FROM tasks WHERE project=? AND state IN ('active','closing') AND batch IS NULL AND updated>?", (project,now-300)))
            deadline=min(t['ready_at'] for t in tasks)+(180 if any(t['state']=='closing' for t in peers) else 90)
            if peers and not any(t['urgent'] or t['separate'] for t in tasks) and now<deadline:
                return None
            data={'id':batch_id,'project':project,'scope':config['scope'],'tasks':[{'id':t['id'],'sha':t['sha'],'validation':json.loads(t['validation'])} for t in tasks],
                  'config':config,'workspace':str(self.root/'worktrees'/batch_id),'results':{}}
            db.execute('INSERT INTO batches VALUES(?,?,?,?,?,?,?,NULL)', (batch_id,project,config['scope'],'integrating',now,now,encode(data)))
            db.executemany("UPDATE tasks SET batch=?,state='batched' WHERE id=?", [(batch_id,t['id']) for t in tasks])
            self._event(db,batch_id,'selected',{'taskIds':[t['id'] for t in tasks]},now)
        return data

    def batch(self, batch_id):
        with self.tx() as db:
            row=db.execute('SELECT * FROM batches WHERE id=?',(batch_id,)).fetchone()
            if not row:
                raise ValueError('Unknown batch')
            d=dict(row);d['data']=json.loads(d['data'])
            return d

    def save(self, batch_id, owner, epoch, data, state):
        now=self.clock()
        with self.tx() as db:
            row=db.execute('SELECT * FROM batches WHERE id=?',(batch_id,)).fetchone()
            self._guard(db,row['scope'],owner,epoch)
            old=json.loads(row['data'])
            if old.get('frozen') and any(old.get(k)!=data.get(k) for k in ('frozen','tasks','config','workspace')):
                raise Conflict('Frozen description is immutable')
            db.execute('UPDATE batches SET data=?,state=?,updated=?,error=NULL WHERE id=?', (encode(data),state,now,batch_id))
            if state=='complete':
                db.execute("UPDATE tasks SET state='complete' WHERE batch=?",(batch_id,))
            self._event(db,batch_id,state,{},now)

    def step(self, batch_id, phase, state, result, owner, epoch):
        with self.tx() as db:
            scope=db.execute('SELECT scope FROM batches WHERE id=?',(batch_id,)).fetchone()[0]
            self._guard(db,scope,owner,epoch)
            db.execute('INSERT OR REPLACE INTO steps VALUES(?,?,?,?,?)', (batch_id,phase,state,encode(result),self.clock()))
            self._event(db,batch_id,'step:'+phase,{'state':state},self.clock())

    def fail(self, batch_id, owner, epoch, error):
        with self.tx() as db:
            row=db.execute('SELECT scope FROM batches WHERE id=?',(batch_id,)).fetchone()
            self._guard(db,row[0],owner,epoch)
            db.execute("UPDATE batches SET state='failed',error=?,updated=? WHERE id=?", (str(error)[-8000:],self.clock(),batch_id))
            self._event(db,batch_id,'failed',{'error':str(error)[-8000:]},self.clock())

    def resume(self, batch_id):
        row=self.batch(batch_id)
        with self.tx() as db:
            lease=db.execute('SELECT expires FROM leases WHERE scope=?',(row['scope'],)).fetchone()
            if lease and lease[0]>self.clock():
                raise Conflict('An owner still holds the lease')
            if row['state'] not in ('failed','integrating','frozen','running','recovering'):
                raise Conflict('Batch cannot be resumed')
            db.execute("UPDATE batches SET state='recovering',error=NULL WHERE id=?",(batch_id,))
            self._event(db,batch_id,'resume-requested',{},self.clock())
        return self.batch(batch_id)

    def snapshot(self, project=None):
        with self.tx() as db:
            projects=[json.loads(r[0]) for r in db.execute('SELECT config FROM projects ORDER BY id')]
            tasks=[dict(r) for r in db.execute('SELECT * FROM tasks ORDER BY created')]
            batches=[dict(r) for r in db.execute('SELECT * FROM batches ORDER BY created')]
            for r in tasks:
                r['validation']=json.loads(r['validation']) if r['validation'] else None
            for r in batches:
                r['data']=json.loads(r['data'])
            leases=[{**dict(r),'expired':r['expires']<=self.clock()} for r in db.execute('SELECT * FROM leases')]
            return {'leases':leases,'projects':[p for p in projects if not project or p['id']==project],
                    'tasks':[t for t in tasks if not project or t['project']==project],
                    'batches':[b for b in batches if not project or b['project']==project]}
