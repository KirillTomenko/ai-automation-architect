"""Тест Mermaid-рендера на финальных blueprint'ах канонических кейсов (print-only).

Рендер печатается целиком — синтаксис диаграмм и сохранность note проверяем
глазами (утверждений нет — конвенция проекта). Без LLM: blueprint'ы берутся
готовые — examples/blueprints (финальный Automation Blueprint, собранный
Stage 4 из канонических фикстур Stage 1–3). Запуск из корня проекта:
    python -m pytest -s tests/test_render.py
"""

from pathlib import Path

from app.config_loader import load_block_catalog, load_taxonomy
from app.render.mermaid import architecture_to_mermaid, process_flow_to_mermaid
from app.schemas import build_blueprint_model

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BLUEPRINT_DIR = PROJECT_ROOT / "examples" / "blueprints"
NAMES = ["example_1", "example_2_ambiguous", "example_3_booking", "example_4_ecommerce"]


def _load_blueprint(name: str):
    taxonomy = {c.name: (c.range_min, c.range_max) for c in load_taxonomy()}
    block_types = [b.name for b in load_block_catalog()]
    raw = (BLUEPRINT_DIR / f"{name}.json").read_text(encoding="utf-8-sig")
    return build_blueprint_model(
        taxonomy=taxonomy, block_types=block_types
    ).model_validate_json(raw)


def test_render_process_flow() -> None:
    for name in NAMES:
        blueprint = _load_blueprint(name)
        print(f"\n===== process_flow_to_mermaid: {name} =====")
        print(process_flow_to_mermaid(blueprint))


def test_render_architecture() -> None:
    for name in NAMES:
        blueprint = _load_blueprint(name)
        print(f"\n===== architecture_to_mermaid: {name} =====")
        print(architecture_to_mermaid(blueprint))