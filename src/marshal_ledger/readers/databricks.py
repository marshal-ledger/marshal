"""Reader for Databricks Asset Bundles.

A bundle is a ``databricks.yml`` file plus the files it includes. The reader
records two resource types as agents:

- **Genie spaces** (``resources.genie_spaces``): natural-language data agents
  whose instructions, data sources and example SQL are declared in the bundle,
  inline or in a ``.geniespace.json`` file.
- **Databricks Apps** (``resources.apps``) that declare access to a model or an
  agent: a serving endpoint, a Genie space or a Unity Catalog model service. This
  is how agents built with the Databricks agent templates are deployed. Their
  model and instructions live in application code and are recorded as unknown.

Model serving endpoints are reported as skipped: a bundle alone cannot tell an
endpoint that serves an agent from one that serves any other model.

Mapping rules are documented in ``docs/formats/databricks.md``.
"""

from __future__ import annotations

import glob
import json
import re
from pathlib import Path
from typing import Any, Optional

import yaml

from ..hashing import canonical_json, sha256_text
from ..model import (
    AccessRule,
    AgentSpec,
    Entity,
    EntityKind,
    Grant,
    IdentityKind,
    Instructions,
    KnowledgeUse,
    Permissions,
    Provenance,
    Scope,
    Source,
    ToolUse,
    Value,
)
from ..redact import redact
from .base import ReadOptions, ReadResult, Reader, Skipped, build_record

PLATFORM = "databricks"
BUNDLE_FILES = ("databricks.yml", "databricks.yaml", "bundle.yml", "bundle.yaml")

_RESOURCE_REF = re.compile(r"^\$\{resources\.(?P<type>\w+)\.(?P<key>\w+)\.\w+\}$")

# Privilege → scope. Grants whose privilege is not listed get no scope and a warning.
_SCOPES: dict[str, Scope] = {
    "CAN_VIEW": Scope.READ,
    "CAN_READ": Scope.READ,
    "READ": Scope.READ,
    "READ_VOLUME": Scope.READ,
    "SELECT": Scope.READ,
    "CAN_QUERY": Scope.EXECUTE,
    "CAN_RUN": Scope.EXECUTE,
    "CAN_USE": Scope.EXECUTE,
    "CAN_MANAGE_RUN": Scope.EXECUTE,
    "EXECUTE": Scope.EXECUTE,
    "USE_CONNECTION": Scope.EXECUTE,
    "CAN_EDIT": Scope.WRITE,
    "WRITE": Scope.WRITE,
    "WRITE_VOLUME": Scope.WRITE,
    "MODIFY": Scope.WRITE,
    "CAN_CONNECT_AND_CREATE": Scope.WRITE,
    "CAN_MANAGE": Scope.ADMIN,
    "MANAGE": Scope.ADMIN,
    "IS_OWNER": Scope.ADMIN,
}


class BundleError(ValueError):
    """The bundle could not be parsed."""


class DatabricksBundleReader(Reader):
    format = "databricks-asset-bundle"
    platform = PLATFORM

    @classmethod
    def detect(cls, path: Path) -> bool:
        return _bundle_file(path) is not None

    def read(self, path: Path, options: ReadOptions | None = None) -> ReadResult:
        options = options or ReadOptions()
        bundle_file = _bundle_file(path)
        if bundle_file is None:
            raise BundleError(f"no bundle file ({', '.join(BUNDLE_FILES)}) at {path}")
        bundle = _Bundle.load(bundle_file, options.target)
        return _BundleRead(bundle, options).run()


