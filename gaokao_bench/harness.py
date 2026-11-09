"""Provider-neutral, answer-isolated harness for Gaokao-Bench.

Design constraints:

* one question per request; no paper/year/region/question-number metadata;
* the answer key is never passed to a provider or included in a prompt;
* text and vision runs share the same denominator and scoring rules;
* vision inputs are derived diagram crops, not the evidence screenshots;
* provider errors and parse failures stay in the denominator as zero;
* every attempt, usage record, prompt hash, and output hash is archived.

The module uses only the Python standard library.  The optional Pillow
dependency is needed only for the vision profile, where four diagram crops
are materialised from the checked-in evidence assets.
"""

from __future__ import annotations

import base64
import concurrent.futures
import dataclasses
import datetime as dt
import hashlib
import io
import json
import os
import re
import statistics
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Protocol, Sequence, Tuple

from .scoring import SCORER_VERSION, Dataset, evaluate_submission, load_dataset, validate_dataset

HARNESS_VERSION = "0.0.1"
PROMPT_VERSION = "gbmath-objective-anonymous-v2"
SCORING_PROFILE = "strict-source-audited-points-all-or-nothing"
SUPPORTED_MODALITIES = ("text", "vision")

SYSTEM_PROMPT = """你是一名严谨的数学解题者。只依据用户给出的题目文字和随附图形作答。

要求：
1. 不猜测题目来自哪个年份、地区、考试或题号，也不使用外部检索。
2. 不输出解题过程、解释、Markdown 代码块或多余文字。
3. 只输出一个合法 JSON 对象：{"answer": ...}
4. 单选题的 answer 是一个大写字母，例如 "C"。
5. 多选题的 answer 是升序拼接的大写字母，例如 "ACD"。
6. 单空题的 answer 是字符串；多空题的 answer 是按空位顺序排列的 JSON 数组。
7. 数学表达式使用题面中的 Unicode 记法；分数、根式、区间、向量按题意简洁书写。
"""

# The evidence crops include printed question numbers and neighbouring text.
# These manually reviewed boxes retain only the diagram material.  They are
# applied at runtime; the source evidence files remain untouched.
#
# The boxes were re-audited after alpha.5 exposed truncated diagram edges.
# alpha.6.1 adds GB25-SH-Q11, whose slope and shadow-direction figure is
# necessary to disambiguate the text.  All four boxes include the complete
# diagram and leave a white margin on every edge.  Keep the pixel hashes in
# sync with the reviewed boxes; they are checked at runtime and in tests.
PROMPT_IMAGE_CROPS: Dict[str, Tuple[int, int, int, int]] = {
    "GB25-N1-Q06": (0, 270, 841, 590),
    "GB25-BJ-Q14": (0, 190, 836, 505),
    "GB25-SH-Q11": (0, 185, 841, 430),
    "GB25-TJ-Q03": (0, 45, 841, 375),
}


# SHA-256 over ``WIDTHxHEIGHT:RGB`` plus raw RGB bytes.  This is more stable
# than hashing the PNG container, whose compression may vary across Pillow
# releases, while still pinning the exact pixels reviewed for a prompt.
PROMPT_IMAGE_PIXEL_SHA256: Dict[str, str] = {
    "GB25-N1-Q06": "cf770f349f42ff9de45a0b135278bc1100ccaec5b843f5c31e2bf148daf3bf90",
    "GB25-BJ-Q14": "01e2ab1ae446db206f80b029707435bbf2b5d7eb2a2336d4ab62a6d51fe75415",
    "GB25-SH-Q11": "d7f127f5a89b886116253530d360d644d9f90fbaca83552ed86b379efbedf521",
    "GB25-TJ-Q03": "813b28d19960b19ebcd1dca27726d8913f29c197cd91f8976baa42dfbea0e7e5",
}


class ProviderError(RuntimeError):
    """Raised when a model provider cannot return a usable response."""

    def __init__(
        self,
        message: str,
        *,
        attempts: int = 0,
        retryable: bool = False,
        finish_reason: Optional[str] = None,
    ):
        super().__init__(message)
        self.attempts = attempts
        self.retryable = retryable
        self.finish_reason = finish_reason


@dataclass(frozen=True)
class ImageInput:
    data: bytes
    media_type: str
    sha256: str
    source_sha256: str
    crop_box: Optional[Tuple[int, int, int, int]]
    filename: str = "diagram.png"
    pixel_sha256: Optional[str] = None


@dataclass(frozen=True)
class ProviderResponse:
    text: str
    model: str
    usage: Dict[str, int] = field(default_factory=dict)
    response_id: Optional[str] = None
    finish_reason: Optional[str] = None
    attempts: int = 1
    raw: Dict[str, Any] = field(default_factory=dict)


class Provider(Protocol):
    name: str

    def complete(
        self,
        *,
        system_prompt: str,
        prompt: str,
        images: Sequence[ImageInput],
        model: str,
        timeout: float,
        max_tokens: int,
        temperature: Optional[float],
    ) -> ProviderResponse:
        ...


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def build_prompt(question: Mapping[str, Any]) -> str:
    """Build a prompt using only learner-visible question content.

    Deliberately does not read ``paper_id``, ``number``, ``source``,
    ``display_number``, ``id``, review status, or the answer key.
    """

    stem = str(question.get("stem") or "").strip()
    if not stem:
        raise ValueError("question stem is empty")
    options = question.get("options") or []
    lines = ["题目：", stem]
    if options:
        lines.extend(["", "选项："])
        for option in options:
            label = str(option.get("label") or "").strip()
            text = str(option.get("text") or "").strip()
            lines.append("%s. %s" % (label, text))
    lines.extend(["", "只输出规定的 JSON 对象。"])
    return "\n".join(lines)


