# -*- coding: utf-8 -*-
"""
test_guided.py —— GRI 逐级归类链（scripts/guided.py）、章注数据（scripts/hts_notes.py）、
升级接线（ai.classify_product / analyze_list_stream）、配置与 Provider 推理强度。

全程离线：假 Provider 按顺序回复，先例三条路径注入假函数；召回只走关键词通道
（HTS_RECALL_CHANNELS=keyword，与其余测试一致）。

运行：HTS_RECALL_CHANNELS=keyword python -m pytest tests/test_guided.py -q
"""
import json
import os
import sys
import tempfile
import unittest

os.environ.setdefault("HTS_RECALL_CHANNELS", "keyword")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import ai
import core
import guided
import hts_notes


class FakeProvider(ai.BaseProvider):
    def __init__(self, replies):
        super().__init__(model="fake", temperature=0)
        self.replies = list(replies)
        self.calls = []

    def chat(self, messages):
        self.calls.append(messages)
        return self.replies.pop(0) if self.replies else "{}"


def _install(provider):
    ai.reset_provider_cache()
    ai._PROVIDER_CACHE.update({"provider": provider, "loaded": True, "error": ""})
    return provider


_DB = None


def _db():
    global _DB
    if _DB is None:
        _DB = core.load_db()
    return _DB


DESC = "women's raincoat of coated woven polyester fabric with TPU film"
KW = json.dumps({"keywords": ["raincoat", "woven", "coated"], "chapters": ["62"]})
HEAD = json.dumps({"headings": [{"heading": "6210", "reason": "第62章注6：5903面料制成的服装归6210", "notes_cited": ["62-6"]},
                                {"heading": "6202", "reason": "若面料不合5903"}],
                   "excluded": [{"heading": "5903", "why": "第59章注8(a)：制成品不归面料品目"}],
                   "other_chapter": None, "missing_facts": ["涂层是否可见"]})
DESCEND = json.dumps({"code": "6210.30.50", "level_reasons": ["女式→6210.30", "化纤", "遮蔽不确定→Other"],
                      "need_verify": ["外层是否完全遮蔽"], "confidence": 0.85})
PREC_OK = json.dumps({"consistent": True, "revisit_heading": None, "revisit_code": None, "reason": "A87383 同类归 .50"})
NO_PREC = dict(_prec_by_code=lambda c, ex: [], _prec_semantic=lambda d, ex: [], _fetch_text=lambda x: None)


def _pool():
    """与平铺流水线同一个候选池（关键词通道）"""
    rows = ai._recall_candidates(_db(), ["raincoat", "woven", "coated"], description=DESC)
    assert rows, "关键词召回为空，测试前提不成立"
    return rows


# ---------- 章注数据 ----------

