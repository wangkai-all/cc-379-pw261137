#!/usr/bin/env python3
"""Start all four processes (console + three services) and optionally seed.

Usage:
    python3 -m clusterproof.cli serve --db var/clusterproof.sqlite3 --reset
    python3 -m clusterproof.cli demo  --db var/demo.sqlite3
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time

from .db import init_db
from . import fixtures

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def cmd_seed(args):
    conn = init_db(args.db)
    result = fixtures.seed_fixtures(conn, reset=args.reset)
    print(json.dumps({"db": args.db, **result}, indent=2))
    return 0


def cmd_serve(args):
    conn = init_db(args.db)
    if args.seed:
        print(json.dumps(fixtures.seed_fixtures(conn, reset=args.reset), indent=2))
    env = dict(os.environ)
    env["CLUSTERPROOF_DB"] = os.path.abspath(args.db)
    env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
    procs = [
        ("sampler", [sys.executable, "-m", "clusterproof.sampler.service",
                     "--port", str(args.sampler_port), "--db", args.db,
                     "--checker-url", f"http://127.0.0.1:{args.checker_port}"]),
        ("checker", [sys.executable, "-m", "clusterproof.checker.service",
                     "--port", str(args.checker_port), "--db", args.db]),
        ("publisher", [sys.executable, "-m", "clusterproof.publisher.service",
                       "--port", str(args.publisher_port), "--db", args.db]),
        ("console", [sys.executable, "-m", "clusterproof.ui.service",
                     "--port", str(args.console_port),
                     "--sampler-url", f"http://127.0.0.1:{args.sampler_port}",
                     "--checker-url", f"http://127.0.0.1:{args.checker_port}",
                     "--publisher-url", f"http://127.0.0.1:{args.publisher_port}"]),
    ]
    children = []
    stopping = {"flag": False}

    def stop(_signum=None, _frame=None):
        if stopping["flag"]:
            return
        stopping["flag"] = True
        for _, child in children:
            if child.poll() is None:
                child.terminate()
        deadline = time.time() + 5
        for _, child in children:
            try:
                child.wait(timeout=max(0.1, deadline - time.time()))
            except subprocess.TimeoutExpired:
                child.kill()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        for name, cmd in procs:
            child = subprocess.Popen(cmd, env=env, cwd=ROOT)
            children.append((name, child))
        print(f"console: http://127.0.0.1:{args.console_port}")
        print("press Ctrl-C to stop all services", flush=True)
        while not stopping["flag"]:
            for name, child in children:
                if child.poll() is not None:
                    print(f"{name} exited with {child.returncode}", file=sys.stderr)
                    stop()
                    return 1
            time.sleep(0.4)
    finally:
        stop()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="clusterproof", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    seed = sub.add_parser("seed", help="create fictional source data")
    seed.add_argument("--db", default="var/clusterproof.sqlite3")
    seed.add_argument("--reset", action="store_true")
    seed.set_defaults(func=cmd_seed)

    serve = sub.add_parser("serve", help="run console, sampler, checker, publisher")
    serve.add_argument("--db", default="var/clusterproof.sqlite3")
    serve.add_argument("--seed", action="store_true", help="seed fixtures on start")
    serve.add_argument("--reset", action="store_true")
    serve.add_argument("--console-port", type=int, default=8710)
    serve.add_argument("--sampler-port", type=int, default=8711)
    serve.add_argument("--checker-port", type=int, default=8712)
    serve.add_argument("--publisher-port", type=int, default=8713)
    serve.set_defaults(func=cmd_serve)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
