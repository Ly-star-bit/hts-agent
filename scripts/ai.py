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
# 测试套件用 AI_CONFIG_FILE 指到一个不存在的路径，让 guided / recall_rewrite 等开关回到默认，
# 不随本机 ai_config.json 的状态变——线上开了逐级升级，假 Provider 的测试不该跟着升级。
CONFIG_FILE = os.environ.get("AI_CONFIG_FILE") or os.path.join(BASE_DIR, "ai_config.json")

_PROVIDER_CACHE = {"provider": None, "loaded": False, "error": ""}


# ---------- Provider 抽象 ----------

class AIProviderError(Exception):
    """AI 服务调用失败"""


class BaseProvider:
    """Provider 基类：负责与模型端点通信"""

    def __init__(self, model, temperature=0.2, timeout=60, seed=None):
        self.model = model
        self.temperature = temperature
        self.timeout = timeout
        # 固定 seed + temperature 0 = 同一提示得到同一答案。只解决可复现，不提升准确率——
        # 但报关工具要的就是"同一份清单今天跑和明天跑一样"，评测集也要它。
        self.seed = seed

    def chat(self, messages):
        """发送对话消息，返回模型回复文本。子类必须实现。"""
        raise NotImplementedError

    def chat_structured(self, messages):
        """
        要求模型按 JSON 出的调用。默认就是 chat()；真实 Provider 覆盖它开启服务端的
        JSON 模式（Ollama 的 format / OpenAI 兼容端的 response_format），少一层正则兜底。
        测试里的假 Provider 只实现 chat()，签名不变照样能用。
        """
        return self.chat(messages)

    def chat_json(self, messages, fallback=None):
        """发送对话并尝试解析 JSON 回复；解析失败返回 fallback 或抛 AIProviderError"""
        text = self.chat_structured(messages)
        return _extract_json(text, fallback)

    def ping(self):
        """
        连通性探测。默认就是 chat()；真实 Provider 覆盖成**绕过缓存**的调用——
        ping 的消息恒定，走缓存的话服务挂了「测试连接」还会返回上次的 pong。
        """
        return self.chat([{"role": "user", "content": "ping，请只回复 pong"}])


# ---------- 调用缓存 ----------
#
# 同一份清单重跑一遍，三次批量 LLM 调用的提示词一字不差，答案却要再等十几秒。
# 按（provider 类型、模型、温度、seed、是否 JSON 模式、消息）哈希落盘，命中零等待。
# 只给真实 Provider 用：测试的假 Provider 不经过这里，否则上一轮真实回答会串进测试。
# 缓存目录随时可删；AI_CACHE=0 关闭。

CACHE_DIR = os.path.join(BASE_DIR, ".cache", "llm")


def _cache_enabled():
    return os.environ.get("AI_CACHE", "1") not in ("0", "false", "no")


def _cache_key(kind, model, temperature, seed, json_mode, messages):
    import hashlib
    blob = json.dumps({"k": kind, "m": model, "t": temperature, "s": seed, "j": json_mode,
                       "msgs": messages}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _cache_get(key):
    if not _cache_enabled():
        return None
    try:
        with open(os.path.join(CACHE_DIR, key + ".json"), encoding="utf-8") as f:
            return json.load(f).get("text")
    except (OSError, json.JSONDecodeError):
        return None


def _cache_put(key, text, meta):
    if not _cache_enabled() or not text:
        return
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = os.path.join(CACHE_DIR, key + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"text": text, **meta}, f, ensure_ascii=False)
        os.replace(tmp, os.path.join(CACHE_DIR, key + ".json"))
    except OSError:
        pass


