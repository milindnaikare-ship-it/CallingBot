"""LLM client abstraction and the Claude (Anthropic Messages API) implementation.

Design notes for live voice calls:

* **Latency** - effort defaults to ``low`` (thinking stays short). The model is configurable
  via ``LLM_MODEL``; measure latency and quality on real call transcripts before changing it.
* **Append-only history** - the engine stores each assistant ``content`` list exactly as
  returned (including thinking blocks) and replays it unchanged on the next turn. Never edit
  or delete earlier messages: on current models that invalidates thinking blocks and the
  prompt cache.
* **Prompt caching** - the system prompt (approved AMC/NFO knowledge) and tool definitions are
  byte-identical for every call, so they carry an explicit cache breakpoint; top-level
  automatic caching covers the growing per-call conversation.
* **Refusal fallback** - server-side ``fallbacks: "default"`` (Claude API only) re-runs a
  request declined by a safety classifier on Anthropic's recommended fallback model.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import anthropic

from callingbot.settings import Settings

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"

# Models known to accept the features we use. Matched by prefix so dated/variant ids still match.
_EFFORT_UNSUPPORTED_PREFIXES = ("claude-haiku-4-5", "claude-sonnet-4-5", "claude-3")
_FALLBACK_MODELS_PREFIXES = ("claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-1")
_SYSTEM_MESSAGE_PREFIXES = (
    "claude-opus-5-5",
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-fable-5",
    "claude-mythos-5",
    "claude-sonnet-5-5",
)


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class LLMResult:
    content: list[dict[str, Any]]  # assistant content blocks, verbatim (JSON-safe) - append to history as-is
    stop_reason: str | None
    model: str
    text: str = ""  # concatenation of text blocks
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)

    @property
    def refused(self) -> bool:
        return self.stop_reason == "refusal"


class LLMError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class LLMClient(ABC):
    """Minimal interface the conversation engine depends on."""

    model: str = "unknown"

    @abstractmethod
    def complete(
        self, *, system: str, tools: list[dict[str, Any]], messages: list[dict[str, Any]]
    ) -> LLMResult:
        """Run one Messages API request. ``messages`` must be passed through unmodified."""

    @property
    def supports_system_messages(self) -> bool:
        """Whether mid-conversation ``{"role": "system"}`` messages are accepted (operator notes)."""
        return self.model.startswith(_SYSTEM_MESSAGE_PREFIXES)


def _result_from_content(
    content: list[dict[str, Any]], stop_reason: str | None, model: str, usage=None
) -> LLMResult:
    text = " ".join(b.get("text", "").strip() for b in content if b.get("type") == "text").strip()
    tool_calls = [
        ToolCall(id=b["id"], name=b["name"], input=dict(b.get("input") or {}))
        for b in content
        if b.get("type") == "tool_use"
    ]
    return LLMResult(
        content=content,
        stop_reason=stop_reason,
        model=model,
        text=text,
        tool_calls=tool_calls,
        usage=usage or {},
    )


class AnthropicLLM(LLMClient):
    def __init__(self, settings: Settings, client: anthropic.Anthropic | None = None):
        self.settings = settings
        self.model = settings.llm_model
        # The SDK resolves credentials from the environment (ANTHROPIC_API_KEY, ...).
        self.client = client or anthropic.Anthropic(
            timeout=settings.llm_timeout_seconds, max_retries=settings.llm_max_retries
        )

    def _request_kwargs(
        self, system: str, tools: list[dict[str, Any]], messages: list[dict[str, Any]]
    ) -> dict:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.settings.llm_max_tokens,
            # Stable prefix: tools -> system. Explicit breakpoint here caches both for every call.
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "tools": tools,
            "messages": messages,
            # Automatic breakpoint on the growing conversation tail.
            "cache_control": {"type": "ephemeral"},
        }
        if not self.model.startswith(_EFFORT_UNSUPPORTED_PREFIXES):
            kwargs["output_config"] = {"effort": self.settings.llm_effort}
        if self.settings.llm_enable_fallbacks and self.model.startswith(_FALLBACK_MODELS_PREFIXES):
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        return kwargs

    def complete(
        self, *, system: str, tools: list[dict[str, Any]], messages: list[dict[str, Any]]
    ) -> LLMResult:
        kwargs = self._request_kwargs(system, tools, messages)
        try:
            response = self.client.beta.messages.create(**kwargs)
        except anthropic.RateLimitError as e:
            raise LLMError(f"rate limited: {e.message}", retryable=True) from e
        except anthropic.BadRequestError as e:
            raise LLMError(f"bad request: {e.message}") from e
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
            raise LLMError(f"authentication/permission error: {e.message}") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"API error {e.status_code}: {e.message}", retryable=e.status_code >= 500) from e
        except anthropic.APIConnectionError as e:  # includes timeouts
            raise LLMError(f"connection error: {e}", retryable=True) from e

        data = response.to_dict(mode="json")
        # The `fallback` block is only an audit marker for a server-side model switch; drop it
        # from the replayed history (allowed by the API) and log it instead.
        content = []
        for block in data.get("content", []):
            if block.get("type") == "fallback":
                log.warning("LLM fallback: %s -> %s", block.get("from"), block.get("to"))
                continue
            content.append(block)
        usage = data.get("usage") or {}
        if response.stop_reason == "refusal":
            log.warning("LLM refusal (request id %s): %s", response._request_id, data.get("stop_details"))
        log.debug(
            "LLM %s stop=%s in=%s cache_read=%s out=%s",
            response.model,
            response.stop_reason,
            usage.get("input_tokens"),
            usage.get("cache_read_input_tokens"),
            usage.get("output_tokens"),
        )
        return _result_from_content(content, response.stop_reason, response.model, usage)


class ScriptedLLM(LLMClient):
    """Test double: returns queued responses and records every request.

    Each queued item is either an ``LLMResult`` or a callable ``(messages) -> LLMResult``.
    Helpers :func:`text_reply` and :func:`tool_reply` build results concisely.
    """

    def __init__(
        self, responses: Iterable[LLMResult | Callable[[list[dict]], LLMResult]] = (), model="scripted"
    ):
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.model = model

    def push(self, *responses: LLMResult | Callable[[list[dict]], LLMResult]) -> None:
        self.responses.extend(responses)

    def complete(
        self, *, system: str, tools: list[dict[str, Any]], messages: list[dict[str, Any]]
    ) -> LLMResult:
        import copy

        self.requests.append({"system": system, "tools": tools, "messages": copy.deepcopy(messages)})
        if not self.responses:
            raise LLMError("ScriptedLLM: no more scripted responses")
        item = self.responses.pop(0)
        return item(messages) if callable(item) else item


_tool_counter = 0


def text_reply(text: str, *, stop_reason: str = "end_turn") -> LLMResult:
    return _result_from_content([{"type": "text", "text": text}], stop_reason, "scripted")


def tool_reply(*calls: tuple[str, dict[str, Any]], text: str | None = None) -> LLMResult:
    """Build a tool_use response, e.g. ``tool_reply(("opt_out", {"reason": "asked"}), text="Sure.")``."""
    global _tool_counter
    content: list[dict[str, Any]] = []
    if text:
        content.append({"type": "text", "text": text})
    for name, inp in calls:
        _tool_counter += 1
        content.append({"type": "tool_use", "id": f"toolu_test_{_tool_counter}", "name": name, "input": inp})
    return _result_from_content(content, "tool_use", "scripted")


def refusal_reply() -> LLMResult:
    return _result_from_content([], "refusal", "scripted")


def build_llm(settings: Settings) -> LLMClient:
    if settings.llm_provider == "fake":
        from callingbot.agent.demo_llm import DemoLLM
        from callingbot.knowledge import load_knowledge

        return DemoLLM(load_knowledge(settings.config_dir))
    return AnthropicLLM(settings)
