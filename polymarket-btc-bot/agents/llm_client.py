from __future__ import annotations

import json
from typing import Any

import aiohttp

from .advisor_settings import AdvisorSettings


async def complete_chat(
    session: aiohttp.ClientSession,
    settings: AdvisorSettings,
    system_prompt: str,
    user_prompt: str,
) -> str:
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    if settings.llm_provider == "ollama":
        return await _ollama_chat(session, settings, messages)
    return await _openai_compatible_chat(session, settings, messages)


async def _ollama_chat(
    session: aiohttp.ClientSession,
    settings: AdvisorSettings,
    messages: list[dict[str, str]],
) -> str:
    url = settings.ollama_base_url.rstrip("/") + "/api/chat"
    payload = {
        "model": settings.ollama_model,
        "messages": messages,
        "stream": False,
    }
    timeout = aiohttp.ClientTimeout(total=settings.advisor_http_timeout)
    async with session.post(url, json=payload, timeout=timeout) as resp:
        if resp.status >= 400:
            body = await resp.text()
            raise RuntimeError(f"Ollama HTTP {resp.status}: {body[:500]}")
        data = await resp.json()
    msg = data.get("message") or {}
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str) and content.strip():
        return content.strip()
    raise RuntimeError(f"Ollama unexpected response: {json.dumps(data)[:400]}")


async def _openai_compatible_chat(
    session: aiohttp.ClientSession,
    settings: AdvisorSettings,
    messages: list[dict[str, str]],
) -> str:
    key = (settings.openai_api_key or "").strip()
    if not key:
        raise RuntimeError("OPENAI_API_KEY is empty (required for openai_compatible provider)")

    url = settings.openai_api_base.rstrip("/") + "/chat/completions"
    payload: dict[str, Any] = {
        "model": settings.openai_model,
        "messages": messages,
        "temperature": 0.4,
    }
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    timeout = aiohttp.ClientTimeout(total=settings.advisor_http_timeout)
    async with session.post(url, json=payload, headers=headers, timeout=timeout) as resp:
        if resp.status >= 400:
            body = await resp.text()
            raise RuntimeError(f"LLM HTTP {resp.status}: {body[:500]}")
        data = await resp.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"LLM unexpected response: {json.dumps(data)[:400]}")
    msg = choices[0].get("message") or {}
    content = msg.get("content")
    if isinstance(content, str) and content.strip():
        return content.strip()
    raise RuntimeError("LLM returned empty content")
