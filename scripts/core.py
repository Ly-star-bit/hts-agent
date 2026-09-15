# -*- coding: utf-8 -*-
"""
core.py —— HTS 多措施查询核心逻辑（命令行工具与 Web 前端共用）

提供：数据库加载、编码解析、单条多措施判定。
查询按「原产地 × 措施栈」泛化：
  - 中国（CN）：基础税率 + Section 301 加征（含 flip 历史 + 排除）+ FLIP 301 强迫劳动关税
  - 越南 / 其他经济体：基础税率（MFN，第二栏国家按第二栏）+ FLIP 301（按经济体查官方标目）；不适用中国 301
  - 未指定原产地：仅 MFN，明示未计入任何原产地措施
  不在模型里的 9903 标目（232 等）按原产地探测并标注「总税负不完整」，AD/CVD 明示未覆盖。
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
    返回 (历史列表, 变化文本)。历史列表每项 {date, c99, pct, note}。

    **"没有数据"与"没有变化"必须分开说**：历史库目前是人工维护的存根，全库只
    覆盖 3 个编码。此前两种情况都返回空字符串，界面上长得一模一样——而一片空白
    看起来像"这个编码的档位从没变过"，不像"我们没有这个编码的历史"。
    前者会让人放心地按当前档位报关，后者才是事实。
    """
    flips = (db.get("flip_301") or {}).get("flips", {})
    history = flips.get(code8) or []
    if not c99:
        # 当前就没命中 301，谈不上档位变化
        return [], ""
    if not history:
        # 不写进备注：命中 301 的 10,391 个编码里 10,388 个没有历史数据，
        # 每行都挂一句会把备注列淹掉。改由 query_one 放进「来源」弹窗——
        # 那里才是看"这条判定的依据与它的边界"的地方。
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
    非中国原产地的适用措施说明：MFN 一般税率（第二栏国家为第二栏），不适用中国 301。
      - VN：越南轨道，覆盖编码给出具体说明，未覆盖标注「数据未覆盖具体说明」
      - 未指定：只按 MFN 计，明说未计入任何原产地措施
      - 其他：通用轨道（任何其他国家），带中文名
    """
    v = db.get("vietnam") or {}
    o = normalize_origin(origin)
    if o == "VN":
        if not v:
            return "数据未覆盖（缺越南措施数据源）"
        base = "适用美国 MFN 一般税率；不适用中国 301 加征。"
        covered = v.get("covered_codes") or []
        if code8 in covered:
            extra = (v.get("notes_per_code") or {}).get(code8, "")
            return base + (" " + extra if extra else "")
        return base + " 该编码暂无具体说明（数据未覆盖具体说明）。"
    if o == ORIGIN_UNSPECIFIED:
        return "未指定原产地：仅按 MFN 一般税率计，未计入任何原产地相关措施（301 / FLIP 301 / 第二栏等）。"
    name = origin_label(o)
    if o in COLUMN2_ORIGINS:
        return f"{name}原产：适用美国第二栏税率（暂停正常贸易关系）；不适用中国 301 加征（其他国家通用轨道）。"
    return f"{name}原产：适用美国 MFN 一般税率；不适用中国 301 加征（其他国家通用轨道）。"


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
    # ISO 3166 alpha-3 → alpha-2（FLIP 60 经济体 + 第二栏国家）
    "DZA": "DZ", "AGO": "AO", "ARG": "AR", "AUS": "AU", "BHS": "BS", "BHR": "BH", "BGD": "BD",
    "BRA": "BR", "KHM": "KH", "CAN": "CA", "CHL": "CL", "CHN": "CN", "COL": "CO", "CRI": "CR",
    "DOM": "DO", "ECU": "EC", "EGY": "EG", "SLV": "SV", "GTM": "GT", "GUY": "GY", "HND": "HN",
    "HKG": "HK", "IND": "IN", "IDN": "ID", "IRQ": "IQ", "ISR": "IL", "JPN": "JP", "JOR": "JO",
    "KAZ": "KZ", "KWT": "KW", "LBY": "LY", "MYS": "MY", "MEX": "MX", "MAR": "MA", "NZL": "NZ",
    "NIC": "NI", "NGA": "NG", "NOR": "NO", "OMN": "OM", "PAK": "PK", "PER": "PE", "PHL": "PH",
    "QAT": "QA", "RUS": "RU", "SAU": "SA", "SGP": "SG", "ZAF": "ZA", "KOR": "KR", "LKA": "LK",
    "CHE": "CH", "THA": "TH", "TTO": "TT", "TUR": "TR", "ARE": "AE", "GBR": "GB", "UKG": "GB",
    "UK": "GB", "URY": "UY", "VEN": "VE", "VNM": "VN", "BLR": "BY", "CUB": "CU", "PRK": "KP",
})

# 「未指定」与「其他国家」是两个不同的回答：
#   OTHER 未指定原产地 —— 仅按 MFN 一般税率计，不叠加任何原产地相关措施，备注必须说明未计入
#   XX    明确不在 FLIP 60 名单、也不在第二栏名单的其他国家 —— MFN 轨道，FLIP 不适用
# 此前网页版下拉只有 CN / VN / OTHER，OTHER 落到「不在 60 名单」，于是界面上任何非中越
# 原产的报价都静默少了 10%–12.5% 的 FLIP 301。
ORIGIN_UNSPECIFIED = "OTHER"
ORIGIN_OTHER_LISTED = "XX"
_UNSPECIFIED_WORDS = {"", "OTHER", "OTHERS", "UNSPECIFIED", "NONE", "N/A", "NA", "未指定", "不详", "其他", "其它"}

# 第二栏国家（HTSUS General Note 3(b)：古巴、朝鲜；俄罗斯、白俄罗斯自 2022-04 暂停正常贸易关系）
COLUMN2_ORIGINS = {"CU", "KP", "RU", "BY"}

# 经济体代码 → 中文名（FLIP 301 的 60 个 + 第二栏 4 个）。下拉与结果表都用它。
ORIGIN_NAMES = {
    "CN": "中国", "VN": "越南", "HK": "香港", "TW": "台湾", "JP": "日本", "KR": "韩国",
    "EU": "欧盟", "GB": "英国", "CH": "瑞士", "NO": "挪威", "CA": "加拿大", "MX": "墨西哥",
    "IN": "印度", "ID": "印度尼西亚", "MY": "马来西亚", "TH": "泰国", "SG": "新加坡",
    "PH": "菲律宾", "KH": "柬埔寨", "BD": "孟加拉国", "PK": "巴基斯坦", "LK": "斯里兰卡",
    "AU": "澳大利亚", "NZ": "新西兰", "BR": "巴西", "AR": "阿根廷", "CL": "智利", "CO": "哥伦比亚",
    "PE": "秘鲁", "EC": "厄瓜多尔", "UY": "乌拉圭", "VE": "委内瑞拉", "GY": "圭亚那",
    "CR": "哥斯达黎加", "DO": "多米尼加", "SV": "萨尔瓦多", "GT": "危地马拉", "HN": "洪都拉斯",
    "NI": "尼加拉瓜", "TT": "特立尼达和多巴哥", "BS": "巴哈马", "IL": "以色列", "JO": "约旦",
    "TR": "土耳其", "SA": "沙特阿拉伯", "AE": "阿联酋", "QA": "卡塔尔", "KW": "科威特",
    "BH": "巴林", "OM": "阿曼", "IQ": "伊拉克", "KZ": "哈萨克斯坦", "RU": "俄罗斯",
    "EG": "埃及", "MA": "摩洛哥", "DZ": "阿尔及利亚", "LY": "利比亚", "NG": "尼日利亚",
    "AO": "安哥拉", "ZA": "南非",
    "BY": "白俄罗斯", "CU": "古巴", "KP": "朝鲜",
}
_NAME_TO_CODE = {v: k for k, v in ORIGIN_NAMES.items()}
_NAME_TO_CODE.update({"中国大陆": "CN", "香港特别行政区": "HK", "中国香港": "HK", "中国台湾": "TW",
                      "南韩": "KR", "大韩民国": "KR", "美国": "US", "德国": "EU", "法国": "EU",
                      "意大利": "EU", "西班牙": "EU", "荷兰": "EU", "波兰": "EU", "比利时": "EU"})


def normalize_origin(origin_code):
    """
    把用户给的原产地归一到判定用的经济体代码：
      成员国 / 三字母 / 中文名 → 代码（DE → EU，CHN → CN，"墨西哥" → MX）；
      空值 / OTHER / "未指定" → OTHER（未指定）；其余原样大写返回。
    301 判定与 FLIP 判定必须用同一个归一化结果——此前只有 FLIP 走这里，
    传 CHN 会得到"301 不适用 + FLIP 12.5%"这种自相矛盾的答案。
    """
    raw = str(origin_code or "").strip()
    o = raw.upper()
    if o in _UNSPECIFIED_WORDS or raw in _UNSPECIFIED_WORDS:
        return ORIGIN_UNSPECIFIED
    if raw in _NAME_TO_CODE:
        return _NAME_TO_CODE[raw]
    return ORIGIN_ALIASES.get(o, o)


def origin_label(origin_code):
    """经济体代码 → 界面/导出用的中文名。未指定 → 未指定；不在表内 → 其他国家。"""
    o = normalize_origin(origin_code)
    if o == ORIGIN_UNSPECIFIED:
        return "未指定"
    return ORIGIN_NAMES.get(o, "其他国家")


def origin_options(db):
    """
    供网页下拉使用的原产地列表：中国、越南在前，其余 FLIP 经济体 + 第二栏国家按名排，
    末尾是「其他国家（不在名单）」与「未指定」。每项带 FLIP 档位文本，让人在选的时候
    就看见这个原产地会不会被加征。
    """
    out = []
    seen = set()

    def _add(code, name, extra=""):
        if code in seen:
            return
        seen.add(code)
        pct, note, _src, spec = flip301_judge(db, code)
        tier = pct or ("不适用" if spec.get("mode") == "none" else "")
        out.append({"code": code, "name": name, "flip301": tier, "column2": code in COLUMN2_ORIGINS,
                    "label": f"{name}（{extra or ('FLIP 301 ' + tier if tier else 'MFN')}）"})

    _add("CN", "中国", "301 + FLIP 301 +12.5%")
    _add("VN", "越南", "FLIP 301 +12.5%，无 301")
    tiers = ((db.get("flip301_headings") or {}).get("by_origin") or {})
    codes = set(tiers) | set(ORIGIN_NAMES)
    for code in sorted(codes, key=lambda c: ORIGIN_NAMES.get(c, c)):
        if code in COLUMN2_ORIGINS:
            _add(code, ORIGIN_NAMES.get(code, code), "第二栏税率" + ("，FLIP 301 +12.5%" if code in tiers else ""))
        else:
            _add(code, ORIGIN_NAMES.get(code, code))
    out.append({"code": ORIGIN_OTHER_LISTED, "name": "其他国家", "flip301": "不适用", "column2": False,
                "label": "其他国家（不在 FLIP 60 名单，仅 MFN）"})
    out.append({"code": ORIGIN_UNSPECIFIED, "name": "未指定", "flip301": "", "column2": False,
                "label": "未指定原产地（仅 MFN，不计任何原产地措施）"})
    return out


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


def _flip_tier(db, o):
    """
    按经济体取 FLIP 301 档位，返回 (显示文本, 说明, 来源Part文本, spec)。

    从 flip301_judge 里拆出来，是因为豁免判定要用到它：ANNEX II 里带范围限制的
    子目，范围外照旧按本档加征，得先知道本档是多少才能给出"不豁免时是多少"。

    档位优先取 build_db 从 htsdata.csv 推导的官方标目（flip301_headings）：
    每个经济体一行 9903.05.xx，税率写在 General 栏，报关要填的就是这个标目——
    此前只有手抄 JSON，档位会漂，标目从没输出过。老库没有推导表时退回 JSON。
    spec 里带 heading（flat）或 heading_below / heading_at_or_above（net-of-MFN）
    与 htsdata 行号，供来源追溯直接定位到官方表那一行。
    """
    derived = ((db.get("flip301_headings") or {}).get("by_origin") or {}).get(o) or {}
    mode = derived.get("mode")
    if mode == "flat" and derived.get("heading"):
        r, h = float(derived["rate"]), derived["heading"]
        return (f"+{r:g}%",
                f"FLIP 301 强迫劳动关税 {r:g}%（在 MFN 之上加征；报关标目 {_fmt_c99(h)}；"
                f"已适用 Section 232 或 Annex 豁免产品除外）",
                f"htsdata.csv 9903 标目 {_fmt_c99(h)}（U.S. note 52，{r:g}% 档）",
                {"mode": "flat", "rate": r, "heading": h, "line": derived.get("line")})
    if mode == "net_mfn" and derived.get("cap") is not None and derived.get("heading_below"):
        cap, hb, ha = float(derived["cap"]), derived["heading_below"], derived.get("heading_at_or_above", "")
        return (f"≤+{cap:g}%",
                f"FLIP 301 与 MFN 合计封顶 {cap:g}%（MFN≥{cap:g}% 则本税 0；报关标目：MFN<{cap:g}% 时 "
                f"{_fmt_c99(hb)}、否则 {_fmt_c99(ha)}；已适用 Section 232 或 Annex 豁免产品除外）",
                f"htsdata.csv 9903 标目 {_fmt_c99(hb)} / {_fmt_c99(ha)}（U.S. note 52，net-of-MFN {cap:g}%）",
                {"mode": "net_mfn", "cap": cap, "heading_below": hb, "heading_at_or_above": ha,
                 "line": derived.get("line_below")})
    # 老库没有推导表：退回手抄 JSON（data/flip301_forced_labor.json）
    rates = (db.get("flip301") or {}).get("rates") or {}
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
    derived = (db.get("flip301_headings") or {}).get("by_origin") or {}
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

    def _tier_src(spec, part):
        """档位来源：推导自官方表时直接指向 htsdata.csv 的那一行（可查看原文），否则指向 FRN"""
        if spec.get("line"):
            return {
                "key": "htsdata",
                "文件": f"{(db.get('meta') or {}).get('hts_csv', 'htsdata.csv')}（USITC 全量税率表，9903 标目）",
                "位置": f"第 {spec['line']} 行",
                "Part": part,
                "范围限制": "无",
            }
        return _src("", part, "")

    # ⓪ 数据源可用性先于任何判定：缺税率表时无法确定该经济体是否在 60 名单内，
    #    此时若因命中 ANNEX II 而返回"豁免"，等于把"缺数据"说成"不加征"——必须显式标注未覆盖。
    if not f and not derived:
        return "", "数据未覆盖（缺 FLIP 301 数据源）", _src("", "", ""), {"mode": "none"}

    # 未指定原产地：不是"不在名单"，是"没告诉我"。两者税额都是 0，但前者要提醒人去选。
    if o == ORIGIN_UNSPECIFIED:
        return ("", "未指定原产地，FLIP 301 未计入；请选择具体经济体后重查",
                _src("", "FRN 税率表（按经济体）", ""), {"mode": "none", "unspecified": True})

    tier_txt, tier_note, tier_part, tier_spec = _flip_tier(db, o)

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
    return tier_txt, tier_note, _tier_src(tier_spec, tier_part), tier_spec


def flip_heading_of(spec):
    """
    档位 spec → 报关要填的 FLIP 301 标目文本。
    flat 一个标目；net-of-MFN 两个（MFN 低于上限填前者，否则后者，calc_total 知道 MFN
    之后会收成一个）；conditional 用 fallback 的；豁免 / 不适用 / 未指定为空。
    """
    spec = spec or {}
    mode = spec.get("mode")
    if mode == "conditional":
        return flip_heading_of(spec.get("fallback"))
    if mode == "flat" and spec.get("heading"):
        return _fmt_c99(spec["heading"])
    if mode == "net_mfn" and spec.get("heading_below"):
        ha = spec.get("heading_at_or_above", "")
        return _fmt_c99(spec["heading_below"]) + (f" / {_fmt_c99(ha)}" if ha else "")
    return ""


def flip_exceptions_of(db, origin_code):
    """
    该经济体适用的 FLIP 301 例外标目（通用 .85–.92 + 经济体专属），供来源弹窗展示。
    这些标目写的是"不加征的条件"（在途、232 产品、民用航空器、医药、USMCA 货等），
    工具判不了商品是否落在其中，只能把条件原文交给人看。
    """
    fh = db.get("flip301_headings") or {}
    o = normalize_origin(origin_code)
    items = list(fh.get("exceptions") or [])
    items += (fh.get("exceptions_by_origin") or {}).get(o) or []
    return [{"标目": it.get("标目", ""), "描述": it.get("描述", "")} for it in items]


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


def exclusion_expiry(db, today=None, warn_days=14):
    """
    当前生效中的 301 排除里，最早哪天到期、还剩几天。

    **到期本身不会让工具算错**：_excl_status 按查询当天重算，过了到期日
    exclusion_lookup 就不再判免、改按满额加征。真正的风险在两侧——

      · 到期后不重抓：USTR 若延期或新发了排除，你享受不到 → **该免的没免，多收**。
        多收只是客户多付钱，不违法，但一笔笔都是白花的。
      · 任何时候都存在的另一侧：USTR **提前撤销**某条排除，而本地数据仍按原定
        到期日判它有效 → 继续判 0% → **少报**，要被 CBP 追补加罚。
        这一侧与到期日无关，只能靠定期重抓兜住。

    （早先这里写成"到期后会继续按失效的排除判 0%"，方向是反的——代码本来就
    按查询日重算。这段注释与界面提示当时都错了，一并改正。）

    当前带日期的排除只剩两个标目且同一天到期，等于整个排除判定有一个统一的悬崖，
    所以值得单独拎出来做一个函数。

    返回 {状态, 最早到期, 剩余天数, 标目, 生效中标目数, 告警}。
    状态为「已全部过期」时 剩余天数 为负——**不返回 None**：调用方拿到 None
    很容易当成"没问题"，而全部过期恰恰是最该喊的时刻。
    """
    ex = db.get("exclusions") or {}
    notes = ex.get("notes") or {}
    if not notes:
        return None
    today = today or _dt.date.today().isoformat()
    dated = {c99: n for c99, n in notes.items() if n.get("effective_to")}
    if not dated:
        return None            # 一条带日期的排除都没有，无从谈到期
    live = {c99: n for c99, n in dated.items() if _excl_status(n, today) == "生效中"}

    def _days(d):
        try:
            return (_dt.date.fromisoformat(d) - _dt.date.fromisoformat(today)).days
        except ValueError:
            return None

    if not live:
        # 全部过期：此刻工具已不再判任何免，等于 301 排除这条链路整个失效。
        # 必须报出来——返回 None 会被调用方读成"没问题"。
        latest = max(n["effective_to"] for n in dated.values())
        d = _days(latest)
        return {
            "状态": "已全部过期",
            "最早到期": latest,
            "剩余天数": d if d is not None else 0,
            "标目": sorted(_fmt_c99(c) for c, n in dated.items()
                          if n["effective_to"] == latest),
            "生效中标目数": 0,
            "告警": True,
        }
    earliest = min(n["effective_to"] for n in live.values())
    days = _days(earliest)
    if days is None:
        return None
    return {
        "状态": "生效中",
        "最早到期": earliest,
        "剩余天数": days,
        "标目": sorted(_fmt_c99(c) for c, n in live.items()
                      if n["effective_to"] == earliest),
        "生效中标目数": len(live),
        # 到期当天才报警来不及：重抓 + 重提 + 重建要人动手，得留出提前量
        "告警": days <= warn_days,
    }


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


# ---------- 未建模措施探测 / Special 栏提示 ----------

def unmodeled_measures(db, origin_code):
    """
    该原产地会触发哪些**本工具未建模**的 9903 标目组（只探测、不判定）。

    htsdata.csv 里 636 个 9903 标目，工具只建模了中国 301（9903.88/.91/.92）与
    FLIP 301（9903.05.20–.99、9903.06）。其余 451 个——note 2 的墨加等原产地标目、
    note 50 巴西、9903.02/.03 的转运与全球档、232 类产品标目——一律不在总税负里。
    此前对此一字不提，总税负看起来像个完整的数。这里按原产地把"标目正文提及该
    原产地"的组报出来，让总税负带上"不完整"的标记；法律状态与是否适用由人核实。

    以"任何国家"为条件的标目只在 note 2 那一族（9903.01/.02/.03）里报；
    9903.45 石英台面这类按产品触发的 any-country 标目归产品类探测，不在这里凑数。
    未指定原产地不报：没有原产地，谈不上原产地类措施。
    """
    o = normalize_origin(origin_code)
    if o == ORIGIN_UNSPECIFIED:
        return []
    out = []
    for g in db.get("c99_unmodeled") or []:
        notes = g.get("依据") or []
        hit = (g.get("按原产地") or {}).get(o)
        anyc = g.get("任何国家") or {}
        common = {"标目组": g["组"], "依据": "、".join(notes) or "—",
                  "产品词": g.get("产品词") or [],
                  # Chapter 99 编者注（如"9903.03.01–.11 已于 2026-07-23 到期"），
                  # 让人一眼看出这组是否还在执行，而不是每次都去翻 PDF
                  "编者注": g.get("编者注") or []}
        if hit:
            out.append({**common, "触发": f"标目正文提及原产地 {o}", "标目数": hit["数量"],
                        "示例": hit.get("示例") or []})
        elif anyc.get("数量") and "U.S. note 2" in notes:
            out.append({**common, "触发": "以任何国家为条件", "标目数": anyc["数量"],
                        "示例": anyc.get("示例") or []})
    return out


def product_measures(db, code, code8, origin_code):
    """
    该编码落在哪些**按产品触发**的 Chapter 99 清单里（note 16 钢铝铜、33 乘用车、
    37 软木、38 中重型车、39 半导体、51 加拿大特定产品），只探测、不计税。

    清单来自 Chapter 99 PDF 各 note 的子条（extract_c99_products.py 提取，4/6 位为前缀、
    8/10 位精确、区间按同长前缀比较）。命中说明"这类产品有一套本工具没算的关税"，
    而且 FLIP 301 按 note 52(f) 对这些产品不适用——两件事都要人核实：
    清单只是必要条件，note 里还有含量、用途、技术参数等条件（note 39 半导体尤其），
    编码本身判不出。
    """
    idx = db.get("c99_product_index") or {}
    ents = idx.get("entries") or []
    if not ents:
        return []
    hit = set()
    for key in {code8, code if len(code) == 10 else None} - {None}:
        hit.update(idx.get("exact", {}).get(key, []))
    for k in (code8[:4], code8[:6]):
        hit.update(idx.get("prefix", {}).get(k, []))
    for r in idx.get("ranges") or []:
        L = len(r["from"])
        c = (code if len(code) >= L else code8)[:L]
        if len(c) == L and r["from"] <= c <= r["to"]:
            hit.add(r["i"])
    o = normalize_origin(origin_code)
    out, seen = [], set()
    for i in sorted(hit):
        e = ents[i]
        if e.get("原产地条件") and o != e["原产地条件"]:
            continue
        key = (e["note"], e["子条"])
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


# Special 栏的协定代码（SPI）→ 协定名与适用原产地。只收现行自贸协定；GSP（A/A+/A*）
# 早已过期、AGOA（D）等状态需另核，都不列——列了会被当成"可以享受"。
SPI_PROGRAMS = {
    "S": ("USMCA 美墨加协定", {"CA", "MX"}), "S+": ("USMCA 美墨加协定", {"CA", "MX"}),
    "KR": ("美韩自贸协定", {"KR"}), "AU": ("美澳自贸协定", {"AU"}), "SG": ("美新自贸协定", {"SG"}),
    "CL": ("美智自贸协定", {"CL"}), "IL": ("美以自贸协定", {"IL"}), "JO": ("美约自贸协定", {"JO"}),
    "BH": ("美巴林自贸协定", {"BH"}), "OM": ("美阿曼自贸协定", {"OM"}), "MA": ("美摩洛哥自贸协定", {"MA"}),
    "PE": ("美秘鲁自贸协定", {"PE"}), "CO": ("美哥伦比亚自贸协定", {"CO"}), "PA": ("美巴拿马自贸协定", {"PA"}),
    "P": ("CAFTA-DR", {"CR", "DO", "SV", "GT", "HN", "NI"}),
    "P+": ("CAFTA-DR", {"CR", "DO", "SV", "GT", "HN", "NI"}),
}
_SPECIAL_SEG_RE = re.compile(r"([^()]*?)\(([^)]*)\)")


def special_rate_hint(special, origin_code):
    """
    Special 栏对该原产地的提示文本；不适用则空串。

    **只提示、不套用**：Special 栏能否享受取决于原产地规则与原产地证明，编码本身
    答不了。此前这一栏完全没用，墨西哥、韩国的货照一般税率报，多算十几个点。
    """
    o = normalize_origin(origin_code)
    if not special or o in (ORIGIN_CN, ORIGIN_UNSPECIFIED):
        return ""
    for rate_txt, codes in _SPECIAL_SEG_RE.findall(special):
        for spi in (c.strip() for c in codes.split(",")):
            prog = SPI_PROGRAMS.get(spi)
            if prog and o in prog[1]:
                rate_txt = rate_txt.strip() or "Free"
                return (f"Special 栏含 {spi}（{prog[0]}）：{rate_txt}。符合该协定原产地规则并具备"
                        f"原产地证明时可按此申报；本工具按{'第二栏' if o in COLUMN2_ORIGINS else '一般'}税率计，未自动套用")
    return ""


# ---------- 主查询 ----------

def query_one(db, code, origin="CN"):
    """
    判定单个 HTS 编码在指定原产地下的措施栈，返回结果字典。
    code 为规范化后的纯数字（8位或10位）。origin 为国家代码（CN / VN / 其他）。
    加征措施（cn301 / flip301）按 measures_config.json 配置裁剪：禁用时输出不含
    该加征字段（如"301加征"、"FLIP 301加征"）、总税负不叠加该项。

    原产地在入口统一归一化（CHN → CN、DE → EU、"墨西哥" → MX、空 → 未指定），
    301 与 FLIP 用同一个结果——此前只有 FLIP 归一化，传 CHN 会得到
    "301 不适用 + FLIP 12.5%"这种自相矛盾的答案。
    """
    rates_8 = db["rates_8"]
    desc_10 = db["desc_10"]
    c99_percent = db["c99_percent"]

    measures = load_measures_config()
    cn301_on = measures.get("cn301", True)
    flip301_on = measures.get("flip301", True)

    origin_code = normalize_origin(origin)
    is_china = origin_code == ORIGIN_CN
    unspecified = origin_code == ORIGIN_UNSPECIFIED
    column2 = origin_code in COLUMN2_ORIGINS
    origin_name = origin_label(origin_code)

    n = len(code)
    code8 = code[:8] if n >= 8 else code

    # FLIP 301 强迫劳动关税判定（所有原产地按国家查表；配置禁用则不判定）
    if flip301_on:
        flip301_pct, flip301_note, flip301_src, flip301_spec = flip301_judge(
            db, origin_code, code8=code8)
    else:
        flip301_pct, flip301_note, flip301_src = "", "FLIP 301 已禁用（配置）", {}
        flip301_spec = {"mode": "none"}

    # 基础信息（所有原产地共用）
    base = rates_8.get(code8, {})
    desc = desc_10.get(code) or base.get("desc", "")
    # 归类路径：祖先品名承载材质/织法/含量阈值等判定条件，供归类论证与人工复核
    _nodes = db.get("path_nodes") or []
    cls_path = [_nodes[i].rstrip(":").strip()
                for i in (base.get("path") or []) if 0 <= i < len(_nodes)]
    general = base.get("general", "")
    special = base.get("special", "")
    col2 = base.get("col2", "")
    # 基础税率栏：古巴/朝鲜/俄罗斯/白俄罗斯按第二栏，其余按一般税率。
    # 此前俄罗斯也按一般税率算，6109.10.00 给到 16.5%，第二栏其实是 90%。
    applied_rate = col2 if column2 else general
    rate_col = "第二栏" if column2 else "一般税率"

    # 中国：301 判定（既有逻辑）；非中国：MFN / 第二栏轨道（不叠加中国 301）
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
    else:  # 越南 / 其他国家 / 未指定：不叠加中国 301
        if origin_code == ORIGIN_VN:
            is301 = "不适用（越南原产）"
            note = "越南原产：不适用中国 301 加征，适用美国 MFN 一般税率"
        elif unspecified:
            is301 = "不适用（未指定原产地）"
            note = ("未指定原产地：仅按 MFN 一般税率计，未计入 301 / FLIP 301 等任何"
                    "原产地相关措施，请选择具体原产地后重查")
        elif column2:
            is301 = "不适用（其他国家原产）"
            note = (f"{origin_name}原产：不适用中国 301 加征；适用第二栏税率"
                    f"（{col2 or '—'}）而非一般税率")
        else:
            is301 = "不适用（其他国家原产）"
            _who = f"{origin_name}（{origin_code}）" if origin_name != "其他国家" else origin_code
            note = f"{_who} 原产：不适用中国 301 加征，适用美国 MFN 一般税率"
        c99_fmt = ""
        pct_txt = ""
        if n < 8:
            note = "⚠ 6位品目仅能给出品目级基础税率" + ("；" + note if note else "")
        if not base:
            note = "⚠ 未在2026现行HTS税率表中找到该子目，可能为旧版编码" + ("；" + note if note else "")
        flip_hist, flip_change = [], ""
        excl_auto, excl_items = None, []   # 301 排除只对中国原产有意义
        vn_measures = vietnam_info(db, code8, origin_code)

    # Special 栏提示（只提示不套用）与未建模措施探测（只探测不判定）
    special_hint = special_rate_hint(special, origin_code) if base else ""
    if special_hint:
        note = (note + "；" if note else "") + special_hint.split("。")[0] + "，见「特殊税率提示」"
    unmodeled = unmodeled_measures(db, origin_code)
    if unmodeled:
        _u = "、".join(f"{u['标目组']}×{u['标目数']}" for u in unmodeled)
        note = (note + "；" if note else "") + (
            f"⚠ 另有未建模 9903 标目以该原产地为条件（{_u}），总税负不完整，请核实")
    # 按产品触发的清单（232 类）：命中即标注，且 FLIP 301 按 note 52(f) 对这些产品不适用。
    # 税额仍按不豁免计（少收比多收危险），文本标"232 存疑"要人核实。
    product_hits = product_measures(db, code, code8, origin_code) if base else []
    if product_hits:
        _p = "、".join(f"note {h['note']} {h['子条'].split(' ')[0]}" for h in product_hits[:3])
        note = (note + "；" if note else "") + (
            f"⚠ 落在按产品触发的 Chapter 99 清单（{_p}，232 类），该措施本工具未计；"
            f"FLIP 301 对此类产品按 note 52(f) 不适用，两者均需人工核实")
        if flip301_on and (flip301_spec or {}).get("mode") in ("flat", "net_mfn", "conditional"):
            # "+12.5%(范围存疑)" 已带括号时并进去，不叠成两个括号
            flip301_pct = (flip301_pct[:-1] + "；232 存疑)" if (flip301_pct or "").endswith(")")
                           else (flip301_pct or "") + "(232 存疑)")
            flip301_spec = {"mode": "conditional", "scope": "232", "fallback": flip301_spec}

    # 配置裁剪：禁用加征时备注标注，且不输出加征字段
    if not cn301_on:
        note = (note + "；301 加征已禁用（配置）") if note else "301 加征已禁用（配置）"
    if "范围存疑" in (flip301_pct or ""):
        # 备注是列表页唯一能看全的文字列，范围限制这种"结论有条件"的信息必须进来，
        # 否则一眼扫过去只看到一个百分比，看不出这笔税还取决于商品用途。
        # 232 存疑可能又套了一层 conditional，ANNEX II 的范围名在最内层
        _s = flip301_spec or {}
        while _s.get("mode") == "conditional" and _s.get("scope") == "232":
            _s = _s.get("fallback") or {}
        _scope = _s.get("scope", "")
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
        # 本次计算实际采用的基础税率栏（第二栏国家用 col2），rate.calc_total 读它
        "基础税率栏": rate_col,
        "适用基础税率": applied_rate,
        "特殊税率提示": special_hint,
        "301判定": is301,
        "9903子目": c99_fmt,
        "备注": note,
        # ---- v1.3 新增字段 ----
        "原产地": origin_name,
        "原产地代码": origin_code,
        "301 flip历史": flip_hist,
        "301 flip变化": flip_change,
        "越南措施": vn_measures,
        # 以该原产地为条件、本工具未建模的 9903 标目组（探测结果，供人核实）
        "未建模措施": unmodeled,
        # 按产品触发的 Chapter 99 清单命中（232 类；探测结果，供人核实）
        "产品类未建模措施": product_hits,
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
        # 报关要填的 9903.05.xx 标目（301 那边一直输出 9903.88.xx，FLIP 此前没有）
        result["FLIP 301标目"] = flip_heading_of(flip301_spec)

    # 来源追溯：每条税负判定的官方出处（文件 + 位置 + 说明），供 Web 端弹窗展示
    sources = []
    meta = db.get("meta") or {}
    line = base.get("line", "")
    sources.append({
        "key": "htsdata",
        "类型": "基础税率",
        "文件": f"{meta.get('hts_csv', 'htsdata.csv')}（USITC 全量税率表）",
        "位置": f"第 {line} 行" if line else "",
        "说明": f"一般税率 {general or '—'}" + (f"；特殊 {special}" if special else "")
                + (f"；第二栏 {col2}" if col2 else "")
                + (f"；本次按第二栏计（{origin_name}）" if column2 else ""),
    })
    if special_hint:
        sources.append({"key": "htsdata", "类型": "Special 栏（协定税率提示）",
                        "文件": f"{meta.get('hts_csv', 'htsdata.csv')}（USITC 全量税率表）",
                        "位置": f"第 {line} 行" if line else "", "说明": special_hint})
    if is_china and c99_fmt and not is301.startswith("无法判定"):
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
        if (flip301_spec or {}).get("mode") not in ("none", None):
            _ex = flip_exceptions_of(db, origin_code)
            if _ex:
                sources.append({
                    "key": "local", "类型": "FLIP 301 例外标目（条件需人工核对）",
                    "文件": "htsdata.csv 9903.05.85–.99 / 9903.06（U.S. note 52 例外）", "位置": "",
                    "说明": "；".join(f"{e['标目']} {e['描述'][:100]}" for e in _ex[:10])
                            + (f"；另有 {len(_ex) - 10} 条" if len(_ex) > 10 else ""),
                })
    if is_china and c99_fmt and not is301.startswith("无法判定"):
        # 有历史就讲变化；没有就讲清楚"是没数据，不是没变过"。
        # 一片空白看起来像"该编码档位从没变过"，会让人放心地按当前档位报关——
        # 而事实只是这个人工维护的存根库没覆盖到它。
        _flips = (db.get("flip_301") or {}).get("flips") or {}
        sources.append({
            "key": "local", "类型": "301 flip 历史",
            "文件": "data/flip_301.json（本地转录）", "位置": "",
            "说明": flip_change or (
                f"⚠ 无此编码的历史档位数据。该库为人工转录存根，当前仅覆盖 "
                f"{len(_flips)} 个编码（{'、'.join(fmt(c, 8) for c in sorted(_flips))}），"
                f"空白不代表该编码档位未变过。"),
        })
    if vn_measures:
        sources.append({"key": "local", "类型": "适用措施说明", "文件": "data/vietnam_measures.json（本地转录）", "位置": "", "说明": vn_measures})
    if unmodeled:
        sources.append({
            "key": "local", "类型": "未建模措施（探测，需人工核实）",
            "文件": "htsdata.csv 9903 标目（本工具未建模的部分）", "位置": "",
            "说明": "；".join(
                f"{u['标目组']}（{u['依据']}，{u['触发']}，{u['标目数']} 个标目，如 "
                + "、".join(f"{s['标目']} {s['税率']}" for s in u["示例"][:2])
                + (f"；编者注：{' / '.join(x[:120] for x in u['编者注'][:2])}" if u.get("编者注") else "")
                + "）"
                for u in unmodeled)
                + "。这些标目的法律状态与是否适用本工具无法判断，总税负未计入，请核实。",
        })
    if product_hits:
        sources.append({
            "key": "ch99_pdf", "类型": "未建模措施（按产品触发，232 类，探测）",
            "文件": f"{meta.get('ch99_pdf', 'Chapter 99 PDF')}（subchapter III U.S. notes）",
            "位置": f"第 {product_hits[0]['页']} 页" if product_hits[0].get("页") else "",
            "说明": "；".join(
                f"note {h['note']} {h['子条']}（{h['措施'][:60]}"
                + (f"；编者注：{h['状态'][0][:80]}" if h.get("状态") else "") + "）"
                for h in product_hits[:4])
                + "。清单只是必要条件，note 内另有含量/用途/技术参数条件，编码判不出；"
                  "该措施税额本工具未计，FLIP 301 对此类产品按 note 52(f) 不适用，请人工核实。",
        })
    # 这一条对每个编码都成立：不是"没查到"，是"本工具没有这份数据"。
    # 此前有一列"附加税"永远为空（数据里只有 99 章标目有值），空格子被读成"没有反倾销"。
    sources.append({
        "key": "none", "类型": "反倾销/反补贴（AD/CVD）", "文件": "未覆盖", "位置": "",
        "说明": "本工具不含 AD/CVD 案件数据。反倾销/反补贴按案件与出口商定税，不在 htsdata.csv 里，"
                "请另查 ITA / ACE；此处空白不代表无案件。",
    })
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
    return results, {"total": len(results), "origin": origin_label(origin),
                     "origin_code": normalize_origin(origin), **counts}
