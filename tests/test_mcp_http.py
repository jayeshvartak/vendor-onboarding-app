"""Vendor Master MCP server accepts a streamable HTTP client."""
import asyncio
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = tempfile.mkdtemp(prefix="vo-mcp-http-")
os.environ["VO_STORE_DIR"] = ROOT
os.environ["VO_SECRET"] = "test-secret"


def _free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


PORT = _free_port()
os.environ["VO_MCP_HOST"] = "127.0.0.1"
os.environ["VO_MCP_PORT"] = str(PORT)
os.environ["VO_MCP_PATH"] = "/mcp"
Path(ROOT, "requests.json").write_text('{"requests": [], "auditLog": []}\n', encoding="utf-8")

import mcp_server  # noqa: E402


class StreamableHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.thread = threading.Thread(target=mcp_server.serve, name="mcp-http-test", daemon=True)
        cls.thread.start()
        deadline = time.time() + 8
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", PORT), 0.2).close()
                return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError(f"streamable HTTP server did not listen on {PORT}")

    def test_missing_or_wrong_token_is_rejected(self):
        self.assertEqual(_initialize(None)[0], 401)
        self.assertEqual(_initialize("wrong-token")[0], 401)
        status, body = _initialize(mcp_server.mcp_token())
        self.assertEqual(status, 200)
        self.assertIn(b"vendor-master", body)

    def test_client_lists_vendor_master_tools(self):
        init, listed = asyncio.run(_connect())
        self.assertEqual(init.server_info.name, "vendor-master")
        names = {tool.name for tool in listed.tools}
        self.assertIn("submit_onboarding_request", names)
        self.assertIn("get_onboarding_status", names)
        self.assertNotIn("create_onboarding_request", names)
        self.assertNotIn("submit_vendor_intake", names)
        self.assertEqual(mcp_server.client_url(), f"http://127.0.0.1:{PORT}/mcp")
        self.assertEqual(mcp_server.mcp_token(), "2026-str-mcp")


def _initialize(token):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        mcp_server.client_url(),
        data=b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"0"}}}',
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


async def _connect():
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    headers = {"Authorization": f"Bearer {mcp_server.mcp_token()}"}
    async with httpx2.AsyncClient(headers=headers) as http:
        async with streamable_http_client(mcp_server.client_url(), http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                listed = await session.list_tools()
                return init, listed


if __name__ == "__main__":
    unittest.main()
