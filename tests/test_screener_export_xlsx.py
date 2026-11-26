import io
from xml.etree import ElementTree
from zipfile import ZipFile

from server import _build_screener_xlsx


def test_screener_xlsx_contains_only_the_approved_columns():
    rows = [{
        "symbol": "فملی",
        "market": "بورس",
        "shareholder_name": "شرکت سرمایه‌گذاری نمونه",
        "ownership_percent_calculated": 7.25,
        "shares_change": -125000,
        "disclosed_ownership_percent_sum": 48.5,
        "hidden_internal_id": "private value must not be exported",
    }]
    payload = _build_screener_xlsx(rows)
    with ZipFile(io.BytesIO(payload)) as archive:
        assert archive.testzip() is None
        sheet = ElementTree.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))

    ns = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    assert workbook.find(".//x:sheet", ns).attrib["name"] == "نتایج فیلتر"
    assert sheet.find(".//x:sheetView", ns).attrib["rightToLeft"] == "1"
    assert len(sheet.findall(".//x:sheetData/x:row", ns)) == 2
    texts = [node.text or "" for node in sheet.findall(".//x:is/x:t", ns)]
    assert "نماد" in texts
    assert "درصد مالکیت" in texts
    assert "فملی" in texts
    assert "شرکت سرمایه‌گذاری نمونه" in texts
    assert "private value must not be exported" not in texts


def test_screener_xlsx_treats_formula_like_text_as_a_literal_string():
    value = '=HYPERLINK("https://example.invalid")'
    payload = _build_screener_xlsx([{
        "symbol": value,
        "market": "فرابورس",
        "shareholder_name": None,
        "ownership_percent_calculated": 1.5,
        "shares_change": 10,
        "disclosed_ownership_percent_sum": 20,
    }])
    with ZipFile(io.BytesIO(payload)) as archive:
        sheet = ElementTree.fromstring(archive.read("xl/worksheets/sheet1.xml"))
    ns = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    assert sheet.findall(".//x:f", ns) == []
    assert value in [node.text or "" for node in sheet.findall(".//x:is/x:t", ns)]
