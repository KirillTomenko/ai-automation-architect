"""Единый LLM-клиент для всех стадий пайплайна.

OpenAI-совместимый клиент через ProxyAPI. Модель и параметры вызова
берутся из окружения — не хардкодятся в коде:
PROXYAPI_BASE_URL, LLM_API_KEY, LLM_MODEL, LLM_TIMEOUT_S, LLM_MAX_TOKENS.
LLM_TEMPERATURE_STAGE1 — отдельная (более низкая) temperature для Stage 1;
Stage 2/3 используют LLM_TEMPERATURE.
LLM_MODEL_STAGE1 — отдельная модель для Stage 1 (по умолчанию — LLM_MODEL);
Stage 2/3 используют LLM_MODEL.
"""

import os
from dataclasses import dataclass

from dotenv import load_dotenv
from openai import AsyncOpenAI


class LLMSettingsError(RuntimeError):
    """Конфигурация LLM неполна (например, не задан LLM_API_KEY)."""


@dataclass(frozen=True)
class LLMSettings:
    base_url: str
    api_key: str
    model: str
    model_stage1: str
    temperature: float
    temperature_stage1: float
    timeout_s: float
    max_tokens: int


def load_llm_settings() -> LLMSettings:
    load_dotenv()

    api_key = os.getenv("LLM_API_KEY")
    if not api_key:
        raise LLMSettingsError(
            "LLM_API_KEY не задан. Скопируй .env.example в .env и вставь ключ ProxyAPI."
        )

    return LLMSettings(
        base_url=os.getenv("PROXYAPI_BASE_URL", "https://api.proxyapi.ru/openai/v1"),
        api_key=api_key,
        model=os.getenv("LLM_MODEL", "gpt-4o-mini"),
        model_stage1=os.getenv("LLM_MODEL_STAGE1") or os.getenv("LLM_MODEL", "gpt-4o-mini"),
        temperature=float(os.getenv("LLM_TEMPERATURE", "0.3")),
        temperature_stage1=float(os.getenv("LLM_TEMPERATURE_STAGE1", "0.1")),
        timeout_s=float(os.getenv("LLM_TIMEOUT_S", "45")),
        max_tokens=int(os.getenv("LLM_MAX_TOKENS", "3000")),
    )


def get_llm_client() -> tuple[AsyncOpenAI, LLMSettings]:
    settings = load_llm_settings()
    client = AsyncOpenAI(base_url=settings.base_url, api_key=settings.api_key, timeout=settings.timeout_s)
    return client, settings


async def complete_json(
    client: AsyncOpenAI,
    settings: LLMSettings,
    messages: list[dict],
    temperature: float | None = None,
    model: str | None = None,
):
    """Единый вызов чата в JSON-режиме для всех стадий пайплайна.

    json_object, а не json_schema strict: ProxyAPI не гарантирует поддержку
    strict-схем, а гарантию формы дают Pydantic-валидация + retry в стадиях.
    temperature/model=None означают дефолт из настроек; стадия может передать
    свои (Stage 1 передаёт settings.temperature_stage1 и settings.model_stage1).
    """
    return await client.chat.completions.create(
        model=model if model is not None else settings.model,
        messages=messages,
        temperature=temperature if temperature is not None else settings.temperature,
        max_tokens=settings.max_tokens,
        response_format={"type": "json_object"},
    )