# خطة بناء أداة Micro-SaaS لاستخراج OCR طبي هجين (Hybrid Medical OCR)

## 1. الهدف

بناء نظام يستخرج النصوص والجداول من ملفات PDF (نص رقمي أو صفحات ممسوحة ضوئياً) بدقة عالية جداً، مع منع الهلوسة تماماً — موجه للمترجمين الطبيين والأطباء.

## 2. المبدأ الأساسي لمنع الهلوسة

لا يُسمح لأي نموذج لغوي/رؤية (LLM) بتوليد نص من الصفر. كل نموذج يعمل كـ**مُصحِّح** لنص استخرجه محرك OCR كلاسيكي أولاً (Ground Truth Anchor)، وليس كمولّد حر. كل تصحيح يُحفظ مع النص الأصلي لغرض التتبع (audit trail).

## 3. البنية الهندسية (Architecture)

### المرحلة 0 — التصنيف لكل صفحة (Triage)
- فتح الصفحة عبر `PyMuPDF` والتحقق من وجود طبقة نص حقيقية.
- توجيه كل صفحة إلى المسار المناسب (بعض الملفات مختلطة: صفحات رقمية + صفحات ممسوحة).

### المسار أ — PDF رقمي (Digital)
- `PyMuPDF` (fitz): استخراج النص + الإحداثيات (bbox) + الخطوط.
- `pdfplumber` / `Camelot`: استخراج الجداول.
- لا تدخّل لأي LLM هنا (النص موثوق 99%+، أي "تصحيح" هنا خطر هلوسة غير ضروري).

### المسار ب — صفحات ممسوحة ضوئياً (Scanned)
1. **طبقة 1 — Ground Truth:** `PaddleOCR` (+ وحدة `PP-Structure` لاكتشاف الجداول وتفكيكها إلى خلايا). يعطي نص + bbox + درجة ثقة (confidence) لكل كلمة.
2. **طبقة 2 — تحقق مزدوج:** تشغيل `Tesseract` كمحرك ثانٍ، ومقارنة (diff) النتيجتين. أي كلمة مختلَف عليها تُعلَّم كمنطقة "منخفضة الاتفاق".
3. **طبقة 3 — Vision LLM كمصحح مقيّد:** تمرير صورة المنطقة المشكوك فيها فقط (crop، وليس الصفحة كاملة) + النص المستخرج إلى نموذج Vision (Claude / GPT-4V) بتعليمات صارمة: تصحيح فقط بناءً على الصورة، عدم إضافة كلمات غير موجودة، وإرجاع `UNCERTAIN` عند عدم اليقين.
4. **الجداول الممسوحة:** اكتشاف بنية الجدول (صفوف/أعمدة) عبر `PP-Structure` أو `Table Transformer (TATR)` أولاً كإحداثيات، ثم تمرير كل خلية للـ OCR بشكل منفصل بدل الجدول ككتلة واحدة.

## 4. مخطط البيانات الموحّد (Unified Schema)

```
Document
 └─ Page (رقم، مصدر: digital/scanned)
     └─ Block (نوع: paragraph / table / heading)
         ├─ text أو rows[][] (للجداول)
         ├─ bbox
         ├─ confidence
         └─ source_engine (pymupdf / paddleocr / llm_corrected)
```

`confidence` و `source_engine` ضروريان لاحقاً لإعطاء أولوية للمراجعة البشرية في المناطق منخفضة الثقة.

## 5. التقطيع (Chunking) قبل DSPy

- التقطيع حسب الوحدة الدلالية (Block)، وليس حسب عدد رموز ثابت.
- كل جدول = chunk واحد كامل (لا يُفصل الرأس عن الصفوف).
- كل فقرة/عنوان = chunk، مع نافذة تداخل (overlap) صغيرة بين الفقرات المتتالية.
- إرفاق بيانات وصفية (رقم الصفحة، confidence، source_engine) كسياق يُمرَّر لموديول DSPy، وليس كنص يُصحَّح.

## 6. تكامل DSPy

- Signature منفصلة لكل مهمة: تصحيح إملاء طبي، ضبط أعمدة/صفوف الجدول، إعادة صياغة — بدون دمجها في موديول واحد.
- ترسيخ (Grounding) تصحيح الإملاء الطبي بمرجع خارجي (قاموس مصطلحات طبية / قائمة أدوية وتشخيصات محلية) عبر بحث تشابه (fuzzy matching أو embeddings) كخطوة Retrieve قبل موديول DSPy.
- استخدام `dspy.Assert` / `dspy.Suggest` كقيود صارمة (مثال: الكلمة المصححة يجب أن تطابق قاموس المصطلحات أو نمط جرعة/رقم معروف) كخط دفاع برمجي، وليس مجرد تعليمة نصية.
- الاحتفاظ بالنص الأصلي (raw) بجانب المُصحَّح لكل تصحيح، لأغراض التتبع (audit trail).

