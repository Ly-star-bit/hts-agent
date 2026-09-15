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
        self.assertEqual(r["原产地"], "加拿大")      # 有中文名的经济体不再笼统叫"其他国家"
        self.assertEqual(r["原产地代码"], "CA")
        self.assertEqual(r["FLIP 301标目"], "9903.05.29")   # 报关要填的官方标目

    def test_eu_net_mfn_10(self):
        r = core.query_one(self.db, "85414300", origin="EU")
        self.assertEqual(r["FLIP 301加征"], "≤+10%")  # 名义上限，实际额度取决于 MFN
        self.assertIn("MFN", r["FLIP 301说明"])
        self.assertEqual((r["FLIP 301档位"]["mode"], r["FLIP 301档位"]["cap"]), ("net_mfn", 10.0))
        # 两个官方标目：MFN 低于上限填 .39（合计 10%），否则 .38（不加征）
        self.assertEqual(r["FLIP 301标目"], "9903.05.39 / 9903.05.38")

    def test_jp_net_mfn_125(self):
        r = core.query_one(self.db, "85414300", origin="JP")
        self.assertEqual(r["FLIP 301加征"], "≤+12.5%")
        self.assertEqual((r["FLIP 301档位"]["mode"], r["FLIP 301档位"]["cap"]), ("net_mfn", 12.5))

    def test_not_investigated(self):
        # 美国（不在 60 名单）：不适用。
        # 注意不能拿德国举例——德国属欧盟，而欧盟在 net-of-MFN 10% 档内。
        r = core.query_one(self.db, "85414300", origin="US")
        self.assertEqual(r["FLIP 301加征"], "")
        self.assertIn("不在 FLIP 301", r["FLIP 301说明"])

    def test_not_investigated_beats_annex_ii(self):
        """
        不在 60 名单的原产地，命中 ANNEX II 也应答"不适用"，不是"豁免"。

        档位判定此前排在 ANNEX II 之后，美国产 0201.20.02 会被答成"官方豁免"。
        税额同样是 0，但结论性质不同：说"已豁免"，复核的人会去 FRN 里找一份
        对美国并不存在的豁免依据。
        """
        r = core.query_one(self.db, "02012002", origin="US")
        self.assertEqual(r["FLIP 301加征"], "")
        self.assertIn("不在 FLIP 301", r["FLIP 301说明"])
        self.assertEqual(r["FLIP 301档位"], {"mode": "none"})

    def test_eu_member_normalized_to_eu(self):
        # 成员国代码必须归一到 EU，否则会落进"不在名单"而静默漏加
        for member in ("DE", "FR", "IT", "DEU", "FRA"):
            r = core.query_one(self.db, "85414300", origin=member)
            self.assertEqual((r["FLIP 301档位"]["mode"], r["FLIP 301档位"]["cap"]),
                             ("net_mfn", 10.0), f"{member} 未被归一到 EU")
        self.assertEqual(core.normalize_origin("TWN"), "TW")
        self.assertEqual(core.normalize_origin("CT"), "TW")

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
        # ANNEX II 通用豁免（Part A）且**无**范围限制才是真豁免：
        # 0201.20.02 牛肉，Scope Limitations 栏为空，FLIP 301 不加征
        r = core.query_one(self.db, "02012002", origin="CN")
        self.assertEqual(r["FLIP 301加征"], "豁免")
        self.assertIn("ANNEX II", r["FLIP 301说明"])
        self.assertIn("豁免", r["备注"])
        # 豁免不叠加进总税负：中国 4% + 301 7.5% + 0 = 11.5%
        t = rate.calc_total(self.db, "02012002", unit_value=10, origin="CN")
        self.assertIn("11.5%", t["总税负估算"])
        self.assertEqual(t["FLIP 301加征数值"], 0.0)

    def test_exemption_not_universal(self):
        # 非豁免编码（光伏）不受影响
        r = core.query_one(self.db, "85414300", origin="CN")
        self.assertEqual(r["FLIP 301加征"], "+12.5%")

    def test_exemption_scope_limitation(self):
        """
        带 Scope Limitations 的 ANNEX II 条目不是无条件豁免。

        9025.19.80（温度计）在 Part A 但标 Aircraft，按 FRN 页 137 只有民用航空器
        及其零部件豁免。此前一律返回"豁免"、FLIP 加征算 0，一支普通工业温度计
        的中国产总税负被给成 25%（正确是 25% + 12.5% = 37.5%）——少收会被 CBP
        追补加罚，所以判不出用途时按不豁免保守计，另标"范围存疑"要人工确认。
        """
        r = core.query_one(self.db, "90251980", origin="CN")
        self.assertEqual(r["FLIP 301加征"], "+12.5%(范围存疑)")
        self.assertEqual(r["FLIP 301档位"]["mode"], "conditional")
        self.assertEqual(r["FLIP 301档位"]["scope"], "Aircraft")
        self.assertEqual((r["FLIP 301档位"]["fallback"]["mode"], r["FLIP 301档位"]["fallback"]["rate"]),
                         ("flat", 12.5))
        self.assertIn("范围限制 “Aircraft”", r["FLIP 301说明"])
        self.assertIn("民用航空器", r["FLIP 301说明"])   # FRN 页 137 的官方定义要给出来
        self.assertIn("FRN 物理页", r["FLIP 301说明"])
        self.assertIn("范围限制", r["备注"])              # 列表页只看备注也要看得出有条件
        # 税额按不豁免保守计：Free + 301 25% + FLIP 12.5%
        t = rate.calc_total(self.db, "90251980", unit_value=10, origin="CN")
        self.assertEqual(t["FLIP 301加征数值"], 12.5)
        self.assertIn("37.5%", t["总税负估算"])
        # 来源字段：FLIP 来源带范围限制与页码
        flip_src = next(s for s in r["来源"] if s["类型"] == "FLIP 301")
        self.assertEqual(flip_src["范围限制"], "Aircraft")
        self.assertRegex(flip_src["位置"], r"第 \d+ 页")
        self.assertEqual(flip_src["key"], "flip_frn")

    def test_scope_ex_carries_description(self):
        """
        "Ex" 档的范围由 ANNEX II 该行 Description 栏正文定义（FRN 页 137 原文），
        只给一个 "Ex" 等于没说。0805.90.01 的 Description 是 Etrogs（香橼），
        说明里必须带上，否则用户无从判断自己的货在不在范围内。
        """
        r = core.query_one(self.db, "08059001", origin="CN")
        self.assertIn("范围存疑", r["FLIP 301加征"])
        self.assertEqual(r["FLIP 301档位"]["scope"], "Ex")
        self.assertIn("Etrogs", r["FLIP 301说明"])

    def test_scope_limitation_net_mfn_origin(self):
        """
        net-of-MFN 档（EU/TW 合计封顶 10%）遇到范围限制，回退额仍要走 net_mfn，
        不能按名义 10% 直接加——否则 MFN 已达上限的商品会被凭空多加一遍。
        """
        r = core.query_one(self.db, "90251980", origin="EU")
        self.assertEqual(r["FLIP 301加征"], "≤+10%(范围存疑)")
        fb = r["FLIP 301档位"]["fallback"]
        self.assertEqual((fb["mode"], fb["cap"]), ("net_mfn", 10.0))

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
                  "第二栏税率", "301判定", "9903子目", "301加征", "备注"):
            self.assertIn(k, r)
        # "附加税"列已删：数据里只有 99 章标目有值，普通编码永远为空，
        # 而 README 把它写成 ADD/CVD——空格子会被读成"没有反倾销"
        self.assertNotIn("附加税", r)
        self.assertIn("反倾销/反补贴（AD/CVD）", {s["类型"] for s in r["来源"]})

    def test_legacy_cn_judgement_unchanged(self):
        # 中国路径判定与既有逻辑一致
        r = core.query_one(self.db, "01012100", origin="CN")
        self.assertEqual(r["301判定"], "是")
        self.assertIn("+7.5%", r["301加征"])
        self.assertEqual(r["9903子目"], "9903.88.15")


