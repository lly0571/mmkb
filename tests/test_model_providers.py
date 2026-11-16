import base64
import json
import math
import unittest

import httpx

from mmkb.config import ModelConfig, ServiceConfig
from mmkb.providers import EmbeddingProvider, ImageInput, ProviderError, VisionProvider, build_providers
from mmkb.providers.embedding import INSTRUCTIONS


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.config = ServiceConfig("https://model.example/api/v1/", "test-model")
        self.image = ImageInput(b"test image bytes", "image/png")
        self.requests = []

    def client(self, response=None, *, handler=None):
        def handle(request):
            self.requests.append(request)
            return handler(request) if handler else httpx.Response(200, json=response)
        client = httpx.Client(transport=httpx.MockTransport(handle))
        self.addCleanup(client.close)
        return client

    def assert_error(self, code, callback):
        with self.assertRaises(ProviderError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def embedding(self, vector=None):
        vector = vector if vector is not None else [3, 4] + [0] * 2046
        return EmbeddingProvider(self.config, client=self.client({"data": [{"embedding": vector}]}))

    def vision(self, content="hello", finish_reason="stop"):
        return VisionProvider(self.config, client=self.client({"choices": [{
            "message": {"content": content}, "finish_reason": finish_reason,
        }]}))

    def test_three_input_kinds_and_two_purposes(self):
        for purpose in ("query", "document"):
            for args in ({"text": "  original text  "}, {"image": self.image}, {"text": "配文", "image": self.image}):
                with self.subTest(purpose=purpose, args=list(args)):
                    before = len(self.requests)
                    vector = self.embedding().embed(purpose=purpose, **args)
                    self.assertEqual(len(self.requests), before + 1)
                    self.assertEqual(len(vector), 2048)
                    self.assertAlmostEqual(math.hypot(*vector), 1)
                    self.assertAlmostEqual(vector[0], 0.6)
                    request = self.requests[-1]
                    self.assertEqual(str(request.url), "https://model.example/api/v1/embeddings")
                    body = json.loads(request.content)
                    self.assertNotIn("input", body)
                    self.assertNotIn("truncate_prompt_tokens", body)
                    self.assertEqual(body["dimensions"], 2048)
                    self.assertTrue(body["add_generation_prompt"])
                    self.assertEqual(body["messages"][0]["content"], INSTRUCTIONS[purpose])
                    content = body["messages"][1]["content"]
                    self.assertEqual(len(content), len(args))
                    if "text" in args:
                        self.assertEqual(content[-1], {"type": "text", "text": args["text"]})
                    if "image" in args:
                        url = content[0]["image_url"]["url"]
                        self.assertEqual(base64.b64decode(url.split(",", 1)[1]), self.image.data)

    def test_invalid_inputs_do_not_send_requests(self):
        provider = self.embedding()
        for args in ({}, {"text": ""}, {"text": " \n"}, {"text": 123},
                     {"text": "", "image": self.image},
                     {"image": ImageInput(b"", "image/png")},
                     {"image": ImageInput(b"x", "image/gif")}, {"image": b"bytes"}):
            with self.subTest(args=args):
                self.assert_error("invalid_input", lambda: provider.embed(purpose="query", **args))
        self.assert_error("invalid_input", lambda: provider.embed(purpose="wrong", text="x"))
        self.assertEqual(self.requests, [])

    def test_supported_image_mime_types_preserve_bytes(self):
        for mime in ("image/png", "image/jpeg", "image/webp"):
            self.embedding().embed(purpose="document", image=ImageInput(b"original bytes", mime))
            part = json.loads(self.requests[-1].content)["messages"][1]["content"][0]
            url = part["image_url"]["url"]
            self.assertTrue(url.startswith(f"data:{mime};base64,"))
            self.assertEqual(base64.b64decode(url.split(",", 1)[1]), b"original bytes")

    def test_invalid_embedding_responses(self):
        vectors = [[1] * 2047, [1] * 2049, [0] * 2048,
                   [True] * 2048, ["1"] * 2048, [None] * 2048,
                   [float("nan")] * 2048, [float("inf")] * 2048,
                   [10 ** 400] * 2048, {"embedding": [1] * 2048}]
        for vector in vectors:
            with self.subTest(type=type(vector), first=repr(vector)[:30]):
                # HTTPX's JSON serializer rejects NaN; simulate the raw service instead.
                client = self.client(handler=lambda req: httpx.Response(
                    200, content=json.dumps({"data": [{"embedding": vector}]}).encode(),
                ))
                provider = EmbeddingProvider(self.config, client=client)
                self.assert_error("invalid_response", lambda: provider.embed(purpose="document", text="x"))
        for response in ({}, {"data": []}, {"data": [None]}, {"data": [{}, {}]}, {"data": "bad"}):
            provider = EmbeddingProvider(self.config, client=self.client(response))
            self.assert_error("invalid_response", lambda: provider.embed(purpose="document", text="x"))

    def test_normalization_handles_extreme_finite_values(self):
        for value in (1e308, 1e-308, -1e308):
            vector = self.embedding([value] * 2048).embed(purpose="document", text="x")
            self.assertTrue(all(math.isfinite(v) for v in vector))
            self.assertAlmostEqual(math.hypot(*vector), 1)

    def test_auth_headers_and_timeout(self):
        for key in ("", "private-test-key"):
            config = ServiceConfig(self.config.base_url, "test-model", key, 17)
            client = self.client({"data": [{"embedding": [1] * 2048}]})
            EmbeddingProvider(config, client=client).embed(purpose="query", text="x")
            request = self.requests[-1]
            self.assertEqual(request.headers.get("authorization"), f"Bearer {key}" if key else None)
            self.assertEqual(request.extensions["timeout"], {"connect": 5.0, "read": 17, "write": 17, "pool": 17})
            self.assertNotIn("private-test-key", repr(config))

    def test_transport_errors_and_no_retry(self):
        for exception, code in ((httpx.ReadTimeout, "timeout"), (httpx.ConnectTimeout, "timeout"),
                                (httpx.ConnectError, "connection_error")):
            def handle(request):
                raise exception("private remote content", request=request)
            provider = EmbeddingProvider(self.config, client=self.client(handler=handle))
            before = len(self.requests)
            error = self.assert_error(code, lambda: provider.embed(purpose="query", text="private input"))
            self.assertEqual(len(self.requests), before + 1)
            self.assertNotIn("private", str(error))

    def test_http_errors_are_safe_and_categorized(self):
        for status, text, code in (
            (400, "maximum context length private input", "input_too_large"),
            (413, "private request", "input_too_large"),
            (422, "private invalid input", "invalid_input"),
            (401, "private secret", "http_error"),
            (429, "private remote content", "http_error"),
            (503, "private remote content", "http_error"),
        ):
            with self.subTest(status=status):
                client = self.client(handler=lambda req: httpx.Response(status, text=text))
                provider = EmbeddingProvider(self.config, client=client)
                error = self.assert_error(code, lambda: provider.embed(purpose="query", text="x"))
                self.assertEqual(error.status_code, status)
                self.assertIn(str(status), str(error))
                self.assertNotIn("private", str(error))

    def test_malformed_json(self):
        for body in (b"not json", b"[]", b"null"):
            client = self.client(handler=lambda req: httpx.Response(200, content=body))
            provider = EmbeddingProvider(self.config, client=client)
            self.assert_error("invalid_response", lambda: provider.embed(purpose="query", text="x"))

    def test_caption_modes_and_ocr(self):
        prompts = []
        for capability, structured, limit in (("caption", False, 1024), ("caption", True, 1024), ("ocr", False, 4096)):
            provider = self.vision("  第一行\nsecond line  ")
            output = provider.caption(self.image, structured=structured) if capability == "caption" else provider.ocr(self.image)
            self.assertEqual(output, "第一行\nsecond line")
            body = json.loads(self.requests[-1].content)
            prompts.append(body["messages"][0]["content"])
            self.assertEqual(body["max_tokens"], limit)
            self.assertEqual(body["temperature"], 0)
            self.assertFalse(body["stream"])
            self.assertEqual(body["chat_template_kwargs"], {"enable_thinking": False})
            self.assertTrue(str(self.requests[-1].url).endswith("/chat/completions"))
        self.assertEqual(len(set(prompts)), 3)
        self.assertEqual(self.vision("").ocr(self.image), "")
        self.assertEqual(self.vision(None).ocr(self.image), "")

    def test_classification(self):
        for kind in ("structured_image", "visual_image", "visdoc"):
            self.assertEqual(self.vision(f" {kind}\n").classify(self.image), kind)
            self.assertEqual(json.loads(self.requests[-1].content)["max_tokens"], 32)
        for kind in ("text", "```visual_image```", "visual_image: photo", ""):
            self.assert_error("invalid_response", lambda: self.vision(kind).classify(self.image))

    def test_vision_invalid_responses_and_truncation(self):
        for capability in ("caption", "ocr", "classify"):
            for content, reason, code in (("partial", "length", "output_truncated"),
                                          ([], "stop", "invalid_response"),
                                          ("refused", "content_filter", "invalid_response")):
                provider = self.vision(content, reason)
                error = self.assert_error(code, lambda: getattr(provider, capability)(self.image))
                self.assertEqual(error.capability, capability)
        self.assert_error("invalid_response", lambda: self.vision("").caption(self.image))
        self.assert_error("invalid_response", lambda: self.vision(None).caption(self.image))
        self.assert_error("invalid_response", lambda: self.vision(None).classify(self.image))
        for response in ({}, {"choices": []}, {"choices": [None]}, {"choices": [{}, {}]}):
            provider = VisionProvider(self.config, client=self.client(response))
            self.assert_error("invalid_response", lambda: provider.ocr(self.image))

    def test_ocr_null_is_only_empty_when_normally_completed(self):
        for message in ({}, {"content": None, "refusal": "refused"},
                        {"content": None, "tool_calls": [{"id": "tool"}]},
                        {"content": None, "function_call": {"name": "tool"}}):
            client = self.client({"choices": [{"finish_reason": "stop", "message": message}]})
            self.assert_error("invalid_response", lambda: VisionProvider(self.config, client=client).ocr(self.image))

    def test_check_connection(self):
        for cls in (EmbeddingProvider, VisionProvider):
            for models, code in (([{"id": "test-model"}], None), ([], "model_not_found"),
                                  ([{"id": "other"}], "model_not_found"), ([None], "invalid_response")):
                provider = cls(self.config, client=self.client({"data": models}))
                if code:
                    self.assert_error(code, provider.check_connection)
                else:
                    self.assertIsNone(provider.check_connection())
                self.assertEqual(self.requests[-1].method, "GET")
                self.assertTrue(str(self.requests[-1].url).endswith("/models"))

    def test_client_ownership_and_lazy_factory(self):
        client = self.client({})
        with EmbeddingProvider(self.config, client=client):
            pass
        self.assertFalse(client.is_closed)
        with build_providers(ModelConfig(self.config)) as providers:
            self.assertIsNone(providers.vision)
            owned = providers.embedding._service._client
            self.assertFalse(owned.is_closed)
        self.assertTrue(owned.is_closed)
        with build_providers(ModelConfig(self.config, self.config)) as providers:
            vision_client = providers.vision._service._client
        self.assertTrue(vision_client.is_closed)
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
