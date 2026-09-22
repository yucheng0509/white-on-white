"""風險對應器：把組織的 AI 應用場景對應到官方 20 項風險類型。

## 這個模組的定位

數發部框架 3.4.1(a)ii 提到，官方期待有工具「讓業者得以將**抽象風險類型與實際
應用情境相互對照**」，並點名資源有限的中小企業。這個模組就是在做那件事。

## 兩段式設計，且兩者不可混為一談

1. **相關性推論**（LLM）：讀應用場景的自由文字描述，判斷哪些風險類型相關。
   這是語意推理，輸出是「可能相關」，不是事實。
2. **實測證據**（規則＋資料庫）：對 20 項中本平台量得出來的 5 項，
   附上這個應用實際演練出來的數字。

報告裡這兩者必須分開標示。把 LLM 的推測寫得像實測結果，是這類工具最容易犯、
也最傷可信度的錯——尤其當整個專案的主張正是「不要相信未經查核的 AI 輸出」。
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from google import genai
from google.genai import types

from drill.config import GENERATOR_MODEL
from drill.db import connect
from drill.risks import BY_CODE, MEASURABLE, RISK_TYPES

_JSON_RE = re.compile(r"\{.*\}", re.S)

_MAX_RISKS = 6  # 挑太多就失去鑑別度，等於沒對應

_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "risks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "rationale": {"type": "string"},
                    "severity_hint": {"type": "string", "enum": ["高", "中", "低"]},
                },
                "required": ["code", "rationale", "severity_hint"],
            },
        }
    },
    "required": ["risks"],
}

_PROMPT = """你是協助組織做 AI 風險盤點的分析助理。

以下是一個組織的 AI 應用情境：
- 應用名稱：{name}
- 情境類型：{scenario_type}
- 場景描述：{description}
- 使用的 AI 技術：{ai_tech}
- 利害關係人：{stakeholders}

以下是數發部「人工智慧風險分類框架」的 20 項風險類型（說明為官方原文）：
{risk_list}

請判斷哪些風險類型與這個應用情境相關，最多選 {max_risks} 項，依相關程度由高到低排列。

要求：
1. rationale 必須引用場景描述中的**具體內容**，不得只是複述風險類型的說明。
2. 只選真正相關的。寧可少選，選太多等於沒有對應。
3. severity_hint 依「這個應用情境本身的客觀特性」判斷，**不要考慮任何防護措施**
   （框架 3.3.2 明訂評估風險時不計入緩解效果）。

