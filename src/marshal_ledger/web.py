"""Read-only web view of a ledger store.

Server-rendered HTML with inline styles and no external resources, so it runs
inside the organization's network without calling out. Every value from the
store is escaped. Pages: the inventory, an agent (environments, definition,
changes against what is shipped, portability, history), and dependents of an
entity. The same data is available as JSON under ``/v0/``.

Authentication is delegated to the organization's SSO proxy: requests must
carry the configured user header, which the proxy sets after sign-in.
"""

from __future__ import annotations

import html
from typing import Any, Optional
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict

from . import __version__
from .diff import diff_specs
from .model import AgentVersion
from .risk import rank
from .store import Store, StoreError


class WebSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    user_header: str = "X-Forwarded-Email"
    """Header the SSO proxy sets with the signed-in user. Requests without it are refused."""
    allow_unauthenticated: bool = False
    """Serve without the user header. Only for local use (``marshal web``)."""
    portability_targets: list[str] = ["bedrock", "copilot-studio", "agentforce", "google-adk", "databricks"]


_STYLE = """
:root { --bg:#fff; --fg:#1b1f24; --muted:#5b6470; --line:#d9dee4; --soft:#f4f6f8; --accent:#2457c5;
        --high:#b42318; --med:#a15c07; --low:#3d5a80; --ok:#1a7f37; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#111418; --fg:#e6e9ed; --muted:#9aa4b0; --line:#2b323b; --soft:#1a1f25; --accent:#7aa2ff;
          --high:#ff8a80; --med:#ffc46b; --low:#9fb8e0; --ok:#6fdc8c; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }
header { border-bottom:1px solid var(--line); padding:12px 24px; display:flex; gap:24px; align-items:center; flex-wrap:wrap; }
header a.brand { font-weight:650; color:var(--fg); text-decoration:none; }
header nav a { margin-right:16px; }
main { padding:24px; max-width:1200px; margin:0 auto; }
a { color:var(--accent); }
h1 { font-size:22px; margin:0 0 4px; } h2 { font-size:17px; margin:28px 0 8px; }
.muted { color:var(--muted); } .mono { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:13px; }
.scroll { overflow-x:auto; border:1px solid var(--line); border-radius:8px; }
table { border-collapse:collapse; width:100%; }
th, td { text-align:left; padding:8px 12px; border-bottom:1px solid var(--line); vertical-align:top; }
th { background:var(--soft); font-weight:600; font-size:13px; white-space:nowrap; }
tr:last-child td { border-bottom:0; }
.tag { display:inline-block; padding:1px 8px; border-radius:999px; font-size:12px; border:1px solid var(--line); background:var(--soft); white-space:nowrap; }
.HIGH, .drift { color:var(--high); border-color:var(--high); } .MED, .stale { color:var(--med); border-color:var(--med); }
.LOW { color:var(--low); } .current { color:var(--ok); border-color:var(--ok); }
form.filters { display:flex; gap:8px; flex-wrap:wrap; margin:12px 0 16px; }
input, select, button { font:inherit; padding:6px 10px; border:1px solid var(--line); border-radius:6px; background:var(--bg); color:var(--fg); }
button { background:var(--accent); color:#fff; border-color:var(--accent); cursor:pointer; }
pre { white-space:pre-wrap; background:var(--soft); border:1px solid var(--line); border-radius:8px; padding:12px; margin:0; }
dl { display:grid; grid-template-columns:max-content 1fr; gap:4px 16px; margin:0; }
dt { color:var(--muted); }
"""


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _page(title: str, body: str, user: Optional[str]) -> HTMLResponse:
    who = f'<span class="muted">{_e(user)}</span>' if user else ""
    return HTMLResponse(
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        f"<title>{_e(title)} · Marshal</title><style>{_STYLE}</style></head><body>"
        '<header><a class=brand href="./">Marshal</a>'
        '<nav><a href="./">Agents</a><a href="deps">Dependencies</a></nav>'
        f'<span style="flex:1"></span>{who}</header><main>{body}</main></body></html>'
    )


def _agent_link(agent: str, label: Optional[str] = None) -> str:
    return f'<a href="agent?{urlencode({"id": agent})}">{_e(label or agent)}</a>'


