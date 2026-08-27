# -*- coding: utf-8 -*-
"""
compare_db_301.py —— 对比数据库中产品的 301 加征与官方判定是否一致

逻辑：
  1. 从 MongoDB products 集合读取每个产品的 HS_CODE 及数据库记录的「加征.加征_301」
  2. 用本项目的核心判定逻辑（scripts/core.py，基于 USITC + USTR 官方数据）
     重新计算该 HS_CODE 的 301 加征
  3. 两者对比，列出不一致的产品（差异清单）

输出：
  - output/db_301_对比_时间戳.xlsx  （Sheet1 差异清单 / Sheet2 全部结果）
  - output/db_301_对比_时间戳.csv   （差异清单）

用法：
  python compare_db_301.py                     # 默认连接本地库，全量对比
  python compare_db_301.py --database remote   # 连接远程库（需 MONGO_* 环境变量）
  python compare_db_301.py --env D:\\RPAProject\\web_vba\\.env --database remote  # 从指定 .env 加载远程配置
  python compare_db_301.py --limit 500         # 只取前 500 条（快速验证）
  python compare_db_301.py -o 我的差异报告.xlsx

说明：
  - 数据库连接配置与 web_vba/sync_mongo.py 保持一致
  - 远程库配置从 MONGO_HOST/PORT/USER/PASS/DB 环境变量读取：
      ① 自动加载当前目录的 .env 文件
      ② 或用 --env 指定 .env 文件路径（如 web_vba 目录下的 .env）
  - 判定依据为 2026 现行 HTS 官方数据；数据库记录可能基于旧清单，属正常差异
"""
import argparse
import csv
import os
import re
import sys
from datetime import datetime

# 加载 .env（dotenv 可用时；找不到也不影响本地库模式）
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts"))
import core

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(BASE_DIR, "output")

# 本地 MongoDB 连接配置（与 web_vba/sync_mongo.py 一致）
LOCAL_MONGO_CONFIG = {
    "host": "192.168.20.111",
    "port": 27018,
    "username": "luoyu",
    "password": "luoyu123456",
    "database": "qingguan",
}

# 远程配置从环境变量读取（可用 .env 提供）
REMOTE_MONGO_CONFIG = {
    "host": os.getenv("MONGO_HOST"),
    "port": os.getenv("MONGO_PORT"),
    "username": os.getenv("MONGO_USER"),
    "password": os.getenv("MONGO_PASS"),
    "database": os.getenv("MONGO_DB"),
}


def parse_db_pct(value):
    """解析数据库加征数值：'0.075' → 0.075；None/''/'NaN' → 0.0"""
    if value is None:
        return 0.0
    s = str(value).strip()
    if not s or s.lower() in ("nan", "none", "-"):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def parse_tool_pct(result):
    """解析工具判定的 301 加征：'+7.5%' → 0.075；'' → 0.0"""
    m = re.search(r"([\d.]+)\s*%", result["301加征"])
    return float(m.group(1)) / 100 if m else 0.0


def classify(db_pct, tool_pct, hit):
    """差异分类"""
    if not hit and db_pct <= 0:
        return "一致"  # 双方都无 301 加征
    if hit and abs(db_pct - tool_pct) < 1e-4:
        return "一致"
    if db_pct > 0 and not hit:
        return "数据库有301、官方判定无"
    if db_pct <= 0 and hit:
        return "数据库缺失、官方判定有"
    if hit and abs(db_pct - tool_pct) >= 1e-4:
        return "税率数值不一致"
    return "其他"


def connect(database, env_path=""):
    """连接 MongoDB，返回数据库对象（集合由调用方取，支持任意集合名）"""
    from pymongo import MongoClient

    if database == "remote":
        # 用户显式指定 .env 时重新加载（覆盖自动加载）
        if env_path:
            load_dotenv(env_path, override=True)
        # 连接时动态读取环境变量（避免模块加载时的旧快照）
        cfg = {
            "host": os.getenv("MONGO_HOST"),
            "port": os.getenv("MONGO_PORT"),
            "username": os.getenv("MONGO_USER"),
            "password": os.getenv("MONGO_PASS"),
            "database": os.getenv("MONGO_DB"),
        }
        missing = [k for k, v in cfg.items() if not v]
        if missing:
            sys.exit(
                "错误：远程库缺少环境变量 " + ", ".join(missing) +
                "\n请任选一种方式提供：\n"
                "  1) --env 指定 .env 文件：python compare_db_301.py --env D:\\RPAProject\\web_vba\\.env --database remote\n"
                "  2) 在 hts_agent 目录放一个 .env（含 MONGO_HOST/PORT/USER/PASS/DB）\n"
                "  3) 或先设置系统环境变量"
            )
        uri = f"mongodb://{cfg['username']}:{cfg['password']}@{cfg['host']}:{cfg['port']}"
        client = MongoClient(uri, serverSelectionTimeoutMS=8000)
    else:
        cfg = LOCAL_MONGO_CONFIG
        client = MongoClient(
            host=cfg["host"],
            port=cfg["port"],
            username=cfg["username"],
            password=cfg["password"],
            serverSelectionTimeoutMS=8000,
        )
    return client[cfg["database"]]


