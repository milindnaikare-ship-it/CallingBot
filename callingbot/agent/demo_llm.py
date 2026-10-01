"""Offline, rule-based stand-in for Claude, used when ``LLM_PROVIDER=fake``.

It lets the whole stack - telephony webhooks, conversation engine, tools, messaging, admin
simulator - be demonstrated without an API key. It speaks the same protocol as the real model:
it reads the Messages-API conversation the engine sends, answers with text and ``tool_use``
blocks, and reacts to ``tool_result`` messages. Its behaviour is deliberately simple and fully
deterministic (English and simple Hinglish/Hindi keywords), and every line it says is built
from the approved knowledge base so it passes the compliance screen.

It is not a model of how Claude behaves; the conversation engine's safety nets (compliance
screen, opt-out detector, limits) apply to it exactly as they do in production.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from callingbot import compliance
from callingbot.agent.llm import LLMClient, LLMResult, ToolCall
from callingbot.agent.prompts import CALL_CONTEXT_HEADER, spoken_date
from callingbot.agent.tools import normalize_email
from callingbot.knowledge import KnowledgeBase
from callingbot.timeutil import to_local, to_utc_naive, utcnow

_HINDI_MONTHS = (
    "",
    "जनवरी",
    "फ़रवरी",
    "मार्च",
    "अप्रैल",
    "मई",
    "जून",
    "जुलाई",
    "अगस्त",
    "सितंबर",
    "अक्टूबर",
    "नवंबर",
    "दिसंबर",
)
_CHANNEL_NAMES = {
    "en": {"sms": "SMS", "whatsapp": "WhatsApp", "email": "email"},
    "hi": {"sms": "SMS", "whatsapp": "WhatsApp", "email": "ईमेल"},
}

# Phrases the bot itself says, used to tell which question the distributor is answering.
_EMPANEL_QUESTION = {"en": "Are you already empanelled with us?", "hi": "क्या आप पहले से हमारे साथ empanelled हैं?"}
_LINK_OFFER = {"en": "Shall I send you the empanelment link", "hi": "क्या मैं आपको empanelment link"}
_CALLBACK_OFFER = {
    "en": "Would you like a call back from our relationship manager",
    "hi": "क्या आप चाहेंगे कि हमारे relationship manager",
}


def _rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


_WRONG = _rx(
    r"wrong (number|person)|\bnot me\b|no one (here )?(by|with|of) that name|galat number|गलत न(ं|म्)बर"
)
_HINDI = _rx(r"\bhindi\b|हिंदी|हिन्दी")
_NOT_INTERESTED = _rx(r"not interested|no interest|interest(ed)? nahi|रुचि नहीं|इंटरेस्ट नहीं|दिलचस्पी नहीं")
_BUSY = _rx(
    r"\bbusy\b|\blater\b|tomorrow|call (me )?back|callback|in a meeting|\bdriving\b|baad m(e|ein)|\bkal\b"
    r"|व्यस्त|बाद में|कल"
)
_COMMISSION = _rx(r"commission|brokerage|payout|कमीशन|ब्रोकरेज")
_DOCUMENTS = _rx(r"document|papers|\bkyd\b|कागज़?|दस्तावेज़?")
_ALREADY = _rx(r"already|pehle se|पहले से")
_EMPANEL_WORD = _rx(r"empanel|registered|partner|work with you")
_NOT_EMPANELLED = _rx(
    r"not (yet )?(empanel|registered)|haven'?t (been )?(empanel|registered)|not with you|nahi (hu+a|hai|hoon)"
    r"|नहीं हूँ|नहीं हूं|नहीं हुआ"
)
_WHATSAPP = _rx(r"whats\s?app|व्हाट्सएप|वॉट्सऐप|व्हाट्सऐप")
_EMAIL = _rx(r"e-?mail|\bmail\b|ईमेल")
_SMS = _rx(r"\bsms\b|text message|\bmessage\b|एसएमएस|मैसेज")
_SEND = _rx(r"\bsend\b|bhej|भेज")
_AFFIRM = _rx(
    r"\b(yes|yeah|yep|yup|sure|ok|okay|haan|han|ji|speaking|correct|right|please|go ahead|of course)\b"
    r"|हाँ|हां|जी|ठीक"
)
_NEGATIVE = _rx(r"\b(no|nope|not|nahi|nahin|na)\b|नहीं")
_EMAIL_IN_TEXT = _rx(r"[\w.%+'-]+@[\w-]+(\.[\w-]+)+")
_CONTEXT_NOW = re.compile(r"\((\d{4}-\d{2}-\d{2} \d{2}:\d{2})\)")
_CONTEXT_LANGUAGE = re.compile(r"^Call language: .*\(([A-Za-z]{2,3}-[A-Za-z]{2})\)", re.MULTILINE)

_FACTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (_rx(r"fund manager|who (will )?manages?|manager kaun"), "managers"),
    (_rx(r"exit load"), "exit_load"),
    (_rx(r"minimum|\bsip\b|how much"), "minimum"),
    (_rx(r"benchmark"), "benchmark"),
    (_rx(r"\brisk|riskometer|जोखिम"), "risk"),
    (_rx(r"\bwhen\b|\bdates?\b|\bopen|\bclos|\bkab\b|कब"), "dates"),
    (_rx(r"what (is|kind of) (the |this )?(scheme|fund|nfo)|which category|\bcategory\b"), "about"),
)


def _base(language: str) -> str:
    return "hi" if language.lower().startswith("hi") else "en"


@dataclass
class _ToolResult:
    name: str
    input: dict[str, Any]
    payload: dict[str, Any]
    is_error: bool


@dataclass
class _View:
    """What the demo bot needs to know about the conversation so far."""

    language: str
    now_local: datetime
    stage: str  # "greeting" | "empanel_q" | "link_offer" | "callback_offer" | "other"
    utterance: str | None = None
    tool_results: list[_ToolResult] = field(default_factory=list)
    tool_use_count: int = 0


def _text_of(content: Any, *, skip_operator_notes: bool = True) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if block.get("type") != "text":
            continue
        text = block.get("text", "")
        if skip_operator_notes and text.startswith("[Operator note]"):
            continue
        parts.append(text)
    return " ".join(parts).strip()


class DemoLLM(LLMClient):
    """Deterministic keyword bot implementing :class:`LLMClient` with the engine's tools."""

    def __init__(
        self, kb: KnowledgeBase, *, tz: str = "Asia/Kolkata", now: Callable[[], datetime] | None = None
    ):
        self.kb = kb
        self.model = "demo"
        self.tz = tz
        # Only used when the call context carries no timestamp (it normally does).
        self._now = now or utcnow

    @property
    def supports_system_messages(self) -> bool:
        return False

    # -----------------------------------------------------------------------------------------

    def complete(
        self, *, system: str, tools: list[dict[str, Any]], messages: list[dict[str, Any]]
    ) -> LLMResult:
        view = self._view(messages)
        if view.tool_results:
            return self._after_tools(view)
        return self._reply_to(view)

    # -- reading the conversation -------------------------------------------------------------

    def _view(self, messages: list[dict[str, Any]]) -> _View:
        convo = [m for m in messages if m.get("role") in ("user", "assistant")]
        context = _text_of(convo[0]["content"]) if convo and convo[0]["role"] == "user" else ""
        if CALL_CONTEXT_HEADER not in context:
            context = ""

        language = self.kb.amc.default_language
        if m := _CONTEXT_LANGUAGE.search(context):
            language = m.group(1)
        now_local = to_local(self._now(), self.tz).replace(tzinfo=None)
        if m := _CONTEXT_NOW.search(context):
            now_local = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M")

        # Replay tool calls to learn the current language and which tool ids are already used.
        tool_uses: dict[str, dict[str, Any]] = {}
        errors: set[str] = set()
        for msg in convo:
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if block.get("type") == "tool_use":
                    tool_uses[block["id"]] = block
                elif block.get("type") == "tool_result" and block.get("is_error"):
                    errors.add(block.get("tool_use_id"))
        for tool_id, block in tool_uses.items():
            if block.get("name") == "set_language" and tool_id not in errors:
                language = str((block.get("input") or {}).get("language") or language)

        assistants = [m for m in convo if m["role"] == "assistant"]
        last_bot = _text_of(assistants[-1]["content"]) if assistants else ""
        base = _base(language)
        if len(assistants) <= 1:
            stage = "greeting"
        elif _EMPANEL_QUESTION[base] in last_bot or _EMPANEL_QUESTION["en"] in last_bot:
            stage = "empanel_q"
        elif _LINK_OFFER[base] in last_bot or _LINK_OFFER["en"] in last_bot:
            stage = "link_offer"
        elif _CALLBACK_OFFER[base] in last_bot or _CALLBACK_OFFER["en"] in last_bot:
            stage = "callback_offer"
        else:
            stage = "other"

        view = _View(language=language, now_local=now_local, stage=stage, tool_use_count=len(tool_uses))
        last = convo[-1] if len(convo) > 1 else None
        if last is None or last["role"] != "user":
            return view
        content = last.get("content")
        if isinstance(content, list) and any(b.get("type") == "tool_result" for b in content):
            for block in content:
                if block.get("type") != "tool_result":
                    continue
                use = tool_uses.get(block.get("tool_use_id"), {})
                try:
                    payload = json.loads(block.get("content") or "{}")
                except (TypeError, ValueError):
                    payload = {}
                view.tool_results.append(
                    _ToolResult(
                        name=use.get("name", ""),
                        input=dict(use.get("input") or {}),
                        payload=payload if isinstance(payload, dict) else {},
                        is_error=bool(block.get("is_error")),
                    )
                )
        else:
            view.utterance = _text_of(content)
        return view

    # -- building responses -------------------------------------------------------------------

    def _result(self, view: _View, text: str | None, *calls: tuple[str, dict[str, Any]]) -> LLMResult:
        content: list[dict[str, Any]] = []
        if text:
            content.append({"type": "text", "text": text})
        tool_calls: list[ToolCall] = []
        for i, (name, tool_input) in enumerate(calls, start=1):
            # Unique within the conversation even if a fresh DemoLLM serves each webhook.
            tool_id = f"toolu_demo_{view.tool_use_count + i:04d}"
            content.append({"type": "tool_use", "id": tool_id, "name": name, "input": tool_input})
            tool_calls.append(ToolCall(id=tool_id, name=name, input=dict(tool_input)))
        return LLMResult(
            content=content,
            stop_reason="tool_use" if calls else "end_turn",
            model=self.model,
            text=text or "",
            tool_calls=tool_calls,
        )

    def _close(
        self, view: _View, text: str, outcome: str, summary: str, *, interest: str | None = None
    ) -> LLMResult:
        record = {"outcome": outcome, "interest_level": interest, "summary_for_rm": summary, "objections": []}
        return self._result(view, text, ("record_outcome", record), ("end_call", {"reason": outcome}))

    def _date(self, view: _View, d: date) -> str:
        return f"{d.day} {_HINDI_MONTHS[d.month]}" if _base(view.language) == "hi" else spoken_date(d)

    def _disclaimer(self, view: _View) -> str:
        return " ".join(self.kb.nfo.disclaimer(view.language).split())

    # -- lines ---------------------------------------------------------------------------------

    def _pitch(self, view: _View) -> str:
        nfo = self.kb.nfo
        opens = self._date(view, nfo.nfo_open_date)
        closes = self._date(view, nfo.nfo_close_date) if nfo.nfo_close_date else None
        ended = nfo.nfo_close_date is not None and view.now_local.date() > nfo.nfo_close_date
        if _base(view.language) == "hi":
            if ended:
                intro = f"धन्यवाद। हमारा NFO {nfo.scheme_name} बंद हो चुका है, लेकिन हम आगे की योजनाओं के लिए आपके साथ पार्टनरशिप करना चाहेंगे।"
            else:
                period = f"यह NFO {opens} से {closes} तक खुला रहेगा" if closes else f"यह NFO {opens} को खुलेगा"
                intro = (
                    f"धन्यवाद। मैं हमारे आने वाले NFO, {nfo.scheme_name} के बारे में कॉल कर रही हूँ, जो एक "
                    f"{nfo.category} है। {period} और हम इसके लिए आपके साथ पार्टनरशिप करना चाहेंगे।"
                )
            return f"{intro} {_EMPANEL_QUESTION['hi']}"
        if ended:
            intro = (
                f"Thank you. Our NFO, {nfo.scheme_name}, has closed, but we'd be glad to partner with you for "
                "our future schemes."
            )
        else:
            article = "an" if nfo.category[:1].lower() in "aeiou" else "a"
            period = f"It is open from {opens} to {closes}" if closes else f"It opens on {opens}"
            intro = (
                f"Thank you. I'm calling about our upcoming NFO, {nfo.scheme_name}, {article} {nfo.category}. "
                f"{period}, and we'd be glad to partner with you for it."
            )
        return f"{intro} {_EMPANEL_QUESTION['en']}"

    def _link_offer(self, view: _View, *, channel_hint: bool = False) -> str:
        if _base(view.language) == "hi":
            return f"{_LINK_OFFER['hi']} SMS से भेज दूँ?"
        return f"{_LINK_OFFER['en']}{' by SMS' if channel_hint else ''}?"

    def _value_prop(self, view: _View) -> str:
        if _base(view.language) == "hi":
            return f"आप empanelment हमारे भेजे गए link से ऑनलाइन पूरा कर सकते हैं। {self._link_offer(view)}"
        lead = ""
        if self.kb.amc.distributor_value_props:
            # Approved wording, spoken verbatim: it may be a phrase ("A dedicated RM ...") or a full
            # sentence starting with a brand name, so it is neither re-cased nor fitted into ours.
            prop = " ".join(self.kb.amc.distributor_value_props[0].split()).rstrip(".")
            lead = f"Here's why partners work with us: {prop}. "
        return (
            f"{lead}You can complete empanelment online through the link we send you. "
            f"{self._link_offer(view, channel_hint=True)}"
        )

    def _fact(self, view: _View, key: str) -> str:
        nfo = self.kb.nfo
        pending = "Our team will share those details with you shortly."
        facts = {
            "managers": f"The scheme will be managed by {' and '.join(nfo.fund_managers)}."
            if nfo.fund_managers
            else pending,
            "exit_load": f"The exit load is {nfo.exit_load}." if nfo.exit_load else pending,
            "minimum": (
                f"The minimum investment is {nfo.min_investment}."
                + (f" {nfo.sip_details}." if nfo.sip_details else "")
            )
            if nfo.min_investment
            else pending,
            "benchmark": f"The scheme's benchmark is {nfo.benchmark}." if nfo.benchmark else pending,
            "risk": f"The scheme is rated {nfo.riskometer} on the riskometer, and returns are market linked."
            if nfo.riskometer
            else "Returns are market linked and not guaranteed.",
            "dates": f"The NFO opens on {spoken_date(nfo.nfo_open_date)}"
            + (f" and closes on {spoken_date(nfo.nfo_close_date)}." if nfo.nfo_close_date else "."),
            "about": f"{nfo.scheme_name} is {nfo.scheme_type[:1].lower()}{nfo.scheme_type[1:].strip()}"
            if nfo.scheme_type
            else f"{nfo.scheme_name} is a {nfo.category}.",
        }
        return f"{facts[key]} {self._disclaimer(view)} {self._link_offer(view)}"

    def _fallback(self, view: _View) -> str:
        if _base(view.language) == "hi":
            return f"मैं आपको NFO की जानकारी दे सकती हूँ या empanelment में मदद कर सकती हूँ। {self._link_offer(view)}"
        return f"I can share details about the NFO or help you get empanelled. {self._link_offer(view)}"

    def _callback_slot(self, view: _View, text: str) -> str:
        policy = self.kb.campaign
        if re.search(r"tomorrow|\bkal\b|कल", text, re.IGNORECASE):
            # Late morning: inside any sensible window and rarely the first slot everyone asks for.
            candidate = datetime.combine(
                view.now_local.date() + timedelta(days=1), max(policy.window_start, time(11))
            )
        else:
            candidate = view.now_local + timedelta(hours=2)
        candidate = candidate.replace(second=0, microsecond=0)
        if candidate.minute % 30:
            candidate += timedelta(minutes=30 - candidate.minute % 30)
        try:
            slot_utc = compliance.next_window_start(policy, to_utc_naive(candidate, self.tz), self.tz)
        except ValueError:
            slot_utc = to_utc_naive(candidate, self.tz)
        return f"{to_local(slot_utc, self.tz):%Y-%m-%d %H:%M}"

    # -- reacting to the distributor ------------------------------------------------------------

    def _reply_to(self, view: _View) -> LLMResult:
        text = (view.utterance or "").strip()
        hi = _base(view.language) == "hi"
        if not text:
            return self._result(view, self._fallback(view))

        if compliance.detect_opt_out(text):
            line = (
                "परेशानी के लिए माफ़ी चाहती हूँ। हम आपको दोबारा कॉल नहीं करेंगे। आपका दिन शुभ हो।"
                if hi
                else "I'm sorry for the disturbance. I've noted your request and we won't call you again. "
                "Have a good day."
            )
            return self._result(view, line, ("opt_out", {"reason": "Asked not to be called again"}))
        if _WRONG.search(text):
            line = (
                "माफ़ कीजिए, परेशानी के लिए क्षमा चाहती हूँ। धन्यवाद।"
                if hi
                else ("I'm sorry for the trouble. Thank you for your time, goodbye.")
            )
            return self._close(view, line, "wrong_person", "The call reached the wrong person.")
        codes = [lang.code for lang in self.kb.amc.languages]
        if _HINDI.search(text) and not hi:
            if "hi-IN" in codes:
                return self._result(view, None, ("set_language", {"language": "hi-IN"}))
            return self._result(
                view, "I'm sorry, I can only speak English on this call. " + self._fallback(view)
            )
        if _NOT_INTERESTED.search(text):
            line = (
                "कोई बात नहीं, आपके समय के लिए धन्यवाद। आपका दिन शुभ हो!"
                if hi
                else ("No problem at all, thank you for your time. Have a good day!")
            )
            return self._close(
                view, line, "not_interested", "Distributor was not interested at this time.", interest="cold"
            )

        channel = self._channel(text)
        if _BUSY.search(text) and channel is None:
            notes = "Distributor was busy and asked for a call back."
            return self._result(
                view,
                None,
                (
                    "schedule_callback",
                    {"when_local": self._callback_slot(view, text), "with_rm": True, "notes": notes},
                ),
            )
        if _COMMISSION.search(text):
            return self._result(
                view, f"{' '.join(self.kb.nfo.commission_response.split())} {self._link_offer(view)}"
            )
        if _DOCUMENTS.search(text):
            docs = [" ".join(d.split()) for d in self.kb.amc.empanelment_documents]
            listed = ", ".join(docs[:-1]) + f", and {docs[-1]}" if len(docs) > 1 else "".join(docs)
            line = f"For empanelment you will need {listed[:1].lower()}{listed[1:]}." if docs else ""
            return self._result(view, f"{line} {self._link_offer(view)}".strip())
        for pattern, key in _FACTS:
            if pattern.search(text):
                return self._result(view, self._fact(view, key))

        if channel is not None or (_SEND.search(text) and view.stage in ("link_offer", "other")):
            return self._send(view, text, channel or "sms")
        if _ALREADY.search(text) and (_EMPANEL_WORD.search(text) or view.stage == "empanel_q"):
            return self._already(view)
        if _NOT_EMPANELLED.search(text):
            return self._result(view, self._value_prop(view))

        affirm, negative = bool(_AFFIRM.search(text)), bool(_NEGATIVE.search(text))
        if view.stage == "greeting":
            return self._result(view, self._pitch(view))
        if view.stage == "empanel_q":
            if negative:
                return self._result(view, self._value_prop(view))
            if affirm:
                return self._already(view)
        if view.stage == "link_offer":
            if negative:
                line = (
                    f"कोई बात नहीं। {_CALLBACK_OFFER['hi']} आपको कॉल बैक करें?"
                    if hi
                    else f"No problem. {_CALLBACK_OFFER['en']} instead?"
                )
                return self._result(view, line)
            if affirm:
                return self._send(view, text, "sms")
        if view.stage == "callback_offer":
            if negative:
                line = "कोई बात नहीं, आपके समय के लिए धन्यवाद।" if hi else "No problem, thank you for your time."
                return self._close(
                    view, line, "not_interested", "Declined the link and a callback.", interest="cold"
                )
            if affirm:
                return self._result(
                    view,
                    None,
                    (
                        "schedule_callback",
                        {
                            "when_local": self._callback_slot(view, text),
                            "with_rm": True,
                            "notes": "Distributor asked for a call back from a relationship manager.",
                        },
                    ),
                )
        return self._result(view, self._fallback(view))

    @staticmethod
    def _channel(text: str) -> str | None:
        if _WHATSAPP.search(text):
            return "whatsapp"
        if _EMAIL.search(text) or _EMAIL_IN_TEXT.search(text):
            return "email"
        if _SMS.search(text):
            return "sms"
        return None

    def _send(self, view: _View, text: str, channel: str) -> LLMResult:
        email = None
        if channel == "email" and (m := _EMAIL_IN_TEXT.search(text)):
            email = normalize_email(m.group(0))
        return self._result(view, None, ("send_empanelment_link", {"channel": channel, "email": email}))

    def _already(self, view: _View) -> LLMResult:
        if _base(view.language) == "hi":
            line = (
                "यह बहुत अच्छी बात है, हमारे साथ पार्टनरशिप के लिए धन्यवाद। मैं आपके relationship manager से "
                f"NFO marketing kit भेजने के लिए कहूँगी। {self._disclaimer(view)} आपका दिन शुभ हो!"
            )
        else:
            line = (
                "That's great, thank you for partnering with us. I'll have your relationship manager send you "
                f"the NFO marketing kit. {self._disclaimer(view)} Have a great day!"
            )
        return self._close(
            view,
            line,
            "already_empanelled",
            "Distributor says they are already empanelled. Please share the NFO marketing kit.",
            interest="warm",
        )

    # -- reacting to tool results ---------------------------------------------------------------

    def _after_tools(self, view: _View) -> LLMResult:
        hi = _base(view.language) == "hi"
        names = {r.name for r in view.tool_results}
        if names & {"end_call", "opt_out"}:
            # The engine hangs up without asking again; answer sensibly if a client does ask.
            return self._result(view, "आपका दिन शुभ हो।" if hi else "Have a good day.")

        for r in view.tool_results:
            if r.name == "send_empanelment_link":
                channel = str(r.input.get("channel") or "sms")
                label = _CHANNEL_NAMES[_base(view.language)].get(channel, channel)
                if not r.is_error:
                    if hi:
                        line = f"मैंने आपको {label} पर empanelment link भेज दिया है। {self._disclaimer(view)} आपके समय के लिए धन्यवाद!"
                    else:
                        line = (
                            f"Done, I've sent you the empanelment link by {label}. {self._disclaimer(view)} "
                            "Thank you for your time, have a great day!"
                        )
                    return self._close(
                        view,
                        line,
                        "link_sent",
                        f"Distributor agreed to receive the empanelment link by {label}.",
                        interest="warm",
                    )
                other = "SMS" if channel != "sms" else "WhatsApp"
                line = (
                    f"माफ़ कीजिए, मैं {label} पर link नहीं भेज पाई। क्या मैं {other} पर भेज दूँ?"
                    if hi
                    else f"I'm sorry, I couldn't send it by {label}. Shall I send it by {other} instead?"
                )
                return self._result(view, line)
            if r.name == "schedule_callback":
                if not r.is_error:
                    when = r.payload.get("when_spoken") or ""
                    line = (
                        f"धन्यवाद। हमारे relationship manager आपको {when} कॉल करेंगे। आपका दिन शुभ हो!"
                        if hi
                        else f"Thank you. Our relationship manager will call you on {when}. Have a good day!"
                    )
                    return self._close(
                        view,
                        line,
                        "callback_requested",
                        f"Distributor asked for a call back on {when}.",
                        interest="warm",
                    )
                suggestion = r.payload.get("next_available_local")
                if suggestion and suggestion != r.input.get("when_local"):
                    retry = {**r.input, "when_local": suggestion}
                    return self._result(view, None, ("schedule_callback", retry))
                line = (
                    "मैं हमारे relationship manager से आपको सुविधाजनक समय पर कॉल बैक करने के लिए कहूँगी। धन्यवाद!"
                    if hi
                    else "I'll ask our relationship manager to call you back at a convenient time. "
                    "Thank you, have a good day!"
                )
                return self._close(
                    view, line, "callback_requested", "Distributor asked for a call back; no slot was booked."
                )
            if r.name == "set_language" and not r.is_error:
                return self._result(view, self._pitch(view))
            if r.name == "record_outcome":
                return self._result(view, None, ("end_call", {"reason": "outcome recorded"}))
        return self._result(view, self._fallback(view))
