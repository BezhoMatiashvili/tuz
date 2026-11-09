"""Serialize query results for download without additional dependencies."""
import csv
import io
import json
import re
import zipfile
from itertools import chain
from xml.sax.saxutils import escape


def text_value(value):
    if value is None:
        return ""
    if isinstance(value, bytes):
        return "\\x" + value.hex()
    if isinstance(value, dict | list):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def spreadsheet_value(value):
    value = text_value(value)
    # Prevent untrusted source values from becoming formulas when CSV is opened.
    formula = value.startswith(("\t", "\r")) or value.lstrip().startswith(("=", "+", "-", "@"))
    return "'" + value if formula else value


def xml_value(value):
    value = text_value(value)
    value = re.sub(r"[^\x09\x0a\x0d\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]", "", value)
    return escape(value)


def column_name(index):
    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def export_result(format_name, columns, rows):
    if format_name == "json":
        return json.dumps({"columns": columns, "rows": rows}, ensure_ascii=False,
                          default=text_value).encode("utf-8"), "application/json; charset=utf-8"
    if format_name == "csv":
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow([spreadsheet_value(value) for value in columns])
        writer.writerows([spreadsheet_value(value) for value in row] for row in rows)
        return ("\ufeff" + output.getvalue()).encode("utf-8"), "text/csv; charset=utf-8"
    if format_name != "xlsx":
        raise ValueError("Choose CSV, JSON, or Excel format.")
    if len(columns) > 16384 or len(rows) > 1048575:
        raise ValueError("The result exceeds Excel's worksheet limits; export CSV or JSON instead.")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
            'relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-'
            'officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.'
            'openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>'
        ))
        archive.writestr("_rels/.rels", (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            'relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>'
        ))
        archive.writestr("xl/workbook.xml", (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="Results" sheetId="1" r:id="rId1"/></sheets></workbook>'
        ))
        archive.writestr("xl/_rels/workbook.xml.rels", (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            'relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>'
        ))
        with archive.open("xl/worksheets/sheet1.xml", "w") as sheet:
            sheet.write(b'<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>')
            for row_number, row in enumerate(chain([columns], rows), 1):
                sheet.write(f'<row r="{row_number}">'.encode())
                for index, value in enumerate(row, 1):
                    cell = (f'<c r="{column_name(index)}{row_number}" t="inlineStr">'
                            f'<is><t xml:space="preserve">{xml_value(value)}</t></is></c>')
                    sheet.write(cell.encode("utf-8"))
                sheet.write(b"</row>")
            sheet.write(b"</sheetData></worksheet>")
    return output.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
