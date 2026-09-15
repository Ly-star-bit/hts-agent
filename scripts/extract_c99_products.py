# -*- coding: utf-8 -*-
"""
extract_c99_products.py —— 从 USITC Chapter 99 PDF 提取 subchapter III 各 U.S. note
按 HTS 子目列举的**适用范围**，产出 data/c99_product_scopes.json（+ note 52 / note 2
原文摘录 data/flip301_note52.json）。

【为什么需要这份数据】
htsdata.csv 里有 636 个 9903 标目，本工具此前只建模了 301（9903.88/.91/.92）与
FLIP 301（手抄 JSON）。钢铝铜、汽车及零件、中重型车、木材、半导体这些按**产品**
触发的措施（9903.82 / .94 / .74 / .76 / .79），标目正文只写 "as provided for in
U.S. note 16"，产品清单在 note 正文里——不提出来，工具既算不出这笔税，也判不了
FLIP 301 对 232 产品的豁免（9903.05.90）。

【正文措辞——先说清楚"232"在哪】
这些 note 的正文**不出现** "section 232" / "Trade Expansion Act" 字样（全书 grep
为 0 是真的，不是提取问题）。它们只写"provide the ordinary customs duty treatment of
certain articles of aluminum, of steel, or of copper"，并引用总统公告
（Proclamation 10925 / 10984、"Adjusting Imports of Aluminum" 的 10522）与 BIS 的
232-aluminum 链接。因此本文件里每条 note 的 measure 字段是**按正文措辞的判断**，
并附 basis 原文，运行时校验该原文确实在 note 里（不在就标 basis_verified=false）。

【解析策略】
  1. 定位 subchapter III：从 "SUBCHAPTER III" 页到税率表开始页（"Heading/ Stat." 表头）。
  2. note 起点：行首 "N." 或 "N. 正文"，且编号必须**递增**——note 20 的排除清单里
     有 "2. Machines…" 这类顺序号条目，不按递增约束会把它当成 note 2。
  3. 子条：按 (a)/(b)…、(i)/(ii)…、(1)/(2)…、(A)/(B)… 维护一个标记栈。(i)/(v)/(x)
     既可能是字母子条也可能是罗马小节：若它正好是上一级字母的下一个，就是字母，
     否则按罗马处理（note 16 里 (h) 之后的 (i) 是字母，(c) 之后的 (i) 是罗马）。
  4. 编码：整行只有编码的"清单行"逐个收；散在正文里的（"described in subheading
     7614.10.50"）标 context=inline 并带上原文片段，由集成方判断是范围还是例外。
     4 位 token 只在清单行接受——正文里的 4 位数多半是年份/公告号。
     区间（"8701 to 8705"）保留起止，不展开。
  5. 产出下限校验：每条 note 有最少条数，低于即报错退出，不写文件。
     提取数为 0 的 note 在报告里点名。

用法：
    python scripts/extract_c99_products.py            # 提取并写入 data/
    python scripts/extract_c99_products.py --report   # 只打印报告，不写文件
"""
import datetime as dt
import json
import os
import re
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
CH99_PDF = os.path.join(BASE_DIR, "Chapter 99_2026HTSRev18.pdf")
OUT_JSON = os.path.join(DATA_DIR, "c99_product_scopes.json")
OUT_NOTE52 = os.path.join(DATA_DIR, "flip301_note52.json")

# 页眉页脚（与 extract_exclusions.PAGE_FURNITURE 同源，多认了 "99 - III - 435" 这种带空格写法）
PAGE_FURNITURE = re.compile(
    r"^(Harmonized Tariff Schedule of the United States.*"
    r"|Annotated for Statistical Reporting Purposes"
    r"|[IVX]{1,6}"
    r"|99\s*-\s*III\s*-\s*\d+"
    r"|U\.S\. Notes(?: \(con\.\))?)$"
)

