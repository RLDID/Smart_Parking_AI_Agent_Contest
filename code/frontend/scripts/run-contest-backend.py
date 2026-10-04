"""Isolated mock-only backend for the additive frontend integration.

Uses a separately verified backend checkout read-only; database belongs to this
frontend worktree. No model configuration, shared budget writer or paid calls.
"""
import argparse
from pathlib import Path
import sys

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[3]

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--backend-root', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8018)
    parser.add_argument('--frontend-port', type=int, default=4178)
    args = parser.parse_args()
    source = args.backend_root.resolve()
    sys.path.insert(0, str(source / 'code'))
    from backend.app import Settings, create_app
    import uvicorn

    artifact = ROOT / 'Work_tree/artifacts/contest-frontend'
    artifact.mkdir(parents=True, exist_ok=True)
    settings = Settings(database=artifact / 'foundation.sqlite3', test_control=True,
                        origins=(f'http://127.0.0.1:{args.frontend_port}',),
                        budget_database=artifact / 'unused-model-budget.sqlite3')
    app = create_app(settings)
    required = {'/api/v1/me/vehicle-locations', '/api/v1/me/parking-map',
                '/api/v1/commands/{command_id}/progress', '/api/v1/incidents/{incident_id}/timeline'}
    if not required.issubset(app.openapi()['paths']):
        raise SystemExit('The selected backend does not include the additive view APIs.')
    uvicorn.run(app, host='127.0.0.1', port=args.port, workers=1,
                proxy_headers=False, access_log=False)
