"""JSON Blueprint → Mermaid-диаграммы (строковый рендер, без фронта).

process_flow_to_mermaid — flow процесса: шаги Stage 1 в порядке следования,
рёбра неявно по порядку (step_1 → step_2 → ...). В финальной схеме нет данных
о ветвлении процесса (критерии ветвления Stage 1 — gap-пометки, а не граф),
поэтому поток линейный; шаги из human_in_the_loop выделяются стилем и меткой 🧑.

architecture_to_mermaid — граф блоков Stage 3: подписи узлов — label из
config/block_catalog.yaml; node.note (канал/gap-сигнал) выводится в текст узла
и подсвечивается — информация не теряется при рендере.
"""

from typing import TYPE_CHECKING

from app.config_loader import load_block_catalog

if TYPE_CHECKING:
    from app.schemas import Blueprint

_HITL = "hitl"
_GAP = "gap"

_HITL_CLASSDEF = "classDef hitl fill:#ffe3e3,stroke:#c0392b,stroke-width:2px;"
_GAP_CLASSDEF = "classDef gap fill:#fff3cd,stroke:#b8860b,stroke-width:2px;"


def _esc(text: str) -> str:
    """Экранирование двойных кавычек внутри Mermaid-строки в кавычках."""
    return text.replace('"', "&quot;")


def process_flow_to_mermaid(blueprint: "Blueprint") -> str:
    """Шаги процесса в порядке следования; human_in_the_loop — стиль + метка 🧑."""
    lines = ["flowchart TD"]
    hitl = set(blueprint.human_in_the_loop)
    for step in blueprint.steps:
        label = _esc(step.name)
        if step.id in hitl:
            label = f"🧑 {label}"
        lines.append(f'    {step.id}["{label}"]')
    for prev, nxt in zip(blueprint.steps, blueprint.steps[1:]):
        lines.append(f"    {prev.id} --> {nxt.id}")
    hitl_ids = [step.id for step in blueprint.steps if step.id in hitl]
    if hitl_ids:
        lines.append(f"    {_HITL_CLASSDEF}")
        lines.append(f"    class {','.join(hitl_ids)} {_HITL}")
    return "\n".join(lines)


def architecture_to_mermaid(
    blueprint: "Blueprint", catalog_labels: dict[str, str] | None = None
) -> str:
    """Граф блоков Stage 3; label блока из каталога, note — в текст узла + подсветка.

    catalog_labels можно передать явно (например, если фронт уже загрузил
    конфиг); по умолчанию подставляется из config/block_catalog.yaml.
    """
    if catalog_labels is None:
        catalog_labels = {block.name: block.label for block in load_block_catalog()}

    lines = ["flowchart TD"]
    gap_ids: list[str] = []
    for node in blueprint.architecture.nodes:
        text = _esc(catalog_labels.get(node.block_type, node.block_type))
        if node.note:
            text += f"<br/>⚠️ {_esc(node.note)}"
            gap_ids.append(node.id)
        lines.append(f'    {node.id}["{text}"]')
    for edge in blueprint.architecture.edges:
        lines.append(f"    {edge.from_} --> {edge.to}")
    if gap_ids:
        lines.append(f"    {_GAP_CLASSDEF}")
        lines.append(f"    class {','.join(gap_ids)} {_GAP}")
    return "\n".join(lines)