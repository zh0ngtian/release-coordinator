import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from release_coordinator.engine import Worker, Scheduler
from release_coordinator.store import Store, Conflict
from release_coordinator.cli import call


VALID={'checks':[{'name':'unit','status':'passed'}]}


def git(root,*args):
    return subprocess.check_output(['git','-C',str(root),*args],stderr=subprocess.DEVNULL,text=True).strip()


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.now=100.
        self.store=Store(self.root/'state',lambda:self.now)
        self.add('one');self.add('two')
    def tearDown(self):self.temp.cleanup()
    def add(self,name,lock_scope=None):
        self.store.add_project({'id':name,'repository':str(self.root),'remote':name,'target':'app','environment':'production','adapter':['python3','adapter.py'],'required_checks':['unit'],'lock_scope':lock_scope})
    def ready(self,id,project='one',**kw):
        self.store.register(project,id,**kw);return self.store.ready(id,'a'*40,VALID)
    def test_no_peer_immediate_and_unrelated_project_does_not_delay(self):
        self.store.register('two','peer');self.ready('ready')
        self.assertTrue(self.store.due('one')['due'])
    def test_window_90_and_closing_extension_never_exceeds_180(self):
        self.store.register('one','peer');self.ready('first')
        self.assertEqual(self.store.due('one')['deadline'],190)
        self.now=160;self.store.touch('peer','closing');self.assertEqual(self.store.due('one')['deadline'],280)
        self.now=270;self.ready('late');self.assertFalse(self.store.due('one')['due'])
        self.now=280;self.assertTrue(self.store.due('one')['due'])
    def test_repeated_ready_does_not_reset_deadline(self):
        self.store.register('one','peer');self.ready('first');self.now=170
        self.store.ready('first','a'*40,VALID);self.assertEqual(self.store.due('one')['deadline'],190)
    def test_blocked_task_is_not_waited_for(self):
        self.store.register('one','peer');self.store.touch('peer','blocked');self.ready('first');self.assertTrue(self.store.due('one')['due'])
    def test_urgent_and_separate(self):
        self.ready('solo',separate=True);self.ready('another')
        config=self.store.project('one');epoch=self.store.claim(config['scope'],'owner')
        batch=self.store.create_batch('one','owner',epoch)
        self.assertEqual([t['id'] for t in batch['tasks']],['solo'])
        self.assertIsNone(self.store.task('another')['batch'])
    def test_lease_is_scoped_and_old_generation_is_fenced(self):
        a=self.store.project('one')['scope'];b=self.store.project('two')['scope']
        epoch=self.store.claim(a,'old');self.assertIsNone(self.store.claim(a,'other'))
        self.assertEqual(self.store.claim(b,'other'),1)
        self.now=161;new=self.store.claim(a,'new');self.assertGreater(new,epoch)
        with self.assertRaises(Conflict):self.store.heartbeat(a,'old',epoch)
        self.store.release(a,'old',epoch);self.store.guard(a,'new',new)
    def test_new_ready_task_cannot_enter_frozen_batch(self):
        self.ready('first');scope=self.store.project('one')['scope'];epoch=self.store.claim(scope,'owner')
        batch=self.store.create_batch('one','owner',epoch);batch['frozen']={'sha':'b'*40}
        self.store.save(batch['id'],'owner',epoch,batch,'frozen');self.ready('late')
        self.assertIsNone(self.store.task('late')['batch'])
        changed=json.loads(json.dumps(batch));changed['tasks'].append({'id':'late','sha':'a'*40})
        with self.assertRaises(Conflict):self.store.save(batch['id'],'owner',epoch,changed,'frozen')
    def test_failed_batch_blocks_only_its_project_and_resume_requires_expired_owner(self):
        self.ready('first');scope=self.store.project('one')['scope'];epoch=self.store.claim(scope,'owner')
        batch=self.store.create_batch('one','owner',epoch);self.store.fail(batch['id'],'owner',epoch,'failed')
        with self.assertRaises(Conflict):self.store.resume(batch['id'])
        self.ready('late');self.assertIsNone(self.store.due('one'))
        self.ready('other','two');self.assertTrue(self.store.due('two')['due'])
        with self.assertRaises(Conflict):self.store.register('one','bad-repair',repairs_batch='missing')

    def test_repair_must_link_to_latest_failed_leaf(self):
        self.ready('first');scope=self.store.project('one')['scope'];epoch=self.store.claim(scope,'owner')
        parent=self.store.create_batch('one','owner',epoch);self.store.fail(parent['id'],'owner',epoch,'first failure');self.store.release(scope,'owner',epoch)
        self.store.register('one','repair-one',repairs_batch=parent['id']);self.store.ready('repair-one','b'*40,VALID)
        epoch=self.store.claim(scope,'owner-two');child=self.store.create_batch('one','owner-two',epoch);self.store.fail(child['id'],'owner-two',epoch,'second failure');self.store.release(scope,'owner-two',epoch)
        with self.assertRaises(Conflict):self.store.register('one','stale-repair',repairs_batch=parent['id'])
        self.assertIsNone(self.store.due('one'))
        self.store.register('one','current-repair',repairs_batch=child['id']);self.store.ready('current-repair','d'*40,VALID)
        self.assertTrue(self.store.due('one')['due'])

    def test_shared_scope_failure_keeps_other_project_queue_and_history_independent(self):
        self.add('one','shared-target');self.add('two','shared-target')
        scope=self.store.project('one')['scope'];self.assertEqual(scope,self.store.project('two')['scope'])
        self.ready('original');epoch=self.store.claim(scope,'first-owner')
        failed=self.store.create_batch('one','first-owner',epoch)
        self.store.fail(failed['id'],'first-owner',epoch,'first project failed');self.store.release(scope,'first-owner',epoch)
        history=self.store.batch(failed['id']);original=self.store.task('original')
        self.ready('ordinary-one');self.ready('ordinary-two','two')
        self.assertIsNone(self.store.due('one'))
        candidate=self.store.due('two');self.assertIsNotNone(candidate)
        self.assertEqual([t['id'] for t in candidate['tasks']],['ordinary-two']);self.assertTrue(candidate['due'])
        epoch=self.store.claim(scope,'second-owner');batch=self.store.create_batch('two','second-owner',epoch)
        self.assertIsNotNone(batch);self.assertIsNone(batch['recoveryOf']);self.assertEqual(batch['recoveryChain'],[])
        self.assertEqual([t['id'] for t in batch['tasks']],['ordinary-two'])
        self.store.save(batch['id'],'second-owner',epoch,batch,'complete');self.store.release(scope,'second-owner',epoch)
        self.assertEqual(self.store.batch(failed['id']),history);self.assertEqual(self.store.task('original'),original)
        self.assertEqual(self.store.task('ordinary-two')['state'],'complete');self.assertIsNone(self.store.due('one'))
        with self.assertRaises(Conflict):self.store.register('two','foreign-repair',repairs_batch=failed['id'])
        self.ready('own-repair',repairs_batch=failed['id']);epoch=self.store.claim(scope,'repair-owner')
        repair=self.store.create_batch('one','repair-owner',epoch)
        self.assertEqual(repair['recoveryOf'],failed['id']);self.assertEqual(repair['recoveryChain'],[failed['id']])
        self.assertEqual({t['id'] for t in repair['tasks']},{'original','own-repair'})
        self.assertIsNone(self.store.task('ordinary-one')['batch']);self.assertEqual(self.store.batch(failed['id']),history)

    def test_shared_scope_projects_repair_their_own_latest_failed_leaf(self):
        self.add('one','shared-target');self.add('two','shared-target');scope=self.store.project('one')['scope']
        failures={}
        for project in ('one','two'):
            self.ready(project+'-original',project);epoch=self.store.claim(scope,project)
            batch=self.store.create_batch(project,project,epoch);self.assertIsNotNone(batch)
            self.store.fail(batch['id'],project,epoch,project+' failed');self.store.release(scope,project,epoch)
            failures[project]=self.store.batch(batch['id']);self.now+=1
        leaves={}
        for project,foreign in (('one','two'),('two','one')):
            with self.assertRaises(Conflict):self.store.register(project,project+'-foreign',repairs_batch=failures[foreign]['id'])
            self.ready(project+'-repair',project,repairs_batch=failures[project]['id'])
            candidate=self.store.due(project);self.assertIsNotNone(candidate);self.assertTrue(candidate['due'])
            epoch=self.store.claim(scope,project);child=self.store.create_batch(project,project,epoch)
            self.assertIsNotNone(child);self.assertEqual(child['recoveryOf'],failures[project]['id'])
            self.assertEqual({t['id'] for t in child['tasks']},{project+'-original',project+'-repair'})
            self.store.fail(child['id'],project,epoch,project+' repair failed');self.store.release(scope,project,epoch)
            leaves[project]=self.store.batch(child['id']);self.now+=1
            with self.assertRaises(Conflict):self.store.register(project,project+'-stale',repairs_batch=failures[project]['id'])
        for project,foreign in (('one','two'),('two','one')):
            with self.assertRaises(Conflict):self.store.register(project,project+'-foreign-leaf',repairs_batch=leaves[foreign]['id'])
            self.ready(project+'-latest',project,repairs_batch=leaves[project]['id'])
            epoch=self.store.claim(scope,project);child=self.store.create_batch(project,project,epoch)
            self.assertIsNotNone(child);self.assertEqual(child['recoveryOf'],leaves[project]['id'])
            self.assertEqual(child['recoveryChain'],[failures[project]['id'],leaves[project]['id']])
            self.assertEqual({t['id'] for t in child['tasks']},{project+'-original',project+'-repair',project+'-latest'})
            self.store.save(child['id'],project,epoch,child,'complete');self.store.release(scope,project,epoch)
            self.assertEqual(self.store.batch(failures[project]['id']),failures[project])
            self.assertEqual(self.store.batch(leaves[project]['id']),leaves[project])

    def test_shared_scope_active_batch_and_lease_block_other_project(self):
        self.add('one','shared-target');self.add('two','shared-target');scope=self.store.project('one')['scope']
        self.ready('one-ready');self.ready('two-ready','two')
        epoch=self.store.claim(scope,'one-owner');self.assertIsNone(self.store.claim(scope,'two-owner'))
        batch=self.store.create_batch('one','one-owner',epoch)
        self.assertIsNone(self.store.due('two'));self.assertIsNone(self.store.create_batch('two','one-owner',epoch))
        self.store.release(scope,'one-owner',epoch)
        self.store.resume(batch['id']);new_epoch=self.store.claim(scope,'two-owner')
        with self.assertRaises(Conflict):self.store.guard(scope,'one-owner',epoch)
        self.assertIsNone(self.store.due('two'));self.assertIsNone(self.store.create_batch('two','two-owner',new_epoch))
        self.assertIsNone(self.store.task('two-ready')['batch'])

    def test_scheduler_resumes_child_despite_failed_parent(self):
        self.ready('original');scope=self.store.project('one')['scope'];epoch=self.store.claim(scope,'owner')
        parent=self.store.create_batch('one','owner',epoch);self.store.fail(parent['id'],'owner',epoch,'failure');self.store.release(scope,'owner',epoch)
        self.ready('repair',repairs_batch=parent['id']);epoch=self.store.claim(scope,'repair-owner')
        child=self.store.create_batch('one','repair-owner',epoch);self.store.release(scope,'repair-owner',epoch)
        scheduler=Scheduler(self.store)
        with patch('release_coordinator.engine.Worker.run') as run:
            scheduler.tick()
            for thread,worker in scheduler.workers.values():thread.join(2)
            run.assert_called_once_with(child['id'])

    def test_legacy_batch_completion_marks_own_tasks_complete(self):
        self.ready('original');scope=self.store.project('one')['scope'];epoch=self.store.claim(scope,'owner')
        batch=self.store.create_batch('one','owner',epoch);batch.pop('newTaskIds')
        self.store.save(batch['id'],'owner',epoch,batch,'complete')
        self.assertEqual(self.store.task('original')['state'],'complete')

    def test_atomic_claim_between_connections(self):
        scope=self.store.project('one')['scope'];barrier=threading.Barrier(3);wins=[]
        def claim(owner):
            s=Store(self.root/'state',lambda:self.now);barrier.wait();wins.append(s.claim(scope,owner))
        threads=[threading.Thread(target=claim,args=(str(i),)) for i in range(2)]
        for t in threads:t.start()
        barrier.wait()
        for t in threads:t.join()
        self.assertEqual(sum(x is not None for x in wins),1)

    def test_existing_state_database_migrates_recovery_columns(self):
        path=self.root/'legacy'/'state.sqlite3';path.parent.mkdir()
        with sqlite3.connect(path) as db:
            db.executescript('''
                CREATE TABLE projects(id TEXT PRIMARY KEY, scope TEXT NOT NULL, config TEXT NOT NULL);
                CREATE TABLE tasks(id TEXT PRIMARY KEY, project TEXT NOT NULL, state TEXT NOT NULL,
                    sha TEXT, validation TEXT, created REAL NOT NULL, updated REAL NOT NULL, ready_at REAL,
                    urgent INTEGER NOT NULL DEFAULT 0, separate INTEGER NOT NULL DEFAULT 0, batch TEXT);
                CREATE TABLE batches(id TEXT PRIMARY KEY, project TEXT NOT NULL, scope TEXT NOT NULL,
                    state TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL, data TEXT NOT NULL, error TEXT);
            ''')
        Store(path.parent)
        with sqlite3.connect(path) as db:
            tasks={row[1] for row in db.execute('PRAGMA table_info(tasks)')}
            batches={row[1] for row in db.execute('PRAGMA table_info(batches)')}
        self.assertTrue({'repairs_batch','recovered_by'} <= tasks)
        self.assertIn('recovery_of',batches)