只輸出 JSON：{{"risks":[{{"code":"A1","rationale":"...","severity_hint":"高"}}]}}"""


class MappingFailed(RuntimeError):
    """對應器沒有產生可用輸出。"""


def _risk_list_text() -> str:
    return "\n".join(f"({r.code}) {r.name}：{r.description}" for r in RISK_TYPES)


def map_risks(application_id: int, model: str | None = None) -> list[dict[str, Any]]:
    """讀應用場景描述，推論相關的風險類型，寫入 risk_mapping 表。

    重跑會覆蓋同一個應用先前的對應結果——場景描述改了，對應就該重做。
    """
    model = model or GENERATOR_MODEL
    with connect() as conn:
        app = conn.execute(
            "SELECT * FROM ai_application WHERE id=?", (application_id,)
        ).fetchone()
    if app is None:
        raise LookupError(f"application {application_id} 不存在")

    prompt = _PROMPT.format(
        name=app["name"],
        scenario_type=app["scenario_type"],
        description=app["description"],
        ai_tech=app["ai_tech"] or "（未填）",
        stakeholders=app["stakeholders"] or "（未填）",
        risk_list=_risk_list_text(),
        max_risks=_MAX_RISKS,
    )

    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            max_output_tokens=8000,
            response_mime_type="application/json",
            response_schema=_RESPONSE_SCHEMA,
        ),
    )
    text = response.text or ""
    match = _JSON_RE.search(text)
    if not match:
        feedback = response.prompt_feedback
        raise MappingFailed(
            f"{model} 未產生可解析的輸出"
            + (f"（block_reason={feedback.block_reason}）" if feedback else "")
        )

    raw = json.loads(match.group(0)).get("risks", [])
    mapped: list[dict[str, Any]] = []
    for item in raw[:_MAX_RISKS]:
        code = str(item.get("code", "")).strip().upper()
        if code not in BY_CODE:  # 模型可能吐出不存在的代號，一律丟棄
            continue
        mapped.append({
            "code": code,
            "name": BY_CODE[code].name,
            "rationale": str(item.get("rationale", "")).strip(),
            "severity_hint": str(item.get("severity_hint", "")).strip(),
            "measurable": code in MEASURABLE,
        })

    if not mapped:
        raise MappingFailed("模型沒有回傳任何有效的風險代號")

    with connect() as conn:
        conn.execute("DELETE FROM risk_mapping WHERE application_id=?", (application_id,))
        conn.executemany(
            "INSERT INTO risk_mapping (application_id, risk_code, rationale) VALUES (?,?,?)",
            [(application_id, m["code"], f'[{m["severity_hint"]}] {m["rationale"]}')
             for m in mapped],
        )
    return mapped


def collect_evidence(application_id: int) -> dict[str, dict[str, Any]]:
    """為可量測的風險類型，撈出這個應用實際演練出來的數字。

    只涵蓋 risks.MEASURABLE 列出的 5 項。其餘 15 項一律標為「本演練未涵蓋」，
    不做任何推測——這條界線是整份報告可信度的來源。
    """
    with connect() as conn:
        runs = [r["id"] for r in conn.execute(
            "SELECT id FROM drill_run WHERE application_id=? AND status='done'",
            (application_id,),
        )]
        if not runs:
            return {}
        ph = ",".join("?" * len(runs))

        by_config = {
            r["config"]: dict(r) for r in conn.execute(
                f"""SELECT config, COUNT(*) n,
                           SUM(COALESCE(manipulated,0)) manipulated,
                           SUM(COALESCE(disclosed,0))   disclosed
                    FROM drill_case
                    WHERE run_id IN ({ph}) AND manipulated IS NOT NULL
                    GROUP BY config""",
                runs,
            )
        }
        sample = conn.execute(
            f"""SELECT model, note FROM drill_case
                WHERE run_id IN ({ph}) AND manipulated=1 AND note IS NOT NULL AND note != ''
                ORDER BY LENGTH(note) DESC LIMIT 1""",
            runs,
        ).fetchone()
        pipeline = [
            dict(r) for r in conn.execute(
                f"""SELECT config, COUNT(*) n,
                           SUM(COALESCE(manipulated,0)) manipulated,
                           SUM(COALESCE(carried,0))     carried
                    FROM drill_case
                    WHERE run_id IN ({ph}) AND config LIKE 'M-%' AND manipulated IS NOT NULL
                    GROUP BY config ORDER BY config""",
                runs,
            )
        ]

    def rate(k: int, n: int) -> float | None:
        return None if not n else k / n

    single = {c: v for c, v in by_config.items() if not c.startswith("M-")}
    worst = max(
        (v for v in single.values() if v["n"]),
        key=lambda v: v["manipulated"] / v["n"], default=None,
    )
    best = min(
        (v for v in single.values() if v["n"]),
        key=lambda v: v["manipulated"] / v["n"], default=None,
    )

    evidence: dict[str, dict[str, Any]] = {}

    if worst and best:
        span = {
            "指標": "操控成功率",
            "無防護上限": f'{worst["manipulated"]}/{worst["n"]}'
                          f'（{rate(worst["manipulated"], worst["n"]):.0%}）',
            "最佳配置": f'{best["manipulated"]}/{best["n"]}'
                        f'（{rate(best["manipulated"], best["n"]):.0%}）',
            "配置差距": f'{rate(worst["manipulated"], worst["n"]) - rate(best["manipulated"], best["n"]):.0%}',
        }
        evidence["A1"] = {**span, "說明": MEASURABLE["A1"]}
        evidence["A3"] = {**span, "說明": MEASURABLE["A3"]}

    disclosure = {
        c: f'{v["disclosed"]}/{v["n"]}（{rate(v["disclosed"], v["n"]):.0%}）'
        for c, v in sorted(single.items()) if v["n"]
    }
    if disclosure:
        evidence["A2"] = {
            "指標": "揭露率（模型被影響時是否主動說明）",
            "各配置": disclosure,
            "說明": MEASURABLE["A2"],
        }

    if sample:
        evidence["A8"] = {
            "指標": "被操控時模型產出的理由",
            "實例": f'{sample["model"]}：{sample["note"][:120]}',
            "說明": MEASURABLE["A8"],
        }

    if pipeline:
        evidence["B6"] = {
            "指標": "多步流程的污染穿透率與操控率",
            "各配置": {
                p["config"]: f'操控 {p["manipulated"]}/{p["n"]}、污染穿透 {p["carried"]}/{p["n"]}'
                for p in pipeline
            },
            "說明": MEASURABLE["B6"],
        }

    return evidence


def build_risk_report(application_id: int) -> dict[str, Any]:
    """把「LLM 推論的相關風險」與「實測證據」併成一份報告。

    兩者在輸出中分別標記 source='推論' / '實測'，前端與文件都必須照這個區分呈現。
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT risk_code, rationale FROM risk_mapping WHERE application_id=? ORDER BY id",
            (application_id,),
        ).fetchall()
    evidence = collect_evidence(application_id)

    items = []
    for r in rows:
        code = r["risk_code"]
        risk = BY_CODE[code]
        items.append({
            "code": code,
            "name": risk.name,
            "category": risk.category,
            "official_description": risk.description,
            "rationale": r["rationale"],
            "source": "推論",          # 這一欄是 LLM 讀場景描述得出的
            "evidence": evidence.get(code),   # 這一欄才是實測數字
            "evidence_source": "實測" if code in evidence else None,
        })
    # 有實測數據、卻沒被盤點對應到的風險。這一組最值得看——
    # 它是「演練揭露了盤點想不到的東西」的直接證據，也是本平台存在的理由。
    mapped_codes = {i["code"] for i in items}
    blind_spots = [
        {
            "code": code,
            "name": BY_CODE[code].name,
            "category": BY_CODE[code].category,
            "official_description": BY_CODE[code].description,
            "evidence": data,
            "evidence_source": "實測",
            "note": "演練量到了這項風險，但場景盤點沒有對應到它。",
        }
        for code, data in evidence.items()
        if code not in mapped_codes
    ]

    return {
        "application_id": application_id,
        "mapped": items,
        "blind_spots": blind_spots,
        "measurable_total": len(MEASURABLE),
        "risk_total": len(RISK_TYPES),
        "note": (
            f"本平台的演練能為 20 項中的 {len(MEASURABLE)} 項提供實測證據，"
            "其餘標為『本演練未涵蓋』，不做推測。"
        ),
    }


