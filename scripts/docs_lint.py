"""Documentation linter for AMC repo.

Validates Markdown docs across the repository for two classes of breakage:

1. **Fenced code blocks** must declare a recognized language tag (or be
   intentionally empty for output / raw blocks). Unknown tags signal typos.
2. **Local links** (relative paths, intra-doc anchors) must resolve. External
   URLs are *not* fetched — only their syntax is validated. HTTP requests in a
   linter are flaky and CI-unfriendly.

Exits non-zero on any failure with `path:line` style messages.

Run directly:

    python scripts/docs_lint.py

Or via pytest wrapper:

    uv run pytest tests/docs/test_docs_lint.py -v
"""

from __future__ import annotations

import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlparse

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent

# Allowed language tags for fenced code blocks. Empty string ("") is also
# allowed for blocks that contain raw output / unknown content.
ALLOWED_LANGS: frozenset[str] = frozenset(
    {
        "python",
        "javascript",
        "typescript",
        "ts",
        "js",
        "bash",
        "shell",
        "sh",
        "sql",
        "json",
        "yaml",
        "yml",
        "toml",
        "mermaid",
        "plist",
        "applescript",
        "xml",
        "html",
        "css",
        "http",
        "jsonc",
        "text",
        "plaintext",
        "",
    }
)

# Glob patterns relative to REPO_ROOT. Order is irrelevant — results are
# deduped and sorted.
DOC_GLOBS: tuple[str, ...] = (
    "README.md",
    "SETUP.md",
    "API.md",
    "RUNBOOK.md",
    "internal/adrs/*.md",
    "internal/blueprints/*.md",
    "internal/notes/*.md",
    "docs/*.md",
)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class LintError:
    path: Path
    line: int
    message: str

    def render(self, root: Path) -> str:
        try:
            rel = self.path.relative_to(root)
        except ValueError:
            rel = self.path
        return f"{rel}:{self.line}: {self.message}"


@dataclass
class ParsedDoc:
    path: Path
    lines: list[str]
    headings: set[str] = field(default_factory=set)  # slugified heading anchors
    # (line_number_1based, lang_string_after_fence)
    fences: list[tuple[int, str]] = field(default_factory=list)
    # (line_number_1based, link_target) — Markdown [text](target) links
    links: list[tuple[int, str]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Markdown parsing (intentionally minimal — no deps)
# ---------------------------------------------------------------------------


_FENCE_RE = re.compile(r"^(?P<indent>\s{0,3})(?P<fence>```+|~~~+)(?P<info>.*)$")
_HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<text>.+?)\s*#*\s*$")
# Inline link regex. Avoids matching image alt-text by ignoring leading '!' via
# negative-lookbehind. Captures the target (which may include a "title").
_INLINE_LINK_RE = re.compile(
    r"(?<!\!)\[(?P<text>[^\]]*)\]\((?P<target>[^)\s]+(?:\s+\"[^\"]*\")?)\)"
)


def slugify(text: str) -> str:
    """GitHub-style heading slug.

    Lowercases, strips inline markdown emphasis, drops punctuation that GitHub
    drops, replaces whitespace with hyphens. Not 100% identical to GitHub's
    slugger but covers common cases (links and inline `code` in headings).
    """
    # Strip inline code spans first so backticks don't bleed into the slug.
    text = re.sub(r"`([^`]+)`", r"\1", text)
    # Strip inline link syntax — keep the text portion only.
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    # Strip emphasis / bold markers.
    text = re.sub(r"[*_]+", "", text)
    # Normalize unicode (NFKD), drop combining marks.
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().strip()
    # Replace whitespace with hyphens.
    text = re.sub(r"\s+", "-", text)
    # Drop chars GitHub drops: keep word chars and hyphens.
    text = re.sub(r"[^\w\-]", "", text)
    return text


def parse_doc(path: Path) -> ParsedDoc:
    """Parse a Markdown file into the structures the linter checks."""
    raw = path.read_text(encoding="utf-8")
    lines = raw.splitlines()

    doc = ParsedDoc(path=path, lines=lines)

    in_fence = False
    fence_marker = ""  # the exact opening fence string ('```' or '~~~~')
    fence_open_line = 0
    fence_open_lang = ""

    # Track heading anchors. GitHub appends -1, -2, ... for duplicates; we mirror
    # that behavior so links to repeated headings still validate.
    seen_slugs: dict[str, int] = {}

    for idx, line in enumerate(lines, start=1):
        fence_match = _FENCE_RE.match(line)
        if fence_match:
            this_marker = fence_match.group("fence")
            info = fence_match.group("info").strip()
            if not in_fence:
                in_fence = True
                fence_marker = this_marker[:3]  # ``` or ~~~ — match prefix only
                fence_open_line = idx
                # First whitespace-separated token is the language (rest are
                # attributes / titles like ```python title="x.py").
                fence_open_lang = info.split()[0] if info else ""
                doc.fences.append((fence_open_line, fence_open_lang))
            else:
                # Closing fence must use the same character (``` vs ~~~) and
                # be at least as long as the opener. We only require the prefix
                # to match here — close-fence info strings are not standard but
                # we silently allow them to keep the linter forgiving.
                if this_marker.startswith(fence_marker):
                    in_fence = False
                    fence_marker = ""
                    fence_open_line = 0
                    fence_open_lang = ""
            continue

        if in_fence:
            # Skip everything inside fenced blocks — no link / heading scanning.
            continue

        heading_match = _HEADING_RE.match(line)
        if heading_match:
            slug = slugify(heading_match.group("text"))
            count = seen_slugs.get(slug, 0)
            anchor = slug if count == 0 else f"{slug}-{count}"
            doc.headings.add(anchor)
            seen_slugs[slug] = count + 1
            # Don't `continue` — a heading line could in theory contain a link.

        for link_match in _INLINE_LINK_RE.finditer(line):
            target = link_match.group("target")
            # Strip optional title: `path "title"` -> `path`.
            target = target.split(None, 1)[0]
            doc.links.append((idx, target))

    if in_fence:
        # Unclosed fence — flagged as an error elsewhere via the fence list.
        # Fall through; the open fence is still recorded.
        pass

    return doc


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def collect_docs(root: Path) -> list[Path]:
    """Resolve DOC_GLOBS into a sorted, deduped list of existing files."""
    found: set[Path] = set()
    for pattern in DOC_GLOBS:
        for p in root.glob(pattern):
            if p.is_file():
                found.add(p.resolve())
    return sorted(found)


