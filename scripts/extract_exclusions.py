# -*- coding: utf-8 -*-
"""
extract_exclusions.py —— 从 USITC Chapter 99 PDF 提取 Section 301 排除清单
（U.S. note 20 各子条正文），产出 data/sec301_exclusions.json。

【为什么需要这份数据】
本工具此前只能回答"这个编码在不在 301 清单"，回答不了"有没有被排除"——
因为原有三份源里都没有排除信息。USTR 那份 China Tariffs PDF 自己第 1 页就写了：
"Product exclusions granted by the USTR are found in U.S. note 20 to subchapter III
of chapter 99."，正文不在那份文件里。

后果不是"少一个提示"，是**直接报错税**。例：9025.19.80.85（温度计）在 List 2，
工具报 +25%；但它整个统计号列在 note 20(vvv)(ii) 第 (3) 项，报关时填 9903.88.69
即可免掉这 25%（有效期至 2026-11-09）。

【排除项的两种形态——这是本数据的核心区分】
  full       条目正文就是一个统计号，如 "(3) 9025.19.8085"
             → 该 10 位号下的**所有**中国产商品都排除，不看描述，可机器判定
  described  条目是产品描述 + "(described in statistical reporting number XXXX)"
             → 只有符合该描述的那一款排除，**不能**按编码自动判免，只能给出原文供人工核对
同一份清单里两种混排（vvv(ii) 前 3 项是 full，第 4 项起是 described）。

【解析策略】
按 note 20 的**子条字母**切分（(h)/(v)/(vvv)/(www)…），因为：
  - 早期子条（88.05~88.48，均已过期）的句式里不含排除标目，标目只能靠字母反查；
  - 字母 → 排除标目 的映射从 htsdata.csv 取（每个 9903.88.xx 行的品名都写着
    "as provided for in U.S. note 20(vvv)"），这是官方口径，比从 PDF 猜可靠。
子条内再按 "shall not apply to the following particular products …:" 定位每段清单，
该句同时给出**被排除的源标目**（List 1/2/3/4A，即 9903.88.01/02/03/04/15）。
条目按 (1)(2)(3)… 顺序号切，只接受**递增**的编号——正文里 "(CAS No. …)"、"(2018)"、
"(described …)" 这类括号才不会被误当成条目。

【有效期】不从 PDF 解析，从 htsdata.csv 的 9903 行品名取
（"Effective with respect to entries on or after June 15, 2024 and through
November 9, 2026"）。同一事实只留一个来源，避免两处各自演化。

用法：python scripts/extract_exclusions.py
"""
import csv
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
HTS_CSV = os.path.join(BASE_DIR, "htsdata.csv")
OUT_JSON = os.path.join(DATA_DIR, "sec301_exclusions.json")

# 页眉页脚：每页都有，拼接前必须剔除，否则会切断跨页的条目正文
PAGE_FURNITURE = re.compile(
    r"^(Harmonized Tariff Schedule of the United States.*"
    r"|Annotated for Statistical Reporting Purposes"
    r"|[IVX]{1,6}"
    r"|99 - III - \d+"
    r"|U\.S\. Notes(?: \(con\.\))?)$"
)

# 子条起始：(h) / (vvv) / (www) 等，后接固定的 USTR 排除程序措辞。
# 后期子条把 List 拆成罗马小节，头部长这样：“(vvv) (i) The U.S.Trade Representative …”，
# 不允许中间这个 (i) 会整段漏掉 —— .66~.70（含唯一还生效的两个）全在这一形态里。
# 不用行首锚（^）：全文已压平成单行（见 load_pages 的说明），
# 后面那句 USTR 措辞本身足够独特，不需要靠行首定位。
SUBDIV = re.compile(
    r"\(([a-z]{1,3})\)\s+(?:\((?:i|ii|iii|iv|v|vi|vii)\)\s+)?"
    r"The U\.S\.\s*Trade Representative determined to establish a process")

# 清单起始句。两种措辞：
#   ① 绝大多数：… the additional duties provided for in heading 9903.88.02 shall not apply
#      to the following particular products …:
#   ② note 20(www)（光伏设备）：… when such products of China are entered with a claim for
#      the tariff treatment provided in heading 9903.88.70 of this subchapter:
# 注意：不要在前面加 "has determined that" 之类的引导语来锚定——实测句式有多种变体，
# 加了会漏掉 9 段清单，而漏掉的段会被并进前一段，导致**整条链的标目错位一位**
# （9025.19.8085 会被记到 9903.88.68 名下，而它实际属于 9903.88.69）。
LIST_START = re.compile(
    r"(?:as provided in heading\s+(9903\.\d\d\.\d\d),?\s*)?"
    r"the additional dut(?:y|ies)\s+(?:provided for in|imposed by)\s+heading\s+"
    r"(9903\.\d\d\.\d\d)(?:\s+or\s+in\s+heading\s+(9903\.\d\d\.\d\d))?\s+"
    r"shall not apply to the following particular products[^:]{0,300}?:")
