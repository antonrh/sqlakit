"""`sqlakit lsp`, a language server for SQL templates.

It reads the project the way `sqlakit check` does, from `pyproject.toml`, and
offers what an editor asks for while a template is written:

- **Problems** as you type: an unknown macro, a call with the wrong arguments,
  a string or a call never closed, an include that is missing or circular.
- **Completion** of macros after `tpl.`, of template names in
  `tpl.include('`, and of the parameters the file already uses after `:`.
- **Hover** on a macro: how a template calls it, and its docstring.
- **Definition** of a macro, in its Python module, and of an included template.

`_Assistant` reads the text at an offset, and knows nothing of the protocol.
`serve` turns it into a server.
"""

from __future__ import annotations

import ast
import inspect
import re
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlparse

from sqlakit._sql import (
    INCLUDE,
    Macro,
    Param,
    SqlMacro,
    _required,
    signature_of,
)
from sqlakit.exceptions import (
    MacroArgumentError,
    MacroSyntaxError,
    ProjectConfigError,
    SQLAKitError,
    UnknownMacroError,
)

from ._project import Project, load_project
from ._static import SKIPPED

if TYPE_CHECKING:
    from lsprotocol import types
    from pygls.lsp.server import LanguageServer
else:
    try:
        from lsprotocol import types
        from pygls.lsp.server import LanguageServer
    except ImportError:  # pragma: no cover - the extra is installed in CI
        types = LanguageServer = None

__all__ = ["Completion", "Diagnostic", "Target", "serve"]

_PARAMETERS = re.compile(r"(?<![:\w\\]):([A-Za-z_]\w*)")
_PARAMETER_TYPED = re.compile(r"(?<![:\w\\]):\w*$")


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """A problem in the text, from one offset to another."""

    start: int
    end: int
    message: str


@dataclass(frozen=True, slots=True)
class Completion:
    """One thing that can be written at the cursor."""

    label: str
    kind: str
    """`macro`, `template` or `parameter`."""
    detail: str = ""
    documentation: str = ""
    snippet: str | None = None
    """The text to insert, with `${1:placeholders}`, when it is not the label."""


@dataclass(frozen=True, slots=True)
class Target:
    """A definition's place: a file, and the line and column in it, from zero.

    The column counts characters, and the server gives it to the editor in the
    UTF-16 units the protocol counts in.
    """

    path: Path
    line: int
    column: int = 0


