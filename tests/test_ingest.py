import json
import os
import tempfile
import unittest
from io import BytesIO
from unittest.mock import Mock, patch

import dspy
import fitz
import requests
from PIL import Image, ImageDraw

import medical_ocr.ingest as ingest_module
from medical_ocr.ingest import (
    VisionAPIError,
    _blocks_from_vision_page,
    _call_vision_api,
    _compress_image_to_limit,
    _get_vision_api_key,
    extract_document,
)
from medical_ocr.schema import Block, BlockType, BoundingBox, PageSource, SourceEngine


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

    def test_on_page_done_callback_fires_once_per_page_with_correct_totals(self):
        # مؤشر التقدّم في الواجهة (UploadPanel.tsx) يعتمد على هذا الاستدعاء ليعرض
        # "صفحة N من M" أثناء استخراج مستند متعدد الصفحات — طُلب بعد قياس فعلي أن
        # مستند 30 صفحة ممسوحة يستغرق ~2.5 دقيقة بلا أي تغذية راجعة للمستخدم.
        with tempfile.TemporaryDirectory() as tmp_dir:
            pdf_path = os.path.join(tmp_dir, "multi.pdf")
            doc = fitz.open()
            for i in range(3):
                page = doc.new_page()
                page.insert_text((72, 72), f"Page {i + 1} content")
            doc.save(pdf_path)
            doc.close()

            calls = []
            extract_document(pdf_path, on_page_done=lambda page, total: calls.append((page, total)))

            self.assertEqual(calls, [(1, 3), (2, 3), (3, 3)])

    def test_on_page_ready_streams_full_page_content_as_pages_complete(self):
        # /extract-document-stream يعتمد على هذا لبثّ كل صفحة فور جهوزيتها بدل تجميع
        # المستند كاملاً في الذاكرة ثم إرساله دفعة واحدة في النهاية (خلل ذاكرة حقيقي
        # على ملفات كبيرة، رُصِد فعلياً عبر Render: "Ran out of memory (used over
        # 512MB)"). on_page_ready يجب أن يستلم محتوى الصفحة كاملاً (blocks حقيقية)
        # فور اكتمالها، وليس فقط رقمها.
        with tempfile.TemporaryDirectory() as tmp_dir:
            pdf_path = os.path.join(tmp_dir, "multi.pdf")
            doc = fitz.open()
            for i in range(3):
                page = doc.new_page()
                page.insert_text((72, 72), f"Page {i + 1} content")
            doc.save(pdf_path)
            doc.close()

            streamed = []
            extract_document(
                pdf_path,
                on_page_ready=lambda page, images: streamed.append((page.page_number, page.blocks, images)),
            )

            self.assertEqual([p for p, _, _ in streamed], [1, 2, 3])
            for _, blocks, _ in streamed:
                self.assertTrue(blocks)

    def test_keep_full_result_false_returns_lightweight_document_and_streams_instead(self):
        # keep_full_result=False (وضع البثّ الحقيقي) يجب ألا يُبقي أي محتوى في القوائم
        # الكلية المُرجَعة — الهدف الأساسي هو عدم بقاء الصفحات محمَّلة في ذاكرة الخادم
        # بعد بثّها، وإلا تذوب فائدة تقليل الذاكرة بالكامل.
        with tempfile.TemporaryDirectory() as tmp_dir:
            pdf_path = os.path.join(tmp_dir, "multi.pdf")
            doc = fitz.open()
            for i in range(3):
                page = doc.new_page()
                page.insert_text((72, 72), f"Page {i + 1} content")
            doc.save(pdf_path)
            doc.close()

            streamed_page_numbers = []
            result = extract_document(
                pdf_path,
                on_page_ready=lambda page, images: streamed_page_numbers.append(page.page_number),
                keep_full_result=False,
            )

            self.assertEqual(streamed_page_numbers, [1, 2, 3])
            self.assertEqual(len(result.pages), 3)
            self.assertTrue(all(page.blocks == [] for page in result.pages))
            self.assertEqual(result.images, [])

    def test_streaming_mode_strips_repeated_header_same_as_normal_mode(self):
        # _strip_repeated_page_headers الأصلية تعمل كتمريرة واحدة بعد اكتمال كل
        # الصفحات — في وضع البثّ (keep_full_result=False) لا تُستدعى تلك التمريرة
        # إطلاقاً (الصفحات هياكل خفيفة فارغة)، فيجب أن يُطبَّق نفس منطق حذف الترويسة
        # المكررة inline أثناء الحلقة نفسها، بنفس النتيجة تماماً كالمسار العادي.
        with tempfile.TemporaryDirectory() as tmp_dir:
            pdf_path = os.path.join(tmp_dir, "header.pdf")
            doc = fitz.open()
            for i in range(2):
                page = doc.new_page()
                page.insert_text((72, 72), "HOSPITAL HEADER LINE")
                page.insert_text((72, 90), f"Distinct content for page {i + 1}")
            doc.save(pdf_path)
            doc.close()

            normal_result = extract_document(pdf_path)

            streamed_blocks = []
            extract_document(
                pdf_path,
                on_page_ready=lambda page, images: streamed_blocks.append(page.blocks),
                keep_full_result=False,
            )

            normal_texts = [[b.text for b in page.blocks] for page in normal_result.pages]
            streamed_texts = [[b.text for b in blocks] for blocks in streamed_blocks]
            self.assertEqual(normal_texts, streamed_texts)
            # الترويسة المكررة يجب أن تختفي من الصفحة الثانية فقط، لا الأولى
            self.assertIn("HOSPITAL HEADER LINE", normal_texts[0])
            self.assertNotIn("HOSPITAL HEADER LINE", normal_texts[1])

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

    @patch("medical_ocr.ingest._scanned_page_blocks_vision", return_value=([], []))
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


def _vision_word(text, x0, x1, top, bottom):
    """يبني dict كلمة بنفس شكل مخرَج `_vision_word_boxes` مباشرة (للاختبارات التي
    تفحص التجميع الهندسي دون المرور عبر JSON استجابة Vision كاملة)."""
    return {"text": text, "x0": x0, "x1": x1, "top": top, "bottom": bottom}


