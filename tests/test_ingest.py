import os
import tempfile
import unittest
from unittest.mock import Mock, patch

import fitz
import requests

from medical_ocr.ingest import (
    VisionAPIError,
    _blocks_from_vision_page,
    _call_vision_api,
    _get_vision_api_key,
    extract_document,
)
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

    @patch("medical_ocr.ingest._scanned_page_blocks_vision", return_value=[])
    def test_blank_page_is_routed_to_scanned_ocr_path(self, mock_scanned_blocks):
        # لا نستدعي Google Vision API الحقيقي هنا — فقط نتحقق أن صفحة بلا طبقة نص
        # تُوجَّه لمسار OCR الممسوح، دون أي استدعاء شبكة حقيقي.
        with tempfile.TemporaryDirectory() as tmp_dir:
            pdf_path = os.path.join(tmp_dir, "blank.pdf")
            _make_pdf(pdf_path, text=None)

            document = extract_document(pdf_path, file_name="blank.pdf")

            self.assertEqual(document.pages[0].source, PageSource.SCANNED)
            mock_scanned_blocks.assert_called_once()

    @patch(
        "medical_ocr.ingest._scanned_page_blocks_vision",
        side_effect=VisionAPIError("500 من الخادم بعد 3 محاولات"),
    )
    def test_scanned_page_vision_failure_does_not_crash_whole_document(self, _mock):
        # فشل صفحة ممسوحة واحدة (بعد استنفاد إعادة المحاولة) يجب ألا يوقف استخراج
        # بقية المستند — يُسجَّل بدلاً منه Block واحد واضح الفشل بدل استثناء غير مُلتقَط.
        with tempfile.TemporaryDirectory() as tmp_dir:
            pdf_path = os.path.join(tmp_dir, "blank.pdf")
            _make_pdf(pdf_path, text=None)

            document = extract_document(pdf_path, file_name="blank.pdf")

            page = document.pages[0]
            self.assertEqual(page.source, PageSource.SCANNED)
            self.assertEqual(len(page.blocks), 1)
            self.assertEqual(page.blocks[0].confidence, 0.0)
            self.assertIn("تعذّر", page.blocks[0].text)
            self.assertEqual(page.blocks[0].source_engine, SourceEngine.GOOGLE_VISION)


class TestGetVisionApiKey(unittest.TestCase):
    @patch.dict(os.environ, {}, clear=True)
    def test_missing_key_raises_clear_error(self):
        with self.assertRaises(VisionAPIError) as ctx:
            _get_vision_api_key()
        self.assertIn("GOOGLE_VISION_API_KEY", str(ctx.exception))

    @patch.dict(os.environ, {"GOOGLE_VISION_API_KEY": "test-key"}, clear=True)
    def test_present_key_is_returned(self):
        self.assertEqual(_get_vision_api_key(), "test-key")


def _fake_response(status_code: int, json_body: dict = None, text: str = "") -> Mock:
    response = Mock()
    response.status_code = status_code
    response.json.return_value = json_body or {}
    response.text = text
    return response


class TestCallVisionApi(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(os.environ, {"GOOGLE_VISION_API_KEY": "test-key"}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        sleep_patcher = patch("medical_ocr.ingest.time.sleep", return_value=None)
        sleep_patcher.start()
        self.addCleanup(sleep_patcher.stop)

    @patch("medical_ocr.ingest.requests.post")
    def test_retries_on_connection_error_then_succeeds(self, mock_post):
        success = _fake_response(200, {"responses": [{"fullTextAnnotation": {"pages": []}}]})
        mock_post.side_effect = [
            requests.exceptions.ConnectionError("انقطاع مؤقت"),
            requests.exceptions.Timeout("مهلة"),
            success,
        ]

        result = _call_vision_api(b"fake-image-bytes")

        self.assertEqual(mock_post.call_count, 3)
        self.assertEqual(result, {"fullTextAnnotation": {"pages": []}})

    @patch("medical_ocr.ingest.requests.post")
    def test_retries_on_server_error_then_succeeds(self, mock_post):
        success = _fake_response(200, {"responses": [{"fullTextAnnotation": {"pages": []}}]})
        mock_post.side_effect = [_fake_response(500, text="internal error"), success]

        result = _call_vision_api(b"fake-image-bytes")

        self.assertEqual(mock_post.call_count, 2)
        self.assertEqual(result, {"fullTextAnnotation": {"pages": []}})

    @patch("medical_ocr.ingest.requests.post")
    def test_does_not_retry_on_client_error(self, mock_post):
        mock_post.return_value = _fake_response(400, text="API key invalid")

        with self.assertRaises(VisionAPIError):
            _call_vision_api(b"fake-image-bytes")

        # لا فائدة من إعادة محاولة مفتاح غير صالح — يجب الفشل من أول محاولة فقط.
        self.assertEqual(mock_post.call_count, 1)

    @patch("medical_ocr.ingest.requests.post")
    def test_raises_after_exhausting_retries_on_repeated_server_error(self, mock_post):
        mock_post.return_value = _fake_response(500, text="internal error")

        with self.assertRaises(VisionAPIError):
            _call_vision_api(b"fake-image-bytes")

        self.assertEqual(mock_post.call_count, 3)

    @patch("medical_ocr.ingest.requests.post")
    def test_vision_api_error_field_in_200_response_raises(self, mock_post):
        mock_post.return_value = _fake_response(
            200, {"responses": [{"error": {"message": "Bad image data"}}]}
        )

        with self.assertRaises(VisionAPIError) as ctx:
            _call_vision_api(b"fake-image-bytes")
        self.assertIn("Bad image data", str(ctx.exception))
        self.assertEqual(mock_post.call_count, 1)


class TestBlocksFromVisionPage(unittest.TestCase):
    def test_parses_paragraphs_into_paragraph_blocks_with_averaged_confidence(self):
        vision_page = {
            "blocks": [
                {
                    "paragraphs": [
                        {
                            "boundingBox": {
                                "vertices": [
                                    {"x": 10, "y": 20},
                                    {"x": 110, "y": 20},
                                    {"x": 110, "y": 40},
                                    {"x": 10, "y": 40},
                                ]
                            },
                            "words": [
                                {
                                    "confidence": 0.9,
                                    "symbols": [{"text": "B"}, {"text": "P"}],
                                },
                                {
                                    "confidence": 0.7,
                                    "symbols": [{"text": "1"}, {"text": "4"}, {"text": "0"}],
                                },
                            ],
                        }
                    ]
                }
            ]
        }

        blocks = _blocks_from_vision_page(vision_page)

        self.assertEqual(len(blocks), 1)
        block = blocks[0]
        self.assertEqual(block.block_type, BlockType.PARAGRAPH)
        self.assertEqual(block.text, "BP 140")
        self.assertEqual(block.source_engine, SourceEngine.GOOGLE_VISION)
        self.assertAlmostEqual(block.confidence, 0.8)
        self.assertEqual(block.bbox.x0, 10)
        self.assertEqual(block.bbox.y1, 40)

    def test_empty_paragraph_text_is_skipped(self):
        vision_page = {"blocks": [{"paragraphs": [{"words": []}]}]}
        self.assertEqual(_blocks_from_vision_page(vision_page), [])


if __name__ == "__main__":
    unittest.main()
