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

def _recall_candidates(db, keywords, limit=40):
    """根据英文关键词在本地税率库召回候选 8 位子目"""
    import rate

    rows = rate.search(db, " ".join(keywords), limit=limit, sort="relevance")
    return rows


# ---------- AI 功能 ----------

def classify_product(db, description, top_n=3):
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
    sys_prompt = (
        "你是美国海关 HTS（协调关税表）归类助手。根据商品描述，输出用于在 HTS 税则库中检索的"
        "英文关键词列表和可能的章号（chapter，两位数字）建议。"
        "关键词必须是税则品名中实际出现的词（如 battery、accumulator、ceramic、tableware），"
        "不要用宽泛的用途/功能词（如 electric、storage、device、product、item）。"
        "只输出 JSON，格式：{\"keywords\": [\"英文关键词\"], \"chapters\": [\"85\", \"90\"]}，"
        "keywords 3-6 个，单数形式，不要输出其他内容。"
    )
    try:
        r1 = provider.chat_json(
            [{"role": "system", "content": sys_prompt},
             {"role": "user", "content": f"商品描述：{description}"}],
            fallback=None,
        )
    except AIProviderError as e:
        return {"error": f"AI 归类失败：{e}"}
    if not isinstance(r1, dict):
        return {"error": "AI 归类失败：未返回有效关键词。"}
    keywords = [str(k) for k in (r1.get("keywords") or []) if str(k).strip()]
    chapters = [str(c).zfill(2) for c in (r1.get("chapters") or [])]
    if not keywords:
        return {"error": "AI 未能提取商品关键词，请尝试更详细的商品描述。"}

    # 本地召回
    rows = _recall_candidates(db, keywords)
    if chapters and rows:
        rows = [r for r in rows if str(r["编码"]).startswith(tuple(chapters))]
    if not rows:
        return {"error": f"本地税则库未找到与「{description}」匹配的商品（关键词：{' '.join(keywords)}），请尝试调整描述。"}
    rows = rows[:40]

    # 第二轮：精排
    cand_lines = []
    for i, r in enumerate(rows, 1):
        add = f"，附加税 {r['附加税']}" if r.get("附加税") else ""
        cand_lines.append(
            f"{i}. {r['编码']} | {r['商品描述']} | 一般税率 {r['一般税率']} | "
            f"301: {r['301判定']} {r['301加征']}{add}"
        )
    sys_prompt2 = (
        "你是美国 HTS 归类专家。下面是从税则库检索出的候选子目（含描述与税率）。"
        f"请为商品「{description}」选择最合适的 {top_n} 个候选，按匹配度排序。"
        "只输出 JSON，格式：{\"picks\": [{\"code\": \"8位编码\", \"confidence\": 0.9, "
        "\"reason\": \"一句话中文理由\"}]}，code 必须来自候选列表，confidence 为 0-1 数值。"
    )
    try:
        r2 = provider.chat_json(
            [{"role": "system", "content": sys_prompt2},
             {"role": "user", "content": "候选子目：\n" + "\n".join(cand_lines)}],
            fallback=None,
        )
    except AIProviderError as e:
        return {"error": f"AI 归类失败：{e}"}
    picks = (r2.get("picks") if isinstance(r2, dict) else None) or []
    if not picks:
        return {"error": "AI 未返回有效归类结果，请重试或联系人工复核。"}

    # 引擎校验
    candidates = []
    code_by_fmt = {r["编码"]: r for r in rows}
    for pk in picks[:top_n]:
        code = str(pk.get("code") or "").strip()
        norm = re.sub(r"\D", "", code)
        row = code_by_fmt.get(code) or code_by_fmt.get(norm, {})
        if not row:
            continue
        total = rate_calc(db, norm)
        candidates.append({
            "编码": row["编码"],
            "商品描述": row["商品描述"],
            "一般税率": row["一般税率"],
            "税率类型": row["税率类型"],
            "等效从价": row["等效从价"],
            "301判定": row["301判定"],
            "9903子目": row["9903子目"],
            "301加征": row["301加征"],
            "附加税": row["附加税"],
            "总税负估算": total["总税负估算"] if total else "",
            "confidence": pk.get("confidence", 0),
            "reason": pk.get("reason", ""),
        })
    if not candidates:
        return {"error": "AI 返回的编码不在候选列表中，请重试。"}
    return {
        "candidates": candidates,
        "keywords": keywords,
        "chapters": chapters,
        "disclaimer": "AI 归类结果仅供参考，正式报关归类以 CBP 裁定与海关税则为准，请人工复核。",
    }


def rate_calc(db, norm_code):
    """带容错的 calc_total 封装"""
    try:
        import rate
        return rate.calc_total(db, norm_code)
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
        return {"type": "classify", "description": desc, **classify_product(db, desc)}
    if isinstance(r, dict) and r.get("type") == "faq":
        try:
            answer = provider.chat(
                [{"role": "system", "content": (
                    "你是美国关税合规助手，用简体中文回答货代客户的问题。"
                    "涉及具体税率时，说明需要以 8 位 HTS 编码核实；"
                    "301 加征仅适用于中国原产商品，且可能存在 USTR 豁免，申报前需核对豁免清单。"
                    "回答要简洁专业，分点列出。")},
                 {"role": "user", "content": question}]
            )
            return {"type": "faq", "answer": answer}
        except AIProviderError as e:
            return {"error": f"AI 调用失败：{e}"}
    return {"error": "无法理解问题，请补充商品名称或 HTS 编码。"}


def analyze_list(db, items):
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
    details = []
    pending = []  # 需要精排的 (序号, 候选行列表)
    for i, it in enumerate(items, 1):
        kws, chs = kw_map.get(i, ([], []))
        rows = _recall_candidates(db, kws, limit=20) if kws else []
        if chs:
            rows = [r for r in rows if str(r["编码"]).startswith(tuple(chs))]
        if not rows:
            details.append({"序号": i, "品名": it.get("name", ""), "error": "本地库未匹配"})
            continue
        pending.append((i, rows[:12], it))

    # 第二轮：批量精排
    details_map = {}
    if pending:
        batch_lines = []
        for i, rows, it in pending:
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
            pick_map[int(pk.get("index", 0))] = pk

        for i, rows, it in pending:
            pk = pick_map.get(i, {})
            code = re.sub(r"\D", "", str(pk.get("code") or ""))
            chosen = next((r for r in rows if re.sub(r"\D", "", r["编码"]) == code), rows[0])
            total = rate_calc(db, re.sub(r"\D", "", chosen["编码"]))
            unit_value = it.get("unit_value")
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
            }

    details = [details_map.get(i, {"序号": i, "品名": it.get("name", ""), "error": "本地库未匹配"})
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