class _Assistant:
    """Read a template's text at an offset, for one project."""

    def __init__(self, project: Project) -> None:
        self.project = project
        namespace = re.escape(project.templates.namespace)
        self._name_at = re.compile(rf"(?<![\w.])({namespace})\.(\w+)", re.IGNORECASE)
        self._macro_typed = re.compile(rf"(?<![\w.]){namespace}\.(\w*)$", re.IGNORECASE)
        self._include_typed = re.compile(
            rf"(?<![\w.]){namespace}\.{INCLUDE}\(\s*'([^']*)$", re.IGNORECASE
        )
        self._include_path = re.compile(
            rf"(?<![\w.]){namespace}\.{INCLUDE}\(\s*'([^']*)'", re.IGNORECASE
        )

    def applies_to(self, path: Path) -> bool:
        """Whether a file is a template of this project, or a file of its macros."""
        name = self.project.name_of(path)
        return (name is not None and name.endswith(".sql")) or self._reads_macros(path)

    def diagnose(self, path: Path, source: str) -> list[Diagnostic]:
        """Return what is wrong with the text: the first problem, where it is."""
        if self._reads_macros(path):
            return self._diagnose_macros(path, source)
        name = self.project.name_of(path) or path.name
        try:
            self.project.load(name, source)
        except (MacroSyntaxError, UnknownMacroError, MacroArgumentError) as error:
            return [self._placed(error, name, source)]
        return []

    def _reads_macros(self, path: Path) -> bool:
        resolved = path.resolve()
        return any(
            isinstance(macro, SqlMacro) and macro.path.resolve() == resolved
            for macro in self.project.templates.macros.values()
        )

    def _diagnose_macros(self, path: Path, source: str) -> list[Diagnostic]:
        """Return what is wrong with each macro of a file of SQL macros."""
        return [
            Diagnostic(*_line_span(source, line), message)
            for line, message in self.project.macro_problems(path, source)
        ]

    def complete(self, source: str, offset: int) -> list[Completion]:
        """Return what can be written at the offset, given what is typed before it."""
        typed = source[source.rfind("\n", 0, offset) + 1 : offset]
        if match := self._include_typed.search(typed):
            return self._templates(match.group(1))
        if match := self._macro_typed.search(typed):
            return self._macros(match.group(1).lower())
        if _PARAMETER_TYPED.search(typed):
            return self._parameters(source)
        return []

    def hover(self, source: str, offset: int) -> str | None:
        """Return how the macro under the offset is called, and its docstring."""
        name = self._macro_at(source, offset)
        if name == INCLUDE:
            return (
                f"```sql\n{self.project.templates.namespace}.{INCLUDE}('path.sql')"
                "\n```\n\nThe query of another template, in parentheses, where a "
                "table goes. It shares the parameters of the call."
            )
        macro = self.project.templates.macros.get(name or "")
        if macro is None:
            return None
        signature = signature_of(macro, self.project.templates.namespace)
        return f"```sql\n{signature}\n```\n\n{macro.doc}".rstrip()

    def definition(self, source: str, offset: int) -> Target | None:
        """Return where the macro or the included template under the offset is."""
        start = source.rfind("\n", 0, offset) + 1
        end = source.find("\n", offset)
        line = source[start : len(source) if end < 0 else end]
        for match in self._include_path.finditer(line):
            if match.start(1) <= offset - start <= match.end(1):
                path = self.project.path_of(match.group(1))
                return None if path is None else Target(path, 0)
        macro = self.project.templates.macros.get(self._macro_at(source, offset) or "")
        return None if macro is None else _source_of(macro)

    def reads_python(self, path: Path) -> bool:
        """Whether a file is Python of this project, which names templates."""
        if path.suffix != ".py":
            return False
        try:
            relative = path.resolve().relative_to(self.project.root.resolve())
        except ValueError:
            return False
        return not any(part in SKIPPED for part in relative.parts[:-1])

    def python_diagnose(self, source: str) -> list[Diagnostic]:
        """Return each template the code reads that no template directory holds."""
        where = ", ".join(
            _relative(Path(root), self.project.root)
            for root in self.project.templates.paths
        )
        return [
            Diagnostic(start, end, f"No SQL template named `{name}` in {where}.")
            for start, end, name in _template_names(source)
            if self.project.path_of(name) is None
        ]

    def python_complete(self, source: str, offset: int) -> list[Completion]:
        """Return the template names that can go where the code reads one."""
        typed = source[source.rfind("\n", 0, offset) + 1 : offset]
        if match := _TEMPLATE_TYPED.search(typed):
            return [
                Completion(name, "template")
                for name in self.project.templates.names()
                if name.startswith(match.group(1))
            ]
        return []

    def python_definition(self, source: str, offset: int) -> Target | None:
        """Return the template the name under the offset reads."""
        for start, end, name in _template_names(source):
            if start <= offset <= end:
                path = self.project.path_of(name)
                return None if path is None else Target(path, 0)
        return None

    def links(self, path: Path, source: str) -> list[tuple[int, int, Path]]:
        """Return each template the text names, where the name is, and its file."""
        if self.reads_python(path):
            named = _template_names(source)
        else:
            named = [
                (found.start(1), found.end(1), found.group(1))
                for found in self._include_path.finditer(source)
            ]
        return [
            (start, end, target)
            for start, end, name in named
            if (target := self.project.path_of(name)) is not None
        ]

    def _macro_at(self, source: str, offset: int) -> str | None:
        start = source.rfind("\n", 0, offset) + 1
        end = source.find("\n", offset)
        line = source[start : len(source) if end < 0 else end]
        for match in self._name_at.finditer(line):
            if match.start() <= offset - start <= match.end():
                return match.group(2).lower()
        return None

    def _macros(self, typed: str) -> list[Completion]:
        namespace = self.project.templates.namespace
        found = [
            Completion(
                macro.name,
                "macro",
                signature_of(macro, namespace),
                macro.doc,
                _snippet(macro),
            )
            for macro in self.project.templates.macros.values()
            if macro.name.startswith(typed)
        ]
        if INCLUDE.startswith(typed):
            found.append(
                Completion(
                    INCLUDE,
                    "macro",
                    f"{namespace}.{INCLUDE}('path.sql')",
                    "The query of another template, in parentheses.",
                    f"{INCLUDE}('${{1}}')",
                )
            )
        return found

    def _templates(self, typed: str) -> list[Completion]:
        return [
            Completion(name, "template")
            for name in self.project.templates.names()
            if name.startswith(typed)
        ]

    @staticmethod
    def _parameters(source: str) -> list[Completion]:
        names = dict.fromkeys(match.group(1) for match in _PARAMETERS.finditer(source))
        return [Completion(name, "parameter") for name in names]

    def _placed(self, error: SQLAKitError, name: str, source: str) -> Diagnostic:
        """Return the error where it is in this text.

        A problem in a template this one includes is put on the line of the
        `include` that leads to it.
        """
        chain = getattr(error, "chain", ())
        if chain and chain[0][0] == name:
            start, end = _line_span(source, chain[0][1])
            return Diagnostic(start, end, str(error))
        span = getattr(error, "span", None)
        if span is None:
            span = _line_span(source, getattr(error, "line", 1))
        message = getattr(error, "problem", "") or str(error)
        if isinstance(error, MacroArgumentError):
            message = f"{self.project.templates.namespace}.{error.name}: {message}"
        return Diagnostic(span[0], span[1], message)


