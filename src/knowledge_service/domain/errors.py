class ModelConfigurationLocked(Exception):
    """知识库模型绑定锁定后又尝试更改。"""


class ConfigurationNotFound(Exception):
    """请求的模型配置不存在、未启用或类型不匹配。"""


class KnowledgeBaseNotFound(Exception):
    """知识库不存在或已归档。"""
