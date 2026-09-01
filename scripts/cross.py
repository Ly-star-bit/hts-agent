# -*- coding: utf-8 -*-
"""
cross.py —— CBP 裁定先例检索（CROSS）

本地税则库只有品名文本，而决定归类的章注/类注/GRI 都不在里面（每张归类卡片
底下那句免责说的就是这件事）。CROSS 是 CBP 自己的裁定库——22 万条、每日增量，
里面写着**海关实际把什么货判给了什么编码**。真人归类时在这一步花的时间最多，
因为先例常常直接给答案：8506 vs 8507 这个"能不能充电"的分界，CBP 在 N286124
里已经白纸黑字判过（不可充电锂电池 → 8506.50.0000，可充电锂离子 → 8507.60.0020）。

接口是 rulings.cbp.gov 前端在用的那套，公开、无需认证，数据属公共领域
（data.gov 标注 usa.gov/government-works）。但它**没有公开文档、没有 SLA**，
因此本模块的所有函数：

  - 绝不抛异常，失败返回 {"error": ...}，由调用方静默降级（与 ai.py 同样的约定）
  - 默认 10 秒超时，结果落本地缓存，失败也短暂缓存以免离线时每次都干等
  - 只做检索与标注，不改写、不总结裁定内容

关于第三条：裁定正文必须原样呈现。先例的全部价值就在于它是 CBP 的原话，
一经转述就不能拿去跟海关讲了——这与"税率、判定条件均来自本地官方原文"是同一条原则。

【引用先例前必须知道的两件事】
  1. 裁定会被撤销/修改。引用一条已撤销的裁定比不引用更糟，它会让整份归类论证
     失去可信度。本模块把 revokedBy / modifiedBy / operationallyRevoked 归一为
     「状态」字段，失效的必须在界面上显著标注。
  2. 裁定只对申请人的那笔交易有法律约束力。他人可作为论证依据参考，
     但**不是保护伞**——货物有差异时结论未必适用。

命令行自查：
    python scripts/cross.py "lithium ion battery"
    python scripts/cross.py "lithium battery" --codes 8507.60.00,8506.50.00
"""
import hashlib
import json
import os
import re
import time

import httpx

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(BASE_DIR, ".cache", "cross")

BASE_URL = "https://rulings.cbp.gov"
RULING_URL = BASE_URL + "/ruling/{number}"

TIMEOUT = 10.0
# 裁定库是每日增量、几乎只增不改，缓存一周足够；失效标记的变动由 TTL 自然带出
CACHE_TTL = 7 * 24 * 3600
# 失败也缓存一小会儿：离线或对方故障时，别让用户每点一次都干等一个超时
ERROR_TTL = 120
# 实测 pageSize=500 可用；取 100 是因为召回后还要按编码过滤，再多也进不了界面
DEFAULT_PAGE_SIZE = 100


class CrossError(Exception):
    """CROSS 接口调用失败（仅在模块内部流转，不向调用方抛出）"""


# ---------- 本地缓存 ----------

