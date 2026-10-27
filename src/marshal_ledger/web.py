"""Read-only web view of a ledger store.

Server-rendered HTML with inline styles and no external resources, so it runs
inside the organization's network without calling out. Every value from the
store is escaped. Pages: the inventory, an agent (environments, definition,
changes against what is shipped, portability, history), and dependents of an
entity. The same data is available as JSON under ``/v0/``.

``export_snapshot`` writes the pages as static files, for publishing a sample
ledger such as ``marshal demo`` without running a server.

Authentication is delegated to the organization's SSO proxy: requests must
carry the configured user header, which the proxy sets after sign-in.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import re
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, ConfigDict

from . import __version__
from .dbauth import PasswordProvider
from .diff import diff_specs
from .model import AgentVersion
from .risk import rank
from .store import Store, StoreError, StoreUnavailable


class WebSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    user_header: str = "X-Forwarded-Email"
    """Header the SSO proxy sets with the signed-in user. Requests without it are refused."""
    allow_unauthenticated: bool = False
    """Serve without the user header. Only for local use (``marshal web``)."""
    portability_targets: list[str] = ["bedrock", "copilot-studio", "agentforce", "google-adk", "databricks"]


_STYLE = """
:root {
  color-scheme: light dark;
  --page:#fbfbfa; --surface:#fff; --side:#f4f4f2; --hover:#f3f2f0; --line:#e8e7e4; --line-2:#d9d7d3;
  --fg:#1d1c1b; --fg-2:#5e5c58; --fg-3:#8c8a85; --accent:#5b5bd6; --accent-fg:#fff; --ring:#5b5bd655;
  --red:#b42318; --red-bg:#fdf0ed; --amber:#975a00; --amber-bg:#fdf5e4; --green:#1d7a3e; --green-bg:#ebf6ee;
  --blue:#2f5fb3; --blue-bg:#edf2fb; --gray-bg:#f1f0ee;
  --mono:ui-monospace,"SF Mono",SFMono-Regular,Menlo,Consolas,monospace;
}
@media (prefers-color-scheme: dark) { :root {
  --page:#141413; --surface:#1a1a19; --side:#111110; --hover:#222220; --line:#292826; --line-2:#383734;
  --fg:#ecebe8; --fg-2:#a6a49f; --fg-3:#76746f; --accent:#8d8ef6; --accent-fg:#141413; --ring:#8d8ef666;
  --red:#ff9580; --red-bg:#ff6a4a1a; --amber:#f2b85b; --amber-bg:#f2a8331a; --green:#6fd08f; --green-bg:#4cc2731a;
  --blue:#93b4f5; --blue-bg:#6f99f01a; --gray-bg:#ffffff0d;
} }
* { box-sizing:border-box; }
html { -webkit-text-size-adjust:100%; }
body { margin:0; background:var(--page); color:var(--fg);
  font:14px/1.5 "Inter var",Inter,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  -webkit-font-smoothing:antialiased; }
a { color:inherit; text-decoration:none; }
a:hover { color:var(--accent); }
:focus-visible { outline:2px solid var(--accent); outline-offset:2px; border-radius:4px; }
.shell { display:grid; grid-template-columns:220px minmax(0,1fr); min-height:100vh; }
.side { background:var(--side); border-right:1px solid var(--line); padding:14px 10px; display:flex; flex-direction:column; gap:18px;
  position:sticky; top:0; height:100vh; }
