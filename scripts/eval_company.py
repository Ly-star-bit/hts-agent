# -*- coding: utf-8 -*-
"""
eval_company.py —— 用本公司历史申报做归类评测（"90%" 应该定义在这份数据上）

【为什么】CROSS 裁定金标是陌生商品、300 字规范英文描述，衡量的是"没见过的东西第一次归"。
日常流量多是报过的品类、几个字的中文品名。两者的准确率不是一回事，目标要分开定。

【数据】MongoDB products 集合（compare_db_301.py 同一套连接与字段）：HS_CODE / 中文品名 / 英文品名 / country。
只取 HS_CODE 在现行税则里、品名非空的记录，按固定种子抽 N 条 → data/eval/company_golden.json
（入不入 git 由你定：它含公司品名，默认 .gitignore 里不排除，请自行判断）。

【注意】历史申报的编码可能本身就错（compare_db_301.py 就是拿来对账的），所以这份金标是
"与过去一致率"，不是"正确率"。评测时同样跑 classify_product（按 ai_config.json 的升级配置），
报 top-1 / top-3 / 6 位；中文与英文品名各一份口径。

用法（在有 .env 的机器上）：
    python scripts/eval_company.py --build 200 [--collection products] [--database local|remote] [--env path/.env]
    python scripts/eval_company.py [--limit 50] [--field 中文品名|英文品名]
"""
import argparse
import datetime as dt
import json
import os
import random
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOLDEN = os.path.join(BASE_DIR, "data", "eval", "company_golden.json")
OUT_DIR = os.path.join(BASE_DIR, "output")


def _norm8(code):
    d = re.sub(r"\D", "", str(code or ""))
    return d[:8] if len(d) >= 8 else ""


def build(n=200, collection="products", database="local", env="", seed=20260916, log=print):
    sys.path.insert(0, BASE_DIR)
    import compare_db_301 as cdb   # 复用它的连接与字段约定（.env / 环境变量）
    import core
    db = core.load_db()
    alive = {c for c in db["rates_8"] if c[:2] not in ("98", "99")}
    client_db = cdb.connect(database, env or "")   # database = local / remote（compare_db_301 的选择器）
    col = client_db[collection]
    rows = []
    for doc in col.find({}, {"HS_CODE": 1, "中文品名": 1, "英文品名": 1, "country": 1}):
        c8 = _norm8(doc.get("HS_CODE"))
        if c8 not in alive:
            continue
        zh = str(doc.get("中文品名") or "").strip()
        en = str(doc.get("英文品名") or "").strip()
        if not zh and not en:
            continue
        rows.append({"id": str(doc.get("_id")), "金标": c8, "中文品名": zh, "英文品名": en,
                     "country": str(doc.get("country") or "")})
    random.Random(seed).shuffle(rows)
    items = rows[:n]
    os.makedirs(os.path.dirname(GOLDEN), exist_ok=True)
    with open(GOLDEN, "w", encoding="utf-8") as f:
        json.dump({"meta": {"built_at": dt.datetime.now().isoformat(timespec="seconds"), "collection": collection,
                            "n": len(items), "pool": len(rows), "seed": seed,
                            "note": "金标 = 历史申报 HS_CODE（与过去一致率，非正确率）"},
                   "items": items}, f, ensure_ascii=False, indent=1)
    log(f"公司金标 {len(items)} 条（可用记录 {len(rows)}）→ {GOLDEN}")
    return items


def evaluate(items, field="中文品名", log=print):
    import ai
    import core
    db = core.load_db()
    if ai.get_provider() is None:
        return {"error": "AI 未配置"}
    res = {"n": 0, "top1": 0, "top3": 0, "top1_6": 0, "升级": 0, "错误": 0, "耗时": 0.0, "field": field, "明细": []}
    for it in items:
        desc = it.get(field) or it.get("英文品名") or it.get("中文品名")
        if not desc:
            continue
        origin = it.get("country") or "CN"
        t = time.time()
        out = ai.classify_product(db, desc, origin=origin)
        res["耗时"] += time.time() - t
        res["n"] += 1
        if "error" in out:
            res["错误"] += 1
            res["明细"].append({"id": it["id"], "品名": desc[:60], "金标": it["金标"], "结果": "error"})
            continue
        picks = [_norm8(c["编码"]) for c in out["candidates"]]
        res["top1"] += bool(picks) and picks[0] == it["金标"]
        res["top3"] += it["金标"] in picks[:3]
        res["top1_6"] += bool(picks) and picks[0][:6] == it["金标"][:6]
        res["升级"] += out.get("归类方式") == "逐级"
        res["明细"].append({"id": it["id"], "品名": desc[:60], "金标": it["金标"], "结果": picks[:3],
                          "归类方式": out.get("归类方式", "平铺"), "升级原因": out.get("升级原因", "")})
        log(f"  {res['n']}/{len(items)} top1={res['top1']} top3={res['top3']}")
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description="本公司历史申报归类评测")
    ap.add_argument("--build", type=int, metavar="N")
    ap.add_argument("--collection", default="products")
    ap.add_argument("--database", default="local", choices=["local", "remote"], help="连接哪套库，同 compare_db_301")
    ap.add_argument("--env", default="", help=".env 文件路径（远程库配置 MONGO_*）")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--field", default="中文品名", choices=["中文品名", "英文品名"])
    a = ap.parse_args(argv)
    if a.build:
        build(a.build, collection=a.collection, database=a.database, env=a.env)
        return 0
    if not os.path.exists(GOLDEN):
        print(f"金标不存在：{GOLDEN}，先 --build N（需要 .env 里的 MongoDB 凭据）")
        return 1
    with open(GOLDEN, encoding="utf-8") as f:
        items = json.load(f)["items"]
    if a.limit:
        items = items[:a.limit]
    res = evaluate(items, field=a.field)
    if "error" in res:
        print(res["error"])
        return 1
    m = res["n"] or 1
    print(f"公司金标 {res['n']} 条（{a.field}）：top1={res['top1']/m:.2f} top3={res['top3']/m:.2f} "
          f"6位top1={res['top1_6']/m:.2f} 升级 {res['升级']} 错误 {res['错误']} 平均 {res['耗时']/m:.1f}s")
    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, f"eval_company_{dt.datetime.now():%Y%m%d_%H%M%S}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    print("报告：", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
