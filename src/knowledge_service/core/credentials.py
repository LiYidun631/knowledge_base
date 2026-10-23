"""模型 API Key 的加密存取；管理接口只写不回读，明文仅在模型调用时解密。"""

from cryptography.fernet import Fernet, InvalidToken

from knowledge_service.core.config import get_settings, resolve_secret
from knowledge_service.domain.errors import CredentialEncryptionError
from knowledge_service.infrastructure.persistence.models import ModelProfile


def _fernet() -> Fernet:
    key = get_settings().credential_encryption_key.get_secret_value()
    if not key:
        raise CredentialEncryptionError(
            "未配置 KNOWLEDGE_SERVICE_CREDENTIAL_ENCRYPTION_KEY，无法加密或解密模型 API Key"
        )
    return Fernet(key.encode("ascii"))


def encrypt_api_key(plaintext: str) -> str:
    """把明文 API Key 加密为可入库的 Fernet 密文。"""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def resolve_profile_api_key(profile: ModelProfile) -> str | None:
    """优先使用管理页写入的库内密钥，其次回落到 secret_ref 引用的环境密钥。"""
    if profile.api_key_cipher:
        try:
            return _fernet().decrypt(profile.api_key_cipher.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise CredentialEncryptionError(
                f"模型档案 {profile.name} 的 API Key 密文无法解密，"
                "请检查 KNOWLEDGE_SERVICE_CREDENTIAL_ENCRYPTION_KEY 是否已更换"
            ) from exc
    if profile.secret_ref:
        return resolve_secret(profile.secret_ref)
    return None