FAKE='''import argparse,json,os,time
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('action');p.add_argument('phase');p.add_argument('--context');a=p.parse_args()
d=json.loads(Path(a.context).read_text());base=Path(a.context).parent.parent/'effects';base.mkdir(exist_ok=True)
marker=base/(d['id']+'-'+a.phase+'.json');options=d['config'].get('options',{})
if a.action=='run':
 if a.phase=='publish' and options.get('fail_publish'):raise RuntimeError('permanent publish failure')
 if a.phase=='publish' and options.get('shared_target'):
  active=base/'shared-target-active'
  fd=os.open(active,os.O_CREAT|os.O_EXCL|os.O_WRONLY);os.close(fd)
  try:
   (base/(d['id']+'.entered')).write_text('yes')
   end=time.time()+10
   while not (base/'shared-target-release').exists() and time.time()<end:time.sleep(.02)
   if not (base/'shared-target-release').exists():raise RuntimeError('shared target gate timed out')
  finally:active.unlink()
 if a.phase=='publish' and options.get('barrier'):
  started=base/(d['id']+'.started');started.write_text(str(time.time()))
  end=time.time()+10
  while len(list(base.glob('*.started')))<2 and time.time()<end:time.sleep(.02)
  if len(list(base.glob('*.started')))<2:raise RuntimeError('workers did not run in parallel')
  time.sleep(.1)
 counter=base/(d['id']+'-'+a.phase+'.count');counter.write_text(str(int(counter.read_text())+1) if counter.exists() else '1')
 marker.write_text(json.dumps({'effectVerified':True,'completedAt':time.time()}))
 if a.phase=='publish' and options.get('lose_response') and not (base/'response-lost').exists():
  (base/'response-lost').write_text('yes');raise RuntimeError('response was lost after publishing')
print(json.dumps({'done':marker.exists(),'result':json.loads(marker.read_text()) if marker.exists() else {}}))
'''


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.store=Store(self.root/'state')
        self.adapter=self.root/'adapter.py';self.adapter.write_text(FAKE)
    def tearDown(self):self.temp.cleanup()
    def repository(self,name,options=None,lock_scope=None):
        repo=self.root/name;repo.mkdir();git(repo,'init','-b','main');git(repo,'config','user.name','Test');git(repo,'config','user.email','test@example.invalid')
        (repo/'file').write_text('base');git(repo,'add','.');git(repo,'commit','-m','base')
        remote=self.root/(name+'.git');subprocess.check_call(['git','clone','--bare',str(repo),str(remote)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        git(repo,'remote','add','origin',str(remote));git(repo,'checkout','-b','task');(repo/'file').write_text('feature');git(repo,'commit','-am','feature')
        sha=git(repo,'rev-parse','HEAD');git(repo,'push','origin','task');git(repo,'checkout','main')
        config=self.store.add_project({'id':name,'repository':str(repo),'remote':str(remote),'target':'app','environment':'production','adapter':[sys.executable,str(self.adapter)],'phases':['publish','verify'],'options':options or {},'lock_scope':lock_scope})
        self.store.register(name,name+'-task');self.store.ready(name+'-task',sha,VALID)
        return repo,config
    def test_distinct_projects_publish_concurrently(self):
        self.repository('one',{'barrier':True});self.repository('two',{'barrier':True})
        threads=[threading.Thread(target=Worker(self.store,name).run) for name in ['one','two']]
        for t in threads:t.start()
        for t in threads:t.join(20)
        batches=self.store.snapshot()['batches'];self.assertEqual([b['state'] for b in batches],['complete','complete'])
    def test_response_loss_is_reconciled_without_second_publish(self):
        self.repository('one',{'lose_response':True});Worker(self.store,'one').run()
        batch=self.store.snapshot()['batches'][0];self.assertEqual(batch['state'],'complete');frozen=batch['data']['frozen']
        complete=self.store.batch(batch['id']);self.assertEqual(complete['state'],'complete');self.assertEqual(complete['data']['frozen'],frozen)
        counter=self.root/'state/effects'/(batch['id']+'-publish.count');self.assertEqual(counter.read_text(),'1')
        self.assertEqual(self.store.task('one-task')['state'],'complete')
        self.assertTrue((self.root/'state/receipts'/(batch['id']+'.json')).exists())

    def test_shared_scope_workers_serialize_the_same_write_target(self):
        self.repository('one',{'shared_target':True},'shared-target');self.repository('two',{'shared_target':True},'shared-target')
        barrier=threading.Barrier(3);results=[];errors=[]
        def publish(project):
            try:
                store=Store(self.store.root);barrier.wait();results.append((project,Worker(store,project).run()))
            except BaseException as error:errors.append(error)
        threads=[threading.Thread(target=publish,args=(project,)) for project in ('one','two')]
        for thread in threads:thread.start()
        barrier.wait();effects=self.store.root/'effects';deadline=time.time()+10
        try:
            while time.time()<deadline and (not effects.exists() or not list(effects.glob('*.entered')) or not results):time.sleep(.02)
            self.assertFalse(errors);self.assertEqual(len(results),1);self.assertIsNone(results[0][1])
            self.assertEqual(len(list(effects.glob('*.entered'))),1);self.assertTrue((effects/'shared-target-active').exists())
            self.assertEqual(len(self.store.snapshot()['batches']),1)
        finally:
            effects.mkdir(exist_ok=True);(effects/'shared-target-release').write_text('yes')
            for thread in threads:thread.join(15)
        self.assertFalse(any(thread.is_alive() for thread in threads));self.assertFalse(errors)
        self.assertEqual(sum(result is not None for _,result in results),1)
        losing_project=next(project for project,result in results if result is None)
        self.assertIsNotNone(Worker(Store(self.store.root),losing_project).run())
        batches=self.store.snapshot()['batches'];self.assertEqual([batch['state'] for batch in batches],['complete','complete'])
        self.assertEqual(len(list(effects.glob('*.entered'))),2);self.assertFalse((effects/'shared-target-active').exists())
        for batch in batches:self.assertEqual((effects/(batch['id']+'-publish.count')).read_text(),'1')

    def test_shared_scope_failure_allows_other_project_response_loss_recovery(self):
        self.repository('one',{'fail_publish':True},'shared-target');self.repository('two',{'lose_response':True},'shared-target')
        self.assertIsNone(Worker(self.store,'one').run())
        failed=self.store.snapshot('one')['batches'][0];original=self.store.task('one-task')
        self.assertEqual(failed['state'],'failed');self.assertIn('permanent publish failure',failed['error'])
        result=Worker(Store(self.store.root),'two').run();self.assertIsNotNone(result)
        self.assertIsNone(result['recoveryOf']);self.assertEqual(result['recoveryChain'],[])
        self.assertEqual([task['id'] for task in result['tasks']],['two-task'])
        self.assertEqual(result['receipt']['recoveryChain'],[]);self.assertIsNone(result['receipt']['recoveryOf'])
        self.assertEqual(self.store.task('two-task')['state'],'complete')
        self.assertEqual(self.store.batch(failed['id']),failed);self.assertEqual(self.store.task('one-task'),original)
        self.assertEqual((self.store.root/'effects'/(result['id']+'-publish.count')).read_text(),'1')
        self.assertTrue((self.store.root/'receipts'/(result['id']+'.json')).exists())
    def test_permanent_phase_failure_retains_diagnostics_without_auto_retry(self):
        self.repository('one',{'fail_publish':True});Worker(self.store,'one').run()
        batch=self.store.snapshot()['batches'][0]
        self.assertEqual(batch['state'],'failed')
        self.assertIn('permanent publish failure',batch['error'])
        self.assertIn('frozen',batch['data'])
        scheduler=Scheduler(self.store);scheduler.tick()
        self.assertFalse(scheduler.workers)

    def test_merge_conflict_keeps_worktree_and_does_not_publish(self):
        repo,config=self.repository('one');base=git(repo,'rev-parse','main')
        git(repo,'checkout','-b','conflict',base);(repo/'file').write_text('incompatible');git(repo,'commit','-am','conflict');sha=git(repo,'rev-parse','HEAD');git(repo,'push','origin','conflict');git(repo,'checkout','main')
        self.store.register('one','second');self.store.ready('second',sha,VALID);Worker(self.store,'one').run()
        batch=self.store.snapshot()['batches'][0];self.assertEqual(batch['state'],'failed');self.assertTrue(Path(batch['data']['workspace']).exists())
        self.assertNotIn('frozen',batch['data']);self.assertFalse((self.root/'state/effects').exists())

    def test_linked_repair_inherits_failed_task_and_preserves_failure_record(self):
        repo,config=self.repository('one')
        parent=Worker(self.store,'one').run()
        self.assertEqual(self.store.batch(parent['id'])['state'],'complete')
        with self.store.tx() as db:
            db.execute("UPDATE batches SET state='failed',error='artifact download failed' WHERE id=?",(parent['id'],))
        git(repo,'push','origin',parent['frozen']['sha']+':refs/heads/main')
        self.store.register('one','repair',repairs_batch=parent['id'])
        git(repo,'checkout','-b','repair',parent['frozen']['sha'])
        (repo/'file').write_text('repaired');git(repo,'commit','-am','repair');repair_sha=git(repo,'rev-parse','HEAD');git(repo,'push','origin','repair');git(repo,'checkout','main')
        self.store.ready('repair',repair_sha,VALID)
        self.assertTrue(self.store.due('one')['due'])
        owner='repair-owner';epoch=self.store.claim(config['scope'],owner)
        child_data=self.store.create_batch('one',owner,epoch)
        self.store.release(config['scope'],owner,epoch)
        work=Path(child_data['workspace']);git(repo,'fetch','origin','main');git(repo,'worktree','add','--detach',str(work),'origin/main');(work/'.release-batch.json').write_text('{"partial":true}')
        child=Worker(self.store,'one').run(child_data['id'])
        if child is None:
            failed=self.store.batch(child_data['id'])
            log=self.root/'state/logs'/(child_data['id']+'.log')
            self.fail(f"recovery batch failed: {failed['error']}\nworkspace={child_data['workspace']}\n{log.read_text() if log.exists() else 'no log'}")
        self.assertEqual(self.store.batch(child['id'])['state'],'complete')
        self.assertEqual(child['recoveryOf'],parent['id'])
        self.assertEqual({t['id'] for t in child['tasks']},{'one-task','repair'})
        self.assertEqual(child['newTaskIds'],['repair'])
        self.assertEqual(self.store.task('one-task')['state'],'recovered')
        self.assertEqual(self.store.task('one-task')['recovered_by'],child['id'])
        self.assertEqual(self.store.task('repair')['state'],'complete')
        self.assertEqual(self.store.batch(parent['id'])['state'],'failed')
        self.assertEqual(self.store.batch(parent['id'])['error'],'artifact download failed')
        for task in child['tasks']:
            self.assertEqual(subprocess.run(['git','-C',str(child['workspace']),'merge-base','--is-ancestor',task['sha'],child['frozen']['sha']]).returncode,0)
        self.assertIsNone(self.store.due('one'))


class ServiceTests(unittest.TestCase):
    def test_socket_health_and_restart_preserves_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'state';source=Path(__file__).resolve().parents[1]
            for attempt in range(2):
                proc=subprocess.Popen([sys.executable,'-B','-m','release_coordinator','--state-dir',str(root),'serve'],cwd=source,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
                try:
                    for _ in range(100):
                        try:health=call(root,'GET','/health');break
                        except OSError:time.sleep(.03)
                    else:
                        stdout,stderr=proc.communicate(timeout=2)
                        if b'Operation not permitted' in stderr:
                            self.skipTest('sandbox denies UNIX socket creation')
                        self.fail(f'service did not start; exit={proc.returncode}; stdout={stdout!r}; stderr={stderr!r}')
                    self.assertTrue(health['ok']);self.assertEqual((root/'service.sock').stat().st_mode&0o777,0o600)
                    if attempt==0:
                        call(root,'POST','/project',{'id':'one','repository':directory,'remote':'example','target':'app','environment':'prod','adapter':['python3','adapter.py']})
                        call(root,'POST','/task',{'project':'one','id':'retained'})
                    else:self.assertEqual(call(root,'GET','/status')['tasks'][0]['id'],'retained')
                finally:proc.terminate();proc.communicate(timeout=15)

if __name__=='__main__':unittest.main()
