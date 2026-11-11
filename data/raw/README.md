# Raw sample data (100% synthetic)

Every name, MRN, date and clinical detail here is made up. No real patient data.

JSON and CSV cannot hold comments, so the deliberate problems are listed here.

## Sources

| File | What it simulates |
|---|---|
| `ehr_export.json` | Epic-style export: patients -> encounters -> documents and labs |
| `lab_results.csv` | Lab system extract, one row per result, with patient name/DOB |
| `notes/*.txt` | "Scanned" notes: loose `Key: value` header, blank line, free text |

## Deliberate problems

**Dates in many formats**
- ISO `2024-03-05` (Smith/Garcia EHR labs), `2024-03-05T14:20:00` (Garcia imaging, has a time part)
- `03/05/2024` Smith discharge summary, CSV Smith glucose (ambiguous: March 5 or May 3)
- `5/3/24` Miller lab report, CSV Brown potassium; `5/3/65` Miller DOB (ambiguous)
- `05-Mar-2024` Brown progress note, CSV Garcia hemoglobin, unknown-patient note
- `March 5 2024` / `July 4 1945` Johnson discharge summary and DOB, CSV Johnson creatinine; `March 19 2024` Smith note
- `20240305` Williams imaging, CSV Williams TSH; `20240410` Garcia note; `19900214` Williams DOB
- `21/08/1983` Brown DOB (day/month, because 21 cannot be a month)
- `04/12/1958`, `12/01/1955` ambiguous DOBs (Smith, Moore)
- Impossible date: `02/30/1950` Wilson DOB
- Future date: `2027-01-15` CSV Miller HbA1c collected date

**Gender**: `M` (Smith, Taylor), `F` (Davis), `m` (Brown), `female` (Garcia), `Male` (Moore),
`1` (Johnson), `2` (Williams), `U` (Miller), `male` (Lee), blank `""` (Clark), key missing (Wilson)

**MRN**: `12345` (Smith EHR), `MRN-0012345` (Smith CSV), `12-345` (Smith note),
`" mrn 23456 "` (Garcia CSV), `0023456` (Garcia note), `MRN 90123` (Lee note)

**Missing fields**
- No DOB: Davis (EHR + CSV), Anderson (note)
- No gender: Wilson (key absent), Clark (blank)
- No MRN at all: Kevin Taylor (EHR patient + his document, CSV row), unnamed scanned note
- No LOINC: CSV Davis LDL; no document date: Davis discharge summary; blank flag: CSV Miller

**Duplicates**
- Linda Williams appears twice in the EHR export, identical (including her imaging document)
- CSV Garcia hemoglobin row is repeated exactly
- Smith HbA1c and Garcia hemoglobin appear in both the JSON and the CSV
  (Garcia value is `11.20` in JSON and `11.2` in CSV: same number)

**Conflicting identifiers**
- MRN 0090123: EHR says David Lee, DOB 1977-09-09; note says DOB 09/19/1977;
  CSV says *Daniel* Lee, DOB 1977-09-09
- Susan Clark, DOB 1988-06-15: MRN 0101234 in the EHR, 101235 in the CSV

**Names in odd case / spacing**: `"  john "`/`SMITH`, `ROBERT`/`johnson`, `"  linda "`, `tHOMAS`/`"moore  "`,
`"maria   garcia"`, `"nancy  anderson"`

**Other**: Clark has document type `Echo Report` (unknown -> `other` with a warning)
