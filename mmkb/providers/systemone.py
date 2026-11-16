"""Typed structured-decision reads through vLLM's ``/v1/systemone`` endpoint.

One request asks several typed questions about one state and returns a
probability for every allowed answer. Each question is one model read on
generation models with a read strategy, and one pooling pass on decision
checkpoints, so keep unrelated states out of a single request. Only the wire
protocol, request limits and response validation live here; no workflow or
persistence logic.
"""
from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

import httpx

from mmkb.config import ServiceConfig
from mmkb.providers.common import ImageInput, ProviderError, ServiceClient, image_data_url

AnswerType = Literal["choice", "noul", "score"]
ProfileName = Literal["auto", "d3", "clef", "generic"]

ENDPOINT = "systemone"
SINGLE_QUESTION = "answer"
# Limits documented for the endpoint itself; options depend on the served model.
MAX_QUESTIONS = 64
MAX_SCORE_LEVELS = 10
# Softmax over the labels sums to one; allow float drift, never renormalize silently.
PROBABILITY_TOLERANCE = 1e-3
MEDIA_URL_PREFIXES = ("http://", "https://", "data:")


@dataclass(frozen=True)
class Profile:
    """Request dialect of a served architecture; see docs/systemone.md."""

    name: str
    # Fixed-prompt checkpoints rebuild the prompt themselves: extra instructions
    # are rejected with 400 and chat_template_kwargs has no effect.
    fixed_prompt: bool
    max_choice_options: int


PROFILES: dict[str, Profile] = {
    # Decision 3.0 (vllm-sr/d3, d3-flash, d3-mini, ...): answer-code readout
    # scores one code per option, so options are not capped at the A-Z labels.
    "d3": Profile("d3", fixed_prompt=True, max_choice_options=255),
    # Clef's joint schema heads also rebuild the prompt, with A-Z labels.
    "clef": Profile("clef", fixed_prompt=True, max_choice_options=26),
    # Generation architectures with a registered read strategy use A-Z labels.
    "generic": Profile("generic", fixed_prompt=False, max_choice_options=26),
}
_D3_TOKEN = re.compile(r"(?:^|[^a-z0-9])d3(?:[^a-z0-9]|$)", re.IGNORECASE)
_CLEF_TOKEN = re.compile(r"(?:^|[^a-z0-9])clef-(?:flash|omni)(?:[^a-z0-9]|$)", re.IGNORECASE)


def resolve_profile(model_id: str, requested: ProfileName = "auto") -> Profile:
    """Pick the request dialect, recognizing Decision 3.0 and Clef checkpoints."""
    if requested not in PROFILES and requested != "auto":
        raise ValueError("Decision model profile must be auto, d3, clef or generic")
    if requested != "auto":
        return PROFILES[requested]
    if _D3_TOKEN.search(model_id or ""):
        return PROFILES["d3"]
    return PROFILES["clef" if _CLEF_TOKEN.search(model_id or "") else "generic"]


@dataclass(frozen=True)
class ChoiceQuestion:
    """Option name to description or ``None``; at least one option."""

    options: Mapping[str, str | None] | Sequence[str]
    instructions: str | None = None


@dataclass(frozen=True)
class NoulQuestion:
    """Yes/no question with optional descriptions of the two answers."""

    true: str | None = None
    false: str | None = None
    instructions: str | None = None


@dataclass(frozen=True)
class ScoreQuestion:
    """Ordered level names; at least two and at most ``MAX_SCORE_LEVELS``."""

    levels: Sequence[str]
    instructions: str | None = None


Question = ChoiceQuestion | NoulQuestion | ScoreQuestion


@dataclass(frozen=True)
class Answer:
    question_id: str
    type: AnswerType
    probabilities: Mapping[str, float]
    confidence: float
    choice: str | None = None
    noul: bool | None = None
    score: float | None = None
    legend: tuple[str, ...] = ()
    label_mass: float | None = None
    argmax_is_label: bool | None = None

    @property
    def value(self) -> str | bool | float:
        """Winning option name, yes/no, or the expected zero-based level."""
        return {"choice": self.choice, "noul": self.noul, "score": self.score}[self.type]

    @property
    def level_name(self) -> str | None:
        """Legend entry nearest the expected level of a ``score`` answer."""
        if self.type != "score" or not self.legend or self.score is None:
            return None
        return self.legend[min(max(round(self.score), 0), len(self.legend) - 1)]


@dataclass(frozen=True)
class Usage:
    input_tokens: int | None
    output_tokens: int | None


