PYTHON ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)
PYTHONPATH := src
ARGS ?=

.PHONY: setup setup-speaker setup-speaker-pyannote dev gui serve doctor devices import finalize retranscribe refine merge test lint

setup:
	$(PYTHON) -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -e ".[dev]"

setup-speaker:
	$(PYTHON) -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -e ".[dev,speaker]"

setup-speaker-pyannote:
	$(PYTHON) -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -e ".[dev,speaker,speaker-pyannote]"

dev:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m live_note start $(ARGS)

gui:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m live_note gui $(ARGS)

serve:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m live_note serve $(ARGS)

doctor:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m live_note doctor

devices:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m live_note devices

import:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m live_note import $(ARGS)

finalize:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m live_note finalize $(ARGS)

retranscribe:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m live_note retranscribe $(ARGS)

refine:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m live_note refine $(ARGS)

merge:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m live_note merge $(ARGS)

test:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m unittest discover -s tests -p 'test_*.py' -v

lint:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m compileall src tests
	ruff check src tests
	ruff format --check src tests
