# -*- coding: utf-8 -*-
"""
app.py —— HTS 301 关税查询 Web 应用（FastAPI）

启动：python app.py  → 浏览器打开 http://127.0.0.1:5000
  或：uvicorn app:app --host 127.0.0.1 --port 5000

功能：
  - 文本粘贴 HTS 编码批量查询
  - 上传 Excel/CSV 文件批量查询
  - 结果表格展示 + 导出 CSV/Excel
  - 与命令行工具共用同一套查询逻辑（scripts/core.py）
"""
import io
import json
import os
import re
import sys
import traceback
from datetime import datetime
from typing import Optional
from urllib.parse import quote

import pandas as pd
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts"))
import core
import rate

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_HTML = os.path.join(BASE_DIR, "templates", "index.html")
MAX_UPLOAD = 20 * 1024 * 1024  # 上传限制 20MB

app = FastAPI(title="HTS 301 关税查询工具", version="1.1.0")

_db = None
_db_key = None      # (mtime_ns, size)：数据库文件变了就重读，不用重启服务


def _db_stat_key():
    try:
        st = os.stat(core.DB_JSON)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def get_db():
    """
    懒加载数据库（带缓存）。缓存键是文件的 mtime/size：check_sources --apply --rebuild
    或手动 build_db 之后，下一次请求自动换成新库——此前要手动重启进程，而这个进程是
    nohup 起的，重建完忘了重启就一直用旧库，页面上的构建时间还显示着旧的。
    """
    global _db, _db_key
    key = _db_stat_key()
    if _db is None or key != _db_key:
        _db = core.load_db()
        _db_key = key
    return _db


class QueryRequest(BaseModel):
    text: str = Field(default="", description="包含 HTS 编码的文本")
    origin: Optional[str] = Field(default="CN", description="原产地：CN（中国，默认）/ VN（越南）/ 其他国家代码")


class ExportRequest(BaseModel):
    results: list = Field(default_factory=list, description="查询结果列表")
    fmt: str = Field(default="csv", description="导出格式：csv 或 xlsx")


@app.get("/")
def index():
    """前端页面"""
    # no-cache：不是"不缓存"，是"每次都拿 ETag 去问一句"，没变就 304。
    # 默认只发 Last-Modified，Chrome 会按启发式规则（文件年龄的 10%）直接吃本地副本，
    # 改完页面刷新看到的还是旧版——改样式时一路被这个坑，装了新版也得让用户硬刷。
    return FileResponse(TEMPLATE_HTML, headers={"Cache-Control": "no-cache"})


@app.get("/api/info")
def api_info():
    """数据源信息（页面头部展示）"""
    meta = get_db()["meta"]
    return {
        "sec301_mapping_count": meta.get("sec301_mapping_count", 0),
        "rates_8_count": meta.get("rates_8_count", 0),
        "hts_csv": meta.get("hts_csv", ""),
        "ustr_pdf": meta.get("ustr_pdf", ""),
        "flip_frn_pdf": meta.get("flip_frn_pdf", ""),
        "ch99_pdf": meta.get("ch99_pdf", ""),   # 301 排除清单来源，顶栏提示里要列全四份源
        "built_at": meta.get("built_at", ""),
        # 排除到期是唯一会让工具**少报**的定时炸弹：到期后不重抓数据，
        # 它会继续按失效的排除判 0%。顶栏据此变色。
        "exclusion_expiry": core.exclusion_expiry(get_db()),
        # 官方源有新版但本地还没应用（定时任务下载/重建失败，或还没跑到）：
        # 此前只藏在「数据来源」弹窗里，Rev19 出来一整天顶栏仍显示"数据就绪"。
        "sources_outdated": _sources_outdated(),
        # 本地文件名不随官方改版换（"Chapter 99_2026HTSRev18.pdf" 里装的可能已是 Rev19 的字节），
        # 真正的内容版本记在探测状态里，这里一并给出，页头提示按它显示
        "source_versions": _source_versions(),
        # 源文件比库新 = 下载成功但重建失败（或还没跑）：状态文件里 applied 已是 true，
        # 只有这个比对能发现。页头据此变黄。
        "db_stale": _db_stale(),
    }


def _db_stale():
    """任一官方源文件的 mtime 晚于 data/sec301_db.json 的 mtime → True。"""
    try:
        db_m = os.path.getmtime(core.DB_JSON)
    except OSError:
        return False
    newer = []
    for key, info in SOURCE_FILES.items():
        path = os.path.join(BASE_DIR, info["path"])
        try:
            if os.path.getmtime(path) > db_m + 1:
                newer.append(info["label"])
        except OSError:
            pass
    return newer


def _source_versions():
    """{key: 内容版本}：最近一次探测判定"与官方一致"或已下载应用的，取远端版本号。"""
    rc = _remote_check_summary()
    if not rc:
        return {}
    out = {}
    for key, s in (rc.get("sources") or {}).items():
        if s.get("error"):
            continue
        if not s.get("updated") or s.get("applied"):
            out[key] = s.get("remote_version", "")
    return out


def _sources_outdated():
    """[{key, label, remote_version}]：最近一次探测发现有新版、且尚未下载应用的官方源。"""
    rc = _remote_check_summary()
    if not rc:
        return []
    out = []
    for key in rc.get("updated") or []:
        s = (rc.get("sources") or {}).get(key) or {}
        if s.get("updated") and not s.get("applied"):
            out.append({"key": key, "label": SOURCE_FILES.get(key, {}).get("label", key),
                        "short": {"htsdata": "税率表", "ustr_pdf": "301 清单", "flip_frn": "FLIP FRN",
                                  "ch99_pdf": "第 99 章"}.get(key, key),
                        "remote_version": s.get("remote_version", ""),
                        "checked_at": s.get("checked_at", "")})
    return out


