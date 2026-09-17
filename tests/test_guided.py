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
import re
import sqlite3
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
# 同级对证：维持原选
VERIFY = json.dumps({"checks": [{"code": "62103030", "判定": "无证据", "依据": "描述未说明是否完全遮蔽"},
                                {"code": "62103050", "判定": "满足", "依据": "Other"}],
                     "code": "62103050", "confidence": 0.8, "reason": "维持", "need_verify": ["遮蔽程度"]})
NO_PREC = dict(_prec_by_code=lambda c, ex: [], _prec_semantic=lambda d, ex: [], _fetch_text=lambda x: None,
               _examples=lambda codes, ex: {})


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
        p = FakeProvider([HEAD, DESCEND, VERIFY, PREC_OK])
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
        self.assertEqual(a["调用次数"], 4)
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
        p = FakeProvider([HEAD, DESCEND, VERIFY, bad])
        out = guided.classify_guided(_db(), DESC, provider=p, rows=_pool(), **NO_PREC)
        self.assertEqual(out["编码"], "6210.30.50")           # 跨品目的 revisit_code 被拒
        self.assertNotIn("改判", out["论证"]["先例核对"])
        good = json.dumps({"consistent": False, "revisit_heading": None, "revisit_code": "62103030", "reason": "A1"})
        p = FakeProvider([HEAD, DESCEND, VERIFY, good])
        out = guided.classify_guided(_db(), DESC, provider=p, rows=_pool(), **NO_PREC)
        self.assertEqual(out["编码"], "6210.30.30")
        self.assertIn("62103050 → 62103030", out["论证"]["先例核对"]["改判"])
        self.assertEqual(out["论证"]["下钻编码"], "6210.30.50")

    def test_revisit_heading_descends_again(self):
        target = guided._tree(_db()).by_h4["6202"][0]
        rev = json.dumps({"consistent": False, "revisit_heading": "6202", "revisit_code": None, "reason": "N1 归 6202"})
        d2 = json.dumps({"code": target, "level_reasons": ["x"], "need_verify": [], "confidence": 0.7})
        p = FakeProvider([HEAD, DESCEND, VERIFY, rev, d2])
        out = guided.classify_guided(_db(), DESC, provider=p, rows=_pool(), **NO_PREC)
        self.assertEqual(out["编码"], core.fmt(target, 8))
        self.assertEqual(out["论证"]["品目"], "6202")
        self.assertIn("回退品目 6210 → 6202", out["论证"]["先例核对"]["改判"])

    def test_other_chapter_detour_once(self):
        first = json.dumps({"headings": [], "excluded": [], "other_chapter": "39", "missing_facts": []})
        p = FakeProvider([first, HEAD, DESCEND, VERIFY, PREC_OK])
        out = guided.classify_guided(_db(), DESC, provider=p, rows=_pool(), **NO_PREC)
        self.assertEqual(out["编码"], "6210.30.50")
        self.assertEqual(out["论证"]["补章"], 39)
        self.assertIn("第 39 章", p.calls[1][1]["content"])

    def test_budget_floor_and_later_steps_skipped_when_spent(self):
        # max_calls 下限是 3：品目 + 下钻 + 同级对证用满，先例步跳过；传 2 也被抬到 3
        p = FakeProvider([HEAD, DESCEND, VERIFY, PREC_OK])
        out = guided.classify_guided(_db(), DESC, provider=p, rows=_pool(), max_calls=2, **NO_PREC)
        self.assertEqual(out["论证"]["调用次数"], 3)
        self.assertTrue(out["论证"]["同级对证"]["对证"])
        self.assertEqual(out["论证"]["先例核对"], {})
        # 下钻第一次无效、重问一次把预算用到 3：对证与先例都跳过，结果仍成立
        p2 = FakeProvider([HEAD, json.dumps({"code": "99999999"}), DESCEND, VERIFY, PREC_OK])
        out2 = guided.classify_guided(_db(), DESC, provider=p2, rows=_pool(), max_calls=3, **NO_PREC)
        self.assertEqual(out2["编码"], "6210.30.50")
        self.assertEqual(out2["论证"]["调用次数"], 3)
        self.assertEqual(out2["论证"]["同级对证"], {})
        self.assertEqual(len(p2.calls), 3)

    def test_notes_absent_is_reported_not_fatal(self):
        p = FakeProvider([HEAD, DESCEND, VERIFY, PREC_OK])
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
        def exs(codes, ex):
            seen["exs"] = ex
            return {c: [{"裁定号": "N9", "日期": "2024-01-01", "描述": "a laminated raincoat", "主题": "s"}] for c in codes}
        p = FakeProvider([HEAD, DESCEND, VERIFY, PREC_OK])
        guided.classify_guided(_db(), DESC, provider=p, rows=_pool(), exclude_ruling="N332157",
                               _prec_by_code=by_code, _prec_semantic=sem, _fetch_text=fetch, _examples=exs)
        self.assertEqual((seen["by_code"], seen["sem"], seen["exs"]), ("N332157", "N332157", "N332157"))
        self.assertIn("a laminated raincoat", p.calls[2][1]["content"])   # 对证提示带先例描述段
        self.assertIn("货物描述：a laminated raincoat", p.calls[3][1]["content"])   # 先例步也带
        self.assertEqual(seen["fetched"], ["N2"])
        u = p.calls[3][1]["content"]
        self.assertIn("N1", u)
        self.assertIn("full text", u)

    def test_ollama_needs_num_ctx(self):
        class OllamaProvider(FakeProvider):   # 只看类名与 num_ctx
            num_ctx = 0
        out = guided.classify_guided(_db(), DESC, provider=OllamaProvider([]), rows=_pool(), **NO_PREC)
        self.assertIn("num_ctx", out["error"])
        p = OllamaProvider([HEAD, DESCEND, VERIFY, PREC_OK])
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
        ai.guided_settings = lambda cfg=None: {"enabled": True, "threshold": th, "max_calls": 6, "effort": "", "parallel": 1}

    def _flat(self, conf):
        return json.dumps({"picks": [{"code": self.flat.replace(".", ""), "confidence": conf, "reason": "flat", "need_verify": []}]})

    def test_low_confidence_escalates(self):
        self._on()
        p = _install(FakeProvider([KW, self._flat(0.7), HEAD, DESCEND, VERIFY, PREC_OK]))
        out = ai.classify_product(_db(), DESC, origin="CN", exclude_ruling="N332157")
        self.assertEqual(out["归类方式"], "逐级")
        self.assertEqual(out["candidates"][0]["编码"], "6210.30.50")
        self.assertEqual(out["candidates"][0]["归类方式"], "逐级")
        self.assertEqual(out["平铺结果"], self.flat)
        self.assertIn("总税负估算", out["candidates"][0])
        self.assertEqual(len(p.calls), 6)
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
        _install(FakeProvider([KW, self._flat(0.99), HEAD, DESCEND, VERIFY, PREC_OK]))
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
        _install(FakeProvider([KW, json.dumps({"picks": [{"code": "00000000", "confidence": 0.9}]}), HEAD, DESCEND, VERIFY, PREC_OK]))
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
        _install(FakeProvider([kw_b, rank_b, HEAD, DESCEND, VERIFY, PREC_OK, "报告"]))
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
        _install(FakeProvider([KW, HEAD, DESCEND, VERIFY, PREC_OK]))
        out = ai.classify_guided_only(_db(), DESC, origin="CN")
        self.assertEqual(out["归类方式"], "逐级")
        self.assertEqual(out["candidates"][0]["编码"], "6210.30.50")
        self.assertIn("论证", out["candidates"][0])
        self.assertEqual(out["补充"], "")

    def test_supplement_reaches_every_step(self):
        p = _install(FakeProvider([KW, HEAD, DESCEND, VERIFY, PREC_OK]))
        out = ai.classify_guided_only(_db(), DESC, origin="CN", supplement="TPU 膜在外表面但不完全遮蔽底布")
        self.assertEqual(out["补充"], "TPU 膜在外表面但不完全遮蔽底布")
        for call in p.calls[1:]:   # 品目 / 下钻 / 先例三步的用户消息都带补充说明
            self.assertIn("补充说明（用户核实后提供）：TPU 膜在外表面", call[1]["content"])


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
        _install(FakeProvider([KW, HEAD, DESCEND, VERIFY, PREC_OK]))
        try:
            client = TestClient(app.app)
            r = client.post("/api/ai/classify/guided", json={"description": DESC, "origin": "CN"})
            self.assertEqual(r.status_code, 200)
            d = r.json()
            self.assertEqual(d["归类方式"], "逐级")
            self.assertEqual(d["candidates"][0]["编码"], "6210.30.50")
            self.assertEqual(client.post("/api/ai/classify/guided", json={"description": ""}).status_code, 400)
            _install(FakeProvider([KW, HEAD, DESCEND, VERIFY, PREC_OK]))
            r2 = client.post("/api/ai/classify/guided", json={"description": DESC, "supplement": "外层不遮蔽"})
            self.assertEqual(r2.json()["补充"], "外层不遮蔽")
            st = client.get("/api/ai/status").json()
            self.assertIn("guided", st)
            self.assertIn("notes_available", st)
        finally:
            guided._prec_by_code_default, guided._prec_semantic_default = old_pc, old_ps
            ai.reset_provider_cache()


