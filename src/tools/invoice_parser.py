"""LLM 驱动的发票信息结构化提取 — 替代正则方案"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from openai import AsyncOpenAI

from src.config import get_settings

logger = logging.getLogger(__name__)

# ── Prompt 模板 ──────────────────────────────────────────────

INVOICE_EXTRACTION_PROMPT = """你是一个发票信息提取助手。从用户消息中提取开票相关信息，输出 JSON。

## 提取规则

1. **发票类型** (invoice_type): 增值税电子普通发票 / 增值税专用发票 / 增值税普通发票
   - 用户说"专票"→"增值税专用发票"，"普票"→"增值税普通发票"，未提及则默认"增值税电子普通发票"

2. **开票对象** (subject_type): "company" 或 "person"
   - 用户说"个人""个人票""开个人"→"person"
   - 用户提公司全称或"公司票"→"company"
   - 未明确时根据是否有完整公司名判断

3. **公司名称** (company_name): 购买方完整公司名
   - 必须包含"有限公司""有限责任公司""股份有限公司"等完整后缀，或者是个体工商户全称
   - 示例: "嘉兴持湘贸易有限公司"、"东莞五一电子商务有限公司"

4. **个人名称** (person_name): 购买方个人姓名（subject_type="person"时提取）
   - 2-4个汉字的自然人姓名

5. **税号** (tax_id): 15-20位数字或字母+数字组合
   - 示例: "91330401MAG18KX804"、"91441900MA55ABCD1X"

6. **项目名称** (item_name): 商品或服务名称
   - 示例: "技术服务费"、"连衣裙"、"帽子"、"咨询服务费"、"蓝牙耳机"
   - 注意排除噪声词如"正在咨询中""请稍等"

7. **数量** (quantity): 正整数或小数
   - 示例: "1"、"3"、"5"、"0.5"

8. **单位** (unit): 个/件/条/套/次/项/顶/双/台/张/批/箱
   - 中文单位，常见如: "件"、"条"、"顶"、"双"

9. **金额** (amount): 含税总金额，纯数字不含"元""¥"
   - 示例: "5000"、"128"、"99.9"

10. **邮箱** (buyer_email): 用于接收电子发票的邮箱

11. **订单号** (order_no): 订单编号

12. **销售方公司** (seller_company_name): 卖方的公司全名
    - 用户说"销售方用XX公司开""换XX公司开"时提取

13. **是否为引用修改** (is_quote_update): true/false
    - 如果消息包含"这条改成""这张改成""第二张改成"等引用修改表述，设为 true
    - 如果消息包含明确的修改目标字段如"金额改成""项目换成""抬头改成"，也设为 true
    - 修改目标 (update_target): "latest"（最新一条）/ "index:N"（第N条）/ "id:ABC"（指定ID）

14. **缺失字段提示** (missing_hint): 列出这条消息自身未包含的必要字段

## 输出格式（严格 JSON）：

```json
{
  "invoice_type": "增值税电子普通发票",
  "subject_type": "company",
  "company_name": "",
  "person_name": "",
  "tax_id": "",
  "item_name": "",
  "quantity": "",
  "unit": "",
  "amount": "",
  "buyer_email": "",
  "order_no": "",
  "seller_company_name": "",
  "is_quote_update": false,
  "update_target": "",
  "update_fields": {},
  "confidence": 0.0,
  "notes": ""
}
```

## 用户消息:
{user_message}

## 上下文（如有）:
{context}

请仅输出 JSON，不要添加任何解释。"""


# ── 提取器 ─────────────────────────────────────────────────

class InvoiceExtractor:
    """基于 LLM 的发票信息提取器"""

    def __init__(self):
        settings = get_settings()
        self.client = AsyncOpenAI(
            api_key=settings.ai_api_key,
            base_url=settings.ai_base_url,
            timeout=settings.ai_timeout,
        )
        self.model = settings.ai_model
        self.intent_model = settings.ai_intent_model

    async def extract(
        self,
        user_message: str,
        context: str = "",
    ) -> dict[str, Any]:
        """从用户消息中提取发票字段"""
        prompt = INVOICE_EXTRACTION_PROMPT.format(
            user_message=user_message,
            context=context or "无",
        )

        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=1024,
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content or "{}"
            result = json.loads(content)
            logger.info("LLM extraction: fields=%d confidence=%.2f",
                         sum(1 for v in result.values() if v),
                         result.get("confidence", 0))
            return result

        except Exception as e:
            logger.error("LLM extraction failed: %s", e)
            return {"confidence": 0, "notes": f"提取失败: {e}"}

    async def classify_intent(
        self,
        user_message: str,
        has_active_session: bool = False,
        recent_requests: str = "",
    ) -> dict[str, Any]:
        """分类用户意图"""
        prompt = f"""你是一个消息分类器。分析用户在群聊中的消息，判断意图。

当前群聊状态: {'有活跃开票会话' if has_active_session else '无活跃开票会话'}

最近的待处理开票单:
{recent_requests or '无'}

## 意图类别

1. **new_invoice** — 用户想开具新发票
   - 包含开票触发词: "开票""帮我开票""要开票""开一张票"
   - 或直接发送了包含公司名/项目/金额的完整开票信息

2. **update_invoice** — 修改/补充已有开票单
   - "金额改成""项目换成""公司的抬头改了""第二张帮我加"
   - 引用了之前的某条消息并说修改
   - 补字段: "税号是xxx"

3. **confirm_invoice** — 确认开票
   - "确认开票""确认""没问题，开吧"

4. **query_status** — 查询开票状态
   - "我那几张票开好了吗""查一下进度"

5. **binding_seller** — 绑定/切换销售方
   - "销售方是xx公司""帮我把销售方改成xx"

6. **greeting** — 问候/闲聊
   - "你好""在吗""谢谢"

7. **unknown** — 无法判断，默认为无关消息

用户消息: {user_message}

输出 JSON:
{{"intent": "new_invoice", "confidence": 0.0, "reason": ""}}"""

        try:
            response = await self.client.chat.completions.create(
                model=self.intent_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=256,
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content or "{}"
            return json.loads(content)
        except Exception as e:
            logger.error("Intent classification failed: %s", e)
            return {"intent": "unknown", "confidence": 0, "reason": str(e)}

    async def generate_reply(
        self,
        user_message: str,
        context: str,
        reply_type: str = "confirm",
    ) -> str:
        """基于上下文生成自然语言回复"""
        prompt = f"""你是一个企业微信群聊中的开票助手。根据上下文生成一条简洁、专业的回复。

上下文:
{context}

用户消息: {user_message}
回复类型: {reply_type}

要求:
- 语气专业但不生硬
- 如果信息不全，友好地提示缺少哪些信息
- 如果有核对链接，用纯文本形式给出
- 控制在 200 字以内
- 不要用 emoji

请直接输出回复文本:"""

        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=400,
            )
            return response.choices[0].message.content or ""
        except Exception as e:
            logger.error("Reply generation failed: %s", e)
            return "收到您的消息，正在处理中。"
