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
import datetime as _dt
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


# 候选扫描：数字与点组成的串。比"直接按 8/10 位匹配"更宽，
# 目的是把位数不对的近似串也捞出来分类，而不是当它不存在。
_CODE_TOKEN_RE = re.compile(r"(?<![\d.])\d[\d.]*\d(?![\d.])")


def extract_codes(text: str):
    """从任意文本中提取 HTS 编码（8位或10位，带点或不带点），去重保序。

    保持原签名与行为不变，供不关心异常项的调用方使用；
    需要知道"哪些输入被丢弃了"时用 extract_codes_detailed。
    """
    return extract_codes_detailed(text)[0]


def extract_codes_detailed(text: str, db=None):
    """
    提取 HTS 编码，并回报无法采用的输入，返回 (codes, issues)。

    此前只认 8/10 位、其余直接丢弃且不作声，两个方向都会出问题：

      漏：Excel 把 '0101.21.00' 存成数值会吃掉前导零变成 1012100（7 位），
          9 位则常见于复制时截断。这些输入直接消失，批量查询的结果行数
          比输入少，而用户无从得知少了哪几行。
      多：'INV-20260827'、'SO2026081234' 里的数字串位数恰好是 8/10，
          会被当成编码送去查询。

    传入 db 时按税则表校验，可区分"真编码"与"位数凑巧的单号/日期"，
    并对缺前导零的串给出可直接采用的补零建议。

    issues 每项：{原文, 位数, 原因, 建议}
    """
    rates_8 = (db or {}).get("rates_8") or {}
    desc_10 = (db or {}).get("desc_10") or {}

    def known(c):
        """该编码是否在税则表内；无 db 时不做判断（一律视为已知）"""
        if not rates_8:
            return True
        return c in rates_8 if len(c) == 8 else (c in desc_10 or c[:8] in rates_8)

    seen, out, issues = set(), [], []
    for m in _CODE_TOKEN_RE.finditer(text or ""):
        raw = m.group()
        code = re.sub(r"\D", "", raw)
        n = len(code)
        if n < 6 or n > 11:
            continue                      # 与 HTS 编码差得太远，不打扰用户
        if n in (8, 10):
            if not known(code):
                issues.append({
                    "原文": raw, "位数": n,
                    "原因": "位数符合但不在 2026 现行 HTS 税则表内",
                    "建议": "确认是否为单号/日期等非编码数字，或为旧版编码",
                })
                continue
            if code not in seen:
                seen.add(code)
                out.append(code)
            continue
        # 位数不对：给出可核对的修复建议，而不是默默扔掉
        issue = {"原文": raw, "位数": n, "原因": "", "建议": ""}
        if n in (7, 9):
            padded = "0" + code
            issue["原因"] = f"{n} 位，非有效编码长度（应为 8 或 10 位）"
            if known(padded):
                # 补零后确实存在，基本可以断定是 Excel 按数值存储吃掉了前导零
                issue["建议"] = (f"疑似前导零丢失（Excel 按数值存储会吃掉开头的 0），"
                                 f"补零后为 {fmt(padded, len(padded))}，已在税则表中，请确认")
            else:
                # 补零不成立，更可能是复制粘贴时截断了尾部
                issue["建议"] = ("可能是前导零丢失或尾部截断；"
                                 f"补零后 {fmt(padded, len(padded))} 不在税则表中，请核对原始编码")
        elif n == 6:
            issue["原因"] = "6 位品目，信息不足以判定 301"
            issue["建议"] = "请补全至 8 位子目"
        else:  # 11
            issue["原因"] = "11 位，超出 HTS 编码长度"
            issue["建议"] = "请核对是否混入了其他数字"
        issues.append(issue)
    return out, issues


def _fmt_c99(c99):
    """9903 子目纯数字 → 带点格式（99038815 → 9903.88.15）"""
    if not c99:
        return ""
    if len(c99) >= 8:
        return f"{c99[0:4]}.{c99[4:6]}.{c99[6:8]}"
    return c99