if __name__ == "__main__":
    unittest.main()


class TestEscalationRules(unittest.TestCase):
    """规则触发：注释决定章 / 候选跨章，与阈值并列。"""

    GS = {"threshold": 0.9, "force_chapters": ai.parse_chapters("28-38"), "cross_chapter": True}

    def test_parse_chapters(self):
        self.assertEqual(ai.parse_chapters("28-38"), {f"{c:02d}" for c in range(28, 39)})
        self.assertEqual(ai.parse_chapters("90, 84-85"), {"90", "84", "85"})
        self.assertEqual(ai.parse_chapters(""), set())
        self.assertEqual(ai.parse_chapters("abc"), set())

    def test_reasons_in_priority(self):
        top = {"confidence": 0.95, "编码": "2933.29.20"}
        self.assertIn("注释决定章", ai._escalation_reason(top, ["29"], self.GS, ["2933.29.20"]))
        top = {"confidence": 0.95, "编码": "6210.30.50"}
        self.assertIn("跨章", ai._escalation_reason(top, ["62"], self.GS, ["6210.30.50", "5903.20.30"]))
        self.assertEqual(ai._escalation_reason(top, ["62"], self.GS, ["6210.30.50", "6202.93.00"]), "")
        self.assertIn("低于阈值", ai._escalation_reason({"confidence": 0.5, "编码": "6210.30.50"}, ["62"], self.GS, []))
        self.assertIn("存疑", ai._escalation_reason({"confidence": 0.95, "编码": "7204.49.00"}, ["82"], self.GS, []))
        self.assertIn("未给出", ai._escalation_reason(None, [], self.GS, []))
        off = {"threshold": 0.9, "force_chapters": set(), "cross_chapter": False}
        self.assertEqual(ai._escalation_reason(top, ["62"], off, ["6210.30.50", "5903.20.30"]), "")

    def test_config_round_trip(self):
        old = ai.CONFIG_FILE
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False); tmp.close(); os.unlink(tmp.name)
        ai.CONFIG_FILE = tmp.name
        try:
            ai.save_config({"guided_force_chapters": " 28-38 , 90", "guided_cross_chapter": "false"})
            gs = ai.guided_settings()
            self.assertEqual(gs["force_chapters"], ai.parse_chapters("28-38,90"))
            self.assertFalse(gs["cross_chapter"])
            ai.save_config({"guided_force_chapters": "not chapters"})   # 脏值不入库
            self.assertEqual(ai.guided_settings()["force_chapters"], ai.parse_chapters("28-38,90"))
            ai.save_config({"guided_force_chapters": ""})
            self.assertEqual(ai.guided_settings()["force_chapters"], set())
            self.assertFalse(ai.guided_settings(dict(ai.DEFAULT_CONFIG))["cross_chapter"])   # 实测净负，默认关
        finally:
            ai.CONFIG_FILE = old
            if os.path.exists(tmp.name):
                os.unlink(tmp.name)
            ai.reset_provider_cache()

    def test_classify_records_reason_and_list_uses_pool(self):
        gs_on = {"enabled": True, "threshold": 0.9, "max_calls": 6, "effort": "",
                 "force_chapters": set(), "cross_chapter": True}
        old_gs, old_pc, old_ps = ai.guided_settings, guided._prec_by_code_default, guided._prec_semantic_default
        ai.guided_settings = lambda cfg=None: gs_on
        guided._prec_by_code_default = lambda c, ex, limit=6: []
        guided._prec_semantic_default = lambda d, ex, alive, limit=8: []
        try:
            pool = ai._recall_candidates(_db(), ["raincoat", "woven", "coated"], limit=20, description=DESC)[:12]
            a = next(r["编码"] for r in pool if r["编码"].startswith("62"))
            b = next(r["编码"] for r in pool if not r["编码"].startswith("62"))
            # 单条：平铺很自信但前三跨章 → 升级，原因记在结果里
            flat = json.dumps({"picks": [{"code": a.replace(".", ""), "confidence": 0.97, "reason": "x"},
                                         {"code": b.replace(".", ""), "confidence": 0.5, "reason": "y"}]})
            _install(FakeProvider([KW, flat, HEAD, DESCEND, VERIFY, PREC_OK]))
            out = ai.classify_product(_db(), DESC, origin="CN")
            self.assertEqual(out["归类方式"], "逐级")
            self.assertIn("跨章", out["升级原因"])
            # 清单：单个 pick 很自信，但池内前五跨章 → 升级
            kw_b = json.dumps({"items": [{"index": 1, "keywords": ["raincoat", "woven", "coated"], "chapters": ["62"]}]})
            rank_b = json.dumps({"picks": [{"index": 1, "code": a.replace(".", ""), "confidence": 0.97, "reason": "x"}]})
            _install(FakeProvider([kw_b, rank_b, HEAD, DESCEND, VERIFY, PREC_OK, "报告"]))
            evs = list(ai.analyze_list_stream(_db(), [{"name": DESC}], origin="CN"))
            det = next(e for e in evs if e["type"] == "details")["details"][0]
            if len({r["编码"][:2] for r in pool[:5]}) >= 2:
                self.assertEqual(det["归类方式"], "逐级")
                self.assertIn("跨章", det["升级原因"])
            else:
                self.assertNotEqual(det.get("归类方式"), "逐级")
        finally:
            ai.guided_settings, guided._prec_by_code_default, guided._prec_semantic_default = old_gs, old_pc, old_ps
            ai.reset_provider_cache()