class TestFlip301DataAvailability(unittest.TestCase):
    """缺 FLIP 301 数据源时必须显式标注未覆盖，不能因命中 ANNEX II 而误报"豁免" """

    @classmethod
    def setUpClass(cls):
        cls.db = core.load_db()

    def test_missing_rates_reports_uncovered_not_exempt(self):
        # 8507.60.00 在 ANNEX II Part A 通用豁免清单内；抽掉税率表后，
        # 无法确定该经济体是否在 60 名单，应报"数据未覆盖"而非"豁免"
        db = dict(self.db)
        db["flip301"] = {}
        db["flip301_headings"] = {}
        pct, note, _src, spec = core.flip301_judge(db, "CN", code8="85076000")
        self.assertEqual(pct, "")
        self.assertIn("数据未覆盖", note)
        self.assertNotIn("豁免", pct)

    def test_official_headings_beat_json(self):
        # 档位以 htsdata.csv 推导的官方标目为主：只抽掉手抄 JSON 仍能判定
        db = dict(self.db)
        db["flip301"] = {}
        pct, note, src, spec = core.flip301_judge(db, "VN", code8="61091000")
        self.assertEqual(pct, "+12.5%")
        self.assertEqual(spec["heading"], "99030584")
        self.assertEqual(src["key"], "htsdata")           # 来源直指官方表那一行
        self.assertRegex(src["位置"], r"第 \d+ 行")

    def test_rates_present_still_exempt(self):
        # 数据齐全时豁免判定不受影响（回归）。用无范围限制的条目——
        # 8507.60.00 标 Aircraft，本就不该是无条件豁免。
        pct, note, _src, spec = core.flip301_judge(self.db, "CN", code8="02012002")
        self.assertEqual(pct, "豁免")
        self.assertIn("ANNEX II", note)


