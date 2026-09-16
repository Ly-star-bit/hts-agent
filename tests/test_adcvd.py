# -*- coding: utf-8 -*-
"""
AD/CVD 案件导入与探测：
  - adcvd_import 认得五花八门的表头，一格多个税号能拆开，案号从杂文里抓出来
  - build_db.compile_adcvd 编出 精确 / 前缀 两级索引
  - core.adcvd_cases 只探测：同原产地排前、其他国家也列；没导入 → None（与"查过没有"区分）
  - query_one 备注与来源栏随之变化；未导入时来源栏保持「未覆盖」
"""
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import adcvd_import
import build_db
import core

CSV = """Case Number,Product Short Name,Country,Case Status,HTS Numbers
A-570-979,Crystalline Silicon Photovoltaic Cells,China,Active,"8541.42.0010, 8541.43.0010; 8501.71.0000"
C-570-980,Crystalline Silicon Photovoltaic Cells,China,Active,8541.42.0010 8541.43.0010
A-552-828,Steel Nails,Vietnam,Active,7317.00.55
A-570-1049,Steel Nails,People's Republic of China,Active,"7317.00.5503
7317.00.5505"
A-201-830,Carbon and Alloy Steel Wire Rod,Mexico,Revoked,7213.91
junk row,,,,
"""


class ImportTest(unittest.TestCase):
    def test_parse_rows_columns_and_hts_split(self):
        import pandas as pd
        df = pd.read_csv(io.StringIO(CSV), dtype=str)
        cases = adcvd_import.parse_rows(df)
        by = {c["案号"]: c for c in cases}
        self.assertEqual(set(by), {"A-570-979", "C-570-980", "A-552-828", "A-570-1049", "A-201-830"})
        self.assertEqual(by["A-570-979"]["hts"], ["8541420010", "8541430010", "8501710000"])
        self.assertEqual(by["A-570-979"]["类型"], "AD")
        self.assertEqual(by["C-570-980"]["类型"], "CVD")
        self.assertEqual(by["A-570-1049"]["国家代码"], "CN")
        self.assertEqual(by["A-570-1049"]["hts"], ["7317005503", "7317005505"])
        self.assertEqual(by["A-201-830"]["hts"], ["721391"])       # 6 位前缀
        self.assertEqual(by["A-201-830"]["状态"], "Revoked")

    def test_missing_required_column_is_explicit(self):
        import pandas as pd
        df = pd.DataFrame({"Product": ["x"], "Country": ["China"]})
        with self.assertRaises(SystemExit) as cm:
            adcvd_import.parse_rows(df)
        self.assertIn("case", str(cm.exception))


class DetectTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import pandas as pd
        cases = adcvd_import.parse_rows(pd.read_csv(io.StringIO(CSV), dtype=str))
        cls.idx = build_db.compile_adcvd({"meta": {"source": "测试表", "imported_at": "2026-09-15 20:00:00",
                                                    "n_cases": len(cases)}, "cases": cases})
        cls.db = core.load_db()

    def test_index_levels(self):
        self.assertIn("8541420010", self.idx["exact"])
        self.assertIn("721391", self.idx["prefix"])
        self.assertEqual(len(self.idx["cases"]), 5)

    def test_detect_orders_same_origin_first(self):
        db = dict(self.db); db["adcvd_index"] = self.idx
        hits = core.adcvd_cases(db, "7317005503", "73170055", "CN")
        self.assertEqual([h["案号"] for h in hits], ["A-570-1049", "A-552-828"])
        self.assertTrue(hits[0]["同原产地"]); self.assertFalse(hits[1]["同原产地"])
        # 8 位查询也能靠 10 位精确号命中（按 8 位前缀不命中；exact 只存 8/10 位）——
        # 所以 8 位查询只在案件表也给了 8 位号时命中；这里 7317.00.55 由越南案给了 8 位
        hits8 = core.adcvd_cases(db, "73170055", "73170055", "VN")
        self.assertEqual([h["案号"] for h in hits8], ["A-552-828"])
        # 6 位前缀：7213.91.xx 任何 10 位都命中墨西哥案
        self.assertEqual([h["案号"] for h in core.adcvd_cases(db, "7213913000", "72139130", "CN")], ["A-201-830"])
        # 没导入案件表 → None
        db2 = dict(self.db); db2["adcvd_index"] = {"cases": [], "exact": {}, "prefix": {}, "meta": {}}
        self.assertIsNone(core.adcvd_cases(db2, "7317005503", "73170055", "CN"))

    def test_query_one_note_and_sources(self):
        db = dict(self.db); db["adcvd_index"] = self.idx
        r = core.query_one(db, "8541420010", origin="CN")
        self.assertEqual([a["案号"] for a in r["AD/CVD案件"]], ["A-570-979", "C-570-980"])
        self.assertIn("AD/CVD 案件的参考 HTS 范围", r["备注"])
        self.assertIn("A-570-979", r["备注"])
        kinds = {s["类型"]: s for s in r["来源"]}
        self.assertIn("反倾销/反补贴（AD/CVD，参考 HTS 探测）", kinds)
        self.assertIn("测试表", kinds["反倾销/反补贴（AD/CVD，参考 HTS 探测）"]["文件"])
        # 查过没有：备注不提，来源栏说"案件表中无"
        r2 = core.query_one(db, "61091000", origin="CN")
        self.assertEqual(r2["AD/CVD案件"], [])
        self.assertNotIn("AD/CVD 案件", r2["备注"])
        self.assertIn("无以该编码为参考", {s["类型"]: s for s in r2["来源"]}["反倾销/反补贴（AD/CVD，参考 HTS 探测）"]["说明"])
        # 未导入：来源栏保持「未覆盖」
        db2 = dict(self.db); db2["adcvd_index"] = {"cases": [], "exact": {}, "prefix": {}, "meta": {}}
        r3 = core.query_one(db2, "8541420010", origin="CN")
        self.assertIsNone(r3["AD/CVD案件"])
        self.assertEqual({s["类型"]: s for s in r3["来源"]}["反倾销/反补贴（AD/CVD）"]["文件"], "未覆盖")


if __name__ == "__main__":
    unittest.main()
