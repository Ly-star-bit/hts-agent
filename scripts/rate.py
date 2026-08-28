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
import json
import os
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

    # 归一化成纯数字。带点的 '8215.99.30' 是本工具在界面、导出、API 响应里到处
    # 显示的形式，调用方原样传回来是最自然的用法，但此前会被当成无法解析的编码，
    # 静默返回"需折算 / 需人工（无法解析）"——不是报错，是一个看起来像合理限制的
    # 错误答案（8215.99.30 实际是 14% 纯从价）。/api/estimate 的 codes 列表路径
    # 就踩了这个坑（text 路径经 extract_codes 清洗过，所以 Web 界面看不出来）。
    code = re.sub(r"\D", "", str(code or ""))

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


def path_of(db, code):
    """取某个 8 位子目的归类路径（祖先品名列表）。db 里存的是字符串表下标。"""
    nodes = db.get("path_nodes") or []
    idxs = (db.get("rates_8", {}).get(code) or {}).get("path") or []
    return [nodes[i] for i in idxs if 0 <= i < len(nodes)]


def full_desc(db, code):
    """归类路径 + 自身品名，用 ' > ' 连接。子目品名多为 'Other'，单看无意义。"""
    info = db.get("rates_8", {}).get(code) or {}
    parts = [p.rstrip(":").strip() for p in path_of(db, code)]
    own = (info.get("desc") or "").rstrip(":").strip()
    if own:
        parts.append(own)
    return " > ".join(p for p in parts if p)


# ---------- 用户词汇 → 官方检索词 ----------

_SYNONYMS_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "synonyms.json")
_synonyms_cache = None


