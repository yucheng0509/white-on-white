"""設定與成本閘門。

成本閘門的設計原則：在「建立 run」的時候就估算並擋下，
不要跑到一半才發現超支。
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = PROJECT_ROOT / os.getenv("DRILL_DB", "data/drill.db")

# 工具用模型：紅隊樣本生成、擬真素材生成、風險對應、報告生成
GENERATOR_MODEL: str = os.getenv("GENERATOR_MODEL", "gemini-2.5-pro")

# 受測模型（實驗對象）。ChatGPT 無 API 金鑰，走網頁版人工測試並錄影，不在此列。
# 此清單經 2026-09-21 實測可呼叫；gemini-2.5-pro 與 2.5-flash-lite 已對新帳號下架。
# 刻意橫跨三個世代（2.5 / 3.1 / 3.5 / 3.8），才能回答「新版模型是否比較抗操控」。
AVAILABLE_TARGETS: tuple[str, ...] = (
    "gemini-2.5-flash",
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash-lite",
    "gemini-3.5-flash",
    "gemini-3.8-flash",
)
TARGET_MODELS: tuple[str, ...] = tuple(
    m.strip()
    for m in os.getenv("TARGET_MODELS", ",".join(AVAILABLE_TARGETS)).split(",")
    if m.strip()
)

# 防護配置（實驗的自變數）。
# C2/D2 是 run #2 之後才加的：實測發現 C（剝除式淨化）會讓明文型注入更容易成功，
# 推測原因是剝掉標籤的同時也剝掉了資料的歸屬邊界。C2 只改這一個變因來驗證。
# 單步配置在前、多步配置在後；報告的欄位順序依此
CONFIGS: tuple[str, ...] = ("A", "B", "C", "C2", "D", "D2", "M-00", "M-10", "M-01", "M-11")
CONFIG_LABELS: dict[str, str] = {
    "A": "無防護（裸模型）",
    "B": "防禦性系統提示",
    "C": "輸入淨化（剝除式）",
    "C2": "輸入淨化（保留邊界）",
    "D": "提示 + 剝除式淨化",
    "D2": "提示 + 保留邊界淨化",
    # 多步流程：M-<第一層><第二層>，1 表示該層有防禦性系統提示
    "M-00": "多步・兩層皆無防禦",
    "M-10": "多步・僅入口有防禦",
    "M-01": "多步・僅決策層有防禦",
    "M-11": "多步・兩層皆有防禦",
}

# 每 1M token 的美元定價，用於事前估算。
# Anthropic 來源：官方定價表（cached 2026-06-24）
# Google 來源：https://ai.google.dev/gemini-api/docs/pricing（cached 2026-09-21）
#   Gemini 的思考 token 併入 output 計費，不另計價；此處採 200k 以下的級距。
PRICING_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-3.1-flash-lite": (0.25, 1.50),
    "gemini-3.5-flash": (1.50, 9.00),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.6-flash": (0.75, 3.75),
    "gemini-3.7-flash": (0.75, 3.75),
    "gemini-3.8-flash": (0.75, 3.75),
}
FALLBACK_PRICE: tuple[float, float] = (5.00, 25.00)  # 未知模型一律以最貴者估算

MAX_RUN_USD: float = float(os.getenv("MAX_RUN_USD", "3.0"))
MAX_DAILY_USD: float = float(os.getenv("MAX_DAILY_USD", "10.0"))


class CostLimitExceeded(RuntimeError):
    """事前估算已超過上限，拒絕建立 run。"""


def price_of(model: str) -> tuple[float, float]:
    """回傳 (input, output) 每 1M token 的美元價格；未知模型以最貴者估算。"""
    return PRICING_PER_MTOK.get(model, FALLBACK_PRICE)


def estimate_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """依 token 數估算單次呼叫成本。"""
    price_in, price_out = price_of(model)
    return (input_tokens * price_in + output_tokens * price_out) / 1_000_000
