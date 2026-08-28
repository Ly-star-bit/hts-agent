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
import hashlib
import json
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
FINGERPRINT_FILE = os.path.join(DATA_DIR, ".db_fingerprint.json")
CHANGES_FILE = os.path.join(DATA_DIR, ".db_changes.json")

# 指纹结构版本。新增字段时递增：旧快照里没有的类别不能拿来对比，
# 否则整批数据会被误报成"新增"。见 compare_with_fingerprint 里的 _新纳入 处理。
SCHEMA = 2

# 逐码对比的表：字段名 → (变动类型, 取值函数)
# 此前只覆盖 rates_8.general / sec301_map / c99_percent 三项，
# 附加税与 FLIP 301 完全在监控之外——FLIP 是 12.5% 的税，豁免清单整体换掉
# 也只会显示"无变化"。
_CODE_TABLES = {
    "add_duty": "附加税变化",
    "sec301_map_10": "301十位映射变化",
}

# 结构化配置表：不是 编码→值 的映射，逐码对比无意义，改为比摘要 + 规模。
# 内容一变就报一条，让人知道要去看这份数据本身。
_DIGEST_TABLES = {
    "flip301": "FLIP 301 措施定义",
    "flip301_exemptions": "FLIP 301 豁免清单",
    "vietnam": "越南措施",
    "flip_301": "301 档位调整记录",
}


def _digest(obj):
    """结构化配置的内容摘要（键序无关）"""
    blob = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def _exemption_scale(db):
    """FLIP 301 豁免清单的规模，用于把"变了"具体成"变了多少"" """
    e = db.get("flip301_exemptions") or {}
    by = e.get("by_economy") or {}
    return {
        "universal": len(e.get("universal") or []),
        "economies": len(by),
        "by_economy_codes": sum(len(v or []) for v in by.values()),
    }


