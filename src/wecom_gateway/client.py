"""企业微信 Gateway 层 — 消息接收、发送、加解密、文件下载"""

from __future__ import annotations

import base64
import json
import logging
import struct
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx
import xmltodict
from Crypto.Cipher import AES

from src.config import get_settings

logger = logging.getLogger(__name__)

WECOM_API_BASE = "https://qyapi.weixin.qq.com"


# ====================================================================
# 消息加解密 (企业微信官方回调规范)
# ====================================================================

class WeComCrypto:
    """企业微信回调消息加解密"""

    def __init__(self, token: str, encoding_aes_key: str, corp_id: str):
        self.token = token
        self.corp_id = corp_id
        self.aes_key = base64.b64decode(encoding_aes_key + "=")

    def verify_signature(self, msg_signature: str, timestamp: str, nonce: str, echostr: str) -> bool:
        """验证回调 URL 签名"""
        import hashlib
        params = sorted([self.token, timestamp, nonce, echostr])
        sign = hashlib.sha1("".join(params).encode()).hexdigest()
        return sign == msg_signature

    def decrypt(self, encrypted: str) -> str:
        """解密消息"""
        cipher = AES.new(self.aes_key, AES.MODE_CBC, self.aes_key[:16])
        raw = base64.b64decode(encrypted)
        plain = cipher.decrypt(raw)
        # 去除 PKCS7 padding
        pad = plain[-1]
        plain = plain[:-pad]
        # 解析: 16字节随机数 + 4字节网络序长度 + 明文 + corp_id
        content_len = struct.unpack("!I", plain[16:20])[0]
        xml_content = plain[20:20 + content_len].decode("utf-8")
        received_corp_id = plain[20 + content_len:].decode("utf-8")
        if received_corp_id != self.corp_id:
            logger.warning("CorpId mismatch: expected=%s received=%s", self.corp_id, received_corp_id)
        return xml_content

    def encrypt(self, plain_text: str) -> tuple[str, str]:
        """加密回复消息，返回 (encrypted, signature)"""
        import hashlib
        random_bytes = base64.b64decode("a" * 16)[:16]  # 简化: 实际应该随机
        plain_bytes = plain_text.encode("utf-8")
        corp_id_bytes = self.corp_id.encode("utf-8")
        content = random_bytes + struct.pack("!I", len(plain_bytes)) + plain_bytes + corp_id_bytes
        # PKCS7 padding
        pad = 32 - len(content) % 32
        content += bytes([pad] * pad)

        cipher = AES.new(self.aes_key, AES.MODE_CBC, self.aes_key[:16])
        encrypted = base64.b64encode(cipher.encrypt(content)).decode()

        timestamp = str(int(time.time()))
        nonce = hashlib.md5(str(time.time()).encode()).hexdigest()[:10]
        params = sorted([self.token, timestamp, nonce, encrypted])
        signature = hashlib.sha1("".join(params).encode()).hexdigest()
        return encrypted, signature


# ====================================================================
# 消息数据模型
# ====================================================================

@dataclass
class WeComMessage:
    """企业微信回调消息"""
    msg_id: str = ""
    msg_type: str = ""          # text / image / file / event
    content: str = ""           # 文本内容
    from_user: str = ""         # 发送者 UserID
    from_name: str = ""         # 发送者名称
    chat_id: str = ""           # 群聊 ID (群聊场景)
    chat_type: str = ""         # single / group
    agent_id: str = ""
    create_time: int = 0
    # 图片/文件
    pic_url: str = ""
    media_id: str = ""
    file_name: str = ""
    # 引用消息
    quoted_msg_id: str = ""
    quoted_content: str = ""
    # 事件
    event: str = ""
    event_key: str = ""
    # 原始 XML/JSON
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_group(self) -> bool:
        return self.chat_type == "group"

    @property
    def is_text(self) -> bool:
        return self.msg_type == "text" and bool(self.content.strip())

    @property
    def is_image(self) -> bool:
        return self.msg_type == "image"

    @property
    def is_file(self) -> bool:
        return self.msg_type == "file"

    @property
    def is_event(self) -> bool:
        return self.msg_type == "event"

    @property
    def has_quote(self) -> bool:
        return bool(self.quoted_msg_id or self.quoted_content)


