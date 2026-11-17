PY=python3
run:
	PYTHONPATH=src $(PY) -m textreclaim.cli
test:
	PYTHONPATH=src $(PY) -m pytest -q
clean:
	rm -rf .pytest_cache **/__pycache__ *.db
