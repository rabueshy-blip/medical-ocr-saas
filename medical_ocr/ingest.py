"""
خط الاستخراج الأول من PDF إلى Document (يُستخدم من واجهة Streamlit — plan.md لم يخصّص
له قسماً بعد لأنه بُني تلبيةً لطلب مباشر لواجهة تفاعلية، وليس كجزء من تسلسل الأيام).

نسخة مبسّطة عمداً مقارنة بمعمارية القسم 3-ب الكاملة (التحقق المزدوج PaddleOCR+easyocr
على الصفحات الممسوحة): هنا محرك OCR واحد فقط (easyocr) للصفحات الممسوحة، لأن الهدف
الحالي هو عرض تفاعلي يعمل فعلياً أمام المستخدم، لا خط الإنتاج الكامل. التحقق المزدوج
يبقى تحسيناً لاحقاً إن احتيج له.

القرار بين صفحة "رقمية" و"ممسوحة": إن استخرج PyMuPDF نصاً أطول من MIN_DIGITAL_CHARS
حرفاً تُعامَل الصفحة كرقمية (نص PyMuPDF + جداول pdfplumber، بلا أي OCR)، وإلا تُعامَل
كممسوحة وتُمرَّر لـ easyocr كصورة (raster) لكامل الصفحة.
"""

from __future__ import annotations

from typing import List, Optional

import fitz  # PyMuPDF
import pdfplumber

from .schema import Block, BlockType, BoundingBox, Document, Page, PageSource, SourceEngine

MIN_DIGITAL_CHARS = 20

_easyocr_reader = None


def _get_easyocr_reader():
    global _easyocr_reader
    if _easyocr_reader is None:
        import easyocr

        _easyocr_reader = easyocr.Reader(["ar", "en"], gpu=False)
    return _easyocr_reader


def _digital_page_blocks(page: fitz.Page) -> List[Block]:
    blocks = []
    for x0, y0, x1, y1, text, *_rest in page.get_text("blocks"):
        text = text.strip()
        if not text:
            continue
        blocks.append(
            Block(
                block_type=BlockType.PARAGRAPH,
                text=text,
                bbox=BoundingBox(x0=x0, y0=y0, x1=x1, y1=y1),
                confidence=1.0,
                source_engine=SourceEngine.PYMUPDF,
            )
        )
    return blocks


def _table_blocks(pdfplumber_page) -> List[Block]:
    blocks = []
    for table in pdfplumber_page.find_tables():
        rows = table.extract()
        if not rows:
            continue
        normalized_rows = [[cell or "" for cell in row] for row in rows]
        x0, y0, x1, y1 = table.bbox
        blocks.append(
            Block(
                block_type=BlockType.TABLE,
                rows=normalized_rows,
                bbox=BoundingBox(x0=x0, y0=y0, x1=x1, y1=y1),
                confidence=1.0,
                source_engine=SourceEngine.PDFPLUMBER,
            )
        )
    return blocks


def _scanned_page_blocks(page: fitz.Page, dpi: int = 200) -> List[Block]:
    import numpy as np
    from PIL import Image

    pixmap = page.get_pixmap(dpi=dpi)
    image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
    results = _get_easyocr_reader().readtext(np.array(image))

    blocks = []
    for box_points, text, confidence in results:
        text = text.strip()
        if not text:
            continue
        xs = [point[0] for point in box_points]
        ys = [point[1] for point in box_points]
        blocks.append(
            Block(
                block_type=BlockType.PARAGRAPH,
                text=text,
                bbox=BoundingBox(x0=min(xs), y0=min(ys), x1=max(xs), y1=max(ys)),
                confidence=float(confidence),
                source_engine=SourceEngine.EASYOCR,
            )
        )
    return blocks


def extract_document(pdf_path: str, file_name: Optional[str] = None) -> Document:
    """يفتح ملف PDF كاملاً ويحوّله إلى Document: نص رقمي عبر PyMuPDF + جداول pdfplumber
    للصفحات التي تحتوي طبقة نص، وOCR عبر easyocr (raster لكامل الصفحة) للصفحات
    الممسوحة ضوئياً بلا طبقة نص."""
    fitz_doc = fitz.open(pdf_path)
    pages: List[Page] = []

    with pdfplumber.open(pdf_path) as plumber_doc:
        for index in range(fitz_doc.page_count):
            fitz_page = fitz_doc[index]
            digital_text = fitz_page.get_text("text").strip()

            if len(digital_text) >= MIN_DIGITAL_CHARS:
                blocks = _digital_page_blocks(fitz_page) + _table_blocks(plumber_doc.pages[index])
                source = PageSource.DIGITAL
            else:
                blocks = _scanned_page_blocks(fitz_page)
                source = PageSource.SCANNED

            pages.append(Page(page_number=index + 1, source=source, blocks=blocks))

    fitz_doc.close()
    return Document(file_name=file_name or pdf_path, pages=pages)
