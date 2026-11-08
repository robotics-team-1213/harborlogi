"""Minimal .xlsx reading and writing for scenario test sheets (KAN-118), standard library only.

Reads plain cell values (shared strings, inline strings, numbers, booleans) of every worksheet by its
sheet name; formulas give their cached value. Writes one inline-string workbook for the template. Size
limits bound an uploaded file before any XML is parsed.
"""
import io
import re
import zipfile
from xml.etree import ElementTree

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_UNPACKED_BYTES = 20 * 1024 * 1024
MAX_ROWS, MAX_COLUMNS = 500, 26
MAIN = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
REL = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
PKG = '{http://schemas.openxmlformats.org/package/2006/relationships}'


class SheetError(ValueError):
    """The upload is not a readable workbook within the limits."""


def _column(reference):
    letters = re.match(r'[A-Z]+', reference or '')
    if not letters:
        raise SheetError('셀 주소를 읽을 수 없습니다.')
    number = 0
    for letter in letters.group():
        number = number * 26 + ord(letter) - 64
    return number - 1


def _text(node):
    return ''.join(t.text or '' for t in node.iter(f'{MAIN}t'))


def read_xlsx(data):
    """{sheet name: [[str, ...], ...]} with trailing empty rows dropped."""
    if len(data) > MAX_FILE_BYTES:
        raise SheetError('파일이 2 MB를 넘습니다.')
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as error:
        raise SheetError('xlsx 파일이 아닙니다.') from error
    with archive:
        if len(archive.infolist()) > 64 or sum(i.file_size for i in archive.infolist()) > MAX_UNPACKED_BYTES:
            raise SheetError('파일 안 내용이 너무 큽니다.')

        def xml(name):
            try:
                return ElementTree.fromstring(archive.read(name))
            except KeyError as error:
                raise SheetError(f'{name}이 없습니다.') from error
            except ElementTree.ParseError as error:
                raise SheetError(f'{name}을 읽을 수 없습니다.') from error

        shared = [_text(item) for item in xml('xl/sharedStrings.xml').iter(f'{MAIN}si')] \
            if 'xl/sharedStrings.xml' in archive.namelist() else []
        targets = {rel.get('Id'): rel.get('Target') for rel in xml('xl/_rels/workbook.xml.rels').iter(f'{PKG}Relationship')}
        sheets = {}
        for sheet in xml('xl/workbook.xml').iter(f'{MAIN}sheet'):
            target = targets.get(sheet.get(f'{REL}id'), '')
            path = target.lstrip('/') if target.startswith('/') else 'xl/' + target
            rows = []
            for row in xml(path).iter(f'{MAIN}row'):
                if len(rows) >= MAX_ROWS:
                    raise SheetError(f'{sheet.get("name")} 시트가 {MAX_ROWS}행을 넘습니다.')
                values = {}
                for cell in row.iter(f'{MAIN}c'):
                    column = _column(cell.get('r'))
                    if column >= MAX_COLUMNS:
                        continue
                    kind, value = cell.get('t'), cell.find(f'{MAIN}v')
                    if kind == 's':
                        index = value.text if value is not None and value.text else ''
                        text = shared[int(index)] if index.isascii() and index.isdigit() and len(index) <= 9 and int(index) < len(shared) else ''
                    elif kind == 'inlineStr':
                        text = _text(cell)
                    elif kind == 'b':
                        text = 'TRUE' if value is not None and value.text == '1' else 'FALSE'
                    else:
                        text = value.text if value is not None and value.text else ''
                        if re.fullmatch(r'-?\d+\.0+', text):
                            text = text.split('.')[0]  # 3.0 written by spreadsheet apps is the integer 3
                    values[column] = text.strip()
                width = max(values, default=-1) + 1
                rows.append([values.get(i, '') for i in range(width)])
            while rows and not any(rows[-1]):
                rows.pop()
            sheets[sheet.get('name')] = rows
        return sheets


def _escape(text):
    return (str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;'))


def write_xlsx(sheets):
    """Bytes of a workbook with one inline-string sheet per (name, rows) pair, in order."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        names = list(sheets)
        archive.writestr('[Content_Types].xml',
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            + ''.join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                      for i in range(1, len(names) + 1)) + '</Types>')
        archive.writestr('_rels/.rels',
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>')
        archive.writestr('xl/workbook.xml',
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
            + ''.join(f'<sheet name="{_escape(name)}" sheetId="{i}" r:id="rId{i}"/>' for i, name in enumerate(names, start=1))
            + '</sheets></workbook>')
        archive.writestr('xl/_rels/workbook.xml.rels',
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + ''.join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                      f'Target="worksheets/sheet{i}.xml"/>' for i in range(1, len(names) + 1)) + '</Relationships>')
        for i, name in enumerate(names, start=1):
            rows = ''.join(
                f'<row r="{r}">' + ''.join(f'<c r="{chr(65 + c)}{r}" t="inlineStr"><is><t>{_escape(value)}</t></is></c>'
                                           for c, value in enumerate(row) if value != '') + '</row>'
                for r, row in enumerate(sheets[name], start=1))
            archive.writestr(f'xl/worksheets/sheet{i}.xml',
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>{rows}</sheetData></worksheet>')
    return buffer.getvalue()
