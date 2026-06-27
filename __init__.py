"""Top-level package marker for the nautobot-upgrades Git jobs repository.

Nautobot (>= 2.0) requires a top-level ``__init__.py`` in a Git repository that
provides ``jobs`` content so the repository is importable as a Python package and
intra-repository (relative) imports work. The actual Jobs live in the ``jobs``
sub-package; see ``jobs/__init__.py`` for registration.
"""
