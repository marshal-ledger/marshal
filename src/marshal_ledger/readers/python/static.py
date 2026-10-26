"""Static evaluation of Python source, for reading agents defined in code.

Nothing here imports or runs the code under analysis. Files are parsed with
:mod:`ast`, and a small evaluator resolves what can be known without running
anything: literals, string concatenation and f-strings, names bound in the same
function or module, imports between modules of the same project, and
``textwrap.dedent``. Everything else evaluates to an :class:`Unknown` that says
why, so readers record it as unknown instead of guessing.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional, Union

MAX_FILES = 2000
MAX_FILE_BYTES = 1_000_000
MAX_DEPTH = 40
_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "site-packages", ".tox", "build", "dist"}


@dataclass(frozen=True)
class Unknown:
    """A value that can't be known statically."""

    reason: str
    where: Optional[str] = None

    def __str__(self) -> str:
        return f"{self.reason} ({self.where})" if self.where else self.reason


@dataclass(frozen=True)
class Ref:
    """A symbol from outside the project, by qualified name, such as ``agents.WebSearchTool``."""

    qualname: str


@dataclass(frozen=True)
class ModuleRef:
    module: "Module"


@dataclass(frozen=True, eq=False)
class Func:
    """A function or method defined in the project."""

    module: "Module"
    node: Union[ast.FunctionDef, ast.AsyncFunctionDef]
    qualname: str

    @property
    def name(self) -> str:
        return self.node.name

    @property
    def where(self) -> str:
        return f"{self.module.rel}:{self.node.lineno}"

    def docstring(self) -> Optional[str]:
        return ast.get_docstring(self.node)


@dataclass(frozen=True, eq=False)
class Call:
    """A call whose callee is known by name, with its evaluated arguments."""

    func: Any
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    module: "Module"
    node: ast.Call

    @property
    def where(self) -> str:
        return f"{self.module.rel}:{self.node.lineno}"

    @property
    def qualname(self) -> Optional[str]:
        return callee_name(self.func)


@dataclass(frozen=True, eq=False)
class Member:
    """An attribute of a value that is not itself a module or project symbol, such as ``toolkit.tools``."""

    base: Any
    attr: str


@dataclass(frozen=True, eq=False)
class Item:
    """A subscript, such as ``self.agents_config["researcher"]``."""

    base: Any
    key: Any


@dataclass(frozen=True)
class Template:
    """A string with parts that can't be resolved, such as an f-string over an SDK constant."""

    parts: tuple[Union[str, Unknown], ...]

    def text(self) -> str:
        return "".join(p if isinstance(p, str) else "{" + p.reason + "}" for p in self.parts)

    def unresolved(self) -> list[Unknown]:
        return [p for p in self.parts if isinstance(p, Unknown)]


def callee_name(value: Any) -> Optional[str]:
    if isinstance(value, Ref):
        return value.qualname
    if isinstance(value, Func):
        return value.qualname
    if isinstance(value, Item):
        return callee_name(value.base)  # Agent[Context](...) is a call to Agent
    if isinstance(value, Member):
        base = callee_name(value.base) if not isinstance(value.base, Call) else value.base.qualname
        return f"{base}.{value.attr}" if base else None
    return None


@dataclass
class Module:
    name: str
    path: Path
    rel: str
    tree: ast.Module
    is_package: bool
    imports: dict[str, tuple[str, Optional[str]]] = field(default_factory=dict)
    """Local name → (module, attribute). ``import a.b as c`` is ("a.b", None)."""
    bindings: dict[str, list[ast.AST]] = field(default_factory=dict)
    """Module-level name → assigned value nodes, in source order."""
    functions: dict[str, Union[ast.FunctionDef, ast.AsyncFunctionDef]] = field(default_factory=dict)
    classes: dict[str, ast.ClassDef] = field(default_factory=dict)
    project: str = ""
    """Name of the directory module names are relative to; scopes tool refs to the project."""

    @property
    def package(self) -> str:
        return self.name if self.is_package else self.name.rpartition(".")[0]


def _bind(target: ast.AST, value: ast.AST, into: dict[str, list[ast.AST]]) -> None:
    if isinstance(target, ast.Name):
        into.setdefault(target.id, []).append(value)


