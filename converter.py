#!/usr/bin/env python3
"""
convert-emr-to-ccda.py
==============
Batch-convert encounter-grouped EMR text dumps into C-CDA
(Continuity of Care Document) XML.

The input is plain-text EMR data grouped into encounters: each encounter is
introduced by an "Encounter <id> - <TYPE> - <date>" header line, followed by
"<KEY> <value>" fields. The converter recognizes a range of common field names
and is tolerant of casing/whitespace, so it handles documents that follow this
general shape -- not one exact vendor export. Fields it doesn't recognize are
not silently dropped: any with substantial free text are carried into the Notes
section so no narrative is lost.

Reads every *.txt under the --input location, converts each to C-CDA, and writes
the result (same relative path, .xml extension) under the --output location.
Both locations may be S3 URIs (s3://bucket/prefix/) or local directory paths.

Usage:
    python3 convert-emr-to-ccda.py \\
        --input s3://emr-input/ \\
        --output s3://ccda-output/

    # local directories also work (handy for testing):
    python3 convert-emr-to-ccda.py --input ./input --output ./out

S3 mode requires boto3 (`pip install boto3`) and AWS credentials in the
environment (env vars, shared config, or an instance/role profile).
"""

import argparse
import html
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

# --------------------------------------------------------------------------- #
# Namespaces / code systems
# --------------------------------------------------------------------------- #
V3 = "urn:hl7-org:v3"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
SDTC = "urn:hl7-org:sdtc"
ET.register_namespace("", V3)
ET.register_namespace("xsi", XSI)
ET.register_namespace("sdtc", SDTC)

CS = {
    "LOINC": "2.16.840.1.113883.6.1",
    "SNOMED": "2.16.840.1.113883.6.96",
    "ICD10": "2.16.840.1.113883.6.90",
    "RXNORM": "2.16.840.1.113883.6.88",
    "NDC": "2.16.840.1.113883.6.69",
    "CVX": "2.16.840.1.113883.12.292",
    "CPT": "2.16.840.1.113883.6.12",
    "ActCode": "2.16.840.1.113883.5.6",
    "AdminGender": "2.16.840.1.113883.5.1",
    "Confidentiality": "2.16.840.1.113883.5.25",
    "RoleClass": "2.16.840.1.113883.5.4",
}
ROOT = "2.16.840.1.113883.19.5"   # generic local id root

# --------------------------------------------------------------------------- #
# Output layout: one folder per input, named by the original XML naming
# convention (minus the extension):
#     <member_id>_<OUTPUT_DATE>_<PROJECT_ID>          (the folder)
#       <member_id>_<OUTPUT_DATE>_<PROJECT_ID>.none   (marker, same name)
#       <member_id>_<encounterDate>_<PROJECT_ID>.xml  (one C-CDA per encounter)
# member_id is parsed from the input filename (emr_<member_id>_<timestamp>.txt);
# OUTPUT_DATE mirrors the DB's to_char(current_date, 'YYYYMMDD').
# --------------------------------------------------------------------------- #
PROJECT_ID = "22"   # <-- set to your project_id
OUTPUT_DATE = datetime.now().strftime("%Y%m%d")   # today, mirrors current_date

# Illustrative terminology maps (extend with a real terminology service).
MED_CODES = {
    "ibuprofen 800": ("RXNORM", "197806", "Ibuprofen 800 MG Oral Tablet"),
    "atorvastatin 80": ("RXNORM", "259255", "Atorvastatin 80 MG Oral Tablet"),
}
VACCINE_CVX = {
    "influenza, injectable, quadrivalent": ("150", "Influenza, injectable, quadrivalent"),
    "zoster recombinant": ("187", "Zoster vaccine recombinant"),
}

VITAL_MAP = {
    "VITALS.BLOODPRESSURE.SYSTOLIC": ("8480-6", "Systolic blood pressure", "mm[Hg]", None),
    "VITALS.BLOODPRESSURE.DIASTOLIC": ("8462-4", "Diastolic blood pressure", "mm[Hg]", None),
    "VITALS.BMI": ("39156-5", "Body mass index", "kg/m2", None),
    "VITALS.O2SATURATION": ("2708-6", "Oxygen saturation", "%", None),
    "VITALS.PULSE.RATE": ("8867-4", "Heart rate", "/min", None),
    "VITALS.RESPIRATIONRATE": ("9279-1", "Respiratory rate", "/min", None),
    "VITALS.TEMPERATURE": ("8310-5", "Body temperature", "[degF]", None),
    "VITALS.WEIGHT": ("29463-7", "Body weight", "kg", lambda v: round(v / 1000.0, 2)),
    "VITALS.HEIGHT": ("8302-2", "Body height", "cm", None),
    "VITALS.BODYSURFACEAREA": ("3140-1", "Body surface area", "m2", None),
    "VITALS.PAINSCALE": ("72514-3", "Pain severity (0-10)", "{score}", None),
}

SIMPLE_KEYS = {
    "RX", "DIAGNOSIS", "DIAGNOSISUPDATED", "AIRLOCKYN", "CLOSEFAILED",
    "CLAIMCREATED", "HPITOROSTEXT", "HPITOROSYN",
}
KEY_PREFIXES = (
    "FROZENSECTIONHTML_", "FROZENENCOUNTERHTML", "FROZENEBILLINGSLIP",
    "ENCOUNTER_SUMMARY_", "ENCOUNTER_PREP_YN", "EXAMFREETEXT_", "EBILLINGSLIP_",
    "HPI_", "HPILOCAL", "HPITOROS", "VITALS.", "INTAKE.", "REVIEWED.",
    "PATIENTALLERGY.", "PATIENTPROBLEMLIST.", "DIAGNOSISORDERS.", "DIAGNOSIS",
    "ASSESSMENT.", "DISCUSSION.", "CLINICAL", "AIRLOCK", "LETTERS.",
    "PATIENTCARESUMMARY", "CLOSEFAILED", "CLAIMCREATED", "RX ",
)

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def parse_date(s):
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y%m%d%H%M%S", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    m = re.search(r"\d{4}-\d{2}-\d{2}", s) or re.search(r"\d{1,2}/\d{1,2}/\d{2,4}", s)
    return parse_date(m.group(0)) if m else None


def ts(dt, full=False):
    if dt is None:
        return None
    return dt.strftime("%Y%m%d%H%M%S") if full else dt.strftime("%Y%m%d")


def iso(dt):
    return dt.strftime("%Y-%m-%d") if dt else ""


def sub(parent, tag, text=None, **attrs):
    e = ET.SubElement(parent, f"{{{V3}}}{tag}")
    for k, v in attrs.items():
        if v is None:
            continue
        e.set(f"{{{XSI}}}type" if k == "xsi_type" else k, str(v))
    if text is not None:
        e.text = str(text)
    return e


def tid(parent, root, ext=None):
    return sub(parent, "templateId", root=root, extension=ext)


def med_code(name):
    low = name.lower()
    for frag, code in MED_CODES.items():
        if frag in low:
            return code
    return None


def cvx_code(name):
    low = name.lower()
    for frag, code in VACCINE_CVX.items():
        if frag in low:
            return code
    return None


def split_person(s):
    """Split a provider string into (given, family, suffix) best-effort."""
    if not s:
        return None, None, None
    suffix = None
    if "," in s:
        s, suffix = [p.strip() for p in s.split(",", 1)]
    parts = s.split()
    if len(parts) >= 2:
        return parts[0], " ".join(parts[1:]), suffix
    return s, None, suffix


# --------------------------------------------------------------------------- #
# Parse the encounter-grouped dump
# --------------------------------------------------------------------------- #
# "Encounter <id> - <TYPE> - <date>". Tolerant of casing and of an id/type/date
# that aren't strictly numeric/uppercase/ISO, so documents that follow the shape
# but differ in detail still parse. The trailing " - <date>$" anchor keeps it
# from matching ordinary prose. parse_date() handles the date variants.
ENC_RE = re.compile(
    r"^Encounter\s+(\S+)\s*-\s*([\w /-]+?)\s*-\s*"
    r"(\d{1,4}[-/]\d{1,2}[-/]\d{1,4})\s*$",
    re.IGNORECASE,
)


class Encounter:
    def __init__(self, enc_id, enc_type, date):
        self.id, self.type, self.date = enc_id, enc_type, date
        self.fields = []

    def get(self, key):
        # case-insensitive: documents vary in field-name casing
        kl = key.lower()
        return [v for k, v in self.fields if k.lower() == kl]

    def first(self, key):
        v = self.get(key)
        return v[0] if v else None


_SIMPLE_KEYS_LC = {k.lower() for k in SIMPLE_KEYS}
_KEY_PREFIXES_LC = tuple(p.lower() for p in KEY_PREFIXES)


def line_starts_field(line):
    if not line or line[0].isspace():
        return False
    tok = line.split(None, 1)[0]
    if ":" in tok:
        return False
    if tok.lower() in _SIMPLE_KEYS_LC:
        return True
    return line.lower().startswith(_KEY_PREFIXES_LC)


