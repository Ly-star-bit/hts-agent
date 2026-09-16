# -*- coding: utf-8 -*-
"""
eval_classify.py —— 归类召回与归类结果的评测集（有了数，再动检索与提示词）

【为什么要有它】此前对归类质量的全部判断是"实测某句品名 5 次出 3 个编码"这类手感。
改一处检索规则、换一个提示词、换一个模型，好没好没有数。CROSS 镜像里 22 万条裁定
每条都带 subject（"The tariff classification of a lithium-ion battery from China"）与
CBP 判给的编码，就是现成的标注数据。

【怎么抽】cross.db 里 2023 年以后、未撤销、subject 以 "The tariff classification of" 开头、
且判给的 8 位编码仍在现行税则的 NY 裁定，按固定随机种子抽 N 条。subject 剥掉套话与
" from <国家>" 作为商品描述。落盘到 data/eval/rulings_golden.json（版本化，评测可复现）。

【量什么】
  召回：关键词 / 语义 / 先例 / 三通道融合 各自的 recall@5 与 recall@20
        （命中 = 金标 8 位在前 k；另报 6 位宽松口径）。
  归类（--llm）：classify_product 的 top-1 / top-3 命中率（需配置 AI provider，慢）。
【防漏答】先例通道会把这条裁定自己检索出来（subject 一模一样），所以评测时把裁定号
  本身从先例里剔除（留一法）；同一商品的其他裁定保留——真实使用里它们本来就在。

用法：
    python scripts/eval_classify.py --build 300        # 从 cross.db 抽金标集
    python scripts/eval_classify.py                    # 评测召回（需 ollama + 两套索引）
    python scripts/eval_classify.py --llm --limit 60   # 再跑归类 top-k（慢）
    python scripts/eval_classify.py --channels keyword # 只评离线通道
"""
import argparse
import datetime as dt
import json
import os
import random
import re
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOLDEN = os.path.join(BASE_DIR, "data", "eval", "rulings_golden.json")
OUT_DIR = os.path.join(BASE_DIR, "output")

_PREFIX = re.compile(r"^\s*the\s+tariff\s+classification\s+of\s+(?:an?\s+|the\s+)?", re.I)
_FROM = re.compile(r"\s+(?:from|manufactured in|made in|produced in)\s+[A-Z][A-Za-z .,'()-]*$")


def _describe(subject):
    """subject → 商品描述：剥套话与产地"""
    s = _PREFIX.sub("", subject or "").strip().rstrip(".")
    s = _FROM.sub("", s).strip()
    return s


def build_golden(n=300, since="2023-01-01", seed=20260915, db_path=None):
    import core
    import cross

    db = core.load_db()
    alive = {c for c in db["rates_8"] if c[:2] not in ("98", "99")}
    conn = sqlite3.connect(f"file:{db_path or cross.DB_PATH}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT number, date, subject, tariffs FROM rulings "
            "WHERE collection='ny' AND date >= ? AND op_revoked=0 "
            "AND (revoked_by IS NULL OR revoked_by='' OR revoked_by='[]') "
            "AND subject LIKE 'The tariff classification of %' ORDER BY number", (since,)).fetchall()
    finally:
        conn.close()
    pool = []
    for number, date, subject, tariffs in rows:
        codes = []
        for t in cross._split_tariffs(tariffs or ""):
            d = re.sub(r"\D", "", t)[:8]
            if len(d) == 8 and d in alive and d not in codes:
                codes.append(d)
        desc = _describe(subject)
        # 一条裁定判给多个编码的（套装、多件），金标不唯一，评测口径会糊——只留单编码裁定
        if len(codes) == 1 and 12 <= len(desc) <= 160:
            pool.append({"裁定号": number, "日期": date, "描述": desc, "金标": codes[0],
                         "subject": subject})
    random.Random(seed).shuffle(pool)
    sample = sorted(pool[:n], key=lambda r: r["裁定号"])
    os.makedirs(os.path.dirname(GOLDEN), exist_ok=True)
    with open(GOLDEN, "w", encoding="utf-8") as f:
        json.dump({"meta": {"built_at": dt.datetime.now().isoformat(timespec="seconds"),
                            "since": since, "seed": seed, "pool": len(pool), "n": len(sample),
                            "note": "NY 裁定，单一现行 8 位编码，留一法评测时剔除裁定号本身"},
                   "items": sample}, f, ensure_ascii=False, indent=1)
    print(f"候选池 {len(pool)} 条，抽样 {len(sample)} 条 → {GOLDEN}")
    return sample


