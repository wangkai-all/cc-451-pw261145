"""HTTP 服务：页面（/...）与 JSON API（/api/...）双入口，零第三方依赖。

页面侧通过表单 + PRG（Post/Redirect/Get）操作；API 侧接收/返回 JSON。
所有用户输入在渲染时统一 HTML 转义；JSON 原文以 <pre> 展示。
"""

from __future__ import annotations

import html
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

from .converter import ConversionError, InvalidEvent
from .schemas import V1, V2, VERSIONS
from .service import NotFoundError, SchemaBridgeService

# ---------------------------------------------------------------------------
# 渲染辅助
# ---------------------------------------------------------------------------

_CSS = """
:root { --bd:#d7dce3; --bg:#f6f7f9; --pri:#2563eb; --ok:#15803d;
        --warn:#b45309; --bad:#b91c1c; --muted:#6b7280; }
* { box-sizing: border-box; }
body { margin:0; font-family: -apple-system, "Segoe UI", "PingFang SC",
       "Microsoft YaHei", sans-serif; background:var(--bg); color:#111827; }
header { background:#111827; color:#fff; padding:14px 28px;
         display:flex; align-items:center; gap:22px; flex-wrap:wrap; }
header h1 { font-size:18px; margin:0; }
header nav a { color:#cbd5e1; text-decoration:none; margin-right:14px;
               font-size:14px; }
header nav a:hover { color:#fff; }
main { max-width:1180px; margin:24px auto; padding:0 20px; }
h2 { font-size:18px; margin:26px 0 10px; }
h3 { font-size:15px; margin:18px 0 8px; }
.card { background:#fff; border:1px solid var(--bd); border-radius:10px;
        padding:16px 18px; margin-bottom:18px; }
table { border-collapse:collapse; width:100%; font-size:13px;
        background:#fff; }
th, td { border:1px solid var(--bd); padding:7px 9px; text-align:left;
         vertical-align:top; }
th { background:#f1f4f8; }
.badge { display:inline-block; padding:2px 9px; border-radius:999px;
         font-size:12px; font-weight:600; }
.badge.v1 { background:#e0e7ff; color:#3730a3; }
.badge.v2 { background:#dcfce7; color:#166534; }
.badge.ok, .badge.delivered, .badge.resolved, .badge.processed {
  background:#dcfce7; color:var(--ok); }
.badge.bad, .badge.failed, .badge.open, .badge.reopened, .badge.invalid {
  background:#fee2e2; color:var(--bad); }
.badge.warn, .badge.duplicate, .badge.unavailable {
  background:#fef3c7; color:var(--warn); }
pre { background:#0f172a; color:#e2e8f0; padding:12px; border-radius:8px;
      font-size:12px; overflow:auto; max-height:420px; }
pre.light { background:#f8fafc; color:#0f172a; border:1px solid var(--bd); }
input, select, textarea { font:inherit; padding:7px 9px; border:1px solid
       var(--bd); border-radius:7px; width:100%; }
textarea { font-family:ui-monospace, Menlo, Consolas, monospace;
       min-height:190px; }
label { font-size:13px; font-weight:600; display:block; margin:10px 0 4px; }
button, .btn { background:var(--pri); color:#fff; border:0; padding:8px 16px;
       border-radius:7px; cursor:pointer; font-size:13px;
       text-decoration:none; display:inline-block; margin-top:12px; }
.btn.secondary { background:#475569; }
.btn.small { padding:4px 10px; margin:0; font-size:12px; }
.row { display:flex; gap:16px; flex-wrap:wrap; }
.row > div { flex:1; min-width:240px; }
.kpis { display:flex; gap:14px; flex-wrap:wrap; }
.kpi { flex:1; min-width:150px; background:#fff; border:1px solid var(--bd);
       border-radius:10px; padding:14px 16px; }
.kpi .n { font-size:26px; font-weight:700; }
.kpi .l { color:var(--muted); font-size:12px; margin-top:3px; }
.muted { color:var(--muted); font-size:12px; }
.flash { padding:10px 14px; border-radius:8px; margin-bottom:14px;
         font-size:13px; }
.flash.ok { background:#dcfce7; color:#166534; }
.flash.err { background:#fee2e2; color:#991b1b; }
a { color:var(--pri); }
code { background:#eef2ff; padding:1px 5px; border-radius:4px;
      font-size:12px; }
"""


