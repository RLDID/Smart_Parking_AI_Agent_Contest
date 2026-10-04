"""Existing local backend behind the frontend's same-origin Vite proxy."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'code'))

from backend.app import Settings, create_app
from agent.live import LiveConfiguration
import uvicorn

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--live-config', type=Path, help='Enable explicit paid read-only AI queries; never starts operational agents')
    args = parser.parse_args()
    settings = Settings(
        database=ROOT / 'data/local/contest.sqlite3',
        test_control=True,
        origins=('http://127.0.0.1:8000', 'http://localhost:8000'),
        live_configuration=LiveConfiguration.read(args.live_config.resolve()) if args.live_config else None,
        budget_database=ROOT / 'data/local/model-budget.sqlite3',
    )
    uvicorn.run(create_app(settings), host='127.0.0.1', port=8010, workers=1,
                proxy_headers=False, access_log=False)
