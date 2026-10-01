"""Distributor list import (CSV or Excel .xlsx) and ARN / EUIN validation.

Lists come from AMFI's "ARN holders" export or from the AMC's CRM, so the importer accepts the
common column spellings of both (see ``_FIELD_ALIASES``), a UTF-8 byte-order mark (Excel),
``,`` / ``;`` / tab delimiters, ``.xlsx`` workbooks (first sheet, read with the standard library)
and a header row that is not the first row (title or blank rows above it are skipped).

The ARN is optional - CRM lists often lack it. Rows are matched to existing distributors by ARN
when present, otherwise by mobile number.

What the importer deliberately does *not* do:

* It never changes ``status``, ``do_not_call`` or ``notes`` of an existing distributor - those
  record what happened on calls and opt-outs, which a re-imported spreadsheet must not undo.
* It never drops an opt-out: a row whose phone is on the internal DNC list is imported (so the
  ARN is known) but marked do-not-call, which keeps it out of every campaign (TRAI TCCCPR).
* It keeps only the columns needed to call and empanel a distributor (DPDP data minimisation);
  unknown columns are ignored.

Row numbers in :class:`ImportReport` are **spreadsheet row numbers** - what an operator sees when
opening the file in Excel (with the header on row 1, the first data row is row 2).
"""

from __future__ import annotations

import csv
import io
import logging
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import BinaryIO, TextIO
from xml.etree import ElementTree

from sqlalchemy import select
from sqlalchemy.orm import Session

from callingbot import compliance, funnel
from callingbot.models import Distributor, EmpanelmentStatus
from callingbot.phone import mask_phone, normalize_indian_mobile

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------------------------
# ARN / EUIN
# ---------------------------------------------------------------------------------------------

# AMFI ARNs are numeric; lists write them as "ARN-12345", "ARN 12345", "ARN12345" or just the
# digits. Digits are kept exactly as given (leading zeros included) because the ARN is an
# identifier, not a number.
_ARN_RE = re.compile(r"^(?:ARN[\s-]*)?(\d{1,7})$", re.IGNORECASE)
_EUIN_RE = re.compile(r"^E\d{6}$", re.IGNORECASE)


def normalize_arn(raw: str | None) -> str | None:
    """``"arn 12345"`` / ``"ARN12345"`` / ``"12345"`` / ``"ARN-12345"`` -> ``"ARN-12345"``; else None."""
    if raw is None:
        return None
    m = _ARN_RE.match(str(raw).strip())
    return f"ARN-{m.group(1)}" if m else None


def normalize_euin(raw: str | None) -> str | None:
    """``"e123456"`` -> ``"E123456"``; anything that is not ``E`` + 6 digits -> None."""
    if raw is None:
        return None
    value = str(raw).strip()
    return value.upper() if _EUIN_RE.match(value) else None


# ---------------------------------------------------------------------------------------------
# CSV import
# ---------------------------------------------------------------------------------------------


@dataclass
class ImportReport:
    """Result of :func:`import_distributors_csv`.

    * ``created`` / ``updated`` - rows that inserted a distributor / changed an existing one.
    * ``skipped`` - valid rows that were not applied: the ARN already exists and either
      ``update_existing`` is False or the row carried no new information.
    * ``errors`` - ``(row number, reason)`` for invalid rows, which are not imported.
    * ``warnings`` - ``(row number, message)`` for rows that were imported but had a value that
      was dropped (bad EUIN, unparseable date, ...) or were marked do-not-call.
    * ``dnc_marked`` - imported rows marked do-not-call because a phone is on the DNC list.
    * ``distributor_ids`` - ids of every distributor matched by a valid row (created, updated or
      skipped), in file order, so callers can add exactly this list to a campaign.
    """

    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: list[tuple[int, str]] = field(default_factory=list)
    warnings: list[tuple[int, str]] = field(default_factory=list)
    dnc_marked: int = 0
    distributor_ids: list[int] = field(default_factory=list)

    @property
    def total_rows(self) -> int:
        return self.created + self.updated + self.skipped + len(self.errors)


def _header_key(header: str | None) -> str:
    # Case-insensitive; ignores spaces, punctuation, apostrophes and a stray BOM, so
    # "ARN Holder's Name" == "arn holders name" == "ARN_HOLDER_NAME"-ish variants.
    return re.sub(r"[^a-z0-9]", "", (header or "").lower())


