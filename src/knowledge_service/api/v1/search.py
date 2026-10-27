"""知识库在线检索 HTTP 接口。"""

import time
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_service.api.v1.schemas import (
    KnowledgeSearchRequest,
    KnowledgeSearchResponse,
    KnowledgeSearchResult,
    ProjectTraceCompletionRead,
    ProjectTraceCompletionRequest,
    SearchAsset,
    SearchContextItem,
    SearchScores,
    SearchSource,
)
from knowledge_service.application.documents.rendering import render_images
from knowledge_service.application.project_traces import (
    PROJECT_KEY_HEADER,
    SESSION_ID_HEADER,
    TRACE_ID_HEADER,
    USER_REF_HEADER,
    ProjectTraceNotFound,
    ProjectTraceScopeInvalid,
    complete_trace,
    record_retrieval,
)
from knowledge_service.application.search.service import (
    KnowledgeSearchDocumentsInvalid,
    KnowledgeSearchModelUnavailable,
    KnowledgeSearchNotFound,
    search_knowledge_base,
)
from knowledge_service.core.config import get_settings
from knowledge_service.infrastructure.chroma.vector_store import ChromaSearchError
from knowledge_service.infrastructure.model_providers.embeddings import EmbeddingProviderError
from knowledge_service.infrastructure.model_providers.rerank import RerankProviderError
from knowledge_service.infrastructure.persistence.session import get_db_session

router = APIRouter(tags=["知识库在线检索"])


def _result_to_schema(index: int, result) -> KnowledgeSearchResult:
    """将应用层结果统一转换为检索和问答接口共用的响应结构。"""
    settings = get_settings()
    asset_base_url = settings.public_base_url.rstrip("/")
    return KnowledgeSearchResult(
        rank=index,
        content=render_images(result.content, result.assets or []),
        scores=SearchScores(
            cosine_distance=result.cosine_distance,
            cosine_similarity=(
                1 - result.cosine_distance if result.cosine_distance is not None else None
            ),
            bm25_score=result.bm25_score,
            rrf_score=result.rrf_score,
            rerank_score=result.rerank_score,
        ),
        source=SearchSource(
            document_id=result.document_id,
            document_version_id=result.document_version_id,
            version_number=result.version_number,
            filename=result.filename,
            file_id=result.file_id or "",
            chunk_id=result.chunk_id,
            page_number=result.page_number,
            section=result.section,
            section_path=getattr(result, "section_path", []),
        ),
        assets=[
            SearchAsset(
                asset_id=asset.id,
                media_type=asset.media_type,
                description=asset.description,
                page_number=asset.page_number,
                bbox=asset.bbox,
                url=f"{asset_base_url}{settings.api_prefix}/document-assets/{asset.id}",
                location=getattr(asset, "location", None),
            )
            for asset in (result.assets or [])
        ],
        context=render_images(result.context, result.assets or []) if result.context else None,
        context_sources=[
            SearchContextItem(
                chunk_id=item.chunk_id,
                relation=item.relation,
                content=render_images(item.content, result.assets or []),
            )
            for item in result.context_sources
        ],
    )


def _trace_header(request: Request, name: str, limit: int) -> str | None:
    """读取轨迹请求头并按列长度截断，超长值不应导致检索失败。"""
    value = request.headers.get(name)
    if value is None:
        return None
    value = value.strip()
    return value[:limit] or None


async def _record_trace(
    request: Request,
    session: AsyncSession,
    *,
    knowledge_base_id: UUID,
    company_id: UUID,
    payload: KnowledgeSearchRequest,
    results,
    duration_ms: int,
    error: Exception | None,
) -> None:
    """调用方声明项目轨迹时记录本次检索；未声明时保持无状态检索。"""
    project_key = _trace_header(request, PROJECT_KEY_HEADER, 80)
    external_request_id = _trace_header(request, TRACE_ID_HEADER, 128)
    if not project_key or not external_request_id:
        return
    await record_retrieval(
        session,
        company_id=company_id,
        knowledge_base_id=knowledge_base_id,
        project_key=project_key,
        external_request_id=external_request_id,
        session_ref=_trace_header(request, SESSION_ID_HEADER, 128),
        user_ref=_trace_header(request, USER_REF_HEADER, 64),
        query=payload.query,
        retrieval_mode=payload.retrieval_mode,
        top_k=payload.top_k,
        include_context=payload.include_context,
        product_name=payload.product_name,
        model_name=payload.model_name,
        results=results,
        duration_ms=duration_ms,
        error_code=type(error).__name__ if error is not None else None,
        error_message=str(error) if error is not None else None,
    )


