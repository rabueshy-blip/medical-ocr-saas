import unittest
from pathlib import Path

from medical_ocr.terminology import MedicalTerminologyRetriever

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "medical_terms_sample.txt"


class TestMedicalTerminologyRetriever(unittest.TestCase):
    def setUp(self):
        self.retriever = MedicalTerminologyRetriever.from_file(DATA_PATH)

    def test_loads_entries_from_sample_file(self):
        self.assertGreater(len(self.retriever._choices), 0)

    def test_suggest_finds_close_misspelling(self):
        # "ميتفورمين" مع خطأ إملائي بسيط (حذف حرف)
        matches = self.retriever.suggest("ميتفورمن")
        terms = [m.term for m in matches]
        self.assertIn("ميتفورمين", terms)

    def test_suggest_returns_empty_for_unrelated_word(self):
        matches = self.retriever.suggest("xyz123nonsense", score_cutoff=95.0)
        self.assertEqual(matches, [])

    def test_suggest_handles_empty_word(self):
        self.assertEqual(self.retriever.suggest(""), [])


if __name__ == "__main__":
    unittest.main()
