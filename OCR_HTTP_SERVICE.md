# 知识库线上 OCR 配置（阿里云百炼）

知识库默认调用阿里云百炼的通义千问视觉模型进行 OCR 和版面分析；可提取的原生文字继续由本机解析器处理。`auto` 模式只分析没有文字层的扫描页，`all` 模式才会分析每一页。PDF 页面会先在本机渲染成 PNG，再发送给百炼。`test_agent` 的体检报告 OCR 使用本地 PaddleOCR，两条 OCR 路径相互独立。

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
