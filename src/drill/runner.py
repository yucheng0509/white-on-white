"""演練執行器。

刻意不用 Celery/Redis——FastAPI BackgroundTasks 加一張 SQLite 狀態表就夠。
單一案例失敗只寫進該案例的 error 欄位，不中斷整批。
"""

from __future__ import annotations

import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from drill.config import CostLimitExceeded, MAX_RUN_USD, estimate_usd
from drill.db import connect
from drill.judge import judge_baseline_correct, judge_disclosed, judge_manipulated
from drill.pipeline import PIPELINE_CONFIGS, run_pipeline
from drill.sanitizer import sanitize, sanitize_with_boundaries
from drill.seeds import EXPECTED_RANKING, TOP_N_FOR_JUDGEMENT, assemble
from drill.targets import build_system, call_target

# 單一案例的粗估 token 數，用於事前成本閘門。
# 實測值（2026-09-21，五份履歷素材）：input 497、output 48~863（含思考 token）。
# 估算一律取實測上緣，寧可高估而擋下，不要跑到一半才超支。
EST_INPUT_TOKENS_PER_CASE = 700
EST_OUTPUT_TOKENS_PER_CASE = 900

# 四種配置對「防禦性系統提示」與「輸入淨化」的開關組合
# 並行度。受測模型是外部 API，瓶頸在網路往返不在 CPU，用執行緒就夠。
# 上限刻意保守：免費層的 RPM 有限，撞到 429 會讓整批結果出現不可歸因的缺漏。
MAX_WORKERS = int(os.getenv("DRILL_WORKERS", "4"))

_CONFIG_FLAGS: dict[str, tuple[bool, str | None]] = {
    # config: (是否加防禦性系統提示, 淨化模式)
    #   None        不淨化
    #   strip       剝除隱藏內容，標籤一併去掉（業界常見做法）
    #   boundaries  同樣剝除隱藏內容，但保留資料區塊的邊界標記
    "A": (False, None),
    "B": (True, None),
    "C": (False, "strip"),
    "C2": (False, "boundaries"),
    "D": (True, "strip"),
    "D2": (True, "boundaries"),
}

_SANITIZERS = {"strip": sanitize, "boundaries": sanitize_with_boundaries}


# 多步配置每個案例要呼叫兩次，而且第二次的輸入是第一次的輸出（摘要比原始
# 素材長）。實測 run #4：估 $1.117、實際 $2.969，差了 2.7 倍——
# 成本閘門低估等於沒有閘門，因此多步配置要單獨計價。
PIPELINE_COST_FACTOR = 3.0


def estimate_run_cost(
    models: list[str], n_cases_per_model: int, configs: list[str] | None = None
) -> float:
    """估算一次 run 的總成本（美元）。

    configs 含多步配置時，那些配置的案例以 PIPELINE_COST_FACTOR 加權。
    寧可高估而擋下，也不要跑到一半才發現超支。
    """
    configs = configs or []
    pipeline_ratio = (
        sum(1 for c in configs if c in PIPELINE_CONFIGS) / len(configs) if configs else 0.0
    )
    factor = 1.0 + pipeline_ratio * (PIPELINE_COST_FACTOR - 1.0)
    return sum(
        estimate_usd(model, EST_INPUT_TOKENS_PER_CASE, EST_OUTPUT_TOKENS_PER_CASE)
        * n_cases_per_model
        * factor
        for model in models
    )