# ---------- Section 301 归属查找 ----------

def _sec301_lookup(db, code, code8):
    """
    查 Section 301 归属，返回 (c99 或 None, 无法判定时的说明文本)。

    USTR 清单绝大多数按 8 位子目列示，但有少量精确到 10 位统计后缀。对这些子目：
      - 给出 10 位编码 → 精确匹配；不在清单内的后缀就是未命中（清单只列特定后缀）
      - 只给 8 位编码 → 无法判定。同一前缀下不同后缀可能档位不同（如 6307.90.98 下
        …42 是 +50%，其余是 +7.5%），任何 8 位层面的归纳都会算错，因此要求补全 10 位。
    """
    partial = (db.get("sec301_partial_8") or {}).get(code8)
    if partial:
        if len(code) == 10:
            return (db.get("sec301_map_10") or {}).get(code), ""
        listed = "、".join(fmt(s, 10) for s in partial[:4])
        more = f" 等 {len(partial)} 个" if len(partial) > 4 else ""
        return None, (f"⚠ 该 8 位子目下仅特定 10 位后缀列入 301 清单（{listed}{more}），"
                      f"且不同后缀加征档位可能不同，请提供完整 10 位编码后重查")
    return (db.get("sec301_map") or {}).get(code8), ""


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

# FLIP 301 税率表以经济体为单位列示（EU / TW 等），但用户可能填成员国或三字母代码。
# 未做归一化时 'DE' 会落到"不在 60 名单" → 静默漏加 10%，因此显式建别名表。
_EU_MEMBERS = (
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU",
    "IE", "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE",
    "AUT", "BEL", "BGR", "HRV", "CYP", "CZE", "DNK", "EST", "FIN", "FRA", "DEU", "GRC",
    "HUN", "IRL", "ITA", "LVA", "LTU", "LUX", "MLT", "NLD", "POL", "PRT", "ROU", "SVK",
    "SVN", "ESP", "SWE",
)
ORIGIN_ALIASES = {m: "EU" for m in _EU_MEMBERS}
ORIGIN_ALIASES.update({
    "EUR": "EU", "EU27": "EU",
    "TWN": "TW", "CT": "TW",          # CT 为 HTS 中台湾的传统代码
    "CHN": "CN", "HKG": "HK", "VNM": "VN", "JPN": "JP", "KOR": "KR",
    "CHE": "CH", "GBR": "GB", "CAN": "CA", "MEX": "MX", "IND": "IN", "BRA": "BR",
})


def normalize_origin(origin_code):
    """把成员国 / 三字母代码归一到 FLIP 301 税率表使用的经济体代码"""
    o = (origin_code or "").strip().upper()
    return ORIGIN_ALIASES.get(o, o)


# ANNEX II "Scope Limitations" 三档的官方定义（FRN 物理页 137 原文，逐条转述）。
# 带这一列的子目**只有落在该范围内**才不适用 FLIP 301，范围外仍按经济体档位加征。
FLIP_SCOPE_DEFS = {
    "Aircraft": "仅民用航空器（军用航空器以外的所有航空器）及其发动机、零部件、组件，"
                "其他部件、组件与分总成，以及地面飞行模拟器及其零部件，且须另行满足 "
                "HTSUS general note 6 的条件（不论是否按 Special 栏 “Free (C)” 申报）",
    "Pharma": "仅用于医药用途（pharmaceutical applications）的商品"
              "（不论是否按 Special 栏 “Free (K)” 申报）",
    "Ex": "仅限 ANNEX II 该行 Description 栏所述商品——该栏正文即范围本身",
}


