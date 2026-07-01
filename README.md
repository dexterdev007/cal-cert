# CalCert - Calibration Certificate Automation

CalCert is a local prototype for automating calibration certificate generation from historical certificate PDFs. Admins upload old certificates, the system extracts and stores their instrument data, and users generate new certificates by selecting an existing instrument configuration and entering only the current test measurements.

## What It Does

- Admin login for uploading historical calibration certificate PDFs.
- PDF extraction into a searchable SQLite-backed historical database.
- Instrument-driven certificate workflow with autocomplete suggestions.
- Configuration locking from historical data: manufacturer, model, range, least count, procedure, and calibration points are selected from existing records.
- Dynamic measurement forms based on the selected instrument/configuration.
- Uncertainty calculation from the current user-entered measurements.
- Certificate review and approval flow.
- Output PDF generated using the matched historical certificate PDF as the visual template, preserving logo, layout, borders, tables, notes, and QR placement.

## Local Run

```bash
python3 app.py
```

Then open:

```text
http://127.0.0.1:8000
```

## Demo Logins

```text
Admin
Email: admin@calcert.local
Password: admin123

User
Email: engineer@calcert.local
Password: user123
```

## Workflow

1. Admin uploads historical certificate PDFs.
2. The backend extracts certificate metadata, instrument details, calibration tables, and source document references.
3. The extracted data is stored in `data/calcert.sqlite3`.
4. User starts typing an instrument name.
5. The UI shows autocomplete suggestions from the historical database.
6. User selects an existing instrument and one valid historical configuration.
7. The frontend generates the correct measurement form for that instrument.
8. User enters current measurement values, date, environment, and job details.
9. Backend calculates uncertainty using the selected instrument model.
10. System creates a draft certificate for review.
11. After approval, the output PDF is generated from the matched historical PDF template with updated values.

## Project Structure

```text
.
├── app.py                         # HTTP server, auth, APIs, PDF upload/extraction, PDF output rendering
├── instrument_workflow.py         # Instrument catalog, dynamic forms, matching, uncertainty, draft logic
├── static/
│   ├── index.html                 # Admin and user UI
│   ├── app.js                     # Frontend workflow, autocomplete, dynamic measurement form
│   └── styles.css                 # Interface styling
├── data/
│   ├── calcert.sqlite3            # Local SQLite database
│   └── uploads/                   # Uploaded historical certificate PDFs
├── output/
│   └── pdf/                       # Generated certificate PDFs
├── docs/                          # Architecture and workflow notes
└── tests/
    └── test_instrument_workflow.py
```

## Core Logic

The system is instrument-driven. Users cannot freely invent manufacturer, model, range, least count, procedure, or calibration points. Those values are loaded from historical certificate data uploaded by the admin.

When a certificate is generated, CalCert chooses the best matching historical certificate/configuration, uses the user-entered current measurements for live values and uncertainty, and copies unchanged static fields from the historical source. The final PDF is not a newly designed report; it is rendered over the original historical PDF template so the design remains the same.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## Contributors

- [dexterdev007](https://github.com/dexterdev007)
- [The-Atul-Pathak](https://github.com/The-Atul-Pathak)