class TestBatchStats(unittest.TestCase):
    """统计口径：命中加征 / 命中但豁免 0% / 未命中 / 无法判定 / 不适用 分开计数"""

    @classmethod
    def setUpClass(cls):
        cls.db = core.load_db()

    def test_categories_sum_to_total(self):
        codes = ["85076000", "01012100", "85414300", "63079098", "6307909899"]
        _results, stats = core.batch_query(self.db, codes, origin="CN")
        parts = ("hit", "hit_exempt", "hit_unresolved", "miss",
                 "undetermined", "not_applicable")
        for k in parts:
            self.assertIn(k, stats)
        self.assertEqual(sum(stats[k] for k in parts), stats["total"])

    def test_exempt_not_counted_as_miss(self):
        # 构造一个"命中清单但 0% 豁免"的结果，确认它既不算 hit 也不算 miss
        results, stats = core.batch_query(self.db, ["85076000"], origin="CN")
        judged = results[0]["301判定"]
        if judged == "是(豁免/0%)":
            self.assertEqual(stats["hit_exempt"], 1)
            self.assertEqual(stats["miss"], 0)
            self.assertEqual(stats["hit"], 0)
        else:  # 该编码当前为实际加征
            self.assertEqual(stats["hit"], 1)
            self.assertEqual(stats["hit_exempt"], 0)

    def test_non_china_counted_as_not_applicable(self):
        _results, stats = core.batch_query(self.db, ["85076000", "01012100"], origin="VN")
        self.assertEqual(stats["not_applicable"], 2)
        self.assertEqual(stats["miss"], 0)
        self.assertEqual(stats["origin"], "越南")


class TestFlip301NetOfMfnAmount(unittest.TestCase):
    """
    net-of-MFN 档必须按 max(0, cap - MFN) 计算，不能把显示文本里的名义上限直接相加。

    此前的测试只断言 FLIP 301加征 == "+10%" 就算通过，从没断言过最终税额，
    这批 bug 正是从这个缝里漏过去的——所以这里一律断言数值。
    """

    @classmethod
    def setUpClass(cls):
        cls.db = core.load_db()

    def test_mfn_above_cap_means_zero_flip(self):
        # 6109.10.00 MFN 16.5% ≥ 上限 → FLIP 实际加征 0，总额就是 MFN
        for origin, cap in (("EU", 10.0), ("TW", 10.0), ("JP", 12.5), ("KR", 12.5)):
            r = rate.calc_total(self.db, "61091000", origin=origin)
            self.assertEqual(r["基础等效从价"], "16.5%")
            self.assertEqual(r["FLIP 301加征数值"], 0.0, f"{origin} 不应在 MFN 已达上限时再加")
            self.assertIn("16.5%", r["总税负估算"])

    def test_mfn_below_cap_tops_up_to_cap(self):
        # 0101.90.40 MFN 4.5% < 上限 → 补足到上限
        eu = rate.calc_total(self.db, "01019040", origin="EU")
        self.assertEqual(eu["FLIP 301加征数值"], 5.5)     # 10 - 4.5
        self.assertIn("10%", eu["总税负估算"])
        jp = rate.calc_total(self.db, "01019040", origin="JP")
        self.assertEqual(jp["FLIP 301加征数值"], 8.0)     # 12.5 - 4.5
        self.assertIn("12.5%", jp["总税负估算"])

    def test_flat_tier_unaffected(self):
        # flat 档仍是在 MFN 之上直接加，不封顶
        r = rate.calc_total(self.db, "61091000", origin="CA")
        self.assertEqual(r["FLIP 301加征数值"], 10.0)
        self.assertIn("26.5%", r["总税负估算"])          # 16.5 + 10

    def test_eu_member_gets_same_amount_as_eu(self):
        de = rate.calc_total(self.db, "01019040", origin="DE")
        eu = rate.calc_total(self.db, "01019040", origin="EU")
        self.assertEqual(de["FLIP 301加征数值"], eu["FLIP 301加征数值"])
        self.assertEqual(de["总税负估算"], eu["总税负估算"])

    def test_net_mfn_unresolvable_mfn_is_not_silently_zero(self):
        # MFN 是复合税且未给货值 → 封顶算不出来，必须标"需人工"而不是当作 0
        r = rate.calc_total(self.db, "04022950", origin="EU")
        self.assertIn("需人工", r["总税负估算"])


