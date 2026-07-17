"""
Signature ثالث: تصنيف كل Block مستخرَج (فقرة أو جدول) إلى فئة دلالية واحدة —
معلومات مريض / نتائج سريرية / ملاحظات طبيب / غير مصنَّف — لدعم واجهة مبوَّبة
وتصدير تقارير منظَّمة حسب الفئة (Word/Excel).

على عكس التصحيح الإملائي وهيكلة الجداول، هذا الموديول لا يُعيد كتابة أي محتوى —
فقط يُلصق تصنيفاً. لذلك بوابة الترسيخ هنا (`is_classification_valid`) بنيوية بحتة
وليست تشابهاً نصياً: التحقّق أن كل index في page_blocks غُطِّي تماماً مرة واحدة
بلا حذف أو تكرار أو رقم خارج النطاق، وأن كل category من القيم الأربع المسموحة.

**تنبيه مهم (تحقّق من مصدر dspy 2.6.27 المثبَّت هنا):** `dspy.Refine` لا يرفع
استثناءً إن لم تصل أي محاولة threshold — تُعيد ببساطة أفضل محاولة (best-of-N)
حتى لو ظلّت غير صالحة بنيوياً. لذلك `apply_classification_to_page` تُعيد التحقق
من صحة الناتج الفعلي *قبل* أي تعديل على `Block.category`، وتتراجع إلى تصنيف
`OTHER` لكامل الصفحة عند الفشل بدل استثناء IndexError/KeyError غير مُلتقَط.

هذا الموديول يُستدعى مرة واحدة **لكل صفحة** (وليس لكل Block) عبر زر "تصنيف
المستند" الصريح في واجهة Streamlit — قرار مقصود لحماية حصة Gemini المجانية
(نفس فلسفة الأزرار المنفصلة للتصحيح الإملائي/هيكلة الجداول)، وليس تلقائياً عند
كل رفع ملف.
"""

from __future__ import annotations

import json
from typing import List, Optional

import dspy

from ..schema import BlockCategory, BlockType, Page

MAX_PREVIEW_CHARS = 400
MAX_TABLE_PREVIEW_ROWS = 6

_BLOCK_CATEGORY_VALUES = {category.value for category in BlockCategory}


class MedicalBlockClassification(dspy.Signature):
    """صنّف كل مقطع (Block) مستخرَج من صفحة مستند طبي إلى واحدة من أربع فئات دلالية.

    الفئات:
    - patient_info: معلومات تعريفية عن المريض (اسم، عمر، رقم ملف، تاريخ ميلاد...).
    - clinical_results: نتائج ومعطيات سريرية/مخبرية (قيم فحوصات، جرعات أدوية، تشخيص).
      الجداول التي تحوي بيانات رقمية/مخبرية تُصنَّف هنا عادةً، ما لم يكن الجدول
      بيانات ديموغرافية بحتة (وحينها تُصنَّف patient_info) — هذا استرشاد وليس
      قاعدة صارمة مبنية على block_type وحدها.
    - doctor_notes: ملاحظات وتعليقات نصية حرة من الطبيب (خطة علاج، توصيات، ملاحظات سريرية حرة).
    - other: أي محتوى لا يتّضح انتماؤه بثقة لأيٍّ من الفئات الثلاث أعلاه — لا تخمّن.

    قواعد صارمة:
    - صنّف كل عنصر في page_blocks تماماً مرة واحدة، بنفس index المُعطى، بلا حذف أو تكرار.
    - هذه فئة تصنيف فقط — ممنوع تغيير أو إضافة أي نص/محتوى.
    """

    page_blocks: str = dspy.InputField(
        desc="JSON: قائمة {index, block_type, preview} لكل مقطع في الصفحة بترتيبه الأصلي"
    )
    classifications: str = dspy.OutputField(
        desc="JSON: قائمة {index, category} تغطي كل index في page_blocks تماماً مرة واحدة؛ "
        "category واحدة من: patient_info, clinical_results, doctor_notes, other"
    )


def _truncate(text: str, limit: int = MAX_PREVIEW_CHARS) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _flatten_rows_preview(rows: List, max_rows: int = MAX_TABLE_PREVIEW_ROWS) -> str:
    """يحوّل صفوف جدول (خام List[List[str]] أو مُهيكَل List[dict]) إلى نص مسطَّح
    مختصر لغرض التصنيف فقط — ليس المقصود الحفاظ على بنية الجدول هنا."""
    lines = []
    for row in rows[:max_rows]:
        if isinstance(row, dict):
            lines.append(" | ".join(str(value) for value in row.values()))
        else:
            lines.append(" | ".join(str(cell) for cell in row))
    return " || ".join(lines)


