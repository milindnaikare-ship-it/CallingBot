"""Compliance guardrails: calling window, internal DNC list, bot-utterance screen, opt-out detection.

Regulatory background (India) that shapes these rules:

* **TRAI TCCCPR / DLT** - promotional calls only inside permitted hours, honour DND/NCPR
  preferences and opt-outs immediately. The calling window here is the AMC's (stricter)
  business-hours policy from ``config/campaign.yaml``; NCPR scrubbing happens upstream.
* **SEBI advertising code for mutual funds** - no assured/guaranteed returns, no return
  projections, no past-performance claims for an NFO (it has no track record), no "risk-free"
  language for a Very High risk equity scheme, and no investment advice in a distributor call.
* **SEBI commission rules / AMFI code of conduct** - no upfront commission, no inducements; the
  only approved commission text is non-numeric, so any figure is a violation.

The utterance screen and opt-out detector are deliberately *deterministic* (regular
expressions, no LLM): they are a safety net that must behave identically in tests, demos and
production, and every decision must be explainable to a compliance reviewer. They cover English,
romanised Hindi ("Hinglish") and Devanagari Hindi.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from callingbot import funnel
from callingbot.knowledge import CampaignPolicy
from callingbot.models import Callback, CallbackStatus, Distributor, DNCEntry, EmpanelmentStatus, audit
from callingbot.phone import mask_phone, normalize_indian_mobile
from callingbot.timeutil import to_local, to_utc_naive

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------------------------
# Calling window
# ---------------------------------------------------------------------------------------------

# How far ahead next_window_start looks. A policy whose next window is further away than this
# (e.g. a holiday list covering two months) is almost certainly misconfigured.
_MAX_SCAN_DAYS = 60


@dataclass(frozen=True)
class WindowDecision:
    allowed: bool
    reason: str | None  # None | "non_calling_day" | "holiday" | "before_window" | "after_window"
    next_allowed_utc: datetime | None  # naive UTC start of next window when not allowed


def _as_naive_utc(dt: datetime) -> datetime:
    # Callers should pass naive UTC (the storage convention); accept aware values defensively so
    # a stray aware datetime cannot silently shift the window by the UTC offset.
    return to_utc_naive(dt) if dt.tzinfo is not None else dt


def check_calling_window(
    policy: CampaignPolicy, now_utc: datetime, tz: str = "Asia/Kolkata"
) -> WindowDecision:
    """Decide whether the dialer may place a call at ``now_utc`` under ``policy``.

    The policy is expressed in local business time (IST by default), so the check converts
    ``now_utc`` to ``tz`` first: 20:00 UTC on a Monday is already 01:30 IST on Tuesday.
    The window is half-open: ``window_start <= local time < window_end``.
    """
    now_utc = _as_naive_utc(now_utc)
    local = to_local(now_utc, tz)
    local_time = local.time().replace(tzinfo=None)
    if local.weekday() not in policy.calling_days:
        reason = "non_calling_day"
    elif local.date() in set(policy.holidays):
        reason = "holiday"
    elif local_time < policy.window_start:
        reason = "before_window"
    elif local_time >= policy.window_end:
        reason = "after_window"
    else:
        return WindowDecision(allowed=True, reason=None, next_allowed_utc=None)

    try:
        next_allowed = next_window_start(policy, now_utc, tz)
    except ValueError:
        # Never let a misconfigured holiday list crash the dial loop: the call is still refused.
        log.warning("No calling window within %d days of %s; check campaign policy", _MAX_SCAN_DAYS, now_utc)
        next_allowed = None
    return WindowDecision(allowed=False, reason=reason, next_allowed_utc=next_allowed)


def next_window_start(policy: CampaignPolicy, after_utc: datetime, tz: str = "Asia/Kolkata") -> datetime:
    """Earliest naive-UTC instant ``>= after_utc`` that lies inside the calling window.

    Returns ``after_utc`` itself when it is already inside a window. Raises ``ValueError`` if no
    window exists in the next ``_MAX_SCAN_DAYS`` days.
    """
    after_utc = _as_naive_utc(after_utc)
    zone = ZoneInfo(tz)
    first_day = to_local(after_utc, tz).date()
    holidays: set[date] = set(policy.holidays)
    for offset in range(_MAX_SCAN_DAYS + 1):
        day = first_day + timedelta(days=offset)
        if day.weekday() not in policy.calling_days or day in holidays:
            continue
        # Compare in UTC so the result is correct for any zone, including ones with DST.
        start_utc = to_utc_naive(datetime.combine(day, policy.window_start, tzinfo=zone))
        end_utc = to_utc_naive(datetime.combine(day, policy.window_end, tzinfo=zone))
        if after_utc < end_utc:
            return max(after_utc, start_utc)
    raise ValueError(f"no calling window within {_MAX_SCAN_DAYS} days after {after_utc.isoformat()} ({tz})")


# ---------------------------------------------------------------------------------------------
# Internal do-not-call list
# ---------------------------------------------------------------------------------------------


def _dnc_key(phone: str | None) -> str:
    # Store and look up E.164 so "98765 43210" and "+919876543210" are the same entry. Numbers
    # that are not Indian mobiles (landlines, foreign) are kept verbatim rather than dropped:
    # an opt-out must never be lost because of formatting.
    return normalize_indian_mobile(phone) or (phone or "").strip()


def is_dnc(session: Session, phone: str) -> bool:
    """True if ``phone`` is on the internal DNC list or belongs to a distributor marked do-not-call."""
    key = _dnc_key(phone)
    if not key:
        return False
    if session.scalar(select(DNCEntry.id).where(DNCEntry.phone == key).limit(1)) is not None:
        return True
    flagged = session.scalar(
        select(Distributor.id)
        .where(
            or_(Distributor.phone == key, Distributor.alt_phone == key),
            or_(Distributor.do_not_call.is_(True), Distributor.status == EmpanelmentStatus.DO_NOT_CALL),
        )
        .limit(1)
    )
    return flagged is not None


def add_to_dnc(session: Session, phone: str, *, reason: str, source: str) -> DNCEntry:
    """Put ``phone`` on the internal DNC list, mark every distributor using it as do-not-call and
    cancel their pending callbacks / follow-up requests (no one may phone them on our behalf).

    Idempotent: an existing entry is returned unchanged (the first reason/source is kept as the
    original evidence). Every request is still audited as ``dnc_added`` - a repeated opt-out is
    itself compliance-relevant (it may mean a call slipped through). Flushes; caller commits.
    Raises ``ValueError`` for an empty phone.
    """
    key = _dnc_key(phone)
    if not key:
        raise ValueError("add_to_dnc: phone is required")

    entry = session.scalar(select(DNCEntry).where(DNCEntry.phone == key))
    new_entry = entry is None
    if entry is None:
        entry = DNCEntry(
            phone=key, reason=(reason or None) and reason[:200], source=(source or None) and source[:100]
        )
        session.add(entry)

    distributors = session.scalars(
        select(Distributor).where(or_(Distributor.phone == key, Distributor.alt_phone == key))
    ).all()
    canceled: dict[int, int] = {}
    for d in distributors:
        funnel.advance_status(d, EmpanelmentStatus.DO_NOT_CALL)
        if not d.dnc_reason and reason:
            d.dnc_reason = reason[:200]
        pending = session.scalars(
            select(Callback).where(Callback.distributor_id == d.id, Callback.status == CallbackStatus.PENDING)
        ).all()
        for cb in pending:
            cb.status = CallbackStatus.CANCELED
        canceled[d.id] = len(pending)

    detail = {"phone": mask_phone(key), "reason": reason, "source": source, "new_entry": new_entry}
    if distributors:
        for d in distributors:
            audit(session, "dnc_added", distributor_id=d.id, callbacks_canceled=canceled[d.id], **detail)
    else:
        audit(session, "dnc_added", **detail)
    session.flush()
    return entry


# ---------------------------------------------------------------------------------------------
# Text normalisation and regex building blocks (shared by the screen and the opt-out detector)
# ---------------------------------------------------------------------------------------------

# Devanagari block minus the dandas (U+0964/U+0965), which are sentence punctuation.
_DEV = "\u0900-\u0963\u0966-\u097f"
_L = rf"(?<![\w{_DEV}])"  # left word boundary that also works for Devanagari (\b does not)
_R = rf"(?![\w{_DEV}])"  # right word boundary
_TOK = r"[^\s.,;:!?।॥()\[\]\"]+"  # one word; never crosses clause punctuation


def _gap(n: int) -> str:
    """Up to ``n`` arbitrary words (each followed by whitespace) inside one clause."""
    return rf"(?:{_TOK}\s+){{0,{n}}}"


# Words that, inside a short noun-phrase gap, signal we have left the phrase ("best support for
# the NFO" is not "best NFO").
_FUNCTION_WORDS = r"(?:for|to|of|with|in|on|at|from|and|or|by|about|than|that|which)"


def _np_gap(n: int) -> str:
    return rf"(?:(?!{_FUNCTION_WORDS}\s){_TOK}\s+){{0,{n}}}"


_NUKTA = "\u093c"
_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff"))
_QUOTES = str.maketrans({"\u2019": "'", "\u2018": "'", "\u02bc": "'", "`": "'"})


def _strip_nukta(s: str) -> str:
    # NFC decomposes precomposed nukta letters (U+0958-U+095F) into base + U+093C, so stripping
    # U+093C afterwards makes "फ़ोन" and "फोन" (both common spellings) identical.
    return unicodedata.normalize("NFC", s).replace(_NUKTA, "")


def _normalize(text: str | None) -> str:
    if not text:
        return ""
    return _strip_nukta(text).translate(_ZERO_WIDTH).translate(_QUOTES)


def _rx(src: str) -> re.Pattern[str]:
    # Patterns go through the same nukta folding as input text; they never contain U+093C as
    # part of regex syntax, so this is safe.
    return re.compile(_strip_nukta(src), re.IGNORECASE)


_NUMWORD = (
    r"(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|"
    r"sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred)"
)
_NUM = (
    rf"(?:(?<![\d.])\d[\d,]*(?:\.\d+)?"
    rf"|{_L}(?:a\s+)?{_NUMWORD}(?:[\s-]+{_NUMWORD})*(?:\s+point\s+{_NUMWORD})?{_R})"
)
_HI_PCT_WORD = r"(?:प्रतिशत|फीसदी|फीसद|परसेंट)"
_PCT_WORD = rf"(?:percent|per\s?cent|pc|pct|parsent|pratishat|fisadi|feesadi|{_HI_PCT_WORD})"
# A percentage: "12%", "12 percent", "twelve per cent", "बारह प्रतिशत".
_PCT = rf"(?:{_NUM}\s*(?:%|{_PCT_WORD}{_R})|{_L}[{_DEV}]+\s+{_HI_PCT_WORD}{_R})"
_TIMES = rf"(?:{_NUM}\s*(?:x|times|fold|गुना|guna){_R})"

# Pseudo-negations: phrases that contain a negator but do not negate the claim that follows
# ("Don't worry, returns are guaranteed"). They are blanked out before the negation check.
_PSEUDO_NEGATION = _rx(
    r"(?:don'?t|do\s+not)\s+worry|no\s+doubt|without\s+(?:a|any)\s+doubt|no\s+problem|no\s+worries|"
    r"not\s+only|not\s+just|no\s+matter|no\s+question|never\s+fails?|nothing\s+to\s+worry|"
    r"(?:fikar|fikr|chinta|tension)\s+(?:mat|na|nahi|nahin)|koi\s+(?:shak|shaq|dikkat|baat)\s+nahi|"
    r"(?:चिंता|फिक्र|फिकर|टेंशन)\s+(?:मत|ना|न|नहीं)|कोई\s+(?:शक|दिक्कत|बात)\s+नहीं"
)
_NEGATORS = frozenset(
    {
        "not", "no", "never", "cannot", "cant", "dont", "doesnt", "isnt", "arent", "wasnt", "werent",
        "wont", "nobody", "none", "neither", "nor", "without", "nothing", "hardly",
        "nahi", "nahin", "nahee", "nhi", "nai", "koi", "bina", "mat",
        "नहीं", "नही", "कोई", "बिना", "मत",
    }
)  # fmt: skip
# Clause-joining words: a negator on the far side of one belongs to a different claim
# ("There is no lock-in but returns are guaranteed"). "or" is deliberately absent: it mostly
# coordinates within one negated claim ("no gifts or incentives").
_CONJUNCTIONS = frozenset(
    {"and", "but", "so", "yet", "however", "although", "though", "while", "because",
     "aur", "lekin", "magar", "kyunki", "और", "लेकिन", "मगर", "क्योंकि"}
)  # fmt: skip
_CLAUSE_BOUNDARY = re.compile(r"[!?;:।॥\n]|[.,](?!\d)")
_WORD = re.compile(_TOK)
_NEG_BEFORE = 5  # words checked before a trigger (English negates before the claim)
_NEG_AFTER = 4  # words checked after it (Hindi/Hinglish negate at the end: "गारंटी नहीं है")


def _is_negator(token: str) -> bool:
    t = token.casefold().strip("'")
    return t in _NEGATORS or t.endswith("n't")


def _is_negated(masked: str, start: int, end: int) -> bool:
    """True if a negator sits inside the match or within a few words of it in the same clause."""
    clause_start = 0
    for m in _CLAUSE_BOUNDARY.finditer(masked, 0, start):
        clause_start = m.end()
    boundary = _CLAUSE_BOUNDARY.search(masked, end)
    clause_end = boundary.start() if boundary else len(masked)

    window: list[str] = _WORD.findall(masked[start:end])
    before = _WORD.findall(masked[clause_start:start])
    for tok in reversed(before[-_NEG_BEFORE:]):
        if tok.casefold() in _CONJUNCTIONS:
            break
        window.append(tok)
    for tok in _WORD.findall(masked[end:clause_end])[:_NEG_AFTER]:
        if tok.casefold() in _CONJUNCTIONS:
            break
        window.append(tok)
    return any(_is_negator(t) for t in window)


@dataclass(frozen=True)
class _Pattern:
    regex: re.Pattern[str]
    # False when the claim is itself phrased with a negator ("no risk", "can't lose") or when any
    # mention is a violation regardless of framing (return figures, commission figures).
    negatable: bool = True


def _p(src: str, *, negatable: bool = True) -> _Pattern:
    return _Pattern(_rx(src), negatable)


# ---------------------------------------------------------------------------------------------
# Bot-utterance screen
# ---------------------------------------------------------------------------------------------

_GUARANTEE_TARGET = (
    r"(?:returns?|income|profits?|gains?|capital|principal|money|investments?|corpus|payouts?|dividends?|"
    r"interest|growth|munafa|munaafa|paisa|paise|kamai|labh|"
    r"रिटर्न|रिटर्न्स|मुनाफा|मुनाफे|लाभ|कमाई|पैसा|पैसे|पूंजी|निवेश|ब्याज)"
)
_GUARANTEE_WORD = r"(?:guaranteed?|guarantees|guaranteeing|assured|assurance|गारंटी|गारंटीड|गारंटेड)"
_RET_STRONG = r"(?:returns?|roi|cagr|yields?|xirr|irr|munafa|munaafa|रिटर्न|रिटर्न्स|मुनाफा|मुनाफे)"
_RET_SOFT = r"(?:growth|gains?|profits?|appreciation|interest\s+rate|labh|fayda|faayda|लाभ|फायदा|ब्याज)"
_GROW_VERB = (
    r"(?:grow|grows|grew|growing|appreciate|appreciates|multiply|multiplies|earn|earns|earning|make|makes|"
    r"deliver|delivers|generate|generates|give|gives|fetch|fetches|yield|yields|expect|expects|expecting)"
)
_HI_GROW_VERB = (
    r"(?:badhega|badhegi|badhenge|milega|milegi|milenge|dega|degi|denge|kamaoge|kamayenge|"
    r"बढ़ेगा|बढ़ेगी|बढ़ेंगे|मिलेगा|मिलेगी|मिलेंगे|देगा|देगी|देंगे|कमाएंगे|कमाओगे)"
)
_MONEY = r"(?:money|investments?|capital|corpus|wealth|amount|paisa|paise|rakam|पैसा|पैसे|रकम|पूंजी|निवेश)"
_GOOD = (
    r"(?:good|high|great|superb|excellent|strong|attractive|handsome|bumper|huge|double[\s-]digit|higher|"
    r"better|best|solid|healthy|superior|stellar|exceptional|impressive)"
)
_COMM = (
    r"(?:commissions?|brokerages?|trail(?:\s+commission)?|trails|payouts?|upfront|kickbacks?|incentives?|"
    r"komishan|kamishan|kameeshan|dalali|कमीशन|ब्रोकरेज|ट्रेल|पेआउट|दलाली)"
)
_TIME_UNIT = r"(?:years?|yrs?|months?|days?|weeks?|hours?|minutes?|working|business|साल|महीने|दिन)"
_AMOUNT = (
    rf"(?:{_PCT}|{_L}(?:rs\.?|inr|rupees?)\s*{_NUM}|₹\s*{_NUM}"
    rf"|{_NUM}\s*(?:bps|basis\s+points?|rupees?|rs|paise|lakhs?|crores?|रुपये|रुपए|रुपया){_R}|(?<![\d.])\d+\.\d+"
    rf"|(?<![\d.])\d+(?![\d.,]|[a-z])(?!\s*{_TIME_UNIT}))"
)

_RULES: tuple[tuple[str, tuple[_Pattern, ...]], ...] = (
    (
        "guaranteed_returns",
        (
            _p(
                rf"{_L}(?:guaranteed|(?<!rest\s)assured|sure[\s-]*shot|pakka|pakki|pakke|पक्का|पक्की|पक्के)"
                rf"(?:\s+(?:or|and|/)\s+(?:guaranteed|assured|sure[\s-]*shot|"
                rf"fixed))*\s+{_gap(1)}{_GUARANTEE_TARGET}{_R}"
            ),
            _p(
                rf"{_L}fixed(?:\s+(?:or|and|/)\s+(?:guaranteed|assured))*\s+(?:returns?|profits?|gains?|"
                rf"return|रिटर्न){_R}"
            ),
            _p(
                rf"{_L}(?:guarantee|guarantees|guaranteeing|assurance|गारंटी|गारंटीड|"
                rf"गारंटेड)\s+{_gap(3)}{_GUARANTEE_TARGET}{_R}"
            ),
            _p(
                rf"{_L}{_GUARANTEE_TARGET}\s+{_gap(5)}(?:{_GUARANTEE_WORD}|pakka|pakki|पक्का|पक्की|पक्के|"
                rf"वादा){_R}"
            ),
            _p(
                rf"{_L}(?:100\s*(?:%|percent|per\s?cent)|hundred\s+percent)\s+(?:guaranteed|assured|pakka|"
                rf"sure|गारंटी|पक्का){_R}"
            ),
            _p(
                rf"{_L}promis(?:e|es|ed|ing)\s+{_gap(3)}(?:returns?|profits?|gains?|income|munafa|रिटर्न|"
                rf"मुनाफा){_R}"
            ),
        ),
    ),
    (
        "return_projection",
        (
            _p(rf"{_PCT}\+?\s+{_gap(3)}{_L}(?:{_RET_STRONG}|{_RET_SOFT}){_R}", negatable=False),
            _p(rf"{_L}{_RET_STRONG}{_R}\s+{_gap(4)}(?:{_PCT}|{_TIMES})", negatable=False),
            _p(rf"{_L}{_RET_SOFT}\s+(?:of|at|by|rate\s+of)\s+{_gap(2)}(?:{_PCT}|{_TIMES})", negatable=False),
            _p(rf"{_L}{_GROW_VERB}{_R}\s+{_gap(3)}(?:{_PCT}|{_TIMES})", negatable=False),
            _p(rf"(?:{_PCT}|{_TIMES})\s+{_gap(2)}{_HI_GROW_VERB}{_R}", negatable=False),
            _p(
                rf"{_L}(?:double|doubles|doubled|doubling|triple|triples|tripled|treble|2x|"
                rf"3x)\s+{_gap(2)}{_MONEY}{_R}",
                negatable=False,
            ),
            _p(
                rf"{_L}{_MONEY}\s+{_gap(3)}(?:double|doubles|doubled|triple|triples|tripled|dugna|doguna|"
                rf"duguna|डबल|दोगुना|दुगना|दुगुना|तिगुना){_R}",
                negatable=False,
            ),
            # Qualitative forward-looking promises ("will give you good returns"). Negatable so
            # "I can't say whether it will give good returns" passes.
            _p(
                rf"{_L}(?:will|would|going\s+to|can|could|should|may)\s+{_gap(2)}"
                rf"(?:give|deliver|generate|earn|make|fetch|provide|offer|"
                rf"get)\s+{_gap(2)}{_GOOD}\s+(?:returns?|gains|profits){_R}"
            ),
            _p(
                rf"{_L}(?:accha|achha|acha|achchha|badhiya|bahut|zabardast|tagda|high|bumper|"
                rf"shandar)\s+{_gap(1)}"
                rf"(?:return|returns|munafa|fayda)\s+{_gap(1)}(?:milega|milenge|milegi|dega|denge|degi|"
                rf"hoga){_R}"
            ),
            _p(
                rf"(?:अच्छा|अच्छे|बढ़िया|बेहतरीन|ज़बरदस्त|ज़्यादा|शानदार|बहुत)\s+{_gap(1)}"
                rf"(?:रिटर्न|मुनाफा|लाभ|फ़ायदा)\s+{_gap(1)}(?:मिलेगा|मिलेंगे|मिलेगी|देगा|देंगे|देगी|होगा){_R}"
            ),
        ),
    ),
    (
        "past_performance",
        (
            _p(
                rf"{_L}(?:has|have|had)\s+{_gap(2)}(?:delivered|given|generated|returned|earned|produced|"
                rf"clocked|"
                rf"posted|achieved|made|grown|outperformed|beaten|beat)\s+{_gap(3)}(?:{_PCT}|{_TIMES})"
            ),
            _p(
                rf"{_L}(?:has|have|had)\s+{_gap(2)}(?:delivered|given|generated|produced|posted|clocked|"
                rf"shown|achieved|returned)\s+{_gap(1)}(?:{_GOOD}|consistent|outstanding|robust|"
                rf"market[\s-]beating)\s+{_gap(1)}(?:returns?|performance|gains|results){_R}"
            ),
            _p(
                rf"{_L}(?:has|have|had)\s+{_gap(2)}(?:outperformed|beaten|beat)\s+{_gap(2)}"
                rf"(?:benchmark|benchmarks|market|markets|index|nifty|sensex|peers|category){_R}"
            ),
            _p(
                rf"{_L}track\s+record\s+of\s+{_gap(3)}(?:{_PCT}|strong|excellent|consistent|good|great|"
                rf"superb|stellar|outperform\w*|beating|delivering|generating)"
            ),
            _p(
                rf"{_L}(?:strong|excellent|proven|stellar|great|good|consistent|impressive|outstanding|"
                rf"superb|solid|enviable)\s+(?:performance\s+)?track\s+record"
            ),
            _p(
                rf"{_L}past\s+{_gap(1)}(?:returns?|performance)\s+{_gap(2)}(?:was|were|has\s+been|"
                rf"have\s+been|showed|shows|show|stood){_R}"
            ),
            _p(
                rf"{_L}(?:last|past|previous)\s+(?:{_NUM}\s+)?(?:years?|yrs?|months?|"
                rf"decade)\s+{_gap(4)}{_PCT}"
            ),
            _p(
                rf"{_L}(?:historically|since\s+inception)\s+{_gap(4)}(?:delivered|given|generated|returned|"
                rf"outperformed|beaten|beat|grown|{_PCT})"
            ),
            _p(
                rf"{_L}(?:pichh?le|pichh?la|pichhli)\s+{_gap(3)}(?:saal|saalon|sal|varsh|year|years|"
                rf"mahine)\s+{_gap(4)}(?:{_PCT}|returns?|munafa)"
            ),
            _p(rf"पिछले\s+{_gap(3)}(?:साल|सालों|वर्ष|वर्षों|महीने)\s+{_gap(4)}(?:{_PCT}|रिटर्न|मुनाफा)"),
            _p(rf"(?:{_L}ab\s+tak|अब\s+तक)\s+{_gap(3)}{_PCT}"),
        ),
    ),
    (
        "risk_free",
        (
            _p(rf"{_L}(?:risk[\s-]*free|risk[\s-]*less|riskless|zero[\s-]+risk|nil\s+risk){_R}"),
            _p(
                rf"{_L}100\s*(?:%|percent|per\s?cent)\s+(?:safe|secure|protected|risk[\s-]*free|surakshit|"
                rf"सुरक्षित|सेफ){_R}"
            ),
            _p(
                rf"{_L}(?:completely|totally|absolutely|fully|entirely|perfectly|hundred\s+percent)\s+"
                rf"(?:safe|secure|protected|risk[\s-]*free){_R}"
            ),
            _p(rf"{_L}capital\s+(?:protection|protected|safety|guarantee){_R}"),
            _p(
                rf"{_L}(?:protects?|safeguards?)\s+(?:your\s+|the\s+)?(?:capital|principal|money|"
                rf"investment){_R}"
            ),
            _p(
                rf"{_L}(?:money|capital|investment|principal|paisa|paise)\s+{_gap(2)}(?:safe|secure|"
                rf"protected|surakshit){_R}"
            ),
            _p(rf"{_L}(?:safe|secure|safest)\s+(?:investment|bet|option|scheme|fund){_R}"),
            _p(
                rf"{_L}(?:bilkul|poori\s+tarah|puri\s+tarah|ekdum|pura|poora)\s+(?:se\s+)?(?:safe|surakshit|"
                rf"secure){_R}"
            ),
            _p(rf"(?:पैसा|पैसे|पूंजी|निवेश|रकम)\s+{_gap(2)}(?:सुरक्षित|सेफ){_R}"),
            _p(rf"(?:पूरी\s+तरह|बिल्कुल|100%)\s+(?:से\s+)?(?:सुरक्षित|सेफ){_R}"),
            _p(r"(?:जोखिम[\s-]*मुक्त|(?:रिस्क|risk)[\s-]*फ्री)"),
            # Claims phrased with an inherent negator: never treated as negated.
            _p(rf"{_L}no\s+risks?{_R}(?![\s-]*(?:free|less))", negatable=False),
            _p(rf"{_L}without\s+(?:any\s+)?risks?{_R}", negatable=False),
            _p(
                rf"{_L}(?:koi|bilkul|zara\s+bhi)\s+(?:bhi\s+)?(?:risk|jokhim|khatra)\s+(?:nahi|nahin|"
                rf"nhi){_R}",
                negatable=False,
            ),
            _p(rf"{_L}(?:risk|jokhim)\s+{_gap(1)}(?:nahi|nahin|nhi)\s+(?:hai|hoga){_R}", negatable=False),
            _p(rf"{_L}bina\s+(?:kisi\s+)?(?:risk|jokhim){_R}", negatable=False),
            _p(rf"{_L}(?:paisa|paise)\s+{_gap(1)}(?:doob|dub)\w*\s+(?:nahi|nahin|nhi){_R}", negatable=False),
            _p(rf"(?:कोई|बिल्कुल|ज़रा\s+भी)\s+(?:भी\s+)?(?:जोखिम|रिस्क|खतरा)\s+(?:नहीं|नही){_R}", negatable=False),
            _p(rf"(?:जोखिम|रिस्क)\s+{_gap(1)}(?:नहीं|नही)\s+(?:है|होगा){_R}", negatable=False),
            _p(rf"बिना\s+(?:किसी\s+)?(?:जोखिम|रिस्क){_R}", negatable=False),
            _p(
                rf"(?:नुकसान|घाटा|{_L}loss|{_L}nuksan|{_L}nuksaan|{_L}ghata)\s+{_gap(1)}(?:नहीं|नही|nahi|"
                rf"nahin|nhi)\s+(?:होगा|hoga){_R}",
                negatable=False,
            ),
        ),
    ),
    (
        "advice",
        (
            _p(
                rf"{_L}(?:you|your\s+clients|clients|investors|customers)\s+{_gap(1)}(?:should|must|"
                rf"need\s+to|have\s+to|"
                rf"ought\s+to|had\s+better)\s+{_gap(2)}(?:invest|buy|subscribe|switch|redeem|allocate|grab|"
                rf"go\s+for|put\s+(?:your|their|some|money)|consider\s+investing){_R}"
            ),
            _p(
                rf"{_L}(?:i|we|i'd|we'd|i\s+would|we\s+would)\s+{_gap(1)}(?:recommend|suggest|advise|"
                rf"advice)\s+{_gap(2)}"
                rf"(?:invest\w*|buy\w*|subscrib\w*|(?:this|the|our)\s+(?:fund|scheme|nfo|product)|"
                rf"you\s+(?:invest|buy|go\s+for|put|allocate|switch|subscribe)){_R}"
            ),
            _p(
                rf"{_L}(?:best|top|number\s+one|no\.?\s*1|#1|top[\s-]+performing|safest|greatest|"
                rf"finest)\s+{_np_gap(2)}"
                rf"(?:fund|funds|scheme|schemes|nfo|nfos|investment|investments|option|choice|product|"
                rf"bet){_R}"
            ),
            _p(
                rf"{_L}(?:will|would|is\s+going\s+to|going\s+to|set\s+to|bound\s+to|sure\s+to|"
                rf"certain\s+to)\s+{_gap(1)}(?:outperform|beat){_R}"
            ),
            _p(rf"{_L}beat(?:s|ing)?\s+the\s+(?:market|markets|index|benchmark|nifty|sensex){_R}"),
            _p(rf"{_L}(?:best|right|perfect|ideal|great)\s+time\s+to\s+(?:invest|buy|enter|get\s+in){_R}"),
            _p(
                rf"{_L}(?:aapko|aap|aapke\s+clients\s+ko|clients\s+ko)\s+{_gap(2)}(?:invest|"
                rf"nivesh)\s+(?:zaroor\s+|jarur\s+)?(?:karna|kar)\s+(?:chahiye|lena\s+chahiye|lijiye|lo){_R}"
            ),
            _p(
                rf"{_L}(?:zaroor|zarur|jarur|jaroor|definitely)\s+{_gap(1)}(?:invest|nivesh)\s+"
                rf"(?:kar|kijiye|karein|karen|karo|kariye|kare){_R}"
            ),
            _p(
                rf"{_L}sabse\s+(?:accha|achha|acha|achchha|badhiya|best|behtareen|behtar)\s+{_gap(1)}"
                rf"(?:fund|scheme|nfo|nivesh|investment){_R}"
            ),
            _p(
                rf"(?:आपको|आप|आपके\s+क्लाइंट्स\s+को)\s+{_gap(2)}निवेश\s+(?:ज़रूर\s+)?(?:करना|कर)\s+"
                rf"(?:चाहिए|लेना\s+चाहिए|लीजिए|लें){_R}"
            ),
            _p(rf"(?:ज़रूर|अवश्य)\s+{_gap(1)}निवेश\s+(?:करें|कीजिए|करिए|करो|करना){_R}"),
            _p(rf"सबसे\s+(?:अच्छा|अच्छी|बढ़िया|बेहतरीन|बेहतर|बेस्ट)\s+{_gap(1)}(?:फंड|स्कीम|योजना|निवेश){_R}"),
            _p(rf"(?:मैं|हम)\s+{_gap(2)}(?:सलाह|सुझाव)\s+{_gap(2)}निवेश"),
            _p(rf"{_L}(?:can'?t|cannot|can\s+not)\s+(?:ever\s+)?lose{_R}", negatable=False),
            _p(
                rf"{_L}(?:won'?t|will\s+not|will\s+never|never)\s+(?:ever\s+)?lose\s+{_gap(1)}"
                rf"(?:money|capital|principal|investment|a\s+(?:single\s+)?(?:rupee|paisa)){_R}",
                negatable=False,
            ),
        ),
    ),
    (
        "commission_figure",
        (
            _p(rf"{_AMOUNT}\s+{_gap(2)}{_L}{_COMM}{_R}", negatable=False),
            _p(rf"{_L}{_COMM}{_R}\s+{_gap(4)}{_AMOUNT}", negatable=False),
        ),
    ),
    (
        "inducement",
        (
            _p(
                rf"{_L}(?:gifts?|gift\s+vouchers?|vouchers?|cash\s*backs?|cash[\s-]+back|bonus(?:es)?|"
                rf"incentives?|"
                rf"(?:foreign|international|overseas)\s+(?:trips?|tours?)|trips?|tour\s+packages?|vacations?|"
                rf"freebies?|goodies|gold\s+coins?|lucky\s+draws?|contests?|prizes?|rewards?|perks?|"
                rf"(?:extra|additional|special|higher)\s+(?:payouts?|commissions?|brokerage)|"
                rf"tohfa|tohfe|inaam|inam|upahar|तोहफा|तोहफे|उपहार|गिफ्ट|इनाम|कैशबैक|बोनस|इंसेंटिव|ट्रिप){_R}"
            ),
        ),
    ),
)

RULE_IDS: tuple[str, ...] = tuple(rule_id for rule_id, _ in _RULES)


@dataclass(frozen=True)
class ScreenResult:
    ok: bool
    violations: list[str]  # rule ids, e.g. "guaranteed_returns", "return_projection", "risk_free", "advice"


def screen_bot_utterance(text: str, approved: Iterable[str] = ()) -> ScreenResult:
    """Check a bot utterance against the SEBI/AMFI content rules before it is spoken.

    Rule ids: ``guaranteed_returns``, ``return_projection``, ``past_performance``, ``risk_free``,
    ``advice``, ``commission_figure``, ``inducement``. Claims that are explicitly negated
    ("returns are not guaranteed", "रिटर्न की गारंटी नहीं") pass; numeric return/commission
    figures never do. Violations are returned in rule order without duplicates.

    ``approved`` is Compliance-approved wording (the AMC's call script, FAQ answers, ...). A sentence
    that reproduces an approved sentence - same figures, near-identical words - is not flagged, so the
    bot can deliver e.g. approved index statistics; any paraphrase that changes a figure still is.
    """
    norm = _normalize(text)
    if not norm.strip():
        return ScreenResult(ok=True, violations=[])
    violations = _violations(norm)
    if violations and approved:
        approved_sentences = _approved_sentences(tuple(approved))
        remaining = [s for s in _split_sentences(norm) if not _is_approved(s, approved_sentences)]
        violations = _violations(" ".join(remaining)) if remaining else []
    return ScreenResult(ok=not violations, violations=violations)


def _violations(norm: str) -> list[str]:
    masked = _PSEUDO_NEGATION.sub(lambda m: " " * len(m.group()), norm)
    return [rule_id for rule_id, patterns in _RULES if _matches_rule(patterns, norm, masked)]


# Sentence boundary: ., ! or ? (or the Devanagari danda) followed by whitespace - so "14.97%" stays whole.
_SENTENCE_END = re.compile(r"(?<=[.!?\u0964])\s+")
_TOKEN = re.compile(r"\d+(?:[.,]\d+)*%?|[^\W\d_]+")
# Share of an utterance sentence's words that must appear in one approved sentence.
_APPROVED_OVERLAP = 0.85
_MIN_APPROVED_TOKENS = 4


def _split_sentences(norm: str) -> list[str]:
    return [s for s in _SENTENCE_END.split(norm) if s.strip()]


def _tokens(sentence: str) -> list[str]:
    return [t.replace(",", "") for t in _TOKEN.findall(sentence.lower())]


@lru_cache(maxsize=32)
def _approved_sentences(approved: tuple[str, ...]) -> tuple[frozenset[str], ...]:
    out = []
    for text in approved:
        for sentence in _split_sentences(_normalize(text)):
            out.append(frozenset(_tokens(sentence)))
    return tuple(out)


def _is_approved(sentence: str, approved: tuple[frozenset[str], ...]) -> bool:
    tokens = _tokens(sentence)
    if len(tokens) < _MIN_APPROVED_TOKENS:
        return False
    numbers = {t for t in tokens if t[0].isdigit()}
    for candidate in approved:
        if not numbers <= candidate:
            continue  # every figure must be exactly as approved
        overlap = sum(1 for t in tokens if t in candidate) / len(tokens)
        if overlap >= _APPROVED_OVERLAP:
            return True
    return False


def _matches_rule(patterns: tuple[_Pattern, ...], norm: str, masked: str) -> bool:
    for pattern in patterns:
        for m in pattern.regex.finditer(norm):
            if not pattern.negatable or not _is_negated(masked, m.start(), m.end()):
                return True
    return False


_SAFE_REPLIES = {
    "en": (
        "I'm sorry, I can only share information from the official scheme documents. "
        "Our relationship manager can help you with that. Shall I arrange a call back?"
    ),
    "hi": (
        "माफ़ कीजिए, मैं केवल आधिकारिक योजना दस्तावेज़ों में दी गई जानकारी ही साझा कर सकती हूँ। "
        "हमारे रिलेशनशिप मैनेजर इसमें आपकी मदद कर सकते हैं। क्या मैं आपके लिए कॉल बैक की व्यवस्था करूँ?"
    ),
}


def safe_reply(language: str) -> str:
    """Neutral replacement spoken when a reply is blocked. Unknown languages get English."""
    base = (language or "").strip().lower().replace("_", "-").split("-")[0]
    return _SAFE_REPLIES.get(base, _SAFE_REPLIES["en"])


# ---------------------------------------------------------------------------------------------
# Opt-out detection
# ---------------------------------------------------------------------------------------------

_CALL_EN = r"(?:call|calls|phone|ring|contact|disturb|bother|message)"
_CALL_HI = r"(?:call|calls|phone|fone|kol)"
_CALL_DEV = r"(?:कॉल|काल|कोल|फ़ोन|संपर्क)"
_NEG_HI = r"(?:mat|mt|na|nahi|nahin|nhi)"
_NEG_DEV = r"(?:मत|ना|न|नहीं|नही)"
_DO_HI = r"(?:karo|karna|kijiye|kijiyega|kariye|kariyega|karein|karen|kare|kro|krna)"
_DO_DEV = r"(?:करो|करना|कीजिए|कीजिये|करिए|करिये|करें|करे)"
_REMOVE_HI = r"(?:hata|hatao|hataiye|hatayein|hataye|hatado|delete|remove|nikal|nikaal|nikalo|nikaliye)"
_REMOVE_DEV = r"(?:हटा|हटाओ|हटाइए|हटाइये|हटाएं|हटाएँ|डिलीट|निकाल|निकालो|निकालिए)"

# Unambiguous requests to stop calling: always an opt-out, whatever else is said.
_OPT_OUT_STRONG = tuple(
    _rx(src)
    for src in (
        rf"{_L}(?:unsubscribe|opt[\s-]?out|opting\s+out){_R}",
        rf"{_L}(?:dnd|ncpr|do[\s-]+not[\s-]+disturb|do[\s-]+not[\s-]+call\s+list|डीएनडी){_R}",
        rf"{_L}no\s+more\s+(?:calls|calling|phone\s+calls){_R}",
        rf"{_L}never\s+(?:ever\s+)?(?:call|phone|ring|contact)\s+(?:me|us|this|again){_R}",
        rf"{_L}stop\s+(?:(?:these|the|your|such|all)\s+)?(?:calling|phoning|ringing|contacting|bothering|"
        rf"disturbing|harassing|spamming|calls){_R}",
        rf"{_L}(?:don'?t|do\s+not|never)\s+{_gap(2)}{_CALL_EN}\s+{_gap(2)}(?:again|anymore|any\s+more|ever|"
        rf"from\s+now\s+on|after\s+this|after\s+today|hereafter|in\s+(?:the\s+)?future){_R}",
        rf"{_L}(?:don'?t|do\s+not)\s+want\s+{_gap(3)}(?:calls?|to\s+be\s+(?:called|contacted))\s+{_gap(2)}"
        rf"(?:again|anymore|any\s+more|ever){_R}",
        rf"{_L}(?:remove|delete|erase|take|strike|drop|block)\s+(?:me|my\s+(?:phone\s+|mobile\s+|contact\s+)?"
        rf"(?:number|name|details|data|information|info|contact)|this\s+(?:phone\s+|"
        rf"mobile\s+)?number)\s+(?:from|off){_R}",
        rf"{_L}(?:remove|delete|erase|block)\s+(?:my|this)\s+(?:phone\s+|mobile\s+|contact\s+)?"
        rf"(?:number|details|data|information|info|contact){_R}",
        rf"{_L}take\s+(?:me|my\s+number|this\s+number)\s+off{_R}",
        # Hinglish / Hindi: "again/ever/from today ... don't call".
        rf"{_L}(?:dobara|dubara|dobaara|doobara|fir\s+se|phir\s+se|firse|phirse|kabhi|aage\s+se|"
        rf"aaj\s+ke\s+baad|ab\s+se)\s+{_gap(2)}{_CALL_HI}\s+{_gap(1)}{_NEG_HI}{_R}",
        rf"(?:दोबारा|दुबारा|फिर\s+से|कभी|आगे\s+से|आज\s+के\s+बाद|"
        rf"अब\s+से)\s+{_gap(2)}{_CALL_DEV}\s+{_gap(1)}{_NEG_DEV}{_R}",
        rf"{_L}(?:mera|mere|mujhe|hamara|humara|is)\s+(?:mobile\s+|phone\s+)?(?:number|naam|details|"
        rf"data)\s+{_gap(2)}{_REMOVE_HI}{_R}",
        rf"{_L}(?:list|database|record)\s+se\s+{_gap(2)}{_REMOVE_HI}{_R}",
        rf"(?:मेरा|मेरे|मुझे|हमारा|इस)\s+(?:मोबाइल\s+|फ़ोन\s+)?(?:नंबर|नम्बर|नाम)\s+{_gap(2)}{_REMOVE_DEV}{_R}",
        rf"(?:लिस्ट|सूची)\s+से\s+{_gap(2)}{_REMOVE_DEV}{_R}",
        rf"{_L}(?:calls|call\s+karna|phone\s+karna|calling)\s+band\s+(?:karo|kar\s+do|kardo|kijiye|karein|"
        rf"kariye){_R}",
        rf"(?:कॉल\s+करना|फ़ोन\s+करना|कॉल्स)\s+बंद\s+(?:करो|कर\s+दो|कीजिए|करें|करिए){_R}",
    )
)

# "Don't call me" style requests that are opt-outs *unless* the caller is only deferring
# ("don't call me now, call me tomorrow" is a callback request, not an opt-out).
_OPT_OUT_WEAK = tuple(
    _rx(src)
    for src in (
        rf"{_L}(?:don'?t|do\s+not)\s+(?:you\s+)?{_CALL_EN}\s+(?:me|us|this\s+number|here){_R}",
        rf"{_L}(?:don'?t|do\s+not)\s+want\s+{_gap(2)}(?:calls?|phone\s+calls|to\s+be\s+(?:called|"
        rf"contacted)){_R}",
        rf"{_L}{_CALL_HI}\s+{_gap(1)}{_NEG_HI}\s+{_DO_HI}{_R}",
        rf"{_L}(?:mat|mt)\s+{_CALL_HI}\s+{_DO_HI}{_R}",
        rf"{_L}(?:pareshan|disturb|tang)\s+(?:mat|na)\s+{_DO_HI}{_R}",
        rf"{_L}(?:call|calls|phone)\s+{_gap(1)}(?:nahi|nahin|nhi)\s+chahiye{_R}",
        rf"{_CALL_DEV}\s+{_gap(1)}{_NEG_DEV}\s+{_DO_DEV}{_R}",
        rf"(?:परेशान|तंग)\s+(?:मत|ना|न)\s+{_DO_DEV}{_R}",
        rf"(?:कॉल|फ़ोन)\s+{_gap(1)}(?:नहीं|नही)\s+चाहिए{_R}",
    )
)

# Time / availability cues that turn a weak "don't call" into "not now".
_DEFERRAL = _rx(
    rf"{_L}(?:now|today|tonight|tomorrow|later|afterwards|after(?!\s+(?:this|today|that))|before|morning|"
    rf"evening|"
    rf"afternoon|night|next\s+(?:week|month|time\s+slot)|monday|tuesday|wednesday|thursday|friday|saturday|"
    rf"sunday|"
    rf"weekend|weekdays?|moment|currently|busy|meeting|driving|travell?ing|office\s+hours|working\s+hours|"
    rf"hours?|hrs?|minutes?|mins?|abhi|abi|baad|baadme|kal|parso|parson|shaam|sham|subah|subha|dopahar|raat|"
    rf"agle|hafte|vyast|ghante|ghanta|thodi\s+der){_R}"
    rf"|(?:अभी|बाद|कल|परसों|शाम|सुबह|दोपहर|रात|अगले|हफ्ते|व्यस्त|मीटिंग|घंटे|घंटा|थोड़ी\s+देर){_R}|\d"
)


def detect_opt_out(text: str) -> bool:
    """Deterministic detector for "don't call me again" in English, Hinglish and Hindi.

    Errs on the side of honouring the request (TRAI/DPDP): explicit requests ("remove my number",
    "never call me", "put me on DND") always count. A bare "don't call me" counts unless the
    same utterance carries a time/availability cue, in which case it is a callback request.
    """
    norm = _normalize(text)
    if not norm.strip():
        return False
    if any(p.search(norm) for p in _OPT_OUT_STRONG):
        return True
    if any(p.search(norm) for p in _OPT_OUT_WEAK):
        return _DEFERRAL.search(norm) is None
    return False
