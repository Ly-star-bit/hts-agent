# -*- coding: utf-8 -*-
"""
rate.py —— 税率解析与总税负计算引擎（Web / 命令行共用）

在 core.py 的 301 判定基础上扩展：
  1. parse_rate()            将任意税率文本解析为结构化形态（Free/百分比/从量/复合/引用/复杂）
  2. estimate_ad_valorem()   折算等效从价税率（百分比），从量部分需单位货值参数
  3. calc_total()            计算单编码总税负（基础 + 301 加征 + 附加税）
  4. search()                关键词搜索（英文品名 + 编码）+ 按总税负排序

税率形态说明（USITC 官方数据实测）：
  - 'Free'                                  免税
  - '6.5%'                                  从价税
  - '1¢/kg' / '$1.646/kg' / '0.9¢ each'     从量税（单位：kg/liter/head/each/pr./doz./m2 等）
  - '46.3¢/kg + 14.9%'                      复合税（从量 + 从价）
  - 'The duty provided in the applicable subheading'   引用式（税率见被引子目）
  - '$1.61 each + 4.4% on the case...'      复杂分部件税率（无法简单折算）
"""
import re

# ---------- 常量 ----------

SPECIFIC_UNITS = {
    "kg": "千克", "kilogram": "千克", "kilograms": "千克", "kilo": "千克", "k": "千克",
    "g": "克", "gram": "克", "grams": "克",
    "lb": "磅", "lbs": "磅", "pound": "磅", "pounds": "磅",
    "liter": "升", "liters": "升", "litre": "升", "litres": "升", "l": "升",
    "head": "头", "each": "件", "ea": "件", "article": "件", "articles": "件",
    "pr": "双", "pr.": "双", "pair": "双", "pairs": "双",
    "doz": "打", "doz.": "打", "dozen": "打", "dozens": "打",
    "m2": "平方米", "sq. m": "平方米", "sq m": "平方米", "sq. meter": "平方米",
    "ton": "吨", "tons": "吨", "metric ton": "吨",
    "gross": "罗", "grosses": "罗",
}

# 百分比正则：6.5% 或 1.2% + 3.4%
_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
# 从量单位正则：kg / liter / head / each / pr. / doz. / m2 / article 等
_SPECIFIC_UNIT_RE = re.compile(
    r"(?:/|per\s+)?\s*([a-zA-Z0-9. ]+?)\s*(?:\+|$|,|on\s)", re.IGNORECASE
)
_REFERENCE_TEXT = "the duty provided in the applicable subheading"
_REFERENCE_TEXT_ALT = "the duty provided in such subheading"


# ---------- 税率解析 ----------

def parse_rate(text, strip_paren=True):
    """
    解析税率文本为结构化形态。

    返回:
      {
        'kind': 'free'|'percent'|'specific'|'compound'|'reference'|'complex'|'unknown',
        'ad_valorem': float|None,   # 从价百分比部分（compound 时为其中百分比之和）
        'specific': [{'usd': float, 'unit': str}],  # 从量部分（usd 为每单位美元）
        'text': 原文,
      }

    strip_paren=True 时剥离括号内容（如 FTA 协定国家标注 "Free (A+,AU,...)"）。
    """
    if not text:
        return {"kind": "unknown", "ad_valorem": None, "specific": [], "text": text or ""}
    raw = text.strip()
    if strip_paren:
        # 去掉括号及内容（FTA 国家标注），保留括号外文本
        cleaned = re.sub(r"\([^)]*\)", "", raw).strip()
    else:
        cleaned = raw
    if not cleaned:
        cleaned = raw

    low = cleaned.lower()
    if low == "free":
        return {"kind": "free", "ad_valorem": 0.0, "specific": [], "text": raw}
    if low in ("no additional duty", "no duty", "none"):
        return {"kind": "free", "ad_valorem": 0.0, "specific": [], "text": raw}
    if _REFERENCE_TEXT in low or _REFERENCE_TEXT_ALT in low:
        return {"kind": "reference", "ad_valorem": None, "specific": [], "text": raw}

    # 复杂分部件税率（"on the case and strap..." 或 3+ 部分组合），即使可解析也标记 complex
    if (" on the " in low or ("+" in cleaned and " on " in low)
            or cleaned.count("+") >= 2):
        pcts = [float(m) for m in _PCT_RE.findall(cleaned)]
        return {
            "kind": "complex",
            "ad_valorem": sum(pcts) if pcts else None,
            "specific": _parse_specific(cleaned),
            "text": raw,
        }

    pcts = [float(m) for m in _PCT_RE.findall(cleaned)]
    specifics = _parse_specific(cleaned)

    if pcts and specifics:
        return {"kind": "compound", "ad_valorem": sum(pcts), "specific": specifics, "text": raw}
    if pcts:
        return {"kind": "percent", "ad_valorem": sum(pcts), "specific": [], "text": raw}
    if specifics:
        return {"kind": "specific", "ad_valorem": None, "specific": specifics, "text": raw}
    return {"kind": "unknown", "ad_valorem": None, "specific": [], "text": raw}


