"""NVIDIA Build chat client. Tool calls use the OpenAI-compatible completions API."""

import json
import os
from dataclasses import dataclass, field

import httpx


class NvidiaError(RuntimeError):
    pass


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class Completion:
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)


def parse_completion(data):
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise NvidiaError(f"unexpected model response: {data!r}"[:500]) from exc
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part) for part in content
        )
    calls = []
    for index, tool_call in enumerate(message.get("tool_calls") or []):
        function = tool_call.get("function") or {}
        raw = function.get("arguments") or {}
        if isinstance(raw, str):
            try:
                raw = json.loads(raw or "{}")
            except json.JSONDecodeError:
                raw = {}
        if not isinstance(raw, dict):
            raw = {}
        calls.append(
            ToolCall(
                id=str(tool_call.get("id") or f"call_{index}"),
                name=str(function.get("name") or ""),
                arguments=raw,
            )
        )
    return Completion(content=content, tool_calls=calls)


class NvidiaModel:
    def ensure_ready(self):
        if not os.environ.get("NVIDIA_API_KEY"):
            raise NvidiaError("NVIDIA_API_KEY is not set")

    def complete(self, messages, tools):
        self.ensure_ready()
        payload = {
            "model": os.environ.get("NVIDIA_MODEL", "nvidia/nemotron-3.5-lightning-30b-a3b"),
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "temperature": 0.2,
            "max_tokens": 1024,
        }
        base = os.environ.get("NVIDIA_API_BASE", "https://integrate.api.nvidia.com/v1").rstrip("/")
        try:
            response = httpx.post(
                f"{base}/chat/completions",
                headers={
                    "Authorization": f"Bearer {os.environ['NVIDIA_API_KEY']}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=90,
            )
        except httpx.HTTPError as exc:
            raise NvidiaError(str(exc)) from exc
        if response.status_code >= 300:
            raise NvidiaError(f"NVIDIA {response.status_code}: {response.text[:400]}")
        return parse_completion(response.json())