class OllamaProvider(BaseProvider):
    """本地 Ollama 服务（默认 http://127.0.0.1:11434）"""

    def __init__(self, model="qwen2.5:7b", base_url="http://127.0.0.1:11434", temperature=0.2, timeout=120,
                 think=False, seed=None, num_ctx=0):
        super().__init__(model, temperature, timeout, seed=seed)
        self.base_url = base_url.rstrip("/")
        # 上下文窗口。Ollama 默认值小且**超出即静默截断提示词末尾**，逐级归类把注释放在末尾，
        # 截掉的恰是法律依据。0 = 不传（用 Ollama 默认），guided.classify_guided 会要求 ≥ 16384。
        self.num_ctx = int(num_ctx or 0)
        # 思考模式默认关。qwen3 这类模型默认先吐几百 token 的隐藏推理再给答案，
        # 本项目的每次调用都是"按格式出 JSON"，推理链只烧时间：实测同一提示
        # think 开 3.8s / 关 0.2s（eval 290 → 9 token），两轮调用的搜索辅助
        # 27s → 秒级。页面上"按钮一直转"的根源就是它。
        # Ollama 对不支持思考的模型也接受 think=false（0.31 实测不报错）。
        self.think = bool(think)

    def _call(self, messages, json_mode, cache=True):
        key = _cache_key(f"ollama:think={int(self.think)}" + (f":ctx={self.num_ctx}" if self.num_ctx else ""),
                         self.model, self.temperature, self.seed, json_mode, messages)
        hit = _cache_get(key) if cache else None
        if hit is not None:
            return hit
        options = {"temperature": self.temperature}
        if self.seed is not None:
            options["seed"] = int(self.seed)
        if self.num_ctx:
            options["num_ctx"] = self.num_ctx
        payload = {"model": self.model, "messages": messages, "stream": False,
                   "think": self.think, "options": options}
        if json_mode:
            payload["format"] = "json"     # 服务端约束输出为合法 JSON，少一层正则兜底
        try:
            resp = httpx.post(f"{self.base_url}/api/chat", json=payload, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
            text = data.get("message", {}).get("content", "")
        except httpx.HTTPError as e:
            raise AIProviderError(f"Ollama 调用失败：{e}") from e
        if cache:
            _cache_put(key, text, {"model": self.model, "json": json_mode})
        return text

    def chat(self, messages):
        return self._call(messages, json_mode=False)

    def chat_structured(self, messages):
        return self._call(messages, json_mode=True)

    def ping(self):
        return self._call([{"role": "user", "content": "ping，请只回复 pong"}], json_mode=False, cache=False)


class OpenAICompatProvider(BaseProvider):
    """OpenAI 兼容 API（DeepSeek / 通义 / OpenAI / 硅基流动 等）"""

    def __init__(self, model, base_url, api_key, temperature=0.2, timeout=60, seed=None, reasoning_effort=""):
        super().__init__(model, temperature, timeout, seed=seed)
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        # 推理强度（gpt-5 / o 系列及兼容端的 reasoning_effort）。2026-09 实测 gpt-5.6：默认档每次调用
        # 先出 300–500 个隐藏推理 token，"none" 档 5 秒 vs 12 秒；60 条金标 top-1 58% vs 50%、top-3 持平。
        # 空 = 不传该参数（模型默认）。逐级归类链可用 guided_effort 单独设。
        self.reasoning_effort = str(reasoning_effort or "").strip().lower()

    def _post(self, payload):
        url = f"{self.base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        resp = httpx.post(url, headers=headers, json=payload, timeout=self.timeout)
        if (resp.status_code == 400 and any(k in payload for k in ("temperature", "seed"))
                and re.search(r"temperature|seed", getattr(resp, "text", "") or "")):
            # 推理类模型直连 OpenAI 时拒绝 temperature/seed（"Unsupported parameter"）：
            # 去掉重试。可复现性这时只剩提示词缓存兜底，比整条链路 400 好。
            payload = {k: v for k, v in payload.items() if k not in ("temperature", "seed")}
            resp = httpx.post(url, headers=headers, json=payload, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    def _call(self, messages, json_mode, cache=True):
        # 推理强度进缓存键：同一提示在 none 与默认档下答案不同，原型实测时踩过"命中上一档缓存"
        key = _cache_key("openai_compat" + (f":effort={self.reasoning_effort}" if self.reasoning_effort else ""),
                         self.model, self.temperature, self.seed, json_mode, messages)
        hit = _cache_get(key) if cache else None
        if hit is not None:
            return hit
        payload = {"model": self.model, "messages": messages, "temperature": self.temperature}
        if self.seed is not None:
            payload["seed"] = int(self.seed)
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        try:
            if json_mode:
                try:
                    text = self._post({**payload, "response_format": {"type": "json_object"}})
                except httpx.HTTPStatusError as e:
                    # 不是所有兼容端都认 response_format（中转站常 400）：退回普通调用，
                    # 提示词本身已要求只输出 JSON，_extract_json 还有兜底
                    if 400 <= e.response.status_code < 500:
                        text = self._post(payload)
                    else:
                        raise
            else:
                text = self._post(payload)
        except (httpx.HTTPError, KeyError, IndexError) as e:
            raise AIProviderError(f"AI API 调用失败：{e}") from e
        if cache:
            _cache_put(key, text, {"model": self.model, "json": json_mode})
        return text

    def chat(self, messages):
        return self._call(messages, json_mode=False)

    def chat_structured(self, messages):
        return self._call(messages, json_mode=True)

    def ping(self):
        return self._call([{"role": "user", "content": "ping，请只回复 pong"}], json_mode=False, cache=False)


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
                    think=cfg.get("think", False),
                    seed=cfg.get("seed", 42),
                    num_ctx=cfg.get("num_ctx", 0) or 0,
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
                        seed=cfg.get("seed", 42),
                        reasoning_effort=cfg.get("reasoning_effort", "") or "",
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
    gs = guided_settings()
    try:
        import hts_notes
        notes_ok = hts_notes.available()
    except Exception:
        notes_ok = False
    return {"enabled": True, "provider": type(p).__name__, "model": p.model,
            "reasoning_effort": getattr(p, "reasoning_effort", "") or "",
            # 逐级归类：开关 + 注释数据是否就绪（缺注释照跑但结果标"注释缺席"）
            "guided": gs["enabled"], "guided_threshold": gs["threshold"], "notes_available": notes_ok}


# ---------- 配置读写（Web 端手动配置） ----------

DEFAULT_CONFIG = {
    "provider": None,
    "base_url": "",
    "api_key": "",
    "model": "",
    "temperature": 0.0,  # 报关工具要可复现：同一提示同一答案。想要多样性再调高
    "timeout": 60,
    "think": False,      # 仅 ollama：思考模式（qwen3 等），默认关，见 OllamaProvider
    "seed": 42,          # 固定采样种子，与 temperature 0 一起保证可复现
    "num_ctx": 0,            # 仅 ollama：上下文窗口（token），0 = 用 Ollama 默认；逐级归类需 ≥ 16384
    "reasoning_effort": "",  # 仅 openai_compat：推理强度（none/low/medium/high…按服务端词表），空 = 不传
    # 低置信度行升级到 GRI 逐级归类链（scripts/guided.py）。默认关：它每件商品 3–4 次调用、
    # 约 1.75 万 token，需要强模型 + data/hts_notes.json；qwen3:8b 这类本地小模型跑不动 1.3 万 token 的注释。
    "guided": False,
    "guided_threshold": 0.9,  # 平铺置信度低于此值、或带存疑信号、或平铺无有效结果时升级
    "guided_max_calls": 8,    # 升级链每件商品的调用上限（品目 ≤2 + 下钻 ≤2 + 同级对证 1 + 先例 1 + 回退 1）
    "guided_effort": "",      # 升级链的推理强度；空 = 不传（平铺可设 none 提速、升级链保留推理）
    "guided_parallel": 3,     # 清单模式升级行的并发数：30 行清单升 10 行，串行 8–10 分钟，3 路约 3 分钟；1 = 串行
    # 规则触发（与阈值并列，任一命中即升级）。2026-09 60 条实测：平铺"很自信但错"的 5 条里 3 条是
    # 化工品（第 VI 类 28–38 章，注释决定一切）；前三候选跨章说明模型自己就在两个方向之间摇摆。
    # 60 条实测（对照无规则的 0.9）：化工章规则多升级 7 行救 1 坏 0，保留；跨章规则多升级 11 行救 0 坏 1，默认关。
    "guided_force_chapters": "28-38",  # 平铺第一名落在这些章一律升级；逗号分隔，可写区间；空 = 关
    "guided_cross_chapter": False,     # 平铺前三候选（清单模式看池内前五）跨章一律升级；实测净负，默认关
    # 召回加一路"英文 subject 改写"查询（语义 + 先例通道）。295 条正文金标实测：融合 r@20 0.81→0.84、
    # r@40 0.84→0.87（精排能看到的上限多 3 个点），先例通道 r@5 0.69→0.73；代价每件一次几十 token 的小调用。
    "recall_rewrite": True,
    # 归类要素表：归类前先把描述抽成固定的表（是什么/材质/工艺/用途/形态/包装/使用者/规格/未提及），
    # 后面平铺精排与逐级链每一步都带着它，"未提及"直接并进需确认。归类员就是先填这张表再翻税则的。
    "attribute_card": True,
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


def guided_settings(cfg=None):
    """逐级归类升级链的开关与阈值（从 ai_config.json 读；测试可传 cfg）。"""
    cfg = cfg if cfg is not None else load_config()
    try:
        th = float(cfg.get("guided_threshold", 0.9))
    except (TypeError, ValueError):
        th = 0.9
    try:
        mc = int(cfg.get("guided_max_calls", 8))
    except (TypeError, ValueError):
        mc = 8
    try:
        par = min(8, max(1, int(cfg.get("guided_parallel", 3))))
    except (TypeError, ValueError):
        par = 3
    return {"enabled": bool(cfg.get("guided")), "threshold": min(1.0, max(0.0, th)),
            "max_calls": max(3, mc), "effort": str(cfg.get("guided_effort") or "").strip().lower(),
            "parallel": par,
            "force_chapters": parse_chapters(cfg.get("guided_force_chapters", "")),
            "cross_chapter": bool(cfg.get("guided_cross_chapter", False))}


def parse_chapters(spec):
    """'28-38,90' → {'28', …, '38', '90'}；脏值忽略。"""
    out = set()
    for part in re.split(r"[,，\s]+", str(spec or "")):
        if not part:
            continue
        m = re.fullmatch(r"(\d{1,2})(?:-(\d{1,2}))?", part)
        if not m:
            continue
        a, b = int(m.group(1)), int(m.group(2) or m.group(1))
        for c in range(min(a, b), max(a, b) + 1):
            out.add(f"{c:02d}")
    return out


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
        elif k == "seed":
            try:
                v = int(v)
            except (TypeError, ValueError):
                continue
        elif k in ("think", "guided"):
            v = v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on")
        elif k in ("num_ctx", "guided_max_calls"):
            try:
                v = max(0, int(float(v)))
            except (TypeError, ValueError):
                continue
        elif k == "guided_parallel":
            try:
                v = min(8, max(1, int(float(v))))
            except (TypeError, ValueError):
                continue
        elif k == "guided_threshold":
            try:
                v = min(1.0, max(0.0, float(v)))
            except (TypeError, ValueError):
                continue
        elif k in ("guided_cross_chapter", "recall_rewrite", "attribute_card"):
            v = v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on")
        elif k == "guided_force_chapters":
            v = re.sub(r"\s+", "", str(v or ""))
            if v and not re.fullmatch(r"(\d{1,2}(-\d{1,2})?)(,\d{1,2}(-\d{1,2})?)*", v):
                continue
        elif k in ("reasoning_effort", "guided_effort"):
            # 词表由服务端定（OpenAI 是 minimal/low/medium/high，部分中转站另有 none/xhigh），
            # 这里只挡明显的脏值，真正的校验交给"测试连接"
            v = str(v or "").strip().lower()
            if not re.fullmatch(r"[a-z]{0,12}", v):
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
        reply = p.ping()      # 绕过缓存：探测的就是"现在"通不通
        return {"ok": True, "message": f"连接成功，模型响应：{str(reply)[:80]}"}
    except AIProviderError as e:
        return {"ok": False, "message": str(e)}


# ---------- 本地召回 ----------

def _ai_subject_line(provider, description):
    """
    把商品描述改写成一句 CBP 裁定 subject 风格的英文，给语义与先例两条通道做第二路查询。

    为什么单独一个小调用而不是塞进出词那一步：改出词提示会让此前所有缓存作废；
    这一步只有几十个 token，失败返回空串，召回照常只用原文。
    文档侧是英文税则行与英文裁定 subject，中文口语描述跨语言检索天然吃亏，
    一句 "a women's woven polyester raincoat laminated with TPU film" 是对齐用的桥。
    """
    sys_prompt = (
        "把下面的商品描述改写成一句美国海关裁定摘要风格的英文（不含产地、不含税号），"
        "用税则与海关的用语描述这是什么货、什么材质、什么用途，20 词以内。"
        "只输出 JSON：{\"subject\": \"英文一句话\"}")
    try:
        r = provider.chat_json([{"role": "system", "content": sys_prompt},
                                {"role": "user", "content": f"商品描述：{description}"}], fallback=None)
    except AIProviderError:
        return ""
    return str((r or {}).get("subject") or "").strip()[:300] if isinstance(r, dict) else ""


def _ai_subject_lines_batch(provider, names):
    """清单模式：一次调用给全部商品各写一句英文 subject。返回 {序号: subject}，失败 {}。"""
    lines = [f"{i + 1}. {n}" for i, n in enumerate(names)]
    sys_prompt = (
        "为下列每个商品写一句美国海关裁定摘要风格的英文（不含产地、不含税号，20 词以内），"
        "用税则与海关的用语说明这是什么货、什么材质、什么用途。"
        "只输出 JSON：{\"items\": [{\"index\": 1, \"subject\": \"...\"}]}")
    try:
        r = provider.chat_json([{"role": "system", "content": sys_prompt},
                                {"role": "user", "content": "商品清单：\n" + "\n".join(lines)}], fallback=None)
    except AIProviderError:
        return {}
    out = {}
    for it in (r.get("items") if isinstance(r, dict) else []) or []:
        try:
            out[int(it.get("index"))] = str(it.get("subject") or "").strip()[:300]
        except (TypeError, ValueError):
            continue
    return out


CARD_FIELDS = (("item", "商品"), ("material", "材质成分"), ("construction", "构造工艺"), ("function", "功能用途"),
               ("form", "形态"), ("packaging", "包装销售"), ("user", "使用者场景"), ("specs", "规格"))
_CARD_RULES = (
    "把商品描述整理成归类要素表。只填描述里明确说了的；没说的字段填 null，并把归类可能用到却没提到的事实"
    "用中文短语列进 missing（如 \"面料是针织还是梭织\"、\"羊毛含量比例\"、\"是否零售包装\"）。字段："
    "item 这是什么（一句话，含名称与类别）；material 材质/成分及比例；construction 构造/工艺（针织/梭织/层压/涂层/铸造/组装…）；"
    "function 功能/用途；form 形态（整机/零件/成套/散件/半成品/原料）；packaging 包装与销售形式；"
    "user 使用者/场景（家用/工业/医用/儿童/宠物…）；specs 规格（尺寸/重量/价值/功率/含量等）；missing 未提及清单。")


def attribute_card_enabled(cfg=None):
    cfg = cfg if cfg is not None else load_config()
    return bool(cfg.get("attribute_card"))


def _clean_card(raw):
    """模型输出 → 规整的要素表 dict（值为字符串或 None；missing 为字符串列表）。"""
    if not isinstance(raw, dict):
        return {}
    card = {}
    for key, _label in CARD_FIELDS:
        v = raw.get(key)
        if isinstance(v, (list, tuple)):
            v = "；".join(str(x) for x in v if str(x).strip())
        v = str(v).strip() if v not in (None, "", "null", "None") else ""
        card[key] = v[:200] if v else None
    miss = raw.get("missing") or []
    if isinstance(miss, str):
        miss = [miss]
    card["missing"] = [str(m).strip()[:60] for m in miss if str(m).strip()][:8]
    return card


def _ai_attribute_card(provider, description):
    """单条：描述 → 要素表。失败返回 {}（归类照常，只是没有表）。"""
    try:
        r = provider.chat_json([{"role": "system", "content": "你是美国海关归类助手。" + _CARD_RULES + " 只输出 JSON。"},
                                {"role": "user", "content": f"商品描述：{description}"}], fallback=None)
    except AIProviderError:
        return {}
    return _clean_card(r)


def _ai_attribute_cards_batch(provider, names):
    """清单：一次调用给全部商品各出一张表。返回 {序号: card}，失败 {}。"""
    lines = [f"{i + 1}. {n}" for i, n in enumerate(names)]
    try:
        r = provider.chat_json([{"role": "system", "content": "你是美国海关归类助手。为下列每个商品" + _CARD_RULES
                                 + " 只输出 JSON：{\"items\": [{\"index\": 1, \"item\": …, \"missing\": […]}]}"},
                                {"role": "user", "content": "商品清单：\n" + "\n".join(lines)}], fallback=None)
    except AIProviderError:
        return {}
    out = {}
    for it in (r.get("items") if isinstance(r, dict) else []) or []:
        try:
            out[int(it.get("index"))] = _clean_card(it)
        except (TypeError, ValueError):
            continue
    return out


def card_text(card):
    """要素表 → 一行文本，给提示词用。空表返回空串。"""
    if not card:
        return ""
    parts = [f"{label}：{card.get(key)}" for key, label in CARD_FIELDS if card.get(key)]
    if card.get("missing"):
        parts.append("未提及：" + "、".join(card["missing"]))
    return "；".join(parts)


def _merge_need_verify(need, card, cap=6):
    """把要素表的"未提及"并进需确认（去重、保序）。"""
    out = [str(v)[:80] for v in (need or []) if str(v).strip()]
    for m in (card or {}).get("missing") or []:
        q = f"{m}（描述未提及）"
        if all(m not in x for x in out):
            out.append(q)
    return out[:cap]


def recall_rewrite_enabled(cfg=None):
    cfg = cfg if cfg is not None else load_config()
    return bool(cfg.get("recall_rewrite"))


def _recall_candidates(db, keywords, limit=40, unit_value=None, origin="CN", description=None, queries=None):
    """
    召回候选 8 位子目：关键词 + 税则行语义 + 裁定先例三通道 RRF 融合（rate.hybrid_search）。

    此前只有关键词一条通道，模型出的英文词在税则里对不上就召回为空，精排看不见的
    东西救不回来。语义与先例通道拿**原始描述**（description，多为中文）检索，
    关键词通道拿模型出的英文词；任一通道不可用自动只剩其余通道。
    排序固定 relevance（融合序）：这批行是要送进精排的候选池。
    unit_value / origin 只影响补的总税负列，不影响召回顺序——但这些行会与本地行
    同表展示，口径必须跟着调用方走。
    """
    import rate

    kw = " ".join(keywords) if isinstance(keywords, (list, tuple)) else str(keywords or "")
    rows, _status = rate.hybrid_search(db, kw, limit=limit, sort="relevance",
                                       unit_value=unit_value, origin=origin,
                                       description=description, queries=queries)
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


def _ai_rerank(provider, db, description, rows, top_n, card=None):
    """
    第二轮：在已召回的候选里精排。返回 picks（模型原始输出，未校验）。

    候选行带归类路径与判定条件（见 _candidate_line）——末级品名大量是
    "Other"，只给品名等于让模型盲选。card：要素表（有则随描述一起给，"未提及"的属性不得当成满足）。
    """
    cand_lines = [_candidate_line(db, i, r) for i, r in enumerate(rows, 1)]
    ct = card_text(card)
    card_note = (f"\n归类要素表（只填了描述明确说了的；「未提及」的属性不能当作满足，应写进 need_verify）：{ct}\n"
                 if ct else "")
    sys_prompt2 = (
        "你是美国 HTS 归类专家。下面是从税则库检索出的候选子目。"
        "每行格式：序号. 编码 | 归类路径（父级 > 子级，判定条件多在父级上）| 税率 | 301 | 判定条件。"
        f"请为商品「{description}」选择最合适的 {top_n} 个候选，按匹配度排序。{card_note}\n"
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

    # FLIP 301 与 301 排除也要进候选行。此前只给"301: 是 +25%"，模型是拿一份
    # 残缺的税负信息在挑码：带 Aircraft/Pharma 范围限制的 FLIP 12.5%、
    # 以及整号排除掉的那 25%，模型都看不到。
    flip = f" | FLIP301: {row['FLIP 301加征']}" if row.get("FLIP 301加征") else ""
    excl = f" | 301排除: {row['301排除']}" if row.get("301排除") else ""
    line = (f"{i}. {row['编码']} | {path_txt} | 一般税率 {row['一般税率']} | "
            f"301: {row['301判定']} {row['301加征']}{flip}{excl}")
    if cr_txt:
        line += f" | 判定条件: {cr_txt}"
    # 先例证据：先例通道（CBP 裁定 kNN 投票）是三条召回里最强的（正文金标 r@5 0.69），
    # 此前候选行里不带它，模型等于拿着最弱的信号在挑。2026-09 正文金标 60 条消融：
    # 只加这一项 top-1 19 → 26、top-3 26 → 31。
    line += _precedent_evidence(row)
    cp = row.get("公司先例")
    if cp:
        line += (f" | 本公司此前申报：「{cp.get('品名', '')}」→ {cp.get('编码', '')}"
                 f"（{str(cp.get('时间', ''))[:10]}，{cp.get('来源', '')}，相似 {cp.get('相似度', 0):.2f}）")
    return line


def _precedent_evidence(row, max_rulings=2):
    """候选行末尾的先例证据："| CBP 先例 3.2 票：N330020(2023) “trailer transition plate”；…"。"""
    votes = row.get("先例票")
    if not votes:
        return ""
    nums = [n for n in (row.get("先例裁定") or []) if isinstance(n, str)][:max_rulings]
    try:
        import cross
        meta = cross.ruling_subjects(nums)
    except Exception:
        meta = {}
    ev = "；".join(
        f"{n}({meta[n][1]}) “{meta[n][0][:70]}”" if n in meta and meta[n][0] else n
        for n in nums)
    return f" | CBP 先例 {votes} 票" + (f"：{ev}" if ev else "")


# ---------- AI 功能 ----------

def _candidate_from_row(db, row, origin, confidence, reason, need_verify):
    """
    候选行 + 模型的选择 → 返回给调用方的候选。用候选行自身的编码算税，不用模型给的字符串。
    归类路径与判定条件来自本地税则，不经模型——模型只负责选，不负责论证。
    """
    code8 = re.sub(r"\D", "", row["编码"])[:8]
    total = rate_calc(db, code8, origin=origin)
    crs = _criteria_safe(db, code8)
    return {
        "编码": row["编码"],
        "商品描述": row["商品描述"],
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
        # 警示字段必须跟着数字一起走。只给"总税负 37.5%"而不说其中 12.5% 取决于
        # 用途、25% 可能已被整号排除，比给错数更糟——它看着像个确定的结论。
        "FLIP 301加征": (total or {}).get("FLIP 301加征", ""),
        "FLIP 301说明": (total or {}).get("FLIP 301说明", ""),
        "301排除": (total or {}).get("301排除", ""),
        "301排除明细": (total or {}).get("301排除明细", []),
        "备注": (total or {}).get("备注", ""),
        "总税负估算": total["总税负估算"] if total else "",
        "confidence": _clamp_confidence(confidence),
        "reason": str(reason or "")[:400],
        "需确认": [str(v)[:80] for v in (need_verify or [])][:5],
    }


def _candidate_from_code(db, code8, origin, confidence, reason, need_verify):
    """逐级链给出的编码可能不在召回池里，按编码直接构造一行再走同一个候选构造。"""
    import core
    import rate
    row = rate._make_row(db, code8, 0.0, core.load_measures_config())
    return _candidate_from_row(db, row, origin, confidence, reason, need_verify)


def _escalation_reason(top, chapters, gs, pool_codes=()):
    """
    平铺结果要不要升级，返回原因（空串 = 不升级）。四条规则任一命中：
      置信度低于阈值 / 带存疑信号 / 第一名落在注释决定章（默认 28–38）/ 前几名跨章。
    pool_codes：用来判跨章的编码列表（单条模式给平铺前三，清单模式给池内前五）。
    原因写进结果的「升级原因」，审核员能看到这一行为什么走了逐级链。
    """
    if not top:
        return "平铺未给出有效编码"
    conf = top.get("confidence") or 0
    code = re.sub(r"\D", "", str(top.get("编码") or ""))
    if conf < gs["threshold"]:
        return f"置信度 {conf:.2f} 低于阈值 {gs['threshold']:g}"
    flags = _classify_flags(conf, chapters, top.get("编码"))
    if flags:
        return "存疑：" + "；".join(flags)
    if code[:2] in gs.get("force_chapters", ()):
        return f"第 {code[:2]} 章属注释决定章（配置 guided_force_chapters）"
    if gs.get("cross_chapter"):
        chs = {re.sub(r"\D", "", str(c))[:2] for c in pool_codes if re.sub(r"\D", "", str(c))}
        if len(chs) >= 2:
            return f"候选跨章（{'/'.join(sorted(chs))}）"
    return ""


def _company_exact(rows):
    """召回行里有没有与品名精确一致的本公司先例（相似度 1.0）。有则返回那一行。"""
    for r in rows or []:
        cp = r.get("公司先例")
        if cp and (cp.get("相似度") or 0) >= 0.999:
            return r
    return None


def _company_candidate(db, row, origin):
    cp = row["公司先例"]
    c = _candidate_from_row(db, row, origin, 0.99,
                            f"与本公司 {str(cp.get('时间', ''))[:10]} 的申报一致（{cp.get('来源', '')}：「{cp.get('品名', '')}」）"
                            + (f"；{cp['说明']}" if cp.get("说明") else ""), [])
    c["归类方式"] = "公司先例"
    c["公司先例"] = cp
    return c


def _needs_escalation(top, chapters, threshold):
    """兼容旧调用：只看阈值与存疑。"""
    gs = {"threshold": threshold, "force_chapters": set(), "cross_chapter": False}
    return bool(_escalation_reason(top, chapters, gs))


class _guided_context:
    """
    升级链的调用环境：推理强度换成 guided_effort（空 = 不传，用模型默认），超时抬到 ≥ 180 秒
    ——品目步带 1.3 万 token 注释，实测慢中转站 30 秒以上，默认 60 秒会掐掉。
    对没有这些属性的 Provider（测试假件）什么都不做。
    """

    def __init__(self, provider, effort):
        self.p, self.effort, self.saved = provider, effort, {}

    def __enter__(self):
        if hasattr(self.p, "reasoning_effort"):
            self.saved["reasoning_effort"] = self.p.reasoning_effort
            self.p.reasoning_effort = self.effort
        if hasattr(self.p, "timeout"):
            self.saved["timeout"] = self.p.timeout
            try:
                self.p.timeout = max(float(self.p.timeout or 0), 180.0)
            except (TypeError, ValueError):
                pass
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            setattr(self.p, k, v)
        return False


def _run_guided(db, description, origin, provider, rows, exclude_ruling, gs, keywords=None, chapters=None, card=None):
    """跑一次逐级链（scripts/guided.py），任何异常都收成 {"error"}，不影响平铺结果。"""
    try:
        import guided
        with _guided_context(provider, gs["effort"]):
            return guided.classify_guided(db, description, origin=origin, provider=provider, rows=rows,
                                          keywords=keywords, chapters=chapters, exclude_ruling=exclude_ruling,
                                          max_calls=gs["max_calls"], card=card_text(card))
    except Exception as e:  # 升级是锦上添花，平铺结果必须保住
        return {"error": f"逐级归类异常：{e}"}


def _run_guided_batch(db, jobs, origin, provider, gs, on_done=None):
    """
    清单模式：一批升级行并发跑逐级链。jobs: [(key, description, rows, chapters, card), …]；
    返回 {key: 结果或 {"error"}}。推理强度 / 超时的上下文在整批外面进出一次——
    _guided_context 改的是共享 provider 的属性，线程里各自进出会互相覆盖。
    并发数 gs["parallel"]（1 = 串行，测试与假 Provider 用）。on_done(key, result) 每完成一行回调一次。
    """
    import guided
    out = {}

    def one(job):
        key, desc, rows, chapters, card = job
        try:
            return key, guided.classify_guided(db, desc, origin=origin, provider=provider, rows=rows,
                                               chapters=chapters, max_calls=gs["max_calls"], card=card_text(card))
        except Exception as e:  # 升级是锦上添花，一行的异常不能拖垮整批
            return key, {"error": f"逐级归类异常：{e}"}

    with _guided_context(provider, gs["effort"]):
        n = max(1, int(gs.get("parallel") or 1))
        if n == 1 or len(jobs) <= 1:
            for job in jobs:
                key, g = one(job)
                out[key] = g
                if on_done:
                    on_done(key, g)
        else:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            with ThreadPoolExecutor(max_workers=min(n, len(jobs))) as ex:
                futs = [ex.submit(one, job) for job in jobs]
                for f in as_completed(futs):
                    key, g = f.result()
                    out[key] = g
                    if on_done:
                        on_done(key, g)
    return out


def classify_product(db, description, top_n=3, origin="CN", force_guided=False, exclude_ruling=None):
    """
    商品描述 → HTS 编码推荐。

    流程：
      1. LLM：中文/英文描述 → 英文搜索关键词 + 章号建议（JSON）
      2. 本地召回：按关键词在税则库搜索 top 40
      3. LLM：从候选编码中挑选最合适的 top_n，输出编码 + 置信度 + 理由（JSON）
      4. 本地引擎校验：对推荐编码计算 301 状态与总税负
      5. 升级（配置 guided 开启时）：平铺置信度低于阈值 / 带存疑信号 / 无有效结果的，
         再走 GRI 逐级链（注释 → 品目 → 子目 → 先例），逐级结果排第一并带「论证」，
         平铺的第一名记在「平铺结果」里供对照。force_guided=True 不看阈值直接升级（API / 评测用）。

    返回：{'candidates': [...], 'keywords': [...], '归类方式': '平铺'|'逐级', 'disclaimer': str}
    exclude_ruling：留一法评测时从升级链的先例路径里剔除的裁定号。
    """
    provider = get_provider()
    if provider is None:
        return {"error": "AI 服务未配置，无法进行智能归类。请先在 ai_config.json 配置。"}

    # 第零轮：归类要素表（配置开关；失败不影响归类）
    card = _ai_attribute_card(provider, description) if attribute_card_enabled() else {}

    # 第一轮：出关键词
    try:
        keywords, chapters = _ai_keywords(provider, description)
    except AIProviderError as e:
        return {"error": f"AI 归类失败：{e}"}
    if not keywords:
        return {"error": "AI 未能提取商品关键词，请尝试更详细的商品描述。"}

    # 本地召回。AI 关键词全落空时降级为原文检索（走同义词表），
    # 而不是直接报"库里没有"——那会把模型的失误说成数据的缺失。
    extra = [_ai_subject_line(provider, description)] if recall_rewrite_enabled() else None
    rows = _recall_candidates(db, keywords, description=description, queries=extra)
    degraded = ""
    if not rows:
        rows = _recall_candidates(db, [description], description=description, queries=extra)
        if rows:
            degraded = (f"AI 给出的检索词（{' '.join(keywords)}）在税则库中无匹配，"
                        f"已降级为按原文检索，候选质量可能下降")
    if not rows:
        return {"error": f"本地税则库未找到与「{description}」匹配的商品（关键词：{' '.join(keywords)}），请尝试调整描述。"}
    rows = _rank_by_chapters(rows, chapters)

    # 本公司先例精确命中：上次就是这么报的，不再花模型调用，也不升级；仍给平铺的其它候选做对照的事交给 UI
    hit = _company_exact(rows)
    if hit:
        return {"candidates": [_company_candidate(db, hit, origin)], "归类方式": "公司先例",
                "keywords": keywords, "chapters": chapters, "跨章": False, "降级": degraded,
                "disclaimer": "编码来自本公司此前的申报记录（审核员采纳 / 改正的结论），税率由本地税则计算。"
                              "商品有变化时请重新归类。"}

    # 第二轮：精排（平铺）。失败不立刻返回：配置了升级链时还有第二条路
    try:
        picks = _ai_rerank(provider, db, description, rows, top_n, card=card)
        flat_error = "" if picks else "AI 未返回有效归类结果，请重试或联系人工复核。"
    except AIProviderError as e:
        picks, flat_error = [], f"AI 归类失败：{e}"

    # 引擎校验：编码两边都归一化后比较。
    # 候选表的键带点（'8507.60.00'），模型却常返回不带点的 '85076000'，
    # 若只比字面量会把大量合法结果误判成幻觉。
    candidates = []
    code_by_norm = {re.sub(r"\D", "", r["编码"]): r for r in rows}
    for pk in picks[:top_n]:
        row = code_by_norm.get(re.sub(r"\D", "", str(pk.get("code") or "")))
        if not row:
            continue
        candidates.append(_candidate_from_row(db, row, origin, pk.get("confidence"), pk.get("reason", ""),
                                              _merge_need_verify(pk.get("need_verify"), card)))
    if picks and not candidates:
        flat_error = "AI 返回的编码不在候选列表中，请重试。"

    # 升级：平铺置信度不够 / 带存疑信号 / 没有有效结果 → GRI 逐级链
    gs = guided_settings()
    top = candidates[0] if candidates else None
    result = {"candidates": candidates, "归类方式": "平铺"} if candidates else None
    reason = ("强制" if force_guided else
              (_escalation_reason(top, chapters, gs, [c["编码"] for c in candidates[:3]]) if gs["enabled"] else ""))
    if reason:
        g = _run_guided(db, description, origin, provider, rows, exclude_ruling, gs, keywords, chapters, card=card)
        if "error" in g:
            if result is None:
                return {"error": flat_error or "AI 未返回有效归类结果", "升级失败": g["error"]}
            result["升级失败"] = g["error"]
        else:
            gc = _candidate_from_code(db, g["code8"], origin, g["confidence"], g["reason"],
                                      _merge_need_verify(g["需确认"], card))
            gc["归类方式"] = "逐级"
            gc["论证"] = g["论证"]
            others = [c for c in candidates if c["编码"] != gc["编码"]]
            result = {"candidates": [gc] + others[:max(0, top_n - 1)], "归类方式": "逐级",
                      "论证": g["论证"], "平铺结果": top["编码"] if top else "", "升级原因": reason}
    if result is None:
        return {"error": flat_error}
    if card:
        result["要素表"] = card
    result.update({
        "keywords": keywords,
        "chapters": chapters,
        "跨章": len({c["编码"][:2] for c in result["candidates"]}) > 1,
        "降级": degraded,
        "disclaimer": "AI 归类结果仅供参考，正式报关归类以 CBP 裁定与海关税则为准，请人工复核。"
                      "各候选的「判定条件」与「证据清单」来自官方税则原文，可作为论证依据。",
    })
    return result


def classify_guided_only(db, description, origin="CN", supplement=""):
    """
    直接走 GRI 逐级链（搜索页「逐级归类」按钮 / API）：出词 → 召回 → 逐级，不跑平铺精排。
    supplement：用户对"缺事实 / 需确认"的补充说明（"TPU 膜在外表面但不完全遮蔽底布"），
    拼在描述后重跑整条链——这是归类员"问一句再定"的那一步，结果里原样记下补充了什么。
    返回与 classify_product 同形（candidates[0] 带「论证」），失败 {"error"}。
    """
    provider = get_provider()
    if provider is None:
        return {"error": "AI 服务未配置，无法进行逐级归类。请先在 ai_config.json 配置。"}
    desc = (description or "").strip()
    if not desc:
        return {"error": "请输入商品描述"}
    supplement = (supplement or "").strip()[:1000]
    if supplement:
        desc = f"{desc}\n补充说明（用户核实后提供）：{supplement}"
    card = _ai_attribute_card(provider, desc) if attribute_card_enabled() else {}
    try:
        keywords, chapters = _ai_keywords(provider, desc)
    except AIProviderError as e:
        return {"error": f"AI 调用失败：{e}"}
    extra = [_ai_subject_line(provider, desc)] if recall_rewrite_enabled() else None
    rows = _recall_candidates(db, keywords or [desc], description=desc, queries=extra)
    if not rows and keywords:
        rows = _recall_candidates(db, [desc], description=desc, queries=extra)
    if not rows:
        return {"error": f"本地税则库未找到与「{desc}」匹配的候选，无法开始逐级归类"}
    rows = _rank_by_chapters(rows, chapters)
    gs = guided_settings()
    g = _run_guided(db, desc, origin, provider, rows, None, gs, keywords, chapters, card=card)
    if "error" in g:
        return {"error": g["error"], "论证": g.get("论证", {})}
    gc = _candidate_from_code(db, g["code8"], origin, g["confidence"], g["reason"], _merge_need_verify(g["需确认"], card))
    gc["归类方式"] = "逐级"
    gc["论证"] = g["论证"]
    return {"candidates": [gc], "归类方式": "逐级", "论证": g["论证"], "keywords": keywords, "chapters": chapters,
            "跨章": False, "降级": "", "补充": supplement, **({"要素表": card} if card else {}),
            "disclaimer": "逐级归类依据本地税则的类注、章注与附加美国注释（不含 WCO 解释性注释），"
                          "先例来自 CROSS 镜像；税率与判定条件仍由本地税则计算。正式归类以 CBP 裁定为准。"}


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
    local_rows, _st = rate.hybrid_search(db, keyword, limit=limit, sort=sort,
                                         unit_value=unit_value, origin=origin)
    local_codes = {re.sub(r"\D", "", str(r["编码"])) for r in local_rows}

    ai_rows = _recall_candidates(db, keywords, limit=limit,
                                 unit_value=unit_value, origin=origin, description=keyword)
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


# 置信度低于此值即标存疑：AI 自己没把握，用户该复核
_LOW_CONF = 0.6


def _classify_flags(confidence, suggested_chapters, code):
    """
    归类结果的存疑信号列表。空列表 = 无明显疑点。

    两个正交信号：
    - 低置信度：AI 自认没把握。
    - 建议章 vs 结果章打架：关键词步骤猜的章与最终编码的章不一致，说明这条在
      AI 内部就是矛盾的。实测「不锈钢菜刀」建议章 85、结果落到 72 章（钢铁废料，
      应为 8211 刀具），置信度却 0.8——低置信度抓不到，靠这个矛盾才标得出来。
      建议章是"加权不过滤"的软信号，所以不一致不等于一定错，只作提示。
    """
    flags = []
    if confidence and confidence < _LOW_CONF:
        flags.append(f"AI 置信度偏低（{confidence:.0%}），建议人工复核")
    ch = re.sub(r"\D", "", str(code or ""))[:2]
    sugg = [str(c).zfill(2)[:2] for c in (suggested_chapters or []) if str(c).strip()]
    if ch and sugg and ch not in sugg:
        flags.append(f"AI 建议章 {'/'.join(sugg)} 与结果编码章 {ch} 不一致，"
                     f"可能召回或归类有偏，请核对是否归错大类")
    return flags


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
            f"{r.get('301加征', '')}"
            + (f" | FLIP301: {r.get('FLIP 301加征')}" if r.get("FLIP 301加征") else "")
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


def _detail_from_row(db, i, it, chosen, conf, reason, note, chs, rows, origin):
    """清单里一行的结果。平铺精排与逐级升级都走这里，列一致。"""
    total = rate_calc(db, re.sub(r"\D", "", chosen["编码"]),
                      unit_value=it.get("unit_value"), origin=origin)
    chosen_norm = re.sub(r"\D", "", chosen["编码"])
    return {
        "序号": i,
        "品名": it.get("name", ""),
        "编码": chosen["编码"],
        "商品描述": chosen["商品描述"],
        "一般税率": chosen["一般税率"],
        "税率类型": chosen["税率类型"],
        "301判定": chosen["301判定"],
        "301加征": chosen["301加征"],
        "9903子目": chosen["9903子目"],
        "FLIP 301加征": (total or {}).get("FLIP 301加征", ""),
        "301排除": (total or {}).get("301排除", ""),
        "总税负估算": total["总税负估算"] if total else "",
        "confidence": conf,
        "reason": reason,
        # 走了降级检索的行要标出来，否则用户无从判断这条为什么质量偏低
        "备注": note,
        # 存疑信号：让"AI 归错但装得很自信"的行在界面上可见。实测「不锈钢菜刀」
        # 被归到 7204 钢铁废料（应为 8211 刀具），置信度却给到 0.8——单看置信度
        # 抓不到，但 AI 建议章(85) 与结果编码章(72) 打架，这个矛盾能标出来。
        "存疑": _classify_flags(conf, chs, chosen["编码"]),
        # 同批召回里 AI 没选的那几个，一并交出去。
        #
        # 实测同一句品名连跑 5 次会得到 3 个不同编码、置信度全是 0.9——
        # 单数形式的"编码"一栏因此是有误导性的：它把一次抽样说成了一个结论。
        # 这些候选本来就在内存里（召回的前 12 条），带出去零成本，
        # 让人能看见 AI 是在什么范围里挑的、被它放过的是什么。
        #
        # 刻意不做的两件事：① 不按"多次采样一致率"排序——按实测那 5 次做
        # 多数表决会选出 7309（3/5，错的），一致性衡量的是模型的惯性而非正确性；
        # ② 不标"AI 选的不在本地检索前 N 名"——拿完整品名验证时，它把正确答案
        # 标记了、把错误答案放过了（「不锈钢菜刀」归到废碎料时本地检索还把废碎料
        # 排第 1）。会在正确答案上报警的提示，只会训练人忽略所有提示。
        "候选": [
            {"编码": r["编码"], "商品描述": r["商品描述"],
             "一般税率": r["一般税率"],
             "总税负估算": (rate_calc(db, re.sub(r"\D", "", r["编码"]),
                                unit_value=it.get("unit_value"),
                                origin=origin) or {}).get("总税负估算", "")}
            for r in rows[:6]
            if re.sub(r"\D", "", r["编码"]) != chosen_norm
        ][:5],
    }


def _needs_escalation_detail(d, chs, gs, rows=()):
    """清单行要不要升级，返回原因（空 = 不升级）。跨章看池内前五（清单模式每行只有一个 pick）。"""
    if not d:
        return ""
    if "error" in d:
        return "平铺精排未给出有效编码"
    conf = d.get("confidence") or 0
    if conf < gs["threshold"]:
        return f"置信度 {conf:.2f} 低于阈值 {gs['threshold']:g}"
    if d.get("存疑"):
        return "存疑：" + "；".join(d["存疑"])
    top = {"confidence": conf, "编码": d.get("编码")}
    return _escalation_reason(top, [], gs, [r["编码"] for r in list(rows)[:5]])


def analyze_list_stream(db, items, origin="CN"):
    """
    商品清单批量分析（生成器版）：边算边往外吐进度与阶段结果。

    **这里不是逐行调 AI，是三次批量调用**：① 一次出全部检索词 ② 本地逐行召回
    ③ 一次批量精排 ④ 一次生成汇总报告。所以"逐行动画"是假的，能吐的是真实阶段。

    真正值钱的是把 details 和 report 分开吐：实测 4 行清单总耗时 14.3s，其中
    出词 5.2s / 精排 4.7s / 报告 4.2s——**表格在精排结束时就齐了，却要陪着
    报告再等 4.2 秒**（29% 的等待）。分开之后表格先落地，报告后到。

    items: [{'name': 品名/描述, 'quantity': 数量(可选), 'unit_value': 单位货值USD(可选)}, ...]
    产出事件（dict）：
      {"type": "stage",   "stage": 阶段键, "text": 人话, "done": 已完成行数, "total": 总行数}
      {"type": "details", "details": [...], "stats": {...}}   表格数据，可直接渲染
      {"type": "report",  "report": "markdown"}
      {"type": "done",    "result": {report, details, stats}} 最终整包（供非流式复用）
      {"type": "error",   "message": "..."}
    """
    def _err(msg):
        return {"type": "error", "message": msg}

    provider = get_provider()
    if provider is None:
        yield _err("AI 服务未配置。请先在 ai_config.json 配置。")
        return
    if not items:
        yield _err("清单为空。")
        return
    if len(items) > 30:
        yield _err(f"单次最多分析 30 个商品（当前 {len(items)} 个），请分批。")
        return

    n = len(items)
    yield {"type": "stage", "stage": "keywords",
           "text": f"AI 正在为 {n} 行商品出税则检索词", "done": 0, "total": n}

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
        yield _err(f"AI 调用失败：{e}")
        return

    kw_map = {}
    for it in (r1.get("items") if isinstance(r1, dict) else []) or []:
        idx = int(it.get("index", 0))
        kws = [str(k) for k in (it.get("keywords") or []) if str(k).strip()]
        chs = [str(c).zfill(2) for c in (it.get("chapters") or [])]
        kw_map[idx] = (kws, chs)
    # 英文 subject 改写：一次批量调用，给语义与先例通道做第二路查询（配置开关）
    subj_map = _ai_subject_lines_batch(provider, [it.get("name", "") for it in items]) if recall_rewrite_enabled() else {}
    # 归类要素表：一次批量调用（配置开关）
    card_map = _ai_attribute_cards_batch(provider, [it.get("name", "") for it in items]) if attribute_card_enabled() else {}

    yield {"type": "stage", "stage": "recall",
           "text": "在本地税则库中召回候选", "done": 0, "total": n}

    # 逐商品本地召回（第一候选）
    recall_failed = {}  # 序号 → 召回失败的说明
    details_map = {}    # 序号 → 结果行（公司先例精确命中在召回阶段就写进来）
    pending = []  # 需要精排的 (序号, 候选行列表, 原始条目, 降级说明)
    for i, it in enumerate(items, 1):
        name = it.get("name", "")
        kws, chs = kw_map.get(i, ([], []))
        extra = [subj_map[i]] if subj_map.get(i) else None
        rows = _recall_candidates(db, kws, limit=20, description=name, queries=extra) if kws else []
        # 章号只加权不过滤（同 classify_product）。硬过滤时模型猜错章会把正确
        # 候选整条滤掉，然后这一行报"本地库未匹配"——清单里几十行，用户看到的
        # 是"库里没有"，真实原因却是模型猜错了章。
        rows = _rank_by_chapters(rows, chs, limit=20)
        note = ""
        if not rows and name:
            # AI 关键词全落空时退回按原文检索，而不是直接判这行无解
            rows = _recall_candidates(db, [name], limit=20, description=name, queries=extra)
            if rows:
                note = f"AI 检索词（{' '.join(kws)}）无匹配，已降级为按品名原文检索"
        hit = _company_exact(rows)
        if hit:
            # 本公司先例精确命中：直接出结论，不进批量精排
            cp = hit["公司先例"]
            d = _detail_from_row(db, i, it, hit, 0.99,
                                 f"与本公司 {str(cp.get('时间', ''))[:10]} 的申报一致（{cp.get('来源', '')}）", note, chs, rows, origin)
            d.update({"归类方式": "公司先例", "公司先例": cp, "存疑": []})
            details_map[i] = d
            yield {"type": "stage", "stage": "recall", "text": "在本地税则库中召回候选", "done": i, "total": n}
            continue
        if not rows:
            # 写进 details_map 而不是 details——末尾会按 details_map 重建整个列表，
            # 早先 append 到 details 的内容会被整个丢掉（原实现里那句 append
            # 就是死代码，所有召回失败最终都塌成一句笼统的"本地库未匹配"）。
            recall_failed[i] = {
                "序号": i, "品名": name,
                "error": f"本地库未匹配（AI 检索词：{' '.join(kws) or '无'}；已按品名原文重试）",
            }
            continue
        pending.append((i, rows[:12], it, note, chs))
        # 召回是本轮唯一真正逐行跑的环节，进度条在这里动是实的
        yield {"type": "stage", "stage": "recall",
               "text": "在本地税则库中召回候选", "done": i, "total": n}

    yield {"type": "stage", "stage": "rank",
           "text": f"AI 正在从候选中为 {len(pending)} 行精排定码", "done": 0, "total": n}

    # 第二轮：批量精排（details_map 已在召回阶段建好，公司先例命中的行已在里面）
    if pending:
        # 候选行与单条归类走同一个 _candidate_line：带归类路径、判定条件、FLIP 与排除。
        # 此前批量模式只给"编码(描述前 40 字, 税率)"且只给 6 条——末级品名大量是 Other，
        # 等于让模型盲选；单条模式早就不这么干了，批量却一直没跟上。
        batch_lines = []
        for i, rows, it, _note, _chs in pending:
            ct = card_text(card_map.get(i))
            batch_lines.append(f"{i}. 商品「{it.get('name', '')}」" + (f"（要素表：{ct}）" if ct else "") + "候选：")
            batch_lines.extend("   " + _candidate_line(db, f"{i}-{j}", r)
                               for j, r in enumerate(rows[:12], 1))
        sys_prompt2 = (
            "你是美国 HTS 归类专家。下面按商品序号列出每个商品的候选子目，"
            "每个候选行格式：商品序号-候选序号. 编码 | 归类路径（父级 > 子级，判定条件多在父级上）"
            "| 税率 | 301 | 判定条件。为每个商品选择最匹配的一个 8 位编码。"
            "选择时必须依据归类路径中的实际措辞（材质、织法、含量阈值、涂层、重量/尺寸/价值门槛），"
            "不要只看末级品名——末级常常只是 'Other'。reason 引用候选行原文。"
            "商品后括号里的要素表只填了描述明确说了的，「未提及」的属性不能当作满足。"
            "只输出 JSON：{\"picks\": [{\"index\": 商品序号, \"code\": \"8位编码\", "
            "\"confidence\": 0.9, \"reason\": \"引用原文的一句话理由\"}]}，code 必须来自该商品的候选。"
        )
        try:
            r2 = provider.chat_json(
                [{"role": "system", "content": sys_prompt2},
                 {"role": "user", "content": "\n".join(batch_lines)}],
                fallback=None,
            )
        except AIProviderError as e:
            yield _err(f"AI 调用失败：{e}")
            return
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

        for i, rows, it, note, chs in pending:
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
            details_map[i] = _detail_from_row(db, i, it, chosen, _clamp_confidence(pk.get("confidence")),
                                              pk.get("reason", ""), note, chs, rows, origin)
            if card_map.get(i):
                details_map[i]["要素表"] = card_map[i]
                details_map[i]["需确认"] = _merge_need_verify([], card_map[i])

    # 升级：置信度不够 / 带存疑 / 精排没给出有效编码的行，再走 GRI 逐级链。
    # 这是本轮唯一会逐行调模型的环节，进度按行报是实的。
    gs = guided_settings()
    if pending and gs["enabled"]:
        esc = []
        for i, rows, it, _note, chs in pending:
            why = _needs_escalation_detail(details_map.get(i), chs, gs, rows)
            if why:
                esc.append((i, rows, it, chs, why))
        if esc:
            yield {"type": "stage", "stage": "guided",
                   "text": f"{len(esc)} 行置信度不足，升级到 GRI 逐级归类（注释 → 品目 → 子目 → 先例）",
                   "done": 0, "total": len(esc)}
        # 并发跑（gs["parallel"] 路），完成一行就把它写回 details_map 并报一次进度。
        # 生成器不能从工作线程里 yield，所以先在池里收结果，主线程按完成顺序吐事件。
        jobs = [(i, it.get("name", ""), rows, chs, card_map.get(i)) for i, rows, it, chs, _why in esc]
        why_of = {i: why for i, _rows, _it, _chs, why in esc}
        it_of = {i: (rows, it) for i, rows, it, _chs, _why in esc}
        results = _run_guided_batch(db, jobs, origin, provider, gs) if jobs else {}
        for k, i in enumerate([j[0] for j in jobs], 1):
            g = results.get(i) or {"error": "升级未返回结果"}
            rows, it = it_of[i]
            why = why_of[i]
            d = details_map.get(i) or {}
            if "error" in g:
                d["升级失败"] = g["error"]
                details_map[i] = d
            else:
                import core as _core
                import rate as _rate
                row = _rate._make_row(db, g["code8"], 0.0, _core.load_measures_config())
                # chs 传空：存疑里的"建议章 vs 结果章打架"针对的是平铺挑码，逐级链的章是读过注释后
                # 定的，不再拿平铺第一轮猜的章号去质疑它；低置信度这一条信号照常保留
                nd = _detail_from_row(db, i, it, row, g["confidence"], g["reason"], d.get("备注", ""), [], rows, origin)
                nd.update({"归类方式": "逐级", "论证": g["论证"], "平铺结果": d.get("编码", ""), "升级原因": why,
                           "需确认": _merge_need_verify(g.get("需确认"), card_map.get(i))})
                if card_map.get(i):
                    nd["要素表"] = card_map[i]
                if "error" in d:
                    nd["平铺结果"] = ""
                    nd["备注"] = (nd.get("备注") or "") + "（平铺精排未给出有效编码，本行由逐级链归类）"
                details_map[i] = nd
            yield {"type": "stage", "stage": "guided", "text": "升级到 GRI 逐级归类", "done": k, "total": len(esc)}

    details = [details_map.get(i) or recall_failed.get(i)
               or {"序号": i, "品名": it.get("name", ""),
                   "error": "未返回该行结果（AI 精排遗漏），请重试或单独查询"}
               for i, it in enumerate(items, 1)]

    # 统计
    hit = sum(1 for d in details if d.get("301判定") == "是")
    stats = {"total": len(details), "hit": hit, "miss": len(details) - hit,
             "failed": sum(1 for d in details if "error" in d)}

    # 表格此刻已经完整——先吐给前端渲染，别让它陪着报告再等一轮 LLM
    yield {"type": "details", "details": details, "stats": stats}
    yield {"type": "stage", "stage": "report",
           "text": "AI 正在写汇总分析报告（表格已可用）", "done": n, "total": n}

    # 第三轮：生成报告
    report_lines = []
    for d in details:
        if "error" in d:
            report_lines.append(f"- {d['序号']}. {d['品名']}：{d['error']}")
        else:
            report_lines.append(
                f"- {d['序号']}. {d['品名']} → {d['编码']} {d['商品描述'][:40]} | "
                f"{d['一般税率']} | 301: {d['301判定']} {d['301加征']}"
                + (f" | FLIP301: {d['FLIP 301加征']}" if d.get("FLIP 301加征") else "")
                + (f" | 301排除: {d['301排除']}" if d.get("301排除") else "")
                + f" | 总税负 {d['总税负估算']}"
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

    yield {"type": "report", "report": report}
    yield {"type": "done",
           "result": {"report": report, "details": details, "stats": stats}}


def analyze_list(db, items, origin="CN"):
    """
    商品清单批量分析（一次性返回版）：把 analyze_list_stream 跑完再交整包。

    返回：{'report': str, 'details': [逐商品归类+税负], 'stats': {...}}
    出错返回 {'error': ...}。签名与返回结构与流式化之前完全一致——
    命令行、既有测试与 /api/ai/analyze 都还按这个用法调。
    """
    for ev in analyze_list_stream(db, items, origin=origin):
        if ev.get("type") == "error":
            return {"error": ev.get("message", "AI 分析失败")}
        if ev.get("type") == "done":
            return ev.get("result") or {}
    return {"error": "AI 分析未返回结果"}


# ---------- 三期：裁定正文深读（AI 定位 + 逐字摘录，不生成归类意见） ----------

def deepread_precedents(query, rulings, provider=None, fetch=None, max_docs=6,
                        max_chars=5000):
    """
    读候选裁定的正文，让 AI 定位与查询商品最相似的先例并**逐字摘录**决定性原文。

    这是二期语义召回之后的"精读确认"环节。二期 subject 向量把 22 万条缩到十几条，
    这里只对前 max_docs 条按需拉正文——正是它替代了"正文全量 embedding + reranker"：
    十几条几万字 AI 一次读完，不需要预先向量化。

    AI 的角色被严格限制在"读 + 定位 + 摘录"，不改写、不综述、不生成新的归类意见——
    先例的价值在于能拿 CBP 原话去跟海关讲，一经转述就作废。为此：
      - 提示要求逐字引用，禁止改写
      - 返回后逐条**原文校验**：AI 引用的句子若不在正文里（whitespace 归一后
        子串比对），标记 quote_verified=False，前端据此警示"引用存疑，请核对原文"

    query    用户商品描述（中/英）
    rulings  二期/一期召回的裁定列表（需含 裁定号/来源/日期，通常还有 编码）
    fetch    注入点，默认 cross.fetch_ruling_text；测试传假抓取器不联网
    返回 {"精读": [...], "综述": str, "未读": [...]}；失败 {"error": ...}
    """
    provider = provider or get_provider()
    if provider is None:
        return {"error": "AI 服务未配置"}
    if not (query or "").strip():
        return {"error": "请输入商品描述"}
    if fetch is None:
        import cross
        fetch = cross.fetch_ruling_text

    # 拉正文（按需、带缓存）。拉不到的归入"未读"，仍给链接，不假装读过
    docs, unread = [], []
    for r in (rulings or [])[:max_docs]:
        num = r.get("裁定号") or r.get("number")
        text = fetch(num, r.get("来源", "").lower() or r.get("collection", ""),
                     r.get("日期") or r.get("rulingDate", ""))
        if text:
            docs.append((r, text[:max_chars]))
        else:
            unread.append({"裁定号": num, "链接": r.get("链接", ""),
                           "原因": "正文暂不可读（非归类裁定/解析失败/离线），可点链接看原文"})
    if not docs:
        return {"error": "候选裁定的正文都拉取失败，无法深读。可稍后重试或直接看链接。",
                "未读": unread}

    blocks = []
    for i, (r, text) in enumerate(docs):
        blocks.append(f"[裁定 {i}] 编号 {r.get('裁定号')}，判给编码 "
                      f"{'、'.join(r.get('编码', [])) or '（见正文）'}\n正文：{text}")
    sys_prompt = (
        "你是美国海关归类专家。下面是若干条 CBP 裁定的正文，以及一个待归类的商品描述。"
        "你的任务只有三件，逐条完成：\n"
        "1) 判断该裁定描述的货物与待归类商品的相似度（high/medium/low）；\n"
        "2) 用一句话说明相似或不同在哪（中文）；\n"
        "3) 从**该裁定正文里逐字摘录**决定归类的那一句英文原文（quote 字段），"
        "一个字都不能改、不能翻译、不能拼接。找不到就留空。\n"
        "严禁给出你自己的归类结论或编码建议——只做定位与摘录。\n"
        '返回 JSON：{"picks":[{"index":0,"relevance":"high","note":"...","quote":"..."}],'
        '"summary":"一句话综述哪条最值得参考及为什么，中文"}')
    try:
        out = provider.chat_json(
            [{"role": "system", "content": sys_prompt},
             {"role": "user", "content": f"待归类商品：{query}\n\n" + "\n\n".join(blocks)}],
            fallback=None)
    except AIProviderError as e:
        return {"error": f"AI 深读失败：{e}", "未读": unread}
    if not isinstance(out, dict):
        return {"error": "AI 返回格式异常", "未读": unread}

    picks = {}
    for p in (out.get("picks") or []):
        try:
            picks[int(p.get("index"))] = p
        except (TypeError, ValueError):
            continue

    result = []
    for i, (r, text) in enumerate(docs):
        p = picks.get(i, {})
        quote = str(p.get("quote") or "").strip()
        # 原文校验：这是整个功能的安全底线。AI 可能"记得"一句差不多的原话，
        # 但拿去跟海关讲必须字字属实。whitespace 归一后做子串比对。
        verified = bool(quote) and _norm_ws(quote) in _norm_ws(text)
        result.append({
            "裁定号": r.get("裁定号"),
            "来源": r.get("来源", ""),
            "日期": r.get("日期", ""),
            "编码": r.get("编码", []),
            "链接": r.get("链接", ""),
            "相似度": str(p.get("relevance", "")).lower(),
            "说明": str(p.get("note", ""))[:200],
            "原文摘录": quote[:500],
            "摘录已核对": verified,
        })
    # high > medium > low > 空
    order = {"high": 0, "medium": 1, "low": 2}
    result.sort(key=lambda x: order.get(x["相似度"], 3))
    return {
        "精读": result,
        "综述": str(out.get("summary", ""))[:400],
        "未读": unread,
        "免责": "AI 仅定位并摘录 CBP 原文，未做归类判断；摘录须以裁定原文为准，"
                "标『引用存疑』的表示未在正文中逐字命中，请点链接核对。",
    }


# 标点归一：模型（尤其中文底座）爱把 ASCII 直引号/连字符"规范化"成 Unicode
# 弯引号、长短破折号。若不抹平，一句逐字正确的摘录会因为一个弯引号被判"引用存疑"
# 标红——校验功能反噬，砸的正是自己的招牌。归一到 ASCII 后再比对。
_QUOTE_MAP = {
    "“": '"', "”": '"', "„": '"', "″": '"',   # 弯/低双引号
    "‘": "'", "’": "'", "‚": "'", "′": "'",   # 弯单引号/撇号
    "–": "-", "—": "-", "―": "-", "−": "-",   # en/em dash、减号
    " ": " ",                                                # 不间断空格
}
_PUNCT_RE = None


def _norm_ws(s):
    """
    校验用归一：换行/多空格压成单空格、去首尾、转小写，并把 Unicode 弯引号/
    破折号抹平成 ASCII。目的是让"逐字但标点被模型改写"的摘录仍能匹配原文，
    同时不放松到"语义相近"——只归一确定等价的标点，不动任何实词。
    """
    global _PUNCT_RE
    import re as _re
    if _PUNCT_RE is None:
        _PUNCT_RE = _re.compile("|".join(map(_re.escape, _QUOTE_MAP)))
    text = _PUNCT_RE.sub(lambda m: _QUOTE_MAP[m.group()], str(s or ""))
    return _re.sub(r"\s+", " ", text).strip().lower()