def _parse_specific(text):
    """提取文本中的从量税部分，返回 [{'usd': float, 'unit': str}]。

    支持两种写法：'$1.646/kg'（美元在数字前）与 '46.3¢/kg'、'0.9 cents each'（美分在数字后）。
    """
    result = []
    low = text.lower()
    # 美元：$1.646 / $ 1.104
    for m in re.finditer(r"\$\s*(\d+(?:\.\d+)?)", low):
        usd = float(m.group(1))
        unit = _extract_unit(low, m.end())
        result.append({"usd": round(usd, 6), "unit": unit})
    # 美分：46.3¢ / 0.9 cents
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*(?:¢|cents?)", low):
        usd = float(m.group(1)) / 100.0
        unit = _extract_unit(low, m.end())
        result.append({"usd": round(usd, 6), "unit": unit})
    return result


def _extract_unit(text, start):
    """从量税金额后截取单位词（kg / liter / each / pr / doz / m2 等），返回原文小写。"""
    tail = text[start:start + 24]
    m = _SPECIFIC_UNIT_RE.match(tail)
    if not m:
        return ""
    token = m.group(1).strip().rstrip(".").strip()
    # 归一化常见单位变体（liters→liter、pair→pr、dozen→doz 等），统一输出
    for k, v in SPECIFIC_UNITS.items():
        if k == "pr.":
            continue
        if token == k or token.startswith(k) or k.startswith(token):
            return k
    return token


# ---------- 等效从价折算 ----------

def estimate_ad_valorem(text, unit_value=None):
    """
    折算等效从价税率（百分比）。

    规则：
      - Free → 0.0
      - 百分比 → 直接返回
      - 复合税 → 从价部分 + 从量部分 ÷ 单位货值
      - 纯从量 / 复杂 / 引用 / 无法解析 → 需 unit_value 才可折算，否则返回 None

    unit_value: 每单位货值（美元/单位），如商品按 kg 计重则传 $/kg。
    """
    p = parse_rate(text)
    if p["kind"] == "free":
        return 0.0
    if p["kind"] == "percent":
        return p["ad_valorem"]
    if p["kind"] in ("compound", "specific", "complex"):
        av = p["ad_valorem"] or 0.0
        if p["specific"] and unit_value and unit_value > 0:
            total_specific = sum(s["usd"] for s in p["specific"])
            return round(av + total_specific / unit_value * 100.0, 4)
        # 复合税只返回从价部分等于凭空抹掉从量部分（'$1.104/kg + 14.9%' 会被当成 14.9%，
        # 实际按 $4/kg 折算是 42.5%），且调用方无从分辨这个数字是否完整 → 一律返回 None，
        # 由 calc_total 落到"需人工"。complex 同理，且其多个百分比常作用于不同价值部件，
        # 相加本身就无意义。
        return None
    return None


