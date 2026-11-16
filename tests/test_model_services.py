"""Opt-in real-service checks: RUN_MODEL_INTEGRATION=1 python -m unittest discover -s tests."""
import math
import os
import struct
import unittest
import zlib
from pathlib import Path

from mmkb.providers import ImageInput, build_providers


def solid_png() -> bytes:
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    pixels = (b"\x00" + bytes([220, 30, 30]) * 64) * 64
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 64, 64, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))


@unittest.skipUnless(os.environ.get("RUN_MODEL_INTEGRATION") == "1", "real model requests are opt-in")
class ModelServiceTests(unittest.TestCase):
    def setUp(self):
        self.providers = build_providers()
        self.addCleanup(self.providers.close)
        self.image = ImageInput(solid_png(), "image/png")

    def test_embedding_three_inputs_and_two_purposes(self):
        self.providers.embedding.check_connection()
        for purpose in ("query", "document"):
            for args in ({"text": "A solid red square."}, {"image": self.image},
                         {"text": "A solid red square.", "image": self.image}):
                with self.subTest(purpose=purpose, inputs=list(args)):
                    vector = self.providers.embedding.embed(purpose=purpose, **args)
                    self.assertEqual(len(vector), 2048)
                    self.assertTrue(all(math.isfinite(value) for value in vector))
                    self.assertLess(abs(math.hypot(*vector) - 1), 1e-6)

    def test_vision_smoke(self):
        vision = self.providers.vision
        if vision is None:
            self.skipTest("vision service is disabled")
        vision.check_connection()
        self.assertTrue(vision.caption(self.image))
        self.assertTrue(vision.caption(self.image, structured=True))
        self.assertEqual(vision.ocr(self.image), "")
        self.assertEqual(vision.classify(self.image), "visual_image")

    def test_supplied_visual_samples(self):
        """Optional representative samples; paths stay in local environment."""
        vision = self.providers.vision
        if vision is None:
            self.skipTest("vision service is disabled")
        samples = [("MODEL_TEST_PHOTO", "visual_image"), ("MODEL_TEST_CHART", "structured_image"),
                   ("MODEL_TEST_OCR", "visdoc")]
        if not all(os.environ.get(name) for name, _ in samples):
            self.skipTest("set MODEL_TEST_PHOTO, MODEL_TEST_CHART and MODEL_TEST_OCR to PNG sample paths")
        for name, expected in samples:
            with self.subTest(sample=name):
                image = ImageInput(Path(os.environ[name]).read_bytes(), "image/png")
                self.assertTrue(vision.caption(image, structured=expected == "structured_image"))
                self.assertEqual(vision.classify(image), expected)
                if expected == "visdoc":
                    text = vision.ocr(image)
                    self.assertTrue(text)
                    if os.environ.get("MODEL_TEST_OCR_EXPECTED"):
                        self.assertIn(os.environ["MODEL_TEST_OCR_EXPECTED"], text)


if __name__ == "__main__":
    unittest.main()