def _dexa_style_words(include_surrounding_paragraph: bool) -> list:
    """يبني كلمات جدول DEXA اصطناعي (Region/BMD/T-Score/Z-Score) بفجوات أفقية واسعة
    بين الأعمدة، مع خيار تضمين سطر نص عادي محيط (لاختبار عدم التأثر بنسبة الجدول من
    الصفحة — انظر توثيق `_detect_scanned_table_regions`).

    كل الفجوات هنا (نثر ~20-30px، أعمدة ~165-270px) مبنية على قياسات حقيقية فعلية
    عبر Vision API حياً على صفحات DEXA حقيقية (وليست أرقاماً مُخترَعة) — نسخة أولى
    من هذا الملف استخدمت فجوات أعمدة صغيرة جداً (~60-85px) قريبة جداً من فجوات
    النثر ففشلت لاحقاً في التمييز، ونسخة أخرى استخدمت فجوة "Femoral"→"Neck" شبه
    صفرية (3px) غير واقعية أصلاً."""
    words = []
    if include_surrounding_paragraph:
        for text, x0, x1 in [
            ("Patient:", 50, 110), ("Jane", 128, 158), ("Doe,", 176, 206),
            ("DOB", 224, 254), ("1985-03-12", 272, 352),
        ]:
            words.append(_vision_word(text, x0, x1, 50, 65))
    for text, x0, x1 in [("Region", 100, 160), ("BMD", 400, 435), ("T-Score", 600, 660), ("Z-Score", 900, 960)]:
        words.append(_vision_word(text, x0, x1, 120, 135))
    for text, x0, x1 in [("L1-L4", 100, 150), ("0.912", 410, 445), ("-1.2", 610, 640), ("-0.5", 910, 935)]:
        words.append(_vision_word(text, x0, x1, 150, 165))
    for text, x0, x1 in [
        ("Femoral", 90, 140), ("Neck", 158, 183), ("0.850", 410, 445), ("-1.8", 610, 640), ("-1.1", 910, 935),
    ]:
        words.append(_vision_word(text, x0, x1, 180, 195))
    return words


class TestVisionWordBoxes(unittest.TestCase):
    def test_extracts_word_text_and_pixel_bbox(self):
        vision_page = {
            "blocks": [
                {
                    "paragraphs": [
                        {
                            "words": [
                                {
                                    "symbols": [{"text": "B"}, {"text": "P"}],
                                    "boundingBox": {
                                        "vertices": [
                                            {"x": 10, "y": 20}, {"x": 30, "y": 20},
                                            {"x": 30, "y": 40}, {"x": 10, "y": 40},
                                        ]
                                    },
                                }
                            ]
                        }
                    ]
                }
            ]
        }

        words = ingest_module._vision_word_boxes(vision_page)

        self.assertEqual(len(words), 1)
        self.assertEqual(words[0]["text"], "BP")
        self.assertEqual((words[0]["x0"], words[0]["top"], words[0]["x1"], words[0]["bottom"]), (10, 20, 30, 40))

    def test_word_without_bounding_box_is_skipped(self):
        vision_page = {"blocks": [{"paragraphs": [{"words": [{"symbols": [{"text": "X"}]}]}]}]}
        self.assertEqual(ingest_module._vision_word_boxes(vision_page), [])


class TestDetectScannedTableRegions(unittest.TestCase):
    """اكتشاف جداول الصفحات الممسوحة هندسياً (بلا أي كيان "جدول" من Vision نفسه) —
    انظر توثيق `_find_gap_threshold`/`_detect_scanned_table_regions` لتاريخ التطوير
    (وسيط ← أضيق فجوة ← أكبر قفزة نسبية ← أول قفزة نسبية بدءاً من الأسفل، كل خطوة
    كانت رداً على فشل حقيقي مُلاحَظ ضد Vision API فعلي وليس افتراضياً)."""

    def test_detects_dexa_table_alongside_surrounding_paragraph(self):
        regions = ingest_module._detect_scanned_table_regions(_dexa_style_words(include_surrounding_paragraph=True))

        self.assertEqual(len(regions), 1)
        self.assertEqual(
            regions[0]["rows"],
            [
                ["Region", "BMD", "T-Score", "Z-Score"],
                ["L1-L4", "0.912", "-1.2", "-0.5"],
                ["Femoral Neck", "0.850", "-1.8", "-1.1"],
            ],
        )

    def test_detects_dexa_table_when_page_is_almost_entirely_tabular(self):
        regions = ingest_module._detect_scanned_table_regions(_dexa_style_words(include_surrounding_paragraph=False))

        self.assertEqual(len(regions), 1)
        self.assertEqual(regions[0]["rows"][0], ["Region", "BMD", "T-Score", "Z-Score"])

    def test_two_unrelated_tables_separated_by_whitespace_are_not_merged(self):
        # اختبار مبني على خلل حقيقي اكتُشف عبر ملف مريض حقيقي: جدول DEXA رئيسي
        # يتبعه مباشرة (بلا سطر نثر يفصل بينهما في قائمة الأسطر) جدول مرجعي مختلف
        # تماماً (معايير WHO)، بمسافة رأسية بيضاء واضحة أكبر من التباعد المعتاد بين
        # أسطر الصفحة — يجب أن يُكتشَفا كمنطقتين منفصلتين، لا منطقة واحدة ملتحمة.
        words = _dexa_style_words(include_surrounding_paragraph=False)
        normal_line_spacing = 30  # يطابق التباعد بين أسطر الجدول الأول (top=120/150/180)
        second_table_top = 180 + 15 + normal_line_spacing * 6  # فجوة أكبر بكثير من المعتاد
        # 3 أسطر تحاكي جدولاً مرجعياً حقيقياً (تسمية + نطاق رقمي) كي يبقى فوق نسبة
        # الخلايا الرقمية الصرفة الدنيا (`_looks_like_non_clinical_metadata`) —
        # اختبار قصده الأصلي التباعد الرأسي، وليس فلتر المحتوى.
        for i, (text, x0, x1) in enumerate([("Test A", 100, 160), ("Test B", 100, 170), ("Test C", 100, 150)]):
            words.append(_vision_word(text, x0, x1, second_table_top + i * normal_line_spacing, second_table_top + i * normal_line_spacing + 15))
        for i, (text, x0, x1) in enumerate([("70 - 99", 400, 460), ("3.5 - 5.0", 400, 460), ("0.5 - 1.5", 400, 460)]):
            words.append(_vision_word(text, x0, x1, second_table_top + i * normal_line_spacing, second_table_top + i * normal_line_spacing + 15))

        regions = ingest_module._detect_scanned_table_regions(words)

        self.assertEqual(len(regions), 2)
        self.assertEqual(regions[0]["rows"][0], ["Region", "BMD", "T-Score", "Z-Score"])
        self.assertEqual(
            regions[1]["rows"], [["Test A", "70 - 99"], ["Test B", "3.5 - 5.0"], ["Test C", "0.5 - 1.5"]]
        )

    def test_plain_prose_page_yields_no_false_positive_table(self):
        words = []
        y = 50
        for line in [
            "This is a normal paragraph sentence with several words in it.",
            "Here is another line of plain prose text following the first one.",
            "And a third line to make sure no false table is detected here.",
        ]:
            x = 50
            for token in line.split(" "):
                width = len(token) * 6
                words.append(_vision_word(token, x, x + width, y, y + 15))
                x += width + 5
            y += 25

        self.assertEqual(ingest_module._detect_scanned_table_regions(words), [])

    def test_single_row_is_not_enough_to_count_as_a_table(self):
        words = [
            _vision_word("Region", 100, 160, 120, 135),
            _vision_word("BMD", 220, 250, 120, 135),
        ]
        self.assertEqual(ingest_module._detect_scanned_table_regions(words), [])


