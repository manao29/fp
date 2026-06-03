"""税收编码匹配 + 买方资料补全工具"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ── 税收分类编码 ────────────────────────────────────────────

# 常见商品服务→税收分类编码简称映射（可扩展）
TAX_CATALOG_ALIASES: dict[str, tuple[str, ...]] = {
    # 服装鞋帽
    "连衣裙": ("服装",),
    "半身裙": ("服装",),
    "长裙": ("服装",),
    "短裙": ("服装",),
    "裤子": ("服装",),
    "短裤": ("服装",),
    "长裤": ("服装",),
    "外套": ("服装",),
    "上衣": ("服装",),
    "打底衫": ("服装",),
    "衬衣": ("服装",),
    "衬衫": ("服装",),
    "T恤": ("服装",),
    "t恤": ("服装",),
    "毛衣": ("服装",),
    "卫衣": ("服装",),
    "家居服": ("服装",),
    "睡衣": ("服装",),
    "袜子": ("服装",),
    "帽子": ("服装",),
    "围巾": ("服装",),
    # 服务类
    "服务费": ("现代服务",),
    "技术服务费": ("研发和技术服务",),
    "咨询服务费": ("咨询服务",),
    "设计服务费": ("设计服务",),
    "软件开发费": ("软件开发服务",),
    "平台服务费": ("信息技术服务",),
    "培训费": ("教育服务",),
    "租赁费": ("租赁服务",),
    # 电子产品
    "手机": ("通信设备",),
    "电脑": ("计算机",),
    "蓝牙耳机": ("通信设备",),
    "耳机": ("通信设备",),
    "充电器": ("通信设备",),
    # 日用百货
    "纸巾": ("纸制品",),
    "洗发水": ("日用化学产品",),
    "洗衣液": ("日用化学产品",),
    # 食品
    "茶叶": ("食品",),
    "水果": ("农产品",),
    "大米": ("农产品",),
    # 宠物
    "宠物猫": ("宠物",),
    "宠物狗": ("宠物",),
    "猫粮": ("宠物食品",),
    "狗粮": ("宠物食品",),
    # 家具
    "桌子": ("家具",),
    "椅子": ("家具",),
    "沙发": ("家具",),
    "床": ("家具",),
}


def match_tax_category(item_name: str) -> list[str]:
    """根据商品名称匹配税收分类编码简称"""
    if not item_name:
        return []
    item = item_name.strip().lower()

    # 精确匹配
    if item in TAX_CATALOG_ALIASES:
        return list(TAX_CATALOG_ALIASES[item])

    # 模糊匹配：包含关键词
    for key, categories in TAX_CATALOG_ALIASES.items():
        if key in item or item in key:
            return list(categories)

    return []


# ── 买方资料补全 ─────────────────────────────────────────────

async def lookup_buyer_profile(
    company_name: str,
    qichacha_api_key: str = "",
) -> dict[str, str]:
    """
    查找买方公司资料
    优先级: 本地缓存 > 企查查 API（如果配置了）
    """
    result: dict[str, str] = {}

    # TODO: 实现本地 buyer_profiles 表查询
    # 这里先返回空，由上层调用方通过 DB 查询

    return result


async def lookup_buyer_from_qichacha(
    company_name: str,
    api_key: str,
) -> dict[str, str]:
    """
    通过企查查 API 补全买方公司资料（税号、地址、电话等）

    注意: 需要有效的企查查 API Key
    """
    if not api_key or not company_name:
        return {}

    try:
        import httpx

        # 企查查搜索接口（示例，实际需根据企查查文档调整）
        url = "https://api.qichacha.com/CompanySearch/Search"
        headers = {"Authorization": f"Token {api_key}"}
        params = {"key": company_name}

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers=headers, params=params)
            data = resp.json()

        if data.get("Status") == "200" and data.get("Result"):
            items = data["Result"].get("Items", [])
            if items:
                company = items[0]
                return {
                    "tax_id": company.get("CreditCode", ""),
                    "address": company.get("Address", ""),
                    "phone": company.get("Phone", ""),
                }
    except Exception as e:
        logger.warning("企查查查询失败: %s", e)

    return {}
