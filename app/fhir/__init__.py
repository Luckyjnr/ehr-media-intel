"""Task 2: map cleaned records to FHIR R4 (R4B) bundles, validate them and store them.

Why R4B and not the library default:
fhir.resources v8 uses FHIR R5 by default, but most real EHRs (Epic, Cerner)
still speak R4. R4B is the R4 line, with the same structure for the resources
we use (Patient, Encounter, DocumentReference, DiagnosticReport, Bundle).
So we always import from fhir.resources.R4B.
"""