class TestStructureScannedTableRows(unittest.TestCase):
    def test_returns_none_when_lm_not_configured(self):
        # بيئة الاختبار بلا GEMINI_API_KEY أصلاً (نفس افتراض بقية هذا الملف)، لذا
        # dspy.settings.lm يبقى None طوال تشغيل المجموعة كاملة.
        self.assertIsNone(dspy.settings.lm)
        self.assertIsNone(ingest_module._structure_scanned_table_rows([["Region", "BMD"], ["L1-L4", "0.912"]]))

    def test_parses_structured_dicts_into_flat_corrected_rows_when_lm_configured(self):
        raw_rows = [["Regoin", "BMD"], ["L1-L4", "0.912"]]
        fake_structurer = Mock(
            return_value=Mock(
                structured_rows=json.dumps(
                    [{"Region": "Region", "BMD": "BMD"}, {"Region": "L1-L4", "BMD": "0.912"}]
                )
            )
        )
        original_lm = dspy.settings.lm
        dspy.settings.lm = "fake-lm-for-test"
        try:
            with patch.object(ingest_module, "_get_table_structurer", return_value=fake_structurer):
                result = ingest_module._structure_scanned_table_rows(raw_rows)
        finally:
            dspy.settings.lm = original_lm

        self.assertEqual(result, [["Region", "BMD"], ["L1-L4", "0.912"]])

    def test_falls_back_to_none_when_row_count_mismatches(self):
        raw_rows = [["Region", "BMD"], ["L1-L4", "0.912"]]
        fake_structurer = Mock(return_value=Mock(structured_rows=json.dumps([{"Region": "Region", "BMD": "BMD"}])))
        original_lm = dspy.settings.lm
        dspy.settings.lm = "fake-lm-for-test"
        try:
            with patch.object(ingest_module, "_get_table_structurer", return_value=fake_structurer):
                result = ingest_module._structure_scanned_table_rows(raw_rows)
        finally:
            dspy.settings.lm = original_lm

        self.assertIsNone(result)

    def test_falls_back_to_none_when_lm_call_raises(self):
        fake_structurer = Mock(side_effect=RuntimeError("network error"))
        original_lm = dspy.settings.lm
        dspy.settings.lm = "fake-lm-for-test"
        try:
            with patch.object(ingest_module, "_get_table_structurer", return_value=fake_structurer):
                result = ingest_module._structure_scanned_table_rows([["Region"], ["L1-L4"]])
        finally:
            dspy.settings.lm = original_lm

        self.assertIsNone(result)


def _char(c: str, x0: float, x1: float, y0: float = 100.0, y1: float = 112.0) -> dict:
    return {"c": c, "bbox": (x0, y0, x1, y1)}


class TestVisibleLineText(unittest.TestCase):
    """خلل حقيقي مُشخَّص عبر PDF تقرير طبي حقيقي (نظام HIS لمستشفى سعودي): النص
    العربي الظاهر يحمل بين حروفه نسخة "شبح" مكرَّرة بترتيب معكوس، كل حرف منها بعرض
    صفري (x0==x1) — على الأرجح أثر توافقية من مولّد التقرير. `_visible_line_text`
    تستبعد أي حرف بعرض صفري فتُبقي النص المرئي الحقيقي فقط."""

    def test_strips_interleaved_zero_width_reversed_duplicate(self):
        # يحاكي حرفياً ما وُجد فعلياً في الملف الحقيقي: "تاريخ" مقسومة بحرفَي "تاري"
        # و"خ" حول نسخة معكوسة بعرض صفري من "المراجعة" ("ةعجارملا")، تتبعها النسخة
        # الحقيقية الظاهرة لاحقاً في نفس السطر — القيم x مأخوذة من نفس نمط الملف
        # الحقيقي (تناقص من اليمين لليسار، RTL).
        real = [("ت", 298.3, 301.3), ("ا", 295.8, 298.4), ("ر", 291.0, 295.8), ("ي", 288.0, 291.0)]
        phantom = [("ة", 288.1), ("ع", 288.1), ("ج", 288.1), ("ا", 288.1),
                   ("ر", 288.1), ("م", 288.1), ("ل", 288.1), ("ا", 288.1), (" ", 288.1)]
        rest = [("خ", 281.9, 288.1), (" ", 279.1, 281.9), ("ا", 276.7, 279.1),
                ("ل", 274.2, 276.8), ("م", 269.7, 274.2), ("ر", 264.9, 269.6),
                ("ا", 262.6, 265.0), ("ج", 256.8, 262.5), ("ع", 252.8, 256.8), ("ة", 248.3, 252.9)]

        chars = (
            [_char(c, x0, x1) for c, x0, x1 in real]
            + [_char(c, x, x) for c, x in phantom]
            + [_char(c, x0, x1) for c, x0, x1 in rest]
        )
        line = {"spans": [{"chars": chars}]}

        self.assertEqual(ingest_module._visible_line_text(line), "تاريخ المراجعة")

    def test_leaves_line_without_zero_width_chars_unchanged(self):
        chars = [_char(c, i * 10, i * 10 + 8) for i, c in enumerate("hello")]
        line = {"spans": [{"chars": chars}]}

        self.assertEqual(ingest_module._visible_line_text(line), "hello")

    def test_joins_multiple_spans_in_line_order(self):
        line = {
            "spans": [
                {"chars": [_char("a", 0, 8), _char("b", 8, 16)]},
                {"chars": [_char("c", 16, 24)]},
            ]
        }

        self.assertEqual(ingest_module._visible_line_text(line), "abc")


