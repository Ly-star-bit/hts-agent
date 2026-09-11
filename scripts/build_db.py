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
    stack = []          # [(indent, desc)]，维护当前所在的层级路径
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
            elif len(n) == 10:
                desc_10[n] = desc
                if add:
                    add_duty.setdefault(n, add)
                # 部分 8 位子目在官方文件中只以 10 位形式出现（如 0203.29.20.00），
                # 税率写在 10 位行上：将其继承到 8 位前缀，保证 301 判定可回查税率。
                if general and n[:8] not in rates_8:
                    rates_8[n[:8]] = entry
            if n.startswith("9903"):
                c99_rates[n] = general

    # 路径节点重复率约 89%（34814 次引用 / 3815 个不同字符串），直接内联会让
    # 数据库多出 2.8MB。改存字符串表 + 下标引用，降到 0.5MB。
    path_nodes = sorted({d for e in rates_8.values() for d in e["path"]})
    node_idx = {s: i for i, s in enumerate(path_nodes)}
    for e in rates_8.values():
        e["path"] = [node_idx[d] for d in e["path"]]
    return rates_8, desc_10, add_duty, c99_rates, path_nodes


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
    rates_8, desc_10, add_duty, c99_rates, path_nodes = parse_hts_csv()
    print(f"   8位子目: {len(rates_8)} | 10位描述: {len(desc_10)} | 附加税行: {len(add_duty)} "
          f"| 9903子目: {len(c99_rates)} | 归类路径节点: {len(path_nodes)}")

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
    # UFLPA 强迫劳动检查维度已移除（v1.5），不再摄入 uflpa_entities.json
    ex_univ = len(flip301_exemptions.get("universal", []))
    ex_econ = {k: len(v) for k, v in (flip301_exemptions.get("by_economy") or {}).items()}
    print(f"   flip 历史编码: {len(flip_301.get('flips', {}))} | "
          f"越南覆盖编码: {len(vietnam.get('covered_codes', []))} | "
          f"FLIP 301: 10%档 {len((flip301.get('rates') or {}).get('10', []))} | "
          f"12.5%档 {len((flip301.get('rates') or {}).get('125', []))} | "
          f"FLIP 301 豁免: 通用 {ex_univ} | 按经济体 {ex_econ}")
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
    }
    print("④ 产出下限校验 ...")
    failures = sanity_check({
        "rates_8": len(rates_8),
        "desc_10": len(desc_10),
        "sec301_map": len(sec301_map),
        "c99_percent": len(c99_percent),
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
