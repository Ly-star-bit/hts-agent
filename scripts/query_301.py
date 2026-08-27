# -*- coding: utf-8 -*-
"""
query_301.py —— 批量查询 301 关税（命令行版）

输入：HTS 编码列表（Excel / CSV / 纯文本 / 命令行参数 / 交互输入）
输出：结果表 Excel + CSV + 控制台明细

用法示例：
  python scripts/query_301.py -i 我的商品.xlsx --sheet 0 --col HTS
  python scripts/query_301.py -i hts_list.csv -o 结果.xlsx
  python scripts/query_301.py -c "8501.10.20, 0203.29.20"
  python scripts/query_301.py        # 交互模式

Web 版：python app.py 后访问 http://127.0.0.1:5000
"""
import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT = os.path.join(BASE_DIR, "output", "query_result.xlsx")
MAX_SHOW = 50


def read_codes_from_file(path, sheet=None):
    """从 xlsx / csv / txt 读取 HTS 编码列表，返回 (编码列表, 文件说明)"""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xlsx", ".xls", ".csv"):
        import pandas as pd

        df = (
            pd.read_excel(path, sheet_name=sheet if sheet is not None else 0, dtype=str)
            if ext in (".xlsx", ".xls")
            else pd.read_csv(path, dtype=str)
        )
        return core.extract_codes(df.to_csv(index=False)), f"{ext[1:].upper()} 文件 {os.path.basename(path)}"
    # 纯文本：逐行读取
    with open(path, encoding="utf-8-sig") as f:
        return core.extract_codes(f.read()), f"文本文件 {os.path.basename(path)}"


def print_table(results):
    """控制台打印结果明细表（关键列），描述/备注过长时截断"""
    headers = ["输入编码", "商品描述", "一般税率", "301判定", "301加征", "FLIP301", "备注"]
    data = []
    for r in results[:MAX_SHOW]:
        data.append([
            r["输入编码"],
            (r["商品描述"] or "")[:36],
            r["一般税率"][:14],
            r["301判定"],
            r.get("301加征", ""),
            (r.get("FLIP 301加征") or ""),
            (r["备注"] or "")[:40],
        ])
    widths = []
    for i, h in enumerate(headers):
        w = len(h)
        for row in data:
            w = max(w, len(row[i]))
        widths.append(w)
    line = "  ".join(f"{h:<{w}}" for h, w in zip(headers, widths))
    print(line)
    print("-" * len(line))
    for row in data:
        print("  ".join(f"{c:<{w}}" for c, w in zip(row, widths)))


