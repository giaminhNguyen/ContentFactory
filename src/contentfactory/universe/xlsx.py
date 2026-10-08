"""XLSX tối thiểu bằng stdlib (zipfile + xml): ghi sheet có tiêu đề in đậm, đóng băng hàng đầu, bộ lọc, độ rộng cột, danh sách chọn; đọc lại cả file do
Excel lưu (shared strings) lẫn file do module này ghi (inline strings). Lõi ContentFactory chỉ dùng stdlib (D-17) nên không kéo openpyxl vào."""
from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main", "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
      "pr": "http://schemas.openxmlformats.org/package/2006/relationships"}
_BAD = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


@dataclass
class Sheet:
    name: str
    header: list[str]
    rows: list[list] = field(default_factory=list)
    widths: list[int] | None = None
    lists: dict[int, list[str]] = field(default_factory=dict)        # chỉ số cột -> giá trị cho phép (data validation)


def _col(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _cell(ref: str, v, style: int) -> str:
    if v is None or v == "":
        return f'<c r="{ref}" s="{style}"/>'
    if isinstance(v, bool):
        return f'<c r="{ref}" s="{style}" t="b"><v>{int(v)}</v></c>'
    if isinstance(v, (int, float)):
        return f'<c r="{ref}" s="{style}"><v>{v}</v></c>'
    return f'<c r="{ref}" s="{style}" t="inlineStr"><is><t xml:space="preserve">{escape(_BAD.sub("", str(v)))}</t></is></c>'


def _sheet_xml(sh: Sheet) -> str:
    n = len(sh.header)
    widths = sh.widths or [22] * n
    cols = "".join(f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>' for i, w in enumerate(widths))
    rows = ['<row r="1">' + "".join(_cell(f"{_col(i)}1", h, 1) for i, h in enumerate(sh.header)) + "</row>"]
    for r, row in enumerate(sh.rows, start=2):
        rows.append(f'<row r="{r}">' + "".join(_cell(f"{_col(i)}{r}", v, 2) for i, v in enumerate(row)) + "</row>")
    last = max(len(sh.rows) + 1, 2)
    dv = ""
    if sh.lists:
        items = "".join(f'<dataValidation type="list" allowBlank="1" showErrorMessage="1" errorTitle="Giá trị không hợp lệ" error="Chọn một giá trị trong danh sách." '
                        f'sqref="{_col(c)}2:{_col(c)}{max(last, 1000)}"><formula1>"{escape(",".join(vals))}"</formula1></dataValidation>' for c, vals in sh.lists.items())
        dv = f'<dataValidations count="{len(sh.lists)}">{items}</dataValidations>'
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<worksheet xmlns="{NS["m"]}"><sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
            f'<sheetFormatPr defaultRowHeight="15"/><cols>{cols}</cols><sheetData>{"".join(rows)}</sheetData>'
            f'<autoFilter ref="A1:{_col(n - 1)}{last}"/>{dv}</worksheet>')


STYLES = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          f'<styleSheet xmlns="{NS["m"]}"><fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font></fonts>'
          '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
          '<fill><patternFill patternType="solid"><fgColor rgb="FF2F4F6F"/><bgColor indexed="64"/></patternFill></fill></fills>'
          '<borders count="2"><border><left/><right/><top/><bottom/><diagonal/></border><border><left style="thin"><color rgb="FFBFBFBF"/></left><right style="thin"><color rgb="FFBFBFBF"/></right>'
          '<top style="thin"><color rgb="FFBFBFBF"/></top><bottom style="thin"><color rgb="FFBFBFBF"/></bottom><diagonal/></border></borders>'
          '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
          '<cellXfs count="3"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
          '<xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment vertical="center" wrapText="1"/></xf>'
          '<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf></cellXfs></styleSheet>')


def write_workbook(sheets: list[Sheet]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                   '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                   + "".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                             for i in range(1, len(sheets) + 1)) + "</Types>")
        z.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr("xl/workbook.xml", f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="{NS["m"]}" xmlns:r="{NS["r"]}"><sheets>'
                   + "".join(f'<sheet name="{escape(s.name)}" sheetId="{i}" r:id="rId{i}"/>' for i, s in enumerate(sheets, 1)) + "</sheets></workbook>")
        z.writestr("xl/_rels/workbook.xml.rels", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   + "".join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>'
                             for i in range(1, len(sheets) + 1))
                   + f'<Relationship Id="rId{len(sheets) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
        z.writestr("xl/styles.xml", STYLES)
        for i, s in enumerate(sheets, 1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", _sheet_xml(s))
    return buf.getvalue()


class XlsxError(ValueError):
    pass


def _col_index(ref: str) -> int:
    letters = re.match(r"[A-Z]+", ref).group(0)
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n - 1


def read_workbook(data: bytes, max_bytes: int = 30 << 20) -> dict[str, list[list[str]]]:
    """{tên sheet: [hàng [ô dạng chuỗi]]}. Từ chối file không phải xlsx/quá lớn/zip bom (kiểm kích thước giải nén)."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise XlsxError("File không phải Excel (.xlsx) hợp lệ.") from None
    if sum(i.file_size for i in z.infolist()) > max_bytes:
        raise XlsxError("File Excel quá lớn sau khi giải nén.")
    try:
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = {r.get("Id"): r.get("Target") for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))}
    except (KeyError, ET.ParseError):
        raise XlsxError("File Excel thiếu workbook.xml (không đúng định dạng).") from None
    shared: list[str] = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", NS):
            shared.append("".join(t.text or "" for t in si.iter(f"{{{NS['m']}}}t")))
    out: dict[str, list[list[str]]] = {}
    for sh in wb.find("m:sheets", NS):
        target = rels.get(sh.get(f"{{{NS['r']}}}id"), "")
        path = "xl/" + target.lstrip("/").removeprefix("xl/")
        if path not in z.namelist():
            continue
        rows: list[list[str]] = []
        for row in ET.fromstring(z.read(path)).iter(f"{{{NS['m']}}}row"):
            vals: list[str] = []
            for c in row.findall("m:c", NS):
                idx = _col_index(c.get("r", "A1"))
                vals.extend([""] * (idx - len(vals)))
                t = c.get("t")
                if t == "inlineStr":
                    v = "".join(x.text or "" for x in c.iter(f"{{{NS['m']}}}t"))
                else:
                    ve = c.find("m:v", NS)
                    v = ve.text or "" if ve is not None else ""
                    if t == "s" and v != "":
                        v = shared[int(v)]
                    elif t == "b":
                        v = "TRUE" if v == "1" else "FALSE"
                    elif re.fullmatch(r"-?\d+\.0", v):
                        v = v[:-2]
                vals.append(v)
            rows.append(vals)
        out[sh.get("name")] = rows
    return out
