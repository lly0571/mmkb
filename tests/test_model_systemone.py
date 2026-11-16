"""Offline checks of the /v1/systemone adapter; no service is contacted."""
import base64
import json
import unittest
from collections import UserDict, UserList

import httpx

from mmkb.config import ModelConfig, ServiceConfig
from mmkb.providers import ImageInput, ProviderError, SystemOneProvider, build_providers
from mmkb.providers.systemone import ChoiceQuestion, MAX_QUESTIONS, NoulQuestion, ScoreQuestion, resolve_profile

CHOICE = {"billing": "payments and refunds", "shipping": "deliveries"}
LEVELS = ["low", "mid", "high"]
CHOICE_ANSWER = {"type": "choice", "choice": "billing", "confidence": 0.9,
                 "probabilities": {"billing": 0.97, "shipping": 0.03}}
NOUL_ANSWER = {"type": "noul", "noul": 0.62, "confidence": 0.6, "probabilities": {"yes": 0.62, "no": 0.38}}
SCORE_ANSWER = {"type": "score", "score": 1.2, "confidence": 0.5,
                "legend": {"0": "low", "1": "mid", "2": "high"}, "probabilities": {"0": 0.2, "1": 0.6, "2": 0.2}}


def envelope(answers, **extra):
    body = {"id": "decision-1", "object": "structured_decision", "created": 1790000000,
            "model": "vllm-sr/d3-mini", "answers": answers}
    body.update(extra)
    return body


