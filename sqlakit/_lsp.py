"""`sqlakit lsp`, a language server for `.tpl.sql` templates.

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

import inspect
import re
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlparse

from ._project import Project, load_project
from ._sql import INCLUDE, Macro, Param, _required, signature_of
from .exceptions import (
    MacroArgumentError,
    MacroSyntaxError,
    ProjectConfigError,
    SQLAKitError,
    UnknownMacroError,
)

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
    """A definition's place: a file, and the line in it, from zero."""

    path: Path
    line: int


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
        """Whether a file is a macro template of this project."""
        name = self.project.name_of(path)
        return name is not None and self.project.reads_macros(name)

    def diagnose(self, path: Path, source: str) -> list[Diagnostic]:
        """Return what is wrong with the text: the first problem, where it is."""
        name = self.project.name_of(path) or path.name
        try:
            self.project.load(name, source)
        except (MacroSyntaxError, UnknownMacroError, MacroArgumentError) as error:
            return [self._placed(error, name, source)]
        return []

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
                f"```sql\n{self.project.templates.namespace}.{INCLUDE}('path.tpl.sql')"
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
                    f"{namespace}.{INCLUDE}('path.tpl.sql')",
                    "The query of another template, in parentheses.",
                    f"{INCLUDE}('${{1}}')",
                )
            )
        return found

    def _templates(self, typed: str) -> list[Completion]:
        return [
            Completion(name, "template")
            for name in self.project.templates.names()
            if name.startswith(typed) and self.project.reads_macros(name)
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
    """Return where a macro's function is written, when Python can say."""
    try:
        path = inspect.getsourcefile(macro.func)
        _, line = inspect.getsourcelines(macro.func)
    except (OSError, TypeError):
        return None
    return None if path is None else Target(Path(path), max(line - 1, 0))


# UTF-16, which the protocol counts columns in.


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


# The server.


def serve() -> None:  # pragma: no cover - run over stdio by an editor
    """Run the language server over standard input and output.

    Raises:
        MissingDependencyError: if the `lsp` extra is not installed.

    """
    _server().start_io()


def _server() -> Any:  # noqa: ANN401, C901 - the protocol's objects, and its handlers
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
        if helper is None or not helper.applies_to(path):
            found: list[Diagnostic] = []
        else:
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

    def at(ls: LanguageServer, params: Any) -> tuple[_Assistant, str, int] | None:  # noqa: ANN401
        helper = assistant()
        path = _path(params.text_document.uri)
        if helper is None or not helper.applies_to(path):
            return None
        source = ls.workspace.get_text_document(params.text_document.uri).source
        position = params.position
        return helper, source, offset_of(source, position.line, position.character)

    kinds = {
        "macro": types.CompletionItemKind.Function,
        "template": types.CompletionItemKind.File,
        "parameter": types.CompletionItemKind.Variable,
    }

    @server.feature(
        types.TEXT_DOCUMENT_COMPLETION,
        types.CompletionOptions(trigger_characters=[".", "'", ":"]),
    )
    def completion(ls: LanguageServer, params: Any) -> Any:  # noqa: ANN401
        found = at(ls, params)
        if found is None:
            return None
        helper, source, offset = found
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
                for one in helper.complete(source, offset)
            ],
        )

    @server.feature(types.TEXT_DOCUMENT_HOVER)
    def hover(ls: LanguageServer, params: Any) -> Any:  # noqa: ANN401
        found = at(ls, params)
        text = None if found is None else found[0].hover(found[1], found[2])
        if text is None:
            return None
        return types.Hover(types.MarkupContent(types.MarkupKind.Markdown, text))

    @server.feature(types.TEXT_DOCUMENT_DEFINITION)
    def definition(ls: LanguageServer, params: Any) -> Any:  # noqa: ANN401
        found = at(ls, params)
        target = None if found is None else found[0].definition(found[1], found[2])
        if target is None:
            return None
        start = types.Position(target.line, 0)
        return types.Location(target.path.resolve().as_uri(), types.Range(start, start))

    return server


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
