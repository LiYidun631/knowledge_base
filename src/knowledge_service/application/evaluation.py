"""检索评测集：用例匹配、进程内运行与指标汇总。

评测只调用检索服务，不生成回答，因此不消耗聊天模型 token。
"""

import asyncio
import logging
from datetime import timedelta
from time import perf_counter
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_service.application.search.service import search_knowledge_base
from knowledge_service.infrastructure.persistence.models import (
    EvaluationCase,
    EvaluationCaseResult,
    EvaluationRun,
    EvaluationSet,
    KnowledgeBase,
    utc_now,
)
from knowledge_service.infrastructure.persistence.session import get_session_factory

logger = logging.getLogger(__name__)

# 服务重启或进程被杀死后，超过该时长仍在 running 的运行视为已中断。
STALE_RUN_AFTER = timedelta(minutes=30)
_EXCERPT_CHARS = 160


class EvaluationSetNotFound(LookupError):
    """评测集不存在。"""


class EvaluationCaseNotFound(LookupError):
    """评测用例不存在。"""


class EvaluationRunNotFound(LookupError):
    """评测运行不存在。"""


class EvaluationKnowledgeBaseUnavailable(ValueError):
    """知识库已归档或不存在，无法评测。"""


class EvaluationRunConflict(RuntimeError):
    """同一知识库已有评测在运行。"""


class EvaluationNoCases(ValueError):
    """评测集没有启用中的用例。"""


