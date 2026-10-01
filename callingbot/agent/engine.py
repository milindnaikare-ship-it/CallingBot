"""Conversation engine: one telephony webhook in, one :class:`VoiceResponse` out.

The engine owns ``Call.llm_messages`` (the raw Claude conversation) and ``Call.engine_state``
(scratch state that must survive between webhooks). Its rules, in the order they matter:

* **Append-only history.** Message 0 is the call-context user message, message 1 the greeting as
  an assistant text block. Each distributor utterance becomes a user message, each Claude
  response is appended verbatim (thinking blocks included) and all tool results for a response
  go back in one user message. Earlier entries are never edited, reordered or removed: that
  would invalidate thinking blocks and the prompt cache. JSON columns are always reassigned
  (``call.llm_messages = [*call.llm_messages, msg]``) because SQLAlchemy does not see in-place
  mutation.
* **Nothing unscreened is spoken.** Every model utterance passes
  :func:`compliance.screen_bot_utterance`; a blocked one is replaced by
  :func:`compliance.safe_reply`, flagged, audited, and the model is told on its next request that
  the reply was not spoken.
* **Deterministic safety nets** that do not depend on the model: silence re-prompts, the opt-out
  detector, turn/time limits, the closing disclaimer and the LLM-failure path (apology + RM
  callback).
* **Operator notes** (engine -> model guidance) travel as a ``system`` message right after the
  user message they relate to when the model supports it, otherwise as an ``[Operator note]``
  text block inside that user message. Notes that must wait for the next distributor utterance
  are kept in ``engine_state["pending_notes"]``.
* **Webhook retries are harmless.** A repeated answer webhook replays the last response instead
  of greeting twice, and anything arriving after the call ended gets a silent hang-up.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from callingbot import compliance
from callingbot.agent.llm import LLMClient, LLMError, LLMResult
from callingbot.agent.prompts import build_call_context, build_system_prompt, render_greeting
from callingbot.agent.tools import ToolContext, build_tool_definitions, execute_tool
from callingbot.knowledge import KnowledgeBase
from callingbot.messaging import Messenger
from callingbot.models import Call, Callback, CallOutcome, CallStatus, Turn, TurnRole, audit
from callingbot.settings import Settings
from callingbot.telephony.base import VoiceAction, VoiceResponse
from callingbot.timeutil import to_local, utcnow

log = logging.getLogger(__name__)

MAX_LLM_ITERATIONS = 4
LOW_CONFIDENCE = 0.5
# Seconds / turns before the hard limit at which the model is asked to wrap up.
WRAP_UP_SECONDS = 60
WRAP_UP_TURNS = 2

NOTE_WRAP_UP = (
    "Time limit approaching: wrap up in your next reply - offer to send the link or a callback, then close."
)
NOTE_LOW_CONFIDENCE = (
    "Low transcription confidence on the last utterance - if anything is unclear, briefly ask the distributor "
    "to confirm or repeat."
)
NOTE_BLOCKED = (
    "Your previous reply was blocked by the compliance filter and was NOT spoken - do not repeat it or "
    'anything similar. The distributor heard this instead: "{spoken}"'
)
NOTE_NOT_DELIVERED = 'Your previous response was not delivered. The distributor heard this instead: "{spoken}" Continue from there.'
NOTE_SILENCE = 'The distributor said nothing for a while, so the platform asked: "{spoken}"'

_OPERATOR_PREFIX = "[Operator note] "
_FAKE_OPERATOR_MARKER = re.compile(r"\[\s*operator\s+note\s*\]", re.IGNORECASE)

# Scripted lines spoken without the LLM, per base language. Unknown languages fall back to English.
_SCRIPTS: dict[str, dict[str, str]] = {
    "reprompt": {
        "en": "Sorry, I couldn't hear you. Are you there?",
        "hi": "माफ़ कीजिए, मुझे आपकी आवाज़ सुनाई नहीं दी। क्या आप लाइन पर हैं?",
    },
    "silence_goodbye": {
        "en": "I'm unable to hear you, so I'll end the call now. Thank you, and have a good day.",
        "hi": "मुझे आपकी आवाज़ सुनाई नहीं दे रही है, इसलिए मैं कॉल समाप्त कर रही हूँ। धन्यवाद, आपका दिन शुभ हो।",
    },
    "repeat": {
        "en": "Sorry, could you please repeat that?",
        "hi": "माफ़ कीजिए, क्या आप दोबारा बता सकते हैं?",
    },
    "goodbye": {
        "en": "Thank you for your time. Goodbye.",
        "hi": "आपके समय के लिए धन्यवाद। नमस्ते।",
    },
    "transfer_hold": {
        "en": "Please hold while I connect you to our relationship manager.",
        "hi": "कृपया लाइन पर बने रहें, मैं आपको हमारे रिलेशनशिप मैनेजर से जोड़ रही हूँ।",
    },
    "opt_out_confirm": {
        "en": "Understood, we won't call you again. Have a good day.",
        "hi": "ठीक है, हम आपको दोबारा कॉल नहीं करेंगे। आपका दिन शुभ हो।",
    },
    "forced_close": {
        "en": "Thank you so much for your time today. Our relationship team will follow up with you. "
        "Have a good day.",
        "hi": "आज आपके समय के लिए बहुत धन्यवाद। हमारी रिलेशनशिप टीम आपसे संपर्क करेगी। आपका दिन शुभ हो।",
    },
    "error_apology": {
        "en": "I'm sorry, I'm having a technical issue. Our relationship manager will call you back shortly. "
        "Thank you for your time.",
        "hi": "माफ़ कीजिए, अभी तकनीकी समस्या आ रही है। हमारे रिलेशनशिप मैनेजर जल्द ही आपको कॉल बैक करेंगे। "
        "आपके समय के लिए धन्यवाद।",
    },
}

# Outcomes after which the closing disclaimer is not spoken: the scheme was not (or must not be)
# pitched to this person.
_NO_DISCLAIMER_OUTCOMES = frozenset({CallOutcome.OPTED_OUT, CallOutcome.WRONG_PERSON, CallOutcome.VOICEMAIL})
_DISCLAIMER_MARKERS = ("market risk", "बाजार जोखिम")


def script(key: str, language: str | None) -> str:
    """Scripted line ``key`` in ``language`` (English when there is no translation)."""
    base = (language or "").strip().lower().replace("_", "-").split("-")[0]
    lines = _SCRIPTS[key]
    return lines.get(base, lines["en"])


def _fold(text: str) -> str:
    # Nukta-insensitive, case-insensitive comparison: "बाज़ार" and "बाजार" are both common.
    return unicodedata.normalize("NFC", text).replace("़", "").casefold()


@dataclass
class _Utterance:
    text: str
    flagged: bool = False
    meta: dict[str, Any] | None = None


@dataclass
class _TurnState:
    utterances: list[_Utterance] = field(default_factory=list)
    tools_called: list[str] = field(default_factory=list)
    end_call: bool = False
    transfer_to: str | None = None
    error: LLMError | None = None
    # True when the model's latest words were not what the distributor heard (refusal, truncated
    # tool call, empty reply) - the model must be told on its next request.
    undelivered: bool = False


class ConversationEngine:
    def __init__(
        self,
        *,
        session: Session,
        kb: KnowledgeBase,
        settings: Settings,
        llm: LLMClient,
        messenger: Messenger,
        now: Callable[[], datetime] = utcnow,
    ):
        self.session = session
        self.kb = kb
        self.settings = settings
        self.llm = llm
        self.messenger = messenger
        self.now = now
        # Built once: both must be byte-identical on every request so the prompt cache hits.
        self.system_prompt = build_system_prompt(kb)
        self.tools = build_tool_definitions(kb)

    # -----------------------------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------------------------

    def start(self, call: Call, *, answered_by: str | None = None) -> VoiceResponse:
        """Call answered: speak the pre-approved greeting (or handle voicemail). Commits."""
        if self._is_over(call):
            return self._response(call, [], "hangup")
        state = call.engine_state or {}
        if call.llm_messages:
            # Webhook retry (or a second answer webhook): replay what we last said, never re-greet.
            return self._response(
                call, list(state.get("last_say") or []), state.get("last_action") or "gather"
            )

        distributor = call.distributor
        codes = {lang.code for lang in self.kb.amc.languages}
        preferred = distributor.preferred_language
        call.language = preferred if preferred in codes else self.kb.amc.default_language
        now = self.now()
        if answered_by is not None:
            call.answered_by = answered_by

        if self._is_machine(answered_by):
            return self._voicemail(call, answered_by, now)

        lang = self.kb.amc.language(call.language)
        greeting = render_greeting(self.kb, lang.code, distributor)
        context = build_call_context(self.kb, distributor, call, to_local(now, self.settings.timezone))
        call.llm_messages = [
            {"role": "user", "content": context},
            {"role": "assistant", "content": [{"type": "text", "text": greeting}]},
        ]
        call.status = CallStatus.IN_PROGRESS
        if call.answered_at is None:
            call.answered_at = now
        audit(
            self.session,
            "disclosure_played",
            call_id=call.id,
            distributor_id=distributor.id,
            text=greeting,
            language=lang.code,
        )
        return self._finish(call, [_Utterance(greeting, meta={"scripted": "greeting"})], "gather", now=now)

    def handle_input(
        self, call: Call, speech_text: str | None, *, confidence: float | None = None
    ) -> VoiceResponse:
        """One conversational turn: the distributor's transcript in, the bot's reply out. Commits."""
        if self._is_over(call):
            return self._response(call, [], "hangup")
        if not call.llm_messages:
            # The answer webhook never reached us; the greeting has not been spoken yet.
            log.warning("Call %s: turn webhook before answer webhook - greeting first", call.id)
            return self.start(call)

        text = " ".join((speech_text or "").split())
        if not text:
            return self._handle_silence(call)

        now = self.now()
        self._add_turn(
            call,
            TurnRole.DISTRIBUTOR,
            text,
            now,
            meta={"confidence": confidence} if confidence is not None else None,
        )
        call.turn_count = (call.turn_count or 0) + 1
        self._update_state(call, silence_streak=0)

        notes = self._take_pending_notes(call)
        if confidence is not None and confidence < LOW_CONFIDENCE:
            notes.append(NOTE_LOW_CONFIDENCE)
        near_limit, limit_reached = self._limits(call, now)
        if near_limit:
            notes.append(NOTE_WRAP_UP)
        self._append_user_message(call, text, notes)

        turn = _TurnState()
        try:
            self._run_llm(call, turn)
        except LLMError as exc:
            turn.error = exc

        opted_out_by_engine = False
        if compliance.detect_opt_out(text) and not {"opt_out", "schedule_callback"} & set(turn.tools_called):
            self._engine_opt_out(call, turn)
            opted_out_by_engine = True
        if turn.error is not None:
            self._handle_llm_error(call, turn, now, apologise=not opted_out_by_engine)

        if call.outcome == CallOutcome.OPTED_OUT:
            # An opt-out always ends the call, whatever else happened this turn.
            turn.end_call, turn.transfer_to = True, None
        if limit_reached and not (turn.end_call or turn.transfer_to):
            turn.utterances.append(
                _Utterance(script("forced_close", call.language), meta={"scripted": "limit"})
            )
            turn.end_call = True

        action: VoiceAction = "transfer" if turn.transfer_to else ("hangup" if turn.end_call else "gather")
        if not turn.utterances:
            key = {"gather": "repeat", "hangup": "goodbye", "transfer": "transfer_hold"}[action]
            turn.utterances.append(_Utterance(script(key, call.language), meta={"scripted": key}))
            turn.undelivered = turn.undelivered or action == "gather"
        if action == "gather":
            self._keep_history_consistent(call, turn)
        return self._finish(call, turn.utterances, action, transfer_to=turn.transfer_to, now=now)

    # -----------------------------------------------------------------------------------------
    # LLM loop
    # -----------------------------------------------------------------------------------------

    def _complete(self, call: Call) -> LLMResult:
        try:
            return self.llm.complete(system=self.system_prompt, tools=self.tools, messages=call.llm_messages)
        except LLMError:
            raise
        except Exception as exc:  # an unexpected client bug must still end the call gracefully
            log.exception("Call %s: unexpected LLM client error", call.id)
            raise LLMError(f"unexpected LLM client error: {type(exc).__name__}: {exc}") from exc

    def _run_llm(self, call: Call, turn: _TurnState) -> None:
        for iteration in range(1, MAX_LLM_ITERATIONS + 1):
            result = self._complete(call)
            content = list(result.content or [])
            tool_uses = [b for b in content if b.get("type") == "tool_use"]

            if result.stop_reason == "refusal":
                # Never run a refused response's tools; keep its text only if it is a complete,
                # tool-free message (a tool_use without its tool_result would break the history).
                if content and not tool_uses:
                    self._append(call, {"role": "assistant", "content": content})
                audit(
                    self.session,
                    "llm_refusal",
                    call_id=call.id,
                    distributor_id=call.distributor_id,
                    model=result.model,
                    iteration=iteration,
                )
                self._speak_fallback(call, turn, "llm_refusal")
                return
            if result.stop_reason == "max_tokens" and tool_uses:
                # The tool input is probably truncated: neither run it nor keep it.
                log.warning("Call %s: response hit max_tokens inside a tool call; not executed", call.id)
                self._speak_fallback(call, turn, "llm_max_tokens")
                return
            if not content:
                log.warning("Call %s: empty LLM response (stop_reason=%s)", call.id, result.stop_reason)
                return

            self._append(call, {"role": "assistant", "content": content})
            text = " ".join(b.get("text", "").strip() for b in content if b.get("type") == "text").strip()
            if text:
                self._collect(call, turn, text)
            if not tool_uses:
                return

            tool_results: list[dict[str, Any]] = []
            for block in tool_uses:
                tool_input = block.get("input")
                outcome = execute_tool(
                    self._tool_context(call),
                    block.get("name", ""),
                    tool_input if isinstance(tool_input, dict) else {},
                )
                turn.tools_called.append(block.get("name", ""))
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block["id"],
                        "content": outcome.content,
                        "is_error": outcome.is_error,
                    }
                )
                turn.end_call = turn.end_call or outcome.end_call
                turn.transfer_to = outcome.transfer_to or turn.transfer_to
            self._append(call, {"role": "user", "content": tool_results})
            if turn.end_call or turn.transfer_to:
                return  # what the model wrote in this response is its goodbye; don't ask again
        log.warning("Call %s: stopped after %d LLM iterations in one turn", call.id, MAX_LLM_ITERATIONS)

    def _collect(self, call: Call, turn: _TurnState, text: str) -> None:
        """Screen one model utterance before it can be spoken."""
        screen = compliance.screen_bot_utterance(text)
        if screen.ok:
            turn.utterances.append(_Utterance(text))
            return
        safe = compliance.safe_reply(call.language)
        log.warning("Call %s: blocked bot utterance (%s)", call.id, ", ".join(screen.violations))
        audit(
            self.session,
            "compliance_flag",
            call_id=call.id,
            distributor_id=call.distributor_id,
            violations=screen.violations,
            original=text,
            replacement=safe,
        )
        self._queue_note(call, NOTE_BLOCKED.format(spoken=safe))
        if not any(u.text == safe for u in turn.utterances):
            turn.utterances.append(
                _Utterance(safe, flagged=True, meta={"violations": screen.violations, "original": text})
            )

    def _speak_fallback(self, call: Call, turn: _TurnState, reason: str) -> None:
        safe = compliance.safe_reply(call.language)
        if not any(u.text == safe for u in turn.utterances):
            turn.utterances.append(_Utterance(safe, meta={"scripted": reason}))
        turn.undelivered = True

    def _tool_context(self, call: Call) -> ToolContext:
        return ToolContext(
            session=self.session,
            call=call,
            distributor=call.distributor,
            kb=self.kb,
            settings=self.settings,
            messenger=self.messenger,
            now_utc=self.now(),
        )

    # -----------------------------------------------------------------------------------------
    # Safety nets
    # -----------------------------------------------------------------------------------------

    def _engine_opt_out(self, call: Call, turn: _TurnState) -> None:
        """The distributor asked not to be called and the model did not act on it: do it ourselves."""
        outcome = execute_tool(
            self._tool_context(call),
            "opt_out",
            {"reason": "Asked not to be called again (detected by the platform)"},
        )
        if outcome.is_error:
            log.error("Call %s: engine opt-out failed: %s", call.id, outcome.content)
        log.info("Call %s: opt-out safety net triggered", call.id)
        turn.utterances = [_Utterance(script("opt_out_confirm", call.language), meta={"scripted": "opt_out"})]
        turn.end_call, turn.transfer_to = True, None

    def _handle_llm_error(self, call: Call, turn: _TurnState, now: datetime, *, apologise: bool) -> None:
        exc = turn.error
        assert exc is not None
        log.error("Call %s: LLM error: %s", call.id, exc)
        call.error = str(exc)[:2000]
        audit(
            self.session,
            "llm_error",
            call_id=call.id,
            distributor_id=call.distributor_id,
            message=str(exc)[:500],
            retryable=exc.retryable,
        )
        if not apologise:
            return
        if "schedule_callback" not in turn.tools_called:
            try:
                when = compliance.next_window_start(
                    self.kb.campaign, now + timedelta(hours=1), self.settings.timezone
                )
            except ValueError:
                when = now + timedelta(hours=1)
            self.session.add(
                Callback(
                    distributor_id=call.distributor_id,
                    call_id=call.id,
                    scheduled_for=when,
                    with_rm=True,
                    notes="Bot error - please call back",
                )
            )
        turn.utterances.append(
            _Utterance(script("error_apology", call.language), meta={"scripted": "llm_error"})
        )
        turn.end_call, turn.transfer_to = True, None

    def _handle_silence(self, call: Call) -> VoiceResponse:
        now = self.now()
        streak = int((call.engine_state or {}).get("silence_streak", 0)) + 1
        call.no_input_count = (call.no_input_count or 0) + 1
        self._update_state(call, silence_streak=streak)
        # Consecutive silences decide the hang-up, so one pause early in a call is not held
        # against the distributor later.
        if streak <= self.settings.no_input_reprompts:
            line = script("reprompt", call.language)
            self._queue_note(call, NOTE_SILENCE.format(spoken=line))
            return self._finish(call, [_Utterance(line, meta={"scripted": "no_input"})], "gather", now=now)
        line = script("silence_goodbye", call.language)
        return self._finish(call, [_Utterance(line, meta={"scripted": "no_input"})], "hangup", now=now)

    def _limits(self, call: Call, now: datetime) -> tuple[bool, bool]:
        """(wrap-up zone, hard limit reached) for turns and elapsed seconds."""
        turns = call.turn_count or 0
        elapsed = (now - call.answered_at).total_seconds() if call.answered_at else 0.0
        max_turns, max_seconds = self.settings.max_call_turns, self.settings.max_call_seconds
        reached = turns >= max_turns or elapsed >= max_seconds
        near = reached or turns >= max_turns - WRAP_UP_TURNS or elapsed >= max_seconds - WRAP_UP_SECONDS
        return near, reached

    def _disclaimer_in(self, text: str) -> bool:
        folded = _fold(text)
        if any(marker in folded for marker in _DISCLAIMER_MARKERS):
            return True
        return any(_fold(self.kb.nfo.disclaimer(lang.code)) in folded for lang in self.kb.amc.languages)

    def _voicemail(self, call: Call, answered_by: str | None, now: datetime) -> VoiceResponse:
        call.status = CallStatus.VOICEMAIL
        call.outcome = CallOutcome.VOICEMAIL
        policy = self.kb.campaign
        say: list[_Utterance] = []
        if (
            (answered_by or "").lower().startswith("machine")
            and policy.leave_voicemail
            and policy.voicemail_message
        ):
            message = (
                policy.voicemail_message.replace("{bot_name}", self.kb.amc.bot_name)
                .replace("{amc_name}", self.kb.amc.name)
                .replace("{name}", call.distributor.name or "")
            )
            say.append(_Utterance(" ".join(message.split()), meta={"scripted": "voicemail"}))
        return self._finish(call, say, "hangup", now=now)

    @staticmethod
    def _is_machine(answered_by: str | None) -> bool:
        value = (answered_by or "").strip().lower()
        return value.startswith("machine") or value == "fax"

    @staticmethod
    def _is_over(call: Call) -> bool:
        terminal = call.status is not None and CallStatus(call.status).is_terminal
        return terminal or bool((call.engine_state or {}).get("ended"))

    # -----------------------------------------------------------------------------------------
    # History and state helpers
    # -----------------------------------------------------------------------------------------

    def _append(self, call: Call, *messages: dict[str, Any]) -> None:
        call.llm_messages = [*(call.llm_messages or []), *messages]

    def _append_user_message(self, call: Call, text: str, notes: list[str]) -> None:
        # A caller saying "[Operator note] ..." must not be able to pass as the platform.
        spoken = _FAKE_OPERATOR_MARKER.sub("(operator note)", text)
        notes = list(dict.fromkeys(notes))
        if not notes:
            self._append(call, {"role": "user", "content": spoken})
        elif self.llm.supports_system_messages:
            # A system message must directly follow the user message it relates to.
            self._append(
                call, {"role": "user", "content": spoken}, {"role": "system", "content": "\n".join(notes)}
            )
        else:
            blocks = [{"type": "text", "text": spoken}]
            blocks += [{"type": "text", "text": _OPERATOR_PREFIX + note} for note in notes]
            self._append(call, {"role": "user", "content": blocks})

    def _keep_history_consistent(self, call: Call, turn: _TurnState) -> None:
        """Make sure the next request is valid and tells the model what was actually heard."""
        spoken = " ".join(u.text for u in turn.utterances)
        last = (call.llm_messages or [{}])[-1]
        if last.get("role") == "system":
            # No assistant reply follows this turn's operator note (refusal / empty reply); a system
            # message must be followed by an assistant turn, so record what the distributor heard.
            self._append(call, {"role": "assistant", "content": [{"type": "text", "text": spoken}]})
        elif turn.undelivered:
            self._queue_note(call, NOTE_NOT_DELIVERED.format(spoken=spoken))

    def _update_state(self, call: Call, **updates: Any) -> None:
        call.engine_state = {**(call.engine_state or {}), **updates}

    def _queue_note(self, call: Call, note: str) -> None:
        pending = list((call.engine_state or {}).get("pending_notes") or [])
        if note not in pending:
            self._update_state(call, pending_notes=[*pending, note])

    def _take_pending_notes(self, call: Call) -> list[str]:
        pending = list((call.engine_state or {}).get("pending_notes") or [])
        if pending:
            self._update_state(call, pending_notes=[])
        return pending

    def _add_turn(
        self,
        call: Call,
        role: TurnRole,
        text: str,
        now: datetime,
        *,
        flagged: bool = False,
        meta: dict[str, Any] | None = None,
    ) -> None:
        self.session.add(
            Turn(call_id=call.id, role=role, text=text, flagged=flagged, meta=meta, created_at=now)
        )

    def _finish(
        self,
        call: Call,
        utterances: list[_Utterance],
        action: VoiceAction,
        *,
        now: datetime,
        transfer_to: str | None = None,
    ) -> VoiceResponse:
        """Closing disclaimer, transcript turns, state and commit for every spoken response."""
        utterances = list(utterances)
        state = call.engine_state or {}
        spoken_before = bool(state.get("disclaimer_spoken"))
        disclaimer_now = any(self._disclaimer_in(u.text) for u in utterances if not u.flagged)
        if (
            action == "hangup"
            and (call.turn_count or 0) >= 1
            and call.outcome not in _NO_DISCLAIMER_OUTCOMES
            and not (spoken_before or disclaimer_now)
        ):
            utterances.append(
                _Utterance(self.kb.nfo.disclaimer(call.language), meta={"scripted": "disclaimer"})
            )
            disclaimer_now = True

        for u in utterances:
            self._add_turn(call, TurnRole.BOT, u.text, now, flagged=u.flagged, meta=u.meta)
        say = [u.text for u in utterances]
        updates: dict[str, Any] = {
            "last_say": say,
            "last_action": action,
            "disclaimer_spoken": spoken_before or disclaimer_now,
        }
        if action != "gather":
            updates["ended"] = True
        self._update_state(call, **updates)
        call.pending_action = None if action == "gather" else action
        self.session.commit()
        return self._response(call, say, action, transfer_to=transfer_to)

    def _response(
        self, call: Call, say: list[str], action: VoiceAction, *, transfer_to: str | None = None
    ) -> VoiceResponse:
        lang = self.kb.amc.language(call.language)
        return VoiceResponse(
            say=say,
            language=lang.code,
            voice=lang.twilio_voice,
            stt_language=lang.stt_language,
            action=action,
            transfer_to=transfer_to if action == "transfer" else None,
            gather_timeout_seconds=self.settings.gather_timeout_seconds,
        )
