#!/usr/bin/env python3
"""Refusal tests for the GitHub app bridge, using the app's observed v1 envelope, never model prose as authority."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('codex_review', ROOT/'review/codex.py')
m = importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
SHA, OLD = 'a'*40, 'b'*40
NOW, LATER = '2026-01-01T00:00:00Z', '2026-01-01T00:01:00Z'

def comment(body, actions=False, id=1, created=NOW, updated=None):
    return {'_history_verified':True,'_review_versions':[],'id':id,'body':body,'created_at':created,'updated_at':updated or created,
            'user':{'id':41898282 if actions else 199175422,'login':'github-actions[bot]' if actions else 'chatgpt-codex-connector[bot]','type':'Bot'},
            'performed_via_github_app':{'id':15368 if actions else 1144995}}

def summary(sha=SHA, status='completed', any=False, unresolved=False, kind='code', updated=LATER, id=2, code_time=LATER):
    data={'code_review':{'commit_sha':None,'status':None,'has_any_findings':None,'has_unresolved_findings':None},
          'security_review':{'commit_sha':None,'status':None,'has_any_findings':None,'has_unresolved_findings':None}}
    data[kind+'_review']={'commit_sha':sha,'status':status,'has_any_findings':any,'has_unresolved_findings':unresolved}
    return comment(m.SUMMARY+'\n<!-- codex-review-state:v1 '+json.dumps(data)+' -->\n\nCodex Review Summary\n| 📝 **Code Review** | ✅ **Completed** <relative-time datetime="'+code_time+'">time</relative-time> | `'+sha[:7]+'` | Manual request |',updated=updated,id=id)

def receipt(sha=SHA[:10], created=LATER):
    return comment('<!-- codex-review-receipt:v1 -->\n\nCodex Review: No major issues.\n\n**Reviewed commit:** `'+sha+'`\n',id=3,created=created)


class Refusals(unittest.TestCase):
    def result(self, comments, kind='code'):return m.decision(comments,kind,SHA,m.stamp(NOW),require_start=False)
    def test_full_app_receipt_passes_and_short_sha_alone_never_does(self):
        self.assertEqual('pass',self.result([summary(),receipt()]))
        self.assertEqual('pending',self.result([receipt()]))
        self.assertEqual('pending',self.result([summary(sha=SHA[:10]),receipt()]))
        self.assertEqual('pending',self.result([summary(sha=OLD),receipt()]))
        self.assertEqual('pending',self.result([summary(),receipt(sha=OLD[:10])]))
    def test_immutable_app_and_bot_identity_both_required(self):
        for key,bad in [('user',{'id':199175422,'login':'chatgpt-codex-connector[bot]','type':'User'}),
                        ('user',{'id':42,'login':'chatgpt-codex-connector[bot]','type':'Bot'}),
                        ('performed_via_github_app',{'id':42}),('performed_via_github_app',None)]:
            c=summary();c[key]=bad
            self.assertEqual('pending',self.result([c,receipt()]),key)
            c=receipt();c[key]=bad
            self.assertEqual('pending',self.result([summary(),c]),key)
    def test_quotes_and_reactions_are_not_receipts(self):
        for body in ['👍','Looks good','Quoted:\n'+summary()['body'],'> '+summary()['body']]:
            self.assertEqual('pending',self.result([comment(body),receipt()]))
    def test_duplicate_or_unknown_machine_state_fails_closed(self):
        invalid=[summary()['body'].replace('"commit_sha":','"commit_sha":"'+OLD+'", "commit_sha":',1),
                 summary()['body'].replace('codex-review-state:v1','codex-review-state:v2'),
                 summary()['body'].replace('"has_any_findings": false','"has_any_findings": "false"',1),
                 summary()['body'].replace('"has_any_findings": false','"has_any_findings": NaN',1)]
        for body in invalid:
            with self.assertRaises(m.Refused):self.result([comment(body),receipt()])
    def test_any_findings_block_even_if_threads_were_resolved(self):
        for any,unresolved in [(True,True),(True,False),(False,True)]:
            self.assertEqual('findings',self.result([summary(any=any,unresolved=unresolved),receipt()]))
    def test_re_review_running_failed_or_newer_old_head_cannot_reuse_a_pass(self):
        for status in ['running','queued',None]:
            self.assertEqual('pending',self.result([summary(status=status),receipt()]))
        self.assertEqual('failed',self.result([summary(status='failed'),receipt()]))
        newer=summary(status='running',updated='2026-01-01T00:02:00Z',id=4)
        with self.assertRaises(m.Refused):self.result([summary(),receipt(),newer])
        newer=summary(sha=OLD,updated='2026-01-01T00:02:00Z',id=4)
        with self.assertRaises(m.Refused):self.result([summary(),receipt(),newer])
    def test_old_completed_review_does_not_satisfy_new_rubric_request(self):
        self.assertEqual('pending',self.result([summary(updated='2025-12-31T23:59:59Z',code_time='2025-12-31T23:59:59Z'),receipt()]))
        self.assertEqual('pending',self.result([summary(),receipt(created='2025-12-31T23:59:59Z')]))
        self.assertEqual('pending',self.result([summary()]))
    def test_security_update_and_receipt_cannot_freshen_an_old_code_review(self):
        old='2025-12-31T23:59:59Z'
        # Model a later Security Review updating the shared summary and posting a generic receipt.
        self.assertEqual('pending',self.result([summary(updated=LATER,code_time=old),receipt(created=LATER)]))
        missing=summary();missing['body']=missing['body'].split('\n|')[0]
        with self.assertRaises(m.Refused):self.result([missing,receipt()])
    def test_edited_intent_requires_an_authenticated_start_after_the_edit(self):
        clean=summary();fresh=receipt()
        self.assertEqual('pending',m.decision([clean,fresh],'code',SHA,m.stamp(NOW),require_start=True))
        running=summary(status='running',code_time=LATER)
        # Historical API snapshots carry the per-kind started time and full head binding.
        running['body']=running['body'].replace('✅ **Completed**','🔄 **Running** since')
        clean['_review_versions']=[{'body':running['body'],'at':LATER}]
        self.assertEqual('pass',m.decision([clean,fresh],'code',SHA,m.stamp(NOW),require_start=True))
        self.assertEqual('pending',m.decision([clean,fresh],'code',SHA,m.stamp('2026-01-01T00:02:00Z'),require_start=True))
        old_start=running['body'].replace(LATER,NOW)
        clean['_review_versions']=[{'body':old_start,'at':NOW}]
        # The body changed during review: completion/receipt are fresh but the reviewed input was old.
        self.assertEqual('pending',m.decision([clean,fresh],'code',SHA,m.stamp('2026-01-01T00:00:30Z'),require_start=True))
    def automatic_without_receipt(self):
        data=json.loads((ROOT/'test/review/fixtures/codex-automatic-no-receipt.json').read_text())
        c=comment(data['body']);c['_review_versions']=data['versions']
        return c,data['intent_since']
    def test_observed_automatic_review_passes_from_its_authenticated_full_head_start(self):
        c,since=self.automatic_without_receipt()
        self.assertFalse(m.fresh_receipt([c],SHA,since))
        self.assertEqual('pass',m.decision([c],'code',SHA,since,require_start=True))
    def test_receipt_free_completion_still_requires_a_fresh_authenticated_start(self):
        c,since=self.automatic_without_receipt()
        self.assertEqual('pending',m.decision([c],'code',SHA,m.code_started_at({'comment':c},SHA)+1,require_start=True))
        c['_review_versions']=[]
        self.assertEqual('pending',m.decision([c],'code',SHA,since,require_start=True))
        c,since=self.automatic_without_receipt();c['_history_verified']=False
        with self.assertRaises(m.Refused):m.decision([c],'code',SHA,since,require_start=True)
    def test_receipt_free_completion_cannot_borrow_an_old_head_or_clear_findings(self):
        c,since=self.automatic_without_receipt()
        c['_review_versions']=[{**v,'body':v['body'].replace(SHA,OLD)} for v in c['_review_versions']]
        self.assertEqual('pending',m.decision([c],'code',SHA,since,require_start=True))
        c,since=self.automatic_without_receipt()
        c['body']=c['body'].replace('"has_any_findings":false','"has_any_findings":true',1)
        self.assertEqual('findings',m.decision([c],'code',SHA,since,require_start=True))
    def test_an_edited_body_start_in_the_same_timestamp_second_is_ambiguous(self):
        clean=summary();running=summary(status='running',code_time='2026-01-01T00:00:00.500000Z')
        running['body']=running['body'].replace('✅ **Completed**','🔄 **Running** since')
        clean['_review_versions']=[{'body':running['body'],'at':NOW}]
        # GitHub's body-edit timestamp has whole seconds; this start could precede
        # an edit later in that same second. A fresh generic receipt cannot fix it.
        for evidence in [[clean],[clean,receipt()]]:
            self.assertEqual('pending',m.decision(evidence,'code',SHA,m.stamp(NOW),require_start=True))
        clean['_review_versions'][0]['body']=running['body'].replace('00:00:00.500000Z','00:00:01Z')
        self.assertEqual('pass',m.decision([clean],'code',SHA,m.stamp(NOW),require_start=True))
    def test_a_newer_review_start_cannot_borrow_an_older_completion(self):
        clean=summary();running=summary(status='running',code_time='2026-01-01T00:02:00Z')
        running['body']=running['body'].replace('✅ **Completed**','🔄 **Running** since')
        clean['_review_versions']=[{'body':running['body'],'at':'2026-01-01T00:02:00Z'}]
        self.assertEqual('pending',self.result([clean,receipt()]))
    def test_unverified_edits_cannot_authorize_a_summary(self):
        c=summary();c['_history_verified']=False
        with self.assertRaises(m.Refused):self.result([c,receipt()])
    def test_security_never_borrows_code_or_absence_of_findings_as_completion(self):
        self.assertEqual('pending',self.result([summary(),receipt()],kind='security'))
        self.assertEqual('pass',self.result([summary(kind='security')],kind='security'))
    def test_observed_nullable_security_flags_prove_only_advisory_scan_completion(self):
        fixture=json.loads((ROOT/'test/review/fixtures/codex-security-null-findings.json').read_text())
        c=comment(fixture['body'])
        result=m.decision([c],'security',SHA,m.stamp(NOW),repo='acme/project',pr=7)
        self.assertEqual('completed_findings_unknown',result)
        self.assertNotEqual('pass',result)
        # Prose and an absent finding list never provide this completion proof.
        with self.assertRaises(m.Refused):self.result([summary(kind='security',any=None,unresolved=None)],kind='security')
    def test_nullable_security_completion_still_requires_exact_context_and_typed_metadata(self):
        fixture=json.loads((ROOT/'test/review/fixtures/codex-security-null-findings.json').read_text())
        original=fixture['body']
        variants=[original.replace('codex-security-review:v1','codex-security-review:v2'),
                  original.replace('"headSha":"'+SHA+'"','"headSha":"'+OLD+'"'),
                  original.replace('"repository":"acme/project"','"repository":"other/project"'),
                  original.replace('"pullRequestNumber":7','"pullRequestNumber":8'),
                  original.replace('"mergeGateEnabled":false','"mergeGateEnabled":true'),
                  original.replace('"blockingSeverityThreshold":"P0"','"blockingSeverityThreshold":"unknown"')]
        for body in variants:
            with self.assertRaises(m.Refused):m.decision([comment(body)],'security',SHA,m.stamp(NOW),repo='acme/project',pr=7)
        with self.assertRaises(m.Refused):m.decision([comment(original)],'security',SHA,m.stamp(NOW))
    def test_null_flags_never_authorize_code_or_a_missing_or_partial_security_state(self):
        for kind in ['code','security']:
            for any,unresolved in [(None,False),(False,None)]+([(None,None)] if kind=='code' else []):
                with self.assertRaises(m.Refused):self.result([summary(kind=kind,any=any,unresolved=unresolved),receipt()],kind=kind)
        c=summary(kind='security',any=None,unresolved=None)
        c['body']=c['body'].replace('"has_any_findings": null, ','')
        with self.assertRaises(m.Refused):self.result([c],kind='security')
    def test_review_rules_must_be_exact_and_unique(self):
        self.assertTrue(m.has_rules('# Existing guidance\n'+m.rules_block()))
        for text in ['',m.rules_block().replace('Proof.', 'Proof is optional.'),m.rules_block()+'\n'+m.rules_block()]:
            self.assertFalse(m.has_rules(text))
    def test_trusted_base_rules_cannot_come_from_a_symlink_or_head(self):
        import base64
        tree={'truncated':False,'tree':[{'path':'AGENTS.md','mode':'100644','type':'blob','sha':SHA}]}
        blob={'encoding':'base64','size':len(m.rules_block()),'content':base64.b64encode(m.rules_block().encode()).decode()}
        api=m.GitHub('acme/project')
        with patch.object(api,'call',side_effect=[tree,blob]):api.review_rules(OLD)
        for mode in ['120000','160000']:
            bad=copy.deepcopy(tree);bad['tree'][0]['mode']=mode
            with patch.object(api,'call',return_value=bad),self.assertRaises(m.Refused):api.review_rules(OLD)
        with patch.object(api,'call',return_value={'truncated':False,'tree':[]}),self.assertRaises(m.Refused):api.review_rules(OLD)
    def test_patterns_brackets_and_protected_review_sources(self):
        self.assertTrue(m.agent('acme-worker[bot]','acme-*[bot]'))
        self.assertFalse(m.agent('acme-workerbot','acme-*[bot]'))
        self.assertFalse(m.agent('someone','*[bot]'))
        for p in ['.github/workflows/review.yml','AGENTS.md','pkg/AGENTS.md']:
            self.assertTrue(m.protected([p]))
        self.assertFalse(m.protected(['src/main.py']))
    def test_signal_and_tool_request_contract_cannot_be_waived_by_model_prose(self):
        p={'user':{'login':'acme-worker[bot]'},'body':'## Signal\n<!-- empty -->\n## Test\npassing'}
        self.assertIsNotNone(m.signal_problem(p,None))
        p['body']='## Signal\nhttps://github.com/acme/tools/issues/7\n'
        get=lambda *_:{'state':'open','labels':[{'name':'tool-request'}]}
        self.assertIsNotNone(m.signal_problem(p,get))
        p['user']['login']='worker-service'
        self.assertIsNotNone(m.signal_problem(p,get,binding=True))
        p['user']['login']='acme-worker[bot]'
        p['body']+='\nPart of acme/tools#7\n'
        self.assertIsNone(m.signal_problem(p,get))
        p['body']=p['body'].replace('Part of acme/tools#7','Part of acme/tools#70')
        self.assertIsNotNone(m.signal_problem(p,get))

class Approvals(unittest.TestCase):
    def test_configured_service_and_bot_reviews_are_not_human_approvals(self):
        api=m.GitHub('acme/project')
        for login,kind in [('worker-service','User'),('Worker-Service','User'),('other-bot','Bot')]:
            review={'id':1,'user':{'login':login,'type':kind},'state':'APPROVED','commit_id':SHA}
            with patch.dict(os.environ,{'AGENTS':'worker-*'}),patch.object(api,'call',return_value=[review]):
                self.assertFalse(api.approved(7,SHA))
    def test_exact_head_writer_approval_and_latest_changes_requested(self):
        api=m.GitHub('acme/project')
        owner={'id':1,'user':{'login':'owner','type':'User'},'state':'APPROVED','commit_id':SHA,'submitted_at':NOW}
        def call(path,pages=False):
            if path.startswith('pulls/'):return reviews
            return {'permission':permission}
        reviews=[owner];permission='write'
        with patch.dict(os.environ,{'AGENTS':'worker-*'}),patch.object(api,'call',call):
            self.assertTrue(api.approved(7,SHA))
            self.assertFalse(api.approved(7,OLD))
            permission='read';self.assertFalse(api.approved(7,SHA));permission='write'
            reviews.append({**owner,'id':2,'state':'CHANGES_REQUESTED','submitted_at':LATER})
            self.assertFalse(api.approved(7,SHA))

class FakeGitHub:
    moved=False;body_changed=False;intent_changed=False;missing_rules=False;approved_value=False;protected_value=False;markdown=False;reads=0;agent_pattern='acme-*[bot]';result='pass';author='acme-worker[bot]'
    def __init__(self,repo):self.pulls=0;self.intent_reads=0
    def pull(self,pr):
        self.pulls+=1;FakeGitHub.reads+=1
        return {'state':'open','head':{'sha':OLD if self.moved and self.pulls>=3 else SHA},'changed_files':1,'base':{'sha':'c'*40},
                'user':{'login':self.author},'body':'Changed intent' if self.body_changed and self.pulls>=3 else '## Signal\nThe owner requested this test.'}
    def files(self,*_):return ['.github/workflows/test.yml'] if self.protected_value else (['README.md'] if self.markdown else ['src/main.py'])
    def approved(self,*_):return self.approved_value
    def comments(self,*_):
        FakeGitHub.reads+=2
        return [] if self.result=='pending' else [summary(any=self.result=='findings'),receipt()]
    def review_rules(self,sha):
        if self.missing_rules:raise m.Refused('base rules missing')
    def intent_time(self,*_):
        self.intent_reads+=1
        return {'since':m.stamp('2026-01-01T00:00:30Z') if self.intent_changed and self.intent_reads>1 else m.stamp(NOW),'edited':self.intent_changed and self.intent_reads>1}
    def call(self,*_):raise AssertionError('Observer must never make unexpected writes')

class EditHistory(unittest.TestCase):
    def test_every_editor_must_be_the_app_even_if_the_latest_edit_is_legitimate(self):
        c=summary();c['node_id']='IC_test';c['updated_at']=LATER
        bot={'__typename':'Bot','databaseId':199175422}
        node={'id':'IC_test','body':c['body'],'updatedAt':LATER,'lastEditedAt':LATER,'editor':bot,'userContentEdits':{'totalCount':2}}
        edits=[{'diff':c['body'],'editedAt':LATER,'editor':bot},{'diff':c['body'],'editedAt':NOW,'editor':{'__typename':'User'}}]
        api=m.GitHub('acme/project')
        def query(q,variables):
            if 'nodes(ids' in q:return {'nodes':[node]}
            return {'node':{'userContentEdits':{'totalCount':2,'nodes':edits,'pageInfo':{'hasNextPage':False,'endCursor':None}}}}
        with patch.object(api,'graphql',query),self.assertRaisesRegex(m.Refused,'non-OpenAI'):api._comment_evidence([c])
        edits[1]['editor']=bot
        with patch.object(api,'graphql',query):verified=api._comment_evidence([c])
        self.assertTrue(verified[0]['_history_verified'])
        self.assertEqual(2,len(verified[0]['_review_versions']))
    def test_edited_body_cannot_be_matched_to_a_different_rest_snapshot(self):
        c=summary();c['node_id']='IC_test'
        node={'id':'IC_test','body':'altered','updatedAt':LATER}
        api=m.GitHub('acme/project')
        with patch.object(api,'graphql',return_value={'nodes':[node]}),self.assertRaises(m.ReviewChanged):api._comment_evidence([c])

class Lifecycle(unittest.TestCase):
    def run_main(self,wait="0",**kwargs):
        attrs={k:getattr(FakeGitHub,k) for k in kwargs}
        try:
            for k,v in kwargs.items():setattr(FakeGitHub,k,v)
            with patch.object(m,'GitHub',FakeGitHub),patch.dict(os.environ,{'REPO':'acme/project','PR':'7','SHA':SHA,'KIND':'code','WAIT':wait,'AGENTS':FakeGitHub.agent_pattern},clear=True):return m.main()
        finally:
            for k,v in attrs.items():setattr(FakeGitHub,k,v)
    def test_a_last_moment_push_cannot_get_a_passing_old_head_verdict(self):
        with self.assertRaisesRegex(m.Refused,'head moved'):self.run_main(moved=True)
    def test_unreviewed_missing_rules_cannot_be_waived_by_a_clean_model_receipt(self):
        with self.assertRaisesRegex(m.Refused,'base rules'):self.run_main(missing_rules=True)
    def test_edit_and_restore_cannot_hide_a_new_intent_timestamp(self):
        with self.assertRaises(m.Refused):self.run_main(intent_changed=True)
    def test_body_changes_cannot_reuse_an_intent_review(self):
        with self.assertRaisesRegex(m.Refused,'body changed'):self.run_main(body_changed=True)
    def test_agent_findings_block_and_human_responsibility_is_preserved(self):
        with self.assertRaises(m.Refused):self.run_main(result='findings')
        self.assertEqual(0,self.run_main(result='findings',author='someone'))
        self.assertEqual(0,self.run_main(result='findings',approved_value=True))
    def test_configured_service_accounts_do_not_take_human_shortcuts(self):
        with self.assertRaisesRegex(m.Refused,'human approval'):
            self.run_main(author='worker-service',agent_pattern='worker-*',protected_value=True)
        with self.assertRaisesRegex(m.Refused,'base rules'):
            self.run_main(author='worker-service',agent_pattern='worker-*',markdown=True,missing_rules=True)
    def test_fifteen_minute_wait_has_a_bounded_api_read_budget(self):
        clock=[0.0];FakeGitHub.reads=0
        def sleep(seconds):clock[0]+=seconds
        with patch.object(m.time,'monotonic',lambda:clock[0]),patch.object(m.time,'sleep',sleep):
            with self.assertRaises(m.Refused):self.run_main(wait='900',result='pending')
        self.assertLess(FakeGitHub.reads,80)
    def test_advisory_scan_completion_is_visible_without_claiming_a_clean_verdict(self):
        import contextlib, io
        fixture=json.loads((ROOT/'test/review/fixtures/codex-security-null-findings.json').read_text())
        class SecurityAPI(FakeGitHub):
            def comments(self,*_):return [comment(fixture['body'])]
        output=io.StringIO()
        with patch.object(m,'GitHub',SecurityAPI),patch.dict(os.environ,{'REPO':'acme/project','PR':'7','SHA':SHA,'KIND':'security','WAIT':'0','AGENTS':'acme-*[bot]'},clear=True),contextlib.redirect_stdout(output):
            self.assertEqual(0,m.main())
        self.assertIn('completed_findings_unknown',output.getvalue())
        self.assertIn('::warning::',output.getvalue())
        self.assertIn('No clean verdict is claimed.',output.getvalue())
    def test_workflow_and_review_rule_changes_need_current_head_human_approval(self):
        with self.assertRaisesRegex(m.Refused,'human approval'):self.run_main(protected_value=True)
        self.assertEqual(0,self.run_main(protected_value=True,approved_value=True))

class Routing(unittest.TestCase):
    def test_unrelated_comment_runs_cannot_cancel_a_filtered_app_refresh(self):
        for relative in ['adopt/templates/codex-review-results.yml','.github/workflows/codex-review-results.yml']:
            workflow=yaml.safe_load((ROOT/relative).read_text())
            self.assertNotIn('concurrency',workflow,'unfiltered workflow events must not cancel the trusted job')
            job=workflow['jobs']['refresh']
            self.assertIn('github.event.comment.user.id == 199175422',job['if'])
            self.assertIn('github.event.issue.pull_request',job['if'])
            self.assertIn('github.event.issue.number',job['concurrency']['group'])
            self.assertTrue(job['concurrency']['cancel-in-progress'])
    def test_codex_never_checks_out_or_executes_the_pr_or_requires_claude_secret(self):
        for relative in ['review/action.yml','review/security/action.yml']:
            a=yaml.safe_load((ROOT/relative).read_text())
            self.assertEqual('claude',a['inputs']['provider']['default'])
            self.assertFalse(a['inputs']['claude-code-oauth-token']['required'])
            for step in a['runs']['steps']:
                if step.get('name')=='validate review provider' or step.get('name','').startswith('gh ('):continue
                if step.get('name')=='Codex GitHub app review':
                    self.assertEqual("inputs.provider == 'codex'",step['if'])
                    self.assertNotIn('CLAUDE',str(step))
                else:self.assertTrue(step.get('if','').startswith("inputs.provider == 'claude'"),step.get('name') or step.get('id'))
            selector=a['runs']['steps'][0]
            import subprocess
            r=subprocess.run(['bash','-c',selector['run']],env={**os.environ,'PROVIDER':'invalid'},capture_output=True)
            self.assertNotEqual(0,r.returncode)

spec2=importlib.util.spec_from_file_location('codex_update',ROOT/'review/codex-rerun.py')
u=importlib.util.module_from_spec(spec2);spec2.loader.exec_module(u)

class UpdateAPI:
    moved=False;busy=False;dependent_busy=False;old_run=False;wrong_pr=False
    def pull(self,pr):return {'state':'open','draft':False,'head':{'sha':OLD if self.moved else SHA}}
    def run(self):
        return {'id':7,'head_sha':OLD if self.old_run else SHA,'event':'pull_request','path':'.github/workflows/claude-review.yml',
                'pull_requests':[{'number':8 if self.wrong_pr else 7}],'status':'in_progress' if self.busy else 'completed'}
    def call(self,path):
        if path.startswith('actions/workflows/'):return {'total_count':1,'workflow_runs':[self.run()]}
        if path=='actions/runs/7':return self.run()
        if path=='actions/runs/7/jobs?filter=latest&per_page=100':
            return {'total_count':3,'jobs':[{'id':10,'name':'claude-review','status':'completed'},
                    {'id':11,'name':'claude-security-review','status':'completed'},
                    {'id':12,'name':'claude-review-rows','status':'in_progress' if self.dependent_busy else 'completed'}]}
        raise AssertionError(path)

class Updates(unittest.TestCase):
    def event(self,c=None):return {'comment':c or summary(),'issue':{'number':7,'pull_request':{'url':'example'}}}
    def test_completed_workflow_refreshes_once_but_busy_and_old_heads_never_do(self):
        api=UpdateAPI();calls=[]
        u.update(api,self.event(),calls.append);self.assertEqual([7],calls)
        calls.clear();api.busy=True
        with self.assertRaises(u.b.Refused):u.update(api,self.event(),calls.append,wait_seconds=0)
        self.assertEqual([],calls)
        u.update(api,self.event(),calls.append,sleep=lambda _:setattr(api,'busy',False));self.assertEqual([7],calls);calls.clear()
        api.busy=False;api.moved=True;u.update(api,self.event(),calls.append);self.assertEqual([],calls)
        api.moved=False;api.old_run=True;u.update(api,self.event(),calls.append);self.assertEqual([],calls)
        api.old_run=False;api.wrong_pr=True;u.update(api,self.event(),calls.append);self.assertEqual([],calls)
    def test_dependent_row_job_must_finish_before_one_whole_workflow_rerun(self):
        api=UpdateAPI();api.dependent_busy=True;calls=[];waits=[]
        with self.assertRaises(u.b.Refused):u.update(api,self.event(),calls.append,wait_seconds=0)
        self.assertEqual([],calls)
        def sleep(seconds):
            self.assertEqual([],calls);waits.append(seconds);api.dependent_busy=False
        u.update(api,self.event(),calls.append,sleep=sleep)
        self.assertTrue(waits);self.assertEqual([7],calls)
    def test_a_concurrent_restart_keeps_the_event_until_a_rerun_is_accepted(self):
        api=UpdateAPI();calls=[];waits=[]
        def rerun(run_id):
            calls.append(run_id)
            if len(calls)==1:api.busy=True;return False
            return True
        def sleep(seconds):waits.append(seconds);api.busy=False
        u.update(api,self.event(),rerun,sleep=sleep)
        self.assertTrue(waits);self.assertEqual([7,7],calls)
    def test_waiting_for_the_full_workflow_still_checks_the_head(self):
        api=UpdateAPI();api.busy=True;calls=[]
        u.update(api,self.event(),calls.append,sleep=lambda _:setattr(api,'moved',True))
        self.assertEqual([],calls)
    def test_old_or_null_replacement_still_invalidates_current_head_rows(self):
        null_state=summary();null_state['body']=null_state['body'].replace('"commit_sha": '+json.dumps(SHA),'"commit_sha": null',1)
        for replacement in [summary(sha=OLD),null_state]:
            calls=[];u.update(UpdateAPI(),self.event(replacement),calls.append)
            self.assertEqual([7],calls)
    def test_security_and_deleted_receipts_invalidate_their_own_rows(self):
        calls=[];event={**self.event(summary(kind='security')),'action':'deleted'}
        u.update(UpdateAPI(),event,calls.append);self.assertEqual([7],calls)
        calls.clear();event['comment']=receipt();u.update(UpdateAPI(),event,calls.append);self.assertEqual([7],calls)
        calls.clear();event['comment'].pop('performed_via_github_app')
        self.assertFalse(m.trusted(event['comment']))
        u.update(UpdateAPI(),event,calls.append);self.assertEqual([7],calls)
        calls.clear();event['comment']['body']='removed header'
        u.update(UpdateAPI(),event,calls.append);self.assertEqual([7],calls)
        calls.clear();event['comment']['user']['id']=42
        u.update(UpdateAPI(),event,calls.append);self.assertEqual([],calls)

if __name__=='__main__':unittest.main()
