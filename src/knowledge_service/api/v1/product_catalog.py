"""全局产品目录管理接口；目录与知识库无关，供各库上传标签选择。"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi import Path as ApiPath
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_service.api.auth import require_admin_service_auth
from knowledge_service.api.v1.schemas import (
    BatchDeleteResponse,
    ProductCatalogBatchDeleteRequest,
    ProductCatalogEntryCreate,
    ProductCatalogEntryRead,
    ProductCatalogEntryUpdate,
)
from knowledge_service.infrastructure.persistence.models import Document, ProductCatalog
from knowledge_service.infrastructure.persistence.session import get_db_session

router = APIRouter(prefix="/product-catalog", tags=["全局产品目录"])

_ADMIN = [Depends(require_admin_service_auth)]


@router.get(
    "",
    response_model=list[ProductCatalogEntryRead],
    summary="查询全局产品目录",
    description=(
        "返回全局产品目录条目；可选按关键词（产品名称或型号模糊匹配）和产品名称过滤，"
        "按产品名称、型号、创建时间排序。管理员或任一有效公司 Token 均可读取，"
        "供外部问答客户端对齐产品标签；写操作仍仅限管理员。"
    ),
)
async def list_product_catalog(
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
    q: Annotated[
        str | None,
        Query(description="关键词，对产品名称与型号做不区分大小写的模糊匹配。"),
    ] = None,
    product_name: Annotated[
        str | None,
        Query(description="精确过滤的产品名称。"),
    ] = None,
) -> list[ProductCatalogEntryRead]:
    statement = select(ProductCatalog).order_by(
        ProductCatalog.product_name,
        ProductCatalog.model_name,
        ProductCatalog.created_at,
    )
    if product_name:
        statement = statement.where(ProductCatalog.product_name == product_name)
    if q:
        keyword = f"%{q.strip()}%"
        statement = statement.where(
            func.lower(ProductCatalog.product_name).like(keyword.lower())
            | func.lower(ProductCatalog.model_name).like(keyword.lower())
        )
    entries = await session.scalars(statement)
    return [ProductCatalogEntryRead.model_validate(entry) for entry in entries]


@router.post(
    "",
    response_model=ProductCatalogEntryRead,
    status_code=status.HTTP_201_CREATED,
    summary="新增产品目录条目",
    description="手动登记产品名称与型号；相同组合已存在时返回 409。",
    dependencies=_ADMIN,
)
async def create_product_catalog_entry(
    payload: ProductCatalogEntryCreate,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> ProductCatalogEntryRead:
    duplicate = await session.scalar(
        select(ProductCatalog.id).where(
            ProductCatalog.product_name == payload.product_name,
            ProductCatalog.model_name == payload.model_name,
        )
    )
    if duplicate is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="该产品名称与型号组合已在目录中",
        )
    entry = ProductCatalog(
        product_name=payload.product_name,
        model_name=payload.model_name,
    )
    session.add(entry)
    await session.commit()
    await session.refresh(entry)
    return ProductCatalogEntryRead.model_validate(entry)


def _rename_document_labels(
    metadata: dict | None,
    *,
    product_from: str | None,
    product_to: str | None,
    model_from: str | None,
    model_to: str | None,
) -> dict | None:
    """把元数据中的旧产品/型号标签替换为新标签；无任何变化时返回 None。"""
    if not metadata:
        return None
    changed = False
    updated = dict(metadata)
    for list_key, scalar_key, old, new in (
        ("product_names", "product_name", product_from, product_to),
        ("model_names", "model_name", model_from, model_to),
    ):
        if not old or old == new:
            continue
        for key in (list_key, scalar_key):
            value = updated.get(key)
            if isinstance(value, list):
                renamed = [new if item == old else item for item in value]
                if renamed != value:
                    updated[key] = renamed
                    changed = True
            elif isinstance(value, str) and value == old:
                updated[key] = new
                changed = True
    return updated if changed else None


@router.put(
    "/{catalog_id}",
    response_model=ProductCatalogEntryRead,
    summary="修改产品目录条目",
    description=(
        "修改目录条目的产品名称或型号。产品名会同步更新目录中同名的其他行，"
        "型号会同步更新目录中同型号的其他行；所有引用旧标签的文档元数据一并更新。"
        "改名后若与现有条目冲突返回 409。"
    ),
    dependencies=_ADMIN,
)
async def update_product_catalog_entry(
    catalog_id: Annotated[UUID, ApiPath(description="目录条目 ID。")],
    payload: ProductCatalogEntryUpdate,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> ProductCatalogEntryRead:
    entry = await session.get(ProductCatalog, catalog_id)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="目录条目不存在"
        )
    old_product, old_model = entry.product_name, entry.model_name
    new_product, new_model = payload.product_name, payload.model_name
    if new_product == old_product and new_model == old_model:
        return ProductCatalogEntryRead.model_validate(entry)

    rename_product = new_product != old_product
    rename_model = new_model != old_model
    rows = list(await session.scalars(select(ProductCatalog)))
    affected = [
        row
        for row in rows
        if (rename_product and row.product_name == old_product)
        or (rename_model and row.model_name == old_model)
    ]

    def target(row: ProductCatalog) -> tuple[str, str]:
        product = (
            new_product
            if rename_product and row.product_name == old_product
            else row.product_name
        )
        model = new_model if rename_model and row.model_name == old_model else row.model_name
        return product, model

    new_pairs = [target(row) for row in affected]
    if len(set(new_pairs)) != len(new_pairs):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="改名后目录中将出现重复的产品名称与型号组合",
        )
    occupied = {
        (row.product_name, row.model_name) for row in rows if row not in affected
    }
    if occupied.intersection(new_pairs):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="新名称与目录中已有条目冲突",
        )

    for row in affected:
        row.product_name, row.model_name = target(row)

    # 目录与文档标签同事务级联，避免改名后文档元数据残留旧标签。
    document_rows = await session.execute(
        select(Document.id, Document.extra_metadata)
    )
    for document_id, metadata in document_rows:
        renamed = _rename_document_labels(
            metadata,
            product_from=old_product if rename_product else None,
            product_to=new_product if rename_product else None,
            model_from=old_model if rename_model else None,
            model_to=new_model if rename_model else None,
        )
        if renamed is not None:
            await session.execute(
                update(Document)
                .where(Document.id == document_id)
                .values(extra_metadata=renamed)
            )
    await session.commit()
    await session.refresh(entry)
    return ProductCatalogEntryRead.model_validate(entry)


@router.post(
    "/batch-delete",
    response_model=BatchDeleteResponse,
    summary="批量删除产品目录条目",
    description="按条目 ID 批量删除目录条目；不存在的 ID 会使整个请求返回 404。",
    dependencies=_ADMIN,
)
async def batch_delete_product_catalog(
    payload: ProductCatalogBatchDeleteRequest,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> BatchDeleteResponse:
    entries = list(
        await session.scalars(
            select(ProductCatalog).where(ProductCatalog.id.in_(payload.catalog_ids))
        )
    )
    found_ids = {entry.id for entry in entries}
    missing_ids = [
        catalog_id for catalog_id in payload.catalog_ids if catalog_id not in found_ids
    ]
    if missing_ids:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "message": "部分目录条目不存在",
                "catalog_ids": [str(catalog_id) for catalog_id in missing_ids],
            },
        )
    # 按请求提交顺序返回删除结果，避免无 ORDER BY 查询导致顺序随机。
    entries_by_id = {entry.id: entry for entry in entries}
    ordered = [entries_by_id[catalog_id] for catalog_id in payload.catalog_ids]
    for entry in ordered:
        await session.delete(entry)
    await session.commit()
    return BatchDeleteResponse(
        deleted_count=len(ordered),
        deleted_ids=[str(entry.id) for entry in ordered],
    )


@router.delete(
    "/{catalog_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除产品目录条目",
    description="删除目录中的一条产品名称与型号记录；条目不存在时返回 404。",
    dependencies=_ADMIN,
)
async def delete_product_catalog_entry(
    catalog_id: Annotated[UUID, ApiPath(description="目录条目 ID。")],
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> Response:
    entry = await session.scalar(
        select(ProductCatalog).where(ProductCatalog.id == catalog_id)
    )
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="目录条目不存在"
        )
    await session.delete(entry)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
