from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, SecretStr, field_validator

from knowledge_service.application.model_profiles import ModelProfileSpec


class KnowledgeSearchRequest(BaseModel):
    """知识库在线检索请求；默认使用 BM25 与向量混合召回。"""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=4000, description="待检索的问题或关键词。")
    top_k: int = Field(default=5, ge=1, le=100, description="最终返回的父块数量。")
    document_ids: list[UUID] | None = Field(
        default=None,
        min_length=1,
        max_length=50,
        description="可选文档范围，最多指定 50 个文档。",
    )
    retrieval_mode: Literal["hybrid", "bm25", "vector"] = Field(
        default="hybrid",
        description="检索方式：hybrid 为加权 RRF 混合检索，也可单独测试 bm25 或 vector。",
    )
    include_context: bool = Field(
        default=False, description="是否返回命中父块前后的相邻父块上下文。"
    )
    product_name: str | None = Field(
        default=None, max_length=200, description="产品名称过滤；仅检索该产品下的资料。"
    )
    model_name: str | None = Field(
        default=None, max_length=200, description="产品型号过滤；仅检索该型号的资料。"
    )

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        """去除首尾空白，避免空白字符串触发无意义检索。"""
        value = value.strip()
        if not value:
            raise ValueError("查询内容不能为空")
        return value

    @field_validator("product_name", "model_name")
    @classmethod
    def normalize_product_filter(cls, value: str | None) -> str | None:
        """空白过滤值视为未提供，避免误过滤整个知识库。"""
        if value is None:
            return None
        value = value.strip()
        return value or None


class SearchScores(BaseModel):
    """返回各召回分支的原始分数及融合分数。"""

    cosine_distance: float | None = Field(description="Chroma cosine 距离，越小越相似。")
    cosine_similarity: float | None = Field(description="由 1 - cosine_distance 换算的相似度。")
    bm25_score: float | None = Field(description="MySQL BM25 相关度分数，越大越相关。")
    rrf_score: float | None = Field(description="混合模式的加权 Reciprocal Rank Fusion 分数。")
    rerank_score: float | None = Field(description="已绑定 Rerank 模型返回的原始分数。")


class SearchSource(BaseModel):
    """结果对应的逻辑文档版本及父块引用位置。"""

    document_id: UUID = Field(description="逻辑文档 ID。")
    document_version_id: UUID = Field(description="当前成功版本 ID。")
    version_number: int = Field(description="文档版本序号。")
    filename: str = Field(description="上传文件名。")
    file_id: str = Field(description="后台文件 ID；后台可据此拼接原文件下载地址。")
    chunk_id: UUID = Field(description="返回正文对应的父块 ID。")
    page_number: int | None = Field(description="页码；无法解析时为空。")
    section: str | None = Field(description="章节；无法解析时为空。")
    section_path: list[str] = Field(
        default_factory=list,
        description="从文档顶层到当前章节的标题路径；无法解析时为空列表。",
    )


class SearchAsset(BaseModel):
    """检索父块关联的图片资产引用；图片通过受保护接口读取。"""

    asset_id: UUID = Field(description="图片资产 ID。")
    media_type: str = Field(description="图片 MIME 类型。")
    description: str | None = Field(description="视觉模型生成的图片说明。")
    page_number: int | None = Field(description="图片所在页码。")
    bbox: list[float] | None = Field(description="归一化版面区域坐标。")
    url: str = Field(description="图片读取接口地址。")
    location: dict[str, Any] | None = Field(
        default=None, description="原始位置：块序号、页码、表格行列及版面坐标。"
    )


class SearchContextItem(BaseModel):
    """上下文扩展块与实际命中块的关系。"""

    chunk_id: UUID = Field(description="上下文父块 ID。")
    relation: Literal["before", "hit", "after"] = Field(description="相对命中块的位置。")
    content: str = Field(description="上下文父块正文。")


class KnowledgeSearchResult(BaseModel):
    """一条父块检索结果。"""

    rank: int = Field(description="按最终排序计算的名次，从 1 开始。")
    content: str = Field(description="父块正文，可直接作为问答上下文。")
    scores: SearchScores = Field(description="检索分支和重排分数。")
    source: SearchSource = Field(description="原始文档与分块来源。")
    assets: list[SearchAsset] = Field(default_factory=list, description="父块关联的图片资产。")
    context: str | None = Field(default=None, description="可选的前后父块组合上下文。")
    context_sources: list[SearchContextItem] = Field(
        default_factory=list, description="上下文块与命中块的关系。"
    )


class KnowledgeSearchResponse(BaseModel):
    """知识库检索响应；不包含生成式答案。"""

    company_id: UUID = Field(description="本次检索所属公司 ID。")
    knowledge_base_id: UUID = Field(description="本次检索使用的知识库 ID。")
    query: str = Field(description="规范化后的原始查询。")
    retrieval_mode: str = Field(description="实际执行的召回模式。")
    results: list[KnowledgeSearchResult] = Field(description="按相关度排序的父块列表。")


