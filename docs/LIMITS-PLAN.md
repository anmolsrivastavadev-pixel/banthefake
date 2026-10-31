# Plan: tackling the three remaining limits

Handoff document so the work can continue in Codex (or any agent) without the original chat.
Started 2026-10-31. Update the **Status** lines as phases complete.

## Ground rules
- Every phase: research → implement → adversarial verification → fix → CI green.
- Never claim more certainty than the evidence. Wording rules in README "limitations" still apply.
- Each fix/feature ships with tests that fail without it. Run both suites:
  `uv run pytest -q` (core) and `HF_HOME=<hf cache> HF_HUB_OFFLINE=1 uv run --extra ml pytest -q`.
- Research data lives in `research/` (git-ignored). Never commit downloaded datasets.

## Limit 1 — "No tool can prove a file is real"
This can't be removed, only narrowed. The strongest honest positive claim is cryptographic
provenance: a C2PA manifest that validates **and** whose signing certificate chains to the official
C2PA trust list. That proves who signed the file and that it hasn't changed since. It does not prove
the scene is real (e.g. re-photographing a screen).

Plan:
- Ship the official C2PA trust list (`research/c2pa/trust/C2PA-TRUST-LIST.pem` and the separate TSA list)
  in `app/trust/` with a version stamp; `BTF_C2PA_TRUST_ANCHORS` still overrides manifest signer roots.
  Keep OCSP and remote-manifest fetch disabled.
- Verdicts: `verified_capture` (trusted signer + digitalCapture / camera claim, no AI source type),
  `verified_ai` (trusted signer + trainedAlgorithmicMedia), `signed_untrusted` (valid signature, signer
  not on the list — informational only). Edits recorded in a trusted manifest are listed.
- Sample files: `research/c2pa/samples/pixel10/` (Google Pixel 10 photos, licences in `LICENSES.jsonl`),
  `tests/fixtures/C.jpg` / `CA.jpg` (test-signed, must stay untrusted). Small licensed sample(s) can be
  copied into `tests/fixtures/` with an ATTRIBUTION entry.
- Wording must say what is and isn't proven.

Status: complete (2026-10-31; revised after adversarial review: only the signer list is loaded as trust
anchors, because c2pa-python pools TSA anchors with signer anchors). The app ships the official C2PA signing and TSA lists issued
2026-10-23 (next update 2027-01-21), defaults to offline official trust, supports
`BTF_C2PA_TRUST=off` and the `BTF_C2PA_TRUST_ANCHORS` override, and never enables OCSP or remote
manifest fetch. `anchors.pem` was excluded because its source and redistribution terms could not be
confirmed. A trusted `c2pa.created` action with `digitalCapture` or `computationalCapture` and no
generative-AI source type drives `verified_capture`; a trusted generative-AI declaration drives
`verified_ai`. Valid untrusted claims are informational and invalid claims are ignored.

Local validation matrix: the three Pixel 10 samples were `Trusted` with `official` and `both`
(`signingCredential.trusted`, `timeStamp.trusted`), and `Valid` but untrusted with `none` and
`legacy` (`signingCredential.untrusted`; timestamp also untrusted for Pixel). With `notrust`, trust
checking was disabled and all files were merely `Valid`. `C.jpg` and `CA.jpg` stayed `Valid` with
`signingCredential.untrusted` in every trust-checking mode, including official/both. The shipped
Pixel fixture is an unmodified 2.8 MB CC BY-SA 4.0 file; a one-byte tamper produced
`assertion.dataHash.mismatch`. Verification: 279 core tests passed (17 deselected), and 17 offline ML
tests passed (279 deselected).

Adversarial verification found that the first implementation merged manifest-signer and TSA roots into
one legacy anchor store. Commit `1d9eeba` replaced it with typed `manifest` and `tsa` stores, hardened
malformed manifest summaries, and changed the UI to say the trusted signer *declares* camera capture rather
than claiming the signature independently proves contemporaneous capture. The focused 71-test regression
set and the full 300-test suite pass; CI is run on every pushed commit.

## Limit 2 — borderline Midjourney detection
Plan:
- Build a labelled, licence-recorded eval set (≥150 AI incl. ≥40 Midjourney, ≥150 real incl. grayscale,
  people, night, graphics/screenshots) in `research/detect/` with a manifest CSV.
- Benchmark current model (`buildborderless/CommunityForensics-DeepfakeDet-ViT`) and 3–5 CPU candidates
  (permissive licences only, no NC; read any trust_remote_code before running).
- Try mean/max/stacked ensembles with a held-out split; recalibrate thresholds for ≤2% real FPR.
- Ship only if test-split numbers improve Midjourney recall without raising FPR. Otherwise document.