def _flip_tier(o, rates):
    """
    按经济体取 FLIP 301 档位，返回 (显示文本, 说明, 来源Part文本, spec)。

    从 flip301_judge 里拆出来，是因为豁免判定要用到它：ANNEX II 里带范围限制的
    子目，范围外照旧按本档加征，得先知道本档是多少才能给出"不豁免时是多少"。
    """
    if o in rates.get("10", []):
        return ("+10%",
                "FLIP 301 强迫劳动关税 10%（在 MFN 之上加征；已适用 Section 232 或 Annex 豁免产品除外）",
                "FRN 税率表（10% 档）", {"mode": "flat", "rate": 10.0})
    if o in rates.get("net_mfn_10", []):
        return ("≤+10%",
                "FLIP 301 与 MFN 合计封顶 10%（MFN≥10% 则本税 0；已适用 Section 232 或 Annex 豁免产品除外）",
                "FRN 税率表（net-of-MFN 10%）", {"mode": "net_mfn", "cap": 10.0})
    if o in rates.get("net_mfn_125", []):
        return ("≤+12.5%",
                "FLIP 301 与 MFN 合计封顶 12.5%（MFN≥12.5% 则本税 0；已适用 Section 232 或 Annex 豁免产品除外）",
                "FRN 税率表（net-of-MFN 12.5%）", {"mode": "net_mfn", "cap": 12.5})
    if o in rates.get("125", []):
        return ("+12.5%",
                "FLIP 301 强迫劳动关税 12.5%（在 MFN 之上加征；已适用 Section 232 或 Annex 豁免产品除外）",
                "FRN 税率表（12.5% 档）", {"mode": "flat", "rate": 12.5})
    return ("", f"{o} 不在 FLIP 301 被调查经济体名单（60 个），不适用",
            "FRN 范围（60 经济体名单）", {"mode": "none"})


