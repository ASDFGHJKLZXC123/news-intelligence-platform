"""PostGIS smoke test: enable the extension, insert points, and query spatially."""

from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from db.base import engine

pytestmark = pytest.mark.integration


def test_postgis_point_bbox_and_distance_query(require_postgres: None) -> None:
    command.upgrade(Config("alembic.ini"), "head")

    with engine.begin() as conn:
        assert (
            conn.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'postgis'")).fetchone()
            is not None
        )
        conn.execute(text("DROP TABLE IF EXISTS _postgis_smoke"))
        conn.execute(
            text(
                """
                CREATE TABLE _postgis_smoke (
                    id int PRIMARY KEY,
                    name text NOT NULL,
                    geom geometry(Point, 4326) NOT NULL
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO _postgis_smoke (id, name, geom)
                VALUES
                  (1, 'Washington DC', ST_SetSRID(ST_MakePoint(-77.0369, 38.9072), 4326)),
                  (2, 'New York', ST_SetSRID(ST_MakePoint(-74.0060, 40.7128), 4326))
                """
            )
        )

        bbox_rows = conn.execute(
            text(
                """
                SELECT name
                FROM _postgis_smoke
                WHERE geom && ST_MakeEnvelope(-78, 38, -76, 40, 4326)
                """
            )
        ).fetchall()
        assert {row[0] for row in bbox_rows} == {"Washington DC"}

        nearby_rows = conn.execute(
            text(
                """
                SELECT name
                FROM _postgis_smoke
                WHERE ST_DWithin(
                    geom::geography,
                    ST_SetSRID(ST_MakePoint(-77.0369, 38.9072), 4326)::geography,
                    50000
                )
                """
            )
        ).fetchall()
        assert {row[0] for row in nearby_rows} == {"Washington DC"}

        conn.execute(text("DROP TABLE _postgis_smoke"))
