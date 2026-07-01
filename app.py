from __future__ import annotations

import hashlib
import io
import json
import mimetypes
import re
import shutil
import sqlite3
import uuid
import warnings
from datetime import date, datetime
from difflib import SequenceMatcher
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

warnings.filterwarnings("ignore", category=DeprecationWarning)
import cgi

import pdfplumber
from pypdf import PdfReader, PdfWriter
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as pdf_canvas
from reportlab.graphics import renderPDF
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing

from instrument_workflow import (
    create_draft as create_instrument_draft,
    list_configurations,
    load_job_for_approval,
    mark_approved,
    reject_job,
    search_instruments,
    switch_candidate,
    sync_catalog,
)


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "output" / "pdf"
DB_PATH = DATA_DIR / "calcert.sqlite3"
SAMPLE_PDF_PATH = Path(
    "/Users/devashishsingh/Desktop/calib cert proj/calib cert documents/AA Electro Magnetic Test Laboratory Private Limited-2.pdf"
)

ADMIN_EMAIL = "admin@calcert.local"
ADMIN_PASSWORD = "admin123"
USER_EMAIL = "engineer@calcert.local"
USER_PASSWORD = "user123"
TOKENS = {
    "admin-local-token": "admin",
    "engineer-local-token": "user",
}


