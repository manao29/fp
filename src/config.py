"""配置管理 — 集中管理所有环境变量和应用配置"""
from __future__ import annotations

import os
from pathlib import Path
from functools import lru_cache

from pydantic_settings import BaseSettings


BASE_DIR = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    """应用配置，所有值从环境变量或 .env 文件读取"""

    # ========== 服务器 ==========
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False
    log_level: str = "INFO"

    # ========== 企业微信官方应用 ==========
    wecom_corp_id: str = ""          # 企业 ID
    wecom_agent_id: str = ""         # 应用 AgentId
    wecom_agent_secret: str = ""     # 应用 Secret (获取 access_token)
    wecom_token: str = ""            # 回调验证 Token
    wecom_encoding_aes_key: str = "" # 回调加解密 Key
    wecom_callback_secret: str = ""  # 管理接口访问密钥 (调试用)

    # ========== 数据库 ==========
    db_url: str = ""                 # 为空则用 SQLite
    db_path: str = ""                # SQLite 路径

    @property
    def effective_db_path(self) -> str:
        if self.db_url:
            return self.db_url
        path = self.db_path or str(BASE_DIR / "data" / "invoice.db")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite+aiosqlite:///{path}"

    # ========== 文件存储 ==========
    upload_dir: str = ""             # 上传文件目录

    @property
    def effective_upload_dir(self) -> str:
        path = self.upload_dir or str(BASE_DIR / "data" / "uploads")
        Path(path).mkdir(parents=True, exist_ok=True)
        return path

    # ========== OCR ==========
    ocr_engine: str = "paddleocr"   # paddleocr | none

    # ========== AI 模型 ==========
    ai_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    ai_api_key: str = ""
    ai_model: str = "qwen-plus"     # 主模型: qwen-plus / qwen-max / deepseek-chat
    ai_embedding_model: str = ""    # 可选: embedding 模型
    ai_timeout: int = 30
    ai_max_tokens: int = 2048

    # 意图分类用小模型 (省钱)
    ai_intent_model: str = "qwen-turbo"
    ai_intent_timeout: int = 10

    # ========== 前端 ==========
    frontend_url: str = ""  # 前端部署地址

    @property
    def effective_frontend_url(self) -> str:
        return self.frontend_url or f"http://localhost:{self.port}"

    # ========== 税务模板 ==========
    tax_template_path: str = ""
    tax_catalog_path: str = ""

    @property
    def effective_tax_template(self) -> str:
        return self.tax_template_path or str(
            BASE_DIR / "templates" / "(V251101版)批量开票-导入开票模板.xlsx"
        )

    @property
    def effective_tax_catalog(self) -> str:
        return self.tax_catalog_path or str(
            BASE_DIR / "templates" / "商品和服务税收分类编码表.xls"
        )

    # ========== 会话管理 ==========
    session_window_minutes: int = 180      # 活跃会话窗口 (分钟)
    collect_delay_seconds: float = 3.0     # 收集延迟 (汇总多条消息后统一回复)
    auto_confirm_minutes: int = 30         # 自动确认时间

    # ========== 企查查 ==========
    qichacha_api_key: str = ""

    # ========== 推导属性 ==========
    @property
    def wecom_ready(self) -> bool:
        return bool(self.wecom_corp_id and self.wecom_agent_secret and self.wecom_agent_id)

    @property
    def callback_ready(self) -> bool:
        return bool(self.wecom_corp_id and self.wecom_token and self.wecom_encoding_aes_key)

    @property
    def ai_ready(self) -> bool:
        return bool(self.ai_api_key and self.ai_model)

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


@lru_cache()
def get_settings() -> Settings:
    return Settings()
