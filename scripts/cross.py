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

# 缓存里存的是归一化**之后**的行，因此行结构一变，旧缓存就是带着旧字段的
# 定时炸弹（加「版本提示」那次，旧条目缺字段会让 precedents() 直接 KeyError）。
# 把结构版本编进缓存键：结构升级 → 旧条目自然失配 → 当作未命中重新拉取。
_CACHE_SCHEMA = 2


def _cache_key(path, params):
    raw = f"v{_CACHE_SCHEMA}:" + path + "?" + json.dumps(params, sort_keys=True,
                                                         ensure_ascii=False)
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


# HS 每 5 年一次大修，6 位编码会被 WCO 改动（HS 2022 大改的恰恰是电子、LED、
# 多功能设备——最需要查先例的那类货）。老裁定的归类**逻辑**通常仍然成立，
# 但它写下的编码可能已不存在或含义已变。关键在于：CROSS 不会把这种情况标成
# "已撤销"——revoked 标记只防"结论被推翻"，防不住"编码被搬家"。
# 日期取美国 HTS 实施日而非 WCO 名义生效日（如 HS 2022 经第 10326 号总统公告
# 于 2022-01-27 落地），因为裁定引用的是 HTSUS。ISO 日期字符串可直接比较。
_HS_REVISIONS = [
    ("2022-01-27", "HS 2022"),
    ("2017-01-01", "HS 2017"),
    ("2012-02-03", "HS 2012"),
]


def _hs_version_note(date):
    """裁定日期早于 HS 修订时给出提示；日期缺失/异常按最老处理（宁可多提醒）"""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(date or "")):
        missed = [name for _, name in _HS_REVISIONS]
    else:
        missed = [name for cutoff, name in _HS_REVISIONS if date < cutoff]
    if not missed:
        return ""
    return (f"该裁定早于 {'、'.join(reversed(missed))} 修订，其中的 6 位编码"
            "可能已变更或删除，引用前须回现行税则核对编码仍然存在")


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
        "版本提示": _hs_version_note(date),
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


def _flag_dead_codes(items, alive_codes):
    """
    与现行税则对账：裁定引用的编码若已不在今天的 HTS 里，逐条标进「失效编码」。

    为什么必须有这一列：撤销有红标（revokedBy），编码被 HS/HTS 修订改号却
    **什么标都没有**——CROSS 不会为此回头撤销裁定。全库实测（2026-09）：
    90 年代裁定引用的编码 42% 已不在现行税则，2000 年代 28%，2010 年代 17%。
    用户看到一条"现行"的老裁定就抄编码，抄到的可能是十年前就删掉的号。

    alive_codes 是现行 8 位码集合（rates_8 的键）。只核 8 位及以上、非 98/99
    章的编码；短码（老裁定只写到 6 位）无法与 8 位表对账，不标——宁可漏标
    也不误标，误标会教用户不信任这个警告。

    返回是否有任何失效编码（调用方据此追加提示行）。
    """
    if not alive_codes:
        return False
    any_dead = False
    for it in items:
        dead = []
        for disp, digits in zip(it["编码"], it["编码数字"]):
            d8 = digits[:8]
            if (len(digits) >= 8 and not d8.startswith(("98", "99"))
                    and d8 not in alive_codes):
                dead.append(disp)
        it["失效编码"] = dead
        any_dead = any_dead or bool(dead)
    return any_dead


_DEAD_CODE_TIP = ("部分裁定引用的编码已不在现行税则（已标注）——税则修订不会触发"
                  "撤销标记，归类思路可参考，编码必须以现行税则重新落位。")


