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
