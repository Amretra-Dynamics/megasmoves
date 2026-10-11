import tempfile
import unittest
from pathlib import Path

from megas_community.bounded_io import read_bounded_bytes, staged_bounded_import
from megas_community.secret_policy import is_sensitive_env_key
from megas_community.terminal_parse import (
    StreamLineBuffer,
    detect_completion_in_text,
    extract_assistant_answer,
)


class UtilityTests(unittest.TestCase):
    def test_bounded_file_read_and_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bundle.json"
            path.write_bytes(b'{"safe":true}')
            self.assertEqual(read_bounded_bytes(path, max_bytes=100), b'{"safe":true}')
            with self.assertRaises(ValueError):
                read_bounded_bytes(path, max_bytes=5)
            with staged_bounded_import(path) as staged:
                self.assertEqual(staged.read_bytes(), path.read_bytes())
            self.assertFalse(staged.exists())

    def test_sensitive_environment_names(self):
        self.assertTrue(is_sensitive_env_key("OPENAI_API_KEY"))
        self.assertTrue(is_sensitive_env_key("ACCESS_TOKEN"))
        self.assertTrue(is_sensitive_env_key("CLIENT_SECRET"))
        self.assertFalse(is_sensitive_env_key("PORT"))
        self.assertFalse(is_sensitive_env_key("PATH"))

    def test_completion_and_stream_buffering(self):
        buffer = StreamLineBuffer()
        self.assertEqual(buffer.feed("hel"), [])
        self.assertEqual(buffer.feed("lo\nmore"), ["hello\n"])
        self.assertEqual(buffer.flush(), ["more\n"])
        hit = detect_completion_in_text("Task abc has completed with exit code 0.")
        self.assertIsNotNone(hit)
        self.assertEqual(hit.exit_code, 0)

    def test_extract_answer(self):
        raw = "Task abc has completed with exit code 0.\nStdout:\nDone safely.\nStderr:\n"
        self.assertEqual(extract_assistant_answer(raw), "Done safely.")


if __name__ == "__main__":
    unittest.main()
