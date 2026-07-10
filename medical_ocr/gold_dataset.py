"""
اليوم الخامس (plan.md): مخطط ومحمّل Gold Dataset — 15 عينة طبية "صعبة" منسّقة
(10 حالات مصطلحات + 5 حالات جداول) مع مخرجات مرجعية (expected_*) تُستخدم لقياس
دقة موديولات DSPy كمّياً (scripts/evaluate_gold.py) ولتزويد مُحسِّن DSPy بأمثلة
تدريب حقيقية (scripts/optimize_modules.py)، بدل الاكتفاء بالمراجعة اليدوية لحقل
reasoning كما في اليوم الرابع (scripts/run_hard_cases.py + medical_ocr/eval_cases.py).

الفرق عن medical_ocr/eval_cases.py: تلك الحالات لا تحمل مخرجات مرجعية (raw + note
فقط للمراجعة اليدوية)، بينما هنا كل عينة تحمل expected_* تُقارَن برمجياً مع مخرجات
الموديول عبر medical_ocr/gold_metrics.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List

import dspy
from pydantic import BaseModel, Field

GOLD_DIR = Path(__file__).resolve().parent.parent / "data" / "gold"


class GoldTerminologyExample(BaseModel):
    id: str
    raw_text: str
    expected_correction_terms: List[str] = Field(default_factory=list)
    forbidden_terms: List[str] = Field(default_factory=list)
    expected_uncertain_terms: List[str] = Field(default_factory=list)
    difficulty: str = "medium"
    tags: List[str] = Field(default_factory=list)
    note: str = ""


class GoldTableExample(BaseModel):
    id: str
    raw_rows: List[List[str]]
    column_hints: List[str] = Field(default_factory=list)
    expected_row_values: Dict[str, List[str]] = Field(default_factory=dict)
    expected_uncertain_row_indices: List[int] = Field(default_factory=list)
    difficulty: str = "medium"
    tags: List[str] = Field(default_factory=list)
    note: str = ""


class GoldDataset(BaseModel):
    terminology: List[GoldTerminologyExample]
    tables: List[GoldTableExample]

    @property
    def size(self) -> int:
        return len(self.terminology) + len(self.tables)


def load_gold_dataset(gold_dir: Path = GOLD_DIR) -> GoldDataset:
    terminology_raw = json.loads((gold_dir / "terminology.json").read_text(encoding="utf-8"))
    tables_raw = json.loads((gold_dir / "tables.json").read_text(encoding="utf-8"))
    return GoldDataset(
        terminology=[GoldTerminologyExample(**item) for item in terminology_raw],
        tables=[GoldTableExample(**item) for item in tables_raw],
    )


# --------------------------------------------------------------------------
# تحويل إلى dspy.Example: يُستخدم من scripts/evaluate_gold.py (عبر dspy.Evaluate)
# وscripts/optimize_modules.py (عبر dspy.teleprompt.BootstrapFewShot) معاً، لتفادي
# ازدواج منطق التحويل. حقول expected_* تبقى مرفقة بالمثال (غير مُعلَّمة كـ input)
# ليستخدمها medical_ocr.gold_metrics عند حساب الدرجة، دون أن تُمرَّر إلى الموديول.
# --------------------------------------------------------------------------


def terminology_example_to_dspy(example: GoldTerminologyExample) -> dspy.Example:
    return dspy.Example(
        raw_text=example.raw_text,
        expected_correction_terms=example.expected_correction_terms,
        forbidden_terms=example.forbidden_terms,
        expected_uncertain_terms=example.expected_uncertain_terms,
    ).with_inputs("raw_text")


def table_example_to_dspy(example: GoldTableExample) -> dspy.Example:
    return dspy.Example(
        raw_rows=example.raw_rows,
        column_hints=example.column_hints,
        expected_row_values=example.expected_row_values,
        expected_uncertain_row_indices=example.expected_uncertain_row_indices,
    ).with_inputs("raw_rows", "column_hints")


def build_terminology_devset(gold: GoldDataset) -> List[dspy.Example]:
    return [terminology_example_to_dspy(example) for example in gold.terminology]


def build_table_devset(gold: GoldDataset) -> List[dspy.Example]:
    return [table_example_to_dspy(example) for example in gold.tables]
