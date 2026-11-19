"""End-to-end API tests (FastAPI TestClient). Runs in CI on SQLite and on PostgreSQL:

    python -m unittest tests.test_api -v
    TEST_DATABASE_URL=postgresql://user:pw@localhost/db python -m unittest tests.test_api -v
"""
from pathlib import Path
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp = tempfile.mkdtemp(prefix='fl_test_')
os.environ.update({
    'ENVIRONMENT': 'test', 'JWT_SECRET': 'test-secret-0123456789-0123456789-abcdef', 'ADMIN_PASSWORD': 'Test-admin-password-1',
    'DATA_DIR': _tmp, 'LOGIN_IP_LIMIT': '10000', 'LOGIN_LIMIT': '8', 'REGISTER_LIMIT': '50', 'SEED_DEMO': 'false', 'DEMO_SHOW_CODES': 'true',
    'ALLOW_SELF_REGISTRATION': 'true', 'PORTAL_URL': 'https://portal.example',
    'DATABASE_URL': os.environ.get('TEST_DATABASE_URL') or f"sqlite:///{os.path.join(_tmp, 'test.db')}",
})
os.environ.pop('OPENAI_API_KEY', None)

try:
    from fastapi.testclient import TestClient
    import backend.app as M
    HAVE_STACK = True
except ImportError as exc:  # allows `unittest discover` to run on a machine without the web stack
    HAVE_STACK, _IMPORT_ERR = False, exc

PLAN = open(os.path.join(ROOT, 'demo_data', 'business_plan_demo.txt'), 'rb').read()
PW = 'Another-strong-pass-1'
IDEA = {'idea_title': 'Solar sorghum mills', 'problem': 'Diesel milling is costly for rural farmers.',
        'solution': 'Cooperative-owned solar mills with mobile payments.', 'target_users': 'Smallholder farmers',
        'sector': 'Agriculture', 'stage': 'Prototype'}


