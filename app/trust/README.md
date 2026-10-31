# C2PA trust material

These are the official C2PA signing and timestamp trust lists published at
<https://c2pa.org/conformance>.

- List issue date: 2026-10-23 16:57:29 UTC (sequence 2, version 1)
- Next update: 2027-01-21 16:57:29 UTC
- Files: `C2PA-TRUST-LIST.pem`, `C2PA-TSA-TRUST-LIST.pem`, and the C2PA `store.cfg`
  extended-key-usage policy

The list header identifies C2PA as the scheme operator and the conformance URL as the
scheme-information source. It contains no separate licence or redistribution terms. The PEM files
are shipped as the trust data required to verify claims against that public scheme.

`anchors.pem` from the research directory is deliberately excluded: its source and redistribution
terms have not been confirmed.

Only `C2PA-TRUST-LIST.pem` is loaded as trust anchors. c2pa-python 0.38 pools all configured anchors when it
checks the signing certificate, whatever their `trust_kind`, so loading `C2PA-TSA-TRUST-LIST.pem` would let a
timestamp-authority root make a manifest signer trusted. The TSA list is kept here for reference and for a future
library version that keeps the two lists apart.

To update, download the current official files from the conformance URL, verify their issue and next
update fields, replace all three files together, update `VERSION` and the dates above, run the C2PA
integration tests, and verify that test certificates remain untrusted. OCSP and remote manifest fetch
stay disabled, so updating these bundled files is an explicit release task.
