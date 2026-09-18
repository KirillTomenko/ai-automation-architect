"""E2E-тест единого эндпоинта: POST /blueprint через HTTP-слой (TestClient).

Первая проверка, что стадии стыкуются как единое целое через сетевой слой,
а не только в изолированных вызовах по стадиям (tests/test_pipeline.py).

Ассертов намеренно нет (конвенция проекта) в LLM-тестах: тест печатает
фактический результат и валидирует ответ финальной моделью Blueprint —
смотрим глазами. Исключение — детерминированные тесты обработчиков ошибок
(ProxyAPI 402, rate-limit): они без LLM (monkeypatch), поэтому с ассертами.

Прогоны идут на живом LLM (~4 вызова на пример при ретраях). Конфигурация
Stage 1 задаётся переопределением переменных окружения ДО load_dotenv (который
не перетирает уже установленные переменные) — как в раннерах. В .env ключей
LLM_MODEL_STAGE1/LLM_TEMPERATURE_STAGE1 нет, поэтому каноническая конфигурация
(gpt-4.1 / t=0.1) фиксируется здесь.

Запуск из корня проекта:
    python -m pytest -s tests/test_api.py
"""

import json
import logging
import os
from pathlib import Path

# Каноническая конфигурация Stage 1 (см. CLAUDE.md) — до любых импортов app.
os.environ["LLM_MODEL_STAGE1"] = "gpt-4.1"
os.environ["LLM_TEMPERATURE_STAGE1"] = "0.1"

import httpx  # noqa: E402
import openai  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from app import main as app_main  # noqa: E402
from app.config_loader import load_block_catalog, load_taxonomy  # noqa: E402
from app.main import app  # noqa: E402
from app.schemas import build_blueprint_model  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_KEYS = [
    "process", "actors", "steps", "automation_candidates", "architecture",
    "human_in_the_loop", "integrations", "risks", "mvp_scope", "estimated_effort",
]

client = TestClient(app)


def _post_and_check(name: str) -> None:
    """POST текста примера и структурная проверка ответа (print-only)."""
    description = (PROJECT_ROOT / "examples" / "raw" / f"{name}.txt").read_text(
        encoding="utf-8-sig"
    ).strip()

    response = client.post("/blueprint", json={"description": description})

    print(f"\n===== POST /blueprint: {name} =====")
    print(f"HTTP status: {response.status_code}")
    if response.status_code != 200:
        print(f"FAIL — тело ответа:\n{response.text}")
        return

    payload = response.json()
    keys_actual = list(payload.keys())
    keys_ok = keys_actual == EXPECTED_KEYS
    print(f"ключи верхнего уровня: {'PASS' if keys_ok else 'FAIL — ' + str(keys_actual)}")

    # Перевалидация ответа финальной моделью Blueprint: enum категорий/блоков,
    # диапазоны % из taxonomy, ссылки human_in_the_loop и step_ids узлов,
    # целостность рёбер архитектуры.
    blueprint_model = build_blueprint_model(
        taxonomy={c.name: (c.range_min, c.range_max) for c in load_taxonomy()},
        block_types=[block.name for block in load_block_catalog()],
    )
    try:
        blueprint_model.model_validate(payload)
        print("валидация Blueprint-моделью: PASS")
    except ValidationError as exc:
        print(f"валидация Blueprint-моделью: FAIL\n{exc}")

    print("финальный JSON:")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def test_post_blueprint_example_1() -> None:
    _post_and_check("example_1")


def test_post_blueprint_example_2_ambiguous() -> None:
    _post_and_check("example_2_ambiguous")


def test_post_blueprint_rejects_bad_input() -> None:
    """Защита входа (без LLM): пустой и чрезмерно длинный description → 422."""
    empty = client.post("/blueprint", json={"description": "   "})
    print("\n===== POST /blueprint: пустой description =====")
    print(f"HTTP status: {empty.status_code} (ожидается 422)")
    print(f"тело: {empty.text}")

    long_text = "заявка в Telegram, менеджер переносит в таблицу. " * 70  # > 3000 символов
    overlong = client.post("/blueprint", json={"description": long_text})
    print(f"\n===== POST /blueprint: description из {len(long_text)} символов =====")
    print(f"HTTP status: {overlong.status_code} (ожидается 422)")
    print(f"тело: {overlong.text}")


# --- Детерминированные тесты обработчиков ошибок (без LLM, с ассертами) ---