def precedents(term, codes=None, limit=20, page_size=DEFAULT_PAGE_SIZE,
               use_cache=True, alive_codes=None):
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

    # 排序：现行在前 > HQ 在前。多条裁定结论冲突时该信哪条，层级说了算——
    # HQ（总部法规裁定办公室）可以撤销/修改 NY（纽约商品专家部）的裁定，反之
    # 不行（实测 2399 条样本里 25 次撤销全部由 HQ 发起，NY 撤销任何裁定 0 次）。
    # Python 的 sort 是稳定的，因此各组内部仍保持 CROSS 返回的相关度顺序——
    # 那是对方的排序结果，比按日期重排有用得多（早先多写了一次按日期排序，
    # 把相关度整个冲掉了，最贴题的 N286124 反而掉出前列）。
    matched.sort(key=lambda r: (r["状态"] != "现行", r["来源"] != "HQ"))
    matched = matched[:limit]

    # 同品目的排前面：它离候选最近，最可能是你漏掉的那个子目
    other_list = sorted(others.values(),
                        key=lambda g: (not g["同品目"], -g["裁定数"]))[:5]

    tips = []
    if not matched and codes:
        tips.append("本次检索未找到判给这些候选编码的裁定。可能是检索词与官方用语不一致，"
                    "换用税则原文里的说法再试（如 lithium-ion / primary cells）。"
                    "确属无先例的新品类且金额较大时，建议申请 CBP 预裁定"
                    "（eRulings，免费，约 30 天），而不是回头硬翻税则猜一个。")
    if matched:
        # 这句在有先例时必须在：subject + tariffs 两行看着就能抄，而裁定的法律
        # 效力锚定在它描述的那个具体货物上，差一个参数（容量/材质比例/是否
        # 零售包装）结论就可能翻转。列表页的信息量天然在鼓励"扫一眼就抄"，
        # 提示必须与之对冲。
        tips.append("引用先例前须读裁定全文的事实描述段，确认货物与本批实际可比——"
                    "相似不等于相同，差一个参数结论可能相反。多条结论冲突时，"
                    "HQ 层级高于 NY，新裁定优于旧裁定。")
    if any(r["状态"] != "现行" for r in matched):
        tips.append("结果中含已撤销或已修改的裁定，已标注状态——引用前务必核对，"
                    "撤销件不可作为归类依据。")
    if any(r["版本提示"] for r in matched):
        tips.append("部分裁定早于 HS 修订（已标注）：归类逻辑通常仍成立，但其中的"
                    "6 位编码可能已变更——CROSS 不会为此标记撤销，须自行回现行税则核对。")
    if _flag_dead_codes(matched, alive_codes):
        tips.append(_DEAD_CODE_TIP)
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


# ---------- 本地镜像（cross_sync.py 构建的 SQLite，一期） ----------

DB_PATH = os.path.join(BASE_DIR, "data", "cross.db")


def db_available(db_path=None):
    return os.path.exists(db_path or DB_PATH)


def _open_ro(db_path):
    import sqlite3
    # 只读打开：查询路径绝不该有机会写库，同步是 cross_sync 的事
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def _row_to_raw(row):
    """库行 → API 原始记录形态，喂给 _norm_ruling 复用同一套归一/状态/版本逻辑。
    状态与版本提示在读取时计算而非入库时固化——判定规则改了，老数据自动跟上。"""
    (number, date, collection, subject, categories, tariffs,
     revoked_by, modified_by, op_revoked, related) = row
    return {
        "rulingNumber": number, "rulingDate": date, "collection": collection,
        "subject": subject, "categories": categories, "tariffs": tariffs,
        "revokedBy": json.loads(revoked_by or "[]"),
        "modifiedBy": json.loads(modified_by or "[]"),
        "operationallyRevoked": bool(op_revoked),
        "relatedRulings": json.loads(related or "[]"),
    }


_RULING_COLS = ("number,date,collection,subject,categories,tariffs,"
                "revoked_by,modified_by,op_revoked,related")


def local_status(db_path=None):
    """镜像状态：给界面标注"数据截至何时"用。不可用返回 {'可用': False}。"""
    # DB_PATH 在调用时解析而非默认参数里绑定：默认参数在 def 时求值，
    # 测试替换 cross.DB_PATH 会悄悄不生效
    db_path = db_path or DB_PATH
    if not db_available(db_path):
        return {"可用": False}
    try:
        conn = _open_ro(db_path)
        try:
            meta = dict(conn.execute("SELECT key, value FROM meta"))
            n = conn.execute("SELECT COUNT(*) FROM rulings").fetchone()[0]
        finally:
            conn.close()
        return {"可用": True, "条数": n,
                "上次同步": meta.get("last_sync", ""),
                "服务端条数": meta.get("server_count", ""),
                "失败切片": int(meta.get("failed_slices") or 0)}
    except Exception:
        return {"可用": False}