## 7. الاعتماديات المتوقعة (Dependencies)

> **قيد بيئي:** الحساب الحالي على الماك ليس حساب Administrator ولا توجد صلاحيات sudo، لذا لا يمكن تثبيت Homebrew أو أي اعتمادية نظام (system binary مثل `tesseract-ocr` أو `ghostscript`). تم تعديل قائمة الاعتماديات لتعتمد **حصراً على مكتبات Python المُثبَّتة عبر pip** داخل بيئة افتراضية (venv):

- `pymupdf` (fitz) — استخراج نص/جداول PDF الرقمي.
- `pdfplumber` — استخراج نص وجداول PDF الرقمي (بديل بايثون خالص لـ Camelot، لا يحتاج ghostscript). **تم إسقاط `camelot-py`** لأنه يعتمد على ghostscript كاعتمادية نظام غير متاحة.
- `paddleocr` + `paddlepaddle` (يشمل PP-Structure) — محرك OCR الأساسي (الطبقة 1) للصفحات الممسوحة.
- `easyocr` — محرك OCR ثانٍ للتحقق المزدوج (الطبقة 2)، **بديل عن `pytesseract`/`tesseract-ocr`** لأن الأخير يتطلب تثبيت ثنائي نظام غير متاح بدون Homebrew.
- `rapidfuzz` — مقارنة/diff بين نتائج المحركين، وربط تصحيح الإملاء الطبي بالقاموس المرجعي (خطوة Retrieve).
- `dspy-ai`
- ~~`anthropic` — عميل API لنموذج Vision (Claude) كمصحح مقيّد.~~ **استُبدل (اليوم السابع،
  2026-07-13) بـ Gemini عبر Google AI Studio** (مجاني، بدون بطاقة دفع)، يُستدعى مباشرة عبر
  `litellm` (اعتمادية موجودة أصلاً ضمن `dspy-ai`) بصيغة `gemini/gemini-flash-latest` — لا حاجة
  لحزمة SDK منفصلة، وتمت إزالة `anthropic` من `requirements.txt`. راجع
  `medical_ocr/lm_config.py` والقسم 13 (اليوم السابع) للتفاصيل.
- `streamlit` — واجهة المستخدم.
- `opencv-python-headless`, `numpy`, `pandas`, `Pillow` — اعتماديات مساعدة لمحركات OCR ومعالجة الصور.
- قاموس/مرجع مصطلحات طبية (يُحدَّد لاحقاً حسب اللغة المستهدفة)

## 8. خطة التنفيذ المرحلية (خطوات لاحقة، بدون كتابة كود بعد)

1. إعداد بيئة المشروع والاعتماديات.
2. بناء وحدة التصنيف (Triage) لكل صفحة.
3. بناء مسار PDF الرقمي (PyMuPDF + pdfplumber/Camelot).
4. بناء مسار الصفحات الممسوحة (PaddleOCR + Tesseract + مقارنة/diff).
5. دمج طبقة تصحيح Vision LLM المقيّدة بالمناطق منخفضة الثقة فقط.
6. توحيد المخرجات ضمن الـ Schema الموحد.
7. بناء وحدة التقطيع (Chunking) حسب الوحدة الدلالية.
8. تصميم DSPy Signatures والموديولات (تصحيح إملائي، ضبط جداول، إعادة صياغة) مع قيود Assert/Suggest.
9. اختبار شامل على عينات PDF حقيقية (رقمية + ممسوحة + مختلطة) وقياس الدقة يدوياً.

## 9. الحالة

تم اعتماد هذه الخطة.

- **الخطوة 1 (إعداد البيئة والاعتماديات): مكتملة ✅**
  - تم إنشاء بيئة افتراضية `.venv` (Python 3.9.6، النظامي على macOS arm64).
  - تم تثبيت جميع الاعتماديات (`requirements.txt`) عبر pip فقط: pymupdf, pdfplumber, paddlepaddle, paddleocr, easyocr, rapidfuzz, dspy-ai, anthropic, streamlit, opencv-python-headless, numpy, pandas, Pillow.
  - تم التحقق عبر اختبار استيراد (smoke test) لجميع المكتبات الحرجة، بما فيها paddleocr و easyocr و torch (مع دعم تسريع MPS على Apple Silicon) و dspy — جميعها تعمل دون أخطاء.
  - **لم يتم** تثبيت Homebrew/tesseract-ocr/ghostscript بسبب غياب صلاحيات الإدارة على الحساب؛ تم استبدالها ببدائل بايثون خالصة (انظر قسم 7).
