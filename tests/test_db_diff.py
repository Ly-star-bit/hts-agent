# -*- coding: utf-8 -*-
"""
test_db_diff.py —— 数据变动追踪

原实现有三处让"无变动"这个结论不可信：
  1. 统计与明细共用一个 300 条上限——`if len(changes) >= MAX: break` 在计数之前，
     攒够 300 条后 stats 也停止累加，用户看到的是个被腰斩的数字
  2. `truncated: len(stats) > MAX_CHANGES` —— stats 是个固定几键的字典，
     4 > 300 恒为 False，截断从未被报告过
  3. 指纹只覆盖 rates_8.general / sec301_map / c99_percent。附加税与 FLIP 301
     完全在监控外——FLIP 是 12.5% 的税，豁免清单整体换掉也只显示"无变化"
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import core
import db_diff


class DiffTestBase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.db = core.load_db()

    def setUp(self):
        self._orig_load = db_diff._load_old

    def tearDown(self):
        db_diff._load_old = self._orig_load

    def diff(self, snap):
        db_diff._load_old = lambda: snap
        return db_diff.compare_with_fingerprint(self.db)

    def snap(self):
        """当前库的指纹（深到可以随意改动而不影响真实 db）"""
        import copy
        return copy.deepcopy(db_diff._fingerprint(self.db))


class TestNoChange(DiffTestBase):

    def test_identical_snapshot_reports_nothing(self):
        r = self.diff(self.snap())
        self.assertEqual(r["total_changes"], 0)
        self.assertEqual(r["changes"], [])
        self.assertFalse(r["truncated"])
        self.assertEqual(r["new_categories"], [])

    def test_first_build(self):
        db_diff._load_old = lambda: None
        r = db_diff.compare_with_fingerprint(self.db)
        self.assertTrue(r["first_build"])


class TestStatsNotTruncated(DiffTestBase):
    """计数必须完整，与明细上限解耦"""

    def _many_rate_changes(self, n):
        s = self.snap()
        for code in sorted(s["rates_8"])[:n]:
            s["rates_8"][code] = {"general": "999%"}
        return s

    def test_stats_count_beyond_detail_cap(self):
        r = self.diff(self._many_rate_changes(500))
        self.assertEqual(r["stats"]["rate_changed"], 500,
                         "统计被明细上限腰斩了")
        self.assertEqual(r["total_changes"], 500)
        self.assertEqual(len(r["changes"]), 300)

    def test_truncated_flag_and_omitted(self):
        r = self.diff(self._many_rate_changes(500))
        self.assertTrue(r["truncated"], "截断从未被报告")
        self.assertEqual(r["omitted"], 200)

    def test_not_truncated_when_under_cap(self):
        r = self.diff(self._many_rate_changes(10))
        self.assertFalse(r["truncated"])
        self.assertEqual(r["omitted"], 0)
        self.assertEqual(len(r["changes"]), 10)


class TestMeasureCoverage(DiffTestBase):
    """FLIP 301 / 附加税等必须在监控内"""

    def test_flip_exemptions_change_detected(self):
        s = self.snap()
        s["_digests"]["flip301_exemptions"] = "0000000000000000"
        s["_exemption_scale"] = {"universal": 2000, "economies": 13, "by_economy_codes": 400}
        r = self.diff(s)
        self.assertEqual(r["stats"]["measure_changed"], 1)
        hit = [c for c in r["changes"] if c["描述"] == "FLIP 301 豁免清单"]
        self.assertEqual(len(hit), 1)
        # 不能只说"变了"，要说变了多少
        self.assertIn("2000", hit[0]["旧"])
        self.assertIn(str(len(self.db["flip301_exemptions"]["universal"])), hit[0]["新"])

    def test_add_duty_change_detected(self):
        s = self.snap()
        code = sorted(s["add_duty"])[0]
        s["add_duty"][code] = "1¢/kg"
        r = self.diff(s)
        self.assertEqual(r["stats"]["add_duty_changed"], 1)
        self.assertTrue([c for c in r["changes"] if c["类型"] == "附加税变化"])

    def test_sec301_10_change_detected(self):
        s = self.snap()
        code = sorted(s["sec301_map_10"])[0]
        s["sec301_map_10"][code] = "9903.99.99"
        r = self.diff(s)
        self.assertEqual(r["stats"]["sec301_10_changed"], 1)

    def test_vietnam_change_detected(self):
        s = self.snap()
        s["_digests"]["vietnam"] = "0000000000000000"
        r = self.diff(s)
        self.assertEqual(r["stats"]["measure_changed"], 1)

    def test_measure_change_survives_truncation(self):
        """
        措施级变动至多几条却影响最大（FLIP 豁免清单换掉 = 12.5% 的税对上千个
        编码的适用性全变）。放在逐码变动之后会被 500 条税率调整挤出明细上限，
        用户只看到一堆琐碎变化，反而漏掉真正该看的那条。
        """
        s = self.snap()
        for code in sorted(s["rates_8"])[:500]:
            s["rates_8"][code] = {"general": "999%"}
        s["_digests"]["flip301_exemptions"] = "0000000000000000"
        r = self.diff(s)
        self.assertTrue(r["truncated"])
        self.assertEqual(r["changes"][0]["类型"], "措施数据变化",
                         "措施级变动被逐码变动挤出了明细")


class TestSchemaMigration(DiffTestBase):
    """旧版指纹缺少新字段时，不能把整批数据误报成新增"""

    def old_v1(self):
        return {
            "rates_8": {k: {"general": v.get("general", "")}
                        for k, v in self.db["rates_8"].items()},
            "sec301_map": self.db["sec301_map"],
            "c99_percent": self.db["c99_percent"],
        }

    def test_no_false_positives(self):
        r = self.diff(self.old_v1())
        self.assertEqual(r["total_changes"], 0,
                         "旧快照缺字段时不得把整批数据报成变动")

    def test_new_categories_declared(self):
        """比不了要说是'比不了'，不能让用户以为'没变'"""
        r = self.diff(self.old_v1())
        self.assertIn("FLIP 301 豁免清单", r["new_categories"])
        self.assertIn("附加税变化", r["new_categories"])

    def test_schema_recorded(self):
        self.assertEqual(db_diff._fingerprint(self.db)["_schema"], db_diff.SCHEMA)


class TestDigest(unittest.TestCase):

    def test_key_order_irrelevant(self):
        self.assertEqual(db_diff._digest({"a": 1, "b": 2}),
                         db_diff._digest({"b": 2, "a": 1}))

    def test_content_change_detected(self):
        self.assertNotEqual(db_diff._digest({"a": 1}), db_diff._digest({"a": 2}))


if __name__ == "__main__":
    unittest.main()