def code_precedents(codes, limit=20, db_path=None, alive_codes=None):
    """
    本地镜像反查：这些候选编码历史上的**全部**先例。

    这是镜像存在的理由——API 的 term 全文检索答不了这个问题（命中的是正文
    提到编码的 protest/drawback 案），客户端过滤又只能看见拉回的前几页。
    本地全量 tariffs 索引给出的是完整答案：「8506.50.00 共 N 条先例」的 N
    是精确计数，不是"检索到的前 N 条"。

    与 precedents()（在线检索）的关系是互补不是替代：这里按编码精确反查、
    离线、毫秒级；那里按商品词模糊召回、能发现候选外编码。排序上本地没有
    相关度可用，按 现行 > HQ > 日期新 排列。

    返回 {"先例","每码先例数","数据截至","提示"}；失败返回 {"error": ...}。
    """
    db_path = db_path or DB_PATH   # 调用时解析，测试可替换 cross.DB_PATH
    codes = [c for c in (codes or []) if len(_norm_code(c)) >= 8]
    if not codes:
        return {"error": "请提供至少一个 8 位候选编码"}
    if not db_available(db_path):
        return {"error": "本地裁定库未构建，请运行 python scripts/cross_sync.py"}
    try:
        conn = _open_ro(db_path)
        try:
            numbers, counts = set(), {}
            for c in codes:
                w = _norm_code(c)[:8]
                # 两段匹配与 match_codes 同口径：
                #   BETWEEN 段抓"等于 w 或以 w 开头的 10 位统计码"
                #   IN 段抓"比 w 短、且是 w 前缀的老编码"（如只写到 6 位的裁定）
                prefixes = [w[:n] for n in range(4, 8)]
                rows = conn.execute(
                    f"SELECT DISTINCT number FROM ruling_codes "
                    f"WHERE (code BETWEEN ? AND ? || '9999') "
                    f"   OR code IN ({','.join('?' * len(prefixes))})",
                    [w, w] + prefixes).fetchall()
                numbers.update(r[0] for r in rows)
                got = conn.execute(
                    "SELECT n FROM code8_counts WHERE code8=?", (w,)).fetchone()
                counts[_fmt8(w)] = got[0] if got else 0

            items = []
            if numbers:
                ph = ",".join("?" * len(numbers))
                for row in conn.execute(
                        f"SELECT {_RULING_COLS} FROM rulings WHERE number IN ({ph})",
                        list(numbers)):
                    it = _norm_ruling(_row_to_raw(row))
                    it["命中候选"] = match_codes(it, codes)
                    items.append(it)
            meta = dict(conn.execute("SELECT key, value FROM meta"))
        finally:
            conn.close()
    except Exception as e:
        return {"error": f"本地裁定库读取失败：{e}"}

    # 现行 > HQ > 日期新。本地没有相关度可用，日期是唯一合理的组内次序；
    # 两次稳定排序：先排日期，再按（失效、非HQ）分层，层内保持日期序。
    # 失效的保留但沉底——藏掉会让用户以为无先例
    items.sort(key=lambda r: r["日期"], reverse=True)
    items.sort(key=lambda r: (r["状态"] != "现行", r["来源"] != "HQ"))
    shown = items[:limit]

    tips = ["以上为本地镜像的完整反查（该编码历史上的全部归类先例），"
            f"数据截至 {meta.get('last_sync', '未知')}。"]
    if _flag_dead_codes(shown, alive_codes):
        tips.append(_DEAD_CODE_TIP)
    if any(r["状态"] != "现行" for r in shown):
        tips.append("含已撤销/修改的裁定（已标注），撤销件不可作为归类依据。")
    if any(r["版本提示"] for r in shown):
        tips.append("部分裁定早于 HS 修订（已标注），6 位编码可能已变更，"
                    "CROSS 不会为此标记撤销，须回现行税则核对。")
    tips.append("引用前须读裁定全文的事实描述段，确认货物可比；"
                "裁定仅对申请人的该笔交易具法律约束力，不构成保护伞。")
    return {"先例": shown, "每码先例数": counts,
            "数据截至": meta.get("last_sync", ""), "提示": " ".join(tips)}