- الخطوات 2-5 (Triage، مسار PDF الرقمي، مسار الصفحات الممسوحة، طبقة Vision LLM) لم تبدأ بعد.
- **الخطوات 6 و7 (Schema الموحّد + Chunking) وجزء من الخطوة 8 (أول Signature-ين لـ DSPy): مكتملة ✅** (اليوم الثالث، المرحلة 2)
  - `medical_ocr/schema.py`: نماذج Pydantic لـ `Document`/`Page`/`Block` مطابقة للمخطط الموحّد في القسم 4 (نوع الـ block، bbox، confidence، source_engine)، مع حفظ `raw_text`/`raw_rows` تلقائياً كنسخة تدقيق (audit trail) قبل أي تصحيح لاحق. تتضمن محوّلات (adapters) من المخرجات الخام الفعلية لـ `pymupdf` (span dict)، `pdfplumber` (extract_table)، و`paddleocr` (نتيجة سطر واحد).
  - `medical_ocr/chunking.py`: `chunk_document()` يقطّع حسب الوحدة الدلالية (Block) وليس عدد رموز ثابت — الجدول = chunk كامل غير مجزّأ، والفقرات تحمل نافذة تداخل (`context_before`) كحقل ميتاداتا منفصل لا يُدمج داخل النص المراد تصحيحه.
  - `medical_ocr/terminology.py`: `MedicalTerminologyRetriever` — خطوة Retrieve عبر `rapidfuzz` فوق قاموس محلي (`data/medical_terms_sample.txt`، **عيّنة مؤقتة placeholder** يجب استبدالها بمرجع رسمي حسب اللغة المستهدفة لاحقاً، انظر القسم 7).
  - `medical_ocr/signatures/spelling.py`: أول `dspy.Signature` (`MedicalSpellingCorrection`) + موديول `MedicalSpellingCorrector` يستدعي خطوة Retrieve قبل الموديل، ويطبّق `dspy.Suggest` كقيد ترسيخ برمجي فعلي (`is_correction_grounded`: يرفض إعادة صياغة كاملة أو إضافة كلمات كثيرة) بدل الاكتفاء بتعليمة نصية.
  - `medical_ocr/signatures/tables.py`: ثاني `dspy.Signature` (`MedicalTableStructuring`) + موديول `MedicalTableStructurer` يفرض `dspy.Suggest` على الحفاظ على عدد الصفوف (`row_count_preserved`) لمنع دمج/حذف صفوف الجدول.
  - اختبارات دخان (`tests/`، عبر `unittest` القياسية بدون pytest) تغطي schema/chunking/terminology وبنية الـ Signatures والقيود البرمجية الصرفة، دون أي استدعاء LM حقيقي (29 اختباراً، كلها ناجحة).
  - **لم يبدأ بعد (حتى نهاية اليوم الثالث):** دمج `dspy.Assert` (وليس `Suggest` فقط)، تكوين LM فعلي عبر `anthropic`/`litellm`، بناء موديول "إعادة الصياغة" الثالث المذكور في القسم 6، وربط كل هذا بمخرجات حقيقية من خطوات Triage/الاستخراج (لأنها لم تُبنَ بعد).
- باقي بنود الخطوة 8 وكذلك الخطوة 9 (الاختبار الشامل على عينات PDF حقيقية) لم تبدأ بعد.

## 11. اليوم الخامس — ضبط الجودة (QA): بناء Gold Dataset وتقييم كمّي ومُحسِّن DSPy

- **Gold Dataset (مكتمل بنيوياً ✅):** 15 عيّنة طبية "صعبة" منسّقة (10 حالات مصطلحات في `data/gold/terminology.json` + 5 حالات جداول في `data/gold/tables.json`)، كل عيّنة تحمل مخرجات مرجعية (`expected_correction_terms`, `forbidden_terms`, `expected_uncertain_terms` للمصطلحات؛ `expected_row_values`, `expected_uncertain_row_indices` للجداول) — بخلاف `medical_ocr/eval_cases.py` (اليوم الرابع) الذي يحمل raw+note للمراجعة اليدوية فقط دون مخرجات مرجعية قابلة للقياس البرمجي.
  - الحالات مصمَّمة عمداً لتغطي: التباس أسماء أدوية بالسياق السريري، خلط حرف/رقم (OCR شائع في النصوص المكتوبة بخط اليد)، كلمات غير مقروءة يجب أن تُعامَل كـ`UNCERTAIN` بدل اختراعها، أسماء تجارية غير موجودة بالقاموس المرجعي (اختبار ضبط النفس عن الاستبدال التلقائي)، أرقام جرعة واضحة لكن غير معتادة سريرياً (اختبار عدم "تصحيح" رقم واضح بحجة أنه غير مألوف)، رؤوس جداول مقسومة على أسطر بخلايا مدمجة، انزياح أعمدة، صفوف متشابهة ظاهرياً يجب ألا تُدمَج، وجداول بلا `column_hints` إطلاقاً.
  - `medical_ocr/gold_dataset.py`: نماذج Pydantic (`GoldTerminologyExample`, `GoldTableExample`, `GoldDataset`) + `load_gold_dataset()`، بالإضافة إلى `build_terminology_devset()`/`build_table_devset()` اللتين تحوّلان كل عيّنة إلى `dspy.Example` (مع `with_inputs` محدَّدة بدقة) لإعادة استخدامهما من كل من سكربت التقييم والمُحسِّن دون ازدواج منطق التحويل.
