# -*- coding: utf-8 -*-
"""
core.py —— HTS 多措施查询核心逻辑（命令行工具与 Web 前端共用）

提供：数据库加载、编码解析、单条多措施判定。
查询按「原产地 × 措施栈」泛化：
  - 中国（CN）：基础税率 + Section 301 加征（含 flip 历史）+ 附加税 + FLIP 301 强迫劳动关税
  - 越南（VN）：基础税率（MFN）+ 附加税 + FLIP 301 强迫劳动关税；不适用中国 301 加征
输出字段保持向后兼容（既有中国 301 字段名不变），并新增：
  原产地 / 301 flip历史 / 301 flip变化 / 越南措施 / FLIP 301加征
各加征措施（cn301 / flip301）可由 measures_config.json 配置启用/禁用（默认全启用，
禁用后查询/估算不含该加征字段、总税负不叠加该项）。
数据来源：data/sec301_db.json（由 build_db.py 生成，含 flip_301 / vietnam / flip301 分区）。
"""
import json
import os
import re

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_JSON = os.path.join(BASE_DIR, "data", "sec301_db.json")
MEASURES_CONFIG = os.path.join(BASE_DIR, "measures_config.json")
DEFAULT_MEASURES = {"cn301": True, "flip301": True}

ORIGIN_CN = "CN"
ORIGIN_VN = "VN"


# 配置缓存：(mtime, size) 作为失效键。批量查询会对每条编码调用 load_measures_config()，
# 无缓存时 1000 条编码 = 1000 次读盘 + JSON 解析。文件被改写（含 Web 端保存）后
# mtime/size 变化即自动失效，仍保持"改动配置立即生效、无需重启"。
_measures_cache = None       # (key, cfg)


def _measures_stat_key():
    """配置文件的失效键；文件不存在时为 None"""
    try:
        st = os.stat(MEASURES_CONFIG)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def load_measures_config():
    """
    读取加征措施开关配置（measures_config.json）。缺省或配置缺失某项 → 全部启用。
    结果按文件 mtime/size 缓存，配置改动后立即失效重读。
    返回 {'cn301': bool, 'flip301': bool}
    """
    global _measures_cache
    key = _measures_stat_key()
    if _measures_cache is not None and _measures_cache[0] == key:
        return dict(_measures_cache[1])

    cfg = dict(DEFAULT_MEASURES)
    if key is not None:
        try:
            with open(MEASURES_CONFIG, encoding="utf-8") as f:
                data = json.load(f)
            m = (data or {}).get("measures") or {}
            for k in cfg:
                if k in m:
                    cfg[k] = bool(m[k])
        except (json.JSONDecodeError, OSError):
            pass
    _measures_cache = (key, dict(cfg))
    return cfg


def _clear_measures_cache():
    """测试用：清空配置缓存（避免同一测试内多次改写文件时 mtime 精度不足导致命中旧值）"""
    global _measures_cache
    _measures_cache = None


def save_measures_config(updates):
    """
    合并更新加征措施开关配置（measures_config.json，原子写入），保存后立即生效。
    updates 为 {'cn301': bool, 'flip301': bool} 的部分更新。返回保存后的完整配置。
    """
    cfg = load_measures_config()
    for k, v in (updates or {}).items():
        if k in cfg:
            cfg[k] = bool(v)
    tmp = MEASURES_CONFIG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"measures": cfg}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, MEASURES_CONFIG)
    _clear_measures_cache()  # 写入后立即失效，不依赖 mtime 精度
    return cfg


def load_db():
    """加载合并查询数据库（带缓存）"""
    if not os.path.exists(DB_JSON):
        raise FileNotFoundError(
            f"找不到数据库 {DB_JSON}。请先运行: python scripts/build_db.py"
        )
    with open(DB_JSON, encoding="utf-8") as f:
        return json.load(f)


def fmt(code: str, length: int = 10) -> str:
    """把纯数字编码格式化为带点的标准形式。如 '01012100' → '0101.21.00'"""
    if length == 10:
        return f"{code[0:4]}.{code[4:6]}.{code[6:8]}.{code[8:10]}"
    return f"{code[0:4]}.{code[4:6]}.{code[6:8]}"


