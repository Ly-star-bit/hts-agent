# -*- coding: utf-8 -*-
"""
正文金标的描述抽取（eval_classify.extract_description）：
  - 从"you requested a tariff classification ruling."之后取商品描述段
  - 剥掉前导套话（Additional information / No samples …）
  - 在"The applicable subheading"处截断，申请人点名的税号一律抹掉（否则金标就是漏答）
  - 超长按句截到 ≤400 字
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import eval_classify as ec

TEXT = ("NY N330020 January 10, 2023 CLA-2-87:OT:RR:NC:N2:201 CATEGORY: Classification TARIFF NO.: 8716.90.5060 "
        "Mr. X Dear Mr. X: In your letter dated December 29, 2022, you requested a tariff classification ruling. "
        "Additional information was provided to this office upon request on January 9, 2023. "
        "The item under consideration is a Trailer Transition Plate, Part Number e32JR. The item consists of three "
        "metal plates of 2.5 mm thick steel. You suggest classification in subheading 7326.90.8688, HTSUS. "
        "The applicable subheading for the Trailer Transition Plate will be 8716.90.5060, HTSUS, which provides for "
        "parts of trailers. The general rate of duty will be 3.1 percent ad valorem.")


class ExtractTest(unittest.TestCase):
    def test_basic(self):
        d = ec.extract_description(TEXT)
        self.assertTrue(d.startswith("The item under consideration is a Trailer Transition Plate"), d)
        self.assertNotIn("Additional information", d)
        self.assertNotIn("applicable subheading", d)
        self.assertNotIn("8716", d)
        self.assertNotIn("7326", d)          # 申请人建议的税号也抹掉
        self.assertNotIn("You suggest", d)    # 在"You suggest"处截断

    def test_no_anchor_returns_empty(self):
        self.assertEqual(ec.extract_description("Nothing here about rulings."), "")
        self.assertEqual(ec.extract_description(""), "")

    def test_length_cap_on_sentence(self):
        long = ("you requested a tariff classification ruling. " + "The product is a widget made of steel. " * 30)
        d = ec.extract_description(long, max_chars=200)
        self.assertLessEqual(len(d), 200)
        self.assertTrue(d.endswith("."), d[-20:])


if __name__ == "__main__":
    unittest.main()