# 目标 note：措施判断 + 判断依据原文（运行时校验）+ 产出下限。
# 下限按 2026HTSRev18 实测值留约 40% 余量；源文件换版导致解析失配时宁可失败也不写库。
TARGET_NOTES = {
    "2": {
        "measure": "按原产地触发：墨西哥/加拿大/中国与香港（9903.01.01–.24）、对等关税与转运"
                   "（9903.01.25+、9903.02）、曾经的全球 10%（9903.03.01–.11）。正文不写法律依据名称，"
                   "只以标目与 Federal Register 引用表述。",
        "basis": "For the purposes of heading 9903.01.01, products of Mexico",
        "trigger": "origin",
        "min_codes": 4000,
    },
    "16": {
        "measure": "按产品触发：铝、钢、铜制品及其衍生品（9903.82.02–9903.82.26）。业内称 Section 232 "
                   "金属关税，但 note 正文不出现 section 232 字样，仅写 ordinary customs duty treatment "
                   "of certain articles of aluminum, of steel, or of copper。",
        "basis": "provide the ordinary customs duty treatment of certain articles of aluminum, of steel, or of copper",
        "trigger": "product",
        "min_codes": 450,
    },
    "19": {
        "measure": "按产品触发：铝制品（9903.85.xx）。编译者注明 9903.85.01–.15、.21–.66、.69–.72 已于 "
                   "2026-04-06 终止，仅余 9903.85.67/.68（俄罗斯铝 200%）。正文引用 Proclamation 10522 "
                   "（Adjusting Imports of Aluminum）与 bis.doc.gov/232-aluminum——全书唯一出现 232 的地方。",
        "basis": "ordinary customs duty treatment applicable to all entries of the aluminum products",
        "trigger": "product",
        "min_codes": 300,
    },
    "33": {
        "measure": "按产品触发：乘用车与轻型卡车及其零部件（9903.94.xx）。引用 Proclamation 10925"
                   "（进口调整抵扣）与 EO 14345（美日协定）。",
        "basis": "ordinary customs duty treatment applicable to all entries of passenger vehicles",
        "trigger": "product",
        "min_codes": 250,
    },
    "37": {
        "measure": "按产品触发：软木原木与锯材（9903.76.01）、软垫木家具（9903.76.02）、厨柜与浴室柜"
                   "（9903.76.03），另有英/日/欧/韩/台专属标目 9903.76.20–.24。",
        "basis": "ordinary customs duty treatment of softwood timber and lumber products",
        "trigger": "product",
        "min_codes": 13,
    },
    "38": {
        "measure": "按产品触发：中重型车辆、客车及其零部件（9903.74.xx）。引用 Proclamation 10984。",
        "basis": "ordinary customs duty treatment of medium- and heavy- duty vehicles",
        "trigger": "product",
        "min_codes": 130,
    },
    "39": {
        "measure": "按产品触发：半导体制品（9903.79.xx）。**编码只框定 8471.50/8471.80/8473.30，"
                   "是否适用还要满足 TPP 与 DRAM 带宽等技术参数**，编码本身判不出。",
        "basis": "ordinary customs duty treatment of semiconductor articles",
        "trigger": "product",
        "min_codes": 3,
    },
    "50": {
        "measure": "按原产地触发：巴西（9903.05.01–.09），(a)(ii)–(a)(vi) 为例外产品清单。",
        "basis": "heading 9903.05.01 imposes an additional ad valorem rate of duty on imports of all products of Brazil",
        "trigger": "origin",
        "min_codes": 1200,
    },
    "51": {
        "measure": "按原产地 + 产品触发：加拿大特定产品（9903.03.12–.16），(b) 列举产品清单。",
        "basis": "impose an additional ad valorem rate of duty on imports of products of Canada",
        "trigger": "origin+product",
        "min_codes": 600,
    },
    "52": {
        "measure": "按原产地触发：FLIP 301 强迫劳动关税，60 经济体（9903.05.20–9903.05.84），"
                   "(b)–(k) 为例外：通用产品清单、232 产品、民用航空器、医药、加/墨 USMCA、"
                   "CAFTA 纺织品、英/欧/瑞及各经济体专属清单、net-of-MFN 计算规则。",
        "basis": "headings 9903.05.20–9903.05.84 impose additional ad valorem rates of duty",
        "trigger": "origin",
        "min_codes": 5000,
    },
}