def extract_codes(text: str):
    """从任意文本中提取 HTS 编码（8位或10位，带点或不带点），去重保序"""
    pat = re.compile(r"\b\d{4}\.?\d{2}\.?\d{2}\.?\d{0,4}\b")
    seen, out = set(), []
    for m in pat.finditer(text or ""):
        code = re.sub(r"\D", "", m.group())
        if len(code) in (8, 10) and code not in seen:
            seen.add(code)
            out.append(code)
    return out


def _fmt_c99(c99):
    """9903 子目纯数字 → 带点格式（99038815 → 9903.88.15）"""
    if not c99:
        return ""
    if len(c99) >= 8:
        return f"{c99[0:4]}.{c99[4:6]}.{c99[6:8]}"
    return c99


# ---------- 301 flip 历史 ----------

def flip_info(db, code8, c99, pct):
    """
    301 措施 flip 历史：同一编码此前的加征档位（带生效日期），当前值取实时判定（2026 现行税则）。
    返回 (历史列表, 变化文本)。历史列表每项 {date, c99, pct, note}；无历史或当前未命中时为空。
    """
    flips = (db.get("flip_301") or {}).get("flips", {})
    history = flips.get(code8) or []
    if not history or not c99:
        return [], ""
    hist = sorted(history, key=lambda h: h.get("date", ""))
    hist_out = [
        {
            "date": h.get("date", ""),
            "c99": _fmt_c99(h.get("c99", "")),
            "pct": h.get("pct"),
            "note": h.get("note", ""),
        }
        for h in hist
    ]
    prev = hist[-1]
    prev_pct = f"+{prev.get('pct'):g}%" if prev.get("pct") else "0%(豁免)"
    cur_pct = f"+{pct:g}%" if pct else "0%(豁免)"
    # 当前档位版本年份：从数据源文件名（如 China Tariffs_2026HTSRev15.pdf）提取
    year = "最新"
    m = re.search(r"(20\d{2})", (db.get("meta") or {}).get("ustr_pdf", ""))
    if m:
        year = m.group(1) + " 现行"
    change = f"此前 {prev_pct}（{prev.get('date', '')}）→ 当前 {cur_pct}（{year}）"
    return hist_out, change


# ---------- 适用措施说明（越南 / 其他国家） ----------

def vietnam_info(db, code8, origin="VN"):
    """
    非中国原产地的适用措施说明：MFN 一般税率，不适用中国 301 加征。
      - VN：越南轨道，覆盖编码给出具体说明，未覆盖标注「数据未覆盖具体说明」
      - 其他：通用 MFN 轨道（任何其他国家）
    """
    v = db.get("vietnam") or {}
    if origin == "VN":
        if not v:
            return "数据未覆盖（缺越南措施数据源）"
        base = "适用美国 MFN 一般税率；不适用中国 301 加征。"
        covered = v.get("covered_codes") or []
        if code8 in covered:
            extra = (v.get("notes_per_code") or {}).get(code8, "")
            return base + (" " + extra if extra else "")
        return base + " 该编码暂无具体说明（数据未覆盖具体说明）。"
    # 其他国家：固定通用轨道（MFN）
    return "适用美国 MFN 一般税率；不适用中国 301 加征（其他国家通用轨道）。"


# ---------- FLIP 301 强迫劳动关税（2026-07-24 生效） ----------