def split_field(line):
    parts = line.split(None, 1)
    key = parts[0]
    rest = parts[1] if len(parts) > 1 else ""
    rest = re.sub(r"^\d+\s*-\s*", "", rest)
    rest = re.sub(r"^-\s*", "", rest)
    return key, rest


def service_period(encounters):
    """Span of care the document summarizes: (earliest, latest) encounter date.

    Derived from the encounter dates -- the most reliable date in the dump,
    since every encounter is introduced by an 'Encounter <id> - <TYPE> - <DATE>'
    header line (see ENC_RE) and that date is parsed into Encounter.date.
    Returns (low, high) datetimes, or (None, None) if no encounter carries a
    date. Generic: it spans whatever encounters a given dump contains.
    """
    dates = sorted(e.date for e in encounters if e.date)
    return (dates[0], dates[-1]) if dates else (None, None)


def parse_dump(text):
    encounters, cur, pending, buf = [], None, None, []

    def flush():
        nonlocal pending, buf
        if cur is not None and pending is not None:
            cur.fields.append((pending, "\n".join(buf).strip()))
        pending, buf = None, []

    for raw in text.splitlines():
        m = ENC_RE.match(raw)
        if m:
            flush()
            cur = Encounter(m.group(1), m.group(2), parse_date(m.group(3)))
            encounters.append(cur)
        elif cur is None:
            continue
        elif line_starts_field(raw):
            flush()
            pending, val = split_field(raw)
            buf = [val]
        else:
            buf.append(raw)
    flush()
    return encounters


# --------------------------------------------------------------------------- #
# Extractors
# --------------------------------------------------------------------------- #
def extract_patient(encounters):
    pat = {"family": None, "given": None, "sex": None, "dob": None,
           "mrn": None, "provider": None, "org": None}
    for enc in sorted(encounters, key=lambda e: e.date or datetime.min, reverse=True):
        block = enc.first("FROZENSECTIONHTML_Patient") or enc.first("FROZENENCOUNTERHTML")
        if not block:
            continue
        lines = [l.strip() for l in block.splitlines()]
        for i, l in enumerate(lines):
            if l == "Name" and i + 1 < len(lines):
                m = re.match(r"^(.*?),\s*(.*?)\s*\((\d+)\s*yo,\s*([MF])\)", lines[i + 1])
                if m:
                    pat["family"], pat["given"], pat["sex"] = m.group(1), m.group(2), m.group(4)
            elif l == "DOB" and i + 1 < len(lines):
                pat["dob"] = parse_date(lines[i + 1])
            elif l == "Provider" and i + 1 < len(lines):
                pat["provider"] = lines[i + 1]
            elif l in ("Service Dept.", "Service Department") and i + 1 < len(lines):
                pat["org"] = lines[i + 1]
        m = re.search(r"ID#\s*(\d+)", block)
        if m:
            pat["mrn"] = m.group(1)
        if pat["family"]:
            break
    return pat


def extract_problems(encounters):
    seen = {}
    # NOTE on status: this format carries NO structured active/resolved flag.
    # A second date after onset (e.g. "Onset: 08/03/2020 - 07/25/2022") is a
    # review/comment date -- the line continues "- Comments only - ...", not a
    # resolution. We deliberately do NOT parse it as a resolved date (doing so
    # wrongly marked actively-managed problems like Hyperlipidemia and Essential
    # hypertension as Resolved). Absent a real status signal, every problem
    # defaults to Active. Wire in an explicit problem status here if/when a
    # document exposes one.
    # The text between the onset date and "Problem Code:" carries the review
    # date(s), status word, authoring provider, and the free-text clinical
    # comment (e.g. "... - Unchanged - Ruth James MD - Recommend decrease dosing
    # and avoid trigger foods"). We capture that whole span as `detail` so this
    # provider narrative is retained rather than discarded. Distinct details for
    # the same problem (it can be commented across multiple visits) are all kept.
    pat = re.compile(
        r"^(?P<name>.+?)\s*-\s*Onset:\s*(?P<onset>\d{2}/\d{2}/\d{4})"
        r"(?P<detail>.*?)\s*Problem Code:\s*(?P<code>[^;]+);\s*Problem Code Type:\s*(?P<ctype>[^;]+);"
    )
    for enc in encounters:
        block = enc.first("FROZENSECTIONHTML_ProblemList")
        if not block:
            continue
        for line in block.splitlines():
            m = pat.search(line)
            if not m:
                continue
            code = m.group("code").strip()
            key = code or m.group("name").strip().lower()
            detail = re.sub(r"\s+", " ", m.group("detail")).strip(" -")
            entry = seen.get(key)
            if entry is None:
                entry = seen[key] = {
                    "name": m.group("name").strip(),
                    "onset": parse_date(m.group("onset")),
                    "resolved": None,   # no reliable status in source -> Active
                    "code": code,
                    "ctype": m.group("ctype").strip(),
                    "details": [],
                }
            if detail and detail not in entry["details"]:
                entry["details"].append(detail)
    return list(seen.values())


def extract_medications(encounters):
    rx = re.compile(r"^(?P<med>.+?)\s*\(\s*(?P<sig>.+?)\s*\)(?:\s*Filled\s*(?P<date>\d{4}-\d{2}-\d{2}))?\s*$")
    meds = {}
    for enc in encounters:
        for val in enc.get("RX"):
            m = rx.match(val.replace("\n", " ").strip())
            if not m:
                continue
            name = re.sub(r"\s+", " ", m.group("med").strip())
            date = parse_date(m.group("date")) or enc.date
            key = name.lower()
            if key not in meds or (date and meds[key]["date"] and date > meds[key]["date"]):
                meds[key] = {"name": name, "sig": m.group("sig").strip(), "date": date}
    return list(meds.values())


_VITAL_MAP_LC = {k.lower(): v for k, v in VITAL_MAP.items()}


def extract_vitals(encounters):
    out = []
    for enc in encounters:
        readings, used = [], set()
        for key, val in enc.fields:
            kl = key.lower()
            if kl in _VITAL_MAP_LC and kl not in used:
                try:
                    num = float(val.split()[0])
                except (ValueError, IndexError):
                    continue
                loinc, disp, unit, xform = _VITAL_MAP_LC[kl]
                readings.append({"loinc": loinc, "display": disp, "unit": unit,
                                 "value": xform(num) if xform else num})
                used.add(kl)
        if readings and enc.date:
            out.append({"date": enc.date, "readings": readings})
    return out


def extract_immunizations(encounters):
    vacc = {}
    date_re = re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4}$")
    # A vaccine record is a name line immediately followed by an administration
    # date. Requiring "name-keyword AND next-line-is-a-date" captures every
    # vaccine family in the source (COVID, influenza, Tdap, Hep A/B, ...) while
    # naturally rejecting the manufacturer/route/lot/vaccinator/exp-date lines
    # that also sit next to dates (none of them carry a vaccine keyword).
    # Illustrative -- back with a real vaccine terminology service for full coverage.
    kw = (
        "vaccine", "vaccin", "mrna", "sars", "covid",
        "influenza", "zoster", "shingrix",
        "tdap", "dtap", "tetanus", "diphtheria", "pertussis",
        "hep", "hepatitis",
        "mmr", "measles", "mumps", "rubella",
        "varicella", "varivax",
        "pneumo", "pneumococcal", "prevnar", "pcv", "ppsv",
        "meningococcal", "menactra", "menveo", "bexsero",
        "hpv", "gardasil",
        "polio", "ipv",
        "rsv", "abrysvo", "arexvy",
        "rotavirus", "rotateq", "rotarix",
        "haemophilus", "hib",
        "cholera", "rabies", "typhoid", "anthrax", "smallpox", "yellow fever",
    )
    for enc in encounters:
        block = enc.first("FROZENSECTIONHTML_VaccineList")
        if not block:
            continue
        lines = [l.strip() for l in block.splitlines() if l.strip()]
        for i, l in enumerate(lines):
            if any(k in l.lower() for k in kw) and i + 1 < len(lines) and date_re.match(lines[i + 1]):
                date = parse_date(lines[i + 1])
                lot = None
                for look in lines[i + 2:i + 6]:
                    lm = re.match(r"^([0-9A-Z]{5,})$", look)
                    if lm:
                        lot = lm.group(1)
                        break
                vacc.setdefault((l.lower(), ts(date)), {"name": l, "date": date, "lot": lot})
    return list(vacc.values())