class KnowledgeAnswerRequest(KnowledgeSearchRequest):
    """知识库问答请求；先检索资料，再调用聊天模型生成答案。"""

    include_context: bool = Field(
        default=True, description="是否将命中父块的前后同章节内容一并交给聊天模型。"
    )
    session_id: UUID | None = Field(
        default=None, description="可选问答会话 ID；相同 ID 的请求会归入同一会话。"
    )


class KnowledgeAnswerResponse(BaseModel):
    """知识库生成式回答及其检索依据。"""

    company_id: UUID = Field(description="本次问答所属公司 ID。")
    knowledge_base_id: UUID = Field(description="本次问答使用的知识库 ID。")
    qa_record_id: UUID = Field(description="问答记录 ID，可用于查询来源和阶段日志。")
    request_id: str = Field(description="本次问答请求 ID，可用于查询问答日志。")
    session_id: UUID | None = Field(description="问答会话 ID；未传会话时为空。")
    query: str = Field(description="规范化后的原始问题。")
    answer: str = Field(description="聊天模型根据知识库资料整理后的答案。")
    clarification_required: bool = Field(default=False, description="是否需要用户先补充产品型号。")
    clarification_question: str | None = Field(
        default=None, description="需要用户补充信息时返回的追问。"
    )
    clarification_options: list[str] = Field(
        default_factory=list, description="可供用户选择的产品型号。"
    )
    # 命中库级/文档级 FAQ 时原样直出，不再走检索与生成。
    faq_matched: bool = Field(default=False, description="答案是否来自 FAQ 直接命中。")
    retrieval_mode: str = Field(description="实际执行的召回模式。")
    sources: list[KnowledgeSearchResult] = Field(description="生成答案使用的检索结果和来源。")


class QARecordRead(BaseModel):
    """在线问答记录摘要；不返回提示词和密钥。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(description="问答记录 ID。")
    request_id: str = Field(description="请求追踪 ID。")
    company_id: UUID | None = Field(description="所属公司 ID。")
    knowledge_base_id: UUID = Field(description="所属知识库 ID。")
    session_id: UUID | None = Field(description="所属会话 ID。")
    query: str = Field(description="用户问题。")
    answer: str | None = Field(description="模型回答；失败或处理中可能为空。")
    retrieval_mode: str = Field(description="检索模式。")
    top_k: int = Field(description="请求的最大结果数。")
    include_context: bool = Field(description="是否启用上下文扩展。")
    status: str = Field(description="问答状态：running、success、no_results 或 failed。")
    review_status: str = Field(
        description="复核状态：none 未复核、pending 待补资料、resolved 已补充。"
    )
    faq_matched: bool = Field(description="答案是否来自 FAQ 直接命中。")
    result_count: int = Field(description="实际保存的检索来源数量。")
    chat_provider: str | None = Field(description="回答模型供应商。")
    chat_model_name: str | None = Field(description="回答模型名称。")
    retrieval_duration_ms: int | None = Field(description="检索耗时，单位毫秒。")
    generation_duration_ms: int | None = Field(description="生成耗时，单位毫秒。")
    total_duration_ms: int | None = Field(description="总耗时，单位毫秒。")
    prompt_tokens: int | None = Field(description="输入 token 数；未记录时为空。")
    completion_tokens: int | None = Field(description="输出 token 数；未记录时为空。")
    total_tokens: int | None = Field(description="总 token 数；未记录时为空。")
    error_code: str | None = Field(description="失败错误代码。")
    error_message: str | None = Field(description="失败错误摘要。")
    created_at: datetime = Field(description="创建时间。")
    updated_at: datetime = Field(description="最后更新时间。")


class QASourceRead(BaseModel):
    """问答引用来源快照。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(description="来源记录 ID。")
    company_id: UUID | None = Field(description="所属公司 ID。")
    rank: int = Field(description="来源在本次问答中的排序。")
    document_id: UUID | None = Field(description="原文档 ID；文档删除后可能为空。")
    document_version_id: UUID | None = Field(description="文档版本 ID。")
    chunk_id: UUID | None = Field(description="命中父块 ID。")
    filename: str = Field(description="文件名快照。")
    version_number: int = Field(description="文档版本号。")
    page_number: int | None = Field(description="页码。")
    section: str | None = Field(description="章节。")
    content_snapshot: str = Field(description="回答生成时使用的正文快照。")
    context_snapshot: str | None = Field(description="回答生成时使用的上下文快照。")
    cosine_distance: float | None = Field(description="Chroma 距离。")
    bm25_score: float | None = Field(description="BM25 分数。")
    rrf_score: float | None = Field(description="RRF 融合分数。")
    rerank_score: float | None = Field(description="Rerank 分数。")
    asset_ids: list[str] = Field(description="来源关联的图片资产 ID。")


