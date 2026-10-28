"""HTTP-level regression checks use only isolated temporary databases."""
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
import json
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from backend.app import create_app
from backend.core.finguard_engine import run_forecast
from backend.core.scenario_actions import payment_identity
from backend.core.store import Store, demo_csv


class WebAPI(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.app = create_app(self.folder.name, allowed_hosts=['testserver'])
        self.client = TestClient(self.app)
        self.state = self.client.get('/api/state').json()
        self.headers = {'X-FinGuard-Token': self.state['csrf_token']}

    def tearDown(self):
        self.client.close()
        self.folder.cleanup()

    def get_state(self):
        self.state = self.client.get('/api/state').json()
        return self.state

    def post(self, path, body=None, mutate=False):
        body = dict(body or {})
        if mutate:
            body.setdefault('revision', self.get_state()['revision'])
        return self.client.post('/api/' + path, json=body, headers=self.headers)

    def parameters(self):
        with self.app.state.service.store() as store:
            by_id = {p.id: p for p in store.payments()}
            return dict(payment_actions=[dict(payment_id=pid,payment_identity=payment_identity(by_id[pid]),action=kind,defer_days=days)
                for pid, kind, days in [(2,'exclude',0),(3,'defer',7),(5,'defer',40)]],horizon=30)

    def test_boot_state_and_parity_with_desktop_engine(self):
        self.assertEqual(len(self.state['payments']),9)
        self.assertEqual(self.state['dashboard']['flagged_payment_count'],3)
        self.assertEqual(self.state['dashboard']['possible_duplicate_exposure_rub'],150000)
        with self.app.state.service.store() as store:
            expected=run_forecast(store.payments(),store.opening_balance(),date.today(),balance_date=store.opening_balance_date())
        self.assertEqual(self.state['dashboard']['forecast']['points'],expected['points'])

    def test_cascade_mixed_actions_and_horizon_obligations(self):
        response=self.post('scenario/calculate',{'parameters':self.parameters()})
        self.assertEqual(response.status_code,200,response.text)
        result=response.json()
        self.assertEqual(result['scenario']['final_rub']-result['base']['final_rub'],198000)
        self.assertEqual(result['scenario']['deferred_beyond_horizon_rub'],48000)
        self.assertEqual(len(result['parameters']['payment_actions']),3)

    def test_global_shocks_apply_once(self):
        params=self.parameters();params.update(revenue_drop_pct=20,income_delay_days=3,extra_expense_rub=10000)
        result=self.post('scenario/calculate',{'parameters':params}).json()
        self.assertEqual(result['scenario']['final_rub'],336000)

    def test_save_load_compare_export_keep_full_cascade(self):
        saved=self.post('scenario/save',{'name':'Каскад','parameters':self.parameters()},True)
        self.assertEqual(saved.status_code,200,saved.text)
        loaded=self.client.get('/api/scenario/'+str(saved.json()['id'])).json()
        self.assertEqual(len(loaded['parameters']['payment_actions']),3)
        compared=self.client.get('/api/scenario/compare').json()
        self.assertEqual(compared[0]['scenario'],loaded['scenario'])
        exported=self.client.get('/api/export').json()
        self.assertEqual(len(exported['scenarios'][0]['parameters']['payment_actions']),3)
        self.assertNotIn('csrf_token',exported)
        self.assertNotIn(str(Path(self.folder.name)),json.dumps(exported))

    def test_changed_member_rejects_entire_saved_cascade(self):
        saved=self.post('scenario/save',{'name':'Старый','parameters':self.parameters()},True).json()['id']
        self.post('payment',{'payment_id':3,'status':'completed','payment_date':self.state['payments'][2]['payment_date']},True)
        self.assertEqual(self.client.get(f'/api/scenario/{saved}').status_code,400)
        comparison=self.client.get('/api/scenario/compare').json()
        self.assertIn('error',comparison[0])
        self.assertNotIn('scenario',comparison[0])

    def test_malformed_cascade_is_rejected_before_write(self):
        params=self.parameters();params['payment_actions'].append(params['payment_actions'][0])
        before=self.state['revision']
        self.assertEqual(self.post('scenario/save',{'name':'Неверный','parameters':params},True).status_code,400)
        self.assertEqual(self.get_state()['revision'],before)

    def test_legacy_single_saved_scenario_loads(self):
        action=self.parameters()['payment_actions'][0]
        with self.app.state.service.store() as store:
            sid=store.save_scenario('v6.1',dict(action,horizon=30))
        loaded=self.client.get(f'/api/scenario/{sid}')
        self.assertEqual(loaded.status_code,200)
        self.assertEqual(len(loaded.json()['parameters']['payment_actions']),1)

    def test_manual_review_changes_risk_but_not_cashflow(self):
        before=self.state['dashboard']['forecast']
        self.assertEqual(self.post('review',{'payment_id':2,'verdict':'dismissed'},True).status_code,200)
        state=self.get_state()
        self.assertEqual(state['dashboard']['possible_duplicate_exposure_rub'],0)
        self.assertEqual(state['dashboard']['forecast'],before)
        self.assertEqual(state['reviews']['2'],'dismissed')
        self.assertEqual(self.post('review',{'payment_id':2,'verdict':'pending'},True).status_code,200)

    def test_status_change_requires_balance_reconciliation(self):
        self.assertEqual(self.post('payment',{'payment_id':1,'status':'completed','payment_date':self.state['payments'][0]['payment_date']},True).status_code,200)
        state=self.get_state()
        self.assertTrue(state['balance_needs_reconciliation'])
        self.assertFalse(state['dashboard']['forecast']['balance_is_current'])
        self.assertEqual(self.post('balance',{'amount_kopecks':-123456,'as_of':date.today().isoformat()},True).status_code,200)
        state=self.get_state()
        self.assertFalse(state['balance_needs_reconciliation'])
        self.assertEqual(state['opening_kopecks'],-123456)

    def test_future_balance_and_float_kopecks_rejected(self):
        for amount,when in [(1,(date.today()+timedelta(days=1)).isoformat()),(1.5,date.today().isoformat()),(True,date.today().isoformat())]:
            response=self.post('balance',{'amount_kopecks':amount,'as_of':when},True)
            self.assertIn(response.status_code,[400,422])

    def test_stale_revision_rejects_second_tab(self):
        revision=self.state['revision']
        self.assertEqual(self.post('balance',{'amount_kopecks':100,'as_of':date.today().isoformat(),'revision':revision}).status_code,200)
        second=self.post('balance',{'amount_kopecks':200,'as_of':date.today().isoformat(),'revision':revision})
        self.assertEqual(second.status_code,409)
        self.assertEqual(self.get_state()['opening_kopecks'],100)

    def test_concurrent_edits_only_one_can_commit(self):
        revision=self.state['revision']
        def edit(amount):
            return self.post('balance',{'amount_kopecks':amount,'as_of':date.today().isoformat(),'revision':revision}).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(edit,[123,456]))
        self.assertEqual(sorted(results),[200,409])

    def test_mutations_require_revision_and_csrf(self):
        self.assertEqual(self.post('balance',{'amount_kopecks':1,'as_of':date.today().isoformat()}).status_code,422)
        self.assertEqual(self.client.post('/api/demo',json={'case':'clean','revision':self.state['revision']}).status_code,403)

    def test_other_origin_and_untrusted_host_rejected(self):
        response=self.client.post('/api/demo',json={'case':'clean','revision':self.state['revision']},headers={**self.headers,'Origin':'https://foreign.example'})
        self.assertEqual(response.status_code,403)
        self.assertEqual(self.client.get('/api/state',headers={'Host':'evil.example'}).status_code,400)

    def test_csv_preview_cancel_never_mutates(self):
        preview=self.post('import/preview',{'content':base64.b64encode(demo_csv('clean')).decode()})
        self.assertEqual(preview.status_code,200,preview.text)
        self.assertEqual(preview.json()['total'],3)
        self.assertEqual(self.get_state()['revision'],self.state['revision'])
        self.assertEqual(len(self.state['payments']),9)

    def test_csv_commit_has_backup_and_no_replay(self):
        preview=self.post('import/preview',{'content':base64.b64encode(demo_csv('clean')).decode()}).json()
        committed=self.post('import/apply',{'token':preview['token']},True)
        self.assertEqual(committed.status_code,200,committed.text)
        self.assertTrue(committed.json()['backup_name'].endswith('.sqlite3'))
        self.assertEqual(len(self.get_state()['payments']),3)
        self.assertEqual(self.post('import/apply',{'token':preview['token']},True).status_code,400)

    def test_preview_cannot_overwrite_changed_registry(self):
        preview=self.post('import/preview',{'content':base64.b64encode(demo_csv('clean')).decode()}).json()
        self.post('balance',{'amount_kopecks':123,'as_of':date.today().isoformat()},True)
        # Even using the latest outer revision cannot revive an outdated preview.
        self.assertEqual(self.post('import/apply',{'token':preview['token']},True).status_code,400)
        self.assertEqual(len(self.get_state()['payments']),9)

    def test_bad_csv_and_base64_are_user_errors(self):
        for raw in ['***',base64.b64encode(b'wrong;headers\nx;y').decode()]:
            self.assertEqual(self.post('import/preview',{'content':raw}).status_code,400)

    def test_backup_restore_roundtrip_including_scenarios_and_reviews(self):
        self.post('scenario/save',{'name':'Сохранённый','parameters':self.parameters()},True)
        self.post('review',{'payment_id':2,'verdict':'confirmed'},True)
        backup=self.post('backup/create',{},True).json()['name']
        downloaded=self.client.get('/api/backup/download',params={'name':backup})
        self.assertTrue(downloaded.content.startswith(b'SQLite format 3'))
        self.post('demo',{'case':'clean'},True)
        restored=self.post('backup/restore',{'content':base64.b64encode(downloaded.content).decode()},True)
        self.assertEqual(restored.status_code,200,restored.text)
        state=self.get_state()
        self.assertEqual(len(state['payments']),9)
        self.assertEqual(state['reviews']['2'],'confirmed')
        self.assertEqual(state['scenarios'][0]['name'],'Сохранённый')

    def test_corrupt_backup_never_changes_current_data(self):
        before=self.state['revision']
        response=self.post('backup/restore',{'content':base64.b64encode(b'broken sqlite').decode()},True)
        self.assertEqual(response.status_code,400,response.text)
        self.assertEqual(self.get_state()['revision'],before)

    def test_path_traversal_download_and_static_rejected(self):
        self.assertEqual(self.client.get('/api/backup/download',params={'name':'../finguard-web.sqlite3'}).status_code,400)
        self.assertNotEqual(self.client.get('/backend/app.py').status_code,200)
        self.assertNotEqual(self.client.get('/.venv/pyvenv.cfg').status_code,200)

    def test_selfcheck_runs_nine_cases(self):
        response=self.post('selfcheck').json()
        self.assertTrue(response['passed'])
        self.assertEqual(len(response['checks']),9)

    def test_static_page_and_security_headers(self):
        page=self.client.get('/')
        self.assertEqual(page.status_code,200)
        self.assertIn('FinGuard',page.text)
        self.assertEqual(page.headers['x-frame-options'],'DENY')
        self.assertIn("script-src 'self'",page.headers['content-security-policy'])

    def test_api_reference_is_available_without_external_assets(self):
        reference=self.client.get('/api/docs')
        self.assertEqual(reference.status_code,200)
        self.assertIn('POST /api/scenario/calculate',reference.text)
        self.assertNotIn('<script',reference.text)
        self.assertEqual(self.client.get('/api/openapi.json').status_code,200)

    def test_http_body_size_limit_is_enforced(self):
        response=self.client.post('/api/import/preview',content=b'x'*(12*1024*1024+1),headers={**self.headers,'content-type':'application/json'})
        self.assertEqual(response.status_code,413)

    def test_password_protects_api_and_frontend(self):
        with tempfile.TemporaryDirectory() as folder:
            with TestClient(create_app(folder,password='test-strong-password',allowed_hosts=['testserver'])) as client:
                self.assertEqual(client.get('/').status_code,401)
                self.assertEqual(client.get('/api/state').status_code,401)
                self.assertEqual(client.get('/api/state',auth=('finguard','wrong')).status_code,401)
                self.assertEqual(client.get('/api/state',auth=('finguard','test-strong-password')).status_code,200)

    def test_reopening_service_preserves_database(self):
        self.post('balance',{'amount_kopecks':789123,'as_of':date.today().isoformat()},True)
        other=TestClient(create_app(self.folder.name,allowed_hosts=['testserver']))
        self.assertEqual(other.get('/api/state').json()['opening_kopecks'],789123)
        other.close()


if __name__=='__main__':
    unittest.main()
