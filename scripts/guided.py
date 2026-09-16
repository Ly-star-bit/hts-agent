# -*- coding: utf-8 -*-
"""
guided.py —— GRI 逐级归类链（把人类归类员的流程做成固定顺序的工具链）

【为什么要有它】平铺流水线是"召回 40 条 → 模型挑 3 条"：末级品名大量是 Other，
模型在 Other 与 Other 之间比。2026-09 正文金标 60 条实测，云端模型平铺精排错的
25 条里 23 条 6 位就错——方向错，不是细分错。归类员不这么干：先读类注章注排除，
按 GRI 1 定 4 位品目，再按 GRI 6 在品目内逐级下钻，最后拿先例核对。

【三步，每步一次 JSON 调用，模型只做那一步的判断】
  ① 品目（GRI 1）：召回候选按 4 位品目分组 + 涉及章的全部品目 + 类注/章注/附加美国注释
     → 排序的品目（最多 2 个）、排除了谁、依据哪条注、缺什么事实；正确品目在未列出的章时
       可点名章号，补该章注释后重判一次。
  ② 下钻（GRI 6）：该品目的完整子目树（缩进，叶子带税率）+ 附加美国注释 → 8 位行 + 每层理由。
     code 必须在树里；无效重问一次，再无效就报错，**绝不退回某个候选**。
  ③ 先例：拟定编码的历史先例（按码反查）+ 语义最近的先例 + 最多 2 条正文 → 一致与否；
     可建议回退到另一品目（再下钻一次）或同品目内的另一 8 位行。改判带裁定号与理由，
     由调用方展示，不静默替换。

【实测（2026-09-16，gpt-5.6-luna，正文金标）】平铺做错的 23 条：品目对 11、最终 8 位对 7；
平铺做对的 15 条对照：做坏 1。先例步共改 5 条：救 3、坏 1、无变化 1。每条约 1.75 万输入 token、
50 秒，是平铺的 3 倍——所以只对低置信度行升级（见 ai.classify_product 的 guided 配置）。

【边界】税率与判定条件仍来自本地税则，模型只选。注释来自 data/hts_notes.json（缺席时照跑，
结果里标"注释缺席"）；WCO 解释性注释有版权，不在其中。留一法评测须传 exclude_ruling，
它会从召回投票之外的三条先例路径（按码反查 / 语义 / 正文）里剔除该裁定号。
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

NOTES_CAP = 80000        # 品目步注释总量上限（字符）；超出截断并告知模型
SUBTREE_CAP = 40000      # 子目树上限（字符）
DEFAULT_MAX_CALLS = 8    # 每件商品最多调用次数（品目 1–2 + 下钻 1–2 + 同级对证 1 + 先例 1 + 回退下钻 1）
EXAMPLES_PER_CODE = 2    # 同级对证 / 先例核对：每个编码给几条"海关实际判进去的货"
EXAMPLE_MAX_CHARS = 220
MIN_OLLAMA_CTX = 16384   # 品目步提示约 1.3 万 token；Ollama 默认 num_ctx 会静默截掉末尾的注释
CHAPTERS_SHOWN = 3       # 品目步展示注释的章数（按候选数排序）


# ---------- 税则树（按 db 对象缓存一次） ----------

_TREE_CACHE = {"key": None, "tree": None}


class _Tree:
    def __init__(self, db):
        self.r8 = db["rates_8"]
        self.nodes = db.get("path_nodes") or []
        self.alive = {c for c in self.r8 if c[:2] not in ("98", "99")}
        self.by_h4 = {}
        for c in sorted(self.alive):
            self.by_h4.setdefault(c[:4], []).append(c)

    def heading_text(self, h4):
        codes = self.by_h4.get(h4) or []
        if not codes:
            return ""
        r = self.r8[codes[0]]
        # 8 位行直接挂在品目下时 path 为空，品目条文就是该行自己的品名
        txt = self.nodes[r["path"][0]] if r.get("path") else r.get("desc", "")
        return txt.rstrip(":").strip()

    def headings_of_chapter(self, ch):
        pre = f"{int(ch):02d}"
        return {h: self.heading_text(h) for h in sorted(self.by_h4) if h.startswith(pre)}

    def subtree(self, h4):
        """整个品目的缩进树：祖先只在变化时打印，叶子带一般税率。"""
        import core
        lines = [f"{h4}  {self.heading_text(h4)}"]
        prev = []
        for c in self.by_h4.get(h4) or []:
            r = self.r8[c]
            anc = [self.nodes[i].strip() for i in (r.get("path") or [])][1:]
            k = 0
            while k < len(anc) and k < len(prev) and anc[k] == prev[k]:
                k += 1
            for j in range(k, len(anc)):
                lines.append("  " * (j + 1) + anc[j])
            lines.append("  " * (len(anc) + 1)
                         + f"{core.fmt(c, 8)}  {r.get('desc', '').strip()}  [一般税率 {r.get('general', '')}]")
            prev = anc
        t = "\n".join(lines)
        return (t[:SUBTREE_CAP] + "\n…（子目树过长已截断）") if len(t) > SUBTREE_CAP else t


def _tree(db):
    key = id(db)
    if _TREE_CACHE["key"] != key:
        _TREE_CACHE["key"], _TREE_CACHE["tree"] = key, _Tree(db)
    return _TREE_CACHE["tree"]


def _digits(s):
    return re.sub(r"\D", "", str(s or ""))


# ---------- 注释 ----------

def _notes_block(chapters, notes):
    """类注（每类一次）+ 章注 + 附加美国注释，总量封顶。返回 (文本, 截断否, 缺席否)。"""
    import hts_notes
    out, secs, total, truncated, absent = [], set(), 0, False, True
    for ch in chapters:
        n = hts_notes.notes_for(ch, notes=notes)
        parts = []
        if n["section_id"] not in secs and n["section"]:
            secs.add(n["section_id"])
            parts.append(n["section"])
        parts += [n["chapter"], n["us"]]
        blk = "\n\n".join(p for p in parts if p)
        if blk.strip():
            absent = False
        else:
            blk = "（本章注释缺席：data/hts_notes.json 未构建或无此章）"
        if total + len(blk) > NOTES_CAP:
            blk = blk[:max(0, NOTES_CAP - total)] + "\n…（注释过长已截断）"
            truncated = True
        out.append(f"##### 第 {ch} 章相关注释 #####\n{blk}")
        total += len(blk)
        if truncated:
            break
    return "\n\n".join(out), truncated, absent


# ---------- 先例（留一法：exclude_ruling 在三条路径里都剔除） ----------

def _prec_by_code_default(code8, exclude, limit=6):
    try:
        import cross
        r = cross.code_precedents([code8], limit=limit + 2)
        return [x for x in (r.get("先例") or []) if x.get("裁定号") != exclude][:limit] if isinstance(r, dict) else []
    except Exception:
        return []


def _prec_semantic_default(desc, exclude, alive, limit=8):
    try:
        import cross
        r = cross.semantic_precedents(desc, [], limit=limit, alive_codes=alive, exclude=exclude)
        return [x for x in (r.get("先例") or []) if x.get("裁定号") != exclude][:limit] if isinstance(r, dict) else []
    except Exception:
        return []


def _fetch_text_default(x):
    try:
        import cross
        return cross.fetch_ruling_text(x.get("裁定号"), x.get("来源"), x.get("日期"))
    except Exception:
        return None


def _examples_default(codes, exclude):
    """候选编码 → 最近先例的描述段（缺的按需拉正文，永久缓存）。裁定库不可用返回 {}。"""
    try:
        import cross_desc
        return cross_desc.precedent_examples(codes, per_code=EXAMPLES_PER_CODE, exclude=exclude)
    except Exception:
        return {}


def _fmt_examples(exs):
    parts = []
    for x in exs or []:
        body = (x.get("描述") or "").strip() or ("（仅有摘要）" + (x.get("主题") or "").strip())
        parts.append(f"{x.get('裁定号', '')}({str(x.get('日期', ''))[:4]})：{body[:EXAMPLE_MAX_CHARS]}")
    return "；".join(parts)


_SYS_VERIFY = (
    "你是美国海关归类专家。下钻已经选了一个 8 位行，现在做同级对证：同一层级的每个子目都列出了条文与"
    "海关实际判进该行的货物描述。对每个子目逐条判断商品是否满足其条文——"
    "'满足' 须引用商品描述里的原文作依据；'无证据' 表示描述没提到该条件；'矛盾' 表示描述与条件相反。"
    "然后给出最终编码：可以维持原选，也可以改成同级的另一个子目；只在证据支持时才改，不要因为先例多就改。"
    "描述没提到的条件写进 need_verify。只输出 JSON：{\"checks\":[{\"code\":\"8位\",\"判定\":\"满足|无证据|矛盾\","
    "\"依据\":\"引用原文或说明\"}],\"code\":\"最终8位\",\"confidence\":0到1,\"reason\":\"维持或改判的理由\","
    "\"need_verify\":[\"…\"]}")


def _fmt_prec(x):
    return (f"{x.get('裁定号', '')} ({str(x.get('日期', ''))[:4]}, {x.get('来源', '')}, {x.get('状态', '')}) "
            f"编码 {', '.join(x.get('编码') or [])}：{str(x.get('主题', ''))[:110]}")


# ---------- 提示词 ----------

_SYS_HEADING = (
    "你是美国海关归类专家，严格按 GRI（归类总规则）工作。任务：只决定 4 位品目（heading），不要决定子目。\n"
    "步骤：1) 先读类注与章注，凡有排除性条款（如 'This chapter does not cover'、'does not apply to'）先排除候选；"
    "2) 按 GRI 1 以品目条文和注释定品目；条文都能覆盖时按 GRI 3(a) 最具体描述、3(b) 基本特征、3(c) 号列最后；"
    "3) 注释是法律依据，先例票数只是证据，不得因票数多就选它。\n"
    "候选品目列表来自检索，可能漏掉正确品目：若你认为正确品目在所列各章的品目清单里但不在候选里，直接选它；"
    "若正确品目在未列出的章，把章号填到 other_chapter（两位数字），我会补该章注释后让你重判。\n"
    "只输出 JSON：{\"headings\":[{\"heading\":\"4位数字\",\"reason\":\"引用条文/注释原文的中文理由\",\"notes_cited\":[\"引用的注释编号\"]}],"
    "\"excluded\":[{\"heading\":\"4位\",\"why\":\"依据哪条注释排除\"}],\"other_chapter\":null或\"两位章号\",\"missing_facts\":[\"缺少的商品事实\"]}。"
    "headings 按可能性排序，最多 2 个。")

_SYS_DESCEND = (
    "你是美国海关归类专家。品目已定，现在按 GRI 6 在该品目内逐级往下定到 8 位税则行：同一层级的子目并列比较，"
    "只有具名子目都不适用时才选 'Other'，并说明为何各具名子目不适用。附加美国注释（Additional U.S. Notes）里的定义对 8 位档有约束力。"
    "只输出 JSON：{\"code\":\"8位数字\",\"level_reasons\":[\"每一层为何这样选\"],\"need_verify\":[\"需确认的商品属性\"],\"confidence\":0到1}。"
    "code 必须是树里出现的 8 位行。")

_SYS_PRECEDENT = (
    "你是美国海关归类专家。请核对拟定编码与 CBP 先例是否一致。先例只对相似货物有参考价值：相似不等于相同，差一个属性结论可能相反；"
    "撤销/修改的裁定不作依据；HQ 高于 NY，新的高于旧的。"
    "若先例清楚地把相似货物归到另一个品目，输出 revisit_heading（4 位）；若品目正确但先例表明同品目内应选另一 8 位行"
    "（例如描述不足以支持拟定行的具名条件，先例对近似货物选了 Other），输出 revisit_code（8 位，须在同一品目内）；否则均为 null。"
    "不要仅因先例数量多就改判。"
    "只输出 JSON：{\"consistent\":true/false,\"revisit_heading\":null或\"4位\",\"revisit_code\":null或\"8位\",\"reason\":\"中文理由，引用裁定号\"}")


# ---------- 主流程 ----------

def classify_guided(db, description, origin="CN", provider=None, rows=None, keywords=None, chapters=None,
                    exclude_ruling=None, max_calls=DEFAULT_MAX_CALLS, notes=None,
                    _prec_by_code=None, _prec_semantic=None, _fetch_text=None, _examples=None, card=""):
    """
    商品描述 → 8 位编码（逐级链）。

    rows：调用方已有的召回候选（升级时复用平铺流水线的池子）；None 则自行召回。
    keywords / chapters：平铺第一轮的产物，只用于自行召回时的关键词通道与章号加权。
    exclude_ruling：留一法评测时剔除的裁定号。notes：注入的注释数据（测试用）。
    返回 {"编码","code8","confidence","reason","需确认","归类方式":"逐级","论证":{...}}，失败 {"error"}。
    """
    import ai
    provider = provider or ai.get_provider()
    if provider is None:
        return {"error": "AI 服务未配置"}
    if type(provider).__name__ == "OllamaProvider" and (getattr(provider, "num_ctx", 0) or 0) < MIN_OLLAMA_CTX:
        return {"error": f"逐级归类的提示约 1.3 万 token，Ollama 需在 ai_config.json 设 num_ctx ≥ {MIN_OLLAMA_CTX}"
                         "（否则末尾的注释会被静默截掉）"}
    desc = (description or "").strip()
    if not desc:
        return {"error": "商品描述为空"}
    # 要素表（ai.card_text 的一行文本）随描述一起进每一步：模型看到的是"哪些说了、哪些没说"，
    # 「未提及」的属性不得当作满足——这是同级对证里"无证据"判定的直接依据
    if card:
        desc = f"{desc}\n\n归类要素表（只填了描述明确说了的；「未提及」的属性不能当作满足）：{card}"
    tree = _tree(db)
    prec_by_code = _prec_by_code or (lambda c, ex: _prec_by_code_default(c, ex))
    prec_semantic = _prec_semantic or (lambda d, ex: _prec_semantic_default(d, ex, tree.alive))
    fetch_text = _fetch_text or _fetch_text_default
    examples = _examples or _examples_default
    budget = {"n": 0, "max": max(3, int(max_calls or DEFAULT_MAX_CALLS))}
    trace = {"调用": [], "警告": []}

    def call(messages, tag):
        if budget["n"] >= budget["max"]:
            raise ai.AIProviderError(f"已达调用上限 {budget['max']}")
        budget["n"] += 1
        out = provider.chat_json(messages, fallback=None)
        trace["调用"].append(tag)
        return out if isinstance(out, dict) else {}

    # 候选池
    if rows is None:
        rows = ai._recall_candidates(db, keywords or [desc], description=desc)
        if not rows and keywords:
            rows = ai._recall_candidates(db, [desc], description=desc)
        rows = ai._rank_by_chapters(rows, chapters or [])
    if not rows:
        return {"error": "本地税则库无候选，无法开始逐级归类"}

    try:
        # ① 品目
        hs, other_ch, out_a, shown, absent = _step_heading(tree, desc, rows, call, notes)
        if other_ch:
            trace["补章"] = other_ch
            hs2, _, out_a2, shown2, absent2 = _step_heading(tree, desc, rows, call, notes, extra=(other_ch,))
            if hs2:
                hs, out_a, shown, absent = hs2, out_a2, shown2, absent2
        if not hs:
            return {"error": "模型未给出有效的 4 位品目", "论证": {"品目步输出": out_a, "调用": trace["调用"]}}
        h4 = hs[0]
        # ② 下钻
        code, out_b = _step_descend(tree, desc, h4, call, notes)
        if not _valid_under(tree, code, h4):
            code, out_b = _step_descend(tree, desc, h4, call, notes,
                                        retry=f"上次给的 code={code or '空'} 不在品目 {h4} 的树里")
        if not _valid_under(tree, code, h4):
            return {"error": f"模型两次都未给出品目 {h4} 内的有效 8 位编码（{code or '空'}），未做归类",
                    "论证": {"品目": h4, "品目理由": _first_reason(out_a), "调用": trace["调用"]}}
        # ②′ 同级对证：同一层每个具名子目 条文 + 海关实际判进去的货物描述 → 逐条 满足/无证据/矛盾
        verify = {}
        if budget["n"] < budget["max"]:
            code2, verify = _step_verify(tree, db, desc, code, h4, exclude_ruling, call, examples)
            if code2 and code2 != code:
                verify["改判"] = f"同级对证改行：{code} → {code2}"
                out_b = {**out_b, "confidence": verify.get("confidence", out_b.get("confidence")),
                         "need_verify": verify.get("need_verify") or out_b.get("need_verify")}
                code = code2
            elif verify.get("confidence") is not None:
                out_b = {**out_b, "confidence": verify["confidence"],
                         "need_verify": verify.get("need_verify") or out_b.get("need_verify")}
        final, revisit = code, {}
        # ③ 先例
        if budget["n"] < budget["max"]:
            rh, rc, out_c = _step_precedent(tree, db, desc, code, h4, exclude_ruling, call,
                                            prec_by_code, prec_semantic, fetch_text, examples)
            revisit = {"一致": bool(out_c.get("consistent")), "理由": str(out_c.get("reason", ""))[:600]}
            if rh and budget["n"] < budget["max"]:
                code2, out_b2 = _step_descend(tree, desc, rh, call, notes)
                if _valid_under(tree, code2, rh):
                    revisit["改判"] = f"先例回退品目 {h4} → {rh}：{code} → {code2}"
                    final, h4, out_b = code2, rh, out_b2
                else:
                    trace["警告"].append(f"先例建议回退到品目 {rh}，但下钻未给出有效编码，保留 {code}")
            elif rc:
                revisit["改判"] = f"先例改 8 位行：{code} → {rc}"
                final = rc
    except ai.AIProviderError as e:
        return {"error": f"逐级归类失败：{e}", "论证": {"调用": trace["调用"]}}

    import core
    conf = ai._clamp_confidence(out_b.get("confidence"))
    lvl = [str(x)[:200] for x in (out_b.get("level_reasons") or [])][:6]
    reason = f"品目 {h4}：{_first_reason(out_a)[:220]}" + (f"；末级：{lvl[-1][:160]}" if lvl else "")
    return {
        "编码": core.fmt(final, 8), "code8": final, "confidence": conf, "reason": reason[:400],
        "需确认": [str(v)[:80] for v in (out_b.get("need_verify") or [])][:5],
        "归类方式": "逐级",
        "论证": {
            "品目": h4, "品目理由": _first_reason(out_a)[:500],
            "品目备选": [{"heading": _digits(h.get("heading"))[:4], "reason": str(h.get("reason", ""))[:300]}
                          for h in (out_a.get("headings") or [])[1:2]],
            "排除": [{"heading": _digits(e.get("heading"))[:4], "why": str(e.get("why", ""))[:200]}
                      for e in (out_a.get("excluded") or []) if isinstance(e, dict)][:6],
            "缺事实": [str(x)[:160] for x in (out_a.get("missing_facts") or [])][:4],
            "逐级理由": lvl,
            "下钻编码": core.fmt(code, 8),   # 先例步之前的结论（同级对证之后）；与最终编码不同即先例步改了判
            "同级对证": {k: v for k, v in verify.items() if k in ("层", "对证", "改判", "理由")},
            "先例核对": revisit,
            "展示的章": shown, "注释可用": not absent,
            "调用次数": budget["n"], "警告": trace["警告"],
            **({"补章": trace["补章"]} if trace.get("补章") else {}),
        },
    }


def _first_reason(out_a):
    hs = out_a.get("headings") or []
    return str(hs[0].get("reason", "")) if hs and isinstance(hs[0], dict) else ""


def _valid_under(tree, code, h4):
    return bool(code) and code in tree.alive and code.startswith(h4)


def _step_heading(tree, desc, rows, call, notes, extra=()):
    groups = {}
    for r in rows:
        c = _digits(r.get("编码"))
        if len(c) < 4:
            continue
        g = groups.setdefault(c[:4], {"n": 0, "votes": 0.0})
        g["n"] += 1
        g["votes"] += float(r.get("先例票") or 0)
    ch_count = {}
    for h, g in groups.items():
        ch_count[int(h[:2])] = ch_count.get(int(h[:2]), 0) + g["n"]
    chapters = [c for c, _ in sorted(ch_count.items(), key=lambda kv: -kv[1])][:CHAPTERS_SHOWN]
    for c in extra:
        if c not in chapters:
            chapters.append(c)
    cand_txt = "\n".join(f"- {h}（召回候选 {g['n']} 条，先例票 {g['votes']:.1f}）：{tree.heading_text(h)}"
                         for h, g in sorted(groups.items(), key=lambda kv: (-kv[1]["votes"], -kv[1]["n"])))
    head_txt = "\n".join(f"第 {ch} 章：\n" + "\n".join(f"  {h}  {t}" for h, t in tree.headings_of_chapter(ch).items())
                         for ch in chapters)
    nb, _truncated, absent = _notes_block(chapters, notes)
    user = f"商品描述：\n{desc}\n\n检索召回的候选品目：\n{cand_txt}\n\n涉及各章的全部品目：\n{head_txt}\n\n注释：\n{nb}"
    out = call([{"role": "system", "content": _SYS_HEADING}, {"role": "user", "content": user}], "品目")
    hs = [_digits(h.get("heading"))[:4] for h in (out.get("headings") or []) if isinstance(h, dict)]
    hs = [h for h in hs if h in tree.by_h4]
    oc = _digits(out.get("other_chapter"))
    oc = int(oc) if oc else None
    if oc is not None and not (1 <= oc <= 97 and oc not in chapters):
        oc = None
    return hs, oc, out, chapters, absent


def _step_descend(tree, desc, h4, call, notes, retry=""):
    import hts_notes
    n = hts_notes.notes_for(int(h4[:2]), notes=notes)
    nb = n["us"] if len(n["chapter"]) > 20000 else "\n\n".join(p for p in (n["chapter"], n["us"]) if p)
    user = (f"商品描述：\n{desc}\n\n品目 {h4} 的完整子目树：\n{tree.subtree(h4)}\n\n本章注释（含附加美国注释）：\n"
            f"{nb or '（注释缺席）'}" + (f"\n\n上次输出无效：{retry}" if retry else ""))
    out = call([{"role": "system", "content": _SYS_DESCEND}, {"role": "user", "content": user}], "下钻")
    return _digits(out.get("code"))[:8], out


def _siblings(tree, code):
    """与 code 同一父节点的全部 8 位行（含自己），按编码序。"""
    path = tuple(tree.r8[code].get("path") or [])
    return [c for c in tree.by_h4.get(code[:4]) or [] if tuple(tree.r8[c].get("path") or []) == path]


def _step_verify(tree, db, desc, code, h4, exclude, call, examples):
    """
    同级对证。只有一个同级（没得比）时跳过。返回 (最终编码, {"层","对证","改判","confidence","need_verify","理由"})。
    最终编码必须在同级里，否则维持原选。
    """
    import core
    sibs = _siblings(tree, code)
    if len(sibs) < 2:
        return code, {}
    exs = examples(sibs, exclude)
    parent = " > ".join(tree.nodes[i].strip() for i in (tree.r8[code].get("path") or [])[1:])
    lines = []
    for c in sibs:
        r = tree.r8[c]
        mark = "（下钻所选）" if c == code else ""
        lines.append(f"- {core.fmt(c, 8)}{mark} | {r.get('desc', '').strip()} | 一般税率 {r.get('general', '')}\n"
                     f"    海关判到此行的货物：{_fmt_examples(exs.get(c)) or '（库里无先例）'}")
    user = (f"商品描述：\n{desc}\n\n品目 {h4}，层级：{tree.heading_text(h4)[:80]} > {parent}\n\n"
            f"同级子目：\n" + "\n".join(lines))
    out = call([{"role": "system", "content": _SYS_VERIFY}, {"role": "user", "content": user}], "对证")
    final = _digits(out.get("code"))[:8]
    if final not in sibs:
        final = code
    checks = [{"code": _digits(c.get("code"))[:8], "判定": str(c.get("判定", ""))[:6], "依据": str(c.get("依据", ""))[:200]}
              for c in (out.get("checks") or []) if isinstance(c, dict)][:12]
    conf = out.get("confidence")
    try:
        conf = float(conf) if conf is not None else None
    except (TypeError, ValueError):
        conf = None
    return final, {"层": parent, "对证": checks, "理由": str(out.get("reason", ""))[:300],
                   "confidence": conf, "need_verify": [str(v)[:80] for v in (out.get("need_verify") or [])][:5]}


def _step_precedent(tree, db, desc, code, h4, exclude, call, prec_by_code, prec_semantic, fetch_text, examples=None):
    import core
    import rate
    byc = prec_by_code(code, exclude)
    sem = prec_semantic(desc, exclude)
    # 按码反查的先例换成描述段：摘要只有两三个词，"海关实际把什么货判到了这个码"要看正文那一段
    descs = {}
    if examples:
        try:
            descs = {x["裁定号"]: x for x in (examples([code], exclude).get(code) or [])}
        except Exception:
            descs = {}
    texts = []
    for x in [s for s in sem if s.get("状态") == "现行" and s.get("裁定号") != exclude][:2]:
        t = fetch_text(x)
        if t:
            texts.append(f"[{x['裁定号']} 正文节选]\n{t[:4000]}")
    path = " > ".join(rate.path_of(db, code))
    byc_lines = []
    for x in byc:
        line = _fmt_prec(x)
        d = (descs.get(x.get("裁定号")) or {}).get("描述")
        if d:
            line += f"\n      货物描述：{d[:EXAMPLE_MAX_CHARS]}"
        byc_lines.append(line)
    for n, x in descs.items():
        if n not in {y.get("裁定号") for y in byc} and x.get("描述"):
            byc_lines.append(f"{n} ({str(x.get('日期', ''))[:4]}) 编码 {core.fmt(code, 8)}：{x.get('主题', '')[:80]}\n"
                             f"      货物描述：{x['描述'][:EXAMPLE_MAX_CHARS]}")
    user = (f"商品描述：\n{desc}\n\n拟定编码：{core.fmt(code, 8)}（品目 {h4}）\n归类路径：{path} > {db['rates_8'][code].get('desc', '')}\n\n"
            f"该编码历史先例（按码反查，带货物描述）：\n" + ("\n".join(byc_lines) or "（无）")
            + "\n\n与商品描述语义最近的先例：\n" + ("\n".join(_fmt_prec(x) for x in sem) or "（无）")
            + ("\n\n" + "\n\n".join(texts) if texts else ""))
    out = call([{"role": "system", "content": _SYS_PRECEDENT}, {"role": "user", "content": user}], "先例")
    rh = _digits(out.get("revisit_heading"))[:4]
    rc = _digits(out.get("revisit_code"))[:8]
    rh = rh if rh in tree.by_h4 and rh != h4 else ""
    rc = rc if (not rh and _valid_under(tree, rc, h4) and rc != code) else ""
    return rh, rc, out