- **سكربت التقييم الكمّي (مكتمل بنيوياً ✅، بانتظار مفتاح API):** `scripts/evaluate_gold.py` يشغّل الموديولين عبر `dspy.Evaluate` ضد Gold Dataset كاملاً، ويطبع/يحفظ درجة إجمالية لكل مجموعة بالإضافة إلى تفصيل لكل عيّنة (raw/prediction/score) في `scripts/gold_eval_results.json` — أول قياس كمّي فعلي للدقة في المشروع، بخلاف اليوم الرابع الذي اكتفى بمراجعة يدوية لحقل reasoning.
- **دوال القياس (مكتمل ✅):** `medical_ocr/gold_metrics.py` (`terminology_metric`, `table_metric`) — مشتركة بين سكربتي التقييم والتحسين لتفادي الازدواج. تطبّق نفس مبدأ منع الهلوسة من القسم 2 كبوابة صارمة أولاً (`is_correction_grounded`/`row_count_preserved` يُفشلان الدرجة كاملة إلى 0.0)، ثم درجة جزئية عبر fuzzy matching (`rapidfuzz.fuzz.partial_ratio`) لمطابقة المصطلحات/الخلايا المتوقعة، وليس مطابقة نصية حرفية.
- **مُحسِّن DSPy (Optimizer) (مكتمل بنيوياً ✅، بانتظار مفتاح API):** `scripts/optimize_modules.py` يستخدم `dspy.teleprompt.BootstrapFewShot` (وليس `MIPROv2`، غير مناسب لحجم بيانات 15 عيّنة فقط) لتوليد demos تلقائياً من قسم train في كل مجموعة (7/3 للمصطلحات، 3/2 للجداول — تقسيم train/dev صارم لتفادي تسريب البيانات)، مع `metric_threshold=0.8` على نفس دوال `gold_metrics`. يقيس درجة dev قبل/بعد التحسين للمقارنة، ويحفظ حالة الموديولين المُحسَّنين (demos فقط، JSON عبر `Module.save(save_program=False)`) تحت `scripts/optimized/`.
  - تحقّق بنيوي (بدون LM): `named_predictors()` على كلا الموديولين يكتشف الـ predictor الداخلي بشكل صحيح رغم التغليف بـ `dspy.Refine` (`correct.module.predict` / `structure.module.predict`)، و`reset_copy()`/`deepcopy()` يعملان دون أخطاء — شرط أساسي لعمل `BootstrapFewShot.compile()`.
- اختبارات دخان جديدة بدون LM حقيقي (`tests/test_gold_dataset.py`, `tests/test_gold_metrics.py`) — المجموع الآن 50 اختباراً، كلها ناجحة.
- **التالي:** بمجرد توفر مفتاح Anthropic API، تشغيل `scripts/evaluate_gold.py` (خط أساس قبل أي تحسين) ثم `scripts/optimize_modules.py`، ومراجعة الفرق قبل/بعد يدوياً. لاحقاً: توسعة القاموس المرجعي (لا يزال placeholder، القسم 7) والنظر في زيادة حجم Gold Dataset إن أظهر التقييم الأول ثغرات غير مغطاة.

## 12. اليوم السادس — المرحلة 4: تنظيف الكود، معالجة الأخطاء، وحماية الرموز الطبية من الهلوسة

- **ثغرة حماية مكتشفة ومُعالَجة (الأهم ✅):** بوابتا الترسيخ القائمتين حتى نهاية اليوم الخامس
  (`is_correction_grounded` عبر تشابه نصي كلي + فرق عدد كلمات، و`row_count_preserved` عبر عدد
  الصفوف فقط) لا تلتقطان تحديداً حالة تغيّر **رقم صريح واحد** (جرعة دواء أو قيمة مخبرية) بينما
  يبقى التشابه العام أو عدد الصفوف/الكلمات كما هو — وهي بالضبط أخطر أنواع الهلوسة الطبية (مثال:
  عيّنة Gold Dataset الحالية "850 ملغ" كانت تُقاس بدرجة جزئية فقط، لا كبوابة رفض صارمة).
  - `medical_ocr/numeric_guard.py` (جديد): `extract_pure_numbers()` — يستخرج الأرقام الصريحة
    الصرفة فقط (محاطة بحدود غير حرفية)، ويستثني عمداً رموز التباس OCR الملتصقة بحروف (مثل
    `2O`، `5oo`) لأن تصحيح تلك إلى رقم مقروء هو الغرض المقصود من الموديولات وليس هلوسة. وحدة
    مشتركة بين مسار النص والجداول لتفادي ازدواج نفس التعريف الدقيق لـ"الرقم الصريح".
  - `medical_ocr/signatures/spelling.py`: `numeric_tokens_preserved()` بوابة جديدة مدموجة داخل
    `is_correction_grounded` (تُستخدم تلقائياً في كل من `dspy.Refine` أثناء التوليد وفي
    `gold_metrics.terminology_metric` أثناء التقييم) — ترفض أي تصحيح يُغيّر رقماً صريحاً
    موجوداً في raw_text دون منع تصحيح التباس حرف/رقم.
  - `medical_ocr/signatures/tables.py`: `row_values_grounded()` بوابة جديدة (تُستخدم في
    `table_row_count_reward` بدل `row_count_preserved` وحدها) تمنع تغيير قيمة رقمية واضحة ضمن
    صف لم يُعلَّم UNCERTAIN بالكامل؛ الصفوف التي تحوي أي خلية UNCERTAIN تُستثنى عمداً من هذا
    الفحص لأن فقدان الرقم الأصلي فيها متوقَّع ومقصود. أُضيفت أيضاً `structured_row_text()`
    (نقل منطق تحويل صف مُهيكَل إلى نص واحد، كان مكرَّراً مرتين داخل `gold_metrics.py`).
  - `medical_ocr/gold_metrics.py`: `table_metric` يستخدم الآن `row_values_grounded` كبوابة
    صارمة بدل `row_count_preserved` وحدها؛ `terminology_metric` يستفيد تلقائياً من التحديث عبر
    `is_correction_grounded`.