def flip301_judge(db, origin_code, code8=""):
    """
    FLIP 301 强迫劳动调查关税（Section 301，2026-07-24 生效）按原产地国家查表。

    返回 (加征文本, 说明, 来源dict)：
      - "豁免"：编码命中 ANNEX II 豁免清单（通用 Part A / 经济体专属 / CAFTA-DR 纺织品），不加征
      - "+12.5%"：12.5% 档（all other investigated）：中国、香港、越南、新加坡、巴西等
      - "+10%"：10% 档：加拿大、墨西哥、印度、英国等 17 个
      - net-of-MFN：欧盟/台湾（合计 10%）、日本/韩国/瑞士（合计 12.5%）
      - 不在 60 名单：不适用
    豁免：已适用 Section 232 关税的产品、ANNEX II 清单。
    来源dict：{"文件", "位置", "Part", "范围限制"}，供 Web 端来源追溯弹窗使用。
    """
    f = db.get("flip301") or {}
    rates = f.get("rates") or {}
    ex = db.get("flip301_exemptions") or {}
    o = (origin_code or "").strip().upper()
    frn_file = (db.get("meta") or {}).get(
        "flip_frn_pdf", "FLIP 301 Investigation Final Action FRN 7-23-26 FINAL.pdf")

    def _src(page, part, scope):
        """统一构造来源字典（key 供前端映射 /api/source 路由）"""
        return {
            "key": "flip_frn",
            "文件": frn_file,
            "位置": f"第 {page} 页" if page else "",
            "Part": part,
            "范围限制": scope or "无",
        }

    # ⓪ 数据源可用性先于任何判定：缺税率表时无法确定该经济体是否在 60 名单内，
    #    此时若因命中 ANNEX II 而返回"豁免"，等于把"缺数据"说成"不加征"——必须显式标注未覆盖。
    if not f:
        return "", "数据未覆盖（缺 FLIP 301 数据源）", _src("", "", "")

    # ① ANNEX II 豁免清单判定（逐编码）：通用 Part A / 经济体专属 / CAFTA-DR（仅 JO/SV/GT）
    if code8:
        universal = ex.get("universal") or []
        by_econ = ex.get("by_economy") or {}
        if code8 in universal:
            scope = (ex.get("universal_scopes") or {}).get(code8, "")
            page = (ex.get("universal_pages") or {}).get(code8, "")
            scope_txt = f"，范围限制：{scope}（仅该范围商品豁免）" if scope else ""
            page_txt = f"（FRN 物理页 {page}）" if page else ""
            return "豁免", f"ANNEX II 通用豁免（Part A{scope_txt}），不适用 FLIP 301{page_txt}", _src(
                page, "ANNEX II Part A（通用豁免，所有被调查经济体）", scope)
        if code8 in by_econ.get(o, []):
            scope = ((ex.get("by_economy_scopes") or {}).get(o) or {}).get(code8, "")
            page = ((ex.get("by_economy_pages") or {}).get(o) or {}).get(code8, "")
            scope_txt = f"，范围限制：{scope}（仅该范围商品豁免）" if scope else ""
            page_txt = f"（FRN 物理页 {page}）" if page else ""
            return "豁免", f"ANNEX II 豁免（该经济体专属 Part{scope_txt}），不适用 FLIP 301{page_txt}", _src(
                page, f"ANNEX II 该经济体专属（{o}）", scope)
        if o in ("JO", "SV", "GT") and code8 in by_econ.get("CAFTA_DR", []):
            page = ((ex.get("by_economy_pages") or {}).get("CAFTA_DR") or {}).get(code8, "")
            page_txt = f"（FRN 物理页 {page}）" if page else ""
            return "豁免", f"ANNEX II Part O（CAFTA-DR 纺织品），不适用 FLIP 301{page_txt}", _src(
                page, "ANNEX II Part O（CAFTA-DR 免税纺织品）", "")
    if o in rates.get("10", []):
        return "+10%", f"FLIP 301 强迫劳动关税 10%（在 MFN 之上加征；已适用 Section 232 或 Annex 豁免产品除外）", _src(
            "", "FRN 税率表（10% 档）", "")
    if o in rates.get("net_mfn_10", []):
        return "+10%", "FLIP 301 合计 MFN+10%（MFN≥10% 则本税 0；已适用 Section 232 或 Annex 豁免产品除外）", _src(
            "", "FRN 税率表（net-of-MFN 10%）", "")
    if o in rates.get("net_mfn_125", []):
        return "+12.5%", "FLIP 301 合计 MFN+12.5%（MFN≥12.5% 则本税 0；已适用 Section 232 或 Annex 豁免产品除外）", _src(
            "", "FRN 税率表（net-of-MFN 12.5%）", "")
    if o in rates.get("125", []):
        return "+12.5%", "FLIP 301 强迫劳动关税 12.5%（在 MFN 之上加征；已适用 Section 232 或 Annex 豁免产品除外）", _src(
            "", "FRN 税率表（12.5% 档）", "")
    return "", f"{o} 不在 FLIP 301 被调查经济体名单（60 个），不适用", _src(
        "", "FRN 范围（60 经济体名单）", "")


# ---------- 主查询 ----------

