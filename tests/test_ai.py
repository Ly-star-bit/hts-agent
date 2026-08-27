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

    def test_faq(self):
        _install_fake([json.dumps({"type": "faq", "question": "什么是301关税"}),
                       "301 是美国对华加征关税。"])
        r = ai.ask_tax_question(self.db, "什么是301关税")
        self.assertEqual(r["type"], "faq")
        self.assertIn("301", r["answer"])

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


if __name__ == "__main__":
    unittest.main()
