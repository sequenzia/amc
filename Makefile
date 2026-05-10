.PHONY: docs-lint mcp-test mcp-lint mcp-import-audit

# Run the documentation linter (link + code-block validation).
# Pure stdlib Python; no extra deps. See scripts/docs_lint.py for details.
docs-lint:
	uv run python scripts/docs_lint.py

# MCP wrapper convenience targets (the wrapper lives at `mcp/`).
mcp-test:
	uv run --project mcp pytest

mcp-lint:
	uv run --project mcp ruff check .
	uv run --project mcp ruff format --check .

mcp-import-audit:
	uv run --project mcp python scripts/import_audit.py
