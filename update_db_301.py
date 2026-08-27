# -*- coding: utf-8 -*-
"""
update_db_301.py —— 用官方判定校准数据库中产品的 301 加征

流程（安全设计）：
  0. 连接 MongoDB（默认线上库，配置从 .env 读取）
  1. 【必须】备份 products 集合：
       - 库内副本：products_backup_<时间戳>（$out 复制）
       - 本地文件：output/backup/products_<时间戳>.json（完整导出）
  2. 逐条判定 HS_CODE，找出与库中「加征.加征_301 / 豁免代码」不一致的文档
  3. 计划模式（默认）：只打印将修改的清单，不写库
  4. --execute 模式：执行更新（仅修改 加征.加征_301 与 豁免代码 两个字段）

用法：
  python update_db_301.py                    # 计划模式（不写库，先看要改什么）
  python update_db_301.py --execute          # 执行更新（自动先备份）
  python update_db_301.py --env D:\\RPAProject\\web_vba\\.env --database remote --execute

说明：
  - 判定依据：2026 现行 HTS 官方数据（scripts/core.py）
  - 仅处理「官方有 301 判定、且与库值不一致」的记录（补齐/更正）
  - 对「官方判定无、但库里有记录」的条目不删除（保守处理，仅提示）
  - 建议修改后重新运行 compare_db_301.py 验证
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts"))
import core

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BACKUP_DIR = os.path.join(BASE_DIR, "output", "backup")

LOCAL_MONGO_CONFIG = {
    "host": "192.168.20.111",
    "port": 27018,
    "username": "luoyu",
    "password": "luoyu123456",
    "database": "qingguan",
}


def parse_db_pct(value):
    if value is None:
        return 0.0
    s = str(value).strip()
    if not s or s.lower() in ("nan", "none", "-"):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def fmt_pct(pct):
    """官方判定值转库中字符串格式：0.075 → '0.075'，0.25 → '0.25'，1.0 → '1'"""
    return f"{pct:.4f}".rstrip("0").rstrip(".") if pct else ""


def connect(database, env_path=""):
    from pymongo import MongoClient

    if database == "remote":
        if env_path:
            load_dotenv(env_path, override=True)
        cfg = {
            "host": os.getenv("MONGO_HOST"),
            "port": os.getenv("MONGO_PORT"),
            "username": os.getenv("MONGO_USER"),
            "password": os.getenv("MONGO_PASS"),
            "database": os.getenv("MONGO_DB"),
        }
        missing = [k for k, v in cfg.items() if not v]
        if missing:
            sys.exit("错误：缺少环境变量 " + ", ".join(missing) + "（可用 --env 指定 .env 文件）")
        uri = f"mongodb://{cfg['username']}:{cfg['password']}@{cfg['host']}:{cfg['port']}"
        client = MongoClient(uri, serverSelectionTimeoutMS=15000)
    else:
        cfg = LOCAL_MONGO_CONFIG
        client = MongoClient(
            host=cfg["host"], port=cfg["port"],
            username=cfg["username"], password=cfg["password"],
            serverSelectionTimeoutMS=15000,
        )
    return client[cfg["database"]]


def backup_products(db, col, collection):
    """备份集合：库内副本 + 本地 JSON 文件。返回 (备份集合名, 本地文件路径, 文档数)"""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_name = f"{collection}_backup_{ts}"
    os.makedirs(BACKUP_DIR, exist_ok=True)

    # ① 库内副本（$out 聚合复制）
    col.aggregate([{"$out": backup_name}])
    backup_col = db[backup_name]
    count = backup_col.count_documents({})
    if count != col.count_documents({}):
        raise RuntimeError(f"库内备份数量不一致（源 {col.count_documents({})} vs 备份 {count}），已中止")

    # ② 本地 JSON 文件
    local_path = os.path.join(BACKUP_DIR, f"{collection}_{ts}.json")
    docs = list(col.find({}, {"_id": 1, "HS_CODE": 1, "加征": 1, "豁免代码": 1, "中文品名": 1, "英文品名": 1, "country": 1}))
    with open(local_path, "w", encoding="utf-8") as f:
        json.dump(docs, f, ensure_ascii=False, default=str)

    print(f"✅ 备份完成：\n  库内副本: {backup_name}（{count} 条）\n  本地文件: {os.path.abspath(local_path)}")
    return backup_name, local_path, count


def main():
    parser = argparse.ArgumentParser(description="用官方判定校准数据库 301 加征")
    parser.add_argument("--database", default="remote", choices=["local", "remote"], help="连接的数据库，默认 remote")
    parser.add_argument("--env", default="", help=".env 文件路径（远程库配置）")
    parser.add_argument("--collection", default="products", help="要校准的集合名，默认 products（如 products_sea）")
    parser.add_argument("--execute", action="store_true", help="真正写库（默认只打印计划）")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 条（测试用）")
    args = parser.parse_args()

    try:
        db = core.load_db()
    except FileNotFoundError as e:
        sys.exit(f"错误：{e}")

    print(f"连接 {args.database} 数据库 ...")
    mdb = connect(args.database, args.env)
    col = mdb[args.collection]
    total = col.count_documents({})
    print(f"{args.collection} 集合共 {total} 条")

    # ---------- 第一步：备份（无论是否执行都先备份） ----------
    print("\n[1/4] 备份集合 ...")
    backup_name, local_path, backup_count = backup_products(mdb, col, args.collection)

    # ---------- 第二步：计算修改计划 ----------
    print("\n[2/4] 逐条判定，生成修改计划 ...")
    plan = []  # (doc_id, hs_code, 品名, 旧值, 新值, 旧子目, 新子目, 类型)
    cursor = col.find({})
    if args.limit:
        cursor = cursor.limit(args.limit)
    for doc in cursor:
        hs_raw = doc.get("HS_CODE")
        add_map = doc.get("加征") or {}
        old_pct = parse_db_pct(add_map.get("加征_301"))
        old_code = str(doc.get("豁免代码") or "").strip()

        codes = core.extract_codes(str(hs_raw)) if hs_raw is not None else []
        if not codes:
            continue  # 无法解析的跳过，不改
        # 按集合中的 country 字段选择原产地轨道（越南集合不套用中国 301）
        origin = "VN" if str(doc.get("country") or "").strip().upper() in ("VN", "VIETNAM", "越南") else "CN"
        result = core.query_one(db, codes[0], origin=origin)
        # 官方 301加征 文本如 "+25%" → 0.25
        m = re.search(r"([\d.]+)\s*%", result["301加征"])
        new_pct = float(m.group(1)) / 100 if m else 0.0
        new_code = result["9903子目"]

        if new_pct <= 0:
            continue  # 官方无 301 的，不删不改
        if abs(old_pct - new_pct) < 1e-4 and old_code == new_code:
            continue  # 已一致

        plan.append({
            "_id": doc["_id"], "HS_CODE": hs_raw, "品名": doc.get("中文品名", ""),
            "旧值": old_pct, "新值": new_pct,
            "旧子目": old_code, "新子目": new_code,
            "类型": "税率/子目不一致" if old_pct > 0 else "数据库缺失",
        })

    print(f"需要更新的记录: {len(plan)} 条")
    for i, p in enumerate(plan[:15], 1):
        print(f"  {i}. {p['HS_CODE']} | 库: {p['旧值'] or '无'} ({p['旧子目'] or '-'}) "
              f"→ 官方: {fmt_pct(p['新值'])} ({p['新子目']}) | {p['类型']} | {p['品名']}")
    if len(plan) > 15:
        print(f"  ...（其余 {len(plan) - 15} 条见完整计划，执行时全部更新）")

    if not plan:
        print("没有需要更新的记录。")
        return

    if not args.execute:
        print("\n⚠️  计划模式：未写库。确认无误后执行：python update_db_301.py --execute")
        return

    # ---------- 第三步：执行更新 ----------
    print(f"\n[3/4] 执行更新（{len(plan)} 条）...")
    from pymongo import UpdateOne

    ops = [
        UpdateOne(
            {"_id": p["_id"]},
            {"$set": {"加征.加征_301": fmt_pct(p["新值"]), "豁免代码": p["新子目"]}},
        )
        for p in plan
    ]
    result = col.bulk_write(ops, ordered=False)
    print(f"✅ 更新完成：matched {result.matched_count} | modified {result.modified_count} | 失败 {len(plan) - result.modified_count}")

    # ---------- 第四步：验证 ----------
    print("\n[4/4] 验证更新结果 ...")
    still = 0
    for p in plan:
        doc = col.find_one({"_id": p["_id"]})
        if doc:
            cur = parse_db_pct((doc.get("加征") or {}).get("加征_301"))
            if abs(cur - p["新值"]) >= 1e-4:
                still += 1
    print(f"验证完成：{len(plan) - still}/{len(plan)} 条已更新到位" if still == 0 else f"⚠ {still} 条未更新成功，请检查")

    print(f"\n如需回滚：将 {backup_name} 集合重命名为 products（或使用本地备份 {local_path}）")


if __name__ == "__main__":
    main()
