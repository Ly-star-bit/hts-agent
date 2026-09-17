# -*- coding: utf-8 -*-
"""audit_company 的对账规则测试。用真实税则库（core.load_db）跑，规则改了必须先在这里过。"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import audit_company as ac
import core

_DB = None


def _db():
    global _DB
    if _DB is None:
        _DB = core.load_db()
    return _DB


def _rows(*recs):
    """recs: (code, duty, desc_en, texture, unit_value) → norm_rows 的输出"""
    raw = [{"HS CODE": c, "DUTY": d, "总加征": "", "DESCRIPTION OF GOODS": e,
            "中文品名": "x", "单价(USD)": v, "TEXTURE": t} for c, d, e, t, v in recs]
    return ac.norm_rows(raw, ac.field_map(raw))


def _kinds(f):
    return {x["类型"] for x in f}


class TestParsers(unittest.TestCase):
    def test_pct(self):
        self.assertEqual(ac.pct("8.3%"), 8.3)
        self.assertEqual(ac.pct("Free"), 0.0)
        self.assertEqual(ac.pct("免"), 0.0)
        self.assertIsNone(ac.pct(""))
        self.assertIsNone(ac.pct("37.1¢/kg"))          # 纯从量折不成百分比
        self.assertEqual(ac.pct("28.8¢/gross + 4.6%"), 4.6)   # 复合税取从价部分

    def test_is_compound(self):
        self.assertTrue(ac.is_compound("28.8¢/gross + 4.6%"))
        self.assertTrue(ac.is_compound("37.1¢/kg + 16.8%"))
        self.assertFalse(ac.is_compound("8.3%"))

    def test_parse_texture(self):
        self.assertEqual(ac.parse_texture("75% silk +25% polyester"), {"silk": 75.0, "mmf": 25.0})
        self.assertEqual(ac.parse_texture("80% Silk 20% polyester"), {"silk": 80.0, "mmf": 20.0})
        self.assertEqual(ac.parse_texture("55% flax fibers 45% Polyurethane"), {"veg": 55.0, "mmf": 45.0})
        self.assertEqual(ac.parse_texture("75% Cotton, 23% Polyester, 2% Spandex"),
                         {"cotton": 75.0, "mmf": 25.0})       # 涤 + 氨纶都并进化纤
        self.assertEqual(ac.parse_texture("Cashmere"), {})     # 无百分比：认不出，不瞎猜
        self.assertEqual(ac.parse_texture(""), {})

    def test_field_map_and_aliases(self):
        raw = [{"HS_CODE": "6108.19.1000", "中文品名": "衬裙", "材质": "80% Silk"}]
        m = ac.field_map(raw)
        self.assertEqual((m["code"], m["name_zh"], m["texture"]), ("HS_CODE", "中文品名", "材质"))
        self.assertEqual(ac.norm_rows(raw, m)[0]["code"], "6108191000")
        self.assertEqual(ac.field_map(raw, {"code": "自定义"})["code"], "自定义")

    def test_load_rows_json_and_csv(self):
        with tempfile.TemporaryDirectory() as d:
            j = os.path.join(d, "a.json")
            json.dump([{"HS CODE": "1"}], open(j, "w", encoding="utf-8"))
            self.assertEqual(ac.load_rows(j), [{"HS CODE": "1"}])
            c = os.path.join(d, "a.csv")
            open(c, "w", encoding="utf-8").write("HS CODE,DUTY\n6108.19.1000,1.1%\n")
            self.assertEqual(ac.load_rows(c)[0]["DUTY"], "1.1%")


class TestCodeMaterial(unittest.TestCase):
    def test_threshold_and_branch(self):
        need, thr, _ = ac.code_material(_db(), "61081910")      # Containing 70%+ silk
        self.assertEqual(thr, (70, "silk"))
        need, thr, _ = ac.code_material(_db(), "62043320")      # 合成纤维制 + 含亚麻 36%+
        self.assertIn("mmf", need)
        self.assertEqual(thr, (36, "veg"))
        need, thr, _ = ac.code_material(_db(), "61101210")      # Wholly of cashmere
        self.assertIn("cashmere", need)


class TestGoodsConflict(unittest.TestCase):
    def test_sweater_in_skirt_line(self):
        self.assertEqual(ac.goods_conflict("Women's sweater", "Skirts and divided skirts: > Of cotton"),
                         ("毛衣/背心", "裙子"))

    def test_no_conflict_when_text_lists_the_same_category(self):
        self.assertIsNone(ac.goods_conflict(
            "Men's Sweater", "Sweaters, pullovers, sweatshirts, waistcoats (vests) and similar articles"))

    def test_ambiguous_text_is_not_flagged(self):
        # 4 位品目条文是大杂烩，同时命中多类 → 不判（正是要用子目条文的原因）
        self.assertIsNone(ac.goods_conflict(
            "Women's sweater", "suits, ensembles, dresses, skirts, trousers, bib and brace overalls"))

    def test_word_boundary_false_positives(self):
        self.assertIsNone(ac.goods_conflict("Background bracket set", "Skirts and divided skirts"))
        self.assertIsNone(ac.goods_conflict("VIBRATION PLATE", "Skirts and divided skirts"))


class TestAudit(unittest.TestCase):
    def test_dead_statistical_suffix(self):
        f = ac.audit(_db(), _rows(("8507.60.0020", "3.4%", "Jump Starter", "Plastic", 3.85)))
        self.assertIn("统计后缀作废", _kinds(f))
        self.assertEqual([x for x in f if x["类型"] == "统计后缀作废"][0]["级别"], "错误")
        self.assertNotIn("统计后缀作废", _kinds(ac.audit(_db(), _rows(
            ("8507.60.0090", "3.4%", "Jump Starter", "Plastic", 3.85)))))

    def test_code_not_in_tariff(self):
        f = ac.audit(_db(), _rows(("9999.99.9999", "1%", "x", "", 1.0)))
        self.assertEqual(_kinds(f), {"编码不存在"})

    def test_duty_folded_surcharge_is_hint_not_error(self):
        # 8.3% + 7.5%(301) = 15.8%：口径混用，不是税率错
        f = ac.audit(_db(), _rows(("6104.52.0010", "15.8%", "Women's skirt", "100% Cotton", 5.0)))
        g = [x for x in f if x["类型"] == "税率口径混用"]
        self.assertTrue(g and g[0]["级别"] == "提示")
        self.assertNotIn("基础税率不符", _kinds(f))

    def test_real_rate_mismatch_is_error(self):
        f = ac.audit(_db(), _rows(("6104.52.0010", "3%", "Women's skirt", "100% Cotton", 5.0)))
        g = [x for x in f if x["类型"] == "基础税率不符"]
        self.assertTrue(g and g[0]["级别"] == "错误")

    def test_material_threshold_not_met(self):
        # 6108.19.10 要求含丝 70%+，申报 30% 丝
        f = ac.audit(_db(), _rows(("6108.19.1000", "1.1%", "Petticoats", "30% silk 70% polyester", 3.0)))
        g = [x for x in f if x["类型"] == "含量门槛不满足"]
        self.assertTrue(g and g[0]["级别"] == "错误")
        # 满足门槛就不报
        f2 = ac.audit(_db(), _rows(("6108.19.1000", "1.1%", "Petticoats", "80% silk 20% polyester", 3.0)))
        self.assertNotIn("含量门槛不满足", _kinds(f2))

    def test_material_branch_mismatch_even_when_threshold_passes(self):
        # 6204.33.20 = 合成纤维制 + 含亚麻 36%+。亚麻 55% 过了门槛，但化纤不占优
        f = ac.audit(_db(), _rows(("6204.33.2000", "2.8%", "Women's Blazers",
                                   "55% flax fibers 45% Polyurethane", 1.1)))
        g = [x for x in f if x["类型"] == "材质支不符"]
        self.assertTrue(g)
        self.assertIn("第 59 章注释 2", g[0]["说明"])        # 含聚氨酯时提示涂层那条路

    def test_goods_conflict_reported(self):
        f = ac.audit(_db(), _rows(("6104.52.0010", "8.3%", "Women's sweater", "100% Cotton", 5.0)))
        g = [x for x in f if x["类型"] == "品名与条文冲突"]
        self.assertTrue(g and g[0]["级别"] == "错误")

    def test_low_price_premium_fiber(self):
        recs = [("6110.12.1010", "4%", "Men's Sweater", "100% Cashmere", 0.62)] * 3
        f = ac.audit(_db(), _rows(*recs))
        g = [x for x in f if x["类型"] == "低价高端纤维"]
        self.assertTrue(g and g[0]["级别"] == "存疑")       # 只提示风险，不判对错
        # 单价正常就不报
        recs2 = [("6110.12.1010", "4%", "Men's Sweater", "100% Cashmere", 120.0)] * 3
        self.assertNotIn("低价高端纤维", _kinds(ac.audit(_db(), _rows(*recs2))))

    def test_compound_duty_missing_specific_part(self):
        f = ac.audit(_db(), _rows(("9615.11.3000", "4.6%", "HAIR BRUSH", "塑料制", 1.85)))
        self.assertIn("复合税缺从量项", _kinds(f))

    def test_grouped_by_8_digit(self):
        recs = [("6110.12.1010", "4%", "Men's Sweater", "100% Cashmere", 0.62),
                ("6110.12.1020", "4%", "Ladies Sweater", "100% Cashmere", 0.70),
                ("6110.12.1030", "4%", "Men's Vest", "100% Cashmere", 0.61)]
        g = [x for x in ac.audit(_db(), _rows(*recs)) if x["类型"] == "低价高端纤维"]
        self.assertEqual(len(g), 1)          # 三个后缀合成一条
        self.assertEqual(g[0]["条数"], 3)
        self.assertEqual(g[0]["编码"], "61101210")

    def test_ordering_errors_first(self):
        recs = [("9999.99.9999", "1%", "x", "", 1.0),
                ("6110.12.1010", "4%", "Men's Sweater", "100% Cashmere", 0.62)]
        f = ac.audit(_db(), _rows(*recs))
        self.assertEqual(f[0]["级别"], "错误")

    def test_report_md_renders(self):
        f = ac.audit(_db(), _rows(("8507.60.0020", "3.4%", "Jump Starter", "Plastic", 3.85)))
        md = ac.report_md(f, 1, 1, "x.json")
        self.assertIn("# 申报数据对账报告", md)
        self.assertIn("统计后缀作废", md)
        self.assertIn("$3.85", md)


if __name__ == "__main__":
    unittest.main()


class TestDecisions(unittest.TestCase):
    """人工/逐级链定下来的结论要落在文件里：每跑一次对账都把同样的问题再报一遍，判过的得能消掉。"""

    DEC = {"62043320": {"改为": "6204.39.8060", "依据": "亚麻占优，进不了合成纤维支",
                        "定于": "2026-09-16", "谁": "逐级链"}}

    def _run(self, dec):
        return ac.audit(_db(), _rows(("6204.33.2000", "2.8%", "Women's Blazers",
                                      "55% flax fibers 45% Polyurethane", 1.1)), decisions=dec)

    def test_decision_downgrades_finding(self):
        f = [x for x in self._run(self.DEC) if x["类型"] == "材质支不符"]
        self.assertTrue(f)
        self.assertEqual(f[0]["级别"], "已定")
        self.assertEqual(f[0]["原级别"], "存疑")        # 原始严重度保留，不是把问题抹掉
        self.assertEqual(f[0]["结论"]["改为"], "6204.39.8060")

    def test_without_decision_stays_open(self):
        f = [x for x in self._run({}) if x["类型"] == "材质支不符"]
        self.assertEqual(f[0]["级别"], "存疑")
        self.assertNotIn("结论", f[0])

    def test_decided_sorts_last(self):
        recs = _rows(("6204.33.2000", "2.8%", "Women's Blazers", "55% flax fibers 45% Polyurethane", 1.1),
                     ("9999.99.9999", "1%", "x", "", 1.0))
        f = ac.audit(_db(), recs, decisions=self.DEC)
        self.assertEqual(f[0]["级别"], "错误")
        self.assertEqual(f[-1]["级别"], "已定")

    def test_load_decisions_normalizes_key(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "dec.json")
            json.dump({"6204.33.20": {"改为": "6204.39.8060"}}, open(p, "w", encoding="utf-8"))
            self.assertEqual(ac.load_decisions(p)["62043320"]["改为"], "6204.39.8060")
        self.assertEqual(ac.load_decisions(""), {})

    def test_report_shows_conclusion_not_advice(self):
        md = ac.report_md(self._run(self.DEC), 12, 1, "x.json")
        self.assertIn("## 已定", md)
        self.assertIn("**已定结论**：改为 **6204.39.8060**", md)
        self.assertIn("2026-09-16", md)
        self.assertIn("亚麻占优", md)