# ---------- 正文金标：用裁定正文里的商品描述段代替 subject ----------
# subject 只有 2–3 个词（"coated fabric"、"chemical mixture"），拿它评归类是猜谜，
# 会低估真实能力也指导不了优化。NY 裁定正文的开头段就是申请人描述的商品，
# 这才接近用户实际会贴进来的东西。留 subject 在 "描述_subject" 里，两套口径都能跑。
TEXT_GOLDEN = os.path.join(BASE_DIR, "data", "eval", "rulings_golden_text.json")
_DESC_START_RE = re.compile(
    r"(?:requested a (?:binding )?(?:tariff classification |classification )?ruling[^.]*\.|"
    r"ruling request[^.]*\.|request for a (?:binding )?(?:tariff classification )?ruling[^.]*\.)", re.I)
_DESC_LEAD_RE = re.compile(
    r"^(?:(?:Additional information|No samples?|Samples?|Photographs?|Pictures?|Descriptive literature|"
    r"Product (?:information|literature)|A sample|The sample|Your (?:sample|request))[^.]*\.\s*)+", re.I)
_DESC_END_RE = re.compile(
    r"\b(?:The applicable (?:sub)?heading|In your (?:letter|request|submission)[^.]{0,80}?"
    r"(?:suggest|propose|state|assert|argue|indicate|believe)|You (?:have |also )?(?:suggest(?:ed)?|propose[d]?|"
    r"state[d]?|assert(?:ed)?|argue[d]?|indicate[d]?|believe[d]?|request(?:ed)?)|"
    r"Classification (?:under|of) the (?:HTSUS|Harmonized)|The General Rules of Interpretation|"
    r"This ruling is being issued|The rate of duty will be|Pursuant to (?:the )?(?:Section|section) 301)", re.I)
# 申请人在描述段里点名的税号是"漏答"（金标就是它），一律抹掉
_HTS_NUM_RE = re.compile(r"\b(?:HTSUS\s+)?(?:sub)?heading\s+\d[\d.]*\b|\b\d{4}\.\d{2}(?:\.\d{2,4}|\.\d{2}\.\d{2})?\b", re.I)


def extract_description(text, max_chars=400):
    """裁定正文 → 商品描述段（申请人描述的货，剥掉套话与税号）。抽不到返回 ''。"""
    t = re.sub(r"\s+", " ", str(text or ""))
    m = _DESC_START_RE.search(t)
    if not m:
        return ""
    body = _DESC_LEAD_RE.sub("", t[m.end():].lstrip())
    e = _DESC_END_RE.search(body)
    if e:
        body = body[:e.start()]
    body = _HTS_NUM_RE.sub("", body)
    body = re.sub(r"\s+", " ", body).strip(" ,;:")
    if len(body) > max_chars:
        cut = body[:max_chars]
        k = cut.rfind(". ")
        body = cut[:k + 1] if k > 150 else cut
    return body


