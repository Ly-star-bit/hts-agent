# -*- coding: utf-8 -*-
"""
db_diff.py —— 数据库版本对比与变动追踪

用途：官方税率数据每月更新后，重建数据库时自动对比上一版本，
      输出变动清单（新增/删除/税率变化/301 变化），供 Web 端"数据变动提醒"展示。

机制：
  - build_db.py 构建完成后调用 save_fingerprint()，把本次库的关键字段快照写入
    data/.db_fingerprint.json
  - 下次构建时调用 compare_with_fingerprint() 加载旧快照与新库对比，得到变动清单，
    并把变动清单写入 data/.db_changes.json（供 /api/changes 读取）
"""
import json
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
FINGERPRINT_FILE = os.path.join(DATA_DIR, ".db_fingerprint.json")
CHANGES_FILE = os.path.join(DATA_DIR, ".db_changes.json")


def _fingerprint(db):
    """提取用于对比的关键字段快照：基础税率 + 301 归属 + 加征比例"""
    return {
        "rates_8": {k: {"general": v.get("general", "")} for k, v in db["rates_8"].items()},
        "sec301_map": db["sec301_map"],
        "c99_percent": db["c99_percent"],
    }


def save_fingerprint(db):
    """保存当前库的关键字段快照"""
    os.makedirs(DATA_DIR, exist_ok=True)
    snap = _fingerprint(db)
    snap["_built_at"] = None  # 占位，构建时间由 build_db 传入
    with open(FINGERPRINT_FILE, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, separators=(",", ":"))
    return FINGERPRINT_FILE


def _load_old():
    if not os.path.exists(FINGERPRINT_FILE):
        return None
    try:
        with open(FINGERPRINT_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def compare_with_fingerprint(db, built_at=None):
    """
    对比当前库与上一版本指纹，返回变动清单。

    返回: {
      'first_build': bool,       # 首次构建（无历史指纹）
      'stats': {'added': n, 'removed': n, 'rate_changed': n, 'sec301_changed': n},
      'changes': [...],          # 变动明细（限量 300 条）
      'built_at': str,
    }
    """
    old = _load_old()
    if old is None:
        return {"first_build": True, "stats": {}, "changes": [], "built_at": built_at}

    old_rates = old.get("rates_8", {})
    old_map = old.get("sec301_map", {})
    old_c99 = old.get("c99_percent", {})
    new_rates = db["rates_8"]
    new_map = db["sec301_map"]
    new_c99 = db["c99_percent"]

    stats = {"added": 0, "removed": 0, "rate_changed": 0, "sec301_changed": 0}
    changes = []
    MAX_CHANGES = 300

    # 新增 / 删除 / 税率变化
    all_codes = set(old_rates) | set(new_rates)
    for code in sorted(all_codes):
        if len(changes) >= MAX_CHANGES:
            break
        old_info = old_rates.get(code)
        new_info = new_rates.get(code)
        desc = (new_info or old_info or {}).get("desc", "")
        fmt_code = _fmt(code)
        if old_info is None:
            stats["added"] += 1
            changes.append({"类型": "新增子目", "编码": fmt_code, "描述": desc,
                            "旧": "", "新": (new_info or {}).get("general", "")})
        elif new_info is None:
            stats["removed"] += 1
            changes.append({"类型": "删除子目", "编码": fmt_code, "描述": desc,
                            "旧": old_info.get("general", ""), "新": ""})
        elif old_info.get("general") != new_info.get("general"):
            stats["rate_changed"] += 1
            changes.append({"类型": "税率变化", "编码": fmt_code, "描述": desc,
                            "旧": old_info.get("general", ""), "新": new_info.get("general", "")})

    # 301 归属 / 加征比例变化
    old_c99_map = {code: old_map.get(code, "") for code in set(old_map)}
    new_c99_map = {code: new_map.get(code, "") for code in set(new_map)}
    all_mapped = set(old_c99_map) | set(new_c99_map)
    for code in sorted(all_mapped):
        if len(changes) >= MAX_CHANGES:
            break
        oc, nc = old_c99_map.get(code, ""), new_c99_map.get(code, "")
        op = old_c99.get(oc) if oc else None
        np = new_c99.get(nc) if nc else None
        if oc != nc or op != np:
            stats["sec301_changed"] += 1
            desc = (db["rates_8"].get(code, {})).get("desc", "")
            changes.append({"类型": "301变化", "编码": _fmt(code), "描述": desc,
                            "旧": f"{_fmt(oc)} {('+' + str(op) + '%') if op else ''}".strip(),
                            "新": f"{_fmt(nc)} {('+' + str(np) + '%') if np else ''}".strip()})

    result = {
        "first_build": False,
        "stats": stats,
        "changes": changes,
        "built_at": built_at,
        "truncated": len(stats) > MAX_CHANGES or False,
    }
    return result


def save_changes(changes_result):
    """把变动清单写入 data/.db_changes.json（供 Web 端读取）"""
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CHANGES_FILE, "w", encoding="utf-8") as f:
        json.dump(changes_result, f, ensure_ascii=False, separators=(",", ":"))
    return CHANGES_FILE


def load_changes():
    """读取最近一次构建的变动清单；无文件时返回 None"""
    if not os.path.exists(CHANGES_FILE):
        return None
    try:
        with open(CHANGES_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _fmt(code):
    if not code:
        return ""
    if len(code) == 10:
        return f"{code[0:4]}.{code[4:6]}.{code[6:8]}.{code[8:10]}"
    if len(code) == 8:
        return f"{code[0:4]}.{code[4:6]}.{code[6:8]}"
    if len(code) == 6:
        return f"{code[0:4]}.{code[4:6]}"
    return code