def extract_payers(encounters):
    pay = {}
    # Each payer line is followed by its OWN "Insurance # :" line, e.g.:
    #     Med Primary: MEDICARE-NH (MEDICARE)
    #     Insurance # : <id-A>
    #     Med : MEDICARE-NH - PART A - RHC-FQHC (MEDICARE)
    #     Insurance # : <id-B>
    # Scan line-by-line and pair each "Insurance #" with the payer immediately
    # above it. (A single block-wide search would attach the FIRST id to every
    # payer -- masked only when ids are redacted to identical values.)
    payer_re = re.compile(r"^Med (?:Primary|Secondary)?\s*:\s*([^\n(]+?)(?:\s*\(([^)]+)\))?\s*$")
    mid_re = re.compile(r"^Insurance #\s*:\s*(\S+)")
    for enc in encounters:
        for key in ("FROZENSECTIONHTML_Patient", "FROZENENCOUNTERHTML", "FROZENEBILLINGSLIP"):
            block = enc.first(key)
            if not block:
                continue
            current = None   # dedup-key of the payer awaiting its Insurance #
            for line in block.splitlines():
                line = line.strip()
                pm = payer_re.match(line)
                if pm:
                    current = None
                    name = pm.group(1).strip()
                    if not name or name.lower().startswith("no insurance"):
                        continue
                    current = name.lower()
                    pay.setdefault(current, {
                        "name": name,
                        "category": (pm.group(2) or "").strip(),
                        "member_id": None,
                    })
                    continue
                idm = mid_re.match(line)
                if idm and current is not None:
                    if pay[current]["member_id"] is None:   # don't clobber a known id
                        pay[current]["member_id"] = idm.group(1)
                    current = None
    return list(pay.values())


def extract_procedures(encounters):
    procs = []
    for enc in sorted(encounters, key=lambda e: e.date or datetime.min, reverse=True):
        block = enc.first("FROZENSECTIONHTML_SurgicalHistoryList")
        if block and "None recorded" not in block:
            for line in block.splitlines():
                m = re.search(r"([A-Za-z][A-Za-z ]+?)\s*-\s*(\d{2}/\d{2}/\d{4})", line)
                if m:
                    procs.append({"name": m.group(1).strip(), "date": parse_date(m.group(2))})
            if procs:
                break
    return procs


def _latest_block(encounters, key, skip_if=()):
    """Return the most recent encounter's value for `key`, skipping empty/no-data blocks."""
    for enc in sorted(encounters, key=lambda e: e.date or datetime.min, reverse=True):
        block = enc.first(key)
        if not block:
            continue
        low = block.lower()
        if any(s in low for s in skip_if):
            continue
        return block
    return None


def extract_allergies(encounters):
    """Parse allergy substances/reactions.

    NOTE: the sample dump only carries a 'Reviewed Allergies' flag with no
    substances, so this returns []. The parser assumes a real dump lists each
    allergen as 'Substance - Reaction' (one per line) -- validate on real data.
    """
    block = _latest_block(encounters, "FROZENSECTIONHTML_AllergyList")
    if not block:
        return []
    out = []
    for line in block.splitlines():
        line = line.strip(" *")
        if not line:
            continue
        low = line.lower()
        if low.startswith(("reviewed allergies", "allergies not reviewed",
                           "no known", "nka", "name", "reaction")):
            continue
        substance, reaction = line, None
        if " - " in line:
            substance, reaction = [p.strip() for p in line.split(" - ", 1)]
        out.append({"substance": substance, "reaction": reaction})
    return out


def extract_lab_results(encounters):
    """Parse lab result values.

    NOTE: the sample dump's FROZENSECTIONHTML_LabResults is empty, so this
    returns []. The parser assumes a real dump lists results as
    'Test Name  value unit  (ref range)' -- validate on real data.
    """
    block = _latest_block(encounters, "FROZENSECTIONHTML_LabResults")
    if not block:
        return []
    out = []
    row = re.compile(r"^(?P<name>[A-Za-z][\w /%-]+?)[:\s]+(?P<val>-?\d+(?:\.\d+)?)\s*(?P<unit>[A-Za-z%/]+)?")
    for line in block.splitlines():
        m = row.match(line.strip())
        if m:
            out.append({"name": m.group("name").strip(),
                        "value": m.group("val"),
                        "unit": (m.group("unit") or "").strip()})
    return out


SMOKING = {
    "never": ("266919005", "Never smoked tobacco"),
    "ex-smoker": ("8517006", "Ex-smoker"),
    "former": ("8517006", "Ex-smoker"),
    "current every day": ("449868002", "Current every day smoker"),
    "current some day": ("428041000124106", "Current some day smoker"),
    "current": ("77176002", "Smoker"),
}


def extract_social_history(encounters):
    block = _latest_block(encounters, "FROZENSECTIONHTML_SocialHistoryList")
    if not block:
        return []
    items, smoking = [], None
    for line in block.splitlines():
        m = re.match(r"^(.*\?):\s*(.+)$", line.strip())
        if not m:
            continue
        q, a = m.group(1).strip(), m.group(2).strip()
        items.append({"q": q, "a": a})
        if "smoked tobacco" in q.lower():
            for frag, code in SMOKING.items():
                if frag in a.lower():
                    smoking = {"answer": a, "code": code}
                    break
    if smoking:
        items.insert(0, {"q": "__smoking__", "a": smoking})
    return items


RELATION = {
    # more specific keys first -- longest containing match wins (see _match_relation),
    # so "maternal grandfather" beats a bare "grandfather".
    "maternal grandmother": "MGRMTH", "maternal grandfather": "MGRFTH",
    "paternal grandmother": "PGRMTH", "paternal grandfather": "PGRFTH",
    "grandmother": "GRMTH", "grandfather": "GRFTH", "grandparent": "GRPRN",
    "mother": "MTH", "father": "FTH", "parent": "PRN",
    "brother": "BRO", "sister": "SIS", "sibling": "SIB",
    "son": "SON", "daughter": "DAU", "child": "CHILD",
    "aunt": "AUNT", "uncle": "UNCLE", "cousin": "COUSN",
    "nephew": "NEPHEW", "niece": "NIECE",
}


def _match_relation(line):
    """Best-effort map a family-history relation label to (display, HL7 code).
    Handles maternal/paternal-qualified grandparents and labels that carry extra
    words (e.g. 'Maternal Grandfather (deceased)'). Returns (None, None) if no
    known relative word is present."""
    l = " ".join(line.lower().split())
    if l in RELATION:
        return line.strip(), RELATION[l]
    hits = [(k, v) for k, v in RELATION.items()
            if re.search(r"\b" + re.escape(k) + r"\b", l)]
    if hits:
        k, v = max(hits, key=lambda kv: len(kv[0]))   # longest phrase wins
        return line.strip(), v
    return None, None


def extract_family_history(encounters):
    block = _latest_block(encounters, "FROZENSECTIONHTML_FamilyHistoryList",
                          skip_if=("not reviewed",))
    if not block:
        return []
    out, lines = [], [l.strip() for l in block.splitlines()]
    for i, l in enumerate(lines):
        rel_disp, rel_code = _match_relation(l)
        if not rel_code:
            continue
        # condition may sit on the same line ("Relative - condition") or the next
        inline = re.sub(r"^.*?\bproblem\b\s*:?\s*", "", l, flags=re.I) if ":" in l else ""
        if " - " in l:
            inline = l.split(" - ", 1)[1].strip()
        cond = inline or (lines[i + 1].lstrip("-* ").strip() if i + 1 < len(lines) else "")
        cond = cond.strip("-* ").strip()
        if cond and not cond.lower().startswith(("onset", "reviewed", "none")):
            out.append({"relation": rel_disp, "rel_code": rel_code, "condition": cond})
    return out


# Clinical-note specs: (label, LOINC code, source fields by preference, authored-time field)
NOTE_SPECS = [
    ("History of Present Illness", "10164-2",
     ["FROZENSECTIONHTML_HPI_Templated", "EXAMFREETEXT_CLOB_HPI_Templated", "ENCOUNTER_SUMMARY_HPI"],
     "EXAMFREETEXT_CLOB_HPI_Templated_UPDATE"),
    ("Physical Exam", "29545-1",
     ["FROZENSECTIONHTML_PhysicalExam", "EXAMFREETEXT_CLOB_PhysicalExam", "ENCOUNTER_SUMMARY_PHYSICALEXAM"],
     "EXAMFREETEXT_CLOB_PhysicalExam_UPDATE"),
    ("Assessment and Plan", "51847-2",
     ["FROZENSECTIONHTML_AssessmentPlan", "ENCOUNTER_SUMMARY_ASSESSMENTPLAN", "ASSESSMENT.NOTECLOB"], None),
    ("Review of Systems", "10187-3", ["ENCOUNTER_SUMMARY_REVIEWOFSYSTEMS"], None),
    ("Discussion", "34109-9", ["DISCUSSION.NOTECLOB", "ENCOUNTER_SUMMARY_DISCUSSION"], None),
    ("Chief Complaint", "10154-3",
     ["FROZENSECTIONHTML_EncounterReason", "ENCOUNTER_SUMMARY_APPOINTMENTSNIPPET"], None),
    ("Past Medical History", "11348-0", ["FROZENSECTIONHTML_PastMedicalHistory"], None),
    ("Screening", "34109-9", ["ENCOUNTER_SUMMARY_SCREENING"], None),
    ("Follow-up / Plan", "18776-5", ["ENCOUNTER_SUMMARY_FOLLOWUP"], None),
    ("Pharmacy", "34109-9", ["FROZENSECTIONHTML_PatientPrescriptionProvider"], None),
]
NOTE_EMPTY = {"", "none recorded", "none recorded.", "n/a", "na", "none"}