_TEMPLATE_CALLS = {"sql", "from_file", "from_sql"}
"""The calls whose first argument names a template: `db.sql(...)` and its kin."""

_TEMPLATE_TYPED = re.compile(r"\.(?:sql|from_file|from_sql)\(\s*[\"']([^\"']*)$")


def _template_names(source: str) -> list[tuple[int, int, str]]:
    """Return where the code names a template, the quotes left out, and the name.

    A name is the first argument of `.sql(...)`, `.sql.from_file(...)` or
    `.from_sql(...)`, written as a string that ends in `.sql`.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    starts = [0]
    for line in source.splitlines(keepends=True):
        starts.append(starts[-1] + len(line))
    found = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _TEMPLATE_CALLS
            and node.args
        ):
            continue
        name = node.args[0]
        if not (
            isinstance(name, ast.Constant)
            and isinstance(name.value, str)
            and name.value.endswith(".sql")
            and name.end_lineno is not None
            and name.end_col_offset is not None
        ):
            continue
        start = _char_offset(source, starts, name.lineno, name.col_offset)
        end = _char_offset(source, starts, name.end_lineno, name.end_col_offset)
        quote = source.find(name.value, start, end)
        if quote >= 0:
            found.append((quote, quote + len(name.value), name.value))
    return found


def _char_offset(source: str, starts: list[int], line: int, column: int) -> int:
    """Return the offset of a position `ast` gives, its column in UTF-8 bytes."""
    begin = starts[line - 1]
    end = starts[line] if line < len(starts) else len(source)
    return begin + len(source[begin:end].encode()[:column].decode(errors="ignore"))


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)


def _line_span(source: str, line: int) -> tuple[int, int]:
    """Return the offsets of a line, counting lines from one."""
    start = 0
    for _ in range(line - 1):
        newline = source.find("\n", start)
        if newline < 0:
            break
        start = newline + 1
    end = source.find("\n", start)
    return start, len(source) if end < 0 else end


def _snippet(macro: Macro) -> str:
    """Return a call to the macro with a placeholder for each required argument."""
    placeholders = [
        f":${{{index}:{slot.name}}}"
        if slot.kind is Param
        else f"${{{index}:{slot.name}}}"
        for index, slot in enumerate((slot for slot in macro.slots if slot.required), 1)
    ]
    return f"{macro.name}({', '.join(placeholders)})"


def _source_of(macro: Macro) -> Target | None:
    """Return where a macro's name is written: in its `SELECT`, or its `def`."""
    written = getattr(macro, "path", None)
    named = getattr(macro, "name_at", None)
    if isinstance(written, Path) and named is not None:
        return Target(written, named[0] - 1, named[1])
    try:
        path = inspect.getsourcefile(macro.func)
        lines, first = inspect.getsourcelines(macro.func)
    except (OSError, TypeError):
        return None
    if path is None:
        return None
    for index, line in enumerate(lines):
        if found := _DEF.match(line):
            return Target(Path(path), first - 1 + index, found.start(1))
    return Target(Path(path), max(first - 1, 0))