# 状态词：这些句子决定一条措施是不是还在执行，必须原样带出，不做法律判断
STATUS_WORDS = re.compile(
    r"\b(terminated|terminate|expired|expire|suspended|suspend|superseded|revoked)\b", re.I)

# 编码 token：4 / 6 / 8 / 10 位（10 位可写成 7616.99.5160 或 9401.61.40.11）
CODE_TOKEN = re.compile(
    r"(?<![\d.])(\d{4})(?:\.(\d{2})(?:\.(\d{2})(?:\.?(\d{2}))?)?)?(?!\d|\.\d)")
# 区间："8701 to 8705"、"7206.10 through 7216.50"、"9903.82.04–9903.82.26"
RANGE = re.compile(
    r"(?<![\d.])(\d{4}(?:\.\d{2}(?:\.\d{2}(?:\.?\d{2})?)?)?)\s*(?:through|to|–|—|-)\s*"
    r"(\d{4}(?:\.\d{2}(?:\.\d{2}(?:\.?\d{2})?)?)?)(?!\d|\.\d)")
# 清单行里允许出现的非编码 token
LIST_FILLER = {"and", "or", "through", "to", "–", "—", "-", ","}
# 疑似排版错误的编码（如正文里的 "903.94.67"）：只报告不采用
SUSPICIOUS = re.compile(r"(?<![\d.])\d{3}\.\d{2}\.\d{2}(?![\d.])")

ROMAN = re.compile(r"^(?=[ivx])m{0,3}(?:x{0,3})(?:ix|iv|v?i{0,3})$")
# 罗马小节可到 (xxviii)（note 2 有 57 个国家条目），所以小写允许到 6 位；
# 但 "(see)" 这类词也长这样，真假由 _valid_marker 再筛一遍
MARKER = re.compile(r"^\(([a-z]{1,6}|[A-Z]{1,2}|\d{1,3})\)\s*(.*)$")
LETTER_LABEL = re.compile(r"^([a-z])\1?$")     # a…z、aa…zz


def _valid_marker(tok):
    return bool(tok.isdigit() or tok.isupper() or ROMAN.match(tok) or LETTER_LABEL.match(tok))


def norm(code):
    return re.sub(r"\D", "", code or "")


# ---------- 页面 ----------

def load_pages(pdf_path=CH99_PDF):
    """整本 PDF 逐页取文本、剔页眉页脚，返回 [(页号, [行])]。不压平：子条标记靠行首定位。"""
    import pdfplumber
    pages = []
    with pdfplumber.open(pdf_path) as pdf:
        for i, p in enumerate(pdf.pages):
            raw = p.extract_text() or ""
            lines = [ln.strip() for ln in raw.split("\n")]
            lines = [ln for ln in lines if ln and not PAGE_FURNITURE.match(ln)]
            pages.append((i + 1, lines))
    return pages


def subchapter_iii_span(pages):
    """
    subchapter III 的 U.S. Notes 所在页区间 [起, 止)。
    起：出现 "SUBCHAPTER III" 行的页；止：其后第一个税率表表头（"Heading/ Stat."）的页。
    找不到就抛错——静默取全书会把别的 subchapter 的 note 混进来。
    """
    start = end = None
    for pg, lines in pages:
        if start is None and any(ln == "SUBCHAPTER III" for ln in lines):
            start = pg
            continue
        if start is not None and any(re.match(r"^Heading/\s+Stat\.", ln) for ln in lines):
            end = pg
            break
    if start is None or end is None:
        raise ValueError(f"未定位到 subchapter III 的 note 区间（起 {start}，止 {end}）")
    return start, end


def find_note_starts(pages, start, end):
    """
    note 起点：行首 "N." 或 "N. 正文"，编号递增（允许跳号：22–28 是 [Note deleted.]，12/29/34 缺号）。
    返回 {N: (页号, 行下标)}。
    """
    starts, last = {}, 0
    for pg, lines in pages:
        if pg < start or pg >= end:
            continue
        for i, ln in enumerate(lines):
            m = re.match(r"^(\d{1,2})\.(?:\s+.*)?$", ln)
            if not m:
                continue
            n = int(m.group(1))
            if last < n <= last + 12:
                starts[n] = (pg, i)
                last = n
    return starts


