# -*- coding: utf-8 -*-
"""
test_dispute.py —— 一物多号的自动识别（criteria.detect_dispute）

背景：原先「归类对比」是搜索页下面一个独立卡片，要用户先勾选候选再点按钮。
但用户搜的时候并不知道自己的商品有归类分歧——"锂电池"同时命中 8507（蓄电池）
与 8506（原电池），这件事恰恰是工具该告诉用户的，不该等用户先想到。

这里锁四件事：
  1. 按 4 位品目分组，不按 8 位子目（同品目下的子目是参数细分，不是分歧）
  2. 单一品目不报分歧（否则每次搜索都弹警告，很快就被无视）
  3. 基础税率最低 ≠ 总税负最低时必须明说（301/FLIP 会把顺序翻过来）
  4. 从量税折算不出百分比的候选要报出来，不能让用户以为"税差 0"
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import core
import criteria
import rate


def row(code, base=None, desc="x"):
    return {"编码": code, "商品描述": desc, "完整品名": desc,
            "一般税率": "", "等效从价": "" if base is None else f"{base}%",
            "等效从价数值": base, "301判定": ""}


class TestGrouping(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.db = core.load_db()

    def test_same_heading_is_not_a_dispute(self):
        """同一 4 位品目下的多个子目只是参数细分，不构成归类分歧"""
        rows = [row("8507.60.00", 3.4), row("8507.80.00", 3.4), row("8507.90.40", 3.5)]
        d = criteria.detect_dispute(self.db, rows)
        self.assertFalse(d["有分歧"])
        self.assertEqual(len(d["分组"]), 1)
        self.assertEqual(d["分组"][0]["同品目候选数"], 3)

    def test_cross_heading_is_a_dispute(self):
        rows = [row("8507.60.00", 3.4), row("8506.50.00", 2.7)]
        d = criteria.detect_dispute(self.db, rows)
        self.assertTrue(d["有分歧"])
        self.assertEqual({g["品目"] for g in d["分组"]}, {"8507", "8506"})

    def test_cross_chapter_flagged(self):
        rows = [row("3926.20.60", 0.0), row("6201.40.35", 14.9)]
        d = criteria.detect_dispute(self.db, rows)
        self.assertTrue(d["跨章"])
        self.assertEqual(d["章列表"], ["39", "62"])
        self.assertIn("GRI", d["提示"])

    def test_same_chapter_not_flagged_cross(self):
        d = criteria.detect_dispute(self.db, [row("8507.60.00", 3.4), row("8506.50.00", 2.7)])
        self.assertFalse(d["跨章"])

    def test_representative_is_most_relevant(self):
        """代表编码取该品目第一次出现的那条（搜索按相关度排序）"""
        rows = [row("8507.80.00", 3.4), row("8507.60.00", 3.4), row("8506.50.00", 2.7)]
        d = criteria.detect_dispute(self.db, rows)
        rep = [g for g in d["分组"] if g["品目"] == "8507"][0]
        self.assertEqual(rep["编码"], "8507.80.00")

    def test_only_top_k_considered(self):
        """靠后的结果多是同词碰巧命中，纳入会把无关品目当成候选"""
        rows = [row("8507.60.00", 3.4)] * 8 + [row("9503.00.00", 0.0)]
        d = criteria.detect_dispute(self.db, rows, top_k=8)
        self.assertEqual(len(d["分组"]), 1)

    def test_groups_capped_but_reported(self):
        """超出展示上限的品目要计数告知，不能静默截断"""
        rows = [row(f"{h}01.00.00", 1.0) for h in ("39", "42", "61", "62", "63", "64", "65")]
        d = criteria.detect_dispute(self.db, rows, top_k=10)
        self.assertEqual(len(d["分组"]), criteria.DISPUTE_MAX_GROUPS)
        self.assertTrue(d["未展示品目"])
        self.assertIn("未展示", d["提示"])

    def test_empty_and_short_codes(self):
        self.assertFalse(criteria.detect_dispute(self.db, [])["有分歧"])
        self.assertFalse(criteria.detect_dispute(self.db, [row("85", None)])["有分歧"])


class TestNarrowingQuestions(unittest.TestCase):
    """
    信息不足时补问什么。

    相关度是逐词二元计分，同一父节点下的子目必然同分——'jacket' 200 条结果
    只有 2 个不同分值，最大并列组 189 条。给这堆并列硬造排序是在替用户猜；
    它们同分恰恰说明查询信息不足，正确的输出是把决定分类的属性问回去。
    """

    @classmethod
    def setUpClass(cls):
        cls.db = core.load_db()

    def _ask(self, q):
        rows = rate.search(self.db, q, limit=200, sort="relevance")
        return {n["类型"]: n for n in criteria.narrowing_questions(self.db, rows)}

    def test_material_asked_when_unspecified(self):
        """用户没说材质，Of cotton / Of wool / Of man-made fibers 就该被问"""
        got = self._ask("梭织涂层夹克")
        self.assertIn("主材质", got)
        vals = " ".join(got["主材质"]["取值"]).lower()
        self.assertIn("cotton", vals)
        self.assertIn("wool", vals)

    def test_value_threshold_asked(self):
        """不锈钢餐具的税号取决于单价档位，这个必须问"""
        got = self._ask("不锈钢餐具")
        self.assertIn("价值门槛", got)
        self.assertIn("¢", " ".join(got["价值门槛"]["取值"]))

    def test_evidence_attached(self):
        got = self._ask("不锈钢餐具")
        self.assertIn("发票", got["价值门槛"]["证据"])

    def test_single_value_not_asked(self):
        """所有候选在某维度取值一致时，问了也不缩小范围，不该出现"""
        rows = rate.search(self.db, "梭织涂层夹克", limit=200, sort="relevance")
        for n in criteria.narrowing_questions(self.db, rows):
            self.assertGreaterEqual(n["取值总数"], 2, f"{n['类型']} 只有一种取值不该被问")

    def test_nothing_to_ask_when_determined(self):
        """锂电池落在 8507.60，没有待定条件"""
        self.assertEqual(self._ask("锂电池"), {})

    def test_values_deduped_case_insensitively(self):
        """'Containing' 与 'containing' 是同一条件，不能算两种取值"""
        rows = rate.search(self.db, "jacket", limit=200, sort="relevance")
        for n in criteria.narrowing_questions(self.db, rows):
            low = [v.lower() for v in n["取值"]]
            self.assertEqual(len(low), len(set(low)), f"{n['类型']} 的取值仅大小写不同")

    def test_statistical_category_stripped(self):
        """
        品名末尾的 (353)/(653) 是纺织品统计类别号，不是条件的一部分。
        不剥掉的话同一条件会被算成多种取值——实测"含量阈值"报 6 种，
        去掉类别号后只有 3 个真条件。
        """
        self.assertEqual(criteria._narrow_key("containing 10 percent of down (353)"),
                         criteria._narrow_key("Containing 10 percent of down (653)"))
        got = self._ask("梭织涂层夹克")
        self.assertEqual(got["含量阈值"]["取值总数"], 3)
        for v in got["含量阈值"]["取值"]:
            self.assertNotRegex(v, r"\(\d{3}\)\s*$", "展示值仍带统计类别号")

    def test_values_unique_after_truncation(self):
        """展示会截断，截断后重复的项对用户就是重复项"""
        for q in ("梭织涂层夹克", "jacket", "wool coat"):
            rows = rate.search(self.db, q, limit=200, sort="relevance")
            for n in criteria.narrowing_questions(self.db, rows):
                shown = [v.strip().lower() for v in n["取值"]]
                self.assertEqual(len(shown), len(set(shown)),
                                 f"{q} 的 {n['类型']} 截断后出现重复展示值")

    def test_empty_rows(self):
        self.assertEqual(criteria.narrowing_questions(self.db, []), [])


class TestTieRatio(unittest.TestCase):
    """并列度：排序几乎没区分力这件事必须让用户知道"""

    @classmethod
    def setUpClass(cls):
        cls.db = core.load_db()

    def test_flat_query_high_ratio(self):
        rows = rate.search(self.db, "梭织涂层夹克", limit=200, sort="relevance")
        self.assertGreater(criteria.tie_ratio(rows), 0.5,
                           "该查询前 20 条大面积同分，应报高并列度")

    def test_single_result(self):
        self.assertEqual(criteria.tie_ratio([{"相关度": 1.0}]), 0.0)
        self.assertEqual(criteria.tie_ratio([]), 0.0)

    def test_all_distinct(self):
        rows = [{"相关度": float(i)} for i in range(10)]
        self.assertAlmostEqual(criteria.tie_ratio(rows), 0.1)

    def test_all_same(self):
        rows = [{"相关度": 5.0} for _ in range(10)]
        self.assertAlmostEqual(criteria.tie_ratio(rows), 1.0)


class TestDistinguishingText(unittest.TestCase):
    """
    分歧点：候选彼此独有的路径措辞。

    并列展示几个候选时，列它们的共同点毫无意义——'wool coat' 的 6201 与 6202
    判定条件完全一样（都是 Of wool + 梭织），真正的分界是男装 / 女装，
    这句话在归类路径里但不会被 extract() 抽成"条件"。
    """

    @classmethod
    def setUpClass(cls):
        cls.db = core.load_db()

    def test_mens_vs_womens(self):
        d = criteria.detect_dispute(
            self.db, rate.search(self.db, "wool coat", limit=60, sort="relevance"))
        by = {g["品目"]: " ".join(g["分歧点"]) for g in d["分组"]}
        self.assertIn("6201", by)
        self.assertIn("6202", by)
        self.assertIn("Men's or boys'", by["6201"])
        self.assertIn("Women's or girls'", by["6202"])

    def test_storage_vs_primary_battery(self):
        d = criteria.detect_dispute(
            self.db, rate.search(self.db, "锂电池", limit=60, sort="relevance"))
        by = {g["品目"]: " ".join(g["分歧点"]) for g in d["分组"]}
        self.assertIn("storage batteries", by.get("8507", ""))
        self.assertIn("Primary cells", by.get("8506", ""))

    def test_shared_segments_excluded(self):
        """共同的路径层级不算分歧点"""
        d = criteria.detect_dispute(
            self.db, rate.search(self.db, "wool coat", limit=60, sort="relevance"))
        allsegs = [s for g in d["分组"] for s in g["分歧点"]]
        self.assertEqual(len(allsegs), len(set(allsegs)),
                         "分歧点在候选间出现重复，说明没有真正排除共同层级")

    def test_capped_at_two(self):
        d = criteria.detect_dispute(
            self.db, rate.search(self.db, "梭织涂层夹克", limit=60, sort="relevance"))
        for g in d["分组"]:
            self.assertLessEqual(len(g["分歧点"]), 2)


class TestTaxReversal(unittest.TestCase):
    """基础税率最低 ≠ 总税负最低——本工具最容易误导人的地方"""

    @classmethod
    def setUpClass(cls):
        cls.db = core.load_db()

    def test_real_reversal_detected(self):
        """
        真实数据：8506.50.00 基础 2.7% 低于 8507.60.00 的 3.4%，
        但 301 档位不同，总税负 40.2% 反而高于 28.4%。
        """
        rows = rate.search(self.db, "锂电池", limit=20, sort="relevance")
        d = criteria.detect_dispute(self.db, rows)
        self.assertTrue(d["有分歧"], "锂电池应命中 8507/8506 两个品目")
        self.assertTrue(d["税负反转"], "该组存在反转，必须提示")
        self.assertIn("8506.50.00", d["税负反转"])
        self.assertIn("8507.60.00", d["税负反转"])

    def test_no_reversal_when_order_agrees(self):
        rows = [row("8507.60.00", 3.4), row("8507.60.00", 3.4)]
        d = criteria.detect_dispute(self.db, rows)
        self.assertEqual(d["税负反转"], "")

    def test_reversal_only_among_fully_comparable(self):
        """
        基础最小值和总税负最小值必须取自同一批候选。
        若一个候选是从量税（总税负算不出），拿它的基础税率去参与比较、
        再和另一批的总税负最小值对照，结论没有意义。
        """
        rows = rate.search(self.db, "wool coat", limit=60, sort="relevance")
        d = criteria.detect_dispute(self.db, rows)
        self.assertTrue(d["无法比较"], "前置条件：该查询应含折算不出的候选")
        for code in d["无法比较"]:
            self.assertNotIn(code, d["税负反转"] or "",
                             "无法折算的候选不应出现在反转结论里")

    def test_base_spread(self):
        d = criteria.detect_dispute(self.db, [row("8507.60.00", 3.4), row("8506.50.00", 2.7)])
        self.assertAlmostEqual(d["基础税差"], 0.7, places=2)

    def test_origin_applies_to_totals(self):
        """
        回归：分歧面板的总税负原先写死中国口径，而它和下方结果表是并排看的。
        选越南时，表里的行不含 301、面板里的却含——同一个编码两个数字。
        """
        rows = [row("8507.60.00", 3.4), row("8506.50.00", 2.7)]
        cn = criteria.detect_dispute(self.db, rows, origin="CN")
        vn = criteria.detect_dispute(self.db, rows, origin="VN")
        for c, v in zip(cn["分组"], vn["分组"]):
            self.assertEqual(v["总税负估算"],
                             rate.calc_total(self.db, v["编码"], origin="VN")["总税负估算"])
            self.assertNotEqual(c["总税负数值"], v["总税负数值"],
                                f"{v['编码']}：中越口径应当不同，否则用例测不出问题")

    def test_unit_value_makes_specific_duty_comparable(self):
        """给了单位货值，原先"无法比较"的复合税候选就该参与税差比较"""
        rows = rate.search(self.db, "wool coat", limit=60, sort="relevance")
        without = criteria.detect_dispute(self.db, rows)
        with_uv = criteria.detect_dispute(self.db, rows, unit_value=50.0)
        self.assertTrue(without["无法比较"], "前置条件：该查询应含折算不出的候选")
        self.assertLess(len(with_uv["无法比较"]), len(without["无法比较"]),
                        "填了单位货值后，复合税候选应能折算成百分比")


class TestUncomparable(unittest.TestCase):
    """从量税/复合税折算不出百分比时必须说出来"""

    @classmethod
    def setUpClass(cls):
        cls.db = core.load_db()

    def test_specific_duty_reported(self):
        # 'wool coat' 命中 6201.20.11 / 6202.20.11，两者都是 "¢/kg + %" 的复合税，
        # 不给单位货值折算不出百分比
        rows = rate.search(self.db, "wool coat", limit=60, sort="relevance")
        d = criteria.detect_dispute(self.db, rows)
        self.assertTrue(d["无法比较"], "wool coat 应含复合税候选")
        self.assertIn("无法折算", d["提示"])
        for code in d["无法比较"]:
            g = [x for x in d["分组"] if x["编码"] == code][0]
            self.assertIsNone(g["总税负数值"])
            self.assertNotIn(code, d["税负反转"] or "",
                             "折算不出的候选不能出现在反转结论里")


class TestPctNum(unittest.TestCase):

    def test_parsing(self):
        self.assertEqual(criteria._pct_num("34.9%（含301/FLIP301/附加税估算）"), 34.9)
        self.assertEqual(criteria._pct_num("Free"), 0.0)
        self.assertEqual(criteria._pct_num("免税"), 0.0)
        self.assertEqual(criteria._pct_num("0%"), 0.0)
        self.assertIsNone(criteria._pct_num("需人工核算（复合/从量税）"), )
        self.assertIsNone(criteria._pct_num(""))
        self.assertIsNone(criteria._pct_num(None))

    def test_fmt8(self):
        self.assertEqual(criteria._fmt8("62014035"), "6201.40.35")
        self.assertEqual(criteria._fmt8("6201.40.35"), "6201.40.35")
        self.assertEqual(criteria._fmt8("8507"), "8507")


if __name__ == "__main__":
    unittest.main()
