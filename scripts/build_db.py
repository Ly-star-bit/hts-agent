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
      - rates_8:   {norm8: {desc, general, special, col2}}     8位子目税率与描述
      - desc_10:   {norm10: desc}                               10位统计后缀描述（更具体）
      - add_duty:  {norm(8或10): 附加关税文本}                   反倾销/反补贴等附加税
      - c99_rates: {norm(9903.xx): General税率文本}             99章子目税率（含301加征比例）
    """
    rates_8 = {}
    desc_10 = {}
    add_duty = {}
    c99_rates = {}
    with open(HTS_CSV, encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        next(reader)  # 跳过表头
        for row in reader:
            if not row or not row[0].strip():
                continue  # 层级描述行（HTS Number 为空）
            # 注意：CSV 中存在跨物理行的记录（描述内嵌换行），enumerate 计数会偏移，
            # 必须用 reader.line_num 记录真实物理行号，保证"来源追溯"定位准确
            line_no = reader.line_num
            raw = row[0].strip().strip('"')
            n = norm(raw)
            desc = row[2].strip()
            general = row[4].strip()
            special = row[5].strip()
            col2 = row[6].strip()
            add = row[8].strip()
            if len(n) == 8:
                rates_8[n] = {"desc": desc, "general": general, "special": special, "col2": col2, "line": line_no}
                if add:
                    add_duty.setdefault(n, add)
            elif len(n) == 10:
                desc_10[n] = desc
                if add:
                    add_duty.setdefault(n, add)
                # 部分 8 位子目在官方文件中只以 10 位形式出现（如 0203.29.20.00），
                # 税率写在 10 位行上：将其继承到 8 位前缀，保证 301 判定可回查税率。
                if general and n[:8] not in rates_8:
                    rates_8[n[:8]] = {"desc": desc, "general": general, "special": special, "col2": col2, "line": line_no}
            if n.startswith("9903"):
                c99_rates[n] = general
    return rates_8, desc_10, add_duty, c99_rates


def parse_ustr_pdf():
    """
    解析 USTR China Tariffs PDF，返回 (mapping, pages)：
      - mapping: {norm8: norm(9903.xx)} 归属映射
      - pages:   {norm8: 物理页号}      每个 8 位子目在 PDF 中的位置（页号 1 起）
    PDF 为两列表格：8位HTS子目 → 适用的 Chapter 99 子目。
    """
    import pdfplumber

    mapping = {}
    pages = {}
    pat = re.compile(r"^(\d{4}\.\d{2}\.\d{2}\.?\d{0,4})\s+(\d{4}\.\d{2}\.\d{2})$")
    with pdfplumber.open(PDF_FILE) as pdf:
        for idx, page in enumerate(pdf.pages):
            page_no = idx + 1  # 物理页号（1 起，与浏览器 #page=N 一致）
            text = page.extract_text() or ""
            for line in text.splitlines():
                m = pat.match(line.strip())
                if m:
                    hts, c99 = m.groups()
                    if c99.startswith("9903"):
                        code = norm(hts)
                        if len(code) == 10:
                            code = code[:8]  # 301 判定统一按 8 位子目
                        mapping.setdefault(code, norm(c99))
                        pages.setdefault(code, page_no)
    return mapping, pages


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


def build():
    os.makedirs(DATA_DIR, exist_ok=True)
    print("① 解析 htsdata.csv ...")
    rates_8, desc_10, add_duty, c99_rates = parse_hts_csv()
    print(f"   8位子目: {len(rates_8)} | 10位描述: {len(desc_10)} | 附加税行: {len(add_duty)} | 9903子目: {len(c99_rates)}")

    print("② 解析 USTR China Tariffs PDF ...")
    sec301_map, sec301_pages = parse_ustr_pdf()
    print(f"   301 归属映射: {len(sec301_map)} 条（含页码）")

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
    # UFLPA 强迫劳动检查维度已移除（v1.5），不再摄入 uflpa_entities.json
    ex_univ = len(flip301_exemptions.get("universal", []))
    ex_econ = {k: len(v) for k, v in (flip301_exemptions.get("by_economy") or {}).items()}
    print(f"   flip 历史编码: {len(flip_301.get('flips', {}))} | "
          f"越南覆盖编码: {len(vietnam.get('covered_codes', []))} | "
          f"FLIP 301: 10%档 {len((flip301.get('rates') or {}).get('10', []))} | "
          f"12.5%档 {len((flip301.get('rates') or {}).get('125', []))} | "
          f"FLIP 301 豁免: 通用 {ex_univ} | 按经济体 {ex_econ}")

    from datetime import datetime

    db = {
        "meta": {
            "hts_csv": os.path.basename(HTS_CSV),
            "ustr_pdf": os.path.basename(PDF_FILE),
            "flip_frn_pdf": "FLIP 301 Investigation Final Action FRN 7-23-26 FINAL.pdf",
            "sec301_mapping_count": len(sec301_map),
            "rates_8_count": len(rates_8),
            "built_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        },
        "rates_8": rates_8,          # norm8 -> 基础税率/描述（含 CSV 行号 line）
        "desc_10": desc_10,          # norm10 -> 具体描述
        "add_duty": add_duty,        # norm(8/10) -> 附加关税
        "sec301_map": sec301_map,    # norm8 -> norm(9903.xx)
        "sec301_pages": sec301_pages,  # norm8 -> USTR PDF 物理页号（来源追溯用）
        "c99_percent": c99_percent,  # norm(9903.xx) -> 加征百分比
        "flip_301": flip_301,        # 301 flip 历史（此前档位）
        "vietnam": vietnam,          # 越南适用措施与代表编码说明
        "flip301": flip301,          # FLIP 301 强迫劳动调查关税（60 经济体税率表 + 豁免）
        "flip301_exemptions": flip301_exemptions,  # FLIP 301 ANNEX II 豁免编码清单
    }
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, separators=(",", ":"))
    print(f"④ 数据库已写入: {OUT_JSON}")

    print("⑤ 对比上一版本，生成数据变动清单 ...")
    from datetime import datetime
    import db_diff
    built_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    result = db_diff.compare_with_fingerprint(db, built_at=built_at)
    if result["first_build"]:
        print("   首次构建，无历史版本可对比（已保存基准快照）。")
    else:
        s = result["stats"]
        print(f"   变动统计：新增 {s.get('added', 0)} | 删除 {s.get('removed', 0)} | "
              f"税率变化 {s.get('rate_changed', 0)} | 301变化 {s.get('sec301_changed', 0)}")
        for ch in result["changes"][:10]:
            print(f"     [{ch['类型']}] {ch['编码']} {ch['描述'][:30]} | {ch['旧']} → {ch['新']}")
        if len(result["changes"]) > 10:
            print(f"     ...（其余 {len(result['changes']) - 10} 条见 Web 端『数据变动』）")
    db_diff.save_changes(result)
    db_diff.save_fingerprint(db)
    print("   已保存变动清单与基准快照。")
    return db


if __name__ == "__main__":
    build()