class QAEventRead(BaseModel):
    """问答阶段日志。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(description="阶段日志 ID。")
    company_id: UUID | None = Field(description="所属公司 ID。")
    request_id: str = Field(description="请求追踪 ID。")
    stage: str = Field(description="阶段：request、retrieval 或 generation。")
    status: str = Field(description="阶段状态：started、success 或 failed。")
    duration_ms: int | None = Field(description="阶段耗时，单位毫秒。")
    error_code: str | None = Field(description="阶段错误代码。")
    message: str | None = Field(description="阶段日志摘要。")
    details: dict[str, object] = Field(description="阶段统计信息。")
    created_at: datetime = Field(description="日志创建时间。")


class QARecordDetail(QARecordRead):
    """问答记录详情及其来源和阶段日志。"""

    sources: list[QASourceRead] = Field(description="本次问答引用的来源快照。")
    events: list[QAEventRead] = Field(description="本次问答的阶段日志。")


class QARecordListResponse(BaseModel):
    """分页返回知识库的在线问答记录。"""

    page: int = Field(description="当前页码。")
    page_size: int = Field(description="每页记录数。")
    total: int = Field(description="符合条件的记录总数。")
    items: list[QARecordRead] = Field(description="问答记录摘要。")


class QARecordAdminRead(QARecordRead):
    """管理员视图的问答记录摘要；附带公司与知识库名称。"""

    company_name: str | None = Field(description="所属公司名称；记录无公司归属时为空。")
    knowledge_base_name: str = Field(description="所属知识库名称。")


class QARecordAdminListResponse(BaseModel):
    """管理员跨公司分页查询的问答记录。"""

    page: int = Field(description="当前页码。")
    page_size: int = Field(description="每页记录数。")
    total: int = Field(description="符合条件的记录总数。")
    items: list[QARecordAdminRead] = Field(description="问答记录摘要。")


class QARecordReviewPatch(BaseModel):
    """管理员对问答记录的复核状态修改。"""

    model_config = ConfigDict(extra="forbid")

    review_status: Literal["none", "resolved"] = Field(
        description="resolved 标记已补充资料；none 重新打开为待复核。"
    )


class AdminDailyTrendRead(BaseModel):
    """统计仪表盘的单日问答趋势点。"""

    date: str = Field(description="日期，格式 YYYY-MM-DD（UTC）。")
    qa_total: int = Field(description="当日问答记录总数。")
    qa_success: int = Field(description="当日成功记录数。")
    avg_total_duration_ms: int | None = Field(description="当日平均总耗时毫秒；无数据时为空。")
    total_tokens: int = Field(default=0, description="当日已记录的生成 token 总数。")


class AdminTokenUsageRead(BaseModel):
    """问答生成模型的 token 用量汇总；仅统计有模型返回用量的记录。"""

    prompt_tokens: int = Field(description="累计输入 token 数。")
    completion_tokens: int = Field(description="累计输出 token 数。")
    total_tokens: int = Field(description="累计 token 总数。")
    recent_prompt_tokens: int = Field(description="近 14 天输入 token 数。")
    recent_completion_tokens: int = Field(description="近 14 天输出 token 数。")
    recent_total_tokens: int = Field(description="近 14 天 token 总数。")
    measured_qa_count: int = Field(description="已记录 token 用量的问答条数。")


class AdminStatisticsRead(BaseModel):
    """管理员统计仪表盘聚合数据。"""

    companies_total: int = Field(description="活跃公司数量。")
    knowledge_bases_total: int = Field(description="活跃知识库数量。")
    documents_total: int = Field(description="文档总数（含各处理状态）。")
    documents_by_status: dict[str, int] = Field(description="文档按处理状态分布。")
    qa_total: int = Field(description="问答记录总数。")
    qa_by_status: dict[str, int] = Field(description="问答记录按状态分布。")
    review_pending: int = Field(description="待补资料复核数。")
    token_usage: AdminTokenUsageRead = Field(description="问答生成模型 token 用量汇总。")
    daily_trend: list[AdminDailyTrendRead] = Field(description="近 14 天每日问答趋势。")


class AdminPromptRead(BaseModel):
    """管理端提示词视图；content 为当前生效文本，brand_name 占位符已替换。"""

    name: str = Field(description="提示词名称标识。")
    label: str = Field(description="提示词中文名称。")
    source: Literal["override", "file", "builtin"] = Field(
        description="override 管理页覆写、file 提示词文件、builtin 内置默认。"
    )
    content: str = Field(description="当前生效的提示词文本。")
    updated_at: datetime | None = Field(description="覆写更新时间；非覆写来源为空。")


class AdminPromptWrite(BaseModel):
    """管理端保存提示词覆写。"""

    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=20000, description="提示词正文。")


class KnowledgeBaseCreate(BaseModel):
    """创建旧版兼容知识库的请求参数；模型由服务端默认配置绑定。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=160, description="知识库名称，不能重复。")
    description: str | None = Field(
        default=None, max_length=1000, description="知识库用途说明，可不填写。"
    )


class KnowledgeBaseUpdate(BaseModel):
    """知识库创建后可修改名称、描述和分块参数；模型绑定不可变。"""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(
        default=None, min_length=1, max_length=160, description="新的知识库名称。"
    )
    description: str | None = Field(
        default=None, max_length=1000, description="新的用途说明；传 null 可清空。"
    )
    chunk_parent_chars: int | None = Field(
        default=None,
        ge=1,
        description="父块字符上限；传 null 表示跟随全局默认，只影响新入库文档。",
    )
    chunk_child_chars: int | None = Field(
        default=None,
        ge=1,
        description="子块字符上限；传 null 表示跟随全局默认，只影响新入库文档。",
    )
    chunk_child_overlap_chars: int | None = Field(
        default=None,
        ge=0,
        description="子块重叠字符数；传 null 表示跟随全局默认，只影响新入库文档。",
    )


