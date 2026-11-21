"""Credential-injecting loopback proxy for external free providers (OpenRouter and other OpenAI-compatible APIs).

Why: OpenCode's file tools run inside the OpenCode process, and a process can always read its own
environment (/proc/self/environ). If a provider key is in that environment, only OpenCode's application-level
permission rules stand between the key and a model-driven tool. So the real key never enters the jail:

* the jailed OpenCode worker gets a random per-run TOKEN in the provider's key variable and a loopback
  baseURL pointing at this proxy, which runs in the orchestrator, outside the jail;
* the proxy accepts only `POST <prefix>/chat/completions` carrying that token and naming an APPROVED model
  (the route's own zero-priced model). Anything else is refused before it reaches the provider. This is also a
  hard guarantee against paid inference: OpenCode makes auxiliary "small model" calls on its own, and on
  OpenRouter its default was a *paid* model (observed live);
* it swaps the token for the real key, forwards the request upstream, and records the model the provider says
  it actually served.
"""
from __future__ import annotations

import http.client
import json
import secrets
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
       "transfer-encoding", "upgrade", "content-length", "content-encoding", "host", "authorization"}


class ProxyRefused(RuntimeError):
    pass


def _loopback(host: str) -> bool:
    return host in ("127.0.0.1", "::1", "localhost")


def served_model(body: bytes) -> str | None:
    """The `model` the upstream reports, from a JSON body or an SSE stream (last one wins)."""
    model = None
    text = body.decode("utf-8", errors="replace")
    candidates = [text] + [ln[5:].strip() for ln in text.splitlines() if ln.startswith("data:")]
    for c in candidates:
        if not c.startswith("{"):
            continue
        try:
            obj = json.loads(c)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("model"), str) and obj["model"]:
            model = obj["model"]
    return model


def reported_cost(body: bytes):
    """Observe provider usage, independent of OpenCode's configured (zero) price table."""
    text = body.decode("utf-8", errors="replace")
    costs = []
    for candidate in [text] + [ln[5:].strip() for ln in text.splitlines() if ln.startswith("data:")]:
        try:
            obj = json.loads(candidate)
        except ValueError:
            continue
        if not isinstance(obj, dict) or not isinstance(obj.get("usage"), dict):
            continue
        for name in ("cost", "total_cost", "cost_microdollars"):
            if name in obj["usage"]:
                from .federation import finite_number
                cost = finite_number(obj["usage"][name])
                if cost is None or cost != 0:
                    return "nonzero-or-ambiguous"
                costs.append(cost)
    return 0 if costs else None


