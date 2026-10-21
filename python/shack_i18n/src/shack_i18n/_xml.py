"""A minimal XML tree with line and column numbers.

``xml.etree`` drops positions, and a finding without a line is no use to the
person fixing it, so this builds the little tree the catalog and layout
checks need straight from expat. External entities are never resolved
(expat does not fetch them by default, and nothing here asks it to).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from xml.parsers import expat


@dataclass
class Node:
    tag: str
    attrib: dict[str, str]
    line: int
    col: int  # 1-based
    children: list["Node"] = field(default_factory=list)
    text: str = ""  # all character data inside, children's included

    def find_all(self, tag: str) -> list["Node"]:
        return [c for c in self.children if c.tag == tag]

    def first(self, tag: str) -> "Node | None":
        return next((c for c in self.children if c.tag == tag), None)

    def iter(self):
        yield self
        for c in self.children:
            yield from c.iter()


class XMLError(Exception):
    def __init__(self, message: str, line: int, col: int):
        super().__init__(message)
        self.line, self.col = line, col


def parse(text: str) -> Node:
    p = expat.ParserCreate()
    stack: list[Node] = []
    root: list[Node] = []

    def start(tag, attrs):
        node = Node(tag, dict(attrs), p.CurrentLineNumber, p.CurrentColumnNumber + 1)
        if stack:
            stack[-1].children.append(node)
        else:
            root.append(node)
        stack.append(node)

    def end(tag):
        stack.pop()

    def data(chars):
        for n in stack:
            n.text += chars

    p.StartElementHandler, p.EndElementHandler, p.CharacterDataHandler = start, end, data
    try:
        p.Parse(text, True)
    except expat.ExpatError as err:
        raise XMLError(expat.ErrorString(err.code), err.lineno, err.offset + 1) from err
    if not root:
        raise XMLError("no root element", 1, 1)
    return root[0]
