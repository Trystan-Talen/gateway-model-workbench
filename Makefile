PYTHON ?= .venv/bin/python
COVERAGE_DIR := artifacts/coverage

.PHONY: install run test check e2e coverage benchmark
install:
	python3.11 -m venv .venv
	$(PYTHON) -m pip install -r requirements-dev.lock
	npm ci
run:
	$(PYTHON) route_test_ui.py
check:
	$(PYTHON) -m compileall -q -x '/(\.venv|node_modules|data|artifacts|backups)/' .
	npm run check
test: check
	$(PYTHON) -W error::ResourceWarning tools/run_tests.py
	npm test
e2e:
	PYTHON=$(PYTHON) npm run e2e
coverage:
	mkdir -p $(COVERAGE_DIR)
	$(PYTHON) -m coverage run --rcfile=pyproject.toml --data-file=$(COVERAGE_DIR)/.coverage tools/run_tests.py
	$(PYTHON) -m coverage report --rcfile=pyproject.toml --data-file=$(COVERAGE_DIR)/.coverage
	$(PYTHON) tools/check_coverage.py
benchmark:
	$(PYTHON) tools/benchmark.py
