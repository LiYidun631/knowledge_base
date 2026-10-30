"""管理端配置接口：模型厂商预设、模型档案与问答记录管理；全部仅限管理员。"""

import asyncio
import csv
import io
import math
import time
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from datetime import time as day_start
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response, status
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_service.api.auth import (
    ServicePrincipal,
    authenticate_service,
    require_admin_service_auth,
)
from knowledge_service.api.v1.schemas import (
    AdminCostRead,
    AdminCredentialCreate,
    AdminCredentialIssued,
    AdminCredentialPatch,
    AdminCredentialRead,
    AdminDailyTrendRead,
    AdminLatencyBreakdownRead,
    AdminLatencyStatsRead,
    AdminMeRead,
    AdminModelCostRead,
    AdminPromptRead,
    AdminPromptWrite,
    AdminRecentStatisticsRead,
    AdminStatisticsRangeRead,
    AdminStatisticsRead,
    AdminTaskCountsRead,
    AdminTaskListRead,
    AdminTaskRead,
    AdminTokenUsageRead,
    AuditLogListResponse,
    AuditLogRead,
    ChunkDefaultsRead,
    GlobalSearchGroup,
    GlobalSearchHit,
    GlobalSearchRead,
    ModelApiKeyWrite,
    ModelPriceRead,
    ModelPriceWrite,
    ModelProfilePatch,
    ModelProfileRead,
    ModelProfileTestRead,
    ModelProfileWrite,
    ModelProviderPreset,
    ModelUsageItemRead,
    ModelUsageRead,
    ModelUsageSlotRead,
    ProjectCreate,
    ProjectRead,
    ProjectTraceAdminListResponse,
    ProjectTraceAdminRead,
    ProjectTraceDetail,
    ProjectTraceRead,
    ProjectTraceSourceRead,
    QAEventRead,
    QARecordAdminListResponse,
    QARecordAdminRead,
    QARecordDetail,
    QARecordRead,
    QARecordReviewPatch,
    QASourceRead,
)
from knowledge_service.application import admin_auth, admin_credentials
from knowledge_service.application import model_profiles as service
from knowledge_service.application.documents.rendering import render_images
from knowledge_service.core.config import Settings, get_settings
from knowledge_service.core.credentials import resolve_profile_api_key
from knowledge_service.core.ocr_runtime import OcrRuntimeConfig, use_ocr_runtime
from knowledge_service.domain.errors import (
    AdminCredentialNotFound,
    CredentialEncryptionError,
    ModelProfileInUse,
    ModelProfileNotFound,
)
from knowledge_service.infrastructure.model_providers.chat import (
    FAQ_GENERATION_PROMPT_NAME,
    FAQ_REWRITE_PROMPT_NAME,
    SYSTEM_PROMPT_NAME,
    ChatProviderError,
    generate_answer,
    resolve_prompt,
)
from knowledge_service.infrastructure.model_providers.embeddings import (
    EmbeddingProviderError,
    embed_texts,
)
from knowledge_service.infrastructure.model_providers.rerank import (
    RerankProviderError,
    rerank_documents,
)
from knowledge_service.infrastructure.ocr_client import OcrServiceError, analyze_image
from knowledge_service.infrastructure.persistence.models import (
    AdminCredential,
    AdminPromptOverride,
    AppProject,
    AuditLog,
    Company,
    Document,
    DocumentAsset,
    DocumentVersion,
    EvaluationRun,
    EvaluationSet,
    FaqEntry,
    IngestionJob,
    KnowledgeBase,
    ModelPrice,
    ModelProfile,
    OutboxEvent,
    ProductCatalog,
    ProjectQaSource,
    ProjectQaTrace,
    QAEvent,
    QARecord,
    QASource,
    utc_now,
)
from knowledge_service.infrastructure.persistence.session import get_db_session

router = APIRouter(
    prefix="/admin",
    tags=["管理端配置"],
    dependencies=[Depends(require_admin_service_auth)],
)

# 管理页可编辑的提示词注册表；name 与生成链路共用，label 仅用于展示。
PROMPT_REGISTRY: dict[str, str] = {
    SYSTEM_PROMPT_NAME: "问答生成系统提示词",
    FAQ_GENERATION_PROMPT_NAME: "FAQ 自动生成提示词",
    FAQ_REWRITE_PROMPT_NAME: "FAQ AI 改写提示词",
}


def _read(profile: ModelProfile) -> ModelProfileRead:
    """拼装档案视图；has_api_key 只表示是否已配置，不携带任何密钥内容。"""
    return ModelProfileRead(
        id=profile.id,
        name=profile.name,
        kind=profile.kind,
        provider=profile.provider,
        model_name=profile.model_name,
        dimensions=profile.dimensions,
        settings=profile.settings,
        secret_ref=profile.secret_ref,
        is_enabled=profile.is_enabled,
        is_default=profile.is_default,
        has_api_key=profile.api_key_cipher is not None,
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )


@router.get("/model-presets", response_model=list[ModelProviderPreset], summary="模型厂商预设")
async def list_model_presets() -> list[ModelProviderPreset]:
    """代码内置的厂商预设；界面据此生成服务商下拉选项。"""
    return [ModelProviderPreset(**preset) for preset in service.VENDOR_PRESETS]


@router.get("/chunk-defaults", response_model=ChunkDefaultsRead, summary="全局分块参数默认值")
async def get_chunk_defaults(
    settings: Annotated[Settings, Depends(get_settings)],
) -> ChunkDefaultsRead:
    """建库向导用于预填表单；子库不传分块参数时即跟随这些默认值。"""
    return ChunkDefaultsRead(
        parent_chunk_chars=settings.parent_chunk_chars,
        child_chunk_chars=settings.child_chunk_chars,
        child_chunk_overlap_chars=settings.child_chunk_overlap_chars,
    )


