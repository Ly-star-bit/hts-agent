# -*- coding: utf-8 -*-
"""
criteria.py —— 归类判定条件抽取与证据清单（Web / 命令行共用）

面向"有商品、要定编码"场景。HTS 里区分相邻子目的往往不是品名，而是藏在
归类路径里的量化条件：材质含量百分比、织法、涂层、重量/尺寸/价值门槛。
这些条件决定税率能差几倍，也决定报关时海关会要什么材料。

例：6201.40.35（14.9%）与 6201.40.40（49.5¢/kg + 19.6%）同属一个父节点，
    唯一区别是后者 "Containing 36 percent or more by weight of wool"。
    用户要报低税率那个，就必须能证明羊毛含量低于 36%——这份"必须证明什么"
    正是本模块要输出的东西。

本模块只做**确定性文本抽取**，不含任何推断：
  - 条件原文逐条摘出，供人工核对
  - 证据清单按条件类型映射，是行业通行做法而非法律意见
抽取不到不代表没有条件（官方还有章注、类注、GRI 未纳入本地数据），
因此输出一律附"仅供核对、正式归类以 CBP 裁定为准"的口径。
"""
import re

# 条件类型 → (匹配规则, 该条件需要的证据)
# 规则依据 htsdata.csv 全量语料实测的真实写法，不是凭空设计的模板。
_RULES = [
    (
        "含量阈值",
        # containing not over 45 percent by weight of butterfat
        # Containing over 6 percent but not over 35 percent by weight of ...
        # Containing 5 percent or more by weight of soybean oil
        # Containing by weight 99 percent or more lactose
        re.compile(
            r"[Cc]ontaining\s+(?:by\s+weight\s+)?"
            r"(?:not\s+over\s+|over\s+|not\s+less\s+than\s+|less\s+than\s+)?"
            r"\d+(?:\.\d+)?\s*percent[^.;>]{0,90}",
        ),
        "第三方成分检测报告（须列出各成分重量百分比）",
    ),
    (
        "主材质",
        re.compile(
            r"\bOf\s+(?:cotton|wool|fine animal hair|silk|man-made fibers|synthetic fibers|"
            r"artificial fibers|leather|plastics|rubber|glass|paper|paperboard|wood|"
            r"iron or steel|stainless steel|aluminum|copper|zinc|nickel)\b",
            re.I,
        ),
        "成分报告；纺织品另需洗水唛（Care Label），且须与报关品名一致",
    ),
    (
        "织法（针织/钩编）",
        re.compile(r"[Kk]nitted or crocheted"),
        "面料织造方式说明（针织 61 章 / 梭织 62 章，归类分章的依据）",
    ),
    (
        "涂层/浸渍/层压",
        re.compile(
            r"\b(?:coated|impregnated|laminated|covered|coating|backed)\b[^.;>]{0,70}", re.I),
        "面料结构剖面图或工艺说明（说明涂层材质、位置、是否可见）",
    ),
    (
        "防水性能",
        re.compile(r"[Ww]ater resistant[^.;>]{0,50}"),
        "防水性能测试报告（如 AATCC 35 淋雨试验）",
    ),
    (
        "价值门槛",
        re.compile(r"valued\s+(?:not\s+)?(?:over|under|at)[^.;>]{0,60}", re.I),
        "商业发票 / 单位价值证明（须能核到每件或每单位）",
    ),
    (
        "重量门槛",
        re.compile(r"weighing\s+(?:not\s+)?(?:more|less)\s+than[^.;>]{0,50}", re.I),
        "装箱单或称重记录（须与申报口径一致：净重 / 毛重 / 单件重）",
    ),
    (
        "尺寸门槛",
        re.compile(
            r"\b(?:exceeding|not exceeding|measuring|of a (?:width|length|thickness))"
            r"[^.;>]{0,60}", re.I),
        "产品规格书或图纸（标注对应尺寸）",
    ),
]

# 章级硬规则：不靠文本匹配，由编码本身决定
_CHAPTER_RULES = {
    "61": ("织法（针织/钩编）", "本章为针织或钩编服装；梭织应归 62 章",
           "面料织造方式说明（针织 vs 梭织直接决定归入 61 还是 62 章）"),
    "62": ("织法（梭织）", "本章为非针织非钩编（梭织）服装；针织应归 61 章",
           "面料织造方式说明（针织 vs 梭织直接决定归入 61 还是 62 章）"),
}