.brand { display:flex; align-items:center; gap:9px; padding:4px 8px; font-weight:600; letter-spacing:-.01em; }
.mark { width:22px; height:22px; border-radius:5px; flex:none; display:block; box-shadow:0 0 0 1px var(--line-2); }
.side nav { display:flex; flex-direction:column; gap:1px; }
.side nav a { display:flex; align-items:center; gap:9px; padding:6px 8px; border-radius:6px; color:var(--fg-2); font-size:13px; font-weight:500; }
.side nav a:hover { background:var(--hover); color:var(--fg); }
.side nav a.on { background:var(--surface); color:var(--fg); box-shadow:0 0 0 1px var(--line); }
.side nav svg { width:15px; height:15px; flex:none; stroke:currentColor; fill:none; stroke-width:1.6; stroke-linecap:round; stroke-linejoin:round; }
.side .who { margin-top:auto; padding:8px; font-size:12px; color:var(--fg-2); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
main { padding:28px 36px 64px; max-width:1240px; width:100%; }
.crumbs { font-size:13px; color:var(--fg-2); margin-bottom:10px; } .crumbs a:hover { color:var(--fg); }
.head { display:flex; align-items:flex-start; justify-content:space-between; gap:16px; flex-wrap:wrap; margin-bottom:20px; }
h1 { font-size:22px; line-height:1.25; font-weight:600; letter-spacing:-.02em; margin:0; display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
.sub { color:var(--fg-2); margin:4px 0 0; font-size:13px; overflow-wrap:anywhere; }
.lede { margin:12px 0 0; max-width:72ch; color:var(--fg); }
h2 { font-size:13px; font-weight:600; margin:0; letter-spacing:-.005em; }
.muted { color:var(--fg-2); } .mono { font-family:var(--mono); font-size:12px; }
.id { font-family:var(--mono); font-size:11.5px; color:var(--fg-2); margin-top:1px; overflow-wrap:anywhere; }
.num { text-align:right; font-variant-numeric:tabular-nums; }
.stats { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; margin-bottom:20px; }
.stat { background:var(--surface); border:1px solid var(--line); border-radius:10px; padding:12px 14px; }
.stat .k { font-size:12px; color:var(--fg-2); } .stat .v { font-size:20px; font-weight:600; letter-spacing:-.02em; font-variant-numeric:tabular-nums; margin-top:2px; }
.stat .v small { font-size:12px; font-weight:400; color:var(--fg-2); margin-left:4px; letter-spacing:0; }
.card { background:var(--surface); border:1px solid var(--line); border-radius:10px; margin-bottom:16px; overflow:hidden; }
.card > header { display:flex; align-items:center; justify-content:space-between; gap:12px; padding:11px 16px; border-bottom:1px solid var(--line); }
.card > header .muted { font-size:12px; }
.card > p, .card > pre, .card > dl { margin:0; padding:14px 16px; }
.grid2 { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:16px; } .grid2 > .card { margin-bottom:16px; }
.scroll { overflow-x:auto; }
.card > .scroll + .scroll { border-top:1px solid var(--line); }
table { border-collapse:collapse; width:100%; font-size:13px; }
th, td { text-align:left; padding:9px 16px; vertical-align:top; }
th { font-size:12px; font-weight:500; color:var(--fg-2); white-space:nowrap; border-bottom:1px solid var(--line); position:sticky; top:0; background:var(--surface); }
td { border-bottom:1px solid var(--line); }
tbody tr:last-child td { border-bottom:0; }
tbody tr:hover td { background:var(--hover); }
td a.name { font-weight:500; overflow-wrap:anywhere; }
td:first-child { min-width:220px; }
.badge { display:inline-flex; align-items:center; gap:6px; padding:1px 8px 1px 7px; border-radius:999px; font-size:12px; font-weight:500;
  line-height:20px; white-space:nowrap; background:var(--gray-bg); color:var(--fg-2); }
.badge::before { content:""; width:6px; height:6px; border-radius:50%; background:currentColor; }
.badge.plain::before { display:none; }
.badges { display:flex; gap:4px; flex-wrap:wrap; }
.drift, .HIGH { background:var(--red-bg); color:var(--red); }
.stale, .MED { background:var(--amber-bg); color:var(--amber); }
.current { background:var(--green-bg); color:var(--green); }
.LOW { background:var(--blue-bg); color:var(--blue); }
.chip { display:inline-block; padding:0 7px; border:1px solid var(--line); border-radius:6px; font-size:12px; line-height:20px; color:var(--fg-2); white-space:nowrap; }
.unknown { color:var(--fg-3); font-style:italic; }
.derived { text-decoration:underline dotted var(--fg-3); text-underline-offset:3px; }
.attested { text-decoration:underline dashed var(--fg-3); text-underline-offset:3px; }
.meter { display:inline-flex; align-items:center; gap:8px; font-variant-numeric:tabular-nums; }
.meter i { display:block; width:44px; height:4px; border-radius:2px; background:var(--line); overflow:hidden; }
.meter i b { display:block; height:100%; background:var(--accent); border-radius:2px; }
.meter.big i { width:100%; height:5px; margin-top:8px; } .meter.big { display:block; }
.toolbar { display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin-bottom:12px; }
.search { position:relative; flex:1 1 260px; max-width:360px; }
.search svg { position:absolute; left:9px; top:50%; transform:translateY(-50%); width:14px; height:14px; stroke:var(--fg-3); fill:none; stroke-width:1.8; stroke-linecap:round; }
.search input { width:100%; padding-left:30px; }
input, select, button, .btn { font:inherit; font-size:13px; height:32px; padding:0 10px; border:1px solid var(--line-2); border-radius:7px;
  background:var(--surface); color:var(--fg); }
input::placeholder { color:var(--fg-3); }
input:focus { outline:none; border-color:var(--accent); box-shadow:0 0 0 3px var(--ring); }
input[type=checkbox] { height:auto; accent-color:var(--accent); }
label.check { display:inline-flex; align-items:center; gap:6px; font-size:13px; color:var(--fg-2); }
button { background:var(--fg); color:var(--page); border-color:var(--fg); font-weight:500; cursor:pointer; }
button:hover { opacity:.88; }
.btn { display:inline-flex; align-items:center; color:var(--fg-2); }
pre { white-space:pre-wrap; font-family:inherit; font-size:13px; line-height:1.6; max-height:360px; overflow:auto; }
dl { display:grid; grid-template-columns:max-content 1fr; gap:8px 20px; font-size:13px; }
dt { color:var(--fg-2); } dd { margin:0; min-width:0; overflow-wrap:anywhere; }
.empty { color:var(--fg-2); font-size:13px; }
.trunc { display:inline-block; max-width:190px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; vertical-align:bottom; }
.limits { font-size:12px; color:var(--fg-2); line-height:1.6; }
.snapshot { font-size:13px; color:var(--fg-2); background:var(--side); border:1px solid var(--line); border-radius:8px; padding:8px 12px; margin:0 0 20px; }
.nowrap { white-space:nowrap; }
@media (max-width: 900px) { .grid2 { grid-template-columns:1fr; } }
@media (max-width: 760px) {
  .shell { grid-template-columns:minmax(0,1fr); }
  .side { position:static; height:auto; flex-direction:row; align-items:center; padding:8px 12px; gap:8px; border-right:0; border-bottom:1px solid var(--line); }
  .side nav { flex-direction:row; } .side .who { display:none; }
  main { padding:20px 16px 48px; }
}
"""

_LOGO_SHAPES = (
    '<rect width="488" height="488" fill="#1a1a1a"/>'
    '<path fill="#fff" d="M66 377V96h94l84 109 84-109h94v281h-38V132h-35L244 268 139 132h-35v245z"/>'
    '<rect x="174" y="309" width="140" height="14" fill="#8f8f8f"/>'
)
_LOGO = f'<svg class="mark" viewBox="0 0 488 488" aria-hidden="true">{_LOGO_SHAPES}</svg>'
_FAVICON = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 488 488">{_LOGO_SHAPES}</svg>'

_ICONS = {
    "agents": '<svg viewBox="0 0 16 16" aria-hidden="true"><rect x="2.5" y="4" width="11" height="9" rx="2.5"/><path d="M8 4V1.8"/>'
              '<circle cx="6" cy="8.5" r=".6"/><circle cx="10" cy="8.5" r=".6"/></svg>',
    "deps": '<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="4" cy="3.5" r="1.6"/><circle cx="4" cy="12.5" r="1.6"/>'
            '<circle cx="12" cy="8" r="1.6"/><path d="M4 5.1v5.8M5.4 4.3 10.6 7.2"/></svg>',
    "search": '<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="7" cy="7" r="4.5"/><path d="m10.5 10.5 3 3"/></svg>',
}


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _page(title: str, body: str, user: Optional[str], active: str = "") -> HTMLResponse:
    who = f'<div class="who" title="{_e(user)}">{_e(user)}</div>' if user else ""
    nav = "".join(
        f'<a href="{href}"{" class=on" if key == active else ""}>{_ICONS[key]}{label}</a>'
        for key, href, label in (("agents", "./", "Agents"), ("deps", "deps", "Dependencies"))
    )
    return HTMLResponse(
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        f'<title>{_e(title)} · Marshal</title><link rel=icon href="favicon.svg"><style>{_STYLE}</style></head><body><div class=shell>'
        f'<aside class=side><a class=brand href="./">{_LOGO}Marshal</a><nav>{nav}</nav>{who}</aside>'
        f"<main>{body}</main></div></body></html>"
    )


def _agent_link(agent: str, label: Optional[str] = None) -> str:
    return f'<a class=name href="agent?{urlencode({"id": agent})}">{_e(label or agent)}</a>'


def _value(v: Any, extra: str = "") -> str:
    if not v.is_known:
        return f'<span class="unknown" title="{_e(v.basis)}">unknown</span>'
    value = getattr(v.value, "value", v.value)
    if isinstance(value, bool):
        value = "Yes" if value else "No"
    marked = v.provenance.value if v.provenance.value in ("derived", "attested") else ""
    css = " ".join(c for c in (marked, extra) if c)
    css = f' class="{css}"' if css else ""
    title = f"{v.provenance.value}: {v.basis}"
    if extra == "trunc":
        title = f"{value}\n{title}"
    return f'<span{css} title="{_e(title)}">{_e(value)}</span>'


def _legend(spec: Any) -> str:
    attested = any(v.provenance.value == "attested" for t in spec.tools for v in (t.scope, t.reversible, t.requires_confirmation))
    return "Dotted values are derived" + ("; dashed values are attested by a person" if attested else "") + "; hover for the basis"


def _badge(css: str, text: str, title: str = "") -> str:
    return f'<span class="badge {_e(css)}"' + (f' title="{_e(title)}"' if title else "") + f">{_e(text)}</span>"


def _state(st: Any) -> str:
    return "drift" if st.drift else "stale" if st.stale else "current"


def _meter(fraction: float, big: bool = False, unit: str = "%") -> str:
    pct = max(0, min(100, round(fraction * 100)))
    if big:
        return f'<span class="meter big">{pct}{unit}<i><b style="width:{pct}%"></b></i></span>'
    return f'<span class="meter"><i><b style="width:{pct}%"></b></i>{pct}{unit}</span>'


def _card(title: str, content: str, note: str = "") -> str:
    aside = f'<span class="muted">{note}</span>' if note else ""
    return f"<section class=card><header><h2>{_e(title)}</h2>{aside}</header>{content}</section>"


def _count(n: int, noun: str) -> str:
    return f"{n}<small>{noun}{'s' * (n != 1)}</small>"


def _stat(label: str, value: str) -> str:
    return f'<div class=stat><div class=k>{_e(label)}</div><div class=v>{value}</div></div>'


_CREDENTIALS = {"end user": "signed-in user", "maker": "maker's connection"}


def _credentials(spec: Any) -> dict[str, str]:
    """Whose credentials each tool uses, where a reader recorded it in the format's extension."""
    found: dict[str, str] = {}
    for ext in spec.extensions.values():
        if not isinstance(ext, dict):
            continue
        for entry in (ext.get("actions") or {}).values():
            if isinstance(entry, dict) and entry.get("tool") and entry.get("credentials"):
                found[str(entry["tool"])] = _CREDENTIALS.get(str(entry["credentials"]), str(entry["credentials"]))
        for ref, entry in (ext.get("topic_calls") or {}).items():
            if isinstance(entry, dict) and entry.get("credentials"):
                found[str(ref)] = _CREDENTIALS.get(str(entry["credentials"]), str(entry["credentials"]))
    return found


def _table(headers: list[str], rows: list[list[str]], empty: str = "None", numeric: tuple[int, ...] = ()) -> str:
    if not rows:
        return f'<p class="empty">{_e(empty)}</p>'

    def cell(tag: str, i: int, content: str) -> str:
        return f"<{tag} class=num>{content}</{tag}>" if i in numeric else f"<{tag}>{content}</{tag}>"

    head = "".join(cell("th", i, _e(h)) for i, h in enumerate(headers))
    body = "".join("<tr>" + "".join(cell("td", i, c) for i, c in enumerate(row)) + "</tr>" for row in rows)
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


_SNAPSHOT_NOTE = ('<p class="snapshot">A static snapshot of a Marshal ledger. Search, filters and dependency lookups '
                  'work in <span class=mono>marshal web</span>.</p>')


def create_web_app(store_path: str, settings: WebSettings, *, password: Optional[PasswordProvider] = None,
                   snapshot: bool = False) -> FastAPI:
    """The web view. ``snapshot`` renders pages for static files: no forms, and the
    dependencies page lists every entity instead of asking for one."""
    app = FastAPI(title="Marshal web view", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(StoreUnavailable)
    def unavailable(request: Request, exc: StoreUnavailable) -> Response:
        # The cause can name hosts, so it stays out of the page.
        return Response("The ledger store is unavailable. Retry shortly.", status_code=503,
                        media_type="text/plain", headers={"Retry-After": "5"})

    def user(request: Request) -> Optional[str]:
        value = request.headers.get(settings.user_header)
        if not value and not settings.allow_unauthenticated:
            raise HTTPException(401, "sign in through the organization's SSO proxy")
        return value

    def store() -> Store:
        return Store(store_path, password=password)

    def page(title: str, body: str, who: Optional[str], active: str = "") -> HTMLResponse:
        return _page(title, (_SNAPSHOT_NOTE if snapshot else "") + body, who, active)

    @app.get("/favicon.svg", include_in_schema=False)
    def favicon() -> Response:
        return Response(_FAVICON, media_type="image/svg+xml", headers={"Cache-Control": "max-age=86400"})

    @app.get("/", response_class=HTMLResponse)
    def inventory(request: Request, q: str = "", tool: str = "", provider: str = "", platform: str = "") -> HTMLResponse:
        who = user(request)
        with store() as s:
            agents = s.agents(tool=tool or None, provider=provider or None, platform=platform or None)
        if q:
            needle = q.lower()
            agents = [a for a in agents if needle in a.id.lower() or needle in (a.display_name or "").lower()]
        rows = []
        for a in agents:
            envs = "".join(
                _badge(_state(st), f"{st.environment} v{st.shipped or st.observed}" + ("" if _state(st) == "current" else f" {_state(st)}"),
                       f"confirmed by {st.channel.value}, {int(st.age_days)} days ago")
                for st in a.environments
            )
            record = a.latest.record
            rows.append([
                _agent_link(a.id, a.display_name or a.id) + f'<div class="id">{_e(a.id)}</div>',
                f'<span class="chip">{_e(a.platform)}</span>', _value(record.spec.model.requested, "trunc"),
                _value(record.spec.permissions.identity), str(len(record.spec.tools)),
                f'<div class="badges">{envs}</div>' if envs else '<span class="muted">Not shipped</span>',
                _meter(record.quality.completeness),
            ])
        statuses = [st for a in agents for st in a.environments]
        stats = "<div class=stats>" + "".join((
            _stat("Agents", str(len(agents))),
            _stat("Drift", _count(sum(st.drift for st in statuses), "environment")),
            _stat("Stale", _count(sum(st.stale and not st.drift for st in statuses), "environment")),
            _stat("Not shipped", str(sum(not a.environments for a in agents))),
        )) + "</div>"
        filtered = any((q, tool, provider, platform))
        filters = (
            '<form class="toolbar" method="get">'
            f'<label class="search">{_ICONS["search"]}<input name="q" placeholder="Search agents" value="{_e(q)}" aria-label="Search agents"></label>'
            f'<input name="tool" placeholder="Uses tool (glob)" value="{_e(tool)}" aria-label="Uses tool">'
            f'<input name="provider" placeholder="Model provider" value="{_e(provider)}" aria-label="Model provider">'
            f'<input name="platform" placeholder="Platform" value="{_e(platform)}" aria-label="Platform">'
            "<button>Filter</button>" + ('<a class="btn" href="./">Clear</a>' if filtered else "") + "</form>"
        )
        body = (
            '<div class="head"><div><h1>Agents</h1>'
            "<p class=sub>Every agent definition Marshal has recorded, and where each one runs.</p></div></div>"
            + stats + ("" if snapshot else filters)
            + _card("Inventory", _table(["Agent", "Platform", "Model", "Identity", "Tools", "Environments", "Complete"], rows,
                                        "No agents match.", numeric=(4,)),
                    f"{len(agents)} agent{'s' * (len(agents) != 1)}" + (" match" + "es" * (len(agents) == 1) if filtered else ""))
        )
        return page("Agents", body, who, "agents")

    @app.get("/agent", response_class=HTMLResponse)
    def agent(request: Request, id: str, v: str = "latest") -> HTMLResponse:
        who = user(request)
        with store() as s:
            try:
                version = s.resolve(id, v)
                history = s.history(id)
                statuses = s.status(id)
                try:
                    shipped = s.resolve(id, "shipped")
                except StoreError:
                    shipped = None
            except StoreError as exc:
                raise HTTPException(404, str(exc)) from exc
        record: AgentVersion = version.record
        spec = record.spec
        name = spec.display_name.value or id
        badges = "".join(_badge(_state(st), f"{st.environment} {_state(st)}") for st in statuses)
        if record.source.format == "marshal-file":
            badges += _badge("plain", "hand-written", record.source.detected_by or "")
        parts = [
            f'<div class="crumbs"><a href="./">Agents</a> / {_e(name)}</div>',
            f'<div class="head"><div><h1>{_e(name)}{badges}</h1>'
            f'<p class="sub mono">{_e(id)} · {_e(record.platform)} · {_e(version.label)} · {_e(record.version[:19])}…</p>'
            + (f'<p class="lede">{_e(spec.description.value)}</p>' if spec.description.is_known else "")
            + "</div></div>",
            "<div class=stats>"
            + _stat("Completeness", _meter(record.quality.completeness, big=True))
            + _stat("Tools", str(len(spec.tools)))
            + _stat("Knowledge sources", str(len(spec.knowledge)))
            + _stat("Identity", f'<span style="font-size:15px">{_value(spec.permissions.identity)}</span>')
            + "</div>",
        ]

        if shipped is not None and shipped.record.version != record.version:
            changes = rank(diff_specs(shipped.record.spec, spec))
            parts.append(_card(f"Changes from shipped {shipped.label}", _table(["Risk", "Change"], [[
                _badge(c.severity.label, c.severity.label), f'<span class=mono>{_e(c.line()[6:])}</span>',
            ] for c in changes], "No changes."), f"{len(changes)} change{'s' * (len(changes) != 1)}"))

        environments = _table(["Environment", "Shipped", "Live", "Confirmed", "State"], [[
            _e(st.environment), _e(f"v{st.shipped}" if st.shipped else "–"), _e(f"v{st.observed}" if st.observed else "–"),
            f'<span class=nowrap>{_e(st.last_confirmed.strftime("%Y-%m-%d"))}</span><div class="muted">by {_e(st.channel.value)}</div>',
            _badge(_state(st), "live differs from shipped" if st.drift else _state(st)),
        ] for st in statuses], "Not shipped or observed yet.")
        definition = "<dl>" + "".join(f"<dt>{_e(label)}</dt><dd>{html_value}</dd>" for label, html_value in (
            ("Model", _value(spec.model.requested)), ("Provider", _value(spec.model.provider)),
            ("Identity", _value(spec.permissions.identity)), ("Principal", _value(spec.permissions.principal)),
            ("Source", f'<span class="mono">{_e(record.source.artifact)}</span><div class="muted">{_e(record.source.detected_by)}</div>'),
        )) + "</dl>"
        parts.append(f'<div class="grid2">{_card("Environments", environments)}{_card("Definition", definition)}</div>')

        if spec.instructions.text:
            parts.append(_card("Instructions", f"<pre>{_e(spec.instructions.text)}</pre>"))
        elif spec.instructions.hash:
            parts.append(_card("Instructions", f'<p class="muted">Stored as a hash only: <span class=mono>{_e(spec.instructions.hash[:23])}…</span></p>'))

        credentials = _credentials(spec)
        parts.append(_card("Tools", _table(["Tool", "Scope", "Reversible", "Confirmation"] + (["Credentials"] if credentials else []), [[
            f'<a href="deps?{urlencode({"q": t.ref})}" class="name">{_e(t.name or t.ref)}</a>' + (f'<div class="id">{_e(t.ref)}</div>' if t.name else ""),
            _value(t.scope), _value(t.reversible), _value(t.requires_confirmation),
        ] + ([_e(credentials[t.ref]) if t.ref in credentials else '<span class="muted">–</span>'] if credentials else [])
            for t in spec.tools], "No tools."), _legend(spec) if spec.tools else ""))
        parts.append(_card("Knowledge", _table(["Source", "Type", "Scope"], [[
            f'<a href="deps?{urlencode({"q": k.ref})}" class="name">{_e(k.name or k.ref)}</a>' + (f'<div class="id">{_e(k.ref)}</div>' if k.name else ""),
            _e(k.type), _value(k.scope),
        ] for k in spec.knowledge], "No knowledge sources.")))
        permissions = _table(["Target", "Privilege", "Scope"], [[
            f'<span class=mono>{_e(g.target)}</span>', _e(g.privilege), _e(g.scope.value),
        ] for g in spec.permissions.grants], "No grants recorded.")
        if spec.permissions.access:
            permissions += _table(["Who may use the agent", "Level"], [[_e(a.principal), _e(a.level)] for a in spec.permissions.access])
        parts.append(_card("Permissions", permissions))

        portability = []
        from .portability import assess, load_matrix

        for target in settings.portability_targets:
            try:
                report = assess(record, target)
            except Exception:  # a broken matrix file shouldn't take the page down
                continue
            if report.current:
                portability.append([_e(load_matrix(target).display_name), '<span class="badge plain">Current platform</span>', ""])
                continue
            portability.append([_e(load_matrix(target).display_name), _meter(report.score / 100, unit=""),
                                '<div class="limits">' + "<br>".join(_e(i.message) for i in report.limitations[:4])
                                + (f"<br>and {len(report.limitations) - 4} more" if len(report.limitations) > 4 else "") + "</div>"])
        parts.append(_card("Portability", _table(["Platform", "Score", "Main limitations"], portability), "Score out of 100"))

        parts.append(_card("History", _table(["When", "Event", "Environment", "Version", "Channel", "By"], [[
            _e(e.at.strftime("%Y-%m-%d %H:%M")), f'<span class="chip">{_e(e.event.value)}</span>', _e(e.environment),
            _agent_link_version(id, seq), _e(e.channel.value), _e(actor.repository if actor else ""),
        ] for e, seq, actor in reversed(history)])))
        return page(name, "".join(parts), who, "agents")

    @app.get("/deps", response_class=HTMLResponse)
    def deps(request: Request, q: str = "", shipped: bool = False) -> HTMLResponse:
        who = user(request)
        rows = []
        if q:
            with store() as s:
                for d in s.dependents(q, shipped_only=shipped):
                    where = ", ".join(d.shipped) or ("latest" if d.latest else "–")
                    rows.append([_agent_link(d.agent), _e(d.label), _e(where), f'<span class="chip">{_e(d.relation)}</span>',
                                 f"<span class=mono>{_e(d.ref)}</span>"])
        form = ('<form class="toolbar" method="get">'
                f'<label class="search" style="max-width:520px">{_ICONS["search"]}<input name="q" '
                f'placeholder="Tool, knowledge source or grant target (glob)" value="{_e(q)}" aria-label="Entity"></label>'
                f'<label class="check"><input type="checkbox" name="shipped" value="true"{" checked" if shipped else ""}> Shipped only</label>'
                "<button>Find</button></form>")
        if snapshot:
            form = ""
            if not q:
                with store() as s:
                    names = {e.ref: e.display_name for e, _ in s.entities() if e.display_name}
                    referenced: dict[str, tuple[str, set[str]]] = {}
                    for d in s.dependents("*"):
                        referenced.setdefault(d.ref, (d.relation, set()))[1].add(d.agent)
                listed = sorted(referenced.items(), key=lambda item: (-len(item[1][1]), item[0]))
                form = _card("Referenced by agents", _table(["Entity", "Relation", "Agents"], [[
                    f'<a href="deps?{urlencode({"q": ref})}" class="name">{_e(names.get(ref, ref))}</a>'
                    + (f'<div class="id">{_e(ref)}</div>' if ref in names else ""),
                    f'<span class="chip">{_e(relation)}</span>', str(len(agents)),
                ] for ref, (relation, agents) in listed], "No agent references anything yet.", numeric=(2,)),
                    f"{len(listed)} entit{'ies' if len(listed) != 1 else 'y'}")
        intro = (f"Every agent that references <span class=mono>{_e(q)}</span>." if snapshot and q else
                 "Every agent that uses a tool, reads a knowledge source or holds a grant." if snapshot else
                 "Find every agent that uses a tool, reads a knowledge source or holds a grant. "
                 "Use <span class=mono>*</span> as a wildcard.")
        body = ('<div class="head"><div><h1>Who depends on this?</h1>'
                f"<p class=sub>{intro}</p></div></div>" + form
                + (_card("Dependents", _table(["Agent", "Version", "Where", "Relation", "Ref"], rows, "No agents reference it."),
                         f"{len(rows)} result{'s' * (len(rows) != 1)}") if q else ""))
        return page("Dependencies", body, who, "deps")

    @app.get("/v0/agents")
    def api_agents(request: Request) -> list[dict[str, Any]]:
        user(request)
        with store() as s:
            return [{"agent": a.id, "platform": a.platform, "display_name": a.display_name, "latest": a.latest.label,
                     "version": a.latest.record.version,
                     "environments": [{"environment": st.environment, "shipped": st.shipped, "observed": st.observed,
                                       "channel": st.channel.value, "stale": st.stale, "drift": st.drift}
                                      for st in a.environments]} for a in s.agents()]

    @app.get("/v0/agent")
    def api_agent(request: Request, id: str, v: str = "latest") -> dict[str, Any]:
        user(request)
        with store() as s:
            try:
                return s.resolve(id, v).record.model_dump(mode="json", by_alias=True)
            except StoreError as exc:
                raise HTTPException(404, str(exc)) from exc

    @app.get("/v0/deps")
    def api_deps(request: Request, q: str, shipped: bool = False) -> list[dict[str, Any]]:
        user(request)
        with store() as s:
            return [d.__dict__ | {"shipped": list(d.shipped)} for d in s.dependents(q, shipped_only=shipped)]

    return app


def _agent_link_version(agent: str, seq: int) -> str:
    return f'<a href="agent?{urlencode({"id": agent, "v": f"v{seq}"})}">v{seq}</a>'


__all__ = ["WebSettings", "create_web_app"]


def _snapshot_file(href: str) -> Optional[str]:
    """The file a page link is saved as, or None for links that leave the web view."""
    url = urlsplit(html.unescape(href))
    if url.scheme or url.netloc or url.fragment:
        return None
    if url.path in ("", "./"):
        return "index.html"
    if url.path == "favicon.svg":
        return url.path
    if url.path not in ("agent", "deps"):
        return None
    if not url.query:
        return f"{url.path}.html"
    words = re.sub(r"[^a-z0-9]+", "-", " ".join(v for _, v in parse_qsl(url.query)).lower()).strip("-")[:60]
    return f"{url.path}-{words}-{hashlib.sha256(url.query.encode()).hexdigest()[:8]}.html"


async def _asgi_get(app: FastAPI, href: str) -> tuple[int, bytes]:
    url = urlsplit(html.unescape(href))
    path = "/" + url.path.removeprefix("./")
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "http",
             "path": path, "raw_path": path.encode(), "query_string": url.query.encode(), "root_path": "",
             "headers": [], "client": ("127.0.0.1", 0), "server": ("snapshot", 80)}
    status, body = 500, b""

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        nonlocal status, body
        if message["type"] == "http.response.start":
            status = message["status"]
        elif message["type"] == "http.response.body":
            body += message.get("body", b"")

    await app(scope, receive, send)
    return status, body


def export_snapshot(store_path: str, out_dir: str | Path, *, settings: Optional[WebSettings] = None,
                    password: Optional[PasswordProvider] = None) -> int:
    """Write every page reachable from the inventory as static HTML files; return the number written.

    Links are rewritten to the saved files, so the result can be served by any static host."""
    settings = (settings or WebSettings()).model_copy(update={"enabled": True, "allow_unauthenticated": True})
    app = create_web_app(store_path, settings, password=password, snapshot=True)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    def link(match: re.Match[str]) -> str:
        name = _snapshot_file(match[1])
        return match[0] if name is None else f'href="{"./" if name == "index.html" else name}"'

    async def crawl() -> int:
        queue, seen, written = ["./", "deps"], {"index.html", "deps.html"}, 0
        while queue:
            href = queue.pop(0)
            status, body = await _asgi_get(app, href)
            if status != 200:
                raise RuntimeError(f"{href} answered {status}")
            name = _snapshot_file(href)
            if name.endswith(".html"):
                text = body.decode()
                for target in re.findall(r'href="([^"]*)"', text):
                    file = _snapshot_file(target)
                    if file is not None and file not in seen:
                        seen.add(file)
                        queue.append(target)
                (out / name).write_text(re.sub(r'href="([^"]*)"', link, text), encoding="utf-8")
                written += 1
            else:
                (out / name).write_bytes(body)
        return written

    return asyncio.run(crawl())
