"""API 路由 — 企业微信回调 + 后台管理 + 导出接口"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import (
    APIRouter, Request, BackgroundTasks, HTTPException, Query, Form,
    UploadFile, File, Depends,
)
from fastapi.responses import PlainTextResponse, JSONResponse, HTMLResponse, FileResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.db.models import get_db, InvoiceRepo, ConversationRepo, GroupConfigRepo
from src.wecom_gateway.client import (
    WeComCrypto, WeComClient, parse_callback_xml,
)
from src.agent.agent import InvoicingAgent

logger = logging.getLogger(__name__)
router = APIRouter()

# 单例
_agent: Optional[InvoicingAgent] = None
_wecom_client: Optional[WeComClient] = None


def get_agent() -> InvoicingAgent:
    global _agent
    if _agent is None:
        _agent = InvoicingAgent()
    return _agent


def get_wecom_client() -> WeComClient:
    global _wecom_client
    if _wecom_client is None:
        _wecom_client = WeComClient()
    return _wecom_client


# ====================================================================
# 企业微信回调
# ====================================================================

@router.get("/api/wecom/callback")
async def verify_wecom_url(
    msg_signature: str = Query(...),
    timestamp: str = Query(...),
    nonce: str = Query(...),
    echostr: str = Query(...),
):
    """企业微信回调 URL 验证 (GET)"""
    settings = get_settings()
    if not settings.callback_ready:
        raise HTTPException(503, "WeCom callback not configured")

    crypto = WeComCrypto(
        settings.wecom_token,
        settings.wecom_encoding_aes_key,
        settings.wecom_corp_id,
    )

    if not crypto.verify_signature(msg_signature, timestamp, nonce, echostr):
        raise HTTPException(403, "Signature verification failed")

    decrypted = crypto.decrypt(echostr)
    return PlainTextResponse(decrypted)


@router.post("/api/wecom/callback")
async def receive_wecom_message(
    request: Request,
    bg: BackgroundTasks,
    msg_signature: str = Query(...),
    timestamp: str = Query(...),
    nonce: str = Query(...),
):
    """接收企业微信回调消息 (POST)"""
    settings = get_settings()
    crypto = WeComCrypto(
        settings.wecom_token,
        settings.wecom_encoding_aes_key,
        settings.wecom_corp_id,
    )

    body = await request.body()

    # 解密
    try:
        decrypted = crypto.decrypt(body.decode("utf-8") if isinstance(body, bytes) else body)
    except Exception as e:
        logger.error("WeCom decrypt failed: %s", e)
        raise HTTPException(400, "Decryption failed")

    # 解析消息
    msg = parse_callback_xml(decrypted.encode("utf-8") if isinstance(decrypted, str) else decrypted)
    logger.info(
        "WeCom msg: id=%s type=%s chat=%s user=%s content=%s",
        msg.msg_id, msg.msg_type, msg.chat_id, msg.from_user, msg.content[:100] if msg.content else ""
    )

    # 非群聊消息忽略 (如果是 1v1 聊天也处理)
    if msg.is_group and msg.chat_id:
        pass
    elif msg.chat_type == "single" and msg.from_user:
        pass  # 1v1 也处理（可选，先支持群聊）
    else:
        return PlainTextResponse("")

    # 异步处理
    bg.add_task(_process_wecom_message, msg)

    return PlainTextResponse("")


async def _process_wecom_message(msg):
    """后台处理企业微信消息"""
    agent = get_agent()
    settings = get_settings()
    wecom = get_wecom_client()

    # 下载媒体文件 (如果是图片/文件)
    media_path = ""
    if msg.is_image and msg.pic_url:
        media_path = await _download_media(msg.pic_url, settings.effective_upload_dir)
    elif msg.is_file and msg.media_id:
        try:
            content = await wecom.download_media(msg.media_id)
            ext = Path(msg.file_name).suffix if msg.file_name else ".bin"
            media_path = str(Path(settings.effective_upload_dir) / f"{uuid.uuid4().hex}{ext}")
            Path(media_path).write_bytes(content)
        except Exception as e:
            logger.warning("下载文件失败: %s", e)

    # 调用 Agent
    try:
        async for session in get_db():
            async with session:
                response = await agent.process_message(
                    session=session,
                    msg_id=msg.msg_id,
                    group_id=msg.chat_id,
                    sender_id=msg.from_user,
                    sender_name=msg.from_name,
                    msg_type=msg.msg_type,
                    content=msg.content,
                    media_path=media_path,
                    file_name=msg.file_name,
                    quoted_msg_id=msg.quoted_msg_id,
                    quoted_content=msg.quoted_content,
                )

                # 保存助手回复到对话历史
                if response.reply_text and response.should_send:
                    await ConversationRepo.add(session, {
                        "group_id": msg.chat_id,
                        "sender_id": "assistant",
                        "sender_name": "开票助手",
                        "role": "assistant",
                        "content": response.reply_text,
                        "message_type": "text",
                    })

                # 记录 AI 决策日志
                if response.decision_log:
                    from src.db.models import AiDecisionLog
                    log = AiDecisionLog(
                        channel="wecom_group",
                        message_id=msg.msg_id,
                        decision_type=response.decision_log.get("decision_type", ""),
                        intent=response.decision_log.get("intent", ""),
                        confidence=response.decision_log.get("confidence", 0),
                        decision_source=response.decision_log.get("decision_source", "ai"),
                        reason=response.decision_log.get("reason", ""),
                        target_request_id=response.decision_log.get("target_request_id"),
                        invoice_request_id=response.decision_log.get("invoice_request_id"),
                        raw_json=json.dumps(response.decision_log),
                        model=settings.ai_model,
                    )
                    session.add(log)

            # 发送回复
            if response.reply_text and response.should_send:
                # 模板卡片优先
                if response.use_template_card and response.template_card_data:
                    await wecom.send_template_card(
                        response.template_card_data, msg.chat_id
                    )
                else:
                    await wecom.send_text(response.reply_text, chat_id=msg.chat_id)

    except Exception as e:
        logger.error("Agent process failed: %s", e, exc_info=True)
        try:
            await wecom.send_text(
                "处理您的消息时遇到异常，请稍后重试或联系管理员。",
                chat_id=msg.chat_id,
            )
        except Exception:
            pass


async def _download_media(url: str, upload_dir: str) -> str:
    """从 URL 下载媒体文件"""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            ext = ".jpg"
            content_type = resp.headers.get("content-type", "")
            if "png" in content_type:
                ext = ".png"
            path = str(Path(upload_dir) / f"{uuid.uuid4().hex}{ext}")
            Path(path).write_bytes(resp.content)
            return path
    except Exception as e:
        logger.warning("媒体下载失败: %s", e)
        return ""


# ====================================================================
# 健康检查
# ====================================================================

@router.get("/healthz")
async def healthz():
    settings = get_settings()
    return {
        "ok": True,
        "timestamp": datetime.utcnow().isoformat(),
        "wecom_ready": settings.wecom_ready,
        "callback_ready": settings.callback_ready,
        "ai_ready": settings.ai_ready,
        "ai_model": settings.ai_model,
        "ocr_engine": settings.ocr_engine,
    }


# ====================================================================
# 开票单 API
# ====================================================================

@router.get("/api/invoice-requests")
async def list_invoice_requests(
    group_id: str = "",
    status: str = "",
    limit: int = Query(100, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """列出开票单"""
    from sqlalchemy import select
    from src.db.models import InvoiceRequest

    stmt = select(InvoiceRequest)
    if group_id:
        stmt = stmt.where(InvoiceRequest.group_id == group_id)
    if status:
        stmt = stmt.where(InvoiceRequest.status == status)
    stmt = stmt.order_by(InvoiceRequest.created_at.desc()).offset(offset).limit(limit)

    result = await db.execute(stmt)
    requests = result.scalars().all()
    return [r.to_dict() for r in requests]


@router.get("/api/invoice-requests/{req_id}")
async def get_invoice_request(req_id: str, db: AsyncSession = Depends(get_db)):
    """获取单张开票单"""
    req = await InvoiceRepo.get(db, req_id)
    if req is None:
        raise HTTPException(404, "Invoice request not found")
    return req.to_dict()


@router.post("/api/invoice-requests/{req_id}/update")
async def update_invoice_request(
    req_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """更新开票单"""
    data = await request.json()
    updated = await InvoiceRepo.update(db, req_id, data)
    if updated is None:
        raise HTTPException(404, "Invoice request not found")
    return updated.to_dict()


# ====================================================================
# 导出
# ====================================================================

@router.get("/api/export-excel")
async def export_excel(
    status: str = "",
    group_id: str = "",
    db: AsyncSession = Depends(get_db),
):
    """导出开票单为 Excel 模板"""
    from sqlalchemy import select
    from src.db.models import InvoiceRequest

    stmt = select(InvoiceRequest)
    if status:
        stmt = stmt.where(InvoiceRequest.status == status)
    if group_id:
        stmt = stmt.where(InvoiceRequest.group_id == group_id)
    stmt = stmt.order_by(InvoiceRequest.created_at.desc())

    result = await db.execute(stmt)
    requests = result.scalars().all()

    if not requests:
        raise HTTPException(404, "No invoice requests to export")

    # 生成 Excel
    output_path = await _generate_export_excel([r.to_dict() for r in requests])

    return FileResponse(
        output_path,
        filename=f"开票导出_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


async def _generate_export_excel(requests: list[dict]) -> str:
    """生成导出 Excel"""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "开票明细"

    # 表头
    headers = [
        "序号", "发票类型", "购买方抬头", "税号", "身份证号",
        "地址", "电话", "开户行", "银行账号",
        "销售方公司", "销售方税号",
        "项目", "数量", "单位", "金额", "税率",
        "邮箱", "订单号", "备注", "状态", "创建时间",
    ]
    ws.append(headers)

    for i, req in enumerate(requests, 1):
        ws.append([
            i,
            req.get("invoice_type", ""),
            req.get("company_name", ""),
            req.get("tax_id", ""),
            req.get("id_card_no", ""),
            req.get("address", ""),
            req.get("phone", ""),
            req.get("bank_name", ""),
            req.get("bank_account", ""),
            req.get("seller_company_name", ""),
            req.get("seller_tax_id", ""),
            req.get("item_name", ""),
            req.get("quantity", ""),
            req.get("unit", ""),
            req.get("amount", ""),
            req.get("tax_rate", ""),
            req.get("buyer_email", ""),
            req.get("order_no", ""),
            req.get("remark", ""),
            req.get("status", ""),
            req.get("created_at", ""),
        ])

    output_dir = Path(get_settings().effective_upload_dir) / "exports"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"export_{datetime.now().strftime('%Y%m%d%H%M%S')}.xlsx"
    wb.save(str(output_path))

    return str(output_path)


# ====================================================================
# 群配置 API
# ====================================================================

@router.get("/api/group-config/{group_id}")
async def get_group_config(group_id: str, db: AsyncSession = Depends(get_db)):
    """获取群配置"""
    config = await GroupConfigRepo.get(db, group_id)
    if config is None:
        return {"exists": False}
    return {"exists": True, "group_id": config.group_id, "group_name": config.group_name,
            "seller_company_name": config.seller_company_name, "seller_tax_id": config.seller_tax_id,
            "seller_products": config.seller_products}


@router.post("/api/group-config/{group_id}")
async def save_group_config(
    group_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """保存群配置"""
    data = await request.json()
    config = await GroupConfigRepo.upsert(db, group_id, data)
    return {"ok": True, "group_id": config.group_id}


# ====================================================================
# 上传 OCR (用于网页端)
# ====================================================================

@router.post("/api/ocr/upload")
async def ocr_upload(file: UploadFile = File(...)):
    """网页端上传图片进行 OCR 识别"""
    settings = get_settings()
    upload_dir = Path(settings.effective_upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)

    suffix = Path(file.filename or "upload.jpg").suffix
    file_path = upload_dir / f"ocr_{uuid.uuid4().hex}{suffix}"

    content = await file.read()
    file_path.write_bytes(content)

    from src.tools.ocr_engine import run_ocr
    results = run_ocr(str(file_path), settings.ocr_engine)

    return {
        "text": "\n".join([text for text, conf in results]),
        "items": [{"text": text, "confidence": conf} for text, conf in results],
    }


# ====================================================================
# 前端页面
# ====================================================================

@router.get("/submit")
async def submit_page(req_id: str = ""):
    """商户核对页"""
    settings = get_settings()
    frontend_dir = Path(__file__).resolve().parents[1] / "frontend" / "merchant"
    html_path = frontend_dir / "index.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse(f"<h1>核对页</h1><p>req_id={req_id}</p>")


@router.get("/admin")
async def admin_page():
    """管理后台"""
    frontend_dir = Path(__file__).resolve().parents[1] / "frontend" / "admin"
    html_path = frontend_dir / "index.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>管理后台</h1><p>功能开发中</p>")


@router.get("/batch-review")
async def batch_review_page(batch_id: str = ""):
    """批次预览页"""
    return HTMLResponse(f"<h1>批次预览</h1><p>batch_id={batch_id}</p>")
