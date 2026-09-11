# -*- coding: utf-8 -*-
"""
extract_flip_scopes.py —— 从 FLIP 301 FRN PDF 提取 ANNEX II 各豁免编码的
Scope Limitations（范围限制，如 Aircraft/Pharma）与物理页码，回写 data/flip301_exemptions.json。

背景：FLIP 301 Final Action FRN（2026-07-23 发布）ANNEX II 豁免清单中，
部分子目带 Scope Limitations（部分覆盖）。原转录（flip301_exemptions.json）只保留了
编码列表（scoped_count=0），未保留范围限制列。本脚本补充提取，并记录每个编码所在物理页，
供 Web 端"来源追溯"点击查看原文定位使用。

输出新增键（保留原有 universal / by_economy / meta 不变）：
  - universal_scopes:      {norm8: "Aircraft"|"Pharma"|"Ex"|""}  Part A 通用豁免的范围限制
  - universal_pages:       {norm8: 物理页号}                  Part A 编码所在页（PDF 页号，1 起）
  - universal_ex_desc:     {norm8: Description 原文}         仅 Ex 档（该栏正文即范围本身）
  - by_economy_scopes:     {经济体: {norm8: scope}}          Parts B-O 范围限制
  - by_economy_pages:      {经济体: {norm8: 页号}}           Parts B-O 编码所在页
  - by_economy_ex_desc:    {经济体: {norm8: Description}}    仅 Ex 档
  - meta 追加 scope_extraction 说明，并更新 scoped_count

用法：python scripts/extract_flip_scopes.py
"""
import json
import os
import re
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
FRN_PDF = os.path.join(BASE_DIR, "FLIP 301 Investigation Final Action FRN 7-23-26 FINAL.pdf")
EXEMPTIONS_JSON = os.path.join(DATA_DIR, "flip301_exemptions.json")

# 编码行首列匹配：8 位或 10 位 HTSUS 编码
HTS_PAT = re.compile(r"^\d{4}\.\d{2}\.\d{2}(\.\d{2})?$")
PART_PAT = re.compile(r"^Part ([A-Z])\.\s")


def norm(code: str) -> str:
    """规范化 HTS 编码：仅保留数字，统一取前 8 位子目"""
    return re.sub(r"\D", "", code or "")[:8]


def extract():
    import pdfplumber

    universal_scopes = {}
    universal_pages = {}
    universal_ex_desc = {}
    by_economy_scopes = {}
    by_economy_pages = {}
    by_economy_ex_desc = {}
    part_aliases = {  # FRN Part → 现有 by_economy 键
        "B": "GB", "C": "EU", "D": "CH", "E": "MY", "F": "KH", "G": "GT",
        "H": "SV", "I": "AR", "J": "BD", "K": "TW", "L": "ID", "M": "EC",
        "N": "JO", "O": "CAFTA_DR",
    }

    with pdfplumber.open(FRN_PDF) as pdf:
        part = None  # 当前 ANNEX II Part（跨页状态机：标题行更新，其余页继承）
        for idx, page in enumerate(pdf.pages):
            page_no = idx + 1  # 物理页号（1 起，与浏览器 #page=N 一致）
            tables = page.extract_tables()
            if not tables:
                continue
            for tb in tables:
                for row in tb:
                    if not row or not row[0]:
                        continue
                    first = (row[0] or "").strip()
                    pm = PART_PAT.match(first)
                    if pm:
                        part = pm.group(1)
                        continue
                    if first == "HTSUS" or first.startswith("HTSUS"):
                        continue
                    hm = HTS_PAT.match(first)
                    if not hm:
                        continue
                    code = norm(first)
                    scope = (row[2] or "").strip() if len(row) > 2 else ""
                    # Aircraft / Pharma 的范围由 FRN 页 137 的定义给出，Description 栏
                    # 只是"informational only"；但 "Ex" 档 FRN 明写"defined and limited
                    # by the product description"——范围本身就在这一栏里。只存编码
                    # 等于把这一档的判定依据丢了，所以 Ex 行连描述一起留下。
                    desc = re.sub(r"\s+", " ", (row[1] or "").strip()) if len(row) > 1 else ""
                    if part == "A":
                        universal_scopes[code] = scope
                        universal_pages[code] = page_no
                        if scope == "Ex" and desc:
                            universal_ex_desc[code] = desc
                    elif part in part_aliases:
                        key = part_aliases[part]
                        by_economy_scopes.setdefault(key, {})[code] = scope
                        by_economy_pages.setdefault(key, {})[code] = page_no
                        if scope == "Ex" and desc:
                            by_economy_ex_desc.setdefault(key, {})[code] = desc
                    # Part A 之外的未识别部分跳过

    return (universal_scopes, universal_pages, universal_ex_desc,
            by_economy_scopes, by_economy_pages, by_economy_ex_desc)


