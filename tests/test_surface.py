"""攻擊面盤點的回歸保護。

這組測試的用途不是驗證某個函式的行為，而是釘住一個系統性質：
**每一種已知的隱藏通道，只要會進到送給模型的文字裡，就必須被標記。**

「進得去卻沒標記」（blind_spot）是最危險的一類漏洞——模型讀得到，
我們卻沒有警告使用者。擷取器之後只要改動，這裡就會擋下新的盲點。
"""

from drill.surface import CHANNELS, MARKER, audit, summary


def test_every_channel_builds():
    """每個通道都要能造出探測檔，否則盤點結果不可信。"""
    rows = audit()
    assert len(rows) == len(CHANNELS)
    broken = [r["id"] for r in rows if r["status"] == "error"]
    assert not broken, f"這些通道的探測檔造不出來：{broken}"


def test_no_blind_spots():
    """不允許有「送進模型卻沒標記」的通道。

    這是本專案最核心的安全性質。新增擷取邏輯若讓某個通道
    進得去卻標不到，這裡就會失敗。
    """
    blind = [
        (r["format"], r["name"]) for r in audit() if r["status"] == "blind_spot"
    ]
    assert not blind, f"出現盲點——模型讀得到但我們沒警告：{blind}"


def test_marker_is_unique_per_channel():
    """探測標記必須逐通道互異，否則判定會互相污染。"""
    marks = [MARKER.format(c.id.upper().replace("_", "")) for c in CHANNELS]
    assert len(set(marks)) == len(marks)


def test_summary_counts_add_up():
    s = summary()
    assert s["covered"] + s["blind_spot"] + s["not_ingested"] + s["error"] == s["total"]
    assert s["total"] == len(CHANNELS)
