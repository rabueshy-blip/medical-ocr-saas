"""
التقطيع (Chunking) قبل DSPy — راجع plan.md القسم 5.

القواعد:
- التقطيع حسب الوحدة الدلالية (Block)، وليس حسب عدد رموز ثابت.
- كل جدول = chunk واحد كامل (لا يُفصل الرأس عن الصفوف).
- كل فقرة/عنوان = chunk، مع نافذة تداخل (overlap) صغيرة بين الفقرات المتتالية.
- بيانات وصفية (رقم الصفحة، confidence، source_engine) تُرفق كسياق يُمرَّر
  لموديول DSPy، وليس كنص يُصحَّح — لذلك `context_before` هنا حقل ميتاداتا منفصل
  عن `content` ولا يُدمج فيه أبداً.
"""

from __future__ import annotations

from typing import List, Optional, Union

from pydantic import BaseModel, Field

from .schema import Block, BlockType, Document, SourceEngine


class Chunk(BaseModel):
    chunk_id: str
    page_number: int
    block_type: BlockType
    content: Union[str, List[List[str]]]
    confidence: float
    source_engine: SourceEngine
    context_before: Optional[str] = Field(
        default=None,
        description=(
            "نافذة تداخل صغيرة من نهاية نص الـ chunk النصي السابق. سياق ميتاداتا "
            "فقط يُمرَّر لموديول DSPy للاسترشاد به، وليس جزءاً من النص المراد تصحيحه."
        ),
    )


def _chunk_id(document: Document, page_number: int, block_index: int) -> str:
    return f"{document.file_name}:p{page_number}:b{block_index}"


def chunk_document(document: Document, overlap_chars: int = 80) -> List[Chunk]:
    """يحوّل مستنداً كاملاً إلى قائمة Chunks جاهزة للتمرير لموديولات DSPy."""
    chunks: List[Chunk] = []
    previous_text_tail: Optional[str] = None

    for page in document.pages:
        for block_index, block in enumerate(page.blocks):
            chunk_id = _chunk_id(document, page.page_number, block_index)

            if block.block_type == BlockType.TABLE:
                chunks.append(
                    Chunk(
                        chunk_id=chunk_id,
                        page_number=page.page_number,
                        block_type=block.block_type,
                        content=block.rows or [],
                        confidence=block.confidence,
                        source_engine=block.source_engine,
                        context_before=None,
                    )
                )
                # لا نمرر سياقاً نصياً عبر جدول كامل بين فقرتين غير متجاورتين فعلياً.
                previous_text_tail = None
                continue

            text = block.text or ""
            chunks.append(
                Chunk(
                    chunk_id=chunk_id,
                    page_number=page.page_number,
                    block_type=block.block_type,
                    content=text,
                    confidence=block.confidence,
                    source_engine=block.source_engine,
                    context_before=previous_text_tail,
                )
            )
            previous_text_tail = text[-overlap_chars:] if text else None

    return chunks
