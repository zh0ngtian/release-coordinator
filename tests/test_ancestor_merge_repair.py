import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from test_coordinator import FAKE, VALID, git
from release_coordinator.engine import Worker
from release_coordinator.store import Conflict, Store


class AncestorMergeRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.store=Store(self.root/'state');self.repo=self.root/'repo';self.repo.mkdir()
        git(self.repo,'init','-b','main');git(self.repo,'config','user.name','Test')
        git(self.repo,'config','user.email','test@example.invalid');git(self.repo,'config','rerere.enabled','false')
        (self.repo/'file').write_text('base\n');git(self.repo,'add','.');git(self.repo,'commit','-m','base')
        self.base=git(self.repo,'rev-parse','HEAD');self.remote=self.root/'remote.git'
        subprocess.check_call(['git','clone','--bare',str(self.repo),str(self.remote)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        git(self.repo,'remote','add','origin',str(self.remote))
        self.adapter=self.root/'adapter.py';self.adapter.write_text(FAKE)
        self.config=self.store.add_project({'id':'test','repository':str(self.repo),'remote':str(self.remote),
            'target':'app','environment':'test','adapter':[sys.executable,str(self.adapter)],
            'phases':['publish','verify'],'options':{'lose_response':True}})

    def tearDown(self):self.temp.cleanup()

    def commit_branch(self,branch,start,filename,content):
        git(self.repo,'checkout','-b',branch,start);(self.repo/filename).write_text(content)
        git(self.repo,'add',filename);git(self.repo,'commit','-m',branch)
        sha=git(self.repo,'rev-parse','HEAD');git(self.repo,'push','origin',branch);git(self.repo,'checkout','main')
        return sha

    def advance_main(self):
        (self.repo/'file').write_text('current main\n');git(self.repo,'commit','-am','advance main')
        git(self.repo,'push','origin','main')
        return git(self.repo,'rev-parse','HEAD')

    def resolved_descendant(self,old_sha):
        git(self.repo,'checkout','-b','repair','main')
        merge=subprocess.run(['git','-C',str(self.repo),'merge','--no-ff','--no-commit',old_sha],capture_output=True,text=True)
        self.assertEqual(merge.returncode,1,merge.stdout+merge.stderr)
        self.assertIn('CONFLICT',merge.stdout)
        (self.repo/'file').write_text('resolved current main + original feature\n')
        git(self.repo,'add','file');git(self.repo,'commit','-m','resolve original task against current main')
        sha=git(self.repo,'rev-parse','HEAD');git(self.repo,'push','origin','repair');git(self.repo,'checkout','main')
        return sha

    def ready(self,task,sha,**kwargs):
        self.store.register('test',task,**kwargs);self.store.ready(task,sha,VALID)

    def run_success(self,batch_id=None):
        result=Worker(self.store,'test').run(batch_id)
        latest=self.store.snapshot('test')['batches'][-1]
        self.assertIsNotNone(result,latest['error']);self.assertEqual(latest['state'],'complete')
        return result

    def assert_sources(self,result,sources):
        self.assertEqual({task['id']:task['sha'] for task in result['tasks']},sources)
        manifest=json.loads((Path(result['workspace'])/'.release-batch.json').read_text())
        self.assertEqual(manifest['tasks'],result['tasks']);self.assertEqual(result['receipt']['tasks'],result['tasks'])
        self.assertEqual({task['id']:task['sha'] for task in result['frozen']['tasks']},sources)
        for sha in sources.values():
            self.assertEqual(subprocess.run(['git','-C',result['workspace'],'merge-base','--is-ancestor',sha,result['frozen']['sha']]).returncode,0)
        self.assertEqual((self.store.root/'effects'/(result['id']+'-publish.count')).read_text(),'1')

    def test_linked_repair_uses_descendant_resolution_and_preserves_lineage(self):
        old=self.commit_branch('feature',self.base,'file','original feature\n');self.advance_main()
        self.ready('original',old);self.assertIsNone(Worker(self.store,'test').run())
        parent=self.store.snapshot('test')['batches'][-1];self.assertEqual(parent['state'],'failed')
        work=Path(parent['data']['workspace']);retained=(git(work,'status','--porcelain'),git(work,'rev-parse','MERGE_HEAD'),(work/'file').read_bytes())
        self.assertIn('UU file',retained[0]);self.assertEqual(retained[1],old)
        with self.assertRaises(Conflict):self.store.resume(parent['id'])
        repair=self.resolved_descendant(old);self.ready('repair',repair,repairs_batch=parent['id'])
        result=self.run_success()
        self.assert_sources(result,{'original':old,'repair':repair})
        self.assertEqual((Path(result['workspace'])/'file').read_text(),'resolved current main + original feature\n')
        self.assertEqual(result['recoveryOf'],parent['id']);self.assertEqual(result['recoveryChain'],[parent['id']])
        self.assertEqual(result['receipt']['recoveryChain'],[parent['id']]);self.assertEqual(result['newTaskIds'],['repair'])
        self.assertNotIn('corrections',result);self.assertEqual(self.store.batch(parent['id']),parent)
        self.assertEqual((git(work,'status','--porcelain'),git(work,'rev-parse','MERGE_HEAD'),(work/'file').read_bytes()),retained)
        original=self.store.task('original');self.assertEqual(original['sha'],old)
        self.assertEqual(original['state'],'recovered');self.assertEqual(original['recovered_by'],result['id'])
        self.assertEqual(self.store.task('repair')['state'],'complete')

    def test_independent_heads_are_all_merged_with_descendant_resolution(self):
        old=self.commit_branch('feature',self.base,'file','original feature\n');self.advance_main()
        repair=self.resolved_descendant(old)
        peer=self.commit_branch('peer',self.base,'peer-file','independent feature\n')
        self.ready('original',old);self.ready('peer',peer);self.ready('repair',repair)
        result=self.run_success();self.assert_sources(result,{'original':old,'peer':peer,'repair':repair})
        self.assertEqual((Path(result['workspace'])/'peer-file').read_text(),'independent feature\n')
        self.assertEqual((Path(result['workspace'])/'file').read_text(),'resolved current main + original feature\n')

    def test_duplicate_shas_and_tasks_already_on_main_keep_full_audit(self):
        feature=self.commit_branch('feature',self.base,'feature-file','feature\n')
        self.ready('already-main',self.base);self.ready('original',feature);self.ready('duplicate',feature)
        result=self.run_success()
        self.assert_sources(result,{'already-main':self.base,'original':feature,'duplicate':feature})
        self.assertEqual((Path(result['workspace'])/'feature-file').read_text(),'feature\n')
        self.assertEqual(len(git(Path(result['workspace']),'rev-list','--merges',self.base+'..HEAD').splitlines()),1)

    def test_unresolved_independent_conflict_retains_worktree(self):
        first=self.commit_branch('first',self.base,'file','first\n')
        second=self.commit_branch('second',self.base,'file','second\n')
        self.ready('first',first);self.ready('second',second)
        self.assertIsNone(Worker(self.store,'test').run());failed=self.store.snapshot('test')['batches'][-1]
        self.assertEqual(failed['state'],'failed');self.assertNotIn('frozen',failed['data'])
        work=Path(failed['data']['workspace']);self.assertIn('UU file',git(work,'status','--porcelain'))
        self.assertEqual(git(work,'rev-parse','MERGE_HEAD'),second)
        self.assertIn('<<<<<<< HEAD',(work/'file').read_text());self.assertFalse((self.store.root/'effects').exists())

    def test_missing_remote_sha_still_fails_before_any_merge(self):
        feature=self.commit_branch('feature',self.base,'feature-file','feature\n')
        self.ready('feature',feature);self.ready('missing','0'*40)
        self.assertIsNone(Worker(self.store,'test').run());failed=self.store.snapshot('test')['batches'][-1]
        self.assertEqual(failed['state'],'failed');self.assertIn('not our ref '+'0'*40,failed['error'])
        work=Path(failed['data']['workspace']);self.assertEqual(git(work,'rev-parse','HEAD'),self.base)
        self.assertFalse((work/'feature-file').exists());self.assertFalse((self.store.root/'effects').exists())

    def test_interrupted_partial_integration_resumes_all_heads(self):
        first=self.commit_branch('first',self.base,'first-file','first\n')
        second=self.commit_branch('second',self.base,'second-file','second\n')
        self.ready('first',first);self.ready('second',second)
        owner='interrupted';epoch=self.store.claim(self.config['scope'],owner)
        batch=self.store.create_batch('test',owner,epoch);self.store.release(self.config['scope'],owner,epoch)
        work=Path(batch['workspace']);git(self.repo,'fetch','origin','main')
        git(self.repo,'worktree','add','--detach',str(work),'origin/main');git(work,'merge','--no-ff','--no-edit',first)
        self.store.resume(batch['id']);result=self.run_success(batch['id'])
        self.assert_sources(result,{'first':first,'second':second})
        self.assertEqual((work/'first-file').read_text(),'first\n');self.assertEqual((work/'second-file').read_text(),'second\n')

    def test_prepare_must_keep_all_task_ancestors(self):
        old=self.commit_branch('feature',self.base,'file','original feature\n');self.advance_main()
        repair=self.resolved_descendant(old)
        self.ready('original',old);self.ready('repair',repair)
        self.adapter.write_text(FAKE.replace("if a.action=='run':", "if a.action=='run':\n if a.phase=='prepare':\n  import subprocess\n  subprocess.check_call(['git','reset','--hard','origin/main'],stdout=subprocess.DEVNULL)"))
        self.assertIsNone(Worker(self.store,'test').run());failed=self.store.snapshot('test')['batches'][-1]
        self.assertEqual(failed['state'],'failed');self.assertNotIn('frozen',failed['data'])
        self.assertTrue((self.store.root/'effects'/(failed['id']+'-prepare.count')).exists())
        self.assertFalse((self.store.root/'effects'/(failed['id']+'-publish.count')).exists())


if __name__=='__main__':unittest.main()