# ============================================================
# 来源追溯：官方源文件清单 / PDF 定位 / CSV 行上下文
# ============================================================

SOURCE_FILES = {
    "htsdata": {
        "path": "htsdata.csv",
        "label": "USITC 全量税率表",
        "mime": "text/csv",
        "desc": "一般/特殊/第二栏税率与商品描述的基础数据源",
    },
    "ustr_pdf": {
        "path": "China Tariffs_2026HTSRev15.pdf",
        "label": "USTR 301 中国清单",
        "mime": "application/pdf",
        "desc": "中国 Section 301 加征：8 位子目 → Chapter 99 子目归属",
    },
    "flip_frn": {
        "path": "FLIP 301 Investigation Final Action FRN 7-23-26 FINAL.pdf",
        "label": "FLIP 301 Final Action FRN",
        "mime": "application/pdf",
        "desc": "FLIP 301 强迫劳动关税：60 经济体税率表 + ANNEX II 豁免清单（含范围限制）",
    },
    # 上一版加了 301 排除判定，但只把 ch99 补进了 /api/info，没进这份清单——
    # 于是"数据来源"弹窗里少了一份实际参与判定的官方文件。
    "ch99_pdf": {
        "path": "Chapter 99_2026HTSRev18.pdf",
        "label": "HTS 第 99 章全文",
        "mime": "application/pdf",
        "desc": "301 排除清单（U.S. note 20）条目正文与页码",
    },
}


def _source_file_info(key):
    """源文件路径存在性检查，返回 (path, info) 或抛 404"""
    info = SOURCE_FILES.get(key)
    if not info:
        raise HTTPException(status_code=404, detail=f"未知源文件 key：{key}")
    path = os.path.join(BASE_DIR, info["path"])
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail=f"源文件缺失：{info['path']}（请确认数据文件存在）")
    return path, info


@app.get("/api/source/list")
def api_source_list():
    """源文件清单（含本地 meta 版本信息），供页面"数据来源"展示"""
    files = []
    for key, info in SOURCE_FILES.items():
        path = os.path.join(BASE_DIR, info["path"])
        files.append({
            "key": key,
            "label": info["label"],
            "path": info["path"],
            "desc": info["desc"],
            "mime": info["mime"],
            "exists": os.path.exists(path),
            "size": os.path.getsize(path) if os.path.exists(path) else 0,
        })
    meta = get_db()["meta"]
    return {
        "files": files,
        "meta": {
            "hts_csv": meta.get("hts_csv", ""),
            "ustr_pdf": meta.get("ustr_pdf", ""),
            "flip_frn_pdf": meta.get("flip_frn_pdf", ""),
            "built_at": meta.get("built_at", ""),
        },
        "remote_check": _remote_check_summary(),
    }


def _remote_check_summary():
    """
    scripts/check_sources.py 最近一次对官方服务器的探测结果（data/.sources_state.json）。
    页面只读不探测——探测要联网、要几十秒，属于 launchd 定时任务，不该挂在弹窗上。
    从未跑过 → None，前端不显示这一块。
    """
    try:
        import check_sources
        state = check_sources.load_state()
    except Exception:
        return None
    if not state.get("checked_at"):
        return None
    per = {}
    for key, s in (state.get("sources") or {}).items():
        per[key] = {
            "remote_version": s.get("remote_version", ""),
            "remote_last_updated": s.get("remote_last_updated", ""),
            "updated": bool(s.get("updated")),
            "applied": bool(s.get("applied")),
            "checked_at": s.get("checked_at", ""),
            "error": s.get("last_error", "") if s.get("last_error_at", "") > s.get("checked_at", "") else "",
        }
    return {"checked_at": state["checked_at"], "updated": state.get("updated", []),
            "errors": state.get("errors", []), "sources": per}


@app.get("/api/source/pdf/{key}")
def api_source_pdf(key: str):
    """内联返回官方 PDF。前端打开 /api/source/pdf/{key}#page=N 可定位到指定物理页"""
    path, info = _source_file_info(key)
    if info["mime"] != "application/pdf":
        raise HTTPException(status_code=400, detail=f"{info['path']} 不是 PDF 文件")
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=os.path.basename(path),
        content_disposition_type="inline",
    )


@app.get("/api/source/csv")
def api_source_csv(key: str = "htsdata", line: int = None, around: int = 3):
    """返回 CSV 源文件指定行（1 起，含表头行）及其上下文的文本，供弹窗展示原文"""
    path, info = _source_file_info(key)
    if info["mime"] != "text/csv":
        raise HTTPException(status_code=400, detail=f"{info['path']} 不是 CSV 文件")
    with open(path, encoding="utf-8-sig") as f:
        lines = f.readlines()
    total = len(lines)
    ctx = []
    if line and 1 <= line <= total:
        around = max(0, min(int(around), 5))
        start = max(1, line - around)
        end = min(total, line + around)
        ctx = [{"行号": i, "内容": lines[i - 1].rstrip("\r\n")} for i in range(start, end + 1)]
    return {"file": os.path.basename(path), "total_lines": total, "target": line if ctx else None, "context": ctx}


