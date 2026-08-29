# -*- mode: python; coding: utf-8; -*-
from __future__ import annotations

import csv
import sys
import xml.sax
import xml.sax.handler

class RowSelector(xml.sax.handler.ContentHandler):

    def __init__(self, columnlist: list[str], callback) -> None:
        self.columnlist: list[str] = columnlist
        self.callback = callback
        #self.csvWriter = csv.writer(sys.stdout)
        #,quotechar='"', escapechar='\\',doublequote=False,quoting=csv.QUOTE_NONE)

    # SAX has already decoded XML entities; only change line breaks here.
    # Algunos lectores de CSV (p.ej. Drill) no permiten retornos de carro
    # en las cadenas con comillas.
    @classmethod
    def prepareString(cls, str_: str) -> str:
        return str_.replace('\r\n', '<br/>').replace('\n', '<br/>')

    def run(self, f = sys.stdin, out=sys.stdout) -> None:
        self.csvWriter = csv.writer(out, lineterminator='\n')
        self.parse(f)

    def parse(self, f) -> None:
        # Estamos en la primera fila, dar la salida de la cabecera
        self.csvWriter.writerow(self.columnlist)
        xml.sax.parse(f, self)

    def startElement(self, name: str, attrs) -> None:
        if name == 'row' and (not self.callback or self.callback(name, attrs)):
            self.csvWriter.writerow(
                (RowSelector.prepareString(attrs.get(c,'')) for c in self.columnlist))
