"""
Groq LLM service for the Enterprise AI Analytics Platform.

Responsibilities
----------------
- Call the Groq Chat Completions API via ``requests`` (sync, no extra SDK required).
- ``generate_response``  — general-purpose conversational turns.
- ``generate_sql_query`` — structured text-to-SQL with schema context injection.
- Retry on transient HTTP errors (429 / 5xx) with exponential back-off.
- Surface as a FastAPI dependency via ``get_llm_service``.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Generator, Optional

import requests
from requests import HTTPError, RequestException, Timeout

from app.core.config import settings

logger = logging.getLogger(__name__)

_GROQ_CHAT_PATH = "/chat/completions"

# HTTP status codes that are worth retrying
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _call_api(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout: int,
    max_retries: int,
) -> dict[str, Any]:
    """
    POST *payload* to *url* with retry / back-off on transient failures.

    Raises
    ------
    HTTPError
        On non-retryable HTTP errors or when all retries are exhausted.
    Timeout
        When the server does not respond within *timeout* seconds.
    RequestException
        For all other network-level errors.
    """
    last_exc: Exception | None = None

    for attempt in range(1, max_retries + 1):
        try:
            logger.debug(
                "Groq API request | attempt=%d/%d url=%s model=%s",
                attempt,
                max_retries,
                url,
                payload.get("model"),
            )
            response = requests.post(
                url,
                json=payload,
                headers=headers,
                timeout=timeout,
            )

            if response.status_code in _RETRYABLE_STATUS_CODES:
                wait = 2 ** (attempt - 1)  # 1 s, 2 s, 4 s, …
                logger.warning(
                    "Groq API transient error | status=%d attempt=%d/%d retrying_in=%ds",
                    response.status_code,
                    attempt,
                    max_retries,
                    wait,
                )
                last_exc = HTTPError(
                    f"HTTP {response.status_code}", response=response
                )
                if attempt < max_retries:
                    time.sleep(wait)
                continue

            response.raise_for_status()
            return response.json()

        except Timeout as exc:
            logger.error("Groq API request timed out | attempt=%d/%d", attempt, max_retries)
            last_exc = exc
            if attempt < max_retries:
                time.sleep(2 ** (attempt - 1))

        except RequestException as exc:
            logger.error("Groq API network error | attempt=%d/%d error=%s", attempt, max_retries, exc)
            last_exc = exc
            break  # Non-transient network errors — don't retry

    raise last_exc  # type: ignore[misc]


def _extract_content(response_json: dict[str, Any]) -> str:
    """Pull the assistant message text out of a Groq chat completion response."""
    try:
        return response_json["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        logger.error("Unexpected Groq response shape: %s", response_json)
        raise ValueError(f"Cannot parse Groq response: {exc}") from exc


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class LLMService:
    """
    Thin, requests-based client for the Groq Chat Completions API.

    All public methods return plain Python values (str / dict) so they are
    trivially JSON-serialisable by FastAPI.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        timeout: Optional[int] = None,
        max_retries: Optional[int] = None,
    ) -> None:
        self._api_key = api_key or settings.GROQ_API_KEY
        self._base_url = settings.GROQ_API_BASE_URL.rstrip("/")
        self._model = model or settings.GROQ_DEFAULT_MODEL
        self._temperature = temperature if temperature is not None else settings.GROQ_DEFAULT_TEMPERATURE
        self._timeout = timeout or settings.GROQ_REQUEST_TIMEOUT
        self._max_retries = max_retries or settings.GROQ_MAX_RETRIES

        if not self._api_key:
            raise ValueError("GROQ_API_KEY is not configured.")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate_response(
        self,
        user_message: str,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        """
        Send a single-turn chat message to Groq and return the assistant reply.

        Parameters
        ----------
        user_message:
            The human turn content.
        system_prompt:
            Optional system instruction prepended to the conversation.
        model:
            Override the default model for this call.
        temperature:
            Sampling temperature (0 = deterministic).
        max_tokens:
            Cap on generated tokens (defaults to ``settings.MAX_TOKENS``).

        Returns
        -------
        str
            Raw assistant reply text.
        """
        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_message})

        payload: dict[str, Any] = {
            "model": model or self._model,
            "messages": messages,
            "temperature": temperature if temperature is not None else self._temperature,
            "max_tokens": max_tokens or settings.MAX_TOKENS,
        }

        logger.info(
            "generate_response | model=%s temperature=%s message_length=%d",
            payload["model"],
            payload["temperature"],
            len(user_message),
        )

        response_json = _call_api(
            url=f"{self._base_url}{_GROQ_CHAT_PATH}",
            payload=payload,
            headers=_build_headers(self._api_key),
            timeout=self._timeout,
            max_retries=self._max_retries,
        )
        reply = _extract_content(response_json)
        logger.info("generate_response complete | reply_length=%d", len(reply))
        return reply

    def generate_sql_query(
        self,
        natural_language_question: str,
        schema_context: Optional[str] = None,
        database: Optional[str] = None,
        dialect: str = "Snowflake SQL",
        model: Optional[str] = None,
    ) -> dict[str, str]:
        """
        Translate a natural-language analytics question into a SQL query.

        Parameters
        ----------
        natural_language_question:
            Plain-English question, e.g. "Top 10 customers by revenue last month".
        schema_context:
            DDL or INFORMATION_SCHEMA column metadata as a string.  When
            provided it is injected into the system prompt so the model can
            reference real table/column names.
        database:
            Optional database name hint surfaced to the model.
        dialect:
            SQL dialect string (default: ``"Snowflake SQL"``).
        model:
            Override the default model.

        Returns
        -------
        dict with keys:
            ``sql``      — the generated query (stripped of markdown fences),
            ``question`` — the original question,
            ``dialect``  — the dialect used.
        """
        schema_section = (
            f"\n\n## Available Schema\n```sql\n{schema_context}\n```"
            if schema_context
            else ""
        )
        db_hint = f" The target database is `{database}`." if database else ""

        system_prompt = (
            f"You are an expert {dialect} query writer for an enterprise analytics platform."
            f"{db_hint}"
            " Your task is to write a single, correct, optimised SQL query that answers the"
            " user's question. Return ONLY the SQL statement — no explanations, no markdown"
            " fences, no commentary."
            f"{schema_section}"
        )

        logger.info(
            "generate_sql_query | dialect=%s schema_context_chars=%d question=%.120s",
            dialect,
            len(schema_context) if schema_context else 0,
            natural_language_question,
        )

        raw = self.generate_response(
            user_message=natural_language_question,
            system_prompt=system_prompt,
            model=model,
            temperature=0.0,  # deterministic for SQL generation
        )

        # Strip accidental markdown code fences if the model adds them
        sql = raw.strip().removeprefix("```sql").removeprefix("```").removesuffix("```").strip()

        logger.info("generate_sql_query complete | sql_length=%d", len(sql))
        return {
            "sql": sql,
            "question": natural_language_question,
            "dialect": dialect,
        }


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------

def get_llm_service() -> Generator[LLMService, None, None]:
    """
    FastAPI dependency that yields a configured ``LLMService``.

    Usage
    -----
    .. code-block:: python

        from fastapi import APIRouter, Depends
        from app.services.llm_service import LLMService, get_llm_service

        router = APIRouter()

        @router.post("/ask")
        def ask(question: str, llm: LLMService = Depends(get_llm_service)):
            return {"answer": llm.generate_response(question)}
    """
    yield LLMService()
from fastapi import APIRouter, Depends
from app.services.llm_service import LLMService, get_llm_service
from app.services.snowflake_service import SnowflakeService, get_snowflake_service

router = APIRouter()

@router.post("/nl-to-sql")
def nl_to_sql(
    question: str,
    llm: LLMService = Depends(get_llm_service),
    sf: SnowflakeService = Depends(get_snowflake_service),
):
    schema = sf.get_schema_metadata()
    schema_str = "\n".join(f"{r['TABLE_NAME']}.{r['COLUMN_NAME']} {r['DATA_TYPE']}" for r in schema)
    return llm.generate_sql_query(question, schema_context=schema_str)