- **معالجة الأخطاء (مكتمل ✅):**
  - `medical_ocr/terminology.py::MedicalTerminologyRetriever.from_file`: رسالة خطأ عربية واضحة
    (تتضمن المسار الكامل وإشارة لقسم 7 من الخطة) بدل `FileNotFoundError` عام غير مفسَّر.
  - `medical_ocr/gold_dataset.py::load_gold_dataset`: دالة `_load_json_file` داخلية مشتركة —
    رسالة واضحة تحدد أي ملف من الاثنين (`terminology.json`/`tables.json`) مفقود أو تالف
    (`json.JSONDecodeError` تُعاد كـ `ValueError` برسالة تحدد اسم الملف).
- **تنظيف الكود (مكتمل ✅):**
  - مسار قاموس المصطلحات (`data/medical_terms_sample.txt`) كان مُعرَّفاً بصيغ مختلفة مكرَّرة
    أربع مرات (`medical_ocr/api/dependencies.py` وثلاث سكربتات تحت `scripts/`) — تم توحيده في
    ثابت واحد `medical_ocr.terminology.DEFAULT_TERMS_PATH` واستُبدلت كل نسخة مكرَّرة به.
  - إزالة ازدواج تحويل "صف مُهيكَل -> نص" (كان مكرَّراً حرفياً مرتين في `gold_metrics.py`) عبر
    `structured_row_text()` المشتركة في `medical_ocr/signatures/tables.py`.
  - تحقّق يدوي (بحث AST عن الاستيرادات غير المستخدمة) على كل الملفات المعدَّلة: لا استيرادات
    ميتة متبقية.
- **اختبارات جديدة (مكتمل ✅):** `tests/test_numeric_guard.py` (جديد)، إضافات إلى
  `tests/test_signatures.py` (`TestNumericTokensPreserved`، `TestRowValuesGrounded`، حالتا
  تغيّر جرعة صريحة/عدم منع تصحيح OCR رقمي داخل `TestIsCorrectionGrounded`)، إضافات إلى
  `tests/test_gold_metrics.py` (حالة هلوسة جرعة نصية وحالة هلوسة قيمة مخبرية جدولية)، وإضافات
  إلى `tests/test_terminology.py`/`tests/test_gold_dataset.py` لرسائل الخطأ الجديدة عند غياب
  الملفات. المجموع الآن 68 اختباراً (كان 50 نهاية اليوم الخامس)، كلها ناجحة، دون أي LM حقيقي.
- **مراجعة شاملة (مكتمل ✅):** قراءة كاملة لكل ملفات `medical_ocr/`، `medical_ocr/api/`،
  `scripts/`، و`tests/` (لا يوجد diff غير ملتزم قبل البدء — الفرع نظيف)، للتحقق من الربط
  الشامل بين وحدات Schema/Chunking/Terminology/Signatures/Gold Dataset/Metrics/API. لم تُكتشف
  ثغرات إضافية بخلاف بوابة الأرقام أعلاه.
- **التالي:** بمجرد توفر مفتاح Anthropic API لأول مرة — تشغيل `scripts/run_hard_cases.py` ثم
  `scripts/evaluate_gold.py` (خط الأساس الحقيقي الأول، والآن مع بوابة حماية الأرقام الجديدة)
  ثم `scripts/optimize_modules.py`، ومراجعة الفرق قبل/بعد. توسعة القاموس المرجعي (لا يزال
  عيّنة مؤقتة) تبقى بنداً مفتوحاً كما في القسم 7.

## 13. اليوم السابع — التجهيز للإطلاق التجريبي وحفظ الشغل