@app.post("/api/query")
def api_query(req: QueryRequest):
    """文本查询：POST JSON {"text": "6204.69.45, 8703.80.00", "origin": "CN"}"""
    db = get_db()
    codes, issues = core.extract_codes_detailed(req.text, db)
    if not codes:
        detail = "未解析到任何 HTS 编码，请检查输入格式（8位或10位，如 6204.69.45）"
        if issues:
            detail += "；以下输入无法采用：" + "；".join(
                f"{i['原文']}（{i['原因']}）" for i in issues[:5])
        raise HTTPException(status_code=400, detail=detail)
    results, stats = core.batch_query(db, codes, origin=req.origin)
    rate.attach_alt_232(results)
    # 未采用的输入必须回报：否则结果行数比输入少，用户不知道少了哪几行
    return {"results": results, "stats": stats, "origin": req.origin, "未采用": issues}


@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...), origin: Optional[str] = Form("CN")):
    """文件上传查询：multipart 文件字段 file（xlsx/csv/txt）+ 可选原产地 origin"""
    filename = file.filename or "未命名文件"
    content = await file.read()
    if len(content) > MAX_UPLOAD:
        raise HTTPException(status_code=400, detail=f"文件超过 {MAX_UPLOAD // 1048576}MB 限制")
    ext = os.path.splitext(filename)[1].lower()
    try:
        db = get_db()
        if ext in (".xlsx", ".xls"):
            df = pd.read_excel(io.BytesIO(content), sheet_name=0, dtype=str)
            codes, issues = core.extract_codes_detailed(df.to_csv(index=False), db)
        elif ext == ".csv":
            df = pd.read_csv(io.StringIO(content.decode("utf-8-sig", errors="replace")), dtype=str)
            codes, issues = core.extract_codes_detailed(df.to_csv(index=False), db)
        else:
            codes, issues = core.extract_codes_detailed(
                content.decode("utf-8-sig", errors="replace"), db)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"文件解析失败：{e}")
    if not codes:
        raise HTTPException(status_code=400, detail=f"文件 {filename} 中未解析到 HTS 编码")
    results, stats = core.batch_query(get_db(), codes, origin=origin)
    rate.attach_alt_232(results)
    return {"results": results, "stats": stats, "source": filename, "origin": origin,
            "未采用": issues}


# Excel / Sheets 会把以 = + - @ 开头（含前导 TAB/CR）的单元格当公式执行。
# 导出结果由前端回传、可含任意文本，且报关场景下导出文件常被转发他人打开，
# 因此写文件前统一加前导单引号中和（仅影响显示，不改变数值判定）。
_FORMULA_PREFIX = ("=", "+", "-", "@", "\t", "\r")


def _defuse(value):
    """中和单元格公式注入；非字符串原样返回"""
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIX):
        return "'" + value
    return value


_stamp_cache = {"key": None, "value": ""}


def data_version_stamp():
    """
    数据版本戳：构建时间 + 四份官方源文件的内容哈希前 8 位。
    哈希按文件 mtime/size 缓存，导出时不用每次重算 20MB。
    """
    import hashlib
    meta = get_db().get("meta") or {}
    parts = [f"构建 {meta.get('built_at', '?')}"]
    key = []
    for k, info in SOURCE_FILES.items():
        path = os.path.join(BASE_DIR, info["path"])
        try:
            st = os.stat(path)
            key.append((k, st.st_mtime_ns, st.st_size))
        except OSError:
            key.append((k, None, None))
    key = tuple(key)
    if _stamp_cache["key"] == key:
        return _stamp_cache["value"]
    for k, info in SOURCE_FILES.items():
        path = os.path.join(BASE_DIR, info["path"])
        if not os.path.exists(path):
            parts.append(f"{info['path']} 缺失")
            continue
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        parts.append(f"{info['path']} {h.hexdigest()[:8]}")
    _stamp_cache["key"], _stamp_cache["value"] = key, "｜".join(parts)
    return _stamp_cache["value"]


def _flatten_for_export(row):
    """
    结构化字段摊平成可读文本。/api/export 按 keys() 铺列，list of dict 会在 Excel 里
    变成 Python repr；前端摊了「来源」「候选」几个，后端再兜一层——今天新加的
    「已终止措施」带整段 IEEPA 依据，不摊每一行都要多几百字。
    """
    o = dict(row)
    def j(items, f):
        return "；".join(f(x) for x in items if isinstance(x, dict))
    v = o.get("未建模措施")
    if isinstance(v, list):
        o["未建模措施"] = j(v, lambda u: f"{u.get('标目组', '')}×{u.get('标目数', '')}（{u.get('依据', '')}）")
    v = o.get("已终止措施")
    if isinstance(v, list):
        o["已终止措施"] = j(v, lambda t: f"{t.get('标目组', '')}×{t.get('数量', '')}（"
                          + "、".join(f"{b.get('状态', '')}{' ' + b['自'] if b.get('自') else ''}" for b in (t.get("依据") or [])[:2]) + "）")
    v = o.get("产品类未建模措施")
    if isinstance(v, list):
        o["产品类未建模措施"] = j(v, lambda h: f"note {h.get('note', '')} {h.get('子条', '').split(' ')[0]}")
    v = o.get("AD/CVD案件")
    if isinstance(v, list):
        o["AD/CVD案件"] = j(v, lambda a: f"{a.get('案号', '')} {a.get('类型', '')} {a.get('国家', '') or a.get('国家代码', '')} {a.get('商品', '')[:30]}")
    elif v is None and "AD/CVD案件" in o:
        o["AD/CVD案件"] = ""
    if isinstance(o.get("归类路径"), list):
        o["归类路径"] = " > ".join(str(x) for x in o["归类路径"])
    if isinstance(o.get("来源"), list):
        o["来源"] = j(o["来源"], lambda x: f"{x.get('类型', '')}: {x.get('文件', '')}")
    for k, val in list(o.items()):
        if isinstance(val, list):
            o[k] = "；".join(json.dumps(x, ensure_ascii=False) if isinstance(x, (dict, list)) else str(x) for x in val)
        elif isinstance(val, dict):
            o[k] = json.dumps(val, ensure_ascii=False)
    return o