class KnowledgeBaseRead(BaseModel):
    """知识库信息及其模型绑定状态。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(description="知识库唯一标识。")
    company_id: UUID | None = Field(description="所属公司 ID；历史兼容记录可能为空。")
    vector_library_id: UUID | None = Field(description="所属公司级大库 ID。")
    name: str = Field(description="知识库名称。")
    description: str | None = Field(description="知识库用途说明。")
    embedding_model_id: UUID = Field(description="绑定的 Embedding 模型档案 ID。")
    rerank_model_id: UUID | None = Field(description="绑定的 Rerank 模型档案 ID；为空表示未启用。")
    models_locked_at: datetime = Field(description="模型绑定锁定时间。")
    chunk_parent_chars: int | None = Field(
        description="父块字符上限；为空表示跟随全局默认，只影响新入库文档。"
    )
    chunk_child_chars: int | None = Field(
        description="子块字符上限；为空表示跟随全局默认，只影响新入库文档。"
    )
    chunk_child_overlap_chars: int | None = Field(
        description="子块重叠字符数；为空表示跟随全局默认，只影响新入库文档。"
    )
    status: str = Field(description="知识库状态，例如 active 或 archived。")
    archived_at: datetime | None = Field(description="归档时间；启用状态下为空。")
    created_at: datetime = Field(description="创建时间。")
    updated_at: datetime = Field(description="最后更新时间。")


class CompanyCreate(BaseModel):
    """创建公司租户的请求参数；公司编码由服务端自动生成。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200, description="公司名称。")
    description: str | None = Field(default=None, max_length=1000, description="公司说明。")


class CompanyRead(BaseModel):
    """公司租户信息。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(description="公司 ID。")
    code: str = Field(description="公司稳定编码。")
    name: str = Field(description="公司名称。")
    description: str | None = Field(description="公司说明。")
    status: str = Field(description="公司状态：active 或 archived。")
    credential_count: int = Field(
        default=0, description="已签发的访问凭据数量，含已停用凭据。"
    )
    active_credential_count: int = Field(
        default=0, description="启用中的访问凭据数量。"
    )
    has_vector_library: bool = Field(
        default=False, description="公司是否已创建向量大库。"
    )
    created_at: datetime = Field(description="创建时间。")
    updated_at: datetime = Field(description="最后更新时间。")


class CompanyCredentialCreate(BaseModel):
    """为公司签发新凭据的请求参数；名称用于区分不同项目。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        min_length=1, max_length=120, description="项目或用途标识，例如“问诊助手”。"
    )

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("项目标识不能为空")
        return stripped


class CompanyCredentialRead(BaseModel):
    """公司凭据信息；不返回任何哈希或明文。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(description="凭据 ID。")
    company_id: UUID = Field(description="凭据所属公司 ID。")
    name: str = Field(description="项目或用途标识。")
    is_enabled: bool = Field(description="是否启用。")
    created_at: datetime = Field(description="创建时间。")
    updated_at: datetime = Field(description="最后更新时间。")


class CompanyCredentialIssued(BaseModel):
    company_id: UUID = Field(description="凭据所属公司 ID。")
    company_credential_id: UUID = Field(description="凭据 ID。")
    name: str = Field(description="项目或用途标识。")
    token: str = Field(repr=False, description="仅本次返回的明文 Token，请立即安全保存。")
    token_type: Literal["Bearer"] = "Bearer"


class AdminCredentialCreate(BaseModel):
    """创建管理员凭据的请求参数。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120, description="管理员标识，例如姓名或用途。")

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("管理员标识不能为空")
        return stripped


class AdminCredentialPatch(BaseModel):
    """修改管理员凭据；只提交要修改的字段。"""

    model_config = ConfigDict(extra="forbid")

    is_enabled: bool = Field(description="是否启用；停用后该 Token 对新请求立即失效。")