class SystemOneProviderTests(unittest.TestCase):
    def setUp(self):
        self.config = ServiceConfig("https://model.example/api/v1/", "vllm-sr/d3-mini")
        self.image = ImageInput(b"test image bytes", "image/png")
        self.requests = []

    def client(self, response=None, *, handler=None):
        def handle(request):
            self.requests.append(request)
            return handler(request) if handler else httpx.Response(200, json=response)
        client = httpx.Client(transport=httpx.MockTransport(handle))
        self.addCleanup(client.close)
        return client

    def responder(self):
        """Serve one plausible answer per requested question id, of the requested type."""
        def handle(request):
            body = json.loads(request.content)
            answers = {}
            for key, question in body["questions"].items():
                criteria = question["criteria"]
                if question["type"] == "choice":
                    names = list(criteria)
                    answers[key] = {"type": "choice", "choice": names[0], "confidence": 0.6,
                                    "probabilities": {name: 1 / len(names) for name in names}}
                elif question["type"] == "noul":
                    answers[key] = {"type": "noul", "noul": 0.6, "confidence": 0.6,
                                    "probabilities": {"yes": 0.6, "no": 0.4}}
                else:
                    indexes = [str(index) for index in range(len(criteria))]
                    answers[key] = {"type": "score", "score": (len(criteria) - 1) / 2, "confidence": 0.6,
                                    "legend": dict(zip(indexes, criteria)),
                                    "probabilities": {index: 1 / len(indexes) for index in indexes}}
            return httpx.Response(200, json=envelope(
                answers,
                usage={"input_tokens": 10, "output_tokens": len(answers)},
                diagnostics={key: {"label_mass": 0.9, "argmax_is_label": True} for key in answers},
            ))
        return handle

    def provider(self, response=None, *, handler=None, model="vllm-sr/d3-mini", profile="auto"):
        config = ServiceConfig(self.config.base_url, model, self.config.api_key, self.config.timeout_seconds)
        return SystemOneProvider(config, client=self.client(response, handler=handler), profile=profile)

    def assert_error(self, code, callback):
        with self.assertRaises(ProviderError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def body(self):
        return json.loads(self.requests[-1].content)

    def test_endpoint_and_doc_example_request(self):
        provider = self.provider(envelope({"team": CHOICE_ANSWER}))
        provider.decide({"ticket": "My card was charged twice for one order."}, {"team": ChoiceQuestion(CHOICE)})
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0].method, "POST")
        self.assertEqual(str(self.requests[0].url), "https://model.example/api/v1/systemone")
        body = self.body()
        self.assertEqual(set(body), {"model", "state", "questions"})
        self.assertEqual(body["model"], "vllm-sr/d3-mini")
        self.assertEqual(body["state"], {"ticket": "My card was charged twice for one order."})
        self.assertEqual(body["questions"], {"team": {"type": "choice", "criteria": CHOICE}})

    def test_string_state_and_question_order_are_preserved(self):
        provider = self.provider(envelope({"tone": SCORE_ANSWER, "team": CHOICE_ANSWER}))
        result = provider.decide("plain text state", {
            "tone": ScoreQuestion(LEVELS),
            "team": ChoiceQuestion(CHOICE),
        })
        body = self.body()
        self.assertEqual(body["state"], "plain text state")
        self.assertEqual(list(body["questions"]), ["tone", "team"])
        self.assertEqual(list(result.answers), ["tone", "team"])
        self.assertEqual(result["team"].value, "billing")
        self.assertEqual(result["tone"].value, 1.2)
        self.assertEqual(result["tone"].level_name, "mid")
        self.assertEqual(result.decision_id, "decision-1")
        self.assertEqual((result.model, result.created), ("vllm-sr/d3-mini", 1790000000))
        self.assertIsNone(result.usage)

    def test_openrouter_systemone_choice_and_score_contract(self):
        # Shape observed from /api/v1/systemone; no vLLM object/diagnostics fields.
        response = {
            "model": "typesafe/jev-1.14-20261014", "provider": "TypeSafe",
            "answers": {
                "category": {"type": "choice", "choice": "bikes", "confidence": 1,
                             "probabilities": {"bikes": 1, "parts": 0, "apparel": 0}},
                "condition": {"type": "score", "score": 0.99, "confidence": 0.97,
                              "probabilities": {"0": 0.02, "1": 0.98, "2": 0},
                              "legend": {"0": "Like new", "1": "Light wear", "2": "Heavy wear"}},
            },
            "usage": {"input_tokens": 1183, "output_tokens": 72, "cost": 0.000049686},
        }
        client = self.client(response)
        config = ServiceConfig("https://openrouter.ai/api/v1", "typesafe/jev-1.14", "test-key")
        provider = SystemOneProvider(config, client=client)
        state = ["Listing: vintage red bicycle", {"type": "image_url", "image_url": {"url": "data:image/png;base64,test"}}]
        result = provider.decide(state, {
            "category": ChoiceQuestion({"bikes": "Complete bicycles", "parts": "Components and accessories",
                                        "apparel": "Clothing and gear"}, instructions="Which category fits?"),
            "condition": ScoreQuestion(["Like new", "Light wear", "Heavy wear"], instructions="How worn is it?"),
        })
        self.assertEqual(str(self.requests[-1].url), "https://openrouter.ai/api/v1/systemone")
        self.assertEqual(self.requests[-1].headers["Authorization"], "Bearer test-key")
        self.assertEqual(self.body()["state"], state)
        self.assertEqual(result["category"].value, "bikes")
        self.assertEqual(result["condition"].value, 0.99)
        self.assertEqual(result.model, "typesafe/jev-1.14-20261014")
        self.assertEqual(result.usage.input_tokens, 1183)

    def test_state_containers_are_normalized_recursively(self):
        shared = UserDict({"ticket": "text"})
        nested = UserDict({"tickets": UserList([shared, shared]), "levels": range(3),
                           "metadata": (None, True, 3, 1.5)})
        provider = self.provider(handler=self.responder())
        for state, expected in (
            (shared, {"ticket": "text"}),
            (range(3), [0, 1, 2]),
            (nested, {"tickets": [{"ticket": "text"}, {"ticket": "text"}],
                      "levels": [0, 1, 2], "metadata": [None, True, 3, 1.5]}),
        ):
            with self.subTest(state=type(state).__name__):
                self.assertEqual(provider.choose(state, CHOICE).value, "billing")
                self.assertEqual(self.body()["state"], expected)
        self.assertIsInstance(nested["tickets"], UserList)
        self.assertIsInstance(nested["levels"], range)

    def test_unsupported_state_contents_send_no_request(self):
        cyclic_list = []
        cyclic_list.append(cyclic_list)
        cyclic_map = UserDict()
        cyclic_map["self"] = cyclic_map
        provider = self.provider()
        for state in (b"text", bytearray(b"text"), memoryview(b"text"),
                      {"secret": object()}, {"secret": {1, 2}}, {"secret": b"text"},
                      {"secret": float("nan")}, [float("inf")], [float("-inf")],
                      {1: "text"}, cyclic_list, cyclic_map):
            with self.subTest(state=type(state).__name__):
                error = self.assert_error("invalid_input", lambda: provider.choose(state, CHOICE))
                self.assertEqual(error.capability, "decision")
                self.assertNotIn("secret", str(error))
        self.assertEqual(self.requests, [])

    def test_three_question_types_serialize_to_documented_criteria(self):
        provider = self.provider(envelope({"a": CHOICE_ANSWER, "b": NOUL_ANSWER, "c": SCORE_ANSWER}))
        result = provider.decide("state", {
            "a": ChoiceQuestion({"billing": None, "shipping": "deliveries"}),
            "b": NoulQuestion(true="money is moving", false="nothing urgent"),
            "c": ScoreQuestion(LEVELS),
        })
        questions = self.body()["questions"]
        self.assertEqual(questions["a"], {"type": "choice", "criteria": {"billing": None, "shipping": "deliveries"}})
        self.assertEqual(questions["b"], {"type": "noul", "criteria": {"true": "money is moving", "false": "nothing urgent"}})
        self.assertEqual(questions["c"], {"type": "score", "criteria": LEVELS})
        self.assertEqual(result["a"].type, "choice")
        self.assertIs(result["b"].value, True)
        self.assertEqual(dict(result["b"].probabilities), {"yes": 0.62, "no": 0.38})
        self.assertEqual(dict(result["c"].probabilities), {"0": 0.2, "1": 0.6, "2": 0.2})
        self.assertEqual(result["c"].legend, tuple(LEVELS))
        self.assertIsNone(result["a"].noul)
        self.assertIsNone(result["b"].score)

    def test_noul_without_criteria_sends_null(self):
        self.provider(envelope({"b": NOUL_ANSWER})).decide("state", {"b": NoulQuestion()})
        self.assertEqual(self.body()["questions"]["b"], {"type": "noul", "criteria": None})

    def test_single_question_helpers(self):
        provider = self.provider(handler=self.responder())
        answer = provider.choose("state", ["billing", "shipping"])
        self.assertEqual(answer.question_id, "answer")
        self.assertEqual(answer.confidence, 0.6)
        self.assertEqual(self.body()["questions"], {"answer": {"type": "choice", "criteria": {"billing": None, "shipping": None}}})
        provider.choose("state", {"billing": "payments"}, question_id="team")
        self.assertEqual(self.body()["questions"], {"team": {"type": "choice", "criteria": {"billing": "payments"}}})
        provider.rate("state", LEVELS, question_id="severity")
        self.assertEqual(self.body()["questions"]["severity"], {"type": "score", "criteria": LEVELS})
        provider.noul("state", true="urgent", question_id="urgent")
        self.assertEqual(self.body()["questions"]["urgent"], {"type": "noul", "criteria": {"true": "urgent"}})
        self.assertEqual(provider.choose("state", ["a", "b"]).value, "a")
        self.assertIs(provider.noul("state").value, True)

    def test_helpers_parse_values(self):
        self.assertIs(self.provider(envelope({"answer": NOUL_ANSWER})).noul("state").value, True)
        self.assertIs(self.provider(envelope({"answer": {**NOUL_ANSWER, "noul": 0.4, "probabilities": {"yes": 0.4, "no": 0.6}}})).noul("state").value, False)
        rated = self.provider(envelope({"answer": SCORE_ANSWER})).rate("state", LEVELS)
        self.assertEqual((rated.value, rated.level_name), (1.2, "mid"))
        self.assertEqual(self.provider(envelope({"answer": {**SCORE_ANSWER, "score": 1.6}})).rate("state", LEVELS).level_name, "high")

    def test_multi_answer_decision_has_no_single_answer(self):
        result = self.provider(envelope({"a": CHOICE_ANSWER, "b": NOUL_ANSWER})).decide(
            "state", {"a": ChoiceQuestion(CHOICE), "b": NoulQuestion()})
        self.assert_error("invalid_response", lambda: result.answer)

    def test_fixed_prompt_profile_rejects_instructions(self):
        provider = self.provider()
        error = self.assert_error(
            "invalid_input",
            lambda: provider.decide("state", {"team": ChoiceQuestion(CHOICE)}, instructions="answer as an agent"),
        )
        self.assertIn("rebuilds the prompt", str(error))
        error = self.assert_error("invalid_input", lambda: provider.decide("state", {"team": ChoiceQuestion(CHOICE, instructions="which team?")}))
        self.assertIn("rebuilds the prompt", str(error))
        self.assertEqual(self.requests, [])
        provider = self.provider(handler=self.responder(), model="Qwen/Qwen3-0.6B")
        provider.decide("state", {"team": ChoiceQuestion(CHOICE, instructions="which team?")}, instructions="answer as an agent")
        body = self.body()
        self.assertEqual(body["instructions"], "answer as an agent")
        self.assertEqual(body["questions"]["team"]["instructions"], "which team?")
        for omitted in ("chat_template_kwargs", "joint", "audio"):
            self.assertNotIn(omitted, body)

    def test_choice_option_limit_follows_the_served_model(self):
        for model, count, accepted in (("Qwen/Qwen3-0.6B", 26, True), ("Qwen/Qwen3-0.6B", 27, False),
                                      ("Cloudflare/clef-flash", 26, True), ("Cloudflare/clef-flash", 27, False),
                                      ("Cloudflare/clef-omni", 26, True), ("Cloudflare/clef-omni", 27, False),
                                      ("vllm-sr/d3-mini", 255, True), ("vllm-sr/d3-mini", 256, False)):
            with self.subTest(model=model, count=count):
                options = {f"option-{index}": None for index in range(count)}
                provider = self.provider(model=model)
                if accepted:
                    provider = self.provider(handler=self.responder(), model=model)
                    provider.decide("state", {"many": ChoiceQuestion(options)})
                    self.assertEqual(len(self.body()["questions"]["many"]["criteria"]), count)
                else:
                    self.assert_error("invalid_input", lambda: provider.decide("state", {"many": ChoiceQuestion(options)}))
        self.assertEqual(len(self.requests), 4)

    def test_clef_profiles_reject_all_instructions_locally(self):
        for model, profile in (("Cloudflare/clef-flash", "auto"), ("Cloudflare/clef-omni", "auto"),
                               ("custom-checkpoint", "clef")):
            with self.subTest(model=model, profile=profile):
                provider = self.provider(model=model, profile=profile)
                self.assertTrue(provider.profile.fixed_prompt)
                self.assert_error("invalid_input", lambda: provider.decide(
                    "state", {"q": NoulQuestion()}, instructions="read carefully"))
                for question in (ChoiceQuestion(CHOICE, instructions="which team?"),
                                 NoulQuestion(instructions="urgent?"),
                                 ScoreQuestion(LEVELS, instructions="severity?")):
                    self.assert_error("invalid_input", lambda: provider.decide("state", {"q": question}))
        self.assertEqual(self.requests, [])

    def test_profile_resolution_and_explicit_override(self):
        for model, name in (("vllm-sr/d3-mini", "d3"), ("vllm-sr/d3", "d3"), ("/models/D3-Mini-4B", "d3"),
                            ("d3-edge", "d3"), ("Qwen/Qwen3-0.6B", "generic"), ("LiquidAI/d1-3B", "generic"),
                            ("Cloudflare/clef-flash", "clef"), ("Cloudflare/clef-omni", "clef"),
                            ("/models/Clef-Omni-AWQ", "clef"), ("clef-flashx", "generic"),
                            ("qwen3-d3x", "generic"), ("", "generic")):
            with self.subTest(model=model):
                self.assertEqual(resolve_profile(model).name, name)
        self.assertEqual(resolve_profile("custom-checkpoint", "d3").name, "d3")
        self.assertEqual(resolve_profile("custom-checkpoint", "clef").name, "clef")
        self.assertEqual(resolve_profile("vllm-sr/d3-mini", "generic").name, "generic")
        with self.assertRaises(ValueError):
            resolve_profile("custom-checkpoint", "omni")
        # An unknown checkpoint can still be told to speak the generic dialect.
        provider = self.provider(handler=self.responder(), model="custom-checkpoint", profile="generic")
        provider.decide("state", {"q": ChoiceQuestion(CHOICE, instructions="which team?")})
        self.assertEqual(self.body()["questions"]["q"]["instructions"], "which team?")

    def test_limits_and_invalid_questions_send_no_request(self):
        provider = self.provider()
        cases = {
            "no questions": lambda: provider.decide("state", {}),
            "too many questions": lambda: provider.decide("state", {f"q{index}": ChoiceQuestion({"a": None}) for index in range(MAX_QUESTIONS + 1)}),
            "empty question id": lambda: provider.decide("state", {"  ": ChoiceQuestion({"a": None})}),
            "padded question id": lambda: provider.decide("state", {" q": ChoiceQuestion({"a": None})}),
            "unknown question type": lambda: provider.decide("state", {"q": {"type": "choice", "criteria": {"a": None}}}),
            "no options": lambda: provider.decide("state", {"q": ChoiceQuestion({})}),
            "empty option name": lambda: provider.decide("state", {"q": ChoiceQuestion({"": None})}),
            "padded option name": lambda: provider.decide("state", {"q": ChoiceQuestion({" a": None})}),
            "non-text description": lambda: provider.decide("state", {"q": ChoiceQuestion({"a": 5})}),
            "blank description": lambda: provider.decide("state", {"q": ChoiceQuestion({"a": " "})}),
            "one score level": lambda: provider.decide("state", {"q": ScoreQuestion(["low"])}),
            "too many score levels": lambda: provider.decide("state", {"q": ScoreQuestion([f"l{index}" for index in range(11)])}),
            "duplicate score levels": lambda: provider.decide("state", {"q": ScoreQuestion(["low", "low"])}),
            "score levels as text": lambda: provider.decide("state", {"q": ScoreQuestion("low,mid")}),
            "blank noul description": lambda: provider.decide("state", {"q": NoulQuestion(true=" ")}),
            "blank cache salt": lambda: provider.decide("state", {"q": NoulQuestion()}, cache_salt=" "),
            "state is a number": lambda: provider.decide(7, {"q": NoulQuestion()}),
            "state is null": lambda: provider.decide(None, {"q": NoulQuestion()}),
            "blank helper question id": lambda: provider.choose("state", ["a"], question_id=""),
        }
        for name, callback in cases.items():
            with self.subTest(case=name):
                self.assert_error("invalid_input", callback)
        self.assertEqual(self.requests, [])

    def test_media_is_sent_as_data_urls_and_urls(self):
        provider = self.provider(handler=self.responder())
        provider.choose(" ", ["billing", "shipping"], images=[self.image, "https://cdn.example/clip.jpg"],
                        videos=["https://cdn.example/clip.mp4"])
        body = self.body()
        self.assertEqual(body["state"], " ")
        self.assertEqual(len(body["images"]), 2)
        url = body["images"][0]
        self.assertTrue(url.startswith("data:image/png;base64,"))
        self.assertEqual(base64.b64decode(url.split(",", 1)[1]), self.image.data)
        self.assertEqual(body["images"][1], "https://cdn.example/clip.jpg")
        self.assertEqual(body["videos"], ["https://cdn.example/clip.mp4"])
        for kwargs in ({"images": self.image}, {"images": ["ftp://cdn.example/a.jpg"]},
                       {"images": [ImageInput(b"x", "image/gif")]}, {"videos": "https://cdn.example/a.mp4"},
                       {"videos": ["/local/a.mp4"]}, {"videos": [None]}):
            with self.subTest(kwargs=list(kwargs)):
                self.assert_error("invalid_input", lambda: provider.choose("state", ["a"], **kwargs))

    def test_empty_media_lists_are_omitted(self):
        self.provider(handler=self.responder()).choose("state", ["a"], images=[], videos=[])
        self.assertNotIn("images", self.body())
        self.assertNotIn("videos", self.body())

    def test_cache_salt_and_auth_header(self):
        config = ServiceConfig(self.config.base_url, "vllm-sr/d3-mini", "private-test-key", 17)
        provider = SystemOneProvider(config, client=self.client(handler=self.responder()))
        provider.choose("state", ["billing", "shipping"], cache_salt="tenant-7")
        self.assertEqual(self.body()["cache_salt"], "tenant-7")
        self.assertEqual(self.requests[-1].headers["authorization"], "Bearer private-test-key")
        self.assertEqual(self.requests[-1].extensions["timeout"], {"connect": 5.0, "read": 17, "write": 17, "pool": 17})

    def test_usage_and_diagnostics_are_parsed(self):
        provider = self.provider(envelope(
            {"team": CHOICE_ANSWER},
            usage={"input_tokens": 142, "output_tokens": 1},
            diagnostics={"team": {"label_mass": 0.93, "argmax_is_label": True}, "other": {"label_mass": 0.5}},
        ))
        result = provider.decide("state", {"team": ChoiceQuestion(CHOICE)})
        self.assertEqual((result.usage.input_tokens, result.usage.output_tokens), (142, 1))
        self.assertEqual((result["team"].label_mass, result["team"].argmax_is_label), (0.93, True))
        # Pooling checkpoints may omit diagnostics entirely: that stays unknown, not an error.
        for diagnostics in ({}, {"other": {"label_mass": 0.5}}, {"team": {}}, []):
            provider = self.provider(envelope({"team": CHOICE_ANSWER}, usage={"input_tokens": 142}, diagnostics=diagnostics))
            answer = provider.decide("state", {"team": ChoiceQuestion(CHOICE)})["team"]
            self.assertEqual((answer.label_mass, answer.argmax_is_label), (None, None))

    def test_choice_answer_validation(self):
        mutations = {
            "unexpected answer": {"team": CHOICE_ANSWER, "other": NOUL_ANSWER},
            "missing answer": {},
            "answer not an object": {"team": "billing"},
            "type mismatch": {"team": NOUL_ANSWER},
            "unknown probability key": {"team": {**CHOICE_ANSWER, "probabilities": {"billing": 1.0}}},
            "probabilities do not sum to one": {"team": {**CHOICE_ANSWER, "probabilities": {"billing": 0.5, "shipping": 0.3}}},
            "probability out of range": {"team": {**CHOICE_ANSWER, "probabilities": {"billing": 1.5, "shipping": -0.5}}},
            "probability is text": {"team": {**CHOICE_ANSWER, "probabilities": {"billing": "0.97", "shipping": 0.03}}},
            "probability is a bool": {"team": {**CHOICE_ANSWER, "probabilities": {"billing": True, "shipping": False}}},
            "choice is not an option": {"team": {**CHOICE_ANSWER, "choice": "security"}},
            "choice is a list": {"team": {**CHOICE_ANSWER, "choice": []}},
            "choice is an object": {"team": {**CHOICE_ANSWER, "choice": {}}},
            "choice is null": {"team": {**CHOICE_ANSWER, "choice": None}},
            "choice is numeric": {"team": {**CHOICE_ANSWER, "choice": 1}},
            "confidence missing": {"team": {key: value for key, value in CHOICE_ANSWER.items() if key != "confidence"}},
            "confidence out of range": {"team": {**CHOICE_ANSWER, "confidence": 1.4}},
            "probability is NaN": None,
            "diagnostics are malformed": None,
        }
        self.assert_invalid_answer(ChoiceQuestion(CHOICE), mutations)

    def test_noul_answer_validation(self):
        mutations = {
            # OpenRouter/Jev returns this valid dialect, which is not the vLLM contract.
            "OpenRouter probability-only noul": {"team": {"type": "noul", "noul": 0.02}},
            "noul out of range": {"team": {**NOUL_ANSWER, "noul": 1.2}},
            "noul missing": {"team": {key: value for key, value in NOUL_ANSWER.items() if key != "noul"}},
            "probabilities are not yes and no": {"team": {**NOUL_ANSWER, "probabilities": {"true": 0.62, "false": 0.38}}},
            "type mismatch": {"team": CHOICE_ANSWER},
        }
        self.assert_invalid_answer(NoulQuestion(), mutations)

    def test_score_answer_validation(self):
        mutations = {
            "legend is incomplete": {"team": {**SCORE_ANSWER, "legend": {"0": "low", "1": "mid"}}},
            "legend has unknown keys": {"team": {**SCORE_ANSWER, "legend": {"1": "low", "2": "mid", "3": "high"}}},
            "legend values are not text": {"team": {**SCORE_ANSWER, "legend": {"0": 1, "1": 2, "2": 3}}},
            "probabilities are not level indices": {"team": {**SCORE_ANSWER, "probabilities": {"low": 0.2, "mid": 0.6, "high": 0.2}}},
            "score is out of range": {"team": {**SCORE_ANSWER, "score": 3.0}},
            "score is negative": {"team": {**SCORE_ANSWER, "score": -0.1}},
            "score missing": {"team": {key: value for key, value in SCORE_ANSWER.items() if key != "score"}},
            "type mismatch": {"team": NOUL_ANSWER},
        }
        self.assert_invalid_answer(ScoreQuestion(LEVELS), mutations)

    def assert_invalid_answer(self, question, mutations):
        for name, answers in mutations.items():
            with self.subTest(case=name):
                if answers is None:  # HTTPX's JSON writer rejects NaN, so send the raw service body.
                    body = json.dumps(envelope({"team": {**CHOICE_ANSWER, "probabilities": {"billing": float("nan"), "shipping": 0.0}}})).encode()
                    provider = self.provider(handler=lambda request: httpx.Response(200, content=body))
                elif name == "diagnostics are malformed":
                    provider = self.provider(envelope({"team": CHOICE_ANSWER}, diagnostics={"team": {"label_mass": "0.93"}}))
                else:
                    provider = self.provider(envelope(answers))
                self.assert_error("invalid_response", lambda: provider.decide("state", {"team": question}))

    def test_envelope_validation(self):
        answers = {"team": CHOICE_ANSWER}
        for name, extra in {
            "answers missing": {"answers": None},
            "answers is not an object": {"answers": []},
            "object type mismatch": {"object": "chat.completion"},
            "usage is not an object": {"usage": 3},
            "usage is negative": {"usage": {"input_tokens": -1, "output_tokens": 1}},
            "usage is not an integer": {"usage": {"input_tokens": 1.5, "output_tokens": 1}},
            "usage count is a bool": {"usage": {"input_tokens": True, "output_tokens": 1}},
        }.items():
            with self.subTest(case=name):
                provider = self.provider({**envelope(answers), **extra})
                self.assert_error("invalid_response", lambda: provider.decide("state", {"team": ChoiceQuestion(CHOICE)}))
        # Optional metadata stays unknown instead of failing the read.
        provider = self.provider(envelope(answers, id=7, model=None, created="soon", usage=None, diagnostics=[], object=None))
        result = provider.decide("state", {"team": ChoiceQuestion(CHOICE)})
        self.assertEqual((result.decision_id, result.model, result.created, result.usage), (None, None, None, None))
        self.assertEqual(result["team"].value, "billing")

    def test_transport_and_http_errors_are_safe(self):
        for exception, code in ((httpx.ReadTimeout, "timeout"), (httpx.ConnectTimeout, "timeout"),
                                (httpx.ConnectError, "connection_error")):
            def handle(request):
                raise exception("private remote content", request=request)
            provider = self.provider(handler=handle)
            before = len(self.requests)
            error = self.assert_error(code, lambda: provider.choose("private state", ["a"]))
            self.assertEqual(len(self.requests), before + 1)
            self.assertNotIn("private", str(error))
        for status, text, code in (
            (400, "state exceeds max_model_len private input", "input_too_large"),
            (400, "each label must be a single token", "invalid_input"),
            (422, "unknown request field private", "invalid_input"),
            (501, "model has no read strategy", "unsupported_model"),
            (401, "private secret", "http_error"),
            (429, "private remote content", "http_error"),
        ):
            with self.subTest(status=status):
                provider = self.provider(handler=lambda request: httpx.Response(status, text=text))
                error = self.assert_error(code, lambda: provider.choose("state", ["a"]))
                self.assertEqual(error.status_code, status)
                self.assertEqual(error.capability, "decision")
                self.assertIn(str(status), str(error))
                self.assertNotIn("private", str(error))

    def test_malformed_json(self):
        for body in (b"not json", b"[]", b"null"):
            provider = self.provider(handler=lambda request: httpx.Response(200, content=body))
            self.assert_error("invalid_response", lambda: provider.choose("state", ["a"]))

    def test_check_connection(self):
        for models, code in (([{"id": "vllm-sr/d3-mini"}], None), ([], "model_not_found"),
                             ([{"id": "other"}], "model_not_found"), ([None], "invalid_response")):
            provider = self.provider({"data": models})
            if code:
                self.assert_error(code, provider.check_connection)
            else:
                self.assertIsNone(provider.check_connection())
            self.assertEqual(self.requests[-1].method, "GET")
            self.assertTrue(str(self.requests[-1].url).endswith("/models"))

    def test_registry_and_client_ownership(self):
        client = self.client({})
        with SystemOneProvider(self.config, client=client):
            pass
        self.assertFalse(client.is_closed)

        decision = ServiceConfig(self.config.base_url, "vllm-sr/d3-mini")
        with build_providers(ModelConfig(self.config, decision=decision, decision_profile="d3")) as providers:
            self.assertIsNone(providers.vision)
            self.assertEqual(providers.decision.profile.name, "d3")
            owned = providers.decision._service._client
            self.assertFalse(owned.is_closed)
        self.assertTrue(owned.is_closed)
        with build_providers(ModelConfig(self.config)) as providers:
            self.assertIsNone(providers.decision)
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
