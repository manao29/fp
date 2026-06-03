"""数据库层 — SQLAlchemy 异步引擎 + 表定义 + Repository"""

from __future__ import annotations

import datetime
import json
import uuid
from typing import AsyncGenerator, Optional

from sqlalchemy import (
    Column, String, Text, Integer, Float, DateTime, Boolean, JSON, create_engine, event,
)
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from src.config import get_settings


class Base(DeclarativeBase):
    pass


# ====================================================================
# 表定义
# ====================================================================

class InvoiceRequest(Base):
    """开票请求主表"""
    __tablename__ = "invoice_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # 来源
    group_id: Mapped[Optional[str]] = mapped_column(String(100), index=True, default=None)
    sender_id: Mapped[Optional[str]] = mapped_column(String(100), index=True, default=None)
    sender_name: Mapped[Optional[str]] = mapped_column(String(100), default=None)
    message_id: Mapped[Optional[str]] = mapped_column(String(100), default=None)
    quoted_message_id: Mapped[Optional[str]] = mapped_column(String(100), default=None)
    input_type: Mapped[str] = mapped_column(String(20), default="text")  # text/image/pdf/excel

    # 买方信息
    subject_type: Mapped[str] = mapped_column(String(10), default="company")  # company/person
    company_name: Mapped[str] = mapped_column(String(200), default="")
    tax_id: Mapped[str] = mapped_column(String(50), default="")
    id_card_no: Mapped[str] = mapped_column(String(20), default="")
    address: Mapped[str] = mapped_column(String(300), default="")
    phone: Mapped[str] = mapped_column(String(30), default="")
    bank_name: Mapped[str] = mapped_column(String(200), default="")
    bank_account: Mapped[str] = mapped_column(String(50), default="")

    # 卖方信息
    seller_company_name: Mapped[str] = mapped_column(String(200), default="")
    seller_tax_id: Mapped[str] = mapped_column(String(50), default="")

    # 发票明细
    invoice_type: Mapped[str] = mapped_column(String(50), default="增值税电子普通发票")
    item_name: Mapped[str] = mapped_column(String(200), default="")
    quantity: Mapped[str] = mapped_column(String(20), default="")
    unit: Mapped[str] = mapped_column(String(20), default="")
    amount: Mapped[str] = mapped_column(String(20), default="")
    tax_rate: Mapped[str] = mapped_column(String(20), default="")

    # 其他
    order_no: Mapped[str] = mapped_column(String(100), default="")
    order_date: Mapped[str] = mapped_column(String(30), default="")
    buyer_email: Mapped[str] = mapped_column(String(100), default="")
    remark: Mapped[str] = mapped_column(String(500), default="")
    source_raw: Mapped[str] = mapped_column(Text, default="")       # 原始消息/OCR 文本
    field_meta: Mapped[str] = mapped_column(Text, default="{}")     # 字段来源+置信度 JSON

    # 批次
    batch_id: Mapped[Optional[str]] = mapped_column(String(36), index=True, default=None)
    batch_index: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    batch_total: Mapped[Optional[int]] = mapped_column(Integer, default=None)

    # 状态
    status: Mapped[str] = mapped_column(String(20), index=True, default="pending")
    execution_status: Mapped[str] = mapped_column(String(20), default="")

    # 会计
    accountant_id: Mapped[Optional[str]] = mapped_column(String(100), default=None)
    accountant_name: Mapped[Optional[str]] = mapped_column(String(100), default=None)

    # 时间戳
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow,
                                                          onupdate=datetime.datetime.utcnow)

    def to_dict(self) -> dict:
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}


class ConversationMessage(Base):
    """对话历史 — 用于 Agent 上下文记忆"""
    __tablename__ = "conversation_messages"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    group_id: Mapped[str] = mapped_column(String(100), index=True)
    sender_id: Mapped[str] = mapped_column(String(100))
    sender_name: Mapped[str] = mapped_column(String(100), default="")
    role: Mapped[str] = mapped_column(String(20))      # user / assistant / system
    content: Mapped[str] = mapped_column(Text)
    message_type: Mapped[str] = mapped_column(String(20), default="text")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow)


class GroupConfig(Base):
    """群聊配置 — 销售方绑定 + 规则"""
    __tablename__ = "group_configs"

    group_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    group_name: Mapped[str] = mapped_column(String(200), default="")
    seller_company_name: Mapped[str] = mapped_column(String(200), default="")
    seller_tax_id: Mapped[str] = mapped_column(String(50), default="")
    seller_products: Mapped[str] = mapped_column(Text, default="")    # 分号分隔的商品列表
    default_invoice_type: Mapped[str] = mapped_column(String(50), default="")
    high_risk_mode: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow)


class BuyerProfile(Base):
    """买方档案 — 缓存的买方公司资料"""
    __tablename__ = "buyer_profiles"

    company_name: Mapped[str] = mapped_column(String(200), primary_key=True)
    tax_id: Mapped[str] = mapped_column(String(50), default="")
    address: Mapped[str] = mapped_column(String(300), default="")
    phone: Mapped[str] = mapped_column(String(30), default="")
    bank_name: Mapped[str] = mapped_column(String(200), default="")
    bank_account: Mapped[str] = mapped_column(String(50), default="")
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow)


