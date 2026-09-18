"""FastAPI-эндпоинт пайплайна: POST /blueprint.

Принимает свободный текст описания бизнес-процесса, последовательно прогоняет
Stage 1 → 2 → 3 → 4 и возвращает финальный Automation Blueprint (JSON по схеме
CLAUDE.md). Ошибки стадий отдаются наружу понятным JSON с указанием стадии и
причины; сырой traceback наружу не пробрасывается (полный стек — только в лог).
"""

import json
import logging
import re
import time
from collections import defaultdict, deque
from pathlib import Path

import openai
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ValidationError, field_validator

from app.config_loader import load_block_catalog, load_taxonomy
from app.llm_client import LLMSettingsError
from app.pipeline.errors import PipelineStageError
from app.pipeline.stage1_extractor import extract_process
from app.pipeline.stage2_classifier import classify_steps
from app.pipeline.stage3_architect import compose_architecture
from app.pipeline.stage4_packager import package_blueprint

# INFO виден в консоли/контейнере; httpx глушим до WARNING — без спама
# от каждого HTTP-запроса клиента LLM.
logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Простая защита от чрезмерно длинных/дорогих запросов.
MAX_DESCRIPTION_CHARS = 3000

# Статика фронта и канонические примеры для GET /examples*.
STATIC_DIR = Path(__file__).resolve().parent / "static"
BLUEPRINTS_DIR = Path(__file__).resolve().parent.parent / "examples" / "blueprints"

app = FastAPI(
    title="AI Automation Architect",
    description="Свободный текст бизнес-процесса → Automation Blueprint (JSON)",
)


