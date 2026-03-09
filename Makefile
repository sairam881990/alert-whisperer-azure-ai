.PHONY: install dev run test lint type-check clean

# Install production dependencies
install:
	pip install -e .

# Install with dev dependencies
dev:
	pip install -e ".[dev]"

# Run the Streamlit application
run:
	streamlit run app.py --server.port 8501

# Run tests
test:
	pytest tests/ -v --tb=short

# Run tests with coverage
test-cov:
	pytest tests/ -v --cov=src --cov-report=html --cov-report=term

# Lint code
lint:
	ruff check src/ ui/ tests/ app.py
	ruff format --check src/ ui/ tests/ app.py

# Auto-fix lint issues
lint-fix:
	ruff check --fix src/ ui/ tests/ app.py
	ruff format src/ ui/ tests/ app.py

# Type checking
type-check:
	mypy src/

# Clean build artifacts
clean:
	rm -rf __pycache__ .pytest_cache .mypy_cache htmlcov .coverage
	rm -rf src/__pycache__ ui/__pycache__ tests/__pycache__
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true

# Index knowledge base (requires MCP servers running)
index-kb:
	python -c "from src.rag.vector_store import VectorStore; print('Vector store initialized')"

# Full CI pipeline
ci: lint type-check test
