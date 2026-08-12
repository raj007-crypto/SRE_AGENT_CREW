"""Root conftest.

Ensures the project root is on sys.path so tests can import the
top-level `data.*`, `agents.*`, `incident_schema`, `graph` and `main`
modules regardless of how pytest is invoked.
"""