def main():
    parser = argparse.ArgumentParser(description="批量查询 301 关税 / 税率搜索 / 成本估算")
    parser.add_argument("-i", "--input", help="输入文件（xlsx/csv/txt），自动识别 HTS 列")
    parser.add_argument("--sheet", help="Excel 工作表名或索引（默认第一个）")
    parser.add_argument("-o", "--output", default=DEFAULT_OUT, help="输出 xlsx 路径")
    parser.add_argument("-c", "--codes", help="直接以逗号/空格分隔传入编码，如 \"8501.10.20, 0203.29.20\"")
    parser.add_argument("-q", "--quiet", action="store_true", help="控制台只显示统计摘要，不打印明细")
    parser.add_argument("--search", metavar="关键词", help="关键词搜索税率（英文品名/编码），按等效从价税率排序（最低税率在前）")
    parser.add_argument("--top", type=int, default=20, help="--search 模式下显示前 N 条（默认 20）")
    parser.add_argument("--estimate", action="store_true", help="成本估算模式：计算总税负（基础 + 301 + 附加税）")
    parser.add_argument("--unit-value", type=float, default=None, help="单位货值 USD，用于折算从量税（--estimate 推荐提供）")
    parser.add_argument("--origin", default="CN", help="原产地：CN（中国，默认）/ VN（越南）/ 其他国家代码（如 CA、EU）")
    args = parser.parse_args()

    try:
        db = core.load_db()
    except FileNotFoundError as e:
        sys.exit(f"错误：{e}")
    print(f"数据库已加载：301映射 {db['meta']['sec301_mapping_count']} 条 | 8位子目 {db['meta']['rates_8_count']} 个")

    # ---------- 搜索模式 ----------
    if args.search:
        import rate

        rows = rate.search(db, args.search, limit=args.top, sort="tax_asc")
        if not rows:
            sys.exit(f"未找到与「{args.search}」匹配的商品，请尝试更短的英文关键词或编码。")
        print(f"「{args.search}」匹配 {len(rows)} 个候选，按等效从价税率升序（最低税率在前）：\n")
        print(f"{'编码':<14}{'等效从价':<12}{'税率类型':<8}{'301':<10}{'一般税率':<16}描述")
        print("-" * 100)
        for r in rows:
            print(f"{r['编码']:<14}{r['等效从价']:<12}{r['税率类型']:<8}"
                  f"{r['301判定'] + ' ' + r['301加征']:<10}{r['一般税率']:<16}{r['商品描述'][:50]}")
        print(f"\n提示：从量/复合税需按货值折算，可加 --unit-value 与 --estimate 估算总税负；"
              f"完整税率见 Web 版或 python scripts/query_301.py -c 编码")
        return

    # ---------- 估算模式（与查询模式共用编码来源） ----------
    if args.estimate:
        import rate

        codes = []
        source = ""
        if args.codes:
            codes = core.extract_codes(args.codes)
            source = "命令行参数"
        elif args.input:
            codes, source = read_codes_from_file(args.input, args.sheet)
        else:
            print("估算模式：请输入 HTS 编码（多个用逗号或换行分隔，空行结束）：")
            buf = []
            while True:
                try:
                    line = input()
                except EOFError:
                    break
                if not line.strip():
                    break
                buf.append(line)
            codes = core.extract_codes("\n".join(buf))
            source = "交互输入"
        if not codes:
            sys.exit("错误：未解析到任何 HTS 编码。")
        print(f"来源：{source} | {len(codes)} 个编码 | 单位货值：{args.unit_value or '未提供（从量税按原样显示）'}\n")
        print(f"{'编码':<14}{'税率类型':<8}{'一般税率':<18}{'等效从价':<10}{'301加征':<10}{'总税负估算'}")
        print("-" * 90)
        for c in codes:
            r = rate.calc_total(db, c, unit_value=args.unit_value, origin=args.origin)
            print(f"{r['输入编码']:<14}{r['税率类型']:<8}{r['一般税率']:<18}"
                  f"{r['基础等效从价']:<10}{r.get('301加征', ''):<10}{r['总税负估算']}")
        return

    # ---------- 常规 301 查询模式 ----------
    if args.codes:
        codes = core.extract_codes(args.codes)
        source = "命令行参数"
    elif args.input:
        codes, source = read_codes_from_file(args.input, args.sheet)
    else:
        print("交互模式：请输入 HTS 编码（支持 8501.10.20 / 85011020 / 8501.10.20.10 等格式，多个用逗号或换行分隔，空行结束）：")
        buf = []
        while True:
            try:
                line = input()
            except EOFError:
                break
            if not line.strip():
                break
            buf.append(line)
        codes = core.extract_codes("\n".join(buf))
        source = "交互输入"

    if not codes:
        sys.exit("错误：未解析到任何 HTS 编码。请检查输入格式（应为 8位或10位 编码）。")

    print(f"来源：{source} | 解析到 {len(codes)} 个编码 | 原产地：{args.origin}，开始判定 ...")
    results, stats = core.batch_query(db, codes, origin=args.origin)

    # 输出 CSV
    out_dir = os.path.dirname(args.output) or "."
    os.makedirs(out_dir, exist_ok=True)
    csv_path = os.path.splitext(args.output)[0] + ".csv"
    fieldnames = list(results[0].keys())
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)

    # 输出 Excel
    try:
        import pandas as pd

        pd.DataFrame(results).to_excel(args.output, index=False)
        print(f"✅ 完成！结果已保存：\n  Excel: {os.path.abspath(args.output)}\n  CSV:   {os.path.abspath(csv_path)}")
    except ImportError:
        print(f"✅ 完成！openpyxl 不可用，已保存 CSV：{os.path.abspath(csv_path)}")

    # 控制台摘要 + 明细
    print(f"\n命中 301 加征: {stats['hit']} 个 | 未命中: {stats['miss']} 个")
    if not args.quiet:
        print("=" * 100)
        print_table(results)
        if len(results) > MAX_SHOW:
            print(f"...（共 {len(results)} 条，其余请查看输出文件）")


if __name__ == "__main__":
    main()
