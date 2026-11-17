"""Minimal JSON-over-HTTP plumbing shared by the three services.

We deliberately avoid a web framework: the services must be startable in a
bare environment and stay small enough to audit.  Every handler returns
(status, dict) and exceptions become structured JSON errors.
"""

from __future__ import annotations

import json
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

MAX_BODY = 8 * 1024 * 1024


class ApiError(Exception):
    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra

    def as_dict(self) -> dict:
        return {"error": self.message, **self.extra}


class Router:
    def __init__(self, name: str):
        self.name = name
        self.routes = []  # (method, prefix-segments, handler)
        self.static_dir = None
        self.static_prefix = "/"

    def route(self, method: str, pattern: str):
        parts = [p for p in pattern.split("/") if p]

        def deco(fn):
            self.routes.append((method.upper(), parts, fn))
            return fn
        return deco

    def match(self, method: str, path: str):
        segments = [p for p in path.split("/") if p]
        for r_method, parts, fn in self.routes:
            if r_method != method or len(parts) != len(segments):
                continue
            args = {}
            ok = True
            for spec, actual in zip(parts, segments):
                if spec.startswith("{") and spec.endswith("}"):
                    args[spec[1:-1]] = actual
                elif spec != actual:
                    ok = False
                    break
            if ok:
                return fn, args
        return None, None


def make_handler(router: Router, services: dict):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            sys.stderr.write(f"[{router.name}] {self.address_string()} {fmt % args}\n")

        def _send(self, status: int, payload: bytes, content_type: str,
                  extra_headers: dict | None = None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra_headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)

        def _json(self, status: int, obj):
            self._send(status, json.dumps(obj, ensure_ascii=False,
                                          sort_keys=True).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _read_json(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ApiError(413, "request body too large")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw.decode("utf-8") or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ApiError(400, f"invalid JSON body: {exc}")
            if not isinstance(data, dict):
                raise ApiError(400, "JSON body must be an object")
            return data

        def _dispatch(self, method: str):
            parsed = urlparse(self.path)
            path = parsed.path
            query = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
            if router.static_dir and method == "GET" and not path.startswith("/api/"):
                return self._static(path)
            handler, args = router.match(method, path)
            if handler is None:
                return self._json(404, {"error": f"no route for {method} {path}"})
            try:
                body = self._read_json() if method in ("POST", "PUT", "PATCH") else {}
                result = handler(services, body, query, **args)
                if isinstance(result, tuple):
                    status, payload = result
                else:
                    status, payload = 200, result
                self._json(status, payload)
            except ApiError as exc:
                self._json(exc.status, exc.as_dict())
            except KeyError as exc:
                self._json(404, {"error": str(exc.args[0]) if exc.args else "not found"})
            except (ValueError, TypeError) as exc:
                self._json(400, {"error": str(exc)})
            except Exception as exc:  # pragma: no cover - defensive
                traceback.print_exc()
                self._json(500, {"error": f"internal error: {exc}"})

        def _static(self, path: str):
            import mimetypes
            import os
            rel = path.lstrip("/") or "index.html"
            if rel.endswith("/"):
                rel += "index.html"
            base = os.path.realpath(router.static_dir)
            full = os.path.realpath(os.path.join(base, rel))
            if not full.startswith(base + os.sep) and full != base:
                return self._json(403, {"error": "forbidden"})
            if not os.path.isfile(full):
                return self._json(404, {"error": f"not found: {path}"})
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            with open(full, "rb") as fh:
                data = fh.read()
            self._send(200, data, ctype)

        def do_GET(self):
            self._dispatch("GET")

        def do_HEAD(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PUT(self):
            self._dispatch("PUT")

        def do_PATCH(self):
            self._dispatch("PATCH")

        def do_DELETE(self):
            self._dispatch("DELETE")

    return Handler


def serve(name: str, router: Router, services: dict, host: str, port: int):
    server = ThreadingHTTPServer((host, port), make_handler(router, services))
    server.daemon_threads = True
    sys.stderr.write(f"[{name}] listening on http://{host}:{port}\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
