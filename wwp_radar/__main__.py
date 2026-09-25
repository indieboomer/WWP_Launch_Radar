"""Entry point: `python -m wwp_radar` starts the dashboard and the collector (one process)."""
from __future__ import annotations

import argparse
import logging
import sys

import uvicorn

from .config import load_settings, validate_exposure


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="wwp_radar", description="WWP Launch Radar")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="run dashboard + collector (default)")
    demo = sub.add_parser("demo", help="generate demo data into a separate demo database")
    demo.add_argument("--hours", type=int, default=48)
    sub.add_parser("backup", help="write a consistent SQLite backup").add_argument("dest")
    args = parser.parse_args(argv)

    settings = load_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per Steam request is too noisy

    if args.cmd == "demo":
        from .demo import generate
        settings.demo = True
        path = generate(settings, hours=args.hours)
        print(f"Demo data written to {path}. Start with WWP_DEMO=1 to view it.")
        return
    if args.cmd == "backup":
        from .db import Database
        Database(settings.db_path).backup_to(args.dest)
        print(f"Backup of {settings.db_path} written to {args.dest}")
        return

    validate_exposure(settings)
    from .app import create_app

    logging.getLogger(__name__).info("Data directory: %s (database %s)", settings.data_dir, settings.db_path.name)
    app = create_app(settings)
    uvicorn.run(app, host=settings.host, port=settings.port, workers=1, log_level=settings.log_level.lower(),
                timeout_graceful_shutdown=10, proxy_headers=settings.trust_proxy_auth)


if __name__ == "__main__":
    main()
