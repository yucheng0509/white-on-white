"""Ground truth 驗證。

素材的「客觀正確答案」不可由我們自行標註——那會讓整個實驗變成循環論證。
規定：必須由多個受測模型在**乾淨素材**（無任何注入）下獨立排序，
全部一致時才採信，並寫回 material.gt_verified=1。
任何一個模型排出不同結果，該素材就視為本身具歧義，不得用於正式實驗。
"""

from __future__ import annotations

import json
from collections import Counter

from drill.config import TARGET_MODELS
from drill.db import connect
from drill.seeds import assemble
from drill.targets import build_system, call_target


def verify_ground_truth(
    material_id: int, models: tuple[str, ...] | None = None
) -> dict[str, object]:
    """讓多個模型在乾淨素材下排序，一致才標記為已驗證。

    Returns:
        dict：含 consensus（一致時的排序）、agreed（是否一致）、per_model 明細。
    """
    models = models or TARGET_MODELS
    with connect() as conn:
        material = conn.execute(
            "SELECT * FROM material WHERE id=?", (material_id,)
        ).fetchone()
    if material is None:
        raise LookupError(f"material {material_id} 不存在")

    clean = assemble(material["content"], None)
    system = build_system(defensive=False)

    per_model: dict[str, list[str] | None] = {}
    for model in models:
        try:
            result = call_target(model, system, clean)
            per_model[model] = result.ranking or None
        except Exception as exc:
            per_model[model] = None
            per_model[f"{model}__error"] = str(exc)[:200]  # type: ignore[assignment]

    rankings = [tuple(r) for r in per_model.values() if isinstance(r, list) and r]
    counts = Counter(rankings)
    agreed = len(counts) == 1 and len(rankings) == len(models)
    consensus = list(rankings[0]) if agreed else None

    if agreed:
        with connect() as conn:
            conn.execute(
                "UPDATE material SET ground_truth_ranking=?, gt_verified=1 WHERE id=?",
                (json.dumps(consensus), material_id),
            )

    return {
        "material_id": material_id,
        "agreed": agreed,
        "consensus": consensus,
        "n_models": len(models),
        "distinct_rankings": [list(k) for k in counts],
        "per_model": per_model,
    }
