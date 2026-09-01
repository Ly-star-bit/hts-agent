# -*- coding: utf-8 -*-
"""
ai.py —— AI 增强层（可插拔，Web / 命令行共用）

功能：
  - classify_product()    商品描述 → HTS 编码推荐（LLM 出关键词 → 本地召回 → LLM 精排 → 引擎校验）
  - ask_tax_question()    自然语言问税（识别编码/描述/一般咨询 → 本地查询 → LLM 解读）
  - interpret_results()   查询结果 → 通俗解读
  - analyze_list()        商品清单批量分析报告

Provider 抽象（配置 ai_config.json，见项目根目录）：
  - OllamaProvider         本地模型（http://127.0.0.1:11434）
  - OpenAICompatProvider   任何 OpenAI 兼容 API（DeepSeek / 通义千问 / OpenAI 等）

未配置 AI 服务或调用失败时，所有函数返回结构化错误信息，不影响本地查询功能。
"""
import json
import os
import re

import httpx

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(BASE_DIR, "ai_config.json")

_PROVIDER_CACHE = {"provider": None, "loaded": False, "error": ""}


# ---------- Provider 抽象 ----------

class AIProviderError(Exception):
    """AI 服务调用失败"""


class BaseProvider:
    """Provider 基类：负责与模型端点通信"""

    def __init__(self, model, temperature=0.2, timeout=60):
        self.model = model
        self.temperature = temperature
        self.timeout = timeout

    def chat(self, messages):
        """发送对话消息，返回模型回复文本。子类必须实现。"""
        raise NotImplementedError

    def chat_json(self, messages, fallback=None):
        """发送对话并尝试解析 JSON 回复；解析失败返回 fallback 或抛 AIProviderError"""
        text = self.chat(messages)
        return _extract_json(text, fallback)


class OllamaProvider(BaseProvider):
    """本地 Ollama 服务（默认 http://127.0.0.1:11434）"""

    def __init__(self, model="qwen2.5:7b", base_url="http://127.0.0.1:11434", temperature=0.2, timeout=120):
        super().__init__(model, temperature, timeout)
        self.base_url = base_url.rstrip("/")

    def chat(self, messages):
        try:
            resp = httpx.post(
                f"{self.base_url}/api/chat",
                json={
                    "model": self.model,
                    "messages": messages,
                    "stream": False,
                    "options": {"temperature": self.temperature},
                },
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("message", {}).get("content", "")
        except httpx.HTTPError as e:
            raise AIProviderError(f"Ollama 调用失败：{e}") from e


class OpenAICompatProvider(BaseProvider):
    """OpenAI 兼容 API（DeepSeek / 通义 / OpenAI / 硅基流动 等）"""

    def __init__(self, model, base_url, api_key, temperature=0.2, timeout=60):
        super().__init__(model, temperature, timeout)
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def chat(self, messages):
        try:
            resp = httpx.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"model": self.model, "messages": messages, "temperature": self.temperature},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]
        except (httpx.HTTPError, KeyError, IndexError) as e:
            raise AIProviderError(f"AI API 调用失败：{e}") from e


def _extract_json(text, fallback=None):
    """从模型回复中提取 JSON：优先整体解析，其次提取 ```json 代码块，最后截取首尾大括号"""
    if not text:
        if fallback is not None:
            return fallback
        raise AIProviderError("AI 返回空内容")
    text = text.strip()
    # 直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # ```json ... ``` 代码块
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            pass
    # 首尾大括号截取
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    if fallback is not None:
        return fallback
    raise AIProviderError(f"无法解析 AI 返回的 JSON：{text[:200]}")


# ---------- 配置加载 ----------