- **ربط المشروع بحساب GitHub (مكتمل ✅):** لم يتوفر `gh` CLI عبر Homebrew (لا صلاحيات sudo)،
  فتم تنزيل ثنائي `gh` المحمول رسمياً (v2.96.0, macOS arm64) إلى `~/.local/bin` بدون أي
  تثبيت على مستوى النظام. تسجيل الدخول تم عبر device flow في المتصفح (لا كلمات مرور/توكن
  مكتوبة يدوياً). تم إنشاء مستودع **خاص (private)** `rabueshy-blip/medical-ocr-saas` وربطه
  كـ `origin`.
- **فحص أمني قبل الرفع (مكتمل ✅):** بحث نصي شامل عن أنماط مفاتيح Anthropic (`sk-ant-`) وأي
  إشارة لـ `ANTHROPIC_API_KEY=` داخل كل الملفات المُتتبَّعة — كل النتائج كانت أمثلة توضيحية
  placeholder فقط ضمن رسائل الخطأ/التعليقات (`medical_ocr/lm_config.py`,
  `scripts/run_hard_cases.py`, إلخ)، لا مفتاح حقيقي مسرَّب. `.env` مستثنى مسبقاً في
  `.gitignore` ولم يُنشأ فعلياً بعد في هذه البيئة أصلاً (لا يوجد مفتاح API حتى الآن).
- **توثيق شامل جديد (مكتمل ✅):**
  - `README.md` (جديد): نظرة عامة، المبدأ الأساسي لمنع الهلوسة، **نطاق العرض التجريبي
    الحالي بصراحة** (توضيح أن Triage ومساري استخراج PDF لم يُبنيا بعد — العرض حالياً على
    مستوى API لموديولات التصحيح فقط، وليس "ارفع PDF واحصل على نتيجة")، القيود البيئية،
    تعليمات إعداد محلي وتشغيل اختبارات، تشغيل FastAPI + Swagger UI للعرض، وقسم أمان/خصوصية
    (منع رفع بيانات مرضى حقيقية، التحقق من عدم تسريب مفاتيح).
  - `.env.example` (جديد): مرجع آمن لمتغيرات البيئة المطلوبة (`ANTHROPIC_API_KEY`,
    `MEDICAL_OCR_LM_MODEL`) دون أي قيمة حقيقية.
  - `LICENSE` (جديد): كل الحقوق محفوظة (Proprietary/All Rights Reserved) بدل ترخيص مفتوح
    المصدر — مناسب لمرحلة حماية الكود قبل الإطلاق التجاري، ومتّسق مع كون المستودع خاصاً.
- **التحقق من جاهزية العرض التجريبي (مكتمل ✅):**
  - تشغيل كامل لمجموعة الاختبارات: **69 اختباراً ناجحاً** (تصحيح من رقم 68 المذكور نهاية
    اليوم السادس — الرقم الفعلي بعد تشغيل `unittest discover` هو 69).
  - تشغيل فعلي لخادم `uvicorn` محلياً والتحقق يدوياً عبر `curl` من `GET /health` (يرجع
    `{"status":"ok","lm_configured":false}` بشكل صحيح لغياب المفتاح) و`GET /docs` (200،
    واجهة Swagger تعمل) — ثم إيقاف الخادم. هذا يؤكد أن العرض عبر Swagger UI (بدون كتابة كود)
    ممكن فعلياً أمام الأطباء/المترجمين للنقاط البنيوية، لكن **أي استدعاء فعلي لنقاط
    `/correct-spelling` أو `/structure-table` يبقى مستحيلاً حتى توفر `ANTHROPIC_API_KEY`
    حقيقي** — هذا القيد لم يتغيّر منذ اليوم الرابع.
- **رفع الكود المستقر (مكتمل ✅):** commit جديد يضم التوثيق الجديد، ثم `git push` إلى
  `origin/main` على GitHub.
- **التالي (محدَّث، انظر القسم 14):** الحاجز لم يعد "الحصول على مفتاح Anthropic API" بل
  الحصول على مفتاح Gemini المجاني من Google AI Studio — تم التبديل الكامل للمزوّد بعد نهاية
  اليوم السابع مباشرة (القسم 14) لتفادي الحاجة لمفتاح مدفوع أصلاً.

## 14. ملحق اليوم السابع — التبديل من Anthropic إلى Google Gemini (مجاني)

- **السبب:** لم يتوفر مفتاح Anthropic API مدفوع في هذه البيئة، بينما يوفّر Google AI Studio
  مستوى مجانياً فعلياً (free tier) دون بطاقة دفع — يسمح بتشغيل العرض التجريبي فعلياً ضد LM
  حقيقي بدون تكلفة، وهو ما كان يمنع أي تشغيل حقيقي منذ اليوم الرابع.