def load_synonyms():
    """
    读取用户词汇 → HTS 官方检索词映射（带缓存）。

    存在的理由：搜索索引是纯英文的（11876 个 token 中文为 0），中文商品名
    直接搜返回 0 条；英文侧也存在口语与税则用词不一致（solar / photovoltaic、
    laptop / automatic data processing machine）。AI 层第一轮做的就是这件事，
    但 AI 默认关闭，不配就整条链路不可用——这张表是离线兜底。

    文件缺失或损坏时返回空表，检索退化为原行为，不影响主链路。
    """
    global _synonyms_cache
    if _synonyms_cache is not None:
        return _synonyms_cache
    terms = {}
    try:
        with open(_SYNONYMS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        for k, v in ((data or {}).get("terms") or {}).items():
            key = str(k).strip().lower()
            vals = [str(x).strip().lower() for x in (v or []) if str(x).strip()]
            if key and vals:
                terms[key] = vals
    except (OSError, json.JSONDecodeError, AttributeError):
        terms = {}
    _synonyms_cache = terms
    return terms


def _clear_synonyms_cache():
    """测试用"""
    global _synonyms_cache
    _synonyms_cache = None


_CJK_RE = re.compile(r"[一-鿿]+")


def expand_query(keyword):
    """
    把用户输入扩展为官方英文检索词，返回 (扩展后的检索串, 应用的映射列表, 未识别的中文片段)。

    中文没有词边界，因此按最长键优先做子串匹配（'太阳能电池板' 要先于 '太阳能'
    命中，否则会退化成只搜 photovoltaic 而丢掉 panels）。英文键按整词匹配，
    避免 'ic' 命中 'plastic'。命中的键从原串中移除，剩余部分照常参与检索。

    第三个返回值是关键：英文分词器会把没映射上的中文**静默丢弃**，
    '塑料制婴儿餐椅' 只识别出 '塑料' 就按 plastics 去搜，返回一个看着挺确定的
    3D 打印机。对报关工具来说，这比返回空更危险——必须把未识别的部分交回给
    调用方提示用户，而不是假装完整检索过。
    """
    terms = load_synonyms()
    text = (keyword or "").lower()
    applied, added = [], []
    for key in sorted(terms, key=len, reverse=True):
        if not key or key not in text:
            continue
        if re.fullmatch(r"[\x00-\x7f]+", key):
            # 纯 ASCII 键要求整词命中，否则 'ic' 会撞上 'plastic'
            if not re.search(r"(?<![a-z0-9])" + re.escape(key) + r"(?![a-z0-9])", text):
                continue
            text = re.sub(r"(?<![a-z0-9])" + re.escape(key) + r"(?![a-z0-9])", " ", text)
        else:
            text = text.replace(key, " ")
        applied.append({"输入词": key, "检索词": terms[key]})
        added.extend(terms[key])
    # 扩展后仍残留的中文 = 词表没覆盖到的部分（'制'、'的' 这类单字虚词不算）
    leftover = [s for s in _CJK_RE.findall(text) if len(s) >= 2]
    if not applied:
        return keyword, [], leftover
    return (text + " " + " ".join(added)).strip(), applied, leftover


def _stem(w):
    """
    轻量英文复数还原。HTS 品名里的词形变体绝大多数就是单复数：
    coats / gloves / batteries / fibers / cells。

    此前只有 prefix5（取前 5 字符）做词干归并，要求词长 ≥5，于是 coat↔coats、
    wool↔woolen、cell↔cells 这些 4 字母词完全匹配不上——而服装、材料类的关键词
    恰恰大量是短词，'wool coat' 这种最自然的查询直接返回空。
    """
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"          # batteries → battery
    if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        return w[:-1]                # coats → coat；glass / status 不动
    return w


def build_search_index(db):
    """
    构建内存倒排索引（模块级缓存）：
      - index: token -> [norm8]         自身品名词索引（同时以原形与词干双键收录）
      - prefix5 -> [norm8]              前 5 字符词干索引（长词的模糊归并）
      - path_index: token -> [norm8]    祖先品名词索引（同样双键收录）

    双键收录 + 查询时也查词干，等于双向归一：'coats' 收在 {coats, coat} 下，
    查 'coat' 或 'coats' 都能命中。

    祖先词单独建索引而不是并进主索引：大量子目品名就是 'Other'，不吃祖先词根本
    检索不到；但祖先词覆盖面很广（一个品目下挂几十个子目），并进主索引会淹没
    精确匹配。因此分开存，打分时祖先命中按较低权重计入。
    """
    global _index_cache
    if _index_cache is not None:
        return _index_cache
    index = {}
    prefix5 = {}
    desc_map = {}
    path_index = {}

    def _add(target, tok, code):
        target.setdefault(tok, set()).add(code)
        st = _stem(tok)
        if st != tok:
            target.setdefault(st, set()).add(code)

    for code, info in db["rates_8"].items():
        desc = info.get("desc", "")
        desc_map[code] = desc
        for tok in re.findall(r"[a-z0-9]+", desc.lower()):
            _add(index, tok, code)
            if len(tok) >= 5:
                prefix5.setdefault(tok[:5], set()).add(code)
        for anc in path_of(db, code):
            for tok in re.findall(r"[a-z0-9]+", anc.lower()):
                _add(path_index, tok, code)
    _index_cache = (index, desc_map, prefix5, path_index)
    return _index_cache


# 祖先词命中的权重系数：够让 'Other' 这类子目被检索到，又不至于压过精确匹配
PATH_WEIGHT = 0.35

# 织法 → 章的偏置。这条知识**无法从文本得到**，必须外挂：
#   61 章 heading 字面写着 "knitted or crocheted"（全库 350 处祖先命中），
#   而 62 章的 heading 完全不提织法——"梭织"是靠"不在 61 章"反向定义的，
#   61/62 两章里出现 'woven' 的行总共只有 4 条。
# 于是产生一个不对称的错误：搜"针织夹克"正确（词面命中 61 章），搜"梭织夹克"
# 前 20 条里却有 14 条是针织的 61 章成衣——织法整个反了。而 61/62 税率不同，
# 归错章就是归错码。criteria._CHAPTER_RULES 早已编码了同一事实，只是检索侧不知道。
# 幅度取 4.0，与既有的词组连续命中加分（+4/+6）同量级。
_WEAVE_CHAPTER_BIAS = {
    "woven":     {"62": 4.0, "61": -4.0},
    "knitted":   {"61": 4.0, "62": -4.0},
    "crocheted": {"61": 4.0, "62": -4.0},
}


_TOTAL_NUM_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*%")


def _total_num(text):
    """'26.5%（含301…）' → 26.5；'需人工（无法解析）' → None（排序时置末）"""
    m = _TOTAL_NUM_RE.match(str(text or ""))
    return float(m.group(1)) if m else None


def _weave_bias(tokens):
    """
    查询词 → {章: 偏置}。

    knitted 与 crocheted 是同一个意图的两种说法（官方固定搭配
    "knitted or crocheted"），只计一次，否则同义词展开会把偏置翻倍。
    若查询同时含互斥意图（woven + knitted），两边相加自然抵消为 0——
    用户自己都没说清织法时，不该由检索替他决定。
    """
    bias, seen = {}, set()
    for t in tokens:
        rule = _WEAVE_CHAPTER_BIAS.get(t)
        if not rule:
            continue
        key = tuple(sorted(rule.items()))
        if key in seen:
            continue
        seen.add(key)
        for ch, d in rule.items():
            bias[ch] = bias.get(ch, 0.0) + d
    return bias


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


def search(db, keyword, limit=100, sort="relevance", include_special=False,
           unit_value=None, origin="CN"):
    """
    关键词搜索 8 位子目：匹配英文品名 + 编码。

    sort:
      - relevance  按匹配相关度（默认）
      - total_asc  按**总税负**升序（基础 + 301 + FLIP + 附加税）
      - tax_asc    按基础等效从价升序
      - tax_desc   按基础等效从价降序
      - code_asc   按编码升序

    默认从 tax_asc 改为 relevance：基础税率最低 ≠ 总税负最低。
    实测 8215.99.30 基础 14%、不在 301 清单，总税负 26.5%；
    8215.99.35 基础 6.8%、+7.5% 301 + 12.5% FLIP，总税负 26.8%——
    按基础税率排序会把更贵的那个排在前面，而这页原本的说法是"找税率最低的编码"。
    要按成本挑请用 total_asc。

    unit_value / origin 仅 total_asc 用到：从量税要有单位货值才能折算成
    百分比，301/FLIP 要有原产地才知道加不加。

    include_special：是否包含第 98/99 章，默认否。
      98 章是特殊归类条款（复进口、随身物品免税等），99 章是临时立法条款
      （9902 临时减免、9903 加征/配额）。两者都不是"给商品定编码"时的答案——
      它们要么是附加适用，要么是特殊情形，正式归类必须落在第 1-97 章。
      而 99 章品名往往写得极其具体（如 "Boys' woven man-made fiber coats,
      containing 36 percent..."），词组匹配得分很高，不排除会霸占结果首位，
      诱导用户拿一个不能用于常规申报的编码去报关。

    返回:
      [{'编码', '商品描述', '一般税率', '税率类型', '等效从价', '等效从价数值',
        '301判定', '9903子目', '301加征', '附加税', '相关度'}, ...]
    """
    raw_kw = (keyword or "").strip()
    kw, _applied, _leftover = expand_query(raw_kw)
    kw = kw.strip().lower()
    if not kw:
        return []
    import core as _core
    measures = _core.load_measures_config()  # cn301 禁用时 301 加征列不输出
    index, desc_map, prefix5, path_index = build_search_index(db)

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
    # 98/99 章的排除必须在 AND/OR 分支判断**之前**生效。
    # 若放在最后过滤：AND 交集恰好只剩 98/99 时 and_hits 非空 → 走 AND 分支 →
    # 过滤后清空 → 永远不会降级到 OR，整个查询返回 0 条。
    # （实测 'wool coat woven' 的 AND 交集就只有 1 条 99 章记录。）
    drop_special = not include_special and not (norm_kw and norm_kw[:2] in ("98", "99"))

    def _filter(s):
        return {c for c in s if c[:2] not in ("98", "99")} if drop_special else s

    def _hits(tok, with_path=True):
        """某个词命中的编码集合：原形 + 词干 + 前缀归并 +（可选）祖先品名"""
        st = _stem(tok)
        hit = set(index.get(tok, set())) | set(index.get(st, set()))
        if len(tok) >= 5:
            hit |= prefix5.get(tok[:5], set())  # 长词的前缀归并
        if with_path:
            # 让品名为 'Other' 的子目也能被检索到
            hit |= path_index.get(tok, set()) | path_index.get(st, set())
        return _filter(hit)

    if tokens:
        and_hits = None
        for tok in tokens:
            hit = _hits(tok)
            and_hits = hit if and_hits is None else (and_hits & hit)
        if and_hits:
            codes |= and_hits
        else:
            min_hits = 2 if len(tokens) >= 2 else 1
            hit_counts = {}
            for tok in tokens:
                for c in _hits(tok):
                    hit_counts[c] = hit_counts.get(c, 0) + 1
            codes |= {c for c, n in hit_counts.items() if n >= min_hits}

    # 打分排序
    import math
    N = max(len(desc_map), 1)
    # idf 权重：罕见词（lithium）权重大，宽泛词（electric）权重小
    weights = {tok: math.log(N / (len(index.get(tok, set())) + 1)) + 0.5 for tok in tokens}
    weave = _weave_bias(tokens)
    rows = []
    for code in codes:
        desc = desc_map.get(code, "")
        info = db["rates_8"].get(code, {})
        gen = info.get("general", "")
        p = parse_rate(gen)
        av = estimate_ad_valorem(gen)  # 无单位货值：纯从价可比较，从量返回 None
        # 相关度：自身品名命中满权重，祖先品名命中按 PATH_WEIGHT 折算，
        # 再加描述开头命中与编码命中的加权
        desc_low = desc.lower()
        words = re.findall(r"[a-z0-9]+", desc_low)
        anc = path_of(db, code)
        anc_words = {_stem(w) for w in re.findall(r"[a-z0-9]+", " ".join(anc).lower())}
        score = 0.0
        for t in tokens:
            w = weights.get(t, 1.0)
            if any(_stem_match(t, x) for x in words):
                score += w
            elif _stem(t) in anc_words:
                score += w * PATH_WEIGHT
        # 词组连续命中加分：'man-made fibers' 是官方固定说法，拆成 man/made/fibers
        # 三个高频词独立计分会让"碰巧含这三个词"的无关子目挤到前面。
        # 整串在完整品名里连续出现，说明匹配到的是术语本身而非几个散词。
        if len(tokens) >= 2:
            full_low = full_desc(db, code).lower()
            if kw in full_low:
                score += 6
            elif kw in desc_low:
                score += 4
        if desc_low.startswith(kw):
            score += 5
        if norm_kw and len(norm_kw) >= 6 and code.startswith(norm_kw):
            score += 8
        # 织法偏置：61 章按定义就是针织，查"梭织"时它不该与 62 章并列
        score += weave.get(code[:2], 0.0)
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
            # 归类路径：子目品名多为 'Other'，判定条件（材质/织法/含量阈值）写在祖先上，
            # 归类争议场景下这才是能拿来论证的依据
            "归类路径": [a.rstrip(":").strip() for a in anc],
            "完整品名": full_desc(db, code),
            "章": code[:2],
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

    # 排序。每种排序都以编码作次级键打破平局——rows 的初始顺序来自 set 迭代，
    # 受 Python 字符串哈希随机化影响，同分项在不同进程里顺序不同。没有次级键时
    # 同一个查询两次会返回不同的候选（"梭织涂层夹克"相关度 6.45 那一档，五次跑出
    # 五组不同编码），limit 截断更把这种抖动放大成"结果里有没有这条"。
    # 报关工具的结果必须可复现，也才对得起页面上"确定性结果"的说法。
    # 总税负列对每一行都补。此前只在调用方给了单位货值时才有值，其余情况表格里
    # 是"—"——而基础税率单独看会误导人（8215.99.30 基础 14% 比 8215.99.35 的
    # 6.8% 贵，总税负却更便宜），这恰恰是这张表最该给出的信息。
    # calc_total 单次约 0.04ms，但 rows 在截断前可能有几千行，所以只有 total_asc
    # 需要全量算（要拿它排序），其余排序等排完序截断后再补。
    def _fill_total(items):
        for r in items:
            t = calc_total(db, r["编码"], unit_value=unit_value, origin=origin)
            r["总税负估算"] = t["总税负估算"]
            r["总税负数值"] = _total_num(t["总税负估算"])
            r["301加征数值"] = t["301加征数值"]
        return items

    if sort == "total_asc":
        _fill_total(rows)
        # 折算不出的（从量/复合税未给单位货值）排最后，与 tax_asc 的处理一致
        rows.sort(key=lambda r: (r["总税负数值"] is None,
                                 r["总税负数值"] if r["总税负数值"] is not None else 0,
                                 r["编码"]))
    elif sort == "tax_asc":
        rows.sort(key=lambda r: (r["等效从价数值"] is None,
                                 r["等效从价数值"] if r["等效从价数值"] is not None else 0,
                                 r["编码"]))
    elif sort == "tax_desc":
        rows.sort(key=lambda r: (r["等效从价数值"] is None,
                                 -(r["等效从价数值"] if r["等效从价数值"] is not None else 0),
                                 r["编码"]))
    elif sort == "code_asc":
        rows.sort(key=lambda r: r["编码"])
    else:
        rows.sort(key=lambda r: (-r["相关度"], r["编码"]))

    out = rows[:limit]
    if sort != "total_asc":
        _fill_total(out)
    return out


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
