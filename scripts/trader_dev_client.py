#!/usr/bin/env python3
"""
MoiraiCore — Trader.dev MCP Client
Connects to trader.dev MCP server for backtesting and strategy deployment.
Uses SSE transport with Bearer token auth.

MCP SSE Protocol:
1. GET /sse → opens SSE stream, receives 'event: endpoint' with session URL
2. POST /messages?sessionId=XXX → sends JSON-RPC requests
3. Responses come back through the SSE stream as 'event: message'
"""

import json
import os
import sys
import uuid
import logging
import threading
from typing import Optional
from queue import Queue, Empty

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("trader-dev")

MCP_URL = "https://mcp.trader.dev/sse"
TIMEOUT = 120


class TraderDevClient:
    """Direct MCP SSE client for trader.dev."""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.environ.get("TRADER_DEV_API_KEY", "")
        if not self.api_key:
            log.warning("No TRADER_DEV_API_KEY provided — client will fail on connect")
        self.session_id: Optional[str] = None
        self._messages_url: Optional[str] = None
        self._tools: list[dict] = []
        self._response_queue: Queue = Queue()
        self._pending: dict[str, Queue] = {}
        self._sse_thread: Optional[threading.Thread] = None
        self._client = httpx.Client(
            timeout=TIMEOUT,
            headers={"Authorization": f"Bearer {self.api_key}"},
        )

    def _parse_sse(self, text: str):
        """Parse an SSE message and return (event_type, data) or None."""
        data_line = None
        event_type = "message"
        for line in text.strip().split("\n"):
            if line.startswith("data:"):
                data_line = line[5:].strip()
            elif line.startswith("event:"):
                event_type = line[6:].strip()
        if data_line:
            return event_type, data_line
        return None, None

    def _sse_listener(self):
        """Background thread that listens to the SSE stream."""
        try:
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "text/event-stream",
            }
            with self._client.stream("GET", MCP_URL, headers=headers) as resp:
                log.info("SSE listener started (HTTP %s)", resp.status_code)
                buffer = ""
                for chunk in resp.iter_text():
                    buffer += chunk
                    while "\n\n" in buffer:
                        parts = buffer.split("\n\n", 1)
                        message_text = parts[0]
                        buffer = parts[1] if len(parts) > 1 else ""

                        event_type, data_line = self._parse_sse(message_text)
                        if not data_line:
                            continue

                        # Endpoint event — extract session URL
                        if event_type == "endpoint":
                            if "sessionId=" in data_line:
                                self.session_id = data_line.split("sessionId=")[1].split("&")[0]
                                self._messages_url = data_line
                                log.info("Session: %s...", self.session_id[:8])
                                self._response_queue.put(("endpoint", data_line))
                            continue

                        # Message event — parse JSON-RPC response
                        if event_type == "message":
                            try:
                                data = json.loads(data_line)
                                req_id = data.get("id")
                                if req_id and req_id in self._pending:
                                    self._pending[req_id].put(data)
                                else:
                                    self._response_queue.put(("message", data))
                            except json.JSONDecodeError:
                                log.warning("Non-JSON SSE message: %s", data_line[:100])
        except Exception as e:
            log.error("SSE listener error: %s", e)
            self._response_queue.put(("error", str(e)))

    def connect(self) -> bool:
        """Connect to trader.dev MCP server and discover tools."""
        if not self.api_key:
            log.error("No API key provided — set TRADER_DEV_API_KEY environment variable")
            return False
        # Start SSE listener in background
        self._sse_thread = threading.Thread(target=self._sse_listener, daemon=True)
        self._sse_thread.start()

        # Wait for endpoint event
        try:
            event_type, data = self._response_queue.get(timeout=10)
            if event_type == "error":
                log.error("Connection failed: %s", data)
                return False
            if event_type != "endpoint":
                log.error("Unexpected first event: %s", event_type)
                return False
        except Empty:
            log.error("Timed out waiting for endpoint event")
            return False

        # Now list tools
        return self._list_tools()

    def _post_request(self, method: str, params: dict, timeout: float = 30) -> Optional[dict]:
        """Send a JSON-RPC request and wait for the response."""
        request_id = str(uuid.uuid4())[:8]
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }

        # Create a queue for this request's response
        response_queue: Queue = Queue()
        self._pending[request_id] = response_queue

        # POST to the messages endpoint
        base_url = MCP_URL.rsplit("/", 1)[0]
        messages_url = f"{base_url}{self._messages_url}"
        try:
            resp = self._client.post(messages_url, json=payload)
            if resp.status_code not in (200, 202):
                log.error("Request failed: HTTP %s — %s", resp.status_code, resp.text[:200])
                del self._pending[request_id]
                return None
        except Exception as e:
            log.error("Request error: %s", e)
            del self._pending[request_id]
            return None

        # Wait for response from SSE stream
        try:
            response = response_queue.get(timeout=timeout)
            del self._pending[request_id]
            return response
        except Empty:
            log.error("Timed out waiting for response to %s", method)
            del self._pending[request_id]
            return None

    def _list_tools(self) -> bool:
        """Send tools/list request to discover available tools."""
        response = self._post_request("tools/list", {})
        if response and "result" in response:
            self._tools = response["result"].get("tools", [])
            log.info("Discovered %d tools", len(self._tools))
            for t in self._tools:
                log.info("  - %s: %s", t["name"], t.get("description", "")[:80])
            return True
        log.error("tools/list failed: %s", response)
        return False

    @property
    def tools(self) -> list[dict]:
        return self._tools

    def call_tool(self, name: str, arguments: dict) -> dict:
        """Call a tool on the trader.dev MCP server."""
        if not self._messages_url:
            raise RuntimeError("Not connected. Call connect() first.")

        response = self._post_request("tools/call", {"name": name, "arguments": arguments})
        if response:
            if "error" in response:
                log.error("Tool call error: %s", response["error"])
                return {"error": response["error"]}
            return response.get("result", {})
        return {"error": "No response"}

    def close(self):
        self._client.close()


def main():
    """Test connection and list available tools."""
    client = TraderDevClient()
    try:
        if client.connect():
            print("\n✅ Connected to trader.dev")
            print(f"\nAvailable tools ({len(client.tools)}):")
            for t in client.tools:
                desc = t.get("description", "No description")[:100]
                print(f"  • {t['name']}: {desc}")
        else:
            print("❌ Failed to connect to trader.dev")
            sys.exit(1)
    finally:
        client.close()


if __name__ == "__main__":
    main()