class TestNotes(unittest.TestCase):
    FIRST = ("SECTION XI\nTEXTILES AND TEXTILE ARTICLES\nNotes\n1. This section does not cover:\n(a) bristles\n"
             "CHAPTER 50\nSILK\nXI\nNotes\n1. Silk note.\nAdditional U.S. Notes\n1. US note.\n")
    OTHER = "CHAPTER 62\nARTICLES OF APPAREL\nXI\nNotes\n1. This chapter applies only to made up articles\nAdditional U.S. Notes\n1. sets\n"

    def test_split_first_chapter_of_section(self):
        p = hts_notes.split_preamble(self.FIRST, 50)
        self.assertIn("This section does not cover", p["section_notes"])
        self.assertNotIn("Silk note", p["section_notes"])
        self.assertIn("Silk note", p["chapter_notes"])
        self.assertIn("US note", p["us_notes"])
        self.assertEqual(p["section_title"], "TEXTILES AND TEXTILE ARTICLES")
        self.assertEqual(p["chapter_title"], "SILK")   # 孤立的类号行 "XI" 不算标题

    def test_split_non_first_chapter(self):
        p = hts_notes.split_preamble(self.OTHER, 62)
        self.assertEqual(p["section_notes"], "")
        self.assertIn("made up articles", p["chapter_notes"])
        self.assertIn("sets", p["us_notes"])
        self.assertEqual(p["chapter_title"], "ARTICLES OF APPAREL")

    def test_section_mapping(self):
        self.assertEqual(hts_notes.section_first_chapter(62), 50)
        self.assertEqual(hts_notes.section_id(85), "XVI")
        self.assertEqual(hts_notes.section_id(1), "I")

    def test_notes_for_injected_and_absent(self):
        data = {"sections": {"XI": {"notes": "SEC"}}, "chapters": {"62": {"section": "XI", "notes": "CH", "us_notes": "US"}}}
        n = hts_notes.notes_for(62, notes=data)
        self.assertEqual((n["section"], n["chapter"], n["us"], n["section_id"]), ("SEC", "CH", "US", "XI"))
        n0 = hts_notes.notes_for(62, notes={})
        self.assertEqual((n0["section"], n0["chapter"], n0["us"]), ("", "", ""))
        self.assertEqual(n0["section_id"], "XI")

    def test_notes_block_absent_flag(self):
        txt, truncated, absent = guided._notes_block([62], notes={})
        self.assertTrue(absent)
        self.assertIn("注释缺席", txt)
        txt2, _, absent2 = guided._notes_block([62, 59], notes={
            "sections": {"XI": {"notes": "S"}},
            "chapters": {"62": {"section": "XI", "notes": "C62", "us_notes": ""}, "59": {"section": "XI", "notes": "C59", "us_notes": ""}}})
        self.assertFalse(absent2)
        self.assertEqual(txt2.count("S\n"), 1)   # 类注同一类只给一次

    def test_repo_notes_file_if_present(self):
        if not hts_notes.available():
            self.skipTest("data/hts_notes.json 未构建")
        n = hts_notes.notes_for(29)
        self.assertIn("Separate chemically defined", n["chapter"])
        self.assertTrue(hts_notes.notes_for(84)["section"])


# ---------- 税则树 ----------

class TestTree(unittest.TestCase):
    def test_heading_text_and_subtree(self):
        t = guided._tree(_db())
        self.assertIn("6210", t.by_h4)
        self.assertTrue(t.heading_text("6210"))
        sub = t.subtree("6210")
        self.assertIn("6210.30.50", sub)
        self.assertIn("一般税率", sub)
        # 直接挂在品目下、path 为空的行也要有品目条文
        flat = next((h for h, codes in t.by_h4.items() if len(codes) == 1 and not t.r8[codes[0]].get("path")), None)
        if flat:
            self.assertTrue(t.heading_text(flat))
        chs = t.headings_of_chapter(62)
        self.assertTrue(all(h.startswith("62") for h in chs) and "6210" in chs)


# ---------- 逐级链本体 ----------