def _defuse_rows(rows):
    """对导出行的每个值做公式中和；非 dict 行原样保留"""
    out = []
    for row in rows:
        if isinstance(row, dict):
            out.append({k: _defuse(v) for k, v in _flatten_for_export(row).items()})
        else:
            out.append(row)
    return out


@app.post("/api/export")
def api_export(req: ExportRequest):
    """导出结果：POST JSON {"results": [...], "fmt": "xlsx"|"csv"}，返回文件下载"""
    if not req.results:
        raise HTTPException(status_code=400, detail="无数据可导出")
    if not isinstance(req.results[0], dict):
        raise HTTPException(status_code=400, detail="导出数据格式错误：results 应为对象列表")
    rows = _defuse_rows(req.results)
    # 每行带数据版本：报关用的表格转发出去后，收件人得知道它基于哪一版税则与清单
    stamp = data_version_stamp()
    for row in rows:
        if isinstance(row, dict) and "数据版本" not in row:
            row["数据版本"] = stamp
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if req.fmt == "xlsx":
        buf = io.BytesIO()
        pd.DataFrame(rows).to_excel(buf, index=False)
        buf.seek(0)
        fname = f"hts_301_结果_{ts}.xlsx"
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    else:
        sio = io.StringIO()
        import csv as _csv

        fieldnames = list(rows[0].keys())
        writer = _csv.DictWriter(sio, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        buf = io.BytesIO(sio.getvalue().encode("utf-8-sig"))
        fname = f"hts_301_结果_{ts}.csv"
        media = "text/csv; charset=utf-8"
    # 中文文件名需 RFC 5987 编码，保证下载时显示正确
    disposition = f"attachment; filename*=UTF-8''{quote(fname)}"
    return Response(content=buf.getvalue(), media_type=media, headers={"Content-Disposition": disposition})


# ============================================================
# 以下为 1.2 新增功能：税率搜索 / 成本估算 / 数据变动 / AI 助手
# ============================================================

class SearchRequest(BaseModel):
    keyword: str = Field(default="", description="关键词（英文品名 / 编码）")
    # 默认从 tax_asc 改为 relevance：基础税率最低 ≠ 总税负最低
    # （8215.99.30 基础 14% 总 26.5% 比 8215.99.35 基础 6.8% 总 26.8% 更便宜）。
    # 按成本挑用 total_asc。
    sort: str = Field(default="relevance",
                      description="排序：relevance / total_asc / tax_asc / tax_desc / code_asc")
    origin: str = Field(default="CN", description="原产地，total_asc 排序时用于算 301/FLIP")
    limit: int = Field(default=100, ge=1, le=500)
    unit_value: Optional[float] = Field(default=None, description="单位货值 USD，用于折算从量税（可选）")
    include_special: bool = Field(default=False, description="是否包含第 98/99 章（特殊/临时条款，默认排除）")
    semantic: bool = Field(default=True, description="是否启用语义 / 先例两条召回通道（需本地索引与 ollama；不可用自动降级为关键词）")


class EstimateRequest(BaseModel):
    text: str = Field(default="", description="包含 HTS 编码的文本（与 codes / items 三选一）")
    codes: list = Field(default_factory=list, description="HTS 编码列表（与 text / items 三选一）")
    # items 是逐行估算的入口：每行自带单价与数量。
    # 单位货值只用来折算从量税——马按头、电池按公斤，"每单位多少钱"根本不是一回事，
    # 全表共用一个单价等于拿电池的公斤价去折算马的头价。text / codes + 全局
    # unit_value 的老用法保留不动（命令行与既有调用方还在用）。
    items: list = Field(default_factory=list,
                        description="逐行估算：[{'code': 编码, 'qty': 数量, 'unit_value': 单位货值}]")
    unit_value: Optional[float] = Field(default=None, description="单位货值 USD，折算从量税（items 未给时作为全行默认）")
    origin: Optional[str] = Field(default="CN", description="原产地：CN（中国，默认）/ VN（越南）/ 其他国家代码")
    ocean: bool = Field(default=True, description="是否海运（计港口维护费 HMF 0.125%）")


class AIAskRequest(BaseModel):
    question: str = Field(default="", description="自然语言问题")
    origin: Optional[str] = Field(default="CN", description="原产地：CN（中国，默认）/ VN（越南）")


class AIClassifyRequest(BaseModel):
    description: str = Field(default="", description="商品描述")
    origin: Optional[str] = Field(default="CN", description="原产地：CN（默认）/ VN / 其他国家代码")


class AIInterpretRequest(BaseModel):
    results: list = Field(default_factory=list, description="查询结果列表")


class AIAnalyzeRequest(BaseModel):
    items: list = Field(default_factory=list, description="商品清单：[{'name': 品名, 'unit_value': 可选}]")
    origin: Optional[str] = Field(default="CN", description="原产地：CN（默认）/ VN / 其他国家代码")


class AIConfigRequest(BaseModel):
    provider: str = Field(default=None, description="provider：openai_compat / ollama / 空(null)")
    base_url: str = Field(default=None)
    model: str = Field(default=None)
    api_key: str = Field(default=None, description="传 '__KEEP__' 表示保留原值")
    temperature: float = Field(default=None)
    timeout: float = Field(default=None)
    think: bool = Field(default=None, description="仅 ollama：思考模式，默认关（qwen3 开着每次多烧几百 token）")


class MeasuresConfigRequest(BaseModel):
    measures: dict = Field(default_factory=dict, description="加征开关，如 {'cn301': true, 'flip301': false}")


@app.get("/api/origins")
def api_origins():
    """
    原产地下拉的数据源：中国、越南、FLIP 301 的 60 个经济体、第二栏国家、
    「其他国家（不在名单）」与「未指定」。

    下拉此前写死 CN / VN / OTHER 三项，OTHER 落到「不在 60 名单」——界面上任何
    非中越原产的报价都静默少了 10%–12.5% 的 FLIP 301。列表从数据出，
    数据里多一个经济体，下拉就多一项。
    """
    return {"origins": core.origin_options(get_db())}


@app.get("/api/measures/config")
def api_measures_get_config():
    """读取加征措施开关配置（cn301 / flip301）"""
    return {"measures": core.load_measures_config()}


@app.post("/api/measures/config")
def api_measures_save_config(req: MeasuresConfigRequest):
    """保存加征措施开关配置（原子写入，立即生效，无需重启）"""
    cfg = core.save_measures_config(req.measures or {})
    return {"measures": cfg}


@app.get("/api/ai/status")
def api_ai_status():
    """AI 服务配置状态"""
    try:
        import ai
        return ai.ai_status()
    except Exception as e:
        return {"enabled": False, "message": f"AI 模块加载失败：{e}"}


@app.get("/api/ai/config")
def api_ai_get_config():
    """读取 AI 配置（api_key 仅返回掩码，绝不返回明文）"""
    import ai

    return ai.mask_config(ai.load_config())


@app.post("/api/ai/config")
def api_ai_save_config(req: AIConfigRequest):
    """保存 AI 配置（Web 端手动配置），保存后立即生效"""
    import ai

    updates = {}
    for field in ("provider", "base_url", "model", "api_key", "temperature", "timeout", "think"):
        v = getattr(req, field)
        if v is not None:
            updates[field] = v
    cfg = ai.save_config(updates)
    status = ai.ai_status()
    return {"config": cfg, "status": status}


@app.post("/api/ai/test")
def api_ai_test():
    """用当前配置测试 AI 服务连通性"""
    import ai

    return ai.test_connection()


def _annotate_precedent_counts(rows):
    """
    给搜索结果行补「先例数」（CBP 对该 8 位码的历史裁定条数，本地镜像键查）。

    None 与 0 是两个信息：None = 镜像没建（"没查"），0 = 查过了确实没有。
    前端分别显示 "—" 和 "0"。cross 模块加载失败也按"没查"处理——先例数
    是增强列，不能挡住搜索主链路。
    """
    try:
        import cross
        counts = cross.precedent_counts([r["编码"] for r in rows])
    except Exception:
        counts = {}
    for r in rows:
        r["先例数"] = counts.get(r["编码"])
    return rows


@app.post("/api/search")
def api_search(req: SearchRequest):
    """关键词搜索：返回匹配子目，可按等效从价税率排序（最低税率）"""
    import traceback
    try:
        import rate

        db = get_db()
        # 三通道召回（关键词 + 税则行语义 + 裁定 kNN）按 RRF 融合；语义 / 先例通道
        # 不可用时 status 写明原因、自动只剩关键词——主链路不依赖 ollama
        channels = None if req.semantic else ("keyword",)   # None = rate.DEFAULT_CHANNELS（可由环境变量收窄）
        rows, channel_status = rate.hybrid_search(
            db, req.keyword, limit=req.limit, sort=req.sort,
            include_special=req.include_special, unit_value=req.unit_value,
            origin=req.origin, channels=channels)
        # 同义词扩展信息：未映射的中文片段必须回报，否则用户会以为已完整检索
        _expanded, applied, leftover = rate.expand_query(req.keyword)
        # 「先例数」列：CROSS 本地镜像的预聚合键查。镜像没建时 counts 为空、
        # 该列显示 "—"——这是增强列，缺席不是错误
        _annotate_precedent_counts(rows)
        # 总税负列由 rate.search 统一补齐（含 unit_value / origin），此处不再重复计算
        # 一物多号自动识别：分歧应该由结果自己报出来，而不是等用户先意识到
        # "我这可能有多个码"再去手动勾选对比
        import criteria
        dispute = criteria.detect_dispute(db, rows, unit_value=req.unit_value,
                                          origin=req.origin)
        return {"results": rows, "count": len(rows), "keyword": req.keyword,
                "检索词": _expanded if applied else "",
                "同义词映射": applied, "未识别": leftover,
                "召回通道": channel_status,
                "归类分歧": dispute,
                # 大量候选同分时，"第一条"并不代表最匹配。与其伪造排序，
                # 不如把决定分类的那几个属性问回去
                "待确认属性": criteria.narrowing_questions(db, rows),
                "并列度": criteria.tie_ratio(rows)}
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"搜索服务端异常：{e}")


