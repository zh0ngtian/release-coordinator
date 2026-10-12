"""Recoverable workers; adapters reconcile remote effects before every action."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
import uuid

from .store import Conflict, encode


def atomic_json(path, data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    temp=path.with_name(path.name+'.'+uuid.uuid4().hex)
    with temp.open('x') as f:
        os.chmod(temp,0o600);f.write(encode(data)+'\n');f.flush();os.fsync(f.fileno())
    os.replace(temp,path)


class Worker:
    def __init__(self, store, project, owner=None):
        self.store=store;self.project=project;self.config=store.project(project)
        self.scope=self.config['scope'];self.owner=owner or f'{os.getpid()}:{uuid.uuid4()}'
        self.epoch=None;self.stop=threading.Event();self.lost=threading.Event()
        self.batch_id=None

    def guard(self):
        if self.stop.is_set() or self.lost.is_set():
            raise Conflict('Worker stopped or lease lost')
        self.store.guard(self.scope,self.owner,self.epoch)

    def _heartbeat(self):
        while not self.stop.wait(10):
            try:self.store.heartbeat(self.scope,self.owner,self.epoch)
            except Exception:
                self.lost.set();return

    def process(self, args, cwd=None, log=None):
        self.guard()
        env={**os.environ,'GIT_TERMINAL_PROMPT':'0'}
        command=[str(x) for x in args]
        # Children are killed if the owner loses its lease. Independent scopes have separate groups.
        deadline=time.monotonic()+self.config.get('command_timeout',3600)
        if Path(command[0]).name=='git':deadline=min(deadline,time.monotonic()+180)
        with subprocess.Popen(command,cwd=cwd,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                              text=True,start_new_session=True) as proc:
            while True:
                try:
                    out,err=proc.communicate(timeout=2);break
                except subprocess.TimeoutExpired:
                    try:
                        self.guard()
                        if time.monotonic()>deadline:raise TimeoutError('Command deadline exceeded; retained batch can be resumed')
                    except BaseException:
                        with contextlib.suppress(ProcessLookupError):os.killpg(proc.pid,signal.SIGTERM)
                        try:proc.communicate(timeout=5)
                        except subprocess.TimeoutExpired:os.killpg(proc.pid,signal.SIGKILL);proc.communicate()
                        raise
        self.guard()
        if log:
            with Path(log).open('a') as f:
                os.chmod(log,0o600);f.write(out);f.write(err)
        if proc.returncode:
            raise RuntimeError(f'{Path(command[0]).name} exited {proc.returncode}: {err[-4000:]}')
        return out.strip()

    def git(self, repo, *args):
        return self.process(['git','-c','http.version=HTTP/1.1','-C',str(repo),*args])

    def adapter(self, data, action, phase):
        context=self.store.root/'contexts'/(data['id']+'.json')
        atomic_json(context,data)
        values={'workspace':data['workspace'],'repository':self.config['repository'],'state_dir':str(self.store.root)}
        args=[x.format_map(values) for x in self.config['adapter']]
        log=self.store.root/'logs'/(data['id']+'.log');log.parent.mkdir(exist_ok=True,mode=0o700)
        output=self.process([*args,action,phase,'--context',str(context)],cwd=data['workspace'],log=log)
        try:result=json.loads(output)
        except json.JSONDecodeError as e:raise RuntimeError('Adapter must emit one JSON object on stdout; diagnostics go to stderr') from e
        if not isinstance(result,dict):raise RuntimeError('Adapter result must be an object')
        return result

    def integrate(self,data):
        repo=Path(self.config['repository']);work=Path(data['workspace'])
        self.git(repo,'fetch','origin',self.config['branch'])
        if not work.exists():
            work.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            self.git(repo,'worktree','add','--detach',str(work),'origin/'+self.config['branch'])
        if self.git(work,'rev-parse','--show-toplevel')!=str(work):
            raise RuntimeError('Unexpected integration worktree')
        if (Path(self.git(work,'rev-parse','--git-dir'))/'MERGE_HEAD').exists():
            raise RuntimeError('Resolve and commit the merge conflict in the retained worktree, then resume')
        if data.get('recoveryOf'):
            self.git(work,'checkout','--detach','origin/'+self.config['branch'])
            self.git(work,'reset','--hard','origin/'+self.config['branch'])
            self.git(work,'clean','-ffd')
        for task in data['tasks']:
            # Fetch by immutable SHA; readiness is never a mutable branch name.
            self.git(repo,'fetch','origin',task['sha'])
            self.git(work,'merge','--no-ff','--no-edit',task['sha'])
        self.git(work,'submodule','update','--init','--recursive')
        return work

    def prepare(self,data):
        work=self.integrate(data)
        prepared=self.adapter(data,'run','prepare')
        if not prepared.get('done') or not isinstance(prepared.get('result'),dict):
            raise RuntimeError('Preparation did not finish')
        data['results']['prepare']=prepared['result']
        self.store.save(data['id'],self.owner,self.epoch,data,'integrating')
        for task in data['tasks']:
            self.git(work,'merge-base','--is-ancestor',task['sha'],'HEAD')
        # The manifest has no self-referential final SHA. CI derives it from the commit introducing it.
        manifest={'schemaVersion':1,'batchId':data['id'],'project':data['project'],'scope':data['scope'],
                  'target':self.config['target'],'environment':self.config['environment'],
                  'tasks':[{'id':t['id'],'sha':t['sha'],'validation':{'checks':[{'name':c['name'],'status':c['status']} for c in t['validation']['checks']]}} for t in data['tasks']],
                  'prepared':{k:v for k,v in prepared['result'].items() if k in ('version','buildNumber','bundleIdentifier','localValidation','inputFingerprint')}}
        atomic_json(work/'.release-batch.json',manifest)
        self.git(work,'add','--all')
        if self.git(work,'status','--porcelain'):
            self.git(work,'commit','-m','Freeze release batch '+data['id'])
        if self.git(work,'log','-1','--format=%H','--','.release-batch.json') != self.git(work,'rev-parse','HEAD'):
            raise RuntimeError('Frozen manifest must be introduced by the exact release commit')
        sha=self.git(work,'rev-parse','HEAD')
        data['frozen']={'sha':sha,'manifestSha256':hashlib.sha256((work/'.release-batch.json').read_bytes()).hexdigest(),
                        'version':prepared['result'].get('version'),'buildNumber':prepared['result'].get('buildNumber'),
                        'tasks':[{'id':t['id'],'sha':t['sha']} for t in data['tasks']],
                        'target':self.config['target'],'environment':self.config['environment']}
        self.store.save(data['id'],self.owner,self.epoch,data,'frozen')

    def run(self, batch_id=None):
        preflight=self.config.get('preflight')
        if preflight:
            try:
                result=subprocess.run(preflight,capture_output=True,text=True,timeout=15)
                ok=result.returncode==0
            except (OSError,subprocess.TimeoutExpired):ok=False
            atomic_json(self.store.root/'preflight'/(self.project+'.json'),{'ok':ok,'checkedAt':time.time()})
            if not ok:return None
        self.epoch=self.store.claim(self.scope,self.owner)
        if self.epoch is None:return None
        heart=threading.Thread(target=self._heartbeat,daemon=True);heart.start()
        data=None
        try:
            if batch_id:
                row=self.store.batch(batch_id)
                if row['project']!=self.project:raise ValueError('Wrong project')
                data=row['data']
            else:data=self.store.create_batch(self.project,self.owner,self.epoch)
            if not data:return None
            self.batch_id=data['id']
            data['owner']={'identity':self.owner,'epoch':self.epoch}
            data.setdefault('progressPath',str(self.store.root/'progress'/(data['id']+'.json')))
            if not data.get('frozen'):self.prepare(data)
            work=Path(data['workspace']);sha=data['frozen']['sha']
            if self.git(work,'rev-parse','HEAD')!=sha or self.git(work,'status','--porcelain'):
                raise RuntimeError('Frozen worktree changed; restore its exact frozen commit before resuming')
            manifest=work/'.release-batch.json'
            if hashlib.sha256(manifest.read_bytes()).hexdigest()!=data['frozen']['manifestSha256']:
                raise RuntimeError('Frozen manifest changed')
            # A resumed worker reconciles every phase, including completed external actions.
            for phase in self.config['phases']:
                self.store.save(data['id'],self.owner,self.epoch,data,'running')
                self.store.step(data['id'],phase,'checking',{},self.owner,self.epoch)
                result=self.adapter(data,'check',phase)
                if not result.get('done'):
                    self.store.step(data['id'],phase,'executing',{},self.owner,self.epoch)
                    self.adapter(data,'run',phase)
                    result=self.adapter(data,'check',phase)
                if result.get('done') is not True:
                    raise RuntimeError('Remote phase not verified: '+phase)
                data['results'][phase]=result.get('result',{})
                self.store.step(data['id'],phase,'verified',data['results'][phase],self.owner,self.epoch)
                self.store.save(data['id'],self.owner,self.epoch,data,'running')
            for task in data['tasks']:
                self.git(work,'merge-base','--is-ancestor',task['sha'],sha)
            receipt={k:data[k] for k in ('id','project','scope','tasks','frozen','results')}
            from . import __version__
            receipt['coordinatorVersion']=__version__
            receipt['completedAt']=self.store.clock()
            atomic_json(self.store.root/'receipts'/(data['id']+'.json'),receipt)
            data['receipt']=receipt
            self.store.save(data['id'],self.owner,self.epoch,data,'complete')
            return data
        except Exception as e:
            if data:
                with contextlib.suppress(Conflict):
                    if self.stop.is_set() or data.get('frozen') and self.store.batch(data['id'])['state'] in ('running','recovering'):
                        self.store.save(data['id'],self.owner,self.epoch,data,'recovering')
                    else:self.store.fail(data['id'],self.owner,self.epoch,e)
            else:raise
            return None
        finally:
            self.stop.set();heart.join(timeout=2)
            self.store.release(self.scope,self.owner,self.epoch)


class Scheduler:
    def __init__(self, store):
        self.store=store;self.workers={};self.lock=threading.Lock();self.stopping=False

    def tick(self):
        snapshot=self.store.snapshot()
        with self.lock:
            self.workers={k:v for k,v in self.workers.items() if v[0].is_alive()}
            if self.stopping:return
            for config in snapshot['projects']:
                if config['scope'] in self.workers:continue
                preflight=self.store.root/'preflight'/(config['id']+'.json')
                if preflight.exists():
                    check=json.loads(preflight.read_text())
                    if not check['ok'] and time.time()-check['checkedAt']<30:continue
                unresolved=[b for b in snapshot['batches'] if b['scope']==config['scope'] and b['state']!='complete']
                batch=None
                if unresolved:
                    row=unresolved[0]
                    if row['project']!=config['id']:continue
                    if row['state']=='failed':
                        candidate=self.store.due(config['id'])
                        if not candidate or not candidate['due']:continue
                    else:batch=row['id']
                else:
                    candidate=self.store.due(config['id'])
                    if not candidate or not candidate['due']:continue
                worker=Worker(self.store,config['id'])
                thread=threading.Thread(target=worker.run,args=(batch,),daemon=True,name=config['id'])
                self.workers[config['scope']]=(thread,worker);thread.start()

    def shutdown(self, wait=False):
        with self.lock:
            self.stopping=True
            pairs=list(self.workers.values())
            for thread,worker in pairs:worker.stop.set()
        if wait:
            for thread,worker in pairs:thread.join(timeout=10)