class AdminCredentialRead(BaseModel):
    """管理员凭据信息；不返回任何哈希或明文。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(description="凭据 ID。")
    name: str = Field(description="管理员标识。")
    is_enabled: bool = Field(description="是否启用。")
    created_at: datetime = Field(description="创建时间。")
    updated_at: datetime = Field(description="最后更新时间。")


class AdminCredentialIssued(BaseModel):
    admin_credential_id: UUID = Field(description="凭据 ID。")
    token: str = Field(repr=False, description="仅本次返回的明文 Token，请立即安全保存。")
    token_type: Literal["Bearer"] = "Bearer"


class ModelProviderPreset(BaseModel):
    """代码内置的模型厂商预设；界面选择服务商后补填模型名与密钥。"""

    key: str = Field(description="厂商键，例如 dashscope。")
    display_name: str = Field(description="界面展示的厂商名称。")
    base_url: str = Field(description="默认 API 基址。")
    embedding_path: str = Field(description="默认 Embedding 路径。")
    supports_embedding: bool = Field(description="是否提供 Embedding 能力。")
    supports_chat: bool = Field(default=False, description="是否提供 Chat 生成能力。")
    chat_path: str | None = Field(default=None, description="默认 Chat 路径；不支持时为 None。")
    known_chat_models: list[str] = Field(
        default_factory=list, description="该厂商常见生成模型名，供界面提示。"
    )
    note: str | None = Field(description="使用说明。")
    known_models: list[str] = Field(
        default_factory=list, description="该厂商常见模型名，供界面提示。"
    )


class ModelProfileWrite(ModelProfileSpec):
    """创建模型档案的请求参数。"""

    model_config = ConfigDict(extra="forbid")


class ModelProfilePatch(BaseModel):
    """修改模型档案的请求参数；只提交要修改的字段。"""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    provider: str | None = Field(default=None, min_length=1, max_length=80)
    model_name: str | None = Field(default=None, min_length=1, max_length=200)
    dimensions: int | None = None
    settings: dict[str, Any] | None = None
    secret_ref: str | None = None
    is_enabled: bool | None = None


class ModelProfileRead(BaseModel):
    """模型档案信息；不返回任何密钥内容。"""

    id: UUID = Field(description="模型档案 ID。")
    name: str = Field(description="档案名称。")
    kind: str = Field(description="模型类型：embedding、rerank 或 chat。")
    provider: str = Field(description="模型供应商。")
    model_name: str = Field(description="供应商模型名或 Endpoint ID。")
    dimensions: int | None = Field(description="Embedding 向量维度；其他类型为空。")
    settings: dict[str, Any] = Field(description="API 地址、路径与批量参数。")
    secret_ref: str | None = Field(description="环境密钥引用；库内密钥配置后优先使用。")
    is_enabled: bool = Field(description="是否启用；停用的档案不能用于新建知识库。")
    is_default: bool = Field(description="是否为同类模型默认项。")
    has_api_key: bool = Field(description="是否已配置库内 API Key；不返回密钥内容。")
    created_at: datetime = Field(description="创建时间。")
    updated_at: datetime = Field(description="最后更新时间。")


class ModelApiKeyWrite(BaseModel):
    """写入模型 API Key；只写不回读。"""

    model_config = ConfigDict(extra="forbid")

    api_key: SecretStr = Field(min_length=1, max_length=500, description="模型供应商 API Key。")

    @field_validator("api_key")
    @classmethod
    def strip_api_key(cls, value: SecretStr) -> SecretStr:
        stripped = value.get_secret_value().strip()
        if not stripped:
            raise ValueError("API Key 不能为空")
        return SecretStr(stripped)


class ModelProfileTestRead(BaseModel):
    """模型档案连通性测试结果；失败也返回 200，便于管理页直接展示原因。"""

    ok: bool = Field(description="是否连通成功。")
    kind: Literal["embedding", "rerank", "chat"] = Field(description="被测模型类型。")
    latency_ms: int = Field(description="本次调用耗时（毫秒）。")
    message: str = Field(description="成功摘要或失败原因，不含密钥。")
    error_code: str | None = Field(
        default=None, description="失败时的错误码，例如 EMBEDDING_PROVIDER_HTTP_401。"
    )


class VectorLibraryCreate(BaseModel):
    """创建公司级向量大库的请求参数；模型在此创建时选定并锁定。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=160, description="大库名称。")
    description: str | None = Field(default=None, max_length=1000, description="大库说明。")
    embedding_model_id: UUID | None = Field(
        default=None,
        description="选定的 Embedding 模型档案 ID；不传使用同类默认档案。",
    )
    rerank_model_id: UUID | None = Field(
        default=None,
        description="选定的 Rerank 模型档案 ID；不传使用同类默认档案。",
    )


class VectorLibraryRead(BaseModel):
    """公司级向量大库信息。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(description="大库 ID。")
    company_id: UUID = Field(description="所属公司 ID。")
    name: str = Field(description="大库名称。")
    description: str | None = Field(description="大库说明。")
    embedding_model_id: UUID = Field(description="锁定的 Embedding 模型 ID。")
    rerank_model_id: UUID | None = Field(description="锁定的 Rerank 模型 ID。")
    models_locked_at: datetime = Field(description="模型锁定时间。")
    status: str = Field(description="大库状态。")
    created_at: datetime = Field(description="创建时间。")
    updated_at: datetime = Field(description="更新时间。")


class KnowledgeSubLibraryCreate(BaseModel):
    """创建公司级大库下子库的请求参数；模型继承大库，分块参数可空跟随全局。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=160, description="子库名称，同一公司内不能重复。")
    description: str | None = Field(default=None, max_length=1000, description="子库说明。")
    chunk_parent_chars: int | None = Field(
        default=None, ge=1, description="父块字符上限；不传使用全局默认。"
    )
    chunk_child_chars: int | None = Field(
        default=None, ge=1, description="子块字符上限；不传使用全局默认。"
    )
    chunk_child_overlap_chars: int | None = Field(
        default=None, ge=0, description="子块重叠字符数；不传使用全局默认。"
    )