class TestMergeSplitHeaderRow(unittest.TestCase):
    """خلل حقيقي وُصِف من المستخدم بعد رفع ملف DEXA حقيقي ("بعض الأرقام تظهر
    وبعضها لا"): ترويسة مطبوعة على سطرين ("Site Region BMD Young Adult Age
    Matched" ثم "(gm/cm2) T-score Z-score" تحتهما مباشرة) كانت تُبنى كصفّين
    منفصلين، فيملأ LLM خلايا Site/Region الناقصة في السطر الثاني بـ"UNCERTAIN" —
    `_merge_split_header_row` يدمجهما قبل وصولهما لـLLM أصلاً."""

    def test_merges_shorter_second_row_right_aligned_into_first(self):
        rows = [
            ["Site", "Region", "BMD", "Young Adult", "Age Matched"],
            ["( gm / cm2 )", "T - score", "Z - score"],
            ["Spine", "Total", "0.846", "-2.1", "-1.2"],
        ]

        merged = ingest_module._merge_split_header_row(rows)

        self.assertEqual(
            merged,
            [
                ["Site", "Region", "BMD ( gm / cm2 )", "Young Adult T - score", "Age Matched Z - score"],
                ["Spine", "Total", "0.846", "-2.1", "-1.2"],
            ],
        )

    def test_leaves_rows_unchanged_when_second_row_is_not_shorter(self):
        rows = [["Test", "Result"], ["Glucose", "95"], ["Sodium", "140"]]
        self.assertEqual(ingest_module._merge_split_header_row(rows), rows)

    def test_leaves_single_row_unchanged(self):
        self.assertEqual(ingest_module._merge_split_header_row([["Only"]]), [["Only"]])


class TestIsCleanNumericCell(unittest.TestCase):
    def test_plain_number_is_clean(self):
        self.assertTrue(ingest_module._is_clean_numeric_cell("0.870"))

    def test_number_with_medical_punctuation_is_clean(self):
        self.assertTrue(ingest_module._is_clean_numeric_cell("-1.5 ( 82 % )"))

    def test_number_with_inline_unit_letters_is_not_clean(self):
        # "158.0 cm"/"98.5 kg" (وحدة ملتصقة بالرقم) هي بالضبط الفارق بين خلية
        # قياس مريض (طول/وزن) وخلية نتيجة مخبرية صرفة (تستخدم % لا حرفاً).
        self.assertFalse(ingest_module._is_clean_numeric_cell("158.0 cm"))
        self.assertFalse(ingest_module._is_clean_numeric_cell("98.5 kg"))

    def test_plain_label_is_not_clean(self):
        self.assertFalse(ingest_module._is_clean_numeric_cell("Neck"))

    def test_empty_cell_is_not_clean(self):
        self.assertFalse(ingest_module._is_clean_numeric_cell(""))
        self.assertFalse(ingest_module._is_clean_numeric_cell("()% "))


class TestLooksLikeNonClinicalMetadata(unittest.TestCase):
    """ثلاث محاولات متتالية على نفس ملف مريض حقيقي: (1) حد أدنى لعدد الصفوف —
    استبعد أيضاً جدولاً سريرياً حقيقياً قصيراً بالخطأ، (2) استهداف ":" حرفياً في
    صفّين فقط — نجح مع NAME/FILE-NO لكن فشل مع شبكة معلومات مريض أعقد فقد OCR
    فيها حرف ":" نفسه، (3) **الحل الحالي:** كثافة الخلايا الرقمية الصرفة في
    المنطقة كلها، بصرف النظر عن عدد الصفوف."""

    def test_name_file_no_pair_is_metadata(self):
        rows = [["NAME : MARZOOKA SLYM", "FILE NO : 252036"], ["DATE : 14/10/2025", "REF : Dr. SHAIMA"]]
        self.assertTrue(ingest_module._looks_like_non_clinical_metadata(rows))

    def test_two_row_clinical_table_is_not_metadata(self):
        rows = [["Neck", "0.698", "-1.9", "-1.2"], ["Total", "0.757", "-1.5", "-1.1"]]
        self.assertFalse(ingest_module._looks_like_non_clinical_metadata(rows))

    def test_demographics_grid_without_literal_colons_is_metadata(self):
        # يحاكي شبكة معلومات مريض حقيقية (Birthdate/Weight/Gender/Ethnicity) حيث
        # فقد OCR حرف ":" نفسه أثناء التقسيم الهندسي — الفلتر السابق (":" حرفياً)
        # كان يفشل هنا تحديداً؛ هذا يستخدم بيانات مصطنعة، وليست بيانات المريض الفعلية.
        rows = [
            ["Birthdate", "1970-01-01 ( 55.0 )", "Height", "170.0 cm"],
            ["PATIENT NAME", "Gender", "Male", "Weight", "80.0 kg"],
            ["Menopause", "No", "Ethnicity", "MIDEAST", "Date Measured 2026-01-01"],
        ]
        self.assertTrue(ingest_module._looks_like_non_clinical_metadata(rows))

    def test_ten_row_clinical_table_is_not_metadata(self):
        rows = [
            [f"L{i}", "0.870", "-1.5 ( 82 % )", "-0.6 ( 92 % )", "10.46", "12.03"] for i in range(1, 11)
        ]
        self.assertFalse(ingest_module._looks_like_non_clinical_metadata(rows))

    def test_empty_region_is_not_metadata(self):
        self.assertFalse(ingest_module._looks_like_non_clinical_metadata([]))


