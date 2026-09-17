# -*- coding: utf-8 -*-
"""
audit_company.py —— 本公司申报数据对账（纯本地、不调模型、几秒钟跑完几千条）

【为什么先对账再入先例库】公司先例库的权重是 4（全场最高），而且品名完全相同时直接出结论、
不走模型。历史申报里只要有一条报错，进了先例库就会被稳定地重复下去，连被质疑的机会都没有。
所以顺序必须是：**对账 → 人工过分歧 → 确认的才 record() 进先例库**。

【查什么】七类，都不需要模型，只拿本地税则比对：
  1. 编码存在性   8 位不在现行税则 / 10 位统计后缀已作废（报关系统会退）
  2. 基础税率     申报的 DUTY 与现行 general 不符
  3. 口径混用     DUTY 里把 301/FLIP 折进去了（与其余行不同口径，容易与「总加征」重复计算）
  4. 复合税缺项   法定是「x¢/kg + y%」而字段只存了从价部分
  5. 材质分支     第十一类注释 2：按重量占优的纤维归类。申报材质与编码的材质支不符
  6. 品名对不上   申报品名的品类词与品目条文冲突（毛衣报在"裙子"项下这种）
  7. 高风险组合   低税率的具名天然纤维行（含丝 70%+、全羊绒等）+ 异常低的申报单价。
                  这一类**不判对错**，只标出来——真假要看第三方成分检测报告，是海关实验室抽检的重点。

【用法】
    python scripts/audit_company.py result_2026.json                 # 终端摘要
    python scripts/audit_company.py result_2026.json --md out.md     # 另存 Markdown 报告
    python scripts/audit_company.py data.xlsx --json out.json        # 机器可读
支持 .json（对象数组）/ .csv / .xlsx。字段名按 FIELD_ALIASES 自动认，认不出用 --map 指定。

【边界】只比对本地税则能算的部分。AD/CVD、232 等未建模措施不在其中；"高风险组合"是风险提示
不是结论。申报数据含公司品名，默认不入 git（.gitignore 里有 result_*.json）。
"""
import argparse
import collections
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------- 字段识别 ----------

FIELD_ALIASES = {
    "code":       ("HS CODE", "HS_CODE", "HSCODE", "hs_code", "编码", "税号", "HTS", "HTS CODE"),
    "duty":       ("DUTY", "Duty", "duty", "基础税率", "税率"),
    "surcharge":  ("总加征", "加征", "surcharge", "总税率"),
    "desc_en":    ("DESCRIPTION OF GOODS", "英文品名", "DESCRIPTION", "英文描述"),
    "name_zh":    ("中文品名", "品名", "中文名称", "Unnamed: 2"),
    "unit_value": ("单价(USD)", "单价", "unit_value", "价格", "UNIT PRICE"),
    "texture":    ("TEXTURE", "材质", "成分", "MATERIAL"),
}


def load_rows(path):
    """.json（对象数组）/ .csv / .xlsx → [dict]"""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".json":
        d = json.load(open(path, encoding="utf-8"))
        return d if isinstance(d, list) else (d.get("items") or d.get("data") or [])
    if ext in (".csv", ".tsv"):
        import csv
        with open(path, encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f, delimiter="\t" if ext == ".tsv" else ","))
    if ext in (".xlsx", ".xlsm"):
        import openpyxl
        ws = openpyxl.load_workbook(path, read_only=True, data_only=True).active
        it = ws.iter_rows(values_only=True)
        head = [str(h or "").strip() for h in next(it)]
        return [dict(zip(head, r)) for r in it]
    raise SystemExit(f"不认识的文件类型：{ext}（支持 .json/.csv/.xlsx）")


def field_map(rows, override=None):
    """样本里出现过的列名 → 规范字段。override 形如 {'code': '税号'}。"""
    seen = set()
    for r in rows[:50]:
        seen |= set(r)
    out = dict(override or {})
    for key, names in FIELD_ALIASES.items():
        if key in out:
            continue
        for n in names:
            if n in seen:
                out[key] = n
                break
    return out