class DocumentLinkIngestionRequest(BaseModel):
    """后台文件系统通过链接提交入库的请求参数。"""

    model_config = ConfigDict(extra="forbid")

    file_id: str = Field(
        min_length=1,
        max_length=500,
        description="后台文件系统中的稳定文件 ID；同一子库内用于覆盖同一文件的新版本。",
    )
    file_url: AnyHttpUrl = Field(description="知识库服务可访问的原文件链接。")
    filename: str = Field(min_length=1, max_length=1000, description="原文件名，需带扩展名。")
    product_name: str | None = Field(default=None, max_length=200, description="产品名称。")
    model_name: str | None = Field(default=None, max_length=200, description="产品型号。")
    product_names: list[str] | None = Field(
        default=None,
        min_length=1,
        max_length=50,
        description="产品名称列表；同一文件可属于多个产品。",
    )
    model_names: list[str] | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        description="产品型号列表；同一文件可对应多个型号。",
    )
    media_type: str | None = Field(default=None, max_length=160, description="文件 MIME 类型。")
    source_directory_id: str | None = Field(
        default=None, max_length=500, description="后台目录 ID；只保存为来源元数据。"
    )
    source_parent_id: str | None = Field(
        default=None, max_length=500, description="后台上级目录或节点 ID。"
    )
    source_path: str | None = Field(
        default=None, max_length=2000, description="后台目录路径；知识库服务不据此创建子库。"
    )
    folder_id: UUID | None = Field(
        default=None,
        description="放入的知识库内目录 ID；目录必须属于同一子库，不传放入根目录。",
    )

    @field_validator("product_names", "model_names")
    @classmethod
    def normalize_product_labels(cls, values: list[str] | None) -> list[str] | None:
        """去重去空并限制单项长度，空白列表视为未提供。"""
        if values is None:
            return None
        normalized = list(dict.fromkeys(value.strip() for value in values if value.strip()))
        if not normalized:
            return None
        if any(len(value) > 200 for value in normalized):
            raise ValueError("产品名称或型号长度不能超过 200 字符")
        return normalized


class ProductOptionEntry(BaseModel):
    """目录条目对（产品名称 + 型号），供问答侧型号映射与单型号自动跳过使用。"""

    product_name: str = Field(description="产品名称。")
    model_name: str = Field(description="产品型号。")


class ProductOptionsRead(BaseModel):
    """知识库内文档实际标注的产品名称与型号，供在线问答的检索过滤下拉使用。"""

    product_names: list[str] = Field(description="去重后的产品名称，按名称排序。")
    model_names: list[str] = Field(description="去重后的产品型号，按名称排序。")
    entries: list[ProductOptionEntry] = Field(
        description="目录全部条目（产品名称 + 型号），按产品名称和型号排序。"
    )


class ProductCatalogEntryCreate(BaseModel):
    """手动登记一条产品目录条目：产品名称 + 型号。"""

    model_config = ConfigDict(extra="forbid")

    product_name: str = Field(min_length=1, max_length=200, description="产品名称。")
    model_name: str = Field(min_length=1, max_length=200, description="产品型号。")

    @field_validator("product_name", "model_name")
    @classmethod
    def strip_catalog_fields(cls, value: str) -> str:
        """去首尾空白，拒绝空白内容。"""
        stripped = value.strip()
        if not stripped:
            raise ValueError("产品名称和型号不能为空")
        return stripped


class ProductCatalogEntryRead(BaseModel):
    """产品目录条目。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    product_name: str
    model_name: str
    created_at: datetime


class ProductCatalogEntryUpdate(BaseModel):
    """修改一条产品目录条目：提交修改后的完整产品名称与型号。"""

    model_config = ConfigDict(extra="forbid")

    product_name: str = Field(min_length=1, max_length=200, description="修改后的产品名称。")
    model_name: str = Field(min_length=1, max_length=200, description="修改后的产品型号。")

    @field_validator("product_name", "model_name")
    @classmethod
    def strip_catalog_fields(cls, value: str) -> str:
        """去首尾空白，拒绝空白内容。"""
        stripped = value.strip()
        if not stripped:
            raise ValueError("产品名称与型号不能为空")
        return stripped


class ProductCatalogBatchDeleteRequest(BaseModel):
    """批量删除产品目录条目的请求参数。"""

    model_config = ConfigDict(extra="forbid")

    catalog_ids: list[UUID] = Field(
        min_length=1,
        max_length=500,
        description="待删除目录条目 ID 列表；重复 ID 会自动去重。",
    )

    @field_validator("catalog_ids")
    @classmethod
    def deduplicate_catalog_ids(cls, values: list[UUID]) -> list[UUID]:
        return list(dict.fromkeys(values))


class DocumentFolderCreate(BaseModel):
    """在知识库内创建目录。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=160, description="目录名称，同一级内不能重复。")
    parent_id: UUID | None = Field(default=None, description="上级目录 ID；不传创建在根目录。")


class DocumentFolderPatch(BaseModel):
    """重命名或移动目录；只提交要修改的字段。"""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=160, description="新名称。")
    parent_id: UUID | None = Field(
        default=None,
        description="新上级目录 ID；显式传 null 移动到根目录。不提交该字段表示不移动。",
    )


