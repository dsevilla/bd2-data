from __future__ import annotations

import argparse
from pathlib import Path
import sys

if __package__:
    from .rowselector import RowSelector
    from .schemaextract import SchemaExtract
else:
    from rowselector import RowSelector
    from schemaextract import SchemaExtract


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Convert a Stack Exchange XML dump file to CSV.')
    parser.add_argument('input', type=Path, help='input XML file')
    parser.add_argument('output', type=Path, nargs='?', help='output CSV file (stdout if omitted)')
    args: argparse.Namespace = parser.parse_args(argv)

    if args.output and args.input.resolve() == args.output.resolve():
        parser.error('input and output must be different files')

    with args.input.open('r', encoding='utf-8', newline='') as source:
        schema = SchemaExtract()
        schema.run(source)
        columns: list[str] = schema.getAttrs()

    # Keep a stable schema and put the conventional primary key first.
    if 'Id' in columns:
        columns.remove('Id')
        columns.insert(0, 'Id')

    print(columns, file=sys.stderr)

    if args.output:
        with args.input.open('r', encoding='utf-8', newline='') as source, \
                args.output.open('w', encoding='utf-8', newline='') as target:
            RowSelector(columns, callback=None).run(source, target)
    else:
        with args.input.open('r', encoding='utf-8', newline='') as source:
            RowSelector(columns, callback=None).run(source, sys.stdout)

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