# Field keys already consumed by a coded section or a NOTE_SPECS note. Anything
# NOT in here is a candidate for the unmapped-narrative catch-all below, so a
# document with field names we didn't anticipate doesn't silently lose content.
_HANDLED_KEYS = (
    {"DIAGNOSIS", "RX"}
    | set(VITAL_MAP)
    | {k for _, _, keys, upd in NOTE_SPECS for k in (keys + ([upd] if upd else []))}
    | {  # blocks read by the structured extractors
        "FROZENSECTIONHTML_Patient", "FROZENENCOUNTERHTML", "FROZENEBILLINGSLIP",
        "FROZENSECTIONHTML_ProblemList", "FROZENSECTIONHTML_VaccineList",
        "FROZENSECTIONHTML_SurgicalHistoryList", "FROZENSECTIONHTML_AllergyList",
        "FROZENSECTIONHTML_LabResults", "FROZENSECTIONHTML_SocialHistoryList",
        "FROZENSECTIONHTML_FamilyHistoryList",
    }
)
_HANDLED_KEYS_LC = {k.lower() for k in _HANDLED_KEYS}


def _clean_note(v):
    if not v:
        return None
    return v.strip() if v.strip().lower() not in NOTE_EMPTY else None


def _strip_tags(s):
    # Unescape first so double-escaped markup (e.g. "&lt;a onclick=...&gt;")
    # is also removed, then drop script blocks and HTML tags. EMR HTML dumps are
    # frequently truncated, so tags may be missing their closing ">"; the pattern
    # matches a tag whether or not it closes. "<" is only treated as a tag start
    # when followed by a letter or "/", so clinical text like "BP <120" survives.
    s = html.unescape(s or "")
    s = re.sub(r"(?is)<script.*?(?:</script>|$)", " ", s)
    s = re.sub(r"</?[A-Za-z][^<>]*>?", " ", s)
    return s


def _looks_like_prose(text):
    """True for substantial free-text narrative; False for ids, timestamps,
    codes, flags, and tiny/empty values. Vendor-agnostic -- judged by shape,
    not by field name."""
    t = re.sub(r"\s+", " ", _strip_tags(text)).strip()
    if len(t) < 40:
        return False
    if len(re.findall(r"[A-Za-z]{2,}", t)) < 8:
        return False
    letters = sum(c.isalpha() for c in t)
    digits = sum(c.isdigit() for c in t)
    return letters > digits          # reject id/timestamp/code-dominated values


def _humanize_key(k):
    """Turn a raw field key into a readable note caption."""
    k = re.sub(r"_(UPDATE|CLOB|SNIPPET|TEMPLATED)$", "", k, flags=re.I)
    k = re.sub(r"^(FROZENSECTIONHTML_|ENCOUNTER_SUMMARY_|EXAMFREETEXT_CLOB_)", "", k, flags=re.I)
    k = re.sub(r"[._]+", " ", k).strip()
    return k.title() or "Note"


def extract_unmapped_notes(encounters, seen_texts):
    """Catch-all: carry free-text fields we don't otherwise recognize into the
    Notes section so no narrative is lost on documents shaped differently from
    the samples. `seen_texts` holds normalized text already emitted as structured
    notes; anything contained in (or equal to) one of those is skipped as a
    duplicate/snippet."""
    out = []
    for enc in sorted(encounters, key=lambda e: e.date or datetime.min):
        for key, val in enc.fields:
            if key.lower() in _HANDLED_KEYS_LC:
                continue
            text = _strip_tags(val).strip()
            if not _looks_like_prose(text):
                continue
            norm = " ".join(text.split())
            if any(norm in s for s in seen_texts):   # already captured (or a subset)
                continue
            seen_texts.add(norm)
            out.append({"label": _humanize_key(key), "code": "34109-9",
                        "text": text, "date": enc.date, "authored": enc.date})
    return out


def extract_notes(encounters):
    """One note per (encounter, note type), picking the cleanest available source field."""
    notes = []
    for enc in sorted(encounters, key=lambda e: e.date or datetime.min):
        for label, code, keys, update_key in NOTE_SPECS:
            text = None
            for k in keys:
                vals = [t for t in (_clean_note(v) for v in enc.get(k)) if t]
                if vals:
                    text = max(vals, key=len)   # dedupe interim/final -> keep longest
                    break
            if not text:
                continue
            text = _strip_tags(text).strip()   # drop embedded HTML/JS chrome, keep the narrative
            if not text:
                continue
            authored = (parse_date(enc.first(update_key)) if update_key else None) or enc.date
            notes.append({"label": label, "code": code, "text": text,
                          "date": enc.date, "authored": authored})
    # drop exact duplicates (e.g. patient-level notes repeated across encounters)
    seen, uniq = set(), []
    seen_texts = set()
    for n in notes:
        norm = " ".join(n["text"].split())
        key = (n["label"], norm)
        if key not in seen:
            seen.add(key)
            seen_texts.add(norm)
            uniq.append(n)
    # catch-all: any unrecognized free-text field -> Notes (no silent drops)
    uniq.extend(extract_unmapped_notes(encounters, seen_texts))
    return uniq


def _doc_text_blob(doc):
    """All human-readable text already present in an assembled document, normalized
    to a single lowercase string. Used by the safety net to detect what content
    still needs carrying."""
    return " ".join(
        " ".join((el.text or "").split())
        for el in doc.iter() if el.text and el.text.strip()
    ).lower()


def leftover_notes(doc, encounters, already):
    """Safety net guaranteeing no free-text is lost. After a document is assembled,
    scan every source field: any prose value whose text is NOT already present in
    the document (nor in the notes about to be emitted) is carried into Notes.

    This backstops the structured extractors when they capture only part of a
    field (e.g. one of several alternative note sources, or the Q&A rows of a
    social-history block that also carries a free-text note)."""
    blob = _doc_text_blob(doc)
    seen = [" ".join(n["text"].split()).lower() for n in already]
    out = []
    for enc in sorted(encounters, key=lambda e: e.date or datetime.min):
        for key, val in enc.fields:
            text = _strip_tags(val).strip()
            if not _looks_like_prose(text):
                continue
            nt = " ".join(text.split()).lower()
            if nt in blob:                                   # already in the document
                continue
            if any(nt in s or s in nt for s in seen):        # already queued / superset
                continue
            seen.append(nt)
            out.append({"label": _humanize_key(key), "code": "34109-9",
                        "text": text, "date": enc.date, "authored": enc.date})
    return out


# --------------------------------------------------------------------------- #
# C-CDA assembly
# --------------------------------------------------------------------------- #
def build_header(doc, patient, now, service_low=None, service_high=None, doc_effective=None, doc_id=None):
    sub(doc, "realmCode", code="US")
    sub(doc, "typeId", root="2.16.840.1.113883.1.3", extension="POCD_HD000040")
    tid(doc, "2.16.840.1.113883.10.20.22.1.1", "2015-08-01")
    tid(doc, "2.16.840.1.113883.10.20.22.1.2", "2015-08-01")
    sub(doc, "id", root=ROOT, extension=doc_id or f"EMRDUMP-{ts(now, True)}")
    sub(doc, "code", code="34133-9", displayName="Summarization of Episode Note",
        codeSystem=CS["LOINC"], codeSystemName="LOINC")
    sub(doc, "title", "Continuity of Care Document")
    # Each document is a single encounter, so effectiveTime carries that
    # encounter's date of service (appointment date/time). Falls back to `now`
    # only if the encounter has no parseable date.
    if doc_effective:
        eff_full = bool(doc_effective.hour or doc_effective.minute or doc_effective.second)
        sub(doc, "effectiveTime", value=ts(doc_effective, eff_full))
    else:
        sub(doc, "effectiveTime", value=ts(now, True))
    sub(doc, "confidentialityCode", code="N", codeSystem=CS["Confidentiality"])
    sub(doc, "languageCode", code="en-US")

    rt = sub(doc, "recordTarget")
    pr = sub(rt, "patientRole")
    sub(pr, "id", root=ROOT, extension=patient["mrn"])
    p = sub(pr, "patient")
    nm = sub(p, "name", use="L")
    if patient["given"]:
        sub(nm, "given", patient["given"])
    if patient["family"]:
        sub(nm, "family", patient["family"])
    if patient["sex"]:
        sub(p, "administrativeGenderCode", code=patient["sex"], codeSystem=CS["AdminGender"])
    if patient["dob"]:
        sub(p, "birthTime", value=ts(patient["dob"]))

    au = sub(doc, "author")
    # author/time matches the encounter's date of service (the header
    # effectiveTime); falls back to generation time when no encounter date.
    if doc_effective:
        au_full = bool(doc_effective.hour or doc_effective.minute or doc_effective.second)
        sub(au, "time", value=ts(doc_effective, au_full))
    else:
        sub(au, "time", value=ts(now, True))
    aa = sub(au, "assignedAuthor")
    sub(aa, "id", root="2.16.840.1.113883.4.6")
    if patient["provider"]:
        g, f, suf = split_person(patient["provider"])
        ap = sub(aa, "assignedPerson")
        an = sub(ap, "name")
        if g:
            sub(an, "given", g)
        if f:
            sub(an, "family", f)
        if suf:
            sub(an, "suffix", suf)
    else:
        dev = sub(aa, "assignedAuthoringDevice")
        sub(dev, "manufacturerModelName", "convert-emr-to-ccda.py")
        sub(dev, "softwareName", "EMR Text Dump -> C-CDA Converter")

    cu = sub(doc, "custodian")
    rco = sub(sub(cu, "assignedCustodian"), "representedCustodianOrganization")
    sub(rco, "id", root="2.16.840.1.113883.4.6")
    sub(rco, "name", patient["org"] or "Healthcare Organization")

    # documentationOf/serviceEvent: the period of care this document covers
    # (the "date of service"). Required by the CCD template; the effectiveTime
    # interval spans the earliest..latest encounter date. Must appear after
    # custodian and before the body component per C-CDA header ordering.
    if service_low:
        se = sub(sub(doc, "documentationOf"), "serviceEvent", classCode="PCPR")
        et = sub(se, "effectiveTime")
        sub(et, "low", value=ts(service_low))
        sub(et, "high", value=ts(service_high or service_low))


