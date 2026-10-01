.PHONY: test init-db bootstrap api collector gateway mock ensure-venv

VENV_PYTHON := .venv/bin/python

ensure-venv:
	@if [ ! -x "$(VENV_PYTHON)" ]; then \
		python3 -m venv .venv && \
		.venv/bin/pip install -r requirements.txt; \
	fi

test:
	python3 -m unittest discover -s tests -v

init-db:
	python3 host/db.py

bootstrap:
	./tools/bootstrap.sh

api:
	python3 host/api.py

collector:
	python3 host/collector.py

gateway:
	python3 host/gateway.py

mock: ensure-venv
	$(VENV_PYTHON) tools/mock_lab.py
