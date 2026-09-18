"""Stage 2 — Classifier.

Классифицирует шаги из выхода Stage 1 по фиксированной таксономии из
config/taxonomy.yaml. Категория — enum из имён таксономии, процент — только
из диапазона категории: это проверяет Pydantic-схема, не доверие к LLM.

Assumptions из Stage 1 передаются в промпт как контекст: отмеченные пробелы
(неясен исполнитель, канал, критерий) — довод снизить процент внутри диапазона
категории, а не выдавать шаг за «полностью определённый».
"""

import json
import logging
from typing import TYPE_CHECKING

from pydantic import ValidationError

from app.config_loader import CategorySpec
from app.llm_client import complete_json, get_llm_client
from app.pipeline.errors import PipelineStageError
from app.pipeline.prompts import STAGE2_RETRY_PROMPT, build_stage2_system_prompt
from app.schemas import ExtractedProcess, build_stage2_models

if TYPE_CHECKING:
    from app.schemas import ClassifiedProcess

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3

# Структурированные gap-поля Stage 1 не включаются в payload Stage 2: та же
# информация уже приложена к шагам в виде assumptions (собранных кодом),
# а вход Stage 2 должен оставаться таким же, как до их появления.
_GAP_FIELDS = {
    "actor_named",
    "actor_gap_reason",
    "branching_criteria_named",
    "branching_criteria_gap_reason",
}


def _build_steps_payload(process: ExtractedProcess) -> str:
    """JSON для LLM: шаги с привязанными к ним assumptions + пробелы процесса целиком.

    Записи assumptions с step_id кладутся внутрь соответствующего шага,
    записи без step_id (например, про канал входа) идут в process_assumptions.
    """
    by_step: dict[str, list[dict]] = {}
    process_level: list[dict] = []
    for assumption in process.assumptions:
        entry = {"gap": assumption.gap, "note": assumption.note}
        if assumption.step_id is None:
            process_level.append(entry)
        else:
            by_step.setdefault(assumption.step_id, []).append(entry)

    payload = {
        "steps": [
            {**step.model_dump(exclude=_GAP_FIELDS), "assumptions": by_step.get(step.id, [])}
            for step in process.steps
        ],
        "process_assumptions": process_level,
    }
    return json.dumps(payload, ensure_ascii=False)


async def classify_steps(
    process: ExtractedProcess, taxonomy: list[CategorySpec]
) -> "ClassifiedProcess":
    """Классифицирует шаги результата Stage 1; при невалидном выходе ретраит с текстом ошибки."""
    if not process.steps:
        raise ValueError("Нет шагов для классификации")
    if not taxonomy:
        raise ValueError("Таксономия пуста")

    _, result_model = build_stage2_models(
        taxonomy={c.name: (c.range_min, c.range_max) for c in taxonomy},
        step_ids=[step.id for step in process.steps],
    )

    client, settings = get_llm_client()
    logger.info(
        "Stage 2: классификация %d шагов по %d категориям таксономии (assumptions: %d)",
        len(process.steps), len(taxonomy), len(process.assumptions),
    )
    logger.debug("Stage 2: вход: %s", _build_steps_payload(process))

    messages: list[dict] = [
        {"role": "system", "content": build_stage2_system_prompt(taxonomy)},
        {"role": "user", "content": _build_steps_payload(process)},
    ]

    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        response = await complete_json(client, settings, messages)
        raw = response.choices[0].message.content or ""
        logger.debug("Stage 2: сырой ответ LLM: %s", raw)

        try:
            result = result_model.model_validate_json(raw)
        except ValidationError as exc:
            last_error = exc
            logger.warning(
                "Stage 2: попытка %d/%d — выход не прошёл валидацию:\n%s",
                attempt, MAX_ATTEMPTS, exc,
            )
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content": STAGE2_RETRY_PROMPT.format(error=exc)})
            continue

        logger.info(
            "Stage 2: выход валиден (попытка %d/%d; кандидатов: %d)",
            attempt, MAX_ATTEMPTS, len(result.automation_candidates),
        )
        logger.debug("Stage 2: выход:\n%s", result.model_dump_json(indent=2))
        return result

    raise PipelineStageError(
        f"Stage 2: не удалось получить валидный выход за {MAX_ATTEMPTS} попыток"
    ) from last_error