"""A minimal MCP server (Streamable HTTP, JSON responses) in front of a Shop.

Speaks enough of the protocol for MCP clients to connect: initialize, notifications, ping,
tools/list and tools/call. One endpoint, POST only; GET answers 405 (no server-sent stream).
A tools/call whose outcome is "lost" gets no response at all: the connection is closed after
the shop has applied the call, which is what a timeout or a dropped connection looks like.
"""

from __future__ import annotations

import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple

from carbon import __version__
from carbon.agent.shop import TOOLS, Shop, ToolError

PATH = "/food"
_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
_LATEST = "2025-06-18"


class _Lost(Exception):
    """Close the connection without answering."""


class _Http(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _envelope_text(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {"type": "text", "text": json.dumps(payload, ensure_ascii=False)}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: "AgentServer"

    def log_message(self, *args: Any) -> None:  # quiet
        pass

    def _send(self, status: int, body: Optional[Dict[str, Any]] = None,
              headers: Optional[Dict[str, str]] = None) -> None:
        raw = b"" if body is None else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        if body is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if raw:
            self.wfile.write(raw)

    def do_GET(self) -> None:
        self._send(405, None, {"Allow": "POST"})

    def do_DELETE(self) -> None:
        self._send(200)

    def do_POST(self) -> None:
        if self.path.split("?", 1)[0].rstrip("/") != PATH:
            self._send(404, {"error": f"MCP endpoint is {PATH}"})
            return
        try:
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            message = json.loads(raw or b"null")
        except (ValueError, json.JSONDecodeError):
            self._send(400, {"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32700, "message": "Parse error"}})
            return
        batch = isinstance(message, list)
        try:
            replies = [r for r in (self._one(m) for m in (message if batch else [message])) if r]
        except _Lost:
            self.close_connection = True
            return
        except _Http as e:
            self._send(e.status, {"success": False, "error": {"message": e.message}})
            return
        headers = {}
        if not batch and isinstance(message, dict) and message.get("method") == "initialize":
            headers["Mcp-Session-Id"] = self.server.session_id
        if not replies:
            self._send(202, None, headers)
        else:
            self._send(200, replies if batch else replies[0], headers)

    def _one(self, msg: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
        if "method" not in msg or "id" not in msg:
            return None  # a notification or a response: accepted, nothing to answer
        rid, method, params = msg["id"], msg["method"], msg.get("params") or {}
        try:
            result = self._dispatch(method, params)
        except _RpcError as e:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": e.code, "message": e.message}}
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    def _dispatch(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        if method == "initialize":
            asked = params.get("protocolVersion")
            return {
                "protocolVersion": asked if asked in _VERSIONS else _LATEST,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "carbon-agent-food", "version": __version__},
                "instructions": (
                    "carbon-layer test shop (not a real store). Order the cart for the user. "
                    + self.server.shop.scenario.task
                ),
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": TOOLS}
        if method == "tools/call":
            name = params.get("name")
            try:
                kind, value = self.server.shop.call(name, params.get("arguments") or {})
            except KeyError:
                raise _RpcError(-32602, f"Unknown tool: {name}")
            except ToolError as e:
                body = {"success": False, "error": {"message": str(e)}}
                return {"content": [_envelope_text(body)], "structuredContent": body, "isError": True}
            if kind == "lost":
                raise _Lost()
            if kind == "http":
                status, message = value
                raise _Http(status, message)
            body = {"success": True, "data": value}
            return {"content": [_envelope_text(body)], "structuredContent": body, "isError": False}
        raise _RpcError(-32601, f"Method not found: {method}")


class _RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class AgentServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, shop: Shop, host: str = "127.0.0.1", port: int = 0) -> None:
        super().__init__((host, port), _Handler)
        self.shop = shop
        self.session_id = uuid.uuid4().hex
        self._thread: Optional[threading.Thread] = None

    @property
    def address(self) -> Tuple[str, int]:
        host, port = self.server_address[:2]
        return str(host), int(port)

    @property
    def url(self) -> str:
        host, port = self.address
        return f"http://{host}:{port}{PATH}"

    def start(self) -> "AgentServer":
        self._thread = threading.Thread(target=self.serve_forever, name="carbon-agent", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self.shutdown()
        self.server_close()
        if self._thread:
            self._thread.join(timeout=5)
