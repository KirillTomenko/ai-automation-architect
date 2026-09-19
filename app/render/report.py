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
- Нумерация этапов фиксированная (1/2/3): пустой этап не выбрасывается, а
  получает явную пометку «не выявлено». Молчаливый пропуск выглядел бы как
  недоделанный отчёт, а отсутствие промежуточной зоны само по себе говорит
  о процессе (шаги резко делятся на «автоматизируется целиком» и «за человеком»).
- Технические ссылки на step_id в скобках («(step_2, step_4)», «(step_4)»)
  вычищаются из всего клиентского текста: пайплайн иногда ссылается на шаги
  идентификаторами (пример — риск прогона A кейса 8), в отчёте клиенту им не
  место. Скобка убирается целиком, только если ВСЁ её содержимое — step_id
  через запятую, поэтому «(30%)» и пояснения в скобках не затрагиваются.
  Сам blueprint не меняется — очистка только при рендере.
- Раздел «Итог» (сразу после описания процесса, перед Этапом 1): агрегат
  по этапам — сколько шагов можно автоматизировать сразу, сколько частично,
  сколько остаётся за человеком (числа согласуются по-русски; группы без
  шагов не печатаются, чтобы не было механических нулей). Плюс суммарный
  диапазон трудозатрат: диапазоны estimated_effort складываются в один
  диапазон, а не перечисляются по блокам. Если формат оценок не распознан —
  оценка не печатается вовсе, а не выдумывается. Вырожденный случай (Этап 1
  пуст, ни один шаг не автоматизируется целиком) получает явную оговорку
  перед оценкой: это стоимость инфраструктуры маршрутизации и эскалации,
  а не автоматизации самого процесса.
- В «MVP-scope» над списками in/out — одно предложение человеческим языком:
  система берёт на себя шаги вне human_in_the_loop, остальные остаются за
  сотрудником. Собирается из имён шагов этого blueprint, не хардкод.
- Раздел «Следующий шаг» в самом конце — текст-заглушка, параметр
  next_step_text с дефолтом DEFAULT_NEXT_STEP_TEXT: конкретные контакты
  в код не зашиваются, их задаёт вызывающий.
