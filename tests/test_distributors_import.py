"""Tests for callingbot.services.distributors: ARN/EUIN normalisation and CSV import."""

from __future__ import annotations

import io
from datetime import date
from pathlib import Path

import pytest
from conftest import ROOT
from sqlalchemy import select

from callingbot.compliance import add_to_dnc
from callingbot.models import Distributor, DNCEntry, EmpanelmentStatus
from callingbot.services.distributors import (
    ImportReport,
    import_distributors_csv,
    normalize_arn,
    normalize_euin,
    parse_date,
    parse_language,
)


def _csv(text: str) -> io.StringIO:
    return io.StringIO(text.lstrip("\n"))


def _by_arn(session, arn: str) -> Distributor | None:
    return session.scalar(select(Distributor).where(Distributor.arn == arn))


# --------------------------------------------------------------------------------------------
# ARN / EUIN
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("ARN-12345", "ARN-12345"),
        ("arn 12345", "ARN-12345"),
        ("ARN12345", "ARN-12345"),
        ("12345", "ARN-12345"),
        ("  ARN-12345  ", "ARN-12345"),
        ("ARN-0012345", "ARN-0012345"),  # digits kept as given
        ("1", "ARN-1"),
        ("ARN-12345678", None),  # more than 7 digits
        ("ARN-12A45", None),
        ("XRN-12345", None),
        ("ARN-", None),
        ("", None),
        ("   ", None),
        (None, None),
    ],
)
def test_normalize_arn(raw, expected):
    assert normalize_arn(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("E123456", "E123456"),
        ("e123456", "E123456"),
        (" E654321 ", "E654321"),
        ("E12345", None),
        ("E1234567", None),
        ("X123456", None),
        ("123456", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_euin(raw, expected):
    assert normalize_euin(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("31-Mar-2027", date(2027, 3, 31)),
        ("31-MAR-2027", date(2027, 3, 31)),
        ("2027-03-31", date(2027, 3, 31)),
        ("31/03/2027", date(2027, 3, 31)),
        ("31-03-2027", date(2027, 3, 31)),
        ("31/13/2027", None),
        ("next year", None),
        ("", None),
    ],
)
def test_parse_date(raw, expected):
    assert parse_date(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Hindi", "hi-IN"),
        ("hi", "hi-IN"),
        ("hi-IN", "hi-IN"),
        ("English", "en-IN"),
        ("en", "en-IN"),
        (" ENGLISH ", "en-IN"),
        ("Bengali", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_language(raw, expected):
    assert parse_language(raw) == expected


# --------------------------------------------------------------------------------------------
# CSV import
# --------------------------------------------------------------------------------------------


def test_import_amfi_style_headers(session):
    report = import_distributors_csv(
        session,
        _csv(
            """
ARN,ARN Holder's Name,Firm Name,Email,City,State,Pin,Telephone (R),Telephone (O),ARN Valid Till,EUIN,Preferred Language
ARN-111,Ravi Kumar,Kumar Wealth,RAVI@Example.com,Pune,Maharashtra,411001,98111 11112,098111-11111,31-Mar-2027,e111111,Hindi
"""
        ),
        source="amfi_2026_10",
    )
    assert report == ImportReport(created=1, distributor_ids=report.distributor_ids)
    d = _by_arn(session, "ARN-111")
    assert d.name == "Ravi Kumar"
    assert d.firm_name == "Kumar Wealth"
    # Telephone (O) is listed before Telephone (R) in the alias order, whatever the column order.
    assert d.phone == "+919811111111"
    assert d.alt_phone == "+919811111112"
    assert d.email == "ravi@example.com"
    assert (d.city, d.state, d.pincode) == ("Pune", "Maharashtra", "411001")
    assert d.euin == "E111111"
    assert d.arn_valid_till == date(2027, 3, 31)
    assert d.preferred_language == "hi-IN"
    assert d.source == "amfi_2026_10"
    assert d.status == EmpanelmentStatus.NEW and d.do_not_call is False
    assert report.distributor_ids == [d.id]


def test_import_crm_style_aliases_and_header_normalisation(session):
    report = import_distributors_csv(
        session,
        _csv(
            """
AMFI Registration Number , DISTRIBUTOR NAME,Company,Mobile No.,Alternate Mobile,E-Mail,PIN CODE,Arn Expiry,language
22222,Sunita Rao,Rao Investments,9822222222,9822222223,sunita@example.com,560 001,2028-01-31,en
"""
        ),
        source="crm",
    )
    assert report.created == 1 and not report.errors
    d = _by_arn(session, "ARN-22222")
    assert d.name == "Sunita Rao"
    assert d.firm_name == "Rao Investments"
    assert d.phone == "+919822222222"
    assert d.alt_phone == "+919822222223"
    assert d.email == "sunita@example.com"
    assert d.pincode == "560001"
    assert d.arn_valid_till == date(2028, 1, 31)
    assert d.preferred_language == "en-IN"


def test_import_handles_bom_and_semicolon_from_path(session, tmp_path: Path):
    path = tmp_path / "list.csv"
    content = "arn;name;mobile;city\nARN-333;Asha Iyer;9833333333;Chennai, TN\n"
    path.write_bytes(b"\xef\xbb\xbf" + content.encode("utf-8"))
    report = import_distributors_csv(session, path, source="excel")
    assert report.created == 1 and not report.errors
    d = _by_arn(session, "ARN-333")
    assert d.phone == "+919833333333"
    assert d.city == "Chennai, TN"  # the comma is data, not a delimiter


def test_import_accepts_str_path_and_text_handle_with_bom(session, tmp_path: Path):
    path = tmp_path / "list.csv"
    path.write_text("ARN,Name,Phone\nARN-444,A One,9844444444\n", encoding="utf-8")
    assert import_distributors_csv(session, str(path), source="s").created == 1
    handle = io.StringIO("﻿ARN,Name,Phone\nARN-445,A Two,9844444445\n")
    assert import_distributors_csv(session, handle, source="s").created == 1
    assert _by_arn(session, "ARN-445") is not None


def test_import_reports_invalid_rows_with_spreadsheet_row_numbers(session):
    report = import_distributors_csv(
        session,
        _csv(
            """
ARN,Name,Phone,Telephone (O)
ARN-501,Valid One,9850000001,
,No Arn,9850000002,
ARN-ABC,Bad Arn,9850000003,
ARN-504,,9850000004,
ARN-505,Landline Only,,040 2345 6789
ARN-506,Valid Two,,+91 98500 00006
"""
        ),
        source="t",
    )
    assert report.created == 3  # a row without an ARN is valid (CRM lists often lack it)
    rows = dict(report.errors)
    assert set(rows) == {4, 5, 6}  # header is row 1, first data row is row 2
    assert "invalid ARN" in rows[4]
    assert "missing name" in rows[5]
    assert "no valid Indian mobile" in rows[6]
    assert "2345" not in rows[6]  # the raw number is not echoed into the report (PII)
    assert _by_arn(session, "ARN-506").phone == "+919850000006"


def test_import_duplicate_arn_in_file_rejects_later_row(session):
    report = import_distributors_csv(
        session,
        _csv(
            """
ARN,Name,Phone
ARN-601,First,9860000001
ARN 601,Second,9860000002
"""
        ),
        source="t",
    )
    assert report.created == 1
    assert report.errors == [(3, "duplicate ARN ARN-601 (first seen in row 2)")]
    assert _by_arn(session, "ARN-601").name == "First"


def test_import_skips_blank_rows_and_placeholder_values(session):
    report = import_distributors_csv(
        session,
        _csv(
            """
ARN,Name,Firm Name,Phone,EUIN,City
ARN-701,Neha,NA,9870000001,N/A,-

,,,,,
"""
        ),
        source="t",
    )
    assert report.created == 1 and not report.errors and not report.warnings
    d = _by_arn(session, "ARN-701")
    assert d.firm_name is None and d.euin is None and d.city is None


def test_import_warns_about_dropped_values(session):
    report = import_distributors_csv(
        session,
        _csv(
            """
ARN,Name,Phone,EUIN,ARN Valid Till,Language,Email,Pin
ARN-801,Kiran,9880000001,E12,someday,Bengali,not-an-email,12
"""
        ),
        source="t",
    )
    assert report.created == 1
    messages = " | ".join(m for _, m in report.warnings)
    for fragment in ("EUIN", "validity date", "language", "email", "PIN"):
        assert fragment in messages
    d = _by_arn(session, "ARN-801")
    assert (d.euin, d.arn_valid_till, d.preferred_language, d.email, d.pincode) == (
        None,
        None,
        None,
        None,
        None,
    )


def test_import_updates_existing_but_never_status_dnc_or_notes(session, make_distributor):
    existing = make_distributor(
        arn="ARN-901",
        name="Old Name",
        firm_name="Old Firm",
        phone="+919890000001",
        city="Pune",
        status=EmpanelmentStatus.LINK_SENT,
        notes="RM spoke on 1 Oct",
        source="first_list",
    )
    report = import_distributors_csv(
        session,
        _csv(
            """
ARN,Name,Firm Name,Phone,City,Email
ARN-901,New Name,,9890000009,,new@example.com
"""
        ),
        source="second_list",
    )
    assert (report.created, report.updated, report.skipped) == (0, 1, 0)
    assert report.distributor_ids == [existing.id]
    assert existing.name == "New Name"
    assert existing.phone == "+919890000009"
    assert existing.email == "new@example.com"
    assert existing.firm_name == "Old Firm"  # empty cells never blank out data
    assert existing.city == "Pune"
    assert existing.status == EmpanelmentStatus.LINK_SENT
    assert existing.notes == "RM spoke on 1 Oct"
    assert existing.do_not_call is False
    assert existing.source == "second_list"


def test_import_unchanged_existing_row_counts_as_skipped(session, make_distributor):
    make_distributor(arn="ARN-902", name="Same", phone="+919890000002")
    report = import_distributors_csv(session, _csv("ARN,Name,Phone\nARN-902,Same,9890000002\n"), source="t")
    assert (report.created, report.updated, report.skipped) == (0, 0, 1)


def test_import_no_update_skips_existing(session, make_distributor):
    existing = make_distributor(arn="ARN-903", name="Keep Me", phone="+919890000003")
    report = import_distributors_csv(
        session,
        _csv("ARN,Name,Phone\nARN-903,Changed,9890000004\nARN-904,Brand New,9890000005\n"),
        source="t",
        update_existing=False,
    )
    assert (report.created, report.updated, report.skipped) == (1, 0, 1)
    assert existing.name == "Keep Me" and existing.phone == "+919890000003"
    assert existing.id in report.distributor_ids


def test_import_dnc_phone_is_imported_but_marked_do_not_call(session):
    add_to_dnc(session, "98910 00001", reason="opted out on a previous campaign", source="call_opt_out")
    report = import_distributors_csv(
        session,
        _csv("ARN,Name,Phone\nARN-1001,Opted Out,+91 98910 00001\nARN-1002,Fine,9891000002\n"),
        source="t",
    )
    assert report.created == 2
    assert report.dnc_marked == 1
    assert any(row == 2 and "DNC" in msg for row, msg in report.warnings)
    opted = _by_arn(session, "ARN-1001")
    assert opted.do_not_call is True
    assert opted.status == EmpanelmentStatus.DO_NOT_CALL
    assert opted.dnc_reason
    fine = _by_arn(session, "ARN-1002")
    assert fine.do_not_call is False and fine.status == EmpanelmentStatus.NEW


def test_import_dnc_alternate_phone_also_marks_do_not_call(session):
    session.add(DNCEntry(phone="+919892000002", reason="manual", source="manual"))
    session.flush()
    report = import_distributors_csv(
        session,
        _csv("ARN,Name,Mobile,Alternate Phone\nARN-1101,Two Phones,9892000001,9892000002\n"),
        source="t",
    )
    assert report.dnc_marked == 1
    assert _by_arn(session, "ARN-1101").do_not_call is True


def test_import_sample_file(session):
    report = import_distributors_csv(session, ROOT / "data" / "sample_distributors.csv", source="sample")
    assert report.created == 10
    errors = dict(report.errors)
    assert "no valid Indian mobile" in errors[6]  # ARN-999905 has only a landline
    assert "duplicate ARN ARN-999903" in errors[13]
    assert _by_arn(session, "ARN-999909") is not None  # bare digits accepted
    assert _by_arn(session, "ARN-999910").preferred_language is None  # Bengali not enabled
    assert _by_arn(session, "ARN-999906").arn_valid_till == date(2025, 3, 31)  # expired, still imported
    d3 = _by_arn(session, "ARN-999903")
    assert (d3.phone, d3.alt_phone, d3.euin) == ("+919000000003", "+919000000013", "E999903")


# --- ARN-less lists and Excel workbooks ---------------------------------------------------------


def _xlsx(rows: list[list[object]], *, header_row: int = 1) -> bytes:
    """Minimal .xlsx (shared strings for text, numbers as numbers) - like the AMC's dummy data file."""
    import zipfile

    strings: list[str] = []

    def cell(ref: str, value: object) -> str:
        if isinstance(value, int | float):
            return f'<c r="{ref}"><v>{value}</v></c>'
        strings.append(str(value))
        return f'<c r="{ref}" t="s"><v>{len(strings) - 1}</v></c>'

    sheet_rows = []
    for offset, values in enumerate(rows):
        r = header_row + offset
        cells = "".join(cell(f"{chr(65 + i)}{r}", v) for i, v in enumerate(values) if v is not None)
        sheet_rows.append(f'<row r="{r}">{cells}</row>')
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    rel_ns = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(
            "xl/workbook.xml",
            f'<workbook {ns} {rel_ns}><sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>',
        )
        z.writestr(
            "xl/_rels/workbook.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
        )
        z.writestr(
            "xl/worksheets/sheet1.xml",
            f"<worksheet {ns}><sheetData>{''.join(sheet_rows)}</sheetData></worksheet>",
        )
        sst = "".join(f"<si><t>{s}</t></si>" for s in strings)
        z.writestr("xl/sharedStrings.xml", f"<sst {ns}>{sst}</sst>")
    return buf.getvalue()


def test_import_xlsx_without_arn_header_on_row_two(session, tmp_path):
    data = _xlsx(
        [
            ["Name", "Mobile No.", "Location", "Email ID"],
            ["Test Person", 9811122233, "Mumbai", "test.person@example.com"],
            ["Other Person", 9811122244.0, "Pune", "other@example.com"],
        ],
        header_row=2,  # row 1 is empty, as in the AMC's file
    )
    path = tmp_path / "list.xlsx"
    path.write_bytes(data)
    report = import_distributors_csv(session, path, source="poc")
    assert (report.created, report.errors) == (2, [])
    people = {d.name: d for d in session.scalars(select(Distributor))}
    assert people["Test Person"].phone == "+919811122233" and people["Test Person"].arn is None
    assert people["Test Person"].city == "Mumbai" and people["Test Person"].email == "test.person@example.com"
    assert people["Other Person"].phone == "+919811122244"


def test_import_xlsx_from_binary_handle(session):
    data = _xlsx([["ARN", "Name", "Mobile"], ["ARN-7001", "Handle Person", 9822233344]])
    report = import_distributors_csv(session, io.BytesIO(data), source="upload")
    assert report.created == 1 and _by_arn(session, "ARN-7001").phone == "+919822233344"


def test_import_without_arn_matches_existing_by_phone(session):
    first = import_distributors_csv(session, _csv("Name,Mobile,City\nNo Arn,9833300011,Mumbai\n"), source="a")
    again = import_distributors_csv(session, _csv("Name,Mobile,City\nNo Arn,9833300011,Thane\n"), source="b")
    assert first.created == 1 and again.updated == 1
    (d,) = session.scalars(select(Distributor)).all()
    assert d.city == "Thane"
    # A later list that carries the ARN attaches it to the same person instead of duplicating them.
    with_arn = import_distributors_csv(
        session, _csv("ARN,Name,Mobile\nARN-8001,No Arn,9833300011\n"), source="c"
    )
    assert with_arn.updated == 1 and session.scalars(select(Distributor)).one().arn == "ARN-8001"


def test_import_duplicate_mobile_without_arn_rejects_later_row(session):
    report = import_distributors_csv(
        session, _csv("Name,Mobile\nOne,9844400011\nTwo,98444 00011\n"), source="t"
    )
    assert report.created == 1
    assert "duplicate mobile" in dict(report.errors)[3]


def test_parse_date_accepts_excel_serial():
    from callingbot.services.distributors import parse_date

    assert parse_date("46477") == date(2027, 3, 31)
