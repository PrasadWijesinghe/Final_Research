"""This expert's own database schema (tables get added here; migrations live in migrations/)."""

from sqlalchemy import MetaData

from .config import DB_SCHEMA

# Tables for this expert's schema attach to this metadata. None yet while the expert is a stub.
metadata = MetaData(schema=DB_SCHEMA)