def build_text_golden(items=None, sleep=0.3, log=print):
    """给现有金标集配正文描述：逐条抓 CROSS 正文（永久缓存），落到 TEXT_GOLDEN。"""
    import cross

    if items is None:
        with open(GOLDEN, encoding="utf-8") as f:
            items = json.load(f)["items"]
    out, n_fail = [], 0
    for i, it in enumerate(items, 1):
        txt = cross.fetch_ruling_text(it["裁定号"], "ny", it["日期"])
        desc = extract_description(txt) if txt else ""
        if len(desc) < 40:
            n_fail += 1
            log(f"  {i}/{len(items)} {it['裁定号']} 抽不到描述（正文 {len(txt or '')} 字）")
            continue
        out.append({**it, "描述_subject": it["描述"], "描述": desc, "正文长度": len(txt)})
        if i % 20 == 0:
            log(f"  {i}/{len(items)} 已抽 {len(out)} 条")
        time.sleep(sleep)
    os.makedirs(os.path.dirname(TEXT_GOLDEN), exist_ok=True)
    with open(TEXT_GOLDEN, "w", encoding="utf-8") as f:
        json.dump({"meta": {"built_at": dt.datetime.now().isoformat(timespec="seconds"),
                            "from": os.path.basename(GOLDEN), "n": len(out), "failed": n_fail,
                            "note": "描述取自 NY 裁定正文的商品描述段（≤400 字，税号已抹去）；"
                                    "描述_subject 为原 subject 口径"},
                   "items": out}, f, ensure_ascii=False, indent=1)
    log(f"正文金标 {len(out)} 条（抽不到 {n_fail}）→ {TEXT_GOLDEN}")
    return out


def _hit(codes, gold, k, lenient=False):
    top = codes[:k]
    if lenient:
        return any(c[:6] == gold[:6] for c in top)
    return gold in top


def eval_recall(items, channels, ks=(5, 20), log=print):
    import core
    import cross
    import rate

    db = core.load_db()
    alive = {c for c in db["rates_8"] if c[:2] not in ("98", "99")}
    stats = {ch: {f"r@{k}": 0 for k in ks} | {f"r6@{k}": 0 for k in ks} | {"可用": 0, "耗时": 0.0}
             for ch in channels + ["融合"]}
    n = len(items)
    t_all = time.time()
    details = []          # 每条的各通道候选列表：离线调融合权重不用再打一遍模型
    for idx, it in enumerate(items, 1):
        desc, gold, number = it["描述"], it["金标"], it["裁定号"]
        per = {}
        if "keyword" in channels:
            t = time.time()
            per["keyword"] = [re.sub(r"\D", "", r["编码"]) for r in rate.search(db, desc, limit=max(ks))]
            stats["keyword"]["耗时"] += time.time() - t
        if "semantic" in channels:
            import hts_embed
            t = time.time()
            r = hts_embed.search_codes(desc, limit=max(ks))
            stats["semantic"]["耗时"] += time.time() - t
            per["semantic"] = [x["编码"] for x in r] if isinstance(r, list) else None

        def votes_excluding_self(q, k, _num=number):
            res = cross.semantic_precedents(q, [], limit=k + 1, alive_codes=alive)
            if isinstance(res, dict) and not res.get("error"):
                res["先例"] = [p for p in res["先例"] if p.get("裁定号") != _num][:k]
            return res

        votes = None
        if "precedent" in channels:
            t = time.time()
            v = cross.code_votes(desc, limit=max(ks), alive_codes=alive, _precedents=votes_excluding_self)
            stats["precedent"]["耗时"] += time.time() - t
            per["precedent"] = [c["编码"] for c in v["候选"]] if not v.get("error") else None
            votes = v
        # 融合：复用各通道结果，不再重复调用模型
        t = time.time()
        rows, _st = rate.hybrid_search(
            db, desc, limit=max(ks), channels=tuple(channels),
            _semantic=(lambda text, lim, _r=per.get("semantic"): (
                [{"编码": c, "相似度": 0} for c in _r] if _r is not None else {"error": "不可用"})),
            _votes=(lambda d, text, lim, _v=votes: _v if _v is not None else {"error": "不可用"}))
        stats["融合"]["耗时"] += time.time() - t
        per["融合"] = [re.sub(r"\D", "", r["编码"]) for r in rows]
        details.append({"裁定号": number, "金标": gold, "描述": desc,
                        "通道": {ch: (codes[:20] if codes is not None else None) for ch, codes in per.items()}})
        for ch, codes in per.items():
            if codes is None:
                continue
            stats[ch]["可用"] += 1
            for k in ks:
                stats[ch][f"r@{k}"] += _hit(codes, gold, k)
                stats[ch][f"r6@{k}"] += _hit(codes, gold, k, lenient=True)
        if idx % 25 == 0 or idx == n:
            log(f"  {idx}/{n}  {time.time() - t_all:.0f}s")
    stats["_明细"] = details
    return stats