class TestRewriteRecall(unittest.TestCase):
    """英文 subject 改写作为第二路查询：通道内按最好名次合并，通道权重不变。"""

    def test_merge_ranked(self):
        import rate
        self.assertEqual(rate.merge_ranked([["a", "b", "c"], ["c", "d", "a"]]), ["a", "c", "b", "d"])
        self.assertEqual(rate.merge_ranked([["x"], []]), ["x"])
        self.assertEqual(rate.merge_ranked([]), [])

    def test_hybrid_search_merges_queries_within_channel(self):
        import rate
        db = _db()
        pool = [c for c in db["rates_8"] if c.startswith("6210")][:4] + [c for c in db["rates_8"] if c.startswith("5903")][:2]
        seen = []
        def sem(text, lim):
            seen.append(("sem", text))
            return ([{"编码": pool[0], "相似度": .9}, {"编码": pool[1], "相似度": .8}] if text == "原文"
                    else [{"编码": pool[4], "相似度": .95}, {"编码": pool[0], "相似度": .7}])
        def votes(d, text, lim):
            seen.append(("vote", text))
            return {"候选": [{"编码": pool[2], "票": 2.0, "裁定": ["N1"]}] if text == "原文"
                    else [{"编码": pool[2], "票": 3.0, "裁定": ["N2"]}, {"编码": pool[3], "票": 1.0, "裁定": []}], "先例数": 5}
        rows, st = rate.hybrid_search(db, "kw", limit=10, description="原文", queries=["rewrite", "原文", ""],
                                      channels=("semantic", "precedent"), _semantic=sem, _votes=votes)
        self.assertEqual([t for k, t in seen if k == "sem"], ["原文", "rewrite"])   # 去重、去空
        self.assertEqual(st["语义"]["查询数"], 2)
        codes = [re.sub(r"\D", "", r["编码"]) for r in rows]
        self.assertIn(pool[4], codes)                       # 只有改写那一路召回到的编码进了池
        r2 = next(r for r in rows if re.sub(r"\D", "", r["编码"]) == pool[2])
        self.assertEqual(r2["先例票"], 3.0)                 # 票数取各路最高
        self.assertEqual(sorted(r2["先例裁定"]), ["N1", "N2"])   # 裁定号合并
        # 通道内合并按最好名次：pool[0] 在原文那一路排第一（改写那一路第二）→ 名次 0，
        # pool[1] 只在原文那一路排第二 → 名次 1；同一通道内前者融合分更高
        r0 = next(r for r in rows if re.sub(r"\D", "", r["编码"]) == pool[0])
        r1 = next(r for r in rows if re.sub(r"\D", "", r["编码"]) == pool[1])
        self.assertGreater(r0["相关度"], r1["相关度"])
        self.assertIn("语义", r0["召回来源"])

    def test_subject_line_and_toggle(self):
        p = FakeProvider([json.dumps({"subject": "A women's woven polyester raincoat laminated with TPU film"})])
        self.assertTrue(ai._ai_subject_line(p, DESC).startswith("A women"))
        self.assertEqual(ai._ai_subject_line(FakeProvider(["not json"]), DESC), "")
        old = ai.recall_rewrite_enabled
        old_gs = ai.guided_settings
        calls = []
        orig = ai._recall_candidates
        def spy(db, keywords, limit=40, unit_value=None, origin="CN", description=None, queries=None):
            calls.append(queries)
            return orig(db, keywords, limit=limit, unit_value=unit_value, origin=origin, description=description, queries=queries)
        ai._recall_candidates = spy
        try:
            ai.recall_rewrite_enabled = lambda cfg=None: True
            _install(FakeProvider([KW, json.dumps({"subject": "a raincoat"}),
                                   json.dumps({"picks": [{"code": "62103050", "confidence": 0.99, "reason": "r"}]})]))
            ai.guided_settings = lambda cfg=None: {"enabled": False, "threshold": 0.9, "max_calls": 6, "effort": "",
                                                    "force_chapters": set(), "cross_chapter": False}
            ai.classify_product(_db(), DESC, origin="CN")
            self.assertEqual(calls[0], ["a raincoat"])
            calls.clear()
            ai.recall_rewrite_enabled = lambda cfg=None: False
            _install(FakeProvider([KW, json.dumps({"picks": [{"code": "62103050", "confidence": 0.99, "reason": "r"}]})]))
            ai.classify_product(_db(), DESC, origin="CN")
            self.assertIsNone(calls[0])
        finally:
            ai._recall_candidates = orig
            ai.recall_rewrite_enabled = old
            ai.guided_settings = old_gs
            ai.reset_provider_cache()