class TestSec301TenDigit(unittest.TestCase):
    """USTR 清单中精确到 10 位统计后缀的子目，不能截断成 8 位后合并"""

    @classmethod
    def setUpClass(cls):
        cls.db = core.load_db()

    def test_ten_digit_exact_match(self):
        # 6307.90.98.42 → 9903.91.07 (+50%)，与同前缀其他后缀的 +7.5% 不同。
        # 截断成 8 位再 setdefault 会让它拿到 +7.5%，漏征 42.5 个百分点。
        r = core.query_one(self.db, "6307909842", origin="CN")
        self.assertEqual(r["301判定"], "是")
        self.assertEqual(r["9903子目"], "9903.91.07")
        self.assertEqual(r["301加征"], "+50%")
        t = rate.calc_total(self.db, "6307909842", origin="CN")
        self.assertEqual(t["301加征数值"], 50.0)

    def test_sibling_suffix_keeps_own_tier(self):
        r = core.query_one(self.db, "6307909825", origin="CN")
        self.assertEqual(r["9903子目"], "9903.88.15")
        self.assertEqual(r["301加征"], "+7.5%")

    def test_unlisted_suffix_is_miss(self):
        # 清单只列特定后缀，未列出的后缀不在清单内
        r = core.query_one(self.db, "6307909899", origin="CN")
        self.assertEqual(r["301判定"], "否")
        self.assertEqual(r["301加征"], "")

    def test_eight_digit_alone_is_undetermined(self):
        # 只给 8 位无法判定档位，必须要求补全而不是猜一个
        r = core.query_one(self.db, "63079098", origin="CN")
        self.assertEqual(r["301判定"], "无法判定")
        self.assertIn("10 位", r["备注"])
        self.assertEqual(r["301加征"], "")

    def test_ordinary_eight_digit_unaffected(self):
        for code, c99, pct in (("85076000", "9903.91.06", "+25%"),
                               ("01012100", "9903.88.15", "+7.5%")):
            r = core.query_one(self.db, code, origin="CN")
            self.assertEqual(r["9903子目"], c99)
            self.assertEqual(r["301加征"], pct)


class TestBuildSanityCheck(unittest.TestCase):
    """构建产出下限校验：解析失配导致产出为空时必须失败，不能照常写库"""

    def test_empty_mapping_fails(self):
        import build_db
        failures = build_db.sanity_check(
            {"rates_8": 0, "desc_10": 0, "sec301_map": 0, "c99_percent": 0,
             "flip301_headings": 0, "c99_headings": 0})
        self.assertEqual(len(failures), len(build_db.SANITY_MINIMUMS))
        self.assertTrue(any("sec301_map" in f for f in failures))

    def test_current_build_passes(self):
        import build_db
        db = core.load_db()
        self.assertEqual(build_db.sanity_check({
            "rates_8": len(db["rates_8"]),
            "desc_10": len(db["desc_10"]),
            "sec301_map": len(db["sec301_map"]),
            "c99_percent": len(db["c99_percent"]),
            "flip301_headings": len(db["flip301_headings"]["by_origin"]),
            "c99_headings": len(db["c99_headings"]),
        }), [])

    def test_partial_drop_detected(self):
        # 只掉一半也要被拦下（下限留了约 15% 余量，掉 50% 必然触发）
        import build_db
        db = core.load_db()
        failures = build_db.sanity_check({
            "rates_8": len(db["rates_8"]) // 2,
            "desc_10": len(db["desc_10"]),
            "sec301_map": len(db["sec301_map"]),
            "c99_percent": len(db["c99_percent"]),
            "flip301_headings": len(db["flip301_headings"]["by_origin"]),
            "c99_headings": len(db["c99_headings"]),
        })
        self.assertTrue(any("rates_8" in f for f in failures))