@router.post(
    "/knowledge-bases/{knowledge_base_id}/search",
    response_model=KnowledgeSearchResponse,
    summary="检索知识库内容",
    description=(
        "默认并行执行 BM25 关键词召回和 Chroma 向量召回，再使用加权 RRF 融合；"
        "可按知识库绑定的 Rerank 模型重排，结果只包含当前成功版本。"
        "可选传入 X-Project-Key、X-Trace-Id、X-Session-Id、X-User-Ref 请求头记录项目轨迹。"
    ),
)
async def search_knowledge_base_endpoint(
    request: Request,
    knowledge_base_id: Annotated[UUID, Path(description="待检索的知识库 ID。")],
    company_id: Annotated[UUID, Query(description="公司租户 ID；必须与子库归属一致。")],
    payload: KnowledgeSearchRequest,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> KnowledgeSearchResponse:
    """执行混合召回并返回父块正文与文档来源。"""
    settings = get_settings()
    if payload.top_k > settings.search_max_top_k:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"top_k 不能大于服务配置上限 {settings.search_max_top_k}",
        )
    started_at = time.perf_counter()
    results = []
    error: Exception | None = None
    try:
        results = await search_knowledge_base(
            session,
            knowledge_base_id=knowledge_base_id,
            company_id=company_id,
            query=payload.query,
            top_k=payload.top_k,
            document_ids=payload.document_ids,
            retrieval_mode=payload.retrieval_mode,
            include_context=payload.include_context,
            product_name=payload.product_name,
            model_name=payload.model_name,
        )
    except KnowledgeSearchNotFound as exc:
        error = exc
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="知识库不存在或已归档"
        ) from exc
    except KnowledgeSearchDocumentsInvalid as exc:
        error = exc
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="document_ids 中存在不属于该知识库的文档",
        ) from exc
    except KnowledgeSearchModelUnavailable as exc:
        error = exc
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"知识库绑定的{exc}模型不可用",
        ) from exc
    except EmbeddingProviderError as exc:
        error = exc
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except ChromaSearchError as exc:
        error = exc
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Chroma 向量检索暂不可用",
        ) from exc
    except RerankProviderError as exc:
        error = exc
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        error = exc
        raise
    finally:
        # 轨迹写入使用保存点并吞掉异常，检索结果始终优先返回。
        await _record_trace(
            request,
            session,
            knowledge_base_id=knowledge_base_id,
            company_id=company_id,
            payload=payload,
            results=results,
            duration_ms=max(0, int((time.perf_counter() - started_at) * 1000)),
            error=error,
        )

    return KnowledgeSearchResponse(
        company_id=company_id,
        knowledge_base_id=knowledge_base_id,
        query=payload.query,
        retrieval_mode=payload.retrieval_mode,
        results=[_result_to_schema(index, result) for index, result in enumerate(results, start=1)],
    )


@router.put(
    "/knowledge-bases/{knowledge_base_id}/project-traces/{external_request_id}",
    response_model=ProjectTraceCompletionRead,
    summary="回填项目问答轨迹",
    description=(
        "外部项目完成回答后回填答案、状态、模型与耗时；检索记录缺失时补建轨迹。"
        "项目标识通过 X-Project-Key 请求头传入，需与检索时保持一致。"
    ),
)
async def complete_project_trace_endpoint(
    knowledge_base_id: Annotated[UUID, Path(description="检索使用的知识库 ID。")],
    external_request_id: Annotated[
        str, Path(min_length=1, max_length=128, description="调用方请求 ID。")
    ],
    payload: ProjectTraceCompletionRequest,
    company_id: Annotated[UUID, Query(description="公司租户 ID；必须与知识库归属一致。")],
    project_key: Annotated[
        str,
        Header(
            alias=PROJECT_KEY_HEADER,
            min_length=1,
            max_length=80,
            description="项目标识；与检索请求头 X-Project-Key 一致。",
        ),
    ],
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> ProjectTraceCompletionRead:
    """回填项目侧答案；项目与知识库归属不一致时返回 404。"""
    try:
        trace = await complete_trace(
            session,
            company_id=company_id,
            knowledge_base_id=knowledge_base_id,
            project_key=project_key,
            external_request_id=external_request_id,
            query=payload.query,
            answer=payload.answer,
            status=payload.status,
            answer_mode=payload.answer_mode,
            intent=payload.intent,
            chat_provider=payload.chat_provider,
            chat_model_name=payload.chat_model_name,
            generation_duration_ms=payload.generation_duration_ms,
            total_duration_ms=payload.total_duration_ms,
            prompt_tokens=payload.prompt_tokens,
            completion_tokens=payload.completion_tokens,
            total_tokens=payload.total_tokens,
            error_code=payload.error_code,
            error_message=payload.error_message,
            extra_metadata=payload.extra_metadata,
        )
    except ProjectTraceNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="项目未登记或已停用"
        ) from exc
    except ProjectTraceScopeInvalid as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="知识库不存在或不属于该公司"
        ) from exc
    return ProjectTraceCompletionRead(
        id=trace.id, status=trace.status, finished_at=trace.finished_at
    )
