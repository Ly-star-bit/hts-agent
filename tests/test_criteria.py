# -*- coding: utf-8 -*-
"""
test_criteria.py —— 归类判定条件抽取与候选比较

运行：python -m unittest tests.test_criteria -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import core
import criteria


class TestExtract(unittest.TestCase):
    """判定条件抽取：条件多写在归类路径的祖先上，本级品名只写差异"""

    @classmethod
    def setUpClass(cls):
        cls.db = core.load_db()

    def _kinds(self, code8):
        return {c["类型"] for c in criteria.extract(self.db, code8)}

    def test_weight_threshold_extracted(self):
        # 6201.40.40 靠"羊毛≥36%"与同父节点的 6201.40.35 分开，税率天差地别
        crs = criteria.extract(self.db, "62014040")
        thr = [c for c in crs if c["类型"] == "含量阈值"]
        self.assertTrue(thr, "未抽出含量阈值")
        self.assertIn("36 percent", thr[0]["原文"])
        self.assertIn("成分检测报告", thr[0]["证据"])

    def test_sibling_without_threshold(self):
        # 6201.40.35 没有羊毛阈值条件——这正是它税率低的原因
        self.assertNotIn("含量阈值", self._kinds("62014035"))

    def test_material_from_ancestor(self):
        # 'Of man-made fibers' 在父节点上，本级品名看不出材质
        crs = criteria.extract(self.db, "62014035")
        mat = [c for c in crs if c["类型"] == "主材质"]
        self.assertTrue(mat)
        self.assertEqual(mat[0]["出处"], "归类路径")
        self.assertIn("man-made fibers", mat[0]["原文"].lower())

    def test_value_threshold(self):
        # 3926.20.60 的 Free 是有单价上限的，光看品名容易漏掉
        crs = criteria.extract(self.db, "39262060")
        val = [c for c in crs if c["类型"] == "价值门槛"]
        self.assertTrue(val, "未抽出价值门槛")
        self.assertIn("10", val[0]["原文"])

    def test_chapter_rule_knit_vs_woven(self):
        # 61/62 章的针织/梭织之分是章级硬规则，不靠文本匹配
        self.assertTrue(any(k.startswith("织法") for k in self._kinds("62014035")))
        self.assertTrue(any(k.startswith("织法") for k in self._kinds("61091000")))

    def test_no_overlapping_duplicates(self):
        # 'Containing 36 percent ... of wool' 已作为含量阈值抽出，
        # 其中的 'of wool' 不应再单独报一条主材质
        crs = criteria.extract(self.db, "62014040")
        own = [c for c in crs if c["出处"] == "本级品名"]
        self.assertEqual(len([c for c in own if c["类型"] == "主材质"]), 0)

    def test_unknown_code_is_empty_not_error(self):
        self.assertEqual(criteria.extract(self.db, "00000000"), [])

    def test_evidence_list_dedupes(self):
        crs = criteria.extract(self.db, "62014040")
        ev = criteria.evidence_list(crs)
        self.assertEqual(len(ev), len(set(ev)))
        self.assertTrue(ev)


class TestCompare(unittest.TestCase):
    """候选并列比较：跨章即典型归类分歧"""

    @classmethod
    def setUpClass(cls):
        cls.db = core.load_db()

    def test_cross_chapter_flagged(self):
        # 塑料雨衣(39) vs 梭织夹克(62)：用户描述的"有没有 PU 涂层"那个场景
        r = criteria.compare(self.db, ["3926.20.60", "6201.40.35"])
        self.assertTrue(r["跨章"])
        self.assertEqual(r["章列表"], ["39", "62"])
        self.assertIn("归类分歧", r["分歧提示"])
        self.assertIn("预裁定", r["分歧提示"])

    def test_same_chapter_not_flagged(self):
        r = criteria.compare(self.db, ["6201.40.35", "6201.40.40"])
        self.assertFalse(r["跨章"])
        self.assertEqual(r["分歧提示"], "")

    def test_candidates_carry_criteria_and_rate(self):
        r = criteria.compare(self.db, ["3926.20.60", "6201.40.35"])
        self.assertEqual(len(r["候选"]), 2)
        for it in r["候选"]:
            self.assertTrue(it["完整品名"])
            self.assertIn("判定条件", it)

    def test_accepts_dotted_and_plain_codes(self):
        a = criteria.compare(self.db, ["6201.40.35", "6201.40.40"])
        b = criteria.compare(self.db, ["62014035", "62014040"])
        self.assertEqual([i["编码"] for i in a["候选"]],
                         [i["编码"] for i in b["候选"]])


class TestQueryIntegration(unittest.TestCase):
    """判定条件要出现在查询结果里，且抽取失败不能影响税率判定"""

    @classmethod
    def setUpClass(cls):
        cls.db = core.load_db()

    def test_query_one_carries_criteria(self):
        r = core.query_one(self.db, "62014040", origin="CN")
        self.assertIn("判定条件", r)
        self.assertTrue(r["判定条件"])

    def test_criteria_failure_does_not_break_query(self):
        # criteria 抽取异常时，税率判定必须照常返回
        import criteria as _c
        orig = _c.extract
        _c.extract = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            r = core.query_one(self.db, "85076000", origin="CN")
            self.assertEqual(r["判定条件"], [])
            self.assertEqual(r["301判定"], "是")      # 主链路不受影响
            self.assertEqual(r["一般税率"], "3.4%")
        finally:
            _c.extract = orig


if __name__ == "__main__":
    unittest.main()
