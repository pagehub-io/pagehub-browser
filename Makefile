.PHONY: up down logs test test-browser lint install

install:
	pip install -e ".[dev]"
	playwright install --with-deps chromium

# GIT_COMMIT: the checkout's HEAD, which /health reports as commit.
up:
	GIT_COMMIT=$$(git rev-parse HEAD) docker-compose up -d --build

down:
	docker-compose down

logs:
	docker-compose logs -f pagehub-browser

test:
	pytest -m "not browser"

test-browser:
	pytest -m browser

lint:
	ruff check api tests
