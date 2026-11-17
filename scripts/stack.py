#!/usr/bin/env python3
"""Start or stop the demo stack by pid file (avoids pkill self-matches)."""
import json
import os
import signal
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PIDFILE = os.path.join(ROOT, "var", "stack.pids")


def stop():
    if not os.path.exists(PIDFILE):
        if ports_busy():
            print("pids file missing but ports are busy; stop those processes "
                  "manually (ps -ef | grep clusterproof)")
        return
    with open(PIDFILE) as fh:
        record = json.load(fh)
    pids = record["pids"] if isinstance(record, dict) else record
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.time() + 5
    for pid in pids:
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    os.unlink(PIDFILE)
    time.sleep(0.5)


PORTS = (8710, 8711, 8712, 8713)


def ports_busy():
    import socket
    busy = []
    for port in PORTS:
        sock = socket.socket()
        sock.settimeout(0.3)
        try:
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                busy.append(port)
        finally:
            sock.close()
    return busy


def start(db: str, reset: bool, seed: bool = True):
    busy = ports_busy()
    if busy:
        print(f"ports already in use: {busy}. Stop the running stack first "
              f"(python3 scripts/stack.py stop) or kill those processes.")
        return 1
    os.makedirs(os.path.join(ROOT, "var"), exist_ok=True)
    if reset:
        for suffix in ("", "-wal", "-shm"):
            path = os.path.join(ROOT, db + suffix)
            if os.path.exists(path):
                os.unlink(path)
    args = [sys.executable, "-m", "clusterproof.cli", "serve", "--db", db]
    if reset:
        args.append("--reset")
    if seed:
        args.append("--seed")
    log = open(os.path.join(ROOT, "var", "stack.log"), "ab")
    proc = subprocess.Popen(args, cwd=ROOT, stdout=log, stderr=log,
                            start_new_session=True)
    # The parent forks four children; record the parent and its descendants.
    time.sleep(1.0)
    pids = [proc.pid]
    try:
        out = subprocess.check_output(["pgrep", "-P", str(proc.pid)], text=True)
        pids += [int(x) for x in out.split()]
    except subprocess.CalledProcessError:
        pass
    with open(PIDFILE, "w") as fh:
        json.dump({"db": db, "pids": pids}, fh)
    print("started:", pids)
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "restart"
    if cmd in ("stop", "restart"):
        stop()
    if cmd in ("start", "restart"):
        positional = [a for a in sys.argv[2:] if not a.startswith("--")]
        raise SystemExit(start(positional[0] if positional else "var/demo.sqlite3",
                               reset="--reset" in sys.argv))
