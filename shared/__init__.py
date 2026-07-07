"""
Argus — shared package.

Contains the SQLite persistence layer used by both the backend trading
engine and the NiceGUI frontend. The database file itself lives on a
shared Docker volume mounted at /app/shared inside both containers.
"""

from shared.database import Database, get_db  # noqa: F401
