.PHONY: check lint test vendor

check: lint test

lint:
	ruff check geosnag/ tests/
	ruff format --check geosnag/ tests/

test:
	python -m pytest tests/test_index.py tests/test_special_paths.py tests/test_writer.py tests/test_e2e.py -v
	python tests/test_integration.py -v

# Download and vendor ExifTool Perl files into geosnag/vendor/exiftool/.
# Run once before building the wheel or committing vendor files.
vendor:
	python scripts/download_exiftool.py