def flip301_judge(db, origin_code, code8=""):
    """
    FLIP 301 强迫劳动调查关税（Section 301，2026-07-24 生效）按原产地国家查表。

    返回 (加征文本, 说明, 来源dict, 档位spec)：
      - "豁免"：编码命中 ANNEX II 豁免清单且**无**范围限制，不加征
      - "+12.5%(范围存疑)"：命中 ANNEX II 但该子目带 Scope Limitations
      - "+12.5%"：12.5% 档（all other investigated）：中国、香港、越南、新加坡、巴西等
      - "+10%"：10% 档：加拿大、墨西哥、印度、英国等 17 个
      - net-of-MFN：欧盟/台湾（合计 10%）、日本/韩国/瑞士（合计 12.5%）
      - 不在 60 名单：不适用

    档位spec 供 rate.calc_total 做数值计算，形如：
      {"mode": "flat",    "rate": 12.5}   在 MFN 之上直接加 12.5%
      {"mode": "net_mfn", "cap": 10.0}    与 MFN 合计封顶 10% → 实际加征 max(0, 10 - MFN)
      {"mode": "exempt"}                  豁免，加征 0
      {"mode": "conditional", "scope": "Aircraft", "fallback": {...}}
                                          仅 scope 范围内豁免，范围外按 fallback 加征
      {"mode": "none"}                    不适用 / 数据未覆盖
    加征文本只适合展示；net-of-MFN 档的 "+10%" 是名义上限而非实际加征额，
    数值计算必须走 spec，否则 MFN 已达上限的商品会被多加一遍。

    豁免：已适用 Section 232 关税的产品、ANNEX II 清单。
    来源dict：{"文件", "位置", "Part", "范围限制"}，供 Web 端来源追溯弹窗使用。
    """
    f = db.get("flip301") or {}
    rates = f.get("rates") or {}
    ex = db.get("flip301_exemptions") or {}
    o = normalize_origin(origin_code)
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
        return "", "数据未覆盖（缺 FLIP 301 数据源）", _src("", "", ""), {"mode": "none"}

    tier_txt, tier_note, tier_part, tier_spec = _flip_tier(o, rates)

    # ① 不在 60 经济体名单 → 本措施对该原产地根本不适用，是否收录进 ANNEX II 无意义。
    #    此前 ANNEX II 判在档位之前，非被调查经济体命中清单会被答成"官方豁免"：
    #    税额同样是 0，但把"不适用"说成"已豁免"，人工复核会去找一份并不存在的豁免依据。
    if tier_spec["mode"] == "none":
        return tier_txt, tier_note, _src("", tier_part, ""), tier_spec

    def _annex(prefix, part_txt, page, scope, ex_desc=""):
        """
        ANNEX II 命中后的结论：无范围限制才是真豁免，带范围限制只是"可能豁免"。

        1257/2113 条 Part A 条目带 Scope Limitations（Pharma 700 / Aircraft 541 / Ex 16），
        此前一律返回 {"mode": "exempt"}，范围限制只写进说明文本、不进档位——
        于是一支普通工业温度计（9025.19.80，限 Aircraft）被算成 FLIP 301 免征，
        中国产总税负给到 25% 而非 37.5%。少收要被 CBP 追补加罚，比多收危险，
        所以判不了用途时按**不豁免**给数，另标"范围存疑"要求人工确认。
        """
        page_txt = f"（FRN 物理页 {page}）" if page else ""
        if not scope:
            return "豁免", f"{prefix}，不适用 FLIP 301{page_txt}", _src(
                page, part_txt, ""), {"mode": "exempt"}
        defn = FLIP_SCOPE_DEFS.get(scope, "见 FRN ANNEX II 原文")
        desc_txt = f"；该行 Description 原文：{ex_desc}" if ex_desc else ""
        note = (f"{prefix}，但该子目带范围限制 “{scope}”：{defn}{desc_txt}。"
                f"仅此范围内的商品豁免，范围外仍按 {tier_txt} 加征——"
                f"下方税额已按不豁免保守计，请核实商品用途后人工确认{page_txt}")
        return (f"{tier_txt}(范围存疑)", note, _src(page, part_txt, scope),
                {"mode": "conditional", "scope": scope, "fallback": tier_spec})

    # ② ANNEX II 判定（逐编码）：通用 Part A / 经济体专属 / CAFTA-DR（仅 JO/SV/GT）
    if code8:
        universal = ex.get("universal") or []
        by_econ = ex.get("by_economy") or {}
        if code8 in universal:
            return _annex(
                "ANNEX II 通用豁免（Part A）",
                "ANNEX II Part A（通用豁免，所有被调查经济体）",
                (ex.get("universal_pages") or {}).get(code8, ""),
                (ex.get("universal_scopes") or {}).get(code8, ""),
                (ex.get("universal_ex_desc") or {}).get(code8, ""))
        if code8 in by_econ.get(o, []):
            return _annex(
                "ANNEX II 豁免（该经济体专属 Part）",
                f"ANNEX II 该经济体专属（{o}）",
                ((ex.get("by_economy_pages") or {}).get(o) or {}).get(code8, ""),
                ((ex.get("by_economy_scopes") or {}).get(o) or {}).get(code8, ""),
                ((ex.get("by_economy_ex_desc") or {}).get(o) or {}).get(code8, ""))
        if o in ("JO", "SV", "GT") and code8 in by_econ.get("CAFTA_DR", []):
            return _annex(
                "ANNEX II Part O（约旦 / 萨尔瓦多 / 危地马拉纺织品）",
                "ANNEX II Part O（约旦 / 萨尔瓦多 / 危地马拉 免税纺织品）",
                ((ex.get("by_economy_pages") or {}).get("CAFTA_DR") or {}).get(code8, ""),
                ((ex.get("by_economy_scopes") or {}).get("CAFTA_DR") or {}).get(code8, ""),
                ((ex.get("by_economy_ex_desc") or {}).get("CAFTA_DR") or {}).get(code8, ""))

    # ③ 未命中 ANNEX II：按经济体档位加征
    return tier_txt, tier_note, _src("", tier_part, ""), tier_spec


# ---------- 301 排除（U.S. note 20）----------

# 只有"整号排除 + 当日在有效期内"才允许机器判免。理由见 exclusion_lookup 的注释。
EXCL_AUTO = "full"