class BlueprintRequest(BaseModel):
    """Тело запроса POST /blueprint."""

    description: str

    @field_validator("description")
    @classmethod
    def _clean(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("description не должен быть пустым")
        if len(cleaned) > MAX_DESCRIPTION_CHARS:
            raise ValueError(
                f"description слишком длинный: {len(cleaned)} символов "
                f"(лимит {MAX_DESCRIPTION_CHARS})"
            )
        return cleaned


class _RetryCounter(logging.Handler):
    """Считает ретраи стадий по их WARNING-записям, не меняя код стадий.

    Стадии логируют «попытка X/3 — выход не прошёл валидацию» перед retry;
    handler вешается на logger "app.pipeline" на время одного запроса и
    подсчитывает такие записи. Одновременные запросы смешали бы счётчики —
    для демо-нагрузки единого эндпоинта приемлемо.
    """

    def __init__(self) -> None:
        super().__init__()
        self.count = 0

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno >= logging.WARNING and "не прошёл валидацию" in record.getMessage():
            self.count += 1


# Простой in-memory rate-limit POST /blueprint: не более RATE_LIMIT_MAX запросов
# за скользящее окно RATE_LIMIT_WINDOW_S с одного IP. Один VPS-инстанс —
# распределённые хранилища (Redis) не нужны; при рестарте процесса счётчики
# обнуляются, для демо-нагрузки приемлемо. GET /config/* и прочие GET-эндпоинты
# не ограничиваются — это статичные конфиги, не LLM-вызовы.
RATE_LIMIT_MAX = 5
RATE_LIMIT_WINDOW_S = 24 * 60 * 60
_rate_limit_hits: dict[str, deque[float]] = defaultdict(deque)


def _client_ip(request: Request) -> str:
    """IP клиента: за reverse-proxy (nginx на VPS) — из X-Forwarded-For, иначе прямое соединение.

    Спуфинг XFF при прямом доступе обошёл бы лимит — но лимит защищает бюджет
    демо, а не является границей безопасности; для демо-нагрузки приемлемо.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() or (request.client.host if request.client else "unknown")


def _check_rate_limit(ip: str, now: float) -> float | None:
    """Скользящее окно: не более RATE_LIMIT_MAX запросов с одного IP.

    Возвращает None и фиксирует запрос, если лимит не исчерпан; иначе —
    число секунд до освобождения слота (запрос не учитывается). Проверка
    синхронна внутри async-обработчика, гонок в событийном цикле нет.
    """
    hits = _rate_limit_hits[ip]
    while hits and now - hits[0] >= RATE_LIMIT_WINDOW_S:
        hits.popleft()
    if len(hits) >= RATE_LIMIT_MAX:
        return RATE_LIMIT_WINDOW_S - (now - hits[0])
    hits.append(now)
    return None


async def _run_pipeline(description: str) -> dict:
    """Полная цепочка Stage 1→2→3→4; возвращает blueprint как JSON-совместимый dict.

    Логирует длительность каждой стадии и число ретраев на стадии.
    """
    taxonomy = load_taxonomy()
    catalog = load_block_catalog()

    counter = _RetryCounter()
    pipeline_logger = logging.getLogger("app.pipeline")
    pipeline_logger.addHandler(counter)
    timings: dict[str, float] = {}
    stage_retries: dict[str, int] = {}
    prev_count = 0
    total_start = time.perf_counter()

    async def run_stage(name: str, stage_coro):
        nonlocal prev_count
        start = time.perf_counter()
        result = await stage_coro
        timings[name] = round(time.perf_counter() - start, 2)
        stage_retries[name] = counter.count - prev_count
        prev_count = counter.count
        logger.info(
            "Пайплайн: %s — %.2f с (ретраев на стадии: %d)",
            name, timings[name], stage_retries[name],
        )
        return result

    try:
        stage1 = await run_stage("stage1", extract_process(description))
        stage2 = await run_stage("stage2", classify_steps(stage1, taxonomy))
        stage3 = await run_stage("stage3", compose_architecture(stage1, stage2, catalog))
        blueprint = await run_stage(
            "stage4",
            package_blueprint(description, stage1, stage2, stage3, taxonomy, catalog),
        )
    finally:
        pipeline_logger.removeHandler(counter)

    total_s = round(time.perf_counter() - total_start, 2)
    logger.info(
        "Пайплайн завершён: %.2f с суммарно; ретраев всего: %d "
        "(stage1: %d, stage2: %d, stage3: %d, stage4: %d)",
        total_s, counter.count,
        stage_retries.get("stage1", 0), stage_retries.get("stage2", 0),
        stage_retries.get("stage3", 0), stage_retries.get("stage4", 0),
    )
    return blueprint.model_dump(by_alias=True)


@app.post("/blueprint")
async def create_blueprint(request: BlueprintRequest, http_request: Request) -> dict:
    """Свободный текст бизнес-процесса → финальный Automation Blueprint."""
    ip = _client_ip(http_request)
    retry_after = _check_rate_limit(ip, time.time())
    if retry_after is not None:
        # Ожидаемая ситуация демо-режима (защита бюджета LLM), не сбой — WARNING.
        logger.warning("Пайплайн: rate-limit исчерпан (IP %s) — повтор через %.0f с", ip, retry_after)
        raise HTTPException(
            status_code=429,
            detail={"error": "Слишком много запросов, попробуйте позже", "kind": "rate_limited"},
            headers={"Retry-After": str(int(retry_after) + 1)},
        )
    try:
        return await _run_pipeline(request.description)
    except PipelineStageError as exc:
        # Сообщение PipelineStageError содержит "Stage N: ..."; причина —
        # последняя ошибка валидации в __cause__. Наружу — понятный JSON,
        # сырой traceback не пробрасывается.
        match = re.search(r"Stage (\d+)", str(exc))
        stage = f"Stage {match.group(1)}" if match else "неизвестная стадия"
        cause = exc.__cause__
        logger.error("Пайплайн: %s — %s", stage, exc)
        raise HTTPException(
            status_code=502,
            detail={
                "error": f"{stage}: не удалось получить валидный выход — исчерпаны ретраи",
                "stage": stage,
                "last_validation_error": (str(cause) if cause is not None else str(exc))[:1000],
            },
        ) from exc
    except openai.APIStatusError as exc:
        # ProxyAPI при исчерпании средств/бюджета отвечает HTTP 402 с телом
        # {"detail": "Insufficient balance to run this request."} (варианты:
        # "API Key budget exceeded.", "Monthly budget exceeded."). У 402 в
        # openai-SDK нет собственного класса исключения — доходит до базового
        # APIStatusError, поэтому проверяем status_code. Семантически это 503:
        # сервис недоступен по внешней причине, а не сломан (502).
        if exc.status_code == 402:
            # Ожидаемое событие демо-режима: WARNING, не ERROR — не должно
            # попадать в те же алерты, что настоящие сбои.
            logger.warning("Пайплайн: ProxyAPI — отклонено по лимиту средств (402): %s", str(exc)[:300])
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "Демо временно недоступно — исчерпан лимит ProxyAPI, попробуйте позже",
                    "kind": "proxyapi_insufficient_funds",
                },
            ) from exc
        logger.error("Пайплайн: ошибка LLM API (HTTP %d): %s", exc.status_code, str(exc)[:300])
        raise HTTPException(
            status_code=502,
            detail={"error": "ошибка обращения к LLM API", "detail": str(exc)[:300]},
        ) from exc
    except openai.APIError as exc:
        logger.error("Пайплайн: ошибка LLM API: %s", exc)
        raise HTTPException(
            status_code=502,
            detail={"error": "ошибка обращения к LLM API", "detail": str(exc)[:300]},
        ) from exc
    except LLMSettingsError as exc:
        logger.error("Пайплайн: конфигурация LLM неполна: %s", exc)
        raise HTTPException(status_code=500, detail={"error": str(exc)}) from exc
    except ValidationError as exc:
        # Сборка blueprint из уже провалидированных выходов стадий —
        # внутренняя ошибка кода/конфига, а не входа пользователя.
        logger.exception("Пайплайн: сборка blueprint не прошла валидацию схемы")
        raise HTTPException(
            status_code=500,
            detail={"error": "внутренняя ошибка сборки blueprint", "detail": str(exc)[:1000]},
        ) from exc
    except ValueError as exc:
        logger.error("Пайплайн: нарушение инвариантов входов стадий: %s", exc)
        raise HTTPException(status_code=500, detail={"error": str(exc)}) from exc
    except Exception:
        logger.exception("Пайплайн: непредвиденная ошибка")
        raise HTTPException(status_code=500, detail={"error": "внутренняя ошибка пайплайна"})


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    """Фронт: одна страница — форма ввода + рендер Blueprint."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/examples")
async def list_examples() -> dict:
    """Имена готовых кейсов из examples/blueprints/ — рендер без вызова LLM."""
    return {"examples": sorted(path.stem for path in BLUEPRINTS_DIR.glob("*.json"))}


@app.get("/examples/{name}")
async def get_example(name: str) -> dict:
    """Финальный Blueprint готового кейса (без вызова LLM)."""
    path = BLUEPRINTS_DIR / f"{name}.json"
    if not path.is_file():
        raise HTTPException(status_code=404, detail={"error": f"пример '{name}' не найден"})
    return json.loads(path.read_text(encoding="utf-8-sig"))


@app.get("/config/blocks")
async def get_block_labels() -> dict:
    """label'ы блоков каталога — подписи узлов architecture-диаграммы на фронте."""
    return {block.name: block.label for block in load_block_catalog()}


@app.get("/config/taxonomy")
async def get_category_labels() -> dict:
    """label'ы категорий таксономии — человекочитаемые подписи карточек Stage 2 на фронте."""
    return {spec.name: spec.label for spec in load_taxonomy()}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)