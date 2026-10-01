"""Shared pytest fixtures: in-memory database, knowledge base, settings, factories."""

from __future__ import annotations

import itertools
from datetime import datetime
from pathlib import Path

import pytest

from callingbot import db
from callingbot.knowledge import load_knowledge
from callingbot.models import Distributor, EmpanelmentStatus
from callingbot.settings import Settings

ROOT = Path(__file__).resolve().parent.parent

# 2026-10-13 is a Tuesday. 05:30 UTC == 11:00 IST, inside the default 10:00-19:00 window.
IN_WINDOW_UTC = datetime(2026, 10, 13, 5, 30)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        database_url="sqlite://",
        config_dir=ROOT / "config",
        llm_provider="fake",
        telephony_provider="simulator",
        public_base_url="https://bot.example.test",
        secret_key="test-secret",
        admin_username="admin",
        admin_password="test-password",
        twilio_account_sid="AC00000000000000000000000000000000",
        twilio_auth_token="test-auth-token",
        twilio_from_number="+911400000000",
    )


@pytest.fixture
def kb():
    return load_knowledge(ROOT / "config")


@pytest.fixture
def session():
    db.configure_engine("sqlite://")
    db.init_db()
    s = db.new_session()
    try:
        yield s
    finally:
        s.close()
        db.Base.metadata.drop_all(db.get_engine())


_seq = itertools.count(1)


@pytest.fixture
def make_distributor(session):
    """Factory: ``make_distributor(name="Ravi", phone="+919812345678", **overrides)``."""

    def _make(**overrides) -> Distributor:
        n = next(_seq)
        data = {
            "arn": f"ARN-{100000 + n}",
            "name": f"Test Distributor {n}",
            "firm_name": f"Test Wealth {n}",
            "phone": f"+9198{n:08d}",
            "email": f"dist{n}@example.com",
            "city": "Pune",
            "state": "Maharashtra",
            "status": EmpanelmentStatus.NEW,
        }
        data.update(overrides)
        d = Distributor(**data)
        session.add(d)
        session.flush()
        return d

    return _make