class AiDecisionLog(Base):
    """AI 决策日志 — 用于排查 AI 判断"""
    __tablename__ = "ai_decision_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    channel: Mapped[str] = mapped_column(String(20))           # wecom_group
    message_id: Mapped[Optional[str]] = mapped_column(String(100), default=None)
    decision_type: Mapped[str] = mapped_column(String(50))     # intent_classify / field_extract / merge_decision
    intent: Mapped[str] = mapped_column(String(50), default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    decision_source: Mapped[str] = mapped_column(String(20), default="ai")  # ai / heuristic
    reason: Mapped[str] = mapped_column(Text, default="")
    target_request_id: Mapped[Optional[str]] = mapped_column(String(36), default=None)
    invoice_request_id: Mapped[Optional[str]] = mapped_column(String(36), default=None)
    raw_json: Mapped[str] = mapped_column(Text, default="{}")
    model: Mapped[str] = mapped_column(String(100), default="")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow)


# ====================================================================
# 引擎 + 会话工厂
# ====================================================================

_async_engine = None
_async_session_factory = None


async def get_engine():
    global _async_engine
    if _async_engine is None:
        settings = get_settings()
        _async_engine = create_async_engine(
            settings.effective_db_path,
            echo=settings.debug,
            pool_size=5,
            max_overflow=10,
        )
        # 自动建表
        async with _async_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    return _async_engine


async def get_session_factory():
    global _async_session_factory
    if _async_session_factory is None:
        engine = await get_engine()
        _async_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return _async_session_factory


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    factory = await get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# ====================================================================
# Repository 封装
# ====================================================================

class InvoiceRepo:
    """开票请求 Repository"""

    @staticmethod
    async def create(session: AsyncSession, data: dict) -> str:
        req = InvoiceRequest(**data)
        session.add(req)
        await session.flush()
        return req.id

    @staticmethod
    async def get(session: AsyncSession, req_id: str) -> Optional[InvoiceRequest]:
        return await session.get(InvoiceRequest, req_id)

    @staticmethod
    async def update(session: AsyncSession, req_id: str, data: dict) -> Optional[InvoiceRequest]:
        req = await session.get(InvoiceRequest, req_id)
        if req is None:
            return None
        data["updated_at"] = datetime.datetime.utcnow()
        for key, value in data.items():
            if hasattr(req, key):
                setattr(req, key, value)
        return req

    @staticmethod
    async def find_recent(
        session: AsyncSession,
        group_id: str,
        sender_id: str = "",
        window_minutes: int = 180,
        limit: int = 5,
    ) -> list[InvoiceRequest]:
        from sqlalchemy import select, and_, text

        stmt = select(InvoiceRequest).where(
            and_(
                InvoiceRequest.group_id == group_id,
                InvoiceRequest.status == "pending",
                InvoiceRequest.updated_at >= text(f"datetime('now', '-{window_minutes} minutes')"),
            )
        )
        if sender_id:
            stmt = stmt.where(InvoiceRequest.sender_id == sender_id)
        stmt = stmt.order_by(InvoiceRequest.updated_at.desc()).limit(limit)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    @staticmethod
    async def list_by_batch(session: AsyncSession, batch_id: str) -> list[InvoiceRequest]:
        from sqlalchemy import select

        stmt = select(InvoiceRequest).where(
            InvoiceRequest.batch_id == batch_id
        ).order_by(InvoiceRequest.batch_index)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    @staticmethod
    async def list_pending(session: AsyncSession, group_id: str = "", limit: int = 100) -> list[InvoiceRequest]:
        from sqlalchemy import select

        stmt = select(InvoiceRequest).where(InvoiceRequest.status == "pending")
        if group_id:
            stmt = stmt.where(InvoiceRequest.group_id == group_id)
        stmt = stmt.order_by(InvoiceRequest.created_at.desc()).limit(limit)
        result = await session.execute(stmt)
        return list(result.scalars().all())


class ConversationRepo:
    """对话历史 Repository"""

    @staticmethod
    async def add(session: AsyncSession, data: dict) -> int:
        msg = ConversationMessage(**data)
        session.add(msg)
        await session.flush()
        return msg.id

    @staticmethod
    async def get_recent(
        session: AsyncSession, group_id: str, limit: int = 20
    ) -> list[ConversationMessage]:
        from sqlalchemy import select

        stmt = (
            select(ConversationMessage)
            .where(ConversationMessage.group_id == group_id)
            .order_by(ConversationMessage.created_at.desc())
            .limit(limit)
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())[::-1]  # 最旧的在前


class GroupConfigRepo:
    """群配置 Repository"""

    @staticmethod
    async def get(session: AsyncSession, group_id: str) -> Optional[GroupConfig]:
        return await session.get(GroupConfig, group_id)

    @staticmethod
    async def upsert(session: AsyncSession, group_id: str, data: dict) -> GroupConfig:
        existing = await session.get(GroupConfig, group_id)
        if existing:
            for k, v in data.items():
                if hasattr(existing, k):
                    setattr(existing, k, v)
            existing.updated_at = datetime.datetime.utcnow()
        else:
            existing = GroupConfig(group_id=group_id, **data)
            session.add(existing)
        await session.flush()
        return existing