class TestDocExpansion(unittest.TestCase):
    """税则行文档扩展：裁定 subject 拼进嵌入文本；评测裁定必须排除。"""

    def _cross(self, rows):
        fp = tempfile.NamedTemporaryFile(suffix=".db", delete=False); fp.close()
        conn = sqlite3.connect(fp.name)
        conn.execute("CREATE TABLE rulings(number TEXT, date TEXT, collection TEXT, subject TEXT, tariffs TEXT, revoked_by TEXT)")
        conn.executemany("INSERT INTO rulings VALUES (?,?,?,?,?,?)", rows)
        conn.commit(); conn.close()
        return fp.name

    def test_build_expansion_and_line_text(self):
        import hts_embed
        db = _db()
        code = next(c for c in db["rates_8"] if c.startswith("6210"))
        dotted = f"{code[:4]}.{code[4:6]}.{code[6:8]}"
        path = self._cross([
            ("N1", "2024-05-01", "ny", "The tariff classification of a women's raincoat from China", dotted + "00", "[]"),
            ("N2", "2023-01-01", "ny", "RE: The classification of a hooded rain jacket from Vietnam", dotted, "[]"),
            ("N3", "2022-01-01", "ny", "The tariff classification of a duplicate raincoat from China", dotted, "[]"),
            ("N4", "2021-01-01", "ny", "The tariff classification of a revoked thing", dotted, '["H1"]'),
            ("EVAL1", "2025-01-01", "ny", "The tariff classification of LEAK", dotted, "[]"),
            ("N5", "2020-01-01", "ny", "The tariff classification of nothing here", "0000.00.0000", "[]"),
        ])
        try:
            exp = hts_embed.build_expansion(db, cross_db_path=path, exclude={"EVAL1"}, log=lambda s: None)
            self.assertEqual(exp[code], ["women's raincoat", "hooded rain jacket", "duplicate raincoat"])   # 新的在前、去套话去产地、撤销不进、评测裁定不进
            self.assertNotIn("00000000", exp)
            texts = hts_embed.line_texts(db, exp)
            self.assertIn("海关判到此行的货物：women's raincoat; hooded rain jacket", texts[code])
            self.assertNotIn("LEAK", texts[code])
            plain = hts_embed.line_texts(db)
            self.assertNotIn("海关判到此行", plain[code])
            self.assertNotEqual(hts_embed._hash(texts[code]), hts_embed._hash(plain[code]))   # 哈希变 → 增量重嵌
        finally:
            os.unlink(path)

    def test_eval_numbers_are_excluded_by_default(self):
        import hts_embed
        nums = hts_embed.eval_ruling_numbers()
        if not nums:
            self.skipTest("无评测金标")
        self.assertIn("N332157", nums)


class TestDescVector(unittest.TestCase):
    """先例语义检索的两张向量表合并：同一裁定取更近的，留一法在截取前过滤。"""

    def test_merge_hits(self):
        import cross
        m = cross._merge_hits([("A", 0.5), ("B", 0.9)], [("B", 0.3), ("C", 0.7)], None)
        self.assertEqual(m, [("B", 0.3), ("A", 0.5), ("C", 0.7)])
        self.assertEqual(cross._merge_hits([], []), [])

    def test_semantic_precedents_exclude_before_truncation(self):
        import cross
        if not cross.db_available():
            self.skipTest("cross.db 未构建")
        try:
            import sqlite_vec  # noqa: F401
        except ImportError:
            self.skipTest("sqlite-vec 不可用")
        fake_embed = lambda texts: [[0.01] * 1024 for _ in texts]
        base = cross.semantic_precedents("raincoat", [], limit=5, _embed=fake_embed)
        if base.get("error"):
            self.skipTest(base["error"])
        first = base["先例"][0]["裁定号"]
        out = cross.semantic_precedents("raincoat", [], limit=5, _embed=fake_embed, exclude=first)
        self.assertNotIn(first, [x["裁定号"] for x in out["先例"]])
        self.assertEqual(len(out["先例"]), 5)


class TestVerifyStep(unittest.TestCase):
    """同级对证：可以改到同级另一行；跨层/无效编码维持原选；只有一个同级时跳过。"""

    def test_verify_changes_within_siblings_only(self):
        change = json.dumps({"checks": [{"code": "62103030", "判定": "满足", "依据": "TPU 膜完全遮蔽"}],
                             "code": "62103030", "confidence": 0.9, "reason": "外层完全遮蔽", "need_verify": []})
        p = FakeProvider([HEAD, DESCEND, change, PREC_OK])
        out = guided.classify_guided(_db(), DESC, provider=p, rows=_pool(), **NO_PREC)
        self.assertEqual(out["编码"], "6210.30.30")
        self.assertIn("62103050 → 62103030", out["论证"]["同级对证"]["改判"])
        self.assertEqual(out["论证"]["下钻编码"], "6210.30.30")   # 对证之后、先例之前
        bad = json.dumps({"checks": [], "code": "62021900", "confidence": 0.9, "reason": "x"})
        p = FakeProvider([HEAD, DESCEND, bad, PREC_OK])
        out = guided.classify_guided(_db(), DESC, provider=p, rows=_pool(), **NO_PREC)
        self.assertEqual(out["编码"], "6210.30.50")            # 不在同级 → 维持
        self.assertNotIn("改判", out["论证"]["同级对证"])

    def test_verify_prompt_lists_siblings_with_examples(self):
        exs = lambda codes, ex: {c: [{"裁定号": f"N{c[-2:]}", "日期": "2025-01-01", "描述": f"goods for {c}", "主题": ""}] for c in codes}
        p = FakeProvider([HEAD, DESCEND, VERIFY, PREC_OK])
        guided.classify_guided(_db(), DESC, provider=p, rows=_pool(), _examples=exs,
                               _prec_by_code=lambda c, ex: [], _prec_semantic=lambda d, ex: [], _fetch_text=lambda x: None)
        u = p.calls[2][1]["content"]
        self.assertIn("6210.30.30", u)
        self.assertIn("6210.30.50（下钻所选）", u)
        self.assertIn("goods for 62103030", u)
        self.assertNotIn("6210.30.70", u)   # 不同父节点的行不在同级里