_DEF = re.compile(r"\s*(?:async\s+)?def\s+(\w+)")


@dataclass(frozen=True, slots=True)
class _At:
    """The text a request is about, the offset in it, and whether it is Python."""

    helper: _Assistant
    source: str
    offset: int
    python: bool = False


def offset_of(source: str, line: int, character: int) -> int:
    """Return the offset of a position the protocol gives, in UTF-16 units."""
    start = 0
    for _ in range(line):
        newline = source.find("\n", start)
        if newline < 0:
            return len(source)
        start = newline + 1
    end = source.find("\n", start)
    text = source[start : len(source) if end < 0 else end]
    units = 0
    for index, char in enumerate(text):
        if units >= character:
            return start + index
        units += 2 if ord(char) > 0xFFFF else 1  # noqa: PLR2004 - past the BMP
    return start + len(text)


def position_of(source: str, offset: int) -> tuple[int, int]:
    """Return the line and the UTF-16 column of an offset, both from zero."""
    line = source.count("\n", 0, offset)
    start = source.rfind("\n", 0, offset) + 1
    column = len(source[start:offset].encode("utf-16-le")) // 2
    return line, column


def serve() -> None:  # pragma: no cover - run over stdio by an editor
    """Run the language server over standard input and output.

    Raises:
        MissingDependencyError: if the `lsp` extra is not installed.

    """
    _server().start_io()


