"""Stage 4 — Blueprint Packager.

Собирает финальный Automation Blueprint по схеме CLAUDE.md. Advisory-часть
(risks / mvp_scope / estimated_effort) генерирует LLM по выходам Stage 1–3 —
это рекомендации, а не факты, поэтому валидация структурная, без биконд-паттерна
Stage 1. Все фактические поля финального JSON собираются КОДОМ из уже
провалидированных выходов стадий, без доверия к LLM:

- steps / actors / automation_candidates — прямая сборка из Stage 1–2
  (упрощение до схемы: шаг без gap-полей Stage 1);
- architecture — узлы Stage 3 переносятся как есть (id/block_type/step_ids/note);
- human_in_the_loop — шаги, покрытые блоками human_escalation в Stage 3;
- integrations — детерминированное отображение типов блоков каталога во
  внешние системы (как и сами блоки — предсказуемо между кейсами).
"""

import json
import logging
from typing import TYPE_CHECKING

from pydantic import ValidationError

from app.config_loader import BlockSpec, CategorySpec
from app.llm_client import complete_json, get_llm_client
from app.pipeline.errors import PipelineStageError
from app.pipeline.prompts import (
    STAGE4_RETRY_PROMPT,
    STAGE4_SYSTEM_PROMPT,
    STAGE4_USER_PROMPT_TEMPLATE,
)
from app.schemas import BlueprintAdvisory, ExtractedProcess, build_blueprint_model

if TYPE_CHECKING:
    from app.schemas import ArchitecturePlan, Blueprint, ClassifiedProcess

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3

# Детерминированное отображение block_type каталога во внешнюю интеграцию.
# Ключи обязаны покрывать весь config/block_catalog.yaml: неизвестный тип —
# ошибка сборки, а не молча пустой список интеграций.
_INTEGRATION_BY_BLOCK = {
    "bot_webhook_receiver": "API мессенджера/вебхуков (приём входящих обращений)",
    "data_extractor_llm": "LLM API (OpenAI-совместимый)",
    "classifier_router": "LLM API (OpenAI-совместимый)",
    "rag_responder": "LLM API + база знаний (FAQ/документы)",
    "generator_llm": "LLM API (OpenAI-совместимый)",
    "rules_engine": "Конфигурируемый движок правил (внутренний конфиг)",
    "crm_connector": "CRM/таблица (внешний API)",
    "notifier": "Канал уведомлений (email/мессенджер)",
    "human_escalation": "Уведомления ответственному сотруднику (email/мессенджер)",
}


def _stage1_json(process: "ExtractedProcess") -> str:
    """Шаги и assumptions Stage 1 для промпта (упрощение до схемы финального JSON)."""
    return json.dumps(
        {
            "actors": process.actors,
            "entry_point": process.entry_point,
            "steps": [
                {
                    "id": step.id,
                    "name": step.name,
                    "input": step.input,
                    "output": step.output,
                    "actor": step.actor,
                }
                for step in process.steps
            ],
            "assumptions": [assumption.model_dump() for assumption in process.assumptions],
        },
        ensure_ascii=False,
    )


def _stage2_json(classified: "ClassifiedProcess") -> str:
    return json.dumps(
        {"automation_candidates": [c.model_dump() for c in classified.automation_candidates]},
        ensure_ascii=False,
    )


def _stage3_json(architecture: "ArchitecturePlan", catalog_labels: dict[str, str]) -> str:
    """Граф Stage 3 с человекочитаемыми label блоков (для estimated_effort) и note."""
    dump = architecture.model_dump(by_alias=True)
    nodes = [{**node, "block_label": catalog_labels[node["block_type"]]} for node in dump["nodes"]]
    return json.dumps({"nodes": nodes, "edges": dump["edges"]}, ensure_ascii=False)


def _derive_human_in_the_loop(
    process: "ExtractedProcess", architecture: "ArchitecturePlan"
) -> list[str]:
    """Шаги, остающиеся за человеком: покрытые блоками human_escalation.

    Решение принимается архитектурой Stage 3 (по данным Stage 2), поэтому
    сборка не доверяет этот список LLM Stage 4. Порядок — как в Stage 1.
    """
    escalated: set[str] = set()
    for node in architecture.nodes:
        if node.block_type == "human_escalation":
            escalated.update(node.step_ids)
    return [step.id for step in process.steps if step.id in escalated]


def _derive_integrations(architecture: "ArchitecturePlan") -> list[str]:
    """Интеграции — детерминированно из типов блоков архитектуры, без LLM."""
    result: list[str] = []
    for node in architecture.nodes:
        try:
            label = _INTEGRATION_BY_BLOCK[node.block_type]
        except KeyError as exc:
            raise ValueError(
                f"block_type '{node.block_type}' отсутствует в _INTEGRATION_BY_BLOCK — "
                f"дополни отображение в stage4_packager.py"
            ) from exc
        if label not in result:
            result.append(label)
    return result