def norm_rows(rows, fmap):
    out = []
    for r in rows:
        g = lambda k: r.get(fmap.get(k, "\x00"))
        out.append({"code": digits(g("code")), "duty": g("duty"), "surcharge": g("surcharge"),
                    "desc_en": str(g("desc_en") or "").strip(), "name_zh": str(g("name_zh") or "").strip(),
                    "unit_value": fnum(g("unit_value")), "texture": str(g("texture") or "").strip(), "_raw": r})
    return out


def digits(s):
    return re.sub(r"\D", "", str(s or ""))


def fnum(x):
    try:
        return float(str(x).strip())
    except (TypeError, ValueError):
        return None


def pct(s):
    """'8.3%' → 8.3；'Free'/'0' → 0.0；从量/复合或空 → None"""
    t = str(s or "").strip()
    if not t:
        return None
    if re.fullmatch(r"(?i)free|免税?|0", t):
        return 0.0
    if re.search(r"[¢$]|/\s*(kg|gross|doz|liter|m2|no\.)", t) and "+" not in t:
        return None            # 纯从量，折不成百分比
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", t)
    return float(m.group(1)) if m else None


def is_compound(gen):
    """法定税率是否含从量部分（'28.8¢/gross + 4.6%'）"""
    return bool(re.search(r"[¢$]|/\s*(kg|gross|doz|liter)", str(gen or "")))


# ---------- 材质 ----------

FIBER_CATS = (
    (r"silk|真丝|桑蚕丝|(?<![a-z])丝(?![袜网])", "silk"),
    (r"cashmere|羊绒|开司米", "cashmere"),
    (r"wool|羊毛|alpaca|mohair|camel hair|fine animal hair", "wool"),
    (r"cotton|棉", "cotton"),
    (r"polyester|nylon|polyamide|acrylic|spandex|elastane|polyurethane|rayon|viscose|modal|lyocell|tencel|"
     r"涤纶|锦纶|尼龙|腈纶|氨纶|聚氨酯|粘胶|人造丝|莫代尔", "mmf"),
    (r"linen|flax|ramie|hemp|jute|亚麻|苎麻|大麻|黄麻", "veg"),
)
CAT_ZH = {"silk": "丝", "cashmere": "羊绒", "wool": "毛", "cotton": "棉", "mmf": "化纤", "veg": "麻"}


def parse_texture(t):
    """'75% silk +25% polyester' → {'silk': 75.0, 'mmf': 25.0}；认不出返回 {}"""
    out = collections.Counter()
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*%?\s*([A-Za-z一-鿿][A-Za-z一-鿿\s\-]*)", str(t or "")):
        p, name = float(m.group(1)), m.group(2).strip()
        for rx, cat in FIBER_CATS:
            if re.search(rx, name, re.I):
                out[cat] += p
                break
    return dict(out)


def code_material(db, c8, _rate=None):
    """编码要求的材质。返回 ({类别}, 具名门槛 or None, 条文全文)。"""
    rate = _rate or __import__("rate")
    txt = " > ".join(rate.path_of(db, c8)) + " > " + db["rates_8"].get(c8, {}).get("desc", "")
    low = txt.lower()
    need, thr = set(), None
    m = re.search(r"containing (\d+) percent or more by weight of (silk|wool|cashmere|cotton|flax|linen)", low)
    if m:
        thr = (int(m.group(1)), {"silk": "silk", "wool": "wool", "cashmere": "cashmere",
                                 "cotton": "cotton", "flax": "veg", "linen": "veg"}[m.group(2)])
    if re.search(r"\bof silk\b|of silk or silk waste", low):
        need.add("silk")
    if re.search(r"\bof cotton\b", low):
        need.add("cotton")
    if re.search(r"of man-?made fibers|of synthetic fibers|of artificial fibers", low):
        need.add("mmf")
    if re.search(r"wholly of cashmere|of kashmir \(cashmere\) goats", low):
        need.add("cashmere")
    elif re.search(r"of wool or fine animal hair|\bof wool\b", low):
        need.add("wool")
    return need, thr, txt


# ---------- 品名 vs 品目 ----------