class TestExtractCodes(unittest.TestCase):
    """编码提取：不能静默丢弃无法采用的输入，也不能把单号/日期当成编码"""

    @classmethod
    def setUpClass(cls):
        cls.db = core.load_db()

    def _issues(self, text):
        return {i["原文"]: i for i in core.extract_codes_detailed(text, self.db)[1]}

    def test_valid_codes_extracted(self):
        codes, issues = core.extract_codes_detailed(
            "8507.60.00, 6204.69.45, 6307.90.9842", self.db)
        self.assertEqual(codes, ["85076000", "62046945", "6307909842"])
        self.assertEqual(issues, [])

    def test_leading_zero_stripped_by_excel(self):
        # Excel 按数值存储会把 0101.21.00 变成 1012100，此前直接消失
        codes, issues = core.extract_codes_detailed("1012100", self.db)
        self.assertEqual(codes, [])
        self.assertEqual(len(issues), 1)
        self.assertIn("0101.21.00", issues[0]["建议"])
        self.assertIn("已在税则表中", issues[0]["建议"])

    def test_six_digit_heading_reported(self):
        issues = self._issues("090111")
        self.assertIn("090111", issues)
        self.assertIn("补全至 8 位", issues["090111"]["建议"])

    def test_order_number_not_treated_as_code(self):
        # 'INV-20260827' 的数字段位数恰好是 8，此前会被当成编码送去查询
        codes, issues = core.extract_codes_detailed("发票号 INV-20260827", self.db)
        self.assertEqual(codes, [])
        self.assertEqual(len(issues), 1)
        self.assertIn("不在 2026 现行 HTS 税则表内", issues[0]["原因"])

    def test_ordinary_numbers_ignored(self):
        # 单价、数量这类短数字不该产生噪音提示
        codes, issues = core.extract_codes_detailed("单价 10.50 USD 数量 1200 件", self.db)
        self.assertEqual(codes, [])
        self.assertEqual(issues, [])

    def test_dedupe_preserves_order(self):
        codes, _ = core.extract_codes_detailed(
            "8507.60.00, 6204.69.45, 8507.60.00", self.db)
        self.assertEqual(codes, ["85076000", "62046945"])

    def test_backward_compatible_wrapper(self):
        # extract_codes 仍返回纯列表，老调用方不受影响
        self.assertEqual(core.extract_codes("8507.60.00, 6204.69.45"),
                         ["85076000", "62046945"])

    def test_without_db_no_table_validation(self):
        # 不传 db 时不做税则表校验，行为与此前一致（不误报）
        codes, issues = core.extract_codes_detailed("8507.60.00")
        self.assertEqual(codes, ["85076000"])
        self.assertEqual(issues, [])


class TestMeasuresConfigCache(unittest.TestCase):
    """配置缓存：命中缓存不重复读盘，但文件改动后立即失效"""

    def setUp(self):
        core._clear_measures_cache()

    def tearDown(self):
        core._clear_measures_cache()

    def test_cache_hit_avoids_reread(self):
        first = core.load_measures_config()
        self.assertIsNotNone(core._measures_cache)
        second = core.load_measures_config()
        self.assertEqual(first, second)

    def test_returned_dict_is_isolated(self):
        # 调用方修改返回值不得污染缓存
        cfg = core.load_measures_config()
        cfg["cn301"] = not cfg["cn301"]
        self.assertNotEqual(cfg["cn301"], core.load_measures_config()["cn301"])

    def test_save_invalidates_cache(self):
        original = core.load_measures_config()
        try:
            core.save_measures_config({"cn301": not original["cn301"]})
            self.assertEqual(
                core.load_measures_config()["cn301"], not original["cn301"])
        finally:
            core.save_measures_config(original)
        self.assertEqual(core.load_measures_config(), original)


if __name__ == "__main__":
    unittest.main()