# ====================================================================
# 消息解析器
# ====================================================================

def parse_callback_xml(xml_body: bytes) -> WeComMessage:
    """将企业微信回调 XML 解析为 WeComMessage"""
    try:
        data = xmltodict.parse(xml_body)
        root = data.get("xml", data)
    except Exception:
        return WeComMessage()

    msg_type = str(root.get("MsgType", "")).strip()
    chat_type = str(root.get("ChatType", "")).strip()

    msg = WeComMessage(
        msg_id=str(root.get("MsgId", "")),
        msg_type=msg_type,
        content=str(root.get("Content", "")),
        from_user=str(root.get("FromUserName", "")),
        from_name=str(root.get("FromName", "")),
        chat_id=str(root.get("ChatId", "")),
        chat_type=chat_type,
        agent_id=str(root.get("AgentID", "")),
        create_time=int(root.get("CreateTime", 0) or 0),
        pic_url=str(root.get("PicUrl", "")),
        media_id=str(root.get("MediaId", "")),
        file_name=str(root.get("FileName", "")),
        event=str(root.get("Event", "")),
        event_key=str(root.get("EventKey", "")),
        raw=root,
    )

    # 解析引用消息
    quote = root.get("Quote")
    if isinstance(quote, dict):
        msg.quoted_msg_id = str(quote.get("MsgId", ""))
        msg.quoted_content = str(quote.get("Content", ""))

    return msg


# ====================================================================
# API 客户端
# ====================================================================