def _index_module(module: Module) -> None:
    for node in module.tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".")[0]
                module.imports[local] = (alias.name if alias.asname else alias.name.split(".")[0], None)
        elif isinstance(node, ast.ImportFrom):
            base = _absolute(module, node.module, node.level)
            for alias in node.names:
                if alias.name != "*":
                    module.imports[alias.asname or alias.name] = (base, alias.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            module.functions[node.name] = node
        elif isinstance(node, ast.ClassDef):
            module.classes[node.name] = node
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                _bind(target, node.value, module.bindings)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            _bind(node.target, node.value, module.bindings)


def _absolute(module: Module, name: Optional[str], level: int) -> str:
    if level == 0:
        return name or ""
    parts = module.package.split(".") if module.package else []
    if level > 1:
        parts = parts[: len(parts) - (level - 1)]
    return ".".join(p for p in [*parts, name or ""] if p)


class Project:
    """Every Python module under a root directory, indexed by dotted name."""

    def __init__(self, root: Path):
        self.root = root
        self.modules: dict[str, Module] = {}
        self.errors: list[str] = []
        bases = [root] + [root / "src"] if (root / "src").is_dir() else [root]
        count = 0
        for path in sorted(root.rglob("*.py")):
            rel_parts = path.relative_to(root).parts
            if any(part in _SKIP_DIRS or part.startswith(".") for part in rel_parts[:-1]):
                continue
            count += 1
            if count > MAX_FILES:
                self.errors.append(f"more than {MAX_FILES} Python files; the rest were not read")
                break
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except (SyntaxError, UnicodeDecodeError, ValueError) as exc:
                self.errors.append(f"{path.relative_to(root).as_posix()}: not parsed: {exc.__class__.__name__}")
                continue
            for base in bases:
                try:
                    parts = list(path.relative_to(base).with_suffix("").parts)
                except ValueError:
                    continue
                is_package = parts[-1] == "__init__"
                if is_package:
                    parts = parts[:-1]
                name = ".".join(parts)
                module = Module(name, path, path.relative_to(root).as_posix(), tree, is_package, project=root.name)
                _index_module(module)
                self.modules.setdefault(name, module)

    def unique_modules(self) -> Iterator[Module]:
        seen: set[Path] = set()
        for module in self.modules.values():
            if module.path not in seen:
                seen.add(module.path)
                yield module

    def resolve_module(self, name: str) -> Optional[Module]:
        if name in self.modules:
            return self.modules[name]
        # Imports written relative to a package inside the root, such as ``from agent_server.utils``.
        for module_name, module in self.modules.items():
            if module_name.endswith("." + name):
                return module
        return None


@dataclass
class Scope:
    module: Module
    locals: dict[str, list[ast.AST]] = field(default_factory=dict)
    params: set[str] = field(default_factory=set)
    function: Optional[str] = None


def function_scope(module: Module, node: Union[ast.FunctionDef, ast.AsyncFunctionDef], qualname: str) -> Scope:
    scope = Scope(module=module, function=qualname)
    args = node.args
    for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
        scope.params.add(arg.arg)
    if args.vararg:
        scope.params.add(args.vararg.arg)
    if args.kwarg:
        scope.params.add(args.kwarg.arg)
    for child in ast.walk(node):
        if child is node:
            continue
        if isinstance(child, ast.Assign):
            for target in child.targets:
                _bind(target, child.value, scope.locals)
        elif isinstance(child, ast.AnnAssign) and child.value is not None:
            _bind(child.target, child.value, scope.locals)
    return scope


_SAFE_STR_METHODS = {"strip", "lstrip", "rstrip", "lower", "upper"}


class Evaluator:
    def __init__(self, project: Project):
        self.project = project
        self._active: set[int] = set()

    def module_scope(self, module: Module) -> Scope:
        return Scope(module=module)

    def where(self, scope: Scope, node: ast.AST) -> str:
        return f"{scope.module.rel}:{getattr(node, 'lineno', '?')}"

    def eval(self, node: Optional[ast.AST], scope: Scope, depth: int = 0) -> Any:
        if node is None:
            return None
        if depth > MAX_DEPTH or id(node) in self._active:
            return Unknown("value too deeply nested to resolve", self.where(scope, node))
        self._active.add(id(node))
        try:
            return self._eval(node, scope, depth + 1)
        finally:
            self._active.discard(id(node))

    def _eval(self, node: ast.AST, scope: Scope, depth: int) -> Any:
        where = self.where(scope, node)
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.JoinedStr):
            parts: list[Union[str, Unknown]] = []
            for part in node.values:
                if isinstance(part, ast.Constant):
                    parts.append(str(part.value))
                elif isinstance(part, ast.FormattedValue):
                    value = self.eval(part.value, scope, depth)
                    if isinstance(value, (str, int, float)) and part.conversion == -1 and part.format_spec is None:
                        parts.append(str(value))
                    elif isinstance(value, Template):
                        parts.extend(value.parts)
                    else:
                        parts.append(Unknown(_describe(part.value), where))
            return _join(parts)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = self.eval(node.left, scope, depth), self.eval(node.right, scope, depth)
            if isinstance(left, list) and isinstance(right, list):
                return left + right
            if (isinstance(left, list) or isinstance(right, list)) and not _stringish(left) and not _stringish(right):
                # ``toolkit.tools + [retriever]``: the non-list side is itself a collection, kept as one element.
                return (left if isinstance(left, list) else [left]) + (right if isinstance(right, list) else [right])
            if _stringish(left) and _stringish(right):
                return _join([*_parts(left), *_parts(right)])
            return Unknown("expression computed at run time", where)
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            items = []
            for element in node.elts:
                if isinstance(element, ast.Starred):
                    value = self.eval(element.value, scope, depth)
                    items.extend(value if isinstance(value, list) else [Unknown("unpacked value", where)])
                else:
                    items.append(self.eval(element, scope, depth))
            return items
        if isinstance(node, ast.Dict):
            out = {}
            for key, value in zip(node.keys, node.values):
                if key is None:
                    continue
                k = self.eval(key, scope, depth)
                if isinstance(k, (str, int, float, bool)):
                    out[k] = self.eval(value, scope, depth)
            return out
        if isinstance(node, ast.Name):
            return self._name(node.id, scope, node, depth)
        if isinstance(node, ast.Attribute):
            base = self.eval(node.value, scope, depth)
            if isinstance(base, ModuleRef):
                return self._global(base.module, node.attr, depth, node)
            if isinstance(base, Ref):
                return Ref(f"{base.qualname}.{node.attr}")
            if isinstance(base, Unknown):
                return base
            return Member(base, node.attr)
        if isinstance(node, ast.Subscript):
            base = self.eval(node.value, scope, depth)
            key = self.eval(node.slice, scope, depth)
            if isinstance(base, dict) and isinstance(key, (str, int)) and key in base:
                return base[key]
            if isinstance(base, list) and isinstance(key, int) and -len(base) <= key < len(base):
                return base[key]
            return Item(base, key)
        if isinstance(node, ast.Call):
            return self._call(node, scope, depth)
        if isinstance(node, ast.Await):
            return Unknown("value awaited at run time", where)
        if isinstance(node, (ast.Lambda, ast.ListComp, ast.DictComp, ast.SetComp, ast.GeneratorExp)):
            return Unknown(f"{node.__class__.__name__.lower()} computed at run time", where)
        if isinstance(node, ast.IfExp):
            return Unknown("conditional value", where)
        return Unknown("expression computed at run time", where)

    def _name(self, name: str, scope: Scope, node: ast.AST, depth: int) -> Any:
        where = self.where(scope, node)
        if name in scope.locals:
            assigned = [v for v in scope.locals[name] if getattr(v, "lineno", 0) <= getattr(node, "lineno", 10**9)]
            if len(assigned) >= 1:
                if len(set(map(ast.dump, assigned))) > 1:
                    return Unknown(f"{name} is assigned more than once", where)
                return self.eval(assigned[-1], scope, depth)
        if name in scope.params:
            if name in ("self", "cls"):
                return Ref(name)  # kept symbolic, so profiles can match ``self.agents_config[...]``
            return Unknown(f"parameter {name}", where)
        return self._global(scope.module, name, depth, node)

    def _global(self, module: Module, name: str, depth: int, node: ast.AST) -> Any:
        where = f"{module.rel}:{getattr(node, 'lineno', '?')}"
        if name in module.functions:
            fn = module.functions[name]
            return Func(module, fn, f"{module.name}.{name}" if module.name else name)
        if name in module.bindings:
            values = module.bindings[name]
            if len(values) > 1 and len(set(map(ast.dump, values))) > 1:
                return Unknown(f"{name} is assigned more than once", where)
            return self.eval(values[-1], Scope(module=module), depth)
        if name in module.classes:
            return Ref(f"{module.name}.{name}" if module.name else name)
        if name in module.imports:
            target, attr = module.imports[name]
            return self._import(target, attr, depth, node, module)
        if name in {"True", "False", "None"}:
            return {"True": True, "False": False, "None": None}[name]
        return Unknown(f"name {name} is not defined statically", where)

    def _import(self, target: str, attr: Optional[str], depth: int, node: ast.AST, module: Module) -> Any:
        local = self.project.resolve_module(target)
        if attr is None:
            return ModuleRef(local) if local else Ref(target)
        if local is not None:
            submodule = self.project.resolve_module(f"{local.name}.{attr}") if local.is_package else None
            if attr in local.functions or attr in local.bindings or attr in local.classes or attr in local.imports:
                return self._global(local, attr, depth, node)
            if submodule is not None:
                return ModuleRef(submodule)
            return Unknown(f"{attr} not found in {target}", f"{module.rel}:{getattr(node, 'lineno', '?')}")
        submodule = self.project.resolve_module(f"{target}.{attr}") if target else self.project.resolve_module(attr)
        if submodule is not None:
            return ModuleRef(submodule)
        return Ref(f"{target}.{attr}" if target else attr)

    def _call(self, node: ast.Call, scope: Scope, depth: int) -> Any:
        where = self.where(scope, node)
        func = self.eval(node.func, scope, depth)
        name = callee_name(func) or ""
        if name in ("os.getenv", "os.environ.get"):
            var = self.eval(node.args[0], scope, depth) if node.args else None
            return Unknown(f"environment variable {var}" if isinstance(var, str) else "environment variable", where)
        args = tuple(self.eval(a, scope, depth) for a in node.args if not isinstance(a, ast.Starred))
        kwargs = {k.arg: self.eval(k.value, scope, depth) for k in node.keywords if k.arg}
        if name == "textwrap.dedent" and args and isinstance(args[0], str):
            import textwrap

            return textwrap.dedent(args[0])
        if isinstance(func, Member) and func.attr in _SAFE_STR_METHODS and isinstance(func.base, str) and not args:
            return getattr(func.base, func.attr)()
        if isinstance(func, Member) and func.attr == "format" and isinstance(func.base, str):
            return Unknown("string formatted at run time", where)
        if name in ("open", "builtins.open") or (isinstance(func, Member) and func.attr in ("read", "read_text")):
            return self._read_file(func, args, scope, where)
        return Call(func, args, kwargs, scope.module, node)

    def _read_file(self, func: Any, args: tuple[Any, ...], scope: Scope, where: str) -> Any:
        """``Path("prompt.md").read_text()`` and ``open("prompt.md").read()``, for files inside the root."""
        target = None
        if isinstance(func, Member) and isinstance(func.base, Call):
            inner = func.base
            if inner.qualname in ("pathlib.Path", "open", "builtins.open") and inner.args and isinstance(inner.args[0], str):
                target = inner.args[0]
        if target is None:
            return Unknown("file read at run time", where)
        for base in (scope.module.path.parent, self.project.root):
            candidate = (base / target).resolve()
            try:
                candidate.relative_to(self.project.root.resolve())
            except ValueError:
                continue
            if candidate.is_file() and candidate.stat().st_size <= MAX_FILE_BYTES:
                return candidate.read_text(encoding="utf-8", errors="replace")
        return Unknown(f"file {target} not found in the project", where)


