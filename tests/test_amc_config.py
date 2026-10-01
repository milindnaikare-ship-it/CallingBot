"""Validates the AMC's real content in config/ (the rest of the suite uses tests/fixtures/config).

These checks run on every change so a YAML edit by the business team cannot silently break calls:
the files load, every approved sentence survives the compliance screen, the prompt renders without
gaps, and the greeting keeps the virtual-assistant disclosure.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from conftest import ROOT

from callingbot.agent.prompts import build_system_prompt, render_greeting
from callingbot.compliance import screen_bot_utterance
from callingbot.knowledge import load_knowledge
from callingbot.models import Distributor

CONFIG = ROOT / "config"


@pytest.fixture(scope="module")
def real_kb():
    return load_knowledge(CONFIG)


def test_config_loads_with_script(real_kb):
    assert real_kb.script.steps, "config/script.yaml should define the approved call flow"
    assert {s.id for s in real_kb.script.standard_responses} >= {
        "rm_request",
        "commission",
        "marketing_collateral",
    }
    assert real_kb.amc.default_language in {lang.code for lang in real_kb.amc.languages}


def test_every_approved_text_passes_the_screen(real_kb):
    approved = real_kb.approved_texts()
    for text in [*approved, *real_kb.nfo.key_highlights, *real_kb.amc.distributor_value_props]:
        result = screen_bot_utterance(text, approved=approved)
        assert result.ok, f"{result.violations}: {text[:120]}"


def test_greeting_discloses_virtual_assistant(real_kb):
    distributor = Distributor(name="Test Partner", phone="+919800000000")
    morning = datetime(2026, 10, 13, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    for lang in real_kb.amc.languages:
        greeting = render_greeting(real_kb, lang.code, distributor, morning)
        assert "{" not in greeting and "Test Partner" in greeting
        assert "virtual assistant" in greeting.lower() or lang.code != "en-IN"
    assert render_greeting(real_kb, "en-IN", distributor, morning).startswith("Good morning")


def test_system_prompt_renders_without_gaps(real_kb):
    prompt = build_system_prompt(real_kb)
    assert "None" not in prompt.replace("(none)", "")
    assert real_kb.nfo.scheme_name in prompt and real_kb.amc.bot_name in prompt
    for step in real_kb.script.steps:
        assert f"[{step.id}]" in prompt
    if real_kb.nfo.pending_fields():
        assert "Not yet available" in prompt