def build_prompt_image(question: Mapping[str, Any], data_dir: Path | str) -> Optional[ImageInput]:
    """Return a sanitised diagram crop for a required-vision question."""

    if question.get("asset_dependency") != "required":
        return None
    qid = question["id"]
    crop_box = PROMPT_IMAGE_CROPS.get(qid)
    if crop_box is None:
        raise ValueError("required image has no reviewed prompt crop: %s" % qid)
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("vision profile requires Pillow; install gaokao-bench[harness]") from exc
    source = Path(data_dir) / str(question["image_path"])
    source_bytes = source.read_bytes()
    source_hash = _sha256_bytes(source_bytes)
    expected_source_hash = question.get("image_sha256")
    if expected_source_hash and source_hash != expected_source_hash:
        raise ValueError("source image hash mismatch for %s" % qid)
    with Image.open(io.BytesIO(source_bytes)) as image:
        width, height = image.size
        left, top, right, bottom = crop_box
        if not (0 <= left < right <= width and 0 <= top < bottom <= height):
            raise ValueError("invalid prompt crop for %s: %r" % (qid, crop_box))
        cropped = image.convert("RGB").crop(crop_box)
        pixel_payload = ("%dx%d:RGB" % (cropped.width, cropped.height)).encode("ascii") + cropped.tobytes()
        pixel_hash = _sha256_bytes(pixel_payload)
        expected_pixel_hash = PROMPT_IMAGE_PIXEL_SHA256.get(qid)
        if expected_pixel_hash and pixel_hash != expected_pixel_hash:
            raise ValueError("prompt crop pixel hash mismatch for %s" % qid)
        output = io.BytesIO()
        cropped.save(output, format="PNG", optimize=True)
    payload = output.getvalue()
    payload_hash = _sha256_bytes(payload)
    return ImageInput(
        data=payload,
        media_type="image/png",
        sha256=payload_hash,
        source_sha256=source_hash,
        crop_box=crop_box,
        filename="diagram-%s.png" % payload_hash[:10],
        pixel_sha256=pixel_hash,
    )


def prepare_prompt(question: Mapping[str, Any], data_dir: Path | str, modality: str) -> Tuple[str, Optional[ImageInput]]:
    if modality not in SUPPORTED_MODALITIES:
        raise ValueError("unsupported modality: %s" % modality)
    prompt = build_prompt(question)
    image = build_prompt_image(question, data_dir) if modality == "vision" else None
    return prompt, image


def _strip_markdown_fence(text: str) -> str:
    stripped = text.strip()
    match = re.fullmatch(r"```(?:json|JSON)?\s*(.*?)\s*```", stripped, flags=re.S)
    return match.group(1).strip() if match else stripped


def _json_objects(text: str) -> List[Mapping[str, Any]]:
    decoder = json.JSONDecoder()
    objects: List[Mapping[str, Any]] = []
    index = 0
    while index < len(text):
        start = text.find("{", index)
        if start < 0:
            break
        try:
            value, end = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            index = start + 1
            continue
        if isinstance(value, Mapping):
            objects.append(value)
        index = start + max(end, 1)
    return objects


