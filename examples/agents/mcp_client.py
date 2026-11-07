"""A tiny MCP client (Streamable HTTP, JSON responses) for the example agents."""

import httpx


class ToolError(Exception):
    """The tool answered with success false."""


class MCP:
    def __init__(self, url, timeout=10.0):
        self.url = url
        self.http = httpx.Client(timeout=timeout)
        self.session = None
        self.next_id = 0
        self._rpc("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "example-agent", "version": "1.0"},
        })
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _post(self, payload):
        headers = {"Accept": "application/json, text/event-stream"}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        response = self.http.post(self.url, json=payload, headers=headers)
        response.raise_for_status()
        self.session = response.headers.get("Mcp-Session-Id", self.session)
        return response.json() if response.content else None

    def _rpc(self, method, params):
        self.next_id += 1
        reply = self._post({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params})
        if "error" in reply:
            raise ToolError(reply["error"]["message"])
        return reply["result"]

    def call(self, tool, **arguments):
        result = self._rpc("tools/call", {"name": tool, "arguments": arguments})
        body = result["structuredContent"]
        if result.get("isError"):
            raise ToolError(body["error"]["message"])
        return body["data"]