class CompareRequest(BaseModel):
    codes: list = Field(default_factory=list, description="待比较的候选 HTS 编码（2 个以上）")
    # 对比卡片里的总税负要与搜索页当前选择同口径，否则选了越南却按中国算 301
    unit_value: Optional[float] = Field(default=None, description="单位货值 USD，用于折算从量税")
    origin: str = Field(default="CN", description="原产地")


class SearchAssistRequest(BaseModel):
    keyword: str = Field(default="", description="商品描述或关键词")
    sort: str = Field(default="relevance", description="本地检索排序，与 /api/search 一致")
    limit: int = Field(default=40, ge=1, le=100, description="送入精排的候选上限")
    unit_value: Optional[float] = Field(default=None, description="单位货值 USD，用于折算新增候选的从量税")
    origin: str = Field(default="CN", description="原产地")


@app.post("/api/search/ai")
def api_search_assist(req: SearchAssistRequest):
    """
    搜索页的 AI 增强：补召回 + 精排。

    与 /api/search 是两段式而非替代关系——前端先拿本地结果渲染出表格，
    再调这里把 AI 新找到的候选并进同一张表。AI 未配置或调用失败时返回
    {'error': ...}，前端静默保留本地结果，不打断查询。
    """
    if not (req.keyword or "").strip():
        raise HTTPException(status_code=400, detail="请输入商品描述或关键词")
    import traceback
    try:
        import ai
        import rate  # noqa: F401 —— 预检：assist_search 内部依赖它，在这里导入
                     # 才能把模块级故障报成"AI 模块加载失败"而非"AI 分析异常"
    except Exception as e:
        return {"error": f"AI 模块加载失败：{e}"}

    db = get_db()
    try:
        # origin / unit_value 必须一路传到 assist_search 里的 rate.search：
        # 新增候选与本地行同表并列展示、还要一起排序，两边的总税负口径必须一致。
        # 此前 origin 只是形参、单位货值靠这里事后补算（且漏掉了 origin 与
        # 总税负数值），越南原产的 AI 行会照中国口径算出 +25% 的 301。
        result = ai.assist_search(db, req.keyword.strip(), origin=req.origin,
                                  limit=req.limit, sort=req.sort,
                                  unit_value=req.unit_value)
    except Exception as e:
        traceback.print_exc()
        return {"error": f"AI 分析异常：{e}"}
    # AI 补召回的行与本地行同表展示，「先例数」列同样要有——
    # 两批行一副面孔，一列缺失就是错位
    if result.get("新增候选"):
        _annotate_precedent_counts(result["新增候选"])
    return result


