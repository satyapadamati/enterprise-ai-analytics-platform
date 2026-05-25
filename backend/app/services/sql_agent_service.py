"""
SQL AI Orchestration Service for the Enterprise AI Analytics Platform.

Pipeline
--------
1. Fetch column-level schema metadata from Snowflake.
2. Format schema into a compact LLM prompt block.
3. Generate a SQL query via the Groq LLM service.
4. Validate the generated SQL for dangerous operations (deny-list).
5. Execute the validated SQL against Snowflake.
6. Generate a plain-English business explanation of the results.
7. Return a structured ``SQLAgentResponse`` dataclass.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from app.services.llm_service import LLMService
from app.services.snowflake_service import SnowflakeService

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SQL safety: operations that must never be executed on user-facing data
# ---------------------------------------------------------------------------

_BLOCKED_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bDROP\b", re.IGNORECASE),
    re.compile(r"\bDELETE\b", re.IGNORECASE),
    re.compile(r"\bTRUNCATE\b", re.IGNORECASE),
    re.compile(r"\bINSERT\b", re.IGNORECASE),
    re.compile(r"\bUPDATE\b", re.IGNORECASE),
    re.compile(r"\bALTER\b", re.IGNORECASE),
    re.compile(r"\bCREATE\b", re.IGNORECASE),
    re.compile(r"\bREPLACE\b", re.IGNORECASE),
    re.compile(r"\bMERGE\b", re.IGNORECASE),
    re.compile(r"\bGRANT\b", re.IGNORECASE),
    re.compile(r"\bREVOKE\b", re.IGNORECASE),
    re.compile(r"\bEXECUTE\b", re.IGNORECASE),
    re.compile(r"\bCALL\b", re.IGNORECASE),
    re.compile(r"--", re.IGNORECASE),          # inline comment injection
    re.compile(r"/\*.*?\*/", re.DOTALL),       # block comment injection
    re.compile(r";\s*\w", re.IGNORECASE),       # stacked statements
]

_MAX_RESULT_ROWS_FOR_EXPLANATION = 50  # keep the explanation prompt manageable


# ---------------------------------------------------------------------------
# Response dataclass
# ---------------------------------------------------------------------------

@dataclass
class SQLAgentResponse:
    question: str
    sql: str
    rows: list[dict[str, Any]]
    row_count: int
    explanation: str
    dialect: str = "Snowflake SQL"
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    error: Optional[str] = None
    success: bool = True


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class SQLAgentService:
    """
    Orchestrates the full natural-language → SQL → results → explanation
    pipeline using :class:`SnowflakeService` and :class:`LLMService`.
    """

    def __init__(self, sf: SnowflakeService, llm: LLMService) -> None:
        self._sf = sf
        self._llm = llm

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def answer(
        self,
        question: str,
        table_pattern: str = "%",
        database: Optional[str] = None,
        schema: Optional[str] = None,
    ) -> SQLAgentResponse:
        """
        End-to-end pipeline: natural language question → ``SQLAgentResponse``.

        Parameters
        ----------
        question:
            Plain-English analytics question.
        table_pattern:
            SQL ``LIKE`` filter applied when fetching schema metadata.
        database / schema:
            Override the connected Snowflake database / schema.
        """
        logger.info("SQLAgentService.answer | question=%.120s", question)

        # 1. Fetch schema metadata
        try:
            metadata = self._sf.get_schema_metadata(
                database=database,
                schema=schema,
                table_pattern=table_pattern,
            )
        except Exception as exc:
            logger.error("Schema metadata fetch failed: %s", exc)
            return self._error_response(question, f"Schema fetch error: {exc}")

        if not metadata:
            return self._error_response(question, "No schema metadata found for the given filter.")

        # 2. Format schema for the prompt
        schema_context = self._format_schema(metadata)
        logger.debug("Schema context built | chars=%d", len(schema_context))

        # 3. Generate SQL
        try:
            sql_result = self._llm.generate_sql_query(
                natural_language_question=question,
                schema_context=schema_context,
                database=database,
            )
            sql = sql_result["sql"]
        except Exception as exc:
            logger.error("SQL generation failed: %s", exc)
            return self._error_response(question, f"SQL generation error: {exc}")

        logger.info("Generated SQL | sql=%.200s", sql)

        # 4. Validate SQL safety
        violation = self._check_sql_safety(sql)
        if violation:
            logger.warning("Blocked unsafe SQL | violation=%s | sql=%.200s", violation, sql)
            return self._error_response(
                question,
                f"Generated SQL was blocked for safety: contains '{violation}'.",
                sql=sql,
            )

        # 5. Execute SQL
        try:
            rows = self._sf.execute_query(sql)
        except Exception as exc:
            logger.error("SQL execution failed: %s | sql=%.200s", exc, sql)
            return self._error_response(question, f"SQL execution error: {exc}", sql=sql)

        logger.info("SQL executed | rows_returned=%d", len(rows))

        # 6. Generate business explanation
        try:
            explanation = self._generate_explanation(question, sql, rows)
        except Exception as exc:
            logger.warning("Explanation generation failed (non-fatal): %s", exc)
            explanation = f"Query returned {len(rows)} row(s)."

        return SQLAgentResponse(
            question=question,
            sql=sql,
            rows=rows,
            row_count=len(rows),
            explanation=explanation,
            dialect=sql_result.get("dialect", "Snowflake SQL"),
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _format_schema(metadata: list[dict[str, Any]]) -> str:
        """
        Convert INFORMATION_SCHEMA column rows into a compact DDL-style block.

        Example output::

            TABLE: ORDERS
              order_id        NUMBER          NOT NULL
              customer_id     NUMBER          NOT NULL
              order_date      TIMESTAMP_NTZ
              total_amount    FLOAT
        """
        tables: dict[str, list[str]] = {}
        for col in metadata:
            table = col.get("TABLE_NAME", "UNKNOWN")
            nullable = "" if col.get("IS_NULLABLE", "YES") == "YES" else " NOT NULL"
            comment = f"  -- {col['COMMENT']}" if col.get("COMMENT") else ""
            tables.setdefault(table, []).append(
                f"  {col.get('COLUMN_NAME', ''):<30}{col.get('DATA_TYPE', ''):<20}{nullable}{comment}"
            )

        lines: list[str] = []
        for table, cols in tables.items():
            lines.append(f"TABLE: {table}")
            lines.extend(cols)
            lines.append("")
        return "\n".join(lines).strip()

    @staticmethod
    def _check_sql_safety(sql: str) -> Optional[str]:
        """
        Return the matched violation string if *sql* contains a blocked pattern,
        ``None`` if the query is safe to execute.
        """
        for pattern in _BLOCKED_PATTERNS:
            match = pattern.search(sql)
            if match:
                return match.group(0).strip()
        return None

    def _generate_explanation(
        self,
        question: str,
        sql: str,
        rows: list[dict[str, Any]],
    ) -> str:
        """Ask the LLM to interpret the query results in plain business language."""
        sample = rows[:_MAX_RESULT_ROWS_FOR_EXPLANATION]
        result_preview = "\n".join(str(r) for r in sample)
        truncation_note = (
            f"\n(Showing first {_MAX_RESULT_ROWS_FOR_EXPLANATION} of {len(rows)} rows.)"
            if len(rows) > _MAX_RESULT_ROWS_FOR_EXPLANATION
            else ""
        )

        prompt = (
            f"A user asked: \"{question}\"\n\n"
            f"The following SQL was executed:\n```sql\n{sql}\n```\n\n"
            f"Results ({len(rows)} row(s) total){truncation_note}:\n{result_preview}\n\n"
            "Write a concise, plain-English business summary of these results. "
            "Do not repeat the SQL. Focus on actionable insights."
        )
        return self._llm.generate_response(
            user_message=prompt,
            system_prompt=(
                "You are a senior data analyst summarising SQL query results "
                "for a non-technical business audience."
            ),
            temperature=0.3,
        )

    @staticmethod
    def _error_response(
        question: str,
        error_message: str,
        sql: str = "",
    ) -> SQLAgentResponse:
        return SQLAgentResponse(
            question=question,
            sql=sql,
            rows=[],
            row_count=0,
            explanation="",
            error=error_message,
            success=False,
        )


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------

def get_sql_agent_service(
    sf: SnowflakeService,
    llm: LLMService,
) -> SQLAgentService:
    """
    FastAPI dependency factory.  Inject ``SnowflakeService`` and ``LLMService``
    via ``Depends`` and pass them here.

    Usage
    -----
    .. code-block:: python

        from fastapi import APIRouter, Depends
        from app.services.sql_agent_service import SQLAgentService, get_sql_agent_service
        from app.services.snowflake_service import SnowflakeService, get_snowflake_service
        from app.services.llm_service import LLMService, get_llm_service

        router = APIRouter()

        @router.post("/nl-query")
        def nl_query(
            question: str,
            agent: SQLAgentService = Depends(
                lambda sf=Depends(get_snowflake_service),
                       llm=Depends(get_llm_service): get_sql_agent_service(sf, llm)
            ),
        ):
            return agent.answer(question)
    """
    return SQLAgentService(sf=sf, llm=llm)