def slice_notes(pages, starts, end):
    """按起点切出每条 note 的 [(页号, 行)]，note 的正文到下一条 note 的起点为止。"""
    by_page = {pg: lines for pg, lines in pages}
    order = sorted(starts)
    out = {}
    for k, n in enumerate(order):
        pg0, i0 = starts[n]
        pg1, i1 = starts[order[k + 1]] if k + 1 < len(order) else (end, 0)
        body = []
        for pg in range(pg0, pg1 + 1):
            lines = by_page.get(pg, [])
            lo = i0 if pg == pg0 else 0
            hi = i1 if pg == pg1 else len(lines)
            body.extend((pg, ln) for ln in lines[lo:hi])
        # 去掉起点行里的 "N." 前缀，保留同一行的正文
        if body:
            pg, ln = body[0]
            body[0] = (pg, re.sub(r"^\d{1,2}\.\s*", "", ln))
        out[n] = body
    return out


# ---------- 子条标记栈 ----------

def _next_letter(s):
    """a→b … z→aa，aa→bb（HTS 子条写法：z 之后是 aa/bb/cc）"""
    if len(s) == 1:
        return chr(ord(s) + 1) if s != "z" else "aa"
    return chr(ord(s[0]) + 1) * len(s)


class SubdivisionTracker:
    """
    维护当前所在的子条路径，如 ["c", "iii"] → "(c)(iii)"。

    四类标记：小写字母 / 罗马数字 / 阿拉伯数字 / 大写字母。同类标记在栈里只占一层：
    再次出现就替换该层并弹掉更深的层。字母有可能出现在更深层（note 2 的 (v)(iii)(a)），
    因此字母不固定在第 1 层：它是"上一级字母的下一个"才回到那一层，否则视为新开一层。
    (i)/(v)/(x) 的歧义按同一规则解决。
    """

    def __init__(self):
        self.path = []      # [(kind, token)]

    def feed(self, line):
        m = MARKER.match(line)
        if not m:
            return
        tok = m.group(1)
        if not _valid_marker(tok):
            return                      # "(see)" 之类的括号词，不是子条标记
        # 一行可能连着多个标记："(m) (A) Effective…" / "(j) (1) As provided…"
        rest = m.group(2)
        self._apply(tok)
        m2 = MARKER.match(rest)
        if m2 and _valid_marker(m2.group(1)):
            self._apply(m2.group(1))

    def _apply(self, tok):
        if tok.isdigit():
            self._replace_or_push("num", tok)
        elif tok.isupper():
            self._replace_or_push("upper", tok)
        else:
            # 字母 vs 罗马：先问是不是某一层字母的"下一个"
            for depth, (kind, t) in enumerate(self.path):
                if kind == "alpha" and _next_letter(t) == tok:
                    self.path = self.path[:depth] + [("alpha", tok)]
                    return
            if ROMAN.match(tok):
                self._replace_or_push("roman", tok)
            elif tok in ("a", "aa"):
                # 新开一层字母（(v)(iii)(a) 这种），或 note 开头第一个 (a)
                self.path.append(("alpha", tok))
            else:
                # 乱序字母（页面拼接偶发）：当作第 1 层
                self.path = [("alpha", tok)]

    def _replace_or_push(self, kind, tok):
        for depth, (k, _) in enumerate(self.path):
            if k == kind:
                self.path = self.path[:depth] + [(kind, tok)]
                return
        self.path.append((kind, tok))

    def key(self):
        return "".join(f"({t})" for _, t in self.path)

    def top(self):
        """第 1 层子条，如 "(c)"；没有则空串"""
        return f"({self.path[0][1]})" if self.path else ""


# ---------- 编码解析 ----------

def _token_code(m):
    """CODE_TOKEN 匹配 → (纯数字, 位数)"""
    parts = [g for g in m.groups() if g]
    code = "".join(parts)
    return code, len(code)


def is_code_only_line(line):
    """整行只有编码（及 and/through 等填充词）"""
    toks = [t.strip(",;.") for t in line.split()]
    toks = [t for t in toks if t]
    if not toks:
        return False
    for t in toks:
        if t.lower() in LIST_FILLER:
            continue
        if not CODE_TOKEN.fullmatch(t):
            return False
    return any(CODE_TOKEN.fullmatch(t) for t in toks)