Status: shipped (2026-10-31) as a two-model agreement rule; see `docs/DETECTOR-BENCHMARK.md`. The benchmark
has 698 audited Commons originals: 169 AI (89 Midjourney) and 529 real, including 100 black-and-white and 100
graphics. It also has 570 degraded variants. Everything is split by source group into train, calibration and
locked test (seed 7). On its own, `haywoodsloan/ai-image-detector-deploy` (H, Apache-2.0) had the best recall,
but at 0.98 it called 2 of 157 locked-test real images "likely AI". The app now runs H and the previous model,
CommunityForensics (C), and needs them to agree:

- "Likely": H >= 0.98 and C >= 0.05.
- "Possible": H >= 0.98 or C >= 0.5. This threshold was chosen on calibration only, for a real-image rate of
  10% or less; it measured 1.2%.

Results:

| Split | Real false positives | AI recall | Midjourney recall |
|---|---:|---:|---:|
| Calibration | 0/160 | 36/49 | 21/26 |
| Locked test | 0/157 (Wilson upper bound about 2.4%) | 39/51 (76%) | 24/28 |
| Previous production rule (C >= 0.85), locked test | 0/157 | 57% | 18/28 |

The "likely" rule also fired on none of the 402 degraded real variants. At "possible", the locked test had 2/157
real images and 47/51 AI images.

Midjourney recall improved without raising the false-positive rate, so the shipping gate is met. Caveats: the
test set is small (51 AI), its images come from Commons rather than social media, GPT-image recall is weak, and
there are no adversarial tests.

History: the first 18-image smoke test found 83.3% recall at 0.5 and 61.1% at 0.85, but it had
no real controls and is not decision evidence. Independent review rejected the initial harness because loose
category search contaminated labels, individual thresholds were calibrated on their evaluation images, hard
real strata were excluded from calibration, model revisions were mutable, and score caches were not tied to
actual bytes. The replacement uses reviewed exact category roots, explicit human audit status, byte and
perceptual hashes, pinned safetensors-only model snapshots, and source-grouped train/calibration/locked-test
partitions.

## Limit 3 — no face-swap detection
Plan:
- Face detection/cropping with a permissive CPU model (OpenCV YuNet, MIT).
- Candidate face-forgery detectors with usable licences; eval on openly licensed data only
  (FaceForensics++/Celeb-DF/DFDC need signed agreements). Never create swaps of real identifiable people;
  synthetic faces only.
- Decision: ship / ship as experimental (off by default, never drives the verdict) / don't ship,
  based on measured FPR on real faces.

Status: researched; **do not ship** (2026-10-31). OpenCV YuNet is a suitable MIT-licensed CPU face cropper.
GenD is the only technically credible detector candidate found: its code and 1.2 GB CLIP-L/14 checkpoint are
labelled MIT, but it was trained on FaceForensics++, whose source-data terms are noncommercial research only.
The authors need to confirm commercial checkpoint rights. It also has no published CPU latency and is likely
too heavy for this synchronous CPU service. The established evaluation sets are agreement-gated,
noncommercial/academic-only, or have no usable licence; repacks cannot relicense those sources. Without a
source-audited open benchmark, the app cannot measure a production false-positive rate honestly.

The experiment may resume only after the checkpoint rights and dataset gates are cleared. Then benchmark
YuNet + GenD with identity/source-disjoint train, calibration and locked test groups, at least 500 real and
500 manipulated samples spanning three unseen methods, compression/resize/blur/pose/face-size slices, and a
2-vCPU latency/RSS test. Required gates: <=2% real-face FPR with a reported 95% interval, >=80% overall recall,
>=65% for each manipulation method, image p95 <3 s, eight-frame video p95 <15 s, and added RSS <1.5 GB. If all
pass, it begins as an opt-in experimental signal that never decides the verdict by itself.

Measured (2026-10-31, `docs/FACESWAP-EVAL.md`): real-face FPR *can* be measured with freely licensed Commons
portraits, so the benchmark was run with 550 real faces (267 source groups) and 640 swaps between synthetic
faces made with four classical methods. YuNet + GenD CLIP-L/14 failed the gates: test FPR 1.17% (Wilson 95%
upper 3.39%; 4.3% on half-size images), overall recall 67.7% (morph 48.8%), 0/8 frames of a real public deepfake
PSA flagged, added RSS 1.62 GB. Latency passed. **Still do not ship, not even opt-in.** Reopen if the GenD
authors confirm commercial rights, a larger real-face set (>1000, incl. phone/recompressed) and a licence-clean
set of modern face swaps become available, or a quantised model clears the RSS gate.