@dataclass(frozen=True)
class Decision:
    answers: Mapping[str, Answer]
    decision_id: str | None = None
    model: str | None = None
    created: int | None = None
    usage: Usage | None = None

    def __getitem__(self, question_id: str) -> Answer:
        return self.answers[question_id]

    @property
    def answer(self) -> Answer:
        """The single answer of a one-question request."""
        if len(self.answers) != 1:
            raise ProviderError("decision", "invalid_response", "Expected exactly one answer")
        return next(iter(self.answers.values()))


class SystemOneProvider:
    """Adapter for the ``/v1/systemone`` decision endpoint.

    ``profile`` selects the request dialect; ``auto`` derives it from the model
    ID. Joint canvas reads and audio inputs are not sent: ``joint`` is
    DiffusionGemma only and the served Decision 3.0 checkpoints take images and
    video, not audio.
    """

    def __init__(self, config: ServiceConfig, *, client: httpx.Client | None = None,
                 profile: ProfileName = "auto"):
        self._service = ServiceClient(config, client)
        self.profile = resolve_profile(config.model, profile)

    def decide(
        self,
        state: str | Mapping | Sequence,
        questions: Mapping[str, Question],
        *,
        instructions: str | None = None,
        images: Sequence[ImageInput | str] | None = None,
        videos: Sequence[str] | None = None,
        cache_salt: str | None = None,
    ) -> Decision:
        """Ask every question about one state; answers keep the request order."""
        capability = "decision"
        specs = _validated_questions(questions, capability)
        body: dict = {
            "model": self._service.config.model,
            "state": _state_value(state, capability),
            "questions": {key: _wire_question(spec, self.profile, capability) for key, spec in specs.items()},
        }
        if instructions is not None:
            body["instructions"] = _question_text(instructions, "instructions", capability, self.profile)
        if cache_salt is not None:
            if not isinstance(cache_salt, str) or not cache_salt.strip():
                raise ProviderError(capability, "invalid_input", "cache_salt must be a non-empty string")
            body["cache_salt"] = cache_salt
        body.update(_media(images, videos, capability))

        result = self._service.request(capability, "POST", ENDPOINT, body=body)
        return _parse_decision(result, specs, capability)

    def choose(self, state, options, *, instructions: str | None = None,
               question_id: str = SINGLE_QUESTION, **kwargs) -> Answer:
        """One ``choice`` question; ``options`` is a mapping or a list of option names."""
        question = ChoiceQuestion(_option_map(options, "decision"), instructions)
        return self._ask_one(state, question_id, question, **kwargs)

    def noul(self, state, *, true: str | None = None, false: str | None = None,
             instructions: str | None = None, question_id: str = SINGLE_QUESTION, **kwargs) -> Answer:
        """One ``noul`` question; ``true``/``false`` optionally describe both answers."""
        return self._ask_one(state, question_id, NoulQuestion(true, false, instructions), **kwargs)

    def rate(self, state, levels, *, instructions: str | None = None,
             question_id: str = SINGLE_QUESTION, **kwargs) -> Answer:
        """One ``score`` question over ordered level names; see ``Answer.score``."""
        return self._ask_one(state, question_id, ScoreQuestion(levels, instructions), **kwargs)

    def _ask_one(self, state, question_id: str, question: Question, **kwargs) -> Answer:
        if not isinstance(question_id, str) or not question_id.strip() or question_id != question_id.strip():
            raise ProviderError("decision", "invalid_input", "Question id must be a non-empty string")
        return self.decide(state, {question_id: question}, **kwargs).answers[question_id]

    def check_connection(self) -> None:
        self._service.check_connection("decision")

    def close(self) -> None:
        self._service.close()

    def __enter__(self) -> "SystemOneProvider":
        return self

    def __exit__(self, *args) -> None:
        self.close()


def _option_map(options, capability: str) -> dict[str, str | None]:
    if isinstance(options, Mapping):
        return dict(options)
    if isinstance(options, Sequence) and not isinstance(options, (str, bytes)):
        return {name: None for name in options}
    raise ProviderError(capability, "invalid_input", "Choice options must be a mapping or a list of option names")


def _level_list(levels, capability: str) -> list[str]:
    if isinstance(levels, Sequence) and not isinstance(levels, (str, bytes)):
        return list(levels)
    raise ProviderError(capability, "invalid_input", "Score levels must be a list of level names")


def _state_value(state, capability: str):
    if isinstance(state, (str, Mapping, Sequence)) and not isinstance(state, (bytes, bytearray, memoryview)):
        try:
            return _json_value(state)
        except (TypeError, ValueError, RecursionError):
            raise ProviderError(
                capability, "invalid_input",
                "State must contain JSON values with string object keys, finite numbers and no cycles",
            ) from None
    raise ProviderError(
        capability, "invalid_input",
        "State must be a string or a JSON object/array; use an empty string for media-only decisions",
    )