def _valid_answer_value(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return True
    if isinstance(value, list):
        return 1 <= len(value) <= 8 and all(
            isinstance(item, (str, int, float)) and not isinstance(item, bool) for item in value
        )
    return False


def parse_model_response(text: str) -> Dict[str, Any]:
    """Parse a model answer without inventing content.

    JSON is required by the prompt.  Two conservative fallbacks are accepted:
    an explicit final-answer label, or a response consisting only of one or
    more choice letters.  Everything else is a parse failure.
    """

    if not isinstance(text, str) or not text.strip():
        return {"status": "parse_error", "answer": None, "reason": "empty provider response"}
    cleaned = _strip_markdown_fence(text)
    candidates = [obj for obj in _json_objects(cleaned) if "answer" in obj]
    for candidate in reversed(candidates):
        answer = candidate.get("answer")
        if _valid_answer_value(answer):
            return {"status": "ok_json", "answer": answer, "reason": None}
    matches = list(
        re.finditer(
            r"(?:^|\n)\s*(?:最终答案|答案|final\s+answer|answer)\s*[:：]\s*(.+?)\s*$",
            cleaned,
            flags=re.I | re.M,
        )
    )
    if matches:
        candidate = matches[-1].group(1).strip().strip("`")
        parsed = _parse_review_like_value(candidate)
        if _valid_answer_value(parsed):
            return {"status": "ok_labeled", "answer": parsed, "reason": None}
    choice = cleaned.strip().strip("`").replace(" ", "")
    if re.fullmatch(r"[A-Da-d]{1,4}", choice):
        return {"status": "ok_choice", "answer": choice.upper(), "reason": None}
    return {"status": "parse_error", "answer": None, "reason": "no unambiguous answer object"}


def _parse_review_like_value(text: str) -> Any:
    stripped = text.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return stripped


def _endpoint(base_url: str, suffix: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith(suffix):
        return base
    if base.endswith("/v1") and suffix.startswith("/v1/"):
        return base + suffix[3:]
    return base + suffix


def _redact(text: str, secrets: Sequence[str]) -> str:
    result = text
    for secret in secrets:
        if secret:
            result = result.replace(secret, "[REDACTED]")
    return result


def _post_json(
    url: str,
    payload: Mapping[str, Any],
    headers: Mapping[str, str],
    *,
    timeout: float,
    retries: int,
    secrets: Sequence[str] = (),
) -> Tuple[Dict[str, Any], int]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last_error: Optional[Exception] = None
    for attempt in range(1, retries + 2):
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": "Gaokao-Bench/0.1", **headers},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ProviderError("provider returned non-JSON response", attempts=attempt) from exc
            if not isinstance(parsed, dict):
                raise ProviderError("provider response must be a JSON object", attempts=attempt)
            return parsed, attempt
        except urllib.error.HTTPError as exc:
            raw_error = exc.read(4096).decode("utf-8", errors="replace")
            message = _redact("HTTP %d from provider: %s" % (exc.code, raw_error[:1000]), secrets)
            retryable = exc.code in (408, 409, 425, 429) or exc.code >= 500
            last_error = ProviderError(message, attempts=attempt, retryable=retryable)
            if not retryable or attempt > retries:
                raise last_error from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = ProviderError(
                _redact("provider network error: %s" % exc, secrets),
                attempts=attempt,
                retryable=True,
            )
            if attempt > retries:
                raise last_error from exc
        time.sleep(min(2 ** (attempt - 1), 8))
    if last_error is not None:
        raise last_error
    raise ProviderError("provider request failed without a response", attempts=retries + 1)


def _image_data_url(image: ImageInput) -> str:
    return "data:%s;base64,%s" % (image.media_type, base64.b64encode(image.data).decode("ascii"))


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, *, api_key: str, base_url: str, retries: int = 2, max_retries: Optional[int] = None):
        if not api_key:
            raise ValueError("Anthropic API key is empty")
        if not base_url:
            raise ValueError("Anthropic base URL is empty")
        self.api_key = api_key
        self.base_url = base_url
        self.retries = retries if max_retries is None else max_retries

    def complete(
        self,
        *,
        system_prompt: str,
        prompt: str,
        images: Sequence[ImageInput],
        model: str,
        timeout: float,
        max_tokens: int,
        temperature: Optional[float],
    ) -> ProviderResponse:
        content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
        for image in images:
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": image.media_type,
                        "data": base64.b64encode(image.data).decode("ascii"),
                    },
                }
            )
        payload: Dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system_prompt,
            "messages": [{"role": "user", "content": content}],
        }
        if temperature is not None:
            payload["temperature"] = temperature
        headers = {
            "x-api-key": self.api_key,
            "Authorization": "Bearer %s" % self.api_key,
            "anthropic-version": "2023-06-01",
        }
        response, attempts = _post_json(
            _endpoint(self.base_url, "/v1/messages"),
            payload,
            headers,
            timeout=timeout,
            retries=self.retries,
            secrets=(self.api_key,),
        )
        text_blocks = [
            str(block.get("text") or "")
            for block in response.get("content", [])
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        text = "\n".join(block for block in text_blocks if block)
        if not text:
            finish_reason = response.get("stop_reason")
            raise ProviderError(
                "Anthropic response contains no text block (stop_reason=%s)" % finish_reason,
                attempts=attempts,
                finish_reason=str(finish_reason) if finish_reason is not None else None,
            )
        usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
        return ProviderResponse(
            text=text,
            model=str(response.get("model") or model),
            usage={
                "input_tokens": int(usage.get("input_tokens", 0) or 0),
                "output_tokens": int(usage.get("output_tokens", 0) or 0),
            },
            response_id=response.get("id"),
            finish_reason=response.get("stop_reason"),
            attempts=attempts,
            raw=response,
        )


class OpenAICompatibleProvider:
    name = "openai_compatible"

    def __init__(self, *, api_key: str, base_url: str, retries: int = 2, max_tokens_field: str = "max_tokens"):
        if not api_key:
            raise ValueError("OpenAI-compatible API key is empty")
        if not base_url:
            raise ValueError("OpenAI-compatible base URL is empty")
        if max_tokens_field not in ("max_tokens", "max_completion_tokens"):
            raise ValueError("invalid max_tokens_field")
        self.api_key = api_key
        self.base_url = base_url
        self.retries = retries
        self.max_tokens_field = max_tokens_field

    def complete(
        self,
        *,
        system_prompt: str,
        prompt: str,
        images: Sequence[ImageInput],
        model: str,
        timeout: float,
        max_tokens: int,
        temperature: Optional[float],
    ) -> ProviderResponse:
        if images:
            user_content: Any = [{"type": "text", "text": prompt}]
            user_content.extend(
                {"type": "image_url", "image_url": {"url": _image_data_url(image)}} for image in images
            )
        else:
            user_content = prompt
        payload: Dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            self.max_tokens_field: max_tokens,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        response, attempts = _post_json(
            _endpoint(self.base_url, "/chat/completions"),
            payload,
            {"Authorization": "Bearer %s" % self.api_key},
            timeout=timeout,
            retries=self.retries,
            secrets=(self.api_key,),
        )
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ProviderError("OpenAI-compatible response has no choices", attempts=attempts)
        choice = choices[0]
        message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        content = message.get("content")
        if isinstance(content, list):
            text = "\n".join(
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict) and part.get("type") in ("text", "output_text")
            )
        else:
            text = str(content or "")
        if not text:
            finish_reason = choice.get("finish_reason")
            reasoning_present = bool(message.get("reasoning_content") or message.get("reasoning"))
            raise ProviderError(
                "OpenAI-compatible response contains no final text "
                "(finish_reason=%s, reasoning_content_present=%s)"
                % (finish_reason, str(reasoning_present).lower()),
                attempts=attempts,
                finish_reason=str(finish_reason) if finish_reason is not None else None,
            )
        usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
        return ProviderResponse(
            text=text,
            model=str(response.get("model") or model),
            usage={
                "input_tokens": int(usage.get("prompt_tokens", 0) or 0),
                "output_tokens": int(usage.get("completion_tokens", 0) or 0),
            },
            response_id=response.get("id"),
            finish_reason=choice.get("finish_reason"),
            attempts=attempts,
            raw=response,
        )