class KeyProxy:
    def __init__(self, upstream_base: str, key: str, allowed_models: set[str], log_path: Path, timeout: float = 600,
                 *, anonymous: bool = False, route=None, inspect_request=None):
        u = urlparse(upstream_base or "")
        if not u.hostname or not (u.scheme == "https" or (u.scheme == "http" and _loopback(u.hostname))):
            raise ProxyRefused(f"upstream {upstream_base!r} must be an https URL (http only on loopback)")
        if u.username or u.password or u.query or u.fragment:
            raise ProxyRefused("upstream URL must not contain credentials, query or fragment")
        if not key and not anonymous:
            raise ProxyRefused("no provider key available")
        self.route = route
        self.inspect_request = inspect_request
        if not allowed_models:
            raise ProxyRefused("no approved model")
        self.upstream, self.prefix = u, u.path.rstrip("/")
        self.key, self.allowed, self.timeout = key, set(allowed_models), timeout
        self.token = "nicks-proxy-" + secrets.token_hex(24)
        self.log_path = log_path
        self._lock = threading.Lock()
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _reply(self, status: int, body: bytes, ctype: str = "application/json", headers=()):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                for k, v in headers:
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _refuse(self, status: int, why: str, model=None):
                proxy._log({"decision": "refused", "status": status, "reason": why, "requested_model": model,
                            "path": self.path})
                self._reply(status, json.dumps({"error": {"message": f"nicks key proxy: {why}", "code": status}}).encode())

            def do_GET(self):
                self._refuse(403, "only POST chat/completions is allowed")

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                if self.headers.get("Authorization") != f"Bearer {proxy.token}":
                    return self._refuse(401, "missing or wrong per-run token")
                if self.path.split("?")[0] != f"{proxy.prefix}/chat/completions":
                    return self._refuse(403, f"path {self.path} is not allowed")
                try:
                    req = json.loads(body or b"{}")
                except json.JSONDecodeError:
                    return self._refuse(400, "request body is not JSON")
                if not isinstance(req, dict) or not isinstance(req.get("model"), str):
                    return self._refuse(400, "request must be an object with a model string")
                model = req.get("model")
                if model not in proxy.allowed:
                    return self._refuse(403, f"model {model!r} is not an approved free route", model)
                if proxy.route and proxy.route.adapter == "openai":
                    from .federation import dispatch_block
                    if why := dispatch_block(proxy.route):
                        return self._refuse(403, why, model)
                    # Do not let model-driven requests opt into paid tools, tiers, BYOK or alternate models.
                    safe = {"model", "messages", "stream", "stream_options", "max_tokens", "max_completion_tokens",
                            "temperature", "top_p", "top_k", "stop", "seed", "presence_penalty", "frequency_penalty",
                            "tools", "tool_choice", "parallel_tool_calls", "response_format", "reasoning_effort",
                            "reasoning", "include_reasoning"}
                    requested_tools = req.get("tools", [])
                    if (set(req) - safe or not isinstance(requested_tools, list) or
                            any(not isinstance(t, dict) or t.get("type") != "function" for t in requested_tools)):
                        return self._refuse(403, "unapproved request options or billed provider tools", model)
                if proxy.inspect_request is not None:
                    try:
                        proxy.inspect_request(body)
                    except ValueError as exc:
                        return self._refuse(413, f"request contract rejected: {exc}", model)
                status, headers, data, err = proxy._forward(body, self.headers)
                proxy._log({"decision": "forwarded", "status": status, "requested_model": model,
                            "served_model": served_model(data) if data else None, "bytes": len(data or b""),
                            "reported_cost": reported_cost(data) if data else None,
                            "error": err})
                if err:
                    return self._reply(502, json.dumps({"error": {"message": f"nicks key proxy: {err}"}}).encode())
                ctype = next((v for k, v in headers if k.lower() == "content-type"), "application/json")
                extra = [(k, v) for k, v in headers if k.lower() not in HOP and k.lower() != "content-type"]
                self._reply(status, data, ctype, extra)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @classmethod
    def for_route(cls, route, key: str, log_path: Path, timeout: float = 600, *, inspect_request=None) -> "KeyProxy":
        """A proxy that forwards only `route`'s own model (the zero-priced route the policy approved)."""
        if not getattr(route, "eligible", False):
            raise ProxyRefused(f"route {route.route} is not eligible")
        from .federation import dispatch_block
        if why := dispatch_block(route):
            raise ProxyRefused(why)
        return cls(route.endpoint, key, {route.model}, log_path, timeout,
                   anonymous=route.adapter == "openai" and not route.auth_required, route=route,
                   inspect_request=inspect_request)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}{self.prefix}"

    def _log(self, rec: dict) -> None:
        with self._lock, open(self.log_path, "a") as f:
            f.write(json.dumps({"t": round(time.time(), 3), **rec}) + "\n")

    def _forward(self, body: bytes, in_headers):
        u = self.upstream
        conn_cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
        kw = {"context": ssl.create_default_context()} if u.scheme == "https" else {}
        headers = {"Content-Type": "application/json", "Accept": in_headers.get("Accept", "*/*"),
                   "Accept-Encoding": "identity"}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        try:
            conn = conn_cls(u.hostname, u.port, timeout=self.timeout, **kw)
            conn.request("POST", f"{self.prefix}/chat/completions", body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            conn.close()
            return resp.status, resp.getheaders(), data, None
        except (OSError, http.client.HTTPException) as exc:
            return 502, [], b"", f"upstream unreachable: {exc.__class__.__name__}"

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        if self.thread.is_alive():
            self.server.shutdown()
        self.server.server_close()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()


def proxy_records(log_path: Path) -> list[dict]:
    if not log_path.exists():
        return []
    return [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
