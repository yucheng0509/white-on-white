"""風險類型與對應器的測試。

引用官方框架時，資料本身正確比任何功能都重要——
數字或條文寫錯，整份報告的可信度就沒了。
"""

from __future__ import annotations

from collections import Counter

from drill.risks import BY_CODE, CATEGORY_NAMES, MEASURABLE, RISK_TYPES, describe


def test_風險類型共二十項且分屬三大類():
    assert len(RISK_TYPES) == 20
    counts = Counter(r.category for r in RISK_TYPES)
    assert counts == {"A": 8, "B": 6, "C": 6}
    assert set(CATEGORY_NAMES) == {"A", "B", "C"}


def test_代號不重複且與索引一致():
    codes = [r.code for r in RISK_TYPES]
    assert len(set(codes)) == len(codes)
    assert set(BY_CODE) == set(codes)


def test_可實測清單裡的代號都存在():
    assert set(MEASURABLE) <= set(BY_CODE)


def test_每項都有官方說明且未被截斷():
    for r in RISK_TYPES:
        assert r.description.endswith("。"), f"{r.code} 的說明不是完整句子"
        assert len(r.description) >= 30, f"{r.code} 的說明過短，可能抄漏"


def test_A1原文含系統操控這個關鍵詞():
    # 這是本專案對接官方框架的核心依據，抄錯就失去引用價值
    assert "系統操控" in BY_CODE["A1"].description


def test_B6原文含行為逐漸偏離原始指令():
    assert "偏離原始指令" in BY_CODE["B6"].description


def test_describe輸出含代號名稱與說明():
    text = describe("A1")
    assert "(A1)" in text and "安全漏洞" in text


def test_規則補充能從履歷場景推出攻擊面風險():
    # 實測顯示 LLM 讀「履歷排序」場景不會對應到 A1，
    # 因為場景描述講的是業務流程不是攻擊面。這層規則就是補這個洞。
    from drill.mapping import _INGESTS_EXTERNAL

    haystack = "將應徵者履歷交由 llm 依職缺條件排序，供人資參考。"
    assert any(w in haystack for w in _INGESTS_EXTERNAL)
