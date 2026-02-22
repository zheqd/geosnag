.PHONY: check lint test

check: lint test

lint:
	ruff check geosnag/ tests/
	ruff format --check geosnag/ tests/

test:
	python -m pytest tests/test_index.py tests/test_special_paths.py tests/test_writer.py tests/test_e2e.py -v
	python tests/test_integration.py -v