def semantic_precedents(query, codes=None, limit=10, db_path=None,
                        alive_codes=None, _embed=None):
    """
    语义找先例（二期）：中文/英文商品描述 → 向量检索裁定 subject。

    与 code_precedents（编码精确反查）和 precedents（在线关键词）互补：
    这里解决的是"入口是模糊描述"——「羊毛大衣」不需要先翻成 wool coat。
    查询侧加 instruct 前缀、文档侧存纯文本，与选型基准的做法一致；
    嵌入模型/维度以 meta 里索引时记录的为准，查询必须用同一个模型。

    codes 只用于标「命中候选」，不过滤——语义检索的价值恰恰在发现候选外的判法。
    _embed 参数供测试注入假嵌入器。

    返回 {"先例","检索词","数据截至","提示"}；失败一律 {"error": ...}。
    """
    query = (query or "").strip()
    if not query:
        return {"error": "请输入商品描述"}
    db_path = db_path or DB_PATH
    if not db_available(db_path):
        return {"error": "本地裁定库未构建，请运行 python scripts/cross_sync.py"}
    try:
        import sqlite3

        import sqlite_vec
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except Exception as e:
        return {"error": f"语义索引不可用（sqlite-vec）：{e}"}

    try:
        try:
            meta = dict(conn.execute("SELECT key, value FROM meta"))
            model = meta.get("embed_model")
            dims = int(meta.get("embed_dims") or 0)
            n_indexed = int(meta.get("embed_count") or 0)
            if not model or not n_indexed:
                return {"error": "语义索引未构建，请运行 python scripts/cross_embed.py"}

            # 查询嵌入（instruct 前缀）。默认走 ollama，测试注入 _embed
            import struct as _struct

            import cross_embed as _ce
            if _embed is None:
                _embed = lambda texts: _ce.embed_batch(texts, model=model)  # noqa: E731
            qv = _ce._truncate_norm(_embed([_ce.QUERY_INSTRUCT + query])[0], dims)
            rows = conn.execute(
                f"SELECT r.number, v.distance FROM ("
                f"  SELECT rowid, distance FROM vec_subjects "
                f"  WHERE emb MATCH ? ORDER BY distance LIMIT ?) v "
                f"JOIN rulings r ON r.rowid = v.rowid",
                (_struct.pack(f"{dims}f", *qv), int(limit) * 3)).fetchall()

            items = []
            if rows:
                order = {n: i for i, (n, _) in enumerate(rows)}
                ph = ",".join("?" * len(rows))
                for row in conn.execute(
                        f"SELECT {_RULING_COLS} FROM rulings "
                        f"WHERE number IN ({ph})", [n for n, _ in rows]):
                    it = _norm_ruling(_row_to_raw(row))
                    it["命中候选"] = match_codes(it, codes or [])
                    items.append(it)
                items.sort(key=lambda r: order.get(r["裁定号"], 1 << 30))
        finally:
            conn.close()
    except Exception as e:
        # ollama 离线、向量表损坏等一律降级——语义检索是锦上添花
        return {"error": f"语义检索失败：{e}"}

    # 失效沉底，组内保持相似度序（稳定排序）；无编码的裁定（原产地/估价类）
    # 对归类场景噪音居多，往后放但不删——语义近邻本身就是信息
    items.sort(key=lambda r: (r["状态"] != "现行", not r["编码"]))
    items = items[:limit]

    tips = [f"按语义相似检索（{model}，索引 {n_indexed} 条，"
            f"数据截至 {meta.get('last_sync', '未知')}）。"]
    if _flag_dead_codes(items, alive_codes):
        tips.append(_DEAD_CODE_TIP)
    if any(r["状态"] != "现行" for r in items):
        tips.append("含已撤销/修改的裁定（已标注），撤销件不可作为归类依据。")
    tips.append("引用前须读裁定全文的事实描述段，确认货物可比；"
                "裁定仅对申请人的该笔交易具法律约束力，不构成保护伞。")
    return {"先例": items, "检索词": query,
            "数据截至": meta.get("last_sync", ""), "提示": " ".join(tips)}