# Field -> accepted header spellings, in priority order (the first non-empty column wins).
_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "arn": ("arn", "arn code", "arn no", "arn number", "amfi registration number"),
    "name": (
        "name",
        "full name",
        "arn holder's name",
        "arn holder name",
        "distributor name",
        "contact person",
    ),
    "firm_name": ("firm", "firm name", "company", "entity name"),
    # Every valid mobile across these columns is collected in this order: the first becomes
    # ``phone`` and the next distinct one ``alt_phone``. Office before residence.
    "phone": (
        "phone",
        "mobile",
        "mobile no",
        "mobile number",
        "contact number",
        "telephone (o)",
        "telephone (r)",
    ),
    "alt_phone": ("alt phone", "alternate phone", "alternate mobile"),
    "email": ("email", "email id", "e-mail"),
    "city": ("city", "location"),
    "state": ("state",),
    "pincode": ("pin", "pincode", "pin code"),
    "euin": ("euin",),
    "arn_valid_till": ("arn valid till", "valid till", "arn expiry"),
    "preferred_language": ("language", "preferred language"),
}

_DATE_FORMATS = ("%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d.%m.%Y")

_LANGUAGES = {
    "hindi": "hi-IN",
    "hi": "hi-IN",
    "hi-in": "hi-IN",
    "hi_in": "hi-IN",
    "english": "en-IN",
    "en": "en-IN",
    "en-in": "en-IN",
    "en_in": "en-IN",
}

# Spreadsheet placeholders for "no value". Treated as empty so "NA" never becomes a firm name.
_EMPTY_MARKERS = frozenset({"", "-", "--", "na", "n/a", "nil", "null", "none", "not available"})

# Column sizes in models.Distributor; PostgreSQL rejects longer values instead of truncating.
_MAX_LEN = {"name": 200, "firm_name": 200, "email": 200, "city": 100, "state": 100, "source": 100}

# Fields copied from a row onto a Distributor. status / do_not_call / notes are never imported.
_DATA_FIELDS = (
    "name",
    "firm_name",
    "phone",
    "alt_phone",
    "email",
    "city",
    "state",
    "pincode",
    "euin",
    "arn_valid_till",
    "preferred_language",
)

_DELIMITERS = (",", ";", "\t")


def parse_language(raw: str | None) -> str | None:
    """``"Hindi"`` / ``"hi"`` / ``"hi-IN"`` -> ``"hi-IN"``; ``"English"`` / ``"en"`` -> ``"en-IN"``; else None."""
    return _LANGUAGES.get((raw or "").strip().lower())


_EXCEL_EPOCH = date(1899, 12, 30)
_EXCEL_SERIAL_RE = re.compile(r"^\d{5}(?:\.0+)?$")


def parse_date(raw: str | None) -> date | None:
    """Parse the date formats seen in AMFI / CRM exports (``31-Mar-2027``, ``2027-03-31``, ``31/03/2027``)
    and Excel date serials (``46477``) from .xlsx cells."""
    value = (raw or "").strip()
    if not value:
        return None
    if _EXCEL_SERIAL_RE.match(value):
        return _EXCEL_EPOCH + timedelta(days=int(float(value)))
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())  # trims and collapses internal whitespace / newlines
    return None if text.lower() in _EMPTY_MARKERS else text


def _truncate(field_name: str, value: str | None) -> str | None:
    limit = _MAX_LEN.get(field_name)
    return value[:limit] if value and limit else value


_XLSX_MAGIC = b"PK\x03\x04"  # .xlsx files are zip archives
_XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_XLSX_REL_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
_XLSX_MAX_PART_BYTES = 50 * 1024 * 1024  # refuse zip bombs disguised as spreadsheets


def _read_source(source_file: TextIO | BinaryIO | Path | str) -> str | bytes:
    """File content: ``bytes`` for an .xlsx workbook, text otherwise. A ``str`` is a path."""
    if isinstance(source_file, str | Path):
        data = Path(source_file).read_bytes()
    else:
        data = source_file.read()
    if isinstance(data, bytes):
        if data.startswith(_XLSX_MAGIC):
            return data
        data = data.decode("utf-8-sig")  # utf-8-sig strips the BOM Excel writes
    return data.lstrip("\ufeff")


