# -*- coding: utf-8 -*-
"""
extract_flip_scopes.py —— 从 FLIP 301 FRN PDF 提取 ANNEX II 各豁免编码的
Scope Limitations（范围限制，如 Aircraft/Pharma）与物理页码，回写 data/flip301_exemptions.json。

背景：FLIP 301 Final Action FRN（2026-07-23 发布）ANNEX II 豁免清单中，
部分子目带 Scope Limitations（部分覆盖）。原转录（flip301_exemptions.json）只保留了
编码列表（scoped_count=0），未保留范围限制列。本脚本补充提取，并记录每个编码所在物理页，
供 Web 端"来源追溯"点击查看原文定位使用。

输出新增键（保留原有 universal / by_economy / meta 不变）：
  - universal_scopes:      {norm8: "Aircraft"|"Pharma"|""}   Part A 通用豁免的范围限制
  - universal_pages:       {norm8: 物理页号}                  Part A 编码所在页（PDF 页号，1 起）
  - by_economy_scopes:     {经济体: {norm8: scope}}          Parts B-O 范围限制
  - by_economy_pages:      {经济体: {norm8: 页号}}           Parts B-O 编码所在页
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
    by_economy_scopes = {}
    by_economy_pages = {}
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
                    if part == "A":
                        universal_scopes[code] = scope
                        universal_pages[code] = page_no
                    elif part in part_aliases:
                        key = part_aliases[part]
                        by_economy_scopes.setdefault(key, {})[code] = scope
                        by_economy_pages.setdefault(key, {})[code] = page_no
                    # Part A 之外的未识别部分跳过

    return universal_scopes, universal_pages, by_economy_scopes, by_economy_pages


def merge_and_save(us, up, es, ep):
    with open(EXEMPTIONS_JSON, encoding="utf-8-sig") as f:
        data = json.load(f)

    data["universal_scopes"] = us
    data["universal_pages"] = up
    data["by_economy_scopes"] = es
    data["by_economy_pages"] = ep
    data["scoped_count"] = sum(1 for s in us.values() if s)

    meta = dict(data.get("meta", {}))
    meta["scope_extraction"] = (
        "2026-08-14 从 FRN 重新提取：补充各编码 Scope Limitations（Aircraft/Pharma）"
        "与物理页码；universal_scopes/universal_pages/by_economy_scopes/by_economy_pages"
        "为新增键，原 universal/by_economy 列表保持不变。"
    )
    meta["parts_page_physical"] = "物理页号 1 起，与浏览器 #page=N 一致（Part A 起始于 PDF 物理页 139 附近）"
    data["meta"] = meta

    tmp = EXEMPTIONS_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, EXEMPTIONS_JSON)
    print(f"已回写 {EXEMPTIONS_JSON}")
    return data


def main():
    print("提取 FLIP FRN ANNEX II 范围限制与页码（约 1-3 分钟）...")
    us, up, es, ep = extract()
    print(f"Part A 编码 {len(us)} 个（其中带范围限制 {sum(1 for s in us.values() if s)} 个）")
    for k, v in es.items():
        print(f"  Part {k}: {len(v)} 个")
    data = merge_and_save(us, up, es, ep)

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