class TestGuidedChain(unittest.TestCase):
    def test_happy_path(self):
        p = FakeProvider([HEAD, DESCEND, PREC_OK])
        out = guided.classify_guided(_db(), DESC, provider=p, rows=_pool(), **NO_PREC)
        self.assertNotIn("error", out)
        self.assertEqual(out["编码"], "6210.30.50")
        self.assertEqual(out["归类方式"], "逐级")
        a = out["论证"]
        self.assertEqual(a["品目"], "6210")
        self.assertEqual(a["下钻编码"], "6210.30.50")
        self.assertEqual(a["排除"][0]["heading"], "5903")
        self.assertEqual(a["缺事实"], ["涂层是否可见"])
        self.assertTrue(a["先例核对"]["一致"])
        self.assertEqual(a["调用次数"], 3)
        # 品目步提示：候选按品目分组 + 涉及章全部品目 + 注释
        u = p.calls[0][1]["content"]
        self.assertIn("检索召回的候选品目", u)
        self.assertIn("涉及各章的全部品目", u)
        self.assertIn("注释", u)
        # 下钻步：子目树
        self.assertIn("6210.30.50", p.calls[1][1]["content"])

    def test_invalid_code_twice_is_error_not_fallback(self):
        p = FakeProvider([HEAD, json.dumps({"code": "99999999"}), json.dumps({"code": "62021900"})])
        out = guided.classify_guided(_db(), DESC, provider=p, rows=_pool(), **NO_PREC)
        self.assertIn("error", out)
        self.assertIn("未做归类", out["error"])
        self.assertIn("不在品目 6210", p.calls[2][1]["content"])   # 重问时告知上次为何无效

    def test_revisit_code_must_stay_in_heading(self):
        bad = json.dumps({"consistent": False, "revisit_heading": None, "revisit_code": "62021900", "reason": "x"})
        p = FakeProvider([HEAD, DESCEND, bad])
        out = guided.classify_guided(_db(), DESC, provider=p, rows=_pool(), **NO_PREC)
        self.assertEqual(out["编码"], "6210.30.50")           # 跨品目的 revisit_code 被拒
        self.assertNotIn("改判", out["论证"]["先例核对"])
        good = json.dumps({"consistent": False, "revisit_heading": None, "revisit_code": "62103030", "reason": "A1"})
        p = FakeProvider([HEAD, DESCEND, good])
        out = guided.classify_guided(_db(), DESC, provider=p, rows=_pool(), **NO_PREC)
        self.assertEqual(out["编码"], "6210.30.30")
        self.assertIn("62103050 → 62103030", out["论证"]["先例核对"]["改判"])
        self.assertEqual(out["论证"]["下钻编码"], "6210.30.50")

    def test_revisit_heading_descends_again(self):
        target = guided._tree(_db()).by_h4["6202"][0]
        rev = json.dumps({"consistent": False, "revisit_heading": "6202", "revisit_code": None, "reason": "N1 归 6202"})
        d2 = json.dumps({"code": target, "level_reasons": ["x"], "need_verify": [], "confidence": 0.7})
        p = FakeProvider([HEAD, DESCEND, rev, d2])
        out = guided.classify_guided(_db(), DESC, provider=p, rows=_pool(), **NO_PREC)
        self.assertEqual(out["编码"], core.fmt(target, 8))
        self.assertEqual(out["论证"]["品目"], "6202")
        self.assertIn("回退品目 6210 → 6202", out["论证"]["先例核对"]["改判"])

    def test_other_chapter_detour_once(self):
        first = json.dumps({"headings": [], "excluded": [], "other_chapter": "39", "missing_facts": []})
        p = FakeProvider([first, HEAD, DESCEND, PREC_OK])
        out = guided.classify_guided(_db(), DESC, provider=p, rows=_pool(), **NO_PREC)
        self.assertEqual(out["编码"], "6210.30.50")
        self.assertEqual(out["论证"]["补章"], 39)
        self.assertIn("第 39 章", p.calls[1][1]["content"])

    def test_budget_floor_and_precedent_step_skipped_when_spent(self):
        # max_calls 下限是 3：品目 + 下钻用掉 2，先例步还能跑 1 次；传 2 也被抬到 3
        p = FakeProvider([HEAD, DESCEND, PREC_OK])
        out = guided.classify_guided(_db(), DESC, provider=p, rows=_pool(), max_calls=2, **NO_PREC)
        self.assertEqual(out["论证"]["调用次数"], 3)
        self.assertIn("理由", out["论证"]["先例核对"])
        # 下钻第一次无效、重问一次把预算用到 3：先例步被跳过，结果仍成立，先例核对为空
        p2 = FakeProvider([HEAD, json.dumps({"code": "99999999"}), DESCEND, PREC_OK])
        out2 = guided.classify_guided(_db(), DESC, provider=p2, rows=_pool(), max_calls=3, **NO_PREC)
        self.assertEqual(out2["编码"], "6210.30.50")
        self.assertEqual(out2["论证"]["调用次数"], 3)
        self.assertEqual(out2["论证"]["先例核对"], {})
        self.assertEqual(len(p2.calls), 3)

    def test_notes_absent_is_reported_not_fatal(self):
        p = FakeProvider([HEAD, DESCEND, PREC_OK])
        out = guided.classify_guided(_db(), DESC, provider=p, rows=_pool(), notes={}, **NO_PREC)
        self.assertEqual(out["编码"], "6210.30.50")
        self.assertFalse(out["论证"]["注释可用"])
        self.assertIn("注释缺席", p.calls[0][1]["content"])

    def test_exclude_ruling_threaded_to_all_precedent_paths(self):
        seen = {}
        def by_code(c, ex):
            seen["by_code"] = ex
            return [{"裁定号": "N1", "日期": "2024-01-01", "来源": "NY", "状态": "现行", "编码": ["6210.30.50"], "主题": "s"}]
        def sem(d, ex):
            seen["sem"] = ex
            return [{"裁定号": "N2", "日期": "2023-01-01", "来源": "NY", "状态": "现行", "编码": ["6210.30.50"], "主题": "t"}]
        def fetch(x):
            seen.setdefault("fetched", []).append(x["裁定号"])
            return "full text"
        p = FakeProvider([HEAD, DESCEND, PREC_OK])
        guided.classify_guided(_db(), DESC, provider=p, rows=_pool(), exclude_ruling="N332157",
                               _prec_by_code=by_code, _prec_semantic=sem, _fetch_text=fetch)
        self.assertEqual((seen["by_code"], seen["sem"]), ("N332157", "N332157"))
        self.assertEqual(seen["fetched"], ["N2"])
        u = p.calls[2][1]["content"]
        self.assertIn("N1", u)
        self.assertIn("full text", u)

    def test_ollama_needs_num_ctx(self):
        class OllamaProvider(FakeProvider):   # 只看类名与 num_ctx
            num_ctx = 0
        out = guided.classify_guided(_db(), DESC, provider=OllamaProvider([]), rows=_pool(), **NO_PREC)
        self.assertIn("num_ctx", out["error"])
        p = OllamaProvider([HEAD, DESCEND, PREC_OK])
        p.num_ctx = 32768
        self.assertEqual(guided.classify_guided(_db(), DESC, provider=p, rows=_pool(), **NO_PREC)["编码"], "6210.30.50")

    def test_provider_error_is_wrapped(self):
        class Boom(ai.BaseProvider):
            def chat(self, messages):
                raise ai.AIProviderError("下线了")
        out = guided.classify_guided(_db(), DESC, provider=Boom("b"), rows=_pool(), **NO_PREC)
        self.assertIn("逐级归类失败", out["error"])


