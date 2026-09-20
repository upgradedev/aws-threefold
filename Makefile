.PHONY: help test lint gate run docker-build docker-up clean

help:
	@echo "Threefold Developer & Operator Commands:"
	@echo "  make test         - Run full pytest pyramid"
	@echo "  make gate         - Run pre-commit security and architecture gate"
	@echo "  make lint         - Run syntax & hygiene checks"
	@echo "  make run          - Start local server on port 8001"
	@echo "  make docker-build - Build production container image"
	@echo "  make docker-up    - Run container via docker-compose"

test:
	python -m pytest tests -v

gate:
	python scripts/pre-commit-gate.py --scan-dir src/

lint:
	python -m py_compile src/threefold/**/*.py

run:
	python src/threefold/interfaces/server.py --port 8001

docker-build:
	docker build -t threefold:latest .

docker-up:
	docker compose up -d

clean:
	rm -rf __pycache__ .pytest_cache .coverage