class TestOnDemandDescriptions(unittest.TestCase):
    """按需取描述段：库里没有的才拉，拉不到的不落库，网络请求封顶；按码取样例带留一法。"""

    def _db_with(self, rulings, codes, descs=()):
        import cross_desc
        fp = tempfile.NamedTemporaryFile(suffix=".db", delete=False); fp.close()
        conn = sqlite3.connect(fp.name)
        conn.execute("CREATE TABLE rulings(number TEXT, date TEXT, collection TEXT, subject TEXT, tariffs TEXT, revoked_by TEXT)")
        conn.execute("CREATE TABLE ruling_codes(number TEXT, code TEXT)")
        conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
        conn.executescript(cross_desc._SCHEMA)
        conn.executemany("INSERT INTO rulings VALUES (?,?,?,?,?,?)", rulings)
        conn.executemany("INSERT INTO ruling_codes VALUES (?,?)", codes)
        conn.executemany("INSERT INTO ruling_desc VALUES (?,?,?)", descs)
        conn.commit(); conn.close()
        return fp.name

    def test_descriptions_for_fetches_missing_with_cap(self):
        import cross_desc
        path = self._db_with([("A", "2024-01-01", "ny", "s", "62103050", "[]"), ("B", "2023-01-01", "ny", "s", "62103050", "[]"),
                              ("C", "2022-01-01", "ny", "s", "62103050", "[]")], [], [("A", "x" * 60, "t")])
        calls = []
        def fetch(num, coll, date):
            calls.append(num)
            return None if num == "C" else "You requested a tariff classification ruling. " + f"The item is a {num} raincoat " * 8 + ". The applicable subheading"
        try:
            out = cross_desc.descriptions_for(["A", "B", "C", "B"], max_fetch=5, delay=0, embed_new=False, db_path=path, _fetch=fetch)
            self.assertEqual(calls, ["B", "C"])          # A 已在库里；B、C 才拉；去重
            self.assertIn("raincoat", out["B"])
            self.assertNotIn("C", out)                   # 网络失败：不落库
            conn = sqlite3.connect(path)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM ruling_desc").fetchone()[0], 2)
            conn.close()
            calls.clear()
            cross_desc.descriptions_for(["C"], max_fetch=0, delay=0, embed_new=False, db_path=path, _fetch=fetch)
            self.assertEqual(calls, [])                  # 封顶 0：不发请求
        finally:
            os.unlink(path)

    def test_precedent_examples_exclude_and_fallback(self):
        import cross_desc
        path = self._db_with(
            [("G", "2025-01-01", "ny", "gold self", "62103050", "[]"), ("N1", "2024-06-01", "ny", "s1", "62103050", "[]"),
             ("N2", "2023-06-01", "ny", "s2", "62103050", "[]"), ("R", "2024-09-01", "ny", "revoked", "62103050", '["H1"]')],
            [("G", "6210305000"), ("N1", "6210305000"), ("N2", "6210305010"), ("R", "6210305000")],
            [("N2", "a second raincoat description that is long enough to count here", "t")])
        try:
            fetch = lambda num, coll, date: None
            ex = cross_desc.precedent_examples(["62103050"], per_code=2, exclude="G", db_path=path, _fetch=fetch)
            nums = [x["裁定号"] for x in ex["62103050"]]
            self.assertNotIn("G", nums)                  # 留一法
            self.assertNotIn("R", nums)                  # 撤销不进
            self.assertEqual(nums[0], "N2")              # 有描述段的排前面
            self.assertEqual(ex["62103050"][1]["裁定号"], "N1")   # 没描述段的用主题兜底
            self.assertEqual(ex["62103050"][1]["主题"], "s1")
        finally:
            os.unlink(path)