def _truncate(text: str, limit: int) -> str:
    value = " ".join(text.split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _document_key(document_id: object) -> str | None:
    return str(document_id).casefold() if document_id is not None else None


def evaluate_case(
    *,
    expected_document_ids: list[str],
    expected_keywords: list[str],
    results: list[Any],
) -> dict[str, Any]:
    """按文档 ID 或正文关键词判断相关性，返回单条用例的命中与排名指标。

    文档 ID 与关键词是「或」关系；召回率按期望文档在 top_k 内的覆盖比例计算，
    仅配置关键词时退化为是否命中（0 或 1）。
    """
    expected_documents = {value.casefold() for value in map(str, expected_document_ids) if value}
    keywords = [value.casefold() for value in expected_keywords if value]
    matches: list[dict[str, Any]] = []
    matched_documents: set[str] = set()
    first_rank: int | None = None
    for rank, result in enumerate(results, start=1):
        document_key = _document_key(getattr(result, "document_id", None))
        content = str(getattr(result, "content", "") or "")
        matched_by = None
        if document_key and document_key in expected_documents:
            matched_by = "document"
            matched_documents.add(document_key)
        elif any(keyword in content.casefold() for keyword in keywords):
            matched_by = "keyword"
        if matched_by is not None and first_rank is None:
            first_rank = rank
        matches.append(
            {
                "rank": rank,
                "document_id": document_key,
                "filename": getattr(result, "filename", None),
                "page_number": getattr(result, "page_number", None),
                "section": getattr(result, "section", None),
                "matched_by": matched_by,
                "excerpt": _truncate(content, _EXCERPT_CHARS),
            }
        )
    if expected_documents:
        recall = len(matched_documents) / len(expected_documents)
    else:
        recall = 1.0 if first_rank is not None else 0.0
    return {
        "hit": first_rank is not None,
        "first_relevant_rank": first_rank,
        "recall": round(recall, 4),
        "reciprocal_rank": round(1 / first_rank, 4) if first_rank else 0.0,
        "matches": matches,
    }


def summarize_run(
    outcomes: list[dict[str, Any]],
    *,
    case_count: int,
    errored_count: int,
    latencies: list[int],
) -> dict[str, Any]:
    """汇总命中率、平均召回率、MRR、无结果率与平均耗时。"""
    completed = len(outcomes)
    if completed == 0:
        return {
            "case_count": case_count,
            "completed_count": 0,
            "errored_count": errored_count,
            "hit_rate": 0.0,
            "recall_at_k": 0.0,
            "mrr": 0.0,
            "no_result_rate": 0.0,
            "avg_latency_ms": 0,
        }
    return {
        "case_count": case_count,
        "completed_count": completed,
        "errored_count": errored_count,
        "hit_rate": round(sum(1 for item in outcomes if item["hit"]) / completed, 4),
        "recall_at_k": round(sum(item["recall"] for item in outcomes) / completed, 4),
        "mrr": round(sum(item["reciprocal_rank"] for item in outcomes) / completed, 4),
        "no_result_rate": round(
            sum(1 for item in outcomes if item["result_count"] == 0) / completed, 4
        ),
        "avg_latency_ms": round(sum(latencies) / len(latencies)) if latencies else 0,
    }


async def recover_stale_runs(
    session: AsyncSession, *, knowledge_base_id: UUID | None = None
) -> int:
    """把超时仍处于 running 的运行标记为失败；进程重启后由启动钩子调用。"""
    conditions = [
        EvaluationRun.status == "running",
        EvaluationRun.started_at < utc_now() - STALE_RUN_AFTER,
    ]
    if knowledge_base_id is not None:
        conditions.append(EvaluationRun.knowledge_base_id == knowledge_base_id)
    runs = list(await session.scalars(select(EvaluationRun).where(*conditions)))
    for run in runs:
        run.status = "failed"
        run.error_message = "评测进程中断或超时，已自动终止"
        run.finished_at = utc_now()
    return len(runs)


async def start_run(session: AsyncSession, set_id: UUID) -> EvaluationRun:
    """创建运行记录；同一知识库同时只允许一个评测在跑。"""
    async with _START_LOCK:
        evaluation_set = await session.get(EvaluationSet, set_id)
        if evaluation_set is None:
            raise EvaluationSetNotFound
        knowledge_base = await session.get(KnowledgeBase, evaluation_set.knowledge_base_id)
        if knowledge_base is None or knowledge_base.status != "active":
            raise EvaluationKnowledgeBaseUnavailable
        await recover_stale_runs(session, knowledge_base_id=evaluation_set.knowledge_base_id)
        running = await session.scalar(
            select(EvaluationRun.id).where(
                EvaluationRun.knowledge_base_id == evaluation_set.knowledge_base_id,
                EvaluationRun.status == "running",
            )
        )
        if running is not None:
            raise EvaluationRunConflict
        enabled_count = int(
            await session.scalar(
                select(func.count(EvaluationCase.id)).where(
                    EvaluationCase.set_id == set_id,
                    EvaluationCase.is_enabled.is_(True),
                )
            )
            or 0
        )
        if enabled_count == 0:
            raise EvaluationNoCases
        run = EvaluationRun(
            set_id=set_id,
            knowledge_base_id=evaluation_set.knowledge_base_id,
            company_id=knowledge_base.company_id,
            status="running",
            case_count=enabled_count,
            completed_count=0,
            metrics={},
            started_at=utc_now(),
        )
        session.add(run)
        await session.flush()
        return run


async def execute_run(run_id: UUID) -> None:
    """顺序执行评测集内启用的用例，每完成一条提交一次进度。"""
    factory = get_session_factory()
    try:
        async with factory() as session:
            run = await session.get(EvaluationRun, run_id)
            if run is None or run.status != "running":
                return
            cases = list(
                await session.scalars(
                    select(EvaluationCase)
                    .where(
                        EvaluationCase.set_id == run.set_id,
                        EvaluationCase.is_enabled.is_(True),
                    )
                    .order_by(EvaluationCase.created_at, EvaluationCase.id)
                )
            )
            run.case_count = len(cases)
            outcomes: list[dict[str, Any]] = []
            latencies: list[int] = []
            errored_count = 0
            for case in cases:
                started_at = perf_counter()
                results: list[Any] = []
                error_text: str | None = None
                try:
                    results = await search_knowledge_base(
                        session,
                        knowledge_base_id=run.knowledge_base_id,
                        company_id=None,
                        query=case.query,
                        top_k=case.top_k,
                        document_ids=None,
                        retrieval_mode=case.retrieval_mode,
                    )
                except Exception as exc:  # noqa: BLE001 - 单条失败不终止整次评测
                    error_text = (str(exc).strip() or exc.__class__.__name__)[:1000]
                    errored_count += 1
                    logger.warning(
                        "Evaluation case failed: run_id=%s case_id=%s error=%s",
                        run.id,
                        case.id,
                        error_text,
                    )
                latency = max(0, int((perf_counter() - started_at) * 1000))
                latencies.append(latency)
                outcome = evaluate_case(
                    expected_document_ids=case.expected_document_ids or [],
                    expected_keywords=case.expected_keywords or [],
                    results=results,
                )
                outcome["result_count"] = len(results)
                outcomes.append(outcome)
                session.add(
                    EvaluationCaseResult(
                        run_id=run.id,
                        case_id=case.id,
                        query=case.query,
                        retrieval_mode=case.retrieval_mode,
                        top_k=case.top_k,
                        hit=outcome["hit"],
                        first_relevant_rank=outcome["first_relevant_rank"],
                        recall=outcome["recall"],
                        reciprocal_rank=outcome["reciprocal_rank"],
                        result_count=outcome["result_count"],
                        duration_ms=latency,
                        matches=outcome["matches"],
                        error_message=error_text,
                    )
                )
                run.completed_count += 1
                await session.commit()

            run.metrics = summarize_run(
                outcomes,
                case_count=len(cases),
                errored_count=errored_count,
                latencies=latencies,
            )
            if cases and errored_count == len(cases):
                run.status = "failed"
                run.error_message = "全部用例检索失败，请检查知识库模型与向量库状态"
            else:
                run.status = "success"
            run.finished_at = utc_now()
            await session.commit()
    except Exception as exc:  # noqa: BLE001 - 运行级异常必须落库，避免长期停在 running
        logger.exception("Evaluation run crashed: run_id=%s", run_id)
        async with factory() as session:
            run = await session.get(EvaluationRun, run_id)
            if run is not None and run.status == "running":
                run.status = "failed"
                run.error_message = (str(exc).strip() or exc.__class__.__name__)[:1000]
                run.finished_at = utc_now()
                await session.commit()


_RUNNING_TASKS: set[asyncio.Task[None]] = set()
# 单进程内串行化启动检查，避免两个请求同时通过「无运行中评测」的判断。
_START_LOCK = asyncio.Lock()


def schedule_run(run_id: UUID) -> None:
    """在当前进程内启动评测任务；任务异常由 execute_run 自行落库。"""
    task = asyncio.create_task(execute_run(run_id), name=f"evaluation-run-{run_id}")
    _RUNNING_TASKS.add(task)
    task.add_done_callback(_RUNNING_TASKS.discard)
