# -*- coding: utf-8 -*-
"""
test_measures.py —— 多措施（301 flip / 越南原产地 / FLIP 301 / 措施开关配置）测试

运行：python -m unittest tests.test_measures -v
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import core
import rate


class TestFlip301(unittest.TestCase):
    """301 flip 历史（当前值 + 此前值）"""

    @classmethod
    def setUpClass(cls):
        cls.db = core.load_db()

    def test_lithium_battery_flip(self):
        # 8507.60.00 锂电池：此前 List 4A 7.5%，当前 9903.91.06 25%（2026 生效）
        r = core.query_one(self.db, "85076000", origin="CN")
        self.assertEqual(r["301判定"], "是")
        self.assertIn("+25%", r["301加征"])
        self.assertEqual(len(r["301 flip历史"]), 1)
        hist = r["301 flip历史"][0]
        self.assertEqual(hist["c99"], "9903.88.15")
        self.assertEqual(hist["pct"], 7.5)
        self.assertIn("7.5%", hist["note"])
        self.assertIn("此前 +7.5%", r["301 flip变化"])
        self.assertIn("当前 +25%", r["301 flip变化"])
        self.assertIn("2019-09-01", r["301 flip变化"])

    def test_ev_flip(self):
        # 8703.80.00 电动车：此前 List 3 25%，当前 9903.91.03 100%
        r = core.query_one(self.db, "87038000", origin="CN")
        self.assertIn("+100%", r["301加征"])
        self.assertEqual(len(r["301 flip历史"]), 1)
        self.assertEqual(r["301 flip历史"][0]["pct"], 25.0)
        self.assertIn("此前 +25%", r["301 flip变化"])
        self.assertIn("当前 +100%", r["301 flip变化"])

    def test_no_flip(self):
        # 9403.50.90 木家具：无 flip 历史
        r = core.query_one(self.db, "94035090", origin="CN")
        self.assertEqual(r["301 flip历史"], [])
        self.assertEqual(r["301 flip变化"], "")

    def test_flip_field_present_for_vn(self):
        # 越南轨道下 flip 字段存在但为空
        r = core.query_one(self.db, "85076000", origin="VN")
        self.assertEqual(r["301 flip历史"], [])
        self.assertEqual(r["301 flip变化"], "")


class TestVietnam(unittest.TestCase):
    """越南原产地轨道"""

    @classmethod
    def setUpClass(cls):
        cls.db = core.load_db()

    def test_vn_not_apply_301(self):
        # 越南原产锂电池：不适用 301，走 MFN
        r = core.query_one(self.db, "85076000", origin="VN")
        self.assertEqual(r["301判定"], "不适用（越南原产）")
        self.assertEqual(r["301加征"], "")
        self.assertEqual(r["9903子目"], "")
        self.assertIn("MFN", r["越南措施"])
        self.assertIn("3.4%", r["越南措施"])
        self.assertEqual(r["原产地"], "越南")

    def test_cn_vs_vn_differ(self):
        cn = core.query_one(self.db, "85076000", origin="CN")
        vn = core.query_one(self.db, "85076000", origin="VN")
        self.assertEqual(cn["301判定"], "是")
        self.assertIn("+25%", cn["301加征"])
        self.assertNotEqual(cn["301加征"], vn["301加征"])
        self.assertNotEqual(cn["301判定"], vn["301判定"])

    def test_vn_covered_note(self):
        r = core.query_one(self.db, "87038000", origin="VN")
        self.assertIn("2.5%", r["越南措施"])

    def test_vn_not_covered_explicit(self):
        # 未覆盖编码：仍适用 MFN，但标注「数据未覆盖具体说明」
        r = core.query_one(self.db, "01012100", origin="VN")
        self.assertIn("MFN", r["越南措施"])
        self.assertIn("数据未覆盖具体说明", r["越南措施"])

    def test_calc_total_vn_vs_cn(self):
        # 总税负（含 FLIP 301）：中国光伏 Free + 50% + 12.5% = 62.5%；越南 Free + 12.5% = 12.5%（8541.43.00）
        cn = rate.calc_total(self.db, "85414300", unit_value=10, origin="CN")
        vn = rate.calc_total(self.db, "85414300", unit_value=10, origin="VN")
        self.assertEqual(cn["301加征数值"], 50.0)
        self.assertEqual(vn["301加征数值"], 0.0)
        self.assertEqual(cn["FLIP 301加征数值"], 12.5)
        self.assertEqual(vn["FLIP 301加征数值"], 12.5)
        self.assertIn("62.5%", cn["总税负估算"])
        self.assertIn("12.5%", vn["总税负估算"])

    def test_batch_query_origin(self):
        results, stats = core.batch_query(self.db, ["85076000", "01012100"], origin="VN")
        self.assertEqual(stats["origin"], "越南")
        self.assertTrue(all("不适用" in r["301判定"] for r in results))


class TestFlip301ForcedLabor(unittest.TestCase):
    """FLIP 301 强迫劳动关税（2026-07-24 生效，USTR Final Action FRN 核实 + ANNEX II 豁免）"""

    # 8541.43.00 光伏：不在 ANNEX II 豁免清单 → 作为加征基准
    # 8507.60.00 锂电池：在 ANNEX II Part A 通用豁免 → 作为豁免示例

    @classmethod
    def setUpClass(cls):
        cls.db = core.load_db()

    def test_cn_125(self):
        # 中国：12.5%（all other investigated，非豁免编码）
        r = core.query_one(self.db, "85414300", origin="CN")
        self.assertEqual(r["FLIP 301加征"], "+12.5%")
        self.assertIn("12.5%", r["FLIP 301说明"])
        self.assertIn("FLIP 301", r["备注"])

    def test_vn_125(self):
        # 越南：12.5%（FRN 修正：越南不在 10% 名单）
        r = core.query_one(self.db, "85414300", origin="VN")
        self.assertEqual(r["FLIP 301加征"], "+12.5%")
        self.assertEqual(r["原产地代码"], "VN")

    def test_ca_10(self):
        # 加拿大：10%
        r = core.query_one(self.db, "85414300", origin="CA")
        self.assertEqual(r["FLIP 301加征"], "+10%")
        self.assertEqual(r["原产地"], "其他国家")
        self.assertEqual(r["原产地代码"], "CA")

    def test_eu_net_mfn_10(self):
        r = core.query_one(self.db, "85414300", origin="EU")
        self.assertEqual(r["FLIP 301加征"], "+10%")
        self.assertIn("MFN", r["FLIP 301说明"])

    def test_jp_net_mfn_125(self):
        r = core.query_one(self.db, "85414300", origin="JP")
        self.assertEqual(r["FLIP 301加征"], "+12.5%")

    def test_not_investigated(self):
        # 德国（不在 60 名单）：不适用
        r = core.query_one(self.db, "85414300", origin="DE")
        self.assertEqual(r["FLIP 301加征"], "")
        self.assertIn("不在 FLIP 301", r["FLIP 301说明"])

    def test_232_exemption_hint(self):
        # 已适用 Section 232 的产品豁免（说明含提示）
        r = core.query_one(self.db, "85414300", origin="CN")
        self.assertIn("Section 232", r["FLIP 301说明"])

    def test_total_with_flip301(self):
        # 总税负叠加：中国光伏 Free + 50% + 12.5% = 62.5%（8541.43.00）
        r = rate.calc_total(self.db, "85414300", unit_value=10, origin="CN")
        self.assertIn("62.5%", r["总税负估算"])
        self.assertEqual(r["FLIP 301加征数值"], 12.5)

    def test_exemption_universal_part_a(self):
        # ANNEX II 通用豁免（Part A）：8507.60.00 锂电池被官方豁免 FLIP 301
        r = core.query_one(self.db, "85076000", origin="CN")
        self.assertEqual(r["FLIP 301加征"], "豁免")
        self.assertIn("ANNEX II", r["FLIP 301说明"])
        self.assertIn("豁免", r["备注"])
        # 豁免不叠加进总税负：中国 3.4% + 25% + 0 = 28.4%
        t = rate.calc_total(self.db, "85076000", unit_value=10, origin="CN")
        self.assertIn("28.4%", t["总税负估算"])
        self.assertEqual(t["FLIP 301加征数值"], 0.0)

    def test_exemption_not_universal(self):
        # 非豁免编码（光伏）不受影响
        r = core.query_one(self.db, "85414300", origin="CN")
        self.assertEqual(r["FLIP 301加征"], "+12.5%")

    def test_exemption_scope_limitation(self):
        # 9025.19.80 在 ANNEX II Part A，但带 Aircraft 范围限制（仅民用航空器用途豁免）
        r = core.query_one(self.db, "90251980", origin="CN")
        self.assertEqual(r["FLIP 301加征"], "豁免")
        self.assertIn("范围限制：Aircraft", r["FLIP 301说明"])
        self.assertIn("FRN 物理页", r["FLIP 301说明"])
        # 来源字段：FLIP 来源带范围限制与页码
        flip_src = next(s for s in r["来源"] if s["类型"] == "FLIP 301")
        self.assertEqual(flip_src["范围限制"], "Aircraft")
        self.assertRegex(flip_src["位置"], r"第 \d+ 页")
        self.assertEqual(flip_src["key"], "flip_frn")

    def test_source_field_structure(self):
        # 来源字段：基础税率带 CSV 行号；301 加征带 USTR PDF 页码；flip 历史带本地文件
        r = core.query_one(self.db, "85076000", origin="CN")
        srcs = {s["类型"]: s for s in r["来源"]}
        self.assertIn("基础税率", srcs)
        self.assertRegex(srcs["基础税率"]["位置"], r"第 \d+ 行")
        self.assertEqual(srcs["基础税率"]["key"], "htsdata")
        self.assertIn("301 加征", srcs)
        self.assertRegex(srcs["301 加征"]["位置"], r"第 \d+ 页")
        self.assertEqual(srcs["301 加征"]["key"], "ustr_pdf")
        self.assertIn("9903", srcs["301 加征"]["说明"])
        self.assertIn("FLIP 301", srcs)
        self.assertEqual(srcs["FLIP 301"]["key"], "flip_frn")
        self.assertIn("301 flip 历史", srcs)
        self.assertEqual(srcs["301 flip 历史"]["key"], "local")


class TestMeasuresConfig(unittest.TestCase):
    """加征措施开关配置（measures_config.json，默认全启用）"""

    @classmethod
    def setUpClass(cls):
        cls.db = core.load_db()

    def setUp(self):
        # 临时替换配置路径，避免污染真实 measures_config.json
        import tempfile
        self._orig = core.MEASURES_CONFIG
        self._tmp = os.path.join(tempfile.mkdtemp(), "measures_config_test.json")
        core.MEASURES_CONFIG = self._tmp

    def tearDown(self):
        core.MEASURES_CONFIG = self._orig
        if os.path.exists(self._tmp):
            os.remove(self._tmp)

    def _write(self, cn301=True, flip301=True):
        with open(self._tmp, "w", encoding="utf-8") as f:
            json.dump({"measures": {"cn301": cn301, "flip301": flip301}}, f)

    def test_default_all_enabled(self):
        # 配置缺失 → 全部默认启用；中国 8541.43.00（非豁免）输出 301 +50% + FLIP 301 +12.5%
        r = core.query_one(self.db, "85414300", origin="CN")
        self.assertEqual(r["301加征"], "+50%")
        self.assertEqual(r["FLIP 301加征"], "+12.5%")
        self.assertNotIn("强迫劳动", r)
        total = rate.calc_total(self.db, "85414300", unit_value=10, origin="CN")
        self.assertIn("62.5%", total["总税负估算"])  # Free + 50% + 12.5%

    def test_disable_flip301(self):
        # 禁用 FLIP 301：无该加征字段，估算总税负 = Free + 50% = 50%
        self._write(cn301=True, flip301=False)
        r = core.query_one(self.db, "85414300", origin="CN")
        self.assertNotIn("FLIP 301加征", r)
        self.assertNotIn("FLIP 301说明", r)
        self.assertIn("FLIP 301 已禁用", r["备注"])
        self.assertEqual(r["301加征"], "+50%")  # 301 不受影响
        total = rate.calc_total(self.db, "85414300", unit_value=10, origin="CN")
        self.assertIn("50%", total["总税负估算"])

    def test_disable_cn301(self):
        # 禁用中国 301：无"301加征"字段，保留判定信息；估算 = Free + 12.5%(FLIP) = 12.5%
        self._write(cn301=False, flip301=True)
        r = core.query_one(self.db, "85414300", origin="CN")
        self.assertNotIn("301加征", r)
        self.assertEqual(r["301判定"], "是")  # 判定信息保留
        self.assertEqual(r["9903子目"], "9903.91.02")
        self.assertIn("301 加征已禁用", r["备注"])
        self.assertEqual(r["FLIP 301加征"], "+12.5%")
        total = rate.calc_total(self.db, "85414300", unit_value=10, origin="CN")
        self.assertIn("12.5%", total["总税负估算"])

    def test_disable_both(self):
        # 全禁用：估算 = Free（基础 only）
        self._write(cn301=False, flip301=False)
        total = rate.calc_total(self.db, "85414300", unit_value=10, origin="CN")
        self.assertIn("0%", total["总税负估算"])

    def test_reenable_restores(self):
        # 禁用后重新启用 → 恢复默认输出
        self._write(cn301=False, flip301=False)
        r1 = core.query_one(self.db, "85414300", origin="CN")
        self.assertNotIn("301加征", r1)
        self._write(cn301=True, flip301=True)
        r2 = core.query_one(self.db, "85414300", origin="CN")
        self.assertEqual(r2["301加征"], "+50%")
        self.assertEqual(r2["FLIP 301加征"], "+12.5%")

    def test_search_301_col_respects_config(self):
        # 税率搜索的 301 加征列随配置裁剪：禁用 cn301 后搜索不显示 +25%
        import rate
        self._write(cn301=True, flip301=True)
        rows = rate.search(self.db, "lithium ion battery", limit=5, sort="relevance")
        self.assertTrue(any(r["301加征"] == "+25%" for r in rows))
        self._write(cn301=False, flip301=True)
        rows2 = rate.search(self.db, "lithium ion battery", limit=5, sort="relevance")
        self.assertTrue(rows2)
        self.assertTrue(all(r["301加征"] == "" for r in rows2), "禁用 cn301 后搜索 301 加征列为空")
        self.assertEqual(rows2[0]["301判定"], "是")  # 判定信息保留

    def test_cn301_basic_judgement_regression(self):
        # 默认配置下中国 301 基础判定回归不变
        self._write(cn301=True, flip301=True)
        r = core.query_one(self.db, "85076000", origin="CN")
        self.assertEqual(r["301判定"], "是")
        self.assertEqual(r["9903子目"], "9903.91.06")
        self.assertIn("+25%", r["301加征"])
        self.assertEqual(len(r["301 flip历史"]), 1)


class TestChinaRegression(unittest.TestCase):
    """中国路径向后兼容回归"""

    @classmethod
    def setUpClass(cls):
        cls.db = core.load_db()

    def test_default_origin_is_cn(self):
        r = core.query_one(self.db, "85076000")  # 默认 CN
        self.assertEqual(r["原产地"], "中国")
        self.assertEqual(r["301判定"], "是")

    def test_other_origin_mfn_track(self):
        # 其他国家（如 XX/IN/DE）：固定 MFN 通用轨道，不叠加 301（不再回退中国）
        r = core.query_one(self.db, "85076000", origin="XX")
        self.assertEqual(r["原产地"], "其他国家")
        self.assertIn("不适用", r["301判定"])
        self.assertEqual(r["301加征"], "")
        self.assertIn("MFN", r["越南措施"])
        self.assertIn("其他国家通用轨道", r["越南措施"])
        # 中国路径不受影响
        cn = core.query_one(self.db, "85076000", origin="CN")
        self.assertEqual(cn["301判定"], "是")
        self.assertIn("+25%", cn["301加征"])

    def test_legacy_fields_present(self):
        # 既有字段全部保留
        r = core.query_one(self.db, "01012100", origin="CN")
        for k in ("输入编码", "8位子目", "商品描述", "一般税率", "特殊税率",
                  "第二栏税率", "301判定", "9903子目", "301加征", "附加税", "备注"):
            self.assertIn(k, r)

    def test_legacy_cn_judgement_unchanged(self):
        # 中国路径判定与既有逻辑一致
        r = core.query_one(self.db, "01012100", origin="CN")
        self.assertEqual(r["301判定"], "是")
        self.assertIn("+7.5%", r["301加征"])
        self.assertEqual(r["9903子目"], "9903.88.15")


if __name__ == "__main__":
    unittest.main()
