import unittest

from pydantic import ValidationError

from medical_ocr.schema import (
    Block,
    BlockType,
    BoundingBox,
    Document,
    Page,
    PageSource,
    SourceEngine,
    block_from_paddleocr_line,
    block_from_pymupdf_span,
    table_block_from_pdfplumber,
)


class TestBlock(unittest.TestCase):
    def test_paragraph_block_seeds_raw_text_audit_trail(self):
        block = Block(
            block_type=BlockType.PARAGRAPH,
            text="مريض يعاني من ارتفاع ضغط الدم",
            source_engine=SourceEngine.PADDLEOCR,
            confidence=0.92,
        )
        self.assertEqual(block.raw_text, block.text)

    def test_table_block_seeds_raw_rows_audit_trail(self):
        block = Block(
            block_type=BlockType.TABLE,
            rows=[["الدواء", "الجرعة"], ["باراسيتامول", "500 ملغ"]],
            source_engine=SourceEngine.PDFPLUMBER,
        )
        self.assertEqual(block.raw_rows, block.rows)
        # التعديل اللاحق على rows يجب ألا يمس raw_rows (نسخة مستقلة).
        block.rows[0][0] = "معدَّل"
        self.assertEqual(block.raw_rows[0][0], "الدواء")

    def test_paragraph_without_text_is_rejected(self):
        with self.assertRaises(ValidationError):
            Block(block_type=BlockType.PARAGRAPH, source_engine=SourceEngine.PYMUPDF)

    def test_table_without_rows_is_rejected(self):
        with self.assertRaises(ValidationError):
            Block(block_type=BlockType.TABLE, source_engine=SourceEngine.PDFPLUMBER)

    def test_confidence_bounds(self):
        with self.assertRaises(ValidationError):
            Block(
                block_type=BlockType.PARAGRAPH,
                text="نص",
                source_engine=SourceEngine.PYMUPDF,
                confidence=1.5,
            )


class TestDocumentTree(unittest.TestCase):
    def test_document_page_block_composition(self):
        block = Block(
            block_type=BlockType.HEADING,
            text="تقرير مخبري",
            source_engine=SourceEngine.PYMUPDF,
        )
        page = Page(page_number=1, source=PageSource.DIGITAL, blocks=[block])
        document = Document(file_name="report.pdf", pages=[page])
        self.assertEqual(document.pages[0].blocks[0].text, "تقرير مخبري")


class TestAdapters(unittest.TestCase):
    def test_block_from_pymupdf_span(self):
        span = {"bbox": (10.0, 20.0, 100.0, 40.0), "text": "Hemoglobin: 13.2 g/dL"}
        block = block_from_pymupdf_span(span)
        self.assertEqual(block.source_engine, SourceEngine.PYMUPDF)
        self.assertEqual(block.confidence, 1.0)
        self.assertEqual(block.bbox, BoundingBox(x0=10.0, y0=20.0, x1=100.0, y1=40.0))

    def test_block_from_paddleocr_line(self):
        line_result = [[[10, 10], [50, 10], [50, 30], [10, 30]], ("ميتفورمين", 0.87)]
        block = block_from_paddleocr_line(line_result)
        self.assertEqual(block.source_engine, SourceEngine.PADDLEOCR)
        self.assertAlmostEqual(block.confidence, 0.87)
        self.assertEqual(block.bbox, BoundingBox(x0=10, y0=10, x1=50, y1=30))

    def test_table_block_from_pdfplumber_normalizes_none_cells(self):
        rows = [["الدواء", None], ["أموكسيسيلين", "250 ملغ"]]
        block = table_block_from_pdfplumber(rows, bbox=(0, 0, 200, 100))
        self.assertEqual(block.rows[0][1], "")
        self.assertEqual(block.source_engine, SourceEngine.PDFPLUMBER)


if __name__ == "__main__":
    unittest.main()