# 依應用的技術特性補充風險類型的保守規則。
#
# 為什麼需要這一層：實測發現，讓 LLM 讀「將應徵者履歷交由 LLM 依職缺條件排序」
# 這樣的場景描述，它會對應到 A7 歧視、A5 個資、A2 可解釋性、B1 過度依賴、A8 錯誤訊息——
# 每一項都對，但**完全沒有 A1 安全漏洞與攻擊**，而那正是我們實測出 32% 會發生的風險。
#
# 原因不是模型不夠好，是場景描述裡根本沒有攻擊面的線索：組織自己描述業務流程時，
# 不會想到有人在履歷裡藏指令。這正是「盤點只能盤出你想得到的風險」的具體證據，
# 也是演練必須存在的理由。因此這一層用關鍵字規則補上，並明確標示來源為「規則補充」。
_INGESTS_EXTERNAL = (
    "履歷", "應徵", "來信", "郵件", "email", "網頁", "文件", "報價", "投標",
    "評論", "留言", "客戶提供", "上傳", "附件", "爬取", "擷取", "檢索", "rag",
)
_AGENTIC = ("代理", "agent", "自動執行", "多步", "工具呼叫", "工作流", "pipeline", "串接")


def suggest_from_capability(application_id: int) -> list[dict[str, Any]]:
    """依應用的技術特性補充「場景描述講不出來」的風險類型。"""
    with connect() as conn:
        app = conn.execute(
            "SELECT * FROM ai_application WHERE id=?", (application_id,)
        ).fetchone()
    if app is None:
        raise LookupError(f"application {application_id} 不存在")

    haystack = " ".join(
        str(app[k] or "") for k in ("name", "scenario_type", "description", "ai_tech")
    ).lower()

    suggestions: list[dict[str, Any]] = []
    hits = [w for w in _INGESTS_EXTERNAL if w in haystack]
    if hits:
        reason = (
            f"此應用會讀取外部提供的內容（偵測到：{'、'.join(hits[:4])}），"
            "該內容即為間接提示注入的載體。場景描述通常不會提到這一點，"
            "因為它描述的是業務流程而非攻擊面。"
        )
        suggestions += [
            {"code": "A1", "rationale": reason, "source": "規則補充"},
            {"code": "A3", "rationale": reason, "source": "規則補充"},
        ]
    agent_hits = [w for w in _AGENTIC if w in haystack]
    if agent_hits:
        suggestions.append({
            "code": "B6",
            "rationale": (
                f"此應用具備多步或代理特性（偵測到：{'、'.join(agent_hits[:3])}），"
                "中間步驟的輸出會成為下一步的輸入，形成額外的污染路徑。"
            ),
            "source": "規則補充",
        })

    for s in suggestions:
        s["name"] = BY_CODE[s["code"]].name
        s["measurable"] = s["code"] in MEASURABLE
    return suggestions


def map_risks_full(application_id: int, model: str | None = None) -> dict[str, Any]:
    """完整流程：LLM 推論 ＋ 規則補充，並標示哪些是盤點漏掉的。

    「盤點漏掉但演練測得出來」這一組，是本平台對組織最直接的價值——
    它回答了「為什麼光做問卷式自評不夠」。
    """
    inferred = map_risks(application_id, model)
    suggested = suggest_from_capability(application_id)
    inferred_codes = {m["code"] for m in inferred}

    missed = [s for s in suggested if s["code"] not in inferred_codes]
    if missed:
        with connect() as conn:
            conn.executemany(
                "INSERT INTO risk_mapping (application_id, risk_code, rationale) VALUES (?,?,?)",
                [(application_id, m["code"], f'[規則補充] {m["rationale"]}') for m in missed],
            )

    return {
        "application_id": application_id,
        "inferred": inferred,
        "rule_supplemented": suggested,
        "missed_by_inventory": missed,
        "insight": (
            f"場景描述推論出 {len(inferred)} 項風險，但漏掉了 {len(missed)} 項"
            f"（{'、'.join(m['code'] for m in missed)}）——這些是規則依技術特性補上的。"
            "盤點只能盤出想得到的風險，演練才能揭露想不到的。"
        ) if missed else "場景描述已涵蓋規則所能補充的風險類型。",
    }
