import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dotenv import load_dotenv

from mmkb.config import ServiceConfig, load_model_config, load_retrieval_config


class ModelConfigTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "EMBEDDING_BASE_URL": "https://model.example/v1", "EMBEDDING_MODEL": "test-model",
        }, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        mock = patch("mmkb.config.load_dotenv")
        self.load_env = mock.start()
        self.addCleanup(mock.stop)

    def test_defaults_and_existing_retrieval_interface(self):
        config = load_model_config()
        self.assertIsNone(config.caption)
        self.assertEqual(config.embedding.timeout_seconds, 60)
        retrieval = load_retrieval_config()
        self.assertEqual(retrieval.embedding_base_url, config.embedding.base_url)
        self.assertEqual(retrieval.embedding_model, config.embedding.model)
        self.assertEqual((retrieval.dimensions, retrieval.top_k, retrieval.lexical_assist_weight), (2048, 20, 0.1))

    def test_vision_configuration_and_auth(self):
        os.environ.update(CAPTION_BASE_URL="https://vision.example/v1/", CAPTION_MODEL="vision-model",
                          CAPTION_API_KEY="caption-key", EMBEDDING_API_KEY="embedding-key")
        config = load_model_config()
        self.assertEqual(config.caption.timeout_seconds, 120)
        self.assertEqual(config.caption.api_key, "caption-key")
        self.assertEqual(config.embedding.api_key, "embedding-key")
        os.environ["CAPTION_TIMEOUT_SECONDS"] = "25"
        self.assertEqual(load_model_config().caption.timeout_seconds, 25)

    def test_decision_configuration_is_optional(self):
        self.assertIsNone(load_model_config().decision)
        self.assertEqual(load_model_config().decision_profile, "auto")
        os.environ.update(DECISION_BASE_URL="http://localhost:8004/v1", DECISION_MODEL="vllm-sr/d3-mini",
                          DECISION_API_KEY="decision-key")
        decision = load_model_config().decision
        self.assertEqual((decision.base_url, decision.model), ("http://localhost:8004/v1", "vllm-sr/d3-mini"))
        self.assertEqual((decision.api_key, decision.timeout_seconds), ("decision-key", 60.0))
        os.environ["DECISION_TIMEOUT_SECONDS"] = "15"
        self.assertEqual(load_model_config().decision.timeout_seconds, 15)

    def test_decision_profile(self):
        os.environ.update(DECISION_BASE_URL="http://localhost:8004/v1", DECISION_MODEL="vllm-sr/d3-mini")
        for value, expected in (("", "auto"), ("auto", "auto"), ("D3", "d3"), (" CLEF ", "clef"), (" generic ", "generic")):
            with self.subTest(value=value):
                os.environ["DECISION_MODEL_PROFILE"] = value
                self.assertEqual(load_model_config().decision_profile, expected)
        for value in ("omni", "decision-3"):
            with self.subTest(value=value), patch.dict(os.environ, {"DECISION_MODEL_PROFILE": value}):
                with self.assertRaisesRegex(ValueError, "DECISION_MODEL_PROFILE"):
                    load_model_config()

    def test_partial_or_missing_configuration(self):
        for variable in ("EMBEDDING_BASE_URL", "EMBEDDING_MODEL"):
            with self.subTest(variable=variable), patch.dict(os.environ, {variable: " "}):
                with self.assertRaisesRegex(ValueError, variable):
                    load_model_config()
        for prefix in ("CAPTION", "DECISION"):
            for variable in (f"{prefix}_BASE_URL", f"{prefix}_MODEL"):
                with self.subTest(variable=variable), patch.dict(os.environ, {variable: "configured"}):
                    with self.assertRaises(ValueError):
                        load_model_config()

    def test_dimensions_and_timeouts(self):
        for value in ("1024", "bad"):
            with patch.dict(os.environ, {"EMBEDDING_DIMENSIONS": value}):
                with self.assertRaises(ValueError):
                    load_model_config()
        for value in ("0", "-1", "nan", "inf", "bad", ""):
            with patch.dict(os.environ, {"EMBEDDING_TIMEOUT_SECONDS": value}):
                with self.assertRaises(ValueError):
                    load_model_config()

    def test_invalid_api_roots(self):
        for url in ("", "model.example/v1", "ftp://model.example", "https://model.example/?key=x", "https://model.example/#fragment"):
            with self.assertRaises(ValueError):
                ServiceConfig(url, "test-model")

    def test_environment_takes_precedence_over_dotenv(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env.local"
            path.write_text("EMBEDDING_BASE_URL=https://file.example/v1\nEMBEDDING_MODEL=file-model\nCAPTION_BASE_URL=https://vision.example/v1\nCAPTION_MODEL=file-vision\n")
            self.load_env.side_effect = lambda ignored, override: load_dotenv(path, override=override)
            config = load_model_config()
            self.assertEqual(config.embedding.model, "test-model")
            self.assertEqual(config.embedding.base_url, "https://model.example/v1")
            self.assertEqual(config.caption.model, "file-vision")
            self.assertFalse(self.load_env.call_args.kwargs["override"])


if __name__ == "__main__":
    unittest.main()