def get_provider():
    """
    加载并缓存 Provider 实例。
    未配置 / 配置不完整 / 文件不存在 → 返回 None（AI 功能不可用），
    具体原因记录在 _PROVIDER_CACHE['error']，供 ai_status() 展示。
    """
    if _PROVIDER_CACHE["loaded"]:
        return _PROVIDER_CACHE["provider"]
    provider = None
    error = ""
    if not os.path.exists(CONFIG_FILE):
        error = "未找到 ai_config.json（AI 功能未启用）"
    else:
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                cfg = json.load(f)
            kind = (cfg.get("provider") or "").strip().lower()
            if kind == "ollama":
                provider = OllamaProvider(
                    model=cfg.get("model") or "qwen2.5:7b",
                    base_url=cfg.get("base_url") or "http://127.0.0.1:11434",
                    temperature=cfg.get("temperature", 0.2),
                    timeout=cfg.get("timeout", 120),
                )
            elif kind == "openai_compat":
                missing = [k for k in ("base_url", "api_key", "model") if not cfg.get(k)]
                if missing:
                    error = f"openai_compat 配置不完整，缺少：{'、'.join(missing)}（请编辑 ai_config.json）"
                else:
                    provider = OpenAICompatProvider(
                        model=cfg.get("model"),
                        base_url=cfg.get("base_url"),
                        api_key=cfg.get("api_key"),
                        temperature=cfg.get("temperature", 0.2),
                        timeout=cfg.get("timeout", 60),
                    )
            elif kind in ("", "null"):
                error = "未启用 AI（provider 为 null）"
            else:
                error = f"未知的 provider：{kind}（可选：null / ollama / openai_compat）"
        except Exception as e:
            error = f"AI 配置读取失败：{e}"
            provider = None
    _PROVIDER_CACHE["provider"] = provider
    _PROVIDER_CACHE["loaded"] = True
    _PROVIDER_CACHE["error"] = error
    return provider


def reset_provider_cache():
    """测试用：重置 provider 缓存"""
    _PROVIDER_CACHE["provider"] = None
    _PROVIDER_CACHE["loaded"] = False
    _PROVIDER_CACHE["error"] = ""


def ai_status():
    """AI 服务配置状态（供前端展示）"""
    p = get_provider()
    if p is None:
        return {"enabled": False, "message": _PROVIDER_CACHE["error"] or "AI 服务未配置"}
    return {"enabled": True, "provider": type(p).__name__, "model": p.model}


# ---------- 配置读写（Web 端手动配置） ----------

DEFAULT_CONFIG = {
    "provider": None,
    "base_url": "",
    "api_key": "",
    "model": "",
    "temperature": 0.2,
    "timeout": 60,
}


def load_config():
    """读取 ai_config.json；文件不存在或损坏时返回默认结构。"""
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                cfg.update({k: v for k, v in data.items() if k in cfg})
        except (json.JSONDecodeError, OSError):
            pass
    return cfg


def mask_config(cfg):
    """返回不含明文 api_key 的配置视图（key 仅保留头尾用于辨识）"""
    out = {k: v for k, v in cfg.items() if k != "api_key"}
    key = str(cfg.get("api_key") or "")
    if key:
        out["api_key_masked"] = key[:4] + "***" + key[-4:] if len(key) > 10 else "***"
    else:
        out["api_key_masked"] = ""
    out["api_key_set"] = bool(key)
    return out


def save_config(updates):
    """
    合并更新 ai_config.json（原子写入），保存后重置 provider 缓存。
    updates 中 api_key 传 '__KEEP__' 表示保留原值（Web 端留空时的默认行为）。
    返回保存后的掩码配置视图。
    """
    cfg = load_config()
    for k, v in (updates or {}).items():
        if k not in cfg:
            continue
        if k == "api_key":
            if v == "__KEEP__":
                continue
            v = str(v).strip()
        elif k == "provider":
            v = (str(v).strip().lower() or None) if v else None
            if v not in (None, "ollama", "openai_compat"):
                continue
        elif k in ("temperature", "timeout"):
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
        else:
            v = str(v).strip() if v else ""
        cfg[k] = v
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_FILE)
    reset_provider_cache()
    return mask_config(cfg)


def test_connection(provider=None):
    """
    用当前（或指定）配置发最小请求验证连通性。
    返回：{'ok': bool, 'message': str}
    """
    p = provider or get_provider()
    if p is None:
        return {"ok": False, "message": _PROVIDER_CACHE["error"] or "AI 服务未配置"}
    try:
        reply = p.chat([{"role": "user", "content": "ping，请只回复 pong"}])
        return {"ok": True, "message": f"连接成功，模型响应：{str(reply)[:80]}"}
    except AIProviderError as e:
        return {"ok": False, "message": str(e)}


# ---------- 本地召回 ----------

def _recall_candidates(db, keywords, limit=40, unit_value=None, origin="CN"):
    """
    根据英文关键词在本地税率库召回候选 8 位子目。

    排序固定 relevance：这批行是要送进精排的候选池，按相关度取前 limit 条最合理。
    unit_value / origin 只影响 rate.search 补的总税负列（每种排序都会补），
    不影响召回顺序——但这些行会与本地行同表展示，口径必须跟着调用方走。
    """
    import rate

    rows = rate.search(db, " ".join(keywords), limit=limit, sort="relevance",
                       unit_value=unit_value, origin=origin)
    return rows


