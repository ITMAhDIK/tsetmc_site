"""Analytics application service for screener queries and safe XLSX output."""
from __future__ import annotations

import io
import math
from datetime import datetime
from decimal import Decimal
from zipfile import ZIP_DEFLATED, ZipFile
from xml.sax.saxutils import escape as xml_escape

from site_api.errors import ApiError


def _xlsx_column_name(index):
    """Return an Excel column name for a 1-based column index."""
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


SCREENER_PUBLIC_COLUMNS = (
    "symbol", "market", "shareholder_name",
    "ownership_percent_calculated", "shares_change",
    "disclosed_ownership_percent_sum",
)


def _screener_public_row(row):
    """Project a query result onto the fields explicitly approved for clients."""
    return {key: row[key] for key in SCREENER_PUBLIC_COLUMNS}


def _build_screener_xlsx(rows):
    """Build an XLSX export with a strict allow-list of public screener fields."""
    columns = (
        ("symbol", "نماد"),
        ("market", "بازار"),
        ("shareholder_name", "سهام‌دار"),
        ("ownership_percent_calculated", "درصد مالکیت"),
        ("shares_change", "تغییر تعداد سهام"),
        ("disclosed_ownership_percent_sum", "تمرکز مالکیت عمده٪"),
    )

    def cell_xml(cell_ref, value):
        if value is None:
            return '<c r="%s" t="inlineStr"><is><t></t></is></c>' % cell_ref
        if isinstance(value, bool):
            return '<c r="%s" t="b"><v>%d</v></c>' % (cell_ref, int(value))
        if isinstance(value, (int, float, Decimal)):
            try:
                numeric = float(value)
            except (TypeError, ValueError, OverflowError):
                numeric = float("nan")
            if math.isfinite(numeric):
                return '<c r="%s"><v>%s</v></c>' % (cell_ref, str(value))
        text_value = xml_escape(str(value))
        return ('<c r="%s" t="inlineStr"><is><t xml:space="preserve">%s</t>'
                '</is></c>') % (cell_ref, text_value)

    rows_xml = []
    header = "".join(
        cell_xml("%s1" % _xlsx_column_name(column_index), title)
        for column_index, (_, title) in enumerate(columns, start=1)
    )
    rows_xml.append('<row r="1">%s</row>' % header)

    for row_index, record in enumerate(rows, start=2):
        cells = "".join(
            cell_xml("%s%d" % (_xlsx_column_name(column_index), row_index),
                     record[key])
            for column_index, (key, _) in enumerate(columns, start=1)
        )
        rows_xml.append('<row r="%d">%s</row>' % (row_index, cells))

    last_row = max(1, len(rows) + 1)
    worksheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetViews><sheetView workbookViewId="0" rightToLeft="1">'
        '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        '</sheetView></sheetViews>'
        '<sheetFormatPr defaultRowHeight="20"/>'
        '<cols>'
        '<col min="1" max="1" width="16" customWidth="1"/>'
        '<col min="2" max="2" width="18" customWidth="1"/>'
        '<col min="3" max="3" width="30" customWidth="1"/>'
        '<col min="4" max="6" width="22" customWidth="1"/>'
        '</cols>'
        '<sheetData>%s</sheetData>'
        '<autoFilter ref="A1:F%d"/>'
        '</worksheet>'
    ) % ("".join(rows_xml), last_row)

    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="نتایج فیلتر" sheetId="1" r:id="rId1"/></sheets>'
        '</workbook>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '</Types>'
    )
    root_relationships = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        '</Relationships>'
    )
    workbook_relationships = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '</Relationships>'
    )

    output = io.BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_relationships)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_relationships)
        archive.writestr("xl/worksheets/sheet1.xml", worksheet)
    return output.getvalue()