class DocumentFolderRead(BaseModel):
    """知识库内目录信息。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(description="目录唯一标识。")
    knowledge_base_id: UUID = Field(description="所属知识库 ID。")
    parent_id: UUID | None = Field(description="上级目录 ID；空表示根目录。")
    name: str = Field(description="目录名称。")
    created_at: datetime = Field(description="创建时间。")
    updated_at: datetime = Field(description="更新时间。")


class DocumentUpdate(BaseModel):
    """修改文档的组织归属；目前仅支持移动目录。"""

    model_config = ConfigDict(extra="forbid")

    folder_id: UUID | None = Field(
        description="目标目录 ID，必须属于文档所在子库；显式传 null 移动到根目录。"
    )


class BatchFileVectorDeleteRequest(BaseModel):
    """批量删除文件向量的请求参数。"""

    model_config = ConfigDict(extra="forbid")

    file_ids: list[str] = Field(
        min_length=1,
        max_length=500,
        description="后台文件 ID 列表；重复 ID 会自动去重。",
    )

    @field_validator("file_ids")
    @classmethod
    def validate_file_ids(cls, values: list[str]) -> list[str]:
        """清理文件 ID 并拒绝空字符串，避免误删整个范围。"""
        normalized = list(dict.fromkeys(value.strip() for value in values))
        if not normalized or any(not value or len(value) > 500 for value in normalized):
            raise ValueError("file_ids 中不能包含空文件 ID")
        return normalized


class BatchSubLibraryDeleteRequest(BaseModel):
    """批量删除子库的请求参数。"""

    model_config = ConfigDict(extra="forbid")

    knowledge_base_ids: list[UUID] = Field(
        min_length=1,
        max_length=100,
        description="待删除子库 ID 列表；重复 ID 会自动去重。",
    )

    @field_validator("knowledge_base_ids")
    @classmethod
    def deduplicate_ids(cls, values: list[UUID]) -> list[UUID]:
        """去除重复子库 ID，保持调用方提交顺序。"""
        return list(dict.fromkeys(values))


class BatchDocumentDeleteRequest(BaseModel):
    """批量删除文档的请求参数。"""

    model_config = ConfigDict(extra="forbid")

    document_ids: list[UUID] = Field(
        min_length=1,
        max_length=200,
        description="待删除文档 ID 列表；重复 ID 会自动去重。",
    )

    @field_validator("document_ids")
    @classmethod
    def deduplicate_ids(cls, values: list[UUID]) -> list[UUID]:
        """去除重复文档 ID，保持调用方提交顺序。"""
        return list(dict.fromkeys(values))


class BatchDeleteResponse(BaseModel):
    """批量删除结果摘要。"""

    deleted_count: int = Field(description="已删除的数量。")
    deleted_ids: list[str] = Field(description="已删除的业务 ID。")


class DocumentRead(BaseModel):
    """逻辑文档信息及其最新版本状态。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(description="逻辑文档唯一标识。")
    company_id: UUID | None = Field(description="所属公司 ID。")
    knowledge_base_id: UUID = Field(description="所属知识库 ID。")
    folder_id: UUID | None = Field(description="所在目录 ID；空表示根目录。")
    document_key: str = Field(description="调用方提供的稳定文档标识。")
    filename: str = Field(description="最新上传版本的文件名。")
    latest_version_number: int = Field(description="当前最新版本号。")
    status: str = Field(description="最新版本的处理状态。")
    error_message: str | None = Field(description="最新版本的错误摘要；成功时为空。")
    product_names: list[str] = Field(
        default_factory=list, description="文档标注的产品名称列表，供上传新版本时预填标签。"
    )
    model_names: list[str] = Field(
        default_factory=list, description="文档标注的产品型号列表，供上传新版本时预填标签。"
    )
    created_at: datetime = Field(description="首次创建时间。")
    updated_at: datetime = Field(description="最后更新时间。")


