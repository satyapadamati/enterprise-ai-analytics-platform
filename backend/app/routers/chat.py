import logging

from fastapi import APIRouter, Depends, HTTPException, status
from requests import HTTPError, Timeout
from snowflake.connector.errors import DatabaseError

from app.schemas.chat import ChatRequest, ChatResponse
from app.services.llm_service import LLMService, get_llm_service
from app.services.snowflake_service import SnowflakeService, get_snowflake_service
from app.services.sql_agent_service import SQLAgentService

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post(
    "/chat",
    response_model=ChatResponse,
    status_code=status.HTTP_200_OK,
    summary="Natural-language analytics query",
    description=(
        "Translates a plain-English question into SQL, executes it against Snowflake, "
        "and returns the results with a business explanation."
    ),
)
def chat(
    request: ChatRequest,
    sf: SnowflakeService = Depends(get_snowflake_service),
    llm: LLMService = Depends(get_llm_service),
) -> ChatResponse:
    logger.info(
        "Chat request | session=%s model=%s message_length=%d table_pattern=%s",
        request.session_id,
        request.model,
        len(request.prompt),
        request.table_pattern,
    )

    agent = SQLAgentService(sf=sf, llm=llm)

    try:
        result = agent.answer(
            question=request.prompt,
            table_pattern=request.table_pattern,
            database=request.database,
            schema=request.schema_name,
        )
    except Timeout:
        logger.error("LLM request timed out | session=%s", request.session_id)
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="LLM request timed out. Please try again.",
        )
    except HTTPError as exc:
        logger.error("LLM API error | session=%s status=%s", request.session_id, exc.response.status_code if exc.response else "unknown")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="LLM service returned an error. Please try again later.",
        )
    except DatabaseError as exc:
        logger.error("Snowflake error | session=%s error=%s", request.session_id, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Data warehouse is unavailable. Please try again later.",
        )
    except Exception as exc:
        logger.exception("Unexpected error in chat endpoint | session=%s error=%s", request.session_id, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred.",
        )

    logger.info(
        "Chat response | session=%s success=%s rows=%d",
        request.session_id,
        result.success,
        result.row_count,
    )

    return ChatResponse(
        session_id=request.session_id,
        model=request.model,
        question=result.question,
        generated_sql=result.sql,
        snowflake_data=result.rows,
        row_count=result.row_count,
        ai_response=result.explanation,
        dialect=result.dialect,
        timestamp=result.timestamp,
        success=result.success,
        error=result.error,
    )
