# -*- coding: utf-8 -*-
"""
bootstrap.py —— 一条命令把仓库跑起来

新克隆的仓库是跑不起来的：数据库 data/sec301_db.json 是 .gitignore 的构建产物，
而构建它要四份官方文件，其中 13MB 的 Chapter 99 PDF 同样不入库。这些步骤本来
散在 README 的不同段落里，换台机器或者换个人接手，"能跑起来"这件事本身有门槛。

用法：
    python scripts/bootstrap.py              # 装依赖 → 拉官方源 → 建库 → 自检
    python scripts/bootstrap.py --skip-deps  # 依赖已装好，只补数据
    python scripts/bootstrap.py --check      # 只体检，不改任何东西

设计上只做编排，不重复实现：拉源文件与重建走 check_sources.py --apply --rebuild
（它自己就管着四份源的地址、哈希判更新、以及更新后该串跑哪几个提取脚本）。
本脚本负责的是"把顺序和前提讲清楚，并在任何一步失败时说明白是哪一步、怎么办"。
"""
import argparse
import os
import subprocess
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "data", "sec301_db.json")
REQ_PATH = os.path.join(BASE_DIR, "requirements.txt")

OK, WARN, BAD = "✓", "!", "✗"


def say(mark, text, detail=""):
    print(f"  {mark} {text}" + (f"\n      {detail}" if detail else ""))


def run(cmd, title):
    """跑一条子命令，实时透传输出。返回 True/False。"""
    print(f"\n$ {' '.join(os.path.relpath(c, BASE_DIR) if str(c).startswith(BASE_DIR) else str(c) for c in cmd)}")
    try:
        return subprocess.call(cmd, cwd=BASE_DIR) == 0
    except FileNotFoundError as e:
        say(BAD, f"{title} 无法执行", str(e))
        return False


# ------------------------------------------------------------
# 体检
# ------------------------------------------------------------

def check_python():
    v = sys.version_info
    ok = v >= (3, 9)
    say(OK if ok else BAD, f"Python {v.major}.{v.minor}.{v.micro}",
        "" if ok else "需要 3.9 以上")
    return ok


def check_deps():
    """只查真正会让判定链跑不起来的那几个，不做全量 pip 校验。"""
    need = {"pandas": "表格读写", "fastapi": "Web 服务", "uvicorn": "Web 服务",
            "httpx": "官方源下载", "pdfplumber": "PDF 提取", "openpyxl": "Excel 导出"}
    missing = []
    for mod, why in need.items():
        try:
            __import__(mod)
        except ImportError:
            missing.append(f"{mod}（{why}）")
    if missing:
        say(BAD, f"缺 {len(missing)} 个依赖", "、".join(missing))
        return False
    say(OK, f"依赖齐全（{len(need)} 项）")
    return True


def check_sources_present():
    """四份官方源文件在不在。缺哪份要说得出它是干什么用的。"""
    sys.path.insert(0, os.path.join(BASE_DIR, "scripts"))
    import check_sources as cs

    missing = []
    for key, info in cs.SOURCES.items():
        path = os.path.join(BASE_DIR, info["path"])
        if os.path.exists(path):
            mb = os.path.getsize(path) / 1048576
            say(OK, f"{info['label']}", f"{info['path']}（{mb:.1f} MB）")
        else:
            missing.append((key, info))
            say(BAD, f"{info['label']} 缺失", info["path"])
    return missing


def check_db():
    if not os.path.exists(DB_PATH):
        say(BAD, "数据库未构建", "data/sec301_db.json 不存在（它是 .gitignore 的构建产物）")
        return False
    sys.path.insert(0, os.path.join(BASE_DIR, "scripts"))
    import core

    try:
        db = core.load_db()
    except Exception as e:
        say(BAD, "数据库读取失败", str(e))
        return False
    meta = db.get("meta") or {}
    say(OK, f"数据库就绪（构建于 {meta.get('built_at', '未知')}）",
        f"8位子目 {len(db.get('rates_8') or {}):,} | "
        f"301 映射 {len(db.get('sec301_map') or {}):,} | "
        f"排除涉及编码 {len(((db.get('exclusions') or {}).get('by_code')) or {}):,}")
    if not (db.get("units_8") or {}):
        say(WARN, "缺计量单位数据",
            "估算页的「计量单位」列会显示 —。重跑 scripts/build_db.py 可补上")
    return True


