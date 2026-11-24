"""MCP Server for muse2api — exposes muse.ai tools to Antigravity."""

from __future__ import annotations

import asyncio
import os

import httpx

try:
    from mcp.server.mcpserver import MCPServer  # type: ignore[import-not-found]
except ImportError:
    from mcp.server.fastmcp import FastMCP as MCPServer  # type: ignore

mcp = MCPServer(name="muse-ai")

BASE_URL = os.environ.get("MUSE2API_URL", "http://127.0.0.1:18610")
API_KEY = os.environ.get("MUSE2API_API_KEY", "sk-muse2api-local-secret")
ADMIN_KEY = os.environ.get("MUSE2API_ADMIN_KEY", "sk-muse2api-admin-secret")

CONTENT_TYPE_JSON = "application/json"


@mcp.tool()
async def muse_chat(prompt: str, system_prompt: str = "", model: str = "muse-chat") -> str:
    """Send a prompt to muse.ai web chat via muse2api and get the AI completion."""
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": CONTENT_TYPE_JSON}
    payload = {"model": model, "messages": messages, "stream": False}

    async with httpx.AsyncClient(timeout=300.0) as client:
        try:
            resp = await client.post(f"{BASE_URL}/v1/chat/completions", headers=headers, json=payload)
            if resp.status_code != 200:
                return f"Error ({resp.status_code}): {resp.text}"
            data = resp.json()
            return data["choices"][0]["message"]["content"]
        except Exception as e:
            return f"Failed to connect to muse2api gateway at {BASE_URL}: {e}"


@mcp.tool()
async def muse_generate_image(prompt: str, size: str = "16:9") -> str:
    """Generate an image using muse.ai text-to-image."""
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": CONTENT_TYPE_JSON}
    payload = {"prompt": prompt, "size": size, "response_format": "url"}

    async with httpx.AsyncClient(timeout=240.0) as client:
        try:
            resp = await client.post(f"{BASE_URL}/v1/images/generations", headers=headers, json=payload)
            if resp.status_code != 200:
                return f"Error ({resp.status_code}): {resp.text}"
            data = resp.json()
            url = data["data"][0]["url"]
            return f"Image generated: {url}"
        except Exception as e:
            return f"Image generation failed: {e}"


@mcp.tool()
async def muse_generate_video(prompt: str, duration: int = 5, size: str = "16:9") -> str:
    """Generate a video using muse.ai text-to-video (submits task and polls until complete)."""
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": CONTENT_TYPE_JSON}
    payload = {"prompt": prompt, "duration": duration, "size": size}

    async with httpx.AsyncClient(timeout=600.0) as client:
        try:
            resp = await client.post(f"{BASE_URL}/v1/videos", headers=headers, json=payload)
            if resp.status_code != 200:
                return f"Error ({resp.status_code}): {resp.text}"
            task = resp.json()
            task_id = task["id"]

            # Poll for completion
            for _ in range(60):
                await asyncio.sleep(5)
                poll = await client.get(f"{BASE_URL}/v1/videos/{task_id}", headers=headers)
                if poll.status_code == 200:
                    pdata = poll.json()
                    status = pdata.get("status")
                    if status == "succeeded":
                        return f"Video generated successfully: {pdata.get('result', {}).get('url')}"
                    elif status in ("failed", "cancelled", "interrupted"):
                        return f"Video generation {status}: {pdata.get('error')}"

            return f"Video task {task_id} timed out polling."
        except Exception as e:
            return f"Video generation failed: {e}"


@mcp.tool()
async def muse_gateway_status() -> str:
    """Check health, account status, and available models on the muse2api gateway."""
    headers = {"Authorization": f"Bearer {ADMIN_KEY}"}
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            h_resp = await client.get(f"{BASE_URL}/healthz")
            acc_resp = await client.get(f"{BASE_URL}/admin/accounts", headers=headers)
            models_resp = await client.get(f"{BASE_URL}/v1/models", headers=headers)

            return (
                f"Gateway: {h_resp.status_code} ({h_resp.text})\n"
                f"Accounts: {acc_resp.text}\n"
                f"Models: {models_resp.text}"
            )
        except Exception as e:
            return f"Gateway unreachable at {BASE_URL}: {e}"


@mcp.tool()
async def muse_import_cookies(
    hatch_sess: str,
    hatch_native_auth_device: str,
    hatch_gw: str = "",
    label: str = "primary",
) -> str:
    """Import muse.ai session cookies into the muse2api account pool."""
    headers = {"Authorization": f"Bearer {ADMIN_KEY}", "Content-Type": CONTENT_TYPE_JSON}
    cookies = {
        "hatch_sess": hatch_sess.strip(),
        "hatch_native_auth_device": hatch_native_auth_device.strip(),
    }
    if hatch_gw.strip():
        cookies["hatch_gw"] = hatch_gw.strip()

    payload = {"label": label, "cookies": cookies}

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.post(f"{BASE_URL}/admin/accounts", headers=headers, json=payload)
            if resp.status_code in (200, 201):
                return f"Successfully imported muse.ai account '{label}': {resp.text}"
            return f"Failed to import account ({resp.status_code}): {resp.text}"
        except Exception as e:
            return f"Failed to connect to gateway: {e}"


if __name__ == "__main__":
    mcp.run()