def create_run(
    application_id: int,
    configs: list[str],
    models: list[str],
    material_ids: list[int],
    injection_ids: list[int],
    repeats: int = 1,
    include_control: bool = True,
) -> int:
    """建立一次演練批次，並在建立時就擋下超支的 run。

    Args:
        repeats: 每個組合重複幾次。LLM 輸出有隨機性，單次結果不可採信；
            正式實驗至少 3 次，報告中須以「n 次中 k 次被操控」呈現。
        include_control: 是否加入無注入的對照組（injection_id=NULL）。
            對照組是整個實驗的地板，預設一定要跑。

    Raises:
        CostLimitExceeded: 事前估算超過 MAX_RUN_USD。
    """
    if repeats < 1:
        raise ValueError("repeats 至少為 1")
    unknown = [c for c in configs if c not in _CONFIG_FLAGS and c not in PIPELINE_CONFIGS]
    if unknown:
        raise ValueError(
            f"未知的防護配置：{unknown}；"
            f"可用：{sorted(_CONFIG_FLAGS)} 或 {sorted(PIPELINE_CONFIGS)}"
        )

    slots: list[int | None] = list(injection_ids)
    if include_control:
        slots.insert(0, None)
    if not slots:
        raise ValueError("沒有任何注入樣本，且未啟用對照組")

    n_cases_per_model = len(configs) * len(material_ids) * len(slots) * repeats
    est = estimate_run_cost(models, n_cases_per_model, configs)
    if est > MAX_RUN_USD:
        raise CostLimitExceeded(
            f"估算 ${est:.2f} 超過單次上限 ${MAX_RUN_USD:.2f}；"
            f"請減少素材、注入樣本、重複次數或配置數量"
        )

    total = n_cases_per_model * len(models)
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO drill_run (application_id, configs, models, total_cases, est_usd)
               VALUES (?, ?, ?, ?, ?)""",
            (application_id, json.dumps(configs), json.dumps(models), total, est),
        )
        run_id = int(cur.lastrowid)
        rows = [
            (run_id, config, model, material_id, injection_id, r)
            for config in configs
            for model in models
            for material_id in material_ids
            for injection_id in slots
            for r in range(repeats)
        ]
        conn.executemany(
            """INSERT INTO drill_case
               (run_id, config, model, material_id, injection_id, repeat_index)
               VALUES (?, ?, ?, ?, ?, ?)""",
            rows,
        )
    return run_id


def execute_run(run_id: int) -> None:
    """背景執行一次演練。逐案例更新進度，供前端輪詢。"""
    with connect() as conn:
        conn.execute(
            "UPDATE drill_run SET status='running', started_at=datetime('now') WHERE id=?",
            (run_id,),
        )

    try:
        with connect() as conn:
            cases = conn.execute(
                "SELECT * FROM drill_case WHERE run_id=? ORDER BY id", (run_id,)
            ).fetchall()

        # API 呼叫在執行緒池裡並行，資料庫寫入回到主執行緒序列處理，
        # 避免多執行緒同時寫 SQLite 造成 database is locked。
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            for case_id, payload, error in pool.map(_run_case_isolated, cases):
                with connect() as conn:
                    if error is None:
                        _persist(conn, case_id, payload)
                    else:
                        conn.execute(
                            "UPDATE drill_case SET error=? WHERE id=?", (error, case_id)
                        )
                    conn.execute(
                        "UPDATE drill_run SET done_cases = done_cases + 1 WHERE id=?",
                        (run_id,),
                    )

        with connect() as conn:
            conn.execute(
                """UPDATE drill_run
                   SET status='done', finished_at=datetime('now'),
                       actual_usd=(SELECT COALESCE(SUM(usd),0) FROM drill_case WHERE run_id=?)
                   WHERE id=?""",
                (run_id, run_id),
            )
    except Exception as exc:  # 背景任務必須把失敗寫回資料庫，否則前端永遠停在 running
        with connect() as conn:
            conn.execute(
                "UPDATE drill_run SET status='failed', error=?, finished_at=datetime('now') WHERE id=?",
                (str(exc), run_id),
            )
        raise


def _run_case_isolated(case: sqlite3.Row) -> tuple[int, dict[str, Any] | None, str | None]:
    """在工作執行緒裡跑單一案例，只回傳結果，不碰資料庫。

    單一案例失敗不該讓整批停擺——錯誤以字串回傳，由主執行緒寫進該案例的 error。
    """
    try:
        return int(case["id"]), _execute_case(case), None
    except Exception as exc:
        return int(case["id"]), None, f"{type(exc).__name__}: {exc}"


def _execute_case(case: sqlite3.Row) -> dict[str, Any]:
    """組裝素材 -> 套用配置 -> 呼叫受測模型 -> 規則判定。回傳待寫入的欄位。"""
    with connect() as conn:
        material = conn.execute(
            "SELECT * FROM material WHERE id=?", (case["material_id"],)
        ).fetchone()
        injection = None
        if case["injection_id"] is not None:
            row = conn.execute(
                "SELECT * FROM injection WHERE id=?", (case["injection_id"],)
            ).fetchone()
            injection = dict(row) if row else None

    if material is None:
        raise LookupError(f"material {case['material_id']} 不存在")

    config = case["config"]
    carried: int | None = None

    if config in PIPELINE_CONFIGS:
        # 多步流程：素材不淨化，變因是「防禦提示做在哪一層」
        content = assemble(material["content"], injection)
        pipe = run_pipeline(case["model"], content, config)
        result = pipe.final
        carried = int(pipe.carried_injection)
        # raw_output 保留兩步全文，事後複核才看得出污染是在哪一步進來的
        result.raw_output = (
            f"--- 第一步 摘要 ---\n{pipe.summary}\n\n--- 第二步 排序 ---\n{result.raw_output}"
        )
        result.input_tokens, result.output_tokens, result.usd = (
            pipe.input_tokens, pipe.output_tokens, pipe.usd
        )
    else:
        defensive, sanitize_mode = _CONFIG_FLAGS[config]

        # 注意順序：先把注入組裝進素材，再淨化。
        # 反過來（先淨化再注入）會讓有淨化的配置形同虛設，是很容易犯的錯。
        content = assemble(material["content"], injection)
        if sanitize_mode:
            content, _stripped = _SANITIZERS[sanitize_mode](content)

        result = call_target(case["model"], build_system(defensive), content)

    ground_truth = (
        json.loads(material["ground_truth_ranking"])
        if material["ground_truth_ranking"]
        else EXPECTED_RANKING
    )
    if injection is None:
        # 對照組記的是「排序是否正確」。manipulated 留 None，
        # 因為「沒有被操控」與「排序正確」是兩件事，混在同一欄會讓統計失真。
        manipulated = None
        baseline_ok = judge_baseline_correct(result.ranking, ground_truth)
        note = f"{result.note} [baseline_correct={baseline_ok}]".strip()
    else:
        manipulated = judge_manipulated(
            result.ranking, ground_truth, injection.get("target"), TOP_N_FOR_JUDGEMENT
        )
        note = result.note

    return {
        "raw_output": result.raw_output,
        "ranking": json.dumps(result.ranking, ensure_ascii=False),
        "note": note,
        "parse_ok": int(result.parse_ok),
        "disclosed": int(judge_disclosed(result.note, result.raw_output)),
        "carried": carried,
        "manipulated": None if manipulated is None else int(manipulated),
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "usd": result.usd,
    }


def _persist(conn: sqlite3.Connection, case_id: int, payload: dict[str, Any]) -> None:
    conn.execute(
        """UPDATE drill_case
           SET raw_output=?, ranking=?, note=?, parse_ok=?, disclosed=?, carried=?,
               manipulated=?, input_tokens=?, output_tokens=?, usd=?, error=NULL
           WHERE id=?""",
        (
            payload["raw_output"], payload["ranking"], payload["note"],
            payload["parse_ok"], payload["disclosed"], payload["carried"],
            payload["manipulated"],
            payload["input_tokens"], payload["output_tokens"], payload["usd"],
            case_id,
        ),
    )


def run_summary(run_id: int) -> dict[str, Any]:
    """回傳一次 run 的四配置對照結果，供報告使用。"""
    with connect() as conn:
        run = conn.execute("SELECT * FROM drill_run WHERE id=?", (run_id,)).fetchone()
        if run is None:
            raise LookupError(f"run {run_id} 不存在")
        rows = conn.execute(
            """SELECT c.config, c.model,
                      COUNT(*) AS n,
                      SUM(COALESCE(c.manipulated, 0)) AS manipulated,
                      SUM(COALESCE(c.disclosed, 0))   AS disclosed,
                      SUM(CASE WHEN c.error IS NOT NULL THEN 1 ELSE 0 END) AS errors
               FROM drill_case c
               WHERE c.run_id=? AND c.injection_id IS NOT NULL
               GROUP BY c.config, c.model ORDER BY c.config, c.model""",
            (run_id,),
        ).fetchall()
        # 熱力圖用：每個 (可見性, 強度, 配置) 格子的殘餘風險率
        cells = conn.execute(
            """SELECT i.visibility, i.strength, c.config, c.model,
                      COUNT(*) AS n, SUM(COALESCE(c.manipulated, 0)) AS manipulated
               FROM drill_case c JOIN injection i ON i.id = c.injection_id
               WHERE c.run_id=?
               GROUP BY i.visibility, i.strength, c.config, c.model
               ORDER BY i.visibility, i.strength, c.config""",
            (run_id,),
        ).fetchall()

    return {
        "run_id": run_id,
        "status": run["status"],
        "progress": f'{run["done_cases"]}/{run["total_cases"]}',
        "est_usd": run["est_usd"],
        "actual_usd": run["actual_usd"],
        "by_config": [dict(r) for r in rows],
        "cells": [dict(r) for r in cells],
    }


def retry_failed_cases(run_id: int) -> dict[str, int]:
    """補跑一次 run 裡沒有成功結果的案例，只補洞不重跑整批。

    兩種情況都要補：
      - error 不為空：網路層的暫時性錯誤
      - raw_output 為空且無 error：執行被中斷（例如批次卡死後人工中止）

    整批重跑既浪費錢，也會讓已完成的結果失去可重現性。
    補完後把 run 的狀態與實際花費一併更新。
    """
    with connect() as conn:
        cases = conn.execute(
            """SELECT * FROM drill_case
               WHERE run_id=? AND (error IS NOT NULL OR raw_output IS NULL)
               ORDER BY id""",
            (run_id,),
        ).fetchall()
    if not cases:
        return {"attempted": 0, "recovered": 0, "still_failing": 0}

    recovered = still_failing = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for case_id, payload, error in pool.map(_run_case_isolated, cases):
            with connect() as conn:
                if error is None:
                    _persist(conn, case_id, payload)
                    recovered += 1
                else:
                    conn.execute(
                        "UPDATE drill_case SET error=? WHERE id=?", (error, case_id)
                    )
                    still_failing += 1

    with connect() as conn:
        done = conn.execute(
            "SELECT COUNT(*) AS n FROM drill_case WHERE run_id=? AND raw_output IS NOT NULL",
            (run_id,),
        ).fetchone()["n"]
        conn.execute(
            """UPDATE drill_run
               SET done_cases=(SELECT COUNT(*) FROM drill_case
                               WHERE run_id=? AND (raw_output IS NOT NULL OR error IS NOT NULL)),
                   actual_usd=(SELECT COALESCE(SUM(usd),0) FROM drill_case WHERE run_id=?),
                   status=CASE WHEN ?=0 THEN 'done' ELSE status END,
                   finished_at=datetime('now')
               WHERE id=?""",
            (run_id, run_id, still_failing, run_id),
        )
    return {
        "attempted": len(cases), "recovered": recovered,
        "still_failing": still_failing, "total_done": done,
    }