def _bundle_file(path: Path) -> Optional[Path]:
    if path.is_file():
        return path if path.name in BUNDLE_FILES else None
    for name in BUNDLE_FILES:
        if (path / name).is_file():
            return path / name
    return None


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise BundleError(f"{path}: invalid YAML: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise BundleError(f"{path}: expected a mapping at the top level")
    return data


def _deep_merge(base: Any, override: Any) -> Any:
    if isinstance(base, dict) and isinstance(override, dict):
        merged = dict(base)
        for key, value in override.items():
            merged[key] = _deep_merge(base.get(key), value) if key in base else value
        return merged
    return override


class _Bundle:
    """A bundle's configuration with its includes merged, and the file each resource came from."""

    def __init__(self, root: Path, config: dict[str, Any], origins: dict[tuple[str, str], Path]):
        self.root = root
        self.config = config
        self.origins = origins
        self.base_resources: dict[str, dict[str, Any]] = {}
        self.resources: dict[str, dict[str, Any]] = {}
        self.target: Optional[str] = None
        self.target_config: dict[str, Any] = {}
        self.warnings: list[str] = []

    @property
    def name(self) -> str:
        return str((self.config.get("bundle") or {}).get("name") or self.root.name)

    def rel(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.root.resolve()).as_posix()
        except ValueError:
            return path.as_posix()

    @classmethod
    def load(cls, bundle_file: Path, target: Optional[str]) -> "_Bundle":
        root = bundle_file.parent
        config = _load_yaml(bundle_file)
        origins: dict[tuple[str, str], Path] = {}
        resources: dict[str, dict[str, Any]] = {}
        targets: dict[str, Any] = dict(config.get("targets") or {})
        warnings: list[str] = []

        def add_resources(block: Any, origin: Path) -> None:
            for rtype, items in (block or {}).items():
                for key, body in (items or {}).items():
                    if (rtype, key) in origins:
                        warnings.append(
                            f"resources.{rtype}.{key} is defined in both "
                            f"{origins[(rtype, key)].name} and {origin.name}; using the first"
                        )
                        continue
                    origins[(rtype, key)] = origin
                    resources.setdefault(rtype, {})[key] = body or {}

        add_resources(config.get("resources"), bundle_file)
        for pattern in config.get("include") or []:
            for match in sorted(glob.glob(str(root / pattern), recursive=True)):
                included = _load_yaml(Path(match))
                add_resources(included.get("resources"), Path(match))
                for tname, tbody in (included.get("targets") or {}).items():
                    targets[tname] = _deep_merge(targets.get(tname) or {}, tbody or {})

        bundle = cls(root, config, origins)
        bundle.warnings = warnings
        bundle.base_resources = resources
        bundle.resources = resources

        chosen = target
        if chosen is not None and chosen not in targets:
            raise BundleError(f"target {chosen!r} is not defined; targets: {sorted(targets)}")
        if chosen is not None:
            bundle.target = chosen
            bundle.target_config = targets[chosen] or {}
            overrides = bundle.target_config.get("resources") or {}
            bundle.resources = _deep_merge(resources, overrides)
            for rtype, items in overrides.items():
                for key, body in (items or {}).items():
                    if (rtype, key) not in origins:
                        origins[(rtype, key)] = bundle_file
                    if isinstance(body, dict) and any(isinstance(v, list) for v in body.values()):
                        warnings.append(
                            f"target {chosen!r} overrides a list in resources.{rtype}.{key}; "
                            "lists are replaced, not merged"
                        )
        return bundle


def _is_reference(value: Any) -> bool:
    return isinstance(value, str) and "${" in value


def _text(lines: Any) -> str:
    """Genie stores text as a list of line fragments."""
    if isinstance(lines, list):
        return "".join(str(part) for part in lines)
    return str(lines or "")


class _BundleRead:
    def __init__(self, bundle: _Bundle, options: ReadOptions):
        self.bundle = bundle
        self.options = options
        self.result = ReadResult(warnings=list(bundle.warnings))
        self._entities: dict[str, Entity] = {}

    # -- helpers ---------------------------------------------------------

    def agent_id(self, key: str) -> str:
        return f"{self.bundle.name}/{key}"

    def entity(self, kind: EntityKind, rtype: str, identifier: str, display: str | None = None) -> str:
        ref = f"{kind.value}/{PLATFORM}.{rtype}.{identifier}"
        self._entities.setdefault(
            ref,
            Entity(ref=ref, kind=kind, platform=PLATFORM, identifier=identifier, display_name=display),
        )
        return ref

    def agent_entity(self, key: str, display: str | None = None) -> str:
        ident = self.agent_id(key)
        ref = f"{EntityKind.AGENT.value}/{ident}"
        self._entities.setdefault(
            ref, Entity(ref=ref, kind=EntityKind.AGENT, platform=PLATFORM, identifier=ident, display_name=display)
        )
        return ref

    def clean(self, text: str, where: str) -> str:
        cleaned, count = redact(text)
        if count:
            self.result.warnings.append(f"{where}: redacted {count} credential-like value(s)")
        return cleaned

    def source(self, rtype: str, key: str, detected_by: str) -> Source:
        origin = self.bundle.origins.get((rtype, key))
        return Source(
            format=DatabricksBundleReader.format,
            artifact=self.bundle.rel(origin) if origin else "databricks.yml",
            locator=f"resources.{rtype}.{key}",
            detected_by=detected_by,
            commit=self.options.commit,
            pipeline_run=self.options.pipeline_run,
        )

    def base_bindings(self) -> dict[str, Any]:
        bindings: dict[str, Any] = {}
        if self.bundle.target:
            bindings["target"] = self.bundle.target
            if mode := self.bundle.target_config.get("mode"):
                bindings["mode"] = mode
        workspace = self.bundle.target_config.get("workspace") or self.bundle.config.get("workspace") or {}
        if host := workspace.get("host"):
            bindings["workspace_host"] = host
        return bindings

    def access_rules(self, body: dict[str, Any]) -> list[AccessRule]:
        rules = []
        for item in body.get("permissions") or []:
            for field, ptype in (
                ("user_name", "user"),
                ("group_name", "group"),
                ("service_principal_name", "service_principal"),
            ):
                if item.get(field):
                    rules.append(AccessRule(principal=str(item[field]), principal_type=ptype, level=str(item.get("level", ""))))
                    break
        return rules

    def scope_for(self, privilege: str, where: str) -> Optional[Scope]:
        scope = _SCOPES.get(privilege.upper())
        if scope is None:
            self.result.warnings.append(f"{where}: unrecognized privilege {privilege!r}")
        return scope

    # -- run -------------------------------------------------------------

    def run(self) -> ReadResult:
        resources = self.bundle.resources
        for key in sorted(resources.get("genie_spaces") or {}):
            self.read_genie_space(key, resources["genie_spaces"][key])
        for key in sorted(resources.get("apps") or {}):
            self.read_app(key, resources["apps"][key])
        for key in sorted(resources.get("model_serving_endpoints") or {}):
            origin = self.bundle.origins.get(("model_serving_endpoints", key))
            self.result.skipped.append(
                Skipped(
                    artifact=self.bundle.rel(origin) if origin else "databricks.yml",
                    locator=f"resources.model_serving_endpoints.{key}",
                    reason="a serving endpoint may serve an agent or any other model; "
                    "the bundle does not say which",
                )
            )
        self.result.entities = sorted(self._entities.values(), key=lambda e: e.ref)
        return self.result

    # -- Genie spaces ----------------------------------------------------

    def load_space(self, key: str, body: dict[str, Any]) -> tuple[Optional[dict[str, Any]], Optional[str]]:
        where = f"resources.genie_spaces.{key}"
        if body.get("file_path"):
            origin = self.bundle.origins.get(("genie_spaces", key), self.bundle.root / "databricks.yml")
            path = (origin.parent / str(body["file_path"])).resolve()
            if not path.is_file():
                self.result.warnings.append(f"{where}: file_path {body['file_path']} not found")
                return None, None
            try:
                return json.loads(path.read_text(encoding="utf-8")), self.bundle.rel(path)
            except json.JSONDecodeError as exc:
                self.result.warnings.append(f"{where}: {body['file_path']} is not valid JSON: {exc}")
                return None, None
        inline = body.get("serialized_space")
        if isinstance(inline, str):
            try:
                return json.loads(inline), None
            except json.JSONDecodeError as exc:
                self.result.warnings.append(f"{where}: serialized_space is not valid JSON: {exc}")
                return None, None
        if isinstance(inline, dict):
            return inline, None
        return None, None

    def read_genie_space(self, key: str, body: dict[str, Any]) -> None:
        where = f"resources.genie_spaces.{key}"
        base = self.bundle.base_resources.get("genie_spaces", {}).get(key, body)
        space, space_file = self.load_space(key, body)
        basis_file = space_file or where

        spec = AgentSpec()
        title = base.get("title")
        if title and not _is_reference(title):
            spec.display_name = Value[str].declared(str(title), f"{where}.title")
        if body.get("description"):
            spec.description = Value[str].declared(self.clean(str(body["description"]), where), f"{where}.description")
        spec.model.requested = Value[str].unknown("Genie spaces use a platform-managed model")

        extension: dict[str, Any] = {"resource_type": "genie_space"}
        if space is not None:
            instructions = space.get("instructions") or {}
            texts = [_text(item.get("content")) for item in instructions.get("text_instructions") or []]
            text = "\n\n".join(t for t in texts if t)
            if text:
                text = self.clean(text, f"{basis_file}: instructions")
                spec.instructions = Instructions(
                    hash=sha256_text(text),
                    text=None if self.options.hash_only else text,
                    provenance=Provenance.DECLARED,
                    basis=f"{basis_file}#instructions.text_instructions",
                )
            for group, items in sorted((space.get("data_sources") or {}).items()):
                for item in items or []:
                    ident = item.get("identifier")
                    if not ident:
                        continue
                    rtype = "uc.table" if group == "tables" else f"uc.{group.rstrip('s')}"
                    ref = self.entity(EntityKind.KNOWLEDGE, rtype, str(ident))
                    spec.knowledge.append(
                        KnowledgeUse(
                            ref=ref,
                            name=str(ident),
                            type="table" if group == "tables" else group.rstrip("s"),
                            scope=Value[Scope].derived(Scope.READ, "Genie runs read-only SQL against its data sources"),
                        )
                    )
            extension["sample_questions"] = [
                _text(q.get("question")) for q in (space.get("config") or {}).get("sample_questions") or []
            ]
            extension["example_sql_count"] = len(instructions.get("example_question_sqls") or [])
            extension["space_digest"] = sha256_text(canonical_json(space))
            if "version" in space:
                extension["serialized_space_version"] = space["version"]
        else:
            spec.instructions = Instructions(basis=f"{where}: no serialized space in the bundle")

        spec.permissions = Permissions(
            identity=Value[IdentityKind].derived(
                IdentityKind.ON_BEHALF_OF_USER, "Genie queries run with the asking user's permissions"
            ),
            principal=Value[str].unknown("the asking user"),
            access=self.access_rules(body),
        )
        spec.extensions[PLATFORM] = extension

        bindings = self.base_bindings()
        for field in ("warehouse_id", "parent_path"):
            if body.get(field):
                bindings[field] = body[field]

        self.result.records.append(
            build_record(
                agent=self.agent_id(key),
                platform=PLATFORM,
                source=self.source("genie_spaces", key, "resource type genie_spaces"),
                spec=spec,
                bindings=bindings,
            )
        )

    # -- Apps ------------------------------------------------------------

    def read_app(self, key: str, body: dict[str, Any]) -> None:
        where = f"resources.apps.{key}"
        app_resources = body.get("resources") or []
        signals = [
            kind
            for item in app_resources
            for kind in ("serving_endpoint", "genie_space")
            if item.get(kind)
        ] + [
            "uc_securable MODEL_SERVICE"
            for item in app_resources
            if (item.get("uc_securable") or {}).get("securable_type") == "MODEL_SERVICE"
        ]
        origin = self.bundle.origins.get(("apps", key))
        if not signals:
            self.result.skipped.append(
                Skipped(
                    artifact=self.bundle.rel(origin) if origin else "databricks.yml",
                    locator=where,
                    reason="app declares no access to a model or an agent",
                )
            )
            return

        base = self.bundle.base_resources.get("apps", {}).get(key, body)
        spec = AgentSpec()
        name = base.get("name")
        if name and not _is_reference(name):
            spec.display_name = Value[str].declared(str(name), f"{where}.name")
        if body.get("description"):
            spec.description = Value[str].declared(self.clean(str(body["description"]), where), f"{where}.description")
        code = body.get("source_code_path")
        spec.model.requested = Value[str].unknown(
            f"set in application code{f' at {code}' if code else ''}"
        )
        spec.instructions = Instructions(basis=f"set in application code{f' at {code}' if code else ''}")

        scopes = body.get("user_api_scopes") or []
        if scopes:
            identity = Value[IdentityKind].derived(
                IdentityKind.MIXED,
                f"{where}.user_api_scopes: the app acts as its service principal and on behalf of users",
            )
        else:
            identity = Value[IdentityKind].derived(
                IdentityKind.SERVICE_PRINCIPAL, "Databricks Apps run as the app's service principal"
            )
        spec.permissions = Permissions(
            identity=identity,
            principal=Value[str].unknown("the app service principal is created at deploy time"),
            access=self.access_rules(body),
        )

        for index, item in enumerate(app_resources):
            self.app_resource(spec, item, f"{where}.resources[{index}]")

        config = body.get("config") or {}
        env = []
        for var in config.get("env") or []:
            entry = {"name": var.get("name")}
            source_ref = var.get("value_from") or var.get("valueFrom")
            if source_ref:
                entry["value_from"] = source_ref
            env.append(entry)
        linked = self.link_code(spec, key, code, where) if self.options.link_code and code else None

        extension: dict[str, Any] = {"resource_type": "app"}
        for field in ("source_code_path", "compute_size"):
            if body.get(field):
                extension[field] = body[field]
        if config.get("command"):
            extension["command"] = config["command"]
        if env:
            extension["env"] = env
        if scopes:
            extension["user_api_scopes"] = scopes
        spec.extensions[PLATFORM] = {**extension, **spec.extensions.get(PLATFORM, {})}

        bindings = self.base_bindings()
        if body.get("name") and body.get("name") != base.get("name"):
            bindings["deployed_name"] = body["name"]

        self.result.records.append(
            build_record(
                agent=self.agent_id(key),
                platform=PLATFORM,
                source=self.source(
                    "apps",
                    key,
                    f"app declares access to: {', '.join(sorted(set(signals)))}"
                    + (f"; code: {linked}" if linked else ""),
                ),
                spec=spec,
                bindings=bindings,
            )
        )

    def link_code(self, spec: AgentSpec, key: str, code: str, where: str) -> Optional[str]:
        """Fill in model, instructions and tools from the app's agent code, when it can be read."""
        from .python import PythonAgentReader

        origin = self.bundle.origins.get(("apps", key), self.bundle.root / "databricks.yml")
        directory = (origin.parent / code).resolve()
        try:
            directory.relative_to(self.bundle.root.resolve())
        except ValueError:
            self.result.warnings.append(f"{where}: source_code_path {code} is outside the bundle; code not read")
            return None
        if not directory.is_dir() or not PythonAgentReader.detect(directory):
            return None
        found = PythonAgentReader().read(directory, ReadOptions(hash_only=self.options.hash_only, link_code=False))
        called = {t.ref.removeprefix("agent/") for r in found.records for t in r.spec.tools if t.ref.startswith("agent/")}
        roots = [r for r in found.records if r.agent not in called]
        if len(roots) != 1:
            if found.records:
                self.result.warnings.append(
                    f"{where}: {len(found.records)} agents in the app code and no single entry point; code not linked"
                )
            return None
        agent = roots[0]
        code_spec = agent.spec
        directory_rel = self.bundle.rel(directory)
        prefix = "" if directory_rel in ("", ".") else f"{directory_rel}/"
        relative = f"{prefix}{agent.source.artifact}"

        def located(value: Value) -> Value:
            return value.model_copy(update={"basis": f"{prefix}{value.basis}" if value.basis else relative})

        if code_spec.model.requested.is_known or not spec.model.requested.is_known:
            spec.model.requested = located(code_spec.model.requested)
        if code_spec.model.provider.is_known:
            spec.model.provider = located(code_spec.model.provider)
        spec.model.parameters = code_spec.model.parameters or spec.model.parameters
        if code_spec.instructions.provenance is not Provenance.UNKNOWN or code_spec.instructions.basis:
            spec.instructions = code_spec.instructions.model_copy(
                update={"basis": f"{prefix}{code_spec.instructions.basis}"}
            )
        for tool in code_spec.tools:
            if all(t.ref != tool.ref for t in spec.tools):
                spec.tools.append(tool)
        for item in code_spec.knowledge:
            if all(k.ref != item.ref for k in spec.knowledge):
                spec.knowledge.append(item)
        for entity in found.entities:
            self._entities.setdefault(entity.ref, entity)
        self.result.warnings += [f"{where}: {w}" for w in found.warnings]
        spec.extensions.setdefault(PLATFORM, {})["code"] = {
            "artifact": relative,
            "framework": code_spec.extensions["python"]["framework"],
            "constructor": agent.source.locator,
        }
        return f"{code_spec.extensions['python']['framework']} agent at {relative}:{agent.source.locator.rsplit(' ', 1)[-1]}"

    def app_resource(self, spec: AgentSpec, item: dict[str, Any], where: str) -> None:
        for kind in ("serving_endpoint", "genie_space", "uc_securable", "sql_warehouse", "job",
                     "secret", "experiment", "database", "postgres", "app"):
            body = item.get(kind)
            if body:
                break
        else:
            self.result.warnings.append(f"{where}: unrecognized app resource {sorted(item)}")
            return

        privilege = str(body.get("permission", ""))
        scope = self.scope_for(privilege, where) if privilege else None

        def grant(target: str) -> None:
            if scope is not None:
                spec.permissions.grants.append(Grant(target=target, privilege=privilege, scope=scope, basis=where))

        def tool_scope() -> Value[Scope]:
            return Value[Scope].derived(scope, f"{where}: {privilege}") if scope else Value[Scope].unknown(where)

        def reversible() -> Value[bool]:
            if scope is Scope.READ:
                return Value[bool].derived(True, "read-only access has no side effects")
            return Value[bool].unknown(f"{where}: effects of {privilege} are not declared")

        if kind == "serving_endpoint":
            grant(self.entity(EntityKind.MODEL, "serving_endpoint", str(body.get("name"))))
        elif kind == "genie_space":
            space_id = str(body.get("space_id", ""))
            match = _RESOURCE_REF.match(space_id)
            if match and match["type"] == "genie_spaces":
                ref = self.agent_entity(match["key"], body.get("name"))
            else:
                ref = self.entity(EntityKind.AGENT, "genie_space", space_id, body.get("name"))
            genie_rule = "a Genie space runs read-only SQL"
            spec.tools.append(
                ToolUse(
                    ref=ref,
                    scope=Value[Scope].derived(Scope.READ, genie_rule),
                    reversible=Value[bool].derived(True, genie_rule),
                )
            )
            grant(ref)
        elif kind == "uc_securable":
            stype = str(body.get("securable_type", "")).upper()
            name = str(body.get("securable_full_name"))
            if stype in ("FUNCTION", "CONNECTION"):
                ref = self.entity(EntityKind.TOOL, f"uc.{stype.lower()}", name)
                spec.tools.append(
                    ToolUse(ref=ref, name=name.rsplit(".", 1)[-1], scope=tool_scope(), reversible=reversible())
                )
            elif stype in ("TABLE", "VOLUME"):
                ref = self.entity(EntityKind.KNOWLEDGE, f"uc.{stype.lower()}", name)
                spec.knowledge.append(KnowledgeUse(ref=ref, name=name, type=stype.lower(), scope=tool_scope()))
            elif stype == "MODEL_SERVICE":
                ref = self.entity(EntityKind.MODEL, "uc.model_service", name)
            else:
                ref = self.entity(EntityKind.OTHER, f"uc.{stype.lower() or 'securable'}", name)
            grant(ref)
        elif kind == "sql_warehouse":
            grant(self.entity(EntityKind.COMPUTE, "sql_warehouse", str(body.get("id"))))
        elif kind == "job":
            ref = self.entity(EntityKind.TOOL, "job", str(body.get("id")))
            spec.tools.append(ToolUse(ref=ref, scope=tool_scope(), reversible=reversible()))
            grant(ref)
        elif kind == "secret":
            grant(self.entity(EntityKind.SECRET, "secret", f"{body.get('scope')}.{body.get('key')}"))
        elif kind == "experiment":
            grant(self.entity(EntityKind.OTHER, "experiment", str(body.get("experiment_id") or item.get("name"))))
        elif kind == "database":
            grant(self.entity(EntityKind.STORE, "database", f"{body.get('instance_name')}.{body.get('database_name')}"))
        elif kind == "postgres":
            grant(self.entity(EntityKind.STORE, "postgres", f"{body.get('branch')}.{body.get('database')}"))
        elif kind == "app":
            grant(self.entity(EntityKind.OTHER, "app", str(body.get("name"))))


__all__ = ["BundleError", "DatabricksBundleReader"]
