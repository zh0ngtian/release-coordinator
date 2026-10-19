"""真实 Git 与子进程副作用覆盖只读网络状态未知的恢复。"""
import contextlib
import io
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

import test_coordinator as fixtures
from test_coordinator import VALID
from release_coordinator.engine import Worker, Scheduler
from release_coordinator.store import Store, Conflict
from release_coordinator.cli import main


ADAPTER='''import argparse,json,sys
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('action');p.add_argument('phase');p.add_argument('--context');a=p.parse_args()
d=json.loads(Path(a.context).read_text());base=Path(a.context).parent.parent/'effects';base.mkdir(exist_ok=True)
mode=d['config']['options'].get('mode','normal');gate=base/'online';marker=base/(d['id']+'-'+a.phase+'.json')
def emit(value):print(json.dumps(value));sys.exit(0)
def unknown():emit({'done':False,'retryable':{'kind':'transient-read','reason':'remote read temporarily unavailable'}})
if a.phase=='prepare':
 if mode=='prepare-unknown':unknown()
 count=base/'prepare.count';count.write_text(str(int(count.read_text())+1) if count.exists() else '1')
 emit({'done':True,'result':{'version':'1.2.3','buildNumber':'42'}})
if a.action=='check':
 calls=base/(d['id']+'-'+a.phase+'.checks');calls.write_text(str(int(calls.read_text())+1) if calls.exists() else '1')
 if mode=='verify-unknown' and a.phase=='publish' and marker.exists() and (base/'omit-result').exists():emit({'done':True})
 if mode=='malformed':emit(json.loads((base/'response.json').read_text()))
 if (mode=='unknown' or (mode=='verify-unknown' and a.phase=='verify')) and not gate.exists():unknown()
 if marker.exists() and mode in ('after-run','structured-run','lost-success','lost-false') and not gate.exists():unknown()
 if mode=='completed-missing' and a.phase=='publish' and (base/'hide').exists():emit({'done':False})
 if mode=='lost-false' and marker.exists():emit({'done':False})
 emit({'done':marker.exists(),'result':json.loads(marker.read_text()) if marker.exists() else {}})
if mode=='structured-before-run' and not (base/'first-attempt').exists():
 (base/'first-attempt').write_text('yes');unknown()
if mode=='permanent':raise RuntimeError('deterministic build failure')
count=base/(d['id']+'-'+a.phase+'.count');count.write_text(str(int(count.read_text())+1) if count.exists() else '1')
result={'runId':d['id']+'-'+a.phase,'sha':d['frozen']['sha'],'version':d['frozen']['version']};marker.write_text(json.dumps(result))
Path(d['progressPath']).parent.mkdir(exist_ok=True);Path(d['progressPath']).write_text(json.dumps(result))
if mode in ('lost-success','lost-false'):raise RuntimeError('write response lost')
if mode=='structured-run' and not gate.exists():unknown()
emit({'done':True,'result':result})
'''


