import base64
import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image


def load_module():
    path = Path(__file__).parents[1] / "scripts" / "openai_image.py"
    spec = importlib.util.spec_from_file_location("openai_image", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


image_api = load_module()


class OpenAiImageTests(unittest.TestCase):
    def test_parse_size_and_api_orientation_mapping(self):
        self.assertEqual(image_api._parse_size("720x1280"), (720, 1280))
        self.assertEqual(image_api._api_size_for(720, 1280), "1024x1536")
        self.assertEqual(image_api._api_size_for(1280, 720), "1536x1024")
        self.assertEqual(image_api._api_size_for(1024, 1024), "1024x1024")

    def test_generate_defaults_match_live_tested_request(self):
        args = image_api._build_parser().parse_args(
            [
                "generate",
                "--prompt",
                "test",
                "--reference",
                "/tmp/reference.png",
                "--output-dir",
                "/tmp/output",
            ]
        )
        self.assertEqual(args.model, "gpt-image-2")
        self.assertEqual(args.size, "720x1280")
        self.assertEqual(args.quality, "low")
        self.assertEqual(args.count, 1)

    def test_prepare_reference_letterboxes_without_changing_target_ratio(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.png"
            prepared = Path(temporary) / "prepared.png"
            Image.new("RGB", (400, 300), "red").save(source)

            image_api._prepare_reference(source, prepared, (720, 1280))

            with Image.open(prepared) as result:
                self.assertEqual(result.size, (720, 1280))
                self.assertEqual(result.getpixel((0, 0)), (255, 255, 255))
                self.assertEqual(result.getpixel((360, 640)), (255, 0, 0))

    def test_normalize_output_accepts_near_ratio_and_writes_exact_size(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "output.png"
            buffer = io.BytesIO()
            Image.new("RGB", (941, 1672), "blue").save(buffer, format="PNG")

            source_size = image_api._normalize_output(
                buffer.getvalue(), output, (720, 1280)
            )

            self.assertEqual(source_size, (941, 1672))
            with Image.open(output) as result:
                self.assertEqual(result.size, (720, 1280))

    def test_normalize_output_rejects_wrong_ratio(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "output.png"
            buffer = io.BytesIO()
            Image.new("RGB", (1024, 1024), "blue").save(buffer, format="PNG")

            with self.assertRaisesRegex(image_api.ImageApiError, "比例不符合"):
                image_api._normalize_output(buffer.getvalue(), output, (720, 1280))

    def test_response_prefers_base64_content(self):
        class Item:
            b64_json = base64.b64encode(b"image-bytes").decode("ascii")
            url = None

        self.assertEqual(
            image_api._response_image_bytes(Item(), 1), b"image-bytes"
        )

    def test_response_rejects_invalid_base64(self):
        class Item:
            b64_json = "not-valid-base64***"
            url = None

        with self.assertRaisesRegex(image_api.ImageApiError, "无效的 base64"):
            image_api._response_image_bytes(Item(), 1)


if __name__ == "__main__":
    unittest.main()
