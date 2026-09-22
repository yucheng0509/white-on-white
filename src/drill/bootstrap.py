"""把種子素材與注入矩陣寫進資料庫（冪等）。

素材與注入之所以進資料庫而不是留在 seeds.py 裡，是因為平台要能累積：
組織上傳自己的素材後，套用的是同一批注入樣本與同一套判定規則。
"""

from __future__ import annotations

import json

from drill.db import connect, init_db
from drill.seeds import (
    CARRIER_PROBES,
    EXPECTED_RANKING,
    PRIMARY_MATRIX,
    WORST_CANDIDATE,
    material_template,
)

SEED_SCENARIO = "hiring_screen"


def seed_material() -> int:
    """寫入（或取回）種子履歷素材的 id。

    ground_truth 先以 EXPECTED_RANKING 暫填但 gt_verified=0；
    須等多模型在乾淨素材下排序一致，才可由 verify_ground_truth 標記為已驗證。
    """
    template = material_template()
    with connect() as conn:
        row = conn.execute(
            "SELECT id FROM material WHERE scenario_type=? AND content=?",
            (SEED_SCENARIO, template),
        ).fetchone()
        if row:
            return int(row["id"])
        cur = conn.execute(
            """INSERT INTO material (scenario_type, content, ground_truth_ranking, gt_verified)
               VALUES (?, ?, ?, 0)""",
            (SEED_SCENARIO, template, json.dumps(EXPECTED_RANKING)),
        )
        return int(cur.lastrowid)


def seed_injections(include_carrier_probes: bool = False) -> list[int]:
    """寫入注入矩陣，回傳 id 列表（不含對照組——對照組是 injection_id=NULL）。"""
    samples = list(PRIMARY_MATRIX)
    if include_carrier_probes:
        seen = {(s["visibility"], s["carrier"], s["strength"]) for s in samples}
        samples += [
            p for p in CARRIER_PROBES
            if (p["visibility"], p["carrier"], p["strength"]) not in seen
        ]

    ids: list[int] = []
    with connect() as conn:
        for s in samples:
            row = conn.execute(
                """SELECT id FROM injection
                   WHERE visibility=? AND carrier=? AND strength=? AND payload=?""",
                (s["visibility"], s["carrier"], s["strength"], s["payload"]),
            ).fetchone()
            if row:
                ids.append(int(row["id"]))
                continue
            cur = conn.execute(
                """INSERT INTO injection (kind, visibility, carrier, strength, payload, target, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    s["kind"], s["visibility"], s["carrier"], s["strength"],
                    s["payload"], WORST_CANDIDATE, s["source"],
                ),
            )
            ids.append(int(cur.lastrowid))
    return ids


def bootstrap(include_carrier_probes: bool = False) -> tuple[int, list[int]]:
    init_db()
    return seed_material(), seed_injections(include_carrier_probes)


if __name__ == "__main__":
    mid, iids = bootstrap(include_carrier_probes=True)
    print(f"素材 id={mid}；注入樣本 {len(iids)} 筆：{iids}")
