# -*- coding: utf-8 -*-
"""
test_exclusions_data.py —— 301 排除清单（data/sec301_exclusions.json）的数据不变量

这份数据是从 Chapter 99 PDF 解析出来的，解析过程踩过两个会**静默算错税**的坑，
都在这里钉住：

  ① 词组匹配遇上换行。正则里 "shall not apply to the following particular products"
     是字面空格，PDF 里这句常跨行。不压平全文就会漏掉 9 段清单，而漏掉的段会被并进
     前一段——不是少几条，是整条链的排除标目**错位一位**：9025.19.8085 会被记到
     9903.88.68（2024 年就过期）名下，于是"已排除"变成"过期排除"，税又报回 25%。
  ② 条目序号的远距离续接。最后一段清单没有下一个锚点兜底，会一路延伸到全书末尾，
     把税则表里巧合出现的 "(15)(16)…" 接着算成排除条目（实测 9903.88.70 多出 20 条）。

因此这里断言的不是"解析没报错"，而是**几个能对着官方原文数出来的确定数字**。
"""
import json
import os
import sys
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import core

EXCL_JSON = os.path.join(BASE, "data", "sec301_exclusions.json")


class TestExclusionData(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        with open(EXCL_JSON, encoding="utf-8-sig") as f:
            cls.d = json.load(f)
        cls.notes = cls.d["notes"]
        cls.by_code = cls.d["by_code"]

    def test_live_headings_are_exactly_69_and_70(self):
        """当前只有这两个排除标目还在有效期内，都到 2026-11-09 止"""
        live = sorted(c for c, v in self.notes.items() if v["status"] == "生效中")
        self.assertEqual(live, ["99038869", "99038870"])
        for c in live:
            self.assertEqual(self.notes[c]["effective_to"], "2026-11-09")

    def test_item_counts_match_official(self):
        """
        对着 FRN/HTS 原文数出来的条目数：20(vvv) 164 条 + 20(www) 14 条 = 178 条。
        数字一变就说明解析边界又漂了（漏段会少、越界续接会多）。
        """
        self.assertEqual(self.notes["99038869"]["item_count"], 164)
        self.assertEqual(self.notes["99038870"]["item_count"], 14)
        self.assertEqual(self.notes["99038869"]["item_count"]
                         + self.notes["99038870"]["item_count"], 178)

    def test_thermometer_full_exclusion_under_69(self):
        """
        锚定那个促成本功能的例子：9025.19.8085 必须是 9903.88.69 名下的**整号**排除，
        且来自 List 2（9903.88.02）。挂到 .68 就是错位，标成 described 就不会判免。
        """
        recs = [r for r in self.by_code["9025198085"] if r["c99"] == "99038869"]
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["covers"], "full")
        self.assertEqual(recs[0]["list_c99"], "99038802")
        self.assertEqual(recs[0]["item"], 3)

    def test_full_items_carry_no_description(self):
        """整号排除按定义不受描述限制；带了描述说明分类分错了"""
        for code, recs in self.by_code.items():
            for r in recs:
                if r["covers"] == "full":
                    self.assertEqual(r["desc"], "", f"{code} 的整号排除不该有描述")

    def test_described_items_always_carry_description(self):
        """按描述排除的原文是唯一判定依据，缺了这条排除就没法核对"""
        for code, recs in self.by_code.items():
            for r in recs:
                if r["covers"] == "described":
                    self.assertTrue(r["desc"], f"{code} 第 {r['item']} 项缺描述原文")

    def test_every_record_cites_a_page(self):
        """每条都要能说出 Chapter 99 PDF 第几页，否则无从复核"""
        for code, recs in self.by_code.items():
            for r in recs:
                self.assertTrue(r.get("page"), f"{code} 第 {r['item']} 项无页码")

    def test_codes_are_8_or_10_digit(self):
        for code in self.by_code:
            self.assertIn(len(code), (8, 10), f"异常编码长度：{code}")
            self.assertTrue(code.isdigit(), f"编码含非数字：{code}")

    def test_no_duplicate_record_per_code(self):
        """同一编码下不该出现完全相同的条目（同标目+同条目号+同源标目）"""
        for code, recs in self.by_code.items():
            keys = [(r["c99"], r["item"], r["list_c99"]) for r in recs]
            self.assertEqual(len(keys), len(set(keys)), f"{code} 有重复条目记录")

    def test_status_values_are_known(self):
        known = {"生效中", "已过期", "未生效", "有效期未标注"}
        self.assertEqual({v["status"] for v in self.notes.values()} - known, set())

    def test_db_carries_the_same_data(self):
        """
        判定读的是 sec301_db.json 里的副本。这份 JSON 改了不重跑 build_db.py，
        改动就不会生效——两边对不上时要能立刻发现。
        """
        db = core.load_db()
        ex = db.get("exclusions") or {}
        self.assertTrue(ex, "sec301_db.json 缺 exclusions 分区，请重跑 scripts/build_db.py")
        self.assertEqual(len(ex.get("by_code") or {}), len(self.by_code))
        self.assertEqual((ex.get("notes") or {}).get("99038869", {}).get("item_count"), 164)


if __name__ == "__main__":
    unittest.main()


class TestExclusionExpiry(unittest.TestCase):
    """排除到期告警：这是唯一会让工具**少报**的定时炸弹，三处都要报得出来"""

    @classmethod
    def setUpClass(cls):
        import core
        cls.db = core.load_db()

    def test_expiry_counts_down_from_query_day(self):
        import core
        e = core.exclusion_expiry(self.db, today="2026-10-30")
        self.assertIsNotNone(e, "应存在生效中且带到期日的排除标目")
        self.assertEqual(e["剩余天数"], 10)
        self.assertTrue(e["告警"], "剩 10 天应触发告警")
        self.assertTrue(e["标目"], "必须点名是哪几个标目到期")

    def test_no_warning_when_far_out(self):
        import core
        e = core.exclusion_expiry(self.db, today="2026-09-11")
        self.assertFalse(e["告警"], "剩 59 天不该天天弹告警")

    def test_all_expired_returns_none_not_silence(self):
        """
        全过期时 exclusion_expiry 返回 None——调用方不能把 None 当"安全"。
        check_sources 那条路径必须在此时喊得最响。
        """
        import core, check_sources
        self.assertIsNone(core.exclusion_expiry(self.db, today="2026-11-20"))
        msg = check_sources.exclusion_expiry_warning(today="2026-11-20")
        self.assertIn("已全部过期", msg)
        self.assertIn("少报", msg)

    def test_probe_warning_is_independent_of_source_updates(self):
        """官方不改版，排除照样会到期——告警不能挂在 updated 分支里"""
        import inspect, check_sources
        src = inspect.getsource(check_sources.main)
        expiry_at = src.index("expiry_msg = exclusion_expiry_warning()")
        notify_at = src.index("if a.notify:")
        self.assertLess(expiry_at, notify_at,
                        "到期判断应独立于 updated/errors，先算再决定怎么报")