class TestSec301Exclusions(unittest.TestCase):
    """
    301 排除（U.S. note 20）判定。

    此前工具完全没有这一层：命中 301 清单就报满额加征。而 USTR 的排除是逐条授予的，
    9025.19.80.85（温度计）整个统计号列在 note 20(vvv)(ii) 第 (3) 项，报关填
    9903.88.69 即免掉 25%——工具却一直报 +25%，是让客户白交钱的方向。
    """

    @classmethod
    def setUpClass(cls):
        cls.db = core.load_db()

    def test_full_exclusion_zeroes_301(self):
        """整号排除 + 当日生效 → 加征归零，9903 子目改成排除标目"""
        r = core.query_one(self.db, "9025198085", origin="CN")
        self.assertEqual(r["301判定"], "是(已排除)")
        self.assertEqual(r["301加征"], "0%(排除)")
        self.assertEqual(r["9903子目"], "9903.88.69")     # 报关要改填这个
        self.assertIn("已排除", r["301排除"])
        self.assertIn("2026-11-09", r["301排除"])
        self.assertIn("9903.88.69", r["备注"])
        # 税额真的要少这 25%：Free + 0 + FLIP 12.5%
        t = rate.calc_total(self.db, "9025198085", unit_value=10, origin="CN")
        self.assertEqual(t["301加征数值"], 0.0)
        self.assertIn("12.5%", t["总税负估算"])

    def test_sibling_suffix_not_excluded(self):
        """
        排除授予到 10 位统计号。同一 8 位子目下没被列入的后缀照常加征——
        整号排除绝不能外溢到兄弟后缀，否则会把该交的税判没了。
        """
        r = core.query_one(self.db, "9025198030", origin="CN")
        self.assertEqual(r["301判定"], "是")
        self.assertEqual(r["301加征"], "+25%")
        self.assertIn("待核", r["301排除"])

    def test_eight_digit_names_the_excluded_stat_numbers(self):
        """
        只给 8 位时不能判免（不知道具体统计号），但必须把被整号排除的后缀点出来。
        只说"有 3 条待核"，用户不会想到其中某个后缀是无条件全免的，仍会按满额报关。
        """
        r = core.query_one(self.db, "90251980", origin="CN")
        self.assertEqual(r["301加征"], "+25%")
        self.assertIn("整号", r["301排除"])
        for suffix in ("9025.19.80.10", "9025.19.80.20", "9025.19.80.85"):
            self.assertIn(suffix, r["备注"])

    def test_described_exclusion_never_auto_applies(self):
        """
        按产品描述授予的排除不能按编码自动判免——同一税号下有的款符合有的不符合，
        自动判免会直接造出错误申报。只提示 + 给原文。
        """
        r = core.query_one(self.db, "3906905000", origin="CN")
        self.assertEqual(r["301加征"], "+25%")
        self.assertIn("描述", r["301排除"])
        live = [x for x in r["301排除明细"] if x["状态"] == "生效中"]
        self.assertTrue(live)
        self.assertTrue(all(x["覆盖方式"] == "按描述排除" for x in live))
        self.assertTrue(all(x["描述"] for x in live), "按描述排除必须带原文，否则无从核对")

    def test_expired_exclusion_does_not_apply(self):
        """
        已过期的排除只能作历史提示。9903.88.66/.67/.68 都已到期，
        它们在 c99_percent 里同样是 0.0，接进判定链时若不看有效期就会把过期排除算成免税。
        """
        r = core.query_one(self.db, "9025198085", origin="CN")
        expired = [x for x in r["301排除明细"] if x["状态"] == "已过期"]
        self.assertTrue(expired, "该编码历史上有过期排除，应作为历史列出")
        # 生效的那条必须来自仍在有效期内的标目
        auto = [x for x in r["301排除明细"]
                if x["状态"] == "生效中" and x["覆盖方式"] == "整号排除"]
        self.assertTrue(auto)
        self.assertEqual(auto[0]["9903子目"], "9903.88.69")

    def test_status_recomputed_at_query_time(self):
        """
        有效期状态必须按**查询当天**算，不能用提取那天的快照。
        9903.88.69/.70 都在 2026-11-09 到期，用快照迟早把过期的说成有效。
        """
        notes = (self.db.get("exclusions") or {}).get("notes") or {}
        n69 = notes.get("99038869")
        self.assertIsNotNone(n69)
        self.assertEqual(core._excl_status(n69, "2026-09-03"), "生效中")
        self.assertEqual(core._excl_status(n69, "2026-11-09"), "生效中")   # 含当日
        self.assertEqual(core._excl_status(n69, "2026-11-10"), "已过期")
        self.assertEqual(core._excl_status(n69, "2024-06-14"), "未生效")

    def test_no_dates_never_auto_applies(self):
        """
        老排除标目在 htsdata.csv 里不带 Effective 字样。"没有日期"不等于"长期有效"——
        这些标目 2020 年就废止了，默认成生效中会把过期排除算成免税。
        """
        self.assertEqual(
            core._excl_status({"effective_from": "", "effective_to": ""}, "2026-09-03"),
            "有效期未标注")

    def test_non_china_origin_has_no_exclusion(self):
        """301 与其排除都只针对中国原产"""
        r = core.query_one(self.db, "9025198085", origin="VN")
        self.assertNotIn("已排除", r.get("301排除", "") or "")

    def test_search_rows_agree_with_query(self):
        """
        搜索表此前自建 301 判定、不认排除，会显示 "+25%" 而查询页显示 "0%(排除)"。
        同一个编码在两处给出不同税负，比两处都错更难发现。
        """
        rows = rate.search(self.db, "9025.19.80", limit=3, sort="relevance", origin="CN")
        row = next(r for r in rows if r["编码"] == "9025.19.80")
        q = core.query_one(self.db, "90251980", origin="CN")
        self.assertEqual(row["301加征"], q["301加征"])
        self.assertEqual(row["301排除"], q["301排除"])


