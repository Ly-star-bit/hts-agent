# -*- coding: utf-8 -*-
"""
test_c99_products.py —— Chapter 99 U.S. notes 产品范围提取（scripts/extract_c99_products.py）

合成文本部分不依赖 PDF：子目区间、10 位统计号、多列清单行、(a)/(i)/(1)/(A) 子条嵌套、
(i)/(v) 字母与罗马的歧义、note 起点的递增约束。集成用例只在本地有 Chapter 99 PDF 时跑，
断言各 note 的产出下限——源文件换版导致解析失配时，这里先于 build 发现。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import extract_c99_products as x  # noqa: E402


class TestLineCodes(unittest.TestCase):
    def test_list_line_columns(self):
        """清单行多列：4/6/8/10 位混排，10 位可不带第四个点"""
        line = "7601 7302.10 7216.10.00 7616.99.5160 9401.61.40.11"
        self.assertTrue(x.is_code_only_line(line))
        codes, heads, hr = x.parse_line_codes(line, True)
        self.assertEqual([c["code"] for c in codes],
                         ["7601", "730210", "72161000", "7616995160", "9401614011"])
        self.assertEqual([c["kind"] for c in codes], ["prefix", "prefix", "exact", "exact", "exact"])
        self.assertEqual(heads, [])

    def test_range_kept_not_expanded(self):
        codes, _, _ = x.parse_line_codes("8701 to 8705", True)
        self.assertEqual(codes, [{"code": "8701", "digits": 4, "kind": "range", "to": "8705"}])
        codes, _, _ = x.parse_line_codes("7206.10 through 7216.50", True)
        self.assertEqual((codes[0]["code"], codes[0]["to"]), ("720610", "721650"))

    def test_fake_range_rejected(self):
        """位数不等或后小于前的不是区间（年份、页码、'12 to 15 percent'）"""
        codes, _, _ = x.parse_line_codes("entered from 2024 to 2026 under 8544.42.20", True)
        self.assertEqual([c["kind"] for c in codes if c["code"].startswith("8544")], ["exact"])

    def test_prose_line_skips_4_digit_and_chapter_98_99(self):
        """正文里的 4 位数是年份/公告号；9802 是 98 章条款；9903 归入 headings"""
        line = ("described in subheading 7614.10.50), Proclamation 10925 of April 29, 2025, "
                "except that duties under subheading 9802.00.60 and heading 9903.85.03 apply")
        self.assertFalse(x.is_code_only_line(line))
        codes, heads, _ = x.parse_line_codes(line, False)
        self.assertEqual([c["code"] for c in codes], ["76141050"])
        self.assertEqual(heads, ["9903.85.03"])

    def test_sentence_final_period(self):
        """'provided for in subheading 7616.99.51.' 句末句号不能吃掉编码"""
        codes, _, _ = x.parse_line_codes("castings and forgings of aluminum provided for in subheading 7616.99.51.", False)
        self.assertEqual([c["code"] for c in codes], ["76169951"])

    def test_heading_ranges(self):
        _, heads, hr = x.parse_line_codes("headings 9903.82.04–9903.82.26 and 9903.82.02 apply", False)
        self.assertEqual(hr, ["9903.82.04–9903.82.26"])
        self.assertEqual(heads, ["9903.82.02"])


class TestSubdivisionTracker(unittest.TestCase):
    def test_letters_then_roman(self):
        """(c) 之后的 (i) 是罗马小节；(h) 之后的 (i) 是字母子条"""
        t = x.SubdivisionTracker()
        t.feed("(c) Headings apply to the following lists:")
        t.feed("(i) Articles of aluminum:")
        self.assertEqual(t.key(), "(c)(i)")
        t.feed("(ii) Derivative aluminum articles:")
        self.assertEqual(t.key(), "(c)(ii)")
        t.feed("(d) Headings 9903.82.04 apply")
        self.assertEqual(t.key(), "(d)")
        for letter in "efgh":
            t.feed(f"({letter}) text")
        t.feed("(i) Heading 9903.82.19 applies to limited quantities")
        self.assertEqual(t.key(), "(i)")

    def test_digit_and_roman_nesting(self):
        """note 52 的 (j)(4)(ii) 与 note 2 的 (v)(iii)(a)"""
        t = x.SubdivisionTracker()
        t.feed("(j) (1) As provided in heading 9903.05.96")
        self.assertEqual(t.key(), "(j)(1)")
        t.feed("(4) As provided in heading 9903.05.99")
        t.feed("(i) the duty shall not apply to")
        self.assertEqual(t.key(), "(j)(4)(i)")
        t.feed("(ii) the duty shall not apply to")
        self.assertEqual(t.key(), "(j)(4)(ii)")
        t.feed("(k) As provided in headings 9903.05.38")
        self.assertEqual(t.key(), "(k)")
        t2 = x.SubdivisionTracker()
        for m in ("(u)", "(v)", "(iii)", "(a)", "(b)"):
            t2.feed(f"{m} text")
        self.assertEqual(t2.key(), "(v)(iii)(b)")

    def test_long_roman_and_bracket_words(self):
        """(xxiv) 这类 4 位以上罗马数也是小节；"(see)" 不是标记"""
        t = x.SubdivisionTracker()
        t.feed("(u) For the purposes of heading 9903.01.24")
        t.feed("(v) As provided in heading 9903.01.25")      # (u) 之后的 (v) 是字母
        t.feed("(xxi) As provided in heading 9903.02.75")
        t.feed("(1) Essential oils (classifiable in subheading 3301.29.51)")
        self.assertEqual(t.key(), "(v)(xxi)(1)")
        t.feed("(xxiv)")
        self.assertEqual(t.key(), "(v)(xxiv)")
        t.feed("(a) As provided in headings 9903.02.79 and 9903.02.80, South Korea")
        self.assertEqual(t.key(), "(v)(xxiv)(a)")
        t.feed("(see) the note above")
        self.assertEqual(t.key(), "(v)(xxiv)(a)")
        t.feed("(xxviii) Zimbabwe")
        self.assertEqual(t.key(), "(v)(xxviii)")

    def test_upper_case_items(self):
        t = x.SubdivisionTracker()
        t.feed("(m) (A) Effective with respect to goods")
        self.assertEqual(t.key(), "(m)(A)")
        t.feed("(B) other goods")
        self.assertEqual(t.key(), "(m)(B)")


class TestNoteSlicing(unittest.TestCase):
    def _pages(self):
        return [
            (1, ["SUBCHAPTER III", "U.S. Notes", "1. This subchapter contains temporary modifications.",
                 "2.", "(a) For the purposes of heading 9903.01.01, products of Mexico",
                 "shall be subject to an additional 25% ad valorem rate of duty."]),
            (2, ["3. For the purposes of subheadings 9903.41.05", "5. The following provisions have been suspended",
                 "16. (a) Except as provided in headings 9903.82.01, headings 9903.82.02 provide the",
                 "ordinary customs duty treatment of certain articles of aluminum, of steel, or of copper",
                 "(c) Headings apply to:", "(i) Articles of aluminum:", "7601 7604 7605 7606",
                 "(ii) Derivative aluminum articles:", "7308.20.0035 7610.10.00",
                 "2. Machines for the reception (this is an exclusion item, not note 2)",
                 "17. (a) Subheadings 9903.45.01 establish"]),
            (3, ["Heading/ Stat. Unit Rates of Duty", "9903.01.01 1/ Except for products"]),
        ]

    def test_sequential_note_starts_ignore_list_items(self):
        pages = self._pages()
        start, end = x.subchapter_iii_span(pages)
        self.assertEqual((start, end), (1, 3))
        starts = x.find_note_starts(pages, start, end)
        self.assertEqual(sorted(starts), [1, 2, 3, 5, 16, 17])
        self.assertEqual(starts[2], (1, 3))
        sliced = x.slice_notes(pages, starts, end)
        self.assertEqual(sliced[2][0], (1, ""))              # "2." 单独成行 → 正文从 (a) 开始
        self.assertEqual(sliced[2][1][1][:31], "(a) For the purposes of heading")
        self.assertIn("2. Machines", " ".join(s for _, s in sliced[16]))  # 顺序号条目留在 note 16 里

    def test_extract_note_groups_and_basis(self):
        pages = self._pages()
        start, end = x.subchapter_iii_span(pages)
        sliced = x.slice_notes(pages, x.find_note_starts(pages, start, end), end)
        d = x.extract_note(16, sliced[16], x.TARGET_NOTES["16"])
        self.assertTrue(d["basis_verified"])
        self.assertEqual(d["groups"]["(c)(i)"]["list"], ["7601", "7604", "7605", "7606"])
        self.assertEqual(d["groups"]["(c)(ii)"]["list"], ["7308200035", "76101000"])
        self.assertEqual(d["count"], 6)
        self.assertIn("9903.82.01", d["headings"])

    def test_split_by_subdivision_keeps_prose_drops_lists(self):
        body = [(5, "(b) As provided in heading 9903.05.86, the duties shall not apply to articles"),
                (5, "classifiable in the following provisions of the HTSUS:"),
                (6, "0201.10.05 0201.10.10 0201.10.50"),
                (6, "(c) As provided in heading 9903.05.87, the duties shall not apply to the products")]
        out = x.split_note_by_subdivision(body)
        self.assertEqual(out["(b)"]["codes_count"], 3)
        self.assertNotIn("0201.10.05", out["(b)"]["text"])
        self.assertIn("shall not apply to articles", out["(b)"]["text"])
        self.assertEqual(out["(b)"]["pages"], [5, 6])
        self.assertEqual(out["(c)"]["pages"], [6])


class TestSanity(unittest.TestCase):
    def test_zero_and_missing_are_named(self):
        result = {"notes": {"16": {"count": 0, "basis_verified": True}}}
        problems = x.sanity_check(result)
        self.assertTrue(any(p.startswith("note 16: 提取 0 条") for p in problems))
        self.assertTrue(any("note 33: 未定位到" == p for p in problems))

    def test_status_sentences(self):
        text = ("Headings 9903.85.01–9903.85.15 are terminated as of April 6, 2026. "
                "Other text here. Headings 9903.03.01–9903.03.11 expired at the close of July 23, 2026. "
                "Nothing else.")
        got = x.status_sentences(text)
        self.assertEqual(len(got), 2)
        self.assertIn("terminated as of April 6, 2026", got[0])


@unittest.skipUnless(os.path.exists(x.CH99_PDF), "本地没有 Chapter 99 PDF")
class TestRealPdf(unittest.TestCase):
    """真实 PDF：各 note 产出下限 + 关键事实（跑一次约 40 秒，只在有 PDF 时执行）"""

    @classmethod
    def setUpClass(cls):
        cls.result, cls.note52 = x.extract()

    def test_minimums_pass(self):
        self.assertEqual(x.sanity_check(self.result), [])

    def test_note_16_lists(self):
        g = self.result["notes"]["16"]["groups"]
        self.assertIn("7206", g["(c)(iii)"]["list"])          # 钢材品目
        self.assertIn("7308200035", g["(c)(ii)"]["list"])     # 10 位衍生铝制品
        self.assertIn("85444290", g["(c)(vi)"]["list"])       # 衍生铝制品里的电线
        self.assertEqual(self.result["notes"]["39"]["groups"]["(b)"]["list"], ["847150", "847180", "847330"])

    def test_note_52_subdivisions_present(self):
        subs = self.note52["subdivisions"]
        for k in ("(a)", "(b)", "(c)", "(d)", "(e)", "(f)", "(g)", "(h)", "(i)", "(k)"):
            self.assertIn(k, subs, k)
        self.assertGreater(subs["(b)"]["codes_count"], 500)
        self.assertIn("9903.05.90", subs["(f)"]["text"])      # 232 产品例外标目

    def test_status_words_captured(self):
        s2 = " ".join(self.note52["note2_excerpt"]["status_sentences"])
        self.assertIn("expired at the close of July 23, 2026", s2)
        s19 = " ".join(self.result["notes"]["19"]["status_sentences"])
        self.assertIn("terminated as of April 6, 2026", s19)


if __name__ == "__main__":
    unittest.main()
