"""Launch the foundation on loopback, always with one worker and no reload."""
import argparse
import os
from pathlib import Path
import sys

import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dev-controls", action="store_true", help="Enable synthetic test controls and /devtools")
    parser.add_argument("--live-config", type=Path, help="Opt in to a key-free model/budget comparison configuration")
    parser.add_argument("--database", type=Path, help="Use a separate local test database")
    parser.add_argument("--team-demo-origin", help="Opt in to one exact HTTPS origin behind a loopback proxy; requires --dev-controls")
    parser.add_argument("--port", type=int, default=8000, help="Loopback port, default 8000")
    args = parser.parse_args()
    if args.team_demo_origin is not None:
        if not args.dev_controls:
            parser.error("--team-demo-origin requires --dev-controls")
        os.environ["PARKING_TEAM_DEMO_ORIGIN"] = args.team_demo_origin
    if args.dev_controls:
        os.environ["TEST_CONTROL_ENABLED"] = "true"
    if not 1024 <= args.port <= 65535:
        parser.error("Use a local port between 1024 and 65535")
    os.environ["PARKING_LOCAL_PORT"] = str(args.port)
    if args.live_config:
        if not args.dev_controls:
            parser.error("--live-config requires --dev-controls")
        os.environ["PARKING_LIVE_CONFIG"] = str(args.live_config.resolve())
    if args.database:
        os.environ["PARKING_LOCAL_DATABASE"] = str(args.database.resolve())
    uvicorn.run("backend.app:create_app", factory=True, host="127.0.0.1", port=args.port,
                workers=1, proxy_headers=False, access_log=False)
