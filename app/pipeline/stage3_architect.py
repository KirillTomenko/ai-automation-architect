"""Stage 3 — Architecture Composer.

Собирает архитектуру решения строго из блоков каталога config/block_catalog.yaml.
block_type — enum из каталога (проверяет Pydantic-схема, не доверие к LLM);
каждый node привязан к шагам Stage 1 через step_ids, и каждый шаг обязан быть
покрыт хотя бы одним блоком. Вход Stage 2 (категория + automation_pct) помогает
модели выбирать блоки по данным, а не наугад.
"""

import json
import logging
from typing import TYPE_CHECKING

from pydantic import ValidationError

from app.config_loader import BlockSpec
from app.llm_client import complete_json, get_llm_client
from app.pipeline.errors import PipelineStageError
from app.pipeline.prompts import STAGE3_RETRY_PROMPT, build_stage3_system_prompt
from app.schemas import ExtractedProcess, build_stage3_models

if TYPE_CHECKING:
    from app.schemas import ArchitecturePlan, ClassifiedProcess

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3


def _build_architecture_payload(
    process: ExtractedProcess, classified: "ClassifiedProcess"
) -> str:
    """JSON для LLM: шаги Stage 1 с классификацией Stage 2 и пробелами из assumptions.

    Assumptions нужны Stage 3 так же, как Stage 2: например, gap='channel' —
    сигнал не придумывать входной блок, а оставить пробел видимым (правило 6 промпта).
    """
    candidates = {candidate.step_id: candidate for candidate in classified.automation_candidates}

    by_step: dict[str, list[dict]] = {}
    process_level: list[dict] = []
    for assumption in process.assumptions:
        entry = {"gap": assumption.gap, "note": assumption.note}
        if assumption.step_id is None:
            process_level.append(entry)
        else:
            by_step.setdefault(assumption.step_id, []).append(entry)

    payload = {
        "entry_point": process.entry_point,
        "steps": [
            {
                "id": step.id,
                "name": step.name,
                "input": step.input,
                "output": step.output,
                "actor": step.actor,
                "category": candidates[step.id].category,
                "automation_pct": candidates[step.id].automation_pct,
                "reasoning": candidates[step.id].reasoning,
                "assumptions": by_step.get(step.id, []),
            }
            for step in process.steps
        ],
        "process_assumptions": process_level,
    }
    return json.dumps(payload, ensure_ascii=False)


async def compose_architecture(
    process: ExtractedProcess,
    classified: "ClassifiedProcess",
    catalog: list[BlockSpec],
) -> "ArchitecturePlan":
    """Строит граф блоков каталога для процесса; при невалидном выходе ретраит с текстом ошибки."""
    if not process.steps:
        raise ValueError("Нет шагов для проектирования архитектуры")
    if not catalog:
        raise ValueError("Каталог блоков пуст")

    step_ids = {step.id for step in process.steps}
    candidate_ids = {candidate.step_id for candidate in classified.automation_candidates}
    if candidate_ids != step_ids:
        raise ValueError(
            "automation_candidates не соответствуют шагам Stage 1: "
            f"нет классификации для {sorted(step_ids - candidate_ids)}, "
            f"лишние кандидаты {sorted(candidate_ids - step_ids)}"
        )

    _, result_model = build_stage3_models(
        block_types=[block.name for block in catalog],
        step_ids=[step.id for step in process.steps],
    )

    client, settings = get_llm_client()
    logger.info(
        "Stage 3: архитектура для %d шагов из %d типов блоков каталога",
        len(process.steps), len(catalog),
    )
    logger.debug("Stage 3: вход: %s", _build_architecture_payload(process, classified))

    messages: list[dict] = [
        {"role": "system", "content": build_stage3_system_prompt(catalog)},
        {"role": "user", "content": _build_architecture_payload(process, classified)},
    ]

    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        response = await complete_json(client, settings, messages)
        raw = response.choices[0].message.content or ""
        logger.debug("Stage 3: сырой ответ LLM: %s", raw)

        try:
            result = result_model.model_validate_json(raw)
        except ValidationError as exc:
            last_error = exc
            logger.warning(
                "Stage 3: попытка %d/%d — выход не прошёл валидацию:\n%s",
                attempt, MAX_ATTEMPTS, exc,
            )
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content": STAGE3_RETRY_PROMPT.format(error=exc)})
            continue

        logger.info(
            "Stage 3: выход валиден (попытка %d/%d; блоков: %d, рёбер: %d)",
            attempt, MAX_ATTEMPTS, len(result.nodes), len(result.edges),
        )
        logger.debug("Stage 3: выход:\n%s", result.model_dump_json(indent=2, by_alias=True))
        return result

    raise PipelineStageError(
        f"Stage 3: не удалось получить валидный выход за {MAX_ATTEMPTS} попыток"
    ) from last_error