def extract(db, code8):
    """
    抽取某个 8 位子目的归类判定条件。

    返回 [{类型, 原文, 出处, 证据}, ...]，按在归类路径中出现的先后排列。
    出处标明该条件来自哪一级（祖先品名 / 本级品名 / 章级规则），
    因为父级条件对整个分支生效，本级条件只对该子目生效——这个区别
    在归类论证时很关键。
    """
    import rate

    info = (db.get("rates_8") or {}).get(code8) or {}
    if not info:
        return []

    segments = [(p, "归类路径") for p in rate.path_of(db, code8)]
    own = info.get("desc") or ""
    if own:
        segments.append((own, "本级品名"))

    out = []
    seen = set()
    for text, where in segments:
        # 已被前序（更具体的）规则占用的字符区间，避免同一句话被拆成多条。
        # 例："Containing 36 percent or more by weight of wool" 已作为含量阈值抽出，
        # 其中的 "of wool" 不应再单独报一条主材质——那是同一个条件的一部分。
        taken = []
        for kind, pat, evidence in _RULES:
            for m in pat.finditer(text):
                if any(m.start() < e and m.end() > s for s, e in taken):
                    continue
                taken.append((m.start(), m.end()))
                raw = re.sub(r"\s+", " ", m.group(0)).strip().rstrip(",;:")
                key = (kind, raw.lower())
                if key in seen:
                    continue
                seen.add(key)
                out.append({"类型": kind, "原文": raw, "出处": where, "证据": evidence})

    ch = code8[:2]
    if ch in _CHAPTER_RULES:
        kind, note, evidence = _CHAPTER_RULES[ch]
        if not any(o["类型"].startswith("织法") for o in out):
            out.append({"类型": kind, "原文": note, "出处": f"第 {ch} 章章级规则",
                        "证据": evidence})
    return out


def evidence_list(criteria):
    """把条件清单折叠成去重后的证据清单（准备材料时按这份清点）"""
    seen, out = set(), []
    for c in criteria:
        e = c.get("证据", "")
        if e and e not in seen:
            seen.add(e)
            out.append(e)
    return out


def compare(db, codes):
    """
    并列比较多个候选编码的判定条件，用于归类分歧场景。

    返回 {候选: [...], 跨章: bool, 章列表: [...], 分歧提示: str}。
    跨章说明候选落在完全不同的商品大类上（如塑料雨衣 39 章 vs 梭织夹克 62 章），
    这是典型的归类争议信号——两者税率常相差十几个百分点，且论证方向完全不同。
    """
    import rate

    items = []
    for c in codes:
        c8 = re.sub(r"\D", "", str(c))[:8]
        info = (db.get("rates_8") or {}).get(c8) or {}
        items.append({
            "编码": c8,
            "章": c8[:2],
            "完整品名": rate.full_desc(db, c8),
            "一般税率": info.get("general", ""),
            "判定条件": extract(db, c8),
        })
    chapters = sorted({i["章"] for i in items if i["章"]})
    cross = len(chapters) > 1
    hint = ""
    if cross:
        hint = (f"候选跨 {len(chapters)} 个章（{'、'.join(chapters)}），属典型归类分歧："
                "不同章的判定依据完全不同，需按 GRI 从『构成基本特征的部件』或"
                "『最终用途』论证。税率差额显著时建议申请海关预裁定。")
    return {"候选": items, "跨章": cross, "章列表": chapters, "分歧提示": hint}


# ---------- 一物多号的自动识别 ----------

DISPUTE_TOP_K = 8       # 只看最相关的前若干条：更靠后的多是同词碰巧命中，不是候选
DISPUTE_MAX_GROUPS = 5  # 展示上限，超出部分计数告知，不静默截断

_PCT_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _fmt8(code):
    """'62014035' → '6201.40.35'（core.fmt 的 8 位特化，避免为一个格式化反向依赖 core）"""
    c = re.sub(r"\D", "", str(code))[:8]
    return ".".join([c[:4], c[4:6], c[6:8]]) if len(c) == 8 else c


def _pct_num(text):
    """'34.9%（含301…）' → 34.9；'Free' → 0.0；'需折算'/空 → None"""
    s = str(text or "").strip()
    if not s:
        return None
    if s.lower() in ("free", "免税"):
        return 0.0
    m = _PCT_RE.search(s)
    return float(m.group()) if m else None