def _cache_key(path, params):
    raw = path + "?" + json.dumps(params, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _cache_read(key):
    """返回 (命中, 内容)。文件损坏按未命中处理，不让坏缓存变成坏答案。"""
    fp = os.path.join(CACHE_DIR, key + ".json")
    try:
        with open(fp, encoding="utf-8") as f:
            blob = json.load(f)
        ttl = ERROR_TTL if blob.get("error") else CACHE_TTL
        if time.time() - float(blob.get("ts", 0)) > ttl:
            return False, None
        return True, blob
    except (OSError, ValueError, TypeError):
        return False, None


def _cache_write(key, blob):
    """临时文件 + rename 原子写入；缓存写不进去不算错误，跳过即可"""
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        fp = os.path.join(CACHE_DIR, key + ".json")
        tmp = fp + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(blob, f, ensure_ascii=False)
        os.replace(tmp, fp)
    except OSError:
        pass


def clear_cache():
    """测试与手工清理用"""
    try:
        for name in os.listdir(CACHE_DIR):
            if name.endswith((".json", ".tmp")):
                os.remove(os.path.join(CACHE_DIR, name))
    except OSError:
        pass


# ---------- HTTP ----------

def _get(path, params):
    """
    发一次 GET，返回解析后的 JSON。失败抛 CrossError。

    单独抽出来是为了让测试能整体替换掉网络层——测试不该依赖 CBP 在线，
    否则它测的是网络而不是本模块的逻辑。
    """
    try:
        resp = httpx.get(BASE_URL + path, params=params, timeout=TIMEOUT,
                         headers={"Accept": "application/json"})
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as e:
        raise CrossError(f"CROSS 接口请求失败：{e}") from e
    except ValueError as e:
        raise CrossError(f"CROSS 返回的不是合法 JSON：{e}") from e


# ---------- 归一化 ----------

def _norm_code(text):
    """'8507.60.0020' → '8507600020'；用于前缀比对"""
    return re.sub(r"\D", "", str(text or ""))


def _fmt8(digits):
    """'85078080' → '8507.80.80'，与本项目其余各处的编码显示保持一致"""
    d = _norm_code(digits)[:8]
    return f"{d[:4]}.{d[4:6]}.{d[6:8]}" if len(d) == 8 else d


def _split_tariffs(text):
    """CBP 的 tariffs 是逗号分隔的一串，可能为空字符串"""
    return [t.strip() for t in str(text or "").split(",") if t.strip()]


def _status(raw):
    """
    撤销/修改状态归一。

    三个来源字段含义不同：revokedBy 是被后续裁定明文撤销；operationallyRevoked
    是 CBP 标注的"实际上已不适用"；modifiedBy 是被修改（部分结论仍有效，
    但必须连同修改件一起读）。撤销优先于修改展示——前者直接不能引用。
    """
    revoked_by = [str(x) for x in (raw.get("revokedBy") or [])]
    modified_by = [str(x) for x in (raw.get("modifiedBy") or [])]
    if revoked_by:
        return "已撤销", f"已被 {'、'.join(revoked_by)} 撤销，不可作为归类依据"
    if raw.get("operationallyRevoked"):
        return "已撤销", "CBP 标注为实际已撤销，不可作为归类依据"
    if modified_by:
        return "已修改", f"已被 {'、'.join(modified_by)} 修改，须连同修改件一并阅读"
    return "现行", ""


def _norm_ruling(raw):
    """CROSS 原始记录 → 本项目风格的结果行"""
    number = str(raw.get("rulingNumber") or "")
    collection = str(raw.get("collection") or "").lower()
    date = str(raw.get("rulingDate") or "")[:10]
    state, note = _status(raw)
    tariffs = _split_tariffs(raw.get("tariffs"))
    year = date[:4]
    return {
        "裁定号": number,
        "日期": date,
        "来源": {"ny": "NY", "hq": "HQ"}.get(collection, collection.upper()),
        "主题": str(raw.get("subject") or ""),
        "类别": str(raw.get("categories") or ""),
        "编码": tariffs,
        "编码数字": [_norm_code(t) for t in tariffs],
        "状态": state,
        "状态说明": note,
        "相关裁定": [str(x) for x in (raw.get("relatedRulings") or [])],
        "链接": RULING_URL.format(number=number),
        # 全文是 OLE2 二进制 .doc，不是 HTML；界面上应作为下载链接而非内嵌渲染
        "全文链接": (f"{BASE_URL}/api/getdoc/{collection}/{year}/{number}.doc"
                     if number and collection and year else ""),
    }


# ---------- 检索 ----------

def search(term, page_size=DEFAULT_PAGE_SIZE, page=1, collection=None,
           sort_by="RELEVANCE", use_cache=True):
    """
    按关键词检索 CBP 裁定。

    注意 term 走的是**全文检索**，不是结构化的编码字段查询：搜 '8507.60.00'
    命中的是正文里提到该编码的裁定（多为 protest、drawback 之类），它们的
    tariffs 字段往往是空的，并不是"归到这个码"的裁定。要找某编码的先例，
    应当用商品英文名检索、再按 tariffs 过滤——见 precedents()。

    返回 {"检索词", "命中总数", "裁定": [...]}；失败返回 {"error": ...}。
    """
    term = (term or "").strip()
    if not term:
        return {"error": "请提供检索词"}

    params = {"term": term, "pageSize": int(page_size), "page": int(page),
              "sortBy": sort_by}
    if collection:
        params["collection"] = collection

    key = _cache_key("/api/search", params)
    if use_cache:
        hit, blob = _cache_read(key)
        if hit:
            return dict(blob["payload"]) if not blob.get("error") else {"error": blob["error"]}

    try:
        data = _get("/api/search", params)
    except Exception as e:
        # 这里刻意捕获 Exception 而非只捕 CrossError。本模块对调用方的承诺是
        # "绝不抛异常"——CROSS 是锦上添花，本地税则查询才是主链路，不能因为
        # 一个未公开接口返回了意料之外的东西就把整个搜索页打断。
        msg = str(e) if isinstance(e, CrossError) else f"CROSS 接口异常：{e}"
        # 失败也写缓存（短 TTL），避免离线时每次点击都干等一个超时
        if use_cache:
            _cache_write(key, {"ts": time.time(), "error": msg})
        return {"error": msg}

    payload = {
        "检索词": term,
        "命中总数": int(data.get("totalHits") or 0),
        "裁定": [_norm_ruling(r) for r in (data.get("rulings") or [])],
    }
    if use_cache:
        _cache_write(key, {"ts": time.time(), "payload": payload})
    return payload


def match_codes(ruling, codes):
    """
    这条裁定判给的编码里，有哪些落在 codes（8 位）之下。

    CBP 写的是 10 位统计编码（8507.60.0020），本地候选是 8 位（8507.60.00），
    因此按数字前缀比对。反向也要支持：裁定里也有只写到 8 位的老记录。
    """
    out = []
    for want in codes:
        w = _norm_code(want)[:8]
        if len(w) < 8:
            continue
        for got in ruling.get("编码数字", []):
            if got.startswith(w) or w.startswith(got):
                out.append(want)
                break
    return out


def precedents(term, codes=None, limit=20, page_size=DEFAULT_PAGE_SIZE,
               use_cache=True):
    """
    给定商品英文名与本地候选编码，返回 CBP 对同类商品实际判过的先例。

    结果分两组，两组都有用：
      - 命中候选：CBP 把同类货判给了你正在考虑的某个编码 → 直接的论证依据
      - 候选外品目：CBP 把同类货判到了你没考虑的品目上 → 这是**归类信号**，
        说明候选集可能漏了东西，值得回头看一眼

    失效的裁定不丢弃、但排在后面并带状态标注：知道"这条曾经这么判、后来被撤销"
    本身是有价值的，直接隐藏反而会让用户以为无先例。

    返回 {"检索词","命中总数","先例","候选外品目","提示"}；失败返回 {"error": ...}。
    """
    res = search(term, page_size=page_size, use_cache=use_cache)
    if "error" in res:
        return res

    codes = [c for c in (codes or []) if _norm_code(c)]
    cand_headings = {_norm_code(c)[:4] for c in codes}
    matched, others = [], {}
    for r in res["裁定"]:
        if not r["编码"]:
            continue          # 无编码的多为原产地/退税裁定，与归类无关
        hits = match_codes(r, codes) if codes else []
        if hits or not codes:
            row = dict(r)
            row["命中候选"] = hits
            matched.append(row)
        else:
            # 按 8 位归组而不是 4 位品目：8507.80.80 与候选 8507.60.00 同品目、
            # 不同子目，按 4 位归组会显示成"候选外品目 8507"——而 8507 明明就是
            # 候选所在的品目，看着像自相矛盾。真正的信息是"CBP 判到了同品目下
            # 另一个子目"，这恰恰比跨品目更值得看一眼。
            for c8 in {c[:8] for c in r["编码数字"] if len(c) >= 8}:
                if c8.startswith(("98", "99")):
                    continue   # 9903 加征条款不是归类结论，计进来会淹没真信号
                g = others.setdefault(c8, {
                    "编码": _fmt8(c8),
                    "同品目": c8[:4] in cand_headings,
                    "裁定数": 0, "示例": [],
                })
                g["裁定数"] += 1
                if len(g["示例"]) < 3:
                    g["示例"].append({"裁定号": r["裁定号"], "主题": r["主题"][:80]})

    # 只把失效的挪到后面。Python 的 sort 是稳定的，因此现行组内部仍保持 CROSS
    # 返回的相关度顺序——那是对方的排序结果，比按日期重排有用得多
    #（早先多写了一次按日期排序，把相关度整个冲掉了，最贴题的 N286124 反而掉出前列）。
    matched.sort(key=lambda r: r["状态"] != "现行")
    matched = matched[:limit]

    # 同品目的排前面：它离候选最近，最可能是你漏掉的那个子目
    other_list = sorted(others.values(),
                        key=lambda g: (not g["同品目"], -g["裁定数"]))[:5]

    tips = []
    if not matched and codes:
        tips.append("本次检索未找到判给这些候选编码的裁定。可能是检索词与官方用语不一致，"
                    "换用税则原文里的说法再试（如 lithium-ion / primary cells）。")
    if any(r["状态"] != "现行" for r in matched):
        tips.append("结果中含已撤销或已修改的裁定，已标注状态——引用前务必核对，"
                    "撤销件不可作为归类依据。")
    if other_list:
        tips.append("「候选外编码」是 CBP 把同类商品判到的、不在你候选集里的编码；"
                    "标「同品目」的与候选同 4 位品目、仅子目不同，最值得先看。")
    tips.append("裁定仅对申请人的该笔交易具法律约束力，他人可参考但不构成保护伞；"
                "正式归类以针对本批货物的 CBP 裁定为准。")

    return {
        "检索词": term,
        "命中总数": res["命中总数"],
        "先例": matched,
        "候选外编码": other_list,
        "提示": " ".join(tips),
    }


# ---------- 命令行自查 ----------

def _main(argv):
    import argparse

    ap = argparse.ArgumentParser(description="CBP 裁定先例检索（CROSS）")
    ap.add_argument("term", help="商品英文名或关键词")
    ap.add_argument("--codes", default="", help="本地候选编码，逗号分隔")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--no-cache", action="store_true")
    a = ap.parse_args(argv)

    codes = [c.strip() for c in a.codes.split(",") if c.strip()]
    r = precedents(a.term, codes, limit=a.limit, use_cache=not a.no_cache)
    if "error" in r:
        print("失败：" + r["error"])
        return 1

    print(f"检索词「{r['检索词']}」 命中 {r['命中总数']} 条，展示 {len(r['先例'])} 条\n")
    for it in r["先例"]:
        flag = "" if it["状态"] == "现行" else f"  ⚠ {it['状态']}"
        print(f"{it['裁定号']:9s} {it['日期']} {it['来源']:2s}{flag}")
        print(f"  编码: {', '.join(it['编码'])}")
        if it["命中候选"]:
            print(f"  命中候选: {', '.join(it['命中候选'])}")
        print(f"  {it['主题'][:88]}")
        if it["状态说明"]:
            print(f"  {it['状态说明']}")
        print(f"  {it['链接']}")
        print()
    if r["候选外编码"]:
        print("候选外编码（CBP 把同类商品判到的、不在候选集里的编码）：")
        for g in r["候选外编码"]:
            same = "同品目" if g["同品目"] else "  跨品目"
            print(f"  {g['编码']}  {same}  {g['裁定数']} 条  例：{g['示例'][0]['主题'][:56]}")
        print()
    print(r["提示"])
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main(sys.argv[1:]))