def _xlsx_number(value: str) -> str:
    # Excel stores mobiles as numbers: "9769116626", sometimes "9769116626.0" or "9.769116626E9".
    try:
        number = Decimal(value)
    except InvalidOperation:
        return value
    return str(int(number)) if number == number.to_integral_value() else value


def _xlsx_column(ref: str) -> int:
    index = 0
    for ch in ref:
        if not ch.isalpha():
            break
        index = index * 26 + (ord(ch.upper()) - 64)
    return index - 1


def _xlsx_rows(data: bytes) -> list[tuple[int, list[str]]]:
    """``(spreadsheet row number, cell values)`` of the workbook's first sheet."""
    with zipfile.ZipFile(io.BytesIO(data)) as book:

        def part(name: str) -> ElementTree.Element:
            if book.getinfo(name).file_size > _XLSX_MAX_PART_BYTES:
                raise ValueError(f"{name} is too large to import")
            return ElementTree.fromstring(book.read(name))

        shared: list[str] = []
        if "xl/sharedStrings.xml" in book.namelist():
            shared = [
                "".join(t.text or "" for t in si.iter(f"{_XLSX_NS}t")) for si in part("xl/sharedStrings.xml")
            ]
        first_sheet = part("xl/workbook.xml").find(f"{_XLSX_NS}sheets/{_XLSX_NS}sheet")
        if first_sheet is None:
            raise ValueError("the workbook has no sheets")
        rels = part("xl/_rels/workbook.xml.rels")
        target = next(r.get("Target", "") for r in rels if r.get("Id") == first_sheet.get(_XLSX_REL_ID))
        sheet = part(target.lstrip("/") if target.startswith("/") else f"xl/{target}")

        rows: list[tuple[int, list[str]]] = []
        for row in sheet.iter(f"{_XLSX_NS}row"):
            values: dict[int, str] = {}
            for cell in row.iter(f"{_XLSX_NS}c"):
                kind, raw = cell.get("t"), cell.find(f"{_XLSX_NS}v")
                if kind == "s" and raw is not None:
                    value = shared[int(raw.text or 0)]
                elif kind == "inlineStr":
                    value = "".join(t.text or "" for t in cell.iter(f"{_XLSX_NS}t"))
                elif raw is None or raw.text is None:
                    continue
                elif kind in (None, "n"):
                    value = _xlsx_number(raw.text)
                else:
                    value = raw.text
                values[_xlsx_column(cell.get("r", "A"))] = value
            if values:
                width = max(values) + 1
                rows.append((int(row.get("r", len(rows) + 1)), [values.get(i, "") for i in range(width)]))
        return rows


def _csv_rows(text: str) -> list[tuple[int, list[str]]]:
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=_detect_delimiter(text))
    return [(reader.line_num, row) for row in reader]


def _find_header(rows: list[tuple[int, list[str]]]) -> int:
    """Index of the header row: the first row naming at least two known columns (else the first
    non-empty row). Lets a sheet carry a title or blank rows above the table."""
    known = {_header_key(alias) for aliases in _FIELD_ALIASES.values() for alias in aliases}
    first_non_empty = None
    for i, (_, cells) in enumerate(rows[:20]):
        keys = {_header_key(c) for c in cells if c and c.strip()}
        if not keys:
            continue
        if first_non_empty is None:
            first_non_empty = i
        if len(keys & known) >= 2:
            return i
    return first_non_empty or 0


def _detect_delimiter(text: str) -> str:
    # Decide from the header line only: data cells (addresses, names) often contain commas,
    # but a header row of column names reliably shows the real separator.
    header_line = next((line for line in text.splitlines() if line.strip()), "")
    counts = {d: header_line.count(d) for d in _DELIMITERS}
    best = max(_DELIMITERS, key=lambda d: counts[d])
    return best if counts[best] > 0 else ","


