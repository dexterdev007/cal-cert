import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

import app
from instrument_workflow import create_draft, list_configurations, search_instruments


class InstrumentWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.original_db = app.DB_PATH
        self.original_output = app.OUTPUT_DIR
        app.DB_PATH = root / "calcert.sqlite3"
        app.OUTPUT_DIR = root / "pdf"
        app.OUTPUT_DIR.mkdir()
        shutil.copy2(self.original_db, app.DB_PATH)
        app.init_db()

    def tearDown(self):
        app.DB_PATH = self.original_db
        app.OUTPUT_DIR = self.original_output
        self.temp_dir.cleanup()

    def test_instrument_driven_draft_requires_approval(self):
        with app.db() as conn:
            suggestions = search_instruments(conn, "Tachometer")
            instrument = next(item for item in suggestions if item["name"] == "Tachometer")
            configurations = list_configurations(conn, instrument["id"])
            config = configurations[0]

            measurements = {}
            for section in config["measurement_schema"]:
                for row in section["rows"]:
                    if row["is_summary"]:
                        continue
                    for column in section["columns"]:
                        if column["kind"] == "measurement":
                            key = f"s{section['index']}.r{row['index']}.{column['key']}"
                            measurements[key] = "10.0, 10.1, 9.9"

            before = conn.execute("SELECT count(*) FROM generated_certificates").fetchone()[0]
            result = create_draft(
                conn,
                {
                    "configuration_id": config["id"],
                    "job_number": "TEST-INSTRUMENT-001",
                    "client_name": "Workflow Test Client",
                    "client_address": "Test address",
                    "serial_number": "TEST-SERIAL",
                    "calibration_date": "2026-06-29",
                    "next_calibration_date": "2027-06-29",
                    "issue_date": "2026-06-29",
                    "environment": {"temperature": "23", "humidity": "50", "pressure": "1013"},
                    "measurements": measurements,
                },
            )
            after = conn.execute("SELECT count(*) FROM generated_certificates").fetchone()[0]

        self.assertEqual(result["status"], "under_review")
        self.assertEqual(before, after)
        self.assertGreater(len(result["candidates"]), 0)
        self.assertTrue(result["draft"]["uncertainty"]["calculations"])
        first_result_row = result["draft"]["result_sections"][0]["rows"][0]
        self.assertIn("current_measurement", first_result_row["provenance"])
        self.assertIn("calculated", first_result_row["provenance"])

        approved = app.approve_instrument_job(result["job_id"])
        self.assertEqual(approved["status"], "approved")
        self.assertTrue((app.OUTPUT_DIR / f"{approved['certificate_number']}.pdf").exists())
        with sqlite3.connect(app.DB_PATH) as conn:
            status = conn.execute("SELECT status FROM calibration_jobs WHERE id = ?", (result["job_id"],)).fetchone()[0]
        self.assertEqual(status, "approved")


if __name__ == "__main__":
    unittest.main()
