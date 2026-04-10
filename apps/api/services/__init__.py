"""Domain services layer.

Rules for code in this package:

1. Generic core + configurable overrides. Each service is a frozen
   ``@dataclass`` value object; construct a custom instance to override
   policy. The module-level singleton covers the common path.
2. No imports from ``routers`` or ``fastapi``. Services must work in
   background workers, cron jobs, and tests without an HTTP context.
   Raise domain exceptions; let callers translate to HTTPException.
3. Explicit inputs over implicit globals. DB sessions, settings, and
   clients are passed in — never fetched via ``get_db()`` /
   ``get_settings()`` inside a service.
"""

from services.pricing import TaskPricing, TaskPricingError, default_pricing

__all__ = [
    "TaskPricing",
    "TaskPricingError",
    "default_pricing",
]