def _describe(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:  # pragma: no cover - unparse handles every expression ast.parse produces
        return "expression"


def _stringish(value: Any) -> bool:
    return isinstance(value, (str, Template))


def _parts(value: Any) -> list[Union[str, Unknown]]:
    return list(value.parts) if isinstance(value, Template) else [value]


def _join(parts: list[Union[str, Unknown]]) -> Union[str, Template]:
    merged: list[Union[str, Unknown]] = []
    for part in parts:
        if isinstance(part, str) and merged and isinstance(merged[-1], str):
            merged[-1] += part
        else:
            merged.append(part)
    if all(isinstance(p, str) for p in merged):
        return "".join(merged)  # type: ignore[arg-type]
    return Template(tuple(merged))


def iter_calls(project: Project) -> Iterator[tuple[ast.Call, Scope, Optional[str]]]:
    """Every call in the project, with the scope it runs in and the name it is assigned to, if any."""
    for module in project.unique_modules():
        yield from _calls_in(module.tree.body, Scope(module=module), module, prefix=module.name)


def _calls_in(body: list[ast.stmt], scope: Scope, module: Module, prefix: str) -> Iterator[tuple[ast.Call, Scope, Optional[str]]]:
    for statement in body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            qualname = f"{prefix}.{statement.name}" if prefix else statement.name
            yield from _calls_in(statement.body, function_scope(module, statement, qualname), module, qualname)
            continue
        if isinstance(statement, ast.ClassDef):
            yield from _calls_in(statement.body, scope, module, f"{prefix}.{statement.name}" if prefix else statement.name)
            continue
        assigned = None
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1 and isinstance(statement.targets[0], ast.Name):
            assigned = statement.targets[0].id
        elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
            assigned = statement.target.id
        elif isinstance(statement, ast.Return):
            assigned = scope.function.rsplit(".", 1)[-1] if scope.function else None
        for child in ast.walk(statement):
            if isinstance(child, ast.Call):
                yield child, scope, assigned if child is getattr(statement, "value", None) else None
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and child is not statement:
                pass