def ensure_dirs() -> None:
    for directory in (STATIC_DIR, DATA_DIR, UPLOAD_DIR, OUTPUT_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    ensure_dirs()
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS historical_documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                original_name TEXT NOT NULL,
                stored_name TEXT NOT NULL,
                checksum TEXT NOT NULL,
                page_count INTEGER NOT NULL DEFAULT 0,
                extracted_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'processed',
                uploaded_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS certificates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL,
                ulr TEXT,
                page_start INTEGER,
                page_end INTEGER,
                page_count INTEGER,
                customer_name TEXT,
                customer_address TEXT,
                srf_no TEXT,
                instrument_receipt_date TEXT,
                calibration_date TEXT,
                next_calibration_date TEXT,
                certificate_issue_date TEXT,
                instrument_name TEXT,
                instrument_type TEXT,
                manufacturer TEXT,
                model TEXT,
                make_model TEXT,
                serial_no TEXT,
                instrument_id TEXT,
                range_text TEXT,
                least_count_text TEXT,
                discipline_parameter TEXT,
                instrument_condition TEXT,
                location_of_calibration TEXT,
                environment_text TEXT,
                calibration_reference_standard TEXT,
                calibration_procedure TEXT,
                master_equipment_json TEXT,
                result_sections_json TEXT,
                result_text TEXT,
                raw_text TEXT,
                raw_tables_json TEXT,
                quality_status TEXT NOT NULL DEFAULT 'usable',
                created_at TEXT NOT NULL,
                FOREIGN KEY(document_id) REFERENCES historical_documents(id)
            );

            CREATE TABLE IF NOT EXISTS generated_certificates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_number TEXT NOT NULL,
                certificate_number TEXT NOT NULL,
                client_name TEXT NOT NULL,
                client_address TEXT,
                instrument_name TEXT NOT NULL,
                instrument_type TEXT,
                manufacturer TEXT,
                model TEXT,
                serial_number TEXT,
                range_text TEXT,
                least_count_text TEXT,
                discipline_parameter TEXT,
                calibration_date TEXT,
                next_calibration_date TEXT,
                certificate_issue_date TEXT,
                matched_certificate_id INTEGER,
                confidence_score REAL,
                match_breakdown_json TEXT,
                draft_json TEXT NOT NULL,
                pdf_path TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(matched_certificate_id) REFERENCES certificates(id)
            );

            CREATE INDEX IF NOT EXISTS idx_certificates_instrument
                ON certificates(instrument_name);
            CREATE INDEX IF NOT EXISTS idx_certificates_parameter
                ON certificates(discipline_parameter);
            CREATE INDEX IF NOT EXISTS idx_generated_certificate_number
                ON generated_certificates(certificate_number);
            """
        )
        sync_catalog(conn)


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def clean_cell(value: object) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def ascii_pdf_text(value: object) -> str:
    text = "" if value is None else str(value)
    replacements = {
        "\u00b0": " deg ",
        "\u00b1": " +/- ",
        "\u03a9": " ohm",
        "\u2126": " ohm",
        "\u00b5": "u",
        "\u00bd": "1/2",
        "\u2013": "-",
        "\u2014": "-",
        "\u2019": "'",
    }
    for src, target in replacements.items():
        text = text.replace(src, target)
    return text.encode("ascii", "ignore").decode("ascii")


def normalize_text(value: object) -> str:
    text = clean_cell(value).lower()
    text = text.replace("\u2126", "ohm").replace("\u03a9", "ohm")
    text = text.replace("\u00b5", "u").replace("\u00b0", "deg")
    text = re.sub(r"[^a-z0-9.]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def fuzzy_ratio(a: object, b: object) -> float:
    left = normalize_text(a)
    right = normalize_text(b)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    return SequenceMatcher(None, left, right).ratio()


def split_make_model(make_model: str) -> tuple[str, str]:
    text = clean_cell(make_model)
    if "/" not in text:
        return text, ""
    manufacturer, model = text.split("/", 1)
    return clean_cell(manufacturer), clean_cell(model)


def extract_numbers(value: object) -> list[float]:
    text = normalize_text(value)
    numbers = []
    for match in re.finditer(r"-?\d+(?:\.\d+)?", text):
        try:
            numbers.append(float(match.group(0)))
        except ValueError:
            pass
    return numbers


def range_overlap_score(new_range: object, old_range: object) -> float:
    new_numbers = extract_numbers(new_range)
    old_numbers = extract_numbers(old_range)
    if not new_numbers or not old_numbers:
        return fuzzy_ratio(new_range, old_range) * 0.7
    new_low, new_high = min(new_numbers), max(new_numbers)
    old_low, old_high = min(old_numbers), max(old_numbers)
    if new_high == new_low or old_high == old_low:
        return 1.0 if abs(new_low - old_low) <= max(abs(old_low) * 0.1, 0.01) else 0.0
    overlap = max(0.0, min(new_high, old_high) - max(new_low, old_low))
    union = max(new_high, old_high) - min(new_low, old_low)
    return max(0.0, min(1.0, overlap / union if union else 0.0))


def find_regex(pattern: str, text: str, default: str = "") -> str:
    match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
    return clean_cell(match.group(1)) if match else default


def table_has_label(table: list[list[object]], label: str) -> bool:
    needle = normalize_text(label)
    for row in table:
        for cell in row:
            if needle in normalize_text(cell):
                return True
    return False


def next_value_after_label(row: list[object], label: str) -> str:
    target = normalize_text(label)
    for index, cell in enumerate(row):
        if normalize_text(cell) == target:
            for next_cell in row[index + 1 :]:
                value = clean_cell(next_cell)
                if value:
                    return value
    return ""


def extract_from_main_table(table: list[list[object]]) -> dict[str, str]:
    fields: dict[str, str] = {}
    for row in table:
        labels = {
            "Instrument Receipt Date": "instrument_receipt_date",
            "SRF No.": "srf_no",
            "Date of Calibration": "calibration_date",
            "Next Calibration Date": "next_calibration_date",
            "Certificate Issue Date": "certificate_issue_date",
            "Instrument Name": "instrument_name",
            "Make / Model No.": "make_model",
            "Range": "range_text",
            "Instrument Condition": "instrument_condition",
            "Least Count": "least_count_text",
            "Location of Calibration": "location_of_calibration",
            "Serial No.": "serial_no",
            "Instrument ID.": "instrument_id",
            "Parameter": "discipline_parameter",
            "Calibration Reference Standard": "calibration_reference_standard",
            "Calibration Procedure": "calibration_procedure",
        }
        for label, key in labels.items():
            value = next_value_after_label(row, label)
            if value and not fields.get(key):
                fields[key] = value

    for row in table:
        if clean_cell(row[0]).startswith("M/S"):
            lines = str(row[0]).splitlines()
            fields["customer_name"] = clean_cell(lines[0])
            fields["customer_address"] = clean_cell(" ".join(lines[1:]))
        if "ENVIRONMENTAL" in clean_cell(row[0]).upper():
            fields["environment_text"] = clean_cell(row[1] if len(row) > 1 else "")

    range_text = fields.get("range_text", "")
    if "\n" in range_text:
        parts = [clean_cell(part) for part in range_text.splitlines() if clean_cell(part)]
        if len(parts) > 1 and not re.search(r"\d", parts[0]):
            fields["instrument_name"] = clean_cell(f"{fields.get('instrument_name', '')} {parts[0]}")
            fields["range_text"] = clean_cell(" ".join(parts[1:]))

    manufacturer, model = split_make_model(fields.get("make_model", ""))
    fields["manufacturer"] = manufacturer
    fields["model"] = model
    return fields


def detect_certificate_groups(page_payloads: list[dict]) -> list[dict]:
    groups: list[dict] = []
    for payload in page_payloads:
        ulr = find_regex(r"Certificate/ULR No\.?:\s*([A-Z0-9/-]+)", payload["text"])
        page_counter = find_regex(
            r"Certificate/ULR No\.?:\s*[A-Z0-9/-]+\s+Page\s+([0-9]+\s+of\s+[0-9]+)",
            payload["text"],
        )
        payload["ulr"] = ulr
        payload["page_counter"] = page_counter
        if groups and ulr and groups[-1].get("ulr") == ulr:
            groups[-1]["pages"].append(payload)
            continue
        groups.append({"ulr": ulr, "pages": [payload]})
    return groups


def extract_master_equipment(tables: list[list[list[object]]]) -> list[dict]:
    equipment = []
    for table in tables:
        if not table:
            continue
        header = [normalize_text(cell) for cell in table[0]]
        if "name" in header and any("certified by" in value for value in header):
            for row in table[1:]:
                equipment.append(
                    {
                        "name": clean_cell(row[0] if len(row) > 0 else ""),
                        "id_or_serial": clean_cell(row[1] if len(row) > 1 else ""),
                        "certificate_ulr": clean_cell(row[2] if len(row) > 2 else ""),
                        "certified_by": clean_cell(row[3] if len(row) > 3 else ""),
                        "valid_upto": clean_cell(row[4] if len(row) > 4 else ""),
                    }
                )
    return equipment


def extract_result_sections(tables: list[list[list[object]]]) -> list[dict]:
    sections = []
    section_index = 1
    for table in tables:
        if not table:
            continue
        if table_has_label(table, "Customer Details") or table_has_label(table, "Instrument Name"):
            continue
        header = [clean_cell(cell) for cell in table[0]]
        header_norm = [normalize_text(cell) for cell in header]
        if "name" in header_norm and any("certified by" in value for value in header_norm):
            continue

        rows = table[1:]
        if header and "certificate ulr" in normalize_text(header[0]) and rows:
            header = [f"Column {index + 1}" for index in range(max(len(row) for row in rows))]
        clean_rows = [[clean_cell(cell) for cell in row] for row in rows if any(clean_cell(cell) for cell in row)]
        if not clean_rows:
            continue
        sections.append(
            {
                "name": f"Calibration Table {section_index}",
                "headers": header,
                "rows": clean_rows,
            }
        )
        section_index += 1
    return sections


def extract_result_text(text: str) -> str:
    marker = re.search(r"(Calibration Results.*)", text, re.IGNORECASE | re.DOTALL)
    if marker:
        return clean_cell(marker.group(1))
    return clean_cell(text[-2500:])


def extract_certificates_from_pdf(pdf_path: Path, original_name: str) -> dict:
    checksum = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    stored_name = pdf_path.name
    uploaded_at = now_iso()

    page_payloads = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for index, page in enumerate(pdf.pages, start=1):
            text = page.extract_text(x_tolerance=1, y_tolerance=3) or ""
            tables = page.extract_tables() or []
            page_payloads.append({"page": index, "text": text, "tables": tables})
        page_count = len(pdf.pages)

    groups = detect_certificate_groups(page_payloads)

    with db() as conn:
        existing = conn.execute(
            "SELECT id, extracted_count FROM historical_documents WHERE checksum = ?",
            (checksum,),
        ).fetchone()
        if existing:
            return {
                "document_id": existing["id"],
                "extracted_count": existing["extracted_count"],
                "duplicate": True,
            }

        cursor = conn.execute(
            """
            INSERT INTO historical_documents
            (original_name, stored_name, checksum, page_count, extracted_count, status, uploaded_at)
            VALUES (?, ?, ?, ?, 0, 'processed', ?)
            """,
            (original_name, stored_name, checksum, page_count, uploaded_at),
        )
        document_id = cursor.lastrowid
        extracted_count = 0

        for group in groups:
            pages = group["pages"]
            first_page = pages[0]
            full_text = "\n\n".join(page["text"] for page in pages)
            all_tables = [table for page in pages for table in page["tables"]]
            main_table = next(
                (table for table in first_page["tables"] if table_has_label(table, "Instrument Name")),
                first_page["tables"][0] if first_page["tables"] else [],
            )
            fields = extract_from_main_table(main_table) if main_table else {}
            ulr = group.get("ulr") or find_regex(r"Certificate/ULR No\.?:\s*([A-Z0-9/-]+)", full_text)
            if not fields.get("instrument_name"):
                fields["instrument_name"] = find_regex(r"Instrument Name\s+(.+?)\s+Make / Model No\.?", full_text)
            if not fields.get("range_text"):
                fields["range_text"] = find_regex(r"Range\s+(.+?)\s+Instrument Condition", full_text)
            if not fields.get("least_count_text"):
                fields["least_count_text"] = find_regex(r"Least Count\s+(.+?)\s+Location of Calibration", full_text)
            if not fields.get("discipline_parameter"):
                fields["discipline_parameter"] = find_regex(r"Parameter\s+(.+?)(?:\n| Calibration)", full_text)

            master_equipment = extract_master_equipment(all_tables)
            result_sections = extract_result_sections(all_tables)
            quality_status = "usable" if fields.get("instrument_name") and result_sections else "incomplete"

            conn.execute(
                """
                INSERT INTO certificates (
                    document_id, ulr, page_start, page_end, page_count, customer_name, customer_address,
                    srf_no, instrument_receipt_date, calibration_date, next_calibration_date,
                    certificate_issue_date, instrument_name, instrument_type, manufacturer, model,
                    make_model, serial_no, instrument_id, range_text, least_count_text,
                    discipline_parameter, instrument_condition, location_of_calibration,
                    environment_text, calibration_reference_standard, calibration_procedure,
                    master_equipment_json, result_sections_json, result_text, raw_text,
                    raw_tables_json, quality_status, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document_id,
                    ulr,
                    pages[0]["page"],
                    pages[-1]["page"],
                    len(pages),
                    fields.get("customer_name", ""),
                    fields.get("customer_address", ""),
                    fields.get("srf_no", ""),
                    fields.get("instrument_receipt_date", ""),
                    fields.get("calibration_date", ""),
                    fields.get("next_calibration_date", ""),
                    fields.get("certificate_issue_date", ""),
                    fields.get("instrument_name", ""),
                    "",
                    fields.get("manufacturer", ""),
                    fields.get("model", ""),
                    fields.get("make_model", ""),
                    fields.get("serial_no", ""),
                    fields.get("instrument_id", ""),
                    fields.get("range_text", ""),
                    fields.get("least_count_text", ""),
                    fields.get("discipline_parameter", ""),
                    fields.get("instrument_condition", ""),
                    fields.get("location_of_calibration", ""),
                    fields.get("environment_text", ""),
                    fields.get("calibration_reference_standard", ""),
                    fields.get("calibration_procedure", ""),
                    json.dumps(master_equipment),
                    json.dumps(result_sections),
                    extract_result_text(full_text),
                    full_text,
                    json.dumps(all_tables),
                    quality_status,
                    now_iso(),
                ),
            )
            extracted_count += 1

        conn.execute(
            "UPDATE historical_documents SET extracted_count = ? WHERE id = ?",
            (extracted_count, document_id),
        )
        sync_catalog(conn)
    return {"document_id": document_id, "extracted_count": extracted_count, "duplicate": False}