def section(body, oids, code, code_disp, title):
    sec = sub(sub(body, "component"), "section")
    for root, ext in oids:
        tid(sec, root, ext)
    sub(sec, "code", code=code, displayName=code_disp, codeSystem=CS["LOINC"], codeSystemName="LOINC")
    sub(sec, "title", title)
    return sec


def narrative(sec, headers, rows):
    txt = sub(sec, "text")
    tbl = sub(txt, "table", border="1", width="100%")
    tr = sub(sub(tbl, "thead"), "tr")
    for h in headers:
        sub(tr, "th", h)
    tb = sub(tbl, "tbody")
    for row in rows:
        rtr = sub(tb, "tr")
        for cell in row:
            sub(rtr, "td", cell if cell else "")


# DIAGNOSIS line: "<SNOMED> (desc) [/ <ICD10> (desc)] [- note]"
# The description captures allow balanced inner parens (e.g. the ICD-10 text
# "Essential (primary) hypertension") -- a plain [^)]* would stop at the first
# inner ')', the anchored pattern would then fail to match, and parse_diagnoses
# would silently drop the whole diagnosis. _PAREN matches "no parens, or a single
# nested (...) group", repeated -- enough for the one-level nesting seen in the wild.
_PAREN = r"(?:[^()]|\([^()]*\))*"
DX_RE = re.compile(
    r"^(?P<sct>\d+)\s*\((?P<sctd>" + _PAREN + r")\)"
    r"(?:\s*/\s*(?P<icd>[A-Z]\d[A-Z0-9]*)\s*\((?P<icdd>" + _PAREN + r")\))?"
    r"(?:\s*-\s*(?P<note>.*))?$")

# DIAGNOSIS lines we couldn't parse -- warned once each (parse_diagnoses runs
# twice per encounter), so silent data loss surfaces instead of vanishing.
_unparsed_dx = set()


def parse_diagnoses(enc):
    """Encounter diagnoses from DIAGNOSIS lines (carry SNOMED + ICD-10 + note)."""
    out = []
    for v in enc.get("DIAGNOSIS"):
        line = " ".join(v.split())
        m = DX_RE.match(line)
        if not m:
            if line and line not in _unparsed_dx:
                _unparsed_dx.add(line)
                print(f"  WARN unparsed DIAGNOSIS line dropped: {line!r}", file=sys.stderr)
            continue
        icd = m.group("icd")
        if icd and "." not in icd and len(icd) > 3:      # Z0000 -> Z00.00
            icd = icd[:3] + "." + icd[3:]
        out.append({"sct": m.group("sct"), "sctd": (m.group("sctd") or "").strip(),
                    "icd": icd, "icdd": (m.group("icdd") or "").strip(),
                    "note": (m.group("note") or "").strip()})
    return out


def _appt_datetime(date_s, time_s):
    """Combine a M/D/Y date with an optional '01:30PM' clock time -> datetime."""
    d = parse_date(date_s)
    if not d or not time_s:
        return d
    try:
        t = datetime.strptime(time_s.replace(" ", "").upper(), "%I:%M%p")
        return d.replace(hour=t.hour, minute=t.minute)
    except ValueError:
        return d


def extract_appt(enc):
    """Appointment date/time for an encounter, read from its Patient/Encounter
    HTML block (labeled 'Appt. Date/Time'). Returns a datetime carrying the clock
    time when the source provides one, else a date, else None. Generic: handles
    both the single-line ('10/03/2023 01:30PM') and split-line layouts. Preferred
    over the encounter-header date as the date of service -- it is when the
    patient was actually seen."""
    for key in ("FROZENENCOUNTERHTML", "FROZENSECTIONHTML_Patient"):
        block = enc.first(key)
        if not block:
            continue
        lines = [l.strip() for l in block.splitlines()]
        for i, l in enumerate(lines):
            if l.lower().startswith("appt. date/time"):
                chunk = " ".join(x for x in lines[i + 1:i + 3] if x)
                m = re.search(r"(\d{1,2}/\d{1,2}/\d{2,4})\s*(\d{1,2}:\d{2}\s*[AP]M)?",
                              chunk, re.I)
                if m:
                    return _appt_datetime(m.group(1), m.group(2))
    return None


def encounter_dos(enc):
    """Date of service for a single encounter: its appointment date/time when the
    source provides one, else the encounter-header date."""
    return extract_appt(enc) or enc.date


def sec_encounters(body, encs, patient):
    sec = section(body, [("2.16.840.1.113883.10.20.22.2.22.1", "2015-08-01")],
                  "46240-8", "Encounters", "ENCOUNTERS")
    rows = []
    for e in encs:
        dxs = parse_diagnoses(e)
        dxtext = "; ".join((d["icd"] or d["sct"]) + " " + (d["icdd"] or d["sctd"]) for d in dxs)
        rows.append([iso(e.date), e.type, patient["provider"] or "", patient["org"] or "", dxtext])
    narrative(sec, ["Date", "Type", "Provider", "Location", "Diagnoses"], rows)
    for e in encs:
        # Date of service: prefer the appointment date/time (when the patient was
        # actually seen) over the encounter-header day; carry clock time if present.
        dos = extract_appt(e) or e.date
        dos_full = bool(dos and (dos.hour or dos.minute or dos.second))
        en = sub(sub(sec, "entry", typeCode="DRIV"), "encounter", classCode="ENC", moodCode="EVN")
        tid(en, "2.16.840.1.113883.10.20.22.4.49", "2015-08-01")
        sub(en, "id", root=ROOT, extension=e.id)
        sub(sub(en, "code", nullFlavor="UNK"), "originalText", e.type)
        sub(sub(en, "effectiveTime"), "low", value=ts(dos, dos_full))
        for d in parse_diagnoses(e):
            act = sub(sub(en, "entryRelationship", typeCode="SUBJ"),
                      "act", classCode="ACT", moodCode="EVN")
            tid(act, "2.16.840.1.113883.10.20.22.4.80", "2015-08-01")   # Encounter Diagnosis
            sub(act, "code", code="29308-4", displayName="Diagnosis",
                codeSystem=CS["LOINC"], codeSystemName="LOINC")
            obs = sub(sub(act, "entryRelationship", typeCode="SUBJ"),
                      "observation", classCode="OBS", moodCode="EVN")
            tid(obs, "2.16.840.1.113883.10.20.22.4.4", "2015-08-01")    # Problem Observation
            sub(obs, "id", root=ROOT)
            sub(obs, "code", code="55607006", displayName="Problem",
                codeSystem=CS["SNOMED"], codeSystemName="SNOMED CT")
            sub(obs, "statusCode", code="completed")
            sub(sub(obs, "effectiveTime"), "low", value=ts(dos, dos_full))
            if d["note"]:
                sub(obs, "text", d["note"])
            if d["icd"]:                                               # ICD-10 value + SNOMED translation
                val = sub(obs, "value", xsi_type="CD", code=d["icd"], displayName=d["icdd"] or d["sctd"],
                          codeSystem=CS["ICD10"], codeSystemName="ICD-10-CM")
                if d["sct"]:
                    sub(val, "translation", code=d["sct"], displayName=d["sctd"],
                        codeSystem=CS["SNOMED"], codeSystemName="SNOMED CT")
            else:
                sub(obs, "value", xsi_type="CD", code=d["sct"], displayName=d["sctd"],
                    codeSystem=CS["SNOMED"], codeSystemName="SNOMED CT")


