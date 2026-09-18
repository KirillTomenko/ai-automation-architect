"""Прогон Stage 1 (Process Extractor), Stage 2 (Classifier) и Stage 3 (Architecture Composer).

Ассертов намеренно нет: цель — посмотреть фактический выход LLM глазами.
Структурные тесты стадий добавим после ревизии выходов.

Запуск из корня проекта:
    python -m pytest -s tests/test_pipeline.py                  # всё
    python -m pytest -s tests/test_pipeline.py -k example_2     # один пример
    python -m pytest -s tests/test_pipeline.py -k stage2        # только Stage 2
    python -m pytest -s tests/test_pipeline.py -k stage3        # только Stage 3
    # с логами стадий:
    python -m pytest -s -o log_cli=true --log-cli-level=INFO tests/test_pipeline.py -k stage3
"""

import asyncio
import json
from pathlib import Path

from app.config_loader import load_block_catalog, load_taxonomy
from app.pipeline.stage1_extractor import extract_process
from app.pipeline.stage2_classifier import classify_steps
from app.pipeline.stage3_architect import compose_architecture
from app.schemas import ExtractedProcess, build_stage2_models

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_1 = PROJECT_ROOT / "examples" / "raw" / "example_1.txt"
EXAMPLE_2_AMBIGUOUS = PROJECT_ROOT / "examples" / "raw" / "example_2_ambiguous.txt"
EXTRACTED_DIR = PROJECT_ROOT / "examples" / "extracted"
CLASSIFIED_DIR = PROJECT_ROOT / "examples" / "classified"


def _run_stage1_and_print(example_path: Path) -> None:
    description = example_path.read_text(encoding="utf-8-sig").strip()

    print("\n=== Вход ===")
    print(description)

    result = asyncio.run(extract_process(description))

    print("\n=== Stage 1: ExtractedProcess ===")
    print(json.dumps(result.model_dump(), ensure_ascii=False, indent=2))


def test_stage1_example_1_prints_result():
    _run_stage1_and_print(EXAMPLE_1)


def test_stage1_example_2_ambiguous_prints_result():
    _run_stage1_and_print(EXAMPLE_2_AMBIGUOUS)


def _run_stage2_and_print(example_name: str) -> None:
    """Классифицирует шаги из сохранённого выхода Stage 1 (Stage 1 не перезапускается)."""
    stage1_path = EXTRACTED_DIR / f"{example_name}.json"
    stage1 = ExtractedProcess.model_validate_json(stage1_path.read_text(encoding="utf-8-sig"))

    print(f"\n=== Stage 2: вход — шаги и assumptions из Stage 1 ({stage1_path.name}) ===")
    print(json.dumps(stage1.model_dump(), ensure_ascii=False, indent=2))

    result = asyncio.run(classify_steps(stage1, load_taxonomy()))

    print(f"\n=== Stage 2: ClassifiedProcess ({stage1_path.name}) ===")
    print(json.dumps(result.model_dump(), ensure_ascii=False, indent=2))


def test_stage2_example_1_prints_result():
    _run_stage2_and_print("example_1")


def test_stage2_example_2_ambiguous_prints_result():
    _run_stage2_and_print("example_2_ambiguous")


def _load_stage1_fixture(example_name: str) -> ExtractedProcess:
    stage1_path = EXTRACTED_DIR / f"{example_name}.json"
    return ExtractedProcess.model_validate_json(stage1_path.read_text(encoding="utf-8-sig"))


def _load_stage2_fixture(example_name: str, stage1: ExtractedProcess):
    """Stage 2 — динамическая модель, поэтому читаем фикстуру через build_stage2_models."""
    stage2_path = CLASSIFIED_DIR / f"{example_name}.json"
    _, classified_model = build_stage2_models(
        taxonomy={c.name: (c.range_min, c.range_max) for c in load_taxonomy()},
        step_ids=[step.id for step in stage1.steps],
    )
    return classified_model.model_validate_json(stage2_path.read_text(encoding="utf-8-sig"))


def _run_stage3_and_print(example_name: str) -> None:
    """Строит архитектуру из сохранённых выходов Stage 1+2 (они не перезапускаются)."""
    stage1 = _load_stage1_fixture(example_name)
    stage2 = _load_stage2_fixture(example_name, stage1)

    print(f"\n=== Stage 3: вход — шаги и assumptions из Stage 1 ({example_name}) ===")
    print(json.dumps(stage1.model_dump(), ensure_ascii=False, indent=2))
    print("\n=== Stage 3: вход — классификация из Stage 2 ===")
    print(json.dumps(stage2.model_dump(), ensure_ascii=False, indent=2))

    result = asyncio.run(compose_architecture(stage1, stage2, load_block_catalog()))

    print(f"\n=== Stage 3: ArchitecturePlan ({example_name}) ===")
    print(json.dumps(result.model_dump(by_alias=True, exclude_none=True), ensure_ascii=False, indent=2))


def test_stage3_example_1_prints_result():
    _run_stage3_and_print("example_1")


def test_stage3_example_2_ambiguous_prints_result():
    _run_stage3_and_print("example_2_ambiguous")