def parse_line_codes(line, list_line):
    """
    一行 → (产品编码列表, 9903 标目列表, 9903 标目区间列表)。
    产品编码每项 {code, digits, kind(exact|prefix|range), to}。
    list_line=False（正文行）时不收 4 位 token。
    """
    codes, headings, hranges = [], [], []
    consumed = []

    for m in RANGE.finditer(line):
        a, b = m.group(1), m.group(2)
        na, nb = norm(a), norm(b)
        if len(na) != len(nb) or nb <= na:
            continue                      # "2204.21.20 to 2204.21.30" 之外的假区间（年份、页码）
        if na.startswith("9903"):
            hranges.append(f"{a}–{b}")
        elif len(na) >= 6 or list_line:
            codes.append({"code": na, "digits": len(na), "kind": "range", "to": nb})
        consumed.append((m.start(), m.end()))

    def taken(pos):
        return any(s <= pos < e for s, e in consumed)

    for m in CODE_TOKEN.finditer(line):
        if taken(m.start()):
            continue
        code, n = _token_code(m)
        if code.startswith("9903"):
            if n == 8:
                headings.append(f"{code[:4]}.{code[4:6]}.{code[6:8]}")
            continue
        if code.startswith("98"):
            continue                      # 98 章条款（9802.00.60 等）不是产品范围
        if n == 4 and not list_line:
            continue                      # 正文里的 4 位数：年份/公告号/法条号
        codes.append({"code": code, "digits": n,
                      "kind": "prefix" if n in (4, 6) else "exact", "to": ""})
    return codes, headings, hranges


# ---------- 单条 note ----------

def status_sentences(text, limit=12):
    """含状态词的句子，去重、限量。编译者注的方括号里的也算。"""
    out, seen = [], set()
    for sent in re.split(r"(?<=[.\]])\s+(?=[A-Z(\[])", text):
        if STATUS_WORDS.search(sent):
            s = re.sub(r"\s+", " ", sent).strip()[:400]
            if s not in seen:
                seen.add(s)
                out.append(s)
        if len(out) >= limit:
            break
    return out


def extract_note(n, body, spec):
    """
    一条 note 的 [(页号, 行)] → 结构化结果。
    codes 每项：{code, digits, kind, to, subdivision, page, context, snippet?}
    """
    tracker = SubdivisionTracker()
    codes, headings, hranges = [], set(), set()
    labels = {}          # 子条路径 → 标题行
    unparsed = []        # 含编码样但没按清单行采用的行（供人工核对）
    suspicious = []
    for pg, line in body:
        tracker.feed(line)
        key = tracker.key()
        mk = MARKER.match(line)
        if mk and _valid_marker(mk.group(1)) and key not in labels:
            labels[key] = {"page": pg, "text": line[:160]}
        if SUSPICIOUS.search(line):
            suspicious.append({"page": pg, "text": line[:160]})
        list_line = is_code_only_line(line)
        cs, hs, hr = parse_line_codes(line, list_line)
        headings.update(hs)
        hranges.update(hr)
        for c in cs:
            c.update({"subdivision": key, "page": pg,
                      "context": "list" if list_line else "inline"})
            if not list_line:
                c["snippet"] = line[:160]
            codes.append(c)
        # 只报"像产品编码却没采用"的行；98/99 章引用是刻意跳过的，不算未采用
        if not list_line and not cs and re.search(r"(?<![\d.])(?!98|99)\d{4}\.\d{2}", line):
            unparsed.append({"page": pg, "text": line[:160]})

    text = " ".join(ln for _, ln in body)
    pages = sorted({pg for pg, _ in body})
    # 按子条分组存：清单编码是纯字符串（21k 条逐条存 dict 会把文件撑到 3.5MB），
    # 区间存 [起, 止]，正文散见的单独放 inline 并带原文片段
    groups, inline = {}, []
    for c in codes:
        if c["context"] == "inline":
            inline.append({"code": c["code"], "kind": c["kind"], "to": c["to"],
                           "subdivision": c["subdivision"], "page": c["page"],
                           "snippet": c["snippet"]})
            continue
        g = groups.setdefault(c["subdivision"], {
            "label": (labels.get(c["subdivision"]) or {}).get("text", ""),
            "pages": [], "list": [], "ranges": []})
        if c["page"] not in g["pages"]:
            g["pages"].append(c["page"])
        if c["kind"] == "range":
            g["ranges"].append([c["code"], c["to"]])
        else:
            g["list"].append(c["code"])
    by_sub = {k: len(g["list"]) + len(g["ranges"]) for k, g in groups.items()}
    for k in {c["subdivision"] for c in inline}:
        by_sub[k] = by_sub.get(k, 0) + sum(1 for c in inline if c["subdivision"] == k)
    basis = spec.get("basis", "")
    return {
        "title": re.sub(r"\s+", " ", text[:400]),
        "measure": spec.get("measure", ""),
        "basis": basis,
        "basis_verified": bool(basis) and basis in text,
        "trigger": spec.get("trigger", ""),
        "pages": [pages[0], pages[-1]] if pages else [],
        "status_sentences": status_sentences(text),
        "headings": sorted(headings),
        "heading_ranges": sorted(hranges),
        "subdivisions": labels,
        "codes_by_subdivision": by_sub,
        "groups": groups,
        "inline": inline,
        "count": len(codes),
        "count_list": sum(1 for c in codes if c["context"] == "list"),
        "count_inline": sum(1 for c in codes if c["context"] == "inline"),
        "unparsed_samples": unparsed[:20],
        "suspicious": suspicious[:20],
    }