def sec_problems(body, problems):
    sec = section(body, [("2.16.840.1.113883.10.20.22.2.5.1", "2015-08-01")],
                  "11450-4", "Problem list", "PROBLEMS")
    narrative(sec, ["Problem", "Code", "Onset", "Status", "Comments"],
              [[p["name"], f'{p["code"]} ({p["ctype"]})', iso(p["onset"]),
                "Resolved" if p["resolved"] else "Active",
                " | ".join(p.get("details", []))] for p in problems])
    for p in problems:
        act = sub(sub(sec, "entry", typeCode="DRIV"), "act", classCode="ACT", moodCode="EVN")
        tid(act, "2.16.840.1.113883.10.20.22.4.3", "2015-08-01")
        sub(act, "id", root=ROOT)
        sub(act, "code", code="CONC", codeSystem=CS["ActCode"])
        sub(act, "statusCode", code="completed")
        et = sub(act, "effectiveTime")
        sub(et, "low", value=ts(p["onset"])) if p["onset"] else None
        obs = sub(sub(act, "entryRelationship", typeCode="SUBJ"),
                  "observation", classCode="OBS", moodCode="EVN")
        tid(obs, "2.16.840.1.113883.10.20.22.4.4", "2015-08-01")
        sub(obs, "id", root=ROOT)
        sub(obs, "code", code="55607006", displayName="Problem",
            codeSystem=CS["SNOMED"], codeSystemName="SNOMED CT")
        # Retain the full provider comment/status narrative for this problem.
        if p.get("details"):
            sub(obs, "text", " | ".join(p["details"]))
        sub(obs, "statusCode", code="completed")
        oet = sub(obs, "effectiveTime")
        if p["onset"]:
            sub(oet, "low", value=ts(p["onset"]))
        if p["resolved"]:
            sub(oet, "high", value=ts(p["resolved"]))
        sub(obs, "value", xsi_type="CD", code=p["code"], displayName=p["name"],
            codeSystem=CS["ICD10"], codeSystemName="ICD-10-CM")


def sec_medications(body, meds):
    sec = section(body, [("2.16.840.1.113883.10.20.22.2.1.1", "2014-06-09")],
                  "10160-0", "History of Medication Use", "MEDICATIONS")
    narrative(sec, ["Medication", "Instructions", "Start Date"],
              [[m["name"], m["sig"], iso(m["date"])] for m in meds])
    for m in meds:
        sa = sub(sub(sec, "entry", typeCode="DRIV"),
                 "substanceAdministration", classCode="SBADM", moodCode="EVN")
        tid(sa, "2.16.840.1.113883.10.20.22.4.16", "2014-06-09")
        sub(sa, "id", root=ROOT)
        sub(sa, "text", m["sig"])
        sub(sa, "statusCode", code="active")
        et = sub(sa, "effectiveTime", xsi_type="IVL_TS")
        if m["date"]:
            sub(et, "low", value=ts(m["date"]))
        mp = sub(sub(sa, "consumable"), "manufacturedProduct", classCode="MANU")
        tid(mp, "2.16.840.1.113883.10.20.22.4.23", "2014-06-09")
        mm = sub(mp, "manufacturedMaterial")
        # Medications are represented as UN-CODED: the dump carries only drug names
        # (no RxNorm/NDC). nullFlavor="OTH" + <originalText> is the C-CDA pattern for
        # "human-readable value present, no standard code". MED_CODES / med_code remain
        # defined but bypassed -- re-enable them once a real terminology service is wired in.
        sub(sub(mm, "code", nullFlavor="OTH"), "originalText", m["name"])


def sec_immunizations(body, vaccs):
    sec = section(body, [("2.16.840.1.113883.10.20.22.2.2", "2015-08-01")],
                  "11369-6", "History of Immunizations", "IMMUNIZATIONS")
    narrative(sec, ["Vaccine", "Date", "Lot"],
              [[v["name"], iso(v["date"]), v["lot"] or ""] for v in vaccs])
    for v in vaccs:
        sa = sub(sub(sec, "entry", typeCode="DRIV"),
                 "substanceAdministration", classCode="SBADM", moodCode="EVN", negationInd="false")
        tid(sa, "2.16.840.1.113883.10.20.22.4.52", "2015-08-01")
        sub(sa, "id", root=ROOT)
        sub(sa, "statusCode", code="completed")
        if v["date"]:
            sub(sa, "effectiveTime", value=ts(v["date"]))
        mp = sub(sub(sa, "consumable"), "manufacturedProduct", classCode="MANU")
        tid(mp, "2.16.840.1.113883.10.20.22.4.54", "2014-06-09")
        mm = sub(mp, "manufacturedMaterial")
        code = cvx_code(v["name"])
        if code:
            c, disp = code
            sub(mm, "code", code=c, displayName=disp, codeSystem=CS["CVX"], codeSystemName="CVX")
        else:
            sub(sub(mm, "code", nullFlavor="OTH"), "originalText", v["name"])
        if v["lot"]:
            sub(mm, "lotNumberText", v["lot"])


def sec_vitals(body, vitals):
    sec = section(body, [("2.16.840.1.113883.10.20.22.2.4.1", "2015-08-01")],
                  "8716-3", "Vital Signs", "VITAL SIGNS")
    rows = [[r["display"], str(r["value"]), r["unit"], iso(g["date"])]
            for g in vitals for r in g["readings"]]
    narrative(sec, ["Vital Sign", "Value", "Unit", "Date"], rows)
    for g in vitals:
        org = sub(sub(sec, "entry", typeCode="DRIV"), "organizer", classCode="CLUSTER", moodCode="EVN")
        tid(org, "2.16.840.1.113883.10.20.22.4.26", "2015-08-01")
        sub(org, "id", root=ROOT)
        sub(org, "code", code="46680005", displayName="Vital signs", codeSystem=CS["SNOMED"])
        sub(org, "statusCode", code="completed")
        sub(org, "effectiveTime", value=ts(g["date"]))
        for r in g["readings"]:
            obs = sub(sub(org, "component"), "observation", classCode="OBS", moodCode="EVN")
            tid(obs, "2.16.840.1.113883.10.20.22.4.27", "2014-06-09")
            sub(obs, "id", root=ROOT)
            sub(obs, "code", code=r["loinc"], displayName=r["display"],
                codeSystem=CS["LOINC"], codeSystemName="LOINC")
            sub(obs, "statusCode", code="completed")
            sub(obs, "effectiveTime", value=ts(g["date"]))
            sub(obs, "value", xsi_type="PQ", value=str(r["value"]), unit=r["unit"])


def sec_payers(body, payers):
    sec = section(body, [("2.16.840.1.113883.10.20.22.2.18", "2015-08-01")],
                  "48768-6", "Payers", "INSURANCE PROVIDERS")
    narrative(sec, ["Priority", "Insurance Company", "Category", "Member ID"],
              [["Primary" if i == 0 else "Secondary", p["name"], p["category"], p["member_id"] or ""]
               for i, p in enumerate(payers)])
    for p in payers:
        act = sub(sub(sec, "entry", typeCode="DRIV"), "act", classCode="ACT", moodCode="EVN")
        tid(act, "2.16.840.1.113883.10.20.22.4.60", "2015-08-01")
        sub(act, "id", root=ROOT)
        sub(act, "code", code="48768-6", displayName="Payers", codeSystem=CS["LOINC"])
        sub(act, "statusCode", code="completed")
        obs = sub(sub(act, "entryRelationship", typeCode="COMP"),
                  "observation", classCode="OBS", moodCode="EVN")
        sub(obs, "id", root=ROOT, extension=p["member_id"])
        sub(obs, "code", code="SUBSC", displayName="Subscriber", codeSystem=CS["RoleClass"])
        ae = sub(sub(obs, "performer"), "assignedEntity")
        sub(ae, "id", nullFlavor="UNK")
        sub(sub(ae, "representedOrganization"), "name", p["name"])


def sec_procedures(body, procs):
    sec = section(body, [("2.16.840.1.113883.10.20.22.2.7.1", "2014-06-09")],
                  "47519-4", "History of Procedures", "PROCEDURES")
    narrative(sec, ["Procedure", "Date"], [[p["name"], iso(p["date"])] for p in procs])
    for p in procs:
        proc = sub(sub(sec, "entry", typeCode="DRIV"), "procedure", classCode="PROC", moodCode="EVN")
        tid(proc, "2.16.840.1.113883.10.20.22.4.14", "2014-06-09")
        sub(proc, "id", root=ROOT)
        sub(sub(proc, "code", nullFlavor="OTH"), "originalText", p["name"])
        sub(proc, "statusCode", code="completed")
        if p["date"]:
            sub(proc, "effectiveTime", value=ts(p["date"]))


