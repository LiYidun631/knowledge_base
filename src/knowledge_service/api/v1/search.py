"""知识库在线检索 HTTP 接口。"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_service.api.v1.schemas import (
    KnowledgeSearchRequest,
    KnowledgeSearchResponse,
    KnowledgeSearchResult,
    SearchAsset,
    SearchContextItem,
    SearchScores,
    SearchSource,
)
from knowledge_service.application.documents.rendering import render_images
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


@router.post(
    "/knowledge-bases/{knowledge_base_id}/search",
    response_model=KnowledgeSearchResponse,
    summary="检索知识库内容",
    description=(
        "默认并行执行 BM25 关键词召回和 Chroma 向量召回，再使用加权 RRF 融合；"
        "可按知识库绑定的 Rerank 模型重排，结果只包含当前成功版本。"
    ),
)
async def search_knowledge_base_endpoint(
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
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="知识库不存在或已归档"
        ) from exc
    except KnowledgeSearchDocumentsInvalid as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="document_ids 中存在不属于该知识库的文档",
        ) from exc
    except KnowledgeSearchModelUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"知识库绑定的{exc}模型不可用",
        ) from exc
    except EmbeddingProviderError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except ChromaSearchError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Chroma 向量检索暂不可用",
        ) from exc
    except RerankProviderError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc

    return KnowledgeSearchResponse(
        company_id=company_id,
        knowledge_base_id=knowledge_base_id,
        query=payload.query,
        retrieval_mode=payload.retrieval_mode,
        results=[_result_to_schema(index, result) for index, result in enumerate(results, start=1)],
    )
