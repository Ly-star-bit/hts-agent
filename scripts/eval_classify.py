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


def eval_llm(items, log=print):
    import ai
    import core

    db = core.load_db()
    if ai.get_provider() is None:
        return {"error": ai._PROVIDER_CACHE.get("error") or "AI 未配置"}
    import cross
    import rate

    alive = {c for c in db["rates_8"] if c[:2] not in ("98", "99")}
    res = {"n": 0, "top1": 0, "top3": 0, "top1_6": 0, "错误": 0, "耗时": 0.0, "明细": []}
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
            out = ai.classify_product(db, it["描述"], origin="CN")
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
        res["明细"].append({"裁定号": it["裁定号"], "描述": it["描述"][:60], "金标": it["金标"],
                          "结果": picks[:3]})
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
    a = ap.parse_args(argv)
    if a.build:
        build_golden(a.build, since=a.since)
        return 0
    if not os.path.exists(GOLDEN):
        print(f"金标集不存在：{GOLDEN}，先 --build N")
        return 1
    with open(GOLDEN, encoding="utf-8") as f:
        items = json.load(f)["items"]
    if a.limit:
        items = items[:a.limit]
    channels = [c.strip() for c in a.channels.split(",") if c.strip()]
    print(f"评测 {len(items)} 条，通道 {channels}")
    stats = eval_recall(items, channels)
    print(_fmt_table(stats, len(items)))
    details = stats.pop("_明细", [])
    report = {"at": dt.datetime.now().isoformat(timespec="seconds"), "n": len(items),
              "channels": channels, "recall": stats, "明细": details}
    if a.llm:
        print("\n归类 top-k（classify_product）：")
        report["llm"] = eval_llm(items)
        r = report["llm"]
        if "error" not in r:
            m = r["n"] or 1
            print(f"  n={r['n']} top1={r['top1']/m:.2f} top3={r['top3']/m:.2f} "
                  f"6位top1={r['top1_6']/m:.2f} 错误={r['错误']} 平均 {r['耗时']/m:.1f}s")
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