def eval_llm(items, log=print, guided=False):
    """
    归类 top-k。guided=True 时每条强制走 GRI 逐级链（classify_product force_guided），
    另报逐步数：品目对 / 下钻后对 / 先例改判 救几坏几；guided=False 时按 ai_config.json 的
    guided 开关决定是否对低置信度行升级（与线上一致）。留一法：裁定号本身从召回投票与
    逐级链的三条先例路径里全部剔除。
    """
    import ai
    import core

    db = core.load_db()
    if ai.get_provider() is None:
        return {"error": ai._PROVIDER_CACHE.get("error") or "AI 未配置"}
    import cross
    import rate

    alive = {c for c in db["rates_8"] if c[:2] not in ("98", "99")}
    res = {"n": 0, "top1": 0, "top3": 0, "top1_6": 0, "错误": 0, "耗时": 0.0, "明细": [],
           "升级": 0, "品目对": 0, "下钻对": 0, "先例改判救": 0, "先例改判坏": 0, "guided": bool(guided)}
    orig_votes = rate._default_votes
    for it in items:
        # 留一法同样适用于归类：先例通道要剔除这条裁定自己，否则 top-1 是漏答出来的
        def _votes(d, text, limit, _num=it["裁定号"]):
            def _prec(q, k):
                r = cross.semantic_precedents(q, [], limit=k + 1, alive_codes=alive)
                if isinstance(r, dict) and not r.get("error"):
                    r["先例"] = [p for p in r["先例"] if p.get("裁定号") != _num][:k]
                return r
            return cross.code_votes(text, limit=limit, alive_codes=alive, _precedents=_prec)
        rate._default_votes = _votes
        t = time.time()
        try:
            out = ai.classify_product(db, it["描述"], origin="CN", force_guided=guided,
                                      exclude_ruling=it["裁定号"])
        finally:
            rate._default_votes = orig_votes
        res["耗时"] += time.time() - t
        res["n"] += 1
        if "error" in out:
            res["错误"] += 1
            res["明细"].append({"裁定号": it["裁定号"], "金标": it["金标"], "结果": "error", "错误": out["error"][:80]})
            continue
        picks = [re.sub(r"\D", "", c["编码"]) for c in out["candidates"]]
        res["top1"] += bool(picks) and picks[0] == it["金标"]
        res["top3"] += it["金标"] in picks[:3]
        res["top1_6"] += bool(picks) and picks[0][:6] == it["金标"][:6]
        row = {"裁定号": it["裁定号"], "描述": it["描述"][:60], "金标": it["金标"], "结果": picks[:3],
               "归类方式": out.get("归类方式", "平铺")}
        arg = out.get("论证") or {}
        if out.get("归类方式") == "逐级" and arg:
            # 逐步拆解：品目步对不对、下钻后对不对、先例步改判是救是坏
            res["升级"] += 1
            res["品目对"] += arg.get("品目") == it["金标"][:4]
            desc_code = re.sub(r"\D", "", str(arg.get("下钻编码") or ""))
            res["下钻对"] += desc_code == it["金标"]
            if (arg.get("先例核对") or {}).get("改判"):
                final_ok = bool(picks) and picks[0] == it["金标"]
                res["先例改判救"] += final_ok and desc_code != it["金标"]
                res["先例改判坏"] += (not final_ok) and desc_code == it["金标"]
            row.update({"品目": arg.get("品目"), "下钻编码": desc_code, "平铺结果": out.get("平铺结果", ""),
                        "先例改判": (arg.get("先例核对") or {}).get("改判", "")})
        elif out.get("升级失败"):
            row["升级失败"] = out["升级失败"][:80]
        res["明细"].append(row)
        log(f"  {res['n']}/{len(items)} top1={res['top1']} top3={res['top3']}")
    return res