def _draw_ruled_grid_png(row_ys, col_xs, size=(900, 700), line_width=3) -> bytes:
    """يبني صورة PNG بجدول خطوط شبكة مرسومة فعلياً (وليس مجرد نص متباعد) — لاختبار
    `_detect_ruled_table_regions` دون الحاجة لملف مريض حقيقي. الشبكة تبقى بعيدة عمداً
    عن حواف الصورة المطلقة (هامش ≥10% من كل جهة) كي لا تُستبعَد خطأً بصفتها ظل/حافة
    صفحة مصوَّرة (انظر `_RULED_TABLE_EDGE_ARTIFACT_MARGIN_RATIO`)."""
    img = Image.new("L", size, 255)
    draw = ImageDraw.Draw(img)
    for y in row_ys:
        draw.line([(col_xs[0], y), (col_xs[-1], y)], fill=0, width=line_width)
    for x in col_xs:
        draw.line([(x, row_ys[0]), (x, row_ys[-1])], fill=0, width=line_width)
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


def _strip_empty_border_cells(rows: list) -> list:
    """يُزيل صفوفاً/أعمدة فارغة تماماً من الحواف — dilate الدمج (merge_kernel) يوسِّع
    صندوق المنطقة المكتشَفة قليلاً عمداً، فيُضيف صفاً/عموداً فارغاً على كل حافة."""
    rows = [row for row in rows if any(cell for cell in row)]
    if not rows:
        return rows
    empty_cols = [i for i in range(len(rows[0])) if not any(row[i] for row in rows)]
    return [[cell for i, cell in enumerate(row) if i not in empty_cols] for row in rows]


class TestDetectRuledTableRegions(unittest.TestCase):
    """اكتشاف جداول بخطوط شبكة مرسومة فعلياً (استمارات/معلومات مريض حدودها مرسومة)
    عبر معالجة صورة — خلاف صريح عن الاكتشاف الهندسي من تباعد النص بلا حدود. طُلب
    صراحة من المستخدم بعد اختبار ملف أشعة/إحالة حقيقي فيه جداول نموذجية بخطوط
    مرسومة لمعلومات مريض (Patient ID/Name/DOB) بلا أي محتوى رقمي — يجب أن تُكتشَف
    كجداول دوماً بصرف النظر عن المحتوى."""

    def _words(self, *rows_of_cells):
        words = []
        for row_idx, cells in enumerate(rows_of_cells):
            top, bottom = 130 + row_idx * 150, 160 + row_idx * 150
            for col_idx, text in enumerate(cells):
                x0 = 150 + col_idx * 300
                words.append({"text": text, "x0": x0, "x1": x0 + 100, "top": top, "bottom": bottom, "slope": 0.0})
        return words

    def test_finds_grid_and_assigns_words_to_correct_cells(self):
        image_bytes = _draw_ruled_grid_png(row_ys=[100, 250, 400, 550], col_xs=[100, 400, 700])
        words = self._words(["Header1", "Header2"], ["Value1", "Value2"], ["Value3", "Value4"])

        regions = ingest_module._detect_ruled_table_regions(image_bytes, words)

        self.assertEqual(len(regions), 1)
        rows = _strip_empty_border_cells(regions[0]["rows"])
        self.assertEqual(rows, [["Header1", "Header2"], ["Value1", "Value2"], ["Value3", "Value4"]])

    def test_plain_image_with_no_lines_yields_no_regions(self):
        img = Image.new("L", (900, 700), 255)
        buffer = BytesIO()
        img.save(buffer, format="PNG")
        regions = ingest_module._detect_ruled_table_regions(buffer.getvalue(), self._words(["Header1", "Header2"]))
        self.assertEqual(regions, [])

    def test_invalid_image_bytes_return_no_regions_without_crashing(self):
        self.assertEqual(ingest_module._detect_ruled_table_regions(b"not-an-image", []), [])

    def test_table_with_non_numeric_patient_metadata_is_still_detected(self):
        # طلب صريح من المستخدم: خطوط مرسومة فعلياً = جدول دوماً، حتى بلا أي أرقام —
        # خلافاً لـ`_looks_like_non_clinical_metadata` التي لا تُطبَّق هنا إطلاقاً.
        image_bytes = _draw_ruled_grid_png(row_ys=[100, 250, 400], col_xs=[100, 400, 700])
        words = self._words(["Patient Name", "Gender"], ["Bndr Al Jehani", "Male"])

        regions = ingest_module._detect_ruled_table_regions(image_bytes, words)

        self.assertEqual(len(regions), 1)
        rows = _strip_empty_border_cells(regions[0]["rows"])
        self.assertEqual(rows, [["Patient Name", "Gender"], ["Bndr Al Jehani", "Male"]])

    def test_form_with_one_narrative_cell_is_rejected_as_table(self):
        # حالة حقيقية أبلغ عنها المستخدم: استمارة إحالة حقيقية بخطوط مرسومة صحيحة،
        # لكن خلية "شكوى المريض" تحوي فقرة سردية سريرية كاملة (مئات الكلمات) بدل قيمة
        # جدول قصيرة. جدول بيانات حقيقي بلا أي خلية سردية طويلة (كالاختبار أعلاه) يجب
        # أن يبقى مكتشَفاً، لكن هذه المنطقة يجب أن تُرفَض بالكامل فتتدفّق كلماتها كنص
        # عادي بدل خلية جدول واحدة تُفجِّر التنسيق عند التصدير.
        image_bytes = _draw_ruled_grid_png(row_ys=[100, 250, 400], col_xs=[100, 400, 700])
        narrative = "CC : LOW BACK PAIN . " * 10  # أطول من _RULED_TABLE_MAX_CELL_CHARS
        words = self._words(["Patient Name", "Gender"], [narrative, "Male"])

        regions = ingest_module._detect_ruled_table_regions(image_bytes, words)

        self.assertEqual(regions, [])