PROXYAPI_CHAT_URL = "https://api.proxyapi.ru/openai/v1/chat/completions"


def _proxyapi_402() -> openai.APIStatusError:
    """402 от ProxyAPI ровно в том виде, в каком его создаёт openai-SDK:
    статус 402 без собственного класса исключения → базовый APIStatusError,
    тело ProxyAPI — {"detail": "Insufficient balance to run this request."}."""
    body = {"detail": "Insufficient balance to run this request."}
    return openai.APIStatusError(
        f"Error code: 402 - {body}",
        response=httpx.Response(402, json=body, request=httpx.Request("POST", PROXYAPI_CHAT_URL)),
        body=body,
    )


def test_post_blueprint_proxyapi_402_returns_503(monkeypatch, caplog) -> None:
    """ProxyAPI 402 (средства/бюджет) → 503 с kind=proxyapi_insufficient_funds,
    а не общий 502; в логе — WARNING (ожидаемое событие демо), не ERROR."""
    app_main._rate_limit_hits.clear()

    async def raise_402(description: str):
        raise _proxyapi_402()

    monkeypatch.setattr("app.main.extract_process", raise_402)
    with caplog.at_level(logging.WARNING, logger="app.main"):
        resp = client.post("/blueprint", json={"description": "Клиенты оставляют заявки, менеджер их разбирает."})

    print("\n===== POST /blueprint: ProxyAPI 402 =====")
    print(f"HTTP status: {resp.status_code} (ожидается 503)")
    print(f"тело: {resp.text}")
    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert detail["kind"] == "proxyapi_insufficient_funds"
    assert "исчерпан лимит ProxyAPI" in detail["error"]
    warnings_402 = [r for r in caplog.records if "402" in r.getMessage()]
    assert warnings_402 and warnings_402[0].levelno == logging.WARNING


def test_post_blueprint_other_api_status_returns_502(monkeypatch) -> None:
    """Прочие статусные ошибки LLM API — прежний путь: 502, общий текст."""
    app_main._rate_limit_hits.clear()
    body = {"error": {"message": "upstream oops", "type": "server_error", "code": None}}

    async def raise_500(description: str):
        raise openai.InternalServerError(
            f"Error code: 500 - {body}",
            response=httpx.Response(500, json=body, request=httpx.Request("POST", PROXYAPI_CHAT_URL)),
            body=body,
        )

    monkeypatch.setattr("app.main.extract_process", raise_500)
    resp = client.post("/blueprint", json={"description": "Клиенты оставляют заявки, менеджер их разбирает."})

    print("\n===== POST /blueprint: прочая ошибка LLM API (500) =====")
    print(f"HTTP status: {resp.status_code} (ожидается 502)")
    print(f"тело: {resp.text}")
    assert resp.status_code == 502
    assert resp.json()["detail"]["error"] == "ошибка обращения к LLM API"


def test_post_blueprint_rate_limit_daily_per_ip(monkeypatch) -> None:
    """Не более 5 запросов в сутки с одного IP: 6-й → 429; XFF изолирует IP."""
    app_main._rate_limit_hits.clear()

    async def raise_402(description: str):
        raise _proxyapi_402()  # 503 вместо реального LLM — лимитер проверяем на этом

    monkeypatch.setattr("app.main.extract_process", raise_402)

    statuses = [
        client.post("/blueprint", json={"description": "Процесс: приём заявок и ответ на вопросы."}).status_code
        for _ in range(5)
    ]
    print(f"\n===== rate-limit: статусы первых 5 запросов =====\n{statuses}")
    assert statuses == [503] * 5  # лимит не мешает: все пять прошли до обработчика ошибок

    sixth = client.post("/blueprint", json={"description": "Процесс: приём заявок и ответ на вопросы."})
    print(f"6-й запрос: {sixth.status_code} (ожидается 429), тело: {sixth.text}")
    assert sixth.status_code == 429
    detail = sixth.json()["detail"]
    assert detail["kind"] == "rate_limited"
    assert detail["error"] == "Слишком много запросов, попробуйте позже"
    assert int(sixth.headers["Retry-After"]) > 0

    other_ip = client.post(
        "/blueprint",
        json={"description": "Процесс: приём заявок и ответ на вопросы."},
        headers={"X-Forwarded-For": "203.0.113.7"},
    )
    print(f"запрос с другого IP (XFF): {other_ip.status_code} (ожидается 503 — свой лимит)")
    assert other_ip.status_code == 503