# ---------- 升级接线 ----------

class TestEscalation(unittest.TestCase):
    def setUp(self):
        self._gs = ai.guided_settings
        self._pc = guided._prec_by_code_default
        self._ps = guided._prec_semantic_default
        self._ft = guided._fetch_text_default
        guided._prec_by_code_default = lambda c, ex, limit=6: []
        guided._prec_semantic_default = lambda d, ex, alive, limit=8: []
        guided._fetch_text_default = lambda x: None
        pool = ai._recall_candidates(_db(), ["raincoat", "woven", "coated"], limit=20, description=DESC)[:12]
        self.flat = next(r["编码"] for r in pool if not r["编码"].startswith("6210"))

    def tearDown(self):
        ai.guided_settings = self._gs
        guided._prec_by_code_default = self._pc
        guided._prec_semantic_default = self._ps
        guided._fetch_text_default = self._ft
        ai.reset_provider_cache()

    def _on(self, th=0.9):
        ai.guided_settings = lambda cfg=None: {"enabled": True, "threshold": th, "max_calls": 6, "effort": ""}

    def _flat(self, conf):
        return json.dumps({"picks": [{"code": self.flat.replace(".", ""), "confidence": conf, "reason": "flat", "need_verify": []}]})

    def test_low_confidence_escalates(self):
        self._on()
        p = _install(FakeProvider([KW, self._flat(0.7), HEAD, DESCEND, PREC_OK]))
        out = ai.classify_product(_db(), DESC, origin="CN", exclude_ruling="N332157")
        self.assertEqual(out["归类方式"], "逐级")
        self.assertEqual(out["candidates"][0]["编码"], "6210.30.50")
        self.assertEqual(out["candidates"][0]["归类方式"], "逐级")
        self.assertEqual(out["平铺结果"], self.flat)
        self.assertIn("总税负估算", out["candidates"][0])
        self.assertEqual(len(p.calls), 5)
        # 平铺的其它候选跟在后面
        self.assertEqual(out["candidates"][1]["编码"], self.flat)

    def test_high_confidence_stays_flat(self):
        self._on()
        p = _install(FakeProvider([KW, self._flat(0.95)]))
        out = ai.classify_product(_db(), DESC, origin="CN")
        self.assertEqual(out["归类方式"], "平铺")
        self.assertEqual(len(p.calls), 2)
        self.assertNotIn("升级失败", out)

    def test_disabled_never_escalates(self):
        ai.guided_settings = lambda cfg=None: {"enabled": False, "threshold": 0.9, "max_calls": 6, "effort": ""}
        p = _install(FakeProvider([KW, self._flat(0.3)]))
        out = ai.classify_product(_db(), DESC, origin="CN")
        self.assertEqual(out["归类方式"], "平铺")
        self.assertEqual(len(p.calls), 2)

    def test_force_guided_ignores_threshold(self):
        ai.guided_settings = lambda cfg=None: {"enabled": False, "threshold": 0.9, "max_calls": 6, "effort": ""}
        _install(FakeProvider([KW, self._flat(0.99), HEAD, DESCEND, PREC_OK]))
        out = ai.classify_product(_db(), DESC, origin="CN", force_guided=True)
        self.assertEqual(out["归类方式"], "逐级")

    def test_guided_failure_keeps_flat(self):
        self._on()
        _install(FakeProvider([KW, self._flat(0.7), HEAD, json.dumps({"code": "1"}), json.dumps({"code": "2"})]))
        out = ai.classify_product(_db(), DESC, origin="CN")
        self.assertEqual(out["归类方式"], "平铺")
        self.assertEqual(out["candidates"][0]["编码"], self.flat)
        self.assertIn("未做归类", out["升级失败"])

    def test_flat_invalid_pick_then_guided_rescues(self):
        self._on()
        _install(FakeProvider([KW, json.dumps({"picks": [{"code": "00000000", "confidence": 0.9}]}), HEAD, DESCEND, PREC_OK]))
        out = ai.classify_product(_db(), DESC, origin="CN")
        self.assertEqual(out["归类方式"], "逐级")
        self.assertEqual(out["平铺结果"], "")

    def test_flat_invalid_and_guided_fails_is_error(self):
        self._on()
        _install(FakeProvider([KW, json.dumps({"picks": [{"code": "00000000"}]}), HEAD, json.dumps({"code": "1"}), json.dumps({"code": "2"})]))
        out = ai.classify_product(_db(), DESC, origin="CN")
        self.assertIn("error", out)
        self.assertIn("升级失败", out)

    def test_list_stream_escalates_low_confidence_row(self):
        self._on()
        kw_b = json.dumps({"items": [{"index": 1, "keywords": ["raincoat", "woven", "coated"], "chapters": ["62"]}]})
        rank_b = json.dumps({"picks": [{"index": 1, "code": self.flat.replace(".", ""), "confidence": 0.5, "reason": "flat"}]})
        _install(FakeProvider([kw_b, rank_b, HEAD, DESCEND, PREC_OK, "报告"]))
        evs = list(ai.analyze_list_stream(_db(), [{"name": DESC}], origin="CN"))
        stages = [(e["stage"], e.get("done"), e.get("total")) for e in evs if e["type"] == "stage"]
        self.assertIn(("guided", 1, 1), stages)
        det = next(e for e in evs if e["type"] == "details")["details"][0]
        self.assertEqual(det["归类方式"], "逐级")
        self.assertEqual(det["编码"], "6210.30.50")
        self.assertEqual(det["平铺结果"], self.flat)
        self.assertEqual(det["论证"]["品目"], "6210")
        self.assertEqual(evs[-1]["type"], "done")
        # 升级后的行仍带候选列（同批候选）与税负
        self.assertIn("总税负估算", det)

    def test_list_stream_no_escalation_when_confident(self):
        self._on()
        kw_b = json.dumps({"items": [{"index": 1, "keywords": ["raincoat", "woven", "coated"], "chapters": ["62"]}]})
        rank_b = json.dumps({"picks": [{"index": 1, "code": self.flat.replace(".", ""), "confidence": 0.95, "reason": "flat"}]})
        p = _install(FakeProvider([kw_b, rank_b, "报告"]))
        evs = list(ai.analyze_list_stream(_db(), [{"name": DESC}], origin="CN"))
        self.assertFalse(any(e.get("stage") == "guided" for e in evs))
        self.assertEqual(len(p.calls), 3)

    def test_classify_guided_only(self):
        _install(FakeProvider([KW, HEAD, DESCEND, PREC_OK]))
        out = ai.classify_guided_only(_db(), DESC, origin="CN")
        self.assertEqual(out["归类方式"], "逐级")
        self.assertEqual(out["candidates"][0]["编码"], "6210.30.50")
        self.assertIn("论证", out["candidates"][0])