def _excl_status(note, today):
    """
    排除标目在 today 的状态。与 extract_exclusions._status 同一套规则，
    但**以查询当天重算**——数据里存的 status 是提取那天的，
    9903.88.69/.70 都在 2026-11-09 到期，靠提取日期判断迟早会把过期的说成有效。
    """
    frm, to = note.get("effective_from"), note.get("effective_to")
    if not frm and not to:
        return "有效期未标注"
    if frm and frm > today:
        return "未生效"
    if to and to < today:
        return "已过期"
    return "生效中"


def exclusion_lookup(db, code, code8, today=None):
    """
    查该编码可用的 301 排除（USTR 按 U.S. note 20 逐条授予）。

    返回 (auto, items)：
      auto   可直接判免的那条（covers=full 且当日生效中），没有则 None
      items  全部相关排除条目，按"生效中优先、整号优先"排序，供展示与人工核对

    **为什么只有 full 能自动判免**：排除分两种形态——
      full       条目正文就是一个统计号（如 note 20(vvv)(ii) 第 (3) 项 "9025.19.8085"）。
                 该号下所有中国产商品都排除，纯粹是"编码 + 日期"问题，不需要判断商品，
                 机器能给确定答案，也**必须**给——不给就等于让客户白交 25%。
      described  排除按产品描述授予（"Infrared thermometers (described in …9025.19.8085)"）。
                 同一个税号下有的款符合、有的不符合，编码本身回答不了，
                 自动判免会直接造出错误申报，所以只列原文供人工核对。

    排除按 10 位统计号授予，8 位查询命中不到具体统计号时只能提示、不能判免。
    """
    ex = db.get("exclusions") or {}
    by_code = ex.get("by_code") or {}
    notes = ex.get("notes") or {}
    if not by_code:
        return None, []
    today = today or _dt.date.today().isoformat()

    recs = list(by_code.get(code) or []) if len(code) == 10 else []
    exact = bool(recs)
    if not exact:
        # 8 位查询：把该 8 位下所有 10 位统计号的排除都捞出来，但一律不判免——
        # 排除授予到 10 位，不知道具体统计号就不知道该不该免。
        for c, rs in by_code.items():
            if c[:8] == code8 and len(c) == 10:
                for r in rs:
                    recs.append({**r, "统计号": fmt(c, 10)})

    out = []
    for r in recs:
        note = notes.get(r["c99"]) or {}
        st = _excl_status(note, today)
        out.append({
            "9903子目": _fmt_c99(r["c99"]),
            "note": r.get("note", ""),
            "适用于": _fmt_c99(r.get("list_c99", "")),
            "覆盖方式": "整号排除" if r["covers"] == "full" else "按描述排除",
            "状态": st,
            "有效期": f"{note.get('effective_from') or '—'} → {note.get('effective_to') or '—'}",
            "生效止": note.get("effective_to", ""),
            "描述": r.get("desc", ""),
            "条目号": r.get("item"),
            "页": r.get("page"),
            **({"统计号": r["统计号"]} if r.get("统计号") else {}),
        })
    order = {"生效中": 0, "未生效": 1, "有效期未标注": 2, "已过期": 3}
    out.sort(key=lambda x: (order.get(x["状态"], 9), 0 if x["覆盖方式"] == "整号排除" else 1))

    auto = None
    if exact:
        auto = next((x for x in out
                     if x["状态"] == "生效中" and x["覆盖方式"] == "整号排除"), None)
    return auto, out


