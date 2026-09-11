# -*- coding: utf-8 -*-
"""
test_rate.py —— 税率引擎单元测试（unittest，无外部依赖）

运行：python -m unittest tests.test_rate -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import core
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
        # 无单位货值时无法折算从量部分 → 必须返回 None。
        # 若只返回从价部分 14.9%，调用方无从分辨这个数字是否完整，
        # 而 46.3¢/kg 按 $2/kg 折算就有 23.15 个百分点被凭空抹掉（见上一条用例）。
        self.assertIsNone(rate.estimate_ad_valorem("46.3¢/kg + 14.9%"))

    def test_specific_zero_or_negative_unit_value(self):
        # 0 是 falsy、负数无意义：都不能当作"已折算"，否则会静默退化成丢弃从量部分
        self.assertIsNone(rate.estimate_ad_valorem("46.3¢/kg + 14.9%", unit_value=0))
        self.assertIsNone(rate.estimate_ad_valorem("46.3¢/kg + 14.9%", unit_value=-5))

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


class TestEstimateLines(unittest.TestCase):
    """逐行估算：每行自带单价与数量，算出实际税费"""

    @classmethod
    def setUpClass(cls):
        cls.db = core.load_db()

    def test_units_of(self):
        # 单位写在 10 位统计行上，8 位要能继承到
        self.assertIn("No.", rate.units_of(self.db, "01051100"))   # 活鸡按只
        self.assertIn("kg", rate.units_of(self.db, "85076000"))    # 锂电池按公斤
        # 带点的写法与 10 位写法都要认
        self.assertEqual(rate.units_of(self.db, "0105.11.00"),
                         rate.units_of(self.db, "01051100"))

    def test_percent_row_duty_matches_value_times_rate(self):
        """纯从价行：税费 = 货值 × 总税负%"""
        out = rate.estimate_lines(
            self.db, [{"code": "8507.60.00", "qty": 100, "unit_value": 20}], origin="CN")
        r = out["rows"][0]
        self.assertEqual(r["货值"], 2000.0)
        self.assertAlmostEqual(r["预估税费"], round(2000.0 * r["总税负数值"] / 100, 2), places=2)
        self.assertEqual(out["summary"]["需人工行数"], 0)

    def test_specific_row_cross_check_against_duty_per_unit(self):
        """
        从量税行的交叉验算：0105.11.00 是 0.9¢/只。

        先用等效从价算出税费，再直接用"每只税额 × 只数"算一遍——两者必须对得上。
        只验"货值 × 百分比"等于自己算的百分比是循环论证，证明不了折算本身没错。
        """
        qty, uv = 500, 2.0
        out = rate.estimate_lines(
            self.db, [{"code": "01051100", "qty": qty, "unit_value": uv}], origin="CN")
        r = out["rows"][0]
        self.assertEqual(r["税率类型"], "从量")
        p = rate.parse_rate(r["一般税率"])
        base_duty_per_unit = sum(x["usd"] for x in p["specific"])   # 0.009 美元/只
        base_duty = base_duty_per_unit * qty
        # 预估税费里还含 301/FLIP 加征，减掉后应等于按只数直接算的基础从量税
        add_pct = r["301加征数值"] + r["FLIP 301加征数值"]
        add_duty = r["货值"] * add_pct / 100.0
        self.assertAlmostEqual(r["预估税费"] - add_duty, base_duty, places=2)

    def test_specific_without_unit_value_is_pending_not_zero(self):
        """从量税缺单价时不能当 0 混进合计——要在合计说明里点名"""
        out = rate.estimate_lines(
            self.db, [{"code": "01051100", "qty": 500}], origin="CN")
        r = out["rows"][0]
        self.assertIsNone(r["预估税费"])
        self.assertIsNone(r["货值"])
        s = out["summary"]
        self.assertEqual(s["税费合计"], 0)
        # 关键：这一行必须被点名，否则合计会显示成一个看着完整的数字
        self.assertEqual(s["未填行数"], 1)
        self.assertIn("未填", s["说明"])

    def test_every_row_is_accounted_for(self):
        """任何一行都必须落进 计价/需人工/未填 三者之一，不能凭空消失"""
        out = rate.estimate_lines(self.db, [
            {"code": "8507.60.00", "qty": 10, "unit_value": 20},   # 可计价
            {"code": "01051100", "qty": 500},                      # 缺单价
            {"code": "6204.69.45"},                                # 什么都没填
        ], origin="CN")
        s = out["summary"]
        self.assertEqual(s["行数"], 3)
        self.assertEqual(s["计价行数"] + s["需人工行数"] + s["未填行数"], s["行数"])

    def test_per_row_unit_value_is_independent(self):
        """
        同一批里两行用各自的单价折算，互不影响——这正是全局单价做不到的事。
        活鸡按只 2 美元 与 按只 0.5 美元，等效从价必须不同。
        """
        out = rate.estimate_lines(self.db, [
            {"code": "01051100", "qty": 10, "unit_value": 2.0},
            {"code": "01051100", "qty": 10, "unit_value": 0.5},
        ], origin="CN")
        a, b = out["rows"]
        self.assertNotEqual(a["总税负数值"], b["总税负数值"])
        self.assertGreater(b["总税负数值"], a["总税负数值"])   # 单价越低，等效从价越高

    def test_summary_excludes_pending_rows(self):
        """合计只累加算得出的行，并报出漏了几行"""
        out = rate.estimate_lines(self.db, [
            {"code": "8507.60.00", "qty": 10, "unit_value": 20},
            {"code": "0101.21.00", "qty": 1, "unit_value": 5000},
        ], origin="CN")
        s = out["summary"]
        self.assertEqual(s["行数"], 2)
        self.assertEqual(s["计价行数"] + s["需人工行数"], 2)
        if s["需人工行数"]:
            self.assertIn("需人工", s["说明"])

    def test_blank_and_zero_inputs_are_ignored(self):
        """空串 / 0 / 负数一律当没填，不能被 float('') 炸掉或当成有效单价"""
        out = rate.estimate_lines(self.db, [
            {"code": "8507.60.00", "qty": "", "unit_value": 0},
            {"code": "8507.60.00", "qty": -5, "unit_value": "abc"},
        ], origin="CN")
        for r in out["rows"]:
            self.assertIsNone(r["数量"])
            self.assertIsNone(r["单位货值"])
            self.assertIsNone(r["货值"])

    def test_plain_string_items_accepted(self):
        """只给编码字符串也要能用——粘贴一列编码是最常见的起手式"""
        out = rate.estimate_lines(self.db, ["8507.60.00", "6204.69.45"], origin="CN")
        self.assertEqual(len(out["rows"]), 2)
        self.assertEqual(out["summary"]["计价行数"], 0)


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


class TestClassificationPath(unittest.TestCase):
    """归类路径：判定条件（材质/织法/含量阈值）写在祖先品名上，必须还原出来"""

    @classmethod
    def setUpClass(cls):
        import core
        cls.db = core.load_db()

    def test_path_carries_material_criterion(self):
        # 6201.40.35 自身品名只有 'Padded sleeveless jackets'，看不出是化纤制；
        # 'Of man-made fibers' 在父节点上——归类争议里的材质分水岭
        path = rate.path_of(self.db, "62014035")
        self.assertTrue(any("man-made fibers" in p.lower() for p in path))

    def test_sibling_differs_only_by_threshold(self):
        # 同父节点下 6201.40.40 靠"羊毛≥36%"与 6201.40.35 分开，税率天差地别
        import core
        a = core.query_one(self.db, "62014035")
        b = core.query_one(self.db, "62014040")
        self.assertIn("36 percent or more by weight of wool", b["商品描述"])
        self.assertNotEqual(a["一般税率"], b["一般税率"])

    def test_full_desc_joins_path(self):
        full = rate.full_desc(self.db, "63079098")
        self.assertIn("Other made up articles", full)   # 祖先
        self.assertTrue(full.endswith("Other"))         # 自身

    def test_query_exposes_path(self):
        import core
        r = core.query_one(self.db, "62014035")
        self.assertIn("归类路径", r)
        self.assertTrue(r["归类路径"])


class TestSearchRecall(unittest.TestCase):
    """搜索召回：短词词干、祖先词、章节过滤"""

    @classmethod
    def setUpClass(cls):
        import core
        cls.db = core.load_db()

    def setUp(self):
        rate._clear_index_cache()

    def tearDown(self):
        rate._clear_index_cache()

    def test_stem_plurals(self):
        # 4 字母短词此前完全匹配不上复数形式（prefix5 要求词长≥5）
        self.assertEqual(rate._stem("coats"), "coat")
        self.assertEqual(rate._stem("gloves"), "glove")
        self.assertEqual(rate._stem("batteries"), "battery")
        self.assertEqual(rate._stem("cells"), "cell")
        # 不该被误伤的
        self.assertEqual(rate._stem("glass"), "glass")
        self.assertEqual(rate._stem("status"), "status")
        self.assertEqual(rate._stem("this"), "this")

    def test_short_word_query_not_empty(self):
        # 'wool coat' 改前返回 0 条
        self.assertTrue(rate.search(self.db, "wool coat", limit=5))

    def test_ancestor_only_term_is_findable(self):
        # 'dress patterns' 只出现在祖先品名里，改前返回 0 条
        rows = rate.search(self.db, "dress patterns", limit=5)
        self.assertTrue(rows)
        self.assertTrue(all(r["编码"].startswith("6307") for r in rows))

    def test_special_chapters_excluded_by_default(self):
        # 98/99 章不是可归类的进口编码，默认不得出现在结果里
        for kw in ("wool coat", "gloves", "lithium battery"):
            rows = rate.search(self.db, kw, limit=20)
            self.assertFalse([r for r in rows if r["编码"][:2] in ("98", "99")],
                             f"'{kw}' 结果混入 98/99 章")

    def test_and_hits_only_special_still_degrades_to_or(self):
        # 'wool coat woven' 的 AND 交集只有 1 条 99 章记录。若 98/99 过滤放在
        # AND/OR 分支判断之后，会走 AND 分支 → 过滤后清空 → 整个查询返回 0 条。
        rows = rate.search(self.db, "wool coat woven", limit=5)
        self.assertTrue(rows, "AND 交集只剩 98/99 时应降级到 OR，而不是返回空")
        self.assertFalse([r for r in rows if r["编码"][:2] in ("98", "99")])

    def test_special_chapters_reachable_when_asked(self):
        # 显式按编码查 99 章仍要能查到
        rows = rate.search(self.db, "9903.88", limit=5, sort="code_asc")
        self.assertTrue(rows)
        self.assertTrue(all(r["编码"].startswith("9903") for r in rows))
        # 或显式打开开关
        self.assertTrue(rate.search(self.db, "gloves", limit=20, include_special=True))

    def test_or_degrade_counts_concepts_not_tokens(self):
        """
        回归：AI 把"锂电池"改写成 'lithium battery accumulator'，8549 废电池
        10 条 + 8601 蓄电池机车 2 条全靠 battery+accumulator 凑满"至少 2 词"
        混进补召回——而这俩是同一概念的并列写法（税则固定搭配 "primary cells,
        primary batteries and electric accumulators"），不能互相凑数。
        """
        rows = rate.search(self.db, "lithium battery accumulator", limit=40)
        self.assertTrue(rows, "真命中（8507/8506）必须还在")
        noisy = [r["编码"] for r in rows if r["编码"][:4] in ("8549", "8601")]
        self.assertEqual(noisy, [], f"同概念词不该凑满 min_hits：{noisy}")
        codes4 = {r["编码"][:4] for r in rows}
        self.assertIn("8507", codes4)
        self.assertIn("8506", codes4)

    def test_single_concept_query_not_starved(self):
        """battery accumulator 两词同概念 → min_hits 须降为 1，不能返回空。
        用户真就只说了'电池/蓄电池'时，8549/8601 恰恰是合理结果。"""
        rows = rate.search(self.db, "battery accumulator", limit=10)
        self.assertTrue(rows)

    def test_two_real_concepts_unaffected(self):
        """两个真概念的 OR 降级照旧：概念归组不能把正确候选挡在 min_hits 外"""
        rows = rate.search(self.db, "woven jacket coated", limit=10)
        self.assertTrue(rows)

    def test_concept_of_stems_before_grouping(self):
        self.assertEqual(rate._concept_of("batteries"), rate._concept_of("accumulator"))
        self.assertEqual(rate._concept_of("cells"), rate._concept_of("battery"))
        self.assertNotEqual(rate._concept_of("lithium"), rate._concept_of("battery"))

    def test_phrase_bonus_ranks_term_match_first(self):
        # 'man-made fibers anorak' 改前首位是塑料地板砖（自身品名含 man-made fibers）
        rows = rate.search(self.db, "man-made fibers anorak", limit=5, sort="relevance")
        self.assertTrue(rows)
        self.assertTrue(rows[0]["编码"].startswith(("61", "62")),
                        f"首位应是服装章，实际 {rows[0]['编码']}")


class TestTotalCostSort(unittest.TestCase):
    """
    按总税负排序。

    默认排序此前是 tax_asc（基础等效从价），配的文案是"找税率最低的编码"，
    但基础税率最低 ≠ 总税负最低：8215.99.30 基础 14% 却不在 301 清单，
    总税负 26.5%；8215.99.35 基础 6.8% 但 +7.5% 301 +12.5% FLIP，总 26.8%。
    按基础税率挑"最便宜"会挑错，且用户看不出来。
    """

    @classmethod
    def setUpClass(cls):
        cls.db = core.load_db()

    def _codes(self, sort, q="不锈钢餐具", **kw):
        return [r["编码"] for r in rate.search(self.db, q, limit=20, sort=sort, **kw)]

    def test_default_is_relevance(self):
        """默认不再按基础税率排——那个顺序会误导人"""
        self.assertEqual(rate.search(self.db, "battery", limit=5),
                         rate.search(self.db, "battery", limit=5, sort="relevance"))

    def test_reversal_case(self):
        """本工具最容易误导人的一组：基础税率顺序与总税负顺序相反"""
        base = self._codes("tax_asc")
        total = self._codes("total_asc")
        self.assertLess(base.index("8215.99.35"), base.index("8215.99.30"),
                        "前置条件：基础税率下 .35 应排在 .30 前")
        self.assertLess(total.index("8215.99.30"), total.index("8215.99.35"),
                        "总税负下 .30（26.5%）应排在 .35（26.8%）前")

    def test_total_ascending(self):
        rows = rate.search(self.db, "不锈钢餐具", limit=20, sort="total_asc")
        vals = [r["总税负数值"] for r in rows if r["总税负数值"] is not None]
        self.assertEqual(vals, sorted(vals))

    def test_uncomputable_last(self):
        """从量税折算不出百分比的排最后，与 tax_asc 处理一致"""
        rows = rate.search(self.db, "不锈钢餐具", limit=20, sort="total_asc")
        seen_none = False
        for r in rows:
            if r["总税负数值"] is None:
                seen_none = True
            else:
                self.assertFalse(seen_none, "可折算的行出现在不可折算的行之后")

    def test_unit_value_makes_specific_comparable(self):
        """给了单位货值，复合税就能折算，不该再垫底"""
        rows = rate.search(self.db, "不锈钢餐具", limit=20,
                           sort="total_asc", unit_value=2.0)
        self.assertTrue(any(r["编码"] == "8215.99.01" and r["总税负数值"] is not None
                            for r in rows), "给了单位货值后复合税仍未折算")

    def test_origin_affects_order(self):
        """越南原产不加 301，顺序应与中国不同"""
        cn = [r["总税负数值"] for r in rate.search(
            self.db, "不锈钢餐具", limit=20, sort="total_asc", origin="CN")
            if r["总税负数值"] is not None]
        vn = [r["总税负数值"] for r in rate.search(
            self.db, "不锈钢餐具", limit=20, sort="total_asc", origin="VN")
            if r["总税负数值"] is not None]
        self.assertTrue(vn and cn)
        self.assertLess(sum(vn), sum(cn), "越南总税负应低于中国（无 301）")

    def test_total_num_parsing(self):
        self.assertEqual(rate._total_num("26.5%（含301/FLIP301/附加税估算）"), 26.5)
        self.assertEqual(rate._total_num("0%"), 0.0)
        self.assertIsNone(rate._total_num("需人工（无法解析）"))
        self.assertIsNone(rate._total_num(""))
        self.assertIsNone(rate._total_num(None))

    def test_deterministic(self):
        self.assertEqual(self._codes("total_asc"), self._codes("total_asc"))


class TestWeaveChapterBias(unittest.TestCase):
    """
    织法 → 章的偏置。

    61 章 heading 字面写着 "knitted or crocheted"，62 章的 heading 完全不提
    织法（"梭织"是靠"不在 61 章"反向定义的，两章里出现 'woven' 的行只有 4 条）。
    结果是不对称的错误：搜"针织夹克"正确，搜"梭织夹克"前 20 条里 14 条却是
    针织的 61 章成衣。61/62 税率不同，归错章就是归错码。
    """

    @classmethod
    def setUpClass(cls):
        cls.db = core.load_db()

    def _chapters(self, q, n=20):
        return [r["章"] for r in rate.search(self.db, q, limit=60, sort="relevance")[:n]]

    def test_woven_query_excludes_knit_chapter(self):
        chs = self._chapters("梭织夹克")
        self.assertTrue(chs)
        self.assertNotIn("61", chs, "查梭织不该返回 61 章（按定义就是针织）")

    def test_knit_query_prefers_61(self):
        chs = self._chapters("针织夹克")
        self.assertTrue(chs)
        self.assertNotIn("62", chs, "查针织不该返回 62 章（按定义就是非针织）")

    def test_woven_coated_jacket(self):
        chs = self._chapters("梭织涂层夹克")
        self.assertNotIn("61", chs)

    def test_contradictory_intent_cancels(self):
        """用户自己都没说清织法时，不该由检索替他决定"""
        b = rate._weave_bias(["woven", "knitted"])
        self.assertEqual(b.get("61", 0), 0.0)
        self.assertEqual(b.get("62", 0), 0.0)

    def test_synonym_pair_counted_once(self):
        """knitted / crocheted 是官方固定搭配的两半，展开后不能把偏置翻倍"""
        one = rate._weave_bias(["knitted"])
        both = rate._weave_bias(["knitted", "crocheted"])
        self.assertEqual(one, both)

    def test_no_bias_without_weave_terms(self):
        self.assertEqual(rate._weave_bias(["battery", "lithium"]), {})

    def test_fabric_chapters_unaffected(self):
        """'woven' 在 50-58 章面料里是正常词，偏置只针对 61/62 成衣章"""
        b = rate._weave_bias(["woven"])
        self.assertEqual(set(b), {"61", "62"})


class TestSearchDeterminism(unittest.TestCase):
    """
    同一查询必须每次返回同一批结果。

    候选集合来自 set，迭代顺序受 Python 字符串哈希随机化影响；排序原先只按
    相关度/税率，同分项因此保留了随机的输入顺序。实测 '梭织涂层夹克' 相关度
    6.45 那一档，五个进程跑出五组不同编码，limit 截断又把顺序抖动放大成
    "结果里到底有没有这条"。报关工具的结果不可复现是硬伤，故每种排序都以
    编码作次级键。
    """

    @classmethod
    def setUpClass(cls):
        cls.db = core.load_db()

    QUERIES = ["梭织涂层夹克", "jacket", "battery", "cotton shirt"]
    SORTS = ["relevance", "tax_asc", "tax_desc", "code_asc"]

    def test_repeated_calls_identical(self):
        for q in self.QUERIES:
            for s in self.SORTS:
                a = [r["编码"] for r in rate.search(self.db, q, limit=60, sort=s)]
                b = [r["编码"] for r in rate.search(self.db, q, limit=60, sort=s)]
                self.assertEqual(a, b, f"{q}/{s} 两次调用结果不一致")

    def test_ties_broken_by_code(self):
        """同分项必须按编码升序，这样才与进程无关"""
        rows = rate.search(self.db, "梭织涂层夹克", limit=100, sort="relevance")
        self.assertGreater(len(rows), 10)
        for prev, cur in zip(rows, rows[1:]):
            if prev["相关度"] == cur["相关度"]:
                self.assertLess(prev["编码"], cur["编码"],
                                f"同分未按编码排序：{prev['编码']} 在 {cur['编码']} 之前")

    def test_tax_sort_ties_broken_by_code(self):
        rows = rate.search(self.db, "jacket", limit=100, sort="tax_asc")
        for prev, cur in zip(rows, rows[1:]):
            if prev["等效从价数值"] == cur["等效从价数值"] is not None:
                self.assertLess(prev["编码"], cur["编码"])

    def test_subprocess_matches(self):
        """
        跨进程验证：哈希种子不同的独立解释器必须给出同一结果。
        同进程内重复调用发现不了这个问题——种子在进程内是固定的。
        """
        import json
        import subprocess

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        code = (
            "import sys; sys.path.insert(0, r'%s')\n"
            "import core, rate, json\n"
            "db = core.load_db()\n"
            "print(json.dumps([r['编码'] for r in "
            "rate.search(db, '梭织涂层夹克', limit=60, sort='relevance')]))\n"
        ) % os.path.join(root, "scripts")

        outs = []
        for seed in ("0", "1", "12345"):
            env = dict(os.environ, PYTHONHASHSEED=seed)
            p = subprocess.run([sys.executable, "-c", code], capture_output=True,
                               text=True, env=env, cwd=root)
            self.assertEqual(p.returncode, 0, p.stderr[-500:])
            outs.append(json.loads(p.stdout.strip().splitlines()[-1]))
        self.assertEqual(outs[0], outs[1], "不同哈希种子下结果不一致")
        self.assertEqual(outs[1], outs[2], "不同哈希种子下结果不一致")


class TestCalcTotalCodeNormalization(unittest.TestCase):
    """
    calc_total 接受带点编码。

    '8215.99.30' 是本工具在界面、导出、API 响应里到处显示的形式，调用方原样传
    回来是最自然的用法。此前会被当成无法解析的编码，静默返回"需折算 /
    需人工（无法解析）"——不是报错，是一个看起来像合理限制的错误答案
    （该子目实际是 14% 纯从价）。/api/estimate 的 codes 列表路径就踩了这个坑；
    text 路径经 extract_codes 清洗过，所以 Web 界面看不出来。
    """

    @classmethod
    def setUpClass(cls):
        cls.db = core.load_db()

    def test_dotted_equals_plain_8(self):
        a = rate.calc_total(self.db, "82159930")
        b = rate.calc_total(self.db, "8215.99.30")
        self.assertEqual(a["基础等效从价"], b["基础等效从价"])
        self.assertEqual(a["总税负估算"], b["总税负估算"])
        self.assertEqual(b["基础等效从价"], "14%")

    def test_dotted_equals_plain_10(self):
        a = rate.calc_total(self.db, "8507600000")
        b = rate.calc_total(self.db, "8507.60.00.00")
        self.assertEqual(a["总税负估算"], b["总税负估算"])
        self.assertNotIn("无法解析", b["总税负估算"])

    def test_no_double_formatting(self):
        r = rate.calc_total(self.db, "8215.99.30")
        self.assertEqual(r["输入编码"], "8215.99.30")
        self.assertNotIn("..", r["输入编码"])

    def test_search_row_code_roundtrips(self):
        """搜索结果里的编码直接喂回 calc_total 必须能算出税"""
        rows = rate.search(self.db, "battery", limit=5, sort="relevance")
        self.assertTrue(rows)
        for r in rows:
            t = rate.calc_total(self.db, r["编码"])
            self.assertEqual(t["输入编码"], r["编码"])

    def test_empty_and_junk(self):
        for bad in ("", None, "abc", "INV-2026"):
            r = rate.calc_total(self.db, bad)
            self.assertIn("总税负估算", r)


if __name__ == "__main__":
    unittest.main()


class TestRefuseToGuess(unittest.TestCase):
    """
    「宁可说不知道」的七处判断，逐条钉死。

    这套代码最值钱的不是它算了什么，而是它在哪些地方选择不算。那些分支目前
    主要靠注释维系——注释拦不住下一个想图省事的人。每条断言对应判定链上的
    一步，任何把它改成兜底的改动都会在这里失败。
    """

    @classmethod
    def setUpClass(cls):
        cls.db = core.load_db()

    def test_step1_six_digit_refuses_301(self):
        """① 6 位品目不拿前 8 位去凑——品目层面没有加征档位"""
        r = core.query_one(self.db, "620469", origin="CN")
        self.assertEqual(r["301判定"], "无法判定")
        self.assertIn("6位", r["备注"])

    def test_step2_compound_rate_refuses_half_answer(self):
        """
        ② 复合税不返回从价那一半。

        '$1.104/kg + 14.9%' 若只返回 14.9，调用方无从分辨这个数字是否完整——
        按 $4/kg 折算实际是 42.5%，差了近三倍。
        """
        self.assertIsNone(rate.estimate_ad_valorem("$1.104/kg + 14.9%", None))
        # 给了单位货值就应该算得出，否则是另一个方向的错误
        self.assertIsNotNone(rate.estimate_ad_valorem("$1.104/kg + 14.9%", 4.0))

    def test_step3_partial_suffix_prefix_refuses_8digit(self):
        """
        ③ 只有部分 10 位后缀入 301 清单的前缀，给 8 位一律判无法判定。

        同一前缀下不同后缀档位可能不同（6307.90.98 下 …42 是 +50%，其余 +7.5%），
        任何 8 位层面的归纳都会算错。
        """
        partial = self.db.get("sec301_partial_8") or {}
        self.assertTrue(partial, "数据库应含 partial8 分区")
        code8 = sorted(partial)[0]
        c99, note = core._sec301_lookup(self.db, code8, code8)
        self.assertIsNone(c99)
        self.assertIn("10 位", note)
        # 补全 10 位后必须能给出结论（否则就成了永远查不了）
        code10 = partial[code8][0]
        self.assertIsNotNone(core._sec301_lookup(self.db, code10, code8)[0])

    def test_step4_described_exclusion_never_auto_exempts(self):
        """
        ④ 按描述授予的排除绝不自动判免。

        同一税号下有的款符合描述、有的不符合，编码本身回答不了；
        自动判免会直接造出错误申报。
        """
        ex = (self.db.get("exclusions") or {}).get("by_code") or {}
        described = [c for c, recs in ex.items()
                     if all(r.get("covers") != "full" for r in recs)]
        self.assertTrue(described, "应存在仅按描述排除的编码")
        for code in described[:5]:
            auto, items = core.exclusion_lookup(self.db, code, code[:8])
            self.assertIsNone(auto, f"{code} 仅按描述排除，不得自动判免")
            self.assertTrue(items, f"{code} 仍应列出条目供人工核对")

    def test_step4b_exclusion_status_recomputed_on_query_day(self):
        """④b 有效期按查询当天重算，不能用提取那天的状态"""
        note = {"effective_from": "2024-01-01", "effective_to": "2026-11-09"}
        self.assertEqual(core._excl_status(note, "2026-09-11"), "生效中")
        self.assertEqual(core._excl_status(note, "2026-11-10"), "已过期")
        self.assertEqual(core._excl_status(note, "2023-12-31"), "未生效")
        # 没标日期的老标目一律不判免
        self.assertEqual(core._excl_status({}, "2026-09-11"), "有效期未标注")

    def test_step5_scoped_annex_ii_is_conditional_not_exempt(self):
        """
        ⑤ 带范围限制的 ANNEX II 豁免必须是 conditional，不能是 exempt。

        1257/2113 条通用豁免带 Scope Limitations（限 Aircraft/Pharma 等用途），
        判成 exempt 会让一支普通工业温度计被算成免征——少收要被 CBP 追补加罚。
        """
        ex = self.db.get("flip301_exemptions") or {}
        scopes = ex.get("universal_scopes") or {}
        scoped = [c for c, s in scopes.items() if s]
        self.assertTrue(scoped, "应存在带范围限制的豁免编码")
        for code8 in scoped[:5]:
            _txt, _note, _src, spec = core.flip301_judge(self.db, "CN", code8)
            self.assertEqual(spec["mode"], "conditional",
                             f"{code8} 带范围限制 {scopes[code8]}，不得判为无条件豁免")
            self.assertIn("fallback", spec)
        # 无范围限制的才是真豁免
        plain = [c for c in (ex.get("universal") or []) if not scopes.get(c)]
        if plain:
            _t, _n, _s, spec = core.flip301_judge(self.db, "CN", plain[0])
            self.assertEqual(spec["mode"], "exempt")

    def test_step5b_tier_decided_before_annex_lookup(self):
        """
        ⑤b 非被调查经济体命中 ANNEX II 时答「不适用」而非「已豁免」。

        税额同样是 0，但把"不适用"说成"已豁免"，人工复核会去找一份
        并不存在的豁免依据。
        """
        ex = self.db.get("flip301_exemptions") or {}
        code8 = (ex.get("universal") or [None])[0]
        self.assertIsNotNone(code8)
        # 冰岛不在 60 个被调查经济体名单里（澳/新/南非/挪威都在，别拿它们当反例）
        txt, note, _src, spec = core.flip301_judge(self.db, "IS", code8)
        self.assertEqual(spec["mode"], "none")
        self.assertNotIn("豁免", txt)
        self.assertIn("不在", note)

    def test_step6_unpriced_specific_row_not_counted_as_zero(self):
        """⑥ 缺单价的从量税行不按 0 计入合计，必须单独点名"""
        out = rate.estimate_lines(self.db, [{"code": "01051100", "qty": 500}], origin="CN")
        self.assertEqual(out["summary"]["税费合计"], 0)
        self.assertEqual(out["summary"]["未填行数"], 1)
        self.assertIn("未填", out["summary"]["说明"])

    def test_step7_unresolvable_part_poisons_the_total(self):
        """
        ⑦ 任一分项折算不出来，总额就是「需人工」，不能把能算的加起来。

        分项的不确定性不允许被一个看着完整的汇总数字吞掉。
        """
        r = rate.calc_total(self.db, "01051100")      # 0.9¢ each，没给单位货值
        self.assertIn("需人工", r["总税负估算"])
        self.assertNotIn("%", r["总税负估算"].split("（")[0])