# ---------- 配置与 Provider ----------

class TestConfigAndProvider(unittest.TestCase):
    def setUp(self):
        self._cfg = ai.CONFIG_FILE
        self.tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        self.tmp.close()
        os.unlink(self.tmp.name)
        ai.CONFIG_FILE = self.tmp.name

    def tearDown(self):
        ai.CONFIG_FILE = self._cfg
        if os.path.exists(self.tmp.name):
            os.unlink(self.tmp.name)
        ai.reset_provider_cache()

    def test_save_round_trip_new_keys(self):
        ai.save_config({"provider": "openai_compat", "base_url": "http://x", "model": "m", "api_key": "k",
                        "reasoning_effort": "None", "guided": "true", "guided_threshold": "1.7",
                        "guided_max_calls": "8", "guided_effort": "low", "num_ctx": "32768"})
        cfg = ai.load_config()
        self.assertEqual(cfg["reasoning_effort"], "none")
        self.assertTrue(cfg["guided"])
        self.assertEqual(cfg["guided_threshold"], 1.0)   # 钳到 [0, 1]
        self.assertEqual(cfg["guided_max_calls"], 8)
        self.assertEqual(cfg["num_ctx"], 32768)
        gs = ai.guided_settings(cfg)
        self.assertEqual((gs["enabled"], gs["threshold"], gs["max_calls"], gs["effort"]), (True, 1.0, 8, "low"))
        # 脏值不入库
        ai.save_config({"reasoning_effort": "DROP TABLE;", "guided_threshold": "abc"})
        cfg = ai.load_config()
        self.assertEqual(cfg["reasoning_effort"], "none")
        self.assertEqual(cfg["guided_threshold"], 1.0)
        masked = ai.mask_config(cfg)
        self.assertNotIn("api_key", masked)
        self.assertIn("guided", masked)

    def test_default_off(self):
        gs = ai.guided_settings(dict(ai.DEFAULT_CONFIG))
        self.assertFalse(gs["enabled"])
        self.assertEqual(gs["threshold"], 0.9)

    def test_openai_compat_effort_in_payload_and_cache_key(self):
        import httpx

        class _Resp:
            def __init__(self, code, data, text=""):
                self.status_code, self._d, self.text = code, data, text
            def json(self):
                return self._d
            def raise_for_status(self):
                if self.status_code >= 400:
                    raise httpx.HTTPStatusError("bad", request=None, response=self)
        calls = []
        def fake_post(url, headers=None, json=None, timeout=None, **kw):
            calls.append(json)
            return _Resp(200, {"choices": [{"message": {"content": '{"a": 1}'}}]})
        old = httpx.post
        httpx.post = fake_post
        try:
            p = ai.OpenAICompatProvider(model="m", base_url="http://x", api_key="k", seed=1, reasoning_effort="none")
            self.assertEqual(p.chat_json([{"role": "user", "content": "q"}]), {"a": 1})
            self.assertEqual(calls[0]["reasoning_effort"], "none")
            # 同一提示换推理强度 = 另一个缓存键
            p.reasoning_effort = "high"
            p.chat_json([{"role": "user", "content": "q"}])
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[1]["reasoning_effort"], "high")
            p.reasoning_effort = ""
            p.chat_json([{"role": "user", "content": "q"}])
            self.assertNotIn("reasoning_effort", calls[2])
        finally:
            httpx.post = old

    def test_openai_compat_retries_without_temperature_on_400(self):
        import httpx

        class _Resp:
            def __init__(self, code, data, text=""):
                self.status_code, self._d, self.text = code, data, text
            def json(self):
                return self._d
            def raise_for_status(self):
                if self.status_code >= 400:
                    raise httpx.HTTPStatusError("bad", request=None, response=self)
        calls = []
        def fake_post(url, headers=None, json=None, timeout=None, **kw):
            calls.append(json)
            if "temperature" in json:
                return _Resp(400, {}, text='{"error": {"message": "Unsupported parameter: temperature"}}')
            return _Resp(200, {"choices": [{"message": {"content": "pong"}}]})
        old = httpx.post
        httpx.post = fake_post
        try:
            p = ai.OpenAICompatProvider(model="m", base_url="http://x", api_key="k", seed=1)
            self.assertEqual(p.ping(), "pong")
            self.assertIn("temperature", calls[0])
            self.assertNotIn("temperature", calls[-1])
            self.assertNotIn("seed", calls[-1])
        finally:
            httpx.post = old

    def test_ollama_num_ctx_in_options(self):
        import httpx

        class _Resp:
            def __init__(self, data):
                self._d = data
                self.status_code = 200
            def json(self):
                return self._d
            def raise_for_status(self):
                pass
        calls = []
        def fake_post(url, json=None, timeout=None, **kw):
            calls.append(json)
            return _Resp({"message": {"content": "{}"}})
        old = httpx.post
        httpx.post = fake_post
        try:
            p = ai.OllamaProvider(model="m", temperature=0.0, seed=7, num_ctx=32768)
            p.chat([{"role": "user", "content": "ctx"}])
            self.assertEqual(calls[0]["options"]["num_ctx"], 32768)
            p0 = ai.OllamaProvider(model="m", temperature=0.0, seed=7)
            p0.chat([{"role": "user", "content": "ctx-none"}])
            self.assertNotIn("num_ctx", calls[1]["options"])
        finally:
            httpx.post = old

    def test_guided_context_restores_provider(self):
        p = ai.OpenAICompatProvider(model="m", base_url="http://x", api_key="k", timeout=60, reasoning_effort="none")
        with ai._guided_context(p, "high"):
            self.assertEqual(p.reasoning_effort, "high")
            self.assertEqual(p.timeout, 180.0)
        self.assertEqual((p.reasoning_effort, p.timeout), ("none", 60))
        f = FakeProvider([])
        with ai._guided_context(f, "high"):   # 没有 reasoning_effort 属性的假件：只抬 timeout，不报错
            self.assertEqual(f.timeout, 180.0)
            self.assertFalse(hasattr(f, "reasoning_effort"))
        self.assertEqual(f.timeout, 60)


