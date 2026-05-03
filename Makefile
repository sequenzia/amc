.PHONY: docs-lint

# Run the documentation linter (link + code-block validation).
# Pure stdlib Python; no extra deps. See scripts/docs_lint.py for details.
docs-lint:
	uv run python scripts/docs_lint.py
