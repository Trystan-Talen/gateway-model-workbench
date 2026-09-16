PYTHON ?= .venv/bin/python

.PHONY: install run test check e2e coverage benchmark
install:
	python3.11 -m venv .venv
	$(PYTHON) -m pip install -r requirements-dev.lock
	npm ci
run:
	$(PYTHON) route_test_ui.py
check:
	$(PYTHON) -m compileall -q -x '/(\.venv|node_modules|data)/' .
	npm run check
test: check
	$(PYTHON) -W error::ResourceWarning -m unittest discover -s tests
	npm test
e2e:
	PYTHON=$(PYTHON) npm run e2e
coverage:
	$(PYTHON) -m coverage run -m unittest discover -s tests
	$(PYTHON) -m coverage report
	$(PYTHON) tools/check_coverage.py
benchmark:
	$(PYTHON) tools/benchmark.py