def _rank_by_chapters(rows, chapters, limit=40):
    """
    按模型建议的章号重排，而不是过滤。

    此前是硬过滤：rows = [r for r in rows if 编码.startswith(chapters)]。
    章号是模型的猜测，一旦猜错（塑料雨衣猜成 62 章而实际在 39 章），
    正确候选会被整条滤掉，然后报"本地税则库未找到"——用户看到的是
    "库里没有"，真实原因却是"模型猜错了章"。归类分歧场景里跨章恰恰是常态，
    所以章号只能加权。
    """
    if not chapters:
        return rows[:limit]
    pref = tuple(chapters)
    hit = [r for r in rows if str(r["编码"]).startswith(pref)]
    miss = [r for r in rows if not str(r["编码"]).startswith(pref)]
    return (hit + miss)[:limit]


def _ai_keywords(provider, description):
    """
    第一轮：商品描述 → 税则原文里实际出现的英文检索词。

    这一步才是 AI 对"本地搜不准"的真正修复——同义词表是 176 条硬映射，
    覆盖不到组合描述（"PU 涂层梭织连帽夹克"）。精排在召回之后，
    召回不到的东西精排看不见，救不回来。

    返回 (keywords, chapters)；失败抛 AIProviderError 或返回 ([], [])。
    """
    sys_prompt = (
        "你是美国海关 HTS（协调关税表）归类助手。根据商品描述，输出用于在 HTS 税则库中检索的"
        "英文关键词列表和可能的章号（chapter，两位数字）建议。"
        "关键词必须是税则品名中实际出现的词（如 battery、accumulator、ceramic、tableware），"
        "不要用宽泛的用途/功能词（如 electric、storage、device、product、item）。"
        "只输出 JSON，格式：{\"keywords\": [\"英文关键词\"], \"chapters\": [\"85\", \"90\"]}，"
        "keywords 3-6 个，单数形式，不要输出其他内容。"
    )
    r1 = provider.chat_json(
        [{"role": "system", "content": sys_prompt},
         {"role": "user", "content": f"商品描述：{description}"}],
        fallback=None,
    )
    if not isinstance(r1, dict):
        return [], []
    keywords = [str(k) for k in (r1.get("keywords") or []) if str(k).strip()]
    chapters = [str(c).zfill(2) for c in (r1.get("chapters") or []) if str(c).strip()]
    return keywords, chapters


def _ai_rerank(provider, db, description, rows, top_n):
    """
    第二轮：在已召回的候选里精排。返回 picks（模型原始输出，未校验）。

    候选行带归类路径与判定条件（见 _candidate_line）——末级品名大量是
    "Other"，只给品名等于让模型盲选。
    """
    cand_lines = [_candidate_line(db, i, r) for i, r in enumerate(rows, 1)]
    sys_prompt2 = (
        "你是美国 HTS 归类专家。下面是从税则库检索出的候选子目。"
        "每行格式：序号. 编码 | 归类路径（父级 > 子级，判定条件多在父级上）| 税率 | 301 | 判定条件。"
        f"请为商品「{description}」选择最合适的 {top_n} 个候选，按匹配度排序。\n"
        "选择时必须依据归类路径中的实际措辞（材质、织法、含量阈值、涂层、"
        "重量/尺寸/价值门槛），不要只看末级品名——末级常常只是 'Other'。\n"
        "reason 必须引用候选行中的原文依据，不要泛泛而谈。\n"
        "若某候选的成立取决于尚未确认的商品属性（如羊毛含量是否达到 36%、"
        "面料是针织还是梭织、有无塑料涂层），写进 need_verify。\n"
        "只输出 JSON，格式：{\"picks\": [{\"code\": \"8位编码\", \"confidence\": 0.9, "
        "\"reason\": \"引用原文的中文理由\", \"need_verify\": [\"需确认的商品属性\"]}]}，"
        "code 必须来自候选列表，confidence 为 0-1 数值。"
    )
    r2 = provider.chat_json(
        [{"role": "system", "content": sys_prompt2},
         {"role": "user", "content": "候选子目：\n" + "\n".join(cand_lines)}],
        fallback=None,
    )
    return (r2.get("picks") if isinstance(r2, dict) else None) or []


