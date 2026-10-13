import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from test_coordinator import FAKE, VALID, git
from release_coordinator.cli import call
from release_coordinator.engine import Worker
from release_coordinator.store import Store, Conflict


class CorrectionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        self.store=Store(self.root/'state')
        self.repo=self.root/'repo'; self.repo.mkdir()
        git(self.repo,'init','-b','main'); git(self.repo,'config','user.name','Test'); git(self.repo,'config','user.email','test@example.invalid')
        (self.repo/'file').write_text('base'); git(self.repo,'add','.'); git(self.repo,'commit','-m','base')
        self.remote=self.root/'remote.git'
        subprocess.check_call(['git','clone','--bare',str(self.repo),str(self.remote)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        git(self.repo,'remote','add','origin',str(self.remote))
        git(self.repo,'checkout','-b','feature'); (self.repo/'file').write_text('feature'); git(self.repo,'commit','-am','feature')
        self.sha=git(self.repo,'rev-parse','HEAD'); git(self.repo,'push','origin','feature'); git(self.repo,'checkout','main')
        adapter=self.root/'adapter.py'; adapter.write_text(FAKE)
        self.config=self.store.add_project({'id':'test','repository':str(self.repo),'remote':str(self.remote),'target':'app','environment':'test',
            'adapter':[sys.executable,str(adapter)],'phases':['publish','verify']})
        self.missing='0'*40
        self.process=None

    def tearDown(self):
        if self.process:
            self.process.terminate(); self.process.communicate(timeout=15)
        self.temp.cleanup()

    def failed_chain(self):
        self.store.register('test','original'); self.store.ready('original',self.missing,VALID)
        Worker(self.store,'test').run()
        parent=self.store.snapshot()['batches'][-1]
        self.assertEqual(parent['state'],'failed'); self.assertIn('not our ref '+self.missing,parent['error'])
        self.store.register('test','ordinary-repair',repairs_batch=parent['id']); self.store.ready('ordinary-repair',self.sha,VALID)
        Worker(self.store,'test').run()
        child=self.store.snapshot()['batches'][-1]
        self.assertEqual(child['state'],'failed'); self.assertNotIn('frozen',child['data'])
        return parent,child

    def service(self):
        self.process=subprocess.Popen([sys.executable,'-B','-m','release_coordinator','--state-dir',str(self.store.root),'serve'],
            cwd=Path(__file__).resolve().parents[1],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        for _ in range(100):
            try: call(self.store.root,'GET','/health'); return
            except OSError: time.sleep(.03)
        self.fail('service did not start')

    def test_corrected_child_preserves_failed_records_and_recovers_all_tasks(self):
        parent,leaf=self.failed_chain()
        self.store.register('test','correction',repairs_batch=leaf['id'],corrects_task='original')
        self.store.ready('correction',self.sha,VALID)
        result=Worker(self.store,'test').run()
        self.assertIsNotNone(result, self.store.snapshot()['batches'][-1]['error'])
        self.assertEqual({t['id'] for t in result['tasks']},{'ordinary-repair','correction'})
        self.assertEqual(result['corrections'],[{'taskId':'original','originalSha':self.missing,'repairTaskId':'correction','replacementSha':self.sha}])
        self.assertEqual(result['receipt']['corrections'],result['corrections'])
        manifest=json.loads((Path(result['workspace'])/'.release-batch.json').read_text())
        self.assertEqual(manifest['corrections'],result['corrections'])
        self.assertEqual(self.store.batch(parent['id']),parent)
        self.assertEqual(self.store.batch(leaf['id']),leaf)
        self.assertEqual(self.store.task('original')['sha'],self.missing)
        for task in ('original','ordinary-repair'):
            self.assertEqual(self.store.task(task)['state'],'recovered')
            self.assertEqual(self.store.task(task)['recovered_by'],result['id'])
        self.assertEqual(self.store.task('correction')['state'],'complete')
        self.assertEqual((self.store.root/'effects'/(result['id']+'-publish.count')).read_text(),'1')

    def test_correction_requires_missing_commit_diagnostic_unfrozen_and_latest_leaf(self):
        parent,leaf=self.failed_chain()
        for kw in ({}, {'repairs_batch':parent['id']}, {'repairs_batch':leaf['id'],'corrects_task':'absent'}):
            with self.assertRaises(Conflict):
                self.store.register('test','bad',**({'corrects_task':'original',**kw}))
        with self.store.tx() as db:
            data=json.loads(leaf['data'] if isinstance(leaf['data'],str) else json.dumps(leaf['data']))
            data['frozen']={'sha':self.sha}
            db.execute('UPDATE batches SET data=? WHERE id=?',(json.dumps(data),leaf['id']))
        with self.assertRaises(Conflict): self.store.register('test','frozen',repairs_batch=leaf['id'],corrects_task='original')
        with self.store.tx() as db:
            db.execute('UPDATE batches SET data=?,error=? WHERE id=?',(json.dumps(leaf['data']),'network timeout',leaf['id']))
        with self.assertRaises(Conflict): self.store.register('test','network',repairs_batch=leaf['id'],corrects_task='original')

    def test_parallel_registration_and_lost_response_are_idempotent(self):
        _,leaf=self.failed_chain(); barrier=threading.Barrier(3); accepted=[]; rejected=[]
        def register(task):
            barrier.wait()
            try: accepted.append(Store(self.store.root).register('test',task,repairs_batch=leaf['id'],corrects_task='original')['id'])
            except Conflict: rejected.append(task)
        threads=[threading.Thread(target=register,args=(task,)) for task in ('correction-a','correction-b')]
        for thread in threads: thread.start()
        barrier.wait()
        for thread in threads: thread.join(5)
        self.assertEqual(len(accepted),1); self.assertEqual(len(rejected),1)
        old=self.store.register('test',accepted[0],repairs_batch=leaf['id'],corrects_task='original')
        self.assertEqual(Store(self.store.root).register('test',accepted[0],repairs_batch=leaf['id'],corrects_task='original'),old)
        with self.assertRaises(Conflict): self.store.register('test',accepted[0],repairs_batch=leaf['id'])

    def test_service_rejects_missing_and_unpushed_commits_before_ready(self):
        self.service()
        call(self.store.root,'POST','/task',{'project':'test','id':'pending'})
        for sha in (self.missing,'main'):
            with self.assertRaises(ValueError): call(self.store.root,'POST','/ready',{'id':'pending','sha':sha,'validation':VALID})
        self.assertEqual(self.store.task('pending')['state'],'active')
        self.assertIsNone(self.store.task('pending')['sha'])
        git(self.repo,'checkout','feature'); (self.repo/'file').write_text('unpushed'); git(self.repo,'commit','-am','unpushed')
        unpushed=git(self.repo,'rev-parse','HEAD')
        with self.assertRaises(ValueError): call(self.store.root,'POST','/ready',{'id':'pending','sha':unpushed,'validation':VALID})
        self.assertEqual(self.store.task('pending')['state'],'active')
        git(self.repo,'push','origin','feature')
        ready=call(self.store.root,'POST','/ready',{'id':'pending','sha':unpushed,'validation':VALID})
        self.assertEqual(ready['sha'],unpushed)

    def test_socket_correction_survives_restart_and_lost_ready_response(self):
        _,leaf=self.failed_chain(); self.service()
        payload={'project':'test','id':'correction','repairsBatch':leaf['id'],'correctsTask':'original'}
        task=call(self.store.root,'POST','/task',payload)
        self.assertEqual(task['corrects_task'],'original')
        self.assertEqual(call(self.store.root,'POST','/task',payload),task)
        self.process.terminate(); self.process.communicate(timeout=15); self.process=None
        self.assertEqual(Store(self.store.root).task('correction')['corrects_task'],'original')
        self.service()
        ready=call(self.store.root,'POST','/ready',{'id':'correction','sha':self.sha,'validation':VALID})
        # The worker can batch immediately after the response; retry must still be safe.
        repeated=call(self.store.root,'POST','/ready',{'id':'correction','sha':self.sha,'validation':VALID})
        self.assertEqual(repeated['sha'],ready['sha'])
        deadline=time.time()+10
        while time.time()<deadline:
            task=self.store.task('correction')
            if task['state']=='complete': break
            time.sleep(.05)
        self.assertEqual(task['state'],'complete')
        self.assertEqual(self.store.task('original')['state'],'recovered')

    def test_remote_original_that_becomes_available_cannot_be_omitted(self):
        parent,leaf=self.failed_chain()
        # A stale failure diagnostic is insufficient: the service must check
        # the remote again when accepting the replacement commit.
        with self.store.tx() as db:
            data=json.loads(json.dumps(leaf['data']))
            data['tasks'][0]['sha']=self.sha
            db.execute('UPDATE tasks SET sha=? WHERE id=?',(self.sha,'original'))
            db.execute('UPDATE batches SET data=?,error=? WHERE id=?',
                (json.dumps(data),'not our ref '+self.sha,leaf['id']))
        self.store.register('test','correction',repairs_batch=leaf['id'],corrects_task='original')
        self.service()
        with self.assertRaisesRegex(ValueError,'original remote commit exists'):
            call(self.store.root,'POST','/ready',{'id':'correction','sha':self.sha,'validation':VALID})
        self.assertEqual(self.store.task('correction')['state'],'active')

    def test_frozen_correction_audit_is_immutable_and_inherited_after_failure(self):
        _,leaf=self.failed_chain()
        self.store.register('test','correction',repairs_batch=leaf['id'],corrects_task='original')
        self.store.ready('correction',self.sha,VALID)
        result=Worker(self.store,'test').run()
        self.assertIsNotNone(result)
        scope=self.config['scope'];owner='audit';epoch=self.store.claim(scope,owner)
        changed=json.loads(json.dumps(result)); changed['corrections'][0]['originalSha']='1'*40
        with self.assertRaises(Conflict): self.store.save(result['id'],owner,epoch,changed,'frozen')
        self.store.fail(result['id'],owner,epoch,'later phase failed'); self.store.release(scope,owner,epoch)
        git(self.repo,'push','origin',result['frozen']['sha']+':refs/heads/main')
        self.store.register('test','later-repair',repairs_batch=result['id']); self.store.ready('later-repair',self.sha,VALID)
        repaired=Worker(self.store,'test').run()
        self.assertIsNotNone(repaired)
        self.assertEqual(repaired['corrections'],result['corrections'])
        self.assertEqual(self.store.task('original')['sha'],self.missing)
        self.assertEqual(self.store.task('original')['recovered_by'],repaired['id'])

    def test_service_rejects_tag_objects_and_remote_errors_without_state_change(self):
        self.service(); call(self.store.root,'POST','/task',{'project':'test','id':'pending'})
        git(self.repo,'tag','-a','v-test',self.sha,'-m','annotated'); git(self.repo,'push','origin','v-test')
        tag=git(self.repo,'rev-parse','v-test')
        with self.assertRaisesRegex(ValueError,'commit object'):
            call(self.store.root,'POST','/ready',{'id':'pending','sha':tag,'validation':VALID})
        self.assertEqual(self.store.task('pending')['state'],'active')
        self.remote.rename(self.root/'unavailable.git')
        with self.assertRaises(ValueError):
            call(self.store.root,'POST','/ready',{'id':'pending','sha':self.sha,'validation':VALID})
        self.assertEqual(self.store.task('pending')['state'],'active')

    def test_withdrawn_correction_cannot_reenter_after_another_is_registered(self):
        _,leaf=self.failed_chain()
        self.store.register('test','correction-a',repairs_batch=leaf['id'],corrects_task='original')
        self.store.touch('correction-a','withdrawn')
        self.store.register('test','correction-b',repairs_batch=leaf['id'],corrects_task='original')
        for state in ('active','closing','blocked'):
            with self.assertRaises(Conflict): self.store.touch('correction-a',state)
        with self.assertRaises(Conflict): self.store.ready('correction-a',self.sha,VALID)
        self.store.ready('correction-b',self.sha,VALID)
        result=Worker(self.store,'test').run()
        self.assertIsNotNone(result)
        self.assertEqual(self.store.task('correction-a')['state'],'withdrawn')
        self.assertEqual(self.store.task('correction-b')['state'],'complete')

    def test_accepted_batched_ready_retry_does_not_depend_on_remote_availability(self):
        _,leaf=self.failed_chain(); self.service()
        payload={'project':'test','id':'correction','repairsBatch':leaf['id'],'correctsTask':'original'}
        call(self.store.root,'POST','/task',payload)
        accepted={'id':'correction','sha':self.sha,'validation':VALID}
        call(self.store.root,'POST','/ready',accepted)
        deadline=time.time()+10
        while time.time()<deadline:
            task=self.store.task('correction')
            if task['state']=='complete': break
            time.sleep(.05)
        self.assertEqual(task['state'],'complete')
        self.remote.rename(self.root/'offline.git')
        repeated=call(self.store.root,'POST','/ready',accepted)
        self.assertEqual(repeated['batch'],task['batch'])
        with self.assertRaises(ValueError):
            call(self.store.root,'POST','/ready',{**accepted,'sha':'1'*40})

    def test_accepted_unbatched_ready_is_idempotent_but_withdrawn_requires_verification(self):
        self.service()
        call(self.store.root,'POST','/task',{'project':'test','id':'peer'})
        call(self.store.root,'POST','/task',{'project':'test','id':'pending'})
        payload={'id':'pending','sha':self.sha,'validation':VALID}
        first=call(self.store.root,'POST','/ready',payload)
        self.assertIsNone(first['batch'])
        self.remote.rename(self.root/'offline.git')
        self.assertEqual(call(self.store.root,'POST','/ready',payload),first)
        self.store.touch('pending','withdrawn')
        with self.assertRaises(ValueError): call(self.store.root,'POST','/ready',payload)
        self.assertEqual(self.store.task('pending')['state'],'withdrawn')


if __name__=='__main__': unittest.main()
