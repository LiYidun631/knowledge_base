# 知识库线上 OCR 配置

知识库默认调用阿里云百炼的通义千问视觉模型进行 OCR 和版面分析；也可在库内默认 OCR 档案中切换千帆同步 PaddleOCR 或百度官方异步 PaddleOCR-VL。可提取的原生文字继续由本机解析器处理。`auto` 版面模式只分析没有文字层的扫描页，`all` 模式才会分析每一页。`test_agent` 的体检报告 OCR 使用本地 PaddleOCR，两条 OCR 路径相互独立。

## 环境配置

在项目 `.env` 中填写：

```dotenv
KNOWLEDGE_SERVICE_OCR_SECRET_REF="dashscope"
KNOWLEDGE_SERVICE_OCR_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1"
KNOWLEDGE_SERVICE_OCR_CHAT_PATH="/chat/completions"
KNOWLEDGE_SERVICE_OCR_MODEL_NAME="qwen-vl-plus"
KNOWLEDGE_SERVICE_OCR_TIMEOUT_SECONDS=180
KNOWLEDGE_SERVICE_OCR_RENDER_DPI=160
KNOWLEDGE_SERVICE_OCR_LAYOUT_ANALYSIS_ENABLED=true
KNOWLEDGE_SERVICE_OCR_LAYOUT_ANALYSIS_MODE="auto"
KNOWLEDGE_SERVICE_MODEL_API_KEYS={"dashscope":"填写百炼 API Key","bigmodel":"","volcengine":"","deepseek":"","qianfan":""}
```

模型需要支持百炼的 OpenAI 兼容 Chat Completions 接口及图片输入（`image_url`，Base64 data URL）。如果控制台给出的服务地址、接口路径或模型名称与示例不同，请按百炼控制台信息修改对应配置。百炼 API Key 复用 `KNOWLEDGE_SERVICE_MODEL_API_KEYS` 中的 `dashscope`，不使用 `OCR_API_KEY`，也不需要为知识库单独搭建 OCR HTTP 服务。

## OCR 模式选择

`KNOWLEDGE_SERVICE_OCR_MODE`（库内默认 OCR 档案则读 `settings.ocr_mode`）可选：

- `auto`：模型名含 `paddleocr` 时走千帆同步 `/ocr/paddleocr`，否则走 OpenAI 兼容视觉对话。
- `vision`：始终走 OpenAI 兼容视觉对话接口。
- `paddleocr`：始终走千帆同步 PaddleOCR 专用接口；PDF 按不超过 10 页分批。
- `baidu_async`：走百度智能云官方异步任务接口（PaddleOCR-VL）；PDF 整份（或选中页组成的子文档）提交，轮询下载结果，支持跨页表格合并。

### 百度官方异步接口

管理页新建 OCR 档案时选择「百度智能云 OCR（异步）」预设，API Key 填写文字识别控制台「应用列表」中的 `API Key:Secret Key`（英文冒号连接，不是 IAM 安全认证里的 Access Key）。也可把这两个值放到环境变量 `BAIDU_OCR_AK` / `BAIDU_OCR_SK`，或通过档案的 `settings.async_ak_secret_ref` / `settings.async_sk_secret_ref` 指向其他环境引用。异步模式固定使用 AIP 基址 `https://aip.baidubce.com`。

调用流程：`POST /oauth/2.0/token` 用 AK/SK 换取 access_token（进程内缓存至过期前 5 分钟）→ `POST /rest/2.0/brain/online/v2/paddle-vl-parser/task` 提交 Base64 文件 → 每 5 秒查询 `/task/query` → 成功后立即下载 `parse_result_url`。百度返回的结果链接 30 天内有效，适配器不会保存链接。

限制：图片不超过 10 MB 且最长边不超过 8192 像素；单次流式提交不超过 50 MB；PDF 不超过 500 页（超出按 500 页分批）。任务默认最多等待 900 秒。版面选项默认开启 `merge_tables`、`relevel_titles`，关闭 `analysis_chart`、`recognize_seal`；可在档案 `settings.async_options` 中覆盖，例如 `{"merge_tables": false, "analysis_chart": true}`。

## 请求格式

```http
POST https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions
Authorization: Bearer <百炼 API Key>
Content-Type: application/json
```

请求体采用 OpenAI 兼容的视觉消息格式：

```json
{
  "model": "qwen-vl-plus",
  "messages": [
    {
      "role": "user",
      "content": [
        {"type": "text", "text": "请完整识别图片中的文字，只输出识别结果。"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
      ]
    }
  ],
  "temperature": 0
}
```

客户端从 `choices[0].message.content` 读取识别文字。多页 PDF 按页依次请求，按原始页码合并结果；`auto` 模式只发送没有原生文字的页面，`all` 模式发送全部页面。请求费用和数据处理遵循百炼账号的服务条款与隐私设置。

## 注意事项

- `.env.example` 只包含空密钥占位，不要把真实 API Key 提交到代码库。
- 未填写模型名称或百炼密钥时，普通文本 PDF 仍可入库；需要 OCR 的扫描页会给出明确配置错误。
- 提高渲染 DPI 可能改善小字识别，但会增大图片和请求耗时；默认 160 DPI，可按文档情况调整。
- 修改 `.env` 后重启应用。