def _column_map(fieldnames: list[str] | None) -> dict[str, list[str]]:
    """Field -> the CSV columns that hold it, in alias priority order."""
    by_key: dict[str, str] = {}
    for name in fieldnames or []:
        key = _header_key(name)
        if key and key not in by_key:  # first of two identically-named columns wins
            by_key[key] = name
    mapping: dict[str, list[str]] = {}
    for field_name, aliases in _FIELD_ALIASES.items():
        cols: list[str] = []
        for alias in aliases:
            col = by_key.get(_header_key(alias))
            if col is not None and col not in cols:
                cols.append(col)
        mapping[field_name] = cols
    return mapping


def _first(row: dict, cols: list[str]) -> str | None:
    for col in cols:
        value = _clean(row.get(col))
        if value:
            return value
    return None


def _valid_mobiles(row: dict, cols: list[str]) -> list[str]:
    found: list[str] = []
    for col in cols:
        phone = normalize_indian_mobile(_clean(row.get(col)))
        if phone and phone not in found:
            found.append(phone)
    return found


def _parse_row(row: dict, columns: dict[str, list[str]], row_no: int, report: ImportReport):
    """Return ``(arn, fields)`` for a valid row (``arn`` may be None), or ``None`` after recording
    the error."""
    raw_arn = _first(row, columns["arn"])
    arn = normalize_arn(raw_arn) if raw_arn else None
    if raw_arn and arn is None:
        report.errors.append((row_no, f"invalid ARN {raw_arn[:20]!r}"))
        return None

    name = _first(row, columns["name"])
    if not name:
        report.errors.append((row_no, f"{arn + ': ' if arn else ''}missing name"))
        return None

    primary = _valid_mobiles(row, columns["phone"])
    alternates = _valid_mobiles(row, columns["alt_phone"])
    phone = primary[0] if primary else (alternates[0] if alternates else None)
    if phone is None:
        # The raw value is left out of the report on purpose (PII); the row number locates it.
        report.errors.append((row_no, f"{arn or name}: no valid Indian mobile number"))
        return None
    alt_phone = next((p for p in alternates + primary[1:] if p != phone), None)
    label = arn or mask_phone(phone)  # identifies the row in warnings without exposing the number

    fields: dict = {
        "name": _truncate("name", name),
        "firm_name": _truncate("firm_name", _first(row, columns["firm_name"])),
        "phone": phone,
        "alt_phone": alt_phone,
        "city": _truncate("city", _first(row, columns["city"])),
        "state": _truncate("state", _first(row, columns["state"])),
    }

    email = _first(row, columns["email"])
    if email and re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        fields["email"] = _truncate("email", email.lower())
    else:
        fields["email"] = None
        if email:
            report.warnings.append((row_no, f"{label}: ignored invalid email"))

    pincode = _first(row, columns["pincode"])
    pin_digits = re.sub(r"\s", "", pincode or "")
    fields["pincode"] = pin_digits if re.fullmatch(r"[1-9]\d{5}", pin_digits) else None
    if pincode and fields["pincode"] is None:
        report.warnings.append((row_no, f"{label}: ignored invalid PIN code {pincode[:10]!r}"))

    raw_euin = _first(row, columns["euin"])
    fields["euin"] = normalize_euin(raw_euin)
    if raw_euin and fields["euin"] is None:
        report.warnings.append((row_no, f"{label}: ignored invalid EUIN {raw_euin[:16]!r}"))

    raw_valid_till = _first(row, columns["arn_valid_till"])
    fields["arn_valid_till"] = parse_date(raw_valid_till)
    if raw_valid_till and fields["arn_valid_till"] is None:
        report.warnings.append(
            (row_no, f"{label}: ignored unrecognised ARN validity date {raw_valid_till[:20]!r}")
        )

    raw_language = _first(row, columns["preferred_language"])
    fields["preferred_language"] = parse_language(raw_language)
    if raw_language and fields["preferred_language"] is None:
        report.warnings.append((row_no, f"{label}: unsupported language {raw_language[:20]!r} (left unset)"))
    return arn, fields


def _apply_dnc(session: Session, distributor: Distributor, row_no: int, report: ImportReport) -> None:
    # Only ever *adds* the restriction: an opted-out number must never be dialled again, even
    # when it arrives on a fresh list under a different ARN.
    for label, phone in (("phone", distributor.phone), ("alternate phone", distributor.alt_phone)):
        if not phone or not compliance.is_dnc(session, phone):
            continue
        already = distributor.do_not_call
        funnel.advance_status(distributor, EmpanelmentStatus.DO_NOT_CALL)
        distributor.do_not_call = True
        if not distributor.dnc_reason:
            distributor.dnc_reason = f"{label} on internal DNC list at import"
        if not already:
            report.dnc_marked += 1
            report.warnings.append(
                (
                    row_no,
                    f"{distributor.arn or mask_phone(distributor.phone)}: {label} is on the DNC list - marked do-not-call",
                )
            )
        return