def smoke_test():
    """
    判定链端到端自检：三条真实编码，覆盖三种最容易出错的路径。

    只断言"算得出且结论合理"，不钉死具体数值——税率随官方数据变，
    把数字写死会让这个自检在下次官方更新后变成噪音。
    """
    sys.path.insert(0, os.path.join(BASE_DIR, "scripts"))
    import core
    import rate

    db = core.load_db()
    ok = True

    # ① 纯从价 + 命中 301：最常见的路径
    r = core.query_one(db, "85076000", origin="CN")
    if r.get("301判定", "").startswith("是") and r.get("一般税率"):
        say(OK, "从价税 + 301 判定", f"8507.60.00 → {r['301判定']} {r.get('301加征', '')}")
    else:
        say(BAD, "从价税 + 301 判定异常", str(r.get("301判定")))
        ok = False

    # ② 从量税：不给单价应答"需人工"，给了要算得出——两个方向都得对
    a = rate.calc_total(db, "01051100")
    b = rate.calc_total(db, "01051100", unit_value=2.0)
    if "需人工" in a["总税负估算"] and "%" in b["总税负估算"]:
        say(OK, "从量税折算", f"0105.11.00 无单价 → 需人工；$2/只 → {b['总税负估算'][:18]}")
    else:
        say(BAD, "从量税折算异常", f"{a['总税负估算']} / {b['总税负估算']}")
        ok = False

    # ③ 逐行估算 + 计量单位：新增的那条链路
    out = rate.estimate_lines(db, [{"code": "8507.60.00", "qty": 100, "unit_value": 20}])
    row = out["rows"][0]
    if row["预估税费"] and row["计量单位"] != "—":
        say(OK, "逐行估算", f"100 × $20 → 货值 ${row['货值']:,.0f}，"
                          f"税费 ${row['预估税费']:,.2f}（计量单位 {row['计量单位']}）")
    else:
        say(BAD, "逐行估算异常", str(row.get("预估税费")))
        ok = False

    # ④ 排除到期：不是失败项，但必须在装机时就让人知道有这么个日期
    e = core.exclusion_expiry(db)
    if e:
        mark = WARN if e["剩余天数"] <= 30 else OK
        say(mark, f"301 排除有效期至 {e['最早到期']}（剩 {e['剩余天数']} 天）",
            "到期后须重抓 Chapter 99 并重跑 extract_exclusions.py，"
            "否则会按失效的排除判 0%")
    else:
        say(WARN, "当前没有生效中的 301 排除",
            "可能是数据已过期。跑 python scripts/check_sources.py --apply --rebuild")
    return ok


# ------------------------------------------------------------
# 主流程
# ------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="一条命令把 hts-agent 跑起来")
    ap.add_argument("--skip-deps", action="store_true", help="跳过 pip install")
    ap.add_argument("--check", action="store_true", help="只体检，不下载不构建")
    a = ap.parse_args()

    print("=" * 68)
    print("  hts-agent 装机自检")
    print("=" * 68)

    print("\n① 运行环境")
    if not check_python():
        return 1
    deps_ok = check_deps()

    if not deps_ok and not a.check:
        if a.skip_deps:
            say(BAD, "依赖缺失但指定了 --skip-deps", "去掉该参数重跑，或手动 pip install")
            return 1
        if not run([sys.executable, "-m", "pip", "install", "-r", REQ_PATH], "安装依赖"):
            say(BAD, "依赖安装失败",
                "若在虚拟环境外，先 python3 -m venv .venv && source .venv/bin/activate")
            return 1
        deps_ok = check_deps()
        if not deps_ok:
            return 1

    print("\n② 官方数据源")
    missing = check_sources_present() if deps_ok else []

    if missing and not a.check:
        print(f"\n  缺 {len(missing)} 份官方源文件，开始下载"
              f"（Chapter 99 有 13MB，慢一点是正常的）")
        keys = [k for k, _ in missing]
        rc = subprocess.call(
            [sys.executable, os.path.join(BASE_DIR, "scripts", "check_sources.py"),
             "--apply", "--only", ",".join(keys)], cwd=BASE_DIR)
        still = check_sources_present()
        if still:
            # 逐份点名而不是笼统说"下载失败"：USITC 偶发 503，
            # 用户要知道是哪一份没下来、能不能手动补
            print()
            say(BAD, f"仍缺 {len(still)} 份，装机无法继续")
            for _k, info in still:
                say(" ", info["label"], f"手动下载后放到仓库根目录：{info['url']}")
            return 1

    print("\n③ 数据库")
    db_ok = check_db() if deps_ok else False
    if not db_ok and not a.check:
        if not run([sys.executable, os.path.join(BASE_DIR, "scripts", "build_db.py")],
                   "构建数据库"):
            say(BAD, "构建失败", "上方日志里有具体原因（常见：官方 PDF 改版导致解析矛盾）")
            return 1
        db_ok = check_db()
        if not db_ok:
            return 1

    if not db_ok:
        print("\n  （--check 模式，未做任何改动）")
        return 1

    print("\n④ 判定链自检")
    if not smoke_test():
        say(BAD, "自检未通过", "数据库构建出来了但判定结果异常，请查上方明细")
        return 1

    # 解释器在仓库外时 relpath 会拼出 ../../../Users/... 这种没法照抄的路径，
    # 那就直接给 python 二字，让人用自己刚才用的那个
    py = os.path.relpath(sys.executable, BASE_DIR)
    if py.startswith(".."):
        py = "python"
    print("\n" + "=" * 68)
    print("  装机完成。启动：")
    print(f"    {py} app.py")
    print("  然后浏览器打开 http://127.0.0.1:5000")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
