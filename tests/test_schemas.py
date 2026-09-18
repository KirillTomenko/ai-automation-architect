"""Офлайн-тесты схем и guard'ов пайплайна — без LLM и без сети.

Две группы:
1. Канонические фикстуры из examples/ обязаны проходить валидацию схем всех
   стадий (extracted → Stage 1, classified → Stage 2, architecture → Stage 3,
   blueprints → финальный Blueprint) — прогон на новых кейсах не ломает старые.
2. Guard'ы обязаны отклонять данные, нарушающие контракты доверия: процент вне
   диапазона категории, неизвестная категория/блок, непокрытый шаг, placeholder
   без assumption, висячее ребро, актёр вне actors.

Запуск из корня проекта:
    python -m pytest tests/test_schemas.py
"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config_loader import load_block_catalog, load_taxonomy
from app.schemas import (
    ACTOR_UNSPECIFIED,
    ExtractedProcess,
    ProcessDraft,
    ProcessStep,
    build_blueprint_model,
    build_stage2_models,
    build_stage3_models,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = PROJECT_ROOT / "examples"

TAXONOMY = {c.name: (c.range_min, c.range_max) for c in load_taxonomy()}
BLOCK_TYPES = [b.name for b in load_block_catalog()]

_EXTRACTED = EXAMPLES / "extracted"
_CLASSIFIED = EXAMPLES / "classified"
_ARCHITECTURE = EXAMPLES / "architecture"
_BLUEPRINTS = EXAMPLES / "blueprints"


def _names(directory: Path) -> list[str]:
    return sorted(p.stem for p in directory.glob("*.json"))


def _read(directory: Path, name: str) -> str:
    # utf-8-sig: Windows-инструменты часто добавляют BOM (конвенция CLAUDE.md)
    return (directory / f"{name}.json").read_text(encoding="utf-8-sig")


def _step_data(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "id": "step_1",
        "name": "Шаг",
        "input": "вход",
        "output": "выход",
        "actor": "менеджер",
        "actor_named": True,
        "actor_gap_reason": None,
    }
    data.update(overrides)
    return data


# --- 1. Канонические фикстуры: валидны на текущих схемах ---


@pytest.mark.parametrize("name", _names(_EXTRACTED))
def test_extracted_fixture_valid(name: str) -> None:
    ExtractedProcess.model_validate_json(_read(_EXTRACTED, name))


@pytest.mark.parametrize("name", _names(_CLASSIFIED))
def test_classified_fixture_valid(name: str) -> None:
    stage1 = ExtractedProcess.model_validate_json(_read(_EXTRACTED, name))
    _, classified_model = build_stage2_models(
        taxonomy=TAXONOMY, step_ids=[step.id for step in stage1.steps]
    )
    classified_model.model_validate_json(_read(_CLASSIFIED, name))


@pytest.mark.parametrize("name", _names(_ARCHITECTURE))
def test_architecture_fixture_valid(name: str) -> None:
    stage1 = ExtractedProcess.model_validate_json(_read(_EXTRACTED, name))
    _, plan_model = build_stage3_models(
        block_types=BLOCK_TYPES, step_ids=[step.id for step in stage1.steps]
    )
    plan_model.model_validate_json(_read(_ARCHITECTURE, name))


@pytest.mark.parametrize("name", _names(_BLUEPRINTS))
def test_blueprint_fixture_valid(name: str) -> None:
    blueprint_model = build_blueprint_model(
        taxonomy=TAXONOMY, block_types=BLOCK_TYPES
    )
    blueprint_model.model_validate_json(_read(_BLUEPRINTS, name))


# --- 2. Guard'ы Stage 1: placeholder ⇔ assumption ⇔ actors ---


def test_stage1_guard_placeholder_requires_actor_gap() -> None:
    # placeholder возможен только при actor_named=false (и наоборот)
    with pytest.raises(ValidationError):
        ProcessStep.model_validate(
            _step_data(actor=ACTOR_UNSPECIFIED, actor_named=True)
        )


def test_stage1_guard_placeholder_normalized() -> None:
    # другой регистр placeholder'а нормализуется к канонической форме
    step = ProcessStep.model_validate(
        _step_data(
            actor="НЕ УКАЗАНО В ТЕКСТЕ",
            actor_named=False,
            actor_gap_reason="исполнитель не назван",
        )
    )
    assert step.actor == ACTOR_UNSPECIFIED


def test_stage1_guard_placeholder_requires_assumption() -> None:
    # Внешний JSON: placeholder-шаг без записи gap='actor' в assumptions отклоняется
    raw = {
        "actors": [],
        "entry_point": "Telegram",
        "channel_named": True,
        "channel_gap_reason": None,
        "steps": [
            _step_data(
                actor=ACTOR_UNSPECIFIED,
                actor_named=False,
                actor_gap_reason="исполнитель не назван",
            )
        ],
        "assumptions": [],
    }
    with pytest.raises(ValidationError):
        ExtractedProcess.model_validate(raw)


def test_stage1_guard_actor_membership() -> None:
    # названный исполнитель обязан входить в actors
    data = {
        "actors": ["менеджер"],
        "entry_point": "Telegram",
        "channel_named": True,
        "channel_gap_reason": None,
        "steps": [_step_data(actor="руководитель")],
        "assumptions": [],
    }
    with pytest.raises(ValidationError):
        ExtractedProcess.model_validate(data)


def test_finalize_derives_assumptions() -> None:
    # assumptions собираются кодом из *_named/*_gap_reason, модель их не пишет
    draft = ProcessDraft.model_validate(
        {
            "actors": ["менеджер"],
            "entry_point": "Telegram",
            "channel_named": False,
            "channel_gap_reason": "канал входа не назван",
            "steps": [
                _step_data(
                    actor=ACTOR_UNSPECIFIED,
                    actor_named=False,
                    actor_gap_reason="исполнитель не назван",
                )
            ],
        }
    )
    extracted = draft.finalize()
    gaps = {(a.gap, a.step_id) for a in extracted.assumptions}
    assert ("channel", None) in gaps
    assert ("actor", "step_1") in gaps


# --- 3. Guard'ы Stage 2: enum категории + процент в диапазоне категории ---


def test_stage2_guard_pct_outside_category_range() -> None:
    candidate_model, _ = build_stage2_models(taxonomy=TAXONOMY, step_ids=["step_1"])
    with pytest.raises(ValidationError):
        candidate_model.model_validate(
            {
                "step_id": "step_1",
                "category": "data_intake",  # диапазон 95–100
                "automation_pct": 10,
                "reasoning": "произвольный процент вне диапазона",
            }
        )


def test_stage2_guard_unknown_category() -> None:
    candidate_model, _ = build_stage2_models(taxonomy=TAXONOMY, step_ids=["step_1"])
    with pytest.raises(ValidationError):
        candidate_model.model_validate(
            {
                "step_id": "step_1",
                "category": "неизвестная_категория",
                "automation_pct": 50,
                "reasoning": "категория вне таксономии",
            }
        )


# --- 4. Guard'ы Stage 3: enum блоков, покрытие шагов, целостность графа ---


def test_stage3_guard_unknown_block_type() -> None:
    _, plan_model = build_stage3_models(
        block_types=BLOCK_TYPES, step_ids=["step_1"]
    )
    with pytest.raises(ValidationError):
        plan_model.model_validate(
            {
                "nodes": [
                    {
                        "id": "block_1",
                        "block_type": "ИзобретённыйБлок",
                        "step_ids": ["step_1"],
                    }
                ],
                "edges": [],
            }
        )


def test_stage3_guard_uncovered_step() -> None:
    _, plan_model = build_stage3_models(
        block_types=BLOCK_TYPES, step_ids=["step_1", "step_2"]
    )
    with pytest.raises(ValidationError):
        plan_model.model_validate(
            {
                "nodes": [
                    {
                        "id": "block_1",
                        "block_type": BLOCK_TYPES[0],
                        "step_ids": ["step_1"],  # step_2 не покрыт
                    }
                ],
                "edges": [],
            }
        )


def test_stage3_guard_dangling_edge() -> None:
    _, plan_model = build_stage3_models(
        block_types=BLOCK_TYPES, step_ids=["step_1"]
    )
    with pytest.raises(ValidationError):
        plan_model.model_validate(
            {
                "nodes": [
                    {
                        "id": "block_1",
                        "block_type": BLOCK_TYPES[0],
                        "step_ids": ["step_1"],
                    }
                ],
                "edges": [{"from": "block_1", "to": "block_missing"}],
            }
        )