# 喂给 LLM 的归类路径保留末尾几级：越靠近末级的祖先越有判别力，
# 顶级品目动辄上百字符（"Men's or boys' overcoats, carcoats, capes..."），
# 40 个候选全带上会挤占上下文却提供不了区分度。
_PATH_TAIL = 3
_PATH_SEG_MAX = 60


def _candidate_line(db, i, row):
    """
    构造精排用的候选行。

    此前只给 row['商品描述']，而 HTS 末级品名大量是 "Other"、"Of silk"、
    "Other (229)"——LLM 拿到一串同样的 "Other" 根本无从选择，等于盲选。
    改为带归类路径与已抽出的判定条件，让模型能依据实际措辞判断。
    """
    import criteria
    import rate

    code8 = re.sub(r"\D", "", str(row.get("编码", "")))[:8]
    segs = [s for s in (row.get("归类路径") or rate.path_of(db, code8))]
    segs = [s if len(s) <= _PATH_SEG_MAX else s[:_PATH_SEG_MAX] + "…" for s in segs[-_PATH_TAIL:]]
    own = (row.get("商品描述") or "").rstrip(":").strip()
    path_txt = " > ".join([s for s in segs if s] + [own]) if own else " > ".join(segs)

    try:
        crs = criteria.extract(db, code8)
    except Exception:
        crs = []
    cr_txt = "；".join(f"{c['类型']}:{c['原文'][:48]}" for c in crs[:3])

    add = f"，附加税 {row['附加税']}" if row.get("附加税") else ""
    line = (f"{i}. {row['编码']} | {path_txt} | 一般税率 {row['一般税率']} | "
            f"301: {row['301判定']} {row['301加征']}{add}")
    if cr_txt:
        line += f" | 判定条件: {cr_txt}"
    return line


# ---------- AI 功能 ----------

def classify_product(db, description, top_n=3, origin="CN"):
    """
    商品描述 → HTS 编码推荐。

    流程：
      1. LLM：中文/英文描述 → 英文搜索关键词 + 章号建议（JSON）
      2. 本地召回：按关键词在税则库搜索 top 40
      3. LLM：从候选编码中挑选最合适的 top_n，输出编码 + 置信度 + 理由（JSON）
      4. 本地引擎校验：对推荐编码计算 301 状态与总税负

    返回：{'candidates': [...], 'keywords': [...], 'disclaimer': str}
    """
    provider = get_provider()
    if provider is None:
        return {"error": "AI 服务未配置，无法进行智能归类。请先在 ai_config.json 配置。"}

    # 第一轮：出关键词
    try:
        keywords, chapters = _ai_keywords(provider, description)
    except AIProviderError as e:
        return {"error": f"AI 归类失败：{e}"}
    if not keywords:
        return {"error": "AI 未能提取商品关键词，请尝试更详细的商品描述。"}

    # 本地召回。AI 关键词全落空时降级为原文检索（走同义词表），
    # 而不是直接报"库里没有"——那会把模型的失误说成数据的缺失。
    rows = _recall_candidates(db, keywords)
    degraded = ""
    if not rows:
        rows = _recall_candidates(db, [description])
        if rows:
            degraded = (f"AI 给出的检索词（{' '.join(keywords)}）在税则库中无匹配，"
                        f"已降级为按原文检索，候选质量可能下降")
    if not rows:
        return {"error": f"本地税则库未找到与「{description}」匹配的商品（关键词：{' '.join(keywords)}），请尝试调整描述。"}
    rows = _rank_by_chapters(rows, chapters)

    # 第二轮：精排
    try:
        picks = _ai_rerank(provider, db, description, rows, top_n)
    except AIProviderError as e:
        return {"error": f"AI 归类失败：{e}"}
    if not picks:
        return {"error": "AI 未返回有效归类结果，请重试或联系人工复核。"}

    # 引擎校验：编码两边都归一化后比较。
    # 候选表的键带点（'8507.60.00'），模型却常返回不带点的 '85076000'，
    # 若只比字面量会把大量合法结果误判成幻觉。
    candidates = []
    code_by_norm = {re.sub(r"\D", "", r["编码"]): r for r in rows}
    for pk in picks[:top_n]:
        norm = re.sub(r"\D", "", str(pk.get("code") or ""))
        row = code_by_norm.get(norm)
        if not row:
            continue
        # 用候选行自身的编码算税，不用模型给的字符串——匹配放宽后两者可能不再等价
        code8 = re.sub(r"\D", "", row["编码"])[:8]
        total = rate_calc(db, code8, origin=origin)
        crs = _criteria_safe(db, code8)
        candidates.append({
            "编码": row["编码"],
            "商品描述": row["商品描述"],
            # 归类路径与判定条件来自本地税则，不经模型——模型只负责选，不负责论证
            "完整品名": row.get("完整品名", ""),
            "归类路径": row.get("归类路径", []),
            "判定条件": crs,
            "证据清单": _evidence_safe(crs),
            "一般税率": row["一般税率"],
            "税率类型": row["税率类型"],
            "等效从价": row["等效从价"],
            "301判定": row["301判定"],
            "9903子目": row["9903子目"],
            "301加征": row["301加征"],
            "附加税": row["附加税"],
            "总税负估算": total["总税负估算"] if total else "",
            "confidence": _clamp_confidence(pk.get("confidence")),
            "reason": str(pk.get("reason", ""))[:300],
            "需确认": [str(v)[:80] for v in (pk.get("need_verify") or [])][:5],
        })
    if not candidates:
        return {"error": "AI 返回的编码不在候选列表中，请重试。"}
    return {
        "candidates": candidates,
        "keywords": keywords,
        "chapters": chapters,
        "跨章": len({c["编码"][:2] for c in candidates}) > 1,
        "降级": degraded,
        "disclaimer": "AI 归类结果仅供参考，正式报关归类以 CBP 裁定与海关税则为准，请人工复核。"
                      "各候选的「判定条件」与「证据清单」来自官方税则原文，可作为论证依据。",
    }