def _json_value(value):
    """Copy supported containers into JSON-native values without coercing data."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("JSON object keys must be strings")
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, memoryview)):
        return [_json_value(item) for item in value]
    raise ValueError("Unsupported JSON value")


def _text(value, name: str, capability: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ProviderError(capability, "invalid_input", f"{name} must be a non-empty string or null")
    return value


def _question_text(value, name: str, capability: str, profile: Profile | None) -> str | None:
    if value is None:
        return None
    if profile is not None and profile.fixed_prompt:
        raise ProviderError(
            capability, "invalid_input",
            f"{profile.name} rebuilds the prompt itself; put that context in the state or the criteria "
            f"instead of {name}",
        )
    return _text(value, name, capability)


def _validated_questions(questions, capability: str) -> dict[str, Question]:
    if not isinstance(questions, Mapping) or not questions:
        raise ProviderError(capability, "invalid_input", "Provide at least one question")
    if len(questions) > MAX_QUESTIONS:
        raise ProviderError(capability, "invalid_input", f"At most {MAX_QUESTIONS} questions per request")
    for key, spec in questions.items():
        if not isinstance(key, str) or not key.strip() or key != key.strip():
            raise ProviderError(capability, "invalid_input", "Question id must be a non-empty string")
        if not isinstance(spec, (ChoiceQuestion, NoulQuestion, ScoreQuestion)):
            raise ProviderError(capability, "invalid_input", "Question must be a choice, noul or score question")
    return dict(questions)


def _choice_criteria(spec: ChoiceQuestion, capability: str) -> dict[str, str | None]:
    options = _option_map(spec.options, capability)
    if not options:
        raise ProviderError(capability, "invalid_input", "A choice question needs at least one option")
    criteria: dict[str, str | None] = {}
    for name, description in options.items():
        if not isinstance(name, str) or not name.strip() or name != name.strip():
            raise ProviderError(capability, "invalid_input", "Option name must be a non-empty string")
        criteria[name] = _text(description, f"description of option {name}", capability)
    return criteria


def _criteria(spec: Question, profile: Profile, capability: str) -> object:
    """Build the ``criteria`` value of one question, validated against the profile."""
    if isinstance(spec, ChoiceQuestion):
        criteria = _choice_criteria(spec, capability)
        if len(criteria) > profile.max_choice_options:
            raise ProviderError(
                capability, "invalid_input",
                f"At most {profile.max_choice_options} options per choice question for this model",
            )
        return criteria
    if isinstance(spec, NoulQuestion):
        criteria = {}
        for name, value in (("true", spec.true), ("false", spec.false)):
            if value is not None:
                criteria[name] = _text(value, f"{name} description", capability)
        return criteria or None
    levels = _level_list(spec.levels, capability)
    if len(levels) < 2:
        raise ProviderError(capability, "invalid_input", "A score question needs at least two level names")
    if len(levels) > MAX_SCORE_LEVELS:
        raise ProviderError(capability, "invalid_input", f"At most {MAX_SCORE_LEVELS} levels per score question")
    if any(not isinstance(level, str) or not level.strip() or level != level.strip() for level in levels):
        raise ProviderError(capability, "invalid_input", "Score level name must be a non-empty string")
    if len(set(levels)) != len(levels):
        raise ProviderError(capability, "invalid_input", "Score level names must be unique")
    return levels


def _wire_question(spec: Question, profile: Profile, capability: str) -> dict:
    question: dict = {"type": _answer_type(spec), "criteria": _criteria(spec, profile, capability)}
    if spec.instructions is not None:
        question["instructions"] = _question_text(spec.instructions, "instructions", capability, profile)
    return question


def _answer_type(spec: Question) -> AnswerType:
    if isinstance(spec, ChoiceQuestion):
        return "choice"
    return "noul" if isinstance(spec, NoulQuestion) else "score"


def _media(images, videos, capability: str) -> dict:
    media: dict = {}
    if images is not None:
        if isinstance(images, (ImageInput, str)) or not isinstance(images, Sequence):
            raise ProviderError(capability, "invalid_input", "images must be a list of ImageInput or media URLs")
        if len(images):
            media["images"] = [
                image_data_url(item, capability) if isinstance(item, ImageInput) else _media_url(item, capability)
                for item in images
            ]
    if videos is not None:
        if isinstance(videos, str) or not isinstance(videos, Sequence):
            raise ProviderError(capability, "invalid_input", "videos must be a list of media URLs")
        if len(videos):
            media["videos"] = [_media_url(item, capability) for item in videos]
    return media


def _media_url(value, capability: str) -> str:
    if not isinstance(value, str) or not value.startswith(MEDIA_URL_PREFIXES):
        raise ProviderError(capability, "invalid_input", "Media must be an HTTP(S) or data URL")
    return value


def _probability(value, what: str, capability: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ProviderError(capability, "invalid_response", f"{what} must be a probability between 0 and 1")
    return float(value)


def _probability_map(value, expected: set[str], what: str, capability: str) -> dict[str, float]:
    if not isinstance(value, dict) or set(value) != expected:
        raise ProviderError(capability, "invalid_response", f"{what} must hold one probability per allowed answer")
    result = {key: _probability(item, f"{what} probability", capability) for key, item in value.items()}
    if abs(math.fsum(result.values()) - 1.0) > PROBABILITY_TOLERANCE:
        raise ProviderError(capability, "invalid_response", f"{what} probabilities must sum to one")
    return result


def _number(value, what: str, capability: str, *, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ProviderError(capability, "invalid_response", f"{what} must be a number between {low:g} and {high:g}")
    return float(value)


def _parse_answer(question_id: str, spec: Question, raw, diagnostics, capability: str) -> Answer:
    if not isinstance(raw, dict):
        raise ProviderError(capability, "invalid_response", f"Answer for question {question_id} must be an object")
    expected = _answer_type(spec)
    if raw.get("type") != expected:
        raise ProviderError(
            capability, "invalid_response", f"Answer type of question {question_id} does not match the request",
        )
    confidence = _probability(raw.get("confidence"), "confidence", capability)
    diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
    label_mass = diagnostics.get("label_mass")
    argmax = diagnostics.get("argmax_is_label")

    fields: dict = {"choice": None, "noul": None, "score": None, "legend": ()}
    if expected == "choice":
        names = set(_choice_criteria(spec, capability))
        probabilities = _probability_map(raw.get("probabilities"), names, "choice", capability)
        if not isinstance(raw.get("choice"), str) or raw["choice"] not in names:
            raise ProviderError(capability, "invalid_response", f"Choice of question {question_id} is not an allowed option")
        fields["choice"] = raw["choice"]
    elif expected == "noul":
        yes = _probability(raw.get("noul"), "noul", capability)
        probabilities = _probability_map(raw.get("probabilities"), {"yes", "no"}, "noul", capability)
        fields["noul"] = yes >= 0.5
    else:
        levels = _level_list(spec.levels, capability)
        keys = {str(index) for index in range(len(levels))}
        probabilities = _probability_map(raw.get("probabilities"), keys, "score", capability)
        legend = raw.get("legend")
        if not isinstance(legend, dict) or set(legend) != keys or not all(isinstance(item, str) for item in legend.values()):
            raise ProviderError(capability, "invalid_response", f"Legend of question {question_id} must name every level")
        fields["score"] = _number(raw.get("score"), "score", capability, low=0.0, high=float(len(levels) - 1))
        # The requested level names are authoritative: the legend only echoes them.
        fields["legend"] = tuple(levels)

    return Answer(
        question_id=question_id,
        type=expected,
        probabilities=probabilities,
        confidence=confidence,
        choice=fields["choice"],
        noul=fields["noul"],
        score=fields["score"],
        legend=fields["legend"],
        label_mass=None if label_mass is None else _probability(label_mass, "label_mass", capability),
        argmax_is_label=argmax if isinstance(argmax, bool) else None,
    )


def _parse_decision(result: dict, specs: Mapping[str, Question], capability: str) -> Decision:
    if result.get("object") not in (None, "structured_decision"):
        raise ProviderError(capability, "invalid_response", "Unexpected object type from decision service")
    answers = result.get("answers")
    if not isinstance(answers, dict) or set(answers) != set(specs):
        raise ProviderError(capability, "invalid_response", "Decision answers do not match the requested questions")
    raw_diagnostics = result.get("diagnostics")
    diagnostics = raw_diagnostics if isinstance(raw_diagnostics, dict) else {}
    parsed = {
        question_id: _parse_answer(question_id, spec, answers[question_id], diagnostics.get(question_id), capability)
        for question_id, spec in specs.items()
    }

    usage = None
    if result.get("usage") is not None:
        raw_usage = result["usage"]
        if not isinstance(raw_usage, dict):
            raise ProviderError(capability, "invalid_response", "Decision usage must be an object")
        usage = Usage(
            input_tokens=_count(raw_usage.get("input_tokens"), capability),
            output_tokens=_count(raw_usage.get("output_tokens"), capability),
        )
    decision_id, model, created = result.get("id"), result.get("model"), result.get("created")
    return Decision(
        answers=parsed,
        decision_id=decision_id if isinstance(decision_id, str) and decision_id else None,
        model=model if isinstance(model, str) and model else None,
        created=created if isinstance(created, int) and not isinstance(created, bool) else None,
        usage=usage,
    )


def _count(value, capability: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProviderError(capability, "invalid_response", "Token counts must be non-negative integers")
    return value