class GeminiCliProvider:
    """Use an already-authenticated Gemini CLI in an isolated temp directory."""

    name = "gemini_cli"

    def __init__(self, *, executable: str = "gemini", retries: int = 1, approval_mode: str = "plan"):
        self.executable = executable
        self.retries = retries
        self.approval_mode = approval_mode

    def complete(
        self,
        *,
        system_prompt: str,
        prompt: str,
        images: Sequence[ImageInput],
        model: str,
        timeout: float,
        max_tokens: int,
        temperature: Optional[float],
    ) -> ProviderResponse:
        del max_tokens, temperature
        last_error: Optional[Exception] = None
        for attempt in range(1, self.retries + 2):
            try:
                with tempfile.TemporaryDirectory(prefix="gaokao-bench-gemini-") as temporary:
                    workspace = Path(temporary)
                    image_refs: List[str] = []
                    for image in images:
                        path = workspace / image.filename
                        path.write_bytes(image.data)
                        image_refs.append("@" + image.filename)
                    input_prompt = system_prompt + "\n\n" + prompt
                    if image_refs:
                        input_prompt += "\n\n随附图形：" + " ".join(image_refs)
                    command = [
                        self.executable,
                        "-o",
                        "json",
                        "--approval-mode",
                        self.approval_mode,
                        "--skip-trust",
                    ]
                    if model:
                        command.extend(["-m", model])
                    command.extend(["-p", input_prompt])
                    completed = subprocess.run(
                        command,
                        cwd=str(workspace),
                        text=True,
                        capture_output=True,
                        timeout=timeout,
                        check=False,
                    )
                    if completed.returncode != 0:
                        raise ProviderError(
                            "Gemini CLI exited %d: %s"
                            % (completed.returncode, (completed.stderr or completed.stdout)[-1000:]),
                            attempts=attempt,
                            retryable=True,
                        )
                    parsed = _parse_gemini_cli_output(completed.stdout)
                    text = parsed.get("text") or ""
                    if not text:
                        raise ProviderError("Gemini CLI returned no response text", attempts=attempt)
                    return ProviderResponse(
                        text=text,
                        model=str(parsed.get("model") or model or "gemini-cli"),
                        usage=parsed.get("usage") or {},
                        response_id=parsed.get("response_id"),
                        finish_reason=None,
                        attempts=attempt,
                        raw=parsed.get("raw") or {},
                    )
            except (OSError, subprocess.SubprocessError) as exc:
                last_error = ProviderError("Gemini CLI failed: %s" % exc, attempts=attempt, retryable=True)
            if attempt > self.retries:
                break
            time.sleep(min(2 ** (attempt - 1), 8))
        if isinstance(last_error, ProviderError):
            raise last_error
        raise ProviderError("Gemini CLI failed without a response", attempts=self.retries + 1)


def _parse_gemini_cli_output(stdout: str) -> Dict[str, Any]:
    stripped = stdout.strip()
    if not stripped:
        return {"text": None, "raw": {}}
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        # stream-json may contain one JSON object per line; use the last one.
        payload = None
        for line in reversed(stripped.splitlines()):
            try:
                candidate = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = candidate
            break
        if payload is None:
            return {"text": stripped, "raw": {"unstructured_stdout": stripped[-4000:]}}
    if not isinstance(payload, dict):
        return {"text": stripped, "raw": {"unstructured_stdout": stripped[-4000:]}}
    text = payload.get("response") or payload.get("text") or payload.get("output")
    stats = payload.get("stats") if isinstance(payload.get("stats"), dict) else {}
    usage: Dict[str, int] = {}
    for source_key, target_key in (
        ("input_tokens", "input_tokens"),
        ("output_tokens", "output_tokens"),
        ("total_tokens", "total_tokens"),
    ):
        value = stats.get(source_key, payload.get(source_key))
        if isinstance(value, (int, float)):
            usage[target_key] = int(value)
    return {
        "text": str(text) if text is not None else None,
        "model": payload.get("model"),
        "response_id": payload.get("id") or payload.get("session_id"),
        "usage": usage,
        "raw": payload,
    }