@app.post("/api/compare")
def api_compare(req: CompareRequest):
    """
    候选编码并列比较：完整品名 / 税率 / 判定条件 / 跨章分歧提示。
    用于"同一商品有多个可能税号"的归类分歧场景。
    """
    if len(req.codes or []) < 2:
        raise HTTPException(status_code=400, detail="请至少提供 2 个候选编码")
    import criteria

    db = get_db()
    # 等效从价 / 总税负 / 301判定 由 criteria.compare 一并算出（同口径），
    # 这里只补证据清单
    result = criteria.compare(db, req.codes, unit_value=req.unit_value,
                              origin=req.origin)
    for it in result["候选"]:
        it["证据清单"] = criteria.evidence_list(it["判定条件"])
    result["提示"] = ("各候选判定条件不同，需按 GRI 与商品实际特征论证；"
                     "正式归类以 CBP 裁定为准。")
    return result


class CrossPrecedentsRequest(BaseModel):
    term: str = Field(default="", description="商品英文名或关键词（CROSS 为英文库）")
    codes: list = Field(default_factory=list, description="本地候选编码，用于命中过滤")
    limit: int = Field(default=10, ge=1, le=50, description="返回的先例条数上限")


@app.post("/api/cross/precedents")
def api_cross_precedents(req: CrossPrecedentsRequest):
    """
    CBP 裁定先例检索（CROSS）。

    与 /api/search/ai 同一降级约定：外部服务失败返回 {'error': ...} 而非 5xx，
    前端静默保留本地结果——CROSS 是锦上添花，本地税则查询才是主链路。
    cross.precedents 自身承诺绝不抛异常，这里的 try 只兜模块加载。
    """
    if not (req.term or "").strip():
        raise HTTPException(status_code=400, detail="请输入英文检索词（CROSS 为英文库）")
    try:
        import cross
    except Exception as e:
        return {"error": f"CROSS 模块加载失败：{e}"}
    # 现行税则对账：裁定引用的编码可能早被修订删掉（90 年代裁定 42% 中招），
    # 而 CROSS 对此零标记。alive_codes 让 cross 层能标出「失效编码」
    alive = set(get_db()["rates_8"].keys())
    return cross.precedents(req.term.strip(), req.codes, limit=req.limit,
                            alive_codes=alive)


class CrossLocalRequest(BaseModel):
    codes: list = Field(default_factory=list, description="候选编码（8 位以上）")
    limit: int = Field(default=10, ge=1, le=50)


@app.post("/api/cross/local")
def api_cross_local(req: CrossLocalRequest):
    """
    本地镜像反查：候选编码历史上的全部先例（cross_sync.py 构建的 SQLite）。

    与 /api/cross/precedents（在线检索）互补：这里离线、毫秒级、计数完整；
    那里按商品词模糊召回、能发现候选外编码。镜像未构建时返回 {error}，
    前端静默跳过——没建镜像的用户仍有在线检索可用，不该被提示打扰。
    """
    if not req.codes:
        raise HTTPException(status_code=400, detail="请提供候选编码")
    try:
        import cross
    except Exception as e:
        return {"error": f"CROSS 模块加载失败：{e}"}
    alive = set(get_db()["rates_8"].keys())
    return cross.code_precedents(req.codes, limit=req.limit, alive_codes=alive)


class CrossSemanticRequest(BaseModel):
    query: str = Field(default="", description="商品描述（中英文皆可，语义匹配）")
    codes: list = Field(default_factory=list, description="候选编码，仅用于标命中，不过滤")
    limit: int = Field(default=10, ge=1, le=30)


@app.post("/api/cross/semantic")
def api_cross_semantic(req: CrossSemanticRequest):
    """
    语义找先例：中文描述直接检索英文裁定（本地向量索引 + ollama 查询嵌入）。
    索引未构建 / ollama 离线一律 {error} 降级，与其余 cross 接口同约定。
    """
    if not (req.query or "").strip():
        raise HTTPException(status_code=400, detail="请输入商品描述")
    try:
        import cross
    except Exception as e:
        return {"error": f"CROSS 模块加载失败：{e}"}
    alive = set(get_db()["rates_8"].keys())
    return cross.semantic_precedents(req.query.strip(), req.codes,
                                     limit=req.limit, alive_codes=alive)