def score_candidate(job: dict, cert: sqlite3.Row) -> dict:
    scores = {
        "instrument_name": fuzzy_ratio(job.get("instrument_name"), cert["instrument_name"]),
        "discipline": 1.0
        if normalize_text(job.get("discipline_parameter")) == normalize_text(cert["discipline_parameter"])
        else fuzzy_ratio(job.get("discipline_parameter"), cert["discipline_parameter"]),
        "range": range_overlap_score(job.get("range_text"), cert["range_text"]),
        "least_count": fuzzy_ratio(job.get("least_count_text"), cert["least_count_text"]),
        "make_model": max(
            fuzzy_ratio(job.get("manufacturer"), cert["manufacturer"]),
            fuzzy_ratio(job.get("model"), cert["model"]),
            fuzzy_ratio(
                f"{job.get('manufacturer', '')} {job.get('model', '')}",
                cert["make_model"],
            ),
        ),
        "table_coverage": 1.0 if cert["result_sections_json"] and cert["result_sections_json"] != "[]" else 0.0,
        "historical_quality": 1.0 if cert["quality_status"] == "usable" else 0.35,
    }
    weights = {
        "instrument_name": 0.28,
        "discipline": 0.22,
        "range": 0.16,
        "least_count": 0.10,
        "make_model": 0.10,
        "table_coverage": 0.09,
        "historical_quality": 0.05,
    }
    total = sum(scores[key] * weights[key] for key in weights)
    return {
        "total": round(total, 4),
        "tier": "HIGH" if total >= 0.85 else "MEDIUM" if total >= 0.60 else "LOW",
        "scores": {key: round(value, 4) for key, value in scores.items()},
    }