@router.get("/model-usage", response_model=ModelUsageRead, summary="模型调用位置总览")
async def get_model_usage(
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ModelUsageRead:
    """汇总服务内部实际调用的模型：知识库绑定、默认档案与环境兜底模型。"""
    profiles = await service.list_model_profiles(session)
    by_id = {item.id: item for item in profiles}

    rows = (
        await session.execute(
            select(
                KnowledgeBase.name,
                KnowledgeBase.embedding_model_id,
                KnowledgeBase.rerank_model_id,
                Company.name,
            )
            .outerjoin(Company, Company.id == KnowledgeBase.company_id)
            .where(KnowledgeBase.status == "active")
            .order_by(KnowledgeBase.name)
        )
    ).all()
    embedding_scopes: dict[UUID, list[str]] = {}
    rerank_scopes: dict[UUID, list[str]] = {}
    for kb_name, embedding_id, rerank_id, company_name in rows:
        scope = f"{company_name} / {kb_name}" if company_name else kb_name
        embedding_scopes.setdefault(embedding_id, []).append(scope)
        if rerank_id is not None:
            rerank_scopes.setdefault(rerank_id, []).append(scope)

    def item(
        profile: ModelProfile,
        *,
        source: Literal["knowledge_base", "default"],
        scopes: list[str] | None = None,
    ) -> ModelUsageItemRead:
        return ModelUsageItemRead(
            profile_id=profile.id,
            name=profile.name,
            provider=profile.provider,
            model_name=profile.model_name,
            source=source,
            scope_names=scopes or [],
            is_default=profile.is_default,
            is_enabled=profile.is_enabled,
        )

    embedding_items = [
        item(by_id[profile_id], source="knowledge_base", scopes=scopes)
        for profile_id, scopes in embedding_scopes.items()
        if profile_id in by_id
    ]
    # 没有知识库绑定时也要展示默认档案，新建库会沿用它。
    default_embedding = next(
        (p for p in profiles if p.kind == "embedding" and p.is_default and p.is_enabled), None
    )
    if default_embedding is not None and default_embedding.id not in embedding_scopes:
        embedding_items.append(item(default_embedding, source="default"))

    rerank_items = [
        item(by_id[profile_id], source="knowledge_base", scopes=scopes)
        for profile_id, scopes in rerank_scopes.items()
        if profile_id in by_id
    ]
    # 默认 Rerank 会在新建知识库时自动绑定，未被绑定时同样需要展示。
    default_rerank = next(
        (p for p in profiles if p.kind == "rerank" and p.is_default and p.is_enabled), None
    )
    if default_rerank is not None and default_rerank.id not in rerank_scopes:
        rerank_items.append(item(default_rerank, source="default"))

    default_chat = next(
        (p for p in profiles if p.kind == "chat" and p.is_default and p.is_enabled), None
    )
    chat_items = [item(default_chat, source="default")] if default_chat is not None else []
    # .env 生成模型始终列出；有默认 Chat 档案时作为未生效的兜底。
    chat_items.append(
        ModelUsageItemRead(
            name="环境兜底模型",
            provider=settings.chat_provider,
            model_name=settings.chat_model_name,
            source="environment",
            is_active=default_chat is None,
        )
    )

    default_ocr = next(
        (p for p in profiles if p.kind == "ocr" and p.is_default and p.is_enabled), None
    )
    ocr_items = [item(default_ocr, source="default")] if default_ocr is not None else []
    # .env OCR 模型始终列出；有默认 OCR 档案时作为未生效的兜底。
    ocr_items.append(
        ModelUsageItemRead(
            name="环境 OCR 模型",
            provider=settings.ocr_secret_ref,
            model_name=settings.ocr_model_name,
            source="environment",
            is_active=default_ocr is None and bool(settings.ocr_model_name.strip()),
        )
    )

    return ModelUsageRead(
        slots=[
            ModelUsageSlotRead(key="embedding", items=embedding_items),
            ModelUsageSlotRead(key="rerank", items=rerank_items),
            ModelUsageSlotRead(key="chat", items=chat_items),
            ModelUsageSlotRead(key="ocr", items=ocr_items),
        ]
    )


def _price_read(price: ModelPrice) -> ModelPriceRead:
    """单价视图；数据库存 Decimal，接口转浮点便于前端直接展示。"""
    return ModelPriceRead(
        id=price.id,
        model_name=price.model_name,
        provider=price.provider,
        input_price_per_million=float(price.input_price_per_million),
        output_price_per_million=float(price.output_price_per_million),
        updated_at=price.updated_at,
    )


@router.get(
    "/model-prices",
    response_model=list[ModelPriceRead],
    summary="生成模型单价列表",
    description="按模型名列出已配置的 token 单价（元/百万 token），用于问答成本核算。",
)
async def list_model_prices(
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> list[ModelPriceRead]:
    rows = await session.scalars(select(ModelPrice).order_by(ModelPrice.model_name))
    return [_price_read(row) for row in rows]


@router.put(
    "/model-prices",
    response_model=ModelPriceRead,
    summary="新增或修改生成模型单价",
    description=(
        "按模型名新增或覆盖单价；模型名与问答记录中的生成模型名一致即可参与成本核算，"
        "包括 .env 兜底模型。"
    ),
)
async def upsert_model_price(
    payload: ModelPriceWrite,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> ModelPriceRead:
    price = await session.scalar(
        select(ModelPrice).where(ModelPrice.model_name == payload.model_name)
    )
    if price is None:
        price = ModelPrice(
            model_name=payload.model_name,
            provider=payload.provider,
            input_price_per_million=payload.input_price_per_million,
            output_price_per_million=payload.output_price_per_million,
        )
        session.add(price)
    else:
        price.provider = payload.provider
        price.input_price_per_million = payload.input_price_per_million
        price.output_price_per_million = payload.output_price_per_million
    await session.flush()
    return _price_read(price)


@router.delete(
    "/model-prices/{model_price_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除生成模型单价",
    description="删除后该模型不再计入成本核算，token 仍保留在用量统计中。",
)
async def delete_model_price(
    model_price_id: Annotated[UUID, Path(description="单价记录 ID。")],
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> Response:
    price = await session.get(ModelPrice, model_price_id)
    if price is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="单价记录不存在")
    await session.delete(price)
    await session.flush()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/model-profiles", response_model=list[ModelProfileRead], summary="模型档案列表")
async def list_model_profiles(
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
    kind: Annotated[
        Literal["embedding", "rerank", "chat", "ocr"] | None,
        Query(description="按模型类型过滤；不传返回全部。"),
    ] = None,
) -> list[ModelProfileRead]:
    items = await service.list_model_profiles(session, kind=kind)
    return [_read(item) for item in items]


@router.post(
    "/model-profiles",
    response_model=ModelProfileRead,
    status_code=status.HTTP_201_CREATED,
    summary="创建模型档案",
)
async def create_model_profile(
    payload: ModelProfileWrite,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> ModelProfileRead:
    try:
        profile = await service.create_model_profile(session, payload)
    except ModelProfileInUse as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _read(profile)


@router.get(
    "/model-profiles/{model_profile_id}", response_model=ModelProfileRead, summary="模型档案详情"
)
async def get_model_profile(
    model_profile_id: UUID,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> ModelProfileRead:
    try:
        profile = await service.get_model_profile(session, model_profile_id=model_profile_id)
    except ModelProfileNotFound as exc:
        raise HTTPException(status_code=404, detail="模型档案不存在") from exc
    return _read(profile)


@router.patch(
    "/model-profiles/{model_profile_id}", response_model=ModelProfileRead, summary="修改模型档案"
)
async def update_model_profile(
    model_profile_id: UUID,
    payload: ModelProfilePatch,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> ModelProfileRead:
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=422, detail="至少提供一个要修改的字段")
    try:
        current = await service.get_model_profile(session, model_profile_id=model_profile_id)
        # 用当前值与补丁合并后整体校验，避免部分修改组合出无效档案。
        base = {field: getattr(current, field) for field in service.ModelProfileSpec.model_fields}
        merged = service.ModelProfileSpec(**{**base, **changes})
        profile = await service.update_model_profile(
            session, model_profile_id=model_profile_id, spec=merged
        )
    except ModelProfileNotFound as exc:
        raise HTTPException(status_code=404, detail="模型档案不存在") from exc
    except ModelProfileInUse as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _read(profile)


@router.delete(
    "/model-profiles/{model_profile_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除模型档案",
    description="仍被大库或子库引用的档案拒绝删除，需先删除或重建相关库。",
)
async def delete_model_profile(
    model_profile_id: UUID,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> Response:
    try:
        await service.delete_model_profile(session, model_profile_id=model_profile_id)
    except ModelProfileNotFound as exc:
        raise HTTPException(status_code=404, detail="模型档案不存在") from exc
    except ModelProfileInUse as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="模型档案仍被引用，无法删除") from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/model-profiles/{model_profile_id}/default",
    response_model=ModelProfileRead,
    summary="设为同类默认模型",
)
async def set_default_model_profile(
    model_profile_id: UUID,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> ModelProfileRead:
    try:
        profile = await service.set_default_model_profile(
            session, model_profile_id=model_profile_id
        )
    except ModelProfileNotFound as exc:
        raise HTTPException(status_code=404, detail="模型档案不存在") from exc
    return _read(profile)


@router.delete(
    "/model-profiles/{model_profile_id}/default",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="取消默认标记",
)
async def clear_default_model_profile(
    model_profile_id: UUID,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> Response:
    try:
        await service.clear_default_model_profile(session, model_profile_id=model_profile_id)
    except ModelProfileNotFound as exc:
        raise HTTPException(status_code=404, detail="模型档案不存在") from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put(
    "/model-profiles/{model_profile_id}/api-key",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="写入模型 API Key",
    description="密钥经 Fernet 加密后入库，只写不回读；读取接口仅返回是否已配置。",
)
async def set_model_api_key(
    model_profile_id: UUID,
    payload: ModelApiKeyWrite,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> Response:
    try:
        await service.set_model_api_key(
            session,
            model_profile_id=model_profile_id,
            plaintext=payload.api_key.get_secret_value(),
        )
    except ModelProfileNotFound as exc:
        raise HTTPException(status_code=404, detail="模型档案不存在") from exc
    except CredentialEncryptionError as exc:
        await session.rollback()
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return Response(
        status_code=status.HTTP_204_NO_CONTENT,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


@router.delete(
    "/model-profiles/{model_profile_id}/api-key",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="清除模型 API Key",
)
async def clear_model_api_key(
    model_profile_id: UUID,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> Response:
    try:
        await service.clear_model_api_key(session, model_profile_id=model_profile_id)
    except ModelProfileNotFound as exc:
        raise HTTPException(status_code=404, detail="模型档案不存在") from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _ocr_probe_image() -> bytes:
    """用 PyMuPDF 生成带文字的测试图，避免为连通性测试引入图像依赖。"""
    import fitz

    document = fitz.open()
    try:
        page = document.new_page(width=360, height=140)
        page.insert_text((40, 80), "OCR TEST 123", fontsize=30)
        return page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False).tobytes("png")
    finally:
        document.close()


@router.post(
    "/model-profiles/{model_profile_id}/test",
    response_model=ModelProfileTestRead,
    summary="测试模型档案连通性",
    description=(
        "按档案类型发起一次最小真实调用：Embedding 校验返回维度，Rerank 校验排序结果，"
        "Chat 校验能否生成回复，OCR 校验能否识别测试图片。失败仍返回 200，"
        "由 ok=false 和 error_code 说明原因。"
    ),
)
async def test_model_profile(
    model_profile_id: UUID,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> ModelProfileTestRead:
    try:
        profile = await service.get_model_profile(session, model_profile_id=model_profile_id)
    except ModelProfileNotFound as exc:
        raise HTTPException(status_code=404, detail="模型档案不存在") from exc

    started = time.perf_counter()
    try:
        if profile.kind == "embedding":
            vectors = await embed_texts(profile, ["知识库连通性测试"])
            message = f"Embedding 调用成功，返回 {len(vectors[0])} 维向量"
        elif profile.kind == "rerank":
            ranked = await rerank_documents(
                profile,
                query="知识库连通性测试",
                documents=["测试文档一", "测试文档二"],
            )
            message = f"Rerank 调用成功，返回 {len(ranked)} 条排序结果"
        elif profile.kind == "ocr":
            runtime = OcrRuntimeConfig(
                base_url=str(profile.settings.get("base_url", "")),
                chat_path=str(profile.settings.get("chat_path", "/chat/completions")),
                model_name=profile.model_name,
                api_key=resolve_profile_api_key(profile),
                mode=str(profile.settings.get("ocr_mode", "auto")),
                profile_name=profile.name,
                settings=dict(profile.settings),
            )
            with use_ocr_runtime(runtime):
                recognized = await asyncio.to_thread(
                    lambda: analyze_image(
                        _ocr_probe_image(),
                        settings=get_settings(),
                        filename="ocr-test.png",
                    ).markdown.strip()
                )
            message = f"OCR 调用成功，识别到：{recognized[:60]}" if recognized else "OCR 调用成功"
        else:
            answer = await generate_answer(
                context="这是一次模型连通性测试，没有参考资料。",
                query="请只回复“正常”两个字。",
                profile=profile,
            )
            message = f"Chat 调用成功，模型回复：{answer[:60]}"
    except (
        EmbeddingProviderError,
        RerankProviderError,
        ChatProviderError,
        OcrServiceError,
    ) as exc:
        # 适配器错误统一为“错误码: 说明”，拆出错误码供界面区分配置错误与网络故障。
        code, _, detail = str(exc).partition(":")
        return ModelProfileTestRead(
            ok=False,
            kind=profile.kind,
            latency_ms=int((time.perf_counter() - started) * 1000),
            message=detail.strip() or str(exc),
            error_code=code.strip() or None,
        )
    except CredentialEncryptionError as exc:
        return ModelProfileTestRead(
            ok=False,
            kind=profile.kind,
            latency_ms=int((time.perf_counter() - started) * 1000),
            message=str(exc),
        )

    return ModelProfileTestRead(
        ok=True,
        kind=profile.kind,
        latency_ms=int((time.perf_counter() - started) * 1000),
        message=message,
    )


@router.get("/me", response_model=AdminMeRead, summary="当前管理员身份与角色")
async def get_admin_me(
    principal: Annotated[ServicePrincipal, Depends(authenticate_service)],
) -> AdminMeRead:
    """管理页据此决定菜单与写操作入口；环境引导 Token 固定为 admin。"""
    return AdminMeRead(
        name=principal.name,
        role=principal.role,
        credential_id=principal.credential_id,
        is_bootstrap=principal.is_bootstrap,
    )


def _read_admin_credential(credential: AdminCredential) -> AdminCredentialRead:
    """凭据转响应；has_password 是派生字段，ORM 上没有对应列。"""
    return AdminCredentialRead.model_validate(
        {
            "id": credential.id,
            "name": credential.name,
            "role": credential.role,
            "is_enabled": credential.is_enabled,
            "username": credential.username,
            "has_password": bool(credential.password_hash),
            "created_at": credential.created_at,
            "updated_at": credential.updated_at,
        }
    )


@router.get("/credentials", response_model=list[AdminCredentialRead], summary="管理员凭据列表")
async def list_admin_credentials(
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> list[AdminCredentialRead]:
    """列出库内管理员凭据；不返回哈希或明文，.env 引导 Token 不在此列。"""
    return [
        _read_admin_credential(item)
        for item in await admin_credentials.list_admin_credentials(session)
    ]


@router.post(
    "/credentials",
    response_model=AdminCredentialIssued,
    status_code=status.HTTP_201_CREATED,
    summary="创建管理员凭据",
    description="明文 Token 仅本次返回；只保存哈希。可同时开通账号密码登录。",
)
async def create_admin_credential(
    payload: AdminCredentialCreate,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> AdminCredentialIssued:
    if payload.username and not payload.password:
        raise HTTPException(status_code=400, detail="开通账号密码登录需同时设置密码")
    try:
        if payload.username:
            await admin_auth.ensure_username_available(session, username=payload.username)
        credential, token = await admin_credentials.create_admin_credential(
            session,
            name=payload.name,
            role=payload.role,
            username=payload.username,
            password=payload.password,
        )
        # 明文仅返回一次，必须先提交成功，不能依赖响应发送后的会话清理提交。
        await session.commit()
    except admin_auth.LoginAccountTaken as exc:
        raise HTTPException(status_code=409, detail="登录账号已被占用") from exc
    except IntegrityError as exc:
        # 唯一索引兜底：并发创建同一账号时不让原始数据库错误外泄。
        await session.rollback()
        raise HTTPException(status_code=409, detail="登录账号已被占用") from exc
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return AdminCredentialIssued(admin_credential_id=credential.id, token=token)


@router.post(
    "/credentials/{admin_credential_id}/reset",
    response_model=AdminCredentialIssued,
    summary="重置管理员凭据",
    description="替换已有凭据并启用；旧 Token 对新请求立即失效，新明文仅本次返回。",
)
async def reset_admin_credential(
    admin_credential_id: UUID,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> AdminCredentialIssued:
    try:
        token = await admin_credentials.reset_admin_credential(
            session, admin_credential_id=admin_credential_id
        )
        await session.commit()
    except AdminCredentialNotFound as exc:
        raise HTTPException(status_code=404, detail="管理员凭据不存在") from exc
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return AdminCredentialIssued(admin_credential_id=admin_credential_id, token=token)


async def _ensure_admin_remains(
    session: AsyncSession, settings: Settings, *, admin_credential_id: UUID
) -> None:
    """环境引导 Token 缺失时，禁止把库内最后一名启用管理员撤掉。"""
    if settings.auth_admin_token.get_secret_value():
        return
    remaining = await admin_credentials.count_enabled_admins(
        session, exclude_id=admin_credential_id
    )
    if remaining == 0:
        raise HTTPException(status_code=409, detail="至少保留一名启用中的管理员凭据")


@router.patch(
    "/credentials/{admin_credential_id}",
    response_model=AdminCredentialRead,
    summary="修改管理员凭据（启停、角色与账号密码）",
)
async def patch_admin_credential(
    admin_credential_id: UUID,
    payload: AdminCredentialPatch,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
    settings: Annotated[Settings, Depends(get_settings)],
) -> AdminCredentialRead:
    try:
        credential = await admin_credentials.get_admin_credential(
            session, admin_credential_id=admin_credential_id
        )
        next_role = payload.role if payload.role is not None else credential.role
        next_enabled = (
            payload.is_enabled if payload.is_enabled is not None else credential.is_enabled
        )
        if (
            credential.role == "admin"
            and credential.is_enabled
            and (next_role != "admin" or not next_enabled)
        ):
            await _ensure_admin_remains(session, settings, admin_credential_id=admin_credential_id)
        if payload.username:
            # 开通或改名都要确认账号未被占用；密码可以后补，但没密码时账号登录不了。
            await admin_auth.ensure_username_available(
                session, username=payload.username, exclude_id=admin_credential_id
            )
        credential = await admin_credentials.update_admin_credential(
            session,
            admin_credential_id=admin_credential_id,
            role=payload.role,
            is_enabled=payload.is_enabled,
            username=payload.username,
            password=payload.password,
        )
        await session.commit()
    except admin_auth.LoginAccountTaken as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="登录账号已被占用") from exc
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="登录账号已被占用") from exc
    except AdminCredentialNotFound as exc:
        raise HTTPException(status_code=404, detail="管理员凭据不存在") from exc
    return _read_admin_credential(credential)


@router.delete(
    "/credentials/{admin_credential_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除管理员凭据",
)
async def delete_admin_credential(
    admin_credential_id: UUID,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    try:
        credential = await admin_credentials.get_admin_credential(
            session, admin_credential_id=admin_credential_id
        )
        if credential.role == "admin" and credential.is_enabled:
            await _ensure_admin_remains(session, settings, admin_credential_id=admin_credential_id)
        await admin_credentials.delete_admin_credential(
            session, admin_credential_id=admin_credential_id
        )
    except AdminCredentialNotFound as exc:
        raise HTTPException(status_code=404, detail="管理员凭据不存在") from exc


AUDIT_EXPORT_MAX_ROWS = 50_000
AUDIT_EXPORT_HEADERS = [
    "时间(UTC)",
    "操作者",
    "角色",
    "方法",
    "路径",
    "状态码",
    "耗时(ms)",
    "客户端 IP",
]


def _audit_conditions(
    *,
    actor_name: str | None,
    method: str | None,
    path: str | None,
    start_time: datetime | None,
    end_time: datetime | None,
) -> list[Any]:
    conditions: list[Any] = []
    if actor_name:
        conditions.append(AuditLog.actor_name.like(f"%{actor_name.strip()}%"))
    if method:
        conditions.append(AuditLog.method == method)
    if path:
        conditions.append(AuditLog.path.like(f"%{path.strip()}%"))
    if start_time is not None:
        conditions.append(AuditLog.created_at >= start_time)
    if end_time is not None:
        conditions.append(AuditLog.created_at <= end_time)
    return conditions


@router.get(
    "/audit-logs",
    response_model=AuditLogListResponse,
    summary="审计日志列表",
    description="仅管理员可读；按操作时间倒序返回管理端写操作记录，不含请求体。",
)
async def list_audit_logs(
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
    actor_name: Annotated[str | None, Query(description="按操作者名称模糊匹配。")] = None,
    method: Annotated[
        Literal["POST", "PUT", "PATCH", "DELETE"] | None,
        Query(description="按请求方法过滤。"),
    ] = None,
    path: Annotated[str | None, Query(description="按请求路径模糊匹配。")] = None,
    start_time: Annotated[datetime | None, Query(description="操作时间下限（含）。")] = None,
    end_time: Annotated[datetime | None, Query(description="操作时间上限（含）。")] = None,
    page: Annotated[int, Query(ge=1, description="页码，从 1 开始。")] = 1,
    page_size: Annotated[int, Query(ge=1, le=200, description="每页记录数。")] = 50,
) -> AuditLogListResponse:
    conditions = _audit_conditions(
        actor_name=actor_name,
        method=method,
        path=path,
        start_time=start_time,
        end_time=end_time,
    )
    total = int(await session.scalar(select(func.count(AuditLog.id)).where(*conditions)) or 0)
    rows = list(
        await session.scalars(
            select(AuditLog)
            .where(*conditions)
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    return AuditLogListResponse(
        page=page,
        page_size=page_size,
        total=total,
        items=[AuditLogRead.model_validate(item) for item in rows],
    )


@router.get(
    "/audit-logs/export",
    summary="导出审计日志 CSV",
    description=f"按与列表相同的筛选条件导出，单次最多 {AUDIT_EXPORT_MAX_ROWS} 条。",
    response_class=Response,
)
async def export_audit_logs(
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
    actor_name: Annotated[str | None, Query(description="按操作者名称模糊匹配。")] = None,
    method: Annotated[
        Literal["POST", "PUT", "PATCH", "DELETE"] | None,
        Query(description="按请求方法过滤。"),
    ] = None,
    path: Annotated[str | None, Query(description="按请求路径模糊匹配。")] = None,
    start_time: Annotated[datetime | None, Query(description="操作时间下限（含）。")] = None,
    end_time: Annotated[datetime | None, Query(description="操作时间上限（含）。")] = None,
) -> Response:
    """加 BOM 让 Excel 直接按 UTF-8 打开中文不乱码。"""
    conditions = _audit_conditions(
        actor_name=actor_name,
        method=method,
        path=path,
        start_time=start_time,
        end_time=end_time,
    )
    total = int(await session.scalar(select(func.count(AuditLog.id)).where(*conditions)) or 0)
    if total > AUDIT_EXPORT_MAX_ROWS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"导出上限 {AUDIT_EXPORT_MAX_ROWS} 条，当前 {total} 条，"
                "请缩小时间范围或增加筛选条件"
            ),
        )
    rows = list(
        await session.scalars(
            select(AuditLog)
            .where(*conditions)
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        )
    )
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(AUDIT_EXPORT_HEADERS)
    for item in rows:
        writer.writerow(
            [
                item.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                item.actor_name,
                item.actor_role,
                item.method,
                item.path,
                item.status_code,
                item.duration_ms,
                item.client_ip or "",
            ]
        )
    filename = f"audit-logs-{utc_now().strftime('%Y%m%d-%H%M%S')}.csv"
    return Response(
        content="\ufeff" + buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


QA_EXPORT_MAX_ROWS = 50_000

QA_EXPORT_HEADERS = [
    "时间(UTC)",
    "公司",
    "知识库",
    "问题",
    "状态",
    "复核状态",
    "用户反馈",
    "回答来源",
    "命中片段数",
    "检索模式",
    "top_k",
    "聊天模型",
    "检索耗时(ms)",
    "生成耗时(ms)",
    "总耗时(ms)",
    "输入token",
    "输出token",
    "总token",
    "错误码",
    "错误信息",
]


def _qa_record_conditions(
    *,
    company_id: UUID | None,
    knowledge_base_id: UUID | None,
    status: str | None,
    review_status: str | None,
    faq_matched: bool | None,
    feedback: str | None,
    q: str | None,
    start_time: datetime | None,
    end_time: datetime | None,
) -> list[Any]:
    """问答记录列表与导出共用的筛选条件，保证两边口径一致。"""
    conditions: list[Any] = []
    if company_id is not None:
        conditions.append(QARecord.company_id == company_id)
    if knowledge_base_id is not None:
        conditions.append(QARecord.knowledge_base_id == knowledge_base_id)
    if status is not None:
        conditions.append(QARecord.status == status)
    if review_status is not None:
        conditions.append(QARecord.review_status == review_status)
    if faq_matched is not None:
        conditions.append(QARecord.faq_matched.is_(faq_matched))
    if feedback is not None:
        conditions.append(QARecord.feedback == feedback)
    if q and q.strip():
        keyword = f"%{q.strip().lower()}%"
        conditions.append(func.lower(QARecord.query).like(keyword))
    if start_time is not None:
        conditions.append(QARecord.created_at >= start_time)
    if end_time is not None:
        conditions.append(QARecord.created_at <= end_time)
    return conditions


def _qa_export_row(
    record: QARecord, company_name: str | None, knowledge_base_name: str | None
) -> list[Any]:
    """把一条问答记录摊平成 CSV 行；未记录字段留空，导出不含答案正文以控制体积。"""

    def cell(value: Any) -> Any:
        return "" if value is None else value

    return [
        cell(record.created_at.strftime("%Y-%m-%d %H:%M:%S") if record.created_at else None),
        cell(company_name),
        cell(knowledge_base_name),
        cell(record.query),
        cell(record.status),
        cell(record.review_status),
        cell(record.feedback),
        "FAQ 直出" if record.faq_matched else "模型生成",
        cell(record.result_count),
        cell(record.retrieval_mode),
        cell(record.top_k),
        cell(record.chat_model_name),
        cell(record.retrieval_duration_ms),
        cell(record.generation_duration_ms),
        cell(record.total_duration_ms),
        cell(record.prompt_tokens),
        cell(record.completion_tokens),
        cell(record.total_tokens),
        cell(record.error_code),
        cell(record.error_message),
    ]


@router.get(
    "/qa-records",
    response_model=QARecordAdminListResponse,
    summary="管理员查询问答记录",
    description=(
        "跨公司与知识库分页查询在线问答记录；可按状态、问题关键词和创建时间范围过滤，"
        "供管理端排查回答质量与检索效果。"
    ),
)
async def list_admin_qa_records(
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
    company_id: Annotated[UUID | None, Query(description="按公司过滤；不传返回全部公司。")] = None,
    knowledge_base_id: Annotated[
        UUID | None, Query(description="按知识库过滤；不传返回全部知识库。")
    ] = None,
    status: Annotated[
        Literal["running", "success", "no_results", "failed"] | None,
        Query(description="按问答状态过滤；不传返回全部状态。"),
    ] = None,
    review_status: Annotated[
        Literal["none", "pending", "resolved"] | None,
        Query(description="按复核状态过滤；pending 即待补资料列表。"),
    ] = None,
    faq_matched: Annotated[
        bool | None, Query(description="按是否 FAQ 直接命中过滤；不传返回全部。")
    ] = None,
    feedback: Annotated[
        Literal["up", "down"] | None,
        Query(description="按用户反馈过滤；down 即用户点踩。"),
    ] = None,
    q: Annotated[str | None, Query(description="对问题文本做不区分大小写的模糊匹配。")] = None,
    start_time: Annotated[datetime | None, Query(description="创建时间下限（含）。")] = None,
    end_time: Annotated[datetime | None, Query(description="创建时间上限（含）。")] = None,
    page: Annotated[int, Query(ge=1, description="页码，从 1 开始。")] = 1,
    page_size: Annotated[int, Query(ge=1, le=100, description="每页数量，最多 100 条。")] = 20,
) -> QARecordAdminListResponse:
    """管理员视角的问答记录列表；不校验公司与知识库的归属关系。"""
    conditions = _qa_record_conditions(
        company_id=company_id,
        knowledge_base_id=knowledge_base_id,
        status=status,
        review_status=review_status,
        faq_matched=faq_matched,
        feedback=feedback,
        q=q,
        start_time=start_time,
        end_time=end_time,
    )
    total = int(await session.scalar(select(func.count(QARecord.id)).where(*conditions)) or 0)
    rows = list(
        await session.execute(
            select(QARecord, Company.name, KnowledgeBase.name)
            .join(Company, Company.id == QARecord.company_id, isouter=True)
            .join(KnowledgeBase, KnowledgeBase.id == QARecord.knowledge_base_id)
            .where(*conditions)
            .order_by(QARecord.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    return QARecordAdminListResponse(
        page=page,
        page_size=page_size,
        total=total,
        items=[
            QARecordAdminRead(
                **QARecordRead.model_validate(record).model_dump(),
                company_name=company_name,
                knowledge_base_name=knowledge_base_name,
            )
            for record, company_name, knowledge_base_name in rows
        ],
    )


@router.get(
    "/qa-records/export",
    summary="导出问答记录 CSV",
    description=(
        "按与列表相同的筛选条件导出问答记录，字段不含答案正文；"
        f"单次最多 {QA_EXPORT_MAX_ROWS} 条，超出需缩小时间范围。"
    ),
    response_class=Response,
)
async def export_admin_qa_records(
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
    company_id: Annotated[UUID | None, Query(description="按公司过滤。")] = None,
    knowledge_base_id: Annotated[UUID | None, Query(description="按知识库过滤。")] = None,
    status: Annotated[
        Literal["running", "success", "no_results", "failed"] | None,
        Query(description="按问答状态过滤。"),
    ] = None,
    review_status: Annotated[
        Literal["none", "pending", "resolved"] | None,
        Query(description="按复核状态过滤。"),
    ] = None,
    faq_matched: Annotated[bool | None, Query(description="按是否 FAQ 直接命中过滤。")] = None,
    feedback: Annotated[Literal["up", "down"] | None, Query(description="按用户反馈过滤。")] = None,
    q: Annotated[str | None, Query(description="对问题文本做模糊匹配。")] = None,
    start_time: Annotated[datetime | None, Query(description="创建时间下限（含）。")] = None,
    end_time: Annotated[datetime | None, Query(description="创建时间上限（含）。")] = None,
) -> Response:
    """导出当前筛选结果；加 BOM 让 Excel 直接按 UTF-8 打开中文不乱码。"""
    conditions = _qa_record_conditions(
        company_id=company_id,
        knowledge_base_id=knowledge_base_id,
        status=status,
        review_status=review_status,
        faq_matched=faq_matched,
        feedback=feedback,
        q=q,
        start_time=start_time,
        end_time=end_time,
    )
    total = int(await session.scalar(select(func.count(QARecord.id)).where(*conditions)) or 0)
    if total > QA_EXPORT_MAX_ROWS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"导出上限 {QA_EXPORT_MAX_ROWS} 条，当前 {total} 条，请缩小时间范围或增加筛选条件"
            ),
        )
    rows = list(
        await session.execute(
            select(QARecord, Company.name, KnowledgeBase.name)
            .join(Company, Company.id == QARecord.company_id, isouter=True)
            .join(KnowledgeBase, KnowledgeBase.id == QARecord.knowledge_base_id)
            .where(*conditions)
            .order_by(QARecord.created_at.desc())
        )
    )
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(QA_EXPORT_HEADERS)
    for record, company_name, knowledge_base_name in rows:
        writer.writerow(_qa_export_row(record, company_name, knowledge_base_name))
    filename = f"qa-records-{utc_now().strftime('%Y%m%d-%H%M%S')}.csv"
    return Response(
        content="\ufeff" + buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get(
    "/qa-records/{qa_record_id}",
    response_model=QARecordDetail,
    summary="管理员查询问答记录详情",
    description="返回单次问答的答案、来源快照和检索/生成阶段日志。",
)
async def get_admin_qa_record(
    qa_record_id: Annotated[UUID, Path(description="问答记录 ID。")],
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> QARecordDetail:
    """管理员视角的问答详情；不做公司范围校验。"""
    record = await session.scalar(select(QARecord).where(QARecord.id == qa_record_id))
    if record is None:
        raise HTTPException(status_code=404, detail="问答记录不存在")
    sources = list(
        await session.scalars(
            select(QASource).where(QASource.qa_record_id == qa_record_id).order_by(QASource.rank)
        )
    )
    events = list(
        await session.scalars(
            select(QAEvent).where(QAEvent.qa_record_id == qa_record_id).order_by(QAEvent.created_at)
        )
    )
    # 快照中的 [[KB_ASSET_KEY:...]] 占位符在输出时替换成图片 URL，正文不落库部署域名。
    assets_by_id = await _load_snapshot_assets(session, sources)
    source_schemas: list[QASourceRead] = []
    for source in sources:
        read = QASourceRead.model_validate(source)
        assets = [
            assets_by_id[asset_id]
            for asset_id in _source_asset_ids(source)
            if asset_id in assets_by_id
        ]
        if assets:
            read.content_snapshot = render_images(read.content_snapshot, assets)
            if read.context_snapshot:
                read.context_snapshot = render_images(read.context_snapshot, assets)
        source_schemas.append(read)
    base = QARecordRead.model_validate(record).model_dump()
    return QARecordDetail(
        **base,
        sources=source_schemas,
        events=[QAEventRead.model_validate(event) for event in events],
    )


def _source_asset_ids(source: QASource | ProjectQaSource) -> list[UUID]:
    """解析快照资产 ID 列表，忽略历史数据中的异常值。"""
    ids: list[UUID] = []
    for raw in source.asset_ids or []:
        try:
            ids.append(UUID(str(raw)))
        except (ValueError, TypeError, AttributeError):
            continue
    return ids


async def _load_snapshot_assets(
    session: AsyncSession, sources: list[QASource] | list[ProjectQaSource]
) -> dict[UUID, DocumentAsset]:
    """按快照 asset_ids 批量加载资产，用于占位符替换。"""
    ids = {asset_id for source in sources for asset_id in _source_asset_ids(source)}
    if not ids:
        return {}
    rows = await session.scalars(select(DocumentAsset).where(DocumentAsset.id.in_(ids)))
    return {asset.id: asset for asset in rows}


def _project_trace_admin_read(
    trace: ProjectQaTrace,
    *,
    project_key: str,
    project_name: str,
    company_name: str | None,
    knowledge_base_name: str,
) -> ProjectTraceAdminRead:
    read = ProjectTraceAdminRead(
        **ProjectTraceRead.model_validate(trace).model_dump(),
        project_key=project_key,
        project_name=project_name,
        company_name=company_name,
        knowledge_base_name=knowledge_base_name,
    )
    if trace.answer:
        read.answer_preview = trace.answer[:280]
    return read


@router.get(
    "/projects",
    response_model=list[ProjectRead],
    summary="列出接入项目",
    description="返回已登记的项目；项目记录页据此提供项目和保留策略筛选。",
)
async def list_admin_projects(
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> list[ProjectRead]:
    projects = await session.scalars(select(AppProject).order_by(AppProject.created_at))
    return [ProjectRead.model_validate(project) for project in projects]


@router.post(
    "/projects",
    response_model=ProjectRead,
    status_code=status.HTTP_201_CREATED,
    summary="登记接入项目",
    description=(
        "登记一个新的外部项目。只有 project_key 与调用方 X-Project-Key 一致，"
        "该项目的新请求才会被记录并出现在项目记录页。"
    ),
)
async def create_admin_project(
    payload: ProjectCreate,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> ProjectRead:
    project = AppProject(
        project_key=payload.project_key,
        display_name=payload.display_name,
        description=payload.description,
        retention_days=payload.retention_days,
    )
    session.add(project)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="项目标识已登记") from exc
    await session.refresh(project)
    return ProjectRead.model_validate(project)


@router.get(
    "/project-traces",
    response_model=ProjectTraceAdminListResponse,
    summary="管理员查询项目轨迹",
    description=(
        "跨项目与公司分页查询外部项目的问答轨迹；可按项目、公司、知识库、状态、"
        "回答路径、问题关键词和创建时间范围过滤。"
    ),
)
async def list_admin_project_traces(
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
    project_id: Annotated[UUID | None, Query(description="按接入项目过滤。")] = None,
    company_id: Annotated[UUID | None, Query(description="按公司过滤。")] = None,
    knowledge_base_id: Annotated[UUID | None, Query(description="按知识库过滤。")] = None,
    status_filter: Annotated[
        Literal["running", "success", "no_results", "failed", "clarification"] | None,
        Query(alias="status", description="按轨迹状态过滤。"),
    ] = None,
    answer_mode: Annotated[str | None, Query(max_length=40, description="按回答路径过滤。")] = None,
    feedback: Annotated[
        Literal["up", "down"] | None,
        Query(description="按用户反馈过滤；down 即用户点踩。"),
    ] = None,
    review_status: Annotated[
        Literal["none", "pending", "resolved"] | None,
        Query(description="按复核状态过滤；pending 即待补资料。"),
    ] = None,
    q: Annotated[str | None, Query(description="对问题文本做不区分大小写的模糊匹配。")] = None,
    start_time: Annotated[datetime | None, Query(description="创建时间下限（含）。")] = None,
    end_time: Annotated[datetime | None, Query(description="创建时间上限（含）。")] = None,
    page: Annotated[int, Query(ge=1, description="页码，从 1 开始。")] = 1,
    page_size: Annotated[int, Query(ge=1, le=100, description="每页数量，最多 100 条。")] = 20,
) -> ProjectTraceAdminListResponse:
    """管理员视角的项目轨迹列表；不校验公司与知识库的归属关系。"""
    conditions = []
    if project_id is not None:
        conditions.append(ProjectQaTrace.project_id == project_id)
    if company_id is not None:
        conditions.append(ProjectQaTrace.company_id == company_id)
    if knowledge_base_id is not None:
        conditions.append(ProjectQaTrace.knowledge_base_id == knowledge_base_id)
    if status_filter is not None:
        conditions.append(ProjectQaTrace.status == status_filter)
    if answer_mode:
        conditions.append(ProjectQaTrace.answer_mode == answer_mode.strip())
    if feedback is not None:
        conditions.append(ProjectQaTrace.feedback == feedback)
    if review_status is not None:
        conditions.append(ProjectQaTrace.review_status == review_status)
    if q and q.strip():
        keyword = f"%{q.strip().lower()}%"
        conditions.append(func.lower(ProjectQaTrace.query).like(keyword))
    if start_time is not None:
        conditions.append(ProjectQaTrace.created_at >= start_time)
    if end_time is not None:
        conditions.append(ProjectQaTrace.created_at <= end_time)
    total = int(await session.scalar(select(func.count(ProjectQaTrace.id)).where(*conditions)) or 0)
    rows = list(
        await session.execute(
            select(
                ProjectQaTrace,
                AppProject.project_key,
                AppProject.display_name,
                Company.name,
                KnowledgeBase.name,
            )
            .join(AppProject, AppProject.id == ProjectQaTrace.project_id)
            .join(Company, Company.id == ProjectQaTrace.company_id, isouter=True)
            .join(KnowledgeBase, KnowledgeBase.id == ProjectQaTrace.knowledge_base_id)
            .where(*conditions)
            .order_by(ProjectQaTrace.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    return ProjectTraceAdminListResponse(
        page=page,
        page_size=page_size,
        total=total,
        items=[
            _project_trace_admin_read(
                trace,
                project_key=project_key,
                project_name=project_name,
                company_name=company_name,
                knowledge_base_name=knowledge_base_name,
            )
            for trace, project_key, project_name, company_name, knowledge_base_name in rows
        ],
    )


@router.get(
    "/project-traces/{trace_id}",
    response_model=ProjectTraceDetail,
    summary="管理员查询项目轨迹详情",
    description="返回项目最终回答、每次检索尝试和全部召回片段快照。",
)
async def get_admin_project_trace(
    trace_id: Annotated[UUID, Path(description="项目轨迹 ID。")],
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> ProjectTraceDetail:
    """管理员视角的项目轨迹详情；不做公司范围校验。"""
    row = (
        await session.execute(
            select(
                ProjectQaTrace,
                AppProject.project_key,
                AppProject.display_name,
                Company.name,
                KnowledgeBase.name,
            )
            .join(AppProject, AppProject.id == ProjectQaTrace.project_id)
            .join(Company, Company.id == ProjectQaTrace.company_id, isouter=True)
            .join(KnowledgeBase, KnowledgeBase.id == ProjectQaTrace.knowledge_base_id)
            .where(ProjectQaTrace.id == trace_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="项目轨迹不存在")
    trace, project_key, project_name, company_name, knowledge_base_name = row
    sources = list(
        await session.scalars(
            select(ProjectQaSource)
            .where(ProjectQaSource.trace_id == trace_id)
            .order_by(ProjectQaSource.attempt_index, ProjectQaSource.rank)
        )
    )
    assets_by_id = await _load_snapshot_assets(session, sources)
    source_schemas: list[ProjectTraceSourceRead] = []
    for source in sources:
        read = ProjectTraceSourceRead.model_validate(source)
        assets = [
            assets_by_id[asset_id]
            for asset_id in _source_asset_ids(source)
            if asset_id in assets_by_id
        ]
        if assets:
            read.content_snapshot = render_images(read.content_snapshot, assets)
            if read.context_snapshot:
                read.context_snapshot = render_images(read.context_snapshot, assets)
        source_schemas.append(read)
    base = _project_trace_admin_read(
        trace,
        project_key=project_key,
        project_name=project_name,
        company_name=company_name,
        knowledge_base_name=knowledge_base_name,
    )
    return ProjectTraceDetail(
        **base.model_dump(),
        answer=trace.answer,
        sources=source_schemas,
    )


@router.patch(
    "/qa-records/{qa_record_id}/review",
    response_model=QARecordAdminRead,
    summary="修改问答记录复核状态",
    description="管理员标记问答已补充资料（resolved）或重新打开复核（none）。",
)
async def review_admin_qa_record(
    qa_record_id: Annotated[UUID, Path(description="问答记录 ID。")],
    payload: QARecordReviewPatch,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> QARecordAdminRead:
    """更新复核状态并返回带公司与知识库名称的最新记录。"""
    row = (
        await session.execute(
            select(QARecord, Company.name, KnowledgeBase.name)
            .join(Company, Company.id == QARecord.company_id, isouter=True)
            .join(KnowledgeBase, KnowledgeBase.id == QARecord.knowledge_base_id)
            .where(QARecord.id == qa_record_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="问答记录不存在")
    record, company_name, knowledge_base_name = row
    record.review_status = payload.review_status
    await session.commit()
    return QARecordAdminRead(
        **QARecordRead.model_validate(record).model_dump(),
        company_name=company_name,
        knowledge_base_name=knowledge_base_name,
    )


@router.patch(
    "/project-traces/{trace_id}/review",
    response_model=ProjectTraceAdminRead,
    summary="修改项目轨迹复核状态",
    description="管理员标记项目轨迹已补充资料（resolved）或重新打开复核（none）。",
)
async def review_admin_project_trace(
    trace_id: Annotated[UUID, Path(description="项目轨迹 ID。")],
    payload: QARecordReviewPatch,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> ProjectTraceAdminRead:
    """更新项目轨迹复核状态并返回带项目名称的最新记录。"""
    row = (
        await session.execute(
            select(
                ProjectQaTrace,
                AppProject.project_key,
                AppProject.display_name,
                Company.name,
                KnowledgeBase.name,
            )
            .join(AppProject, AppProject.id == ProjectQaTrace.project_id)
            .join(Company, Company.id == ProjectQaTrace.company_id, isouter=True)
            .join(KnowledgeBase, KnowledgeBase.id == ProjectQaTrace.knowledge_base_id)
            .where(ProjectQaTrace.id == trace_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="项目轨迹不存在")
    trace, project_key, project_name, company_name, knowledge_base_name = row
    trace.review_status = payload.review_status
    await session.commit()
    return _project_trace_admin_read(
        trace,
        project_key=project_key,
        project_name=project_name,
        company_name=company_name,
        knowledge_base_name=knowledge_base_name,
    )


def _percentile(sorted_values: list[int], ratio: float) -> int | None:
    """最近秩法取分位数；调用方需保证入参已升序。"""
    if not sorted_values:
        return None
    index = max(math.ceil(ratio * len(sorted_values)) - 1, 0)
    return sorted_values[index]


def _rate_percent(part: int, total: int) -> float:
    """占比转百分比，保留两位小数；分母为 0 时返回 0。"""
    return round(part * 100 / total, 2) if total else 0.0


MILLION_TOKENS = Decimal(1_000_000)


def _tokens_cost(
    prompt_tokens: int, completion_tokens: int, price: ModelPrice | None
) -> tuple[Decimal, Decimal]:
    """按模型单价计算输入/输出 token 费用；未配置单价时按 0 计。"""
    if price is None:
        return Decimal(0), Decimal(0)
    return (
        Decimal(prompt_tokens) * price.input_price_per_million / MILLION_TOKENS,
        Decimal(completion_tokens) * price.output_price_per_million / MILLION_TOKENS,
    )


async def _build_cost_block(
    session: AsyncSession, *, window_start: datetime, window_end: datetime
) -> AdminCostRead:
    """按模型单价核算问答生成成本；FAQ 直出与未记录 token 的记录天然不参与。"""
    prices = list(await session.scalars(select(ModelPrice)))
    price_by_model = {item.model_name: item for item in prices}

    def token_query(*conditions) -> Any:
        statement = select(
            QARecord.chat_model_name,
            QARecord.chat_provider,
            func.coalesce(func.sum(QARecord.prompt_tokens), 0),
            func.coalesce(func.sum(QARecord.completion_tokens), 0),
            func.count(QARecord.total_tokens),
        ).group_by(QARecord.chat_model_name, QARecord.chat_provider)
        if conditions:
            statement = statement.where(*conditions)
        return statement

    window_rows = list(
        await session.execute(
            token_query(QARecord.created_at >= window_start, QARecord.created_at < window_end)
        )
    )
    total_rows = list(await session.execute(token_query()))

    def total_cost_of(rows: list) -> Decimal:
        total = Decimal(0)
        for model_name, _provider, prompt_tokens, completion_tokens, _count in rows:
            input_cost, output_cost = _tokens_cost(
                int(prompt_tokens), int(completion_tokens), price_by_model.get(model_name or "")
            )
            total += input_cost + output_cost
        return total

    model_items: list[AdminModelCostRead] = []
    priced_tokens = 0
    unpriced_tokens = 0
    unpriced_models: list[str] = []
    for model_name, provider, prompt_tokens, completion_tokens, count in window_rows:
        prompt_value, completion_value = int(prompt_tokens), int(completion_tokens)
        if prompt_value + completion_value == 0:
            continue
        key = model_name or ""
        price = price_by_model.get(key)
        input_cost, output_cost = _tokens_cost(prompt_value, completion_value, price)
        tokens = prompt_value + completion_value
        if price is None:
            unpriced_tokens += tokens
            unpriced_models.append(model_name or "未记录模型")
        else:
            priced_tokens += tokens
        model_items.append(
            AdminModelCostRead(
                model_name=key,
                provider=provider,
                qa_count=int(count),
                prompt_tokens=prompt_value,
                completion_tokens=completion_value,
                input_cost=float(round(input_cost, 6)),
                output_cost=float(round(output_cost, 6)),
                total_cost=float(round(input_cost + output_cost, 6)),
                priced=price is not None,
            )
        )
    model_items.sort(key=lambda item: (-item.total_cost, -item.prompt_tokens, item.model_name))
    return AdminCostRead(
        currency="CNY",
        recent_cost=float(round(total_cost_of(window_rows), 6)),
        total_cost=float(round(total_cost_of(total_rows), 6)),
        recent_priced_tokens=priced_tokens,
        recent_unpriced_tokens=unpriced_tokens,
        unpriced_models=sorted(set(unpriced_models)),
        models=model_items,
    )


def _latency_breakdown(
    groups: dict[str, dict[str, Any]],
) -> list[AdminLatencyBreakdownRead]:
    """把按组累计的耗时明细转成响应列表，按条数降序。"""
    items = []
    for key, group in groups.items():
        durations = sorted(group["durations"])
        items.append(
            AdminLatencyBreakdownRead(
                key=key,
                label=group["label"],
                qa_total=group["total"],
                qa_failed=group["failed"],
                failure_rate=_rate_percent(group["failed"], group["total"]),
                avg_total_duration_ms=(
                    round(sum(durations) / len(durations)) if durations else None
                ),
                p90_total_duration_ms=_percentile(durations, 0.9),
            )
        )
    items.sort(key=lambda item: (-item.qa_total, item.key))
    return items


@router.get(
    "/statistics",
    response_model=AdminStatisticsRead,
    summary="统计仪表盘聚合数据",
    description=(
        "返回公司、知识库、文档与问答的汇总数字，以及统计窗口内的每日问答趋势、"
        "RAG 问答整体耗时分布（P50/P90）、失败率与按模型/知识库的耗时分布；"
        "窗口默认最近 14 天，可用 start/end 指定（UTC 日期、含首尾，最长 366 天）；"
        "耗时与分组统计不含 FAQ 直出。"
    ),
)
async def get_admin_statistics(
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
    start: Annotated[
        date | None,
        Query(description="统计窗口起始日期（UTC，含）；缺省为结束日期前 13 天。"),
    ] = None,
    end: Annotated[
        date | None,
        Query(description="统计窗口结束日期（UTC，含）；缺省为今天（UTC）。"),
    ] = None,
) -> AdminStatisticsRead:
    """聚合统计仪表盘所需数据；趋势按 UTC 日期对齐，缺失日期补零。"""
    end_date = end or utc_now().date()
    start_date = start or end_date - timedelta(days=13)
    if start_date > end_date:
        raise HTTPException(status_code=400, detail="统计窗口起始日期不能晚于结束日期")
    window_days = (end_date - start_date).days + 1
    if window_days > 366:
        raise HTTPException(status_code=400, detail="统计窗口不能超过 366 天")
    first_day = datetime.combine(start_date, day_start.min, tzinfo=UTC)
    # 右边界取结束日期次日零点，窗口含结束当天全部记录。
    window_end = datetime.combine(end_date + timedelta(days=1), day_start.min, tzinfo=UTC)
    companies_total = await session.scalar(
        select(func.count()).select_from(Company).where(Company.status == "active")
    )
    knowledge_bases_total = await session.scalar(
        select(func.count()).select_from(KnowledgeBase).where(KnowledgeBase.status == "active")
    )
    document_rows = list(
        await session.execute(select(Document.status, func.count()).group_by(Document.status))
    )
    documents_by_status = {status: count for status, count in document_rows}
    documents_total = sum(documents_by_status.values())

    qa_rows = list(
        await session.execute(select(QARecord.status, func.count()).group_by(QARecord.status))
    )
    qa_by_status = {status: count for status, count in qa_rows}
    qa_total = sum(qa_by_status.values())
    review_pending = await session.scalar(
        select(func.count()).select_from(QARecord).where(QARecord.review_status == "pending")
    )
    # token 字段为空的记录（FAQ 直出、未命中、改造前历史数据）不计入用量汇总。
    (
        prompt_tokens_total,
        completion_tokens_total,
        total_tokens_total,
        measured_qa_count,
    ) = (
        await session.execute(
            select(
                func.coalesce(func.sum(QARecord.prompt_tokens), 0),
                func.coalesce(func.sum(QARecord.completion_tokens), 0),
                func.coalesce(func.sum(QARecord.total_tokens), 0),
                func.count(QARecord.total_tokens),
            )
        )
    ).one()

    cost = await _build_cost_block(session, window_start=first_day, window_end=window_end)

    # 窗口内按 UTC 日期聚合；MySQL/SQLite 都支持 DATE() 分组。
    trend_rows = list(
        await session.execute(
            select(
                func.date(QARecord.created_at),
                func.count(),
                func.sum(case((QARecord.status == "success", 1), else_=0)),
                func.avg(QARecord.total_duration_ms),
                func.sum(QARecord.prompt_tokens),
                func.sum(QARecord.completion_tokens),
                func.sum(QARecord.total_tokens),
            )
            .where(QARecord.created_at >= first_day, QARecord.created_at < window_end)
            .group_by(func.date(QARecord.created_at))
        )
    )
    trend_map = {
        str(day): {
            "qa_total": total,
            "qa_success": success or 0,
            "avg_total_duration_ms": (round(avg_ms) if avg_ms is not None else None),
            "prompt_tokens": prompt_tokens or 0,
            "completion_tokens": completion_tokens or 0,
            "total_tokens": tokens or 0,
        }
        for (
            day,
            total,
            success,
            avg_ms,
            prompt_tokens,
            completion_tokens,
            tokens,
        ) in trend_rows
    }
    # 窗口内逐条明细：整体 P50/P90、失败率与按模型/知识库分组。
    # FAQ 直出没调聊天模型，混进来会稀释分位数，耗时与分组统计剔除、质量计数保留。
    detail_rows = list(
        await session.execute(
            select(
                QARecord.knowledge_base_id,
                QARecord.chat_model_name,
                QARecord.status,
                QARecord.faq_matched,
                QARecord.retrieval_duration_ms,
                QARecord.generation_duration_ms,
                QARecord.total_duration_ms,
                KnowledgeBase.name.label("knowledge_base_name"),
            )
            .outerjoin(KnowledgeBase, KnowledgeBase.id == QARecord.knowledge_base_id)
            .where(QARecord.created_at >= first_day, QARecord.created_at < window_end)
        )
    )
    recent_status = Counter(row.status for row in detail_rows)
    rag_rows = [row for row in detail_rows if not row.faq_matched]
    retrieval_durations: list[int] = []
    generation_durations: list[int] = []
    total_durations: list[int] = []
    model_groups: dict[str, dict[str, Any]] = {}
    kb_groups: dict[str, dict[str, Any]] = {}
    for row in rag_rows:
        if row.retrieval_duration_ms is not None:
            retrieval_durations.append(row.retrieval_duration_ms)
        if row.generation_duration_ms is not None:
            generation_durations.append(row.generation_duration_ms)
        if row.total_duration_ms is not None:
            total_durations.append(row.total_duration_ms)
        # 未命中这类没调聊天模型的记录归入「未记录模型」分组。
        model_group = model_groups.setdefault(
            row.chat_model_name or "",
            {
                "label": row.chat_model_name or "未记录模型",
                "total": 0,
                "failed": 0,
                "durations": [],
            },
        )
        kb_group = kb_groups.setdefault(
            str(row.knowledge_base_id),
            {
                "label": row.knowledge_base_name or "已删除知识库",
                "total": 0,
                "failed": 0,
                "durations": [],
            },
        )
        for group in (model_group, kb_group):
            group["total"] += 1
            if row.status == "failed":
                group["failed"] += 1
            if row.total_duration_ms is not None:
                group["durations"].append(row.total_duration_ms)
    retrieval_durations.sort()
    generation_durations.sort()
    total_durations.sort()
    recent_total = len(detail_rows)
    recent_failed = recent_status.get("failed", 0)
    recent = AdminRecentStatisticsRead(
        qa_total=recent_total,
        qa_success=recent_status.get("success", 0),
        qa_no_results=recent_status.get("no_results", 0),
        qa_failed=recent_failed,
        failure_rate=_rate_percent(recent_failed, recent_total),
        no_result_rate=_rate_percent(recent_status.get("no_results", 0), recent_total),
        latency=AdminLatencyStatsRead(
            sample_count=len(total_durations),
            retrieval_p50_ms=_percentile(retrieval_durations, 0.5),
            retrieval_p90_ms=_percentile(retrieval_durations, 0.9),
            generation_p50_ms=_percentile(generation_durations, 0.5),
            generation_p90_ms=_percentile(generation_durations, 0.9),
            total_p50_ms=_percentile(total_durations, 0.5),
            total_p90_ms=_percentile(total_durations, 0.9),
            total_max_ms=total_durations[-1] if total_durations else None,
        ),
        models=_latency_breakdown(model_groups),
        knowledge_bases=_latency_breakdown(kb_groups),
    )

    daily_trend = []
    for offset in range(window_days):
        day = (first_day + timedelta(days=offset)).date().isoformat()
        item = trend_map.get(day)
        daily_trend.append(
            AdminDailyTrendRead(
                date=day,
                qa_total=item["qa_total"] if item else 0,
                qa_success=item["qa_success"] if item else 0,
                avg_total_duration_ms=(item["avg_total_duration_ms"] if item else None),
                prompt_tokens=item["prompt_tokens"] if item else 0,
                completion_tokens=item["completion_tokens"] if item else 0,
                total_tokens=item["total_tokens"] if item else 0,
            )
        )
    return AdminStatisticsRead(
        companies_total=companies_total or 0,
        knowledge_bases_total=knowledge_bases_total or 0,
        documents_total=documents_total,
        documents_by_status=documents_by_status,
        qa_total=qa_total,
        qa_by_status=qa_by_status,
        review_pending=review_pending or 0,
        token_usage=AdminTokenUsageRead(
            prompt_tokens=prompt_tokens_total,
            completion_tokens=completion_tokens_total,
            total_tokens=total_tokens_total,
            recent_prompt_tokens=sum(item["prompt_tokens"] for item in trend_map.values()),
            recent_completion_tokens=sum(item["completion_tokens"] for item in trend_map.values()),
            recent_total_tokens=sum(item["total_tokens"] for item in trend_map.values()),
            measured_qa_count=measured_qa_count or 0,
        ),
        cost=cost,
        range=AdminStatisticsRangeRead(
            start=start_date.isoformat(),
            end=end_date.isoformat(),
            days=window_days,
        ),
        recent=recent,
        daily_trend=daily_trend,
    )


TASK_LIST_LIMIT = 300

INGEST_STAGE_LABELS = {
    "load": "读取文件",
    "parse": "解析内容",
    "chunk": "切分文本",
    "embed": "向量化",
    "index": "写入索引",
    "complete": "整理条目",
}

TASK_STATUS_PRIORITY = {"running": 0, "queued": 1, "failed": 2, "cancelled": 2, "success": 3}


def _task_duration_ms(started_at: datetime | None, finished_at: datetime | None) -> int | None:
    """任务耗时；未结束的任务按当前时间估算，未开始则为空。"""
    if started_at is None:
        return None
    return max(0, int(((finished_at or utc_now()) - started_at).total_seconds() * 1000))


def _task_status_matches(task_status: str, wanted: str) -> bool:
    """status=open 含排队/进行/失败（含取消），failed 也收编已取消。"""
    if wanted == "all":
        return True
    if wanted == "open":
        return task_status in {"queued", "running", "failed", "cancelled"}
    if wanted == "failed":
        return task_status in {"failed", "cancelled"}
    if wanted == "done":
        return task_status == "success"
    return task_status == wanted


@router.get(
    "/tasks",
    response_model=AdminTaskListRead,
    summary="任务中心聚合列表",
    description=(
        "把离线入库、FAQ 自动生成与检索评测三类后台任务归一化后统一展示："
        "默认返回排队、进行中、失败待处理的任务，另含 UTC 当日已完成记录；"
        "同一文档或同一评测集只保留最新一条，单次最多返回 300 条。"
    ),
)
async def list_admin_tasks(
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
    kind: Annotated[
        Literal["ingest", "faq", "evaluation"] | None,
        Query(description="按任务类型过滤；不传返回全部类型。"),
    ] = None,
    status: Annotated[
        Literal["open", "queued", "running", "failed", "done", "all"],
        Query(
            description=(
                "open 为未完成（排队/进行/失败，默认），done 为当日完成，"
                "all 为全部；也可以传单个状态。"
            )
        ),
    ] = "open",
    company_id: Annotated[UUID | None, Query(description="按公司过滤。")] = None,
    knowledge_base_id: Annotated[UUID | None, Query(description="按知识库过滤。")] = None,
) -> AdminTaskListRead:
    """三类任务同屏展示；counts 不受 status 过滤影响，供顶部统计卡常显。"""
    today_start = datetime.combine(utc_now().date(), day_start.min, tzinfo=UTC)
    tasks: list[AdminTaskRead] = []

    if kind in (None, "ingest"):
        ingest_stmt = (
            select(
                IngestionJob,
                Document,
                DocumentVersion,
                KnowledgeBase.name,
                Company.name,
            )
            .join(Document, Document.id == IngestionJob.document_id)
            .join(DocumentVersion, DocumentVersion.id == IngestionJob.document_version_id)
            .join(KnowledgeBase, KnowledgeBase.id == Document.knowledge_base_id)
            .join(Company, Company.id == Document.company_id, isouter=True)
            # 只看最新版本的入库任务，历史失败不打扰当前页面。
            .where(
                DocumentVersion.version_number == Document.latest_version_number,
                or_(
                    IngestionJob.status.in_(("queued", "running", "failed", "cancelled")),
                    and_(
                        IngestionJob.status == "completed",
                        IngestionJob.finished_at >= today_start,
                    ),
                ),
            )
            .order_by(IngestionJob.created_at.desc())
        )
        if company_id is not None:
            ingest_stmt = ingest_stmt.where(Document.company_id == company_id)
        if knowledge_base_id is not None:
            ingest_stmt = ingest_stmt.where(Document.knowledge_base_id == knowledge_base_id)
        seen_documents: set[UUID] = set()
        for job, document, version, kb_name, company_name in await session.execute(ingest_stmt):
            if document.id in seen_documents:
                continue
            seen_documents.add(document.id)
            task_status = "success" if job.status == "completed" else job.status
            stage_label = INGEST_STAGE_LABELS.get(job.stage or "", job.stage)
            if task_status == "running":
                detail = f"v{version.version_number} · {stage_label or '处理中'}"
            elif task_status == "queued":
                detail = f"v{version.version_number} · 等待处理"
            elif task_status == "success":
                detail = f"v{version.version_number} · 入库完成"
            elif task_status == "cancelled":
                detail = f"v{version.version_number} · 已取消"
            else:
                detail = f"v{version.version_number} · 第 {job.attempts} 次尝试"
            error_message = None
            if task_status == "failed":
                error_message = (job.error_details or {}).get("message")
            tasks.append(
                AdminTaskRead(
                    kind="ingest",
                    task_id=job.id,
                    status=task_status,
                    title=document.filename,
                    detail=detail,
                    company_id=document.company_id,
                    company_name=company_name,
                    knowledge_base_id=document.knowledge_base_id,
                    knowledge_base_name=kb_name,
                    document_id=document.id,
                    attempts=job.attempts,
                    created_at=job.created_at,
                    started_at=job.started_at,
                    finished_at=job.finished_at,
                    duration_ms=_task_duration_ms(job.started_at, job.finished_at),
                    error_message=error_message,
                )
            )

    if kind in (None, "faq"):
        faq_stmt = (
            select(OutboxEvent, Document, KnowledgeBase.name, Company.name)
            .join(Document, Document.id == OutboxEvent.aggregate_id)
            .join(KnowledgeBase, KnowledgeBase.id == Document.knowledge_base_id)
            .join(Company, Company.id == Document.company_id, isouter=True)
            .where(
                OutboxEvent.event_type == "document.faq_generation_requested",
                or_(
                    OutboxEvent.status.in_(("pending", "processing", "failed")),
                    and_(
                        OutboxEvent.status == "published",
                        OutboxEvent.published_at >= today_start,
                    ),
                ),
            )
            .order_by(OutboxEvent.created_at.desc())
        )
        if company_id is not None:
            faq_stmt = faq_stmt.where(Document.company_id == company_id)
        if knowledge_base_id is not None:
            faq_stmt = faq_stmt.where(Document.knowledge_base_id == knowledge_base_id)
        faq_rows: list[Any] = []
        seen_faq_documents: set[UUID] = set()
        for event, document, kb_name, company_name in await session.execute(faq_stmt):
            if document.id in seen_faq_documents:
                continue
            seen_faq_documents.add(document.id)
            faq_rows.append((event, document, kb_name, company_name))
        # 失败事件只代表历史；若该版本已手动生成过自动 FAQ，则视为已处理不再提示。
        failed_versions: set[UUID] = set()
        for event, _, _, _ in faq_rows:
            if event.status != "failed":
                continue
            raw_version = (event.payload or {}).get("document_version_id")
            try:
                failed_versions.add(UUID(str(raw_version)))
            except (TypeError, ValueError):
                continue
        generated_versions: set[UUID] = set()
        if failed_versions:
            generated_versions = set(
                await session.scalars(
                    select(FaqEntry.document_version_id).where(
                        FaqEntry.document_version_id.in_(failed_versions),
                        FaqEntry.source == "auto",
                    )
                )
            )
        faq_status_map = {
            "pending": "queued",
            "processing": "running",
            "published": "success",
            "failed": "failed",
        }
        for event, document, kb_name, company_name in faq_rows:
            raw_version = (event.payload or {}).get("document_version_id")
            try:
                version_id = UUID(str(raw_version)) if raw_version else None
            except ValueError:
                version_id = None
            if (
                event.status == "failed"
                and version_id is not None
                and version_id in generated_versions
            ):
                continue
            task_status = faq_status_map.get(event.status, "queued")
            if task_status == "running":
                detail = "正在生成 FAQ"
            elif task_status == "queued":
                detail = "等待生成 FAQ"
            elif task_status == "success":
                detail = "FAQ 生成完成"
            else:
                detail = f"FAQ 生成失败 · 第 {event.attempts} 次尝试"
            tasks.append(
                AdminTaskRead(
                    kind="faq",
                    task_id=event.id,
                    status=task_status,
                    title=document.filename,
                    detail=detail,
                    company_id=document.company_id,
                    company_name=company_name,
                    knowledge_base_id=document.knowledge_base_id,
                    knowledge_base_name=kb_name,
                    document_id=document.id,
                    attempts=event.attempts,
                    created_at=event.created_at,
                    started_at=(event.created_at if task_status == "running" else None),
                    finished_at=event.published_at,
                    duration_ms=_task_duration_ms(
                        event.created_at if task_status == "running" else None,
                        event.published_at,
                    ),
                    error_message=(
                        "自动生成失败，可在 FAQ 页手动重新生成" if task_status == "failed" else None
                    ),
                )
            )

    if kind in (None, "evaluation"):
        run_stmt = (
            select(EvaluationRun, EvaluationSet.name, KnowledgeBase.name, Company.name)
            .join(EvaluationSet, EvaluationSet.id == EvaluationRun.set_id)
            .join(KnowledgeBase, KnowledgeBase.id == EvaluationRun.knowledge_base_id)
            .join(Company, Company.id == EvaluationRun.company_id, isouter=True)
            .where(
                or_(
                    EvaluationRun.status.in_(("running", "failed")),
                    and_(
                        EvaluationRun.status == "success",
                        EvaluationRun.finished_at >= today_start,
                    ),
                )
            )
            .order_by(EvaluationRun.created_at.desc())
        )
        if company_id is not None:
            run_stmt = run_stmt.where(EvaluationRun.company_id == company_id)
        if knowledge_base_id is not None:
            run_stmt = run_stmt.where(EvaluationRun.knowledge_base_id == knowledge_base_id)
        seen_sets: set[UUID] = set()
        for run, set_name, kb_name, company_name in await session.execute(run_stmt):
            # 同一评测集只展示最近一次运行，旧失败会被新运行自然顶掉。
            if run.set_id in seen_sets:
                continue
            seen_sets.add(run.set_id)
            if run.status == "running":
                detail = f"进度 {run.completed_count}/{run.case_count} 条用例"
            else:
                detail = f"{run.case_count} 条用例"
            tasks.append(
                AdminTaskRead(
                    kind="evaluation",
                    task_id=run.id,
                    status=run.status,
                    title=set_name,
                    detail=detail,
                    company_id=run.company_id,
                    company_name=company_name,
                    knowledge_base_id=run.knowledge_base_id,
                    knowledge_base_name=kb_name,
                    document_id=None,
                    attempts=0,
                    progress_current=run.completed_count,
                    progress_total=run.case_count,
                    created_at=run.created_at,
                    started_at=run.started_at,
                    finished_at=run.finished_at,
                    duration_ms=_task_duration_ms(run.started_at, run.finished_at),
                    error_message=run.error_message if run.status == "failed" else None,
                )
            )

    counts = AdminTaskCountsRead(
        queued=sum(1 for item in tasks if item.status == "queued"),
        running=sum(1 for item in tasks if item.status == "running"),
        failed=sum(1 for item in tasks if item.status in {"failed", "cancelled"}),
        done_today=sum(1 for item in tasks if item.status == "success"),
    )
    filtered = [item for item in tasks if _task_status_matches(item.status, status)]
    filtered.sort(
        key=lambda item: (
            TASK_STATUS_PRIORITY[item.status],
            -item.created_at.timestamp(),
        )
    )
    return AdminTaskListRead(
        items=filtered[:TASK_LIST_LIMIT],
        counts=counts,
        truncated=len(filtered) > TASK_LIST_LIMIT,
        generated_at=utc_now(),
    )


@router.get(
    "/prompts",
    response_model=list[AdminPromptRead],
    summary="提示词列表",
    description="返回管理页可编辑的系统提示词及其当前生效文本与来源。",
)
async def list_admin_prompts(
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> list[AdminPromptRead]:
    """提示词文本与生成链路同源解析，管理页看到的就是线上生效内容。"""
    items = []
    for name, label in PROMPT_REGISTRY.items():
        content, source, updated_at = await resolve_prompt(session, name=name)
        items.append(
            AdminPromptRead(
                name=name, label=label, source=source, content=content, updated_at=updated_at
            )
        )
    return items


@router.put(
    "/prompts/{name}",
    response_model=AdminPromptRead,
    summary="保存提示词覆写",
    description="写入数据库覆写，即时对在线问答生成生效；重启服务后仍然保留。",
)
async def upsert_admin_prompt(
    name: Annotated[str, Path(description="提示词名称标识。")],
    payload: AdminPromptWrite,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> AdminPromptRead:
    """保存覆写内容；空正文视为无效，不允许清空提示词。"""
    if name not in PROMPT_REGISTRY:
        raise HTTPException(status_code=404, detail="提示词不存在")
    content = payload.content.strip()
    if not content:
        raise HTTPException(status_code=422, detail="提示词正文不能为空")
    override = await session.get(AdminPromptOverride, name)
    if override is None:
        override = AdminPromptOverride(name=name, content=content)
        session.add(override)
    else:
        override.content = content
    await session.commit()
    return AdminPromptRead(
        name=name,
        label=PROMPT_REGISTRY[name],
        source="override",
        content=content,
        updated_at=override.updated_at,
    )


@router.delete(
    "/prompts/{name}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除提示词覆写",
    description="移除数据库覆写，提示词回落到提示词文件或内置默认。",
)
async def delete_admin_prompt(
    name: Annotated[str, Path(description="提示词名称标识。")],
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> Response:
    """不存在覆写时幂等返回 204。"""
    if name not in PROMPT_REGISTRY:
        raise HTTPException(status_code=404, detail="提示词不存在")
    override = await session.get(AdminPromptOverride, name)
    if override is not None:
        await session.delete(override)
        await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# 全局搜索每类结果的条数上限；再大对管理页没有意义。
_GLOBAL_SEARCH_LIMIT_CAP = 10


def _search_pattern(keyword: str) -> str:
    """把用户输入转成安全的 LIKE 模式，避免 % 与 _ 被当成通配符扫全表。"""
    escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _search_excerpt(text: str | None, keyword: str, width: int = 90) -> str | None:
    """截取命中词附近的正文片段；未命中时返回开头，便于快速判断相关性。"""
    if not text:
        return None
    collapsed = " ".join(text.split())
    if not collapsed:
        return None
    position = collapsed.lower().find(keyword)
    if position < 0:
        return collapsed[:width] + ("…" if len(collapsed) > width else "")
    start = max(0, position - width // 3)
    end = min(len(collapsed), position + len(keyword) + width)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(collapsed) else ""
    return f"{prefix}{collapsed[start:end]}{suffix}"


def _search_ownership(company_name: str | None, knowledge_base_name: str | None) -> str | None:
    """把公司与知识库拼成归属说明；缺失的一侧自动省略。"""
    parts = [part for part in (company_name, knowledge_base_name) if part]
    return " · ".join(parts) if parts else None


@router.get(
    "/search",
    response_model=GlobalSearchRead,
    summary="全局搜索",
    description=(
        "跨知识库、文档、FAQ、问答记录、项目轨迹与产品目录做关键词模糊检索；"
        "每类返回命中总数与前若干条结果，供管理页全局搜索页使用，所有管理端角色可读。"
    ),
)
async def global_search(
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
    q: Annotated[
        str,
        Query(min_length=1, max_length=100, description="检索词；去空白后不能为空。"),
    ],
    limit: Annotated[
        int, Query(ge=1, le=_GLOBAL_SEARCH_LIMIT_CAP, description="每类最多条数。")
    ] = 5,
) -> GlobalSearchRead:
    """六类资源各查一次总数与样例，按固定顺序返回；检索词只做字面模糊匹配。"""
    started = time.perf_counter()
    keyword = q.strip().lower()
    if not keyword:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="检索词不能为空"
        )
    pattern = _search_pattern(keyword)
    like = {"escape": "\\"}
    groups: list[GlobalSearchGroup] = []

    # 知识库：名称与说明
    kb_filter = or_(
        func.lower(KnowledgeBase.name).like(pattern, **like),
        func.lower(func.coalesce(KnowledgeBase.description, "")).like(pattern, **like),
    )
    kb_total = int(await session.scalar(select(func.count(KnowledgeBase.id)).where(kb_filter)) or 0)
    kb_rows = list(
        await session.execute(
            select(KnowledgeBase, Company.name)
            .outerjoin(Company, Company.id == KnowledgeBase.company_id)
            .where(kb_filter)
            .order_by(KnowledgeBase.created_at.desc())
            .limit(limit)
        )
    )
    groups.append(
        GlobalSearchGroup(
            kind="knowledge_base",
            total=kb_total,
            items=[
                GlobalSearchHit(
                    kind="knowledge_base",
                    id=kb.id,
                    title=kb.name,
                    snippet=_search_excerpt(kb.description, keyword),
                    subtitle=company_name,
                    status=kb.status,
                    knowledge_base_id=kb.id,
                    created_at=kb.created_at,
                )
                for kb, company_name in kb_rows
            ],
        )
    )

    # 文档：文件名与文档标识
    doc_filter = or_(
        func.lower(Document.filename).like(pattern, **like),
        func.lower(Document.document_key).like(pattern, **like),
    )
    doc_total = int(await session.scalar(select(func.count(Document.id)).where(doc_filter)) or 0)
    doc_rows = list(
        await session.execute(
            select(Document, KnowledgeBase.name, Company.name)
            .join(KnowledgeBase, KnowledgeBase.id == Document.knowledge_base_id)
            .outerjoin(Company, Company.id == KnowledgeBase.company_id)
            .where(doc_filter)
            .order_by(Document.created_at.desc())
            .limit(limit)
        )
    )
    groups.append(
        GlobalSearchGroup(
            kind="document",
            total=doc_total,
            items=[
                GlobalSearchHit(
                    kind="document",
                    id=document.id,
                    title=document.filename,
                    snippet=document.document_key,
                    subtitle=_search_ownership(company_name, kb_name),
                    status=document.status,
                    knowledge_base_id=document.knowledge_base_id,
                    created_at=document.created_at,
                )
                for document, kb_name, company_name in doc_rows
            ],
        )
    )

    # FAQ：问题与答案；旧版本文档级 FAQ 已退出检索，这里同样不返回
    faq_filter = and_(
        or_(
            func.lower(FaqEntry.question).like(pattern, **like),
            func.lower(FaqEntry.answer).like(pattern, **like),
        ),
        or_(
            FaqEntry.document_version_id.is_(None),
            FaqEntry.document_version_id.in_(
                select(DocumentVersion.id).where(DocumentVersion.is_current.is_(True))
            ),
        ),
    )
    faq_total = int(await session.scalar(select(func.count(FaqEntry.id)).where(faq_filter)) or 0)
    faq_rows = list(
        await session.execute(
            select(FaqEntry, KnowledgeBase.name, Company.name)
            .join(KnowledgeBase, KnowledgeBase.id == FaqEntry.knowledge_base_id)
            .outerjoin(Company, Company.id == KnowledgeBase.company_id)
            .where(faq_filter)
            .order_by(FaqEntry.created_at.desc())
            .limit(limit)
        )
    )
    groups.append(
        GlobalSearchGroup(
            kind="faq",
            total=faq_total,
            items=[
                GlobalSearchHit(
                    kind="faq",
                    id=faq.id,
                    title=faq.question,
                    snippet=_search_excerpt(faq.answer, keyword),
                    subtitle=_search_ownership(company_name, kb_name),
                    status=faq.source,
                    knowledge_base_id=faq.knowledge_base_id,
                    created_at=faq.created_at,
                )
                for faq, kb_name, company_name in faq_rows
            ],
        )
    )

    # 在线问答记录：问题与答案
    qa_filter = or_(
        func.lower(QARecord.query).like(pattern, **like),
        func.lower(func.coalesce(QARecord.answer, "")).like(pattern, **like),
    )
    qa_total = int(await session.scalar(select(func.count(QARecord.id)).where(qa_filter)) or 0)
    qa_rows = list(
        await session.execute(
            select(QARecord, KnowledgeBase.name, Company.name)
            .join(KnowledgeBase, KnowledgeBase.id == QARecord.knowledge_base_id)
            .outerjoin(Company, Company.id == QARecord.company_id)
            .where(qa_filter)
            .order_by(QARecord.created_at.desc())
            .limit(limit)
        )
    )
    groups.append(
        GlobalSearchGroup(
            kind="qa_record",
            total=qa_total,
            items=[
                GlobalSearchHit(
                    kind="qa_record",
                    id=record.id,
                    title=record.query,
                    snippet=_search_excerpt(record.answer, keyword),
                    subtitle=_search_ownership(company_name, kb_name),
                    status=record.status,
                    knowledge_base_id=record.knowledge_base_id,
                    created_at=record.created_at,
                )
                for record, kb_name, company_name in qa_rows
            ],
        )
    )

    # 项目轨迹：只检索问题文本；答案由调用方回填，可能为空
    trace_filter = func.lower(ProjectQaTrace.query).like(pattern, **like)
    trace_total = int(
        await session.scalar(select(func.count(ProjectQaTrace.id)).where(trace_filter)) or 0
    )
    trace_rows = list(
        await session.execute(
            select(
                ProjectQaTrace,
                AppProject.project_key,
                AppProject.display_name,
                Company.name,
                KnowledgeBase.name,
            )
            .join(AppProject, AppProject.id == ProjectQaTrace.project_id)
            .outerjoin(Company, Company.id == ProjectQaTrace.company_id)
            .join(KnowledgeBase, KnowledgeBase.id == ProjectQaTrace.knowledge_base_id)
            .where(trace_filter)
            .order_by(ProjectQaTrace.created_at.desc())
            .limit(limit)
        )
    )
    groups.append(
        GlobalSearchGroup(
            kind="project_trace",
            total=trace_total,
            items=[
                GlobalSearchHit(
                    kind="project_trace",
                    id=trace.id,
                    title=trace.query,
                    snippet=_search_excerpt(trace.answer, keyword),
                    subtitle=_search_ownership(company_name, kb_name),
                    status=trace.answer_mode,
                    knowledge_base_id=trace.knowledge_base_id,
                    created_at=trace.created_at,
                )
                for trace, _project_key, _project_name, company_name, kb_name in trace_rows
            ],
        )
    )

    # 产品目录：产品名称与型号
    product_filter = or_(
        func.lower(ProductCatalog.product_name).like(pattern, **like),
        func.lower(ProductCatalog.model_name).like(pattern, **like),
    )
    product_total = int(
        await session.scalar(select(func.count(ProductCatalog.id)).where(product_filter)) or 0
    )
    product_rows = list(
        await session.scalars(
            select(ProductCatalog)
            .where(product_filter)
            .order_by(ProductCatalog.product_name, ProductCatalog.model_name)
            .limit(limit)
        )
    )
    groups.append(
        GlobalSearchGroup(
            kind="product",
            total=product_total,
            items=[
                GlobalSearchHit(
                    kind="product",
                    id=entry.id,
                    title=entry.product_name,
                    snippet=entry.model_name,
                    subtitle=None,
                    status=None,
                    knowledge_base_id=None,
                    created_at=entry.created_at,
                )
                for entry in product_rows
            ],
        )
    )

    return GlobalSearchRead(
        q=keyword,
        took_ms=int((time.perf_counter() - started) * 1000),
        groups=groups,
    )
