"""Public mock-only HTTP stimuli for the separately observed frontend UI.

Not an acceptance runner: API preparation is not reported as UI evidence.
Synthetic test accounts only; no cookie/token/body logging and no model calls.
"""
import argparse
import json
from pathlib import Path
from uuid import uuid4
import httpx

ROOT = Path(__file__).resolve().parents[3]
FACILITY = 'fac-demo-01'

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=('pages', 'incident'))
    args = parser.parse_args()
    with httpx.Client(base_url='http://127.0.0.1:4178', trust_env=False, timeout=15) as client:
        csrf = ''
        def request(method, path, body=None, status=200):
            headers = {'Origin': 'http://127.0.0.1:4178'}
            if method != 'GET' and path != '/api/v1/auth/session':
                headers.update({'X-CSRF-Token': csrf, 'Idempotency-Key': str(uuid4())})
            response = client.request(method, path, json=body, headers=headers)
            if response.status_code != status:
                raise RuntimeError(f'{method} {path.split("?")[0]}: HTTP {response.status_code}')
            return response.json()
        request('POST', '/api/v1/auth/session', {'username': 'demo-operator', 'password': 'parking-demo-only'})
        csrf = request('GET', '/api/v1/me')['csrf_token']
        if args.phase == 'pages':
            run = request('GET', '/health/ready')['current_run_id']
            state = request('GET', f'/api/v1/facilities/{FACILITY}/state?run_id={run}')['snapshot']['state_version']
            ids = [request('POST', f'/api/v1/facilities/{FACILITY}/commands', {
                'run_id': run, 'purpose': 'query', 'text': f'가상 이력 페이지 확인 {index + 1}',
                'based_on_state_version': state}, 201)['command_id'] for index in range(51)]
            result = {'phase': 'pages', 'run_id': run, 'prepared_commands': len(ids), 'command_ids': ids}
        else:
            run = request('POST', '/api/v1/test/runs', {'facility_id': FACILITY,
                'fixture_ref': 's1a-foundation-v1', 'seed': 42, 'config_ref': 'foundation-v1'}, 201)['run_id']
            for _ in range(60):
                request('POST', f'/api/v1/test/runs/{run}/control', {'action': 'step'})
            notified = request('POST', '/api/v1/test/s1a/manual', {'run_id': run, 'action': 'notify'})
            result = {'phase': 'incident', 'run_id': run, 'incident_id': notified['incident_id'],
                      'notification_id': notified['execution']['result']['notification_id']}
        (ROOT / 'Work_tree/artifacts/contest-frontend' / f'prepared-{args.phase}.json').write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'prepared {args.phase}: public API stimuli only; provider calls=0')

if __name__ == '__main__':
    main()
