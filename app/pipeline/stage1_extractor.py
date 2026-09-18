"""Stage 1 — Process Extractor.

Принимает свободный текст описания процесса, через LLM со structured output
извлекает actors / entry_point / steps и структурированные gap-поля
(*_named / *_gap_reason). Assumptions из них собирает КОД (ProcessDraft.finalize),
а не модель: модель отвечает только «названо/не названо» по каждому измерению.
"""

import logging

from pydantic import ValidationError

from app.llm_client import complete_json, get_llm_client
from app.pipeline.errors import PipelineStageError
from app.pipeline.prompts import STAGE1_RETRY_PROMPT, STAGE1_SYSTEM_PROMPT
from app.schemas import ExtractedProcess, ProcessDraft

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3


async def extract_process(description: str) -> ExtractedProcess:
    """Извлекает процесс из текста; при невалидном выходе ретраит с текстом ошибки."""
    if not description or not description.strip():
        raise ValueError("Описание процесса пустое")

    client, settings = get_llm_client()
    logger.info("Stage 1: получено описание процесса (%d символов)", len(description))
    logger.debug("Stage 1: вход: %s", description)

    messages: list[dict] = [
        {"role": "system", "content": STAGE1_SYSTEM_PROMPT},
        {"role": "user", "content": description.strip()},
    ]

    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        # (H) Stage 1: отдельные модель (LLM_MODEL_STAGE1) и temperature
        response = await complete_json(
            client,
            settings,
            messages,
            temperature=settings.temperature_stage1,
            model=settings.model_stage1,
        )
        raw = response.choices[0].message.content or ""
        logger.debug("Stage 1: сырой ответ LLM: %s", raw)

        try:
            draft = ProcessDraft.model_validate_json(raw)
        except ValidationError as exc:
            last_error = exc
            logger.warning(
                "Stage 1: попытка %d/%d — выход не прошёл валидацию:\n%s",
                attempt, MAX_ATTEMPTS, exc,
            )
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content": STAGE1_RETRY_PROMPT.format(error=exc)})
            continue

        result = draft.finalize()
        logger.info(
            "Stage 1: выход валиден (попытка %d/%d; шагов: %d, assumptions собраны кодом: %d)",
            attempt, MAX_ATTEMPTS, len(result.steps), len(result.assumptions),
        )
        logger.debug("Stage 1: выход:\n%s", result.model_dump_json(indent=2))
        return result

    raise PipelineStageError(
        f"Stage 1: не удалось получить валидный выход за {MAX_ATTEMPTS} попыток"
    ) from last_error