LIST_START_WWW = re.compile(
    r"the additional duties imposed by heading\s+(9903\.\d\d\.\d\d)[^:]{0,400}?"
    r"entered with a claim for the tariff treatment provided in heading\s+"
    r"(9903\.\d\d\.\d\d) of this subchapter:")

# 条目正文里的统计号：8 位或 10 位，带点
STAT_CODE = re.compile(r"\b(\d{4}\.\d{2}\.\d{2}(?:\.?\d{4}|\.?\d{2})?)\b")
# "整条就是一个统计号"——允许尾随标点/空白，不允许任何其他文字
BARE_CODE = re.compile(r"^(\d{4}\.\d{2}\.\d{2}(?:\.?\d{4}|\.?\d{2})?)[\s.,;]*$")

EXPIRED_MARK = re.compile(r"\[Compiler'?s note:\s*expired", re.I)

MONTHS = {m: i + 1 for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"])}


def norm(code):
    """统计号归一：只留数字。10 位保持 10 位，8 位保持 8 位。"""
    return re.sub(r"\D", "", code or "")


def _date(txt):
    m = re.match(r"([A-Z][a-z]+)\s+(\d{1,2}),?\s+(\d{4})", (txt or "").strip())
    if not m or m.group(1) not in MONTHS:
        return ""
    return f"{int(m.group(3)):04d}-{MONTHS[m.group(1)]:02d}-{int(m.group(2)):02d}"


def read_headings_from_csv():
    """
    从 htsdata.csv 读 9903.88.xx / 9903.91.xx 排除标目的三件事：
      note 字母、生效起、生效止。
    只认"each covered by an exclusion granted by the U.S. Trade Representative"
    这一句——它是排除标目的标志，加征标目（88.01/02/03…）没有这句。
    """
    out = {}
    with open(HTS_CSV, encoding="utf-8-sig", newline="") as f:
        for row in csv.reader(f):
            if not row or not row[0].startswith("9903."):
                continue
            code, desc = norm(row[0]), (row[2] if len(row) > 2 else "")
            if "covered by an exclusion granted" not in desc:
                continue
            note = re.search(r"U\.S\.\s*note\s+20\(([a-z]{1,3})\)", desc)
            frm = re.search(r"on or after\s+([A-Z][a-z]+\s+\d{1,2},?\s+\d{4})", desc)
            to = re.search(r"(?:through|and before)\s+([A-Z][a-z]+\s+\d{1,2},?\s+\d{4})", desc)
            # "and before X" 是"截至 X 前一日"，"through X" 含当日；统一存成含当日的末日
            to_date = _date(to.group(1)) if to else ""
            if to and to.group(0).startswith("and before") and to_date:
                to_date = (dt.date.fromisoformat(to_date) - dt.timedelta(days=1)).isoformat()
            out[code] = {
                "note": f"20({note.group(1)})" if note else "",
                "letter": note.group(1) if note else "",
                "effective_from": _date(frm.group(1)) if frm else "",
                "effective_to": to_date,
            }
    return out


def load_pages():
    """
    整本 PDF 逐页取文本、剔页眉页脚，**把每页压平成单行**，返回 [(页号, 文本)]。

    必须压平：PDF 里一句话随时换行，而下面几条正则含 "shall not apply to the
    following particular products" 这样的字面词组，字面空格匹配不了换行符。
    留着换行会静默漏掉 9 段清单（恰好是 List 1/2 那几段，含 9025.19.8085 所在段），
    漏掉的段并进前一段，整条链的标目还会错位一位——不是少几条，是记到别的标目名下。
    """
    import pdfplumber
    pages = []
    with pdfplumber.open(CH99_PDF) as pdf:
        for i, p in enumerate(pdf.pages):
            raw = p.extract_text() or ""
            kept = [l for l in raw.split("\n") if not PAGE_FURNITURE.match(l.strip())]
            pages.append((i + 1, re.sub(r"\s+", " ", " ".join(kept)).strip()))
    return pages


# 相邻条目的最大字符间距。实测全部清单内的真实最大间距是 612（一条超长产品描述），
# 取 4000 留 6 倍余量。没有这个约束时，最后一段清单会一路延伸到全书末尾，
# 把税则表里巧合出现的 "(15) (16) …" 接着算成条目（实测 9903.88.70 多出 20 条）。
MAX_ITEM_GAP = 4000


def parse_items(seg):
    """
    按 (1)(2)(3)… 顺序号切条目，只接受**递增且相邻**的编号。

    正文里括号很多（"(CAS No. 9005-38-3)"、"(described in …)"、年份），
    靠"下一个编号必须正好是 n+1"区分真条目；再加 MAX_ITEM_GAP 距离约束，
    序号断了或隔得太远就判定清单到头——这同时是本段的天然结束边界。
    """
    items, n, cur, start = [], 1, None, None
    for m in re.finditer(r"\((\d{1,3})\)\s", seg):
        if int(m.group(1)) != n:
            continue
        if cur is not None:
            if m.start() - start > MAX_ITEM_GAP:
                break
            items.append((cur, start, seg[start:m.start()].strip()))
        cur, start, n = n, m.end(), n + 1
    if cur is not None:
        tail = seg[start:start + MAX_ITEM_GAP]
        items.append((cur, start, tail.strip()))
    return items


def classify(body):
    """
    条目 → (covers, [编码], 描述)。

    full      正文就是一个统计号 → 整号排除，可机器判免
    described 产品描述 + "(described in statistical reporting number X)" → 需人工核对
    """
    body = re.sub(r"\s+", " ", body).strip()
    m = BARE_CODE.match(body)
    if m:
        return "full", [norm(m.group(1))], ""
    # 取正文里出现的**全部**统计号，按出现顺序去重。
    # 不能只取 "described in …number" 之后那一段：条目常写成
    # "3926.90.9910 prior to July 1, 2026; described in statistical reporting numbers
    #  3926.90.9915 or 3926.90.9920 effective July 1, 2026"——改版前的旧号在前半句，
    # 只取后半句会让按旧号来查的人查不到。CAS 号（9005-38-3）不带点分节，
    # 不会被 STAT_CODE 误收。
    codes, seen = [], set()
    for c in STAT_CODE.findall(body):
        nc = norm(c)
        if len(nc) in (8, 10) and nc not in seen:
            seen.add(nc)
            codes.append(nc)
    return "described", codes, body


def _status(info, today):
    """
    排除标目在 today 的状态。

    关键：**没有日期不等于长期有效**。9903.88.20 之类的老排除标目在 htsdata.csv 里
    根本不带 Effective 字样（它们的废止只体现在 PDF 的 [Compiler's note: expired]
    与税则的灰底），把"无日期"默认成生效中，等于把 2020 年就作废的排除算成今天能用，
    会直接算出偏低的税。所以只有**明确落在日期区间内**才算生效；无日期一律"有效期未标注"，
    永不自动判免。
    """
    frm, to = info.get("effective_from"), info.get("effective_to")
    if info.get("expired_marked") and not (to and to >= today):
        return "已过期"
    if not frm and not to:
        return "有效期未标注"
    if frm and frm > today:
        return "未生效"
    if to and to < today:
        return "已过期"
    return "生效中"


def extract():
    headings = read_headings_from_csv()
    letter2c99 = {v["letter"]: k for k, v in headings.items() if v["letter"]}
    print(f"htsdata.csv 中的排除标目 {len(headings)} 个")

    pages = load_pages()
    # 拼成一条长文本，同时记录每个字符属于哪一页（条目要能报出 PDF 页码供人工复核）
    buf, offs, pos = [], [], 0
    for pg, t in pages:
        s = t + " "
        buf.append(s)
        offs.append((pos, pos + len(s), pg))
        pos += len(s)
    text = "".join(buf)

    def page_at(i):
        for a, b, pg in offs:
            if a <= i < b:
                return pg
        return 0

    # ① 子条字母区间：只用于给"句里没写排除标目"的老清单兜底定标目
    marks = sorted((m.start(), m.group(1)) for m in SUBDIV.finditer(text))
    print(f"note 20 子条 {len(marks)} 个")

    def letter_at(i):
        cur = ""
        for start, letter in marks:
            if start <= i:
                cur = letter
            else:
                break
        return cur

    # ② 全局找清单起始句。标目优先取句内的 "as provided in heading 9903.88.69"，
    #    老清单没这半句，才回退到所在子条字母。
    anchors = []
    for m in LIST_START.finditer(text):
        anchors.append((m.start(), m.end(), m.group(1), m.group(2), m.group(3)))
    for m in LIST_START_WWW.finditer(text):
        anchors.append((m.start(), m.end(), m.group(2), m.group(1), None))
    anchors.sort()
    print(f"排除清单段 {len(anchors)} 段")

    notes, by_code, no_heading = {}, {}, []
    for k, (s0, e0, exc, src, alt) in enumerate(anchors):
        end = anchors[k + 1][0] if k + 1 < len(anchors) else len(text)
        c99 = norm(exc) if exc else letter2c99.get(letter_at(s0), "")
        if not c99:
            no_heading.append((page_at(s0), letter_at(s0)))
            continue
        seg = text[e0:end]
        info = notes.get(c99)
        if info is None:
            info = dict(headings.get(c99) or {})
            info.pop("letter", None)
            info.setdefault("note", "")
            info.setdefault("effective_from", "")
            info.setdefault("effective_to", "")
            info["pages"] = []
            info["item_count"] = 0
            info["full_count"] = 0
            info["expired_marked"] = False
            notes[c99] = info
        # 到期标记就印在清单起始句和第 (1) 项之间
        if EXPIRED_MARK.search(seg[:400]):
            info["expired_marked"] = True
        for num, off, body in parse_items(seg):
            covers, codes, desc = classify(body)
            pg = page_at(e0 + off)
            rec = {
                "c99": c99, "note": info.get("note", ""), "list_c99": norm(src),
                "item": num, "covers": covers, "desc": desc, "page": pg,
            }
            if alt:
                rec["list_c99_alt"] = norm(alt)
            for code in codes or []:
                by_code.setdefault(code, []).append(rec)
            info["item_count"] += 1
            if covers == "full":
                info["full_count"] += 1
            if pg and pg not in info["pages"]:
                info["pages"].append(pg)
    if no_heading:
        print(f"⚠ {len(no_heading)} 段清单定不到排除标目（子条字母不在 htsdata.csv 里，"
              f"多为已从税则删除的旧标目）：{no_heading[:6]}")
    return notes, by_code


def main():
    if not os.path.exists(CH99_PDF):
        sys.exit(f"找不到 {CH99_PDF}；先运行 python scripts/check_sources.py --apply --only ch99_pdf")
    print("解析 Chapter 99 U.S. note 20 排除清单（约 1-3 分钟）...")
    notes, by_code = extract()

    today = dt.date.today().isoformat()
    for c, v in notes.items():
        v["status"] = _status(v, today)
    live = sorted(c for c, v in notes.items() if v["status"] == "生效中")
    full_live = sorted({c for c, recs in by_code.items()
                        if any(r["covers"] == "full" and r["c99"] in live for r in recs)})

    data = {
        "meta": {
            "source": os.path.basename(CH99_PDF),
            "source_note": "USITC HTS Chapter 99，subchapter III U.S. note 20 各子条正文",
            "dates_from": "htsdata.csv（9903 排除标目品名里的生效起止）",
            "extracted_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "status_as_of": today,
            "covers_note": (
                "covers=full：条目正文就是一个统计号 → 该号下所有中国产商品排除，可机器判定；"
                "covers=described：排除按产品描述授予 → 只能给出原文供人工核对，不可按编码自动判免。"),
            "status_note": (
                "status 按 status_as_of 当日计算，查询时会用当天重算——"
                "生效中 / 已过期 / 未生效 / 有效期未标注。"
                "『有效期未标注』是老标目在 htsdata.csv 里不带 Effective 字样所致，"
                "不等于长期有效，一律不自动判免。"),
        },
        "notes": notes,
        "by_code": by_code,
    }
    tmp = OUT_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, OUT_JSON)

    print(f"\n排除标目 {len(notes)} 个（{today} 生效中 {len(live)} 个）")
    for c in sorted(notes):
        v = notes[c]
        fmt_c = f"{c[:4]}.{c[4:6]}.{c[6:]}"
        print(f"  {fmt_c} {v.get('note',''):<8} {v['item_count']:>4} 项"
              f"（整号 {v['full_count']}）  "
              f"{v.get('effective_from') or '—'} → {v.get('effective_to') or '—'}  {v['status']}")
    print(f"\n涉及编码 {len(by_code)} 个；被**生效中的整号排除**覆盖的 {len(full_live)} 个")
    print(f"已写入 {OUT_JSON}")

    for probe in ("9025198085", "9025198010"):
        recs = by_code.get(probe) or []
        print(f"自检 {probe}: {[(r['c99'], r['covers'], r['page']) for r in recs]}")


if __name__ == "__main__":
    main()
