# -*- coding: utf-8 -*-
"""
test_rate.py —— 税率引擎单元测试（unittest，无外部依赖）

运行：python -m unittest tests.test_rate -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import rate


class TestParseRate(unittest.TestCase):
    """税率解析"""

    def test_free(self):
        p = rate.parse_rate("Free")
        self.assertEqual(p["kind"], "free")
        self.assertEqual(p["ad_valorem"], 0.0)

    def test_percent(self):
        p = rate.parse_rate("6.5%")
        self.assertEqual(p["kind"], "percent")
        self.assertEqual(p["ad_valorem"], 6.5)

    def test_no_additional_duty(self):
        p = rate.parse_rate("No additional duty")
        self.assertEqual(p["kind"], "free")
        self.assertEqual(p["ad_valorem"], 0.0)

    def test_specific_cents(self):
        p = rate.parse_rate("1¢/kg")
        self.assertEqual(p["kind"], "specific")
        self.assertEqual(p["specific"][0]["usd"], 0.01)
        self.assertEqual(p["specific"][0]["unit"], "kg")

    def test_specific_dollar(self):
        p = rate.parse_rate("$1.646/kg")
        self.assertEqual(p["kind"], "specific")
        self.assertEqual(p["specific"][0]["usd"], 1.646)

    def test_specific_each(self):
        p = rate.parse_rate("0.9¢ each")
        self.assertEqual(p["kind"], "specific")
        self.assertEqual(p["specific"][0]["usd"], 0.009)
        self.assertEqual(p["specific"][0]["unit"], "each")

    def test_compound(self):
        p = rate.parse_rate("46.3¢/kg + 14.9%")
        self.assertEqual(p["kind"], "compound")
        self.assertEqual(p["ad_valorem"], 14.9)
        self.assertEqual(p["specific"][0]["usd"], 0.463)
        self.assertEqual(p["specific"][0]["unit"], "kg")

    def test_compound_dollar(self):
        p = rate.parse_rate("$1.104/kg + 14.9%")
        self.assertEqual(p["kind"], "compound")
        self.assertEqual(p["specific"][0]["usd"], 1.104)

    def test_reference(self):
        p = rate.parse_rate("The duty provided in the applicable subheading")
        self.assertEqual(p["kind"], "reference")
        self.assertIsNone(p["ad_valorem"])

    def test_complex_multipart(self):
        p = rate.parse_rate("13.5¢/kg + 6.3% + 1.9¢/article")
        self.assertEqual(p["kind"], "complex")

    def test_complex_component(self):
        p = rate.parse_rate("$1.61 each + 4.4% on the case and strap, band or bracelet")
        self.assertEqual(p["kind"], "complex")

    def test_strip_paren(self):
        # special 列带 FTA 国家标注
        p = rate.parse_rate("Free (A+,AU,BH,CL,CO,D,E,IL,JO,KR,MA,OM,P,PA,PE,S,SG)")
        self.assertEqual(p["kind"], "free")

    def test_empty(self):
        p = rate.parse_rate("")
        self.assertEqual(p["kind"], "unknown")

    def test_pair_unit(self):
        p = rate.parse_rate("76¢/pr. + 32%")
        self.assertEqual(p["kind"], "compound")
        self.assertEqual(p["specific"][0]["usd"], 0.76)
        self.assertEqual(p["specific"][0]["unit"], "pr")


class TestEstimateAdValorem(unittest.TestCase):
    """等效从价折算"""

    def test_free(self):
        self.assertEqual(rate.estimate_ad_valorem("Free"), 0.0)

    def test_percent(self):
        self.assertEqual(rate.estimate_ad_valorem("6.5%"), 6.5)

    def test_specific_requires_unit_value(self):
        self.assertIsNone(rate.estimate_ad_valorem("1¢/kg"))
        self.assertEqual(rate.estimate_ad_valorem("1¢/kg", unit_value=1.0), 1.0)
        self.assertEqual(rate.estimate_ad_valorem("1¢/kg", unit_value=0.5), 2.0)

    def test_compound_with_unit_value(self):
        # 46.3¢/kg + 14.9%，货值 $2/kg → 23.15% + 14.9% = 38.05%
        self.assertAlmostEqual(rate.estimate_ad_valorem("46.3¢/kg + 14.9%", unit_value=2.0), 38.05, places=4)

    def test_compound_without_unit_value(self):
        # 无单位货值时仅返回从价部分
        self.assertEqual(rate.estimate_ad_valorem("46.3¢/kg + 14.9%"), 14.9)

    def test_reference(self):
        self.assertIsNone(rate.estimate_ad_valorem("The duty provided in the applicable subheading"))

    def test_complex_without_unit(self):
        self.assertIsNone(rate.estimate_ad_valorem("$1.61 each + 4.4% on the case and strap"))


class TestCalcTotal(unittest.TestCase):
    """总税负计算（加载真实数据库）"""

    @classmethod
    def setUpClass(cls):
        import core
        cls.db = core.load_db()

    def test_pure_percent(self):
        # 8507.60.00（锂电池）通常命中 301
        r = rate.calc_total(self.db, "85076000")
        self.assertIn("总税负估算", r)
        self.assertIn("税率类型", r)
        self.assertGreaterEqual(r["301加征数值"], 0)

    def test_free_base(self):
        r = rate.calc_total(self.db, "01012100")  # 纯种繁殖动物 Free
        self.assertEqual(r["税率类型"], "免税")
        self.assertIn("0%", r["总税负估算"]) if r["301加征数值"] == 0 else None

    def test_specific_base(self):
        r = rate.calc_total(self.db, "01051100")  # 活鸡 0.9¢ each
        self.assertEqual(r["税率类型"], "从量")

    def test_with_unit_value(self):
        r = rate.calc_total(self.db, "01051100", unit_value=2.0)
        self.assertIn("%", r["总税负估算"])


class TestSearch(unittest.TestCase):
    """关键词搜索与排序"""

    @classmethod
    def setUpClass(cls):
        import core
        cls.db = core.load_db()

    def setUp(self):
        rate._clear_index_cache()

    def test_keyword_hit(self):
        rows = rate.search(self.db, "lithium ion battery", limit=20, sort="relevance")
        self.assertTrue(rows)
        # 相关度排序下，前几条应都包含关键词
        for r in rows[:5]:
            self.assertTrue("lithium" in r["商品描述"].lower() or "battery" in r["商品描述"].lower())
        self.assertEqual(rows[0]["编码"], "8507.60.00")

    def test_broad_keywords_or_semantics(self):
        # AI 归类链路常见场景：LLM 给出宽泛关键词（electric/storage 不在官方品名中），
        # 加权 OR 语义下精确词仍能召回 8507.60.00，且排在前面
        rows = rate.search(self.db, "lithium battery electric storage", limit=20, sort="relevance")
        self.assertTrue(rows)
        lithium_rows = [r for r in rows if "lithium" in r["商品描述"].lower()]
        self.assertTrue(lithium_rows)
        self.assertTrue(any(r["编码"] == "8507.60.00" for r in rows))
        # 精确词（lithium）命中的应排在仅命中宽泛词（electric/storage）的前面
        self.assertEqual(rows[0]["相关度"], max(r["相关度"] for r in rows))

    def test_code_match(self):
        rows = rate.search(self.db, "0101.21.00", limit=10)
        self.assertTrue(any(r["编码"] == "0101.21.00" for r in rows))

    def test_sort_tax_asc(self):
        rows = rate.search(self.db, "lithium", limit=30, sort="tax_asc")
        vals = [r["等效从价数值"] for r in rows if r["等效从价数值"] is not None]
        self.assertEqual(vals, sorted(vals))

    def test_sort_code_asc(self):
        rows = rate.search(self.db, "battery", limit=20, sort="code_asc")
        codes = [r["编码"] for r in rows]
        self.assertEqual(codes, sorted(codes))

    def test_free_sorted_first(self):
        rows = rate.search(self.db, "apparel", limit=50, sort="tax_asc")
        non_none = [r for r in rows if r["等效从价数值"] is not None]
        if non_none:
            self.assertEqual(non_none[0]["等效从价数值"], 0.0)

    def test_empty_keyword(self):
        self.assertEqual(rate.search(self.db, ""), [])


if __name__ == "__main__":
    unittest.main()