def _criteria_of(db, code8):
    """抽取归类判定条件；抽取失败不应影响主查询（税率判定与它无关）"""
    try:
        import criteria
        return criteria.extract(db, code8)
    except Exception:
        return []


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
        flip301_pct, flip301_note, flip301_src, flip301_spec = flip301_judge(
            db, origin_code, code8=code8)
    else:
        flip301_pct, flip301_note, flip301_src = "", "FLIP 301 已禁用（配置）", {}
        flip301_spec = {"mode": "none"}

    # 基础信息（两种原产地共用）
    base = rates_8.get(code8, {})
    desc = desc_10.get(code) or base.get("desc", "")
    # 归类路径：祖先品名承载材质/织法/含量阈值等判定条件，供归类论证与人工复核
    _nodes = db.get("path_nodes") or []
    cls_path = [_nodes[i].rstrip(":").strip()
                for i in (base.get("path") or []) if 0 <= i < len(_nodes)]
    general = base.get("general", "")
    special = base.get("special", "")
    col2 = base.get("col2", "")
    add = add_duty.get(code) or add_duty.get(code8, "")

    # 中国：301 判定（既有逻辑）；非中国：MFN 通用轨道（不叠加中国 301）
    if is_china:
        c99, undetermined_note = _sec301_lookup(db, code, code8)
        if undetermined_note:
            # 该 8 位子目下只有特定 10 位后缀入清单，且档位可能不同 → 不做 8 位层面的猜测
            pct = None
            is301 = "无法判定"
            c99_fmt = ""
            pct_txt = ""
            note = undetermined_note
        elif c99:
            pct = c99_percent.get(c99)
            c99_fmt = _fmt_c99(c99)
            if c99 not in c99_percent:
                # 数据缺失（9903 税率文本解析失败）≠ 确认豁免，不能折叠成 0%
                is301 = "是(比例待核)"
                pct_txt = "需人工核对"
                note = f"命中301清单，但 {c99_fmt} 的加征比例无法从税率表解析，请人工核对"
            elif pct:
                is301 = "是"
                pct_txt = f"+{pct:g}%"
                note = "命中301清单，具体以 USTR 豁免状态为准"
            else:
                is301 = "是(豁免/0%)"
                pct_txt = "0%(豁免)"
                note = "命中301但对应子目为豁免/排除(0%)"
        else:
            pct = None
            is301 = "否"
            c99_fmt = ""
            pct_txt = ""
            note = ""
        # 301 排除（U.S. note 20）：命中清单后还要看有没有被 USTR 排除。
        # 只有"整号排除 + 当日生效"才改税额；按描述授予的只列原文供人工核对。
        excl_auto, excl_items = (None, [])
        if cn301_on and is301.startswith("是"):
            excl_auto, excl_items = exclusion_lookup(db, code, code8)
            if excl_auto:
                pct = 0.0
                is301 = "是(已排除)"
                pct_txt = "0%(排除)"
                c99_fmt = excl_auto["9903子目"]
                note = (f"命中301清单，但该统计号整号列入 {excl_auto['note']} 排除"
                        f"（{excl_auto['9903子目']}，有效期 {excl_auto['有效期']}）"
                        f"，报关时申报 {excl_auto['9903子目']} 即免除加征")
            elif excl_items:
                live = [x for x in excl_items if x["状态"] == "生效中"]
                if live:
                    # 整号排除要单独点名并给出统计号。8 位查询看不到 10 位后缀，
                    # 而排除恰恰授予到 10 位：只说"有 N 条待核"，用户不会想到
                    # 其中某个后缀是**无条件全免**的，仍旧按满额报关。
                    full = [x for x in live if x["覆盖方式"] == "整号排除"]
                    parts = []
                    if full:
                        nums = "、".join(dict.fromkeys(
                            x.get("统计号", "") for x in full if x.get("统计号")))
                        parts.append(f"{len(full)} 条整号排除"
                                     + (f"（统计号 {nums}，报这些号即全免加征）" if nums else ""))
                    if len(live) > len(full):
                        parts.append(f"{len(live) - len(full)} 条按描述排除（需逐条核对是否适用）")
                    note += "；该子目下有生效中的排除：" + "，".join(parts)
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
        excl_auto, excl_items = None, []   # 301 排除只对中国原产有意义
        vn_measures = vietnam_info(db, code8, "VN" if origin_code == ORIGIN_VN else "OTHER")

    # 配置裁剪：禁用加征时备注标注，且不输出加征字段
    if not cn301_on:
        note = (note + "；301 加征已禁用（配置）") if note else "301 加征已禁用（配置）"
    if "范围存疑" in (flip301_pct or ""):
        # 备注是列表页唯一能看全的文字列，范围限制这种"结论有条件"的信息必须进来，
        # 否则一眼扫过去只看到一个百分比，看不出这笔税还取决于商品用途。
        _scope = (flip301_spec or {}).get("scope", "")
        _t = f"FLIP 301 命中 ANNEX II 但带范围限制“{_scope}”，已按不豁免计（{flip301_pct}），需人工核实用途"
        note = (note + "；" + _t) if note else _t
    elif flip301_pct and flip301_pct != "豁免":
        note = (note + f"；FLIP 301 强迫劳动关税 {flip301_pct}") if note else f"FLIP 301 强迫劳动关税 {flip301_pct}"
    elif flip301_pct == "豁免":
        note = (note + "；FLIP 301 豁免（ANNEX II 清单）") if note else "FLIP 301 豁免（ANNEX II 清单）"
    elif not flip301_on:
        note = (note + "；FLIP 301 已禁用（配置）") if note else "FLIP 301 已禁用（配置）"

    result = {
        "输入编码": fmt(code, 10) if n == 10 else (fmt(code, 8) if n == 8 else code),
        "8位子目": fmt(code8, 8) if len(code8) == 8 else code8,
        "商品描述": desc or "（无描述，见备注）",
        "归类路径": cls_path,
        # 判定条件与证据清单：报这个编码需要能证明什么（归类论证与查验备料用）
        "判定条件": _criteria_of(db, code8),
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
        # 排除信息独立成列：既然报关时要改填 9903 子目，就得让人看到依据与有效期
        _live = [x for x in excl_items if x["状态"] == "生效中"]
        _full = [x for x in _live if x["覆盖方式"] == "整号排除"]
        # 列宽有限，单元格只放结论；完整依据在「301排除明细」与备注里。
        # 9903 子目已被改写成排除标目，这里不必重复。
        if excl_auto:
            result["301排除"] = f"已排除 至 {excl_auto['生效止']}"
        elif _full:
            _d = len(_live) - len(_full)
            result["301排除"] = f"待核：{len(_full)} 整号" + (f" / {_d} 描述" if _d else "")
        elif _live:
            result["301排除"] = f"待核：{len(_live)} 描述"
        else:
            result["301排除"] = ""
        result["301排除明细"] = excl_items[:20]
    if flip301_on:
        result["FLIP 301加征"] = flip301_pct
        result["FLIP 301说明"] = flip301_note
        # 供 rate.calc_total 做数值计算；net-of-MFN 档不能按显示文本直接相加
        result["FLIP 301档位"] = flip301_spec

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
        # 10 位精确命中时定位到该 10 位行的页码，否则用 8 位子目的页码
        pages = db.get("sec301_pages") or {}
        matched = code if code in (db.get("sec301_map_10") or {}) else code8
        upage = pages.get(matched, "")
        sources.append({
            "key": "ustr_pdf",
            "类型": "301 加征",
            "文件": f"{meta.get('ustr_pdf', 'China Tariffs_2026HTSRev15.pdf')}（USTR 301 中国清单）",
            "位置": f"第 {upage} 页" if upage else "",
            "说明": f"{'10 位子目' if len(matched) == 10 else '8 位子目'} {fmt(matched, len(matched))}"
                    f" → Chapter 99 子目 {c99_fmt}，加征 {pct_txt or '—'}",
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
      - hit_unresolved  命中清单但加征比例无法解析（'是(比例待核)'）——不是豁免，需人工
      - miss            未命中 301 清单（'否'）
      - undetermined    信息不足无法判定（6 位品目、或需补全 10 位后缀）
      - not_applicable  非中国原产，不适用中国 301
    六项之和 == total
    """
    results = [query_one(db, c, origin=origin) for c in codes]
    counts = {"hit": 0, "hit_exempt": 0, "hit_unresolved": 0,
              "miss": 0, "undetermined": 0, "not_applicable": 0}
    for r in results:
        j = str(r["301判定"])
        if j == "是":
            counts["hit"] += 1
        elif "比例待核" in j:             # 是(比例待核)：数据缺失，不能算作豁免
            counts["hit_unresolved"] += 1
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