class TestDetectScannedPhotoRegions(unittest.TestCase):
    """اكتشاف صور/أشكال داخل صفحة ممسوحة (بيتماب واحد بلا كائنات PDF صور منفصلة) —
    طُلب صراحة من المستخدم بعد رفع صفحة كتاب حقيقية (مونتاج Fig. 80.6/80.7: أشعة
    ورسوم توضيحية) لم يُستخرَج منها أي صورة إطلاقاً (الصفحة كلها كانت تُعامَل كنص
    OCR فقط، بلا أي مفهوم لصور فرعية داخلها)."""

    def _blank_image(self, size=(900, 700)) -> Image.Image:
        return Image.new("L", size, 255)

    def _png_bytes(self, img: Image.Image) -> bytes:
        buffer = BytesIO()
        img.save(buffer, format="PNG")
        return buffer.getvalue()

    def test_two_separated_photo_blocks_are_each_detected_separately(self):
        # **درس حاسم من اختبار حقيقي:** إغلاق/تمديد مورفولوجي (حتى بنواة صغيرة) كان
        # يُلحم لوحات متجاورة في مكوّن واحد رغم فجوة بيضاء حقيقية بينها — هذا الاختبار
        # يتحقق أن العتبة الخام وحدها (بلا أي معالجة إضافية) تُبقيهما منفصلتين.
        img = self._blank_image()
        draw = ImageDraw.Draw(img)
        draw.rectangle([100, 100, 300, 300], fill=80)
        draw.rectangle([500, 100, 750, 350], fill=120)

        regions = ingest_module._detect_scanned_photo_regions(self._png_bytes(img), words=[])

        boxes = sorted((r.x0, r.y0, r.x1, r.y1) for r in regions)
        self.assertEqual(boxes, [(100.0, 100.0, 301.0, 301.0), (500.0, 100.0, 751.0, 351.0)])

    def test_region_mostly_covered_by_ocr_words_is_excluded_as_text(self):
        # تمييز "صورة حقيقية" عن "نص/صندوق نص" (مثال BOX 80.2 بخلفية ملوَّنة في
        # الملف الحقيقي المُشخَّص) عبر نسبة تغطية كلمات Vision، لا شكل المحتوى.
        img = self._blank_image()
        draw = ImageDraw.Draw(img)
        draw.rectangle([100, 100, 400, 300], fill=80)  # ستُغطَّى بكلمات = نص
        draw.rectangle([600, 100, 800, 300], fill=80)  # بلا أي كلمة فوقها = صورة

        words = [{"x0": 100, "x1": 400, "top": 100, "bottom": 300}]
        regions = ingest_module._detect_scanned_photo_regions(self._png_bytes(img), words)

        self.assertEqual(len(regions), 1)
        self.assertEqual((regions[0].x0, regions[0].y0, regions[0].x1, regions[0].y1), (600.0, 100.0, 801.0, 301.0))

    def test_small_label_inside_photo_does_not_disqualify_it(self):
        # تسمية قصيرة جداً (حرف واحد "A"/"B") داخل لوحة شكل حقيقية شائعة (مثال Fig.
        # 80.7 A-I) يجب ألا تُسقطها من التصنيف كصورة — التغطية النصية ضئيلة جداً.
        img = self._blank_image()
        draw = ImageDraw.Draw(img)
        draw.rectangle([100, 100, 400, 400], fill=80)

        words = [{"x0": 110, "x1": 120, "top": 110, "bottom": 125}]
        regions = ingest_module._detect_scanned_photo_regions(self._png_bytes(img), words)

        self.assertEqual(len(regions), 1)

    def test_component_spanning_whole_page_is_excluded_as_artifact(self):
        # مكوّن يلامس حواف الصورة كاملةً (عرض وارتفاع الصفحة معاً) هو التحام كاذب عبر
        # القماشة كاملة (نفس فئة خلل ظل/حافة الصفحة الموثَّقة سابقاً في
        # `_drop_oversized_line_components`)، وليس شكلاً حقيقياً واحداً بهذا الحجم.
        img = self._blank_image()
        draw = ImageDraw.Draw(img)
        draw.rectangle([0, 0, 899, 699], outline=0, width=5)

        regions = ingest_module._detect_scanned_photo_regions(self._png_bytes(img), words=[])

        self.assertEqual(regions, [])

    def test_tiny_noise_speck_is_ignored(self):
        img = self._blank_image()
        draw = ImageDraw.Draw(img)
        draw.rectangle([100, 100, 110, 110], fill=80)

        regions = ingest_module._detect_scanned_photo_regions(self._png_bytes(img), words=[])

        self.assertEqual(regions, [])

    def test_invalid_image_bytes_return_no_regions_without_crashing(self):
        self.assertEqual(ingest_module._detect_scanned_photo_regions(b"not-an-image", []), [])


class TestDropOverlappingBoxes(unittest.TestCase):
    """خلل حقيقي: دمج صندوقين متداخلين (جدول حقيقي + شعار/ختم مُزخرَف ملتصق بحافته)
    في اتحادهما كان يُخفِّض كثافة الخطوط الرأسية دون عتبة الكشف، فيفشل استخراج حدود
    الأعمدة كلياً رغم وجود جدول حقيقي واضح — الإبقاء على الأكثف وإسقاط الآخر يحلّ هذا."""

    def test_keeps_denser_box_when_overlapping(self):
        sparse_logo_like = ((0, 0, 200, 200), 0.02)
        dense_real_table = ((50, 50, 300, 300), 0.05)

        kept = ingest_module._drop_overlapping_boxes([sparse_logo_like, dense_real_table])

        self.assertEqual(kept, [(50, 50, 300, 300)])

    def test_non_overlapping_boxes_are_both_kept(self):
        box_a = ((0, 0, 100, 100), 0.05)
        box_b = ((200, 200, 300, 300), 0.05)

        kept = ingest_module._drop_overlapping_boxes([box_a, box_b])

        self.assertEqual(set(kept), {(0, 0, 100, 100), (200, 200, 300, 300)})


