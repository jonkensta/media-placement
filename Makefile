PYTHON ?= python3
VERSION ?= $(shell git rev-parse --short=12 HEAD)

.PHONY: help test check image
help:
	@echo 'make check  - run isolated tests and validate the example configuration/Compose'
	@echo 'make image  - build local/media-placement:<commit> (no deployment)'

test:
	PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src $(PYTHON) -m unittest discover -s tests -v

check: test
	PYTHONPATH=src $(PYTHON) -m media_placement --config config.example.toml check
	docker compose config --quiet

image:
	docker build -t local/media-placement:$(VERSION) .
