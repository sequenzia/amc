"""Plist renderer for the ``amc`` CLI.

This module ports the template-render → lint → atomic-write pipeline that
was previously implemented in a now-removed bash installer. It is consumed
by ``amc install`` (spec §5.3) to render each service's plist template
into ``~/Library/LaunchAgents/<label>.plist``.

Pipeline (per service):
    1. :func:`render_plist` reads ``service.plist_template`` and performs
       **only** ``__INSTALL_DIR__`` and ``__HOME__`` substitution. Any
       other literal ``__X__`` token in the template is left untouched
       (spec §6.2 — substitution surface is intentionally minimal).
    2. :func:`lint_plist` writes the rendered text to a temp file and
       runs ``plutil -lint`` against it. On lint failure the temp file is
       **retained** on disk and :class:`PlistLintError` is raised with the
       temp-file path so the operator can inspect it (spec §5.3 Edge
       Cases).
    3. :func:`install_plist` chains the two: render → lint → atomic
       ``os.replace`` into ``dest``. The atomic-replace step writes the
       lint-validated bytes to a temp file in ``dest.parent`` first, so
       there is never a partial-write window visible at ``dest``.

Spec references: §5.3 Install, §6.2 Security, §7.1 Architecture Overview.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from amc.cli.services import Service

__all__ = [
    "PlistLintError",
    "render_plist",
    "lint_plist",
    "install_plist",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PlistLintError(RuntimeError):
    """Raised when ``plutil -lint`` rejects a rendered plist.

    The ``tmpfile`` path points at the on-disk temp file that contains the
    rejected content; the file is intentionally **not** cleaned up so the
    operator can run ``plutil -lint <tmpfile>`` themselves to see the full
    error. The CLI command body catches this and maps it to exit code 2
    with the canonical message ``Rendered plist failed plutil -lint:
    <tmpfile>`` (spec §5.3 Edge Cases).
    """

    def __init__(self, tmpfile: Path) -> None:
        super().__init__(f"Rendered plist failed plutil -lint: {tmpfile}")
        self.tmpfile = tmpfile


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def render_plist(service: Service, *, install_dir: Path, home: Path) -> str:
    """Render a service's plist template by substituting placeholders.

    Reads ``service.plist_template`` and replaces:

    * ``__INSTALL_DIR__`` → ``str(install_dir)``
    * ``__HOME__``         → ``str(home)``

    No other tokens are touched, even if they share the ``__X__`` shape
    (spec §6.2 — substitution surface is bounded to keep injection
    impossible). The substitution is order-independent and idempotent:
    re-rendering with identical inputs produces byte-identical output.

    Args:
        service: Service registry entry. ``service.plist_template`` is
            expected to be a repo-relative path; the caller is responsible
            for joining it against the repo root before passing — but for
            convenience this function accepts either form (any path that
            ``Path.read_text`` can open).
        install_dir: Absolute path to the AMC repo root. Substituted for
            ``__INSTALL_DIR__``.
        home: Absolute path to the operator's home directory. Substituted
            for ``__HOME__``.

    Returns:
        The rendered plist as a string.

    Raises:
        FileNotFoundError: If ``service.plist_template`` does not exist on
            disk. The CLI command body maps this to exit code 2 with the
            message ``Plist template not found: <path>``.
    """
    template_path = Path(service.plist_template)
    try:
        text = template_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        # Re-raise with the original path so callers can format a clean
        # operator-facing message without re-discovering it.
        raise FileNotFoundError(f"Plist template not found: {template_path}") from exc

    # Order does not matter — neither replacement string contains the
    # other's placeholder. Use ``str.replace`` (literal substitution) so
    # no regex metacharacters in install_dir / home are reinterpreted.
    text = text.replace("__INSTALL_DIR__", str(install_dir))
    text = text.replace("__HOME__", str(home))
    return text


# ---------------------------------------------------------------------------
# Lint
# ---------------------------------------------------------------------------


def lint_plist(text: str) -> Path:
    """Write ``text`` to a temp file and run ``plutil -lint`` against it.

    On success the temp file path is returned (the caller is responsible
    for unlinking or moving it). On failure the temp file is **retained**
    so the operator can re-run ``plutil -lint`` manually, and
    :class:`PlistLintError` is raised carrying the temp-file path.

    Args:
        text: Fully rendered plist content (XML).

    Returns:
        Path to the temp file containing ``text``. The caller owns the
        file from this point — it is the caller's job to either move it
        into its final location (see :func:`install_plist`) or delete it.

    Raises:
        PlistLintError: If ``plutil -lint`` exits non-zero. The temp file
            is left in place; ``.tmpfile`` on the exception points at it.
    """
    # ``delete=False`` because we want the file to outlive this function
    # in both the success case (caller will ``os.replace`` it onto dest)
    # and the failure case (the operator needs it for debugging).
    with tempfile.NamedTemporaryFile(  # noqa: SIM115 — handle escapes via .name
        mode="w",
        encoding="utf-8",
        suffix=".plist",
        delete=False,
    ) as tmp:
        tmp.write(text)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmpfile = Path(tmp.name)

    # ``shell=False`` (default for list args) — no injection surface.
    # ``check=False`` so we control the failure mode and can raise a
    # typed error rather than ``CalledProcessError``.
    result = subprocess.run(
        ["plutil", "-lint", str(tmpfile)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        # Leave the temp file in place for operator inspection.
        raise PlistLintError(tmpfile)

    return tmpfile


# ---------------------------------------------------------------------------
# Install (render-validated → atomic replace)
# ---------------------------------------------------------------------------


def install_plist(text: str, dest: Path) -> None:
    """Lint ``text`` and atomically install it at ``dest``.

    Pipeline:
        1. Ensure ``dest.parent`` (typically ``~/Library/LaunchAgents/``)
           exists; create it with mode 0o700 if missing.
        2. Lint ``text`` via :func:`lint_plist`. If the lint passes the
           temp file lives in the system temp dir.
        3. Re-create the validated bytes in a temp file **inside
           ``dest.parent``** (so the final ``os.replace`` is atomic — POSIX
           guarantees atomic rename only within the same filesystem) and
           ``os.replace`` it onto ``dest``.

    The ``lint_plist`` temp file is cleaned up after the atomic replace
    completes; on any failure during the second-stage write it is left in
    place along with whatever same-dir temp we may have created.

    Args:
        text: Fully rendered plist content.
        dest: Final on-disk location, e.g.
            ``~/Library/LaunchAgents/com.user.amc-adapter.plist``.

    Raises:
        PlistLintError: Propagated from :func:`lint_plist` if validation
            fails. The original temp file is retained.
        OSError: If ``dest.parent`` cannot be created or the replace
            fails.
    """
    # Ensure parent directory exists. Mode 0o700 matches the conservative
    # default for files in the user's ``Library/`` (no group/other
    # access). ``exist_ok=True`` keeps the call idempotent.
    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    # Lint first. If this raises, no write to dest.parent has happened.
    lint_tmp = lint_plist(text)

    # Write to a temp file in dest.parent so the final os.replace is
    # atomic (rename within the same filesystem). We don't simply move
    # ``lint_tmp`` because tempfile likes /var/folders which is on a
    # different volume than ~/Library on some Macs (notably APFS firmlink
    # boundaries). Re-writing the same bytes locally is cheap and
    # guarantees rename atomicity.
    with tempfile.NamedTemporaryFile(  # noqa: SIM115 — handle escapes via .name
        mode="w",
        encoding="utf-8",
        dir=str(dest.parent),
        prefix=f".{dest.name}.",
        suffix=".tmp",
        delete=False,
    ) as same_dir_tmp:
        same_dir_tmp.write(text)
        same_dir_tmp.flush()
        os.fsync(same_dir_tmp.fileno())
        same_dir_tmp_name = same_dir_tmp.name

    try:
        os.replace(same_dir_tmp_name, dest)
    except OSError:
        # Replace failed — clean up the same-dir temp so we don't leak
        # dotfiles into LaunchAgents/. Lint temp is left in place for
        # debugging.
        with contextlib.suppress(OSError):
            os.unlink(same_dir_tmp_name)
        raise

    # Success path — drop the lint-stage temp file; the atomic-replace
    # temp file is already gone (consumed by os.replace). Best-effort:
    # if the temp file is already gone or inaccessible we don't fail
    # the install.
    with contextlib.suppress(OSError):
        os.unlink(lint_tmp)
