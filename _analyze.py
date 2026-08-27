# -*- coding: utf-8 -*-
"""确认关键加征机制子目"""
import csv
import re

rows = []
with open("htsdata.csv", encoding="utf-8-sig") as f:
    rows = list(csv.reader(f))[1:]

targets = [
    "99030120",  # 中国 IEEPA 芬太尼
    "99038567",  # +200%
    "99031301",  # 药品?
    "99031501",  # 半导体?
    "99031601",  # 木材?
    "99039401",  # 汽车 232
    "99038901",  # DST/空客 301
    "99039008",  # 俄罗斯
    "99034505",  # 201 光伏
    "99038201",  # 232 钢铝扩展
]
for t in targets:
    hit = [r for r in rows if re.sub(r"\D", "", r[0].strip()) == t]
    if hit:
        r = hit[0]
        print(f"{r[0]:12s} | {r[4][:40]:40s} | {r[2][:70]}")
    else:
        # 尝试前缀匹配
        hit2 = [r for r in rows if re.sub(r"\D", "", r[0].strip()).startswith(t)]
        if hit2:
            r = hit2[0]
            print(f"{r[0]:12s} | {r[4][:40]:40s} | {r[2][:70]}  ...(共{len(hit2)}个)")
        else:
            print(f"{t:12s} | 未找到")
