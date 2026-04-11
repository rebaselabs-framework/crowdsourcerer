"""In-process task handlers.

Each module here implements one AI task type without depending on any
external CrowdSorcerer / RebaseKit service. A few reach out to
Anthropic directly for LLM-backed tasks, but that's a vendor
dependency — not an internal cross-service call.

The ``workers.router.execute_task`` dispatcher wires task type
strings to the appropriate module here.
"""