- **التغيير التقني (مكتمل ✅):** `dspy.LM` يدعم Gemini مباشرة عبر `litellm` (اعتمادية موجودة
  أصلاً ضمن `dspy-ai`، تم التحقق فعلياً بإنشاء `dspy.LM(...)` بنجاح) —
  **لا حاجة لأي حزمة SDK إضافية**. تم تعديل:
  - `medical_ocr/lm_config.py`: `DEFAULT_MODEL = "gemini/gemini-3-flash-preview"`، والتحقق الآن
    من `GEMINI_API_KEY` (أو `GOOGLE_API_KEY` كبديل يدعمه litellm) بدل `ANTHROPIC_API_KEY`.
    **رحلة اختيار الموديل (اكتُشفت أثناء أول اختبار حي فعلي، كلها بمفتاح صحيح 100% — المشكلة
    في توفر/حصة كل موديل لا في المصادقة):**
    1. `gemini-2.5-flash` (الاختيار الأول) → `404 no longer available to new users`.
    2. `gemini-flash-latest` (اسم مستعار) → عمل، لكنه تبيّن أنه يُحوَّل فعلياً إلى
       `gemini-3.5-flash` بحصة مجانية **20 طلباً/يوم فقط** — استُهلكت بالكامل من أول تشغيل حقيقي
       لـ `scripts/run_hard_cases.py` (كل تصحيح يستهلك حتى 3 طلبات عبر `dspy.Refine`).
    3. `gemini-2.0-flash-lite` → حصة **صفر** لهذا الحساب تحديداً (`limit: 0`، غير مُفعَّل بعد).
    4. `gemini-3-flash-preview` → **نجح فعلياً بالكامل** ضد `scripts/run_hard_cases.py` (3 حالات
       صعبة، تفاصيل كاملة أدناه) وأصبح الافتراضي النهائي.
    الدرس المستفاد: حصص المستوى المجاني في Google AI Studio صغيرة جداً ومختلفة **لكل موديل على
    حدة** لكل حساب جديد، وليست موحّدة — القيمة الافتراضية قابلة للتغيير في أي وقت عبر
    `MEDICAL_OCR_LM_MODEL` في `.env` دون تعديل كود.
  - `medical_ocr/api/lm_guard.py`, `medical_ocr/api/app.py`: رسائل الخطأ/التعليقات محدَّثة لذكر
    `GEMINI_API_KEY`.
  - `requirements.txt`: أُزيلت `anthropic` (لم تكن مستوردة مباشرة في أي مكان من الكود أصلاً —
    تحقّق عبر بحث `import anthropic` لم يُطابق شيئاً).
  - `.env.example`, `README.md`: محدَّثان بالكامل ليذكرا `GEMINI_API_KEY` ورابط الحصول على
    مفتاح مجاني (`https://aistudio.google.com/apikey`) بدل Anthropic.
  - `scripts/run_hard_cases.py`, `scripts/evaluate_gold.py`, `scripts/optimize_modules.py`:
    أسطر الاستخدام التوضيحية محدَّثة إلى `GEMINI_API_KEY=...`.
  - `tests/test_lm_config.py`, `tests/test_api.py`: محدَّثان لتوقّع رسالة `GEMINI_API_KEY` بدل
    `ANTHROPIC_API_KEY` — أُعيد تشغيل كامل مجموعة الاختبارات (69 اختباراً) بعد التعديل وكلها
    ناجحة، دون أي LM حقيقي.
  - القسم 7 أعلاه (الاعتماديات) محدَّث بشطب `anthropic` وتوثيق البديل.
- **لم يتغيّر:** مبدأ منع الهلوسة والبوابات البرمجية (`is_correction_grounded`,
  `row_values_grounded`, `numeric_guard.py`) مستقلة تماماً عن مزوّد LM المستخدم — تعمل بنفس
  الصرامة بغضّ النظر عن كون النموذج Gemini أو Claude.
- **أول تشغيل حقيقي فعلي في تاريخ المشروع (مكتمل ✅، نجاح كامل):** بعد تفعيل مفتاح Gemini
  المجاني وضبط `gemini-3-flash-preview`، تم تشغيل `scripts/run_hard_cases.py` فعلياً ضد LM
  حقيقي لأول مرة. **النتائج الثلاث كلها صحيحة طبياً وملتزمة بمبدأ منع الهلوسة:**
  - `drug_name_ambiguity`: صحّح "ميتفوبرولين" إلى "ميتوبرولول" (حاصر بيتا لضغط الدم/القلب) لا
    "ميتفورمين" (سكري)، بالاعتماد على السياق السريري (ضغط الدم وتسارع النبض) وليس التشابه
    الحرفي الأقرب فقط — نجح الاختبار المصمَّم عمداً لقياس هذا بالضبط.
  - `ocr_digit_letter_confusion`: صحّح "2O" إلى "20" (جرعة أوميبرازول) دون اختلاق رقم غير مرتبط
    بالنص الخام.
  - `multi_level_lab_header`: حافظ على 5 صفوف بالضبط رغم رأس الجدول المقسوم على سطرين، ووضع
    `UNCERTAIN` لقيمة Glucose المفقودة **بدل اختراع رقم** — التحقق العملي الأول لبوابة
    `row_values_grounded`/`numeric_guard.py` ضد LM حقيقي وليس اختبار دخان.
  - النتائج الكاملة (raw/corrected/reasoning) محفوظة في `scripts/hard_case_results.json`
    (غير مُتتبَّع في git — مُستثنى ضمنياً، ملف مخرجات تشغيل لا كود).
  - `dspy.Refine` (N=3 محاولات) لم يحتج لأكثر من محاولة واحدة في أي من الحالات الثلاث — إشارة
    أولية جيدة لجودة الموديول دون الحاجة لإعادة محاولات كثيرة.