@unittest.skipUnless(HAVE_STACK, 'fastapi/sqlalchemy not installed')
class ApiScenario(unittest.TestCase):
    """One sequential scenario (methods run in name order and share state)."""
    ids: dict = {}

    @classmethod
    def setUpClass(cls):
        if os.environ.get('TEST_DATABASE_URL'):
            M.Base.metadata.drop_all(M.engine)
        cls._ctx = TestClient(M.app)
        cls.c = cls._ctx.__enter__()  # runs lifespan: init_db + seed

    @classmethod
    def tearDownClass(cls):
        cls._ctx.__exit__(None, None, None)

    # helpers
    def login(self, user, pw=PW):
        r = self.c.post('/api/token', data={'username': user, 'password': pw})
        self.assertEqual(r.status_code, 200, r.text)
        return {'Authorization': 'Bearer ' + r.json()['access_token']}

    def upload(self, headers, app_id, name='plan.txt', data=PLAN):
        return self.c.post(f'/api/applications/{app_id}/analyse-file', headers=headers, files={'file': (name, data, 'text/plain')})

    def decide(self, headers, decision, rationale='Reasoned decision for the record.', app_id=None):
        return self.c.post(f"/api/applications/{app_id or self.ids['app']}/decision", headers=headers,
                           json={'decision': decision, 'rationale': rationale})

    def user_id(self, username):
        return [u for u in self.c.get('/api/users', headers=self.ids['admin']).json() if u['username'] == username][0]['id']

    def assign(self, app_id, *usernames):
        return self.c.put(f'/api/applications/{app_id}/assignments', headers=self.ids['admin'],
                          json={'reviewer_ids': [self.user_id(n) for n in usernames]})

    # ---- 01 basics
    def test_01_health_and_headers(self):
        r = self.c.get('/api/health')
        self.assertEqual(r.json()['version'], open(os.path.join(ROOT, 'VERSION')).read().strip())
        self.assertEqual(self.c.get('/api/health/ready').status_code, 200)
        self.assertIn("script-src 'self'", r.headers['content-security-policy'])
        self.assertEqual(r.headers['x-frame-options'], 'DENY')
        self.assertEqual(self.c.get('/').status_code, 200)  # portal is served

    def test_02_auth(self):
        self.assertEqual(self.c.get('/api/applications').status_code, 401)
        self.assertEqual(self.c.post('/api/token', data={'username': 'admin', 'password': 'wrong'}).status_code, 401)
        ids = self.ids
        ids['admin'] = {'Authorization': 'Bearer ' + self.c.post('/api/token', data={'username': 'admin', 'password': 'Test-admin-password-1'}).json()['access_token']}
        self.assertEqual(self.c.get('/api/me', headers=ids['admin']).json()['role'], 'admin')

    def test_03_user_management(self):
        a = self.ids['admin']
        for name, role in (('officer1', 'officer'), ('committee1', 'committee'), ('applicant1', 'applicant'), ('applicant2', 'applicant')):
            r = self.c.post('/api/users', headers=a, json={'username': name, 'password': PW, 'role': role})
            self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.c.post('/api/users', headers=a, json={'username': 'OFFICER1', 'password': PW, 'role': 'officer'}).status_code, 409)
        self.assertEqual(self.c.post('/api/users', headers=a, json={'username': 'weak1', 'password': 'short', 'role': 'officer'}).status_code, 400)
        self.assertEqual(self.c.post('/api/users', headers=a, json={'username': 'bad1', 'password': PW, 'role': 'root'}).status_code, 422)
        for k, u in (('officer', 'officer1'), ('committee', 'committee1'), ('applicant', 'applicant1'), ('applicant2', 'applicant2')):
            self.ids[k] = self.login(u)
        self.assertEqual(self.c.get('/api/users', headers=self.ids['officer']).status_code, 403)
        self.assertEqual(self.c.get('/api/users', headers=self.ids['applicant']).status_code, 403)
        me = self.c.get('/api/me', headers=a).json()
        self.assertEqual(self.c.patch(f"/api/users/{me['id']}", headers=a, json={'active': False}).status_code, 400)

    # ---- 04 application, upload, assessment
    def test_04_applicant_flow_and_hidden_assessment(self):
        h = self.ids['applicant']
        prog = self.c.get('/api/programmes', headers=h).json()[0]['id']
        self.ids['prog'] = prog
        r = self.c.post('/api/applications', headers=h, json={'applicant_name': 'Acme Foods <b>x</b>', 'funding_request': 500000,
                                                              'owner_contribution': 100000, 'programme_id': prog, **IDEA})
        self.assertEqual(r.status_code, 200, r.text)
        self.ids['app'] = r.json()['id']
        chk = self.c.get(f"/api/applications/{self.ids['app']}/applicant-check", headers=h).json()
        self.assertFalse(chk['ready_for_officer_review'])  # no document yet
        r = self.upload(h, self.ids['app'])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['status'], 'received')
        self.assertNotIn('rubric', r.json())  # applicants never see the assessment
        got = self.c.get(f"/api/applications/{self.ids['app']}", headers=h).json()
        self.assertIsNone(got['assessment'])
        self.assertEqual(got['status'], 'under_review')
        self.assertEqual(self.c.get(f"/api/applications/{self.ids['app']}", headers=self.ids['applicant2']).status_code, 404)
        self.assertEqual(self.c.get(f"/api/applications/{self.ids['app']}/due-diligence", headers=h).status_code, 403)
        self.assertEqual(self.c.get('/api/applications', headers=self.ids['applicant2']).json()['total'], 0)
        # evaluators only see ideas assigned to them
        self.assertEqual(self.c.get(f"/api/applications/{self.ids['app']}", headers=self.ids['officer']).status_code, 404)
        self.assertEqual(self.c.get('/api/applications', headers=self.ids['officer']).json()['total'], 0)
        r = self.assign(self.ids['app'], 'officer1')
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual([x['username'] for x in r.json()], ['officer1'])
        self.assertEqual(self.c.get('/api/applications', headers=self.ids['officer']).json()['total'], 1)
        self.assertEqual(got['idea_title'], IDEA['idea_title'])
        self.assertEqual(got['problem'], IDEA['problem'])

    def test_05_officer_sees_assessment(self):
        r = self.c.get(f"/api/applications/{self.ids['app']}", headers=self.ids['officer']).json()
        a = r['assessment']
        self.assertEqual(a['metrics']['revenue'], 1200000)
        self.assertAlmostEqual(a['metrics']['margin'], 25.0)
        self.assertIn('revenue', a['needs_confirmation'])
        self.assertEqual(a['ai']['mode'], 'rules_only')
        self.assertTrue(a['rubric']['criteria'])
        self.assertEqual(r['allowed_actions'], ['request_information', 'refer_committee'])
        self.assertTrue(r['eligibility']['eligible'])
        dd = self.c.get(f"/api/applications/{self.ids['app']}/due-diligence", headers=self.ids['officer']).json()
        self.assertTrue(dd)
        q = dd[0]
        r = self.c.post(f"/api/applications/{self.ids['app']}/due-diligence/{q['id']}/answer", headers=self.ids['officer'],
                        json={'answer': 'Provided in annex 2', 'evidence_note': 'annex2.pdf'})
        self.assertEqual(r.json()['status'], 'answered')
        self.ids['answered_q'] = q['id']

    def test_06_reupload_keeps_answers_and_versions(self):
        r = self.upload(self.ids['officer'], self.ids['app'], data=PLAN + b'\nWe also have a licence and registration.\n')
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['assessment_version'], 2)
        dd = self.c.get(f"/api/applications/{self.ids['app']}/due-diligence", headers=self.ids['officer']).json()
        kept = [x for x in dd if x['id'] == self.ids['answered_q']][0]
        self.assertEqual(kept['status'], 'answered')  # v1.1 lost this state
        vs = self.c.get(f"/api/applications/{self.ids['app']}/versions", headers=self.ids['officer']).json()
        self.assertEqual([v['version'] for v in vs], [2, 1])
        self.assertEqual(self.c.get(f"/api/applications/{self.ids['app']}/versions/1", headers=self.ids['officer']).status_code, 200)
        docs = self.c.get(f"/api/applications/{self.ids['app']}", headers=self.ids['officer']).json()['documents']
        self.assertEqual([d['version'] for d in docs], [2, 1])

    def test_07_officer_confirms_figures(self):
        o = self.ids['officer']
        r = self.c.post(f"/api/applications/{self.ids['app']}/financials", headers=o, json={'revenue': 2000000, 'expenses': 1000000})
        self.assertEqual(r.status_code, 200, r.text)
        j = r.json()
        self.assertEqual(j['metrics']['revenue'], 2000000)
        self.assertEqual(j['sources']['revenue']['origin'], 'officer_override')
        self.assertEqual(j['assessment_version'], 3)
        self.assertEqual(self.c.post(f"/api/applications/{self.ids['app']}/financials", headers=self.ids['committee'],
                                     json={'revenue': 1}).status_code, 403)
        self.assertEqual(self.c.post(f"/api/applications/{self.ids['app']}/financials", headers=o, json={'revenue': -5}).status_code, 422)

    def test_08_upload_validation(self):
        o, a = self.ids['officer'], self.ids['app']
        self.assertEqual(self.upload(o, a, 'evil.exe', b'MZ' + b'x' * 100).status_code, 415)
        self.assertEqual(self.upload(o, a, 'fake.pdf', b'not a pdf at all' * 10).status_code, 415)
        self.assertEqual(self.upload(o, a, 'tiny.txt', b'too short').status_code, 422)
        self.assertEqual(self.upload(o, a, 'empty.txt', b'').status_code, 400)

    # ---- 08a human scoring (runs before the referral in test_09)
    def test_08a_assignment_and_scoring(self):
        app_id, o, c, ad = self.ids['app'], self.ids['officer'], self.ids['committee'], self.ids['admin']
        path = f'/api/applications/{app_id}/scores'
        self.assertEqual(self.c.get(path, headers=self.ids['applicant']).status_code, 403)
        crit = self.c.get('/api/score-criteria', headers=o).json()
        self.assertEqual(len(crit), 5)
        self.assertEqual(self.c.get(f'/api/applications/{app_id}', headers=o).json()['can_score'], True)
        values = {'innovation': 5, 'feasibility': 4, 'impact': 3, 'team': 4, 'alignment': 5}  # weights 25/25/25/15/10 -> 82.0
        body = {'scores': [{'criterion_id': x['id'], 'score': values[x['key']], 'comment': 'ok'} for x in crit]}
        self.assertEqual(self.c.put(path, headers=c, json=body).status_code, 409)  # adjudicators score after referral
        self.assertEqual(self.c.put(path, headers=o, json={'scores': [{'criterion_id': crit[0]['id'], 'score': 6}]}).status_code, 422)
        self.assertEqual(self.c.put(path, headers=o, json={'scores': [{'criterion_id': 'nope', 'score': 3}]}).status_code, 422)
        r = self.c.put(path, headers=o, json=body)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['my_total'], 82.0)
        self.assertIsNone(r.json()['panel'])
        # a second evaluator, not yet assigned, cannot see or score the idea
        self.assertEqual(self.c.post('/api/users', headers=ad, json={'username': 'officer2', 'password': PW, 'role': 'officer'}).status_code, 200)
        o2 = self.login('officer2')
        self.assertEqual(self.c.put(path, headers=o2, json=body).status_code, 404)
        self.assertEqual(self.assign(app_id, 'committee1').status_code, 422)  # only evaluators can be assigned
        self.assertEqual(self.c.put(f'/api/applications/{app_id}/assignments', headers=o, json={'reviewer_ids': []}).status_code, 403)
        r = self.assign(app_id, 'officer1', 'officer2')
        self.assertEqual(sorted(x['username'] for x in r.json()), ['officer1', 'officer2'])
        low = {'scores': [{'criterion_id': x['id'], 'score': 2} for x in crit]}  # -> 40.0
        r = self.c.put(path, headers=o2, json=low)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['my_total'], 40.0)
        mine = self.c.get(path, headers=o).json()  # independent scoring: evaluators never see each other
        self.assertIsNone(mine['panel'])
        self.assertEqual(mine['my_total'], 82.0)
        panel = self.c.get(path, headers=ad).json()['panel']
        self.assertEqual(len(panel['reviewers']), 2)
        self.assertEqual(panel['average_total'], 61.0)
        self.assertEqual(panel['spread'], 42.0)
        self.assertTrue(panel['disagreement'])
        self.assertTrue(all(x['has_scored'] for x in self.c.get(f'/api/applications/{app_id}/assignments', headers=ad).json()))
        # admin configures criteria; evaluators cannot
        new = {'key': 'sustainability', 'label': 'Sustainability', 'weight': 5}
        self.assertEqual(self.c.post('/api/score-criteria', headers=o, json=new).status_code, 403)
        made = self.c.post('/api/score-criteria', headers=ad, json=new)
        self.assertEqual(made.status_code, 200, made.text)
        self.assertEqual(self.c.post('/api/score-criteria', headers=ad, json=new).status_code, 409)
        self.assertEqual(self.c.put(f"/api/score-criteria/{made.json()['id']}", headers=ad, json={**new, 'enabled': False}).status_code, 200)
        self.assertEqual(len(self.c.get(path, headers=ad).json()['criteria']), 5)

    # ---- 09 workflow
    def test_09_workflow_state_machine(self):
        o, c, ad = self.ids['officer'], self.ids['committee'], self.ids['admin']
        self.assertEqual(self.decide(c, 'approve').status_code, 409)         # right role, wrong state: must be referred first
        self.assertEqual(self.decide(c, 'refer_committee').status_code, 403)  # committee cannot refer
        self.assertEqual(self.decide(o, 'approve').status_code, 403)         # officer cannot approve
        short = self.c.post(f"/api/applications/{self.ids['app']}/decision", headers=o, json={'decision': 'refer_committee', 'rationale': 'short'})
        self.assertEqual(short.status_code, 422)
        self.assertEqual(self.decide(o, 'banana').status_code, 400)
        r = self.decide(o, 'refer_committee')
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['assessment_version'], 3)
        self.assertEqual(self.c.post(f"/api/applications/{self.ids['app']}/financials", headers=o, json={'revenue': 1}).status_code, 409)
        self.assertEqual(self.upload(o, self.ids['app']).status_code, 409)   # locked while with committee
        # committee sees it and gets exactly approve/decline/request_information
        acts = self.c.get(f"/api/applications/{self.ids['app']}", headers=c).json()['allowed_actions']
        self.assertEqual(sorted(acts), ['approve', 'decline', 'request_information'])
        self.assertEqual(self.decide(o, 'decline').status_code, 403)
        r = self.decide(c, 'approve', 'Approved subject to conditions in the file.')
        self.assertEqual(r.status_code, 200, r.text)
        recs = self.c.get(f"/api/applications/{self.ids['app']}/decisions", headers=o).json()
        self.assertEqual([x['decision'] for x in recs], ['approve', 'refer_committee'])  # v1.1 never wrote this record
        self.assertEqual(recs[0]['decided_by'], 'committee1')
        self.assertEqual(self.decide(c, 'decline').status_code, 409)         # already final

    def test_10_separation_of_duties(self):
        a, ad = self.ids['applicant2'], self.ids['admin']
        app_id = self.c.post('/api/applications', headers=a, json={'applicant_name': 'Beta Traders', 'funding_request': 100000,
                                                                   'owner_contribution': 20000, **IDEA}).json()['id']
        self.assertEqual(self.upload(a, app_id).status_code, 200)
        r = self.c.post(f'/api/applications/{app_id}/decision', headers=ad, json={'decision': 'refer_committee', 'rationale': 'Referring for committee review.'})
        self.assertEqual(r.status_code, 200, r.text)
        r = self.c.post(f'/api/applications/{app_id}/decision', headers=ad, json={'decision': 'approve', 'rationale': 'Approving my own referral.'})
        self.assertEqual(r.status_code, 403)
        self.assertIn('separation', r.json()['detail'].lower())
        self.assertNotIn('approve', self.c.get(f'/api/applications/{app_id}', headers=ad).json()['allowed_actions'])

    # ---- 11 reports
    def test_11_reports_require_auth_and_escape(self):
        path = f"/api/applications/{self.ids['app']}/committee-report"
        self.assertEqual(self.c.get(path).status_code, 401)
        self.assertEqual(self.c.get(path, headers=self.ids['applicant']).status_code, 403)
        r = self.c.get(path, headers=self.ids['committee'])
        self.assertEqual(r.status_code, 200)
        self.assertIn('Acme Foods &lt;b&gt;x&lt;/b&gt;', r.text)
        self.assertNotIn('<b>x</b>', r.text)
        self.assertIn('approve', r.text)
        d = self.c.get(path + '.docx', headers=self.ids['committee'])
        self.assertEqual(d.status_code, 200)
        self.assertEqual(d.content[:2], b'PK')
        self.assertFalse(any(f.startswith('committee_report_') for f in os.listdir(_tmp)))  # nothing written to disk

    # ---- 12 standards
    def test_12_rubric_admin_controls_analysis(self):
        ad, o = self.ids['admin'], self.ids['officer']
        rows = self.c.get('/api/rubric', headers=o).json()
        margin = [x for x in rows if x['key'] == 'operating_margin'][0]
        self.assertEqual(self.c.put(f"/api/rubric/{margin['id']}", headers=o, json=margin).status_code, 403)
        body = {k: v for k, v in margin.items() if k != 'id'}
        body['enabled'] = False
        self.assertEqual(self.c.put(f"/api/rubric/{margin['id']}", headers=ad, json=body).status_code, 200)
        app_id = self.c.post('/api/applications', headers=o, json={'applicant_name': 'Gamma Ltd', 'funding_request': 100000,
                                                                   'owner_contribution': 50000, 'programme_id': self.ids['prog']}).json()['id']
        low = b'Annual revenue: BWP 1,000,000\nAnnual expenses: BWP 950,000\nNo market research has been done.\n'
        res = self.upload(o, app_id, data=low).json()
        keys = [c['key'] for c in res['rubric']['criteria']]
        self.assertNotIn('operating_margin', keys)  # a disabled criterion is really disabled (v1.1 still fired it)
        self.assertFalse(any('Operating margin' in r['title'] for r in res['risks']))
        market = [c for c in res['rubric']['criteria'] if c['key'] == 'market_demand'][0]
        self.assertEqual(market['status'], 'negated')  # v1.1 said "supported"
        self.assertTrue(any('Mandatory criterion not evidenced' in r['title'] for r in res['risks']))
        body['enabled'], body['threshold'] = True, 20.0
        self.assertEqual(self.c.put(f"/api/rubric/{margin['id']}", headers=ad, json=body).status_code, 200)
        # validation
        bad = {'key': 'x1', 'label': 'Bad', 'kind': 'numeric', 'metric': 'nonsense', 'comparator': '>=', 'threshold': 1}
        self.assertEqual(self.c.post('/api/rubric', headers=ad, json=bad).status_code, 422)
        ok = {'key': 'has_website', 'label': 'Website', 'kind': 'evidence', 'keywords': ['website'], 'weight': 1}
        self.assertEqual(self.c.post('/api/rubric', headers=ad, json=ok).status_code, 200)
        self.assertEqual(self.c.post('/api/rubric', headers=ad, json=ok).status_code, 409)

    # ---- 13 tenancy
    def test_13_tenant_isolation(self):
        with M.SessionLocal() as s:
            inst = M.Institution(id=M.new_id(), name='Other Funder', code='OTHER')
            s.add(inst)
            s.flush()
            s.add(M.User(username='other_officer', password_hash=M.hash_password(PW), role='officer', institution_id=inst.id))
            s.commit()
        h = self.login('other_officer')
        self.assertEqual(self.c.get('/api/applications', headers=h).json()['total'], 0)
        self.assertEqual(self.c.get(f"/api/applications/{self.ids['app']}", headers=h).status_code, 404)
        self.assertEqual(self.c.get(f"/api/applications/{self.ids['app']}/committee-report", headers=h).status_code, 404)
        self.assertEqual(self.c.get('/api/rubric', headers=h).json(), [])

    # ---- 14 audit
    def test_14_audit_chain(self):
        ad = self.ids['admin']
        self.assertTrue(self.c.get('/api/audit/verify', headers=ad).json()['ok'])
        rows = self.c.get('/api/audit', headers=ad).json()
        self.assertTrue(any(r['action'] == 'human_decision' for r in rows))
        self.assertTrue(any(r['action'] == 'login' for r in rows))
        self.assertEqual(self.c.get('/api/audit', headers=self.ids['officer']).status_code, 403)
        with M.SessionLocal() as s:
            row = s.scalar(M.select(M.AuditLog).where(M.AuditLog.action == 'human_decision'))
            original, rid = row.detail, row.id
            row.detail = original + ' tampered'
            s.commit()
        bad = self.c.get('/api/audit/verify', headers=ad).json()
        self.assertFalse(bad['ok'])
        self.assertEqual(bad['first_bad_id'], rid)
        with M.SessionLocal() as s:
            s.get(M.AuditLog, rid).detail = original
            s.commit()
        self.assertTrue(self.c.get('/api/audit/verify', headers=ad).json()['ok'])

    # ---- 15 security
    def test_15_password_change_revokes_old_tokens(self):
        old = self.login('applicant2')
        r = self.c.post('/api/me/password', headers=old, json={'current_password': PW, 'new_password': 'Brand-new-password-9'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.c.get('/api/me', headers=old).status_code, 401)
        new = {'Authorization': 'Bearer ' + r.json()['access_token']}
        self.assertEqual(self.c.get('/api/me', headers=new).status_code, 200)
        self.assertEqual(self.c.post('/api/me/password', headers=new, json={'current_password': 'wrong-wrong-1', 'new_password': 'Whatever-password-1'}).status_code, 400)

    def test_16_deactivated_user_loses_access(self):
        ad = self.ids['admin']
        uid = [u for u in self.c.get('/api/users', headers=ad).json() if u['username'] == 'applicant1'][0]['id']
        self.assertEqual(self.c.patch(f'/api/users/{uid}', headers=ad, json={'active': False}).status_code, 200)
        self.assertEqual(self.c.get('/api/me', headers=self.ids['applicant']).status_code, 401)
        self.assertEqual(self.c.post('/api/token', data={'username': 'applicant1', 'password': PW}).status_code, 401)

    def test_17_ai_is_opt_in(self):
        ad = self.ids['admin']
        self.assertFalse(self.c.get('/api/institution', headers=ad).json()['ai_enabled'])
        self.assertEqual(self.c.put('/api/institution', headers=self.ids['officer'], json={'ai_enabled': True}).status_code, 403)
        self.assertTrue(self.c.put('/api/institution', headers=ad, json={'ai_enabled': True}).json()['ai_enabled'])
        self.c.put('/api/institution', headers=ad, json={'ai_enabled': False})

    def test_18_pagination_and_dashboard(self):
        o, ad = self.ids['officer'], self.ids['admin']
        r = self.c.get('/api/applications?limit=1&offset=0', headers=ad).json()
        self.assertEqual(len(r['items']), 1)
        self.assertGreaterEqual(r['total'], 3)
        self.assertEqual(self.c.get('/api/applications?limit=1000', headers=o).status_code, 422)
        d = self.c.get('/api/management/dashboard', headers=o).json()
        self.assertGreaterEqual(d['applications'], 3)
        self.assertIn('approve', d['status'])
        p = self.c.get('/api/pilot/summary', headers=o).json()
        self.assertGreaterEqual(p['final_decisions'], 1)

    def test_19_login_rate_limit(self):
        codes = [self.c.post('/api/token', data={'username': 'ghost_user', 'password': 'x'}).status_code for _ in range(10)]
        self.assertEqual(codes[:8], [401] * 8)
        self.assertEqual(codes[-1], 429)

    def test_20_input_validation(self):
        o = self.ids['officer']
        for body in ({'applicant_name': 'A', 'funding_request': 1, 'owner_contribution': 1},
                     {'applicant_name': 'Valid Name', 'funding_request': -1, 'owner_contribution': 1},
                     {'applicant_name': 'Valid Name', 'funding_request': 1e15, 'owner_contribution': 1}):
            self.assertEqual(self.c.post('/api/applications', headers=o, json=body).status_code, 422, body)


    # ---- 21 innovation idea: mandatory fields, editing, send back with feedback, resubmit, timeline
    def test_21_idea_lifecycle_and_timeline(self):
        ad, o = self.ids['admin'], self.ids['officer']
        self.assertEqual(self.c.post('/api/users', headers=ad, json={'username': 'submitter3', 'password': PW, 'role': 'applicant'}).status_code, 200)
        h = self.login('submitter3')
        base = {'applicant_name': 'Kgosi Innovations', 'funding_request': 0, 'owner_contribution': 0}
        r = self.c.post('/api/applications', headers=h, json={**base, 'idea_title': 'Only a title'})
        self.assertEqual(r.status_code, 422)
        self.assertIn('Problem', r.json()['detail'])
        app_id = self.c.post('/api/applications', headers=h, json={**base, **IDEA}).json()['id']
        edited = {**IDEA, 'idea_title': 'Solar sorghum mills v2'}
        self.assertEqual(self.c.put(f'/api/applications/{app_id}/idea', headers=h, json=edited).status_code, 200)
        self.assertEqual(self.c.put(f'/api/applications/{app_id}/idea', headers=h, json={**IDEA, 'problem': ''}).status_code, 422)
        self.assertEqual(self.upload(h, app_id).status_code, 200)
        self.assertEqual(self.c.put(f'/api/applications/{app_id}/idea', headers=h, json=edited).status_code, 409)  # locked in review
        self.assertEqual(self.c.put(f'/api/applications/{app_id}/idea', headers=self.ids['applicant2'], json=edited).status_code, 401)
        self.assign(app_id, 'officer1')
        feedback = 'Please add a budget and describe your team.'
        r = self.decide(o, 'request_information', feedback, app_id=app_id)
        self.assertEqual(r.status_code, 200, r.text)
        got = self.c.get(f'/api/applications/{app_id}', headers=h).json()
        self.assertEqual(got['status'], 'request_information')
        self.assertTrue(got['can_edit'])
        tl = self.c.get(f'/api/applications/{app_id}/timeline', headers=h).json()
        self.assertTrue(any(e.get('feedback') == feedback for e in tl['events']))
        self.assertEqual(self.c.put(f'/api/applications/{app_id}/idea', headers=h, json=edited).status_code, 200)
        self.assertEqual(self.upload(h, app_id).status_code, 200)
        tl = self.c.get(f'/api/applications/{app_id}/timeline', headers=h).json()
        self.assertEqual(tl['status'], 'under_review')
        self.assertEqual(sum(1 for e in tl['events'] if e['kind'] == 'submitted'), 2)
        self.assertEqual(self.c.get(f'/api/applications/{app_id}/timeline', headers=self.login('officer2')).status_code, 404)

    # ---- 22 browsers always revalidate the page, styles and script
    def test_22_static_files_are_revalidated(self):
        for path in ('/', '/styles.css', '/app.js'):
            r = self.c.get(path)
            self.assertEqual(r.status_code, 200, path)
            self.assertEqual(r.headers.get('cache-control'), 'no-cache', path)
        page = self.c.get('/').text
        version = (Path(__file__).resolve().parents[1] / 'VERSION').read_text().strip()
        self.assertIn(f'styles.css?v={version}', page, 'bump the ?v= in index.html when VERSION changes')
        self.assertIn(f'app.js?v={version}', page, 'bump the ?v= in index.html when VERSION changes')
    # ---- 23 self-registration: account is locked until the emailed code is confirmed
    def test_23_register_and_verify(self):
        body = {'full_name': 'Neo Molefe', 'username': 'neo.m', 'email': 'Neo@Example.com', 'phone': '71 234 567',
                'password': PW, 'accept_privacy': True}
        self.assertEqual(self.c.post('/api/auth/register', json={**body, 'accept_privacy': False}).status_code, 422)
        self.assertEqual(self.c.post('/api/auth/register', json={**body, 'email': 'not-an-email'}).status_code, 422)
        self.assertEqual(self.c.post('/api/auth/register', json={**body, 'password': 'short'}).status_code, 400)
        r = self.c.post('/api/auth/register', json=body)
        self.assertEqual(r.status_code, 200, r.text)
        j = r.json()
        self.assertEqual(j['sent_to'], 'n***@example.com')
        code = j['demo_code']
        self.assertRegex(code, r'^\d{6}$')
        self.assertEqual(self.c.post('/api/auth/register', json={**body, 'username': 'neo2'}).status_code, 409)  # same email
        self.assertEqual(self.c.post('/api/auth/register', json={**body, 'email': 'other@example.com'}).status_code, 409)  # same username
        # cannot sign in yet; a fresh code comes back with the refusal
        r = self.c.post('/api/token', data={'username': 'neo.m', 'password': PW})
        self.assertEqual(r.status_code, 403)
        self.assertTrue(r.json()['verification_required'])
        code = r.json()['demo_code']  # the earlier code is now void
        self.assertEqual(self.c.post('/api/auth/verify', json={'identifier': 'neo.m', 'code': '000000' if code != '000000' else '111111'}).status_code, 400)
        r = self.c.post('/api/auth/verify', json={'identifier': 'neo@example.com', 'code': code})
        self.assertEqual(r.status_code, 200, r.text)
        h = {'Authorization': 'Bearer ' + r.json()['access_token']}
        me = self.c.get('/api/me', headers=h).json()
        self.assertEqual((me['role'], me['full_name']), ('applicant', 'Neo Molefe'))
        self.assertEqual(self.c.post('/api/auth/verify', json={'identifier': 'neo.m', 'code': code}).status_code, 400)  # single use
        p = self.c.get('/api/me/preferences', headers=h).json()
        self.assertEqual((p['email'], p['phone'], p['email_verified']), ('neo@example.com', '+26771234567', True))
        self.login('neo@example.com')  # email works as the sign-in name
        self.ids['neo'] = h

    # ---- 24 forgotten password
    def test_24_forgot_and_reset(self):
        same = self.c.post('/api/auth/forgot', json={'identifier': 'nobody-here'}).json()
        self.assertEqual(same, {'status': 'sent_if_exists'})
        j = self.c.post('/api/auth/forgot', json={'identifier': 'neo.m'}).json()
        self.assertEqual(j['status'], 'sent_if_exists')
        new_pw = 'Brand-new-pass-word-2'
        self.assertEqual(self.c.post('/api/auth/reset', json={'identifier': 'neo.m', 'code': j['demo_code'], 'new_password': 'short'}).status_code, 400)
        r = self.c.post('/api/auth/reset', json={'identifier': 'neo.m', 'code': j['demo_code'], 'new_password': new_pw})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.c.get('/api/me', headers=self.ids['neo']).status_code, 401)  # old sessions end
        self.assertEqual(self.c.post('/api/token', data={'username': 'neo.m', 'password': PW}).status_code, 401)
        self.ids['neo'] = self.login('neo.m', new_pw)
        self.ids['neo_pw'] = new_pw
        # wrong codes run out
        j = self.c.post('/api/auth/forgot', json={'identifier': 'neo.m'}).json()
        wrong = '000000' if j['demo_code'] != '000000' else '111111'
        for _ in range(M.CODE_MAX_ATTEMPTS):
            self.c.post('/api/auth/reset', json={'identifier': 'neo.m', 'code': wrong, 'new_password': 'Yet-another-pass-3'})
        self.assertEqual(self.c.post('/api/auth/reset', json={'identifier': 'neo.m', 'code': j['demo_code'],
                                                              'new_password': 'Yet-another-pass-3'}).status_code, 400)

    # ---- 25 one-time code at sign-in, and changing email
    def test_25_login_code_and_email_change(self):
        h = self.ids['neo']
        r = self.c.put('/api/me/preferences', headers=h, json={'otp_login': True})
        self.assertEqual(r.status_code, 200, r.text)
        r = self.c.post('/api/token', data={'username': 'neo.m', 'password': self.ids['neo_pw']})
        j = r.json()
        self.assertTrue(j['otp_required'])
        self.assertNotIn('access_token', j)
        self.assertEqual(self.c.post('/api/auth/login-code', json={'identifier': 'neo.m', 'code': 'abcdef'}).status_code, 400)
        r = self.c.post('/api/auth/login-code', json={'identifier': 'neo.m', 'code': j['demo_code']})
        self.assertEqual(r.status_code, 200, r.text)
        h = {'Authorization': 'Bearer ' + r.json()['access_token']}
        self.c.put('/api/me/preferences', headers=h, json={'otp_login': False})
        # new email only counts once confirmed
        j = self.c.put('/api/me/preferences', headers=h, json={'email': 'neo.new@example.com'}).json()
        self.assertEqual((j['email'], j['pending_email']), ('neo@example.com', 'neo.new@example.com'))
        self.assertEqual(self.c.post('/api/me/email/verify', headers=h, json={'code': j['demo_code']}).json()['email'], 'neo.new@example.com')
        self.assertEqual(self.c.put('/api/me/preferences', headers=h, json={'email': 'NEO.NEW@example.com'}).status_code, 200)
        # SMS alerts need a phone number
        self.assertEqual(self.c.put('/api/me/preferences', headers=h, json={'phone': '', 'alert_sms': True}).status_code, 422)
        self.assertEqual(self.c.put('/api/me/preferences', headers=h, json={'phone': '+267 71 234 567', 'alert_sms': True}).status_code, 200)
        self.ids['neo'] = h

    # ---- 26 alerts are queued, delivered (recorded) and visible to the administrator with codes masked
    def test_26_alerts_and_message_log(self):
        h, ad, o = self.ids['neo'], self.ids['admin'], self.ids['officer']
        app_id = self.c.post('/api/applications', headers=h, json={'applicant_name': 'Neo Labs', 'funding_request': 0,
                                                                    'owner_contribution': 0, **IDEA}).json()['id']
        self.assertEqual(self.upload(h, app_id).status_code, 200)
        self.assign(app_id, 'officer1')
        self.assertEqual(self.decide(o, 'request_information', 'Please add your pilot results.', app_id=app_id).status_code, 200)
        M.dispatch_outbox()
        self.assertEqual(self.c.get('/api/admin/messages', headers=h).status_code, 403)
        log = self.c.get('/api/admin/messages', headers=ad).json()
        mine = [m for m in log['items'] if m['to'] in ('neo.new@example.com', '+26771234567')]
        alerts = [m for m in mine if m['kind'] == 'alert']
        self.assertTrue({'email', 'sms'} <= {m['channel'] for m in alerts}, mine)
        self.assertTrue(all(m['status'] in ('logged', 'sent') for m in alerts))
        self.assertTrue(any('https://portal.example' in m['body'] for m in alerts))
        codes = [m for m in mine if m['kind'] in ('verify', 'reset', 'login', 'email')]
        self.assertTrue(codes)
        self.assertTrue(all(not __import__('re').search(r'\b\d{6}\b', m['body']) for m in codes), 'codes must be masked in the log')
        # switching alerts off stops new ones
        self.c.put('/api/me/preferences', headers=h, json={'alert_email': False, 'alert_sms': False})
        before = {m['id'] for m in self.c.get('/api/admin/messages?limit=500', headers=ad).json()['items']}
        self.assertEqual(self.upload(h, app_id).status_code, 200)
        self.assertEqual(self.decide(o, 'request_information', 'One more thing please.', app_id=app_id).status_code, 200)
        M.dispatch_outbox()
        new = [m for m in self.c.get('/api/admin/messages?limit=500', headers=ad).json()['items'] if m['id'] not in before]
        self.assertFalse([m for m in new if m['kind'] == 'alert' and m['to'] in ('neo.new@example.com', '+26771234567')], new)

    def test_27_options_and_admin_contact_fields(self):
        self.assertTrue(self.c.get('/api/auth/options').json()['self_registration'])
        ad = self.ids['admin']
        r = self.c.post('/api/users', headers=ad, json={'username': 'eval9', 'password': PW, 'role': 'officer',
                                                        'full_name': 'Eval Nine', 'email': 'neo.new@example.com'})
        self.assertEqual(r.status_code, 409)
        r = self.c.post('/api/users', headers=ad, json={'username': 'eval9', 'password': PW, 'role': 'officer',
                                                        'full_name': 'Eval Nine', 'email': 'eval9@example.com', 'phone': '72000000'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['phone'], '+26772000000')

    # ---- 28 public help page: FAQs, regulator contacts and support details, editable by the administrator
    def test_28_public_help_and_content(self):
        r = self.c.get('/api/public/help')  # no sign-in needed
        self.assertEqual(r.status_code, 200)
        h = r.json()
        self.assertGreaterEqual(len(h['faqs']), 10)
        names = ' '.join(c['name'] for c in h['contacts'])
        for org in ('CIPA', 'BOBS', 'BOCRA', 'BURS', 'NBFIRA', 'CEDA'):
            self.assertIn(org, names)
        self.assertTrue(h['support_categories'])
        ad, o = self.ids['admin'], self.ids['officer']
        self.assertEqual(self.c.put('/api/admin/content', headers=o, json={'announcement': 'x'}).status_code, 403)
        self.assertEqual(self.c.put('/api/admin/content', headers=ad, json={'nonsense': 'x'}).status_code, 422)
        self.assertEqual(self.c.put('/api/admin/content', headers=ad, json={'support_email': 'not-email'}).status_code, 422)
        bad_cats = [{'key': 'a1', 'label': 'One', 'days': 1}, {'key': 'a1', 'label': 'Two', 'days': 2}]
        self.assertEqual(self.c.put('/api/admin/content', headers=ad, json={'support_categories': bad_cats}).status_code, 422)
        cats = [{'key': 'account', 'label': 'Account help', 'days': 1}, {'key': 'idea', 'label': 'My idea', 'days': 2}]
        r = self.c.put('/api/admin/content', headers=ad, json={'announcement': 'Submissions close on 30 November.',
                                                               'support_email': 'help@example.com', 'support_categories': cats})
        self.assertEqual(r.status_code, 200, r.text)
        h = self.c.get('/api/public/help').json()
        self.assertEqual(h['content']['announcement'], 'Submissions close on 30 November.')
        self.assertEqual([c['key'] for c in h['support_categories']], ['account', 'idea'])
        # FAQ and contact management; unpublished items disappear from the public page
        f = self.c.post('/api/faqs', headers=ad, json={'category': 'Dates', 'question': 'When does the call close?',
                                                       'answer': 'On 30 November at 17:00.', 'sort': 1}).json()
        self.assertEqual(self.c.get('/api/public/help').json()['faqs'][0]['question'], 'When does the call close?')
        self.c.put(f"/api/faqs/{f['id']}", headers=ad, json={**{k: f[k] for k in ('category', 'question', 'answer', 'sort')}, 'published': False})
        self.assertNotIn(f['id'], [x['id'] for x in self.c.get('/api/public/help').json()['faqs']])
        self.assertIn(f['id'], [x['id'] for x in self.c.get('/api/admin/faqs', headers=ad).json()])
        self.assertEqual(self.c.delete(f"/api/faqs/{f['id']}", headers=ad).status_code, 200)
        self.assertEqual(self.c.post('/api/contacts', headers=ad, json={'name': 'Test Org', 'website': 'ftp://x'}).status_code, 422)
        c = self.c.post('/api/contacts', headers=ad, json={'name': 'Test Org', 'phone': '+267 300 0000', 'sort': 0}).json()
        self.assertEqual(self.c.get('/api/public/help').json()['contacts'][0]['name'], 'Test Org')
        self.assertEqual(self.c.delete(f"/api/contacts/{c['id']}", headers=ad).status_code, 200)

    # ---- 29 administrator adds questions to the idea form; submitters must answer the required ones
    def test_29_form_builder(self):
        ad = self.ids['admin']
        bad = {'key': 'team_size', 'label': 'Team size', 'kind': 'select', 'options': ['only one']}
        self.assertEqual(self.c.post('/api/form-fields', headers=ad, json=bad).status_code, 422)
        self.assertEqual(self.c.post('/api/form-fields', headers=ad, json={'key': 'problem', 'label': 'Clash'}).status_code, 409)
        self.assertEqual(self.c.post('/api/form-fields', headers=self.ids['officer'], json={'key': 'x1', 'label': 'No'}).status_code, 403)
        q1 = self.c.post('/api/form-fields', headers=ad, json={'key': 'team_size', 'label': 'Team size', 'kind': 'number',
                                                              'required': True, 'sort': 1}).json()
        q2 = self.c.post('/api/form-fields', headers=ad, json={'key': 'district', 'label': 'District', 'kind': 'select',
                                                              'options': ['Gaborone', 'Francistown', 'Maun'], 'sort': 2}).json()
        self.c.post('/api/form-fields', headers=ad, json={'key': 'youth_led', 'label': 'Youth-led?', 'kind': 'yes_no', 'sort': 3})
        self.assertEqual([f['key'] for f in self.c.get('/api/form-fields', headers=self.ids['neo']).json()],
                         ['team_size', 'district', 'youth_led'])
        h = self.ids['neo']
        base = {'applicant_name': 'Neo Labs', 'funding_request': 0, 'owner_contribution': 0, **IDEA}
        r = self.c.post('/api/applications', headers=h, json=base)
        self.assertEqual(r.status_code, 422)
        self.assertIn('Team size', r.json()['detail'])
        self.assertEqual(self.c.post('/api/applications', headers=h, json={**base, 'answers': {'team_size': 'many'}}).status_code, 422)
        self.assertEqual(self.c.post('/api/applications', headers=h,
                                     json={**base, 'answers': {'team_size': 3, 'district': 'Lobatse'}}).status_code, 422)
        r = self.c.post('/api/applications', headers=h, json={**base, 'answers': {'team_size': '3', 'district': 'Maun',
                                                                                  'youth_led': 'yes', 'sneaky': 'dropped'}})
        self.assertEqual(r.status_code, 200, r.text)
        got = self.c.get(f"/api/applications/{r.json()['id']}", headers=h).json()
        self.assertEqual(got['answers'], {'team_size': 3.0, 'district': 'Maun', 'youth_led': True})
        # editing keeps the rules; switching a question off stops asking for it
        self.assertEqual(self.c.put(f"/api/applications/{r.json()['id']}/idea", headers=h,
                                    json={**IDEA, 'answers': {'district': 'Maun'}}).status_code, 422)
        self.assertEqual(self.c.put(f"/api/form-fields/{q1['id']}", headers=ad, json={**{k: q1[k] for k in ('key', 'label', 'kind', 'sort')},
                                                                                  'required': True, 'enabled': False}).status_code, 200)
        self.assertEqual(self.c.put(f"/api/applications/{r.json()['id']}/idea", headers=h,
                                    json={**IDEA, 'answers': {'district': 'Maun'}}).status_code, 200)
        self.assertEqual(self.c.put(f"/api/form-fields/{q2['id']}", headers=ad, json={'key': 'region', 'label': 'District'}).status_code, 409)
        self.assertEqual(len(self.c.get('/api/form-fields?include_disabled=true', headers=ad).json()), 3)

    # ---- 30 support requests with reply times
    def test_30_support_tickets(self):
        h, ad, o = self.ids['neo'], self.ids['admin'], self.ids['officer']
        self.assertEqual(self.c.post('/api/support/tickets', headers=h, json={'category': 'nope', 'subject': 'Help',
                                                                              'message': 'I cannot upload my file.'}).status_code, 422)
        r = self.c.post('/api/support/tickets', headers=h, json={'category': 'idea', 'subject': 'Upload help',
                                                                 'message': 'I cannot upload my concept note.'})
        self.assertEqual(r.status_code, 200, r.text)
        t = r.json()
        self.assertEqual((t['status'], t['category_label'], t['overdue']), ('open', 'My idea', False))
        self.assertGreater(t['due_at'], t['created_at'])
        self.assertEqual(self.c.post('/api/users', headers=ad, json={'username': 'other.sub', 'password': PW, 'role': 'applicant'}).status_code, 200)
        other = self.login('other.sub')
        self.assertEqual(self.c.get('/api/support/tickets', headers=other).json()['items'], [])  # not theirs
        staff = self.c.get('/api/support/tickets', headers=o).json()
        self.assertEqual(staff['stats']['open'], 1)
        self.assertEqual(self.c.post(f"/api/support/tickets/{t['id']}/reply", headers=h, json={'response': 'self'}).status_code, 403)
        r = self.c.post(f"/api/support/tickets/{t['id']}/reply", headers=ad, json={'response': 'Please try a PDF under 15 MB.'})
        self.assertEqual(r.json()['status'], 'answered')
        notes = self.c.get('/api/notifications', headers=h).json()
        self.assertTrue(any('PDF under 15 MB' in n['message'] for n in notes))
        self.assertEqual(self.c.get('/api/support/tickets', headers=ad).json()['stats']['answered_on_time'], 1)
        self.assertEqual(self.c.post(f"/api/support/tickets/{t['id']}/close", headers=other).status_code, 404)
        self.assertEqual(self.c.post(f"/api/support/tickets/{t['id']}/close", headers=h).json()['status'], 'closed')
        self.assertEqual(M.add_working_days(M.datetime(2026, 11, 13, 10), 1).day, 16)  # Friday + 1 working day = Monday

    # ---- 31 reports: breakdowns, timings and spreadsheet downloads
    def test_31_reports(self):
        h, ad, o = self.ids['neo'], self.ids['admin'], self.ids['officer']
        self.assertEqual(self.c.put('/api/me/preferences', headers=h, json={'gender': 'robot'}).status_code, 422)
        self.assertEqual(self.c.put('/api/me/preferences', headers=h, json={'birth_year': 1800}).status_code, 422)
        self.assertEqual(self.c.put('/api/me/preferences', headers=h, json={'district': 'Atlantis'}).status_code, 422)
        r = self.c.put('/api/me/preferences', headers=h, json={'gender': 'female', 'birth_year': M.utcnow().year - 26, 'district': 'North-West'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((r.json()['gender'], r.json()['district']), ('female', 'North-West'))
        idea = {**IDEA, 'idea_title': '=HYPERLINK("http://x")'}
        app_id = self.c.post('/api/applications', headers=h, json={'applicant_name': 'Neo Labs', 'funding_request': 5000,
                                                                    'owner_contribution': 0, **idea}).json()['id']
        self.assertEqual(self.upload(h, app_id).status_code, 200)
        self.assign(app_id, 'officer1')
        self.assertEqual(self.decide(o, 'refer_committee', 'Strong idea, refer.', app_id=app_id).status_code, 200)
        self.assertEqual(self.decide(self.ids['committee'], 'approve', 'Approved for support.', app_id=app_id).status_code, 200)
        self.assertEqual(self.c.get('/api/reports/summary', headers=h).status_code, 403)
        d = self.c.get('/api/reports/summary', headers=ad).json()
        self.assertGreaterEqual(d['totals']['ideas'], 3)
        self.assertGreaterEqual(d['totals']['approved'], 1)
        female = next(x for x in d['by_gender'] if x['label'] == 'Female')
        self.assertGreaterEqual(female['approved'], 1)
        self.assertIn('25-29', [x['label'] for x in d['by_age']])
        self.assertIn('North-West', [x['label'] for x in d['by_district']])
        self.assertEqual(d['by_gender'][-1]['label'], 'Not stated')  # staff-entered ideas have no submitter profile
        self.assertIsNotNone(d['timing_days']['to_decision'])
        self.assertEqual(self.c.get('/api/reports/summary?from=2999-01-01', headers=ad).json()['totals']['ideas'], 0)
        self.assertEqual(self.c.get('/api/reports/summary?from=yesterday', headers=ad).status_code, 422)
        r = self.c.get('/api/reports/ideas.csv', headers=o)
        self.assertEqual(r.status_code, 200)
        self.assertIn('text/csv', r.headers['content-type'])
        text = r.content.decode('utf-8-sig')
        self.assertTrue(text.startswith('Idea,Submitter,'))
        self.assertIn('District', text.splitlines()[0])  # extra form question column
        self.assertIn("'=HYPERLINK", text)  # formulas are neutralised
        self.assertNotIn('\n=HYPERLINK', text)
        r = self.c.get('/api/reports/breakdowns.csv', headers=ad)
        self.assertEqual(r.status_code, 200)
        self.assertIn('Gender,Ideas,Approved,Declined', r.content.decode('utf-8-sig'))
        self.assertEqual(M.reports.age_band(2000, __import__('datetime').date(2026, 6, 1)), '25-29')
        self.assertEqual(M.reports.age_band(None), 'Not stated')


if __name__ == '__main__':
    unittest.main()