class TestScannedTableBlocks(unittest.TestCase):
    def test_builds_table_block_with_raw_grid_when_lm_unconfigured(self):
        # bytes فارغة عمداً: تُفشِل `_ruled_line_masks` بأمان (تُعيد None، فتُتخطى
        # منطقة الخطوط المرسومة) وتترك المسار الهندسي النصي وحده قيد الاختبار هنا.
        blocks = ingest_module._scanned_table_blocks(b"", _dexa_style_words(include_surrounding_paragraph=True))

        self.assertEqual(len(blocks), 1)
        block = blocks[0]
        self.assertEqual(block.block_type, BlockType.TABLE)
        self.assertEqual(block.source_engine, SourceEngine.GOOGLE_VISION)
        self.assertEqual(block.rows, block.raw_rows)
        self.assertEqual(block.rows[0], ["Region", "BMD", "T-Score", "Z-Score"])


class TestScannedPageTableIntegration(unittest.TestCase):
    """اختبار تكامل كامل: `_scanned_page_blocks_vision` يجب أن يبني Block(TABLE) واحداً
    من الجدول ويستبعد نصه من الفقرات العادية (بلا تكرار)، مع بقاء ترتيب القراءة صحيحاً
    (الفقرة قبل الجدول)."""

    @patch("medical_ocr.ingest._call_vision_api")
    def test_table_extracted_once_without_duplicating_paragraph_text(self, mock_call_vision_api):
        words = _dexa_style_words(include_surrounding_paragraph=True)

        def make_word(w):
            return {
                "symbols": [{"text": ch} for ch in w["text"]],
                "boundingBox": {
                    "vertices": [
                        {"x": w["x0"], "y": w["top"]}, {"x": w["x1"], "y": w["top"]},
                        {"x": w["x1"], "y": w["bottom"]}, {"x": w["x0"], "y": w["bottom"]},
                    ]
                },
                "confidence": 0.95,
            }

        # سطر الفقرة العادية بمفرده كـparagraph منفصل عن كلمات الجدول، بنفس التقسيم
        # الحقيقي الذي يعطيه Vision (فقرة لكل سطر هنا لتبسيط الاختبار).
        para_words = [w for w in words if w["text"] in ("Patient:", "Jane", "Doe,", "DOB", "1985-03-12")]
        table_words = [w for w in words if w not in para_words]

        def paragraph_from(ws):
            xs0 = min(w["x0"] for w in ws)
            xs1 = max(w["x1"] for w in ws)
            ys0 = min(w["top"] for w in ws)
            ys1 = max(w["bottom"] for w in ws)
            return {
                "boundingBox": {
                    "vertices": [
                        {"x": xs0, "y": ys0}, {"x": xs1, "y": ys0}, {"x": xs1, "y": ys1}, {"x": xs0, "y": ys1},
                    ]
                },
                "words": [make_word(w) for w in ws],
            }

        # كل سطر جدول فقرة Vision منفصلة (واقعي: Vision يقسّم كل سطر لفقرة عادة).
        table_lines = {}
        for w in table_words:
            table_lines.setdefault(w["top"], []).append(w)

        vision_page = {
            "blocks": [
                {
                    "paragraphs": [paragraph_from(para_words)]
                    + [paragraph_from(line_words) for line_words in table_lines.values()]
                }
            ]
        }
        mock_call_vision_api.return_value = {"fullTextAnnotation": {"pages": [vision_page]}}

        # صفحة fitz حقيقية (فارغة) فقط لتوليد raster حقيقي يمرّ عبر
        # `_compress_image_to_limit` بدون أخطاء — استدعاء Vision API نفسه مموَّه أعلاه.
        real_doc = fitz.open()
        real_page = real_doc.new_page()
        blocks, _images = ingest_module._scanned_page_blocks_vision(real_page)
        real_doc.close()

        table_blocks = [b for b in blocks if b.block_type == BlockType.TABLE]
        paragraph_blocks = [b for b in blocks if b.block_type == BlockType.PARAGRAPH]

        self.assertEqual(len(table_blocks), 1)
        self.assertEqual(table_blocks[0].rows[0], ["Region", "BMD", "T-Score", "Z-Score"])

        # نص الجدول (مثال "BMD") يجب ألا يظهر مكرَّراً كفقرة منفصلة أيضاً.
        paragraph_texts = " ".join(b.text for b in paragraph_blocks)
        self.assertIn("Patient", paragraph_texts)
        self.assertNotIn("T-Score", paragraph_texts)

        # ترتيب القراءة: الفقرة (أعلى الصفحة) يجب أن تسبق الجدول في قائمة blocks.
        self.assertLess(blocks.index(paragraph_blocks[0]), blocks.index(table_blocks[0]))


