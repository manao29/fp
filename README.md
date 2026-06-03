# Invoicing Assistant

企业微信群聊开票助手 — 基于解耦架构的智能开票机器人。

## 架构

```
企业微信群聊 (@开票助手)
        │
┌───────▼──────────────────────────────────────┐
│  wecom_gateway/  企业微信 API 层               │
│  - 消息回调接收/解密                           │
│  - 消息发送 (文本/Markdown/模板卡片)            │
│  - 文件下载                                    │
└───────┬──────────────────────────────────────┘
        │
┌───────▼──────────────────────────────────────┐
│  agent/  Agent 智能决策层                      │
│  - 对话管理 + 上下文记忆                        │
│  - 意图分类 (LLM)                              │
│  - 发票字段提取 (LLM Structured Output)        │
│  - 回复编排 (文本/模板卡片)                     │
└───────┬──────────────────────────────────────┘
        │
┌───────▼──────────────────────────────────────┐
│  tools/  业务工具层                            │
│  - OCR (PaddleOCR)                            │
│  - Excel/PDF 解析                              │
│  - 税收编码匹配                                │
│  - 买方资料补全 (企查查)                        │
└───────┬──────────────────────────────────────┘
        │
┌───────▼──────────────────────────────────────┐
│  db/  数据层                                   │
│  - SQLite / PostgreSQL                        │
│  - 开票单 CRUD                                 │
│  - 对话历史                                    │
│  - 群配置                                      │
└──────────────────────────────────────────────┘
```

## 快速开始

### 1. 环境准备

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# OCR (可选，不需要 OCR 时跳过)
pip install paddlepaddle paddleocr
```

### 2. 配置

```bash
cp .env.example .env
# 编辑 .env，填入企业微信和 AI 凭证
```

### 3. 运行

```bash
python -m src.app
# 或
uvicorn src.app:app --host 0.0.0.0 --port 8000
```

### 4. 配置企业微信回调

在企业微信管理后台 -> 应用管理 -> 选择应用 -> 接收消息 -> 设置回调 URL:

```
https://your-domain.com/api/wecom/callback
```

## 环境变量

| 变量 | 说明 | 必填 |
|------|------|------|
| `WECOM_CORP_ID` | 企业 ID | 是 |
| `WECOM_AGENT_ID` | 应用 AgentId | 是 |
| `WECOM_APP_SECRET` | 应用 Secret | 是 |
| `WECOM_TOKEN` | 回调 Token | 是 |
| `WECOM_ENCODING_AES_KEY` | 回调 EncodingAESKey | 是 |
| `AI_API_KEY` | AI API Key | 是 |
| `AI_MODEL` | AI 模型 | 是 |
| `AI_BASE_URL` | AI API 地址 | 否 |
| `OCR_ENGINE` | OCR 引擎 (paddleocr/none) | 否 |

## 使用方式

### 群聊中开票

```
@开票助手 我要开票
公司：嘉兴持湘贸易有限公司
税号：91330401MAG18KX804
项目：技术服务费
数量：1
金额：5000
```

### 补充/修改

```
@开票助手 项目改成咨询服务费
@开票助手 金额改成3000
```

### 确认开票

```
@开票助手 确认开票
```

### 发送截图/PDF/Excel

直接在群里 @开票助手 并发送文件即可。

## 与旧版的主要区别

| 旧版 Invoice-assistant | 新版 Invoicing Assistant |
|------------------------|-------------------------|
| main.py 单体 11184 行 | 解耦分层，每层独立模块 |
| 正则堆砌提取字段 | LLM Structured Output |
| 纯文本回复 | 文本 + 模板卡片 |
| 依赖 QiWe 第三方 API | 企业微信官方 API |
| 对话上下文靠 DB flag | Agent 持有完整对话历史 |
| 0 分层，改一处可能炸全局 | 网关→Agent→工具 三层隔离 |

## 测试

```bash
pytest tests/ -v
```

## 许可证

MIT
