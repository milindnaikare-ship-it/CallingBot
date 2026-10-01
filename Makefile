# CallingBot shortcuts. Run "make help" for the list.
#
# Examples:
#   make install
#   make run                                  # admin dashboard on http://127.0.0.1:8000
#   make simulate ARN=ARN-999901 LANGUAGE_CODE=en-IN      # any code enabled in config/amc.yaml
#   make dialer CAMPAIGN="NFO Launch"

PYTHON ?= python3
HOST ?= 127.0.0.1
PORT ?= 8000
CAMPAIGN ?= NFO Launch
ARN ?=
LANGUAGE_CODE ?=

# The app reads .env by itself, but the Anthropic SDK reads ANTHROPIC_API_KEY from the process
# environment only. LOAD_ENV passes just that key through from .env (an already-exported key wins),
# using the app's own dotenv parser. Exporting the whole file instead would let shell-mangled
# values (e.g. a password containing "$") override the correct ones the app reads from .env.
DOTENV_KEY = $(PYTHON) -c 'from dotenv import dotenv_values; print(dotenv_values(".env").get("ANTHROPIC_API_KEY") or "")' 2>/dev/null
LOAD_ENV = KEY="$${ANTHROPIC_API_KEY:-$$($(DOTENV_KEY))}"; [ -n "$$KEY" ] && export ANTHROPIC_API_KEY="$$KEY";

.PHONY: help install test lint format run simulate dialer

help:
	@echo "make install    install the package with dev tools (editable)"
	@echo "make test       run the test suite"
	@echo "make lint       ruff lint + format check (what CI runs)"
	@echo "make format     auto-format and auto-fix lint issues"
	@echo "make run        start the web app with auto-reload (HOST=$(HOST) PORT=$(PORT))"
	@echo "make simulate   talk to the bot in the terminal (optional ARN=..., LANGUAGE_CODE=en-IN)"
	@echo "make dialer     run the dialer for CAMPAIGN=\"$(CAMPAIGN)\""

install:
	$(PYTHON) -m pip install -e ".[dev]"

test:
	$(PYTHON) -m pytest -q

lint:
	$(PYTHON) -m ruff check .
	$(PYTHON) -m ruff format --check .

format:
	$(PYTHON) -m ruff format .
	$(PYTHON) -m ruff check --fix .

run:
	@$(LOAD_ENV) callingbot serve --host $(HOST) --port $(PORT) --reload

simulate:
	@$(LOAD_ENV) callingbot simulate $(if $(ARN),--arn "$(ARN)") $(if $(LANGUAGE_CODE),--language "$(LANGUAGE_CODE)")

dialer:
	@$(LOAD_ENV) callingbot run-dialer --campaign "$(CAMPAIGN)"