"""

import re
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

# Нумерация этапов в отчёте фиксированная (1/2/3): этап без шагов не
# выбрасывается, а получает явную пометку «не выявлено» — молчаливый пропуск
# выглядел бы как недоделанный отчёт, а «резкое» деление шагов между полной
# автоматизацией и участием человека — само по себе наблюдение о процессе.
_STAGE_TITLES = {
    1: "Этап 1 — автоматизировать в первую очередь",
    2: "Этап 2 — частичная автоматизация",
    3: "Этап 3 — остаётся за человеком",
}
_STAGE_INTROS = {1: _STAGE1_INTRO, 2: _STAGE2_INTRO, 3: _STAGE3_INTRO}
_STAGE_EMPTY_NOTE = {
    1: ("Не выявлено шагов, которые можно автоматизировать целиком: "
        "каждый шаг автоматизируется частично или остаётся за человеком."),
    2: ("Не выявлено шагов с частичной автоматизацией в этом процессе: "
        "между «автоматизируется целиком» и «остаётся за человеком» "
        "промежуточной зоны нет."),
    3: ("Не выявлено шагов, которые полностью остаются за человеком: "
        "каждый шаг автоматизируется целиком или частично."),
}

# Техническая ссылка: скобка, внутри которой ТОЛЬКО step_id через запятую.
_STEP_ID_PAREN_RE = re.compile(r"\s*\((?:step_\d+(?:\s*,\s*step_\d+)*)\)")


def _client_text(text: str) -> str:
    """Клиентский текст без технических ссылок на step_id в скобках."""
    return _STEP_ID_PAREN_RE.sub("", text)


# Текст-заглушка финальной секции; контакты задаёт вызывающий, не код.
DEFAULT_NEXT_STEP_TEXT = "Свяжитесь с нами, чтобы обсудить детали внедрения."

# Оценка трудозатрат блока: «2–3 человеко-дня» (тире — длинное, среднее или дефис).
_ESTIMATE_RE = re.compile(r"^(\d+)\s*[–—-]\s*(\d+)\s*человеко-дн", re.IGNORECASE)


def _is_singular_ru(n: int) -> bool:
    """Согласование в единственном числе: 1, 21, 31… (но не 11)."""
    return n % 10 == 1 and n % 100 != 11


def _person_days_word(n: int) -> str:
    """«человеко-день / человеко-дня / человеко-дней» по числу."""
    if n % 100 in (11, 12, 13, 14):
        return "человеко-дней"
    if n % 10 == 1:
        return "человеко-день"
    if n % 10 in (2, 3, 4):
        return "человеко-дня"
    return "человеко-дней"


def _ru_list(items: list[str]) -> str:
    """Список в ёлочках: «A», «B» и «C»."""
    quoted = [f"«{item}»" for item in items]
    if len(quoted) == 1:
        return quoted[0]
    return ", ".join(quoted[:-1]) + " и " + quoted[-1]


def _itog_paragraph(
    blueprint: "Blueprint", stage1: list[str], stage2: list[str], stage3: list[str]
) -> str | None:
    """Агрегат для раздела «Итог»: этапы + суммарный диапазон трудозатрат."""
    n_total = len(blueprint.steps)
    if not n_total:
        return None
    n1, n2, n3 = len(stage1), len(stage2), len(stage3)
    if not n1:
        # Вырожденный случай: автоматизировать целиком нечего. Печатаем
        # явную оговорку, а не суммарный диапазон «как обычно» — иначе
        # оценка читается как стоимость автоматизации, которой не будет.
        return _itog_degenerate(blueprint, n_total)
    parts = []
    if n1:
        parts.append(f"{n1} можно автоматизировать сразу")
    if n2:
        verb = "требует" if _is_singular_ru(n2) else "требуют"
        parts.append(f"{n2} {verb} частичной автоматизации")
    if n3:
        verb = "остаётся" if _is_singular_ru(n3) else "остаются"
        parts.append(f"{n3} {verb} за человеком")
    if not parts:
        return None
    gen = "шага" if _is_singular_ru(n_total) else "шагов"
    if n1 + n2 + n3 == n_total:
        itog = f"Из {n_total} {gen} процесса {', '.join(parts)}."
    else:
        itog = f"Процесс состоит из {n_total} {gen}: {', '.join(parts)}."
    effort = _effort_sentence(blueprint)
    if effort:
        itog += " " + effort
    return itog


def _effort_range_days(blueprint: "Blueprint") -> tuple[int, int] | None:
    """Суммарный диапазон estimated_effort в человеко-днях или None."""
    lows: list[int] = []
    highs: list[int] = []
    for estimate in blueprint.estimated_effort.values():
        match = _ESTIMATE_RE.match(estimate.strip())
        if match:
            lows.append(int(match.group(1)))
            highs.append(int(match.group(2)))
        else:
            return None  # незнакомый формат — оценку не придумываем
    if not lows:
        return None
    return sum(lows), sum(highs)


def _effort_sentence(blueprint: "Blueprint") -> str | None:
    """Суммарный диапазон estimated_effort одним предложением или None."""
    rng = _effort_range_days(blueprint)
    if rng is None:
        return None
    lo, hi = rng
    if lo == hi:
        return f"Ориентировочная оценка внедрения MVP: {lo} {_person_days_word(lo)}."
    return f"Ориентировочная оценка внедрения MVP: {lo}–{hi} {_person_days_word(hi)}."


def _itog_degenerate(blueprint: "Blueprint", n_total: int) -> str:
    """«Итог» для вырожденного случая (Этап 1 пуст): явная оговорка перед
    оценкой — без неё суммарный диапазон читается как обычная MVP-оценка
    автоматизации, хотя автоматизировать целиком нечего (см. риски)."""
    gen = "шага" if _is_singular_ru(n_total) else "шагов"
    itog = (
        f"Из {n_total} {gen} процесса ни один не автоматизируется полностью — все "
        "требуют участия человека из-за неопределённостей в описании процесса "
        "(см. риски ниже)."
    )
    rng = _effort_range_days(blueprint)
    if rng is not None:
        lo, hi = rng
        if lo == hi:
            amount = f"{lo} {_person_days_word(lo)}"
        else:
            amount = f"{lo}–{hi} {_person_days_word(hi)}"
        itog += (
            " Оценка ниже — это стоимость построения инфраструктуры маршрутизации "
            f"и эскалации, а не стоимость автоматизации самого процесса: {amount}."
        )
    return itog


def _mvp_summary_sentence(blueprint: "Blueprint", hitl: set[str]) -> str | None:
    """Обобщающее предложение для MVP-scope из шагов этого blueprint."""
    auto_names = [_client_text(s.name) for s in blueprint.steps if s.id not in hitl]
    human_names = [_client_text(s.name) for s in blueprint.steps if s.id in hitl]
    if auto_names and human_names:
        noun = "шаг" if len(human_names) == 1 else "шаги"
        verb = "останется" if len(human_names) == 1 else "останутся"
        return (
            f"Вы получите систему, которая сама возьмёт на себя {_ru_list(auto_names)}, — "
            f"{noun} {_ru_list(human_names)} пока {verb} за сотрудником."
        )
    if auto_names:
        return (
            "Вы получите систему, которая сама возьмёт на себя все шаги процесса: "
            f"{_ru_list(auto_names)}."
        )
    if human_names:
        return (
            "Вы получите основу для автоматизации — карту процесса, актёров и "
            f"архитектуру, — сами шаги {_ru_list(human_names)} пока останутся за сотрудником."
        )
    return None


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
        bullet = f"- **{_client_text(step.name)}** — {cand.automation_pct}%"
        if not first:
            # Этап 2: reasoning из JSON встроен в фразу как объяснение,
            # без метки поля — единственная доступная без LLM
            # «переформулировка»; суть не меняется.
            bullet += f" — {_client_text(cand.reasoning)}"
        bullets.append(bullet)
    return bullets


def _bullets_stage3(blueprint: "Blueprint") -> list[str]:
    bullets = []
    for number, step in enumerate(blueprint.steps, start=1):
        if step.id not in blueprint.human_in_the_loop:
            continue
        reason = _client_text(_reason_for_hitl_step(blueprint, step.id, number))
        bullets.append(f"- **{_client_text(step.name)}** — {reason}")
    return bullets


def generate_report(
    blueprint: "Blueprint", next_step_text: str = DEFAULT_NEXT_STEP_TEXT
) -> str:
    """Финальный Blueprint → Markdown-отчёт для клиента. Без LLM, детерминированно."""
    candidates = _candidate_map(blueprint)
    hitl = set(blueprint.human_in_the_loop)

    stage1 = _bullets_stage_auto(blueprint, candidates, hitl, first=True)
    stage2 = _bullets_stage_auto(blueprint, candidates, hitl, first=False)
    stage3 = _bullets_stage3(blueprint)

    lines = ["# Automation Blueprint — отчёт для клиента", "", blueprint.process, ""]

    itog = _itog_paragraph(blueprint, stage1, stage2, stage3)
    if itog:
        lines += ["## Итог", "", itog, ""]

    stage_bullets = {1: stage1, 2: stage2, 3: stage3}
    for number in (1, 2, 3):
        lines += [f"## {_STAGE_TITLES[number]}", ""]
        if stage_bullets[number]:
            lines += [_STAGE_INTROS[number], "", *stage_bullets[number], ""]
        else:
            lines += [_STAGE_EMPTY_NOTE[number], ""]

    if blueprint.risks:
        lines += ["## Риски", ""]
        lines += [f"- {_client_text(risk)}" for risk in blueprint.risks] + [""]
    if blueprint.mvp_scope.in_ or blueprint.mvp_scope.out:
        lines += ["## MVP-scope", ""]
        summary = _mvp_summary_sentence(blueprint, hitl)
        if summary:
            lines += [summary, ""]
        if blueprint.mvp_scope.in_:
            lines += ["Входит в MVP:"] + [f"- {_client_text(item)}" for item in blueprint.mvp_scope.in_] + [""]
        if blueprint.mvp_scope.out:
            lines += ["За пределами MVP:"] + [f"- {_client_text(item)}" for item in blueprint.mvp_scope.out] + [""]

    lines += ["## Следующий шаг", "", _client_text(next_step_text), ""]

    return "\n".join(lines).rstrip()