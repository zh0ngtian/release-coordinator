import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from release_coordinator.engine import Worker
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
    def add(self,name):
        self.store.add_project({'id':name,'repository':str(self.root),'remote':name,'target':'app','environment':'production','adapter':['python3','adapter.py'],'required_checks':['unit']})
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
    def test_failed_batch_blocks_only_its_scope_and_resume_requires_expired_owner(self):
        self.ready('first');scope=self.store.project('one')['scope'];epoch=self.store.claim(scope,'owner')
        batch=self.store.create_batch('one','owner',epoch);self.store.fail(batch['id'],'owner',epoch,'failed')
        with self.assertRaises(Conflict):self.store.resume(batch['id'])
        self.ready('late');self.assertIsNone(self.store.due('one'))
        self.ready('other','two');self.assertTrue(self.store.due('two')['due'])
        self.store.release(scope,'owner',epoch);self.assertEqual(self.store.resume(batch['id'])['state'],'recovering')
    def test_atomic_claim_between_connections(self):
        scope=self.store.project('one')['scope'];barrier=threading.Barrier(3);wins=[]
        def claim(owner):
            s=Store(self.root/'state',lambda:self.now);barrier.wait();wins.append(s.claim(scope,owner))
        threads=[threading.Thread(target=claim,args=(str(i),)) for i in range(2)]
        for t in threads:t.start()
        barrier.wait()
        for t in threads:t.join()
        self.assertEqual(sum(x is not None for x in wins),1)


FAKE='''import argparse,json,time
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('action');p.add_argument('phase');p.add_argument('--context');a=p.parse_args()
d=json.loads(Path(a.context).read_text());base=Path(a.context).parent.parent/'effects';base.mkdir(exist_ok=True)
marker=base/(d['id']+'-'+a.phase+'.json');options=d['config'].get('options',{})
if a.action=='run':
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
    def repository(self,name,options=None):
        repo=self.root/name;repo.mkdir();git(repo,'init','-b','main');git(repo,'config','user.name','Test');git(repo,'config','user.email','test@example.invalid')
        (repo/'file').write_text('base');git(repo,'add','.');git(repo,'commit','-m','base')
        remote=self.root/(name+'.git');subprocess.check_call(['git','clone','--bare',str(repo),str(remote)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        git(repo,'remote','add','origin',str(remote));git(repo,'checkout','-b','task');(repo/'file').write_text('feature');git(repo,'commit','-am','feature')
        sha=git(repo,'rev-parse','HEAD');git(repo,'push','origin','task');git(repo,'checkout','main')
        config=self.store.add_project({'id':name,'repository':str(repo),'remote':str(remote),'target':'app','environment':'production','adapter':[sys.executable,str(self.adapter)],'phases':['publish','verify'],'options':options or {}})
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
        batch=self.store.snapshot()['batches'][0];self.assertEqual(batch['state'],'failed');frozen=batch['data']['frozen']
        self.store.resume(batch['id']);Worker(self.store,'one').run(batch['id'])
        complete=self.store.batch(batch['id']);self.assertEqual(complete['state'],'complete');self.assertEqual(complete['data']['frozen'],frozen)
        counter=self.root/'state/effects'/(batch['id']+'-publish.count');self.assertEqual(counter.read_text(),'1')
        self.assertEqual(self.store.task('one-task')['state'],'complete')
        self.assertTrue((self.root/'state/receipts'/(batch['id']+'.json')).exists())
    def test_merge_conflict_keeps_worktree_and_does_not_publish(self):
        repo,config=self.repository('one');base=git(repo,'rev-parse','main')
        git(repo,'checkout','-b','conflict',base);(repo/'file').write_text('incompatible');git(repo,'commit','-am','conflict');sha=git(repo,'rev-parse','HEAD');git(repo,'push','origin','conflict');git(repo,'checkout','main')
        self.store.register('one','second');self.store.ready('second',sha,VALID);Worker(self.store,'one').run()
        batch=self.store.snapshot()['batches'][0];self.assertEqual(batch['state'],'failed');self.assertTrue(Path(batch['data']['workspace']).exists())
        self.assertNotIn('frozen',batch['data']);self.assertFalse((self.root/'state/effects').exists())


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
                    else:self.fail('service did not start')
                    self.assertTrue(health['ok']);self.assertEqual((root/'service.sock').stat().st_mode&0o777,0o600)
                    if attempt==0:
                        call(root,'POST','/project',{'id':'one','repository':directory,'remote':'example','target':'app','environment':'prod','adapter':['python3','adapter.py']})
                        call(root,'POST','/task',{'project':'one','id':'retained'})
                    else:self.assertEqual(call(root,'GET','/status')['tasks'][0]['id'],'retained')
                finally:proc.terminate();proc.communicate(timeout=15)

if __name__=='__main__':unittest.main()