# ---------- 搜索页的 AI 增强（与「税率搜索」合并的入口） ----------

def assist_search(db, keyword, top_n=3, origin="CN", limit=40, sort="relevance",
                  unit_value=None):
    """
    在「税率搜索」已出本地结果的基础上，用 AI 补召回 + 精排。

    与 classify_product 的区别：这里不替换本地结果，而是"并入"——
    本地词表搜到的行保留，AI 关键词新搜到的行标记来源后追加。
    这样模型抽风时用户手上仍有那份确定性的本地结果兜底。

    返回 {'关键词','章号建议','新增候选','精排','降级','disclaimer'}，
    或 {'error': ...}（AI 未配置/调用失败），调用方据此静默降级。
    """
    import rate

    provider = get_provider()
    if provider is None:
        return {"error": "AI 服务未配置"}

    try:
        keywords, chapters = _ai_keywords(provider, keyword)
    except AIProviderError as e:
        return {"error": f"AI 调用失败：{e}"}
    if not keywords:
        return {"error": "AI 未能从该描述中提取检索词"}

    # origin / unit_value 决定总税负列怎么算。新增候选要和本地行同表并列、
    # 还要一起排序，两边必须用同一口径，否则表里会出现越南原产的行按中国
    # 口径加了 301 的情况——数字并排放着，看不出是两套算法。
    local_rows = rate.search(db, keyword, limit=limit, sort=sort,
                             unit_value=unit_value, origin=origin)
    local_codes = {re.sub(r"\D", "", str(r["编码"])) for r in local_rows}

    ai_rows = _recall_candidates(db, keywords, limit=limit,
                                 unit_value=unit_value, origin=origin)
    new_rows = [r for r in ai_rows
                if re.sub(r"\D", "", str(r["编码"])) not in local_codes]

    # 精排在"本地 + AI 新增"的合集上做，否则模型看不到本地那部分，
    # 可能挑出一个不如本地首条的候选却显得很确定
    merged = _rank_by_chapters(local_rows + new_rows, chapters, limit=limit)
    if not merged:
        return {"error": "本地税则库无匹配候选"}

    try:
        picks = _ai_rerank(provider, db, keyword, merged, top_n)
    except AIProviderError as e:
        return {"error": f"AI 调用失败：{e}"}

    code_by_norm = {re.sub(r"\D", "", r["编码"]): r for r in merged}
    ranked = []
    for pk in picks[:top_n]:
        row = code_by_norm.get(re.sub(r"\D", "", str(pk.get("code") or "")))
        if not row:
            continue
        code8 = re.sub(r"\D", "", row["编码"])[:8]
        crs = _criteria_safe(db, code8)
        ranked.append({
            "编码": row["编码"],
            "商品描述": row["商品描述"],
            "判定条件": crs,
            "证据清单": _evidence_safe(crs),
            "confidence": _clamp_confidence(pk.get("confidence")),
            "reason": str(pk.get("reason", ""))[:300],
            "需确认": [str(v)[:80] for v in (pk.get("need_verify") or [])][:5],
        })

    for r in new_rows:
        r["来源"] = "AI"
    return {
        "关键词": keywords,
        "章号建议": chapters,
        "新增候选": new_rows,
        "精排": ranked,
        "跨章": len({c["编码"][:2] for c in ranked}) > 1,
        "disclaimer": "AI 只负责在候选中挑选与说明理由；税率、判定条件、证据清单均来自本地官方税则原文。"
                      "正式归类以 CBP 裁定为准。",
    }


