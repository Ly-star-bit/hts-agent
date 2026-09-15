# -*- coding: utf-8 -*-
"""
build_db.py —— 构建 301 关税本地查询数据库

从两份官方原始数据构建合并索引：
  1. htsdata.csv                       —— USITC 全量税率表（基础税率 + 9903 子目税率 + 附加关税）
  2. China Tariffs_2026HTSRev15.pdf    —— USTR 301 中国清单（8位HTS → 9903.xx 归属映射）

输出：data/sec301_db.json，供 query_301.py 批量查询使用。
数据更新方法：替换上述两个原始文件后重新运行本脚本即可。

用法：python scripts/build_db.py
"""
import csv
from collections import Counter
import json
import os
import re
import sys

# Windows 控制台中文输出兼容
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
HTS_CSV = os.path.join(BASE_DIR, "htsdata.csv")
PDF_FILE = os.path.join(BASE_DIR, "China Tariffs_2026HTSRev15.pdf")
OUT_JSON = os.path.join(DATA_DIR, "sec301_db.json")


def norm(code: str) -> str:
    """规范化 HTS 编码：仅保留数字。如 '0101.21.00' → '01012100'，'0101.21.00.10' → '0101210010'"""
    return re.sub(r"\D", "", code or "")


def parse_hts_csv():
    """
    解析 htsdata.csv，返回：
      - rates_8:   {norm8: {desc, path, general, special, col2, line}}  8位子目税率与描述
      - desc_10:   {norm10: desc}                               10位统计后缀描述（更具体）
      - add_duty:  {norm(8或10): 附加关税文本}                   反倾销/反补贴等附加税
      - c99_rates: {norm(9903.xx): General税率文本}             99章子目税率（含301加征比例）

    关于 path（归类路径）：
    HTS 是树形税则，子目品名只写与父级的差异，单看往往没有意义——6201.40.35 的品名
    就是 "Padded sleeveless jackets"，看不出它是化纤制；而"是不是化纤制"恰恰写在
    父节点 6201.40 "Of man-made fibers" 上，正是归类争议里的材质分水岭。
    更极端的是大量子目品名就是 "Other" / "Of cotton (220)"，完全无法检索。

    CSV 的 Indent 列给出了层级，且**结构节点（HTS Number 为空的行，如 "Other:"、
    "Of man-made fibers:"）本身也参与层级**，因此不能像此前那样直接跳过——
    跳过它们会让路径断层。这里用 (缩进 → 品名) 的栈还原每个编码的祖先链。
    """
    rates_8 = {}
    desc_10 = {}
    add_duty = {}
    c99_rates = {}
    # 9903 标目正文与税率：FLIP 301 逐经济体标目从这里推导，未建模措施的探测也读它
    c99_rows = {}
    stack = []          # [(indent, desc)]，维护当前所在的层级路径
    # 计量单位（Unit of Quantity）：8 位行上基本是空的（6857 条里 6839 条空），
    # 单位写在 10 位统计行上。估算页要按数量算钱，必须告诉用户"这行该按什么计数"，
    # 所以 8 位的单位从它的 10 位子目继承（取出现最多的那个）。
    units_10, units_by_8 = {}, {}
    with open(HTS_CSV, encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        next(reader)  # 跳过表头
        for row in reader:
            if not row or len(row) < 3:
                continue
            # 注意：CSV 中存在跨物理行的记录（描述内嵌换行），enumerate 计数会偏移，
            # 必须用 reader.line_num 记录真实物理行号，保证"来源追溯"定位准确
            line_no = reader.line_num
            desc = row[2].strip()
            try:
                indent = int((row[1] or "").strip())
            except ValueError:
                indent = len(stack)     # 缩进列异常时按当前深度处理，不让路径断掉

            # 维护层级栈：弹出所有不比当前浅的节点，再压入自己
            while stack and stack[-1][0] >= indent:
                stack.pop()
            ancestors = [d for _, d in stack]
            if desc:
                stack.append((indent, desc))

            raw = row[0].strip().strip('"')
            if not raw:
                continue        # 纯结构节点：已入栈供后代取用，本身无编码无税率
            n = norm(raw)
            units = row[3].strip() if len(row) > 3 else ""
            general = row[4].strip() if len(row) > 4 else ""
            special = row[5].strip() if len(row) > 5 else ""
            col2 = row[6].strip() if len(row) > 6 else ""
            add = row[8].strip() if len(row) > 8 else ""
            entry = {"desc": desc, "path": list(ancestors), "general": general,
                     "special": special, "col2": col2, "line": line_no}
            if len(n) == 8:
                rates_8[n] = entry
                if add:
                    add_duty.setdefault(n, add)
                if units:
                    units_10[n] = units      # 少数 8 位行自带单位（全表仅 18 条）
            elif len(n) == 10:
                desc_10[n] = desc
                if units:
                    units_10[n] = units
                    units_by_8.setdefault(n[:8], []).append(units)
                if add:
                    add_duty.setdefault(n, add)
                # 部分 8 位子目在官方文件中只以 10 位形式出现（如 0203.29.20.00），
                # 税率写在 10 位行上：将其继承到 8 位前缀，保证 301 判定可回查税率。
                if general and n[:8] not in rates_8:
                    rates_8[n[:8]] = entry
            if n.startswith("9903"):
                c99_rates[n] = general
                if len(n) == 8 or n[:8] not in c99_rows:
                    c99_rows[n[:8]] = {"desc": re.sub(r"\s+", " ", desc), "general": general,
                                       "line": line_no}

    # 路径节点重复率约 89%（34814 次引用 / 3815 个不同字符串），直接内联会让
    # 数据库多出 2.8MB。改存字符串表 + 下标引用，降到 0.5MB。
    path_nodes = sorted({d for e in rates_8.values() for d in e["path"]})
    node_idx = {s: i for i, s in enumerate(path_nodes)}
    for e in rates_8.values():
        e["path"] = [node_idx[d] for d in e["path"]]

    # 8 位单位 = 子目下最常见的那个 10 位单位
    units_8 = {}
    for c8, lst in units_by_8.items():
        units_8[c8] = Counter(lst).most_common(1)[0][0]
    units_8.update({k: v for k, v in units_10.items() if len(k) == 8})
    # 10 位只留"和 8 位父级不一致"的那些——同一子目下绝大多数 10 位单位相同，
    # 全存一遍会给库白加几百 KB。查不到就回落到 8 位。
    units_10 = {k: v for k, v in units_10.items()
                if len(k) == 10 and units_8.get(k[:8]) != v}
    return rates_8, desc_10, add_duty, c99_rates, path_nodes, units_8, units_10, c99_rows


# ---------- Chapter 99 标目：FLIP 301 官方标目推导 + 未建模措施探测 ----------

# 9903 标目正文里出现过的国家/经济体写法 → 代码。只收 htsdata.csv 实测出现的写法，
# 匹配时按最长名优先整词匹配（"Hong Kong, China" 先命中 Hong Kong，不再算成 China）。
# 没映射上的名字不会被静默丢掉：探测结果里带"未识别名称"，人工补表。
NAME_TO_ISO = {
    "Afghanistan": "AF", "Algeria": "DZ", "Angola": "AO", "Argentina": "AR", "Australia": "AU",
    "Bahamas": "BS", "Bahrain": "BH", "Bangladesh": "BD", "Belarus": "BY", "Bolivia": "BO",
    "Bosnia and Herzegovina": "BA", "Botswana": "BW", "Brazil": "BR", "Brunei": "BN",
    "Cambodia": "KH", "Cameroon": "CM", "Canada": "CA", "Chad": "TD", "Chile": "CL",
    "China": "CN", "Colombia": "CO", "Costa Rica": "CR", "Cuba": "CU",
    "Democratic Republic of the Congo": "CD", "Dominican Republic": "DO", "Ecuador": "EC",
    "Egypt": "EG", "El Salvador": "SV", "Equatorial Guinea": "GQ", "European Union": "EU",
    "Falkland Islands": "FK", "Fiji": "FJ", "Ghana": "GH", "Guatemala": "GT", "Guyana": "GY",
    "Honduras": "HN", "Hong Kong, China": "HK", "Hong Kong": "HK", "Iceland": "IS", "India": "IN", "Indonesia": "ID",
    "Iraq": "IQ", "Israel": "IL", "Japan": "JP", "Jordan": "JO", "Kazakhstan": "KZ",
    "Kuwait": "KW", "Laos": "LA", "Lesotho": "LS", "Libya": "LY", "Liechtenstein": "LI",
    "Madagascar": "MG", "Malawi": "MW", "Malaysia": "MY", "Mauritius": "MU", "Mexico": "MX",
    "Moldova": "MD", "Morocco": "MA", "Mozambique": "MZ", "Myanmar": "MM", "Namibia": "NA",
    "Nauru": "NR", "New Zealand": "NZ", "Nicaragua": "NI", "Nigeria": "NG",
    "North Korea": "KP", "North Macedonia": "MK", "Norway": "NO", "Oman": "OM",
    "Pakistan": "PK", "Papua New Guinea": "PG", "Peru": "PE", "Philippines": "PH",
    "Qatar": "QA", "Russian Federation": "RU", "Russia": "RU", "Saudi Arabia": "SA",
    "Serbia": "RS", "Singapore": "SG", "South Africa": "ZA", "South Korea": "KR",
    "Sri Lanka": "LK", "Switzerland": "CH", "Syria": "SY", "Taiwan": "TW", "Thailand": "TH",
    "Trinidad and Tobago": "TT", "Tunisia": "TN", "Türkiye": "TR", "Turkey": "TR",
    "United Arab Emirates": "AE", "United Kingdom": "GB", "Uruguay": "UY", "Vanuatu": "VU",
    "Venezuela": "VE", "Vietnam": "VN", "Zimbabwe": "ZW",
}
_NAME_PATTERNS = [(re.compile(r"(?<![A-Za-z])" + re.escape(k) + r"(?![A-Za-z])"), v)
                  for k, v in sorted(NAME_TO_ISO.items(), key=lambda kv: -len(kv[0]))]
_NOTE_RE = re.compile(r"U\.S\. note (\d+)")
# 中国 301 计划的三组标目（USTR 清单 + 排除 + 2024/2026 新增档位），工具已建模
MODELED_301_PREFIXES = ("990388", "990391", "990392")
# 按产品触发的措施在标目正文里的关键词，只用来给未建模组打"产品类"标签
PRODUCT_TERMS = ("steel", "aluminum", "copper", "passenger vehicles", "light trucks",
                 "medium- and heavy-duty", "semiconductor", "lumber", "timber",
                 "civil aircraft", "pharmaceutical", "quartz", "tires", "leather")


def _fmt_c99(h):
    return f"{h[:4]}.{h[4:6]}.{h[6:8]}"


def origins_in(desc):
    """标目正文提及的经济体代码列表（去重保序）。只认 NAME_TO_ISO 里的写法。"""
    text, found = desc, []
    for pat, iso in _NAME_PATTERNS:
        if pat.search(text):
            text = pat.sub(" ", text)
            if iso not in found:
                found.append(iso)
    return found


def derive_flip301_headings(c99_rows):
    """
    从 htsdata.csv 的 9903.05/9903.06 标目推导 FLIP 301 逐经济体档位与报关标目。

    官方表里每个经济体一行："articles the product of X, as provided for in U.S. note 52"，
    税率写在 General 栏（"… + 12.5%"）；EU/TW/JP/KR/CH 这类 net-of-MFN 档是两行：
    MFN ≥ 上限的那行不加征，MFN < 上限的那行 General 栏直接写 "10%"（合计封顶）。
    .85–.92 与 9903.06.xx 是例外标目（在途、232 产品、民用航空器、医药、USMCA 货等）。

    此前档位靠手抄 JSON（data/flip301_forced_labor.json）。手抄会漂，而且报关要填的
    9903.05.xx 标目从没输出过——301 那边是输出 9903.88.xx 的。JSON 保留作交叉校验。
    返回 {"by_origin": {ISO: {...}}, "exceptions": [...], "exceptions_by_origin": {ISO: [...]},
          "unparsed": [...]}
    """
    by_origin, general_ex, ex_by_origin, unparsed = {}, [], {}, []
    for h in sorted(c99_rows):
        row = c99_rows[h]
        d, g = row["desc"], row["general"]
        # .85–.92 是 FLIP 通用例外（在途 / 捐赠 / 信息材料等），有几行正文不引用 note 52，
        # 只能按标目号归属；9903.06 整组都是 note 52 的经济体例外
        in_flip_block = ((h.startswith("990305") and 85 <= int(h[6:8]) <= 99)
                         or h.startswith("990306"))
        if 52 not in {int(n) for n in _NOTE_RE.findall(d)} and not in_flip_block:
            continue
        origins = origins_in(d)
        item = {"标目": _fmt_c99(h), "描述": d[:220], "line": row["line"]}
        is_exception = ("subdivision" in d or h.startswith("990306")
                        or (h.startswith("990305") and 85 <= int(h[6:8]) <= 99))
        if is_exception:
            if origins:
                for o in origins:
                    ex_by_origin.setdefault(o, []).append(item)
            else:
                general_ex.append(item)
            continue
        if len(origins) != 1:
            unparsed.append(item)
            continue
        o = origins[0]
        rec = by_origin.setdefault(o, {"origin": o})
        m_add = re.search(r"\+\s*(\d+(?:\.\d+)?)\s*%", g)
        m_flat = re.fullmatch(r"(\d+(?:\.\d+)?)\s*%", g.strip())
        m_lt = re.search(r"less than (\d+(?:\.\d+)?) percent", d)
        if m_add and not m_lt:
            rec.update(mode="flat", rate=float(m_add.group(1)), heading=h, line=row["line"])
        elif m_lt and m_flat:
            rec.update(mode="net_mfn", cap=float(m_flat.group(1)), heading_below=h,
                       line_below=row["line"])
        elif "equal to or greater than" in d:
            rec.update(mode="net_mfn", heading_at_or_above=h, line_at_or_above=row["line"])
        else:
            unparsed.append(item)
    return {"by_origin": by_origin, "exceptions": general_ex,
            "exceptions_by_origin": ex_by_origin, "unparsed": unparsed}


# 按产品触发的 Chapter 99 措施（业内叫 232 类：钢铝铜 note 16、乘用车 note 33、软木 37、
# 中重型车 38、半导体 39；加拿大特定产品 note 51 兼有原产地条件）。清单由
# scripts/extract_c99_products.py 从 Chapter 99 PDF 提取；note 正文不出现 "section 232"
# 字样，"232" 是按措辞与公告号的推断。这里只编译成"编码 → 命中哪条 note 哪个子条"的索引，
# 查询时探测并标注，不计税。
PRODUCT_NOTES = {"16": "", "33": "", "37": "", "38": "", "39": "", "51": "CA"}


def compile_product_scopes(scopes):
    """
    c99_product_scopes.json → {"entries": [...], "exact": {code: [i]}, "prefix": {code: [i]},
    "ranges": [{"from","to","i"}]}。entries 去重存一份，各编码只引下标，否则 2500 个编码
    各挂一份说明字典会让库白多 500KB。
    """
    entries, exact, prefix, ranges = [], {}, {}, []
    for n, origin_cond in PRODUCT_NOTES.items():
        note = (scopes.get("notes") or {}).get(n)
        if not note:
            continue
        base = {"note": n, "措施": (note.get("measure") or "")[:160],
                "状态": [x[:200] for x in (note.get("status_sentences") or [])[:2]],
                "标目": [h for h in (note.get("headings") or []) if not h.startswith("9903.01")][:6],
                "原产地条件": origin_cond}
        for sub, g in (note.get("groups") or {}).items():
            # 子条标签开头常重复编号（"(iii) Articles of steel"），去掉再拼
            label = re.sub(r"^\s*\([A-Za-z0-9]+\)\s*", "", g.get("label") or "")[:70]
            ent = {**base, "子条": f"{sub} {label}".strip(),
                   "页": (g.get("pages") or [None])[0]}
            entries.append(ent)
            i = len(entries) - 1
            for c in g.get("list") or []:
                d = norm(str(c))
                if len(d) in (8, 10):
                    exact.setdefault(d, []).append(i)
                elif len(d) in (4, 6):
                    prefix.setdefault(d, []).append(i)
            for pair in g.get("ranges") or []:
                if len(pair) == 2:
                    a, b = norm(str(pair[0])), norm(str(pair[1]))
                    if a and b and len(a) == len(b):
                        ranges.append({"from": a, "to": b, "i": i})
    return {"entries": entries, "exact": exact, "prefix": prefix, "ranges": ranges}


def group_unmodeled(c99_rows, flip_headings, note_status=None):
    """
    把工具没建模的 9903 标目按前 6 位分组，记录每组提及了哪些原产地、哪些是
    "any country"、正文里出现了哪些产品词，供查询时探测"总税负不完整"。

    这里只做**探测**不做判定：标目正文提及某原产地（含出现在例外从句里）就记一笔，
    查询时按原产地报"另有 N 个标目以该原产地为条件、本工具未建模"，由人核实。
    """
    groups = {}
    for h in sorted(c99_rows):
        if h[:6] in MODELED_301_PREFIXES or h in flip_headings:
            continue
        row = c99_rows[h]
        d = row["desc"]
        g = groups.setdefault(h[:6], {
            "组": f"{h[:4]}.{h[4:6]}", "标目数": 0, "依据": set(), "按原产地": {},
            "任何国家": {"数量": 0, "示例": []}, "产品词": set(), "示例": None})
        g["标目数"] += 1
        g["依据"].update(f"U.S. note {n}" for n in _NOTE_RE.findall(d))
        sample = {"标目": _fmt_c99(h), "税率": row["general"][:60], "描述": d[:160]}
        if g["示例"] is None:
            g["示例"] = sample
        for o in origins_in(d):
            ent = g["按原产地"].setdefault(o, {"数量": 0, "示例": []})
            ent["数量"] += 1
            if len(ent["示例"]) < 4:
                ent["示例"].append(sample)
        if "any country" in d:
            g["任何国家"]["数量"] += 1
            if len(g["任何国家"]["示例"]) < 4:
                g["任何国家"]["示例"].append(sample)
        low = d.lower()
        g["产品词"].update(t for t in PRODUCT_TERMS if t in low)
    out = []
    for key in sorted(groups):
        g = groups[key]
        g["依据"] = sorted(g["依据"], key=lambda s: int(s.rsplit(" ", 1)[1]))
        g["产品词"] = sorted(g["产品词"])
        # Chapter 99 编者注（"headings 9903.03.01–9903.03.11 expired at the close of July 23, 2026"）：
        # 探测出来的组是否还在执行，PDF 里其实写了，带上它，提示才不会沦为噪音
        notes_cited = [s.rsplit(" ", 1)[1] for s in g["依据"]]
        pool = [x for n in notes_cited for x in (note_status or {}).get(n, [])]
        # 先挑点名本组标目的（"headings 9903.03.01–9903.03.11 expired…"），再挑带状态词的；
        # 一条 note 的编者注可能有七八句，与本组无关的不要占位
        mine = [x for x in pool if g["组"] in x]
        status_words = ("terminated", "expired", "suspended", "Compiler")
        rest = [x for x in pool if x not in mine and any(w in x for w in status_words)]
        g["编者注"] = (mine + rest)[:2]
        out.append(g)
    return out


def parse_ustr_pdf():
    """
    解析 USTR China Tariffs PDF，返回 (mapping, pages)：
      - mapping:  {norm8: norm(9903.xx)}   8 位子目归属映射
      - mapping10:{norm10: norm(9903.xx)}  10 位统计后缀的精确归属
      - partial8: {norm8: [norm10, ...]}   仅部分 10 位后缀被列入的 8 位前缀
      - pages:    {norm(8或10): 物理页号}  在 PDF 中的位置（页号 1 起）

    PDF 为两列表格：HTS 子目 → 适用的 Chapter 99 子目。绝大多数行是 8 位，
    但有少量行精确到 10 位统计后缀（本版 10460 行中 69 行）。

    这 69 行不能截断成 8 位后合并：
      ① 同一 8 位前缀下的不同后缀可能归属不同 9903 子目（本版有 4 个前缀如此，
         例如 6307.90.98 下 …42 是 9903.91.07(+50%)，其余是 9903.88.15(+7.5%)），
         截断 + setdefault 会让先读到的那个覆盖全部，造成漏征或多征；
      ② 即使后缀档位一致，清单列出的也只是**特定后缀**，未列出的后缀不在清单内，
         把归属提升到 8 位会让整个子目被误判为命中。
    因此这类前缀记入 partial8，查询时要求提供完整 10 位编码，不做 8 位层面的猜测。
    """
    import pdfplumber

    mapping = {}
    mapping10 = {}
    pages = {}
    prefix_suffixes = {}     # norm8 -> [norm10, ...]，仅由 10 位行填充
    pat = re.compile(r"^(\d{4}\.\d{2}\.\d{2}\.?\d{0,4})\s+(\d{4}\.\d{2}\.\d{2})$")
    with pdfplumber.open(PDF_FILE) as pdf:
        for idx, page in enumerate(pdf.pages):
            page_no = idx + 1  # 物理页号（1 起，与浏览器 #page=N 一致）
            text = page.extract_text() or ""
            for line in text.splitlines():
                m = pat.match(line.strip())
                if m:
                    hts, c99 = m.groups()
                    if not c99.startswith("9903"):
                        continue
                    code, c99n = norm(hts), norm(c99)
                    if len(code) == 10:
                        mapping10[code] = c99n
                        prefix_suffixes.setdefault(code[:8], []).append(code)
                        pages.setdefault(code, page_no)
                    else:
                        if code in mapping and mapping[code] != c99n:
                            raise ValueError(
                                f"USTR PDF 中 8 位子目 {hts} 出现互相矛盾的 9903 归属："
                                f"{mapping[code]} 与 {c99n}（第 {page_no} 页）。"
                                "请人工核对 PDF 后再构建。")
                        mapping[code] = c99n
                        pages.setdefault(code, page_no)

    # 仅以 10 位形式出现的前缀 → partial8；若该前缀同时有 8 位记录则是数据矛盾，直接报错
    partial8 = {}
    for pref, suffixes in prefix_suffixes.items():
        if pref in mapping:
            raise ValueError(
                f"USTR PDF 中 8 位子目 {pref} 同时存在 8 位记录（{mapping[pref]}）"
                f"与 10 位后缀记录（{sorted(suffixes)}），归属语义不明确，请人工核对。")
        partial8[pref] = sorted(suffixes)
    return mapping, mapping10, partial8, pages


def parse_301_percent(text: str):
    """
    从 9903 子目税率文本解析加征百分比：
      'The duty provided in the applicable subheading plus 25%'  → 25.0
      'The duty provided in the applicable subheading + 7.5%'    → 7.5
      'The duty provided in the applicable subheading'           → 0.0（无加征，如豁免子目）
      其他无法识别                                          → None
    """
    if not text:
        return None
    m = re.search(r"(?:plus|\+)\s*(\d+(?:\.\d+)?)\s*%", text, re.IGNORECASE)
    if m:
        return float(m.group(1))
    if "no change" in text.lower():
        return 0.0
    if text.strip().lower() == "the duty provided in the applicable subheading":
        return 0.0
    return None


def load_json_data(filename, default):
    """从 data/ 目录读取 JSON 数据源；文件缺失或损坏时返回 default（并打印提示）"""
    path = os.path.join(DATA_DIR, filename)
    if not os.path.exists(path):
        print(f"   ⚠ 缺少数据源文件 {filename}，相关功能将返回『数据未覆盖』")
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"   ⚠ 数据源文件 {filename} 解析失败（{e}），相关功能将返回『数据未覆盖』")
        return default