def query_one(db, code, origin="CN"):
    """
    判定单个 HTS 编码在指定原产地下的措施栈，返回结果字典。
    code 为规范化后的纯数字（8位或10位）。origin 为国家代码（CN / VN / 其他）。
    加征措施（cn301 / flip301）按 measures_config.json 配置裁剪：禁用时输出不含
    该加征字段（如"301加征"、"FLIP 301加征"）、总税负不叠加该项。
    """
    rates_8 = db["rates_8"]
    desc_10 = db["desc_10"]
    add_duty = db["add_duty"]
    sec301_map = db["sec301_map"]
    c99_percent = db["c99_percent"]

    measures = load_measures_config()
    cn301_on = measures.get("cn301", True)
    flip301_on = measures.get("flip301", True)

    origin_raw = (origin or ORIGIN_CN).strip().upper()
    origin_code = origin_raw  # 保留国家代码（供 FLIP 301 查表）
    is_china = origin_raw == ORIGIN_CN

    n = len(code)
    code8 = code[:8] if n >= 8 else code

    # FLIP 301 强迫劳动关税判定（所有原产地按国家查表；配置禁用则不判定）
    if flip301_on:
        flip301_pct, flip301_note, flip301_src = flip301_judge(db, origin_code, code8=code8)
    else:
        flip301_pct, flip301_note, flip301_src = "", "FLIP 301 已禁用（配置）", {}

    # 基础信息（两种原产地共用）
    base = rates_8.get(code8, {})
    desc = desc_10.get(code) or base.get("desc", "")
    general = base.get("general", "")
    special = base.get("special", "")
    col2 = base.get("col2", "")
    add = add_duty.get(code) or add_duty.get(code8, "")

    # 中国：301 判定（既有逻辑）；非中国：MFN 通用轨道（不叠加中国 301）
    if is_china:
        c99 = sec301_map.get(code8)
        if c99:
            pct = c99_percent.get(c99)
            is301 = "是" if pct else "是(豁免/0%)"
            c99_fmt = _fmt_c99(c99)
            pct_txt = f"+{pct:g}%" if pct else "0%(豁免)"
            note = "命中301清单，具体以 USTR 豁免状态为准" if pct else "命中301但对应子目为豁免/排除(0%)"
        else:
            pct = None
            is301 = "否"
            c99_fmt = ""
            pct_txt = ""
            note = ""
        if n < 8:
            note = "⚠ 6位品目无法判定301，请提供8位子目" + ("；" + note if note else "")
            is301 = "无法判定"
        if not base:
            note = "⚠ 未在2026现行HTS税率表中找到该子目，可能为旧版编码" + ("；" + note if note else "")
        flip_hist, flip_change = flip_info(db, code8, c99, pct)
        vn_measures = ""
    else:  # 越南及其他国家：MFN 通用轨道（不叠加中国 301）
        if origin_code == ORIGIN_VN:
            is301 = "不适用（越南原产）"
            note = "越南原产：不适用中国 301 加征，适用美国 MFN 一般税率"
        else:
            is301 = "不适用（其他国家原产）"
            note = f"{origin_code} 原产：不适用中国 301 加征，适用美国 MFN 一般税率"
        c99_fmt = ""
        pct_txt = ""
        if n < 8:
            note = "⚠ 6位品目仅能给出品目级基础税率" + ("；" + note if note else "")
        if not base:
            note = "⚠ 未在2026现行HTS税率表中找到该子目，可能为旧版编码" + ("；" + note if note else "")
        flip_hist, flip_change = [], ""
        vn_measures = vietnam_info(db, code8, "VN" if origin_code == ORIGIN_VN else "OTHER")

    # 配置裁剪：禁用加征时备注标注，且不输出加征字段
    if not cn301_on:
        note = (note + "；301 加征已禁用（配置）") if note else "301 加征已禁用（配置）"
    if flip301_pct and flip301_pct != "豁免":
        note = (note + f"；FLIP 301 强迫劳动关税 {flip301_pct}") if note else f"FLIP 301 强迫劳动关税 {flip301_pct}"
    elif flip301_pct == "豁免":
        note = (note + "；FLIP 301 豁免（ANNEX II 清单）") if note else "FLIP 301 豁免（ANNEX II 清单）"
    elif not flip301_on:
        note = (note + "；FLIP 301 已禁用（配置）") if note else "FLIP 301 已禁用（配置）"

    result = {
        "输入编码": fmt(code, 10) if n == 10 else (fmt(code, 8) if n == 8 else code),
        "8位子目": fmt(code8, 8) if len(code8) == 8 else code8,
        "商品描述": desc or "（无描述，见备注）",
        "一般税率": general,
        "特殊税率": special,
        "第二栏税率": col2,
        "301判定": is301,
        "9903子目": c99_fmt,
        "附加税": add,
        "备注": note,
        # ---- v1.3 新增字段 ----
        "原产地": "中国" if is_china else ("越南" if origin_code == ORIGIN_VN else "其他国家"),
        "原产地代码": origin_code,
        "301 flip历史": flip_hist,
        "301 flip变化": flip_change,
        "越南措施": vn_measures,
    }
    # 加征字段：仅启用时输出（禁用则不输出、总税负不叠加）
    if cn301_on:
        result["301加征"] = pct_txt
    if flip301_on:
        result["FLIP 301加征"] = flip301_pct
        result["FLIP 301说明"] = flip301_note

    # 来源追溯：每条税负判定的官方出处（文件 + 位置 + 说明），供 Web 端弹窗展示
    sources = []
    meta = db.get("meta") or {}
    line = base.get("line", "")
    sources.append({
        "key": "htsdata",
        "类型": "基础税率",
        "文件": f"{meta.get('hts_csv', 'htsdata.csv')}（USITC 全量税率表）",
        "位置": f"第 {line} 行" if line else "",
        "说明": f"一般税率 {general or '—'}" + (f"；特殊 {special}" if special else "") + (f"；第二栏 {col2}" if col2 else ""),
    })
    if is_china and c99:
        upage = (db.get("sec301_pages") or {}).get(code8, "")
        sources.append({
            "key": "ustr_pdf",
            "类型": "301 加征",
            "文件": f"{meta.get('ustr_pdf', 'China Tariffs_2026HTSRev15.pdf')}（USTR 301 中国清单）",
            "位置": f"第 {upage} 页" if upage else "",
            "说明": f"8 位子目 {fmt(code8, 8)} → Chapter 99 子目 {c99_fmt}，加征 {pct_txt or '—'}",
        })
    if flip301_on and flip301_src:
        sources.append({"key": flip301_src.get("key", "flip_frn"), "类型": "FLIP 301", **flip301_src, "说明": flip301_note})
    if flip_hist:
        sources.append({"key": "local", "类型": "301 flip 历史", "文件": "data/flip_301.json（本地转录）", "位置": "", "说明": flip_change})
    if vn_measures:
        sources.append({"key": "local", "类型": "适用措施说明", "文件": "data/vietnam_measures.json（本地转录）", "位置": "", "说明": vn_measures})
    result["来源"] = sources
    return result