class ScreenerService:
    """Validated screener query and export operations over materialized Sidecar data."""

    _SCREEN_FIELDS = {
        "ownership_percent_calculated": ("h.ownership_percent_calc", "num"),
        "disclosed_shareholder_count": ("w.resolved_shareholder_count", "num"),
        "disclosed_ownership_percent_sum": ("w.resolved_ownership_percent_sum", "num"),
        "shares_change": ("h.shares_change", "num"),
        "change_type": ("h.change_type", "str"),
        "entry_status": ("h.entry_status", "str"),
        "shareholder_type": ("n.shareholder_type", "str"),
        "identity_status": ("h.identity_status", "str"),
        "data_completeness": ("w.data_completeness", "str"),
        "market": ("w.market", "str"),
        "has_corporate_action": ("COALESCE(ca.n, 0)", "num"),
        "date_gregorian": ("h.latest_date", "date"),
    }
    _SCREEN_OPS = frozenset({">=", "<=", "=", ">", "<"})

    def __init__(self, *, sidecar, require_ready, norm_date):
        self.sidecar = sidecar
        self.require_ready = require_ready
        self._norm_date = norm_date

    def _screener_query(self, body, limit):
        logic = str(body.get("logic", "AND")).upper()
        if logic not in ("AND", "OR"):
            logic = "AND"
        conds = body.get("conditions") or []
        sort_by = str(body.get("sort_by", "ownership_percent_calculated"))
        sort_dir = str(body.get("sort_dir", "DESC")).upper()
        if sort_dir not in ("ASC", "DESC"):
            sort_dir = "DESC"
        where_parts = []
        args = []
        for c in conds:
            field = self._SCREEN_FIELDS.get(str(c.get("field", "")))
            op = str(c.get("op", "="))
            if field is None or op not in self._SCREEN_OPS:
                continue
            col, kind = field
            val = c.get("value")
            if kind == "num":
                try:
                    val = float(val)
                except (TypeError, ValueError):
                    continue
            elif kind == "date":
                # Stored sidecar dates are canonical dashed Gregorian.
                val = self._norm_date(val)
                try:
                    datetime.strptime(val, "%Y-%m-%d")
                except ValueError:
                    continue
            else:
                val = str(val)
            where_parts.append("%s %s ?" % (col, op))
            args.append(val)
        clause = ""
        if where_parts:
            clause = "WHERE " + (" %s " % logic).join(where_parts)
        sort_col, _ = self._SCREEN_FIELDS.get(
            sort_by, ("h.ownership_percent_calc", "num"))
        sql = (
            "SELECT h.symbol AS symbol, w.market AS market, "
            "  n.name AS shareholder_name, "
            "  h.ownership_percent_calc AS ownership_percent_calculated, "
            "  h.shares_change AS shares_change, "
            "  w.resolved_ownership_percent_sum "
            "    AS disclosed_ownership_percent_sum "
            "FROM site_holder_latest h "
            "JOIN site_watch w ON w.ins_code = h.ins_code "
            "LEFT JOIN site_shareholder_names n "
            "  ON n.id_scheme = h.id_scheme "
            " AND n.shareholder_id = h.shareholder_id "
            "LEFT JOIN site_corp_action_counts ca ON ca.ins_code = h.ins_code "
            + clause +
            " ORDER BY %s %s LIMIT ?" % (sort_col, sort_dir))
        return sql, args, limit

    def screener_query(self, body):
        limit = max(1, min(500, int(body.get("limit", 100) or 100)))
        sql, args, _ = self._screener_query(body, limit)
        sc = self.sidecar()
        try:
            self.require_ready(sc)
            rows = sc.execute(sql, args + [limit]).fetchall()
            return {"rows": [_screener_public_row(r) for r in rows]}
        finally:
            sc.close()

    def screener_export(self, body):
        """Export the selected filter AST as XLSX, capped and column-allow-listed."""
        limit = 5000
        sql, args, _ = self._screener_query(body, limit)
        sc = self.sidecar()
        try:
            self.require_ready(sc)
            rows = sc.execute(sql, args + [limit]).fetchall()
        finally:
            sc.close()
        safe_rows = [_screener_public_row(row) for row in rows]
        return _build_screener_xlsx(safe_rows)