class CrossDeepreadRequest(BaseModel):
    query: str = Field(default="", description="待归类商品描述")
    rulings: list = Field(default_factory=list,
                          description="召回的候选裁定（含 裁定号/来源/日期/编码/链接）")


@app.post("/api/cross/deepread")
def api_cross_deepread(req: CrossDeepreadRequest):
    """
    裁定正文深读：拉候选裁定正文，AI 定位最相似者并逐字摘录 CBP 原文。
    AI 只做定位+摘录，不生成归类意见；摘录经原文校验。
    AI 未配置 / 正文全拉失败一律 {error} 降级。
    """
    if not (req.query or "").strip():
        raise HTTPException(status_code=400, detail="请输入商品描述")
    if not req.rulings:
        raise HTTPException(status_code=400, detail="请提供候选裁定")
    try:
        import ai
    except Exception as e:
        return {"error": f"AI 模块加载失败：{e}"}
    return ai.deepread_precedents(req.query.strip(), req.rulings)


@app.get("/api/cross/text/{number}")
def api_cross_text(number: str, collection: str = "", date: str = ""):
    """
    站内查看裁定正文。rulings.cbp.gov 在境内直连经常打不开（用户点裁定号"没有反应"），
    而正文本来就由服务端拉取并永久缓存（深读用的同一份）——这里把它直接给到页面。
    collection / date 优先用调用方传的（先例行里有），没传则从本地镜像补；都没有 → error。
    """
    import cross
    number = re.sub(r"[^A-Za-z0-9]", "", number or "")[:16]
    if not number:
        raise HTTPException(status_code=400, detail="裁定号无效")
    collection = (collection or "").strip().lower()
    date = (date or "").strip()
    if not (collection and date[:4].isdigit()):
        meta = cross.ruling_meta(number)
        if meta:
            collection, date = meta
    if not (collection and date[:4].isdigit()):
        return {"error": f"缺少裁定 {number} 的库别/年份，无法定位正文", "链接": cross.RULING_URL.format(number=number)}
    text = cross.fetch_ruling_text(number, collection, date)
    if not text:
        return {"error": "正文拉取失败（CBP 服务无响应或文件不可解析），可稍后重试或打开官网链接",
                "链接": cross.RULING_URL.format(number=number)}
    return {"裁定号": number, "库别": collection.upper(), "日期": date,
            "链接": cross.RULING_URL.format(number=number),
            "全文链接": f"{cross.BASE_URL}/api/getdoc/{collection}/{date[:4]}/{number}.doc",
            "正文": text, "段落": cross.split_ruling_sections(text)}


@app.post("/api/estimate")
def api_estimate(req: EstimateRequest):
    """成本估算：按编码批量计算总税负（基础 + 301 + 附加税）"""
    import traceback
    try:
        import core
        import rate

        db = get_db()
        if req.items:
            # 逐行：每行用自己的单价折算，并算出该行货值与预估税费
            items = [dict(it) if isinstance(it, dict) else {"code": it} for it in req.items]
            for it in items:
                if it.get("unit_value") in (None, "") and req.unit_value:
                    it["unit_value"] = req.unit_value    # 未逐行填时沿用全局值
            out = rate.estimate_lines(db, items, origin=req.origin, ocean=req.ocean)
            if not out["rows"]:
                raise HTTPException(status_code=400, detail="未解析到任何 HTS 编码")
            return {"results": out["rows"], "count": len(out["rows"]),
                    "summary": out["summary"], "origin": req.origin}
        codes = req.codes or []
        if req.text and not codes:
            codes = core.extract_codes(req.text)
        if not codes:
            raise HTTPException(status_code=400, detail="未解析到任何 HTS 编码")
        results = [rate.calc_total(db, c, unit_value=req.unit_value, origin=req.origin) for c in codes]
        return {"results": results, "count": len(results), "unit_value": req.unit_value, "origin": req.origin}
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"估算服务端异常：{e}")


# 导入表头匹配：关务手里的底单列名五花八门（编码/HTS/税号/Code…），
# 认死一个列名等于逼用户改表。用正则各认一组同义写法，认不到再报错说清楚缺哪列。
# 实测：报关底单的编码列常写作「税则号列」，只认"税号"两个连着的字会整表导不进；
# 而"序号"里也有个"号"，所以不能放宽到裸的"号"字。
_IMPORT_COLS = {
    "code": re.compile(r"(编\s*码|编\s*号|税\s*则\s*号|税\s*号|商\s*品\s*码|hts|code)", re.I),
    "qty": re.compile(r"(数\s*量|件\s*数|qty|quantity|pcs)", re.I),
    "unit_value": re.compile(r"(单\s*价|单位货值|货\s*值|单\s*值|unit.?value|price)", re.I),
}


def _match_import_cols(columns):
    """表头 → {字段: 列名}。同一列被多个字段匹中时，先到先得。"""
    taken, out = set(), {}
    for field, pat in _IMPORT_COLS.items():
        for col in columns:
            if col in taken:
                continue
            if pat.search(str(col)):
                out[field] = col
                taken.add(col)
                break
    return out


