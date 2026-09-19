"""Тесты детерминированного текстового отчёта (app/render/report.py).

Без LLM: blueprint'ы берутся готовые из examples/blueprints. Проверяются
правила группировки (HITL приоритетнее процента, диапазоны этапов), источники
причины в Этапе 3 (note архитектуры → риск о шаге → общая формулировка),
фиксированная нумерация этапов (пустой этап получает явную пометку «не
выявлено», а не выбрасывается), verbatim-сохранность risks/mvp_scope и
отсутствие технических терминов.
Запуск из корня проекта:
    python -m pytest tests/test_report.py
"""

from pathlib import Path

from app.config_loader import load_block_catalog, load_taxonomy
from app.render.report import generate_report
from app.schemas import build_blueprint_model

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BLUEPRINT_DIR = PROJECT_ROOT / "examples" / "blueprints"


def _load_blueprint(name: str):
    taxonomy = {c.name: (c.range_min, c.range_max) for c in load_taxonomy()}
    block_types = [b.name for b in load_block_catalog()]
    raw = (BLUEPRINT_DIR / f"{name}.json").read_text(encoding="utf-8-sig")
    return build_blueprint_model(
        taxonomy=taxonomy, block_types=block_types
    ).model_validate_json(raw)


def test_report_structure_and_buckets() -> None:
    """example_1: секции есть ⇔ есть шаги в их диапазоне; шаги не перепутаны."""
    blueprint = _load_blueprint("example_1")
    report = generate_report(blueprint)

    by_cand = {c.step_id: c for c in blueprint.automation_candidates}
    non_hitl = [s for s in blueprint.steps if s.id not in blueprint.human_in_the_loop]
    # Нумерация этапов фиксированная: все три заголовка есть всегда, пустой
    # этап помечается «не выявлено», а не выбрасывается.
    assert "## Этап 1" in report
    assert "## Этап 2" in report
    assert "## Этап 3" in report
    assert "## Риски" in report
    assert "## MVP-scope" in report

    def _section(heading: str) -> str:
        return report.split(heading)[1].split("##")[0] if heading in report else ""

    stage1 = _section("## Этап 1")
    stage2 = _section("## Этап 2")
    stage3 = _section("## Этап 3")
    # Пустая секция содержит пометку «не выявлено», непустая — только буллеты.
    has_full = any(by_cand[s.id].automation_pct >= 80 for s in non_hitl)
    has_partial = any(by_cand[s.id].automation_pct < 80 for s in non_hitl)
    assert ("Не выявлено шагов, которые можно автоматизировать целиком" in stage1) != has_full
    assert ("Не выявлено шагов с частичной автоматизацией" in stage2) != has_partial
    # Порядок этапов фиксированный, без пропусков номеров.
    assert report.index("## Этап 1") < report.index("## Этап 2") < report.index("## Этап 3")
    for step in blueprint.steps:
        cand = by_cand[step.id]
        if step.id in blueprint.human_in_the_loop:
            assert step.name not in stage1
            assert step.name in stage3
        elif cand.automation_pct >= 80:
            assert step.name in stage1


def test_empty_stage2_gets_explicit_note() -> None:
    """example_1 и example_7: все не-HITL шаги >=80% — Этап 2 пуст, но секция
    на месте с явной пометкой «не выявлено» (нумерация 1/2/3 без пропусков)."""
    for name in ["example_1", "example_7_refund_claim"]:
        blueprint = _load_blueprint(name)
        report = generate_report(blueprint)
        by_cand = {c.step_id: c for c in blueprint.automation_candidates}
        non_hitl = [s for s in blueprint.steps if s.id not in blueprint.human_in_the_loop]
        assert all(by_cand[s.id].automation_pct >= 80 for s in non_hitl)  # предпосылка
        assert "## Этап 2 — частичная автоматизация" in report
        assert "Не выявлено шагов с частичной автоматизацией" in report
        assert report.index("## Этап 1") < report.index("## Этап 2") < report.index("## Этап 3")