def batch_query(db, codes, origin="CN"):
    """
    批量判定，返回 (结果列表, 统计字典)。

    统计口径（"命中 301 清单但税率为 0"与"根本不在清单上"是两回事，分开计数）：
      - hit             命中清单且实际加征（301判定 == '是'）
      - hit_exempt      命中清单但对应子目为豁免/排除 0%（'是(豁免/0%)'）
      - miss            未命中 301 清单（'否'）
      - undetermined    信息不足无法判定（如仅给到 6 位品目）
      - not_applicable  非中国原产，不适用中国 301
    hit + hit_exempt + miss + undetermined + not_applicable == total
    """
    results = [query_one(db, c, origin=origin) for c in codes]
    counts = {"hit": 0, "hit_exempt": 0, "miss": 0, "undetermined": 0, "not_applicable": 0}
    for r in results:
        j = str(r["301判定"])
        if j == "是":
            counts["hit"] += 1
        elif j.startswith("是"):          # 是(豁免/0%)
            counts["hit_exempt"] += 1
        elif "不适用" in j:
            counts["not_applicable"] += 1
        elif "无法判定" in j:
            counts["undetermined"] += 1
        else:                             # 否
            counts["miss"] += 1
    o = (origin or "CN").strip().upper()
    origin_txt = "中国" if o in (ORIGIN_CN, "") else ("越南" if o == ORIGIN_VN else "其他国家")
    return results, {"total": len(results), "origin": origin_txt, **counts}