def _value(v: Any) -> str:
    if not v.is_known:
        return f'<span class="muted" title="{_e(v.basis)}">unknown</span>'
    value = getattr(v.value, "value", v.value)
    return f'<span title="{_e(v.provenance.value)}: {_e(v.basis)}">{_e(value)}</span>'


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


def _table(headers: list[str], rows: list[list[str]], empty: str = "None") -> str:
    if not rows:
        return f'<p class="muted">{_e(empty)}</p>'
    head = "".join(f"<th>{_e(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows)
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def create_web_app(store_path: str, settings: WebSettings) -> FastAPI:
    app = FastAPI(title="Marshal web view", version=__version__, docs_url=None, redoc_url=None)

    def user(request: Request) -> Optional[str]:
        value = request.headers.get(settings.user_header)
        if not value and not settings.allow_unauthenticated:
            raise HTTPException(401, "sign in through the organization's SSO proxy")
        return value

    def store() -> Store:
        return Store(store_path)

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
            envs = " ".join(
                f'<span class="tag {"drift" if st.drift else "stale" if st.stale else "current"}" '
                f'title="confirmed by {_e(st.channel.value)}, {int(st.age_days)} days ago">'
                f'{_e(st.environment)} v{_e(st.shipped or st.observed)}{" drift" if st.drift else " stale" if st.stale else ""}</span>'
                for st in a.environments
            ) or '<span class="muted">not shipped</span>'
            record = a.latest.record
            rows.append([
                _agent_link(a.id, a.display_name or a.id) + f'<div class="muted mono">{_e(a.id)}</div>',
                _e(a.platform), _value(record.spec.model.requested), _value(record.spec.permissions.identity),
                str(len(record.spec.tools)), envs, f"{record.quality.completeness:.0%}",
            ])
        filters = (
            '<form class="filters" method="get">'
            f'<input name="q" placeholder="Search agents" value="{_e(q)}">'
            f'<input name="tool" placeholder="Uses tool (glob)" value="{_e(tool)}">'
            f'<input name="provider" placeholder="Model provider" value="{_e(provider)}">'
            f'<input name="platform" placeholder="Platform" value="{_e(platform)}">'
            "<button>Filter</button></form>"
        )
        body = (f"<h1>Agents</h1><p class=muted>{len(agents)} agent{'s' * (len(agents) != 1)}</p>{filters}"
                + _table(["Agent", "Platform", "Model", "Identity", "Tools", "Environments", "Complete"], rows,
                         "No agents match."))
        return _page("Agents", body, who)

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
        parts = [f"<h1>{_e(spec.display_name.value or id)}</h1>",
                 f'<p class="muted mono">{_e(id)} · {_e(record.platform)} · {_e(version.label)} · {_e(record.version[:19])}…</p>']
        if spec.description.is_known:
            parts.append(f"<p>{_e(spec.description.value)}</p>")

        parts.append("<h2>Environments</h2>")
        parts.append(_table(["Environment", "Shipped", "Live (observed)", "Confirmed", "State"], [[
            _e(st.environment), _e(f"v{st.shipped}" if st.shipped else "–"), _e(f"v{st.observed}" if st.observed else "–"),
            f"{_e(st.last_confirmed.strftime('%Y-%m-%d'))} by {_e(st.channel.value)}",
            f'<span class="tag {"drift" if st.drift else "stale" if st.stale else "current"}">'
            f'{"drift: live differs from shipped" if st.drift else "stale" if st.stale else "current"}</span>',
        ] for st in statuses], "Not shipped or observed yet."))

        parts.append("<h2>Definition</h2><dl>")
        parts += [f"<dt>{_e(label)}</dt><dd>{html_value}</dd>" for label, html_value in (
            ("Model", _value(spec.model.requested)), ("Provider", _value(spec.model.provider)),
            ("Identity", _value(spec.permissions.identity)), ("Principal", _value(spec.permissions.principal)),
            ("Completeness", f"{record.quality.completeness:.0%}"),
            ("Source", f'<span class="mono">{_e(record.source.artifact)}</span> <span class="muted">{_e(record.source.detected_by)}</span>'),
        )]
        parts.append("</dl>")
        if spec.instructions.text:
            parts.append(f"<h2>Instructions</h2><pre>{_e(spec.instructions.text)}</pre>")
        elif spec.instructions.hash:
            parts.append(f'<h2>Instructions</h2><p class="muted">Stored as a hash only: <span class=mono>{_e(spec.instructions.hash[:23])}…</span></p>')

        credentials = _credentials(spec)
        parts.append("<h2>Tools</h2>" + _table(["Tool", "Scope", "Reversible", "Confirmation"] + (["Credentials"] if credentials else []), [[
            f'<a href="deps?{urlencode({"q": t.ref})}" class=mono>{_e(t.name or t.ref)}</a>' + (f'<div class="muted mono">{_e(t.ref)}</div>' if t.name else ""),
            _value(t.scope), _value(t.reversible), _value(t.requires_confirmation),
        ] + ([_e(credentials[t.ref]) if t.ref in credentials else '<span class="muted">–</span>'] if credentials else [])
            for t in spec.tools], "No tools."))
        parts.append("<h2>Knowledge</h2>" + _table(["Source", "Type", "Scope"], [[
            f'<a href="deps?{urlencode({"q": k.ref})}" class=mono>{_e(k.name or k.ref)}</a>' + (f'<div class="muted mono">{_e(k.ref)}</div>' if k.name else ""),
            _e(k.type), _value(k.scope),
        ] for k in spec.knowledge], "No knowledge sources."))
        parts.append("<h2>Permissions</h2>" + _table(["Target", "Privilege", "Scope"], [[
            f'<span class=mono>{_e(g.target)}</span>', _e(g.privilege), _e(g.scope.value),
        ] for g in spec.permissions.grants], "No grants recorded."))
        if spec.permissions.access:
            parts.append(_table(["Who may use the agent", "Level"], [[_e(a.principal), _e(a.level)] for a in spec.permissions.access]))

        if shipped is not None and shipped.record.version != record.version:
            changes = rank(diff_specs(shipped.record.spec, spec))
            parts.append(f"<h2>Changes from shipped {_e(shipped.label)}</h2>" + _table(["Risk", "Change"], [[
                f'<span class="tag {_e(c.severity.label)}">{_e(c.severity.label)}</span>',
                f'<span class=mono>{_e(c.line()[6:])}</span>'] for c in changes], "No changes."))

        portability = []
        from .portability import assess, load_matrix

        for target in settings.portability_targets:
            try:
                report = assess(record, target)
            except Exception:  # a broken matrix file shouldn't take the page down
                continue
            if report.current:
                portability.append([_e(load_matrix(target).display_name), '<span class="muted">–</span>',
                                    '<span class="muted">Current platform</span>'])
                continue
            portability.append([_e(load_matrix(target).display_name), f"<strong>{report.score}</strong>",
                                "<br>".join(_e(i.message) for i in report.limitations[:4])
                                + (f'<br><span class=muted>and {len(report.limitations) - 4} more</span>' if len(report.limitations) > 4 else "")])
        parts.append("<h2>Portability</h2>" + _table(["Platform", "Score", "Main limitations"], portability))

        parts.append("<h2>History</h2>" + _table(["When", "Event", "Environment", "Version", "Channel", "By"], [[
            _e(e.at.strftime("%Y-%m-%d %H:%M")), _e(e.event.value), _e(e.environment),
            _agent_link_version(id, seq), _e(e.channel.value), _e(actor.repository if actor else ""),
        ] for e, seq, actor in reversed(history)]))
        return _page(spec.display_name.value or id, "".join(parts), who)

    @app.get("/deps", response_class=HTMLResponse)
    def deps(request: Request, q: str = "", shipped: bool = False) -> HTMLResponse:
        who = user(request)
        rows = []
        if q:
            with store() as s:
                for d in s.dependents(q, shipped_only=shipped):
                    where = ", ".join(d.shipped) or ("latest" if d.latest else "–")
                    rows.append([_agent_link(d.agent), _e(d.label), _e(where), _e(d.relation), f"<span class=mono>{_e(d.ref)}</span>"])
        form = ('<form class="filters" method="get">'
                f'<input name="q" size="50" placeholder="Tool, knowledge source or grant target (glob)" value="{_e(q)}">'
                f'<label><input type="checkbox" name="shipped" value="true"{" checked" if shipped else ""}> shipped only</label>'
                "<button>Find</button></form>")
        body = "<h1>Who depends on this?</h1>" + form + (_table(["Agent", "Version", "Where", "Relation", "Ref"], rows,
                                                                  "No agents reference it.") if q else "")
        return _page("Dependencies", body, who)

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
