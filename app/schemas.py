"""Pydantic-схемы пайплайна — единый источник правды для всех стадий и фронта.

Сейчас описан выход Stage 1 (Process Extractor). Модели финального Blueprint
добавляются сюда по мере реализации стадий 2–4.
"""

import logging
from collections.abc import Mapping, Sequence
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

logger = logging.getLogger(__name__)

# Литеральный placeholder для исполнителя/адресата, не названного в тексте.
# Часть контракта Stage 1: модель не выдумывает роли, а ставит placeholder
# (см. STAGE1_SYSTEM_PROMPT); пайплайн кодом связывает его с assumptions.
ACTOR_UNSPECIFIED = "не указано в тексте"


def _strip_non_empty(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("строка не должна быть пустой")
    return cleaned


class ProcessStep(BaseModel):
    """Один шаг процесса (используется и в Stage 1, и в финальном Blueprint).

    Поля *_named/*_gap_reason — структурированная gap-детекция: модель отвечает
    на вопрос «названо/не названо» флагом, а формулировку пробела пишет в
    *_gap_reason. Итоговые assumptions собираются из них кодом (ProcessDraft),
    модель не классифицирует пробелы в свободном тексте сама.
    """

    id: str = Field(..., description="Идентификатор шага, например step_1")
    name: str = Field(..., description="Короткое название действия")
    input: str = Field(..., description="Что поступает на вход шагу")
    output: str = Field(..., description="Что является результатом шага")
    actor: str = Field(
        ...,
        description=f"Исполнитель шага: роль из текста или «{ACTOR_UNSPECIFIED}»",
    )
    actor_named: bool = Field(
        ...,
        description="Исполнитель шага назван в тексте дословно или однозначным синонимом",
    )
    actor_gap_reason: str | None = Field(
        None,
        description="Чего не хватает, если actor_named=false; иначе null",
    )
    branching_criteria_named: bool | None = Field(
        None,
        description="Назван ли критерий ветвления/перехода; null для линейных шагов (ветвления нет)",
    )
    branching_criteria_gap_reason: str | None = Field(
        None,
        description="Чего не хватает, если branching_criteria_named=false; иначе null",
    )

    @field_validator("id", "name", "input", "output", "actor")
    @classmethod
    def _clean(cls, value: str) -> str:
        return _strip_non_empty(value)

    @field_validator("actor")
    @classmethod
    def _normalize_unspecified(cls, value: str) -> str:
        # Модель может написать placeholder в другом регистре — нормализуем
        # к канонической форме, чтобы не жечь retry на косметике.
        if value.casefold() == ACTOR_UNSPECIFIED.casefold():
            return ACTOR_UNSPECIFIED
        return value

    @model_validator(mode="after")
    def _check_gap_fields(self) -> "ProcessStep":
        # Бикондиционал actor: named=false ⇔ непустой gap_reason; named=true ⇔ null.
        if self.actor_named:
            if self.actor_gap_reason is not None:
                raise ValueError(
                    f"шаг '{self.id}': actor_named=true требует actor_gap_reason=null"
                )
        elif not (self.actor_gap_reason and self.actor_gap_reason.strip()):
            raise ValueError(
                f"шаг '{self.id}': actor_named=false требует непустого actor_gap_reason — "
                f"что именно не названо"
            )
        # Бикондиционал placeholder ⇔ actor_named=false (прежний контракт Stage 1,
        # перенесённый с уровня assumptions на уровень шага).
        if (self.actor == ACTOR_UNSPECIFIED) == self.actor_named:
            raise ValueError(
                f"шаг '{self.id}': противоречие между actor='{self.actor}' и actor_named="
                f"{self.actor_named} — placeholder «{ACTOR_UNSPECIFIED}» возможен только при "
                f"actor_named=false, названная роль — только при actor_named=true"
            )
        # Бикондиционал branching_criteria: null ⇔ линейный шаг; при named — как у actor.
        if self.branching_criteria_named is None:
            if self.branching_criteria_gap_reason is not None:
                raise ValueError(
                    f"шаг '{self.id}': branching_criteria_named=null (линейный шаг) требует "
                    f"branching_criteria_gap_reason=null"
                )
        elif self.branching_criteria_named:
            if self.branching_criteria_gap_reason is not None:
                raise ValueError(
                    f"шаг '{self.id}': branching_criteria_named=true требует "
                    f"branching_criteria_gap_reason=null"
                )
        elif not (self.branching_criteria_gap_reason and self.branching_criteria_gap_reason.strip()):
            raise ValueError(
                f"шаг '{self.id}': branching_criteria_named=false требует непустого "
                f"branching_criteria_gap_reason — что именно не названо"
            )
        return self


class Assumption(BaseModel):
    """Одна запись в assumptions — ровно один пробел исходного текста."""

    step_id: str | None = Field(
        None,
        description="id шага, к которому относится пробел; None для общих (канал входа)",
    )
    gap: Literal["actor", "channel", "criteria"] = Field(..., description="Тип пробела в тексте")
    note: str = Field(..., description="Что именно не указано в тексте")

    @field_validator("note")
    @classmethod
    def _clean_note(cls, value: str) -> str:
        return _strip_non_empty(value)


class _ChannelGapCheck(BaseModel):
    """Канальные поля уровня процесса + бикондиционал named ⇔ gap_reason.

    channel_named=false требует непустой channel_gap_reason (чего именно не хватает),
    channel_named=true требует channel_gap_reason=null — тот же паттерн, что у
    actor-полей на шаге.
    """

    channel_named: bool = Field(
        ...,
        description="Канал/платформа поступления первого сигнала названы в тексте",
    )
    channel_gap_reason: str | None = Field(
        None,
        description="Чего не хватает, если channel_named=false; иначе null",
    )

    @model_validator(mode="after")
    def _check_channel_biconditional(self) -> "_ChannelGapCheck":
        if self.channel_named:
            if self.channel_gap_reason is not None:
                raise ValueError("channel_named=true требует channel_gap_reason=null")
        elif not (self.channel_gap_reason and self.channel_gap_reason.strip()):
            raise ValueError(
                "channel_named=false требует непустой channel_gap_reason — что именно не названо"
            )
        return self


class ProcessDraft(_ChannelGapCheck):
    """Сырой выход Stage 1 до кодогенерации assumptions.

    Модель заполняет только *_named/*_gap_reason-поля и НЕ пишет assumptions:
    итоговый список пробелов собирает код (finalize) — модель больше не
    классифицирует пробел в свободном тексте сама.
    """

    actors: list[str] = Field(..., description="Роли/участники, названные в тексте")
    entry_point: str = Field(..., description="Откуда поступает первый сигнал, запускающий процесс")
    steps: list[ProcessStep] = Field(..., description="Шаги в порядке следования")

    @field_validator("actors")
    @classmethod
    def _clean_actors(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for value in values:
            name = _strip_non_empty(value)
            if name.casefold() == ACTOR_UNSPECIFIED.casefold():
                continue  # placeholder не является ролью, в actors ему не место
            if name not in cleaned:
                cleaned.append(name)
        return cleaned

    @field_validator("entry_point")
    @classmethod
    def _clean_entry_point(cls, value: str) -> str:
        return _strip_non_empty(value)

    @model_validator(mode="after")
    def _check_steps(self) -> "ProcessDraft":
        if not self.steps:
            raise ValueError("не извлечён ни один шаг — описание слишком расплывчато или пусто")
        step_ids = {step.id for step in self.steps}
        if len(step_ids) != len(self.steps):
            raise ValueError(f"id шагов не уникальны: {[step.id for step in self.steps]}")
        return self

    def derive_assumptions(self) -> list[Assumption]:
        """Собирает assumptions из *_named/*_gap_reason-полей — КОДОМ, не моделью.

        channel_named=false → одна запись gap='channel' (step_id=None); у каждого
        шага actor_named=false → gap='actor', branching_criteria_named=false →
        gap='criteria'. Текст note берётся из соответствующего *_gap_reason.
        """
        result: list[Assumption] = []
        if not self.channel_named:
            result.append(
                Assumption(step_id=None, gap="channel", note=self.channel_gap_reason or "")
            )
        for step in self.steps:
            if not step.actor_named:
                result.append(
                    Assumption(step_id=step.id, gap="actor", note=step.actor_gap_reason or "")
                )
            if step.branching_criteria_named is False:
                result.append(
                    Assumption(
                        step_id=step.id,
                        gap="criteria",
                        note=step.branching_criteria_gap_reason or "",
                    )
                )
        return result

    def finalize(self) -> "ExtractedProcess":
        """Сырой выход → итоговый ExtractedProcess с assumptions, собранными кодом."""
        return ExtractedProcess(
            actors=self.actors,
            entry_point=self.entry_point,
            steps=self.steps,
            channel_named=self.channel_named,
            channel_gap_reason=self.channel_gap_reason,
            assumptions=self.derive_assumptions(),
        )


class ExtractedProcess(ProcessDraft):
    """Выход Stage 1 — Process Extractor.

    Строго из текста описания: пробелы фиксируются полями *_named/*_gap_reason,
    из которых код собирает assumptions (ProcessDraft.finalize). Роль, не названная
    в тексте, не выдумывается — в step.actor ставится ACTOR_UNSPECIFIED.
    """

    assumptions: list[Assumption] = Field(
        default_factory=list,
        description="Пробелы исходного текста; собираются кодом из *_named/*_gap_reason полей",
    )

    @model_validator(mode="after")
    def _check_consistency(self) -> "ExtractedProcess":
        # Структурные проверки шагов (непустота, уникальность id) — в ProcessDraft.
        step_ids = {step.id for step in self.steps}
        actor_by_id = {step.id: step.actor for step in self.steps}

        seen: set[tuple[str | None, str]] = set()
        for assumption in self.assumptions:
            if assumption.gap == "actor":
                if not assumption.step_id:
                    raise ValueError(f"assumption gap='actor' требует step_id: {assumption.note}")
                if assumption.step_id not in step_ids:
                    raise ValueError(f"assumption gap='actor' ссылается на неизвестный шаг '{assumption.step_id}'")
                # Бикондиционал, прямое направление: запись про исполнителя допустима,
                # только если исполнитель реально не назван (placeholder).
                if actor_by_id[assumption.step_id] != ACTOR_UNSPECIFIED:
                    raise ValueError(
                        f"противоречие: у шага '{assumption.step_id}' исполнитель назван "
                        f"('{actor_by_id[assumption.step_id]}'), а не «{ACTOR_UNSPECIFIED}» — "
                        f"assumption gap='actor' для него недопустима: {assumption.note}"
                    )
            elif assumption.gap == "channel" and assumption.step_id is not None:
                raise ValueError(
                    f"assumption gap='channel' — общий пробел, step_id должен быть null: {assumption.note}"
                )
            elif assumption.gap == "criteria" and assumption.step_id is not None and assumption.step_id not in step_ids:
                raise ValueError(
                    f"assumption gap='criteria' ссылается на неизвестный шаг '{assumption.step_id}'"
                )

            key = (assumption.step_id, assumption.gap)
            if key in seen:
                raise ValueError(
                    f"дублирующая assumption: gap='{assumption.gap}', step_id={assumption.step_id}"
                )
            seen.add(key)

        # Бикондиционал, обратное направление: placeholder-исполнитель обязан иметь
        # запись gap='actor' в assumptions. Для выхода Stage 1 это гарантировано
        # кодогенерацией (derive_assumptions); проверка страхует загруженные извне JSON.
        for step in self.steps:
            if step.actor == ACTOR_UNSPECIFIED and (step.id, "actor") not in seen:
                raise ValueError(
                    f"у шага '{step.id}' actor = «{ACTOR_UNSPECIFIED}», но в assumptions нет "
                    f"записи gap='actor' с step_id='{step.id}'"
                )

        actor_names = set(self.actors)
        for step in self.steps:
            if step.actor != ACTOR_UNSPECIFIED and step.actor not in actor_names:
                raise ValueError(
                    f"актёр '{step.actor}' шага {step.id} не входит в actors: {sorted(actor_names)}"
                )
        return self

    @model_validator(mode="after")
    def _warn_copied_notes(self) -> "ExtractedProcess":
        # Дешёвая структурная проверка шума Stage 1: дословно одинаковый note
        # (после strip/lower) у двух разных шагов — подозрение, что модель
        # скопировала формулировку, а не нашла два реальных одинаковых пробела.
        # Не ошибка и не retry (редкий шум ~1 прогон из 6, не системная
        # проблема) — только WARNING в лог, пайплайн не блокируется.
        first_by_note: dict[str, str] = {}
        for assumption in self.assumptions:
            if assumption.step_id is None:
                continue  # channel-пробел общий: с шагами не сравнивается
            key = assumption.note.strip().lower()
            if not key:
                continue
            first = first_by_note.setdefault(key, assumption.step_id)
            if first != assumption.step_id:
                logger.warning(
                    "assumptions: одинаковый note у шагов %s и %s (совпадение после strip/lower) — "
                    "подозрение на копирование, а не на два реальных пробела: %s",
                    first,
                    assumption.step_id,
                    assumption.note,
                )
        return self


def build_stage2_models(
    taxonomy: Mapping[str, tuple[int, int]],
    step_ids: Sequence[str],
) -> tuple[type[BaseModel], type[BaseModel]]:
    """Строит Stage 2-схемы под конкретную таксономию и набор шагов Stage 1.

    category — Literal строго из имён категорий таксономии (enum, не свободная
    строка); automation_pct обязан попадать в диапазон своей категории — иначе
    ошибка валидации, а не «подрихтованный» кодом процент.
    """
    category_type = Literal[tuple(taxonomy)]
    ranges = dict(taxonomy)
    allowed_step_ids = set(step_ids)

    class AutomationCandidate(BaseModel):
        """Классификация одного шага (совпадает с automation_candidates в финальном Blueprint)."""

        step_id: str
        category: category_type
        automation_pct: int = Field(..., ge=0, le=100)
        reasoning: str

        @field_validator("step_id", "reasoning")
        @classmethod
        def _clean(cls, value: str) -> str:
            return _strip_non_empty(value)

        @model_validator(mode="after")
        def _check_pct_in_range(self) -> "AutomationCandidate":
            low, high = ranges[self.category]
            if not low <= self.automation_pct <= high:
                raise ValueError(
                    f"шаг '{self.step_id}': automation_pct={self.automation_pct} вне диапазона "
                    f"категории '{self.category}' [{low}, {high}] — процент обязан лежать "
                    f"в диапазоне категории из taxonomy.yaml"
                )
            return self

    class ClassifiedProcess(BaseModel):
        """Выход Stage 2 — Classifier: классификация всех шагов Stage 1."""

        automation_candidates: list[AutomationCandidate]

        @field_validator("automation_candidates")
        @classmethod
        def _non_empty(cls, value: list[AutomationCandidate]) -> list[AutomationCandidate]:
            if not value:
                raise ValueError("не классифицирован ни один шаг")
            return value

        @model_validator(mode="after")
        def _check_coverage(self) -> "ClassifiedProcess":
            ids = [candidate.step_id for candidate in self.automation_candidates]
            if len(ids) != len(set(ids)):
                raise ValueError(f"повторяющиеся step_id в классификации: {ids}")
            missing = sorted(allowed_step_ids - set(ids))
            unknown = sorted(set(ids) - allowed_step_ids)
            if missing:
                raise ValueError(f"нет классификации для шагов Stage 1: {missing}")
            if unknown:
                raise ValueError(f"классификация для неизвестных шагов (нет в Stage 1): {unknown}")
            return self

    return AutomationCandidate, ClassifiedProcess


def build_stage3_models(
    block_types: Sequence[str],
    step_ids: Sequence[str],
) -> tuple[type[BaseModel], type[BaseModel]]:
    """Строит Stage 3-схемы под конкретный каталог блоков и набор шагов Stage 1.

    block_type — Literal строго из имён блоков каталога (enum, не свободная
    строка). Каждый node несёт step_ids — шаги Stage 1, которые он реализует;
    каждый шаг обязан быть покрыт хотя бы одним блоком, иначе — ошибка валидации,
    а не «достроенная» кодом архитектура.
    """
    block_type_type = Literal[tuple(block_types)]
    allowed_step_ids = set(step_ids)

    class ArchitectureNode(BaseModel):
        """Блок архитектуры, привязанный к шагам процесса, которые он реализует."""

        id: str = Field(..., description="Идентификатор блока, например block_1")
        block_type: block_type_type
        step_ids: list[str] = Field(
            default_factory=list,
            description="Шаги Stage 1, которые реализует этот блок",
        )
        note: str | None = Field(
            None,
            description="Необязательная пометка узла (например, о неопределённости канала входа)",
        )

        @field_validator("id")
        @classmethod
        def _clean_id(cls, value: str) -> str:
            return _strip_non_empty(value)

        @field_validator("note")
        @classmethod
        def _clean_note(cls, value: str | None) -> str | None:
            # Пустая строка и пробелы — то же, что отсутствие пометки.
            if value is None:
                return None
            return value.strip() or None

        @field_validator("step_ids")
        @classmethod
        def _clean_step_ids(cls, values: list[str]) -> list[str]:
            cleaned: list[str] = []
            for value in values:
                step_id = _strip_non_empty(value)
                if step_id not in cleaned:
                    cleaned.append(step_id)
            return cleaned

    class ArchitectureEdge(BaseModel):
        """Направленное ребро потока данных между блоками (не между шагами)."""

        from_: str = Field(..., alias="from", description="id блока-источника")
        to: str = Field(..., description="id блока-приёмника")

        @field_validator("from_", "to")
        @classmethod
        def _clean(cls, value: str) -> str:
            return _strip_non_empty(value)

    class ArchitecturePlan(BaseModel):
        """Выход Stage 3 — Architecture Composer: граф из блоков каталога."""

        nodes: list[ArchitectureNode]
        edges: list[ArchitectureEdge]

        @field_validator("nodes")
        @classmethod
        def _non_empty_nodes(cls, value: list[ArchitectureNode]) -> list[ArchitectureNode]:
            if not value:
                raise ValueError("архитектура без блоков — нужен хотя бы один node")
            return value

        @model_validator(mode="after")
        def _check_graph(self) -> "ArchitecturePlan":
            node_ids = [node.id for node in self.nodes]
            if len(node_ids) != len(set(node_ids)):
                raise ValueError(f"дубликаты id блоков: {node_ids}")
            node_id_set = set(node_ids)

            covered: set[str] = set()
            for node in self.nodes:
                unknown = sorted(set(node.step_ids) - allowed_step_ids)
                if unknown:
                    raise ValueError(
                        f"блок '{node.id}' (block_type='{node.block_type}') привязан к шагам, "
                        f"которых нет в Stage 1: {unknown}"
                    )
                covered.update(node.step_ids)
            missing = sorted(allowed_step_ids - covered)
            if missing:
                raise ValueError(
                    f"шаги Stage 1 не покрыты ни одним блоком архитектуры: {missing} — "
                    f"каждый шаг обязан быть покрыт хотя бы одним node через step_ids"
                )

            if len(self.nodes) > 1 and not self.edges:
                raise ValueError(
                    "граф из нескольких блоков не содержит ни одного ребра — нет потока данных"
                )
            for edge in self.edges:
                dangling = [end for end in (edge.from_, edge.to) if end not in node_id_set]
                if dangling:
                    raise ValueError(
                        f"ребро {edge.from_} → {edge.to} ссылается на несуществующие блоки: {dangling}"
                    )
            return self

    return ArchitectureNode, ArchitecturePlan


class MvpScope(BaseModel):
    """Границы MVP: что входит и что осознанно откладывается (advisory-часть Stage 4).

    Ключ «in» — зарезервированное слово Python, поэтому поле называется in_
    с alias="in" (тот же паттерн, что from_ у ребра архитектуры); в JSON
    финального blueprint уходит по alias.
    """

    in_: list[str] = Field(..., alias="in", description="Что входит в MVP")
    out: list[str] = Field(..., description="Что осознанно вынесено за пределы MVP")

    @field_validator("in_", "out")
    @classmethod
    def _clean_items(cls, value: list[str]) -> list[str]:
        # Структурная валидация advisory-полей: непустой список, непустые строки.
        # Содержание (сколько пунктов, насколько детально) — на совести LLM.
        if not value:
            raise ValueError("список mvp_scope не должен быть пустым")
        return [_strip_non_empty(item) for item in value]


class BlueprintAdvisory(BaseModel):
    """Выход Stage 4 — Blueprint Packager: advisory-часть blueprint.

    Это рекомендации, а не факты: факты процесса зафиксированы Stage 1–3 и
    здесь не пересказываются. Валидация намеренно только структурная
    (непустые списки, непустые строки) — жёсткий биконд-паттерн Stage 1 здесь
    неуместен: risks/mvp_scope/estimated_effort не факт-чувствительны, и
    строгая валидация тратила бы retry впустую.
    """

    risks: list[str] = Field(..., description="3–5 конкретных рисков ИМЕННО этого процесса")
    mvp_scope: MvpScope
    estimated_effort: dict[str, str] = Field(
        ..., description="Компонент → грубая оценка в человеко-днях"
    )

    @field_validator("risks")
    @classmethod
    def _clean_risks(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("risks пуст — нужен хотя бы один конкретный риск процесса")
        return [_strip_non_empty(item) for item in value]

    @field_validator("estimated_effort")
    @classmethod
    def _clean_effort(cls, value: dict[str, str]) -> dict[str, str]:
        if not value:
            raise ValueError(
                "estimated_effort пуст — нужна оценка хотя бы для одного компонента"
            )
        cleaned: dict[str, str] = {}
        for component, estimate in value.items():
            cleaned[_strip_non_empty(component)] = _strip_non_empty(estimate)
        return cleaned


def build_blueprint_model(
    taxonomy: Mapping[str, tuple[int, int]],
    block_types: Sequence[str],
) -> type[BaseModel]:
    """Строит класс финального Blueprint под таксономию и каталог блоков.

    Категории (Stage 2) и block_type (Stage 3) в финальном JSON остаются
    enum'ами — Blueprint перевалидирует их при сборке и при любой загрузке
    JSON (фронтом и т.п.). Шаг в финальном JSON — упрощённый по схеме
    CLAUDE.md (без gap-полей Stage 1); узел несёт id/block_type/step_ids/note
    как в Stage 3 — связь блок↔шаг и пометки доходят до фронта.
    """
    # Кандидаты Stage 2 переиспользуются как есть: enum категории и диапазон
    # процента уже проверены Stage 2, а при сборке/загрузке blueprint их
    # перевалидирует тот же динамический класс. step_ids=[] безопасен:
    # проверка покрытия живёт в ClassifiedProcess, а не в AutomationCandidate.
    automation_candidate_model, _ = build_stage2_models(taxonomy, step_ids=[])
    block_type_type = Literal[tuple(block_types)]

    class BlueprintStep(BaseModel):
        """Шаг в финальном blueprint — 5 полей по схеме CLAUDE.md."""

        id: str
        name: str
        input: str
        output: str
        actor: str

        @field_validator("id", "name", "input", "output", "actor")
        @classmethod
        def _clean(cls, value: str) -> str:
            return _strip_non_empty(value)

    class BlueprintNode(BaseModel):
        """Узел архитектуры в финальном blueprint — те же поля, что вычислены в Stage 3.

        step_ids связывают блок с шагами процесса, note переносит пометки
        Stage 3 (например, маркер неназванного канала входа) в финальный JSON.
        """

        id: str
        block_type: block_type_type
        step_ids: list[str] = Field(
            default_factory=list,
            description="Шаги Stage 1, которые реализует этот блок",
        )
        note: str | None = Field(
            None,
            description="Пометка узла из Stage 3 (например, про неопределённость канала входа); null у обычных узлов",
        )

        @field_validator("id")
        @classmethod
        def _clean_id(cls, value: str) -> str:
            return _strip_non_empty(value)

        @field_validator("note")
        @classmethod
        def _clean_note(cls, value: str | None) -> str | None:
            # Пустая строка и пробелы — то же, что отсутствие пометки.
            if value is None:
                return None
            return value.strip() or None

        @field_validator("step_ids")
        @classmethod
        def _clean_step_ids(cls, values: list[str]) -> list[str]:
            cleaned: list[str] = []
            for value in values:
                step_id = _strip_non_empty(value)
                if step_id not in cleaned:
                    cleaned.append(step_id)
            return cleaned

    class BlueprintEdge(BaseModel):
        """Направленное ребро потока данных (тот же паттерн, что в Stage 3)."""

        from_: str = Field(..., alias="from", description="id блока-источника")
        to: str = Field(..., description="id блока-приёмника")

        @field_validator("from_", "to")
        @classmethod
        def _clean(cls, value: str) -> str:
            return _strip_non_empty(value)

    class BlueprintArchitecture(BaseModel):
        nodes: list[BlueprintNode]
        edges: list[BlueprintEdge]

        @field_validator("nodes")
        @classmethod
        def _non_empty_nodes(cls, value: list[BlueprintNode]) -> list[BlueprintNode]:
            if not value:
                raise ValueError("архитектура blueprint без блоков")
            return value

        @model_validator(mode="after")
        def _check_edge_endpoints(self) -> "BlueprintArchitecture":
            # Дешёвая защита рендера диаграмм: висячее ребро уронит Mermaid.
            node_ids = {node.id for node in self.nodes}
            for edge in self.edges:
                dangling = [end for end in (edge.from_, edge.to) if end not in node_ids]
                if dangling:
                    raise ValueError(
                        f"ребро {edge.from_} → {edge.to} ссылается на несуществующие блоки: {dangling}"
                    )
            return self

    class Blueprint(BaseModel):
        """Финальный Automation Blueprint — сборка выходов Stage 1–4 по схеме CLAUDE.md."""

        process: str = Field(..., description="Исходное текстовое описание процесса")
        actors: list[str]
        steps: list[BlueprintStep]
        automation_candidates: list[automation_candidate_model]
        architecture: BlueprintArchitecture
        human_in_the_loop: list[str] = Field(
            ...,
            description="Шаги, остающиеся за человеком (собирается кодом из Stage 3)",
        )
        integrations: list[str] = Field(
            ...,
            description="Внешние системы, с которыми интегрируется архитектура (кодом из Stage 3)",
        )
        risks: list[str]
        mvp_scope: MvpScope
        estimated_effort: dict[str, str]

        @field_validator("process")
        @classmethod
        def _clean_process(cls, value: str) -> str:
            return _strip_non_empty(value)

        @field_validator("automation_candidates")
        @classmethod
        def _non_empty_candidates(cls, value: list) -> list:
            if not value:
                raise ValueError("blueprint без automation_candidates")
            return value

        @field_validator("risks")
        @classmethod
        def _non_empty_risks(cls, value: list[str]) -> list[str]:
            if not value:
                raise ValueError("blueprint без risks")
            return value

        @field_validator("integrations")
        @classmethod
        def _non_empty_integrations(cls, value: list[str]) -> list[str]:
            if not value:
                raise ValueError(
                    "integrations пуст — типы блоков каталога не отображены в интеграции"
                )
            return value

        @model_validator(mode="after")
        def _check_step_references(self) -> "Blueprint":
            # Шаги Stage 1 — якорь для всех ссылок: human_in_the_loop и step_ids
            # узлов архитектуры обязаны указывать на реальные шаги.
            step_ids = {step.id for step in self.steps}
            unknown_hitl = sorted(set(self.human_in_the_loop) - step_ids)
            if unknown_hitl:
                raise ValueError(
                    f"human_in_the_loop ссылается на неизвестные шаги: {unknown_hitl}"
                )
            for node in self.architecture.nodes:
                unknown_node = sorted(set(node.step_ids) - step_ids)
                if unknown_node:
                    raise ValueError(
                        f"блок '{node.id}' привязан к шагам, которых нет в Stage 1: {unknown_node}"
                    )
            return self

    return Blueprint