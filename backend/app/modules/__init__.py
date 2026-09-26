"""Domain modules — the seams of the modular monolith (CLAUDE.md §2).

Each subpackage owns its tables and exposes exactly one public interface:
`service.py`. Cross-module reads go through another module's service functions,
never its `models.py`, its `router.py`, or a raw SQL join across the boundary.
That discipline is what makes billing and notifications extractable later
without a rewrite.

Standard layout per module:
    __init__.py   models.py   schemas.py   service.py   router.py
    events.py (optional)      rbac.py (optional)
"""