class IngestionJobRead(BaseModel):
    """离线入库任务的当前状态与安全错误摘要。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(description="离线入库任务唯一标识。")
    document_id: UUID = Field(description="逻辑文档 ID。")
    document_version_id: UUID = Field(description="本次处理的文档版本 ID。")
    status: str = Field(description="任务状态，例如 queued、running、completed 或 failed。")
    stage: str | None = Field(
        description="当前处理阶段：load、parse、chunk、embed、index 或 complete。"
    )
    attempts: int = Field(description="已执行次数。")
    error_details: dict[str, object] = Field(description="经过脱敏的错误摘要。")
    started_at: datetime | None = Field(description="任务开始时间。")
    finished_at: datetime | None = Field(description="任务结束时间。")
    created_at: datetime = Field(description="任务创建时间。")
    updated_at: datetime = Field(description="任务最后更新时间。")


class ChunkDefaultsRead(BaseModel):
    """全局分块参数默认值；管理端建库向导用它预填表单。"""

    parent_chunk_chars: int = Field(description="父块字符上限默认值。")
    child_chunk_chars: int = Field(description="子块字符上限默认值。")
    child_chunk_overlap_chars: int = Field(description="子块重叠字符数默认值。")


class ChunkPreviewChild(BaseModel):
    """切块预览中的子块。"""

    ordinal: int = Field(description="子块在父块内的序号。")
    content: str = Field(description="子块文本。")
    char_count: int = Field(description="子块字符数。")


class ChunkPreviewAsset(BaseModel):
    """切块预览中的图片资产，供前端把正文占位符渲染成图片地址。"""

    id: UUID = Field(description="资产唯一标识。")
    marker: str = Field(description="正文中的占位符。")
    media_type: str = Field(description="资产媒体类型。")
    description: str | None = Field(description="图片描述，可能为空。")
    page_number: int | None = Field(description="图片所在页码。")
    chunk_id: UUID | None = Field(description="图片所属切块。")


class ChunkPreviewParent(BaseModel):
    """切块预览中的父块及其子块。"""

    ordinal: int = Field(description="父块在文档内的序号。")
    content: str = Field(description="父块文本。")
    char_count: int = Field(description="父块字符数。")
    page_number: int | None = Field(description="父块所在页码；解析器未提供时为空。")
    section: str | None = Field(description="父块所在章节路径；解析器未识别章节时为空。")
    block_type: str = Field(description="来源块类型：表格或普通文本。")
    children: list[ChunkPreviewChild] = Field(description="该父块下的子块，按序号排列。")


class ChunkPreviewResponse(BaseModel):
    """切块预览分页结果与整体统计。"""

    total_parents: int = Field(description="该版本父块总数。")
    total_children: int = Field(description="该版本子块总数。")
    total_chars: int = Field(description="父块字符总数（不含子块，避免与父块重复计数）。")
    offset: int = Field(description="本次返回的父块起始序号。")
    limit: int = Field(description="本次请求的父块数量上限。")
    parents: list[ChunkPreviewParent] = Field(description="当前页父块及其子块。")
    assets: list[ChunkPreviewAsset] = Field(description="该版本全部图片资产，用于渲染正文占位符。")


class DocumentVersionRead(BaseModel):
    """对外展示版本与入库状态，不暴露内部对象存储键。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(description="文档版本唯一标识。")
    document_id: UUID = Field(description="所属逻辑文档 ID。")
    version_number: int = Field(description="版本序号，从 1 开始递增。")
    filename: str = Field(description="该版本的文件名。")
    media_type: str | None = Field(description="文件 MIME 类型。")
    size_bytes: int = Field(description="文件大小，单位为字节。")
    content_sha256: str = Field(description="文件内容的 SHA-256 摘要。")
    status: str = Field(description="该版本的离线入库状态。")
    is_current: bool = Field(description="是否为当前可检索版本。")
    error_message: str | None = Field(description="该版本的错误摘要；成功时为空。")
    created_at: datetime = Field(description="版本创建时间。")
    updated_at: datetime = Field(description="版本最后更新时间。")


class FaqRead(BaseModel):
    """FAQ 条目对外视图。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(description="FAQ 唯一标识。")
    knowledge_base_id: UUID = Field(description="所属知识库。")
    document_id: UUID | None = Field(description="所属文档；为空表示库级 FAQ。")
    document_version_id: UUID | None = Field(description="生成时的文档版本；文档级 FAQ 必填。")
    question: str = Field(description="问题原文。")
    answer: str = Field(description="答案原文。")
    source: str = Field(description="来源：auto 自动生成 / manual 手动添加。")
    created_at: datetime = Field(description="创建时间。")
    updated_at: datetime = Field(description="最后更新时间。")


class FaqCreate(BaseModel):
    """创建 FAQ 请求；指定 document_id 时成为文档级 FAQ。"""

    question: str = Field(min_length=1, max_length=1000, description="问题原文。")
    answer: str = Field(min_length=1, max_length=20000, description="答案原文。")
    document_id: UUID | None = Field(default=None, description="可选；指定则创建文档级 FAQ。")


class FaqUpdate(BaseModel):
    """修改 FAQ 请求；两个字段都可选，但至少传一个。"""

    question: str | None = Field(default=None, min_length=1, max_length=1000)
    answer: str | None = Field(default=None, min_length=1, max_length=20000)


class FaqRewriteRequest(BaseModel):
    """AI 改写答案请求；instruction 为空时自动润色。"""

    instruction: str | None = Field(default=None, max_length=500)


class FaqGenerateResponse(BaseModel):
    """文档 FAQ 生成结果。"""

    entries: list[FaqRead] = Field(description="本次生成的 FAQ 条目。")


class FaqBatchGenerateResponse(BaseModel):
    """批量补齐 FAQ 的入队结果。"""

    queued: int = Field(description="本次新入队生成的文档数。")
    skipped: int = Field(description="已有自动 FAQ 或已在排队的文档数。")


class FaqListResponse(BaseModel):
    """分页返回知识库的 FAQ 条目。"""

    page: int = Field(description="当前页码。")
    page_size: int = Field(description="每页条数。")
    total: int = Field(description="符合条件的条目总数。")
    items: list[FaqRead] = Field(description="FAQ 条目。")