# ---------- note 52 / note 2 原文 ----------

def split_note_by_subdivision(body):
    """
    按子条路径切原文：{路径: {"pages": [...], "text": 非清单行原文, "codes_count": N}}。
    清单行不进 text（(b)/(c) 各有上千行编码，弹窗要的是条件原文，编码在 scopes 文件里）。
    """
    tracker = SubdivisionTracker()
    out = {}
    for pg, line in body:
        tracker.feed(line)
        key = tracker.key() or "(head)"
        d = out.setdefault(key, {"pages": [], "text_lines": [], "codes_count": 0})
        if pg not in d["pages"]:
            d["pages"].append(pg)
        if is_code_only_line(line):
            d["codes_count"] += len(parse_line_codes(line, True)[0])
        else:
            d["text_lines"].append(line)
    for key, d in out.items():
        d["text"] = re.sub(r"\s+", " ", " ".join(d.pop("text_lines"))).strip()
    return out


def note2_excerpt(body):
    """note 2 开头两段原文 + 全部状态句，不做法律判断。"""
    text = " ".join(ln for _, ln in body)
    # 开头两段：(a) 与 (b) 子条
    m = re.search(r"\(c\)\s+For the purposes", text)
    opening = text[:m.start()] if m else text[:1500]
    return {
        "pages": [body[0][0], body[-1][0]] if body else [],
        "opening": re.sub(r"\s+", " ", opening).strip()[:2500],
        "status_sentences": status_sentences(text, limit=20),
    }


# ---------- 主流程 ----------

def extract(pdf_path=CH99_PDF, notes=None):
    pages = load_pages(pdf_path)
    start, end = subchapter_iii_span(pages)
    starts = find_note_starts(pages, start, end)
    sliced = slice_notes(pages, starts, end)
    wanted = list(notes or TARGET_NOTES)
    result = {"meta": {
        "source": os.path.basename(pdf_path),
        "extracted_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "subchapter_iii_pages": [start, end - 1],
        "notes_found": sorted(starts),
        "notes_missing": [int(n) for n in wanted if int(n) not in sliced],
        "note": "groups[子条].list 为清单行里的编码（纯数字：4/6 位为前缀、8/10 位为精确），"
                "groups[子条].ranges 为 [起, 止]（不展开）；inline 为正文散见的编码（带 snippet，"
                "可能是例外而非范围，由集成方判断）。"
                "measure 为按正文措辞的判断，basis 为依据原文（basis_verified 表示原文确实存在）。"
                "正文不出现 section 232 字样。",
    }, "notes": {}}
    for n in wanted:
        body = sliced.get(int(n))
        if not body:
            continue
        result["notes"][str(n)] = extract_note(int(n), body, TARGET_NOTES.get(str(n), {}))
    note52 = {"meta": {"source": os.path.basename(pdf_path),
                       "extracted_at": result["meta"]["extracted_at"]}}
    if 52 in sliced:
        note52["subdivisions"] = split_note_by_subdivision(sliced[52])
    if 2 in sliced:
        note52["note2_excerpt"] = note2_excerpt(sliced[2])
    return result, note52