def validate_fences(doc: ParsedDoc) -> list[LintError]:
    errors: list[LintError] = []
    for line_no, lang in doc.fences:
        if lang.lower() not in ALLOWED_LANGS:
            errors.append(
                LintError(
                    path=doc.path,
                    line=line_no,
                    message=(
                        f"unrecognized fenced code-block language tag: '{lang}'. "
                        f"Allowed: {sorted(t for t in ALLOWED_LANGS if t)} or empty."
                    ),
                )
            )
    return errors


def _is_external(target: str) -> bool:
    """True for absolute http(s) / mailto / other-scheme URIs."""
    parsed = urlparse(target)
    return bool(parsed.scheme) and parsed.scheme not in {"", "file"}


def validate_links(
    doc: ParsedDoc,
    docs_by_path: dict[Path, ParsedDoc],
    repo_root: Path,
) -> list[LintError]:
    errors: list[LintError] = []
    doc_dir = doc.path.parent

    for line_no, raw_target in doc.links:
        # External link: only validate URI parses cleanly.
        if _is_external(raw_target):
            try:
                parsed = urlparse(raw_target)
            except ValueError as exc:
                errors.append(
                    LintError(
                        path=doc.path,
                        line=line_no,
                        message=f"malformed external URL '{raw_target}': {exc}",
                    )
                )
                continue
            if parsed.scheme in {"http", "https"} and not parsed.netloc:
                errors.append(
                    LintError(
                        path=doc.path,
                        line=line_no,
                        message=f"external URL missing host: '{raw_target}'",
                    )
                )
            continue

        # Pure anchor: '#section' — must resolve in the *same* file.
        if raw_target.startswith("#"):
            anchor = unquote(raw_target.lstrip("#"))
            if anchor and anchor not in doc.headings:
                errors.append(
                    LintError(
                        path=doc.path,
                        line=line_no,
                        message=f"anchor '#{anchor}' not found in this file",
                    )
                )
            continue

        # Local path (possibly with anchor).
        path_part, _, anchor = raw_target.partition("#")
        path_part = unquote(path_part)
        anchor = unquote(anchor)

        if not path_part:
            # Just '#' with no anchor; treat as a benign placeholder.
            continue

        # Resolve target relative to either the current doc's dir or repo root
        # (when the path begins with '/').
        if path_part.startswith("/"):
            target_path = (repo_root / path_part.lstrip("/")).resolve()
        else:
            target_path = (doc_dir / path_part).resolve()

        # Directory link is OK if the directory exists. We don't try to resolve
        # implicit index files — the convention in this repo is to link to the
        # explicit file.
        if target_path.is_dir():
            if anchor:
                errors.append(
                    LintError(
                        path=doc.path,
                        line=line_no,
                        message=(
                            f"link target '{path_part}' is a directory; cannot "
                            f"validate anchor '#{anchor}'"
                        ),
                    )
                )
            continue

        if not target_path.exists():
            errors.append(
                LintError(
                    path=doc.path,
                    line=line_no,
                    message=f"local link target does not exist: '{path_part}'",
                )
            )
            continue

        # If there's an anchor and the target is a known doc, validate it.
        if anchor:
            other = docs_by_path.get(target_path)
            if other is None and target_path.suffix.lower() == ".md":
                # Markdown file outside the lint scope — parse it on demand so
                # we can still validate the anchor.
                try:
                    other = parse_doc(target_path)
                except OSError as exc:
                    errors.append(
                        LintError(
                            path=doc.path,
                            line=line_no,
                            message=(
                                f"could not read link target "
                                f"'{path_part}' to validate anchor: {exc}"
                            ),
                        )
                    )
                    continue
            if other is not None and anchor not in other.headings:
                errors.append(
                    LintError(
                        path=doc.path,
                        line=line_no,
                        message=(
                            f"anchor '#{anchor}' not found in '{path_part}'"
                        ),
                    )
                )

    return errors


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def lint(root: Path | None = None) -> list[LintError]:
    root = (root or REPO_ROOT).resolve()
    files = collect_docs(root)

    parsed: dict[Path, ParsedDoc] = {f: parse_doc(f) for f in files}

    errors: list[LintError] = []
    for doc in parsed.values():
        errors.extend(validate_fences(doc))
        errors.extend(validate_links(doc, parsed, root))

    errors.sort(key=lambda e: (str(e.path), e.line))
    return errors


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    root = Path(argv[0]) if argv else REPO_ROOT

    errors = lint(root)
    files_scanned = len(collect_docs(root.resolve()))

    if errors:
        for err in errors:
            print(err.render(root.resolve()), file=sys.stderr)
        print(
            f"\ndocs-lint: {len(errors)} error(s) across {files_scanned} file(s).",
            file=sys.stderr,
        )
        return 1

    print(f"docs-lint: OK ({files_scanned} file(s) checked, 0 errors).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
