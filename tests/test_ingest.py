import os
import tempfile
import unittest
from io import BytesIO
from unittest.mock import Mock, patch

import fitz
import requests
from PIL import Image

import medical_ocr.ingest as ingest_module
from medical_ocr.ingest import (
    VisionAPIError,
    _blocks_from_vision_page,
    _call_vision_api,
    _compress_image_to_limit,
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


def _make_tight_borderless_table_pdf(path: str, rows, col_x) -> None:
    """يبني PDF بجدول بلا خطوط شبكة (نص مُحاذى بالمواضع فقط، كنتائج مخبرية شائعة) مع
    أعمدة متقاربة جداً عمداً — يستخدم لاختبار أن التفاوت الديناميكي
    (`_dynamic_text_table_settings`) يمنع التحام نص عمودين متجاورين في خلية واحدة."""
    doc = fitz.open()
    page = doc.new_page()
    row_h = 25
    for r, row in enumerate(rows):
        y = 50 + r * row_h
        for c, cell in enumerate(row):
            page.insert_text((col_x[c], y), cell, fontsize=10)
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

    def test_borderless_table_with_tight_columns_does_not_bleed_into_neighbor_cell(self):
        # عمود "Result" ("95"، "4.2") وعمود "Unit" ("mg/dL"، "mmol/L") متباعدان عمداً
        # ~3px فقط (أقل بكثير من التفاوت الثابت القديم 5px) — قبل التفاوت الديناميكي كان
        # هذا يُنتج "ResultUnit"/"95 mg/dL" ملتحمَين في خلية واحدة (تحقّقنا من هذا فعلياً
        # عبر تشغيل الإعداد الثابت القديم مباشرة قبل كتابة هذا الاختبار).
        rows = [
            ["Test", "Result", "Unit", "Range"],
            ["Glucose", "95", "mg/dL", "70-99"],
            ["Potassium", "4.2", "mmol/L", "3.5-5.0"],
        ]
        col_x = [50, 150, 181, 260]
        with tempfile.TemporaryDirectory() as tmp_dir:
            pdf_path = os.path.join(tmp_dir, "tight.pdf")
            _make_tight_borderless_table_pdf(pdf_path, rows, col_x)

            document = extract_document(pdf_path, file_name="tight.pdf")

            table_blocks = [b for b in document.pages[0].blocks if b.block_type == BlockType.TABLE]
            self.assertEqual(len(table_blocks), 1)
            self.assertEqual(table_blocks[0].rows, rows)

    def test_embedded_image_is_extracted_as_png_with_placeholder_in_correct_position(self):
        # ميزة استخراج الأصول: صورة مُضمَّنة بين فقرتين يجب أن (1) تُستخرَج كـImageAsset
        # مستقل بصيغة PNG، و(2) يظهر Placeholder نصي واضح في مكانها الأصلي بين الفقرتين
        # في تدفّق المستند (وليس مُلحَقاً في آخر الصفحة).
        with tempfile.TemporaryDirectory() as tmp_dir:
            pdf_path = os.path.join(tmp_dir, "with_image.pdf")
            doc = fitz.open()
            page = doc.new_page()
            page.insert_text((50, 50), "Before the chart")
            image = Image.new("RGB", (60, 40), (0, 128, 255))
            buf = BytesIO()
            image.save(buf, format="JPEG")
            page.insert_image(fitz.Rect(50, 100, 150, 180), stream=buf.getvalue())
            page.insert_text((50, 220), "After the chart")
            doc.save(pdf_path)
            doc.close()

            document = extract_document(pdf_path, file_name="with_image.pdf")

            self.assertEqual(len(document.images), 1)
            asset = document.images[0]
            self.assertEqual(asset.mime_type, "image/png")
            self.assertEqual(asset.image_id, "Image_01")
            self.assertIsNotNone(asset.bbox)

            texts = [b.text for b in document.pages[0].blocks if b.text]
            self.assertEqual(
                texts, ["Before the chart", "[Insert Image_01 here]", "After the chart"]
            )

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


def _random_image_bytes(width: int, height: int, fmt: str = "PNG") -> bytes:
    image = Image.frombytes("RGB", (width, height), os.urandom(width * height * 3))
    buffer = BytesIO()
    image.save(buffer, format=fmt)
    return buffer.getvalue()


class TestCompressImageToLimit(unittest.TestCase):
    def test_returns_original_bytes_unchanged_when_already_within_limit(self):
        small = _random_image_bytes(20, 20)

        result = _compress_image_to_limit(small, max_bytes=len(small) + 1000)

        self.assertIs(result, small)

    def test_compresses_via_jpeg_quality_reduction_when_over_limit(self):
        # صورة PNG لضجيج عشوائي 1200x1200 (~4.3MB) — أسوأ حالة ضغط ممكنة، تحاكي صفحة
        # ممسوحة بدقة DPI عالية تتجاوز حد Vision API.
        large = _random_image_bytes(1200, 1200)
        max_bytes = 600_000

        result = _compress_image_to_limit(large, max_bytes=max_bytes)

        self.assertLessEqual(len(result), max_bytes)
        result_image = Image.open(BytesIO(result))
        self.assertEqual(result_image.format, "JPEG")
        # الجودة وحدها كفت هنا — لا حاجة لتصغير الأبعاد.
        self.assertEqual(result_image.size, (1200, 1200))

    def test_shrinks_dimensions_when_quality_reduction_alone_is_not_enough(self):
        large = _random_image_bytes(1200, 1200)
        max_bytes = 200_000

        result = _compress_image_to_limit(large, max_bytes=max_bytes)

        self.assertLessEqual(len(result), max_bytes)
        result_image = Image.open(BytesIO(result))
        self.assertLess(result_image.width, 1200)
        self.assertLess(result_image.height, 1200)

    def test_raises_when_limit_is_impossibly_small(self):
        large = _random_image_bytes(1200, 1200)

        with self.assertRaises(VisionAPIError):
            _compress_image_to_limit(large, max_bytes=50)


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

        self.assertEqual(mock_post.call_count, ingest_module._VISION_MAX_ATTEMPTS)

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