def _criteria_safe(db, code8):
    """抽判定条件；失败不影响归类结果"""
    try:
        import criteria
        return criteria.extract(db, code8)
    except Exception:
        return []


def _evidence_safe(crs):
    try:
        import criteria
        return criteria.evidence_list(crs)
    except Exception:
        return []


def _clamp_confidence(v):
    """模型可能返回 '非常高' 这类非数值，钳到 [0,1]，避免前端进度条与排序异常"""
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return 0.0


def rate_calc(db, norm_code, unit_value=None, origin="CN"):
    """带容错的 calc_total 封装。origin 必须透传，否则越南/其他原产地会被按中国算。"""
    try:
        import rate
        return rate.calc_total(db, norm_code, unit_value=unit_value, origin=origin)
    except Exception:
        return None


def interpret_results(results):
    """
    查询结果 → 通俗解读（AI 生成 Markdown 文本）。
    results: core.batch_query 返回的结果列表。
    """
    provider = get_provider()
    if provider is None:
        return {"error": "AI 服务未配置，无法生成解读。请先在 ai_config.json 配置。"}
    if not results:
        return {"error": "无查询结果可解读。"}

    lines = []
    for r in results:
        lines.append(
            f"- {r.get('输入编码', '')} {r.get('商品描述', '')[:60]} | "
            f"一般税率 {r.get('一般税率', '')} | 301: {r.get('301判定', '')} "
            f"{r.get('301加征', '')} | 附加税 {r.get('附加税', '')}"
        )
    sys_prompt = (
        "你是为货代公司客户服务的美国关税解读专家。根据下面的查询结果，用简体中文写一段通俗解读："
        "①哪些商品受 301 加征影响最大；②总税负大致水平（说明从量税需按货值折算）；"
        "③给货代业务员 2-3 条可执行的建议（如核对豁免、原产地规划）。"
        "用 Markdown 格式，简洁专业，不要编造查询结果之外的数据。"
    )
    try:
        text = provider.chat(
            [{"role": "system", "content": sys_prompt},
             {"role": "user", "content": "查询结果：\n" + "\n".join(lines)}]
        )
        return {"interpretation": text}
    except AIProviderError as e:
        return {"error": f"AI 解读失败：{e}"}


def ask_tax_question(db, question, origin="CN"):
    """
    自然语言问税入口。返回：
      - 类型 'codes'：问题含 HTS 编码 → 本地查询（不依赖 AI）+ 可选 AI 解读
      - 类型 'classify'：问题描述商品 → 走归类推荐
      - 类型 'faq'：一般咨询 → AI 基于税则知识回答
    origin: CN（中国，默认）/ VN（越南），透传至本地查询。
    """
    import core

    codes = core.extract_codes(question)
    if codes:
        results, stats = core.batch_query(db, codes, origin=origin)
        interp = interpret_results(results)
        return {
            "type": "codes",
            "codes": codes,
            "results": results,
            "stats": stats,
            "interpretation": interp.get("interpretation", ""),
            "error": interp.get("error", ""),
        }

    provider = get_provider()
    if provider is None:
        return {"error": "AI 服务未配置。请先在 ai_config.json 配置。"}

    # 交给 LLM 判断意图
    sys_prompt = (
        "你是美国关税助手。判断用户问题类型："
        "若问题在描述某类商品（如'锂电池出口美国要交多少税'、'帮我查一下地毯的编码'），输出 "
        "{\"type\": \"classify\", \"description\": \"商品描述\"}；"
        "若是关于关税/301/税则的一般咨询，输出 {\"type\": \"faq\", \"question\": \"问题原文\"}。"
        "只输出 JSON。"
    )
    try:
        r = provider.chat_json(
            [{"role": "system", "content": sys_prompt},
             {"role": "user", "content": question}],
            fallback=None,
        )
    except AIProviderError as e:
        return {"error": f"AI 调用失败：{e}"}

    if isinstance(r, dict) and r.get("type") == "classify":
        desc = r.get("description") or question
        return {"type": "classify", "description": desc,
                **classify_product(db, desc, origin=origin)}
    # 原先这里还有一条 faq 分支：把问题直接丢给模型自由回答，不碰任何本地数据。
    # 与本项目"税率、判定条件、证据一律来自官方税则原文，AI 只负责在候选中挑选"
    # 的原则相悖——报关场景里用户分不出哪句有依据、哪句是模型编的，
    # 一段听起来专业的错误答复比一句"答不了"危险得多。故整条移除。
    return {"error": "这里只处理「HTS 编码查询」和「商品归类」两类问题。"
                     "关税政策类咨询本工具不作答——本地数据只涵盖税则表与 301/FLIP 清单，"
                     "无法为政策解释提供依据。"}