# 产出下限：低于此值说明源文件换版/换排版导致解析失配，宁可构建失败也不能写库。
# 依据本版实测值（rates_8 14954 / sec301_map 10391 / c99_percent 463）留约 15% 余量。
SANITY_MINIMUMS = {
    "rates_8": 12000,
    "desc_10": 15000,
    "sec301_map": 9000,
    "c99_percent": 350,
    # FLIP 301 官方表 60 个经济体（Rev18 实测 60）；9903 标目 636（Rev18）
    "flip301_headings": 50,
    "c99_headings": 500,
}


def sanity_check(counts):
    """
    构建产出的下限校验。返回不合格项列表（空列表表示通过）。

    存在的理由：解析"能跑通但抽不出东西"是静默的——若 USTR 换排版导致正则全部失配，
    sec301_map 会变成 0 条，所有中国原产查询都返回"未命中 301"，业务含义等同于
    "不加征"，属于全库级漏征，且没有任何报错。
    """
    return [
        f"{name}: {counts.get(name, 0)} 条，低于下限 {low}"
        for name, low in SANITY_MINIMUMS.items()
        if counts.get(name, 0) < low
    ]


def build():
    os.makedirs(DATA_DIR, exist_ok=True)
    print("① 解析 htsdata.csv ...")
    (rates_8, desc_10, add_duty, c99_rates, path_nodes,
     units_8, units_10, c99_rows) = parse_hts_csv()
    print(f"   8位子目: {len(rates_8)} | 10位描述: {len(desc_10)} | 附加关税栏行: {len(add_duty)} "
          f"| 9903子目: {len(c99_rates)} | 9903标目: {len(c99_rows)} | 归类路径节点: {len(path_nodes)}")

    print("② 解析 USTR China Tariffs PDF ...")
    sec301_map, sec301_map_10, sec301_partial_8, sec301_pages = parse_ustr_pdf()
    print(f"   301 归属映射: 8位 {len(sec301_map)} 条 | 10位精确 {len(sec301_map_10)} 条 "
          f"| 需10位判定的前缀 {len(sec301_partial_8)} 个")

    print("③ 建立 9903.xx → 加征% 映射 ...")
    c99_percent = {}
    for code, rate_text in c99_rates.items():
        pct = parse_301_percent(rate_text)
        if pct is not None:
            c99_percent[code] = pct
    for code in sorted(c99_percent):
        print(f"   {code}: +{c99_percent[code]}%")

    print("③b 摄入多措施数据源（301 flip / 越南措施 / FLIP 301 关税与豁免）...")
    flip_301 = load_json_data("flip_301.json", {"flips": {}})
    vietnam = load_json_data("vietnam_measures.json", {})
    flip301 = load_json_data("flip301_forced_labor.json", {})
    flip301_exemptions = load_json_data("flip301_exemptions.json", {})
    exclusions = load_json_data("sec301_exclusions.json", {})
    product_scopes = load_json_data("c99_product_scopes.json", {})
    c99_product_index = compile_product_scopes(product_scopes)
    print(f"   按产品触发的 Chapter 99 清单: note {sorted(PRODUCT_NOTES)} → 子条 "
          f"{len(c99_product_index['entries'])} 个 | 精确编码 {len(c99_product_index['exact'])} | "
          f"前缀 {len(c99_product_index['prefix'])} | 区间 {len(c99_product_index['ranges'])}")
    if not c99_product_index["entries"]:
        print("   ⚠ 未找到 data/c99_product_scopes.json —— 232 类产品探测将整体缺失，"
              "请先跑 python scripts/extract_c99_products.py")
    # UFLPA 强迫劳动检查维度已移除（v1.5），不再摄入 uflpa_entities.json
    ex_univ = len(flip301_exemptions.get("universal", []))
    ex_econ = {k: len(v) for k, v in (flip301_exemptions.get("by_economy") or {}).items()}
    print(f"   flip 历史编码: {len(flip_301.get('flips', {}))} | "
          f"越南覆盖编码: {len(vietnam.get('covered_codes', []))} | "
          f"FLIP 301: 10%档 {len((flip301.get('rates') or {}).get('10', []))} | "
          f"12.5%档 {len((flip301.get('rates') or {}).get('125', []))} | "
          f"FLIP 301 豁免: 通用 {ex_univ} | 按经济体 {ex_econ}")
    print("③c 从 9903 标目推导 FLIP 301 官方标目，并探测未建模措施 ...")
    flip_headings = derive_flip301_headings(c99_rows)
    fh = flip_headings["by_origin"]
    modes = Counter(v.get("mode", "?") for v in fh.values())
    print(f"   FLIP 301 经济体标目: {len(fh)} 个（{dict(modes)}）| 通用例外标目 "
          f"{len(flip_headings['exceptions'])} | 经济体例外 "
          f"{sum(len(v) for v in flip_headings['exceptions_by_origin'].values())} | "
          f"未解析 {len(flip_headings['unparsed'])}")
    for it in flip_headings["unparsed"][:5]:
        print(f"     ⚠ 未解析: {it['标目']} {it['描述'][:80]}")
    # 与手抄 JSON 交叉校验：两边不一致要喊出来——官方表是主，JSON 只是校验用
    json_tier = {}
    for tier, lst in ((flip301.get("rates") or {}).items()):
        for o in lst:
            json_tier[o] = tier
    for o, rec in sorted(fh.items()):
        exp = json_tier.get(o)
        got = (f"{rec.get('rate'):g}".replace(".", "") if rec.get("mode") == "flat"
               else f"net_mfn_{rec.get('cap', 0):g}".replace(".", ""))
        if exp is None:
            print(f"     ⚠ 官方表有 {o}（{got}）但 flip301_forced_labor.json 无此经济体")
        elif exp != got:
            print(f"     ⚠ {o} 档位不一致：官方表 {got} / JSON {exp}，以官方表为准")
    for o in sorted(set(json_tier) - set(fh)):
        print(f"     ⚠ JSON 有 {o}（{json_tier[o]}）但官方表未推导出该经济体")
    flip_heading_set = set()
    for rec in fh.values():
        flip_heading_set.update(x for x in (rec.get("heading"), rec.get("heading_below"),
                                            rec.get("heading_at_or_above")) if x)
    for lst in [flip_headings["exceptions"], *flip_headings["exceptions_by_origin"].values()]:
        flip_heading_set.update(norm(it["标目"]) for it in lst)
    note_status = {n: [x for x in (v.get("status_sentences") or []) if x]
                   for n, v in (product_scopes.get("notes") or {}).items()}
    c99_unmodeled = group_unmodeled(c99_rows, flip_heading_set, note_status)
    n_unmodeled = sum(g["标目数"] for g in c99_unmodeled)
    print(f"   未建模 9903 标目: {n_unmodeled} 个，分 {len(c99_unmodeled)} 组："
          + "、".join(f"{g['组']}×{g['标目数']}" for g in c99_unmodeled))

    ex_notes = exclusions.get("notes") or {}
    ex_live = [c for c, v in ex_notes.items() if v.get("status") == "生效中"]
    print(f"   301 排除（U.S. note 20）: 标目 {len(ex_notes)} 个 | 生效中 {len(ex_live)} 个"
          f"（{', '.join(sorted(ex_live)) or '无'}）| 涉及编码 {len(exclusions.get('by_code') or {})}")
    if not ex_notes:
        # 缺这份数据不阻断构建（老库仍可用），但必须喊出来：
        # 没有它，命中 301 的编码一律报满额加征，被整号排除的商品会被高报 25%。
        print("   ⚠ 未找到 data/sec301_exclusions.json —— 301 排除判定将整体缺失，"
              "请先跑 python scripts/extract_exclusions.py")

    from datetime import datetime

    db = {
        "meta": {
            "hts_csv": os.path.basename(HTS_CSV),
            "ustr_pdf": os.path.basename(PDF_FILE),
            "flip_frn_pdf": "FLIP 301 Investigation Final Action FRN 7-23-26 FINAL.pdf",
            "ch99_pdf": (exclusions.get("meta") or {}).get("source", ""),
            "sec301_mapping_count": len(sec301_map),
            "sec301_mapping_10_count": len(sec301_map_10),
            "rates_8_count": len(rates_8),
            "built_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        },
        "rates_8": rates_8,          # norm8 -> 基础税率/描述（path 为 path_nodes 下标列表）
        "path_nodes": path_nodes,    # 归类路径节点字符串表（供 path 下标引用）
        "desc_10": desc_10,          # norm10 -> 具体描述
        # 计量单位：估算页按数量算钱时要显示"这行按什么计数"（kg / No. / doz.）。
        # 8 位由 10 位子目继承；units_10 只存与父级不同的那些，查不到即回落 8 位。
        "units_8": units_8,
        "units_10": units_10,
        "add_duty": add_duty,        # norm(8/10) -> 附加关税
        "sec301_map": sec301_map,    # norm8 -> norm(9903.xx)
        "sec301_map_10": sec301_map_10,        # norm10 -> norm(9903.xx)，10 位精确归属
        "sec301_partial_8": sec301_partial_8,  # norm8 -> [norm10]，仅部分后缀入清单，需 10 位判定
        "sec301_pages": sec301_pages,  # norm(8或10) -> USTR PDF 物理页号（来源追溯用）
        "c99_percent": c99_percent,  # norm(9903.xx) -> 加征百分比
        "flip_301": flip_301,        # 301 flip 历史（此前档位）
        "vietnam": vietnam,          # 越南适用措施与代表编码说明
        "flip301": flip301,          # FLIP 301 强迫劳动调查关税（60 经济体税率表 + 豁免）
        "flip301_exemptions": flip301_exemptions,  # FLIP 301 ANNEX II 豁免编码清单
        "exclusions": exclusions,    # 301 排除（U.S. note 20）：notes 标目元信息 + by_code 逐编码
        # 9903 标目正文与税率（来源追溯 + 探测用）；FLIP 301 官方标目；未建模措施分组
        "c99_headings": c99_rows,
        "flip301_headings": flip_headings,
        "c99_unmodeled": c99_unmodeled,
        # 按产品触发的 Chapter 99 清单索引（232 类）：编码 → note/子条，查询时探测
        "c99_product_index": c99_product_index,
    }
    print("④ 产出下限校验 ...")
    failures = sanity_check({
        "rates_8": len(rates_8),
        "desc_10": len(desc_10),
        "sec301_map": len(sec301_map),
        "c99_percent": len(c99_percent),
        "flip301_headings": len(fh),
        "c99_headings": len(c99_rows),
    })
    if failures:
        print("   ✗ 校验未通过，已中止构建，未写入数据库（保留上一版）：")
        for f_ in failures:
            print(f"     - {f_}")
        print("   多半是源文件换版或换排版导致解析失配，请人工核对 htsdata.csv / USTR PDF。")
        sys.exit(1)
    print(f"   ✓ 通过（rates_8 {len(rates_8)} | desc_10 {len(desc_10)} | "
          f"sec301_map {len(sec301_map)} | c99_percent {len(c99_percent)}）")

    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, separators=(",", ":"))
    print(f"⑤ 数据库已写入: {OUT_JSON}")

    print("⑥ 对比上一版本，生成数据变动清单 ...")
    from datetime import datetime
    import db_diff
    built_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    result = db_diff.compare_with_fingerprint(db, built_at=built_at)
    if result["first_build"]:
        print("   首次构建，无历史版本可对比（已保存基准快照）。")
    else:
        s = result["stats"]
        # 这里必须把 stats 的全部键打出来，不能写死几个。此前只印前四项，
        # 结果一次改动了 189 个 FLIP 301 豁免编码的重建在控制台上显示"全 0"，
        # 运维看到的是"没变"。构建日志是这份数据唯一的人工核对点。
        print(f"   变动统计（共 {result.get('total_changes', 0)} 条）："
              + " | ".join(f"{k} {v}" for k, v in s.items() if v) or "   无变动")
        if result.get("new_categories"):
            print("   ⚠ 以下类别本次首次纳入监控，无历史快照可比对，本次不代表"
                  "『无变化』，下次构建起生效：")
            print("     " + "、".join(result["new_categories"]))
        for ch in result["changes"][:10]:
            print(f"     [{ch['类型']}] {ch['编码']} {ch['描述'][:30]} | {ch['旧']} → {ch['新']}")
        if result.get("truncated"):
            print(f"     ...（明细另有 {result['omitted']} 条未展示，见 data/.db_changes.json）")
        elif len(result["changes"]) > 10:
            print(f"     ...（其余 {len(result['changes']) - 10} 条见 Web 端『数据变动』）")
    db_diff.save_changes(result)
    db_diff.save_fingerprint(db)
    print("   已保存变动清单与基准快照。")
    return db


if __name__ == "__main__":
    build()