def _find_existing(session: Session, arn: str | None, phone: str) -> Distributor | None:
    if arn:
        found = session.scalar(select(Distributor).where(Distributor.arn == arn))
        if found is not None:
            return found
        # A row that now carries an ARN may match an earlier ARN-less import of the same mobile.
        return session.scalar(
            select(Distributor).where(Distributor.arn.is_(None), Distributor.phone == phone)
        )
    return session.scalar(select(Distributor).where(Distributor.phone == phone).order_by(Distributor.id))


def import_distributors_csv(
    session: Session,
    source_file: TextIO | BinaryIO | Path | str,
    *,
    source: str,
    update_existing: bool = True,
) -> ImportReport:
    """Import distributors from a CSV or .xlsx file (a path, or an open text / binary handle).

    Each row needs a name and a valid Indian mobile number; the ARN is optional but must be valid
    when present. Invalid rows are reported in ``errors`` and skipped. A repeated ARN (or, for rows
    without an ARN, a repeated mobile) within the file is an error on the later row. A distributor
    already in the database - matched by ARN, else by mobile - is updated with the row's non-empty
    values when ``update_existing`` (``status``, ``do_not_call`` and ``notes`` are never touched),
    otherwise skipped. A row whose phone is on the internal DNC list is imported but marked
    do-not-call.

    Flushes; the caller commits. Raises ``OSError`` / ``UnicodeDecodeError`` / ``csv.Error`` /
    ``ValueError`` / ``zipfile.BadZipFile`` for a file that cannot be read.
    """
    report = ImportReport()
    content = _read_source(source_file)
    rows = _xlsx_rows(content) if isinstance(content, bytes) else _csv_rows(content)
    header_index = _find_header(rows)
    header = rows[header_index][1] if rows else []
    fieldnames = [h if h.strip() else f"__column{i}" for i, h in enumerate(header)]
    columns = _column_map(fieldnames)
    source = _truncate("source", (source or "").strip() or None) or "csv"

    seen: dict[str, int] = {}
    for row_no, cells in rows[header_index + 1 :]:
        row = dict(zip(fieldnames, cells, strict=False))
        if not any(_clean(v) for v in row.values() if isinstance(v, str)):
            continue  # blank line or a row of empty cells (Excel leaves these at the end)
        parsed = _parse_row(row, columns, row_no, report)
        if parsed is None:
            continue
        arn, fields = parsed
        key = arn or fields["phone"]
        if key in seen:
            what = f"ARN {arn}" if arn else f"mobile {mask_phone(fields['phone'])}"
            report.errors.append((row_no, f"duplicate {what} (first seen in row {seen[key]})"))
            continue
        seen[key] = row_no

        existing = _find_existing(session, arn, fields["phone"])
        if existing is None:
            distributor = Distributor(
                arn=arn,
                status=EmpanelmentStatus.NEW,
                do_not_call=False,
                source=source,
                **fields,
            )
            session.add(distributor)
            session.flush()
            report.created += 1
        elif not update_existing:
            distributor = existing
            report.skipped += 1
        else:
            distributor = existing
            changed = False
            if arn and distributor.arn is None:
                distributor.arn = arn
                changed = True
            for name in _DATA_FIELDS:
                value = fields.get(name)
                if value not in (None, "") and getattr(distributor, name) != value:
                    setattr(distributor, name, value)
                    changed = True
            if changed:
                distributor.source = source
                report.updated += 1
            else:
                report.skipped += 1

        _apply_dnc(session, distributor, row_no, report)
        report.distributor_ids.append(distributor.id)

    session.flush()
    log.info(
        "Imported distributors from %s: %d created, %d updated, %d skipped, %d errors, %d marked DNC",
        source,
        report.created,
        report.updated,
        report.skipped,
        len(report.errors),
        report.dnc_marked,
    )
    return report
