"""Service layer — shared implementations for REST routes and Agent
tools so the two surfaces cannot drift. Phase 5 (``§9`` of the plan)
mandates that tools registered with the host Brain dispatch into the
*same* helpers that the FastAPI router uses; this package is where
those helpers live.

Only :mod:`query` is required for V1.0; additional services (create /
update paths) are free to land here later without touching the public
plugin surface.
"""

from __future__ import annotations

from finpulse_services import query, radar_library

__all__ = ["query", "radar_library"]
