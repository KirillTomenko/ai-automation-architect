"""Blueprint → текстовый отчёт для клиента (детерминированный рендер, без LLM).

Отчёт — трансформация уже готовых полей финального Blueprint: шаги,
automation_candidates, human_in_the_loop, note'ы архитектуры, risks, mvp_scope.
Ни одного LLM-вызова; язык — простой, без технических терминов категорий и
типов блоков: отчёт читает клиент, а не разработчик. Категория шага нигде не
печатаются — «переводчиком» с таксономии на человеческий язык служат
формулировки секций.

Правила группировки (детерминированные):
- Этап 3 (human_in_the_loop) приоритетнее: шаг из HITL не попадает в Этапы 1/2,
  даже при высоком проценте — пайплайн уже решил, что он за человеком
  (например, step_1 в example_5: 80%, но канал входа не назван в тексте).
- Этап 1: automation_pct >= 80 и не в HITL.
- Этап 2: 40–79% и не в HITL; сюда же — шаги ниже 40% вне HITL: пайплайн не
  оставил их человеку целиком (в архитектуре у них автоматические блоки),
  терять их из отчёта нельзя — пример: step_6 example_6 (30%, Notifier).
- Причина в Этапе 3: note блока архитектуры, покрывающего шаг (пробел данных);
  если note нет — риск, упоминающий шаг (по названию, «Шаг N» или id);
  иначе — общая формулировка без привязки к конкретному тексту.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.schemas import Blueprint

_AUTO_MIN = 80  # Этап 1: от этой доли автоматизации шаг идёт «в первую очередь»;
# Этап 2 — все шаги вне HITL ниже этой границы (40–79% плюс <40% вне HITL)

_STAGE1_INTRO = (
    "Эти части процесса могут выполняться автоматически — без участия сотрудника."
)
_STAGE2_INTRO = (
    "Эти части процесса можно автоматизировать частично: система возьмёт на себя "
    "часть работы, но человек останется в процессе."
)
_STAGE3_INTRO = (
    "Эти шаги остаются за человеком — автоматизация здесь упирается в участие сотрудника."
)
_HITL_FALLBACK = (
    "требует человеческого участия: передать этот шаг системе "
    "без потери качества нельзя"
)


def _reason_for_hitl_step(
    blueprint: "Blueprint", step_id: str, step_number: int
) -> str:
    """Причина, почему шаг остаётся за человеком: note → риск о шаге → общая фраза."""
    for node in blueprint.architecture.nodes:
        if step_id in node.step_ids and node.note:
            return node.note

    for step in blueprint.steps:
        if step.id == step_id:
            markers = [step.name.lower(), f"шаг {step_number}", step_id]
            for risk in blueprint.risks:
                low = risk.lower()
                if any(marker in low for marker in markers):
                    return risk
            break
    return _HITL_FALLBACK


def _candidate_map(blueprint: "Blueprint") -> dict[str, object]:
    """step_id → карточка Stage 2 (у кандидатов и шагов множества могут расходиться)."""
    return {c.step_id: c for c in blueprint.automation_candidates}


def _bullets_stage_auto(
    blueprint: "Blueprint",
    candidates: dict[str, object],
    exclude: set[str],
    *,
    first: bool,
) -> list[str]:
    """Буллеты этапов 1/2. first=True → pct >= 80; иначе — все вне HITL ниже 80:
    и диапазон 40–79, и шаги <40 вне HITL (пайплайн не оставил их человеку
    целиком — в архитектуре у них автоматические блоки; терять из отчёта нельзя).
    """
    bullets = []
    for step in blueprint.steps:
        if step.id in exclude or step.id not in candidates:
            continue
        cand = candidates[step.id]
        eligible = cand.automation_pct >= _AUTO_MIN if first else cand.automation_pct < _AUTO_MIN
        if not eligible:
            continue
        bullet = f"- **{step.name}** — {cand.automation_pct}%"
        if not first:
            # Этап 2: reasoning из JSON встроен в фразу как объяснение,
            # без метки поля — единственная доступная без LLM
            # «переформулировка»; суть не меняется.
            bullet += f" — {cand.reasoning}"
        bullets.append(bullet)
    return bullets


def _bullets_stage3(blueprint: "Blueprint") -> list[str]:
    bullets = []
    for number, step in enumerate(blueprint.steps, start=1):
        if step.id not in blueprint.human_in_the_loop:
            continue
        reason = _reason_for_hitl_step(blueprint, step.id, number)
        bullets.append(f"- **{step.name}** — {reason}")
    return bullets


def generate_report(blueprint: "Blueprint") -> str:
    """Финальный Blueprint → Markdown-отчёт для клиента. Без LLM, детерминированно."""
    candidates = _candidate_map(blueprint)
    hitl = set(blueprint.human_in_the_loop)

    stage1 = _bullets_stage_auto(blueprint, candidates, hitl, first=True)
    stage2 = _bullets_stage_auto(blueprint, candidates, hitl, first=False)
    stage3 = _bullets_stage3(blueprint)

    lines = ["# Automation Blueprint — отчёт для клиента", "", blueprint.process, ""]

    if stage1:
        lines += ["## Этап 1 — автоматизировать в первую очередь", "", _STAGE1_INTRO, ""]
        lines += stage1 + [""]
    if stage2:
        lines += ["## Этап 2 — частичная автоматизация", "", _STAGE2_INTRO, ""]
        lines += stage2 + [""]
    if stage3:
        lines += ["## Этап 3 — остаётся за человеком", "", _STAGE3_INTRO, ""]
        lines += stage3 + [""]

    if blueprint.risks:
        lines += ["## Риски", ""]
        lines += [f"- {risk}" for risk in blueprint.risks] + [""]
    if blueprint.mvp_scope.in_ or blueprint.mvp_scope.out:
        lines += ["## MVP-scope", ""]
        if blueprint.mvp_scope.in_:
            lines += ["Входит в MVP:"] + [f"- {item}" for item in blueprint.mvp_scope.in_] + [""]
        if blueprint.mvp_scope.out:
            lines += ["За пределами MVP:"] + [f"- {item}" for item in blueprint.mvp_scope.out] + [""]

    return "\n".join(lines).rstrip()