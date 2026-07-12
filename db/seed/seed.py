"""Database seed entrypoint.

Stage 1 defines no business tables, so there is nothing to seed yet. This script is a
working placeholder that confirms a database connection and reports that no seed data
exists for the foundation stage. Later stages (sources, etc.) extend it.
"""

from __future__ import annotations

from sqlalchemy import text

from db.base import engine
from packages.config.logging import configure_logging, get_logger


def main() -> None:
    configure_logging()
    logger = get_logger("db.seed")
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    logger.info("seed complete: no Stage 1 tables to populate")


if __name__ == "__main__":
    main()
