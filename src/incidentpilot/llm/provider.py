"""LLM provider abstraction.

Agent nodes must not create provider clients directly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel


class StructuredOutputError(RuntimeError):
    """A model exhausted the bounded structured-output repair attempts."""


@dataclass
class LLMMessage:
    role: str
    content: str


@dataclass
class LLMResponse:
    content: str
    provider: str
    model: str
    usage: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


class LLMProvider(ABC):
    @property
    @abstractmethod
    def provider_name(self) -> str: ...

    @property
    @abstractmethod
    def model_name(self) -> str: ...

    @abstractmethod
    def supports_tool_calling(self) -> bool: ...

    @abstractmethod
    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        system: str | None = None,
        temperature: float = 0.0,
    ) -> LLMResponse: ...

    async def complete_structured(
        self,
        schema: type[BaseModel],
        messages: list[LLMMessage],
        *,
        system: str | None = None,
        temperature: float = 0.0,
    ) -> BaseModel:
        """Request JSON and validate it at the provider boundary."""
        import json

        schema_instruction = (
            "Return only one JSON object matching this JSON Schema: "
            + json.dumps(schema.model_json_schema(), separators=(",", ":"))
        )
        last_error: Exception | None = None
        repair_messages = list(messages)
        for _ in range(3):
            response = await self.complete(
                repair_messages,
                system=f"{system or ''}\n{schema_instruction}".strip(),
                temperature=temperature,
            )
            content = response.content.strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[-1].rsplit("```", 1)[0]
            try:
                return schema.model_validate_json(content)
            except (ValueError, TypeError) as exc:
                last_error = exc
                repair_messages = [
                    *messages,
                    LLMMessage(
                        role="user",
                        content="The previous output was invalid. Return only schema-valid JSON.",
                    ),
                ]
        raise StructuredOutputError("structured output invalid after 3 attempts") from last_error


class FakeLLMProvider(LLMProvider):
    """Deterministic provider for local development and tests."""

    def __init__(
        self,
        model_name: str = "fake-model-v1",
        responses: list[str] | None = None,
    ) -> None:
        self._model = model_name
        self._responses = list(responses or [])
        self._calls = 0

    @property
    def provider_name(self) -> str:
        return "fake"

    @property
    def model_name(self) -> str:
        return self._model

    def supports_tool_calling(self) -> bool:
        return False

    def enqueue(self, response: str) -> None:
        self._responses.append(response)

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        system: str | None = None,
        temperature: float = 0.0,
    ) -> LLMResponse:
        self._calls += 1
        if self._responses:
            content = self._responses.pop(0)
        else:
            last = messages[-1].content if messages else ""
            content = f"FAKE_RESPONSE:{last[:200]}"
        return LLMResponse(
            content=content,
            provider=self.provider_name,
            model=self.model_name,
            usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        )


class OpenAICompatibleProvider(LLMProvider):
    """OpenAI-compatible HTTP adapter using httpx."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        supports_tools: bool = True,
        timeout: float = 60.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._supports_tools = supports_tools
        self._timeout = timeout

    @property
    def provider_name(self) -> str:
        return "openai-compatible"

    @property
    def model_name(self) -> str:
        return self._model

    def supports_tool_calling(self) -> bool:
        return self._supports_tools

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        system: str | None = None,
        temperature: float = 0.0,
    ) -> LLMResponse:
        return await self._complete(
            messages,
            system=system,
            temperature=temperature,
        )

    async def complete_structured(
        self,
        schema: type[BaseModel],
        messages: list[LLMMessage],
        *,
        system: str | None = None,
        temperature: float = 0.0,
    ) -> BaseModel:
        import json

        schema_instruction = (
            "Return only one JSON object matching this JSON Schema: "
            + json.dumps(schema.model_json_schema(), separators=(",", ":"))
        )
        last_error: Exception | None = None
        repair_messages = list(messages)
        for _ in range(3):
            response = await self._complete(
                repair_messages,
                system=f"{system or ''}\n{schema_instruction}".strip(),
                temperature=temperature,
                response_format={"type": "json_object"},
                max_tokens=16384,
            )
            content = response.content.strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[-1].rsplit("```", 1)[0]
            try:
                return schema.model_validate_json(content)
            except (ValueError, TypeError) as exc:
                last_error = exc
                repair_messages = [
                    *messages,
                    LLMMessage(
                        role="user",
                        content="The previous output was invalid. Return only schema-valid JSON.",
                    ),
                ]
        raise StructuredOutputError("structured output invalid after 3 attempts") from last_error

    async def _complete(
        self,
        messages: list[LLMMessage],
        *,
        system: str | None,
        temperature: float,
        response_format: dict[str, str] | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        import httpx

        payload_messages: list[dict[str, str]] = []
        if system:
            payload_messages.append({"role": "system", "content": system})
        payload_messages.extend({"role": m.role, "content": m.content} for m in messages)

        payload = {
            "model": self._model,
            "messages": payload_messages,
            "temperature": temperature,
        }
        if response_format is not None:
            payload["response_format"] = response_format
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                f"{self._base_url}/chat/completions",
                json=payload,
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()

        content = ""
        choices = data.get("choices") or []
        if choices:
            message = choices[0].get("message") or {}
            content = message.get("content") or ""
        usage = data.get("usage") or {}
        return LLMResponse(
            content=content,
            provider=self.provider_name,
            model=self._model,
            usage={
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "total_tokens": usage.get("total_tokens"),
            },
            raw=data,
        )


def build_llm_provider(
    *,
    llm_enabled: bool,
    provider_name: str,
    base_url: str,
    api_key: str,
    model: str,
) -> LLMProvider:
    if not llm_enabled or not api_key:
        return FakeLLMProvider()
    if provider_name != "openai-compatible":
        raise ValueError(f"unsupported LLM provider: {provider_name}")
    return OpenAICompatibleProvider(base_url=base_url, api_key=api_key, model=model)