def sec_allergies(body, allergies):
    sec = section(body, [("2.16.840.1.113883.10.20.22.2.6.1", "2015-08-01")],
                  "48765-2", "Allergies", "ALLERGIES")
    narrative(sec, ["Substance", "Reaction"],
              [[a["substance"], a["reaction"] or ""] for a in allergies])
    for a in allergies:
        act = sub(sub(sec, "entry", typeCode="DRIV"), "act", classCode="ACT", moodCode="EVN")
        tid(act, "2.16.840.1.113883.10.20.22.4.30", "2015-08-01")
        sub(act, "id", root=ROOT)
        sub(act, "code", code="CONC", codeSystem=CS["ActCode"])
        sub(act, "statusCode", code="active")
        obs = sub(sub(act, "entryRelationship", typeCode="SUBJ"),
                  "observation", classCode="OBS", moodCode="EVN")
        tid(obs, "2.16.840.1.113883.10.20.22.4.7", "2014-06-09")
        sub(obs, "id", root=ROOT)
        sub(obs, "code", code="ASSERTION", codeSystem=CS["ActCode"])
        sub(obs, "statusCode", code="completed")
        sub(obs, "value", xsi_type="CD", code="419511003",
            displayName="Propensity to adverse reactions to drug", codeSystem=CS["SNOMED"])
        part = sub(obs, "participant", typeCode="CSM")
        pr = sub(part, "participantRole", classCode="MANU")
        pe = sub(pr, "playingEntity", classCode="MMAT")
        sub(sub(pe, "code", nullFlavor="OTH"), "originalText", a["substance"])
        if a["reaction"]:
            rel = sub(obs, "entryRelationship", typeCode="MFST", inversionInd="true")
            robs = sub(rel, "observation", classCode="OBS", moodCode="EVN")
            tid(robs, "2.16.840.1.113883.10.20.22.4.9", "2014-06-09")
            sub(robs, "code", code="ASSERTION", codeSystem=CS["ActCode"])
            sub(robs, "statusCode", code="completed")
            sub(sub(robs, "value", xsi_type="CD", nullFlavor="OTH"), "originalText", a["reaction"])


def sec_results(body, results):
    sec = section(body, [("2.16.840.1.113883.10.20.22.2.3.1", "2015-08-01")],
                  "30954-2", "Relevant diagnostic tests and/or laboratory data", "RESULTS")
    narrative(sec, ["Test", "Value", "Unit"],
              [[r["name"], r["value"], r["unit"]] for r in results])
    for r in results:
        org = sub(sub(sec, "entry", typeCode="DRIV"), "organizer", classCode="BATTERY", moodCode="EVN")
        tid(org, "2.16.840.1.113883.10.20.22.4.1", "2015-08-01")
        sub(org, "id", root=ROOT)
        sub(sub(org, "code", nullFlavor="OTH"), "originalText", r["name"])
        sub(org, "statusCode", code="completed")
        obs = sub(sub(org, "component"), "observation", classCode="OBS", moodCode="EVN")
        tid(obs, "2.16.840.1.113883.10.20.22.4.2", "2015-08-01")
        sub(obs, "id", root=ROOT)
        sub(sub(obs, "code", nullFlavor="OTH"), "originalText", r["name"])
        sub(obs, "statusCode", code="completed")
        if r["unit"]:
            sub(obs, "value", xsi_type="PQ", value=r["value"], unit=r["unit"])
        else:
            sub(obs, "value", xsi_type="PQ", value=r["value"], unit="1")


def sec_social_history(body, items):
    sec = section(body, [("2.16.840.1.113883.10.20.22.2.17", "2015-08-01")],
                  "29762-2", "Social history", "SOCIAL HISTORY")
    rows = [[it["q"], it["a"]] for it in items if it["q"] != "__smoking__"]
    narrative(sec, ["Observation", "Value"], rows)
    smoking = next((it["a"] for it in items if it["q"] == "__smoking__"), None)
    if smoking:
        obs = sub(sub(sec, "entry", typeCode="DRIV"),
                  "observation", classCode="OBS", moodCode="EVN")
        tid(obs, "2.16.840.1.113883.10.20.22.4.78", "2014-06-09")
        sub(obs, "id", root=ROOT)
        sub(obs, "code", code="72166-2", displayName="Tobacco smoking status",
            codeSystem=CS["LOINC"], codeSystemName="LOINC")
        sub(obs, "statusCode", code="completed")
        code, disp = smoking["code"]
        sub(obs, "value", xsi_type="CD", code=code, displayName=disp,
            codeSystem=CS["SNOMED"], codeSystemName="SNOMED CT")


def sec_family_history(body, fam):
    sec = section(body, [("2.16.840.1.113883.10.20.22.2.15", "2015-08-01")],
                  "10157-6", "History of family member diseases", "FAMILY HISTORY")
    narrative(sec, ["Relative", "Condition"], [[f["relation"], f["condition"]] for f in fam])
    for f in fam:
        org = sub(sub(sec, "entry", typeCode="DRIV"), "organizer", classCode="CLUSTER", moodCode="EVN")
        tid(org, "2.16.840.1.113883.10.20.22.4.45", "2015-08-01")
        sub(org, "id", root=ROOT)
        sub(org, "statusCode", code="completed")
        subj = sub(org, "subject")
        rel = sub(subj, "relatedSubject", classCode="PRS")
        sub(rel, "code", code=f["rel_code"], displayName=f["relation"],
            codeSystem="2.16.840.1.113883.5.111", codeSystemName="RoleCode")
        obs = sub(sub(org, "component"), "observation", classCode="OBS", moodCode="EVN")
        tid(obs, "2.16.840.1.113883.10.20.22.4.46", "2015-08-01")
        sub(obs, "id", root=ROOT)
        sub(obs, "code", code="64572001", displayName="Condition", codeSystem=CS["SNOMED"])
        sub(obs, "statusCode", code="completed")
        sub(sub(obs, "value", xsi_type="CD", nullFlavor="OTH"), "originalText", f["condition"])


def sec_notes(body, notes, provider):
    sec = section(body, [("2.16.840.1.113883.10.20.22.2.65", "2016-11-01")],
                  "34109-9", "Note", "NOTES")
    # narrative: one referenceable paragraph per note
    txt = sub(sec, "text")
    for i, n in enumerate(notes, 1):
        n["ref"] = f"note{i}"
        para = sub(txt, "paragraph", ID=n["ref"])
        sub(para, "caption", f'{n["label"]} — {iso(n["date"])}')
        for line in (l.strip() for l in n["text"].splitlines() if l.strip()):
            sub(para, "content", line)
    g, f, suf = split_person(provider)
    for n in notes:
        act = sub(sub(sec, "entry", typeCode="DRIV"), "act", classCode="ACT", moodCode="EVN")
        tid(act, "2.16.840.1.113883.10.20.22.4.202", "2016-11-01")
        sub(act, "id", root=ROOT)
        sub(act, "code", code=n["code"], displayName=n["label"],
            codeSystem=CS["LOINC"], codeSystemName="LOINC")
        sub(sub(act, "text"), "reference", value=f'#{n["ref"]}')
        sub(act, "statusCode", code="completed")
        a = n["authored"]
        full = bool(a and (a.hour or a.minute or a.second))
        sub(act, "effectiveTime", value=ts(a, full))
        au = sub(act, "author")
        tid(au, "2.16.840.1.113883.10.20.22.4.119")
        sub(au, "time", value=ts(a, full))
        aa = sub(au, "assignedAuthor")
        sub(aa, "id", root="2.16.840.1.113883.4.6")
        if provider:
            ap = sub(aa, "assignedPerson")
            nm = sub(ap, "name")
            if g:
                sub(nm, "given", g)
            if f:
                sub(nm, "family", f)
            if suf:
                sub(nm, "suffix", suf)


# --------------------------------------------------------------------------- #
# Conversion core (text dump -> C-CDA XML string)
# --------------------------------------------------------------------------- #
def build_ccda(patient, encs, now, dos_low, dos_high, doc_id):
    """Build one C-CDA for a group of encounters that share a date of service:
    the header + an Encounters section (all these encounters + their diagnoses),
    plus the per-encounter clinical sections -- Vital Signs, Medications, and
    clinical Notes -- scoped to just these encounters and emitted only when they
    carry data.

    Patient-level/longitudinal sections (Problems, Immunizations, Insurance,
    Social/Family history) are NOT per-encounter -- they describe the patient
    across time, not this visit -- so they are intentionally left out here."""
    doc = ET.Element(f"{{{V3}}}ClinicalDocument")
    build_header(doc, patient, now, service_low=dos_low, service_high=dos_high,
                 doc_effective=dos_low, doc_id=doc_id)
    body = sub(sub(doc, "component"), "structuredBody")
    sec_encounters(body, encs, patient)

    vitals = extract_vitals(encs)
    if vitals:
        sec_vitals(body, vitals)
    meds = extract_medications(encs)
    if meds:
        sec_medications(body, meds)
    notes = extract_notes(encs)
    notes += leftover_notes(doc, encs, notes)   # safety net: no free-text left behind
    if notes:
        sec_notes(body, notes, patient["provider"])

    tree = ET.ElementTree(doc)
    ET.indent(tree, space="  ")
    return "<?xml version='1.0' encoding='UTF-8'?>\n" + ET.tostring(doc, encoding="unicode")