def precedent_counts(codes, db_path=None):
    """
    批量取 8 位码的先例数（code8_counts 预聚合表的键查），给搜索结果表的
    「先例数」列用。

    返回 {原样传入的编码: 条数}；镜像不可用或读取失败返回 {}——这是增强列，
    缺席不是错误，不该让一列数字的缺失挡住整个搜索。计数为 0 也如实返回 0：
    "查过了没有"和"没查"是两个信息。
    """
    db_path = db_path or DB_PATH
    if not db_available(db_path):
        return {}
    try:
        conn = _open_ro(db_path)
        try:
            out = {}
            for c in codes or []:
                w = _norm_code(c)[:8]
                if len(w) < 8:
                    continue
                row = conn.execute(
                    "SELECT n FROM code8_counts WHERE code8=?", (w,)).fetchone()
                out[c] = row[0] if row else 0
            return out
        finally:
            conn.close()
    except Exception:
        return {}


# ---------- 裁定正文按需拉取（三期：深读的原料） ----------

DOC_CACHE_DIR = os.path.join(BASE_DIR, ".cache", "cross_docs")
# 正文提取的质量断言：抽出的文本必须含"结论句"或 HTS 编码，否则视为解析失败。
# 探针实测归类裁定 ~100% 满足；不满足的多是非归类裁定（原产地/估价）或坏文件，
# 这类退回"仅链接"比展示残缺文本诚实。
_DOC_CONCLUSION = re.compile(
    r"applicable subheading|classifiable under|is provided for in|"
    r"\b\d{4}\.\d{2}\.\d{2,4}\b", re.I)


def _parse_doc_bytes(data):
    """
    裁定文件字节 → 纯文本。按魔数分派：
      PDF（%PDF）        → pdfplumber（项目已有依赖）
      OLE2（D0CF11E0）   → 提取可打印 ASCII 连续串
    2025 年起 CBP 新裁定发 PDF，之前是 OLE2 .doc。两种都实测 ~100% 可读。
    解析不出关键段返回 None——个体失败不猜，退回仅链接。
    """
    if not data:
        return None
    if data[:4] == b"%PDF":
        try:
            import io

            import pdfplumber
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                txt = " ".join((p.extract_text() or "") for p in pdf.pages)
        except Exception:
            return None
    elif data[:4] == b"\xd0\xcf\x11\xe0":
        # OLE2：正文以明文 ASCII 躺在字节流里（TARIFF NO.: 8506.50.0000 直接可见）。
        # 不能先剥 HTML 标签——一个杂散 '<' 到下个 '>' 之间会把大段正文整块删掉
        # （探针里就是这个 bug）。直接抓可打印连续串。
        runs = re.findall(rb"[\x20-\x7e]{4,}", data)
        txt = " ".join(r.decode("ascii", "ignore") for r in runs)
    else:
        return None
    txt = re.sub(r"\s+", " ", txt).strip()
    if len(txt) < 400 or not _DOC_CONCLUSION.search(txt):
        return None
    return txt


def fetch_ruling_text(number, collection, date, use_cache=True):
    """
    按需拉取并解析一条裁定的正文。永久缓存——裁定正文不可变（改判用新裁定号），
    所以缓存没有 TTL，只存看过的那几十条，不做 22 万全量下载。

    返回纯文本；拉取/解析失败返回 None（调用方退回仅给链接）。
    """
    number = str(number or "")
    collection = str(collection or "").lower()
    year = str(date or "")[:4]
    if not (number and collection and year.isdigit()):
        return None

    fp = os.path.join(DOC_CACHE_DIR, f"{number}.txt")
    if use_cache:
        try:
            with open(fp, encoding="utf-8") as f:
                cached = f.read()
            return cached or None
        except OSError:
            pass

    try:
        resp = httpx.get(f"{BASE_URL}/api/getdoc/{collection}/{year}/{number}.doc",
                         timeout=TIMEOUT * 2)   # 正文比元数据大，给更宽超时
        resp.raise_for_status()
        got_file = True
        text = _parse_doc_bytes(resp.content)
    except Exception:
        got_file = False   # 网络失败：不缓存，下次可重试
        text = None

    # 只在"确实拿到了文件"时才写缓存：解析失败缓存空串（标记不可读，不反复重试），
    # 但网络失败不缓存——否则一次断网会把这条永久钉成"无正文"
    if use_cache and got_file:
        try:
            os.makedirs(DOC_CACHE_DIR, exist_ok=True)
            with open(fp, "w", encoding="utf-8") as f:
                f.write(text or "")
        except OSError:
            pass
    return text


