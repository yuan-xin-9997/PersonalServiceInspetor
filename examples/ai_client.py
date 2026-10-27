"""Official SDK MCP client; inject secrets and CA path through protected environment variables."""
from __future__ import annotations

import asyncio
import os
import ssl
import time

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


class AutomaticToken(httpx.Auth):
    def __init__(self, base: str, key: str, context: ssl.SSLContext):
        self.base, self.key, self.context = base, key, context
        self.token, self.expiry = "", 0
        self.lock = asyncio.Lock()

    async def async_auth_flow(self, request):
        async with self.lock:
            if time.time() >= self.expiry - 30:
                async with httpx.AsyncClient(verify=self.context) as client:
                    response = await client.post(self.base + "/api/v1/token", headers={"Authorization": "Bearer " + self.key})
                    response.raise_for_status()
                    grant = response.json()
                    self.token = grant["access_token"]
                    self.expiry = time.time() + grant["expires_in"]
        request.headers["Authorization"] = "Bearer " + self.token
        yield request


async def main():
    base = os.environ["INSPECTOR_URL"].rstrip("/")
    if not base.startswith("https://"):
        raise ValueError("Remote clients must use HTTPS")
    context = ssl.create_default_context(cafile=os.environ.get("INSPECTOR_CA_FILE"))
    auth = AutomaticToken(base, os.environ["INSPECTOR_API_KEY"], context)
    async with (
        httpx.AsyncClient(auth=auth, verify=context, timeout=30) as http,
        streamable_http_client(base + "/mcp", http_client=http) as streams,
        ClientSession(streams[0], streams[1]) as client,
    ):
        await client.initialize()
        tools = await client.list_tools()
        print("Tools:", ", ".join(tool.name for tool in tools.tools))
        result = await client.call_tool("get_summary", {})
        if result.is_error:
            raise RuntimeError("Business query failed; verify the user's current permissions")
        print("Summary:", result.structured_content)


if __name__ == "__main__":
    asyncio.run(main())
