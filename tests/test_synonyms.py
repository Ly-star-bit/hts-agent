# -*- coding: utf-8 -*-
"""
test_synonyms.py —— 用户词汇 → 官方检索词映射

运行：python -m unittest tests.test_synonyms -v

这里最重要的是 test_every_entry_retrieves：词表里每一条都必须能在真实语料上
检索到结果。之前手写的 7 条就因为凭印象填了语料中根本不存在的词而失效
（tyres 是英式拼法、rucksacks/chargers 不存在、slippers 只出现在 99 章、
denim 只出现在 52 章的面料上而非成衣）。没有这条测试，词表会悄悄烂掉。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import core
import rate


class TestSynonymTable(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.db = core.load_db()
        rate._clear_synonyms_cache()
        cls.terms = rate.load_synonyms()

    def test_table_loads(self):
        self.assertGreater(len(self.terms), 100)

    def test_every_entry_retrieves(self):
        """每条映射都必须能检索到结果，否则等于死条目"""
        dead = [k for k in self.terms
                if not rate.search(self.db, k, limit=3, sort="relevance")]
        self.assertEqual(dead, [], f"以下词条在语料中检索不到：{dead}")

    def test_values_are_lowercase_lists(self):
        for k, v in self.terms.items():
            self.assertIsInstance(v, list, k)
            self.assertTrue(v, k)
            for t in v:
                self.assertEqual(t, t.lower(), f"{k} 的检索词未小写：{t}")


class TestExpandQuery(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.db = core.load_db()
        rate._clear_synonyms_cache()

    def test_chinese_expanded(self):
        exp, applied, left = rate.expand_query("锂电池")
        self.assertIn("lithium", exp)
        self.assertIn("batteries", exp)
        self.assertEqual(left, [])
        self.assertEqual(applied[0]["输入词"], "锂电池")

    def test_longest_key_wins(self):
        # '太阳能电池板' 必须整体命中，否则会退化成 '太阳能' 而丢掉 panels
        exp, applied, _ = rate.expand_query("太阳能电池板")
        self.assertIn("panels", exp)
        self.assertEqual([a["输入词"] for a in applied], ["太阳能电池板"])

    def test_english_whole_word_only(self):
        # 'ic' 是词表里的键，但不能命中 'plastic' 里的 ic
        exp, applied, _ = rate.expand_query("plastic")
        self.assertEqual(applied, [])
        self.assertEqual(exp, "plastic")

    def test_english_synonym(self):
        exp, applied, _ = rate.expand_query("solar")
        self.assertIn("photovoltaic", exp)
        self.assertTrue(applied)

    def test_leftover_reported(self):
        # 未映射的中文必须回报，不能静默丢弃——否则用户以为已完整检索
        _exp, _applied, left = rate.expand_query("不锈钢智能保温杯")
        self.assertIn("智能", left)

    def test_no_leftover_when_fully_mapped(self):
        _exp, _applied, left = rate.expand_query("针织手套")
        self.assertEqual(left, [])

    def test_untouched_when_no_match(self):
        exp, applied, left = rate.expand_query("battery")
        self.assertEqual(applied, [])
        self.assertEqual(exp, "battery")
        self.assertEqual(left, [])

    def test_single_char_particles_not_reported(self):
        # '制' 这类单字虚词不算未识别，否则每个中文查询都会报警
        _exp, _applied, left = rate.expand_query("塑料制餐椅")
        self.assertNotIn("制", left)


class TestChineseSearch(unittest.TestCase):
    """中文商品名此前一律返回 0 条（索引为纯英文）"""

    @classmethod
    def setUpClass(cls):
        cls.db = core.load_db()
        rate._clear_synonyms_cache()

    def test_common_chinese_queries(self):
        cases = {
            "锂电池": "8507",
            "太阳能电池板": "8541",
            "针织手套": "6116",
            "不锈钢餐具": "82",
        }
        for q, prefix in cases.items():
            rows = rate.search(self.db, q, limit=5, sort="relevance")
            self.assertTrue(rows, f"'{q}' 返回空")
            self.assertTrue(
                any(r["编码"].replace(".", "").startswith(prefix) for r in rows),
                f"'{q}' 的结果里没有 {prefix} 开头的编码：{[r['编码'] for r in rows]}")

    def test_chinese_results_exclude_special_chapters(self):
        for q in ("锂电池", "针织手套"):
            rows = rate.search(self.db, q, limit=20)
            self.assertFalse([r for r in rows if r["编码"][:2] in ("98", "99")])


if __name__ == "__main__":
    unittest.main()
