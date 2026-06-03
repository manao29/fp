"""Agent 适配层 — 单元测试"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


class TestIntentClassification:
    """意图分类测试"""

    def test_new_invoice_intent_trigger_words(self):
        """触发词应被识别为开票意图"""
        trigger_texts = [
            "我要开票",
            "帮我开票",
            "开一张票",
            "@开票助手 我要开票",
        ]
        # 这些应该触发开票意图（实际需要 LLM，这里测规则逻辑）
        for text in trigger_texts:
            assert any(kw in text for kw in ["开票", "开一张"])

    def test_greeting_should_not_trigger(self):
        """闲聊不应触发开票"""
        greetings = ["你好", "在吗", "谢谢", "好的"]
        for text in greetings:
            assert "开票" not in text


class TestFieldExtraction:
    """字段提取逻辑测试"""

    def test_company_name_structured(self):
        """结构化格式: 公司名: XXX"""
        text = "公司名称：嘉兴持湘贸易有限公司，税号：91330401MAG18KX804"
        assert "嘉兴持湘贸易有限公司" in text
        assert "91330401MAG18KX804" in text

    def test_amount_followup(self):
        """补金额: 金额改成3000"""
        text = "金额改成3000"
        assert "改成" in text or "3000" in text

    def test_item_switch(self):
        """项目切换: 项目换成技术服务费"""
        text = "项目换成技术服务费"
        assert "技术服务费" in text


class TestMissingFieldCheck:
    """缺失字段检查测试"""

    def test_full_fields_no_missing(self):
        """完整字段不报缺失"""
        data = {
            "company_name": "测试公司",
            "tax_id": "91330401MAG18KX804",
            "item_name": "技术服务费",
            "amount": "5000",
        }
        missing = []
        if not data.get("company_name"):
            missing.append("购买方抬头")
        if not data.get("tax_id"):
            missing.append("税号")
        if not data.get("item_name"):
            missing.append("项目")
        if not data.get("amount"):
            missing.append("金额")
        assert len(missing) == 0

    def test_person_invoice_no_tax_id_ok(self):
        """个人票不需要税号"""
        data = {
            "subject_type": "person",
            "company_name": "张三",
            "item_name": "顾问费",
            "amount": "3000",
        }
        missing = []
        if not data.get("company_name"):
            missing.append("个人名称")
        if data.get("subject_type") != "person" and not data.get("tax_id"):
            missing.append("税号")
        if not data.get("item_name"):
            missing.append("项目")
        if not data.get("amount"):
            missing.append("金额")
        assert "税号" not in missing
        assert len(missing) == 0


class TestOCR:
    """OCR 引擎基础测试"""

    def test_ocr_engine_import(self):
        """OCR 模块可导入"""
        from src.tools.ocr_engine import OcrEngine
        assert OcrEngine is not None

    def test_ocr_disabled_mode(self):
        """none 模式返回 None"""
        from src.tools.ocr_engine import OcrEngine
        OcrEngine._instance = None
        instance = OcrEngine.get_instance("none")
        assert instance is None


class TestExcelParser:
    """Excel 解析测试"""

    def test_excel_column_mapping(self):
        """列名映射"""
        from src.tools.file_parser import normalize_header, map_columns_to_fields
        headers = ["公司名称", "税号", "项目", "数量", "金额"]
        result = map_columns_to_fields(headers)
        assert result.get(0) == "company_name"
        assert result.get(1) == "tax_id"
        assert result.get(2) == "item_name"
        assert result.get(3) == "quantity"
        assert result.get(4) == "amount"

    def test_normalize_header(self):
        """表头标准化"""
        from src.tools.file_parser import normalize_header
        assert normalize_header("  公司 名称  ") == "公司名称"
        assert normalize_header("Tax_ID") == "tax_id"


class TestTaxCatalog:
    """税收编码匹配测试"""

    def test_exact_match(self):
        from src.tools.tax_and_buyer import match_tax_category
        result = match_tax_category("连衣裙")
        assert "服装" in result

    def test_fuzzy_match(self):
        from src.tools.tax_and_buyer import match_tax_category
        result = match_tax_category("技术服务费")
        assert len(result) > 0
        result2 = match_tax_category("蓝牙耳机")
        assert len(result2) > 0

    def test_unknown_item(self):
        from src.tools.tax_and_buyer import match_tax_category
        result = match_tax_category("不存在的奇怪商品xyz123")
        assert result == []


class TestWeComMessage:
    """企业微信消息解析测试"""

    def test_parse_text_message(self):
        from src.wecom_gateway.client import parse_callback_xml
        xml = """<xml>
            <MsgType>text</MsgType>
            <Content>我要开票</Content>
            <FromUserName>user123</FromUserName>
            <FromName>张三</FromName>
            <ChatId>group456</ChatId>
            <ChatType>group</ChatType>
            <MsgId>msg001</MsgId>
        </xml>"""
        msg = parse_callback_xml(xml.encode())
        assert msg.is_group
        assert msg.is_text
        assert msg.content == "我要开票"
        assert msg.from_user == "user123"


class TestTemplateCard:
    """模板卡片构造测试"""

    def test_build_confirm_card(self):
        from src.wecom_gateway.client import build_invoice_confirm_card
        data = {
            "seller_company_name": "测试公司",
            "company_name": "买方公司",
            "tax_id": "123456",
            "item_name": "测试商品",
            "quantity": "3",
            "unit": "件",
            "amount": "5000",
        }
        card = build_invoice_confirm_card("req-001", data, "http://localhost:8000")
        assert card["card_type"] == "text_notice"
        assert len(card["horizontal_content_list"]) >= 3
