from django.test import TestCase

# Create your tests here.


class PDFExtractionResourceLimitTests(TestCase):
    def test_page_limit_is_checked_before_extraction(self):
        from unittest.mock import patch
        from ingestion.tasks import PDFResourceLimitError, _bounded_join_pages

        class FakePage:
            def extract_text(self):
                raise AssertionError("page extraction must not start above the page limit")

        with patch("ingestion.tasks.MAX_PDF_PAGES", 1), self.assertRaises(PDFResourceLimitError):
            _bounded_join_pages([FakePage(), FakePage()], page_count=2)

    def test_character_limit_is_enforced_during_page_iteration(self):
        from unittest.mock import patch
        from ingestion.tasks import PDFResourceLimitError, _bounded_join_pages

        class FakePage:
            def __init__(self, text):
                self.text = text

            def extract_text(self):
                return self.text

        with patch("ingestion.tasks.MAX_EXTRACTED_CHARACTERS", 5), self.assertRaises(PDFResourceLimitError):
            _bounded_join_pages([FakePage("123"), FakePage("456")], page_count=2)