def sanity_check(result):
    """产出下限：低于下限或提取为 0 的 note 一律列出，由调用方决定是否中止。"""
    problems = []
    for n, spec in TARGET_NOTES.items():
        got = (result["notes"].get(n) or {}).get("count")
        if got is None:
            problems.append(f"note {n}: 未定位到")
        elif got < spec["min_codes"]:
            problems.append(f"note {n}: 提取 {got} 条，低于下限 {spec['min_codes']}")
        if n in result["notes"] and not result["notes"][n]["basis_verified"]:
            problems.append(f"note {n}: 判断依据原文未在正文中找到（措辞可能已变）")
    return problems


def format_report(result):
    lines = [f"Chapter 99 subchapter III U.S. notes 产品范围提取报告（{result['meta']['source']}）",
             f"  note 区间页 {result['meta']['subchapter_iii_pages']}，定位到 {len(result['meta']['notes_found'])} 条 note"
             + (f"，缺 {result['meta']['notes_missing']}" if result['meta']['notes_missing'] else "")]
    for n, d in result["notes"].items():
        lines.append(f"\n[note {n}] 页 {d['pages'][0]}–{d['pages'][-1]}  提取 {d['count']} 条"
                     f"（清单 {d['count_list']} / 正文散见 {d['count_inline']}）"
                     f"  9903 标目 {len(d['headings'])} 个" + ("  ⚠ 提取为 0" if not d["count"] else ""))
        lines.append(f"  措施：{d['measure']}")
        lines.append(f"  依据原文{'（已核）' if d['basis_verified'] else '（⚠ 未在正文找到）'}：{d['basis']}")
        lines.append(f"  摘录：{d['title'][:300]}")
        for s in d["status_sentences"][:6]:
            lines.append(f"  状态：{s[:220]}")
        subs = sorted(d["codes_by_subdivision"].items(), key=lambda kv: -kv[1])[:8]
        if subs:
            lines.append("  子条条数：" + "，".join(f"{k or '(顶层)'} {v}" for k, v in subs))
        for u in d["unparsed_samples"][:5]:
            lines.append(f"  未采用行：p{u['page']} {u['text'][:120]}")
        for u in d["suspicious"][:3]:
            lines.append(f"  疑似排版错误：p{u['page']} {u['text'][:120]}")
    return "\n".join(lines)


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="提取 Chapter 99 U.S. notes 的产品范围")
    ap.add_argument("--pdf", default=CH99_PDF)
    ap.add_argument("--report", action="store_true", help="只打印报告，不写文件")
    ap.add_argument("--out", default=OUT_JSON)
    ap.add_argument("--out-note52", default=OUT_NOTE52)
    a = ap.parse_args(argv)
    if not os.path.exists(a.pdf):
        print(f"缺少 {a.pdf}；可用 python scripts/check_sources.py --apply --only ch99_pdf 下载")
        return 1
    result, note52 = extract(a.pdf)
    print(format_report(result))
    problems = sanity_check(result)
    if problems:
        print("\n✗ 产出下限校验未通过，未写入文件：")
        for p in problems:
            print("   -", p)
        return 1
    print("\n✓ 产出下限校验通过")
    if a.report:
        return 0
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    with open(a.out_note52, "w", encoding="utf-8") as f:
        json.dump(note52, f, ensure_ascii=False, indent=1)
    print(f"已写入 {a.out}（{os.path.getsize(a.out)//1024} KB）与 {a.out_note52}"
          f"（{os.path.getsize(a.out_note52)//1024} KB）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
