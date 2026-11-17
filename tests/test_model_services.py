"""Opt-in real-service checks: RUN_MODEL_INTEGRATION=1 python -m unittest discover -s tests."""
import math
import os
import struct
import unittest
import zlib
from pathlib import Path

from mmkb.providers import ImageInput, build_providers
from mmkb.providers.systemone import ChoiceQuestion, NoulQuestion, ScoreQuestion


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

    def test_decision_smoke(self):
        """One multi-question read; expects a served /v1/systemone model such as vllm-sr/d3-mini."""
        decision = self.providers.decision
        if decision is None:
            self.skipTest("decision service is disabled")
        decision.check_connection()
        result = decision.decide("我的银行卡在同一笔订单里被扣了两次款。", {
            "team": ChoiceQuestion({"billing": "支付与退款", "shipping": "物流配送", "security": "账号登录"}),
            "urgent": NoulQuestion(true="涉及资金损失或账号无法登录"),
            "tone": ScoreQuestion(["平静", "不满", "愤怒"]),
        })
        self.assertEqual(list(result.answers), ["team", "urgent", "tone"])
        self.assertEqual(result["team"].type, "choice")
        self.assertIn(result["team"].value, ("billing", "shipping", "security"))
        self.assertIsInstance(result["urgent"].value, bool)
        self.assertIn(result["tone"].level_name, ("平静", "不满", "愤怒"))
        for answer in result.answers.values():
            self.assertAlmostEqual(math.fsum(answer.probabilities.values()), 1.0, places=6)
            self.assertTrue(0.0 <= answer.confidence <= 1.0)
            self.assertGreater(result.usage.input_tokens, 0)
            self.assertGreaterEqual(result.usage.output_tokens, 0)

    def test_decision_image_input(self):
        decision = self.providers.decision
        if decision is None:
            self.skipTest("decision service is disabled")
        # The Decision 3.0 prompt is fixed, so the question itself goes into the state.
        answer = decision.choose("这张图片属于哪一类？", ["chart", "document", "photo"], images=[self.image])
        self.assertIn(answer.value, ("chart", "document", "photo"))
        self.assertEqual(answer.type, "choice")

    def test_supplied_visual_samples(self):
        """Optional representative samples; paths stay in local environment."""
        vision = self.providers.vision
        if vision is None:
            self.skipTest("vision service is disabled")
        samples = [("MODEL_TEST_PHOTO", "visual_image"), ("MODEL_TEST_CHART", "structured_image"),
                   ("MODEL_TEST_OCR", "info_rich_image")]
        if not all(os.environ.get(name) for name, _ in samples):
            self.skipTest("set MODEL_TEST_PHOTO, MODEL_TEST_CHART and MODEL_TEST_OCR to PNG sample paths")
        for name, expected in samples:
            with self.subTest(sample=name):
                image = ImageInput(Path(os.environ[name]).read_bytes(), "image/png")
                if expected == "info_rich_image":
                    self.assertTrue(vision.caption_info_rich(image))
                else:
                    self.assertTrue(vision.caption(image, structured=expected == "structured_image"))
                self.assertEqual(vision.classify(image), expected)
                if expected == "info_rich_image":
                    text = vision.ocr(image)
                    self.assertTrue(text)
                    if os.environ.get("MODEL_TEST_OCR_EXPECTED"):
                        self.assertIn(os.environ["MODEL_TEST_OCR_EXPECTED"], text)


if __name__ == "__main__":
    unittest.main()