# ---------- Web 层 ----------

class TestWeb(unittest.TestCase):
    def test_export_flattens_argument(self):
        import app
        row = {"编码": "6210.30.50", "论证": {"品目": "6210", "品目理由": "注6", "排除": [{"heading": "5903", "why": "注8"}],
                                            "缺事实": ["涂层"], "先例核对": {"一致": False, "理由": "A1", "改判": "x → y"}}}
        o = app._flatten_for_export(row)
        self.assertIsInstance(o["论证"], str)
        self.assertIn("品目 6210：注6", o["论证"])
        self.assertIn("5903（注8）", o["论证"])
        self.assertIn("不一致", o["论证"])
        self.assertIn("x → y", o["论证"])

    def test_guide_page_served(self):
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest("fastapi testclient 不可用")
        import app
        r = TestClient(app.app).get("/guide")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/html", r.headers.get("content-type", ""))
        self.assertIn("工作原理", r.text)
        self.assertIn("N332157", r.text)   # 示例商品来自真实裁定，页面必须说明出处
        self.assertEqual(r.headers.get("cache-control"), "no-cache")

    def test_guided_endpoint_and_config_fields(self):
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest("fastapi testclient 不可用")
        import app
        old_pc, old_ps = guided._prec_by_code_default, guided._prec_semantic_default
        guided._prec_by_code_default = lambda c, ex, limit=6: []
        guided._prec_semantic_default = lambda d, ex, alive, limit=8: []
        _install(FakeProvider([KW, HEAD, DESCEND, PREC_OK]))
        try:
            client = TestClient(app.app)
            r = client.post("/api/ai/classify/guided", json={"description": DESC, "origin": "CN"})
            self.assertEqual(r.status_code, 200)
            d = r.json()
            self.assertEqual(d["归类方式"], "逐级")
            self.assertEqual(d["candidates"][0]["编码"], "6210.30.50")
            self.assertEqual(client.post("/api/ai/classify/guided", json={"description": ""}).status_code, 400)
            st = client.get("/api/ai/status").json()
            self.assertIn("guided", st)
            self.assertIn("notes_available", st)
        finally:
            guided._prec_by_code_default, guided._prec_semantic_default = old_pc, old_ps
            ai.reset_provider_cache()


if __name__ == "__main__":
    unittest.main()
