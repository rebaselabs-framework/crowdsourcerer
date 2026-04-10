"""Domain services layer.

Services sit between the routers (HTTP) and the models (persistence). They
own business rules, pricing, validation, and orchestration — anything that
is pure-ish domain logic and shouldn't be duplicated across endpoints.

Design principles
-----------------

1. **Generic core + configurable overrides.** Each service exposes a
   frozen, pure-Python value object (``@dataclass(frozen=True, slots=True)``)
   so callers can construct a custom instance with different pricing,
   quotas, or policy knobs. The module-level ``DEFAULT`` singleton covers
   the common path.

2. **No imports from ``routers`` or ``fastapi``.** Services must work in
   background workers, cron jobs, CLI scripts, and tests without an HTTP
   request context. Raise domain exceptions (plain ``ValueError``,
   ``LookupError``) and let the caller translate to ``HTTPException``.

3. **Explicit inputs over implicit globals.** Database sessions, settings,
   and clients are passed in; services never call ``get_db()`` or
   ``get_settings()`` themselves. That keeps them drop-in replaceable in
   tests.

Current services
----------------

- :mod:`services.pricing` — task credit calculation for both AI and
  human tasks. Previously ``_calc_credits`` / ``_compute_task_cost``
  copy-pasted inside ``routers/tasks.py``.
"""

from services.pricing import TaskPricing, TaskPricingError, default_pricing

__all__ = [
    "TaskPricing",
    "TaskPricingError",
    "default_pricing",
]
