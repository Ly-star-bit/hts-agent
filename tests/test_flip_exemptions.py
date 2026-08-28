# -*- coding: utf-8 -*-
"""
test_flip_exemptions.py —— FLIP 301 ANNEX II 豁免清单的一致性

背景：同一份事实此前存了两套且互不校验。
  - by_economy / universal        判豁免用，直接影响税额，来自更早的**按页**归属
  - by_economy_pages / *_scopes   仅作出处展示，来自 extract_flip_scopes.py 的
                                  表格行级 Part 状态机

ANNEX II 里 Part 常从页面中部开始（FRN 物理页 245 上半是 Part B、下半是 Part C），
按页归属就会整段错位。实测两者相差 189 个编码：83 个被错判为豁免（少收 10~12.5%
FLIP 301），106 个漏判（多收）。例：3823.11.00（硬脂酸）在页 245 属 Part C（EU），
却被记进 GB——英国产该货会被告知"豁免"，而 GB 实际在 10% 档。

已改为两者同源生成。这里锁住不变量，防止再次分叉。
"""
import json
import os
import sys
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import core

EXEMPTIONS_JSON = os.path.join(BASE, "data", "flip301_exemptions.json")


class TestExemptionConsistency(unittest.TestCase):
    """判豁免用的列表，必须与带出处的那份逐个相等"""

    @classmethod
    def setUpClass(cls):
        with open(EXEMPTIONS_JSON, encoding="utf-8-sig") as f:
            cls.ex = json.load(f)

    def test_universal_matches_pages(self):
        self.assertEqual(set(self.ex["universal"]), set(self.ex["universal_pages"]),
                         "Part A 通用豁免列表与页码表不一致")

    def test_by_economy_matches_pages(self):
        be, bp = self.ex["by_economy"], self.ex["by_economy_pages"]
        self.assertEqual(set(be), set(bp), "经济体集合不一致")
        for eco in sorted(be):
            extra = set(be[eco]) - set(bp[eco])
            missing = set(bp[eco]) - set(be[eco])
            self.assertEqual(extra, set(),
                             f"{eco} 多出 {len(extra)} 个豁免编码（会少收 FLIP 301）：{sorted(extra)[:5]}")
            self.assertEqual(missing, set(),
                             f"{eco} 缺少 {len(missing)} 个豁免编码（会多收 FLIP 301）：{sorted(missing)[:5]}")

    def test_every_code_has_provenance(self):
        """每个豁免编码都要能说出它来自 FRN 第几页——否则无从复核"""
        for code in self.ex["universal"]:
            self.assertIn(code, self.ex["universal_pages"], f"通用豁免 {code} 无页码")
        for eco, codes in self.ex["by_economy"].items():
            for code in codes:
                self.assertIn(code, self.ex["by_economy_pages"][eco],
                              f"{eco} 的豁免 {code} 无页码")

    def test_codes_are_normalized_8digit(self):
        allc = list(self.ex["universal"]) + [c for v in self.ex["by_economy"].values() for c in v]
        for code in allc:
            self.assertTrue(code.isdigit() and len(code) == 8, f"编码格式异常：{code!r}")


class TestKnownBoundaryCases(unittest.TestCase):
    """
    Part 边界上的具体编码，逐个对着 FRN 原文核过。

    这些是回归的着火点：一旦提取退回按页归属，它们会最先错。
    """

    @classmethod
    def setUpClass(cls):
        with open(EXEMPTIONS_JSON, encoding="utf-8-sig") as f:
            cls.ex = json.load(f)

    def _in(self, eco, code):
        return code in (self.ex["by_economy"].get(eco) or [])

    def test_stearic_acid_belongs_to_eu_not_gb(self):
        """FRN 物理页 245：上半 Part B（GB）结束，下半 Part C（EU）起，3823.11.00 在 Part C 段"""
        self.assertTrue(self._in("EU", "38231100"), "3823.11.00 应属 EU（Part C）")
        self.assertFalse(self._in("GB", "38231100"), "3823.11.00 不应属 GB")

    def test_page_245_part_c_block(self):
        """页 245 Part C 段的整组编码都应归 EU"""
        for code in ("38231100", "38231200", "38231920", "45011000", "45031020"):
            self.assertTrue(self._in("EU", code), f"{code} 应属 EU")
            self.assertFalse(self._in("GB", code), f"{code} 不应属 GB")

    def test_page_315_split_between_n_and_o(self):
        """
        页 315 同页跨两个 Part：第 0 行 Part N（JO）、第 34 行 Part O（CAFTA-DR）。
        标题前的 8112.92.07 归 JO，标题后的 4202.11.00 归 CAFTA_DR。
        按页归属会把整页判给其中一个，两边都错。
        """
        self.assertTrue(self._in("CAFTA_DR", "42021100"),
                        "4202.11.00 在 Part O 标题之后，应归 CAFTA_DR")
        self.assertTrue(self._in("JO", "81129207"),
                        "8112.92.07 在 Part O 标题之前，应归 JO")
        self.assertFalse(self._in("CAFTA_DR", "81129207"),
                         "8112.92.07 不应被 Part O 吸走")

    def test_gb_count(self):
        """对着 FRN Part B 数过：48 个（此前 57 个，多吞了 Part C 开头 9 个）"""
        self.assertEqual(len(self.ex["by_economy"]["GB"]), 48)


class TestExemptionAffectsTax(unittest.TestCase):
    """豁免判定要真的作用到税额上"""

    @classmethod
    def setUpClass(cls):
        cls.db = core.load_db()

    def test_gb_stearic_acid_not_exempt(self):
        """英国产硬脂酸此前被误判豁免，现应正常适用 FLIP 301"""
        r = core.query_one(self.db, "38231100", origin="GB")
        self.assertNotIn("豁免", r.get("FLIP 301加征", ""),
                         "3823.11.00 对 GB 不应豁免")

    def test_eu_stearic_acid_exempt(self):
        r = core.query_one(self.db, "38231100", origin="EU")
        self.assertIn("豁免", r.get("FLIP 301加征", "") + r.get("FLIP 301说明", ""),
                      "3823.11.00 对 EU 应豁免")

    def test_exemption_reason_cites_page(self):
        """豁免结论必须带 FRN 页码，便于人工复核"""
        r = core.query_one(self.db, "38231100", origin="EU")
        self.assertRegex(r.get("FLIP 301说明", ""), r"物理页 \d+")


if __name__ == "__main__":
    unittest.main()