class FixedResponseProvider:
    """Deterministic test provider; never represents a real model score."""

    name = "mock"

    def __init__(self, response_text: str = '{"answer": "A"}', *, fail: bool = False):
        self.response_text = response_text
        self.fail = fail
        self.calls: List[Dict[str, Any]] = []

    def complete(
        self,
        *,
        system_prompt: str,
        prompt: str,
        images: Sequence[ImageInput],
        model: str,
        timeout: float,
        max_tokens: int,
        temperature: Optional[float],
    ) -> ProviderResponse:
        del timeout, max_tokens, temperature
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "prompt": prompt,
                "image_count": len(images),
                "image_sha256": [image.sha256 for image in images],
                "model": model,
            }
        )
        if self.fail:
            raise ProviderError("mock provider failure", attempts=1, retryable=False)
        return ProviderResponse(
            text=self.response_text,
            model=model or "mock",
            usage={"input_tokens": 0, "output_tokens": 0},
            response_id="mock-%d" % len(self.calls),
            finish_reason="stop",
            attempts=1,
        )


def select_questions(
    dataset: Dataset,
    *,
    paper_ids: Optional[Sequence[str]] = None,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    selected = list(dataset.questions)
    if paper_ids:
        paper_set = set(paper_ids)
        known = {paper["id"] for paper in dataset.papers}
        unknown = sorted(paper_set - known)
        if unknown:
            raise ValueError("unknown paper IDs: %s" % ", ".join(unknown))
        selected = [question for question in selected if question["paper_id"] in paper_set]
    if limit is not None:
        if limit <= 0:
            raise ValueError("limit must be positive")
        selected = selected[:limit]
    if not selected:
        raise ValueError("question selection is empty")
    return selected


def _utc_timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _run_id(created_at: str, provider: str, model: str, modality: str, repeats: int) -> str:
    compact = re.sub(r"[^A-Za-z0-9._-]+", "-", model or "unknown")[:48].strip("-") or "unknown"
    stamp = re.sub(r"[^0-9A-Za-z]+", "", created_at)[:20]
    config_hash = hashlib.sha256(
        json.dumps(
            {
                "provider": provider,
                "model": model,
                "modality": modality,
                "repeats": repeats,
                "prompt": PROMPT_VERSION,
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:8]
    return "%s-%s-%s-%s-%s" % (stamp, provider, compact, modality, config_hash)


def _jsonl_write(handle: Any, row: Mapping[str, Any]) -> None:
    handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    handle.flush()


def _aggregate_usage(attempts: Sequence[Mapping[str, Any]]) -> Dict[str, int]:
    totals: Dict[str, int] = {}
    for attempt in attempts:
        usage = attempt.get("usage") if isinstance(attempt.get("usage"), dict) else {}
        for key, value in usage.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                totals[str(key)] = totals.get(str(key), 0) + int(value)
    return totals


def _percentile(values: Sequence[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    index = (len(ordered) - 1) * fraction
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    weight = index - lower
    return float(ordered[lower] * (1.0 - weight) + ordered[upper] * weight)


def _scope_summary(
    results: Sequence[Mapping[str, Any]],
    selected_ids: set,
    answerable_ids: set,
    *,
    selected_questions: int,
    total_questions: int,
) -> Dict[str, Any]:
    rows = [row for row in results if row["question_id"] in selected_ids]
    answerable_rows = [row for row in rows if row["question_id"] in answerable_ids]
    awarded = sum(int(row["awarded"]) for row in rows)
    possible = sum(int(row["points"]) for row in rows)
    answerable_awarded = sum(int(row["awarded"]) for row in answerable_rows)
    answerable_possible = sum(int(row["points"]) for row in answerable_rows)
    correct = sum(1 for row in rows if row["status"] == "correct")
    statuses: Dict[str, int] = {}
    for row in rows:
        statuses[row["status"]] = statuses.get(row["status"], 0) + 1
    return {
        "selected_questions": selected_questions,
        "dataset_questions": total_questions,
        "earned_points": awarded,
        "possible_points": possible,
        "correct_questions": correct,
        "point_accuracy": awarded / possible if possible else 0.0,
        "exact_accuracy": correct / len(rows) if rows else 0.0,
        "status_counts": statuses,
        "full_dataset_denominator_used": selected_questions != total_questions,
        "profile_answerable_questions": len(answerable_rows),
        "profile_answerable_points": answerable_possible,
        "profile_earned_points": answerable_awarded,
        "profile_point_accuracy": answerable_awarded / answerable_possible if answerable_possible else 0.0,
    }


def run_benchmark(
    dataset: Dataset,
    provider: Provider,
    *,
    provider_name: str,
    model: str,
    modality: str,
    repeats: int,
    output_dir: Path | str,
    paper_ids: Optional[Sequence[str]] = None,
    limit: Optional[int] = None,
    timeout: float = 120.0,
    max_tokens: int = 1024,
    temperature: Optional[float] = 0.0,
    created_at: Optional[str] = None,
    prepare_only: bool = False,
    provider_metadata: Optional[Mapping[str, Any]] = None,
    concurrency: int = 1,
) -> Dict[str, Any]:
    """Run a reproducible text/vision evaluation and write an audit bundle."""

    if modality not in SUPPORTED_MODALITIES:
        raise ValueError("unsupported modality: %s" % modality)
    if repeats <= 0:
        raise ValueError("repeats must be positive")
    if concurrency <= 0:
        raise ValueError("concurrency must be positive")
    validate_dataset(dataset)
    selected = select_questions(dataset, paper_ids=paper_ids, limit=limit)
    destination = Path(output_dir).resolve()
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("output directory is not empty: %s" % destination)
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "submissions").mkdir(exist_ok=True)
    (destination / "reports").mkdir(exist_ok=True)
    seen_answers = {question["id"] for question in selected}
    missing_answers = sorted(set(dataset.answer_keys) - seen_answers)

    created_at = created_at or _utc_timestamp()
    run_id = _run_id(created_at, provider_name, model, modality, repeats)
    selected_ids = {question["id"] for question in selected}
    answerable_ids = {
        question["id"]
        for question in selected
        if not (modality == "text" and question.get("asset_dependency") == "required")
    }
    prompt_rows: List[Dict[str, Any]] = []
    for question in selected:
        prompt, image = prepare_prompt(question, dataset.data_dir, modality)
        prompt_hash = _sha256_bytes(prompt.encode("utf-8"))
        prompt_rows.append(
            {
                "question_id": question["id"],
                "paper_id": question["paper_id"],
                "prompt_version": PROMPT_VERSION,
                "prompt_sha256": prompt_hash,
                "prompt_chars": len(prompt),
                "contains_question_id": question["id"] in prompt,
                "contains_dataset_year": "2025" in prompt,
                "image": (
                    {
                        "sha256": image.sha256,
                        "source_sha256": image.source_sha256,
                        "pixel_sha256": image.pixel_sha256,
                        "crop_box": list(image.crop_box) if image.crop_box else None,
                        "media_type": image.media_type,
                        "bytes": len(image.data),
                    }
                    if image
                    else None
                ),
            }
        )
    prompt_manifest_path = destination / "prompts.jsonl"
    with prompt_manifest_path.open("w", encoding="utf-8") as handle:
        for row in prompt_rows:
            _jsonl_write(handle, row)
    if any(row["contains_question_id"] or row["contains_dataset_year"] for row in prompt_rows):
        raise ValueError("prompt isolation check failed: metadata leaked into a prompt")

    manifest: Dict[str, Any] = {
        "harness": "Gaokao-Bench",
        "harness_version": HARNESS_VERSION,
        "prompt_version": PROMPT_VERSION,
        "scorer_version": SCORER_VERSION,
        "run_id": run_id,
        "created_at": created_at,
        "dataset_version": dataset.manifest["dataset_version"],
        "dataset_manifest_sha256": _sha256_bytes((dataset.data_dir / "manifest.json").read_bytes()),
        "scoring_profile": SCORING_PROFILE,
        "provider": provider_name,
        "provider_config": dict(provider_metadata or {}),
        "model": model,
        "modality": modality,
        "repeats": repeats,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "timeout_seconds": timeout,
        "concurrency": concurrency,
        "selected_papers": sorted({question["paper_id"] for question in selected}),
        "selected_questions": len(selected),
        "dataset_questions": len(dataset.questions),
        "selected_possible_points": sum(int(question["points"]) for question in selected),
        "dataset_possible_points": sum(int(question["points"]) for question in dataset.questions),
        "selected_question_ids": sorted(selected_ids),
        "unanswered_question_ids": missing_answers,
        "prompt_isolation": {
            "one_question_per_request": True,
            "answer_key_supplied": False,
            "question_id_in_prompt": False,
            "year_in_prompt": False,
            "paper_or_region_in_prompt": False,
            "vision_assets": "sanitised diagram crops only",
            "text_profile_image_policy": "image-required items are zero without a provider call",
        },
        "prompt_manifest": "prompts.jsonl",
        "contamination": {
            "status": str((dataset.manifest.get("quality") or {}).get("contamination_status", "unspecified")),
            "score_label": "retrospective_public_2025",
            "freshness_claim_allowed": False,
        },
        "status": "prepared" if prepare_only else "running",
        "public_release": False,
    }
    manifest_path = destination / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if prepare_only:
        manifest["status"] = "prepared"
        return manifest

    attempt_path = destination / "attempts.jsonl"
    attempts: List[Dict[str, Any]] = []
    repeat_summaries: List[Dict[str, Any]] = []
    def execute_one(question: Mapping[str, Any], repeat_index: int, position: int) -> Tuple[Dict[str, Any], Any]:
        qid = question["id"]
        prompt, image = prepare_prompt(question, dataset.data_dir, modality)
        images = [image] if image else []
        started = time.monotonic()
        record: Dict[str, Any] = {
            "run_id": run_id,
            "repeat": repeat_index,
            "position": position,
            "question_id": qid,
            "paper_id": question["paper_id"],
            "prompt_sha256": _sha256_bytes(prompt.encode("utf-8")),
            "image_sha256": image.sha256 if image else None,
        }
        parsed_answer = None
        if modality == "text" and question.get("asset_dependency") == "required":
            record.update(
                {
                    "status": "image_unavailable_text_profile",
                    "parse_reason": "the question requires a diagram that the text profile does not supply",
                    "response_text": None,
                    "parsed_answer": None,
                    "usage": {},
                    "provider_attempts": 0,
                }
            )
            record["latency_ms"] = 0.0
            return record, None
        try:
            response = provider.complete(
                system_prompt=SYSTEM_PROMPT,
                prompt=prompt,
                images=images,
                model=model,
                timeout=timeout,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            parsed = parse_model_response(response.text)
            parsed_answer = parsed["answer"]
            record.update(
                {
                    "status": parsed["status"],
                    "parse_reason": parsed["reason"],
                    "response_text": response.text,
                    "requested_model": model,
                    "response_model": response.model,
                    "response_id": response.response_id,
                    "finish_reason": response.finish_reason,
                    "usage": response.usage,
                    "provider_attempts": response.attempts,
                    "parsed_answer": parsed_answer,
                }
            )
        except ProviderError as exc:
            record.update(
                {
                    "status": "provider_error",
                    "parse_reason": None,
                    "error": str(exc),
                    "provider_retryable": exc.retryable,
                    "provider_attempts": exc.attempts,
                    "finish_reason": exc.finish_reason,
                    "response_text": None,
                    "parsed_answer": None,
                }
            )
        except Exception as exc:  # keep the item in the denominator
            record.update(
                {
                    "status": "harness_error",
                    "parse_reason": None,
                    "error": "%s: %s" % (type(exc).__name__, exc),
                    "provider_attempts": 0,
                    "response_text": None,
                    "parsed_answer": None,
                }
            )
        record["latency_ms"] = round((time.monotonic() - started) * 1000.0, 3)
        return record, parsed_answer

    with attempt_path.open("w", encoding="utf-8") as attempt_handle:
        for repeat_index in range(1, repeats + 1):
            answers: Dict[str, Any] = {}
            results_by_position: Dict[int, Tuple[Dict[str, Any], Any]] = {}
            if concurrency == 1:
                for position, question in enumerate(selected, 1):
                    result = execute_one(question, repeat_index, position)
                    results_by_position[position] = result
                    _jsonl_write(attempt_handle, result[0])
            else:
                with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
                    futures = {
                        executor.submit(execute_one, question, repeat_index, position): position
                        for position, question in enumerate(selected, 1)
                    }
                    for future in concurrent.futures.as_completed(futures):
                        position = futures[future]
                        try:
                            result = future.result()
                        except Exception as exc:
                            question = selected[position - 1]
                            result = (
                                {
                                    "run_id": run_id,
                                    "repeat": repeat_index,
                                    "position": position,
                                    "question_id": question["id"],
                                    "paper_id": question["paper_id"],
                                    "status": "harness_error",
                                    "error": "%s: %s" % (type(exc).__name__, exc),
                                    "provider_attempts": 0,
                                    "response_text": None,
                                    "parsed_answer": None,
                                    "latency_ms": 0.0,
                                },
                                None,
                            )
                        results_by_position[position] = result
                        _jsonl_write(attempt_handle, result[0])
            for position in sorted(results_by_position):
                record, parsed_answer = results_by_position[position]
                attempts.append(record)
                if parsed_answer is not None:
                    answers[record["question_id"]] = parsed_answer

            submission = {"answers": answers}
            submission_path = destination / "submissions" / ("repeat-%02d.json" % repeat_index)
            submission_path.write_text(
                json.dumps(submission, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            report = evaluate_submission(dataset, answers)
            report["run_id"] = run_id
            report["repeat"] = repeat_index
            report["scope"] = _scope_summary(
                report["results"],
                selected_ids,
                answerable_ids,
                selected_questions=len(selected),
                total_questions=len(dataset.questions),
            )
            report_path = destination / "reports" / ("repeat-%02d.json" % repeat_index)
            report_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            scope = report["scope"]
            repeat_summaries.append(
                {
                    "repeat": repeat_index,
                    "earned_points": report["summary"]["earned_points"],
                    "possible_points": report["summary"]["possible_points"],
                    "GB-2025-Math-Obj": report["summary"]["GB-2025-Math-Obj"],
                    "scope": {
                        "earned_points": scope["earned_points"],
                        "possible_points": scope["possible_points"],
                        "point_accuracy": scope["point_accuracy"],
                        "correct_questions": scope["correct_questions"],
                        "status_counts": scope["status_counts"],
                        "profile_answerable_questions": scope["profile_answerable_questions"],
                        "profile_answerable_points": scope["profile_answerable_points"],
                        "profile_earned_points": scope["profile_earned_points"],
                        "profile_point_accuracy": scope["profile_point_accuracy"],
                    },
                    "submission_sha256": _sha256_bytes(submission_path.read_bytes()),
                    "report_sha256": _sha256_bytes(report_path.read_bytes()),
                }
            )

    latencies = [float(row["latency_ms"]) for row in attempts]
    provider_errors = sum(1 for row in attempts if row["status"] == "provider_error")
    harness_errors = sum(1 for row in attempts if row["status"] == "harness_error")
    parse_errors = sum(1 for row in attempts if row["status"] == "parse_error")
    finish_reasons: Dict[str, int] = {}
    for row in attempts:
        reason = row.get("finish_reason")
        if reason:
            finish_reasons[str(reason)] = finish_reasons.get(str(reason), 0) + 1
    scope_points = [row["scope"]["earned_points"] for row in repeat_summaries]
    scope_denominators = [row["scope"]["possible_points"] for row in repeat_summaries]
    scope_accuracies = [row["scope"]["point_accuracy"] for row in repeat_summaries]
    profile_accuracies = [row["scope"]["profile_point_accuracy"] for row in repeat_summaries]
    profile_earned = [row["scope"]["profile_earned_points"] for row in repeat_summaries]
    profile_points = [row["scope"]["profile_answerable_points"] for row in repeat_summaries]
    summary = {
        "run_id": run_id,
        "dataset_version": dataset.manifest["dataset_version"],
        "dataset_manifest_sha256": _sha256_bytes((dataset.data_dir / "manifest.json").read_bytes()),
        "harness_version": HARNESS_VERSION,
        "prompt_version": PROMPT_VERSION,
        "scorer_version": SCORER_VERSION,
        "scoring_profile": SCORING_PROFILE,
        "provider": provider_name,
        "model": model,
        "modality": modality,
        "repeats": repeats,
        "concurrency": concurrency,
        "created_at": created_at,
        "conditions": {
            "max_tokens": max_tokens,
            "temperature": temperature,
            "timeout_seconds": timeout,
            "concurrency": concurrency,
        },
        "scope": {
            "selected_papers": manifest["selected_papers"],
            "selected_questions": len(selected),
            "dataset_questions": len(dataset.questions),
            "selected_possible_points": manifest["selected_possible_points"],
            "dataset_possible_points": manifest["dataset_possible_points"],
            "scope_denominator": sorted(set(scope_denominators)),
            "profile_answerable_questions": repeat_summaries[0]["scope"]["profile_answerable_questions"],
            "profile_answerable_points": (
                profile_points[0] if len(set(profile_points)) == 1 else sorted(set(profile_points))
            ),
        },
        "aggregate": {
            "mean_selected_earned_points": statistics.mean(scope_points),
            "min_selected_earned_points": min(scope_points),
            "max_selected_earned_points": max(scope_points),
            "mean_selected_point_accuracy": statistics.mean(scope_accuracies),
            "stdev_selected_point_accuracy": statistics.pstdev(scope_accuracies) if len(scope_accuracies) > 1 else 0.0,
            "mean_profile_answerable_earned_points": statistics.mean(profile_earned),
            "mean_profile_answerable_point_accuracy": statistics.mean(profile_accuracies),
            "stdev_profile_answerable_point_accuracy": statistics.pstdev(profile_accuracies) if len(profile_accuracies) > 1 else 0.0,
            "mean_full_test_metric": statistics.mean(row["GB-2025-Math-Obj"] for row in repeat_summaries),
        },
        "attempts": {
            "total": len(attempts),
            "provider_errors": provider_errors,
            "harness_errors": harness_errors,
            "parse_errors": parse_errors,
            "status_counts": {
                status: sum(1 for row in attempts if row["status"] == status)
                for status in sorted({row["status"] for row in attempts})
            },
            "finish_reasons": finish_reasons,
            "latency_ms": {
                "mean": statistics.mean(latencies) if latencies else 0.0,
                "p50": _percentile(latencies, 0.50),
                "p95": _percentile(latencies, 0.95),
                "max": max(latencies) if latencies else 0.0,
            },
            "usage": _aggregate_usage(attempts),
        },
        "repeats_detail": repeat_summaries,
        "contamination": manifest.get("contamination"),
        "files": {
            "manifest": "manifest.json",
            "prompts": "prompts.jsonl",
            "attempts": "attempts.jsonl",
            "submissions": "submissions/",
            "reports": "reports/",
        },
        "public_release": False,
        "warning": "A mock provider is not a model result." if provider_name == "mock" else None,
    }
    summary_path = destination / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    manifest.update(
        {
            "status": "completed",
            "attempts": len(attempts),
            "summary": "summary.json",
            "summary_sha256": _sha256_bytes(summary_path.read_bytes()),
            "attempts_sha256": _sha256_bytes(attempt_path.read_bytes()),
            "prompts_sha256": _sha256_bytes(prompt_manifest_path.read_bytes()),
        }
    )
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return summary


def resolve_api_credentials(provider: str, api_key_env: Optional[str] = None) -> Tuple[str, str, str]:
    """Resolve key/base URL without ever printing or persisting either value."""

    if provider == "anthropic":
        key_env = api_key_env or ("ANTHROPIC_AUTH_TOKEN" if os.getenv("ANTHROPIC_AUTH_TOKEN") else "ANTHROPIC_API_KEY")
        key = os.getenv(key_env, "")
        base = os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
        return key, base, key_env
    if provider == "openai_compatible":
        key_env = api_key_env or ("OPENAI_API_KEY" if os.getenv("OPENAI_API_KEY") else "DASHSCOPE_API_KEY")
        key = os.getenv(key_env, "")
        base = os.getenv("OPENAI_BASE_URL") or os.getenv("DASHSCOPE_BASE_URL") or "https://dashscope.aliyuncs.com/compatible-mode/v1"
        return key, base, key_env
    return "", "", ""


def build_provider(
    provider: str,
    *,
    api_key_env: Optional[str] = None,
    base_url: Optional[str] = None,
    retries: int = 2,
    mock_response: str = '{"answer": "A"}',
    max_tokens_field: str = "max_tokens",
) -> Tuple[Provider, Dict[str, Any]]:
    """Instantiate a provider and return a safe, non-secret launch record."""

    if provider == "mock":
        return FixedResponseProvider(mock_response), {"provider": "mock", "is_mock": True}
    if provider == "gemini_cli":
        return GeminiCliProvider(retries=retries), {"provider": "gemini_cli", "retries": retries}
    if provider not in ("anthropic", "openai_compatible"):
        raise ValueError("unknown provider: %s" % provider)
    key, default_base, resolved_key_env = resolve_api_credentials(provider, api_key_env)
    if not key:
        raise ValueError("missing API key in the selected environment variable")
    resolved_base = base_url or default_base
    metadata = {
        "provider": provider,
        "key_env": resolved_key_env,
        "key_present": True,
        "base_url": resolved_base,
        "retries": retries,
    }
    if provider == "anthropic":
        return AnthropicProvider(api_key=key, base_url=resolved_base, retries=retries), metadata
    return (
        OpenAICompatibleProvider(
            api_key=key,
            base_url=resolved_base,
            retries=retries,
            max_tokens_field=max_tokens_field,
        ),
        metadata,
    )
