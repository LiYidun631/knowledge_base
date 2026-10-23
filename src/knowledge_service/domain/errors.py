class ModelConfigurationLocked(Exception):
    """知识库模型绑定锁定后又尝试更改。"""


class ConfigurationNotFound(Exception):
    """请求的模型配置不存在、未启用或类型不匹配。"""


class KnowledgeBaseNotFound(Exception):
    """知识库不存在或已归档。"""


class CompanyCredentialAlreadyExists(Exception):
    """公司已签发凭据，必须显式重置以免意外撤销现有访问。"""


class CompanyCredentialNotFound(Exception):
    """公司尚未签发访问凭据。"""


class AdminCredentialNotFound(Exception):
    """请求的管理员凭据不存在。"""


class CredentialEncryptionError(Exception):
    """模型 API Key 加密密钥缺失或密文无法解密。"""


class ModelProfileNotFound(Exception):
    """请求的模型档案不存在。"""


class ModelProfileInUse(Exception):
    """模型档案仍被大库或子库引用，不能删除。"""


class FolderNotFound(Exception):
    """目录不存在或不属于指定知识库。"""


class FolderNotEmpty(Exception):
    """目录下仍有子目录或文件，不能删除。"""


class FolderNameConflict(Exception):
    """同一级目录下已存在同名目录。"""


class KnowledgeBaseArchived(Exception):
    """知识库已归档，不允许再修改目录、文档归属或重试入库。"""
