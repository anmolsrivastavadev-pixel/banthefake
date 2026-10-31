# Probe C2PA test material

Generated test material, **not real certificates**. Licence: the project's licence (the signed images keep the
CC0 licence of their sources, `../real_cat_q70_640.jpg` and `../ai_dalle_cat_q70_640.jpg`).

- `ca.pem`: a throwaway root CA ("BTF Probe Root", O=Probe) created by `make_probe_c2pa.py`. It is on no trust
  list. Its private key, and the end-entity key ("Probe Camera"), were discarded after signing.
- `s_capture.jpg`: `c2pa.created` with `digitalSourceType` `digitalCapture`.
- `s_capture_unknown.jpg`: capture plus a `c2pa.unknown` action.
- `s_capture_placed.jpg`: capture plus `c2pa.placed` of one ingredient without its own manifest.
- `s_ai.jpg`: `c2pa.created` with `trainedAlgorithmicMedia`.
- `s_capture_placed_ai.jpg`: capture plus `c2pa.placed` of `s_ai.jpg`, whose own manifest declares generative AI.

Tests trust these only by pointing `BTF_C2PA_TRUST_ANCHORS` at `ca.pem`, or by adding `ca.pem` to a temporary
copy of the trust lists. Regenerate everything (new CA, new signatures) from the repository root with
`.venv/bin/python tests/fixtures/probe_c2pa/make_probe_c2pa.py`; the certificates are valid for about 100 years.
