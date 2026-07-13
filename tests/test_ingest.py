import os
import tempfile
import unittest
from unittest.mock import patch

import fitz

from medical_ocr.ingest import extract_document
from medical_ocr.schema import BlockType, PageSource, SourceEngine


def _make_pdf(path: str, text: str = None) -> None:
    doc = fitz.open()
    page = doc.new_page()
    if text:
        page.insert_text((72, 72), text)
    doc.save(path)
    doc.close()


def _make_table_pdf(path: str, rows) -> None:
    """يبني PDF بجدول حقيقي بخطوط شبكة صريحة — pdfplumber.find_tables() يحتاج خطوطاً
    فعلية على الصفحة لاكتشاف الجدول، وليس مجرد نص مُحاذى بمسافات."""
    doc = fitz.open()
    page = doc.new_page()
    x0, y0 = 50, 50
    col_w, row_h = 150, 30
    num_cols = len(rows[0])

    for r in range(len(rows) + 1):
        y = y0 + r * row_h
        page.draw_line((x0, y), (x0 + num_cols * col_w, y))
    for c in range(num_cols + 1):
        x = x0 + c * col_w
        page.draw_line((x, y0), (x, y0 + len(rows) * row_h))

    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            page.insert_text((x0 + c * col_w + 5, y0 + r * row_h + 20), cell)

    doc.save(path)
    doc.close()


class TestExtractDocument(unittest.TestCase):
    def test_digital_page_uses_pymupdf_text_without_ocr(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            pdf_path = os.path.join(tmp_dir, "digital.pdf")
            _make_pdf(pdf_path, text="Patient report: blood pressure 140/90 mmHg")

            document = extract_document(pdf_path, file_name="digital.pdf")

            self.assertEqual(len(document.pages), 1)
            page = document.pages[0]
            self.assertEqual(page.source, PageSource.DIGITAL)
            paragraph_blocks = [b for b in page.blocks if b.block_type == BlockType.PARAGRAPH]
            self.assertTrue(paragraph_blocks)
            self.assertTrue(all(b.source_engine == SourceEngine.PYMUPDF for b in paragraph_blocks))
            joined_text = " ".join(b.text for b in paragraph_blocks)
            self.assertIn("blood pressure", joined_text)

    def test_digital_page_extracts_real_grid_table_via_pdfplumber(self):
        rows = [["Drug", "Dose"], ["Metformin", "500mg"], ["Aspirin", "100mg"]]
        with tempfile.TemporaryDirectory() as tmp_dir:
            pdf_path = os.path.join(tmp_dir, "table.pdf")
            _make_table_pdf(pdf_path, rows)

            document = extract_document(pdf_path, file_name="table.pdf")

            table_blocks = [b for b in document.pages[0].blocks if b.block_type == BlockType.TABLE]
            self.assertEqual(len(table_blocks), 1)
            self.assertEqual(table_blocks[0].rows, rows)
            self.assertEqual(table_blocks[0].source_engine, SourceEngine.PDFPLUMBER)

    @patch("medical_ocr.ingest._scanned_page_blocks", return_value=[])
    def test_blank_page_is_routed_to_scanned_ocr_path(self, mock_scanned_blocks):
        # لا نحمّل نموذج easyocr الحقيقي هنا (ثقيل، يتطلب تنزيل أوزان) — فقط نتحقق
        # أن صفحة بلا طبقة نص تُوجَّه لمسار OCR الممسوح، دون تشغيل LM أو OCR حقيقي.
        with tempfile.TemporaryDirectory() as tmp_dir:
            pdf_path = os.path.join(tmp_dir, "blank.pdf")
            _make_pdf(pdf_path, text=None)

            document = extract_document(pdf_path, file_name="blank.pdf")

            self.assertEqual(document.pages[0].source, PageSource.SCANNED)
            mock_scanned_blocks.assert_called_once()


if __name__ == "__main__":
    unittest.main()