def build_patient_summary_ccda(patient, encounters, now, dos_low, dos_high, doc_id):
    """Build the patient-level (longitudinal) C-CDA: the sections that describe
    the patient across time rather than a single visit -- Problems, Allergies,
    Immunizations, Procedures (surgical history), Results, Social & Family
    history, and Insurance. Sections are emitted only when they carry data;
    returns None if none do (so an empty summary file isn't written).

    The serviceEvent spans all encounter dates. Keeping these here -- instead of
    repeating them in every visit document -- avoids attributing a longitudinal
    code (e.g. a problem-list ICD-10) to an unrelated date of service."""
    doc = ET.Element(f"{{{V3}}}ClinicalDocument")
    build_header(doc, patient, now, service_low=dos_low, service_high=dos_high,
                 doc_effective=dos_high or dos_low, doc_id=doc_id)
    body = sub(sub(doc, "component"), "structuredBody")
    for items, builder in [
        (extract_problems(encounters), sec_problems),
        (extract_allergies(encounters), sec_allergies),
        (extract_immunizations(encounters), sec_immunizations),
        (extract_procedures(encounters), sec_procedures),
        (extract_lab_results(encounters), sec_results),
        (extract_social_history(encounters), sec_social_history),
        (extract_family_history(encounters), sec_family_history),
        (extract_payers(encounters), sec_payers),
    ]:
        if items:
            builder(body, items)
    if len(body) == 0:          # no longitudinal sections -> no summary document
        return None
    tree = ET.ElementTree(doc)
    ET.indent(tree, space="  ")
    return "<?xml version='1.0' encoding='UTF-8'?>\n" + ET.tostring(doc, encoding="unicode")


def convert_text_to_ccda(text):
    """Convert one EMR text dump to a list of C-CDAs: [(label, xml_string), ...].

    - One VISIT document per date of service (label = YYYYMMDD): encounters that
      share a date are grouped into a SINGLE document (so same-day encounters
      aren't split across files), carrying that day's encounters, diagnoses, and
      per-encounter sections (vitals, medications, notes).
    - One patient-level SUMMARY document (label = "summary") with the longitudinal
      sections (problems, immunizations, allergies, etc.), when any exist."""
    encounters = parse_dump(text)
    if not encounters:
        raise ValueError("no encounters found -- unexpected format")

    patient = extract_patient(encounters)
    now = datetime.now()

    # Group encounters by date of service (YYYYMMDD); undated -> "unknown".
    groups = {}
    for enc in encounters:
        dos = encounter_dos(enc)
        groups.setdefault(ts(dos) if dos else "unknown", []).append((enc, dos))

    results = []
    for key in sorted(groups):
        items = groups[key]
        encs = [e for e, _ in items]
        doses = [d for _, d in items if d]
        low, high = (min(doses), max(doses)) if doses else (None, None)
        xml = build_ccda(patient, encs, now, low, high, doc_id=f"EMRDUMP-{key}")
        results.append((key, xml))

    # Patient-level summary document (longitudinal sections), spanning all dates.
    all_doses = sorted(d for d in (encounter_dos(e) for e in encounters) if d)
    s_low, s_high = (all_doses[0], all_doses[-1]) if all_doses else (None, None)
    summary = build_patient_summary_ccda(patient, encounters, now, s_low, s_high,
                                         doc_id="EMRDUMP-summary")
    if summary:
        results.append(("summary", summary))
    return results


def _member_id(filename):
    """Member id from an input filename like 'emr_<member_id>_<timestamp>.txt'
    (e.g. emr_ABCDEFG_2026-04-22-110615.txt -> ABCDEFG). Falls back to the file
    stem when the name doesn't match, so unexpected names still produce output."""
    stem = re.sub(r"\.txt$", "", filename, flags=re.IGNORECASE)
    m = re.match(r"(?i)^emr_([^_]+)_", stem)
    return m.group(1) if m else stem


def _folder_name(member):
    """Output folder name, following the original XML naming convention minus the
    extension: <member_id>_<OUTPUT_DATE>_<PROJECT_ID>."""
    return f"{member}_{OUTPUT_DATE}_{PROJECT_ID}"


def _doc_out_name(member, label, used):
    """Output filename for one document: <member>_<label>_<PROJECT_ID>.xml, where
    `label` is the date of service (YYYYMMDD) for a visit document or "summary"
    for the patient-level document. `used` guards against name clashes with a
    -2, -3, ... suffix so nothing is overwritten."""
    base = f"{member}_{label}_{PROJECT_ID}"
    name, n = f"{base}.xml", 2
    while name in used:
        name, n = f"{base}-{n}.xml", n + 1
    used.add(name)
    return name


# --------------------------------------------------------------------------- #
# S3 batch
# --------------------------------------------------------------------------- #
def parse_s3_uri(uri):
    bucket, _, prefix = uri[len("s3://"):].partition("/")
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    return bucket, prefix


def run_s3(input_uri, output_uri):
    try:
        import boto3
    except ImportError:
        sys.exit("S3 mode needs boto3. Install it with:  pip install boto3")

    s3 = boto3.client("s3")
    in_bucket, in_prefix = parse_s3_uri(input_uri)
    out_bucket, out_prefix = parse_s3_uri(output_uri)

    # S3 has no real folders; uploading under a prefix creates the path. Drop an
    # empty marker object so the "folder" is visible in the console if requested.
    if out_prefix:
        s3.put_object(Bucket=out_bucket, Key=out_prefix, Body=b"")

    ok = fail = 0
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=in_bucket, Prefix=in_prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/") or not key.lower().endswith(".txt"):
                continue
            try:
                body = s3.get_object(Bucket=in_bucket, Key=key)["Body"].read().decode("utf-8", "replace")
                results = convert_text_to_ccda(body)
                member = _member_id(key.rsplit("/", 1)[-1])
                fname = _folder_name(member)
                folder = f"{out_prefix}{fname}/"
                # marker file sibling to the folder, same name, with a .none extension
                s3.put_object(Bucket=out_bucket, Key=f"{out_prefix}{fname}.none", Body=b"")
                used = set()
                for label, xml in results:
                    out_key = folder + _doc_out_name(member, label, used)
                    s3.put_object(Bucket=out_bucket, Key=out_key,
                                  Body=xml.encode("utf-8"), ContentType="application/xml")
                print(f"  OK  {key} -> s3://{out_bucket}/{folder}  ({len(results)} documents)")
                ok += 1
            except Exception as e:                       # noqa: BLE001 - keep batch going
                print(f"  ERR {key}: {e}")
                fail += 1
    if ok == 0 and fail == 0:
        print(f"No .txt files found under {input_uri}")
    print(f"Done. {ok} converted, {fail} failed.")
    return fail == 0


# --------------------------------------------------------------------------- #
# Local batch (for testing without S3)
# --------------------------------------------------------------------------- #
def run_local(input_dir, output_dir):
    in_root = Path(input_dir)
    if not in_root.is_dir():
        sys.exit(f"Input directory not found: {input_dir}")
    ok = fail = 0
    for path in sorted(in_root.rglob("*.txt")):
        try:
            results = convert_text_to_ccda(path.read_text(encoding="utf-8", errors="replace"))
            member = _member_id(path.name)
            fname = _folder_name(member)
            folder = Path(output_dir) / fname
            folder.mkdir(parents=True, exist_ok=True)
            # marker file sibling to the folder, same name, with a .none extension
            (Path(output_dir) / f"{fname}.none").write_text("", encoding="utf-8")
            used = set()
            for label, xml in results:
                (folder / _doc_out_name(member, label, used)).write_text(xml, encoding="utf-8")
            print(f"  OK  {path} -> {folder}/  ({len(results)} documents)")
            ok += 1
        except Exception as e:                           # noqa: BLE001
            print(f"  ERR {path}: {e}")
            fail += 1
    if ok == 0 and fail == 0:
        print(f"No .txt files found under {input_dir}")
    print(f"Done. {ok} converted, {fail} failed.")
    return fail == 0


# --------------------------------------------------------------------------- #
# Credentials (.env fallback)
# --------------------------------------------------------------------------- #
def load_env_file(path):
    """Load KEY=VALUE lines from a .env file into the environment (without
    overriding variables that are already set). Used as a fallback when AWS
    isn't otherwise configured. Recognized keys include AWS_ACCESS_KEY_ID,
    AWS_SECRET_ACCESS_KEY, AWS_SESSION_TOKEN, AWS_DEFAULT_REGION, AWS_PROFILE."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return False
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, sep, val = line.partition("=")
        if not sep:
            continue
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] in "\"'" and val[-1] == val[0]:
            val = val[1:-1]
        os.environ.setdefault(key, val)        # don't override real env / AWS config
    return True


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(
        description="Batch-convert encounter-grouped EMR text dumps to C-CDA XML (S3 or local).")
    ap.add_argument("--input", required=True,
                    help="S3 URI (s3://bucket/prefix/) or local directory of .txt dumps")
    ap.add_argument("--output", required=True,
                    help="S3 URI or local directory for the .xml output (created if missing)")
    ap.add_argument("--env-file",
                    help="load AWS credentials/region from this .env file "
                         "(defaults to ./.env if present); real env vars take precedence")
    args = ap.parse_args()

    # Fallback credentials from .env (explicit --env-file, else ./.env if present).
    if args.env_file:
        if not load_env_file(args.env_file):
            sys.exit(f"--env-file not found: {args.env_file}")
    else:
        load_env_file(".env")

    in_s3, out_s3 = args.input.startswith("s3://"), args.output.startswith("s3://")
    if in_s3 != out_s3:
        sys.exit("--input and --output must both be S3 URIs or both be local paths.")

    print(f"Converting {args.input}  ->  {args.output}")
    ok = run_s3(args.input, args.output) if in_s3 else run_local(args.input, args.output)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

