# Plan: tackling the three remaining limits

Handoff document so the work can continue in Codex (or any agent) without the original chat.
Started 2026-10-31. Update the **Status** lines as phases complete.

## Ground rules
- Every phase: research → implement → adversarial verification → fix → CI green.
- Never claim more certainty than the evidence. Wording rules in README "limitations" still apply.
- Each fix/feature ships with tests that fail without it. Run both suites:
  `uv run pytest -q` (core) and `HF_HOME=<hf cache> HF_HUB_OFFLINE=1 uv run --extra ml pytest -q`.
- Research data lives in `research/` (git-ignored). Never commit downloaded datasets.
- Codex sandbox here: no network, read-only `.git`. Downloads and commits are done outside Codex
  (Claude session, or the user via `! <command>`).

## Limit 1 — "No tool can prove a file is real"
This can't be removed, only narrowed. The strongest honest positive claim is cryptographic
provenance: a C2PA manifest that validates **and** whose signing certificate chains to the official
C2PA trust list. That proves who signed the file and that it hasn't changed since. It does not prove
the scene is real (e.g. re-photographing a screen).

Plan:
- Ship the official C2PA trust list (`research/c2pa/trust/C2PA-TRUST-LIST.pem`, TSA list,
  `store.cfg` EKU config) in `app/trust/` with a version stamp; `BTF_C2PA_TRUST_ANCHORS` still overrides.
  Keep OCSP and remote-manifest fetch disabled.
- Verdicts: `verified_capture` (trusted signer + digitalCapture / camera claim, no AI source type),
  `verified_ai` (trusted signer + trainedAlgorithmicMedia), `signed_untrusted` (valid signature, signer
  not on the list — informational only). Edits recorded in a trusted manifest are listed.
- Sample files: `research/c2pa/samples/pixel10/` (Google Pixel 10 photos, licences in `LICENSES.jsonl`),
  `tests/fixtures/C.jpg` / `CA.jpg` (test-signed, must stay untrusted). Small licensed sample(s) can be
  copied into `tests/fixtures/` with an ATTRIBUTION entry.
- Wording must say what is and isn't proven.

Status: research partly done (trust lists + Pixel 10 samples downloaded, `research/c2pa/scripts/verify.py`).

## Limit 2 — borderline Midjourney detection
Plan:
- Build a labelled, licence-recorded eval set (≥150 AI incl. ≥40 Midjourney, ≥150 real incl. grayscale,
  people, night, graphics/screenshots) in `research/detect/` with a manifest CSV.
- Benchmark current model (`buildborderless/CommunityForensics-DeepfakeDet-ViT`) and 3–5 CPU candidates
  (permissive licences only, no NC; read any trust_remote_code before running).
- Try mean/max/stacked ensembles with a held-out split; recalibrate thresholds for ≤2% real FPR.
- Ship only if test-split numbers improve Midjourney recall without raising FPR. Otherwise document.

Status: started (`research/detect/collect.py`, 18 images).

## Limit 3 — no face-swap detection
Plan:
- Face detection/cropping with a permissive CPU model (OpenCV YuNet, Apache-2.0).
- Candidate face-forgery detectors with usable licences; eval on openly licensed data only
  (FaceForensics++/Celeb-DF/DFDC need signed agreements). Never create swaps of real identifiable people;
  synthetic faces only.
- Decision: ship / ship as experimental (off by default, never drives the verdict) / don't ship,
  based on measured FPR on real faces.

Status: not started (a GenD source checkout was downloaded; licence and weights not yet checked).
