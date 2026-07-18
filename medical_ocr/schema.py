"""
مخطط البيانات الموحّد (Unified Schema) — راجع plan.md القسم 4.

يحوّل المخرجات الخام لمحركات الاستخراج (PyMuPDF للـ PDF الرقمي، PaddleOCR/pdfplumber
للصفحات الممسوحة والجداول) إلى نماذج Pydantic محكمة:

    Document -> Page (رقم، مصدر: digital/scanned)
             -> Block (نوع: paragraph / table / heading)
                  - text أو rows[][] (للجداول)
                  - bbox, confidence, source_engine

`confidence` و `source_engine` ضروريان لاحقاً لإعطاء أولوية للمراجعة البشرية
في المناطق منخفضة الثقة. `raw_text`/`raw_rows` يحافظان على النص الأصلي قبل أي
تصحيح لاحق من DSPy لغرض التتبع (audit trail) — انظر القسم 6.
"""

from __future__ import annotations

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field, model_validator


class PageSource(str, Enum):
    DIGITAL = "digital"
    SCANNED = "scanned"


class BlockType(str, Enum):
    PARAGRAPH = "paragraph"
    HEADING = "heading"
    TABLE = "table"


class SourceEngine(str, Enum):
    PYMUPDF = "pymupdf"
    PDFPLUMBER = "pdfplumber"
    PADDLEOCR = "paddleocr"
    EASYOCR = "easyocr"
    GOOGLE_VISION = "google_vision"
    LLM_CORRECTED = "llm_corrected"


class BlockCategory(str, Enum):
    """تصنيف دلالي لمحتوى Block (معلومات مريض / نتائج سريرية / ملاحظات طبيب)، يُضاف
    بعد الاستخراج عبر موديول DSPy منفصل (`medical_ocr/signatures/classification.py`)
    — وليس أثناء extract_document نفسها، لأن التصنيف يستدعي LM (مكلف/محدود الحصة)
    بخلاف الاستخراج الحر. OTHER تُستخدَم لأي محتوى غامض بدل تخمين تصنيف غير موثوق،
    بنفس فلسفة UNCERTAIN في numeric_guard.py."""

    PATIENT_INFO = "patient_info"
    CLINICAL_RESULTS = "clinical_results"
    DOCTOR_NOTES = "doctor_notes"
    OTHER = "other"


class BoundingBox(BaseModel):
    x0: float
    y0: float
    x1: float
    y1: float


class Block(BaseModel):
    """وحدة دلالية واحدة داخل صفحة: فقرة/عنوان (text) أو جدول (rows)."""

    block_type: BlockType
    text: Optional[str] = None
    raw_text: Optional[str] = None
    rows: Optional[List[List[str]]] = None
    raw_rows: Optional[List[List[str]]] = None
    bbox: Optional[BoundingBox] = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    source_engine: SourceEngine
    category: Optional[BlockCategory] = None

    @model_validator(mode="after")
    def _check_content_and_seed_audit_trail(self) -> "Block":
        if self.block_type == BlockType.TABLE:
            if self.rows is None:
                raise ValueError("Block من نوع table يجب أن يحتوي rows")
            if self.raw_rows is None:
                self.raw_rows = [row[:] for row in self.rows]
        else:
            if self.text is None:
                raise ValueError(f"Block من نوع {self.block_type.value} يجب أن يحتوي text")
            if self.raw_text is None:
                self.raw_text = self.text
        return self


class Page(BaseModel):
    page_number: int = Field(ge=1)
    source: PageSource
    blocks: List[Block] = Field(default_factory=list)


class ImageAsset(BaseModel):
    """صورة/رسم بياني مُضمَّن مُستخرَج من صفحة رقمية (get_images في PyMuPDF)، لعرضه في
    مكتبة الوسائط الجانبية بالواجهة كي يسحبه المترجم يدوياً لمكانه الصحيح — الاستخراج
    مقصور على الصفحات الرقمية لأن الصفحة الممسوحة بالكامل هي نفسها صورة واحدة كبيرة
    (تُستخدم أصلاً كمدخل OCR)، وليست "شكلاً" مضمَّناً بالمعنى المقصود هنا."""

    page_number: int = Field(ge=1)
    index: int
    mime_type: str
    data_base64: str
    width: int
    height: int


class Document(BaseModel):
    file_name: str
    pages: List[Page] = Field(default_factory=list)
    images: List[ImageAsset] = Field(default_factory=list)


# --------------------------------------------------------------------------
# محوّلات (Adapters): من المخرجات الخام لمحركات الاستخراج إلى Block/Page.
# --------------------------------------------------------------------------


def block_from_pymupdf_span(span: dict, block_type: BlockType = BlockType.PARAGRAPH) -> Block:
    """يحوّل span واحد من `page.get_text('dict')['blocks'][i]['lines'][j]['spans'][k]` إلى Block.

    المسار الرقمي (القسم 3-أ): النص موثوق 99%+، فلا تدخّل لأي LLM هنا — لذلك
    confidence ثابتة عند 1.0 و source_engine = pymupdf.
    """
    x0, y0, x1, y1 = span["bbox"]
    return Block(
        block_type=block_type,
        text=span["text"],
        bbox=BoundingBox(x0=x0, y0=y0, x1=x1, y1=y1),
        confidence=1.0,
        source_engine=SourceEngine.PYMUPDF,
    )


def table_block_from_pdfplumber(
    rows: List[List[Optional[str]]], bbox: Optional[tuple] = None
) -> Block:
    """يحوّل جدولاً مستخرَجاً عبر `page.extract_table()` من pdfplumber إلى Block من نوع table."""
    normalized_rows = [[cell or "" for cell in row] for row in rows]
    block_bbox = BoundingBox(x0=bbox[0], y0=bbox[1], x1=bbox[2], y1=bbox[3]) if bbox else None
    return Block(
        block_type=BlockType.TABLE,
        rows=normalized_rows,
        bbox=block_bbox,
        confidence=1.0,
        source_engine=SourceEngine.PDFPLUMBER,
    )


def block_from_paddleocr_line(line_result: list, block_type: BlockType = BlockType.PARAGRAPH) -> Block:
    """يحوّل عنصراً واحداً من نتيجة PaddleOCR: `[[[x0,y0],[x1,y0],[x1,y1],[x0,y1]], (text, confidence)]`.

    المسار الممسوح (القسم 3-ب، الطبقة 1 — Ground Truth).
    """
    box_points, (text, confidence) = line_result
    xs = [p[0] for p in box_points]
    ys = [p[1] for p in box_points]
    return Block(
        block_type=block_type,
        text=text,
        bbox=BoundingBox(x0=min(xs), y0=min(ys), x1=max(xs), y1=max(ys)),
        confidence=float(confidence),
        source_engine=SourceEngine.PADDLEOCR,
    )