def _fmt_table(stats, n):
    lines = [f"{'通道':<10}{'可用':>6}{'r@5':>8}{'r@20':>8}{'6位r@5':>9}{'6位r@20':>9}{'平均耗时':>9}"]
    for ch, s in stats.items():
        if ch.startswith("_"):
            continue
        m = s["可用"] or 1
        lines.append(f"{ch:<10}{s['可用']:>6}{s['r@5']/m:>8.2f}{s['r@20']/m:>8.2f}"
                     f"{s['r6@5']/m:>9.2f}{s['r6@20']/m:>9.2f}{s['耗时']/m*1000:>8.0f}ms")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="归类召回 / 归类结果评测")
    ap.add_argument("--build", type=int, metavar="N", help="从 cross.db 抽 N 条金标集")
    ap.add_argument("--since", default="2023-01-01")
    ap.add_argument("--limit", type=int, default=0, help="只评前 N 条")
    ap.add_argument("--channels", default="keyword,semantic,precedent")
    ap.add_argument("--llm", action="store_true", help="再跑 classify_product 的 top-k（慢）")
    ap.add_argument("--guided", action="store_true",
                    help="配合 --llm：每条强制走 GRI 逐级链（注释→品目→子目→先例），另报逐步数（更慢、约 3 倍 token）")
    ap.add_argument("--build-text", action="store_true",
                    help="给金标集配裁定正文的商品描述段（联网抓 CROSS，永久缓存）")
    ap.add_argument("--golden", default="", help="评测用的金标文件（默认 subject 口径；--text 用正文口径）")
    ap.add_argument("--text", action="store_true", help="用正文金标（data/eval/rulings_golden_text.json）评测")
    a = ap.parse_args(argv)
    if a.build:
        build_golden(a.build, since=a.since)
        return 0
    if a.build_text:
        build_text_golden()
        return 0
    golden = a.golden or (TEXT_GOLDEN if a.text else GOLDEN)
    if not os.path.exists(golden):
        print(f"金标集不存在：{golden}，先 --build N（正文口径再 --build-text）")
        return 1
    with open(golden, encoding="utf-8") as f:
        items = json.load(f)["items"]
    print(f"金标：{os.path.basename(golden)}")
    if a.limit:
        items = items[:a.limit]
    channels = [c.strip() for c in a.channels.split(",") if c.strip()]
    print(f"评测 {len(items)} 条，通道 {channels}")
    stats = eval_recall(items, channels)
    print(_fmt_table(stats, len(items)))
    details = stats.pop("_明细", [])
    report = {"at": dt.datetime.now().isoformat(timespec="seconds"), "n": len(items),
              "golden": os.path.basename(golden), "channels": channels, "recall": stats, "明细": details}
    if a.llm or a.guided:
        print("\n归类 top-k（classify_product" + ("，强制逐级链" if a.guided else "") + "）：")
        report["llm"] = eval_llm(items, guided=a.guided)
        r = report["llm"]
        if "error" not in r:
            m = r["n"] or 1
            print(f"  n={r['n']} top1={r['top1']/m:.2f} top3={r['top3']/m:.2f} "
                  f"6位top1={r['top1_6']/m:.2f} 错误={r['错误']} 平均 {r['耗时']/m:.1f}s")
            if r.get("升级"):
                print(f"  逐级链 {r['升级']} 条：品目对 {r['品目对']}  下钻后8位对 {r['下钻对']}  "
                      f"先例改判 救 {r['先例改判救']} / 坏 {r['先例改判坏']}")
        else:
            print("  ", r["error"])
    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, f"eval_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print(f"报告：{out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