# 品类互斥表：(类名, 申报品名里的词, 税则条文里的词)。
# 判"申报品名属 A 类，但编码的归类路径明确写着 B 类"——比"4 位品目在不在白名单里"准得多：
# 女士毛衣报在 6104.52（Skirts and divided skirts），4 位 6104 本身没错（针织女装大类），
# 错的是子目条文写着"裙子"。\b 是必须的，否则 'bra' 命中 'bracket'、'vest' 命中 'investment'。
GOODS_CATS = (
    ("毛衣/背心", r"\bsweaters?\b|\bpullovers?\b|\bcardigans?\b|\bvests?\b|\bwaistcoats?\b",
     r"sweaters, pullovers|waistcoats \(vests\)"),
    ("裙子",     r"\bskirts?\b", r"skirts and divided skirts"),
    ("连衣裙",   r"\bdress(es)?\b", r"\bdresses\b"),
    ("裤子",     r"\btrousers\b|\bpants\b|\bshorts\b|\bbreeches\b",
     r"trousers, bib and brace overalls"),
    ("衬衫",     r"\bshirts?\b|\bblouses?\b", r"\bshirts\b|blouses, shirts"),
    ("T恤/背心衫", r"\bt-?shirts?\b|\btank tops?\b|\bsinglets?\b", r"t-shirts, singlets"),
    ("袜子",     r"\bsocks?\b|\bstockings?\b|\bhosiery\b", r"panty hose, tights, stockings"),
    ("手套",     r"\bgloves?\b|\bmittens?\b", r"gloves, mittens"),
    ("胸罩",     r"\bbrassieres?\b|\bbras\b", r"brassieres, girdles"),
    ("睡衣",     r"\bpajamas?\b|\bpyjamas?\b|\bnightdress", r"nightdresses and pajamas|nightshirts and pajamas"),
)


def goods_conflict(desc_en, tariff_text):
    """申报品名属 A 类、条文明确是 B 类 → 返回 (A, B)；否则 None。两边都只认唯一归属。"""
    d, t = " " + str(desc_en or "").lower() + " ", str(tariff_text or "").lower()
    in_desc = [n for n, rx, _ in GOODS_CATS if re.search(rx, d)]
    in_text = [n for n, _, rx in GOODS_CATS if re.search(rx, t)]
    if len(in_desc) != 1 or len(in_text) != 1 or in_desc[0] == in_text[0]:
        return None
    # 条文里同时写着申报的那一类（"Sweaters, pullovers ... and similar articles"）就不算冲突
    if re.search(dict((n, rx) for n, _, rx in GOODS_CATS)[in_desc[0]], t):
        return None
    return in_desc[0], in_text[0]


# ---------- 对账 ----------