class TestOriginSemantics(unittest.TestCase):
    """
    原产地口径（2026-09 评估修的几处会直接算错的地方）：
      - CHN / 中文名 / 空值 都要归一，301 与 FLIP 用同一个结果
      - 「未指定」与「其他国家」是两个回答
      - 第二栏国家按第二栏税率
      - Special 栏只提示不套用
      - 以该原产地为条件的未建模 9903 标目要探测出来，总税负标不完整
    """

    @classmethod
    def setUpClass(cls):
        cls.db = core.load_db()

    def test_alias_chn_gets_301(self):
        # 此前 CHN 走 FLIP 不走 301：得到"301 不适用 + FLIP 12.5%"这种自相矛盾的答案
        a = core.query_one(self.db, "61091000", origin="CHN")
        b = core.query_one(self.db, "61091000", origin="CN")
        self.assertEqual(a["301判定"], "是")
        self.assertEqual(a["301加征"], b["301加征"])
        self.assertEqual(a["原产地代码"], "CN")
        self.assertEqual(core.normalize_origin("墨西哥"), "MX")
        self.assertEqual(core.normalize_origin("mex"), "MX")
        self.assertEqual(core.normalize_origin(""), core.ORIGIN_UNSPECIFIED)
        self.assertEqual(core.normalize_origin("other"), core.ORIGIN_UNSPECIFIED)

    def test_unspecified_is_explicit(self):
        # 未指定：只按 MFN，备注与 FLIP 说明都要明说"未计入"，不能说成"不在名单"
        for o in ("", "OTHER", "未指定"):
            r = core.query_one(self.db, "61091000", origin=o)
            self.assertEqual(r["原产地"], "未指定")
            self.assertEqual(r["301判定"], "不适用（未指定原产地）")
            self.assertEqual(r["FLIP 301加征"], "")
            self.assertIn("未指定原产地", r["FLIP 301说明"])
            self.assertNotIn("不在 FLIP 301", r["FLIP 301说明"])
            self.assertIn("未计入", r["备注"])
            self.assertEqual(r["未建模措施"], [])
        t = rate.calc_total(self.db, "61091000", origin="OTHER")
        self.assertTrue(t["总税负估算"].startswith("16.5%"))

    def test_other_listed_country_is_not_unspecified(self):
        # XX / 不在名单的国家：明确"不在 60 名单"，与未指定分开
        r = core.query_one(self.db, "61091000", origin="KE")
        self.assertEqual(r["原产地"], "其他国家")
        self.assertIn("不在 FLIP 301", r["FLIP 301说明"])
        self.assertIn("其他国家通用轨道", r["越南措施"])

    def test_column2_origin_uses_col2(self):
        # 俄罗斯 2022 起适用第二栏：6109.10.00 第二栏 90%，一般 16.5%
        r = core.query_one(self.db, "61091000", origin="RU")
        self.assertEqual(r["基础税率栏"], "第二栏")
        self.assertEqual(r["适用基础税率"], "90%")
        self.assertIn("第二栏", r["备注"])
        t = rate.calc_total(self.db, "61091000", origin="RU")
        self.assertEqual(t["基础等效从价"], "90%")
        self.assertTrue(t["总税负估算"].startswith("102.5%"), t["总税负估算"])  # 90 + FLIP 12.5
        # 中国照旧走一般税率
        self.assertEqual(core.query_one(self.db, "61091000", origin="CN")["基础税率栏"], "一般税率")

    def test_special_column_hint_not_applied(self):
        # 墨西哥：Special 栏 Free (…S…) 只提示，总税负仍按一般税率
        r = core.query_one(self.db, "61091000", origin="MX")
        self.assertIn("USMCA", r["特殊税率提示"])
        self.assertIn("未自动套用", r["特殊税率提示"])
        self.assertIn("Special 栏", r["备注"])
        t = rate.calc_total(self.db, "61091000", origin="MX")
        self.assertEqual(t["基础等效从价"], "16.5%")
        # 韩国：KR 代码；中国 / 未指定：不提示
        self.assertIn("美韩", core.query_one(self.db, "61091000", origin="KR")["特殊税率提示"])
        self.assertEqual(core.query_one(self.db, "61091000", origin="CN")["特殊税率提示"], "")
        self.assertEqual(core.query_one(self.db, "61091000", origin="OTHER")["特殊税率提示"], "")

    def test_unmodeled_measures_detected_for_origin(self):
        # 墨西哥：9903.01（note 2）里有提及墨西哥的标目，工具未建模 → 探测出来并标不完整
        r = core.query_one(self.db, "61091000", origin="MX")
        groups = {u["标目组"]: u for u in r["未建模措施"]}
        self.assertIn("9903.01", groups)
        self.assertIn("U.S. note 2", groups["9903.01"]["依据"])
        self.assertTrue(groups["9903.01"]["示例"])
        self.assertIn("总税负不完整", r["备注"])
        t = rate.calc_total(self.db, "61091000", origin="MX")
        self.assertIn("不含 AD/CVD", t["总税负估算"])
        self.assertIn("原产地类未建模标目待核", t["总税负估算"])
        self.assertIn("未建模措施（探测，需人工核实）", {s["类型"] for s in r["来源"]})
        # 中国也有 note 2 的标目提及中国
        self.assertIn("9903.01", {u["标目组"] for u in
                                  core.query_one(self.db, "61091000", origin="CN")["未建模措施"]})

    def test_total_text_always_states_exclusions(self):
        # 总税负文本常驻"不含"说明：AD/CVD 没数据、232 未建模
        t = rate.calc_total(self.db, "85076000", origin="CN")
        self.assertRegex(t["总税负估算"], r"^\d")
        self.assertIn("不含 AD/CVD、232", t["总税负估算"])
        self.assertEqual(rate._total_num(t["总税负估算"]), 40.9)   # 3.4 + 25 + 12.5

    def test_origin_options_cover_flip_economies(self):
        opts = core.origin_options(self.db)
        codes = [o["code"] for o in opts]
        self.assertEqual(codes[:2], ["CN", "VN"])
        self.assertEqual(codes[-2:], [core.ORIGIN_OTHER_LISTED, core.ORIGIN_UNSPECIFIED])
        for c in ("MX", "EU", "JP", "KR", "TW", "GB", "RU", "BY", "CU", "KP"):
            self.assertIn(c, codes)
        by = {o["code"]: o for o in opts}
        self.assertEqual(by["MX"]["flip301"], "+10%")
        self.assertEqual(by["EU"]["flip301"], "≤+10%")
        self.assertTrue(by["RU"]["column2"])
        self.assertEqual(len(codes), len(set(codes)))


