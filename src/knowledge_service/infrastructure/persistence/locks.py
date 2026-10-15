"""生成 MySQL 命名锁使用的稳定锁名。"""

from uuid import UUID


def ingestion_lock_name(job_id: UUID) -> str:
    """返回不超过 MySQL 64 字节限制且跨进程稳定的任务锁名。"""
    return f"ingestion:{job_id.hex}"