def _server() -> Any:  # noqa: ANN401, C901, PLR0915 - a handler for each request
    language_server = _required(
        LanguageServer, "pygls", "`sqlakit lsp`", "sqlakit[lsp]"
    )
    server = language_server("sqlakit", version("sqlakit"))
    state: dict[str, Any] = {"assistant": None, "problem": None}

    def assistant() -> _Assistant | None:
        return state["assistant"]

    @server.feature(types.INITIALIZED)
    def initialized(ls: LanguageServer, _: Any) -> None:  # noqa: ANN401
        root = ls.workspace.root_path
        try:
            state["assistant"] = _Assistant(load_project(Path(root or ".")))
        except ProjectConfigError as error:
            state["problem"] = str(error)
            ls.window_show_message(
                types.ShowMessageParams(types.MessageType.Warning, str(error))
            )

    def publish(ls: LanguageServer, uri: str) -> None:
        document = ls.workspace.get_text_document(uri)
        path = _path(uri)
        helper = assistant()
        found: list[Diagnostic] = []
        if helper is not None and helper.reads_python(path):
            found = helper.python_diagnose(document.source)
        elif helper is not None and helper.applies_to(path):
            found = helper.diagnose(path, document.source)
        ls.text_document_publish_diagnostics(
            types.PublishDiagnosticsParams(
                uri=uri,
                version=document.version,
                diagnostics=[_diagnostic(document.source, one) for one in found],
            )
        )

    @server.feature(types.TEXT_DOCUMENT_DID_OPEN)
    def did_open(ls: LanguageServer, params: Any) -> None:  # noqa: ANN401
        publish(ls, params.text_document.uri)

    @server.feature(types.TEXT_DOCUMENT_DID_CHANGE)
    def did_change(ls: LanguageServer, params: Any) -> None:  # noqa: ANN401
        publish(ls, params.text_document.uri)

    @server.feature(types.TEXT_DOCUMENT_DID_SAVE)
    def did_save(ls: LanguageServer, params: Any) -> None:  # noqa: ANN401
        publish(ls, params.text_document.uri)

    def at(ls: LanguageServer, params: Any) -> _At | None:  # noqa: ANN401
        helper = assistant()
        path = _path(params.text_document.uri)
        if helper is None:
            return None
        python = helper.reads_python(path)
        if not (python or helper.applies_to(path)):
            return None
        source = ls.workspace.get_text_document(params.text_document.uri).source
        position = params.position
        offset = offset_of(source, position.line, position.character)
        return _At(helper, source, offset, python=python)

    kinds = {
        "macro": types.CompletionItemKind.Function,
        "template": types.CompletionItemKind.File,
        "parameter": types.CompletionItemKind.Variable,
    }

    @server.feature(
        types.TEXT_DOCUMENT_COMPLETION,
        types.CompletionOptions(trigger_characters=[".", "'", '"', ":"]),
    )
    def completion(ls: LanguageServer, params: Any) -> Any:  # noqa: ANN401
        found = at(ls, params)
        if found is None:
            return None
        helper, source, offset = found.helper, found.source, found.offset
        written = (
            helper.python_complete(source, offset)
            if found.python
            else helper.complete(source, offset)
        )
        return types.CompletionList(
            is_incomplete=False,
            items=[
                types.CompletionItem(
                    label=one.label,
                    kind=kinds[one.kind],
                    detail=one.detail or None,
                    documentation=types.MarkupContent(
                        types.MarkupKind.Markdown, one.documentation
                    )
                    if one.documentation
                    else None,
                    insert_text=one.snippet,
                    insert_text_format=types.InsertTextFormat.Snippet
                    if one.snippet
                    else None,
                )
                for one in written
            ],
        )

    @server.feature(types.TEXT_DOCUMENT_HOVER)
    def hover(ls: LanguageServer, params: Any) -> Any:  # noqa: ANN401
        found = at(ls, params)
        text = (
            None
            if found is None or found.python
            else found.helper.hover(found.source, found.offset)
        )
        if text is None:
            return None
        return types.Hover(types.MarkupContent(types.MarkupKind.Markdown, text))

    def located(ls: LanguageServer, params: Any) -> Any:  # noqa: ANN401
        """Return where the macro or the template under the cursor is written."""
        found = at(ls, params)
        if found is None:
            return None
        target = (
            found.helper.python_definition(found.source, found.offset)
            if found.python
            else found.helper.definition(found.source, found.offset)
        )
        if target is None:
            return None
        start = types.Position(target.line, _utf16_column(target))
        return types.Location(target.path.resolve().as_uri(), types.Range(start, start))

    # A macro has one place, so the three requests get one answer.
    for method in (
        types.TEXT_DOCUMENT_DEFINITION,
        types.TEXT_DOCUMENT_IMPLEMENTATION,
        types.TEXT_DOCUMENT_DECLARATION,
    ):
        server.feature(method)(located)

    @server.feature(types.TEXT_DOCUMENT_DOCUMENT_LINK)
    def document_link(ls: LanguageServer, params: Any) -> Any:  # noqa: ANN401
        helper = assistant()
        path = _path(params.text_document.uri)
        if helper is None or not (helper.reads_python(path) or helper.applies_to(path)):
            return []
        source = ls.workspace.get_text_document(params.text_document.uri).source
        return [
            types.DocumentLink(
                range=types.Range(
                    types.Position(*position_of(source, start)),
                    types.Position(*position_of(source, end)),
                ),
                target=target.resolve().as_uri(),
            )
            for start, end, target in helper.links(path, source)
        ]

    return server


def _utf16_column(target: Target) -> int:
    """Return a target's column in the UTF-16 units the protocol counts in."""
    if not target.column:
        return 0
    try:
        line = target.path.read_text(encoding="utf-8").splitlines()[target.line]
    except (OSError, IndexError):
        return target.column
    return len(line[: target.column].encode("utf-16-le")) // 2


def _diagnostic(source: str, found: Diagnostic) -> types.Diagnostic:
    start = types.Position(*position_of(source, found.start))
    end = types.Position(*position_of(source, max(found.end, found.start)))
    return types.Diagnostic(
        range=types.Range(start, end),
        message=found.message,
        severity=types.DiagnosticSeverity.Error,
        source="sqlakit",
    )


def _path(uri: str) -> Path:
    return Path(unquote(urlparse(uri).path))