def esc(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def badge(text) -> str:
    cls = str(text).split()[0].lower()
    return f'<span class="badge {esc(cls)}">{esc(text)}</span>'


def pretty_json(value) -> str:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return esc(value)
    return esc(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def page(title: str, body: str, flash: tuple[str, str] | None = None) -> bytes:
    nav = [("/", "总览"), ("/diff", "模式差异"), ("/events", "事件双写"),
           ("/quarantine", "隔离队列"), ("/replay", "回放"),
           ("/consumers", "消费者绑定"), ("/impact", "下线影响报告")]
    nav_html = "".join(
        f'<a href="{href}">{esc(label)}</a>' for href, label in nav)
    flash_html = ""
    if flash:
        kind, msg = flash
        flash_html = f'<div class="flash {esc(kind)}">{esc(msg)}</div>'
    doc = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} · Schema Bridge</title><style>{_CSS}</style></head>
<body>
<header><h1>Schema Bridge 事件模式转换服务</h1><nav>{nav_html}</nav></header>
<main>{flash_html}{body}</main></body></html>"""
    return doc.encode("utf-8")


def table(headers: list[str], rows: list[list[str]]) -> str:
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body_rows = []
    for row in rows:
        body_rows.append("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>")
    if not body_rows:
        body_rows.append(
            f'<tr><td colspan="{len(headers)}" class="muted">暂无数据</td></tr>')
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>"


# ---------------------------------------------------------------------------
# 页面渲染
# ---------------------------------------------------------------------------

def render_dashboard(svc: SchemaBridgeService, flash=None) -> bytes:
    s = svc.stats()
    kpis = [
        ("事件总数", s["events_total"]),
        ("双写版本行数", s["converted_rows"]),
        ("隔离未解决", s["quarantine_open"]),
        ("已解决隔离", s["quarantine_resolved"]),
        ("消费者", s["consumers_total"]),
        ("回放批次", s["replay_runs"]),
        ("投递记录", s["deliveries"]),
    ]
    kpi_html = "".join(
        f'<div class="kpi"><div class="n">{v}</div><div class="l">{esc(k)}</div></div>'
        for k, v in kpis)
    cv = s["consumers_by_version"]
    body = f"""
<h2>运行总览</h2>
<div class="kpis">{kpi_html}</div>
<div class="card"><h3>消费者版本分布</h3>
<p>{badge(V1)} {cv.get(V1, 0)} 个　{badge(V2)} {cv.get(V2, 0)} 个</p>
<p class="muted">服务对每个事件同时双写 v1/v2 两个版本；消费者按绑定版本取数，
重复摄入与重复投递均幂等，转换失败保留原文进入隔离队列。</p></div>
<div class="card"><h3>快速摄入事件</h3>
<form method="post" action="/ingest">
<label>事件原文（JSON，v1 或 v2 均可）</label>
<textarea name="payload" placeholder='{{"event_id":"evt-x", ...}}'></textarea>
<button type="submit">摄入并双写</button></form></div>"""
    return page("总览", body, flash)


def render_diff(svc: SchemaBridgeService, flash=None) -> bytes:
    diff = svc.schema_diff()
    mapping_rows = [[esc(a), esc(b), esc(kind), esc(note)]
                    for a, b, kind, note in diff["mappings"]]
    v1_rows = [[esc(p), esc(t), "是" if req else "否", esc(d)]
               for p, t, req, d in diff["v1_fields"]]
    v2_rows = [[esc(p), esc(t), "是" if req else "否", esc(d)]
               for p, t, req, d in diff["v2_fields"]]
    body = f"""
<h2>v1 / v2 模式差异</h2>
<div class="card"><p>{esc(diff["summary"])}</p>
<p class="muted">转换方向：{esc(diff["direction"])}</p></div>
<div class="card"><h3>字段映射（v1 → v2）</h3>
{table(["v1 路径", "v2 路径", "变化类型", "说明"], mapping_rows)}</div>
<div class="row">
<div class="card"><h3>{badge("v1")} 结构</h3>
{table(["字段", "类型", "必填", "说明"], v1_rows)}</div>
<div class="card"><h3>{badge("v2")} 结构</h3>
{table(["字段", "类型", "必填", "说明"], v2_rows)}</div></div>
<div class="card"><h3>反向转换（v2 → v1 回放）丢弃的字段</h3>
<ul>{"".join(f"<li>{esc(x)}</li>" for x in diff["dropped_on_reverse"])}</ul></div>"""
    return page("模式差异", body, flash)


def render_events(svc: SchemaBridgeService, flash=None) -> bytes:
    events = svc.list_events()
    rows = []
    for e in events:
        versions = " ".join(badge(v) for v in e["versions"])
        q = "".join(
            f'{badge(x["status"])}<br><span class="muted">→{esc(x["target_version"])} '
            f'(<a href="/quarantine?id={x["id"]}">#{x["id"]}</a>)</span>'
            for x in e["quarantine"]) or '<span class="muted">—</span>'
        rows.append([
            f'<a href="/events/{quote(e["event_id"])}">{esc(e["event_id"])}</a>',
            badge(e["source_version"]), versions, q, esc(e["first_seen_at"])])
    body = f"""
<h2>事件与双写结果（{len(events)}）</h2>
<div class="card">{table(
    ["event_id", "来源版本", "已转换版本", "隔离记录", "首次摄入时间"], rows)}
</div>
<div class="card"><h3>摄入新事件</h3>
<form method="post" action="/ingest">
<textarea name="payload"></textarea>
<button type="submit">摄入并双写</button></form></div>"""
    return page("事件双写", body, flash)


def render_event_detail(svc: SchemaBridgeService, event_id: str, flash=None) -> bytes:
    detail = svc.get_event_detail(event_id)
    converted = "".join(
        f'<h3>{badge(r["version"])} 转换结果</h3><pre>{pretty_json(r["payload"])}</pre>'
        for r in detail["converted"]) or '<p class="muted">无成功转换结果</p>'
    q_rows = [[f'<a href="/quarantine?id={q["id"]}">#{q["id"]}</a>',
               esc(q["target_version"]), badge(q["status"]),
               esc(q["error_message"]), esc(q["attempts"]),
               esc(q["updated_at"])]
              for q in detail["quarantine"]]
    d_rows = [[esc(d["consumer_name"]), badge(d["version"]),
               badge(d["status"]), esc(d["created_at"])]
              for d in detail["deliveries"]]
    body = f"""
<h2>事件 {esc(event_id)}</h2>
<div class="card"><h3>原文（{badge(detail["source_version"])}）</h3>
<pre>{pretty_json(detail["source_payload"])}</pre></div>
<div class="card">{converted}</div>
<div class="card"><h3>隔离记录</h3>
{table(["#", "目标版本", "状态", "错误", "尝试次数", "更新时间"], q_rows)}</div>
<div class="card"><h3>投递记录</h3>
{table(["消费者", "版本", "状态", "时间"], d_rows)}</div>
<p><a class="btn secondary" href="/events">← 返回事件列表</a></p>"""
    return page("事件详情", body, flash)


def render_quarantine(svc: SchemaBridgeService, selected_id: int | None = None, flash=None) -> bytes:
    records = svc.list_quarantine()
    rows = [[
        f'<a href="/quarantine?id={q["id"]}">#{q["id"]}</a>',
        esc(q["event_id"] or "（无法提取）"),
        badge(q["source_version"] or "unknown"),
        esc(q["target_version"]), badge(q["status"]),
        esc(q["error_message"])[:120], esc(q["attempts"]),
        esc(q["created_at"])] for q in records]
    detail_html = ""
    if selected_id is not None:
        q = svc.get_quarantine_record(selected_id)
        detail_html = f"""
<div class="card"><h3>隔离样本 #{q["id"]} {badge(q["status"])}</h3>
<p><b>event_id：</b>{esc(q["event_id"] or "（无法提取）")}
<b>目标版本：</b>{esc(q["target_version"])}　<b>尝试：</b>{q["attempts"]}</p>
<p><b>错误：</b>{esc(q["error_message"])}</p>
<details open><summary class="muted">字段级错误</summary>
<pre class="light">{pretty_json(q["error_detail"])}</pre></details>
<details open><summary class="muted">保留的原文（永不丢弃）</summary>
<pre>{pretty_json(q["raw_payload"])}</pre></details>
<form method="post" action="/replay">
<input type="hidden" name="quarantine_id" value="{q["id"]}">
<label>修正后原文回放（可选；留空则用原原文重试）</label>
<textarea name="corrected" placeholder='可在此修正字段后重新回放'></textarea>
<button type="submit">回放此样本</button></form></div>"""
    body = f"""
<h2>隔离队列（{len(records)}）</h2>
<div class="card">{table(
    ["#", "event_id", "来源", "目标版本", "状态", "错误信息", "尝试", "时间"], rows)}
</div>{detail_html}"""
    return page("隔离队列", body, flash)


def render_replay(svc: SchemaBridgeService, flash=None) -> bytes:
    runs = svc.list_replay_runs()
    run_rows = []
    for r in runs:
        results = "<br>".join(
            f'{badge(x["status"])} #{esc(x["quarantine_id"])} '
            f'{esc(x["event_id"] or "")} '
            f'{"(修正)" if x["corrected"] else ""} — {esc(x["message"])[:80]}'
            for x in r["results"])
        run_rows.append([r["id"], esc(r["scope"]), r["total"], r["succeeded"],
                         r["failed"], results, esc(r["created_at"])])
    body = f"""
<h2>回放中心</h2>
<div class="card"><h3>触发回放</h3>
<form method="post" action="/replay">
<button type="submit" name="all_open" value="1">回放全部未解决隔离事件</button>
</form>
<form method="post" action="/replay" style="margin-top:8px">
<label>按 event_id 批量回放（逗号分隔）</label>
<input name="event_ids" placeholder="evt-100, evt-101">
<button type="submit">批量回放</button></form>
<p class="muted">单条样本回放请进入「隔离队列」打开样本后操作，支持提交修正原文。</p>
</div>
<div class="card"><h3>历史回放批次（{len(runs)}）</h3>
{table(["批次", "范围", "总数", "成功", "失败", "明细", "时间"], run_rows)}</div>"""
    return page("回放", body, flash)


def render_consumers(svc: SchemaBridgeService, flash=None) -> bytes:
    consumers = svc.list_consumers()
    rows = []
    for c in consumers:
        bind_form = f"""
<form method="post" action="/consumers/bind" style="display:flex;gap:6px;align-items:center">
<input type="hidden" name="name" value="{esc(c["name"])}">
<select name="version" style="width:auto">
<option value="v1" {"selected" if c["bound_version"] == V1 else ""}>v1</option>
<option value="v2" {"selected" if c["bound_version"] == V2 else ""}>v2</option>
</select>
<button class="btn small" type="submit">切换</button></form>"""
        rows.append([esc(c["name"]), esc(c["description"]),
                     badge(c["bound_version"]), bind_form, esc(c["updated_at"])])
    history = svc.binding_history()
    h_rows = [[esc(h["consumer_name"]),
               badge(h["from_version"] or "—"), badge(h["to_version"]),
               esc(h["reason"]), esc(h["changed_at"])] for h in history]
    body = f"""
<h2>消费者绑定（按消费者选择版本，支持部分升级与回滚）</h2>
<div class="card"><h3>注册消费者</h3>
<form method="post" action="/consumers/register" class="row">
<div><label>名称</label><input name="name" placeholder="risk-engine"></div>
<div><label>说明</label><input name="description" placeholder="风控服务"></div>
<div><label>初始版本</label><select name="version">
<option value="v1">v1</option><option value="v2">v2</option></select></div>
<div><button type="submit">注册</button></div></form></div>
<div class="card"><h3>消费者列表</h3>
{table(["名称", "说明", "绑定版本", "切换绑定（升级/回滚）", "更新时间"], rows)}</div>
<div class="card"><h3>绑定变更审计</h3>
{table(["消费者", "从", "到", "原因", "时间"], h_rows)}</div>"""
    return page("消费者绑定", body, flash)


def render_impact(svc: SchemaBridgeService, version: str = V1,
                  report: dict | None = None, flash=None) -> bytes:
    options = "".join(
        f'<option value="{v}" {"selected" if v == version else ""}>{v}</option>'
        for v in VERSIONS)
    report_html = ""
    if report:
        consumers = report["affected_consumers"]
        c_rows = [[esc(c["name"]), esc(c["description"]),
                   badge(c["bound_version"]),
                   c["delivered_on_retiring_version"],
                   " → ".join(
                       f'{badge(h["from_version"] or "—")}→{badge(h["to_version"])}'
                       f'({esc(h["reason"])})'
                       for h in c["binding_history"]) or "—"]
                  for c in consumers]
        blocker_rows = [[f'<a href="/quarantine?id={q["id"]}">#{q["id"]}</a>',
                         esc(q["event_id"] or "—"), esc(q["target_version"]),
                         esc(q["error_message"])[:100]]
                        for q in report["blocking_quarantine"]]
        report_html = f"""
<div class="card"><h3>评估结论：{badge(report["verdict"])}</h3>
<p>下线 <b>{esc(report["retire_version"])}</b>，替代版本
<b>{esc(report["alternative_version"])}</b>；生成于 {esc(report["generated_at"])}</p>
<ul>{"".join(f"<li>{esc(r)}</li>" for r in report["reasons"])}</ul>
<p>事件双写就绪：{report["alternative_ready"]}/{report["events_total"]}
（缺失 {report["alternative_missing"]}）</p></div>
<div class="card"><h3>受影响消费者（{len(consumers)}）</h3>
{table(["消费者", "说明", "绑定", "旧版本投递量", "绑定历史"], c_rows)}</div>
<div class="card"><h3>阻塞性隔离事件（{len(blocker_rows)}）</h3>
{table(["#", "event_id", "目标版本", "错误"], blocker_rows)}</div>"""
    body = f"""
<h2>版本下线消费者影响报告</h2>
<div class="card"><form method="get" action="/impact" class="row">
<div><label>计划下线的版本</label><select name="version">{options}</select></div>
<div><button type="submit">生成报告</button></div></form></div>
{report_html}"""
    return page("影响报告", body, flash)


# ---------------------------------------------------------------------------
# HTTP Handler
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "SchemaBridge/1.0"

    # 注入点：ThreadingHTTPServer 会把 service 挂在 server 上
    @property
    def svc(self) -> SchemaBridgeService:
        return self.server.service  # type: ignore[attr-defined]

    def log_message(self, fmt, *args):  # 安静日志
        pass

    # -- 响应工具 --------------------------------------------------------

    def _send_html(self, content: bytes, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _send_json(self, payload: dict, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except json.JSONDecodeError as exc:
            raise ValueError(f"请求体不是合法 JSON：{exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return data

    def _form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        parsed = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
        return {k: v[0] for k, v in parsed.items()}

    # -- GET -------------------------------------------------------------

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        flash = ("ok", query["flash"][0]) if "flash" in query else None
        try:
            if path.startswith("/api/"):
                return self._api_get(path, query)
            if path == "/":
                return self._send_html(render_dashboard(self.svc))
            if path == "/diff":
                return self._send_html(render_diff(self.svc, flash))
            if path == "/events":
                return self._send_html(render_events(self.svc, flash))
            if path.startswith("/events/"):
                return self._send_html(
                    render_event_detail(
                        self.svc, path.split("/", 2)[2], flash))
            if path == "/quarantine":
                selected = int(query["id"][0]) if "id" in query else None
                return self._send_html(
                    render_quarantine(self.svc, selected, flash))
            if path == "/replay":
                return self._send_html(render_replay(self.svc, flash))
            if path == "/consumers":
                return self._send_html(render_consumers(self.svc, flash))
            if path == "/impact":
                version = query.get("version", [V1])[0]
                if version not in VERSIONS:
                    version = V1
                return self._send_html(
                    render_impact(
                        self.svc, version,
                        self.svc.impact_report(version), flash))
            self._send_html(page("未找到", "<p>页面不存在</p>"), 404)
        except NotFoundError as exc:
            self._send_html(page("未找到", f"<p>{esc(exc)}</p>"), 404)
        except (ValueError, KeyError) as exc:
            self._send_html(page("请求错误", f"<p>{esc(exc)}</p>"), 400)

    def _api_get(self, path: str, query: dict) -> None:
        if path == "/api/diff":
            return self._send_json(self.svc.schema_diff())
        if path == "/api/stats":
            return self._send_json(self.svc.stats())
        if path == "/api/events":
            return self._send_json({"events": self.svc.list_events()})
        if path.startswith("/api/events/"):
            event_id = path.split("/", 3)[3]
            return self._send_json(self.svc.get_event_detail(event_id))
        if path == "/api/quarantine":
            status = query.get("status", [None])[0]
            return self._send_json(
                {"quarantine": self.svc.list_quarantine(status)})
        if path == "/api/replay":
            return self._send_json({"runs": self.svc.list_replay_runs()})
        if path == "/api/consumers":
            return self._send_json({"consumers": self.svc.list_consumers()})
        if path == "/api/impact":
            version = query.get("version", [V1])[0]
            return self._send_json(self.svc.impact_report(version))
        self._send_json({"error": "接口不存在"}, 404)

    # -- POST ------------------------------------------------------------

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        is_api = path.startswith("/api/")
        try:
            if is_api:
                data = self._read_json()
                result = self._post_action(path, data)
                return self._send_json(result, 200)
            form = self._form()
            location, flash = self._post_form(path, form)
            self._redirect(f"{location}?flash={quote(flash)}")
        except NotFoundError as exc:
            self._handle_post_error(exc, is_api, 404)
        except (ValueError, KeyError, InvalidEvent, ConversionError) as exc:
            self._handle_post_error(exc, is_api, 400)

    def _handle_post_error(self, exc: Exception, is_api: bool,
                           status: int) -> None:
        if is_api:
            payload = {"error": str(exc)}
            if isinstance(exc, ConversionError):
                payload["errors"] = exc.errors
            return self._send_json(payload, status)
        self._send_html(
            page("操作失败", f"<div class='flash err'>{esc(exc)}</div>"
                            "<p><a class='btn secondary' href='/'>返回总览</a></p>"),
            status)

    def _post_action(self, path: str, data: dict) -> dict:
        if path == "/api/ingest":
            if "payload" in data:
                return self.svc.ingest(data["payload"])
            return self.svc.ingest(data)
        if path == "/api/replay":
            corrected = data.get("corrected")
            return self.svc.replay(
                quarantine_id=data.get("quarantine_id"),
                all_open=bool(data.get("all_open", False)),
                event_ids=data.get("event_ids"), corrected=corrected)
        if path == "/api/consumers/register":
            return self.svc.register_consumer(
                data["name"], data["version"], data.get("description", ""))
        if path == "/api/consumers/bind":
            return self.svc.bind_consumer(
                data["name"], data["version"], data.get("reason", ""))
        if path == "/api/deliver":
            return self.svc.deliver(data["event_id"], data["consumer"])
        if path == "/api/deliver-all":
            return {"results": self.svc.deliver_to_all(data["event_id"])}
        raise NotFoundError(f"接口 {path} 不存在")

    def _post_form(self, path: str, form: dict[str, str]) -> tuple[str, str]:
        if path == "/ingest":
            try:
                payload = json.loads(form.get("payload", ""))
            except json.JSONDecodeError as exc:
                raise ValueError(f"原文不是合法 JSON：{exc}") from exc
            result = self.svc.ingest(payload)
            if result["status"] == "invalid":
                return "/quarantine", "事件结构无法识别，已保留原文进入隔离队列"
            failed = [r for r in result.get("results", []) if not r["ok"]]
            if failed:
                return "/quarantine", (
                    f"事件 {result['event_id']} 部分版本转换失败，已进入隔离队列")
            if result["status"] == "duplicate":
                return f"/events/{result['event_id']}", "重复事件，幂等跳过转换"
            return f"/events/{result['event_id']}", "摄入成功，已双写 v1/v2"
        if path == "/replay":
            kwargs: dict = {}
            if form.get("quarantine_id"):
                kwargs["quarantine_id"] = int(form["quarantine_id"])
            elif form.get("all_open"):
                kwargs["all_open"] = True
            elif form.get("event_ids", "").strip():
                kwargs["event_ids"] = [
                    x.strip() for x in form["event_ids"].split(",") if x.strip()]
            else:
                raise ValueError("未指定回放范围")
            corrected_text = form.get("corrected", "").strip()
            if corrected_text:
                try:
                    kwargs["corrected"] = json.loads(corrected_text)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"修正原文不是合法 JSON：{exc}") from exc
            result = self.svc.replay(**kwargs)
            return "/replay", (
                f"回放批次 #{result['run_id']} 完成："
                f"成功 {result['succeeded']} / 失败 {result['failed']}")
        if path == "/consumers/register":
            self.svc.register_consumer(
                form["name"], form["version"], form.get("description", ""))
            return "/consumers", f"消费者 {form['name']} 注册成功"
        if path == "/consumers/bind":
            c = self.svc.bind_consumer(form["name"], form["version"])
            return "/consumers", (
                f"消费者 {form['name']} 已切换到 {c['bound_version']}")
        raise NotFoundError(f"表单动作 {path} 不存在")


def build_server(db_path: str, host: str = "127.0.0.1",
                 port: int = 8000) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), Handler)
    server.service = SchemaBridgeService(db_path)  # type: ignore[attr-defined]
    return server


def serve(db_path: str, host: str = "127.0.0.1", port: int = 8000) -> None:
    server = build_server(db_path, host, port)
    print(f"Schema Bridge 已启动：http://{host}:{port} （Ctrl+C 退出）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭……")
    finally:
        server.service.close()
        server.server_close()