class TestScannedPageImageExtractionIntegration(unittest.TestCase):
    """اختبار تكامل كامل end-to-end (`extract_document`) لاكتشاف صور داخل صفحة ممسوحة
    بلا أي طبقة نص رقمي: صفحة PDF حقيقية فيها شكلان مرسومان فعلياً (بلا نص PyMuPDF
    قابل للاستخراج، فتُوجَّه لمسار OCR الممسوح)، مع تعليق نصي مموَّه عبر Vision API —
    يجب أن يُستخرَج كل شكل كـImageAsset مستقل، مع Placeholder في مكانه الصحيح بين
    الشكلين والتعليق، تماماً كما يحدث فعلاً للصفحات الرقمية (`_page_images`)."""

    @patch("medical_ocr.ingest._call_vision_api")
    def test_two_drawn_shapes_are_extracted_as_separate_images_with_placeholders(self, mock_call_vision_api):
        with tempfile.TemporaryDirectory() as tmp_dir:
            pdf_path = os.path.join(tmp_dir, "scanned_with_shapes.pdf")
            doc = fitz.open()
            page = doc.new_page(width=595, height=842)

            # شكلان حقيقيان (وليس نصاً) مفصولان بفجوة رأسية حقيقية — بلا أي طبقة نص
            # رقمي في الصفحة كلها، فتُوجَّه حتماً لمسار OCR الممسوح في extract_document.
            shape1 = page.new_shape()
            shape1.draw_rect(fitz.Rect(50, 50, 250, 200))
            shape1.finish(fill=(0.3, 0.3, 0.3), color=(0.3, 0.3, 0.3))
            shape1.commit()

            shape2 = page.new_shape()
            shape2.draw_rect(fitz.Rect(50, 300, 250, 480))
            shape2.finish(fill=(0.5, 0.5, 0.5), color=(0.5, 0.5, 0.5))
            shape2.commit()

            doc.save(pdf_path)
            doc.close()
            self.assertEqual(len(fitz.open(pdf_path)[0].get_text("text").strip()), 0)

            # تعليق نصي (Vision مموَّه) أسفل الشكلين تماماً، بلا أي تداخل معهما —
            # تحقّق أن نسبة تغطية كلماته لا تُسقط أياً من الشكلين من التصنيف كصورة.
            scale = 200 / 72  # dpi=200 الافتراضي في _scanned_page_blocks_vision
            caption_x0, caption_y0 = 50 * scale, 550 * scale
            caption_x1, caption_y1 = 300 * scale, 570 * scale
            vision_page = {
                "blocks": [
                    {
                        "paragraphs": [
                            {
                                "boundingBox": {
                                    "vertices": [
                                        {"x": caption_x0, "y": caption_y0},
                                        {"x": caption_x1, "y": caption_y0},
                                        {"x": caption_x1, "y": caption_y1},
                                        {"x": caption_x0, "y": caption_y1},
                                    ]
                                },
                                "words": [
                                    {
                                        "symbols": [{"text": ch} for ch in "Caption"],
                                        "boundingBox": {
                                            "vertices": [
                                                {"x": caption_x0, "y": caption_y0},
                                                {"x": caption_x1, "y": caption_y0},
                                                {"x": caption_x1, "y": caption_y1},
                                                {"x": caption_x0, "y": caption_y1},
                                            ]
                                        },
                                        "confidence": 0.95,
                                    }
                                ],
                            }
                        ]
                    }
                ]
            }
            mock_call_vision_api.return_value = {"fullTextAnnotation": {"pages": [vision_page]}}

            document = extract_document(pdf_path, file_name="scanned_with_shapes.pdf")

            self.assertEqual(document.pages[0].source, PageSource.SCANNED)
            self.assertEqual(len(document.images), 2)
            self.assertEqual({img.image_id for img in document.images}, {"Image_01", "Image_02"})
            for asset in document.images:
                self.assertEqual(asset.mime_type, "image/png")
                self.assertIsNotNone(asset.bbox)

            texts = [b.text for b in document.pages[0].blocks if b.text]
            self.assertEqual(
                texts,
                ["[Insert Image_01 here]", "[Insert Image_02 here]", "Caption"],
            )


def _text_block(x0, y0, x1, y1, text) -> Block:
    return Block(
        block_type=BlockType.PARAGRAPH,
        text=text,
        bbox=BoundingBox(x0=x0, y0=y0, x1=x1, y1=y1),
        confidence=1.0,
        source_engine=SourceEngine.PYMUPDF,
    )


class TestSortBlocksByPosition(unittest.TestCase):
    """خلل حقيقي مُشخَّص على صفحة حقيقية من كتاب مرجعي أكاديمي (Lindhe's Clinical
    Periodontology، تخطيط عمودين): الفرز بمجرد bbox.y0 يُلحِم فقرة عمود يمين بفقرة
    عمود يسار بنفس الارتفاع تقريباً، فيُنتج ترتيب قراءة مبعثراً يخلط نصفَي الصفحة."""

    def test_single_column_page_keeps_pure_y0_order(self):
        # صفحة تقرير طبي عادية أحادية العمود (نمط كل الاختبارات السابقة في هذا
        # المشروع) — يجب ألا يتغيّر سلوكها إطلاقاً بعد إضافة منطق الأعمدة.
        b1 = _text_block(50, 50, 550, 70, "Patient report header")
        b2 = _text_block(50, 100, 550, 120, "Blood pressure 140/90 mmHg")
        b3 = _text_block(50, 150, 550, 170, "Follow-up in two weeks")

        result = ingest_module._sort_blocks_by_position([b3, b1, b2])

        self.assertEqual([b.text for b in result], [b1.text, b2.text, b3.text])

    def test_two_column_paragraphs_are_read_left_then_right(self):
        # نفس هندسة الخلل الحقيقي: عنوان ممتد (full-width) يعبر خط المنتصف، ثم فقرتا
        # عمودين حقيقيتين بنفس الارتفاع تقريباً (فرق كسري في y0 كما يحدث فعلياً بسبب
        # قياسات الخط) — يجب أن تُقرأ اليسرى كاملة قبل اليمنى، لا بترتيب y0 الخام.
        heading = _text_block(50, 20, 560, 40, "Full width heading")
        left = _text_block(50, 736.22, 290, 800, "LEFT paragraph")
        right = _text_block(310, 736.21, 560, 800, "RIGHT paragraph")  # y0 أصغر قليلاً

        result = ingest_module._sort_blocks_by_position([heading, right, left])

        self.assertEqual([b.text for b in result], [heading.text, left.text, right.text])

    def test_narrow_single_column_blocks_are_not_reordered_as_columns(self):
        # فقرتان ضيقتان (أقصر من عرض الصفحة) لكن **متتاليتان رأسياً بلا تداخل** —
        # عمود واحد فعلي بمحاذاة يسرى، وليس عمودين. يجب أن يبقى ترتيب y0 كما هو.
        b1 = _text_block(50, 50, 300, 70, "Short line one")
        b2 = _text_block(50, 100, 300, 120, "Short line two")

        result = ingest_module._sort_blocks_by_position([b2, b1])

        self.assertEqual([b.text for b in result], [b1.text, b2.text])


if __name__ == "__main__":
    unittest.main()