def detect_dispute(db, rows, top_k=DISPUTE_TOP_K):
    """
    从搜索结果里自动识别"一物多号"，不需要用户先勾选。

    分组依据是 4 位品目而不是 8 位子目：同一品目下的相邻子目只是同类商品的
    参数细分（羊毛含量 36% 上下、尺寸档次），要的是继续问参数；**跨品目**
    才是真正的归类分歧（塑料雨衣 3926 vs 梭织夹克 6201），两边的论证方向
    和所需证据完全不同。

    返回 {有分歧, 跨章, 章列表, 分组, 基础税差, 税负反转, 无法比较, 提示}。
    无分歧时 有分歧=False，前端不显示——避免每次搜索都弹一条警告。
    """
    import rate

    groups, order = {}, []
    for r in (rows or [])[:top_k]:
        code = re.sub(r"\D", "", str(r.get("编码", "")))
        if len(code) < 4:
            continue
        h4 = code[:4]
        if h4 not in groups:
            groups[h4] = {"品目": h4, "章": h4[:2], "代表": r, "命中数": 0}
            order.append(h4)
        groups[h4]["命中数"] += 1

    shown, hidden = order[:DISPUTE_MAX_GROUPS], order[DISPUTE_MAX_GROUPS:]

    # 各候选的归类路径，用来算"分歧点"。判定条件常常是一样的
    # （6201 男式大衣与 6202 女式大衣都是 Of wool + 梭织），列出共同点
    # 说明不了任何问题——用户要看的是这几个码彼此差在哪一句上。
    paths = {}
    for h4 in shown:
        c8 = re.sub(r"\D", "", str(groups[h4]["代表"].get("编码", "")))[:8]
        segs = groups[h4]["代表"].get("归类路径") or rate.path_of(db, c8)
        paths[h4] = [re.sub(r"\s+", " ", s).strip().rstrip(":") for s in segs if s]

    items, uncomparable = [], []
    for h4 in shown:
        g = groups[h4]
        r = g["代表"]
        code8 = re.sub(r"\D", "", str(r.get("编码", "")))[:8]
        total = rate.calc_total(db, code8) or {}
        base_n = r.get("等效从价数值")
        total_n = _pct_num(total.get("总税负估算"))
        if total_n is None:
            # 从量税/复合税没有单位货值折算不出百分比。这类候选不能参与税差
            # 比较，但必须说出来——否则用户会以为"税差 0"是它们真的一样。
            uncomparable.append(_fmt8(code8))
        others = {s for k, v in paths.items() if k != h4 for s in v}
        distinct = [s for s in paths[h4] if s not in others]
        items.append({
            "品目": h4,
            # 只此候选独有的路径措辞——这几句就是选它 or 不选它的分界
            "分歧点": distinct[:2],
            "章": g["章"],
            "编码": _fmt8(code8),
            "商品描述": r.get("商品描述", ""),
            "完整品名": r.get("完整品名", "") or rate.full_desc(db, code8),
            "一般税率": r.get("一般税率", ""),
            "等效从价": r.get("等效从价", ""),
            "等效从价数值": base_n,
            "总税负估算": total.get("总税负估算", ""),
            "总税负数值": total_n,
            "301判定": r.get("301判定", ""),
            "判定条件": extract(db, code8),
            "同品目候选数": g["命中数"],
        })

    chapters = sorted({i["章"] for i in items})
    cross = len(chapters) > 1

    # 基础税率最低 ≠ 总税负最低。301/FLIP 常把顺序整个翻过来
    # （Free 但 +25%，输给 0.7% 但 +7.5% 的那个），这是本工具最容易误导人的地方。
    base_ok = [i for i in items if i["等效从价数值"] is not None]
    spread = None
    if len(base_ok) >= 2:
        spread = round(max(i["等效从价数值"] for i in base_ok)
                       - min(i["等效从价数值"] for i in base_ok), 2)
    # 反转只在两个值都有的候选里判断：拿 A 集合的基础最小值去比 B 集合的
    # 总税负最小值，两边成员不同，结论没有意义
    both = [i for i in items
            if i["等效从价数值"] is not None and i["总税负数值"] is not None]
    reversal = ""
    if len(both) >= 2:
        cheap_base = min(both, key=lambda i: i["等效从价数值"])
        cheap_total = min(both, key=lambda i: i["总税负数值"])
        if cheap_base["编码"] != cheap_total["编码"]:
            reversal = (f"基础税率最低的是 {cheap_base['编码']}（{cheap_base['等效从价']}），"
                        f"但计入 301/FLIP 后总税负最低的反而是 {cheap_total['编码']}"
                        f"（{cheap_total['总税负数值']:g}% vs {cheap_base['总税负数值']:g}%）"
                        f"——别只按基础税率挑便宜的。")

    tips = []
    if cross:
        tips.append(f"候选跨 {len(chapters)} 个章（{'、'.join(chapters)}），"
                    "不同章的判定依据完全不同，需按 GRI 从『构成基本特征的部件』或『最终用途』论证。")
    else:
        tips.append("候选同属一章、不同品目，分歧点通常在材质/织法/用途的具体措辞上。")
    if hidden:
        tips.append(f"另有 {len(hidden)} 个品目未展示（{'、'.join(hidden)}），可在下方结果表中勾选比较。")
    if uncomparable:
        tips.append(f"{'、'.join(uncomparable)} 为从量税或复合税，未填单位货值时无法折算成百分比，"
                    "未计入税差比较。")

    return {
        "有分歧": len(items) >= 2,
        "跨章": cross,
        "章列表": chapters,
        "分组": items,
        "基础税差": spread,
        "税负反转": reversal,
        "无法比较": uncomparable,
        "未展示品目": hidden,
        "提示": " ".join(tips),
    }
