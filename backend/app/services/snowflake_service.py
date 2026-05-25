"""
Snowflake service for the Enterprise AI Analytics Platform.

Responsibilities
----------------
- Manage a thread-safe Snowflake connection lifecycle.
- Execute parameterised queries and return JSON-serialisable results.
- Expose schema / column metadata for downstream AI context building.
- Surface as a FastAPI dependency via ``get_snowflake_service``.
"""

from __future__ import annotations

import decimal
import logging
from contextlib import contextmanager
from datetime import date, datetime, time
from typing import Any, Generator, Optional

import snowflake.connector
from snowflake.connector import DictCursor, SnowflakeConnection
from snowflake.connector.errors import (
    DatabaseError,
    InterfaceError,
    ProgrammingError,
)

from app.core.config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_serialisable(value: Any) -> Any:
    """Recursively convert a Snowflake result value to a JSON-safe type."""
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    return value


def _row_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    return {k: _make_serialisable(v) for k, v in row.items()}


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class SnowflakeService:
    """
    Encapsulates Snowflake connectivity and query execution.

    Intended to be used as a FastAPI dependency (singleton per request or
    application lifetime — see ``get_snowflake_service`` below).
    """

    def __init__(self) -> None:
        self._conn: Optional[SnowflakeConnection] = None

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Open a new connection using credentials from ``settings``."""
        if self._conn and not self._conn.is_closed():
            logger.debug("Snowflake connection already open — reusing.")
            return

        connect_kwargs: dict[str, Any] = {
            "account": settings.SNOWFLAKE_ACCOUNT,
            "user": settings.SNOWFLAKE_USER,
            "password": settings.SNOWFLAKE_PASSWORD,
            "database": settings.SNOWFLAKE_DATABASE,
            "schema": settings.SNOWFLAKE_SCHEMA,
            "warehouse": settings.SNOWFLAKE_WAREHOUSE,
            "login_timeout": settings.SNOWFLAKE_LOGIN_TIMEOUT,
            "network_timeout": settings.SNOWFLAKE_NETWORK_TIMEOUT,
            "client_session_keep_alive": True,
        }
        if settings.SNOWFLAKE_ROLE:
            connect_kwargs["role"] = settings.SNOWFLAKE_ROLE

        logger.info(
            "Connecting to Snowflake | account=%s database=%s schema=%s warehouse=%s",
            settings.SNOWFLAKE_ACCOUNT,
            settings.SNOWFLAKE_DATABASE,
            settings.SNOWFLAKE_SCHEMA,
            settings.SNOWFLAKE_WAREHOUSE,
        )
        try:
            self._conn = snowflake.connector.connect(**connect_kwargs)
            logger.info("Snowflake connection established.")
        except InterfaceError as exc:
            logger.error("Snowflake interface error during connect: %s", exc)
            raise
        except DatabaseError as exc:
            logger.error("Snowflake database error during connect: %s", exc)
            raise

    def disconnect(self) -> None:
        """Gracefully close the connection if open."""
        if self._conn and not self._conn.is_closed():
            self._conn.close()
            logger.info("Snowflake connection closed.")
        self._conn = None

    @contextmanager
    def _cursor(self) -> Generator[DictCursor, None, None]:
        """Yield a ``DictCursor``; auto-connect if needed."""
        self.connect()
        cursor: DictCursor = self._conn.cursor(DictCursor)  # type: ignore[union-attr]
        try:
            yield cursor
        finally:
            cursor.close()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def execute_query(
        self,
        sql: str,
        params: Optional[tuple[Any, ...] | list[Any]] = None,
    ) -> list[dict[str, Any]]:
        """
        Execute *sql* (optionally with *params*) and return all rows as a
        list of JSON-serialisable dicts.

        Parameters
        ----------
        sql:
            Parameterised SQL string. Use ``%s`` placeholders for values.
        params:
            Positional bind parameters corresponding to ``%s`` placeholders.

        Returns
        -------
        list[dict[str, Any]]
            Each element maps column name → serialisable Python value.

        Raises
        ------
        ProgrammingError
            When the SQL statement itself is invalid.
        DatabaseError
            For all other Snowflake-level errors.
        """
        logger.debug("Executing query | sql=%.200s params=%s", sql, params)
        try:
            with self._cursor() as cur:
                cur.execute(sql, params or ())
                rows = cur.fetchall()
                result = [_row_to_dict(row) for row in rows]
                logger.info(
                    "Query complete | rows_returned=%d sql_preview=%.120s",
                    len(result),
                    sql.strip().replace("\n", " "),
                )
                return result
        except ProgrammingError as exc:
            logger.error("SQL programming error | sql=%.200s error=%s", sql, exc)
            raise
        except DatabaseError as exc:
            logger.error("Snowflake database error | sql=%.200s error=%s", sql, exc)
            raise

    def get_schema_metadata(
        self,
        database: Optional[str] = None,
        schema: Optional[str] = None,
        table_pattern: str = "%",
    ) -> list[dict[str, Any]]:
        """
        Return column-level metadata for all tables matching *table_pattern*
        in the given *database*/*schema* (defaults to the connected ones).

        The result is a flat list of rows from ``INFORMATION_SCHEMA.COLUMNS``,
        JSON-serialisable and ready to be used as AI context.

        Parameters
        ----------
        database:
            Override the connected database.
        schema:
            Override the connected schema.
        table_pattern:
            SQL ``LIKE`` pattern for ``TABLE_NAME`` (default ``%`` = all).
        """
        target_db = database or settings.SNOWFLAKE_DATABASE
        target_schema = schema or settings.SNOWFLAKE_SCHEMA

        sql = """
            SELECT
                TABLE_CATALOG,
                TABLE_SCHEMA,
                TABLE_NAME,
                COLUMN_NAME,
                ORDINAL_POSITION,
                DATA_TYPE,
                IS_NULLABLE,
                CHARACTER_MAXIMUM_LENGTH,
                NUMERIC_PRECISION,
                NUMERIC_SCALE,
                COLUMN_DEFAULT,
                COMMENT
            FROM {db}.INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = %s
              AND TABLE_NAME   LIKE %s
            ORDER BY TABLE_NAME, ORDINAL_POSITION
        """.format(db=target_db)  # noqa: S608 — db name is from trusted config

        logger.info(
            "Fetching schema metadata | database=%s schema=%s table_pattern=%s",
            target_db,
            target_schema,
            table_pattern,
        )
        return self.execute_query(sql, (target_schema.upper(), table_pattern.upper()))

    def ping(self) -> bool:
        """Return ``True`` if the connection is alive, ``False`` otherwise."""
        try:
            result = self.execute_query("SELECT 1 AS alive")
            return bool(result)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Snowflake ping failed: %s", exc)
            return False


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------

def get_snowflake_service() -> Generator[SnowflakeService, None, None]:
    """
    FastAPI dependency that yields a connected ``SnowflakeService`` and
    guarantees the connection is released after the request completes.

    Usage
    -----
    .. code-block:: python

        from fastapi import APIRouter, Depends
        from app.services.snowflake_service import SnowflakeService, get_snowflake_service

        router = APIRouter()

        @router.get("/tables")
        def list_tables(sf: SnowflakeService = Depends(get_snowflake_service)):
            return sf.get_schema_metadata()
    """
    service = SnowflakeService()
    try:
        service.connect()
        yield service
    finally:
        service.disconnect()