class TestCompanyPrecedents(unittest.TestCase):
    """本公司先例库：存 / 精确 / 子串 / 向量检索；同名覆盖；召回第四通道；精确命中不调模型。"""

    def setUp(self):
        import company
        fp = tempfile.NamedTemporaryFile(suffix=".db", delete=False); fp.close(); os.unlink(fp.name)
        self.path = fp.name
        self._old = company.DB_PATH
        company.DB_PATH = self.path

    def tearDown(self):
        import company
        company.DB_PATH = self._old
        for ext in ("", "-shm", "-wal"):
            if os.path.exists(self.path + ext):
                os.unlink(self.path + ext)
        ai.reset_provider_cache()

    def test_record_search_overwrite(self):
        import company
        fake = lambda texts: [[0.02] * 1024 for _ in texts]
        r1 = company.record("女式雨衣 TPU 贴膜", "6210.30.50", description="梭织涤纶", origin="CN", action="改正", note="遮蔽不全", _embed=fake)
        self.assertFalse(r1["更新"])
        r2 = company.record("女式雨衣 TPU 贴膜", "62103030", action="采纳", _embed=fake)
        self.assertTrue(r2["更新"]); self.assertEqual(r1["id"], r2["id"])
        self.assertEqual(company.count(), 1)
        hit = company.search("女式雨衣 tpu 贴膜", _embed=fake)      # 归一后精确
        self.assertEqual((hit[0]["code8"], hit[0]["相似度"], hit[0]["来源"]), ("62103030", 1.0, "采纳"))
        sub = company.search("雨衣", _embed=fake)                    # 子串
        self.assertEqual(sub[0]["相似度"], 0.85)
        self.assertEqual(company.search("", _embed=fake), [])
        with self.assertRaises(ValueError):
            company.record("x", "1234")

    def test_hybrid_search_company_channel(self):
        import rate
        db = _db()
        code = next(c for c in db["rates_8"] if c.startswith("6210"))
        comp = lambda text, lim: [{"id": 1, "code8": code, "编码": core.fmt(code, 8), "品名": "女式雨衣", "时间": "2026-09-16T10:00:00",
                                   "来源": "改正", "相似度": 1.0, "谁": "", "说明": "遮蔽不全"}]
        rows, st = rate.hybrid_search(db, "raincoat", limit=10, description="女式雨衣", channels=("company",), _company=comp)
        self.assertEqual(st["公司"]["数量"], 1)
        self.assertEqual(re.sub(r"\D", "", rows[0]["编码"]), code)
        self.assertEqual(rows[0]["公司先例"]["来源"], "改正")
        self.assertIn("公司", rows[0]["召回来源"])
        line = ai._candidate_line(db, 1, rows[0])
        self.assertIn("本公司此前申报", line)
        rows2, st2 = rate.hybrid_search(db, "raincoat", limit=10, channels=("company",), _company=lambda t, l: [])
        self.assertEqual((rows2, st2["公司"]["数量"]), ([], 0))

    def test_exact_hit_skips_model(self):
        import company, rate
        fake = lambda texts: [[0.02] * 1024 for _ in texts]
        company.record(DESC, "62103050", action="改正", note="A87383", _embed=fake)
        old = rate._default_company
        rate._default_company = lambda text, lim: company.search(text, limit=lim, _embed=fake)
        old_ch = rate.DEFAULT_CHANNELS
        rate.DEFAULT_CHANNELS = ("keyword", "company")
        try:
            p = _install(FakeProvider([KW]))         # 只剩出词这一次调用；精排不该发生
            out = ai.classify_product(_db(), DESC, origin="CN")
            self.assertEqual(out["归类方式"], "公司先例")
            self.assertEqual(out["candidates"][0]["编码"], "6210.30.50")
            self.assertIn("A87383", out["candidates"][0]["reason"])
            self.assertEqual(len(p.calls), 1)
            # 清单模式同样直接出结论，不进精排；"报告"那一次照常
            kw_b = json.dumps({"items": [{"index": 1, "keywords": ["raincoat"], "chapters": ["62"]}]})
            p = _install(FakeProvider([kw_b, "报告"]))
            evs = list(ai.analyze_list_stream(_db(), [{"name": DESC}], origin="CN"))
            det = next(e for e in evs if e["type"] == "details")["details"][0]
            self.assertEqual(det["归类方式"], "公司先例")
            self.assertEqual(det["编码"], "6210.30.50")
            self.assertEqual(len(p.calls), 2)
        finally:
            rate._default_company = old
            rate.DEFAULT_CHANNELS = old_ch

    def test_endpoints(self):
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest("fastapi testclient 不可用")
        import app, company
        client = TestClient(app.app)
        self.assertEqual(client.post("/api/company/precedent", json={"name": "x", "code": "12"}).status_code, 400)
        self.assertEqual(client.post("/api/company/precedent", json={"name": "x", "code": "00000000"}).status_code, 400)
        r = client.post("/api/company/precedent", json={"name": "不锈钢菜刀", "code": "8211.92.90", "action": "采纳"})
        self.assertEqual(r.status_code, 200); self.assertEqual(r.json()["count"], 1)
        lst = client.get("/api/company/precedents").json()
        self.assertEqual(lst["items"][0]["编码"], "8211.92.90")
        q = client.get("/api/company/precedents", params={"q": "菜刀"}).json()
        self.assertEqual(q["items"][0]["相似度"], 0.85)
        self.assertEqual(client.delete(f"/api/company/precedent/{lst['items'][0]['id']}").json()["count"], 0)


