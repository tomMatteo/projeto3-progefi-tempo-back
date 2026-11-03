import os

# Must be set before servidor is imported: skips the database check and the scheduler
os.environ.setdefault("DISABLE_STARTUP_TASKS", "1")
os.environ.setdefault("DISABLE_RATE_LIMIT", "1")   # the tests make many POSTs from the same IP