@app.post("/api/estimate/import")
async def api_estimate_import(file: UploadFile = File(...)):
    """
    导入申报底单（xlsx/csv）→ 逐行估算的 items。

    只解析不计算：解析结果回前端填进表格，用户核对改动后再点估算。
    直接算完返回等于把"我们认成了什么"藏起来——列认错了也看不出来。
    """
    filename = file.filename or "未命名文件"
    content = await file.read()
    if len(content) > MAX_UPLOAD:
        raise HTTPException(status_code=400, detail=f"文件超过 {MAX_UPLOAD // 1048576}MB 限制")
    ext = os.path.splitext(filename)[1].lower()
    try:
        if ext in (".xlsx", ".xls"):
            df = pd.read_excel(io.BytesIO(content), sheet_name=0, dtype=str)
        elif ext == ".csv":
            df = pd.read_csv(io.StringIO(content.decode("utf-8-sig", errors="replace")), dtype=str)
        else:
            raise HTTPException(status_code=400, detail="仅支持 .xlsx / .xls / .csv")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"文件解析失败：{e}")

    cols = _match_import_cols(list(df.columns))
    if "code" not in cols:
        raise HTTPException(
            status_code=400,
            detail=f"没找到编码列。表头需含「编码 / 税号 / HTS / code」其中之一，"
                   f"当前表头：{'、'.join(str(c) for c in list(df.columns)[:8])}")

    db = get_db()
    items, skipped = [], []
    for _, row in df.iterrows():
        raw = str(row.get(cols["code"]) or "").strip()
        if not raw or raw.lower() == "nan":
            continue
        codes = core.extract_codes(raw)
        if not codes:
            skipped.append(raw[:40])
            continue

        def _num(field):
            if field not in cols:
                return None
            v = str(row.get(cols[field]) or "").strip().replace(",", "")
            try:
                f = float(v)
            except ValueError:
                return None
            return f if f > 0 else None

        items.append({"code": codes[0], "qty": _num("qty"), "unit_value": _num("unit_value")})

    if not items:
        raise HTTPException(status_code=400, detail=f"文件 {filename} 中未解析到 HTS 编码")
    return {"items": items, "count": len(items), "source": filename,
            "matched_columns": {k: str(v) for k, v in cols.items()},
            # 认不出的行要回报：静默丢行会让导出的清单比底单少几条，且没人发现
            "skipped": skipped[:20], "skipped_count": len(skipped)}


@app.get("/api/estimate/template")
def api_estimate_template():
    """下载逐行估算的导入模板（3 列 + 两行示例）"""
    rows = [
        {"HTS编码": "8507.60.00", "数量": 1000, "单位货值USD": 20},
        {"HTS编码": "0105.11.00", "数量": 500, "单位货值USD": 2},
    ]
    buf = io.BytesIO()
    pd.DataFrame(_defuse_rows(rows)).to_excel(buf, index=False, sheet_name="申报清单")
    buf.seek(0)
    fname = "hts_估算导入模板.xlsx"
    return Response(
        content=buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}"})


@app.get("/api/changes")
def api_changes():
    """最近一次数据更新后的变动清单（由 build_db.py 生成）"""
    import db_diff

    data = db_diff.load_changes()
    if data is None:
        return {"first_build": True, "message": "暂无变动记录（请先运行 python scripts/build_db.py 生成基准）"}
    return data


@app.post("/api/ai/classify")
def api_ai_classify(req: AIClassifyRequest):
    """AI 商品归类：描述 → 推荐 HTS 编码"""
    if not req.description.strip():
        raise HTTPException(status_code=400, detail="请提供商品描述")
    import ai

    return ai.classify_product(get_db(), req.description.strip(), origin=req.origin)


@app.post("/api/ai/ask")
def api_ai_ask(req: AIAskRequest):
    """AI 自然语言问税"""
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="请提供问题")
    import ai

    return ai.ask_tax_question(get_db(), req.question.strip(), origin=req.origin)


@app.post("/api/ai/interpret")
def api_ai_interpret(req: AIInterpretRequest):
    """AI 解读查询结果"""
    if not req.results:
        raise HTTPException(status_code=400, detail="无结果可解读")
    import ai

    return ai.interpret_results(req.results)


@app.post("/api/ai/analyze")
def api_ai_analyze(req: AIAnalyzeRequest):
    """AI 批量清单分析"""
    if not req.items:
        raise HTTPException(status_code=400, detail="清单为空")
    import ai

    return ai.analyze_list(get_db(), req.items, origin=req.origin)


@app.post("/api/ai/analyze/stream")
def api_ai_analyze_stream(req: AIAnalyzeRequest):
    """
    AI 批量清单分析（SSE 流式）。

    分开吐 details 与 report 是这个接口存在的理由：后端是三次批量 LLM 调用
    （出词 / 精排 / 报告），**表格在精排结束时就齐了，却要陪着报告再等一轮**。
    实测 4 行清单 14.3s 里报告占 4.2s——表格先落地，等待感少掉近三成。

    事件格式：每帧 `data: {json}\n\n`，type 见 ai.analyze_list_stream 的文档。
    """
    if not req.items:
        raise HTTPException(status_code=400, detail="清单为空")
    import ai

    db = get_db()

    def gen():
        try:
            for ev in ai.analyze_list_stream(db, req.items, origin=req.origin):
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        except Exception as e:      # 生成器里抛异常前端只会看到连接断开，必须转成事件
            traceback.print_exc()
            yield f"data: {json.dumps({'type': 'error', 'message': f'服务端异常：{e}'}, ensure_ascii=False)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        # 反向代理（本项目对外走 tailscale serve）默认会缓冲响应体，
        # 缓冲了就等于没流式——整包在最后一起到，进度条白做。
        "X-Accel-Buffering": "no",
    })


if __name__ == "__main__":
    import uvicorn

    print("HTS 301 关税查询工具已启动：http://127.0.0.1:5000")
    uvicorn.run(app, host="127.0.0.1", port=5000)