def find_best_match(job: dict) -> tuple[sqlite3.Row | None, dict, list[dict]]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM certificates
            WHERE quality_status IN ('usable', 'incomplete')
            ORDER BY created_at DESC, id DESC
            """
        ).fetchall()
    ranked = []
    for row in rows:
        breakdown = score_candidate(job, row)
        ranked.append(
            {
                "certificate": row,
                "breakdown": breakdown,
                "summary": {
                    "id": row["id"],
                    "ulr": row["ulr"],
                    "instrument_name": row["instrument_name"],
                    "discipline_parameter": row["discipline_parameter"],
                    "range_text": row["range_text"],
                    "least_count_text": row["least_count_text"],
                    "calibration_date": row["calibration_date"],
                    "score": breakdown["total"],
                    "tier": breakdown["tier"],
                },
            }
        )
    ranked.sort(key=lambda item: item["breakdown"]["total"], reverse=True)
    if not ranked:
        return None, {"total": 0, "tier": "NO_DATA", "scores": {}}, []
    return ranked[0]["certificate"], ranked[0]["breakdown"], [item["summary"] for item in ranked[:5]]


def generate_certificate_number() -> str:
    return f"CALCERT-{date.today().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"


def build_draft(job: dict, cert: sqlite3.Row, certificate_number: str, breakdown: dict) -> dict:
    result_sections = json.loads(cert["result_sections_json"] or "[]")
    master_equipment = json.loads(cert["master_equipment_json"] or "[]")
    return {
        "certificate_number": certificate_number,
        "job": {
            "job_number": job.get("job_number", ""),
            "client_name": job.get("client_name", ""),
            "client_address": job.get("client_address", ""),
            "calibration_date": job.get("calibration_date", ""),
            "next_calibration_date": job.get("next_calibration_date", ""),
            "certificate_issue_date": job.get("certificate_issue_date", ""),
        },
        "instrument": {
            "instrument_name": job.get("instrument_name") or cert["instrument_name"],
            "instrument_type": job.get("instrument_type", ""),
            "manufacturer": job.get("manufacturer") or cert["manufacturer"],
            "model": job.get("model") or cert["model"],
            "serial_number": job.get("serial_number", ""),
            "range_text": job.get("range_text") or cert["range_text"],
            "least_count_text": job.get("least_count_text") or cert["least_count_text"],
            "discipline_parameter": job.get("discipline_parameter") or cert["discipline_parameter"],
        },
        "reused_from_history": {
            "source_certificate_id": cert["id"],
            "source_ulr": cert["ulr"],
            "source_pages": f"{cert['page_start']}-{cert['page_end']}",
            "source_calibration_date": cert["calibration_date"],
            "confidence_score": breakdown["total"],
            "confidence_tier": breakdown["tier"],
            "environment_text": cert["environment_text"],
            "calibration_reference_standard": cert["calibration_reference_standard"],
            "calibration_procedure": cert["calibration_procedure"],
            "master_equipment": master_equipment,
            "result_sections": result_sections,
        },
        "provenance": {
            "new_fields": [
                "client_name",
                "client_address",
                "job_number",
                "certificate_number",
                "calibration_date",
                "next_calibration_date",
                "serial_number",
            ],
            "historical_fields": [
                "environment_text",
                "calibration_reference_standard",
                "calibration_procedure",
                "master_equipment",
                "result_sections",
            ],
            "approval_status": "draft_created",
        },
    }


def render_generated_pdf(draft: dict, output_path: Path) -> None:
    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=landscape(A4),
        leftMargin=12 * mm,
        rightMargin=12 * mm,
        topMargin=10 * mm,
        bottomMargin=10 * mm,
    )
    styles = getSampleStyleSheet()
    story = []

    def p(text: object, style: str = "Normal") -> Paragraph:
        return Paragraph(ascii_pdf_text(text), styles[style])

    story.append(p("Calibration Certificate", "Title"))
    story.append(Spacer(1, 5 * mm))
    header_data = [
        ["Certificate No.", draft["certificate_number"], "Job No.", draft["job"]["job_number"]],
        ["Client", draft["job"]["client_name"], "Calibration Date", draft["job"]["calibration_date"]],
        ["Address", draft["job"]["client_address"], "Next Calibration Date", draft["job"]["next_calibration_date"]],
        ["Instrument", draft["instrument"]["instrument_name"], "Issue Date", draft["job"]["certificate_issue_date"]],
        ["Discipline", draft["instrument"]["discipline_parameter"], "Serial No.", draft["instrument"]["serial_number"]],
        ["Range", draft["instrument"]["range_text"], "Least Count", draft["instrument"]["least_count_text"]],
    ]
    table = Table([[p(cell) for cell in row] for row in header_data], colWidths=[34 * mm, 88 * mm, 36 * mm, 88 * mm])
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.4, colors.black),
                ("BACKGROUND", (0, 0), (0, -1), colors.whitesmoke),
                ("BACKGROUND", (2, 0), (2, -1), colors.whitesmoke),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
            ]
        )
    )
    story.append(table)
    story.append(Spacer(1, 5 * mm))

    source = draft["reused_from_history"]
    story.append(p("Retrieved Historical Data", "Heading2"))
    source_rows = [
        ["Source ULR", source["source_ulr"], "Source Date", source["source_calibration_date"]],
        ["Confidence", f"{source['confidence_score']} ({source['confidence_tier']})", "Source Pages", source["source_pages"]],
        ["Reference Standard", source["calibration_reference_standard"], "Procedure", source["calibration_procedure"]],
        ["Environment", source["environment_text"], "", ""],
    ]
    source_table = Table([[p(cell) for cell in row] for row in source_rows], colWidths=[38 * mm, 88 * mm, 38 * mm, 82 * mm])
    source_table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.35, colors.black), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(source_table)
    story.append(Spacer(1, 4 * mm))

    if source["master_equipment"]:
        story.append(p("Master Equipment", "Heading2"))
        eq_rows = [["Name", "ID / Serial", "Certificate", "Certified By", "Valid Upto"]]
        for item in source["master_equipment"]:
            eq_rows.append(
                [
                    item.get("name", ""),
                    item.get("id_or_serial", ""),
                    item.get("certificate_ulr", ""),
                    item.get("certified_by", ""),
                    item.get("valid_upto", ""),
                ]
            )
        eq_table = Table([[p(cell) for cell in row] for row in eq_rows], repeatRows=1)
        eq_table.setStyle(
            TableStyle(
                [
                    ("GRID", (0, 0), (-1, -1), 0.35, colors.black),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke),
                    ("FONTSIZE", (0, 0), (-1, -1), 7),
                ]
            )
        )
        story.append(eq_table)
        story.append(Spacer(1, 4 * mm))

    story.append(p("Calibration Results Copied From Historical Dataset", "Heading2"))
    for section in source["result_sections"]:
        headers = section.get("headers") or []
        rows = section.get("rows") or []
        if not rows:
            continue
        table_rows = [headers] + rows
        max_cols = max(len(row) for row in table_rows)
        normalized_rows = []
        for row in table_rows[:40]:
            padded = list(row) + [""] * (max_cols - len(row))
            normalized_rows.append([p(cell) for cell in padded])
        result_table = Table(normalized_rows, repeatRows=1)
        result_table.setStyle(
            TableStyle(
                [
                    ("GRID", (0, 0), (-1, -1), 0.25, colors.black),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke),
                    ("FONTSIZE", (0, 0), (-1, -1), 6),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ]
            )
        )
        story.append(p(section.get("name", "Calibration Table"), "Heading3"))
        story.append(result_table)
        story.append(Spacer(1, 3 * mm))

    story.append(Spacer(1, 3 * mm))
    story.append(
        p(
            "Audit: Calibration values marked as historical were retrieved from source certificate "
            f"{source['source_ulr']} and copied into this new draft certificate for engineer review."
        )
    )
    story.append(PageBreak())
    doc.build(story)


def insert_generated_certificate(job: dict, cert: sqlite3.Row, breakdown: dict) -> dict:
    certificate_number = generate_certificate_number()
    draft = build_draft(job, cert, certificate_number, breakdown)
    pdf_path = OUTPUT_DIR / f"{certificate_number}.pdf"
    render_generated_pdf(draft, pdf_path)
    with db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO generated_certificates (
                job_number, certificate_number, client_name, client_address, instrument_name,
                instrument_type, manufacturer, model, serial_number, range_text, least_count_text,
                discipline_parameter, calibration_date, next_calibration_date, certificate_issue_date,
                matched_certificate_id, confidence_score, match_breakdown_json, draft_json, pdf_path, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job.get("job_number", ""),
                certificate_number,
                job.get("client_name", ""),
                job.get("client_address", ""),
                draft["instrument"]["instrument_name"],
                job.get("instrument_type", ""),
                draft["instrument"]["manufacturer"],
                draft["instrument"]["model"],
                job.get("serial_number", ""),
                draft["instrument"]["range_text"],
                draft["instrument"]["least_count_text"],
                draft["instrument"]["discipline_parameter"],
                job.get("calibration_date", ""),
                job.get("next_calibration_date", ""),
                job.get("certificate_issue_date", ""),
                cert["id"],
                breakdown["total"],
                json.dumps(breakdown),
                json.dumps(draft),
                str(pdf_path),
                now_iso(),
            ),
        )
        generated_id = cursor.lastrowid
    return {
        "id": generated_id,
        "certificate_number": certificate_number,
        "pdf_url": f"/api/generated/{generated_id}/pdf",
        "draft": draft,
    }


def render_approved_instrument_pdf(draft: dict, output_path: Path) -> None:
    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=landscape(A4),
        leftMargin=10 * mm,
        rightMargin=10 * mm,
        topMargin=9 * mm,
        bottomMargin=9 * mm,
        title=f"Calibration Certificate {draft['job']['certificate_number']}",
    )
    styles = getSampleStyleSheet()
    story = []

    def p(text: object, style: str = "Normal") -> Paragraph:
        return Paragraph(ascii_pdf_text(text), styles[style])

    job = draft["job"]
    instrument = draft["instrument"]
    source = draft["historical"]
    story.append(p("Calibration Certificate", "Title"))
    story.append(Spacer(1, 3 * mm))
    header_rows = [
        ["Certificate No.", job["certificate_number"], "ULR No.", job.get("ulr_number", "")],
        ["Job No.", job["job_number"], "Issue Date", job.get("issue_date", "")],
        ["Client", job["client_name"], "Calibration Date", job["calibration_date"]],
        ["Address", job.get("client_address", ""), "Next Calibration", job.get("next_calibration_date", "")],
        ["Instrument", instrument["name"], "Serial No.", job["serial_number"]],
        ["Make / Model", f"{instrument['manufacturer']} / {instrument['model']}", "Discipline", instrument["discipline"]],
        ["Range", instrument["range_text"], "Least Count", instrument["least_count_text"]],
        ["Procedure", instrument["calibration_procedure"], "Environment", ", ".join(f"{key}: {value}" for key, value in job.get("environment", {}).items() if value)],
    ]
    header = Table([[p(cell) for cell in row] for row in header_rows], colWidths=[32 * mm, 92 * mm, 34 * mm, 102 * mm])
    header.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.45, colors.HexColor("#344054")),
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#eef2f6")),
                ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#eef2f6")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("FONTSIZE", (0, 0), (-1, -1), 7.5),
            ]
        )
    )
    story.append(header)
    story.append(Spacer(1, 4 * mm))

    for section in draft["result_sections"]:
        story.append(p(section["name"], "Heading2"))
        table_rows = [section["headers"]] + [row["values"] for row in section["rows"]]
        max_columns = max((len(row) for row in table_rows), default=1)
        normalized = [list(row) + [""] * (max_columns - len(row)) for row in table_rows]
        result_table = Table([[p(cell) for cell in row] for row in normalized], repeatRows=1)
        result_table.setStyle(
            TableStyle(
                [
                    ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#475467")),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dfe7ec")),
                    ("FONTSIZE", (0, 0), (-1, -1), 6.2),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("ALIGN", (0, 1), (-1, -1), "CENTER"),
                ]
            )
        )
        story.append(result_table)
        story.append(Spacer(1, 3 * mm))

    story.append(p("Reference Standards and Traceability", "Heading2"))
    trace_rows = [
        ["Calibration reference standard", source.get("reference_standard", "")],
        ["Historical structure source", f"{source.get('source_ulr', '')}, pages {source.get('source_pages', '')}"],
        ["Uncertainty statement", draft["uncertainty"]["statement"]],
        ["Uncertainty model", f"{draft['uncertainty']['model']} v{draft['uncertainty']['version']}"],
    ]
    trace = Table([[p(cell) for cell in row] for row in trace_rows], colWidths=[54 * mm, 206 * mm])
    trace.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#475467")), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(trace)
    story.append(Spacer(1, 5 * mm))
    story.append(p("Approved by engineer. End of certificate."))
    doc.build(story)


def _register_template_fonts() -> tuple[str, str]:
    regular_name = "CalCertArial"
    bold_name = "CalCertArialBold"
    registered = set(pdfmetrics.getRegisteredFontNames())
    if regular_name not in registered:
        regular_path = Path("/System/Library/Fonts/Supplemental/Arial.ttf")
        bold_path = Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf")
        if regular_path.exists() and bold_path.exists():
            pdfmetrics.registerFont(TTFont(regular_name, str(regular_path)))
            pdfmetrics.registerFont(TTFont(bold_name, str(bold_path)))
        else:
            regular_name, bold_name = "Helvetica", "Helvetica-Bold"
    return regular_name, bold_name


def _resolve_historical_pdf(source: sqlite3.Row) -> Path:
    candidates = [
        UPLOAD_DIR / clean_cell(source["stored_name"]),
        UPLOAD_DIR / f"sample-{clean_cell(source['original_name'])}",
    ]
    if clean_cell(source["original_name"]) == SAMPLE_PDF_PATH.name:
        candidates.append(SAMPLE_PDF_PATH)
    candidates.extend(sorted(UPLOAD_DIR.glob(f"*-{clean_cell(source['original_name'])}"), reverse=True))
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate
    raise ValueError("The historical source PDF is missing; exact-layout generation cannot continue")


def _template_date(value: object) -> str:
    text = clean_cell(value)
    if not text:
        return ""
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d")
        return f"{parsed.day}-{parsed.strftime('%b-%Y')}"
    except ValueError:
        return text


def _wrap_pdf_text(text: str, font_name: str, font_size: float, width: float) -> list[str]:
    words = clean_cell(text).split()
    if not words:
        return [""]
    lines = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if pdfmetrics.stringWidth(candidate, font_name, font_size) <= width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _clear_rect(canvas: pdf_canvas.Canvas, rect: tuple, page_height: float, inset: float = 0.8) -> tuple:
    x0, top, x1, bottom = rect
    x = x0 + inset
    y = page_height - bottom + inset
    width = max(1, x1 - x0 - inset * 2)
    height = max(1, bottom - top - inset * 2)
    canvas.setFillColor(colors.white)
    canvas.rect(x, y, width, height, stroke=0, fill=1)
    canvas.setFillColor(colors.black)
    return x, y, width, height


def _replace_cell_text(
    canvas: pdf_canvas.Canvas,
    rect: tuple,
    text: object,
    page_height: float,
    font_name: str,
    font_size: float = 7.0,
    align: str = "left",
) -> None:
    x, y, width, height = _clear_rect(canvas, rect, page_height)
    value = ascii_pdf_text(text)
    lines = _wrap_pdf_text(value, font_name, font_size, width - 4)
    leading = font_size + 1.2
    total_height = len(lines) * leading
    baseline = y + max(1.5, (height - total_height) / 2) + total_height - leading + 1
    canvas.setFont(font_name, font_size)
    for line in lines[: max(1, int(height // leading))]:
        if align == "center":
            canvas.drawCentredString(x + width / 2, baseline, line)
        else:
            canvas.drawString(x + 3, baseline, line)
        baseline -= leading


def _replace_customer_cell(
    canvas: pdf_canvas.Canvas, rect: tuple, client: str, address: str, page_height: float,
    regular_font: str, bold_font: str,
) -> None:
    x, y, width, height = _clear_rect(canvas, rect, page_height)
    cursor = y + height - 9
    name_lines = _wrap_pdf_text(f"M/S. {client}", bold_font, 7.4, width - 6)
    canvas.setFont(bold_font, 7.4)
    for line in name_lines[:2]:
        canvas.drawString(x + 3, cursor, line)
        cursor -= 9
    canvas.setFont(regular_font, 7.0)
    for line in _wrap_pdf_text(address, regular_font, 7.0, width - 6)[:3]:
        canvas.drawString(x + 3, cursor, line)
        cursor -= 8.5


def _draw_replacement_qr(
    canvas: pdf_canvas.Canvas, image: dict, certificate_number: str, page_height: float,
) -> None:
    x0, x1 = float(image["x0"]), float(image["x1"])
    y0, y1 = float(image["y0"]), float(image["y1"])
    width, height = x1 - x0, y1 - y0
    canvas.setFillColor(colors.white)
    canvas.rect(x0 - 1, y0 - 1, width + 2, height + 2, stroke=0, fill=1)
    qr = QrCodeWidget(f"CALCERT:{certificate_number}")
    qx0, qy0, qx1, qy1 = qr.getBounds()
    scale = min(width / (qx1 - qx0), height / (qy1 - qy0))
    drawing = Drawing(width, height, transform=[scale, 0, 0, scale, 0, 0])
    drawing.add(qr)
    renderPDF.draw(drawing, canvas, x0, y0)


def render_historical_template_pdf(draft: dict, source: sqlite3.Row, output_path: Path) -> None:
    source_path = _resolve_historical_pdf(source)
    page_start = int(source["page_start"] or 1)
    page_end = int(source["page_end"] or page_start)
    regular_font, bold_font = _register_template_fonts()
    reader = PdfReader(str(source_path))
    writer = PdfWriter()
    section_index = 0

    with pdfplumber.open(str(source_path)) as extracted:
        for source_page_number in range(page_start, page_end + 1):
            source_page = extracted.pages[source_page_number - 1]
            base_page = reader.pages[source_page_number - 1]
            page_width, page_height = float(source_page.width), float(source_page.height)
            overlay_buffer = io.BytesIO()
            overlay = pdf_canvas.Canvas(overlay_buffer, pagesize=(page_width, page_height))
            tables = source_page.find_tables()

            if source_page_number == page_start:
                main_table = next(
                    (table for table in tables if table_has_label(table.extract(), "Instrument Name")),
                    None,
                )
                if not main_table:
                    raise ValueError("The source certificate identification table could not be mapped safely")
                rows = main_table.extract()
                cells = [row.cells for row in main_table.rows]
                job = draft["job"]
                instrument = draft["instrument"]
                values = {
                    "instrument receipt date": _template_date(job["calibration_date"]),
                    "srf no": job["job_number"],
                    "date of calibration": _template_date(job["calibration_date"]),
                    "next calibration date": _template_date(job.get("next_calibration_date")),
                    "certificate issue date": _template_date(job.get("issue_date")),
                    "instrument name": instrument["name"],
                    "make model no": f"{instrument['manufacturer']}/{instrument['model']}",
                    "range": instrument["range_text"],
                    "least count": instrument["least_count_text"],
                    "serial no": job["serial_number"],
                    "instrument id": job.get("instrument_id", ""),
                    "parameter": instrument["discipline"],
                    "environmental condition": (
                        f"Temperature: {job.get('environment', {}).get('temperature', '')} degC, "
                        f"Relative Humidity: {job.get('environment', {}).get('humidity', '')}%"
                    ),
                }

                certificate_row = cells[0][0]
                certificate_rect = (
                    certificate_row[0] + min(110, (certificate_row[2] - certificate_row[0]) * 0.23),
                    certificate_row[1] + 1,
                    certificate_row[2] - min(62, (certificate_row[2] - certificate_row[0]) * 0.12),
                    certificate_row[3] - 1,
                )
                certificate_value = job.get("ulr_number") or job["certificate_number"]
                _replace_cell_text(overlay, certificate_rect, certificate_value, page_height, regular_font, 7.0)

                for row_values, row_cells in zip(rows, cells):
                    first_value = clean_cell(row_values[0] if row_values else "")
                    if first_value.upper().startswith("M/S.") and row_cells[0]:
                        _replace_customer_cell(
                            overlay, row_cells[0], job["client_name"], job.get("client_address", ""),
                            page_height, regular_font, bold_font,
                        )
                    for index, label in enumerate(row_values):
                        normalized_label = normalize_text(label).replace(".", "")
                        if normalized_label not in values or not row_cells[index]:
                            continue
                        target_index = next(
                            (candidate for candidate in range(index + 1, len(row_cells)) if row_cells[candidate]),
                            None,
                        )
                        if target_index is not None:
                            _replace_cell_text(
                                overlay, row_cells[target_index], values[normalized_label], page_height,
                                regular_font, 7.0,
                            )

            for table in tables:
                extracted_rows = table.extract()
                if not extracted_rows:
                    continue
                header_text = normalize_text(" ".join(clean_cell(value) for value in extracted_rows[0]))
                if "uncertainty" not in header_text or not any(
                    term in header_text for term in ("uuc", "observed", "error", "deviation")
                ):
                    continue
                if section_index >= len(draft["result_sections"]):
                    raise ValueError("The source PDF contains more result tables than the selected configuration")
                section = draft["result_sections"][section_index]
                table_rows = table.rows
                if len(table_rows) - 1 < len(section["rows"]):
                    raise ValueError("The new result rows do not fit the historical table layout")
                for row_number, result_row in enumerate(section["rows"], start=1):
                    row_cells = table_rows[row_number].cells
                    if len(row_cells) != len(result_row["values"]) or any(cell is None for cell in row_cells):
                        raise ValueError("The historical result table geometry does not match the selected configuration")
                    for cell, value in zip(row_cells, result_row["values"]):
                        _replace_cell_text(overlay, cell, value, page_height, regular_font, 6.6, "center")
                section_index += 1

            for image in source_page.images:
                width = float(image["x1"]) - float(image["x0"])
                height = float(image["y1"]) - float(image["y0"])
                if abs(width - height) < 3 and image.get("top", 0) > page_height * 0.65:
                    _draw_replacement_qr(overlay, image, draft["job"]["certificate_number"], page_height)

            overlay.save()
            overlay_buffer.seek(0)
            overlay_page = PdfReader(overlay_buffer).pages[0]
            base_page.merge_page(overlay_page)
            writer.add_page(base_page)

    if section_index != len(draft["result_sections"]):
        raise ValueError("Not all result sections could be mapped to the historical PDF template")
    with output_path.open("wb") as handle:
        writer.write(handle)


def approve_instrument_job(job_id: int) -> dict:
    with db() as conn:
        job, draft = load_job_for_approval(conn, job_id)
        certificate_number = clean_cell(job["certificate_number"]) or generate_certificate_number()
        draft["job"]["certificate_number"] = certificate_number
        pdf_path = OUTPUT_DIR / f"{certificate_number}.pdf"
        source = conn.execute(
            """
            SELECT c.*, d.original_name, d.stored_name
            FROM certificates c
            JOIN historical_documents d ON d.id = c.document_id
            WHERE c.id = ?
            """,
            (draft["historical"]["source_certificate_id"],),
        ).fetchone()
        if not source:
            raise ValueError("The selected historical certificate is no longer available")
        render_historical_template_pdf(draft, source, pdf_path)
        cursor = conn.execute(
            """
            INSERT INTO generated_certificates (
                job_number, certificate_number, client_name, client_address, instrument_name,
                instrument_type, manufacturer, model, serial_number, range_text, least_count_text,
                discipline_parameter, calibration_date, next_calibration_date, certificate_issue_date,
                matched_certificate_id, confidence_score, match_breakdown_json, draft_json, pdf_path, created_at
            ) VALUES (?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job["job_number"],
                certificate_number,
                job["client_name"],
                job["client_address"],
                draft["instrument"]["name"],
                draft["instrument"]["manufacturer"],
                draft["instrument"]["model"],
                job["serial_number"],
                draft["instrument"]["range_text"],
                draft["instrument"]["least_count_text"],
                draft["instrument"]["discipline"],
                job["calibration_date"],
                job["next_calibration_date"],
                job["issue_date"],
                job["matched_certificate_id"],
                draft["historical"]["confidence_score"],
                json.dumps(draft["historical"]["match_explanation"]),
                json.dumps(draft),
                str(pdf_path),
                now_iso(),
            ),
        )
        conn.execute(
            "UPDATE calibration_jobs SET certificate_number = ? WHERE id = ?",
            (certificate_number, job_id),
        )
        mark_approved(conn, job_id, USER_EMAIL)
        return {
            "id": cursor.lastrowid,
            "job_id": job_id,
            "certificate_number": certificate_number,
            "status": "approved",
            "pdf_url": f"/api/generated/{cursor.lastrowid}/pdf",
        }


