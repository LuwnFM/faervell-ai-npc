from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from faervell_npc.config import get_settings
from faervell_npc.models import ModelCall

T = TypeVar("T", bound=BaseModel)
logger = logging.getLogger("uvicorn.error")


class LLMUnavailable(RuntimeError):
    pass


@dataclass(slots=True)
class CatalogModel:
    id: str
    prompt_per_million: float
    completion_per_million: float
    request_price: float
    context_length: int
    supported_parameters: set[str] = field(default_factory=set)
    free: bool = False


@dataclass(slots=True)
class LLMResult:
    content: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    selection_reason: str = ""
    finish_reason: str = ""
    native_finish_reason: str = ""


class OpenRouterClient:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.client = httpx.AsyncClient(
            base_url=self.settings.openrouter_base_url,
            timeout=httpx.Timeout(
                float(self.settings.openrouter_response_timeout_seconds), connect=20.0
            ),
        )
        self._catalog: dict[str, CatalogModel] = {}
        self._catalog_loaded_at: datetime | None = None
        logger.info(
            "OpenRouter policy preferred_actor=%s preferred_planner=%s dynamic_catalog=%s "
            "max_prompt_per_m=%.3f max_completion_per_m=%.3f paid_fallback=%s blocklist=%s",
            ",".join(self.settings.effective_actor_models),
            ",".join(self.settings.effective_planner_models),
            self.settings.openrouter_dynamic_catalog,
            self.settings.openrouter_max_prompt_price_per_million,
            self.settings.openrouter_max_completion_price_per_million,
            self.settings.openrouter_allow_paid_fallback,
            ",".join(self.settings.model_blocklist),
        )

    async def close(self) -> None:
        await self.client.aclose()

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.settings.openrouter_api_key}",
            "Content-Type": "application/json",
        }
        if self.settings.openrouter_site_url:
            headers["HTTP-Referer"] = self.settings.openrouter_site_url
        if self.settings.openrouter_app_name:
            headers["X-OpenRouter-Title"] = self.settings.openrouter_app_name
        return headers

    async def _load_catalog(self, *, force: bool = False) -> dict[str, CatalogModel]:
        if not self.settings.openrouter_dynamic_catalog:
            return {}
        now = datetime.now(UTC)
        if (
            not force
            and self._catalog
            and self._catalog_loaded_at
            and now - self._catalog_loaded_at
            < timedelta(seconds=self.settings.openrouter_catalog_ttl_seconds)
        ):
            return self._catalog
        try:
            response = await self.client.get(
                "/models",
                params={"output_modalities": "text", "sort": "most-popular"},
                headers=self._headers(),
            )
            response.raise_for_status()
            payload = response.json()
            catalog: dict[str, CatalogModel] = {}
            for item in payload.get("data") or []:
                model_id = str(item.get("id") or "").strip()
                if not model_id or self._blocked(model_id):
                    continue
                architecture = item.get("architecture") or {}
                outputs = set(architecture.get("output_modalities") or ["text"])
                if "text" not in outputs:
                    continue
                pricing = item.get("pricing") or {}
                prompt = self._price_per_million(pricing.get("prompt"))
                completion = self._price_per_million(pricing.get("completion"))
                request = self._float(pricing.get("request"))
                catalog[model_id] = CatalogModel(
                    id=model_id,
                    prompt_per_million=prompt,
                    completion_per_million=completion,
                    request_price=request,
                    context_length=int(item.get("context_length") or 0),
                    supported_parameters=set(item.get("supported_parameters") or []),
                    free=prompt == 0.0 and completion == 0.0 and request == 0.0,
                )
            self._catalog = catalog
            self._catalog_loaded_at = now
            logger.info("OpenRouter catalog loaded: %d text models", len(catalog))
        except Exception as exc:
            logger.warning("OpenRouter catalog refresh failed: %s: %s", type(exc).__name__, exc)
        return self._catalog

    @staticmethod
    def _float(value: object) -> float:
        try:
            return float(str(value or 0.0))
        except (TypeError, ValueError):
            return float("inf")

    @classmethod
    def _price_per_million(cls, value: object) -> float:
        price = cls._float(value)
        if price == float("inf"):
            return price
        return price * 1_000_000.0

    def _blocked(self, model_id: str) -> bool:
        folded = model_id.casefold()
        return any(token.casefold().strip() in folded for token in self.settings.model_blocklist if token.strip())

    def _catalog_allowed(self, item: CatalogModel) -> bool:
        if item.free:
            return True
        if not self.settings.openrouter_allow_paid_fallback:
            return False
        return (
            item.prompt_per_million <= self.settings.openrouter_max_prompt_price_per_million
            and item.completion_per_million
            <= self.settings.openrouter_max_completion_price_per_million
            and (
                item.request_price == 0.0
                if self.settings.openrouter_max_request_price_usd <= 0
                else item.request_price <= self.settings.openrouter_max_request_price_usd
            )
        )

    async def resolve_models(
        self,
        *,
        kind: str,
        preferred: list[str],
        schema_required: bool,
        free_only: bool = False,
        exclude: set[str] | None = None,
    ) -> tuple[list[CatalogModel], str]:
        catalog = await self._load_catalog()
        exclude_folded = {item.casefold() for item in (exclude or set())}
        preferred_clean = self.settings.filter_allowed_models(preferred)
        planner = kind.upper().startswith("PLANNER")

        eligible: list[CatalogModel] = []
        for item in catalog.values():
            if item.id.casefold() in exclude_folded or not self._catalog_allowed(item):
                continue
            if free_only and not item.free:
                continue
            # Structured output support improves reliability but is not an allowlist:
            # plain-JSON parsing remains available for every otherwise eligible model.
            eligible.append(item)

        by_id = {item.id.casefold(): item for item in eligible}
        ordered: list[CatalogModel] = []
        seen: set[str] = set()
        for model_id in preferred_clean:
            preferred_item = by_id.get(model_id.casefold())
            if preferred_item and preferred_item.id.casefold() not in seen:
                ordered.append(preferred_item)
                seen.add(preferred_item.id.casefold())

        # No free whitelist: all catalogued free models are allowed except the explicit blocklist.
        free_models = [item for item in eligible if item.free and item.id.casefold() not in seen]
        free_models.sort(
            key=lambda item: (
                0
                if planner and {"structured_outputs", "response_format"} & item.supported_parameters
                else 1,
                0 if "120b" in item.id.casefold() or "large" in item.id.casefold() else 1,
                -item.context_length,
                item.id,
            )
        )
        ordered.extend(free_models)
        seen.update(item.id.casefold() for item in free_models)

        paid_models = [item for item in eligible if not item.free and item.id.casefold() not in seen]
        paid_models.sort(
            key=lambda item: (
                0 if item.id == "deepseek/deepseek-v4-flash" and planner else 1,
                item.completion_per_million,
                item.prompt_per_million,
                -item.context_length,
            )
        )
        if not free_only:
            ordered.extend(paid_models)

        if not ordered:
            # Catalog outages must not disable the bot. Preferred :free IDs are safe; paid
            # preferred models remain protected by provider.max_price.
            for model_id in preferred_clean:
                if model_id.casefold() in exclude_folded:
                    continue
                free = model_id.casefold().endswith(":free")
                if free_only and not free:
                    continue
                ordered.append(
                    CatalogModel(
                        id=model_id,
                        prompt_per_million=0.0 if free else float("inf"),
                        completion_per_million=0.0 if free else float("inf"),
                        request_price=0.0,
                        context_length=0,
                        supported_parameters=set(),
                        free=free,
                    )
                )
        return ordered[: self.settings.openrouter_max_catalog_candidates], (
            "dynamic_catalog_blocklist_price_filter" if catalog else "preferred_fallback_catalog_unavailable"
        )

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
        free_only: bool = False,
        exclude_models: set[str] | None = None,
    ) -> tuple[LLMResult, T | None]:
        if not self.settings.llm_enabled:
            raise LLMUnavailable("OPENROUTER_API_KEY is not configured")
        candidates, selection_reason = await self.resolve_models(
            kind=kind,
            preferred=models,
            schema_required=schema_model is not None,
            free_only=free_only,
            exclude=exclude_models,
        )
        if not candidates:
            raise LLMUnavailable("No model remains after blocklist and price filtering")

        failures: list[str] = []
        for index, candidate in enumerate(candidates):
            try:
                return await self._attempt_model(
                    session,
                    kind=kind,
                    scene_id=scene_id,
                    candidate=candidate,
                    candidate_index=index,
                    candidate_count=len(candidates),
                    selection_reason=selection_reason,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    schema_model=schema_model,
                )
            except LLMUnavailable as exc:
                failures.append(f"{candidate.id}: {exc}")
                continue
        raise LLMUnavailable("All model attempts failed: " + " | ".join(failures[-8:]))

    async def _attempt_model(
        self,
        session: AsyncSession,
        *,
        kind: str,
        scene_id: str | None,
        candidate: CatalogModel,
        candidate_index: int,
        candidate_count: int,
        selection_reason: str,
        messages: list[dict[str, Any]],
        max_tokens: int,
        temperature: float,
        schema_model: type[T] | None,
    ) -> tuple[LLMResult, T | None]:
        body: dict[str, Any] = {
            "model": candidate.id,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        # Send optional generation controls only when the live catalogue says that the
        # selected model accepts them. This avoids provider-specific 400 responses.
        if not candidate.supported_parameters or "temperature" in candidate.supported_parameters:
            body["temperature"] = temperature

        # Free endpoints need no provider routing object at all. Paid models were filtered
        # against the catalogue and get a second server-side price guard.
        if not candidate.free:
            max_price: dict[str, float] = {
                "prompt": self.settings.openrouter_max_prompt_price_per_million,
                "completion": self.settings.openrouter_max_completion_price_per_million,
            }
            if self.settings.openrouter_max_request_price_usd > 0:
                max_price["request"] = self.settings.openrouter_max_request_price_usd
            body["provider"] = {"max_price": max_price}

        planner = kind.upper().startswith("PLANNER")
        if planner and "reasoning" in candidate.supported_parameters:
            effort = self.settings.openrouter_planner_reasoning_effort
            body["reasoning"] = {"exclude": True, **({"effort": effort} if effort != "none" else {})}

        schema_mode = "none"
        if schema_model is not None:
            if "structured_outputs" in candidate.supported_parameters:
                body["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": schema_model.__name__.lower(),
                        "strict": True,
                        "schema": schema_model.model_json_schema(),
                    },
                }
                schema_mode = "json_schema"
            elif "response_format" in candidate.supported_parameters:
                body["response_format"] = {"type": "json_object"}
                schema_mode = "json_object"
            else:
                schema_mode = "plain_json"

        started = time.perf_counter()
        response: httpx.Response | None = None
        try:
            response = await self.client.post("/chat/completions", headers=self._headers(), json=body)
            # A few OpenRouter providers advertise a parameter but reject it at runtime. Retry
            # exactly once with the smallest OpenAI-compatible request and then continue to
            # the next catalogue model if the endpoint still rejects the request.
            if response.status_code == 400:
                first_error = response.text.strip()[:1800]
                minimal: dict[str, Any] = {
                    "model": candidate.id,
                    "messages": messages,
                    "max_tokens": max_tokens,
                }
                logger.warning(
                    "OpenRouter 400 compatibility retry model=%s first_response=%s",
                    candidate.id,
                    first_error,
                )
                response = await self.client.post(
                    "/chat/completions", headers=self._headers(), json=minimal
                )
                body = minimal
            response.raise_for_status()
            payload = response.json()
            if payload.get("error"):
                raise ValueError(f"OpenRouter error payload: {payload['error']}")
            choice = payload["choices"][0]
            message = choice["message"]
            content = self._content_text(message.get("content"))
            finish_reason = str(choice.get("finish_reason") or "")
            native_finish_reason = str(choice.get("native_finish_reason") or "")
            if finish_reason and finish_reason != "stop":
                raise ValueError(
                    "incomplete completion: "
                    f"finish_reason={finish_reason} native_finish_reason={native_finish_reason or '-'}"
                )
            if not content.strip():
                raise ValueError("empty completion content")
            usage = payload.get("usage") or {}
            model_name = str(payload.get("model") or candidate.id)
            parsed: T | None = None
            if schema_model is not None:
                parsed = self._parse_schema(content, schema_model)
            result = LLMResult(
                content=content,
                model=model_name,
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                cost_usd=float(usage.get("cost") or 0.0),
                selection_reason=f"{selection_reason}; candidate={candidate_index + 1}/{candidate_count}; schema={schema_mode}",
                finish_reason=finish_reason,
                native_finish_reason=native_finish_reason,
            )
            latency_ms = int((time.perf_counter() - started) * 1000)
            session.add(
                ModelCall(
                    kind=kind,
                    model=model_name,
                    scene_id=scene_id,
                    prompt_tokens=result.prompt_tokens,
                    completion_tokens=result.completion_tokens,
                    cost_usd=result.cost_usd,
                    latency_ms=latency_ms,
                    success=True,
                    http_status=response.status_code,
                    selection_reason=result.selection_reason,
                    request_metadata={
                        "candidate_index": candidate_index,
                        "candidate_count": candidate_count,
                        "free": candidate.free,
                        "schema_mode": schema_mode,
                        "body_keys": sorted(body),
                    },
                    response_metadata={
                        "id": payload.get("id"),
                        "provider": payload.get("provider"),
                        "finish_reason": finish_reason,
                        "native_finish_reason": native_finish_reason,
                    },
                )
            )
            logger.info(
                "model_call success kind=%s selected=%s free=%s candidate=%d/%d prompt_tokens=%d completion_tokens=%d cost_usd=%.8f latency_ms=%d finish=%s",
                kind,
                model_name,
                candidate.free,
                candidate_index + 1,
                candidate_count,
                result.prompt_tokens,
                result.completion_tokens,
                result.cost_usd,
                latency_ms,
                result.finish_reason or "unknown",
            )
            return result, parsed
        except (httpx.HTTPError, KeyError, ValueError, json.JSONDecodeError, ValidationError) as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
            status = response.status_code if response is not None else None
            response_text = response.text.strip()[:3500] if response is not None else ""
            error_detail = f"{type(exc).__name__}: {exc}"
            if response_text:
                error_detail += f"; response={response_text}"
            session.add(
                ModelCall(
                    kind=kind,
                    model=candidate.id,
                    scene_id=scene_id,
                    latency_ms=latency_ms,
                    success=False,
                    error=error_detail[:4000],
                    http_status=status,
                    selection_reason=f"{selection_reason}; candidate={candidate_index + 1}/{candidate_count}",
                    request_metadata={
                        "candidate_index": candidate_index,
                        "candidate_count": candidate_count,
                        "free": candidate.free,
                        "body": self._redact_body(body),
                    },
                    response_metadata={"body": response_text[:3000]},
                )
            )
            logger.warning(
                "model_call failure kind=%s attempted=%s candidate=%d/%d status=%s latency_ms=%d error=%s",
                kind,
                candidate.id,
                candidate_index + 1,
                candidate_count,
                status,
                latency_ms,
                error_detail[:2200],
            )
            raise LLMUnavailable(error_detail) from exc

    @staticmethod
    def _redact_body(body: dict[str, Any]) -> dict[str, Any]:
        return {
            "model": body.get("model"),
            "max_tokens": body.get("max_tokens"),
            "temperature": body.get("temperature"),
            "provider": body.get("provider"),
            "reasoning": body.get("reasoning"),
            "response_format": body.get("response_format"),
            "message_count": len(body.get("messages") or []),
        }

    @staticmethod
    def _content_text(content: object) -> str:
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            return "".join(
                str(item.get("text") or "") for item in content if isinstance(item, dict)
            ).strip()
        if isinstance(content, dict):
            return json.dumps(content, ensure_ascii=False)
        return str(content or "").strip()

    @staticmethod
    def _parse_schema(content: str, schema_model: type[T]) -> T:
        try:
            return schema_model.model_validate_json(content)
        except ValidationError:
            match = re.search(r"\{.*\}", content, re.DOTALL)
            if not match:
                raise
            return schema_model.model_validate_json(match.group(0))
