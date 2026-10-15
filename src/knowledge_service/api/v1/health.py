from fastapi import APIRouter

from knowledge_service import __version__
from knowledge_service.core.config import get_settings

router = APIRouter(tags=["健康检查"])


@router.get(
    "/health",
    summary="检查服务健康状态",
    description="返回服务状态、服务名称和当前版本，可用于部署环境的存活检查。",
)
def health_check() -> dict[str, str]:
    settings = get_settings()
    return {
        "status": "ok",
        "service": settings.name,
        "version": __version__,
    }
