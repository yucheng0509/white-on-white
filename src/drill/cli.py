"""終端報告工具。

`python -m drill.cli report <run_id>` 會印出四配置對照、強度梯度與跨模型比較。
影片拍攝與詢答時直接投這個畫面，不必等前端做完。
"""

from __future__ import annotations

import argparse
import json
import sys

from drill.db import connect
from drill.report import build_matrix, build_report

_BAR_WIDTH = 18


def _bar(rate: float | None) -> str:
    if rate is None:
        return ""
    return "█" * round(rate * _BAR_WIDTH)


def _fmt(cell: dict | None) -> str:
    if not cell or cell["n"] == 0:
        return "    —    "
    return f'{cell["k"]:>2}/{cell["n"]:<3}{cell["rate"]:>5.0%}'


def print_report(run_id: int) -> None:
    rep = build_report(run_id)
    from drill.config import CONFIGS

    configs = [c for c in CONFIGS if c in rep["by_config"]]

    print(f"\n演練 #{run_id}　狀態 {rep['status']}　已判定 {rep['graded_cases']} 案例")
    ex = rep["excluded"]
    if any(ex.values()):
        print(f"排除：錯誤 {ex['errors']}、解析失敗 {ex['parse_failures']}、無法判定 {ex['ungradable']}")

    ctrl = rep["control"]
    if ctrl["n"]:
        flag = "" if ctrl["rate"] == 1 else "　⚠️ 對照組本身就排錯，注入組的結果無法歸因"
        print(f"對照組（乾淨素材排序正確）：{ctrl['correct']}/{ctrl['n']}　{ctrl['rate']:.0%}{flag}")

    print("\n■ 防護措施有效性")
    for cfg in configs:
        v = rep["by_config"][cfg]
        eff = "（基準）" if cfg == "A" else (
            "—" if v["effectiveness"] is None else f"降低 {v['effectiveness']:.0%}"
        )
        print(f"  {cfg} {v['label']:<14} {v['k']:>3}/{v['n']:<4}{v['rate']:>6.0%} {_bar(v['rate']):<{_BAR_WIDTH}} {eff}")

    residual = rep["residual_risk"]
    if residual and residual["n"]:
        print(f"\n  ▸ 殘餘風險率（兩種防護都上，仍被操控）：{residual['k']}/{residual['n']} = {residual['rate']:.0%}")

    print("\n■ 注入類型 × 防護配置（k/n 與比率）")
    # 欄位只放配置代號，完整名稱上面那張表已經列過，截斷反而看不懂
    header = "".join(f"{'配置 ' + c:^13}" for c in configs)
    print(f"  {'注入類型':<16}{header}")
    rows = sorted({(v, s) for (v, s, _c) in rep["heatmap"]}, key=lambda x: (x[1], x[0]))
    for vis, strength in rows:
        label = f"{'隱藏' if vis == 'hidden' else '明文'}-S{strength}"
        line = f"  {label:<18}"
        for cfg in configs:
            line += f'{_fmt(rep["heatmap"].get((vis, strength, cfg))):^13}'
        print(line)

    print("\n■ 受測模型（全配置合計）")
    for model, cell in rep["by_model"].items():
        print(f"  {model:<24} {cell['k']:>3}/{cell['n']:<4}{cell['rate']:>6.0%} {_bar(cell['rate'])}")

    if len(rep["by_source"]) > 1:
        print("\n■ 樣本來源")
        names = {"seed": "人工種子（指令型）", "redteam": "紅隊生成（非指令型）"}
        for src, cell in rep["by_source"].items():
            print(f"  {names.get(src, src):<24} {cell['k']:>3}/{cell['n']:<4}{cell['rate']:>6.0%}")

    d = rep["disclosure"]
    if d["n"]:
        print(f"\n■ 揭露率（模型主動指出素材含可疑指令）：{d['k']}/{d['n']} = {d['rate']:.0%}")
        print("  注意：以關鍵字規則判定，須人工抽樣複核後才可寫進正式報告")
    print()


def print_runs() -> None:
    with connect() as conn:
        rows = conn.execute(
            """SELECT id, status, done_cases, total_cases, est_usd, actual_usd, created_at
               FROM drill_run ORDER BY id DESC"""
        ).fetchall()
    for r in rows:
        print(
            f"#{r['id']:<4}{r['status']:<9}{r['done_cases']:>4}/{r['total_cases']:<5}"
            f"估 ${r['est_usd'] or 0:.3f}　實 ${r['actual_usd'] or 0:.3f}　{r['created_at']}"
        )


def print_matrix(run_ids: list[int]) -> None:
    from drill.config import CONFIG_LABELS, CONFIGS

    mx = build_matrix(run_ids)
    configs = [c for c in CONFIGS if c in mx["totals"]]
    if mx["mixed_materials"]:
        print(f"⚠️ 這些 run 用了不同素材（id {mx['material_ids']}），合併後的數字不可直接比較")

    print(f"\n注入類型 × 防護配置　（演練 #{', #'.join(map(str, run_ids))}）\n")
    print(f"  {'':<14}" + "".join(f"{c:^11}" for c in configs))
    rows = sorted(
        {(v, s) for (v, s, _c) in mx["cells"]}, key=lambda x: (x[0] != "hidden", x[1])
    )
    for vis, strength in rows:
        label = f"{'隱藏' if vis == 'hidden' else '明文'}-S{strength}"
        line = f"  {label:<16}"
        for cfg in configs:
            cell = mx["cells"].get((vis, strength, cfg))
            line += f"{'—':^11}" if not cell else f"{cell['rate']:^10.0%} "
        print(line)
    print(f"\n  {'合計':<15}" + "".join(
        f"{mx['totals'][c]['rate']:^10.0%} " for c in configs
    ))
    print()
    for c in configs:
        t = mx["totals"][c]
        print(f"  {c:<3}{CONFIG_LABELS.get(c, ''):<18} {t['k']:>3}/{t['n']}")
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="drill", description="AI 操控風險演練平台")
    sub = parser.add_subparsers(dest="command", required=True)
    p_report = sub.add_parser("report", help="印出一次演練的完整報告")
    p_report.add_argument("run_id", type=int)
    p_report.add_argument("--json", action="store_true", help="輸出原始 JSON")
    sub.add_parser("runs", help="列出所有演練批次")
    p_retry = sub.add_parser("retry", help="補跑一次演練裡失敗的案例")
    p_retry.add_argument("run_id", type=int)
    p_matrix = sub.add_parser("matrix", help="跨多次演練合併出完整對照矩陣")
    p_matrix.add_argument("run_ids", type=int, nargs="+")

    args = parser.parse_args(argv)
    if args.command == "runs":
        print_runs()
    elif args.command == "matrix":
        print_matrix(args.run_ids)
    elif args.command == "retry":
        from drill.runner import retry_failed_cases

        result = retry_failed_cases(args.run_id)
        print(
            f"重試 {result['attempted']} 筆："
            f"救回 {result['recovered']}、仍失敗 {result['still_failing']}"
        )
    elif args.command == "report":
        if args.json:
            rep = build_report(args.run_id)
            # heatmap 的 key 是 tuple，JSON 不支援，轉成字串
            rep["heatmap"] = {"|".join(map(str, k)): v for k, v in rep["heatmap"].items()}
            rep["by_kind"] = {"|".join(map(str, k)): v for k, v in rep["by_kind"].items()}
            print(json.dumps(rep, ensure_ascii=False, indent=2))
        else:
            print_report(args.run_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
