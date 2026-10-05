"""Pure synthetic sentinel regressions for retained native-capture redaction."""
import importlib.util
from pathlib import Path
import unittest

module_path = Path(__file__).with_name('extract-browser-captures.py')
spec = importlib.util.spec_from_file_location('item6_capture_extractor', module_path)
extractor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(extractor)

SENTINEL = 'synthetic-unredacted-regression-sentinel'

class SecretGuardTest(unittest.TestCase):
    def test_same_line_unredacted_rejected(self):
        with self.assertRaises(RuntimeError):
            extractor.verify_secret_safe('- textbox "API access key": text: ' + SENTINEL)

    def test_multiline_unredacted_rejected(self):
        with self.assertRaises(RuntimeError):
            extractor.verify_secret_safe('- textbox "API access key":\n    - text: ' + SENTINEL + '\n- button "Done"')

    def test_redacted_same_and_multiline_pass(self):
        extractor.verify_secret_safe('- textbox "API access key": text: <redacted>')
        extractor.verify_secret_safe('- textbox "API access key":\n    - text: <redacted>\n- button "Done"')

    def test_no_value_and_sibling_text_pass(self):
        extractor.verify_secret_safe('- textbox "API access key"\n- paragraph:\n    - text: Public application explanation')
        extractor.verify_secret_safe('- textbox "API access key":\n    - text: \n- button "Done"')

if __name__ == '__main__':
    unittest.main()