def audit(db, rows, low_price=2.0):
    """rows 为 norm_rows 的输出。返回按严重度排序的 findings。"""
    import rate
    # 按 8 位分组：归类的判断单位是 8 位（税率、材质支、品目条文都在这一级）。
    # 10 位统计后缀的有效性在组内单独查，否则同一个 8 位会因后缀不同被拆成好几条重复发现。
    f, by_code = [], collections.defaultdict(list)
    for r in rows:
        by_code[r["code"][:8]].append(r)

    def add(level, kind, code, items, msg, advice=""):
        f.append({"级别": level, "类型": kind, "编码": code, "条数": len(items),
                  "说明": msg, "建议": advice,
                  "样例": [{"中文品名": x["name_zh"], "英文品名": x["desc_en"], "材质": x["texture"],
                            "单价": x["unit_value"]} for x in items[:3]]})

    for c8, items in by_code.items():
        code = c8
        info = db["rates_8"].get(c8)
        if not info:
            add("错误", "编码不存在", c8, items, f"8 位 {c8} 不在现行税则（1–97 章）里", "重新归类")
            continue
        # ① 10 位统计后缀（组内按后缀分别查）
        live = sorted(k[8:] for k in db["desc_10"] if k.startswith(c8))
        dead = collections.defaultdict(list)
        for it in items:
            if len(it["code"]) >= 10 and it["code"][:10] not in db["desc_10"]:
                dead[it["code"][:10]].append(it)
        for c10, its in dead.items():
            add("错误", "统计后缀作废", c10, its,
                f"10 位 {c10} 不存在；{c8} 现行后缀：{', '.join(live) or '无'}",
                "改用现行后缀，否则报关系统会退单")
        gen = info.get("general", "")
        mine, theirs = pct(gen), pct(items[0]["duty"])
        # ② 复合税只存了从价部分
        if is_compound(gen) and theirs is not None and mine is not None and abs(mine - theirs) < 0.001:
            add("提示", "复合税缺从量项", c8, items,
                f"法定税率是「{gen}」，字段只存了从价部分 {theirs}%", "按数量/重量另算从量部分")
        # ③ 基础税率
        elif mine is not None and theirs is not None and abs(mine - theirs) > 0.001:
            t = rate.calc_total(db, c8, origin="CN")
            s301 = pct(t.get("301加征")) or 0.0
            flip = 12.5 if "FLIP 301" in str(t.get("备注", "")) else 0.0
            folded = next((x for x in (s301, flip, s301 + flip) if x and abs(mine + x - theirs) < 0.051), None)
            if folded:
                add("提示", "税率口径混用", c8, items,
                    f"DUTY 写 {theirs}% = 基础 {mine}% + 加征 {folded:g}%；其余行的 DUTY 是纯基础税率",
                    "统一口径，并检查是否与「总加征」重复计算")
            else:
                add("错误", "基础税率不符", c8, items,
                    f"申报 {theirs}%，现行 general 是 {gen}", "核对 HTS 版本或编码")
        # ④ 材质分支。门槛（"containing 70% or more of silk"）与占优（"of man-made fibers"）
        #    是两个独立条件，一条税则行可能同时带：6204.33.20 = 合成纤维制 + 含亚麻 36% 以上。
        #    只查前者会漏掉"亚麻 55% / 聚氨酯 45% 却报在合成纤维支"这种。
        need, thr, txt = code_material(db, c8)
        fib = parse_texture(items[0]["texture"]) if (need or thr) else {}
        if fib:
            dom = max(fib, key=fib.get)
            pu = re.search(r"polyurethane|聚氨酯|\bPU\b", items[0]["texture"], re.I)
            pu_note = ("；含聚氨酯，若它是涂层而非纤维，则按第 59 章注释 2 应是 5903 面料制的服装"
                       "（6113 针织 / 6210 梭织）" if pu else "")
            if thr and fib.get(thr[1], 0) + 1e-9 < thr[0]:
                add("错误", "含量门槛不满足", c8, items,
                    f"条文要求「{CAT_ZH.get(thr[1], thr[1])} ≥ {thr[0]}%」，申报材质是 {_fmt_fib(fib)}",
                    f"改归不带该门槛的同级行；含量以第三方检测报告为准")
            elif need and dom not in need:
                add("存疑", "材质支不符", c8, items,
                    f"条文要求「{'/'.join(CAT_ZH.get(x, x) for x in sorted(need))}制」，"
                    f"申报以{CAT_ZH.get(dom, dom)}为主（{_fmt_fib(fib)}）；"
                    f"第十一类注释 2：混纺按重量占优的纤维归类" + pu_note,
                    "核实占优纤维，改归对应材质支")
        # ⑤ 品名与条文的品类冲突。只看**品目以下**的条文：4 位品目条文本身是个大杂烩
        #    （6104 = "suits, ensembles, jackets, blazers, dresses, skirts, trousers..."），
        #    拿它比对必然同时命中好几类，什么都判不出来。子目条文才是具体品类。
        sub_text = " > ".join(rate.path_of(db, c8)[1:] or [""]) + " > " + info["desc"]
        conflict = goods_conflict(items[0]["desc_en"], sub_text)
        if conflict:
            a, b = conflict
            add("错误", "品名与条文冲突", c8, items,
                f"申报品名「{items[0]['desc_en']}」是{a}，但该编码的子目条文写的是{b}：{sub_text[:110]}",
                "重新归类到该品类对应的子目")
        # ⑥ 高风险组合：低税率的具名天然纤维行 + 低单价
        if thr and thr[1] in ("silk", "cashmere", "wool") or "cashmere" in need:
            av = rate.estimate_ad_valorem(gen)
            ps = [x["unit_value"] for x in items if x["unit_value"] is not None]
            cheap = [p for p in ps if p < low_price]
            if av is not None and av <= 5 and cheap:
                add("存疑", "低价高端纤维", c8, items,
                    f"基础税率仅 {gen}（具名{CAT_ZH.get(thr[1] if thr else 'cashmere')}行），"
                    f"但 {len(cheap)}/{len(ps)} 条申报单价低于 ${low_price:g}"
                    f"（{min(ps):.2f}–{max(ps):.2f}）",
                    "取第三方成分检测报告留档；纤维成分是 CBP 实验室抽检的常规项目")

    order = {"错误": 0, "存疑": 1, "提示": 2}
    return sorted(f, key=lambda x: (order[x["级别"]], -x["条数"], x["编码"]))