- **التالي:** `scripts/evaluate_gold.py` كخط أساس كمّي على كامل Gold Dataset (15 عيّنة) — أكبر
  من 3 حالات فقط، فقد يستهلك حصة يومية أكبر؛ يُفضَّل تنفيذه بعد التأكد من تعافي الحصة اليومية أو
  ضبط `MEDICAL_OCR_LM_MODEL` لموديل بحصة أعلى إن تكرر `429`. بعدها `scripts/optimize_modules.py`.

## 10. اليوم الرابع — دمج موديولات التفكير وتطوير دقة الاستخراج العالية

- **تكوين LM فعلي (جزئياً مكتمل ✅، بانتظار مفتاح API):**
  - `medical_ocr/lm_config.py`: `configure_lm()` يحمّل `.env` (عبر `python-dotenv`، أُضيف إلى `requirements.txt`) ويُعدّ `dspy.settings.configure` بنموذج `dspy.LM("anthropic/claude-sonnet-5")` (قابل للتغيير عبر `MEDICAL_OCR_LM_MODEL`). يرفع `RuntimeError` برسالة عربية واضحة إن غاب `ANTHROPIC_API_KEY` بدل خطأ شبكة غامض من LiteLLM.
  - **لم يُنفَّذ بعد فعلياً ضد LM حقيقي** — لا يوجد مفتاح Anthropic API متاح في هذه البيئة حتى الآن. بمجرد توفره، التشغيل عبر `scripts/run_hard_cases.py`.
- **حالات اختبار "صعبة" لقياس دقة CoT (مكتمل ✅):** `medical_ocr/eval_cases.py` يحوي حالتين للمصطلحات (`drug_name_ambiguity`: كلمة OCR مشوَّهة تتطابق تشابهياً مع دوائين مختلفين "ميتفورمين"/"ميتوبرولول" ويجب حسمها بالسياق السريري لا بالتشابه الحرفي فقط؛ `ocr_digit_letter_confusion`: خلط حرف O بدل الرقم 0 في جرعة دواء) وحالة جدول واحدة (`multi_level_lab_header`: رأس جدول مقسوم على سطرين بسبب خلايا مدمجة + خلية قيمة مفقودة يجب أن تصبح `UNCERTAIN`). أُضيف "ميتوبرولول" إلى `data/medical_terms_sample.txt` لجعل حالة التباس اسم الدواء حقيقية (تحقّق تلقائي في `tests/test_eval_cases.py` أن الكلمة الملتبسة تُطابق فعلاً المصطلحين معاً عبر `retrieve_candidate_terms`).
- **`scripts/run_hard_cases.py` (مكتمل بنيوياً ✅، بانتظار تشغيل فعلي):** يُشغّل `MedicalSpellingCorrector`/`MedicalTableStructurer` على الحالات الصعبة، ويطبع/يحفظ حقل `reasoning` (تفكير CoT الفعلي المُتاح من `dspy.ChainOfThought`، تم التحقق أنه موجود ضمن مخرجات كلا الموديولين) إلى جانب المخرجات، كأول تشغيل حقيقي (وليس بنيوي فقط) لموديولات DSPy في المشروع.
- اختبارات دخان جديدة بدون LM حقيقي (`tests/test_lm_config.py`, `tests/test_eval_cases.py`) — المجموع الآن 33 اختباراً، كلها ناجحة.
- **هيكل FastAPI (مكتمل ✅):** `medical_ocr/api/` (`app.py`, `dependencies.py`, `lm_guard.py`, `schemas.py`, `routers/spelling.py`, `routers/tables.py`) يُغلّف `MedicalSpellingCorrector`/`MedicalTableStructurer` بنقاط API. `lm_guard.py` يمنع الإقلاع من الفشل الصامت إن غاب مفتاح Anthropic API (يرجع 503 بدل خطأ غامض — مغطى باختبارات `tests/test_api.py` بدون LM حقيقي). تم تشغيل السيرفر عبر uvicorn والتحقق يدوياً ثم إغلاقه.
- **التالي:** بمجرد توفر مفتاح Anthropic API، تشغيل `scripts/run_hard_cases.py` فعلياً ومراجعة جودة التفكير/الدقة يدوياً ضد LM حقيقي — لم يحدث بعد.