def _fingerprint(db):
    """
    提取用于对比的关键字段快照。

    逐码表存明细（能定位到具体编码），结构化配置只存摘要与规模
    （逐码对比无意义，但内容变了必须让人知道）。
    """
    snap = {
        "_schema": SCHEMA,
        "rates_8": {k: {"general": v.get("general", "")} for k, v in db["rates_8"].items()},
        "sec301_map": db["sec301_map"],
        "c99_percent": db["c99_percent"],
    }
    for key in _CODE_TABLES:
        snap[key] = dict(db.get(key) or {})
    snap["_digests"] = {k: _digest(db.get(k)) for k in _DIGEST_TABLES}
    snap["_exemption_scale"] = _exemption_scale(db)
    return snap


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
      'stats': {...},            # 各类变动的完整计数（不受明细上限影响）
      'changes': [...],          # 变动明细（限量 MAX_CHANGES 条）
      'truncated': bool,         # 明细是否被截断
      'omitted': n,              # 被截断掉的明细条数
      'new_categories': [...],   # 本次新纳入对比、无历史可比的类别
      'built_at': str,
    }
    """
    old = _load_old()
    if old is None:
        return {"first_build": True, "stats": {}, "changes": [], "built_at": built_at}

    stats = {"added": 0, "removed": 0, "rate_changed": 0, "sec301_changed": 0,
             "add_duty_changed": 0, "sec301_10_changed": 0, "measure_changed": 0}
    changes = []
    total = 0  # 变动总数，与 changes 的长度解耦

    def record(kind, code, desc, old_val, new_val, stat_key):
        """
        计数与明细分开：计数永远累加，明细满了才停止追加。

        原实现在循环顶部 `if len(changes) >= MAX: break`——一旦攒够 300 条，
        统计数字也跟着停在那里。用户看到的 stats 是个被腰斩的数，
        却没有任何迹象表明它不完整。
        """
        nonlocal total
        stats[stat_key] += 1
        total += 1
        if len(changes) < MAX_CHANGES:
            changes.append({"类型": kind, "编码": _fmt(code), "描述": desc,
                            "旧": old_val, "新": new_val})

    MAX_CHANGES = 300

    # ---- 措施级变动最先处理 ----
    # 它们至多几条，却是影响面最大的（FLIP 301 豁免清单整体更换 = 12.5% 的税
    # 对上千个编码的适用性全变）。放在逐码变动之后会被 500 条税率变动挤出
    # 明细上限，用户就只看到一堆琐碎的税率调整，反而漏掉真正该看的那条。
    new_categories = []
    old_digests = old.get("_digests")
    if old_digests is None:
        new_categories.extend(_DIGEST_TABLES.values())
    else:
        old_scale = old.get("_exemption_scale") or {}
        new_scale = _exemption_scale(db)
        for key, label in _DIGEST_TABLES.items():
            if key not in old_digests:
                new_categories.append(label)
                continue
            if old_digests[key] == _digest(db.get(key)):
                continue
            if key == "flip301_exemptions":
                detail_old = (f"豁免 {old_scale.get('universal', '?')} 项通用 + "
                              f"{old_scale.get('economies', '?')} 个经济体"
                              f"/{old_scale.get('by_economy_codes', '?')} 项")
                detail_new = (f"豁免 {new_scale['universal']} 项通用 + "
                              f"{new_scale['economies']} 个经济体"
                              f"/{new_scale['by_economy_codes']} 项")
            else:
                detail_old, detail_new = "（内容已变）", "（内容已变）"
            record("措施数据变化", "", label, detail_old, detail_new, "measure_changed")

    # 新增 / 删除 / 税率变化
    old_rates = old.get("rates_8", {})
    new_rates = db["rates_8"]
    for code in sorted(set(old_rates) | set(new_rates)):
        old_info, new_info = old_rates.get(code), new_rates.get(code)
        desc = (new_info or old_info or {}).get("desc", "")
        if old_info is None:
            record("新增子目", code, desc, "", (new_info or {}).get("general", ""), "added")
        elif new_info is None:
            record("删除子目", code, desc, old_info.get("general", ""), "", "removed")
        elif old_info.get("general") != new_info.get("general"):
            record("税率变化", code, desc,
                   old_info.get("general", ""), new_info.get("general", ""), "rate_changed")

    # 301 归属 / 加征比例变化
    old_map, new_map = old.get("sec301_map", {}), db["sec301_map"]
    old_c99, new_c99 = old.get("c99_percent", {}), db["c99_percent"]
    for code in sorted(set(old_map) | set(new_map)):
        oc, nc = old_map.get(code, ""), new_map.get(code, "")
        op = old_c99.get(oc) if oc else None
        np = new_c99.get(nc) if nc else None
        if oc != nc or op != np:
            record("301变化", code, (db["rates_8"].get(code, {})).get("desc", ""),
                   f"{_fmt(oc)} {('+' + str(op) + '%') if op else ''}".strip(),
                   f"{_fmt(nc)} {('+' + str(np) + '%') if np else ''}".strip(),
                   "sec301_changed")

    # 其余逐码表（附加税、301 十位映射）
    for key, kind in _CODE_TABLES.items():
        if key not in old:
            # 旧快照里没有这一类：不能对比，否则整批会被误报成"新增"
            new_categories.append(kind)
            continue
        ov, nv = old.get(key) or {}, db.get(key) or {}
        stat_key = "add_duty_changed" if key == "add_duty" else "sec301_10_changed"
        for code in sorted(set(ov) | set(nv)):
            a, b = ov.get(code, ""), nv.get(code, "")
            if a != b:
                record(kind, code, (db["rates_8"].get(code[:8], {})).get("desc", ""),
                       str(a), str(b), stat_key)

    return {
        "first_build": False,
        "stats": stats,
        "changes": changes,
        "built_at": built_at,
        # 原实现写的是 len(stats) > MAX_CHANGES —— stats 是个 4 键字典，
        # 4 > 300 恒为 False，截断从未被报告过。
        "truncated": total > len(changes),
        "omitted": max(0, total - len(changes)),
        "total_changes": total,
        "new_categories": new_categories,
    }


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
