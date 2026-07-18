from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from faervell_npc.config import get_settings
from faervell_npc.models import ModelCall

T = TypeVar("T", bound=BaseModel)


class LLMUnavailable(RuntimeError):
    pass


@dataclass(slots=True)
class LLMResult:
    content: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0


class OpenRouterClient:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.client = httpx.AsyncClient(
            base_url=self.settings.openrouter_base_url,
            timeout=httpx.Timeout(60.0, connect=10.0),
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def chat(
        self,
        session: AsyncSession,
        *,
        kind: str,
        scene_id: str | None,
        models: list[str],
        messages: list[dict[str, Any]],
        max_tokens: int,
        temperature: float,
        schema_model: type[T] | None = None,
    ) -> tuple[LLMResult, T | None]:
        if not self.settings.llm_enabled:
            raise LLMUnavailable("OPENROUTER_API_KEY is not configured")
        if not models:
            raise LLMUnavailable("No model configured")

        body: dict[str, Any] = {
            "models": models,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if schema_model is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_model.__name__.lower(),
                    "strict": True,
                    "schema": schema_model.model_json_schema(),
                },
            }
            body["provider"] = {"require_parameters": True}

        headers = {
            "Authorization": f"Bearer {self.settings.openrouter_api_key}",
            "Content-Type": "application/json",
        }
        if self.settings.openrouter_site_url:
            headers["HTTP-Referer"] = self.settings.openrouter_site_url
        if self.settings.openrouter_app_name:
            headers["X-OpenRouter-Title"] = self.settings.openrouter_app_name

        started = time.perf_counter()
        model_name = models[0]
        try:
            response = await self.client.post("/chat/completions", headers=headers, json=body)
            response.raise_for_status()
            payload = response.json()
            model_name = payload.get("model") or model_name
            message = payload["choices"][0]["message"]
            content = message.get("content") or ""
            if isinstance(content, list):
                content = "".join(
                    item.get("text", "") for item in content if isinstance(item, dict)
                )
            elif isinstance(content, dict):
                content = json.dumps(content, ensure_ascii=False)
            usage = payload.get("usage") or {}
            result = LLMResult(
                content=content,
                model=model_name,
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                cost_usd=float(usage.get("cost") or 0.0),
            )
            parsed: T | None = None
            if schema_model is not None:
                parsed = schema_model.model_validate_json(content)
            session.add(
                ModelCall(
                    kind=kind,
                    model=model_name,
                    scene_id=scene_id,
                    prompt_tokens=result.prompt_tokens,
                    completion_tokens=result.completion_tokens,
                    cost_usd=result.cost_usd,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    success=True,
                )
            )
            return result, parsed
        except (httpx.HTTPError, KeyError, ValueError, json.JSONDecodeError) as exc:
            session.add(
                ModelCall(
                    kind=kind,
                    model=model_name,
                    scene_id=scene_id,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    success=False,
                    error=str(exc)[:2000],
                )
            )
            raise LLMUnavailable(str(exc)) from exc