class WeComClient:
    """企业微信官方 API 客户端"""

    def __init__(self):
        self._access_token: str = ""
        self._token_expires_at: float = 0

    @property
    def settings(self):
        return get_settings()

    async def _get_access_token(self) -> str:
        """获取并缓存 access_token"""
        if self._access_token and time.time() < self._token_expires_at - 60:
            return self._access_token

        url = f"{WECOM_API_BASE}/cgi-bin/gettoken"
        params = {
            "corpid": self.settings.wecom_corp_id,
            "corpsecret": self.settings.wecom_agent_secret,
        }
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, params=params)
            data = resp.json()

        if data.get("errcode") != 0:
            raise RuntimeError(f"获取 access_token 失败: {data}")

        self._access_token = data["access_token"]
        self._token_expires_at = time.time() + data.get("expires_in", 7200)
        return self._access_token

    async def _post(self, path: str, payload: dict) -> dict:
        token = await self._get_access_token()
        url = f"{WECOM_API_BASE}{path}?access_token={token}"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json=payload)
            data = resp.json()
        if data.get("errcode") != 0:
            logger.warning("WeCom API error: %s %s", path, data)
        return data

    async def _get(self, path: str, params: dict = None) -> dict:
        token = await self._get_access_token()
        url = f"{WECOM_API_BASE}{path}"
        all_params = {"access_token": token, **(params or {})}
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, params=all_params)
            data = resp.json()
        if data.get("errcode") != 0:
            logger.warning("WeCom API error: %s %s", path, data)
        return data

    # ---------- 消息发送 ----------

    async def send_text(self, content: str, chat_id: str = "", user_id: str = "") -> dict:
        """发送文本消息到群聊或用户"""
        payload: dict = {
            "msgtype": "text",
            "agentid": int(self.settings.wecom_agent_id),
            "text": {"content": content},
            "safe": 0,
        }
        if chat_id:
            payload["touser"] = chat_id
            path = "/cgi-bin/message/send"
        elif user_id:
            payload["touser"] = user_id
            path = "/cgi-bin/message/send"
        else:
            return {"errcode": -1, "errmsg": "chat_id or user_id required"}
        return await self._post(path, payload)

    async def send_markdown(self, content: str, chat_id: str = "") -> dict:
        """发送 Markdown 消息"""
        payload = {
            "msgtype": "markdown",
            "agentid": int(self.settings.wecom_agent_id),
            "touser": chat_id,
            "markdown": {"content": content},
        }
        return await self._post("/cgi-bin/message/send", payload)

    async def send_template_card(self, card: dict, chat_id: str) -> dict:
        """发送模板卡片消息"""
        payload = {
            "msgtype": "template_card",
            "agentid": int(self.settings.wecom_agent_id),
            "touser": chat_id,
            "template_card": card,
        }
        return await self._post("/cgi-bin/message/send", payload)

    async def send_image(self, media_id: str, chat_id: str) -> dict:
        """发送图片"""
        payload = {
            "msgtype": "image",
            "agentid": int(self.settings.wecom_agent_id),
            "touser": chat_id,
            "image": {"media_id": media_id},
        }
        return await self._post("/cgi-bin/message/send", payload)

    async def send_file(self, media_id: str, chat_id: str) -> dict:
        """发送文件"""
        payload = {
            "msgtype": "file",
            "agentid": int(self.settings.wecom_agent_id),
            "touser": chat_id,
            "file": {"media_id": media_id},
        }
        return await self._post("/cgi-bin/message/send", payload)

    # ---------- 文件下载 ----------

    async def download_media(self, media_id: str) -> bytes:
        """下载临时素材 (图片/文件/语音)"""
        token = await self._get_access_token()
        url = f"{WECOM_API_BASE}/cgi-bin/media/get"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, params={"access_token": token, "media_id": media_id})
            resp.raise_for_status()
            return resp.content

    async def upload_media(self, file_path: str, media_type: str = "file") -> str:
        """上传临时素材，返回 media_id"""
        token = await self._get_access_token()
        url = f"{WECOM_API_BASE}/cgi-bin/media/upload"
        async with httpx.AsyncClient(timeout=30) as client:
            with open(file_path, "rb") as f:
                files = {"media": (file_path.split("/")[-1], f)}
                resp = await client.post(
                    url,
                    params={"access_token": token, "type": media_type},
                    files=files,
                )
                data = resp.json()
        if data.get("errcode") != 0:
            raise RuntimeError(f"上传素材失败: {data}")
        return data["media_id"]


# ====================================================================
# 模板卡片构造器
# ====================================================================

def build_invoice_confirm_card(
    req_id: str,
    data: dict,
    frontend_url: str,
) -> dict:
    """构造开票确认模板卡片"""
    subject_label = "个人名称" if data.get("subject_type") == "person" else "购买方抬头"
    tax_display = "个人无需提供" if data.get("subject_type") == "person" else (data.get("tax_id") or "待补充")

    return {
        "card_type": "text_notice",
        "source": {
            "icon_url": f"{frontend_url}/static/icon.png",
            "desc": "开票助手",
        },
        "main_title": {
            "title": f"开票单 {req_id}",
            "desc": "请核对以下开票信息",
        },
        "emphasis_content": {
            "title": f"¥{data.get('amount', '0')}",
            "desc": "总金额（含税）",
        },
        "horizontal_content_list": [
            {"keyname": "销售方", "value": data.get("seller_company_name", "待确认")},
            {"keyname": subject_label, "value": data.get("company_name", "待补充")},
            {"keyname": "税号", "value": tax_display},
            {"keyname": "项目", "value": data.get("item_name", "待补充")},
            {"keyname": "数量", "value": f"{data.get('quantity', '')} {data.get('unit', '')}".strip() or "待补充"},
        ],
        "card_action": {
            "type": 1,
            "url": f"{frontend_url}/submit?req_id={req_id}",
        },
        "task_id": req_id,
    }