def _fmt_fib(fib):
    return " ".join(f"{CAT_ZH.get(k, k)}{v:g}%" for k, v in sorted(fib.items(), key=lambda kv: -kv[1]))


# ---------- 输出 ----------

def summary(findings, n_rows, n_codes):
    c = collections.Counter(x["级别"] for x in findings)
    hit = sum(x["条数"] for x in findings)
    return (f"{n_rows} 条申报、{n_codes} 个编码；命中 {len(findings)} 组、覆盖 {hit} 条\n"
            f"  错误 {c['错误']}　存疑 {c['存疑']}　提示 {c['提示']}")


def report_md(findings, n_rows, n_codes, src=""):
    out = ["# 申报数据对账报告", "",
           f"来源：`{os.path.basename(src)}`　{n_rows} 条申报、{n_codes} 个不同编码", "",
           "> 纯本地税则比对，未调用模型。AD/CVD、232 等未建模措施不在核对范围。",
           "> 「存疑」是风险提示不是结论，需第三方证据（成分检测报告、实物）才能定。", ""]
    for lvl in ("错误", "存疑", "提示"):
        got = [x for x in findings if x["级别"] == lvl]
        if not got:
            continue
        out += [f"## {lvl}（{len(got)} 组，{sum(x['条数'] for x in got)} 条）", ""]
        for x in got:
            out += [f"### {x['编码']}　×{x['条数']}　{x['类型']}", "",
                    f"- **问题**：{x['说明']}",
                    f"- **建议**：{x['建议'] or '—'}", "", "| 中文品名 | 英文品名 | 材质 | 单价 |", "|---|---|---|---|"]
            for s in x["样例"]:
                price = "—" if s["单价"] is None else "${:.2f}".format(s["单价"])
                out.append("| {} | {} | {} | {} |".format(s["中文品名"], s["英文品名"], s["材质"], price))
            out.append("")
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description="本公司申报数据对账（本地税则比对，不调模型）")
    ap.add_argument("path", help="申报数据文件（.json / .csv / .xlsx）")
    ap.add_argument("--md", default="", help="另存 Markdown 报告")
    ap.add_argument("--json", dest="js", default="", help="另存 JSON（机器可读）")
    ap.add_argument("--low-price", type=float, default=2.0, help="「低价高端纤维」的单价阈值（默认 2.0 美元）")
    ap.add_argument("--map", default="", help='字段名覆盖，如 code=税号,texture=成分')
    a = ap.parse_args(argv)

    import core
    override = dict(kv.split("=", 1) for kv in a.map.split(",") if "=" in kv) if a.map else None
    raw = load_rows(a.path)
    if not raw:
        raise SystemExit("文件里没有记录")
    fmap = field_map(raw, override)
    if "code" not in fmap:
        raise SystemExit(f"认不出编码列。现有列：{list(raw[0])}；用 --map code=列名 指定")
    rows = norm_rows(raw, fmap)
    n_codes = len({r["code"] for r in rows})
    findings = audit(core.load_db(), rows, low_price=a.low_price)

    print(f"字段映射：{fmap}\n")
    print(summary(findings, len(rows), n_codes), "\n")
    for x in findings:
        print(f"[{x['级别']}] {x['编码']} ×{x['条数']}  {x['类型']}")
        print(f"        {x['说明']}")
        if x["建议"]:
            print(f"        → {x['建议']}")
        print(f"        例：{x['样例'][0]['中文品名']} | {x['样例'][0]['英文品名']} | {x['样例'][0]['材质']}")
    if a.md:
        open(a.md, "w", encoding="utf-8").write(report_md(findings, len(rows), n_codes, a.path))
        print(f"\nMarkdown → {a.md}")
    if a.js:
        json.dump({"来源": os.path.basename(a.path), "条数": len(rows), "编码数": n_codes,
                   "findings": findings}, open(a.js, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"JSON → {a.js}")
    return findings


if __name__ == "__main__":
    main()