def _report_delta(old_u, old_e, new_u, new_e):
    """
    重建前后的差异必须打出来。

    这是影响税额的数据：多一个编码 = 少收一笔 FLIP 301，少一个 = 多收。
    静默覆盖等于把税率变更藏进一次例行重跑里。
    """
    du = set(new_u) - set(old_u), set(old_u) - set(new_u)
    print(f"  Part A 通用豁免：{len(old_u)} → {len(new_u)}"
          f"（新增 {len(du[0])}，移除 {len(du[1])}）")
    add_t = rm_t = 0
    for eco in sorted(set(old_e) | set(new_e)):
        a, b = set(old_e.get(eco) or []), set(new_e.get(eco) or [])
        add, rm = b - a, a - b
        add_t += len(add)
        rm_t += len(rm)
        if add or rm:
            print(f"  {eco:9} {len(a):5} → {len(b):5}  新增 {len(add):3} 移除 {len(rm):3}"
                  + (f"  移除例 {sorted(rm)[:3]}" if rm else ""))
    print(f"  Parts B-O 合计：新增豁免 {add_t} 个（这些此前被多收 FLIP 301），"
          f"移除豁免 {rm_t} 个（这些此前被少收）")


def merge_and_save(us, up, ud, es, ep, ed):
    with open(EXEMPTIONS_JSON, encoding="utf-8-sig") as f:
        data = json.load(f)

    data["universal_scopes"] = us
    data["universal_pages"] = up
    data["universal_ex_desc"] = ud
    data["by_economy_scopes"] = es
    data["by_economy_pages"] = ep
    data["by_economy_ex_desc"] = ed
    data["scoped_count"] = sum(1 for s in us.values() if s)

    # 权威豁免列表也由本次提取重建。
    #
    # 此前这里写的是"原 universal/by_economy 列表保持不变"，于是同一份事实存了两套：
    # by_economy（判豁免、影响税额）来自更早的按页归属，by_economy_pages（仅作出处
    # 展示）来自这里的行/表级 Part 状态机。ANNEX II 里 Part 常常从页面中部开始
    # （物理页 245 上半是 Part B、下半是 Part C），按页归属就会整段错位。
    # 实测两者相差 189 个编码：83 个被错判为豁免（少收 10~12.5% FLIP 301），
    # 106 个漏判（多收）。例：3823.11.00（硬脂酸）在 FRN 页 245 属 Part C（EU），
    # 却被记进 GB，英国产该货会被告知"豁免"，实际 GB 在 10% 档。
    #
    # 两份数据能各自演化而无人比对，本身就是缺陷。现在统一由这里生成，
    # 并有 tests/test_flip_exemptions.py 锁住一致性。
    prev_u, prev_e = data.get("universal") or [], data.get("by_economy") or {}
    data["universal"] = sorted(up)
    data["by_economy"] = {k: sorted(v) for k, v in sorted(ep.items())}
    _report_delta(prev_u, prev_e, data["universal"], data["by_economy"])

    meta = dict(data.get("meta", {}))
    meta["scope_extraction"] = (
        "2026-08-14 从 FRN 提取 Scope Limitations 与物理页码；"
        "2026-08-28 起 universal / by_economy 亦由同一次提取重建（此前按页归属，"
        "Part 从页面中部开始时会整段错位，两套数据相差 189 个编码）。"
    )
    meta["parts_page_physical"] = (
        "物理页号 1 起，与浏览器 #page=N 一致。注意 meta.parts 的页范围是人工粗标，"
        "一页上可能同时结束上一 Part、开始下一 Part（如页 245 上半 Part B、下半 Part C），"
        "因此编码的实际页号可能落在标注范围之外——以 *_pages 为准，勿用 parts 反推归属。"
    )
    data["meta"] = meta

    tmp = EXEMPTIONS_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, EXEMPTIONS_JSON)
    print(f"已回写 {EXEMPTIONS_JSON}")
    return data


def main():
    print("提取 FLIP FRN ANNEX II 范围限制与页码（约 1-3 分钟）...")
    us, up, ud, es, ep, ed = extract()
    print(f"Part A 编码 {len(us)} 个（其中带范围限制 {sum(1 for s in us.values() if s)} 个）")
    for k, v in es.items():
        print(f"  Part {k}: {len(v)} 个")
    print(f"  其中 Ex 档（范围由 Description 栏定义）留存描述："
          f"Part A {len(ud)} 条，Parts B-O {sum(len(v) for v in ed.values())} 条")
    data = merge_and_save(us, up, ud, es, ep, ed)

    # 自检：关键编码
    for probe in ("90251980", "85076000", "85414300"):
        scope = us.get(probe, "（未提取到）")
        page = up.get(probe, "（未提取到）")
        print(f"自检 {probe}: scope={scope!r} 物理页={page}")
    # 与原有 universal 列表比对
    missing = [c for c in data.get("universal", []) if c not in us]
    print(f"原 universal 列表 {len(data.get('universal', []))} 个中未匹配到表格的: {len(missing)} 个"
          + (f"（如 {missing[:5]}）" if missing else ""))


if __name__ == "__main__":
    main()