# ---------- 总税负计算 ----------

def calc_total(db, code, unit_value=None, origin="CN"):
    """
    计算单个 HTS 编码在指定原产地下的总税负（基础税率 + 适用措施 + 附加税）。

    origin：
      - CN（默认）：总税负 = 基础 + 301 加征 + 附加税
      - VN：总税负 = 基础 + 附加税（越南原产不适用中国 301）
    加征叠加受 measures_config.json 配置控制：禁用项由 core.query_one 裁剪，
    本函数读取裁剪后的字段，自动不叠加禁用项。

    返回在 core.query_one 结果基础上扩展的字典，新增字段：
      - 税率类型:     Free / 从价 / 从量 / 复合 / 引用 / 复杂 / 未知
      - 基础等效从价: 折算后的基础税率百分比文本（'6.5%' / '需折算' / '无法解析'）
      - 301加征数值:  百分比数值（越南为 0）
      - FLIP 301加征数值: FLIP 301 百分比数值
      - 附加税等效:   附加税折算百分比文本
      - 总税负估算:   总税负文本（'31.5%' / '需人工'）
    """
    import core  # 延迟导入，避免循环依赖

    base = core.query_one(db, code, origin=origin)
    gen = base.get("一般税率", "")
    p = parse_rate(gen)
    base_av = estimate_ad_valorem(gen, unit_value)

    # 301 加征百分比（仅中国原产适用；配置禁用时 query_one 已不输出该字段 → 0）
    if (origin or "CN").strip().upper() == "VN":
        pct301 = 0.0
    else:
        m = re.search(r"([\d.]+)\s*%", base.get("301加征", ""))
        pct301 = float(m.group(1)) if m else 0.0

    # FLIP 301 强迫劳动关税（2026-07-24 生效，按原产地查表；配置禁用 → 无档位 → 0）
    pct_flip, flip_note = _flip_amount(base.get("FLIP 301档位"), base_av)

    # 附加税
    add_text = base.get("附加税", "") or ""
    add_av = estimate_ad_valorem(add_text, unit_value) if add_text else 0.0
    add_unresolved = bool(add_text) and add_av is None

    # 汇总：任一分项无法折算，就不能给出确定的总额
    if base_av is None:
        total = None
        total_txt = ("需人工（复合/从量税，请提供单位货值）"
                     if p["kind"] in ("specific", "compound", "complex")
                     else "需人工（无法解析）")
    elif add_unresolved:
        total = None
        total_txt = "需人工（附加税为从量税，请提供单位货值）"
    elif flip_note:
        total = None
        total_txt = f"需人工（{flip_note}）"
    else:
        total = round(base_av + pct301 + pct_flip + (add_av or 0.0), 4)
        total_txt = f"{total:g}%（含301/FLIP301/附加税估算）"

    kind_names = {
        "free": "免税", "percent": "从价", "specific": "从量",
        "compound": "复合", "reference": "引用", "complex": "复杂", "unknown": "未知",
    }
    result = dict(base)
    result.update({
        "税率类型": kind_names.get(p["kind"], p["kind"]),
        "基础等效从价": _fmt_av(base_av),
        "301加征数值": pct301,
        "FLIP 301加征数值": pct_flip,
        "附加税等效": _fmt_av(add_av),
        "总税负估算": total_txt,
    })
    return result