def analyze_list(db, items, origin="CN"):
    """
    商品清单批量分析：为每个商品归类 + 查税，LLM 汇总分析报告。

    items: [{'name': 品名/描述, 'quantity': 数量(可选), 'unit_value': 单位货值USD(可选)}, ...]
    返回：{'report': str, 'details': [逐商品归类+税负], 'stats': {...}}
    """
    provider = get_provider()
    if provider is None:
        return {"error": "AI 服务未配置。请先在 ai_config.json 配置。"}
    if not items:
        return {"error": "清单为空。"}
    if len(items) > 30:
        return {"error": f"单次最多分析 30 个商品（当前 {len(items)} 个），请分批。"}

    # 第一轮：批量出关键词
    item_lines = [f"{i + 1}. {it.get('name', '')}" for i, it in enumerate(items)]
    sys_prompt = (
        "你是美国 HTS 归类助手。为下列每个商品输出英文检索关键词（税则品名实际出现的词，"
        "避免 electric、storage、device 等宽泛用途词）与章号建议。"
        "只输出 JSON：{\"items\": [{\"index\": 1, \"keywords\": [\"...\"], \"chapters\": [\"85\"]}]}"
    )
    try:
        r1 = provider.chat_json(
            [{"role": "system", "content": sys_prompt},
             {"role": "user", "content": "商品清单：\n" + "\n".join(item_lines)}],
            fallback=None,
        )
    except AIProviderError as e:
        return {"error": f"AI 调用失败：{e}"}

    kw_map = {}
    for it in (r1.get("items") if isinstance(r1, dict) else []) or []:
        idx = int(it.get("index", 0))
        kws = [str(k) for k in (it.get("keywords") or []) if str(k).strip()]
        chs = [str(c).zfill(2) for c in (it.get("chapters") or [])]
        kw_map[idx] = (kws, chs)

    # 逐商品本地召回（第一候选）
    recall_failed = {}  # 序号 → 召回失败的说明
    pending = []  # 需要精排的 (序号, 候选行列表, 原始条目, 降级说明)
    for i, it in enumerate(items, 1):
        name = it.get("name", "")
        kws, chs = kw_map.get(i, ([], []))
        rows = _recall_candidates(db, kws, limit=20) if kws else []
        # 章号只加权不过滤（同 classify_product）。硬过滤时模型猜错章会把正确
        # 候选整条滤掉，然后这一行报"本地库未匹配"——清单里几十行，用户看到的
        # 是"库里没有"，真实原因却是模型猜错了章。
        rows = _rank_by_chapters(rows, chs, limit=20)
        note = ""
        if not rows and name:
            # AI 关键词全落空时退回按原文检索，而不是直接判这行无解
            rows = _recall_candidates(db, [name], limit=20)
            if rows:
                note = f"AI 检索词（{' '.join(kws)}）无匹配，已降级为按品名原文检索"
        if not rows:
            # 写进 details_map 而不是 details——末尾会按 details_map 重建整个列表，
            # 早先 append 到 details 的内容会被整个丢掉（原实现里那句 append
            # 就是死代码，所有召回失败最终都塌成一句笼统的"本地库未匹配"）。
            recall_failed[i] = {
                "序号": i, "品名": name,
                "error": f"本地库未匹配（AI 检索词：{' '.join(kws) or '无'}；已按品名原文重试）",
            }
            continue
        pending.append((i, rows[:12], it, note))

    # 第二轮：批量精排
    details_map = {}
    if pending:
        batch_lines = []
        for i, rows, it, _note in pending:
            tops = "; ".join(f"{r['编码']}({r['商品描述'][:40]}, {r['一般税率']})" for r in rows[:6])
            batch_lines.append(f"{i}. 商品「{it.get('name', '')}」候选: {tops}")
        sys_prompt2 = (
            "为下列每个商品从候选编码中选择最匹配的一个 8 位编码。"
            "只输出 JSON：{\"picks\": [{\"index\": 1, \"code\": \"8位编码\", \"confidence\": 0.9, \"reason\": \"一句话理由\"}]}"
        )
        try:
            r2 = provider.chat_json(
                [{"role": "system", "content": sys_prompt2},
                 {"role": "user", "content": "\n".join(batch_lines)}],
                fallback=None,
            )
        except AIProviderError as e:
            return {"error": f"AI 调用失败：{e}"}
        pick_map = {}
        for pk in (r2.get("picks") if isinstance(r2, dict) else []) or []:
            # index 由模型返回，可能是 null / 字符串 / 缺失。int(None) 会直接
            # TypeError 炸掉整个清单分析——几十行商品陪一个坏 index 一起死。
            # 无效 index 的 pick 跳过即可：对应行会走"模型未给出结论"的错误路径，
            # 用户看到的是该行需人工，而不是整份报告 500。
            try:
                idx = int(pk.get("index"))
            except (TypeError, ValueError):
                continue
            pick_map[idx] = pk

        for i, rows, it, note in pending:
            pk = pick_map.get(i, {})
            code = re.sub(r"\D", "", str(pk.get("code") or ""))
            # 匹配不上就报错，不能退回 rows[0]。退回等于把 AI 从未选过的编码
            # 连同 AI 为另一个编码写的理由和置信度一起交给用户，而用户看不出这是兜底——
            # 对报关来说，"存在但归错的编码"比"不存在的编码"更危险（后者报关时会被打回）。
            # 与 classify_product 的拦截策略保持一致。
            chosen = next((r for r in rows if re.sub(r"\D", "", r["编码"]) == code), None)
            if chosen is None:
                details_map[i] = {
                    "序号": i,
                    "品名": it.get("name", ""),
                    "error": ("AI 返回的编码不在本地召回的候选列表中，未做归类。"
                              "候选：" + "、".join(r["编码"] for r in rows[:5])),
                }
                continue
            total = rate_calc(db, re.sub(r"\D", "", chosen["编码"]),
                              unit_value=it.get("unit_value"), origin=origin)
            details_map[i] = {
                "序号": i,
                "品名": it.get("name", ""),
                "编码": chosen["编码"],
                "商品描述": chosen["商品描述"],
                "一般税率": chosen["一般税率"],
                "税率类型": chosen["税率类型"],
                "301判定": chosen["301判定"],
                "301加征": chosen["301加征"],
                "9903子目": chosen["9903子目"],
                "总税负估算": total["总税负估算"] if total else "",
                "confidence": pk.get("confidence", 0),
                "reason": pk.get("reason", ""),
                # 走了降级检索的行要标出来，否则用户无从判断这条为什么质量偏低
                "备注": note,
            }

    details = [details_map.get(i) or recall_failed.get(i)
               or {"序号": i, "品名": it.get("name", ""),
                   "error": "未返回该行结果（AI 精排遗漏），请重试或单独查询"}
               for i, it in enumerate(items, 1)]

    # 统计
    hit = sum(1 for d in details if d.get("301判定") == "是")
    stats = {"total": len(details), "hit": hit, "miss": len(details) - hit,
             "failed": sum(1 for d in details if "error" in d)}

    # 第三轮：生成报告
    report_lines = []
    for d in details:
        if "error" in d:
            report_lines.append(f"- {d['序号']}. {d['品名']}：{d['error']}")
        else:
            report_lines.append(
                f"- {d['序号']}. {d['品名']} → {d['编码']} {d['商品描述'][:40]} | "
                f"{d['一般税率']} | 301: {d['301判定']} {d['301加征']} | 总税负 {d['总税负估算']}"
            )
    try:
        report = provider.chat(
            [{"role": "system", "content": (
                "你是货代公司的关税分析专家。根据下列商品清单的归类与税负结果，写一份简体中文分析报告："
                "①总览（多少商品受 301 影响）；②税负最高的 3 个商品及原因；"
                "③给客户的降税建议（核对豁免、编码复核、原产地规划、从量税按货值评估）。"
                "用 Markdown，分点，不超过 400 字，不要编造清单外的数据。")},
             {"role": "user", "content": "\n".join(report_lines)}]
        )
    except AIProviderError as e:
        report = f"（AI 报告生成失败：{e}）"

    return {"report": report, "details": details, "stats": stats}
