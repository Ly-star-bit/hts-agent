# -*- coding: utf-8 -*-
"""
adcvd_import.py —— 把 AD/CVD（反倾销/反补贴）案件清单导入成本工具能用的索引源。

【为什么是"导入"而不是"抓取"】
  案件 ↔ HTS 参考号的官方来源有两处：
    · CBP ACE 公开检索（https://trade.cbp.dhs.gov/ace/adcvd/adcvd-public/）——案件服务
      走 cargo-public.cbp.dhs.gov，2026-09 实测本机（含无头浏览器）连不上，同域的
      service-cases 只回 302 到登录页；
    · ITA 的 ADCVD Proceedings 看板（https://www.trade.gov/data-visualization/adcvd-proceedings）
      ——Tableau，可在页面上"Export"成 Excel/CSV，但没有可编程接口。
  所以这里只做导入：你从 ITA 看板（或 ACE 的 exportHTS）导出一份表，跑本脚本落成
  data/adcvd_cases.json，build_db.py 编译成 HTS → 案件 索引。查询时**只探测**：
  "该编码落在 N 个案件的参考 HTS 范围"——AD/CVD 的范围是按商品描述（scope）定的，
  HTS 只是参考，而且税率按出口商定，所以不判定、不计税。

【表头怎么认】列名五花八门，按正则各认一组同义写法（认不到就报错说缺哪列）：
    案号     case / case number / 案号            例 A-570-979 / C-570-980
    商品     product / short name / 商品 / 品名
    国家     country / 国家
    HTS      hts / harmonized / tariff / 税号        一格里可以是多个号（逗号/分号/空格/换行分隔）
    状态     status / 状态                            可选
    类型     type（AD/CVD）                           可选；缺省从案号首字母推（A=AD，C=CVD）

用法：
    python scripts/adcvd_import.py 导出的表.xlsx              # → data/adcvd_cases.json
    python scripts/adcvd_import.py 导出的表.csv --source "ITA ADCVD Proceedings 2026-09-15 导出"
    python scripts/build_db.py                                 # 编译进库
"""
import argparse
import datetime as dt
import json
import os
import re
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE_DIR, "data", "adcvd_cases.json")

COLS = {
    "case": re.compile(r"(case\s*(no|number|#)?|案\s*号|案件)", re.I),
    "product": re.compile(r"(product|short\s*name|merchandise|商\s*品|品\s*名|描\s*述)", re.I),
    "country": re.compile(r"(country|国\s*家|原产)", re.I),
    "hts": re.compile(r"(hts|harmonized|tariff|税\s*号|税则)", re.I),
    "status": re.compile(r"(status|状\s*态)", re.I),
    "type": re.compile(r"(^type$|case\s*type|类\s*型)", re.I),
}
_CASE_RE = re.compile(r"\b([AC])-(\d{3})-(\d{3,4})\b", re.I)
_HTS_RE = re.compile(r"\d{4}(?:\.\d{2}){0,3}\d*")

# 国家名 → ISO（与 build_db.NAME_TO_ISO 保持一致的小子集；认不出保留原文）
COUNTRY_ISO = {
    "china": "CN", "people's republic of china": "CN", "prc": "CN", "vietnam": "VN",
    "socialist republic of vietnam": "VN", "india": "IN", "korea": "KR", "south korea": "KR",
    "republic of korea": "KR", "taiwan": "TW", "japan": "JP", "mexico": "MX", "canada": "CA",
    "thailand": "TH", "malaysia": "MY", "indonesia": "ID", "turkey": "TR", "türkiye": "TR",
    "germany": "DE", "italy": "IT", "spain": "ES", "brazil": "BR", "russia": "RU",
    "united kingdom": "GB", "cambodia": "KH", "philippines": "PH", "argentina": "AR",
}


def _match_cols(columns):
    taken, out = set(), {}
    for field, pat in COLS.items():
        for col in columns:
            if col in taken:
                continue
            if pat.search(str(col)):
                out[field] = col
                taken.add(col)
                break
    return out


def _read_table(path):
    import pandas as pd
    if path.lower().endswith((".xlsx", ".xls")):
        return pd.read_excel(path, dtype=str)
    return pd.read_csv(path, dtype=str, encoding="utf-8-sig")


def norm_hts(text):
    """一格里的税号们 → 纯数字列表（4/6/8/10 位），去重保序。"""
    out = []
    for m in _HTS_RE.findall(str(text or "")):
        d = re.sub(r"\D", "", m)
        if len(d) in (4, 6, 8, 10) and d not in out:
            out.append(d)
    return out


def parse_rows(df):
    cols = _match_cols(list(df.columns))
    missing = [k for k in ("case", "hts") if k not in cols]
    if missing:
        raise SystemExit(f"表头认不出必需列 {missing}；现有列：{list(df.columns)}")
    cases = {}
    for _, row in df.iterrows():
        raw_case = str(row.get(cols["case"], "") or "")
        m = _CASE_RE.search(raw_case)
        if not m:
            continue
        num = f"{m.group(1).upper()}-{m.group(2)}-{m.group(3)}"
        ent = cases.setdefault(num, {
            "案号": num,
            "类型": "AD" if num[0] == "A" else "CVD",
            "商品": "", "国家": "", "国家代码": "", "状态": "", "hts": []})
        if "product" in cols and not ent["商品"]:
            ent["商品"] = str(row.get(cols["product"], "") or "").strip()[:120]
        if "country" in cols and not ent["国家"]:
            c = str(row.get(cols["country"], "") or "").strip()
            ent["国家"] = c
            ent["国家代码"] = COUNTRY_ISO.get(c.lower(), "")
        if "status" in cols and not ent["状态"]:
            ent["状态"] = str(row.get(cols["status"], "") or "").strip()[:40]
        if "type" in cols:
            t = str(row.get(cols["type"], "") or "").strip().upper()
            if t in ("AD", "CVD"):
                ent["类型"] = t
        for h in norm_hts(row.get(cols["hts"], "")):
            if h not in ent["hts"]:
                ent["hts"].append(h)
    return [v for v in cases.values() if v["hts"]]


def main(argv=None):
    ap = argparse.ArgumentParser(description="导入 AD/CVD 案件清单（ITA / ACE 导出表）")
    ap.add_argument("path", help="导出的 xlsx / csv")
    ap.add_argument("--source", default="", help="来源说明（写进 meta，来源弹窗会显示）")
    ap.add_argument("--out", default=OUT)
    a = ap.parse_args(argv)
    df = _read_table(a.path)
    cases = parse_rows(df)
    if not cases:
        raise SystemExit("没解析出任何带 HTS 的案件")
    meta = {"imported_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "source": a.source or os.path.basename(a.path), "file": os.path.basename(a.path),
            "n_cases": len(cases), "n_hts": sum(len(c["hts"]) for c in cases),
            "note": "HTS 只是案件的参考号，范围以案件 scope（商品描述）为准；税率按出口商定。本工具只探测不判定。"}
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "cases": cases}, f, ensure_ascii=False, indent=1)
    by_c = {}
    for c in cases:
        by_c[c["国家代码"] or c["国家"] or "?"] = by_c.get(c["国家代码"] or c["国家"] or "?", 0) + 1
    print(f"案件 {len(cases)} 个（{', '.join(f'{k} {v}' for k, v in sorted(by_c.items(), key=lambda kv: -kv[1])[:8])}）"
          f"，HTS 参考号 {meta['n_hts']} 个 → {a.out}\n下一步：python scripts/build_db.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
