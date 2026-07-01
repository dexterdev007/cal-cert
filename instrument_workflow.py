from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import statistics
from datetime import datetime
from difflib import SequenceMatcher


CALCULATED_TERMS = (
    "uncertainty",
    "error",
    "correction",
    "deviation",
    "average",
    "accuracy",
    "repeatab",
    "resolution",
    "zero error",
    "tolerance result",
)
MEASUREMENT_TERMS = (
    "observed",
    "reading",
    "standard value",
    "std value",
    "std. value",
    "division at room",
)


def clean(value: object) -> str:
    return re.sub(r"\s+", " ", "" if value is None else str(value)).strip()


def normalize(value: object) -> str:
    text = clean(value).lower().replace("\u2126", "ohm").replace("\u03a9", "ohm")
    text = text.replace("\u00b5", "u").replace("\u00b0", "deg")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9.]+", " ", text)).strip()


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def classify_header(header: str) -> str:
    value = normalize(header)
    if any(term in value for term in CALCULATED_TERMS):
        return "calculated"
    if any(term in value for term in MEASUREMENT_TERMS):
        return "measurement"
    if "uuc value" in value and "observed" in value:
        return "measurement"
    return "historical"


def infer_measurement_schema(result_sections: list[dict]) -> list[dict]:
    schema = []
    previous_headers: list[str] = []
    for section_index, section in enumerate(result_sections):
        headers = [clean(item) or f"Column {index + 1}" for index, item in enumerate(section.get("headers") or [])]
        rows = section.get("rows") or []
        max_columns = max([len(headers), *(len(row) for row in rows)] or [0])
        headers += [f"Column {index + 1}" for index in range(len(headers), max_columns)]
        if headers and all(normalize(header).startswith("column ") for header in headers):
            if len(previous_headers) == len(headers):
                headers = list(previous_headers)
        else:
            previous_headers = list(headers)
        columns = [
            {
                "key": f"c{index}",
                "label": header,
                "kind": classify_header(header),
            }
            for index, header in enumerate(headers)
        ]
        schema_rows = []
        for row_index, raw_row in enumerate(rows):
            values = [clean(cell) for cell in list(raw_row) + [""] * (max_columns - len(raw_row))]
            static_values = {
                column["key"]: values[index]
                for index, column in enumerate(columns)
                if column["kind"] == "historical"
            }
            joined_row = normalize(" ".join(values))
            measurement_values = [
                values[index] for index, column in enumerate(columns) if column["kind"] == "measurement"
            ]
            schema_rows.append(
                {
                    "index": row_index,
                    "static_values": static_values,
                    "is_summary": (
                        any(term in joined_row for term in ("stdev at", "standard deviation", "summary"))
                        or not any(re.search(r"-?\d+(?:\.\d+)?", value or "") for value in measurement_values)
                    ),
                }
            )
        schema.append(
            {
                "index": section_index,
                "name": clean(section.get("name")) or f"Calibration Table {section_index + 1}",
                "columns": columns,
                "rows": schema_rows,
            }
        )
    return schema


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS uncertainty_models (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            discipline TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            version TEXT NOT NULL,
            model_json TEXT NOT NULL,
            validation_status TEXT NOT NULL DEFAULT 'requires_lab_validation',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS instrument_catalog (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            normalized_name TEXT NOT NULL UNIQUE,
            usage_count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS instrument_configurations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            config_key TEXT NOT NULL UNIQUE,
            instrument_id INTEGER NOT NULL,
            source_certificate_id INTEGER NOT NULL,
            manufacturer TEXT,
            model TEXT,
            range_text TEXT,
            least_count_text TEXT,
            discipline TEXT,
            calibration_procedure TEXT,
            point_set_hash TEXT NOT NULL,
            measurement_schema_json TEXT NOT NULL,
            result_template_json TEXT NOT NULL,
            uncertainty_model_id INTEGER NOT NULL,
            usage_count INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(instrument_id) REFERENCES instrument_catalog(id),
            FOREIGN KEY(source_certificate_id) REFERENCES certificates(id),
            FOREIGN KEY(uncertainty_model_id) REFERENCES uncertainty_models(id)
        );

        CREATE TABLE IF NOT EXISTS calibration_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_number TEXT NOT NULL,
            client_name TEXT NOT NULL,
            client_address TEXT,
            certificate_number TEXT,
            ulr_number TEXT,
            serial_number TEXT,
            instrument_id INTEGER NOT NULL,
            configuration_id INTEGER NOT NULL,
            matched_certificate_id INTEGER,
            calibration_date TEXT NOT NULL,
            next_calibration_date TEXT,
            issue_date TEXT,
            environment_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'under_review',
            approved_by TEXT,
            approved_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(instrument_id) REFERENCES instrument_catalog(id),
            FOREIGN KEY(configuration_id) REFERENCES instrument_configurations(id)
        );

        CREATE TABLE IF NOT EXISTS job_measurements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            section_index INTEGER NOT NULL,
            row_index INTEGER NOT NULL,
            field_key TEXT NOT NULL,
            field_label TEXT NOT NULL,
            values_json TEXT NOT NULL,
            FOREIGN KEY(job_id) REFERENCES calibration_jobs(id)
        );

        CREATE TABLE IF NOT EXISTS match_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            certificate_id INTEGER NOT NULL,
            rank INTEGER NOT NULL,
            score REAL NOT NULL,
            explanation_json TEXT NOT NULL,
            selected INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(job_id) REFERENCES calibration_jobs(id),
            FOREIGN KEY(certificate_id) REFERENCES certificates(id)
        );

        CREATE TABLE IF NOT EXISTS certificate_drafts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'under_review',
            draft_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(job_id) REFERENCES calibration_jobs(id)
        );

        CREATE TABLE IF NOT EXISTS field_provenance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            field_path TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_certificate_id INTEGER,
            source_ulr TEXT,
            source_page INTEGER,
            source_field TEXT,
            confidence REAL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(job_id) REFERENCES calibration_jobs(id)
        );

        CREATE TABLE IF NOT EXISTS approval_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            actor TEXT NOT NULL,
            notes TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(job_id) REFERENCES calibration_jobs(id)
        );

        CREATE INDEX IF NOT EXISTS idx_instrument_configurations_instrument
            ON instrument_configurations(instrument_id);
        CREATE INDEX IF NOT EXISTS idx_calibration_jobs_status
            ON calibration_jobs(status);
        """
    )


def _model_for_discipline(discipline: str) -> dict:
    key = normalize(discipline)
    profiles = {
        "force": {"reference_fraction": 0.0025, "environment_coefficient": 0.0002},
        "thermal": {"reference_fraction": 0.0015, "environment_coefficient": 0.0020},
        "dimension": {"reference_fraction": 0.0010, "environment_coefficient": 0.0005},
        "speed": {"reference_fraction": 0.0010, "environment_coefficient": 0.0001},
        "pressure": {"reference_fraction": 0.0020, "environment_coefficient": 0.0002},
        "electro technical": {"reference_fraction": 0.0008, "environment_coefficient": 0.0001},
    }
    profile = profiles.get(key, {"reference_fraction": 0.0020, "environment_coefficient": 0.0002})
    return {
        "method": "current_measurement_rss",
        "coverage_factor": 2.0,
        "resolution_distribution": "rectangular",
        "resolution_divisor": math.sqrt(12),
        **profile,
    }


def sync_catalog(conn: sqlite3.Connection) -> None:
    init_schema(conn)
    certificate_rows = conn.execute(
        """
        SELECT * FROM certificates
        WHERE trim(instrument_name) <> '' AND quality_status IN ('usable', 'incomplete')
        ORDER BY id
        """
    ).fetchall()
    grouped_instruments: dict[str, dict] = {}
    for row in certificate_rows:
        normalized = normalize(row["instrument_name"])
        item = grouped_instruments.setdefault(normalized, {"names": {}, "rows": []})
        name = clean(row["instrument_name"])
        item["names"][name] = item["names"].get(name, 0) + 1
        item["rows"].append(row)

    for normalized, item in grouped_instruments.items():
        preferred_name = max(item["names"], key=lambda name: (item["names"][name], -len(name)))
        conn.execute(
            """
            INSERT INTO instrument_catalog(name, normalized_name, usage_count, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(normalized_name) DO UPDATE SET
                name=excluded.name, usage_count=excluded.usage_count, updated_at=excluded.updated_at
            """,
            (preferred_name, normalized, len(item["rows"]), now_iso()),
        )

    disciplines = {clean(row["discipline_parameter"]) or "General" for row in certificate_rows}
    for discipline in disciplines:
        conn.execute(
            """
            INSERT INTO uncertainty_models(discipline, name, version, model_json, created_at)
            VALUES (?, ?, '1.0', ?, ?)
            ON CONFLICT(discipline) DO UPDATE SET model_json=excluded.model_json
            """,
            (discipline, f"{discipline} current-measurement model", json.dumps(_model_for_discipline(discipline)), now_iso()),
        )

    aggregates: dict[str, dict] = {}
    for row in certificate_rows:
        result_template = json.loads(row["result_sections_json"] or "[]")
        schema = infer_measurement_schema(result_template)
        point_payload = [
            {
                "name": section["name"],
                "static": [entry["static_values"] for entry in section["rows"]],
                "columns": [column["label"] for column in section["columns"]],
            }
            for section in schema
        ]
        point_hash = hashlib.sha256(json.dumps(point_payload, sort_keys=True).encode()).hexdigest()
        normalized_instrument = normalize(row["instrument_name"])
        config_payload = [
            normalized_instrument,
            normalize(row["manufacturer"]),
            normalize(row["model"]),
            normalize(row["range_text"]),
            normalize(row["least_count_text"]),
            normalize(row["discipline_parameter"]),
            normalize(row["calibration_procedure"]),
            point_hash,
        ]
        config_key = hashlib.sha256("|".join(config_payload).encode()).hexdigest()
        aggregate = aggregates.setdefault(
            config_key,
            {"row": row, "schema": schema, "template": result_template, "point_hash": point_hash, "count": 0},
        )
        aggregate["count"] += 1
        if row["calibration_date"] and row["calibration_date"] > (aggregate["row"]["calibration_date"] or ""):
            aggregate.update(row=row, schema=schema, template=result_template)

    for config_key, item in aggregates.items():
        row = item["row"]
        instrument = conn.execute(
            "SELECT id FROM instrument_catalog WHERE normalized_name = ?", (normalize(row["instrument_name"]),)
        ).fetchone()
        model = conn.execute(
            "SELECT id FROM uncertainty_models WHERE discipline = ?",
            (clean(row["discipline_parameter"]) or "General",),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO instrument_configurations(
                config_key, instrument_id, source_certificate_id, manufacturer, model, range_text,
                least_count_text, discipline, calibration_procedure, point_set_hash,
                measurement_schema_json, result_template_json, uncertainty_model_id, usage_count, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(config_key) DO UPDATE SET
                source_certificate_id=excluded.source_certificate_id,
                measurement_schema_json=excluded.measurement_schema_json,
                result_template_json=excluded.result_template_json,
                uncertainty_model_id=excluded.uncertainty_model_id,
                usage_count=excluded.usage_count,
                updated_at=excluded.updated_at
            """,
            (
                config_key,
                instrument["id"],
                row["id"],
                row["manufacturer"],
                row["model"],
                row["range_text"],
                row["least_count_text"],
                row["discipline_parameter"],
                row["calibration_procedure"],
                item["point_hash"],
                json.dumps(item["schema"]),
                json.dumps(item["template"]),
                model["id"],
                item["count"],
                now_iso(),
            ),
        )

    if aggregates:
        placeholders = ",".join("?" for _ in aggregates)
        conn.execute(
            f"""
            DELETE FROM instrument_configurations
            WHERE config_key NOT IN ({placeholders})
              AND id NOT IN (SELECT configuration_id FROM calibration_jobs)
            """,
            tuple(aggregates),
        )