def test_step_id_paren_refs_cleaned_from_report() -> None:
    """Технические ссылки «(step_2, step_4)» из риск-формулировок не попадают
    в клиентский отчёт (артефакт прогона A кейса 8); содержательные скобки
    вроде «(30%)» и «(перенос заявки в таблицу)» сохраняются."""
    blueprint = _load_blueprint("example_1")
    blueprint.risks = [
        *blueprint.risks,
        "Шаги с низким процентом автоматизации (step_2, step_4) требуют участия юристов",
    ]
    report = generate_report(blueprint)
    assert "(step_2, step_4)" not in report
    assert "Шаги с низким процентом автоматизации требуют участия юристов" in report
    assert "(30%)" in report
    assert "(перенос заявки в таблицу)" in report


def test_hitl_note_step_id_paren_cleaned() -> None:
    """Причина в Этапе 3 из note архитектуры тоже чистится от «(step_N)»."""
    blueprint = _load_blueprint("example_1")
    for node in blueprint.architecture.nodes:
        if "step_4" in node.step_ids:
            node.note = "Эскалация на руководителя (step_4) при сложных обращениях"
    report = generate_report(blueprint)
    assert "(step_4)" not in report
    assert "Эскалация на руководителя при сложных обращениях" in report


def test_hitl_takes_precedence_over_high_pct() -> None:
    """example_5: step_1 имеет 80%, но в HITL (канал входа не назван) — только Этап 3."""
    blueprint = _load_blueprint("example_5_hr_onboarding")
    report = generate_report(blueprint)
    step1 = next(s for s in blueprint.steps if s.id == "step_1")
    stage1 = report.split("## Этап 1")[1].split("##")[0]
    stage3 = report.split("## Этап 3")[1].split("##")[0]
    assert step1.name not in stage1
    assert step1.name in stage3
    # Причина — note из архитектуры, а не общая формулировка.
    note = next(n.note for n in blueprint.architecture.nodes if n.note)
    assert note in report


def test_stage3_reason_prefers_note_then_risk() -> None:
    """example_7: причины HITL-шагов — note про неоперационализированные критерии."""
    blueprint = _load_blueprint("example_7_refund_claim")
    report = generate_report(blueprint)
    assert "Критерии 'небольшая сумма', 'крупная или спорная' не определены в тексте" in report


def test_low_pct_non_hitl_steps_not_lost() -> None:
    """example_6: step_6 (30%, вне HITL) не выпадает из отчёта — попадает в Этап 2."""
    blueprint = _load_blueprint("example_6_logistics")
    report = generate_report(blueprint)
    step6 = next(s for s in blueprint.steps if s.id == "step_6")
    assert step6.id not in blueprint.human_in_the_loop
    stage2 = report.split("## Этап 2")[1].split("##")[0]
    assert step6.name in stage2
    assert "Не выявлено" not in stage2  # при наличии шагов пометка не нужна


def test_risks_and_mvp_verbatim() -> None:
    """Risks и mvp_scope входят дословно — отчёт не искажает факты JSON."""
    blueprint = _load_blueprint("example_2_ambiguous")
    report = generate_report(blueprint)
    for risk in blueprint.risks:
        assert risk in report
    for item in blueprint.mvp_scope.in_:
        assert item in report
    for item in blueprint.mvp_scope.out:
        assert item in report


def test_no_technical_terms_and_deterministic() -> None:
    """Ни категорий таксономии, ни типов блоков; повторный вызов даёт тот же текст."""
    taxonomy = {c.name: (c.range_min, c.range_max) for c in load_taxonomy()}
    block_types = [b.name for b in load_block_catalog()]
    for name in ["example_1", "example_2_ambiguous", "example_7_refund_claim"]:
        blueprint = _load_blueprint(name)
        report = generate_report(blueprint)
        for category in taxonomy:
            assert category not in report
        for block_type in block_types:
            assert block_type not in report
        assert generate_report(blueprint) == report