def row_to_dict(row: sqlite3.Row) -> dict:
    return {key: row[key] for key in row.keys()}


def list_certificates() -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT id, ulr, page_start, page_end, instrument_name, manufacturer, model,
                   range_text, least_count_text, discipline_parameter, calibration_date,
                   quality_status
            FROM certificates
            ORDER BY id DESC
            LIMIT 200
            """
        ).fetchall()
    return [row_to_dict(row) for row in rows]


def list_generated() -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT id, certificate_number, job_number, client_name, instrument_name,
                   confidence_score, created_at
            FROM generated_certificates
            ORDER BY id DESC
            LIMIT 100
            """
        ).fetchall()
    return [row_to_dict(row) for row in rows]


class CalCertHandler(BaseHTTPRequestHandler):
    server_version = "CalCertPrototype/1.0"

    def log_message(self, format: str, *args: object) -> None:
        return

    def send_json(self, payload: dict | list, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, message: str, status: int = 400) -> None:
        self.send_json({"error": message}, status)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    def role_from_request(self) -> str:
        query = parse_qs(urlparse(self.path).query)
        query_token = (query.get("token") or [""])[0]
        if query_token in TOKENS:
            return TOKENS[query_token]
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return ""
        return TOKENS.get(header.replace("Bearer ", "").strip(), "")

    def require_role(self, role: str) -> bool:
        actual = self.role_from_request()
        if actual != role and not (role == "user" and actual == "admin"):
            self.send_error_json("Unauthorized", HTTPStatus.UNAUTHORIZED)
            return False
        return True

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            return self.serve_file(STATIC_DIR / "index.html")
        if path.startswith("/static/"):
            return self.serve_file(STATIC_DIR / path.replace("/static/", "", 1))
        if path == "/api/health":
            return self.send_json({"ok": True, "time": now_iso()})
        if path == "/api/admin/certificates":
            if not self.require_role("admin"):
                return
            return self.send_json({"certificates": list_certificates()})
        if path == "/api/instruments":
            if not self.require_role("user"):
                return
            query = (parse_qs(parsed.query).get("q") or [""])[0]
            with db() as conn:
                return self.send_json({"instruments": search_instruments(conn, query)})
        config_match = re.match(r"^/api/instruments/(\d+)/configurations$", path)
        if config_match:
            if not self.require_role("user"):
                return
            with db() as conn:
                return self.send_json(
                    {"configurations": list_configurations(conn, int(config_match.group(1)))}
                )
        if path == "/api/generated":
            if not self.require_role("user"):
                return
            return self.send_json({"generated": list_generated()})
        match = re.match(r"^/api/generated/(\d+)/pdf$", path)
        if match:
            if not self.require_role("user"):
                return
            return self.serve_generated_pdf(int(match.group(1)))
        self.send_error_json("Not found", HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/login":
                return self.handle_login()
            if path == "/api/admin/upload":
                if not self.require_role("admin"):
                    return
                return self.handle_upload()
            if path == "/api/admin/load-sample":
                if not self.require_role("admin"):
                    return
                return self.handle_load_sample()
            if path == "/api/user/generate":
                if not self.require_role("user"):
                    return
                return self.send_error_json(
                    "Direct generation is retired. Create a review draft and approve it instead.",
                    HTTPStatus.GONE,
                )
            if path == "/api/jobs/draft":
                if not self.require_role("user"):
                    return
                return self.handle_create_draft()
            candidate_match = re.match(r"^/api/jobs/(\d+)/candidate$", path)
            if candidate_match:
                if not self.require_role("user"):
                    return
                return self.handle_switch_candidate(int(candidate_match.group(1)))
            reject_match = re.match(r"^/api/jobs/(\d+)/reject$", path)
            if reject_match:
                if not self.require_role("user"):
                    return
                return self.handle_reject_job(int(reject_match.group(1)))
            approve_match = re.match(r"^/api/jobs/(\d+)/approve$", path)
            if approve_match:
                if not self.require_role("user"):
                    return
                return self.handle_approve_job(int(approve_match.group(1)))
        except Exception as exc:
            self.send_error_json(f"Server error: {exc}", HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self.send_error_json("Not found", HTTPStatus.NOT_FOUND)

    def serve_file(self, path: Path) -> None:
        if not path.exists() or not path.is_file():
            self.send_error_json("Not found", HTTPStatus.NOT_FOUND)
            return
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def serve_generated_pdf(self, generated_id: int) -> None:
        with db() as conn:
            row = conn.execute(
                "SELECT pdf_path FROM generated_certificates WHERE id = ?",
                (generated_id,),
            ).fetchone()
        if not row:
            self.send_error_json("Generated certificate not found", HTTPStatus.NOT_FOUND)
            return
        path = Path(row["pdf_path"])
        if not path.exists():
            path = OUTPUT_DIR / path.name
        if not path.exists():
            self.send_error_json("PDF file missing", HTTPStatus.NOT_FOUND)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", f'inline; filename="{path.name}"')
        self.end_headers()
        self.wfile.write(body)

    def handle_login(self) -> None:
        payload = self.read_json()
        email = payload.get("email", "")
        password = payload.get("password", "")
        if email == ADMIN_EMAIL and password == ADMIN_PASSWORD:
            self.send_json({"token": "admin-local-token", "role": "admin"})
            return
        if email == USER_EMAIL and password == USER_PASSWORD:
            self.send_json({"token": "engineer-local-token", "role": "user"})
            return
        self.send_error_json("Invalid credentials", HTTPStatus.UNAUTHORIZED)

    def handle_upload(self) -> None:
        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                "CONTENT_LENGTH": self.headers.get("Content-Length", "0"),
            },
        )
        file_item = form["file"] if "file" in form else None
        if file_item is None or not getattr(file_item, "filename", ""):
            self.send_error_json("Missing file")
            return
        original_name = Path(file_item.filename).name
        if not original_name.lower().endswith(".pdf"):
            self.send_error_json("Only PDF upload is supported in this prototype")
            return
        stored_name = f"{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}-{original_name}"
        stored_path = UPLOAD_DIR / stored_name
        with stored_path.open("wb") as handle:
            shutil.copyfileobj(file_item.file, handle)
        result = extract_certificates_from_pdf(stored_path, original_name)
        self.send_json({"upload": result, "certificates": list_certificates()})

    def handle_load_sample(self) -> None:
        if not SAMPLE_PDF_PATH.exists():
            self.send_error_json("Sample PDF path not found", HTTPStatus.NOT_FOUND)
            return
        stored_path = UPLOAD_DIR / f"sample-{SAMPLE_PDF_PATH.name}"
        if not stored_path.exists():
            shutil.copy2(SAMPLE_PDF_PATH, stored_path)
        result = extract_certificates_from_pdf(stored_path, SAMPLE_PDF_PATH.name)
        self.send_json({"upload": result, "certificates": list_certificates()})

    def handle_generate(self) -> None:
        job = self.read_json()
        required = ["client_name", "instrument_name", "discipline_parameter", "range_text", "calibration_date"]
        missing = [field for field in required if not clean_cell(job.get(field))]
        if missing:
            self.send_error_json(f"Missing required fields: {', '.join(missing)}")
            return
        match, breakdown, candidates = find_best_match(job)
        if not match:
            self.send_error_json("No historical certificates are available. Upload admin data first.", HTTPStatus.CONFLICT)
            return
        generated = insert_generated_certificate(job, match, breakdown)
        self.send_json(
            {
                "generated": generated,
                "match": {
                    "id": match["id"],
                    "ulr": match["ulr"],
                    "instrument_name": match["instrument_name"],
                    "discipline_parameter": match["discipline_parameter"],
                    "range_text": match["range_text"],
                    "least_count_text": match["least_count_text"],
                    "calibration_date": match["calibration_date"],
                },
                "breakdown": breakdown,
                "candidates": candidates,
            }
        )

    def handle_create_draft(self) -> None:
        payload = self.read_json()
        try:
            with db() as conn:
                result = create_instrument_draft(conn, payload)
            self.send_json(result, HTTPStatus.CREATED)
        except ValueError as exc:
            self.send_error_json(str(exc))

    def handle_switch_candidate(self, job_id: int) -> None:
        payload = self.read_json()
        try:
            certificate_id = int(payload.get("certificate_id") or 0)
            with db() as conn:
                result = switch_candidate(conn, job_id, certificate_id)
            self.send_json(result)
        except (TypeError, ValueError) as exc:
            self.send_error_json(str(exc))

    def handle_reject_job(self, job_id: int) -> None:
        payload = self.read_json()
        try:
            with db() as conn:
                reject_job(conn, job_id, USER_EMAIL, payload.get("notes", ""))
            self.send_json({"job_id": job_id, "status": "rejected"})
        except ValueError as exc:
            self.send_error_json(str(exc))

    def handle_approve_job(self, job_id: int) -> None:
        try:
            self.send_json(approve_instrument_job(job_id))
        except ValueError as exc:
            self.send_error_json(str(exc))


def run(host: str = "127.0.0.1", port: int = 8000) -> None:
    init_db()
    server = ThreadingHTTPServer((host, port), CalCertHandler)
    print(f"CalCert prototype running at http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    run()
