import unittest

from medical_ocr.chunking import chunk_document
from medical_ocr.schema import Block, BlockType, Document, Page, PageSource, SourceEngine


def _paragraph(text: str) -> Block:
    return Block(block_type=BlockType.PARAGRAPH, text=text, source_engine=SourceEngine.PADDLEOCR)


def _table(rows) -> Block:
    return Block(block_type=BlockType.TABLE, rows=rows, source_engine=SourceEngine.PDFPLUMBER)


class TestChunkDocument(unittest.TestCase):
    def test_table_is_a_single_whole_chunk(self):
        rows = [["الدواء", "الجرعة"], ["باراسيتامول", "500 ملغ"]]
        page = Page(page_number=1, source=PageSource.SCANNED, blocks=[_table(rows)])
        document = Document(file_name="doc.pdf", pages=[page])

        chunks = chunk_document(document)

        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].block_type, BlockType.TABLE)
        self.assertEqual(chunks[0].content, rows)
        self.assertIsNone(chunks[0].context_before)

    def test_consecutive_paragraphs_get_overlap_context(self):
        first = _paragraph("الجملة الأولى من التقرير الطبي")
        second = _paragraph("الجملة الثانية تتابع السياق السابق")
        page = Page(page_number=1, source=PageSource.DIGITAL, blocks=[first, second])
        document = Document(file_name="doc.pdf", pages=[page])

        chunks = chunk_document(document, overlap_chars=10)

        self.assertIsNone(chunks[0].context_before)
        self.assertEqual(chunks[1].context_before, first.text[-10:])
        # السياق ميتاداتا فقط، لا يُدمج داخل content.
        self.assertEqual(chunks[1].content, second.text)

    def test_overlap_resets_after_a_table(self):
        first = _paragraph("فقرة قبل الجدول")
        table = _table([["A", "B"]])
        second = _paragraph("فقرة بعد الجدول")
        page = Page(page_number=1, source=PageSource.DIGITAL, blocks=[first, table, second])
        document = Document(file_name="doc.pdf", pages=[page])

        chunks = chunk_document(document)

        self.assertIsNone(chunks[2].context_before)

    def test_chunk_id_encodes_document_page_and_block_index(self):
        page = Page(page_number=2, source=PageSource.DIGITAL, blocks=[_paragraph("نص")])
        document = Document(file_name="report.pdf", pages=[page])

        chunks = chunk_document(document)

        self.assertEqual(chunks[0].chunk_id, "report.pdf:p2:b0")


if __name__ == "__main__":
    unittest.main()