def main():
    parser = argparse.ArgumentParser(description="对比数据库 301 加征与官方判定")
    parser.add_argument("--database", default="local", choices=["local", "remote"], help="连接的数据库，默认 local")
    parser.add_argument("--env", default="", help=".env 文件路径（远程库配置 MONGO_*），如 web_vba 目录的 .env")
    parser.add_argument("--collection", default="products", help="要对比的集合名，默认 products（如 products_sea）")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 条（0 表示全部）")
    parser.add_argument("-o", "--output", default="", help="输出 Excel 路径（默认 output/ 下自动命名）")
    args = parser.parse_args()

    # 加载核心数据库
    try:
        db = core.load_db()
    except FileNotFoundError as e:
        sys.exit(f"错误：{e}")

    # 连接业务数据库
    print(f"连接 {args.database} 数据库 {args.collection} 集合 ...")
    col = connect(args.database, args.env)[args.collection]

    # 查询字段：HS_CODE / 加征(嵌套) / 豁免代码 / 品名 / country
    projection = {
        "HS_CODE": 1, "加征": 1, "豁免代码": 1,
        "中文品名": 1, "英文品名": 1, "country": 1, "总税率": 1,
        "_id": 0,
    }
    cursor = col.find({}, projection)
    if args.limit:
        cursor = cursor.limit(args.limit)

    # 逐条判定与对比
    rows = []
    diff_rows = []
    stats = {"total": 0, "parsed": 0, "unparsed": 0, "consistent": 0,
             "db_only": 0, "tool_only": 0, "rate_diff": 0, "other": 0}

    for doc in cursor:
        stats["total"] += 1
        hs_raw = doc.get("HS_CODE")
        add_map = doc.get("加征") or {}
        db_pct = parse_db_pct(add_map.get("加征_301"))
        ex_code = str(doc.get("豁免代码") or "").strip()

        codes = core.extract_codes(str(hs_raw)) if hs_raw is not None else []
        if not codes:
            stats["unparsed"] += 1
            rows.append({
                "HS_CODE": hs_raw, "中文品名": doc.get("中文品名", ""), "英文品名": doc.get("英文品名", ""),
                "数据库加征301": db_pct, "官方判定301": "", "差异类型": "无法解析HS_CODE",
                "数据库豁免代码": ex_code, "官方9903子目": "", "country": doc.get("country", ""),
            })
            continue

        result = core.query_one(db, codes[0])
        tool_pct = parse_tool_pct(result)
        hit = result["301判定"] == "是"
        cls = classify(db_pct, tool_pct, hit)

        stats["parsed"] += 1
        if cls == "一致":
            stats["consistent"] += 1
        elif cls == "数据库有301、官方判定无":
            stats["db_only"] += 1
        elif cls == "数据库缺失、官方判定有":
            stats["tool_only"] += 1
        elif cls == "税率数值不一致":
            stats["rate_diff"] += 1
        else:
            stats["other"] += 1

        row = {
            "HS_CODE": hs_raw,
            "中文品名": doc.get("中文品名", ""),
            "英文品名": doc.get("英文品名", ""),
            "数据库加征301": db_pct,
            "官方判定301": f"{tool_pct:.4f}".rstrip("0").rstrip(".") if tool_pct else "",
            "差异类型": cls,
            "数据库豁免代码": ex_code,
            "官方9903子目": result["9903子目"],
            "country": doc.get("country", ""),
        }
        rows.append(row)
        if cls != "一致":
            diff_rows.append(row)

        if stats["total"] % 500 == 0:
            print(f"  已处理 {stats['total']} 条 ...")

    # 输出
    os.makedirs(OUT_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = args.output or os.path.join(OUT_DIR, f"db_301_对比_{ts}.xlsx")
    csv_path = os.path.splitext(out_path)[0] + ".csv"

    df_all = pd.DataFrame(rows)
    df_diff = pd.DataFrame(diff_rows)
    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        df_diff.to_excel(writer, sheet_name="差异清单", index=False)
        df_all.to_excel(writer, sheet_name="全部结果", index=False)
    # 差异清单 CSV（utf-8-sig 便于 Excel 打开）
    if df_diff.empty:
        pd.DataFrame([{"提示": "无差异"}]).to_csv(csv_path, index=False, encoding="utf-8-sig")
    else:
        df_diff.to_csv(csv_path, index=False, encoding="utf-8-sig")

    # 控制台统计
    print("\n" + "=" * 60)
    print(f"总产品数:       {stats['total']}")
    print(f"成功解析:       {stats['parsed']}（无法解析 HS_CODE: {stats['unparsed']}）")
    print(f"一致:           {stats['consistent']}")
    print(f"差异合计:       {len(diff_rows)}")
    print(f"  - 数据库有301、官方判定无: {stats['db_only']}")
    print(f"  - 数据库缺失、官方判定有: {stats['tool_only']}")
    print(f"  - 税率数值不一致:         {stats['rate_diff']}")
    print(f"  - 其他:                   {stats['other']}")
    print("=" * 60)
    print(f"✅ 报告已生成：\n  Excel: {os.path.abspath(out_path)}\n  CSV:   {os.path.abspath(csv_path)}")

    # 差异预览（前 10 条）
    if diff_rows:
        print("\n差异预览（前 10 条）：")
        for r in diff_rows[:10]:
            print(f"  {r['HS_CODE']} | 库:{r['数据库加征301']} vs 官方:{r['官方判定301'] or '无'} | {r['差异类型']} | {r['中文品名']}")


if __name__ == "__main__":
    main()