def search_instruments(conn: sqlite3.Connection, query: str, limit: int = 8) -> list[dict]:
    needle = normalize(query)
    if not needle:
        return []
    rows = conn.execute("SELECT * FROM instrument_catalog").fetchall()
    max_usage = max((row["usage_count"] for row in rows), default=1)
    ranked = []
    for row in rows:
        name = row["normalized_name"]
        ratio = SequenceMatcher(None, needle, name).ratio()
        exact = 1.0 if needle == name else 0.0
        prefix = 0.94 if name.startswith(needle) else 0.0
        contains = 0.78 if needle in name else 0.0
        fuzzy = ratio * 0.72
        usage = (math.log1p(row["usage_count"]) / math.log1p(max_usage)) * 0.06
        score = min(1.0, max(exact, prefix, contains, fuzzy) + usage)
        if score >= 0.28:
            ranked.append(
                {
                    "id": row["id"],
                    "name": row["name"],
                    "usage_count": row["usage_count"],
                    "score": round(score, 4),
                }
            )
    ranked.sort(key=lambda item: (item["score"], item["usage_count"]), reverse=True)
    return ranked[:limit]


def list_configurations(conn: sqlite3.Connection, instrument_id: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT c.*, u.name uncertainty_model_name, u.version uncertainty_model_version,
               u.validation_status, h.ulr source_ulr, h.calibration_date source_calibration_date
        FROM instrument_configurations c
        JOIN uncertainty_models u ON u.id = c.uncertainty_model_id
        JOIN certificates h ON h.id = c.source_certificate_id
        WHERE c.instrument_id = ?
        ORDER BY c.usage_count DESC, c.manufacturer, c.model, c.range_text
        """,
        (instrument_id,),
    ).fetchall()
    return [
        {
            "id": row["id"],
            "manufacturer": row["manufacturer"],
            "model": row["model"],
            "range_text": row["range_text"],
            "least_count_text": row["least_count_text"],
            "discipline": row["discipline"],
            "calibration_procedure": row["calibration_procedure"],
            "usage_count": row["usage_count"],
            "measurement_schema": json.loads(row["measurement_schema_json"]),
            "uncertainty_model": {
                "name": row["uncertainty_model_name"],
                "version": row["uncertainty_model_version"],
                "validation_status": row["validation_status"],
            },
            "source": {
                "certificate_id": row["source_certificate_id"],
                "ulr": row["source_ulr"],
                "calibration_date": row["source_calibration_date"],
            },
        }
        for row in rows
    ]


def _numbers(value: object) -> list[float]:
    if isinstance(value, list):
        raw = value
    else:
        raw = re.split(r"[,;\n]", clean(value))
    numbers = []
    for item in raw:
        match = re.search(r"-?\d+(?:\.\d+)?", clean(item))
        if match:
            numbers.append(float(match.group(0)))
    return numbers


def _first_number(value: object, default: float = 0.0) -> float:
    values = _numbers(value)
    return values[0] if values else default


def _format_number(value: float) -> str:
    if not math.isfinite(value):
        return ""
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _calculate_row(
    columns: list[dict], values_by_key: dict[str, list[float]], static_values: dict[str, str],
    least_count: str, environment: dict, model: dict
) -> dict:
    measured = {key: statistics.fmean(values) for key, values in values_by_key.items() if values}
    repeat_components = []
    for values in values_by_key.values():
        if len(values) > 1:
            repeat_components.append(statistics.stdev(values) / math.sqrt(len(values)))
    type_a = math.sqrt(sum(value * value for value in repeat_components)) if repeat_components else 0.0

    labels = {column["key"]: normalize(column["label"]) for column in columns}
    uuc_values = [measured[key] for key, label in labels.items() if key in measured and "uuc" in label]
    historical_points = [
        _first_number(static_values.get(key))
        for key, label in labels.items()
        if static_values.get(key) and any(term in label for term in ("uuc value", "load", "force"))
    ]
    standard_values = [
        measured[key]
        for key, label in labels.items()
        if key in measured and ("standard" in label or re.search(r"\bstd\b", label))
    ]
    all_values = list(measured.values())
    uuc = statistics.fmean(uuc_values) if uuc_values else (
        historical_points[0] if historical_points else (all_values[0] if all_values else 0.0)
    )
    standard = statistics.fmean(standard_values) if standard_values else (all_values[-1] if all_values else uuc)
    error = uuc - standard
    nominal = max(abs(standard), abs(uuc), 1.0)
    resolution = abs(_first_number(least_count)) / float(model.get("resolution_divisor", math.sqrt(12)))
    reference = nominal * float(model.get("reference_fraction", 0.002))
    temperature = _first_number(environment.get("temperature"), 23.0)
    environment_component = abs(temperature - 23.0) * nominal * float(model.get("environment_coefficient", 0.0))
    type_b = math.sqrt(resolution**2 + reference**2 + environment_component**2)
    combined = math.sqrt(type_a**2 + type_b**2)
    expanded = combined * float(model.get("coverage_factor", 2.0))
    repeatability_percent = 0.0
    flattened = [value for values in values_by_key.values() for value in values]
    if len(flattened) > 1 and nominal:
        repeatability_percent = (max(flattened) - min(flattened)) / nominal * 100
    return {
        "measured_means": measured,
        "uuc_mean": uuc,
        "standard_mean": standard,
        "error": error,
        "correction": -error,
        "deviation": error,
        "relative_accuracy_percent": abs(error) / nominal * 100,
        "repeatability_percent": repeatability_percent,
        "resolution_percent": abs(_first_number(least_count)) / nominal * 100,
        "type_a": type_a,
        "type_b": type_b,
        "combined": combined,
        "expanded": expanded,
        "coverage_factor": float(model.get("coverage_factor", 2.0)),
    }


def _calculated_value(label: str, result: dict) -> str:
    normalized = normalize(label)
    if "uncertainty" in normalized:
        return _format_number(result["expanded"])
    if "correction" in normalized:
        return _format_number(result["correction"])
    if "deviation" in normalized:
        return _format_number(result["deviation"])
    if "repeatab" in normalized:
        return _format_number(result["repeatability_percent"])
    if "resolution" in normalized:
        return _format_number(result["resolution_percent"])
    if "accuracy" in normalized or "error" in normalized:
        if "%" in label:
            return _format_number(result["relative_accuracy_percent"])
        return _format_number(result["error"])
    if "average" in normalized:
        return _format_number(result["uuc_mean"])
    return "Calculated"


def _candidate_score(config: sqlite3.Row, cert: sqlite3.Row) -> tuple[float, dict]:
    fields = {
        "instrument": 1.0,
        "discipline": 1.0 if normalize(config["discipline"]) == normalize(cert["discipline_parameter"]) else 0.0,
        "manufacturer": SequenceMatcher(None, normalize(config["manufacturer"]), normalize(cert["manufacturer"])).ratio(),
        "model": SequenceMatcher(None, normalize(config["model"]), normalize(cert["model"])).ratio(),
        "range": SequenceMatcher(None, normalize(config["range_text"]), normalize(cert["range_text"])).ratio(),
        "least_count": SequenceMatcher(None, normalize(config["least_count_text"]), normalize(cert["least_count_text"])).ratio(),
        "procedure": SequenceMatcher(
            None, normalize(config["calibration_procedure"]), normalize(cert["calibration_procedure"])
        ).ratio(),
        "quality": 1.0 if cert["quality_status"] == "usable" else 0.4,
    }
    weights = {
        "instrument": 0.24,
        "discipline": 0.18,
        "manufacturer": 0.10,
        "model": 0.10,
        "range": 0.14,
        "least_count": 0.10,
        "procedure": 0.08,
        "quality": 0.06,
    }
    score = sum(fields[key] * weights[key] for key in weights)
    explanation = {
        "scores": {key: round(value, 4) for key, value in fields.items()},
        "matched": [key for key, value in fields.items() if value >= 0.9],
        "weak": [key for key, value in fields.items() if value < 0.6],
    }
    return round(score, 4), explanation


def _rank_candidates(conn: sqlite3.Connection, config: sqlite3.Row) -> list[dict]:
    instrument = conn.execute("SELECT normalized_name FROM instrument_catalog WHERE id = ?", (config["instrument_id"],)).fetchone()
    rows = conn.execute("SELECT * FROM certificates WHERE quality_status IN ('usable', 'incomplete')").fetchall()
    ranked = []
    for cert in rows:
        if normalize(cert["instrument_name"]) != instrument["normalized_name"]:
            continue
        score, explanation = _candidate_score(config, cert)
        ranked.append(
            {
                "certificate": cert,
                "score": score,
                "tier": "HIGH" if score >= 0.85 else "MEDIUM" if score >= 0.60 else "LOW",
                "explanation": explanation,
            }
        )
    ranked.sort(key=lambda item: (item["score"], item["certificate"]["calibration_date"] or ""), reverse=True)
    return ranked[:5]


def create_draft(conn: sqlite3.Connection, payload: dict) -> dict:
    config_id = int(payload.get("configuration_id") or 0)
    config = conn.execute(
        """
        SELECT c.*, i.name instrument_name, u.name uncertainty_model_name,
               u.version uncertainty_model_version, u.model_json, u.validation_status
        FROM instrument_configurations c
        JOIN instrument_catalog i ON i.id = c.instrument_id
        JOIN uncertainty_models u ON u.id = c.uncertainty_model_id
        WHERE c.id = ?
        """,
        (config_id,),
    ).fetchone()
    if not config:
        raise ValueError("Select a valid historical instrument configuration")

    required = ("client_name", "job_number", "calibration_date", "serial_number")
    missing = [field for field in required if not clean(payload.get(field))]
    if missing:
        raise ValueError(f"Missing required fields: {', '.join(missing)}")

    schema = json.loads(config["measurement_schema_json"])
    submitted = payload.get("measurements") or {}
    normalized_measurements: dict[str, list[float]] = {}
    missing_measurements = []
    required_measurement_count = 0
    for section in schema:
        for row in section["rows"]:
            if row.get("is_summary"):
                continue
            for column in section["columns"]:
                if column["kind"] != "measurement":
                    continue
                required_measurement_count += 1
                field_id = f"s{section['index']}.r{row['index']}.{column['key']}"
                values = _numbers(submitted.get(field_id))
                if not values:
                    missing_measurements.append(f"{section['name']} row {row['index'] + 1}: {column['label']}")
                else:
                    normalized_measurements[field_id] = values
    if required_measurement_count == 0:
        raise ValueError("This configuration has no reliable current-measurement schema and requires admin review")
    if missing_measurements:
        preview = ", ".join(missing_measurements[:4])
        suffix = "..." if len(missing_measurements) > 4 else ""
        raise ValueError(f"Enter current readings for {preview}{suffix}")

    candidates = _rank_candidates(conn, config)
    if not candidates:
        raise ValueError("No compatible historical certificate is available")
    requested_candidate_id = int(payload.get("candidate_id") or 0)
    selected = next(
        (candidate for candidate in candidates if candidate["certificate"]["id"] == requested_candidate_id),
        candidates[0],
    )
    source = selected["certificate"]
    environment = payload.get("environment") or {}
    model = json.loads(config["model_json"])

    now = now_iso()
    cursor = conn.execute(
        """
        INSERT INTO calibration_jobs(
            job_number, client_name, client_address, certificate_number, ulr_number, serial_number,
            instrument_id, configuration_id, matched_certificate_id, calibration_date,
            next_calibration_date, issue_date, environment_json, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'under_review', ?, ?)
        """,
        (
            clean(payload["job_number"]),
            clean(payload["client_name"]),
            clean(payload.get("client_address")),
            clean(payload.get("certificate_number")),
            clean(payload.get("ulr_number")),
            clean(payload["serial_number"]),
            config["instrument_id"],
            config["id"],
            source["id"],
            clean(payload["calibration_date"]),
            clean(payload.get("next_calibration_date")),
            clean(payload.get("issue_date")),
            json.dumps(environment),
            now,
            now,
        ),
    )
    job_id = cursor.lastrowid

    result_sections = []
    calculation_audit = []
    for section in schema:
        result_rows = []
        for row in section["rows"]:
            row_measurements = {}
            for column in section["columns"]:
                field_id = f"s{section['index']}.r{row['index']}.{column['key']}"
                if field_id in normalized_measurements:
                    row_measurements[column["key"]] = normalized_measurements[field_id]
                    conn.execute(
                        """
                        INSERT INTO job_measurements(job_id, section_index, row_index, field_key, field_label, values_json)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            job_id,
                            section["index"],
                            row["index"],
                            column["key"],
                            column["label"],
                            json.dumps(normalized_measurements[field_id]),
                        ),
                    )
            calculation = _calculate_row(
                section["columns"], row_measurements, row["static_values"],
                config["least_count_text"], environment, model
            )
            values = []
            provenance = []
            for column in section["columns"]:
                key = column["key"]
                if column["kind"] == "historical":
                    value = row["static_values"].get(key, "")
                    source_type = "historical"
                elif column["kind"] == "measurement":
                    numbers = row_measurements.get(key, [])
                    value = _format_number(statistics.fmean(numbers)) if numbers else ""
                    source_type = "current_measurement"
                else:
                    value = _calculated_value(column["label"], calculation)
                    source_type = "calculated"
                values.append(value)
                provenance.append(source_type)
            result_rows.append({"values": values, "provenance": provenance})
            if row_measurements:
                calculation_audit.append(
                    {
                        "section": section["name"],
                        "row": row["index"] + 1,
                        **{key: round(value, 10) for key, value in calculation.items() if key != "measured_means"},
                    }
                )
        result_sections.append(
            {
                "name": section["name"],
                "headers": [column["label"] for column in section["columns"]],
                "rows": result_rows,
            }
        )

    master_equipment = json.loads(source["master_equipment_json"] or "[]")
    draft = {
        "job_id": job_id,
        "status": "under_review",
        "job": {
            "job_number": clean(payload["job_number"]),
            "client_name": clean(payload["client_name"]),
            "client_address": clean(payload.get("client_address")),
            "certificate_number": clean(payload.get("certificate_number")),
            "ulr_number": clean(payload.get("ulr_number")),
            "serial_number": clean(payload["serial_number"]),
            "instrument_id": clean(payload.get("instrument_id")),
            "calibration_date": clean(payload["calibration_date"]),
            "next_calibration_date": clean(payload.get("next_calibration_date")),
            "issue_date": clean(payload.get("issue_date")),
            "environment": environment,
        },
        "instrument": {
            "id": config["instrument_id"],
            "name": config["instrument_name"],
            "configuration_id": config["id"],
            "manufacturer": config["manufacturer"],
            "model": config["model"],
            "range_text": config["range_text"],
            "least_count_text": config["least_count_text"],
            "discipline": config["discipline"],
            "calibration_procedure": config["calibration_procedure"],
        },
        "historical": {
            "source_certificate_id": source["id"],
            "source_ulr": source["ulr"],
            "source_pages": f"{source['page_start']}-{source['page_end']}",
            "source_calibration_date": source["calibration_date"],
            "confidence_score": selected["score"],
            "match_explanation": selected["explanation"],
            "reference_standard": source["calibration_reference_standard"],
            "master_equipment": master_equipment,
        },
        "uncertainty": {
            "model": config["uncertainty_model_name"],
            "version": config["uncertainty_model_version"],
            "validation_status": config["validation_status"],
            "method": model["method"],
            "calculations": calculation_audit,
            "statement": "All uncertainty values were recalculated from current measurements.",
        },
        "result_sections": result_sections,
    }
    draft_cursor = conn.execute(
        """
        INSERT INTO certificate_drafts(job_id, version, status, draft_json, created_at, updated_at)
        VALUES (?, 1, 'under_review', ?, ?, ?)
        """,
        (job_id, json.dumps(draft), now, now),
    )
    for rank, candidate in enumerate(candidates, start=1):
        cert = candidate["certificate"]
        conn.execute(
            """
            INSERT INTO match_candidates(job_id, certificate_id, rank, score, explanation_json, selected)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (job_id, cert["id"], rank, candidate["score"], json.dumps(candidate["explanation"]), cert["id"] == source["id"]),
        )

    provenance_rows = [
        ("job.client_name", "current_measurement", None, None, None, "client_name", 1.0),
        ("job.serial_number", "current_measurement", None, None, None, "serial_number", 1.0),
        ("instrument.calibration_procedure", "historical", source["id"], source["ulr"], source["page_start"], "calibration_procedure", selected["score"]),
        ("historical.master_equipment", "historical", source["id"], source["ulr"], source["page_start"], "master_equipment", selected["score"]),
        ("result_sections.*.measurements", "current_measurement", None, None, None, "job_measurements", 1.0),
        ("result_sections.*.uncertainty", "calculated", None, None, None, "uncertainty_engine", 1.0),
    ]
    conn.executemany(
        """
        INSERT INTO field_provenance(
            job_id, field_path, source_type, source_certificate_id, source_ulr,
            source_page, source_field, confidence, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [(job_id, *row, now) for row in provenance_rows],
    )
    return {
        "job_id": job_id,
        "draft_id": draft_cursor.lastrowid,
        "status": "under_review",
        "draft": draft,
        "candidates": [
            {
                "certificate_id": item["certificate"]["id"],
                "ulr": item["certificate"]["ulr"],
                "calibration_date": item["certificate"]["calibration_date"],
                "score": item["score"],
                "tier": item["tier"],
                "explanation": item["explanation"],
            }
            for item in candidates
        ],
    }


def switch_candidate(conn: sqlite3.Connection, job_id: int, certificate_id: int) -> dict:
    candidate = conn.execute(
        "SELECT * FROM match_candidates WHERE job_id = ? AND certificate_id = ?", (job_id, certificate_id)
    ).fetchone()
    if not candidate:
        raise ValueError("That certificate is not a compatible candidate for this job")
    draft_row = conn.execute(
        "SELECT * FROM certificate_drafts WHERE job_id = ? ORDER BY version DESC LIMIT 1", (job_id,)
    ).fetchone()
    cert = conn.execute("SELECT * FROM certificates WHERE id = ?", (certificate_id,)).fetchone()
    draft = json.loads(draft_row["draft_json"])
    draft["historical"].update(
        {
            "source_certificate_id": cert["id"],
            "source_ulr": cert["ulr"],
            "source_pages": f"{cert['page_start']}-{cert['page_end']}",
            "source_calibration_date": cert["calibration_date"],
            "confidence_score": candidate["score"],
            "match_explanation": json.loads(candidate["explanation_json"]),
            "reference_standard": cert["calibration_reference_standard"],
            "master_equipment": json.loads(cert["master_equipment_json"] or "[]"),
        }
    )
    now = now_iso()
    conn.execute("UPDATE match_candidates SET selected = (certificate_id = ?) WHERE job_id = ?", (certificate_id, job_id))
    conn.execute(
        "UPDATE calibration_jobs SET matched_certificate_id = ?, updated_at = ? WHERE id = ?",
        (certificate_id, now, job_id),
    )
    conn.execute(
        "UPDATE certificate_drafts SET draft_json = ?, updated_at = ? WHERE id = ?",
        (json.dumps(draft), now, draft_row["id"]),
    )
    conn.execute(
        """
        UPDATE field_provenance
        SET source_certificate_id = ?, source_ulr = ?, source_page = ?, confidence = ?
        WHERE job_id = ? AND source_type = 'historical'
        """,
        (cert["id"], cert["ulr"], cert["page_start"], candidate["score"], job_id),
    )
    return {"job_id": job_id, "status": "under_review", "draft": draft}


def reject_job(conn: sqlite3.Connection, job_id: int, actor: str, notes: str = "") -> None:
    job = conn.execute("SELECT status FROM calibration_jobs WHERE id = ?", (job_id,)).fetchone()
    if not job:
        raise ValueError("Calibration job not found")
    if job["status"] == "approved":
        raise ValueError("An approved certificate cannot be rejected")
    now = now_iso()
    conn.execute("UPDATE calibration_jobs SET status = 'rejected', updated_at = ? WHERE id = ?", (now, job_id))
    conn.execute("UPDATE certificate_drafts SET status = 'rejected', updated_at = ? WHERE job_id = ?", (now, job_id))
    conn.execute(
        "INSERT INTO approval_events(job_id, action, actor, notes, created_at) VALUES (?, 'rejected', ?, ?, ?)",
        (job_id, actor, clean(notes), now),
    )


def load_job_for_approval(conn: sqlite3.Connection, job_id: int) -> tuple[sqlite3.Row, dict]:
    job = conn.execute("SELECT * FROM calibration_jobs WHERE id = ?", (job_id,)).fetchone()
    if not job:
        raise ValueError("Calibration job not found")
    if job["status"] != "under_review":
        raise ValueError("Only a draft under review can be approved")
    draft_row = conn.execute(
        "SELECT * FROM certificate_drafts WHERE job_id = ? ORDER BY version DESC LIMIT 1", (job_id,)
    ).fetchone()
    return job, json.loads(draft_row["draft_json"])


def mark_approved(conn: sqlite3.Connection, job_id: int, actor: str) -> None:
    now = now_iso()
    conn.execute(
        "UPDATE calibration_jobs SET status = 'approved', approved_by = ?, approved_at = ?, updated_at = ? WHERE id = ?",
        (actor, now, now, job_id),
    )
    conn.execute("UPDATE certificate_drafts SET status = 'approved', updated_at = ? WHERE job_id = ?", (now, job_id))
    conn.execute(
        "INSERT INTO approval_events(job_id, action, actor, created_at) VALUES (?, 'approved', ?, ?)",
        (job_id, actor, now),
    )
