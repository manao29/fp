"""OCR 引擎 — PaddleOCR 封装 + 远程 Qwen NER 回退"""
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class OcrEngine:
    """OCR 引擎：优先 PaddleOCR，回退 Tesseract，可配置为 none"""

    _instance: Optional[object] = None
    _initialized: bool = False

    @classmethod
    def get_instance(cls, engine: str = "paddleocr"):
        """懒加载 OCR 实例"""
        if cls._instance is not None:
            return cls._instance

        if engine == "none":
            return None

        if engine == "paddleocr":
            try:
                cls._init_paddleocr()
            except Exception as e:
                logger.warning("PaddleOCR 初始化失败: %s，尝试 Tesseract 回退", e)
                cls._instance = cls._init_tesseract()
        else:
            cls._instance = cls._init_tesseract()

        return cls._instance

    @classmethod
    def _init_paddleocr(cls) -> object:
        from paddleocr import PaddleOCR

        os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")

        init_options = [
            {"lang": "ch", "ocr_version": "PP-OCRv4"},
            {"lang": "ch"},
        ]
        last_error = None
        for kwargs in init_options:
            try:
                cls._instance = PaddleOCR(**kwargs)
                logger.info("PaddleOCR 初始化成功")
                cls._initialized = True
                return cls._instance
            except Exception as e:
                last_error = e

        raise last_error or RuntimeError("PaddleOCR 初始化失败")

    @classmethod
    def _init_tesseract(cls) -> Optional[object]:
        """Tesseract 回退 (不是真正的实例，只是一个标记)"""
        if subprocess.run(["which", "tesseract"], capture_output=True).returncode == 0:
            logger.info("使用 Tesseract 作为 OCR 引擎")
            return True  # sentinel
        logger.warning("Tesseract 未安装")
        return None


def run_ocr(image_path: str, engine: str = "paddleocr") -> list[tuple[str, float]]:
    """
    对图片执行 OCR，返回 [(文本, 置信度), ...]

    优先级: PaddleOCR > Tesseract > 空列表
    """
    ocr = OcrEngine.get_instance(engine)

    if ocr is None or ocr is True:
        # Tesseract sentinel
        return _run_tesseract(image_path)

    # PaddleOCR
    try:
        # 尝试新版 API (predict)
        if hasattr(ocr, "predict"):
            results = ocr.predict(image_path)
            items: list[tuple[str, float]] = []
            for page in results or []:
                data = getattr(page, "json", None)
                if isinstance(data, dict):
                    data = data.get("res", data)
                if not isinstance(data, dict):
                    continue
                texts = data.get("rec_texts") or []
                scores = data.get("rec_scores") or []
                for idx, text in enumerate(texts):
                    conf = scores[idx] if idx < len(scores) else 0.0
                    items.append((str(text), float(conf or 0)))
            return items

        # 旧版 API (ocr)
        result = ocr.ocr(image_path, cls=True)
        if not result or not result[0]:
            return []
        return [(line[1][0], float(line[1][1])) for line in result[0]]

    except Exception as e:
        logger.warning("PaddleOCR 识别失败: %s", e)
        return _run_tesseract(image_path)


def _run_tesseract(image_path: str) -> list[tuple[str, float]]:
    """Tesseract OCR"""
    try:
        cmd = ["tesseract", image_path, "stdout", "-l", "chi_sim+eng", "--psm", "6"]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            logger.warning("Tesseract 失败: %s", (proc.stderr or "")[:200])
            return []
        return [(line.strip(), 0.70) for line in proc.stdout.splitlines() if line.strip()]
    except FileNotFoundError:
        return []
    except Exception as e:
        logger.warning("Tesseract 异常: %s", e)
        return []