def encode_page_blocks(page_blocks: List[dict]) -> str:
    return json.dumps(page_blocks, ensure_ascii=False)


def build_page_blocks_payload(page: Page, corrections: Optional[dict] = None) -> List[dict]:
    """يبني حمولة JSON-قابلة لصفحة واحدة من Blocks الفعلية، مستخدماً نص/جدول
    مُصحَّحاً بالذكاء الاصطناعي (من قاموس `corrections` في Streamlit) إن وُجد،
    وإلا النص/الجدول الخام كما استُخرج — التصنيف يستفيد من التصحيح إن كان متاحاً
    لكنه لا يتطلبه."""
    corrections = corrections or {}
    payload: List[dict] = []
    for index, block in enumerate(page.blocks):
        block_key = f"p{page.page_number}_b{index}"
        correction = corrections.get(block_key, {})
        if block.block_type == BlockType.TABLE:
            structured_rows = correction.get("structured_rows")
            rows = structured_rows if isinstance(structured_rows, list) and structured_rows else (block.rows or [])
            preview = _flatten_rows_preview(rows)
        else:
            text = correction.get("corrected_text") or block.text or ""
            preview = _truncate(text)
        payload.append({"index": index, "block_type": block.block_type.value, "preview": preview})
    return payload


def is_classification_valid(page_blocks_json: str, classifications_json: str) -> bool:
    """بوابة ترسيخ بنيوية بحتة (لا تشابه نصي): التصنيف لا يُعيد كتابة أي محتوى،
    فالخطر الوحيد هو بنيوي — index مفقود/مكرر/خارج النطاق، أو category غير صالحة."""
    try:
        page_blocks = json.loads(page_blocks_json)
        classifications = json.loads(classifications_json)
    except (json.JSONDecodeError, TypeError):
        return False

    if not isinstance(page_blocks, list) or not isinstance(classifications, list):
        return False
    if len(classifications) != len(page_blocks):
        return False

    for item in classifications:
        if not isinstance(item, dict) or set(item.keys()) != {"index", "category"}:
            return False
        if not isinstance(item["index"], int) or not isinstance(item["category"], str):
            return False

    indices = {item["index"] for item in classifications}
    if indices != set(range(len(page_blocks))):
        return False

    return all(item["category"] in _BLOCK_CATEGORY_VALUES for item in classifications)


def classification_reward(call_kwargs: dict, prediction: dspy.Prediction) -> float:
    """reward_fn لـ dspy.Refine: 1.0 إن كان ناتج التصنيف صالحاً بنيوياً، وإلا 0.0."""
    return 1.0 if is_classification_valid(call_kwargs["page_blocks"], prediction.classifications) else 0.0


def apply_classification_to_page(page: Page, page_blocks: List[dict], prediction: dspy.Prediction) -> bool:
    """يُطبِّق ناتج التصنيف على Blocks الصفحة فعلياً (in place).

    يُعيد التحقق من الصحة البنيوية *قبل* أي تعديل — dspy.Refine قد يُعيد أفضل
    محاولة حتى لو ظلّت غير صالحة (لا ترفع استثناءً بنفسها، انظر توثيق أعلى الملف)،
    فالثقة بـ prediction.classifications دون تحقّق مستقل قد تُسبِّب KeyError عند
    الربط index -> block. عند الفشل: كل Block في الصفحة يُصنَّف OTHER صراحة
    (بدل تخمين) وتُعاد False كي يعرض المستدعي تحذيراً بدل انهيار صامت."""
    page_blocks_json = encode_page_blocks(page_blocks)
    if not is_classification_valid(page_blocks_json, prediction.classifications):
        for block in page.blocks:
            block.category = BlockCategory.OTHER
        return False

    classifications = json.loads(prediction.classifications)
    category_by_index = {item["index"]: BlockCategory(item["category"]) for item in classifications}
    for index, block in enumerate(page.blocks):
        block.category = category_by_index[index]
    return True


class MedicalBlockClassifier(dspy.Module):
    """موديول DSPy الذي يغلّف MedicalBlockClassification بترميز JSON وقيد ترسيخ
    بنيوي عبر dspy.Refine — يُستدعى مرة واحدة لكل صفحة (batched)، وليس لكل Block."""

    def __init__(self, max_attempts: int = 3):
        super().__init__()
        base = dspy.ChainOfThought(MedicalBlockClassification)
        self.classify = dspy.Refine(
            module=base,
            N=max_attempts,
            reward_fn=classification_reward,
            threshold=1.0,
        )

    def forward(self, page_blocks: List[dict]) -> dspy.Prediction:
        return self.classify(page_blocks=encode_page_blocks(page_blocks))