class TestAttributeCard(unittest.TestCase):
    """归类要素表：只填明确说了的，未提及并进需确认；开着时平铺与逐级链的提示都带表。"""

    CARD = json.dumps({"item": "女式雨衣", "material": "100% 涤纶梭织布 + TPU 膜", "construction": "两层层压",
                       "function": None, "form": "整件", "packaging": "null", "user": "", "specs": None,
                       "missing": ["TPU 膜是否位于外表面并完全遮蔽底布", "是否零售包装"]})

    def test_clean_and_text(self):
        c = ai._clean_card(json.loads(self.CARD))
        self.assertEqual(c["item"], "女式雨衣")
        self.assertIsNone(c["function"]); self.assertIsNone(c["packaging"]); self.assertIsNone(c["user"])
        self.assertEqual(len(c["missing"]), 2)
        t = ai.card_text(c)
        self.assertIn("材质成分：100% 涤纶", t); self.assertIn("未提及：TPU 膜", t); self.assertNotIn("功能用途", t)
        self.assertEqual(ai.card_text({}), "")
        self.assertEqual(ai._clean_card("not a dict"), {})
        nv = ai._merge_need_verify(["外层是否完全遮蔽"], c)
        self.assertEqual(len(nv), 3)
        self.assertTrue(nv[1].endswith("（描述未提及）"))

    def test_card_threads_into_flat_and_guided(self):
        old_on = ai.attribute_card_enabled; ai.attribute_card_enabled = lambda cfg=None: True
        old_ip = ai.attribute_card_in_prompt; ai.attribute_card_in_prompt = lambda cfg=None: True
        old_gs = ai.guided_settings
        ai.guided_settings = lambda cfg=None: {"enabled": True, "threshold": 0.9, "max_calls": 8, "effort": "",
                                                "force_chapters": set(), "cross_chapter": False}
        old_pc, old_ps, old_ex = guided._prec_by_code_default, guided._prec_semantic_default, guided._examples_default
        guided._prec_by_code_default = lambda c, ex, limit=6: []
        guided._prec_semantic_default = lambda d, ex, alive, limit=8: []
        guided._examples_default = lambda codes, ex: {}
        try:
            pool = ai._recall_candidates(_db(), ["raincoat", "woven", "coated"], limit=20, description=DESC)[:12]
            flat_code = next(r["编码"] for r in pool if not r["编码"].startswith("6210")).replace(".", "")
            flat = json.dumps({"picks": [{"code": flat_code, "confidence": 0.7, "reason": "flat", "need_verify": []}]})
            p = _install(FakeProvider([self.CARD, KW, flat, HEAD, DESCEND, VERIFY, PREC_OK]))
            out = ai.classify_product(_db(), DESC, origin="CN")
            self.assertEqual(out["要素表"]["item"], "女式雨衣")
            self.assertIn("归类要素表", p.calls[2][0]["content"])          # 精排 system 提示带表
            self.assertIn("未提及：TPU 膜", p.calls[3][1]["content"])      # 品目步用户消息带表
            self.assertIn("未提及：TPU 膜", p.calls[5][1]["content"])      # 对证步也带
            self.assertTrue(any("描述未提及" in x for x in out["candidates"][0]["需确认"]))
            # 关掉：不出表、提示里没有
            ai.attribute_card_enabled = lambda cfg=None: False
            p = _install(FakeProvider([KW, flat, HEAD, DESCEND, VERIFY, PREC_OK]))
            out = ai.classify_product(_db(), DESC, origin="CN")
            self.assertNotIn("要素表", out)
            self.assertNotIn("归类要素表", p.calls[1][0]["content"])
            # 开表但不进提示词（默认）：有表、有问题，提示词里没有表
            ai.attribute_card_enabled = lambda cfg=None: True
            ai.attribute_card_in_prompt = lambda cfg=None: False
            p = _install(FakeProvider([self.CARD, KW, flat, HEAD, DESCEND, VERIFY, PREC_OK]))
            out = ai.classify_product(_db(), DESC, origin="CN")
            self.assertEqual(out["要素表"]["item"], "女式雨衣")
            self.assertNotIn("归类要素表", p.calls[2][0]["content"])
            self.assertNotIn("归类要素表", p.calls[3][1]["content"])
            self.assertTrue(any("描述未提及" in x for x in out["candidates"][0]["需确认"]))
        finally:
            ai.attribute_card_enabled = old_on; ai.attribute_card_in_prompt = old_ip; ai.guided_settings = old_gs
            guided._prec_by_code_default, guided._prec_semantic_default, guided._examples_default = old_pc, old_ps, old_ex
            ai.reset_provider_cache()

    def test_batch_cards(self):
        old_on = ai.attribute_card_enabled; ai.attribute_card_enabled = lambda cfg=None: True
        old_ip = ai.attribute_card_in_prompt; ai.attribute_card_in_prompt = lambda cfg=None: True
        old_gs = ai.guided_settings
        ai.guided_settings = lambda cfg=None: {"enabled": False, "threshold": 0.9, "max_calls": 8, "effort": "",
                                                "force_chapters": set(), "cross_chapter": False}
        try:
            pool = ai._recall_candidates(_db(), ["raincoat", "woven", "coated"], limit=20, description=DESC)[:12]
            code = pool[0]["编码"].replace(".", "")
            kw_b = json.dumps({"items": [{"index": 1, "keywords": ["raincoat", "woven", "coated"], "chapters": ["62"]}]})
            cards = json.dumps({"items": [{"index": 1, "item": "女式雨衣", "missing": ["是否零售包装"]}]})
            rank_b = json.dumps({"picks": [{"index": 1, "code": code, "confidence": 0.95, "reason": "x"}]})
            p = _install(FakeProvider([kw_b, cards, rank_b, "报告"]))
            evs = list(ai.analyze_list_stream(_db(), [{"name": DESC}], origin="CN"))
            det = next(e for e in evs if e["type"] == "details")["details"][0]
            self.assertEqual(det["要素表"]["item"], "女式雨衣")
            self.assertIn("是否零售包装（描述未提及）", det["需确认"])
            self.assertIn("要素表：商品：女式雨衣", p.calls[2][1]["content"])
        finally:
            ai.attribute_card_enabled = old_on; ai.attribute_card_in_prompt = old_ip; ai.guided_settings = old_gs
            ai.reset_provider_cache()


class TestParallelEscalation(unittest.TestCase):
    """清单模式升级行并发：按内容路由回复的假 Provider，3 路并发跑 4 行都要拿到结果，顺序无关。"""

    def test_batch_parallel_all_rows_get_results(self):
        import threading
        lock = threading.Lock(); calls = []

        class Router(ai.BaseProvider):
            def __init__(self): super().__init__("router", 0)
            def chat(self, messages):
                sysm, user = messages[0]["content"], messages[1]["content"]
                with lock: calls.append(sysm[:12])
                if "只决定 4 位品目" in sysm: return HEAD
                if "按 GRI 6" in sysm: return DESCEND
                if "同级对证" in sysm: return VERIFY
                if "核对拟定编码" in sysm: return PREC_OK
                return "{}"
        gs = {"enabled": True, "threshold": 0.9, "max_calls": 8, "effort": "", "force_chapters": set(),
              "cross_chapter": False, "parallel": 3}
        old_pc, old_ps, old_ex = guided._prec_by_code_default, guided._prec_semantic_default, guided._examples_default
        guided._prec_by_code_default = lambda c, ex, limit=6: []
        guided._prec_semantic_default = lambda d, ex, alive, limit=8: []
        guided._examples_default = lambda codes, ex: {}
        try:
            pool = ai._recall_candidates(_db(), ["raincoat", "woven", "coated"], limit=20, description=DESC)
            jobs = [(i, DESC, pool[:12], ["62"], None) for i in range(1, 5)]
            done = []
            res = ai._run_guided_batch(_db(), jobs, "CN", Router(), gs, on_done=lambda k, g: done.append(k))
            self.assertEqual(sorted(res), [1, 2, 3, 4])
            self.assertTrue(all(r.get("编码") == "6210.30.50" for r in res.values()))
            self.assertEqual(sorted(done), [1, 2, 3, 4])
            self.assertEqual(len(calls), 16)   # 每行 4 次调用
            # 串行路径同样可用
            gs1 = {**gs, "parallel": 1}
            res1 = ai._run_guided_batch(_db(), jobs[:2], "CN", Router(), gs1)
            self.assertEqual(sorted(res1), [1, 2])
        finally:
            guided._prec_by_code_default, guided._prec_semantic_default, guided._examples_default = old_pc, old_ps, old_ex


# ---------- 决定性注释挂载（跨章） ----------