def _flip_amount(spec, base_av):
    """
    按 FLIP 301 档位算实际加征百分比，返回 (百分比, 无法计算时的原因)。

    档位来自 core.flip301_judge：
      flat    —— 在 MFN 之上直接加 rate
      net_mfn —— 与 MFN 合计封顶 cap，实际加征 max(0, cap - MFN)；
                 EU/TW 合计 10%，JP/KR/CH 合计 12.5%。这一档不能按显示文本
                 "≤+10%" 直接相加，否则 MFN 已达上限的商品会被凭空多加一遍
                 （如 EU 产 6109.10.00，MFN 16.5%，正确总额就是 16.5%）。
      exempt / none —— 0
    MFN 无法折算时 net_mfn 档也算不出来，返回原因让调用方标"需人工"。
    """
    spec = spec or {}
    mode = spec.get("mode", "none")
    if mode == "flat":
        return float(spec.get("rate", 0.0)), ""
    if mode == "net_mfn":
        cap = float(spec.get("cap", 0.0))
        if base_av is None:
            return 0.0, f"FLIP 301 与 MFN 合计封顶 {cap:g}%，但基础税率无法折算"
        return max(0.0, round(cap - base_av, 4)), ""
    return 0.0, ""


def _fmt_av(av):
    """等效从价百分比转显示文本：None → '需折算/无法解析'"""
    if av is None:
        return "需折算"
    return f"{av:g}%"


# ---------- 关键词搜索 ----------

_index_cache = None


def build_search_index(db):
    """
    构建内存倒排索引（模块级缓存）：
      - token(小写词) -> [norm8 编码]          精确词索引
      - prefix5(词前5字符) -> [norm8 编码]     词干索引（battery/batteries 共用 'batte'）
    """
    global _index_cache
    if _index_cache is not None:
        return _index_cache
    index = {}
    prefix5 = {}
    desc_map = {}
    for code, info in db["rates_8"].items():
        desc = info.get("desc", "")
        desc_map[code] = desc
        for tok in re.findall(r"[a-z0-9]+", desc.lower()):
            index.setdefault(tok, set()).add(code)
            if len(tok) >= 5:
                prefix5.setdefault(tok[:5], set()).add(code)
    _index_cache = (index, desc_map, prefix5)
    return _index_cache


def _clear_index_cache():
    """测试用：清空索引缓存"""
    global _index_cache
    _index_cache = None


def _stem_match(a, b):
    """词干启发式匹配：完全相同 / 互为前缀 / 最长公共前缀 >= 5（覆盖 battery/batteries 等变体）"""
    if not a or not b:
        return False
    if a == b:
        return True
    if a.startswith(b) or b.startswith(a):
        return True
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n >= 5