def assemble_blueprint(
    description: str,
    process: "ExtractedProcess",
    classified: "ClassifiedProcess",
    architecture: "ArchitecturePlan",
    advisory: "BlueprintAdvisory",
    taxonomy: list[CategorySpec],
    catalog: list[BlockSpec],
) -> "Blueprint":
    """Собирает финальный Blueprint из выходов Stage 1–4 строго по схеме CLAUDE.md.

    Всё перевалидируется моделью Blueprint: enum категорий и типов блоков,
    ссылки human_in_the_loop на шаги, целостность рёбер архитектуры.
    """
    blueprint_model = build_blueprint_model(
        taxonomy={c.name: (c.range_min, c.range_max) for c in taxonomy},
        block_types=[block.name for block in catalog],
    )

    arch_dump = architecture.model_dump(by_alias=True)
    blueprint = blueprint_model(
        process=description.strip(),
        actors=process.actors,
        steps=[
            {
                "id": step.id,
                "name": step.name,
                "input": step.input,
                "output": step.output,
                "actor": step.actor,
            }
            for step in process.steps
        ],
        automation_candidates=[c.model_dump() for c in classified.automation_candidates],
        architecture={
            # Узлы переносятся из Stage 3 как есть: id/block_type/step_ids/note —
            # связь блок↔шаг и пометки (например, маркер канала) доходят до фронта.
            "nodes": arch_dump["nodes"],
            "edges": arch_dump["edges"],
        },
        human_in_the_loop=_derive_human_in_the_loop(process, architecture),
        integrations=_derive_integrations(architecture),
        risks=advisory.risks,
        mvp_scope=advisory.mvp_scope,
        estimated_effort=advisory.estimated_effort,
    )
    logger.debug("Stage 4: финальный blueprint:\n%s", blueprint.model_dump_json(by_alias=True, indent=2))
    return blueprint


async def package_blueprint(
    description: str,
    process: "ExtractedProcess",
    classified: "ClassifiedProcess",
    architecture: "ArchitecturePlan",
    taxonomy: list[CategorySpec],
    catalog: list[BlockSpec],
) -> "Blueprint":
    """Генерирует advisory-часть по выходам Stage 1–3 и собирает финальный Blueprint."""
    if not description or not description.strip():
        raise ValueError("Описание процесса пустое")
    if not catalog:
        raise ValueError("Каталог блоков пуст")

    catalog_labels = {block.name: block.label for block in catalog}
    user_content = STAGE4_USER_PROMPT_TEMPLATE.format(
        description=description.strip(),
        stage1_json=_stage1_json(process),
        stage2_json=_stage2_json(classified),
        stage3_json=_stage3_json(architecture, catalog_labels),
    )

    client, settings = get_llm_client()
    logger.info(
        "Stage 4: advisory-часть для процесса из %d шагов, %d блоков, %d assumptions",
        len(process.steps), len(architecture.nodes), len(process.assumptions),
    )
    logger.debug("Stage 4: вход:\n%s", user_content)

    messages: list[dict] = [
        {"role": "system", "content": STAGE4_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]

    # Advisory-часть допускает базовые модель и temperature (CLAUDE.md: чуть выше
    # допустим только на Stage 4; LLM_TEMPERATURE — уровень Stage 2/3).
    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        response = await complete_json(client, settings, messages)
        raw = response.choices[0].message.content or ""
        logger.debug("Stage 4: сырой ответ LLM: %s", raw)

        try:
            advisory = BlueprintAdvisory.model_validate_json(raw)
        except ValidationError as exc:
            last_error = exc
            logger.warning(
                "Stage 4: попытка %d/%d — выход не прошёл валидацию:\n%s",
                attempt, MAX_ATTEMPTS, exc,
            )
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content": STAGE4_RETRY_PROMPT.format(error=exc)})
            continue

        logger.info(
            "Stage 4: выход валиден (попытка %d/%d; risks: %d, mvp in/out: %d/%d, компонентов effort: %d)",
            attempt, MAX_ATTEMPTS,
            len(advisory.risks),
            len(advisory.mvp_scope.in_), len(advisory.mvp_scope.out),
            len(advisory.estimated_effort),
        )
        return assemble_blueprint(
            description, process, classified, architecture, advisory, taxonomy, catalog
        )

    raise PipelineStageError(
        f"Stage 4: не удалось получить валидный выход за {MAX_ATTEMPTS} попыток"
    ) from last_error