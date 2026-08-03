"""Single source of truth for the installed version.

Committed value is a dev placeholder. CI overwrites this file at build time
with the real version (e.g. ``0.1.42``) before building the wheel; the
overwrite is never committed back. pyproject reads it via
``tool.setuptools.dynamic`` so the wheel version and the app's ``--version``
both come from here.
"""
__version__ = "0.1.0.dev0"