class NetworkRecoveryTests(unittest.TestCase):
    setUp=fixtures.WorkerTests.setUp
    tearDown=fixtures.WorkerTests.tearDown
    repository=fixtures.WorkerTests.repository

    def setup_project(self, mode, name='one', scope=None):
        self.now=getattr(self,'now',100.)
        self.store=Store(self.root/'state',lambda:self.now)
        self.adapter.write_text(ADAPTER)
        return self.repository(name,{'mode':mode},scope)

    def batch(self):return self.store.snapshot('one')['batches'][0]
    def effects(self):return self.store.root/'effects'
    def online(self): (self.effects()/'online').write_text('yes')
    def recover(self):
        row=self.batch();self.now=row['data']['recovery']['nextAttemptAt']
        return Worker(Store(self.store.root,lambda:self.now),'one').run(row['id'])
    def count(self, phase='publish'):
        path=self.effects()/(self.batch()['id']+'-'+phase+'.count')
        return int(path.read_text()) if path.exists() else 0

    def test_unknown_initial_check_waits_without_running_and_restarts_with_backoff(self):
        self.setup_project('unknown');Worker(self.store,'one').run();row=self.batch()
        self.assertEqual(row['state'],'recovering');self.assertEqual(self.count(),0)
        recovery=row['data']['recovery'];self.assertEqual(recovery['phase'],'publish')
        self.assertEqual(recovery['attempts'],1);self.assertEqual(recovery['firstFailureAt'],100)
        self.assertGreaterEqual(recovery['nextAttemptAt'],105);self.assertLess(recovery['nextAttemptAt'],107)
        restarted=Store(self.store.root,lambda:self.now);scheduler=Scheduler(restarted);scheduler.tick()
        self.assertFalse(scheduler.workers)
        self.assertIsNone(Worker(restarted,'one').run(row['id']));self.assertEqual(self.batch(),row)
        self.online();self.assertIsNotNone(self.recover());self.assertEqual(self.count(),1)
        self.assertEqual(self.batch()['data']['frozen'],row['data']['frozen'])
        self.assertEqual((self.effects()/'prepare.count').read_text(),'1')

    def test_run_then_unknown_query_recovers_same_external_operation_once(self):
        for mode in ('after-run','structured-run'):
            with self.subTest(mode=mode):
                if hasattr(self,'temp'):self.tearDown();self.setUp()
                self.setup_project(mode);Worker(self.store,'one').run();row=self.batch()
                self.assertEqual(row['state'],'recovering');self.assertEqual(self.count(),1)
                checkpoint=json.loads(Path(row['data']['progressPath']).read_text())
                self.online();result=self.recover();self.assertIsNotNone(result)
                self.assertEqual(result['id'],row['id']);self.assertEqual(result['frozen'],row['data']['frozen'])
                self.assertEqual(result['results']['publish'],checkpoint);self.assertEqual(self.count(),1)
                self.assertEqual((self.effects()/'prepare.count').read_text(),'1')

    def test_lost_write_response_keeps_original_error_until_known_success_or_failure(self):
        for mode in ('lost-success','lost-false'):
            with self.subTest(mode=mode):
                self.tearDown();self.setUp();self.setup_project(mode);Worker(self.store,'one').run();row=self.batch()
                self.assertEqual(row['state'],'recovering');self.assertEqual(self.count(),1)
                self.assertIn('write response lost',row['data']['pendingActionError']['error'])
                self.online();self.recover();final=self.batch();self.assertEqual(self.count(),1)
                self.assertEqual(final['state'],'complete' if mode=='lost-success' else 'failed')
                if mode=='lost-false':
                    self.assertIn('write response lost',final['error'])
                    with self.assertRaises(Conflict):self.store.resume(final['id'])

    def test_attention_after_fifteen_minutes_wait_returns_and_resume_rechecks_now(self):
        self.setup_project('unknown');Worker(self.store,'one').run();first=self.batch()
        self.now+=901;Worker(self.store,'one').run(first['id']);row=self.batch()
        self.assertEqual(row['state'],'recovering');self.assertTrue(row['data']['recovery']['attentionRequired'])
        scheduler=Scheduler(self.store);scheduler.tick();self.assertFalse(scheduler.workers)
        with patch('release_coordinator.cli.call',return_value=self.store.snapshot()),patch('release_coordinator.cli.time.sleep',side_effect=AssertionError('wait did not return')),contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(['wait','--task','one-task','--timeout','45']),0)
        self.assertTrue(json.loads(out.getvalue())['batch']['data']['recovery']['attentionRequired'])
        self.online();self.store.resume(row['id']);self.assertIsNotNone(Worker(self.store,'one').run(row['id']))

    def test_attention_is_observable_without_blocking_automatic_recovery(self):
        self.setup_project('unknown');Worker(self.store,'one').run();row=self.batch()
        self.now+=901;Worker(self.store,'one').run(row['id'])
        self.assertTrue(self.batch()['data']['recovery']['attentionRequired']);self.online()
        self.now=self.batch()['data']['recovery']['nextAttemptAt'];scheduler=Scheduler(Store(self.store.root,lambda:self.now))
        scheduler.tick()
        for thread,worker in scheduler.workers.values():thread.join(10)
        self.assertEqual(self.batch()['state'],'complete');self.assertEqual(self.count(),1)

    def test_recovering_scope_blocks_peers_but_other_scope_publishes(self):
        self.setup_project('unknown',scope='shared');self.repository('two',{'mode':'normal'},'shared');self.repository('three',{'mode':'normal'})
        Worker(self.store,'one').run();self.assertEqual(self.batch()['state'],'recovering')
        self.assertIsNone(self.store.due('two'));scheduler=Scheduler(self.store);scheduler.tick()
        for thread,worker in scheduler.workers.values():thread.join(10)
        self.assertEqual(self.store.snapshot('three')['batches'][0]['state'],'complete')
        self.assertFalse(self.store.snapshot('two')['batches']);self.assertEqual(self.count(),0)

    def test_completed_phase_is_reconciled_without_repeating_effect_when_missing(self):
        self.setup_project('completed-missing');result=Worker(self.store,'one').run();self.assertIsNotNone(result)
        # Model a crash after verified phases, before writing the final receipt.
        with self.store.tx() as db:db.execute("UPDATE batches SET state='running' WHERE id=?",(result['id'],))
        (self.effects()/'hide').write_text('yes');Worker(self.store,'one').run(result['id'])
        self.assertEqual(self.batch()['state'],'failed');self.assertEqual(self.count(),1)

    def test_deterministic_failure_stays_failed(self):
        self.setup_project('permanent');Worker(self.store,'one').run();row=self.batch()
        self.assertEqual(row['state'],'failed');self.assertIn('deterministic build failure',row['error'])
        with self.assertRaises(Conflict):self.store.resume(row['id'])

    def test_later_phase_failure_episode_survives_reconciliation_of_healthy_prior_phase(self):
        self.setup_project('verify-unknown');Worker(self.store,'one').run();first=self.batch()
        self.assertEqual(first['data']['recovery']['phase'],'verify');self.assertEqual(self.count(),1)
        for expected in (2,3,4,5,6):
            self.recover();recovery=self.batch()['data']['recovery']
            self.assertEqual(recovery['attempts'],expected);self.assertEqual(recovery['firstFailureAt'],100)
            delay=recovery['nextAttemptAt']-self.now
            self.assertGreaterEqual(delay,min(60,5*2**(expected-1)));self.assertLess(delay,61)
        self.now=1001;Worker(self.store,'one').run(first['id'])
        self.assertTrue(self.batch()['data']['recovery']['attentionRequired']);self.assertEqual(self.count(),1)
        self.online();self.store.resume(first['id']);self.assertIsNotNone(Worker(self.store,'one').run(first['id']))
        self.assertEqual(self.count(),1)

    def test_completed_evidence_survives_legacy_check_without_result(self):
        self.setup_project('verify-unknown');Worker(self.store,'one').run();first=self.batch()
        evidence=first['data']['results']['publish'];(self.effects()/'omit-result').write_text('yes')
        self.online();result=self.recover();self.assertIsNotNone(result)
        self.assertEqual(result['results']['publish'],evidence);self.assertEqual(self.count(),1)

    def test_structured_run_retry_can_fill_work_proven_missing_by_recovery_check(self):
        self.setup_project('structured-before-run');Worker(self.store,'one').run();row=self.batch()
        self.assertEqual(row['state'],'recovering');self.assertEqual(self.count(),0)
        self.assertIsNotNone(self.recover());self.assertEqual(self.count(),1)

    def test_prepare_is_outside_transient_read_recovery_contract(self):
        self.setup_project('prepare-unknown');Worker(self.store,'one').run();row=self.batch()
        self.assertEqual(row['state'],'failed');self.assertNotIn('frozen',row['data']);self.assertEqual(self.count(),0)

    def test_resume_cannot_overwrite_completion_or_failure_committed_after_its_snapshot(self):
        for final_state in ('complete','failed'):
            with self.subTest(final_state=final_state):
                self.tearDown();self.setUp();self.setup_project('unknown');Worker(self.store,'one').run();row=self.batch()
                batch_read=self.store.batch;errors=[];finished=[]
                def finish():
                    try:
                        other=Store(self.store.root,lambda:self.now);epoch=other.claim(row['scope'],'finisher')
                        if final_state=='complete':other.save(row['id'],'finisher',epoch,row['data'],'complete')
                        else:other.fail(row['id'],'finisher',epoch,'confirmed permanent failure')
                        other.release(row['scope'],'finisher',epoch);finished.append(other.batch(row['id']))
                    except BaseException as error:errors.append(error)
                def stale_snapshot(batch_id):
                    snapshot=batch_read(batch_id)
                    if not finished:
                        thread=threading.Thread(target=finish);thread.start();thread.join(5)
                        self.assertFalse(thread.is_alive());self.assertFalse(errors)
                    return snapshot
                with patch.object(self.store,'batch',side_effect=stale_snapshot):
                    try:self.store.resume(row['id'])
                    except Conflict:pass
                self.assertTrue(finished);self.assertEqual(batch_read(row['id']),finished[0])

    def test_protocol_is_strict_and_contradictions_fail_closed(self):
        self.setup_project('malformed');self.effects().mkdir(exist_ok=True)
        invalid=[{}, {'done':0}, {'done':True,'retryable':{'kind':'transient-read','reason':'x'}},
                 {'done':False,'retryable':{'kind':'other','reason':'x'}},
                 {'done':False,'retryable':{'kind':'transient-read','reason':''}},
                 {'done':False,'retryable':{'kind':'transient-read','reason':'x'},'result':{}},
                 {'done':True,'result':[]}, {'done':False,'retryable':{'kind':'transient-read','reason':'x','extra':1}},
                 {'done':False,'retryable':{'kind':'transient-read','reason':'x'},'extra':True}]
        for response in invalid:
            with self.subTest(response=response):
                (self.effects()/'response.json').write_text(json.dumps(response));Worker(self.store,'one').run()
                row=self.store.snapshot('one')['batches'][-1];self.assertEqual(row['state'],'failed');self.assertFalse((self.effects()/(row['id']+'-publish.count')).exists())
                if response!=invalid[-1]:
                    self.store.register('one','repair-'+str(len(self.store.snapshot()['batches'])),repairs_batch=row['id'])
                    self.store.ready('repair-'+str(len(self.store.snapshot()['batches'])),row['data']['tasks'][0]['sha'],VALID)

if __name__=='__main__':unittest.main()
