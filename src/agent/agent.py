"""Agent 适配层 — 对话管理、意图路由、消息编排

这是整个系统的"大脑"，负责:
1. 接收 WeCom Gateway 传来的标准化消息
2. 加载群聊上下文和对话历史
3. 判断用户意图
4. 调用相应的业务工具（提取、解析、查询）
5. 编排回复（文本 or 模板卡片）
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.db.models import (
    InvoiceRepo, ConversationRepo, GroupConfigRepo,
    InvoiceRequest, AiDecisionLog,
)
from src.tools.invoice_parser import InvoiceExtractor
from src.tools.ocr_engine import run_ocr
from src.tools.file_parser import parse_excel_rows, parse_pdf_text
from src.tools.tax_and_buyer import match_tax_category

logger = logging.getLogger(__name__)

# 意图常量
INTENT_NEW_INVOICE = "new_invoice"
INTENT_UPDATE_INVOICE = "update_invoice"
INTENT_CONFIRM_INVOICE = "confirm_invoice"
INTENT_QUERY_STATUS = "query_status"
INTENT_BINDING_SELLER = "binding_seller"
INTENT_GREETING = "greeting"
INTENT_UNKNOWN = "unknown"


@dataclass
class AgentResponse:
    """Agent 响应"""
    reply_text: str = ""
    reply_markdown: str = ""
    use_template_card: bool = False
    template_card_data: Optional[dict] = None
    should_send: bool = True
    should_create_request: bool = False
    request_data: dict = field(default_factory=dict)
    decision_log: dict = field(default_factory=dict)


class InvoicingAgent:
    """开票助手 Agent"""

    def __init__(self):
        self.settings = get_settings()
        self.extractor = InvoiceExtractor()
        # 群聊收集状态 (内存缓存，避免过于频繁的 DB 查询)
        self._collect_states: dict[str, dict[str, Any]] = {}

    async def process_message(
        self,
        session: AsyncSession,
        msg_id: str,
        group_id: str,
        sender_id: str,
        sender_name: str,
        msg_type: str,
        content: str,
        media_path: str = "",
        file_name: str = "",
        quoted_msg_id: str = "",
        quoted_content: str = "",
    ) -> AgentResponse:
        """
        处理群聊消息的主入口

        流程:
        1. 保存对话历史
        2. 加载上下文
        3. 判断意图
        4. 路由到具体处理函数
        5. 编排回复
        """

        # 1. 保存用户消息到对话历史
        await ConversationRepo.add(session, {
            "group_id": group_id,
            "sender_id": sender_id,
            "sender_name": sender_name,
            "role": "user",
            "content": content or f"[{msg_type}: {file_name}]",
            "message_type": msg_type,
            "metadata_json": json.dumps({
                "msg_id": msg_id,
                "quoted_msg_id": quoted_msg_id,
                "quoted_content": quoted_content,
            }),
        })

        # 2. 加载群配置 + 对话历史
        group_config = await GroupConfigRepo.get(session, group_id)
        conversation = await ConversationRepo.get_recent(session, group_id, limit=20)
        recent_requests = await InvoiceRepo.find_recent(
            session, group_id, sender_id, self.settings.session_window_minutes, 5
        )

        # 构建上下文摘要
        context_str = self._build_context(
            group_config, conversation, recent_requests
        )

        # 3. 处理图片/文件消息
        if msg_type == "image" and media_path:
            return await self._handle_image(
                session, msg_id, group_id, sender_id, sender_name,
                media_path, quoted_msg_id, recent_requests, group_config
            )
        elif msg_type == "file" and media_path:
            return await self._handle_file(
                session, msg_id, group_id, sender_id, sender_name,
                media_path, file_name, recent_requests, group_config
            )

        # 4. 文本消息: 意图分类
        if not content.strip():
            return AgentResponse(reply_text="", should_send=False)

        intent_result = await self.extractor.classify_intent(
            content,
            has_active_session=bool(recent_requests),
            recent_requests=self._format_recent_requests(recent_requests),
        )
        intent = intent_result.get("intent", INTENT_UNKNOWN)

        # 5. 路由
        if intent == INTENT_GREETING:
            return await self._handle_greeting(group_config)

        elif intent == INTENT_BINDING_SELLER:
            return await self._handle_binding_seller(
                session, content, group_id, group_config
            )

        elif intent == INTENT_CONFIRM_INVOICE:
            return await self._handle_confirm(
                session, content, group_id, recent_requests
            )

        elif intent == INTENT_QUERY_STATUS:
            return await self._handle_query(session, group_id, sender_id)

        elif intent == INTENT_UPDATE_INVOICE:
            return await self._handle_update(
                session, msg_id, content, group_id, sender_id, sender_name,
                quoted_msg_id, quoted_content, recent_requests
            )

        elif intent == INTENT_NEW_INVOICE:
            return await self._handle_new_invoice(
                session, msg_id, content, group_id, sender_id, sender_name,
                quoted_content, context_str, recent_requests, group_config
            )

        else:
            # 未知意图: 如果是活跃会话中的补充信息，尝试当作 update 处理
            if recent_requests and self._looks_like_followup(content):
                return await self._handle_update(
                    session, msg_id, content, group_id, sender_id, sender_name,
                    quoted_msg_id, quoted_content, recent_requests
                )
            return AgentResponse(
                reply_text="",
                should_send=False,
                decision_log={
                    "decision_type": "intent_classify",
                    "intent": INTENT_UNKNOWN,
                    "confidence": 0.0,
                    "reason": "未识别为开票相关消息，静默跳过",
                    "decision_source": "ai",
                },
            )

    # ================================================================
    # 意图处理函数
    # ================================================================

    async def _handle_new_invoice(
        self, session, msg_id, content, group_id, sender_id, sender_name,
        quoted_content, context_str, recent_requests, group_config,
    ) -> AgentResponse:
        """处理新开票请求"""
        # 合并引用消息内容
        full_content = content
        if quoted_content:
            full_content = f"[引用消息]: {quoted_content}\n[当前消息]: {content}"

        # LLM 提取字段
        extracted = await self.extractor.extract(full_content, context_str)

        if extracted.get("confidence", 0) < 0.3:
            return AgentResponse(
                reply_text="未能从您的消息中识别到完整开票信息。请发送包含公司抬头、项目、金额的开票资料。",
                decision_log={
                    "decision_type": "field_extract",
                    "intent": INTENT_NEW_INVOICE,
                    "confidence": extracted.get("confidence", 0),
                    "reason": "提取置信度不足",
                    "decision_source": "ai",
                },
            )

        # 应用群默认销售方
        if group_config and group_config.seller_company_name:
            if not extracted.get("seller_company_name"):
                extracted["seller_company_name"] = group_config.seller_company_name
                extracted["seller_tax_id"] = group_config.seller_tax_id or ""

        # 判断是公司票还是个人票
        if not extracted.get("subject_type"):
            if extracted.get("person_name") and not extracted.get("company_name"):
                extracted["subject_type"] = "person"
                extracted["company_name"] = extracted["person_name"]
            else:
                extracted["subject_type"] = "company"

        # 匹配税编
        item = extracted.get("item_name", "")
        tax_categories = match_tax_category(item) if item else []
        if tax_categories:
            extracted["tax_category"] = tax_categories[0]

        # 填充默认值
        if not extracted.get("invoice_type"):
            extracted["invoice_type"] = group_config.default_invoice_type if group_config else "增值税电子普通发票"

        # 创建开票请求
        req_data = {
            "group_id": group_id,
            "sender_id": sender_id,
            "sender_name": sender_name,
            "message_id": msg_id,
            "input_type": "text",
            "subject_type": extracted.get("subject_type", "company"),
            "company_name": extracted.get("company_name", ""),
            "tax_id": extracted.get("tax_id", ""),
            "invoice_type": extracted.get("invoice_type", "增值税电子普通发票"),
            "item_name": item,
            "quantity": extracted.get("quantity", ""),
            "unit": extracted.get("unit", ""),
            "amount": extracted.get("amount", ""),
            "buyer_email": extracted.get("buyer_email", ""),
            "order_no": extracted.get("order_no", ""),
            "seller_company_name": extracted.get("seller_company_name", ""),
            "seller_tax_id": extracted.get("seller_tax_id", ""),
            "source_raw": content,
            "field_meta": json.dumps({"source": "ai", "confidence": extracted.get("confidence", 0)}),
            "status": "pending",
        }

        req_id = await InvoiceRepo.create(session, req_data)

        # 构建回复
        frontend_url = self.settings.effective_frontend_url
        reply_text = self._format_invoice_summary(req_data, req_id)

        # 判断是否信息完整
        missing = self._check_missing_fields(req_data)
        if missing:
            reply_text += f"\n\n还缺: {'、'.join(missing)}，请直接在群里补充。"

        reply_text += f"\n\n核对入口: {frontend_url}/submit?req_id={req_id}"

        response = AgentResponse(
            reply_text=reply_text,
            should_create_request=True,
            request_data={"id": req_id, **req_data},
            decision_log={
                "decision_type": "field_extract",
                "intent": INTENT_NEW_INVOICE,
                "confidence": extracted.get("confidence", 0),
                "reason": f"创建开票单 {req_id}",
                "decision_source": "ai",
                "invoice_request_id": req_id,
            },
        )

        # 如果群配置支持模板卡片，使用卡片
        if group_config:
            response.use_template_card = True
            response.template_card_data = self._build_confirm_card(req_id, req_data)

        return response

    async def _handle_update(
        self, session, msg_id, content, group_id, sender_id, sender_name,
        quoted_msg_id, quoted_content, recent_requests,
    ) -> AgentResponse:
        """处理修改/补充开票单"""
        if not recent_requests:
            return AgentResponse(
                reply_text="当前没有待修改的开票单。请先发送开票资料。",
                decision_log={
                    "decision_type": "merge_decision",
                    "intent": INTENT_UPDATE_INVOICE,
                    "reason": "无活跃开票单",
                },
            )

        # 确定目标开票单
        target = recent_requests[0]  # 默认最新一条

        # 如果有引用消息，尝试匹配
        if quoted_msg_id:
            for req in recent_requests:
                if req.message_id == quoted_msg_id:
                    target = req
                    break

        # LLM 提取更新字段
        full_content = content
        if quoted_content:
            full_content = f"[引用消息]: {quoted_content}\n[当前消息]: {content}"

        extracted = await self.extractor.extract(full_content)

        # 只取有值的字段进行更新
        update_fields = {}
        field_names = [
            "company_name", "tax_id", "item_name", "quantity", "unit",
            "amount", "buyer_email", "order_no", "invoice_type",
            "seller_company_name", "seller_tax_id",
        ]
        for field in field_names:
            val = extracted.get(field, "")
            if val:
                update_fields[field] = val

        # 个人票切换
        if extracted.get("subject_type") == "person":
            update_fields["subject_type"] = "person"
            if extracted.get("person_name"):
                update_fields["company_name"] = extracted["person_name"]
            update_fields["tax_id"] = ""

        if not update_fields:
            return AgentResponse(
                reply_text="未能识别到要修改的内容。请说明具体要改什么，例如：金额改成3000、项目换成技术服务费。",
                decision_log={
                    "decision_type": "merge_decision",
                    "intent": INTENT_UPDATE_INVOICE,
                    "reason": "无有效更新字段",
                },
            )

        # 执行更新
        await InvoiceRepo.update(session, target.id, update_fields)

        # 重新加载
        updated = await InvoiceRepo.get(session, target.id)

        if updated is None:
            return AgentResponse(reply_text="更新失败，开票单已不存在。")

        data = updated.to_dict()
        reply_text = f"已更新开票单 {updated.id}：\n" + self._format_invoice_summary(data, updated.id)
        missing = self._check_missing_fields(data)
        if missing:
            reply_text += f"\n\n还缺: {'、'.join(missing)}"
        else:
            reply_text += "\n\n如确认无误，请回复：确认开票"

        return AgentResponse(
            reply_text=reply_text,
            decision_log={
                "decision_type": "merge_decision",
                "intent": INTENT_UPDATE_INVOICE,
                "confidence": extracted.get("confidence", 0),
                "reason": f"更新字段: {', '.join(update_fields.keys())}",
                "decision_source": "ai",
                "target_request_id": target.id,
                "invoice_request_id": target.id,
            },
        )

    async def _handle_confirm(
        self, session, content, group_id, recent_requests,
    ) -> AgentResponse:
        """处理确认开票"""
        if not recent_requests:
            return AgentResponse(reply_text="当前没有待确认的开票单。")

        # 确认最近一条待确认的
        target = recent_requests[0]
        await InvoiceRepo.update(session, target.id, {
            "status": "confirmed",
            "execution_status": "ready_for_execute",
        })

        reply_text = f"已确认开票单 {target.id}，进入待执行队列。会计将统一处理。"

        return AgentResponse(
            reply_text=reply_text,
            decision_log={
                "decision_type": "confirm",
                "intent": INTENT_CONFIRM_INVOICE,
                "reason": f"确认开票单 {target.id}",
                "invoice_request_id": target.id,
            },
        )

    async def _handle_query(
        self, session, group_id, sender_id,
    ) -> AgentResponse:
        """查询开票状态"""
        requests = await InvoiceRepo.list_pending(session, group_id, limit=5)

        if not requests:
            return AgentResponse(reply_text="当前没有待处理的开票单。")

        lines = [f"该群共有 {len(requests)} 条待处理开票单："]
        for req in requests[:5]:
            lines.append(
                f"- {req.id[:8]}: {req.company_name or '待补充'} | "
                f"{req.item_name or '待补充'} | ¥{req.amount or '?'} | "
                f"状态: {req.status}"
            )

        return AgentResponse(reply_text="\n".join(lines))

    async def _handle_greeting(self, group_config) -> AgentResponse:
        seller = group_config.seller_company_name if group_config else ""
        if seller:
            return AgentResponse(
                reply_text=f"您好，我是本群的开票助手。当前默认销售方: {seller}。"
                           "如需开票，请直接发送开票资料（抬头、税号、项目、数量、金额）。\n"
                           "支持: 文字 / 截图 / 拍照 / PDF / Excel。"
            )
        return AgentResponse(
            reply_text="您好，我是开票助手。如需开票，请先绑定销售方公司，或者直接发送包含销售方信息的开票资料。\n"
                       "支持: 文字 / 截图 / 拍照 / PDF / Excel。"
        )

    async def _handle_binding_seller(
        self, session, content, group_id, group_config,
    ) -> AgentResponse:
        """处理销售方绑定"""
        extracted = await self.extractor.extract(content)
        seller_name = extracted.get("seller_company_name", "")

        if not seller_name:
            # 从内容中直接提取公司名
            from src.tools.invoice_parser import InvoiceExtractor
            m = re.search(r"销售方[是:：为]?\s*(.+?)(?:公司|有限公司|有限责任公司)", content)
            if m:
                seller_name = m.group(1) + "公司" if "公司" not in m.group(1) else m.group(1)

        if not seller_name:
            return AgentResponse(reply_text="未能识别到销售方公司名称。请发送完整的公司名，例如：销售方是东莞五一电子商务有限公司。")

        await GroupConfigRepo.upsert(session, group_id, {
            "seller_company_name": seller_name,
            "seller_tax_id": extracted.get("seller_tax_id", ""),
        })

        return AgentResponse(
            reply_text=f"已为本群绑定默认销售方: {seller_name}。后续开票如需切换，请说明。",
            decision_log={
                "decision_type": "binding_seller",
                "intent": INTENT_BINDING_SELLER,
                "reason": f"绑定销售方: {seller_name}",
            },
        )

    async def _handle_image(
        self, session, msg_id, group_id, sender_id, sender_name,
        media_path, quoted_msg_id, recent_requests, group_config,
    ) -> AgentResponse:
        """处理图片消息: OCR → 提取 → 创建/更新开票单"""
        # 执行 OCR
        ocr_results = run_ocr(media_path, self.settings.ocr_engine)
        ocr_text = "\n".join([text for text, conf in ocr_results if conf > 0.5])

        if not ocr_text.strip():
            return AgentResponse(
                reply_text="收到图片，但未能识别到文字内容。请尝试发送更清晰的图片，或直接发送文字信息。",
                decision_log={"decision_type": "ocr", "reason": "OCR 未识别到文字"},
            )

        # 用 LLM 从 OCR 文本中提取字段
        extracted = await self.extractor.extract(ocr_text)
        logger.info("OCR extraction: text_len=%d fields=%d", len(ocr_text),
                     sum(1 for v in extracted.values() if v))

        if not extracted.get("confidence", 0) > 0.3:
            return AgentResponse(
                reply_text=f"从图片中识别到以下文字，但未能确认开票信息:\n{ocr_text[:300]}\n\n请补充说明或发送更完整的开票资料。",
                decision_log={"decision_type": "ocr", "reason": "OCR 提取置信度不足"},
            )

        # 如果有活跃开票单，尝试更新
        if recent_requests and quoted_msg_id:
            return await self._handle_update(
                session, msg_id, ocr_text, group_id, sender_id, sender_name,
                quoted_msg_id, "", recent_requests,
            )

        # 创建新开票单
        req_data = {
            "group_id": group_id,
            "sender_id": sender_id,
            "sender_name": sender_name,
            "message_id": msg_id,
            "input_type": "image",
            "subject_type": extracted.get("subject_type", "company"),
            "company_name": extracted.get("company_name", ""),
            "tax_id": extracted.get("tax_id", ""),
            "item_name": extracted.get("item_name", ""),
            "quantity": extracted.get("quantity", ""),
            "unit": extracted.get("unit", ""),
            "amount": extracted.get("amount", ""),
            "source_raw": ocr_text,
            "field_meta": json.dumps({"source": "ocr+ai", "confidence": extracted.get("confidence", 0)}),
            "status": "pending",
        }

        # 应用群默认销售方
        if group_config and group_config.seller_company_name:
            if not req_data.get("seller_company_name"):
                req_data["seller_company_name"] = group_config.seller_company_name

        req_id = await InvoiceRepo.create(session, req_data)

        reply_text = f"已从图片中识别开票信息并创建开票单 {req_id}。\n"
        reply_text += self._format_invoice_summary(req_data, req_id)
        missing = self._check_missing_fields(req_data)
        if missing:
            reply_text += f"\n\n还缺: {'、'.join(missing)}，请直接在群里补充。"

        return AgentResponse(
            reply_text=reply_text,
            should_create_request=True,
            request_data={"id": req_id, **req_data},
            decision_log={
                "decision_type": "ocr",
                "intent": INTENT_NEW_INVOICE,
                "confidence": extracted.get("confidence", 0),
                "reason": f"OCR 识别创建开票单 {req_id}",
                "invoice_request_id": req_id,
            },
        )

    async def _handle_file(
        self, session, msg_id, group_id, sender_id, sender_name,
        media_path, file_name, recent_requests, group_config,
    ) -> AgentResponse:
        """处理文件消息: Excel / PDF"""
        fn_lower = file_name.lower()

        if fn_lower.endswith((".xlsx", ".xls")):
            return await self._handle_excel_file(
                session, group_id, sender_id, sender_name, msg_id,
                media_path, group_config
            )
        elif fn_lower.endswith(".pdf"):
            return await self._handle_pdf_file(
                session, group_id, sender_id, sender_name, msg_id,
                media_path, recent_requests, group_config
            )
        else:
            return AgentResponse(
                reply_text=f"收到文件 {file_name}。当前支持 Excel 和 PDF 格式的开票资料。"
            )

    async def _handle_excel_file(
        self, session, group_id, sender_id, sender_name, msg_id, file_path, group_config,
    ) -> AgentResponse:
        """处理 Excel 批量开票"""
        rows = parse_excel_rows(file_path)
        if not rows:
            return AgentResponse(reply_text="未能从 Excel 中识别到开票资料。请检查表格格式。")

        created_ids: list[str] = []
        for row in rows:
            row["group_id"] = group_id
            row["sender_id"] = sender_id
            row["sender_name"] = sender_name
            row["message_id"] = msg_id
            if group_config and group_config.seller_company_name:
                if not row.get("seller_company_name"):
                    row["seller_company_name"] = group_config.seller_company_name
            req_id = await InvoiceRepo.create(session, row)
            created_ids.append(req_id)

        reply_text = f"已从 Excel 中识别 {len(created_ids)} 条开票记录:\n"
        for i, rid in enumerate(created_ids[:10]):
            reply_text += f"  {i+1}. {rid[:8]} - {rows[i].get('company_name', '')} / {rows[i].get('item_name', '')} / ¥{rows[i].get('amount', '')}\n"
        if len(created_ids) > 10:
            reply_text += f"  ...等共 {len(created_ids)} 条\n"

        frontend_url = self.settings.effective_frontend_url
        batch_id = rows[0].get("batch_id", "") if rows else ""
        reply_text += f"\n批次预览: {frontend_url}/batch-review?batch_id={batch_id}"

        return AgentResponse(
            reply_text=reply_text,
            decision_log={
                "decision_type": "excel_parse",
                "reason": f"创建 {len(created_ids)} 条开票单",
                "invoice_request_id": created_ids[0] if created_ids else "",
            },
        )

    async def _handle_pdf_file(
        self, session, group_id, sender_id, sender_name, msg_id,
        file_path, recent_requests, group_config,
    ) -> AgentResponse:
        """处理 PDF 开票资料"""
        pdf_text = parse_pdf_text(file_path)
        if not pdf_text.strip():
            return AgentResponse(reply_text="收到 PDF，但未能提取到文字内容。请确认 PDF 包含可选择的文本。")

        extracted = await self.extractor.extract(pdf_text)

        if extracted.get("confidence", 0) < 0.3:
            return AgentResponse(
                reply_text=f"从 PDF 提取到文字，但未能确认完整开票信息。\n\n提取内容:\n{pdf_text[:400]}",
            )

        req_data = {
            "group_id": group_id,
            "sender_id": sender_id,
            "sender_name": sender_name,
            "message_id": msg_id,
            "input_type": "pdf",
            "subject_type": extracted.get("subject_type", "company"),
            "company_name": extracted.get("company_name", ""),
            "tax_id": extracted.get("tax_id", ""),
            "item_name": extracted.get("item_name", ""),
            "quantity": extracted.get("quantity", ""),
            "unit": extracted.get("unit", ""),
            "amount": extracted.get("amount", ""),
            "source_raw": pdf_text,
            "field_meta": json.dumps({"source": "pdf+ai"}),
            "status": "pending",
        }

        if group_config and group_config.seller_company_name:
            req_data["seller_company_name"] = group_config.seller_company_name

        req_id = await InvoiceRepo.create(session, req_data)

        reply_text = f"已从 PDF 中提取开票信息，创建开票单 {req_id}。\n"
        reply_text += self._format_invoice_summary(req_data, req_id)

        return AgentResponse(
            reply_text=reply_text,
            should_create_request=True,
            request_data={"id": req_id, **req_data},
            decision_log={
                "decision_type": "pdf_parse",
                "reason": f"PDF 解析创建开票单 {req_id}",
                "invoice_request_id": req_id,
            },
        )

    # ================================================================
    # 辅助方法
    # ================================================================

    def _build_context(
        self,
        group_config,
        conversation: list,
        recent_requests: list,
    ) -> str:
        """构建 Agent 上下文摘要"""
        parts: list[str] = []

        if group_config:
            parts.append(f"群默认销售方: {group_config.seller_company_name}")
            parts.append(f"销售方税号: {group_config.seller_tax_id}")

        if recent_requests:
            parts.append(f"当前活跃开票单数: {len(recent_requests)}")
            for req in recent_requests[:3]:
                parts.append(
                    f"  开票单 {req.id[:8]}: 买方={req.company_name or '?'} "
                    f"项目={req.item_name or '?'} 金额={req.amount or '?'} 状态={req.status}"
                )

        # 最近 5 条对话
        recent_msgs = [m for m in conversation[-10:]]
        if recent_msgs:
            parts.append("最近对话:")
            for m in recent_msgs[-5:]:
                role_label = "用户" if m.role == "user" else "助手"
                content_preview = (m.content or "")[:100]
                parts.append(f"  [{role_label}] {content_preview}")

        return "\n".join(parts)

    def _format_recent_requests(self, requests: list) -> str:
        if not requests:
            return "无"
        items = []
        for req in requests[:5]:
            items.append(
                f"{req.id[:8]}: {req.company_name or '?'} | "
                f"{req.item_name or '?'} | ¥{req.amount or '?'} | "
                f"{req.status}"
            )
        return "\n".join(items)

    def _format_invoice_summary(self, data: dict, req_id: str) -> str:
        """格式化开票摘要文本"""
        is_person = data.get("subject_type") == "person"
        title_label = "个人名称" if is_person else "购买方抬头"
        tax_display = "个人无需提供" if is_person else (data.get("tax_id") or "待补充")

        lines = [
            f"销售方: {data.get('seller_company_name') or '待绑定'}",
            f"{title_label}: {data.get('company_name') or '待补充'}",
            f"税号: {tax_display}",
            f"项目: {data.get('item_name') or '待补充'}",
            f"数量: {data.get('quantity', '')} {data.get('unit', '')}".strip() or "数量: 待补充",
            f"总金额(含税): ¥{data.get('amount') or '待补充'}",
            f"发票类型: {data.get('invoice_type') or '增值税电子普通发票'}",
        ]
        return "\n".join(lines)

    def _check_missing_fields(self, data: dict) -> list[str]:
        """检查必填字段"""
        is_person = data.get("subject_type") == "person"
        missing = []

        if not data.get("company_name"):
            missing.append("个人名称" if is_person else "购买方抬头")
        if not is_person and not data.get("tax_id"):
            missing.append("税号")
        if not data.get("item_name"):
            missing.append("项目")
        if not data.get("amount"):
            missing.append("金额")

        return missing

    def _looks_like_followup(self, content: str) -> bool:
        """启发式判断是否像补充信息"""
        followup_patterns = [
            r"(改成|换成|改为|变更为|更新为|修改为)",
            r"\d+(?:\.\d+)?元?$",
            r"^[A-Za-z0-9]{15,20}$",  # 税号
            r"公司是|抬头是|项目是|税号是|金额是",
        ]
        return any(re.search(p, content) for p in followup_patterns)

    def _build_confirm_card(self, req_id: str, data: dict) -> dict:
        """构造模板卡片"""
        from src.wecom_gateway.client import build_invoice_confirm_card
        return build_invoice_confirm_card(
            req_id, data, self.settings.effective_frontend_url
        )
