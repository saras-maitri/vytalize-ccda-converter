# EMR Text → C-CDA Converter

Convert encounter-grouped EMR text dumps into C-CDA (Continuity of Care Document)
XML, then render the results into a browser-viewable folder.

The pipeline is two scripts:

| Script | Purpose |
|--------|---------|
| `converter.py` | EMR `.txt` → C-CDA `.xml` (one document per date of service + a longitudinal patient summary) |
| `make_viewer.py` | C-CDA `.xml` → viewable HTML (attaches an XSL stylesheet and pre-renders standalone HTML) |

---

## Requirements

- Python 3.8+
- `converter.py` (local mode) uses **only the standard library** — no dependencies.
- S3 mode needs `boto3`.
- `make_viewer.py` uses `lxml` when available (to apply the XSL), and falls back
  to a pure-stdlib renderer if `lxml` isn't installed.

### Setup

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install lxml        # optional: nicer HTML rendering + XSL transform
.venv/bin/python -m pip install boto3       # only if using S3 mode
```

---

## 1. Convert EMR text to C-CDA

```bash
# local directories
.venv/bin/python converter.py --input ./input --output ./output

# S3 (both must be S3 URIs)
.venv/bin/python converter.py --input s3://emr-input/ --output s3://ccda-output/
```

### Input format

Plain-text EMR data grouped into encounters. Each encounter starts with a header
line and is followed by `<KEY> <value>` fields:

```
Encounter 514139 - PHARMACY - 2023-07-17
RX Suboxone 8 mg-2 mg sublingual tablet ( take 2 tab under tongue every day )
...
```

Field-name matching is case/whitespace tolerant, so documents that follow this
general shape parse even if they differ in detail from one exact vendor export.

Input filenames of the form `emr_<member_id>_<timestamp>.txt` set the member id
used in output names (falls back to the file stem otherwise).

### Output layout

```
output/
  <member>_<YYYYMMDD>_<PROJECT_ID>.none          # marker file
  <member>_<YYYYMMDD>_<PROJECT_ID>/              # one folder per input
    <member>_<dateOfService>_<PROJECT_ID>.xml    # one VISIT document per date of service
    ...
    <member>_summary_<PROJECT_ID>.xml            # one longitudinal SUMMARY document
```

- **Visit documents** (one per date of service): Encounters + diagnoses, and the
  per-visit sections that carry data — Vital Signs, Medications, and clinical Notes.
- **Summary document** (longitudinal): Problems, Allergies, Immunizations,
  Procedures, Results, Social History, Family History, and Insurance — whichever
  carry data.

`PROJECT_ID` and the output-date convention are set near the top of `converter.py`.

### Content retention

The converter is built to avoid silently dropping clinical content:

- Structured extractors capture Problems (with provider comments), Medications,
  Vitals, Immunizations, Allergies, Procedures, Social & Family History, Payers,
  and coded diagnoses.
- Clinical notes (HPI, Physical Exam, Assessment & Plan, Review of Systems,
  Discussion, Chief Complaint, PMH, Screening, Follow-up, Pharmacy) are carried
  from whichever source field(s) contain them.
- A **safety net** runs after each visit document is assembled: it scans every
  source field and carries any free-text prose not already present in the
  document into the Notes section — so alternative note sources and partially
  parsed blocks aren't lost.
- Embedded HTML/JS chrome is stripped from note text (handles truncated/malformed
  tags), while clinical text such as `BP <120` is preserved.

Non-clinical data is intentionally not carried: internal IDs, timestamps, action
codes, billing IDs, table chrome (NDC/route/manufacturer), staff usernames, and
UI markup.

---

## 2. Render C-CDA to viewable HTML

```bash
.venv/bin/python make_viewer.py --input ./output --output ./viewable-output
```

Produces, mirroring the input folder structure:

```
viewable-output/
  index.html                     # links every rendered document
  <folder>/
    ccda.xsl                     # the C-CDA → HTML stylesheet
    <name>.xml                   # copy with <?xml-stylesheet ... ?> attached
    <name>.html                  # standalone rendered HTML (double-click to view)
```

Two rendering paths are provided because browsers (Chrome) block XSLT on
`file://` URLs:

- **`<name>.html`** — the same `ccda.xsl` applied server-side, so it opens by
  double-click in any browser.
- **`<name>.xml` + `ccda.xsl`** — for live in-browser XSLT when served over HTTP
  or opened in Firefox.

### View it

```bash
# open the index in the default browser
xdg-open   ./viewable-output/index.html      # Linux
open       ./viewable-output/index.html      # macOS
explorer.exe "$(wslpath -w ./viewable-output/index.html)"   # WSL

# or serve over HTTP (also enables the live XML + XSL rendering)
cd viewable-output && python3 -m http.server 8000   # then browse http://localhost:8000/
```

---

## End-to-end

```bash
.venv/bin/python converter.py   --input ./input  --output ./output
.venv/bin/python make_viewer.py --input ./output --output ./viewable-output
# open ./viewable-output/index.html
```

---

## Repository layout

```
converter.py                     # EMR text → C-CDA XML
make_viewer.py                   # C-CDA XML → viewable HTML
input/                           # place EMR .txt dumps here
output/                          # generated C-CDA XML (git-ignored)
viewable-output/                 # generated HTML viewer (git-ignored)
example-ccda.xml                 # reference C-CDA (expected-output sample)
```
