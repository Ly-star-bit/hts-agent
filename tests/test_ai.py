# -*- coding: utf-8 -*-
"""
test_ai.py —— AI 层单元测试（使用假 Provider，不依赖真实模型服务）

运行：python -m unittest tests.test_ai -v
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import ai


class FakeProvider(ai.BaseProvider):
    """按调用顺序返回预设回复的假 Provider"""

    def __init__(self, replies):
        super().__init__(model="fake", temperature=0)
        self.replies = list(replies)
        self.calls = []

    def chat(self, messages):
        self.calls.append(messages)
        if not self.replies:
            return "{}"
        return self.replies.pop(0)


def _install_fake(replies):
    """把假 Provider 装入 ai 模块缓存"""
    ai.reset_provider_cache()
    ai._PROVIDER_CACHE["provider"] = FakeProvider(replies)
    ai._PROVIDER_CACHE["loaded"] = True
    return ai._PROVIDER_CACHE["provider"]


class TestExtractJson(unittest.TestCase):
    """JSON 提取"""

    def test_direct(self):
        self.assertEqual(ai._extract_json('{"a": 1}'), {"a": 1})

    def test_code_block(self):
        self.assertEqual(ai._extract_json('```json\n{"a": 1}\n```'), {"a": 1})

    def test_braces(self):
        self.assertEqual(ai._extract_json('结果如下 {"a": 1} 完毕'), {"a": 1})

    def test_invalid(self):
        with self.assertRaises(ai.AIProviderError):
            ai._extract_json("不是 JSON")

    def test_fallback(self):
        self.assertEqual(ai._extract_json("乱码", fallback={"a": 2}), {"a": 2})


class TestClassify(unittest.TestCase):
    """商品归类推荐"""

    @classmethod
    def setUpClass(cls):
        import core
        cls.db = core.load_db()

    def test_success(self):
        _install_fake([
            json.dumps({"keywords": ["lithium", "ion", "battery"], "chapters": ["85"]}),
            json.dumps({"picks": [{"code": "8507.60.00", "confidence": 0.9, "reason": "锂电池"}]}),
        ])
        r = ai.classify_product(self.db, "便携式锂电池")
        self.assertNotIn("error", r)
        self.assertEqual(r["candidates"][0]["编码"], "8507.60.00")
        self.assertIn("301判定", r["candidates"][0])
        self.assertIn("disclaimer", r)

    def test_no_provider(self):
        ai.reset_provider_cache()
        ai._PROVIDER_CACHE["loaded"] = True
        ai._PROVIDER_CACHE["provider"] = None
        r = ai.classify_product(self.db, "锂电池")
        self.assertIn("error", r)

    def test_keywords_fail(self):
        _install_fake([json.dumps({"keywords": [], "chapters": []})])
        r = ai.classify_product(self.db, "锂电池")
        self.assertIn("error", r)


class TestAskQuestion(unittest.TestCase):
    """自然语言问税"""

    @classmethod
    def setUpClass(cls):
        import core
        cls.db = core.load_db()

    def test_with_codes(self):
        # 问题含编码：无需 LLM 也能走本地查询
        _install_fake([json.dumps({"interpretation": "解读文本"})])
        r = ai.ask_tax_question(self.db, "查一下 8507.60.00 的关税")
        self.assertEqual(r["type"], "codes")
        self.assertEqual(r["results"][0]["8位子目"], "8507.60.00")

    def test_faq_refused_not_answered(self):
        """
        政策类咨询不再由模型自由作答。

        原实现把问题直接丢给 LLM、不碰任何本地数据，与本项目"税率、判定条件、
        证据一律来自官方税则原文，AI 只负责在候选中挑选"的原则相悖——报关场景里
        用户分不出哪句有依据、哪句是模型编的，一段听起来专业的错误答复比一句
        "答不了"危险得多。
        """
        _install_fake([json.dumps({"type": "faq", "question": "什么是301关税"}),
                       "301 是美国对华加征关税。"])
        r = ai.ask_tax_question(self.db, "什么是301关税")
        self.assertNotIn("type", r)
        self.assertNotIn("answer", r, "不得返回模型自由生成的答复")
        self.assertIn("error", r)
        self.assertIn("无法为政策解释提供依据", r["error"])

    def test_no_provider(self):
        ai.reset_provider_cache()
        ai._PROVIDER_CACHE["loaded"] = True
        ai._PROVIDER_CACHE["provider"] = None
        r = ai.ask_tax_question(self.db, "锂电池的税率")
        self.assertIn("error", r)


class TestAnalyzeList(unittest.TestCase):
    """批量清单分析"""

    @classmethod
    def setUpClass(cls):
        import core
        cls.db = core.load_db()

    def test_success(self):
        _install_fake([
            json.dumps({"items": [{"index": 1, "keywords": ["lithium", "battery"], "chapters": ["85"]},
                                  {"index": 2, "keywords": ["wood", "furniture"], "chapters": ["94"]}]}),
            json.dumps({"picks": [{"index": 1, "code": "8507.60.00", "confidence": 0.8, "reason": "锂电池"},
                                  {"index": 2, "code": "9403.60.80", "confidence": 0.7, "reason": "木家具"}]}),
            "分析报告文本",
        ])
        r = ai.analyze_list(self.db, [{"name": "锂电池"}, {"name": "木制家具"}])
        self.assertNotIn("error", r)
        self.assertEqual(len(r["details"]), 2)
        self.assertEqual(r["stats"]["total"], 2)
        self.assertIn("报告", r["report"])

    def test_too_many(self):
        _install_fake([])
        r = ai.analyze_list(self.db, [{"name": f"商品{i}"} for i in range(31)])
        self.assertIn("error", r)


class TestConfig(unittest.TestCase):
    """AI 配置读写（Web 端手动配置）"""

    def setUp(self):
        # 将配置路径临时指向测试文件，避免污染真实 ai_config.json
        import tempfile
        self._orig = ai.CONFIG_FILE
        self._tmp = os.path.join(tempfile.mkdtemp(), "ai_config_test.json")
        ai.CONFIG_FILE = self._tmp

    def tearDown(self):
        ai.CONFIG_FILE = self._orig
        ai.reset_provider_cache()
        if os.path.exists(self._tmp):
            os.remove(self._tmp)

    def test_load_default(self):
        cfg = ai.load_config()
        self.assertIsNone(cfg["provider"])
        self.assertEqual(cfg["api_key"], "")

    def test_save_and_mask(self):
        masked = ai.save_config({
            "provider": "openai_compat",
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-4o-mini",
            "api_key": "sk-test1234567890abcdef",
        })
        self.assertEqual(masked["provider"], "openai_compat")
        self.assertTrue(masked["api_key_set"])
        self.assertIn("***", masked["api_key_masked"])
        self.assertNotIn("sk-test1234567890abcdef", json.dumps(masked))
        # 掩码里不含完整明文
        self.assertNotEqual(masked.get("api_key"), "sk-test1234567890abcdef")

    def test_save_keep_key(self):
        ai.save_config({"api_key": "sk-abcdefghijklmnop"})
        # '__KEEP__' 应保留原值
        masked = ai.save_config({"api_key": "__KEEP__", "model": "gpt-4o"})
        self.assertTrue(masked["api_key_set"])
        self.assertEqual(masked["model"], "gpt-4o")

    def test_save_invalid_provider_ignored(self):
        masked = ai.save_config({"provider": "not_a_provider"})
        self.assertIsNone(masked["provider"])

    def test_test_connection_fake(self):
        class FakeProvider(ai.BaseProvider):
            def __init__(self):
                super().__init__(model="fake")

            def chat(self, messages):
                return "pong"

        r = ai.test_connection(FakeProvider())
        self.assertTrue(r["ok"])

    def test_test_connection_none(self):
        ai.reset_provider_cache()
        ai._PROVIDER_CACHE["loaded"] = True
        ai._PROVIDER_CACHE["provider"] = None
        ai._PROVIDER_CACHE["error"] = "未配置"
        r = ai.test_connection()
        self.assertFalse(r["ok"])


class TestHallucinationGuard(unittest.TestCase):
    """LLM 给出的编码不在本地召回候选内时，两条归类链路都必须拒绝，不能兜底"""

    @classmethod
    def setUpClass(cls):
        import core
        cls.db = core.load_db()

    def test_stream_emits_details_before_report(self):
        """
        流式的全部意义在于表格先于报告落地。

        后端是三次批量调用（出词 / 精排 / 报告），表格在精排结束时就齐了，
        报告还要再等一轮 LLM（实测占总耗时约三成）。若哪天有人把 details
        事件挪到报告之后，等待时间就白白涨回去——这条测试钉住这个顺序。
        """
        order = [ev["type"] for ev in
                 ai.analyze_list_stream(self.db, [{"name": "锂电池"}, {"name": "木制家具"}])]
        self.assertIn("details", order)
        self.assertIn("done", order)
        self.assertLess(order.index("details"), order.index("done"))
        if "report" in order:
            self.assertLess(order.index("details"), order.index("report"))
        # 阶段事件要覆盖四步，进度条才不会跳格
        stages = [ev["stage"] for ev in
                  ai.analyze_list_stream(self.db, [{"name": "锂电池"}])
                  if ev["type"] == "stage"]
        self.assertEqual(set(stages), {"keywords", "recall", "rank", "report"})

    def test_stream_errors_are_events_not_exceptions(self):
        """
        超限/未配置要走 error 事件。生成器里抛异常，前端只会看到连接莫名断开，
        连"为什么失败"都拿不到。
        """
        evs = list(ai.analyze_list_stream(self.db, [{"name": f"商品{i}"} for i in range(31)]))
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["type"], "error")
        self.assertIn("30", evs[0]["message"])

    def test_analyze_list_wrapper_matches_stream(self):
        """一次性版本必须与流式版本给出同一份结果——两条路径不能各说各话"""
        r = ai.analyze_list(self.db, [{"name": "锂电池"}])
        final = None
        for ev in ai.analyze_list_stream(self.db, [{"name": "锂电池"}]):
            if ev["type"] == "done":
                final = ev["result"]
        self.assertEqual(len(r["details"]), len(final["details"]))
        self.assertEqual(r["stats"], final["stats"])

    def test_analyze_list_rejects_code_outside_candidates(self):
        # 第一轮出关键词，第二轮 pick 一个候选集里不存在的编码
        _install_fake([
            '{"items": [{"index": 1, "keywords": ["furniture", "wood"]}]}',
            '{"picks": [{"index": 1, "code": "9403.99.90", "confidence": 0.95,'
            ' "reason": "木制卧室家具，整体归入此目"}]}',
            "报告",
        ])
        r = ai.analyze_list(self.db, [{"name": "木制卧室家具"}])
        d = r["details"][0]
        self.assertIn("error", d, "幻觉编码必须被拒绝，不能静默替换成召回第一名")
        # 不得把 AI 为别的编码写的理由/置信度透传出去
        self.assertNotIn("confidence", d)
        self.assertNotIn("reason", d)

    def test_classify_accepts_code_without_dots(self):
        # 模型返回不带点的编码是常见形态，不应被误判为幻觉
        _install_fake([
            '{"keywords": ["lithium", "battery"]}',
            '{"picks": [{"code": "85076000", "confidence": 0.9, "reason": "锂离子电池"}]}',
        ])
        r = ai.classify_product(self.db, "锂电池")
        self.assertNotIn("error", r)
        self.assertTrue(r["candidates"])
        self.assertEqual(r["candidates"][0]["编码"], "8507.60.00")

    def test_classify_still_rejects_true_hallucination(self):
        _install_fake([
            '{"keywords": ["lithium", "battery"]}',
            '{"picks": [{"code": "0000.00.00", "confidence": 0.9, "reason": "编造的"}]}',
        ])
        r = ai.classify_product(self.db, "锂电池")
        self.assertIn("error", r)


class TestRerankContext(unittest.TestCase):
    """精排候选行必须带归类路径与判定条件，否则 LLM 面对一堆 'Other' 只能盲选"""

    @classmethod
    def setUpClass(cls):
        import core
        cls.db = core.load_db()

    def _rerank_prompt(self, desc="男式梭织羊毛混纺夹克"):
        _install_fake([
            '{"keywords": ["wool", "coat", "woven"], "chapters": ["62"]}',
            '{"picks": []}',
        ])
        provider = ai.get_provider()
        ai.classify_product(self.db, desc)
        return provider.calls[1][1]["content"]

    def test_candidate_lines_carry_path(self):
        text = self._rerank_prompt()
        self.assertIn(" > ", text, "候选行未带归类路径")

    def test_candidate_lines_carry_criteria(self):
        self.assertIn("判定条件", self._rerank_prompt())

    def test_candidate_line_builder(self):
        row = {"编码": "6201.40.40", "商品描述": "Containing 36 percent or more by weight of wool",
               "一般税率": "49.5¢/kg + 19.6%", "301判定": "是", "301加征": "+7.5%", "附加税": ""}
        line = ai._candidate_line(self.db, 1, row)
        self.assertIn("6201.40.40", line)
        self.assertIn(" > ", line)                 # 路径
        self.assertIn("含量阈值", line)             # 判定条件
        self.assertIn("man-made fibers", line)     # 祖先里的材质限定

    def test_candidate_line_survives_missing_code(self):
        row = {"编码": "0000.00.00", "商品描述": "x", "一般税率": "", "301判定": "否",
               "301加征": "", "附加税": ""}
        self.assertIn("0000.00.00", ai._candidate_line(self.db, 1, row))


class TestClassifyEnrichment(unittest.TestCase):
    """归类结果要带判定条件 / 证据清单 / 需确认项，且这些不经模型加工"""

    @classmethod
    def setUpClass(cls):
        import core
        cls.db = core.load_db()

    def _classify(self, confidence=0.82):
        _install_fake([
            '{"keywords": ["wool", "coat", "woven"], "chapters": ["62"]}',
            '{"picks": [{"code": "6201.40.15", "confidence": ' + repr(confidence) + ','
            ' "reason": "依据候选行原文", "need_verify": ["羊毛含量是否达到 36%"]}]}',
        ])
        return ai.classify_product(self.db, "男式梭织羊毛混纺夹克")

    def test_candidate_carries_criteria_and_evidence(self):
        c = self._classify()["candidates"][0]
        self.assertTrue(c["判定条件"])
        self.assertTrue(c["证据清单"])
        kinds = {x["类型"] for x in c["判定条件"]}
        self.assertIn("含量阈值", kinds)
        self.assertTrue(any(k.startswith("织法") for k in kinds))

    def test_need_verify_passed_through(self):
        c = self._classify()["candidates"][0]
        self.assertEqual(c["需确认"], ["羊毛含量是否达到 36%"])

    def test_confidence_clamped(self):
        self.assertEqual(ai._clamp_confidence("非常高"), 0.0)
        self.assertEqual(ai._clamp_confidence(5), 1.0)
        self.assertEqual(ai._clamp_confidence(-2), 0.0)
        self.assertEqual(ai._clamp_confidence(None), 0.0)
        self.assertAlmostEqual(ai._clamp_confidence("0.7"), 0.7)

    def test_criteria_not_model_generated(self):
        # 判定条件来自本地税则，与模型给的 reason 无关：换个 reason 条件不变
        a = self._classify()["candidates"][0]["判定条件"]
        _install_fake([
            '{"keywords": ["wool", "coat", "woven"], "chapters": ["62"]}',
            '{"picks": [{"code": "6201.40.15", "confidence": 0.1, "reason": "胡说八道"}]}',
        ])
        b = ai.classify_product(self.db, "随便什么")["candidates"][0]["判定条件"]
        self.assertEqual(a, b)


class TestAIOriginPlumbing(unittest.TestCase):
    """origin 必须贯通到 AI 链路，否则界面选了越南仍按中国原产算 301"""

    @classmethod
    def setUpClass(cls):
        import core
        cls.db = core.load_db()

    def test_classify_respects_origin(self):
        replies = ['{"keywords": ["lithium", "battery"]}',
                   '{"picks": [{"code": "8507.60.00", "confidence": 0.9, "reason": "锂电池"}]}']
        _install_fake(list(replies))
        cn = ai.classify_product(self.db, "锂电池", origin="CN")["candidates"][0]
        _install_fake(list(replies))
        vn = ai.classify_product(self.db, "锂电池", origin="VN")["candidates"][0]
        # 8507.60.00 在 ANNEX II 但带 Aircraft 范围限制，按不豁免保守计 FLIP 301 12.5%：
        # 中国 3.4% + 301 25% + 12.5% = 40.9%；越南不适用中国 301 → 3.4% + 12.5% = 15.9%
        self.assertIn("40.9%", cn["总税负估算"])
        self.assertIn("15.9%", vn["总税负估算"])
        self.assertNotEqual(cn["总税负估算"], vn["总税负估算"])


if __name__ == "__main__":
    unittest.main()


class TestOllamaThink(unittest.TestCase):
    """
    Ollama 思考模式默认关：qwen3 开着每次先吐几百 token 隐藏推理，
    两轮调用的搜索辅助要 27s——"按钮一直转"的根源。请求体里必须显式带 think。
    """

    def _capture(self, provider):
        import httpx
        sent = {}

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"message": {"content": '{"ok": 1}'}}

        def fake_post(url, json=None, timeout=None):
            sent["url"], sent["json"] = url, json
            return _Resp()

        orig = httpx.post
        httpx.post = fake_post
        try:
            out = provider.chat([{"role": "user", "content": "hi"}])
        finally:
            httpx.post = orig
        return out, sent

    def test_think_default_off(self):
        out, sent = self._capture(ai.OllamaProvider(model="qwen3:8b"))
        self.assertEqual(out, '{"ok": 1}')
        self.assertIs(sent["json"]["think"], False)
        self.assertTrue(sent["url"].endswith("/api/chat"))

    def test_think_opt_in(self):
        _, sent = self._capture(ai.OllamaProvider(model="qwen3:8b", think=True))
        self.assertIs(sent["json"]["think"], True)

    def test_config_think_parsed_and_passed(self):
        import tempfile
        orig = ai.CONFIG_FILE
        ai.CONFIG_FILE = os.path.join(tempfile.mkdtemp(), "ai_config.json")
        try:
            ai.save_config({"provider": "ollama", "model": "qwen3:8b", "think": "true"})
            self.assertIs(ai.load_config()["think"], True)
            ai.save_config({"think": False})
            self.assertIs(ai.load_config()["think"], False)
            ai.reset_provider_cache()
            p = ai.get_provider()
            self.assertIsInstance(p, ai.OllamaProvider)
            self.assertIs(p.think, False)
        finally:
            ai.CONFIG_FILE = orig
            ai.reset_provider_cache()


class TestAICarriesCaveats(unittest.TestCase):
    """
    AI 链路必须把税负的**警示字段**一起带出来，不能只给一个总数。

    此前 classify_product 的候选里只有 301判定/301加征/总税负估算：
    总税负虽然算对了（走的是同一个 rate.calc_total），但"其中 12.5% 取决于商品用途"
    （FLIP ANNEX II 范围限制）和"其中 25% 可能已被整号排除"这两条都被丢掉了。
    一个看起来很确定、实际有条件的数字，比一个错误的数字更容易被直接拿去报关。
    喂给模型的候选行同理——模型是拿这行文本在挑码。
    """

    @classmethod
    def setUpClass(cls):
        import core
        cls.db = core.load_db()

    def test_candidate_line_includes_flip_and_exclusion(self):
        import rate
        rows = rate.search(self.db, "9025.19.80", limit=1, sort="relevance", origin="CN")
        line = ai._candidate_line(self.db, 1, rows[0])
        self.assertIn("FLIP301:", line)
        self.assertIn("301排除:", line)

    def test_classify_result_carries_caveat_fields(self):
        replies = ['{"keywords": ["thermometer"]}',
                   '{"picks": [{"code": "9025.19.80", "confidence": 0.9, "reason": "温度计"}]}']
        _install_fake(list(replies))
        res = ai.classify_product(self.db, "温度计", origin="CN")
        c = res["candidates"][0]
        self.assertIn("范围存疑", c["FLIP 301加征"])
        self.assertIn("待核", c["301排除"])
        self.assertTrue(c["301排除明细"], "排除明细要带出来，否则用户无从核对描述")
        self.assertIn("整号", c["备注"])

