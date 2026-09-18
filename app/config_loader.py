"""Загрузка редактируемых конфигов пайплайна из config/*.yaml.

Файлы читаются с encoding='utf-8-sig': Windows-инструменты часто добавляют BOM
(см. CLAUDE.md), 'utf-8' без -sig портит первый символ.
"""

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"


class CategorySpec(BaseModel):
    """Категория автоматизации из config/taxonomy.yaml."""

    name: str = Field(..., description="Машинное имя категории (для enum в Stage 2)")
    label: str = Field(..., description="Человекочитаемое название (в промпт Stage 2)")
    range_min: int = Field(..., ge=0, le=100)
    range_max: int = Field(..., ge=0, le=100)

    @field_validator("name", "label")
    @classmethod
    def _clean(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("строка не должна быть пустой")
        return cleaned

    @model_validator(mode="after")
    def _check_range(self) -> "CategorySpec":
        if self.range_min > self.range_max:
            raise ValueError(
                f"диапазон категории '{self.name}' задан наоборот: "
                f"[{self.range_min}, {self.range_max}]"
            )
        return self


def load_taxonomy(path: Path | None = None) -> list[CategorySpec]:
    """Читает config/taxonomy.yaml; структура валидируется схемой CategorySpec."""
    taxonomy_path = path or (CONFIG_DIR / "taxonomy.yaml")

    data = yaml.safe_load(taxonomy_path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or not isinstance(data.get("categories"), list):
        raise ValueError(f"{taxonomy_path}: ожидался ключ 'categories' со списком")

    specs: list[CategorySpec] = []
    for index, item in enumerate(data["categories"]):
        try:
            specs.append(CategorySpec(
                name=item["name"],
                label=item["label"],
                range_min=item["range"][0],
                range_max=item["range"][1],
            ))
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError(f"{taxonomy_path}: некорректная категория #{index}: {exc}") from exc
        except Exception as exc:
            raise ValueError(f"{taxonomy_path}: некорректная категория #{index}: {exc}") from exc

    names = [spec.name for spec in specs]
    if len(names) != len(set(names)):
        raise ValueError(f"{taxonomy_path}: дублирующиеся имена категорий: {names}")

    return specs


class BlockSpec(BaseModel):
    """Блок архитектуры из config/block_catalog.yaml."""

    name: str = Field(..., description="Машинное имя блока (для enum в Stage 3)")
    label: str = Field(..., description="Человекочитаемое название (в промпт Stage 3)")

    @field_validator("name", "label")
    @classmethod
    def _clean(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("строка не должна быть пустой")
        return cleaned


def load_block_catalog(path: Path | None = None) -> list[BlockSpec]:
    """Читает config/block_catalog.yaml; структура валидируется схемой BlockSpec."""
    catalog_path = path or (CONFIG_DIR / "block_catalog.yaml")

    data = yaml.safe_load(catalog_path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or not isinstance(data.get("blocks"), list):
        raise ValueError(f"{catalog_path}: ожидался ключ 'blocks' со списком")

    specs: list[BlockSpec] = []
    for index, item in enumerate(data["blocks"]):
        try:
            specs.append(BlockSpec(name=item["name"], label=item["label"]))
        except Exception as exc:
            raise ValueError(f"{catalog_path}: некорректный блок #{index}: {exc}") from exc

    names = [spec.name for spec in specs]
    if len(names) != len(set(names)):
        raise ValueError(f"{catalog_path}: дублирующиеся имена блоков: {names}")

    return specs