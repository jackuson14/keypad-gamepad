"""Single source of truth for the app version.

Kept deliberately import-free and trivial so the release CI can rewrite it from
the git tag (see .github/workflows/release.yml) and so importing it is cheap.
The string is bare semver (no leading "v"); git tags carry the "v".
"""

__version__ = "0.2.2"
