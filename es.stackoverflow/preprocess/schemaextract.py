# -*- mode: python; coding: utf-8; -*-
from __future__ import annotations

import sys
import xml.sax
import xml.sax.handler

class SchemaExtract(xml.sax.handler.ContentHandler):

    def __init__(self) -> None:
        self.attrs: set[str] = set()

    def run(self, f=sys.stdin) -> None:
        self.attrs.clear()
        self.parse(f)

    def getAttrs(self) -> list[str]:
        return sorted(self.attrs, key=lambda attr: (attr.casefold(), attr))

    def parse(self, f) -> None:
        xml.sax.parse(f, self)

    def startElement(self, name, attrs) -> None:
        if name == 'row':
            self.attrs.update(attrs.keys())

if __name__ == "__main__":
    se = SchemaExtract()
    se.run()
    print (se.getAttrs())