def search(db, keyword, limit=100, sort="tax_asc"):
    """
    关键词搜索 8 位子目：匹配英文品名 + 编码。

    sort:
      - tax_asc   按等效从价税率升序（默认，配合"最低税率"场景）
      - tax_desc  按等效从价税率降序
      - relevance 按匹配相关度
      - code_asc  按编码升序

    返回:
      [{'编码', '商品描述', '一般税率', '税率类型', '等效从价', '等效从价数值',
        '301判定', '9903子目', '301加征', '附加税', '相关度'}, ...]
    """
    kw = (keyword or "").strip().lower()
    if not kw:
        return []
    import core as _core
    measures = _core.load_measures_config()  # cn301 禁用时 301 加征列不输出
    index, desc_map, prefix5 = build_search_index(db)

    # 编码直接匹配
    codes = set()
    norm_kw = re.sub(r"\D", "", kw)
    if len(norm_kw) in (8, 10) or kw.replace(".", "").isdigit():
        for code in desc_map:
            # 纯前缀匹配：'8507' → 8507 章；避免子串命中（如 '8507' 出现在 87085070 中段）
            if norm_kw and code.startswith(norm_kw):
                codes.add(code)

    # 词匹配：先 AND（所有词都命中）；AND 为空时降级为加权 OR（至少命中 2 个词）。
    # 原因：AI 归类链路中 LLM 可能给出宽泛关键词（如 electric storage），
    # 纯 AND 会因官方品名不含这些词而召回为空，纯 OR 又会被宽泛词淹没精确词。
    tokens = [t for t in re.findall(r"[a-z0-9]+", kw)
              if len(t) >= 2 and not t.isdigit()]  # 纯数字 token（编码片段）无语义，过滤
    if tokens:
        and_hits = None
        for tok in tokens:
            hit = set(index.get(tok, set()))
            if len(tok) >= 5:
                hit |= prefix5.get(tok[:5], set())  # 词干变体：battery → batteries
            and_hits = hit if and_hits is None else (and_hits & hit)
        if and_hits:
            codes |= and_hits
        else:
            min_hits = 2 if len(tokens) >= 2 else 1
            hit_counts = {}
            for tok in tokens:
                hit = set(index.get(tok, set()))
                if len(tok) >= 5:
                    hit |= prefix5.get(tok[:5], set())
                for c in hit:
                    hit_counts[c] = hit_counts.get(c, 0) + 1
            codes |= {c for c, n in hit_counts.items() if n >= min_hits}

    # 打分排序
    import math
    N = max(len(desc_map), 1)
    # idf 权重：罕见词（lithium）权重大，宽泛词（electric）权重小
    weights = {tok: math.log(N / (len(index.get(tok, set())) + 1)) + 0.5 for tok in tokens}
    rows = []
    for code in codes:
        desc = desc_map.get(code, "")
        info = db["rates_8"].get(code, {})
        gen = info.get("general", "")
        p = parse_rate(gen)
        av = estimate_ad_valorem(gen)  # 无单位货值：纯从价可比较，从量返回 None
        # 相关度：加权词干命中分 + 描述开头命中加权 + 编码命中加权
        desc_low = desc.lower()
        words = re.findall(r"[a-z0-9]+", desc_low)
        score = sum(weights.get(t, 1.0) for t in tokens
                    if any(_stem_match(t, w) for w in words))
        if desc_low.startswith(kw):
            score += 5
        if norm_kw and len(norm_kw) >= 6 and code.startswith(norm_kw):
            score += 8
        c99 = db["sec301_map"].get(code)
        pct301 = db["c99_percent"].get(c99) if c99 else None
        if pct301:
            judge301, pct_txt = "是", f"+{pct301:g}%"
        elif c99:
            judge301, pct_txt = "是(豁免)", "0%(豁免)"
        else:
            judge301, pct_txt = "否", ""
        # 配置裁剪：cn301 禁用时 301 加征列不输出（与查询/估算路径一致）
        if not measures.get("cn301", True):
            pct_txt = ""
        rows.append({
            "编码": core_fmt(code),
            "商品描述": desc,
            "一般税率": gen,
            "税率类型": {"free": "免税", "percent": "从价", "specific": "从量",
                         "compound": "复合", "reference": "引用", "complex": "复杂",
                         "unknown": "未知"}.get(p["kind"], p["kind"]),
            "等效从价": _fmt_av(av),
            "等效从价数值": av,
            "301判定": judge301,
            "9903子目": core_fmt(c99) if c99 else "",
            "301加征": pct_txt,
            "附加税": db["add_duty"].get(code, ""),
            "相关度": round(score, 2),
        })

    # 排序
    if sort == "tax_asc":
        rows.sort(key=lambda r: (r["等效从价数值"] is None, r["等效从价数值"] if r["等效从价数值"] is not None else 0))
    elif sort == "tax_desc":
        rows.sort(key=lambda r: (r["等效从价数值"] is None, -(r["等效从价数值"] if r["等效从价数值"] is not None else 0)))
    elif sort == "code_asc":
        rows.sort(key=lambda r: r["编码"])
    else:
        rows.sort(key=lambda r: -r["相关度"])

    return rows[:limit]


def core_fmt(code):
    """编码纯数字 → 带点格式（兼容 8/10 位）"""
    if not code:
        return ""
    if len(code) == 10:
        return f"{code[0:4]}.{code[4:6]}.{code[6:8]}.{code[8:10]}"
    if len(code) == 8:
        return f"{code[0:4]}.{code[4:6]}.{code[6:8]}"
    if len(code) == 6:
        return f"{code[0:4]}.{code[4:6]}"
    return code