def ruling_meta(number, db_path=None):
    """
    裁定号 → (库别, 日期)，从本地镜像取。拉正文要知道 hq/ny 与年份（getdoc 路径按这两个分目录），
    调用方手上若只有裁定号（比如用户从别处粘的），靠这里补。镜像没建/没这条 → None。
    """
    number = str(number or "").strip()
    if not number or not db_available(db_path):
        return None
    try:
        con = _open_ro(db_path or DB_PATH)
        try:
            row = con.execute("SELECT collection, date FROM rulings WHERE number = ?", (number,)).fetchone()
        finally:
            con.close()
    except Exception:
        return None
    if not row or not row[0] or not row[1]:
        return None
    return str(row[0]).lower(), str(row[1])[:10]


# 裁定正文里的固定小节标题（HQ 结构最完整：FACTS / ISSUE / LAW AND ANALYSIS / HOLDING；
# NY 信函体只有 Dear … / Sincerely）。正文来自 .doc 抽取，段落早就被压成一行——
# 站内阅读时按这些标题切回段落，HOLDING 能一眼找到。
_SECTION_HEADS = ("FACTS:", "ISSUE:", "ISSUES:", "LAW AND ANALYSIS:", "ANALYSIS:", "HOLDING:",
                  "EFFECT ON OTHER RULINGS:", "Re:", "RE:", "Sincerely,", "TARIFF NO.:", "CATEGORY:")
# 这些只是段落起点，不是小节标题（"Dear Ms. Ratto:" 拆成 [Dear] + "Ms. Ratto:" 读着别扭）：换段但正文原样保留
_SOFT_HEADS = ("Dear ", "This ruling is being issued", "A copy of this ruling letter")
_SECTION_RE = re.compile("(" + "|".join(re.escape(h) for h in _SECTION_HEADS + _SOFT_HEADS) + ")")


def split_ruling_sections(text):
    """
    纯文本正文 → [{'标题', '内容'}, ...]。标题为 '' 表示开头的信头/杂项。
    OLE2 抽取会在开头带 'bjbj…' 之类的二进制残留，剥掉到裁定号首次出现处。
    """
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if not text:
        return []
    m = re.search(r"\b(HQ|NY)\s+[A-Z]\d{5,6}\b", text)
    if m and m.start() < 200:
        text = text[m.start():]
    # 尾部同样有 Word 域码残留（"PAGE \* MERGEFORMAT hVWD {dP9!…"），从域码起截掉
    m = re.search(r"\bPAGE\s*\\\*\s*MERGEFORMAT", text)
    if m:
        text = text[:m.start()].rstrip()
    parts = _SECTION_RE.split(text)
    out, title = [], ""
    buf = parts[0].strip()
    for i in range(1, len(parts), 2):
        if buf:
            out.append({"标题": title, "内容": buf})
        head, rest = parts[i], parts[i + 1].strip() if i + 1 < len(parts) else ""
        if head in _SOFT_HEADS:
            title, buf = "", re.sub(r"\s+", " ", head + " " + rest).strip()
        else:
            title, buf = head.strip().rstrip(":"), rest
    if buf or title:
        out.append({"标题": title, "内容": buf})
    return out


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
    # 本地税则库可用时顺带做现行编码对账；没建库也不该挡住先例查询
    try:
        import core
        alive = set(core.load_db()["rates_8"].keys())
    except Exception:
        alive = None
    r = precedents(a.term, codes, limit=a.limit, use_cache=not a.no_cache,
                   alive_codes=alive)
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
            print(f"  ⚠ {it['状态说明']}")
        if it["版本提示"]:
            print(f"  ⚠ {it['版本提示']}")
        if it.get("失效编码"):
            print(f"  ⚠ 已不在现行税则: {', '.join(it['失效编码'])}")
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
