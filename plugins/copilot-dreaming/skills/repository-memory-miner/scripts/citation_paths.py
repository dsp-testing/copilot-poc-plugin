"""Shared citation-path heuristics for bucket-pool.py and validate-output.py.

Both scripts must agree on what a citation's *file path* is — one to bucket memories by
their source path, the other to check those paths exist on disk. Keeping the rule here
means a change (e.g. recognizing a new kind of file) can never desync the two.
"""

import re

# A path token that carries a file extension, e.g. `src/main.py`, `.github/workflows/ci.yml`.
_HAS_EXT = re.compile(r"[\w.\-/]+\.[\w]+")
# A root-level dotfile basename with no extension, e.g. `.gitignore`, `.eslintrc`, `.env`.
_DOTFILE = re.compile(r"\.[\w-]+")

# Common files that carry no extension and no `/`, so the extension rule above misses them —
# recognized by exact basename instead of being dumped in with non-file citations.
EXTLESS_FILENAMES = {
    "Makefile",
    "Dockerfile",
    "Jenkinsfile",
    "Rakefile",
    "Gemfile",
    "Procfile",
    "Brewfile",
    "Vagrantfile",
    "CODEOWNERS",
    "LICENSE",
    "NOTICE",
    "VERSION",
}


def is_file_path(path):
    """True if `path` (already stripped of any :line / :line-range suffix) looks like a real
    file reference: it has a file extension, or its basename is a known extensionless file
    (Makefile, CODEOWNERS, …) or a dotfile (.gitignore, .eslintrc, …). Non-file citations such
    as `owner/repo` or `repos/<owner>/<repo>/pulls/123` contain `/` but return False."""
    if _HAS_EXT.fullmatch(path):
        return True
    base = path.rsplit("/", 1)[-1]
    return base in EXTLESS_FILENAMES or bool(_DOTFILE.fullmatch(base))


# A parenthetical note that annotates a path, e.g. `(lint/test jobs)`. A note may itself contain
# commas/semicolons, so notes are removed BEFORE the citation is split into separate paths.
_PAREN_NOTE = re.compile(r"\([^)]*\)")
# A single citation may pack several paths, comma- or semicolon-separated.
_SEPARATORS = re.compile(r"[,;]")
# A trailing :line or :line-range suffix on a path token, e.g. the `:21-48` in `file.py:21-48`.
_LINE_SUFFIX = re.compile(r":\d+(?:-\d+)?$")
# A real path token is only word chars, dot, slash, hyphen — this keeps prose, URLs, and refs
# like `owner/repo#123` out of the path set. It is NOT redundant with is_file_path: the
# extensionless/dotfile branch of is_file_path inspects only the basename, leaving the rest of
# the token (e.g. a stray `#` or `:`) unconstrained, so the guard still matters.
_SAFE_PATH = re.compile(r"^[\w./-]+$")
# A bare version-like token such as `0.15.16` satisfies is_file_path's extension rule but is not
# a file; drop pure digit/dot tokens so an un-parenthesized version in a citation is not treated
# as a path (and so disk-checked or bucketed).
_VERSION_LIKE = re.compile(r"^[\d.]+$")
# A path-prefixed release tag such as `ts/v0.1.81`, `go/v1.0.0`, or a SemVer prerelease/build tag
# `v1.0.0-rc.1` also passes is_file_path (a trailing numeric segment like `.81`/`.0`/`.1` reads as an
# extension) but is not a file; reject a token whose final segment is such a tag. The optional
# prerelease/build suffix must itself end in `.<digits>`, so a real version-named file with an alpha
# extension (`docs/v1.2.3.md`, `data/v1.2.3-final.md`) and a numeric-extension man page (`man/git.1`)
# are kept; requiring >= 2 numeric groups up front leaves a Go major-version dir (`v2`) untouched.
_RELEASE_TAG = re.compile(r"^v?\d+(?:\.\d+)+(?:[-+][0-9A-Za-z.-]*\.\d+)?$")


def citation_file_paths(cit):
    """All file-path tokens in one citation string, in order, de-duplicated.

    A single citation may pack several comma/semicolon-separated paths and carry parenthetical
    notes or trailing prose — the style this repo's own memories use, e.g.
    ``.github/workflows/ci.yml (jobs), requirements-dev.txt (versions)`` or
    ``tests/conftest.py:21-48, pyproject.toml:25-27``. Parenthetical notes are removed first
    (they may contain commas), the remainder is split on ``,``/``;``, and each piece is reduced to
    its leading whitespace-free token with any :line / :line-range suffix stripped. A token is
    kept only when it is made of path characters (word, dot, slash, hyphen), is not a bare version
    number or a path-prefixed release tag (e.g. ``ts/v0.1.81``), and is_file_path accepts it. De-duplication is within this one citation. Non-file
    citations (``owner/repo``, ``repos/<owner>/<repo>/pulls/123``, ``owner/repo#123``, URLs, plain
    prose) yield no paths. Absolute or ``..`` paths are returned as-is for the caller to vet."""
    if not isinstance(cit, str):
        return []
    paths = []
    for piece in _SEPARATORS.split(_PAREN_NOTE.sub("", cit)):
        piece = piece.strip()
        if not piece:
            continue
        token = _LINE_SUFFIX.sub("", piece.split()[0])
        if (
            _SAFE_PATH.match(token)
            and not _VERSION_LIKE.match(token)
            and not _RELEASE_TAG.match(token.rsplit("/", 1)[-1])
            and is_file_path(token)
            and token not in paths
        ):
            paths.append(token)
    return paths