class TestDecisiveNotes(unittest.TestCase):
    """
    2026-09-16 实测的洞：PU 涂层涤纶情趣内衣，商品最后落 39 或 62 章，而决定它落哪边的
    第 59 章注释 2 从头到尾没进过上下文——模型凭训练记忆引第十一类注释 1(h) 就跳进 3926，
    15.8% 的货报成 42.5%。按特征词强制挂载该章注释。
    """
    NOTES = {"sections": {"XI": {"notes": "SEC-XI"}},
             "chapters": {"62": {"section": "XI", "notes": "C62", "us_notes": "U62"},
                          "59": {"section": "XI", "notes": "C59-注2(a)(3)", "us_notes": ""},
                          "61": {"section": "XI", "notes": "C61", "us_notes": ""}}}

    def test_trigger_words(self):
        for t in ("PU 涂层涤纶面料", "fabric laminated with polyurethane", "人造革情趣内衣",
                  "coated woven polyester", "PVC 皮革手袋", "覆膜无纺布"):
            ext, why = guided._extra_note_chapters(t)
            self.assertEqual(ext, [59], t)
            self.assertTrue(why and "第 59 章" in why[0])
        for t in ("女式全棉针织衬衫", "stainless steel kitchen knife", "锂离子电池"):
            self.assertEqual(guided._extra_note_chapters(t), ([], []), t)

    def test_already_shown_chapter_not_duplicated(self):
        self.assertEqual(guided._extra_note_chapters("PU 涂层布", (59, 62)), ([], []))

    def test_notes_block_puts_decisive_first(self):
        txt, _, _ = guided._notes_block([62, 61], self.NOTES, extra=[59], why=["第 59 章注释 2 决定归哪章"])
        self.assertLess(txt.index("C59-注2(a)(3)"), txt.index("C62"))
        self.assertIn("决定性，必须先判", txt)
        self.assertIn("第 59 章注释 2 决定归哪章", txt)
        self.assertEqual(txt.count("SEC-XI"), 1)      # 类注仍只给一次

    def test_absent_flag_ignores_decisive_chapter(self):
        # 展示章无注释、挂载章有注释：仍应报"注释缺席"，不能被挂载章顶替
        notes = {"sections": {}, "chapters": {"59": {"section": "XI", "notes": "C59", "us_notes": ""}}}
        _txt, _t, absent = guided._notes_block([62], notes, extra=[59], why=["w"])
        self.assertTrue(absent)

    def _apparel_pool(self):
        """只留 61/62 章的候选：召回自带 59 章时走的是正常展示路径，测不到"强制挂载"。"""
        rows = [r for r in _pool() if re.sub(r"\D", "", r["编码"])[:2] in ("61", "62")]
        self.assertTrue(rows, "服装候选为空，测试前提不成立")
        return rows

    def test_chain_attaches_and_records(self):
        p = FakeProvider([HEAD, DESCEND, VERIFY, PREC_OK])
        out = guided.classify_guided(_db(), "女式情趣内衣 55% 涤纶 45% 聚氨酯涂层", provider=p,
                                     rows=self._apparel_pool(), notes=self.NOTES, **NO_PREC)
        self.assertEqual(out["论证"]["决定性注释"], [59])
        self.assertNotIn(59, out["论证"]["展示的章"])                 # 挂注释，不把 59 章品目列给模型选
        self.assertIn("C59-注2(a)(3)", p.calls[0][1]["content"])     # 品目步看得见
        self.assertIn("C59-注2(a)(3)", p.calls[1][1]["content"])     # 下钻步也看得见
        self.assertIn("决定性，必须先判", p.calls[1][1]["content"])

    def test_no_trigger_no_attachment(self):
        p = FakeProvider([HEAD, DESCEND, VERIFY, PREC_OK])
        out = guided.classify_guided(_db(), "女式全棉针织衬衫", provider=p, rows=self._apparel_pool(),
                                     notes=self.NOTES, **NO_PREC)
        self.assertNotIn("决定性注释", out["论证"])
        self.assertNotIn("C59-注2(a)(3)", p.calls[0][1]["content"])
        self.assertNotIn("决定性，必须先判", p.calls[1][1]["content"])


# ---------- 先例回退后品目理由要跟着换 ----------

class TestHeadingReasonAfterRollback(unittest.TestCase):
    """实测遇到过：论证显示「品目 6211」，理由讲的却是 6114——先例步换了品目、理由没跟着换。"""

    def test_reason_follows_new_heading(self):
        target = guided._tree(_db()).by_h4["6202"][0]
        rev = json.dumps({"consistent": False, "revisit_heading": "6202", "revisit_code": None,
                          "reason": "先例 N1 把同类货归 6202"})
        d2 = json.dumps({"code": target, "level_reasons": ["x"], "need_verify": [], "confidence": 0.7})
        p = FakeProvider([HEAD, DESCEND, VERIFY, rev, d2])
        out = guided.classify_guided(_db(), DESC, provider=p, rows=_pool(), **NO_PREC)
        a = out["论证"]
        self.assertEqual(a["品目"], "6202")
        self.assertEqual(a["品目理由"], "若面料不合5903")      # HEAD 里 6202 自己那条
        self.assertNotIn("归6210", a["品目理由"])
        self.assertIn("品目 6202：若面料不合5903", out["reason"])

    def test_falls_back_to_precedent_reason_when_heading_unlisted(self):
        # 先例回退到一个模型没列过的品目：理由用先例步的，不能拿旧品目的理由冒充
        target = guided._tree(_db()).by_h4["6201"][0]
        rev = json.dumps({"consistent": False, "revisit_heading": "6201", "revisit_code": None,
                          "reason": "先例 N2 归 6201"})
        d2 = json.dumps({"code": target, "level_reasons": ["y"], "need_verify": [], "confidence": 0.6})
        p = FakeProvider([HEAD, DESCEND, VERIFY, rev, d2])
        out = guided.classify_guided(_db(), DESC, provider=p, rows=_pool(), **NO_PREC)
        self.assertEqual(out["论证"]["品目"], "6201")
        self.assertEqual(out["论证"]["品目理由"], "先例 N2 归 6201")

    def test_unchanged_heading_keeps_first_reason(self):
        p = FakeProvider([HEAD, DESCEND, VERIFY, PREC_OK])
        out = guided.classify_guided(_db(), DESC, provider=p, rows=_pool(), **NO_PREC)
        self.assertEqual(out["论证"]["品目理由"], "第62章注6：5903面料制成的服装归6210")