class TestFlipOfficialHeadings(unittest.TestCase):
    """FLIP 301 档位与报关标目从 htsdata.csv 的 9903.05/.06 标目推导，手抄 JSON 只作校验"""

    @classmethod
    def setUpClass(cls):
        cls.db = core.load_db()

    def test_derived_table_matches_json(self):
        fh = self.db["flip301_headings"]["by_origin"]
        self.assertEqual(len(fh), 60)
        rates = self.db["flip301"]["rates"]
        for o in rates["10"]:
            self.assertEqual((fh[o]["mode"], fh[o]["rate"]), ("flat", 10.0), o)
        for o in rates["125"]:
            self.assertEqual((fh[o]["mode"], fh[o]["rate"]), ("flat", 12.5), o)
        for o in rates["net_mfn_10"]:
            self.assertEqual((fh[o]["mode"], fh[o]["cap"]), ("net_mfn", 10.0), o)
        for o in rates["net_mfn_125"]:
            self.assertEqual((fh[o]["mode"], fh[o]["cap"]), ("net_mfn", 12.5), o)
        self.assertEqual(fh["CN"]["heading"], "99030531")
        self.assertEqual(fh["HK"]["heading"], "99030543")
        self.assertEqual(fh["VN"]["heading"], "99030584")

    def test_heading_in_result_and_calc(self):
        # 8507.60.00 命中 ANNEX II 但带 Aircraft 范围限制：说明讲范围，标目走 fallback 档
        r = core.query_one(self.db, "85076000", origin="CN")
        self.assertEqual(r["FLIP 301标目"], "9903.05.31")
        # 普通命中（6109.10.00）：说明里直接写报关标目
        r2 = core.query_one(self.db, "61091000", origin="CN")
        self.assertEqual(r2["FLIP 301标目"], "9903.05.31")
        self.assertIn("报关标目 9903.05.31", r2["FLIP 301说明"])
        # net-of-MFN：calc_total 知道 MFN 后收成一个标目。EU 产 6109.10.00 MFN 16.5% ≥ 10 → .38
        t = rate.calc_total(self.db, "61091000", origin="EU")
        self.assertEqual(t["FLIP 301标目"], "9903.05.38")
        self.assertEqual(t["FLIP 301加征数值"], 0.0)
        # MFN 3.4% < 10 → .39，实际加 6.6
        t2 = rate.calc_total(self.db, "85076000", origin="EU")
        self.assertEqual(t2["FLIP 301标目"], "9903.05.39")
        self.assertEqual(t2["FLIP 301加征数值"], 6.6)

    def test_exceptions_listed_in_sources(self):
        r = core.query_one(self.db, "85076000", origin="MX")
        srcs = {s["类型"]: s for s in r["来源"]}
        self.assertIn("FLIP 301 例外标目（条件需人工核对）", srcs)
        self.assertIn("9903.05.94", srcs["FLIP 301 例外标目（条件需人工核对）"]["说明"])   # 墨西哥专属例外
        self.assertIn("9903.05.85", srcs["FLIP 301 例外标目（条件需人工核对）"]["说明"])   # 通用在途例外
