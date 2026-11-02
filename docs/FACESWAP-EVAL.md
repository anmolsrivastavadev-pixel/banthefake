# Face-swap detection evaluation (YuNet + GenD)

Date: 2026-10-31. Status: **evaluated; do not ship.** Gates failed (details below), so nothing was added to `app/`.

This follows "Limit 3" in `docs/LIMITS-PLAN.md`. The earlier write-up said the real-face false-positive rate
(FPR) "cannot be measured honestly". That was too strong. FPR needs only real faces, and Wikimedia Commons has
plenty of freely licensed portraits, so we measured it. Recall is still the weak point. No openly licensed
benchmark of in-the-wild face swaps exists, so recall below comes from swaps we made ourselves between
synthetic faces. Treat it as an optimistic proxy.

## 1. Licences

| Asset | Licence (verified 2026-10-31) | Notes |
|---|---|---|
| GenD code (github.com/yermandy/GenD) | MIT | |
| `yermandy/GenD_CLIP_L_14` @ `891ce014a0308386c4d7d25b3dcf436a22db5504` | Model card: `license: mit` | Trained on FaceForensics++ c23. The card says it is "the GenD (CLIP) model from Tab. 2", and the paper's Table 2 caption reads "models trained on the FF++ dataset". |
| `yermandy/GenD_PE_L` @ `4fd5a38c…` | Model card: MIT | Also trained on FF++ (Tab. 2). Backbone: Meta Perception Encoder. Needs `timm` and a repo-local import. |
| `yermandy/GenD_DINOv3_L` @ `7c6ce541…` | Model card: MIT | Also trained on FF++ (Tab. 2). Backbone: DINOv3, which ships under Meta's own DINOv3 licence. |
| CLIP ViT-L/14 backbone (`openai/clip-vit-large-patch14` @ `32bd6428…`) | The HF card has no licence tag. The OpenAI CLIP repo is MIT. | We use only its config and preprocessor. All weights come from the GenD checkpoint. |
| FaceForensics++ | "FaceForensics Terms of Use" (kaldir.vc.in.tum.de/faceforensics_tos.pdf): **"Researcher shall use the Database only for non-commercial research and educational purposes."** If the researcher works for a company, the employer is also bound. | The code is MIT. The data is not. |
| YuNet `face_detection_yunet_2023mar.onnx` (sha256 `8f2383e4…52fa4`, opencv_zoo @ `eb9f4f2d`) | **MIT**: "All files in this directory are licensed under MIT License", Copyright (c) 2020 Shiqi Yu. The opencv_zoo repo as a whole is Apache-2.0, but per-model licences take precedence. The training repo libfacedetection.train is now BSD-3-Clause. | The earlier "MIT vs Apache" confusion comes from that per-model vs whole-repo split. YuNet was trained on WIDER FACE, a research dataset. |

**Checkpoint choice.** All three published GenD checkpoints were trained on the same FF++ data. None has
less restrictive training terms. We chose **GenD_CLIP_L_14** because it has the simplest dependency path
(`transformers` only) and its backbone is MIT. The open legal question from Limit 3 is unchanged: the
weights are labelled MIT, but they come from non-commercial-only data. The authors still need to confirm
that commercial use of the checkpoint is allowed.

The official `modeling_gend.py` does not load under transformers 5.x. It calls `from_pretrained` inside
`__init__` while running under meta-device init. We therefore re-implemented the architecture
(CLIPVisionModel `pooler_output` → L2-norm → linear head) and load weights from the pinned safetensors. On the
FF++ sample frames in the GenD README, it gives P(fake)=0.967 for the DF frame and 0.319 for the real frame.

## 2. Data

All data came from Commons through a polite client: User-Agent `BanTheFake-eval/1.0 (anmol.srivastava.dev@gmail.com)`,
at most 2 requests/s, standard 1280 px thumbnails. Allowed licences: CC0, PD, CC BY and CC BY-SA. NC, ND, GFDL
and fair-use files were rejected. Every file has a manifest entry with source URL, licence, author, uploader and sha256.

* **Real faces: 550 images** (≥400 required) from 5 categories. Quality images of people: 150. Portrait
  photographs: 150. Black-and-white portrait photographs: 90. Group photographs: 90. Featured pictures of
  people: 70. Faces per image: 331 have 1, 74 have 2, 43 have 3, 19 have 4, 83 have 5 or more. Licences:
  CC BY-SA 288, PD 171, CC0 42, CC BY 49. Rejected candidates: 27 for licence, 56 for AI, painting,
  composite or retouch categories, and 189 with no face found.
  * **Source grouping.** We used union-find over normalised author, uploader and filename series, which gives
    267 groups. A hash of the group assigns it to a split: calibration has 294 images in 135 groups, test has
    256 images in 132 groups.
  * **Degraded variants** of every image: JPEG q60, 0.5× resize, and Gaussian blur with σ=1.
* **Synthetic faces (swap material): 70 of 138 candidates** from Commons AI-face categories (Midjourney,
  DALL-E, Stable Diffusion, SDXL, Flux, Artbreeder, GAN). Licences: 53 PD, 3 CC0, 14 CC BY-SA 4.0. Each
  candidate was reviewed by hand. We removed every named or real person and every derivative of a real photo
  (22 files, for example "Ann Graham Lotz stable diffusion", AI-"enhanced" portraits of real people, Mona
  Lisa, Lincoln). We also removed every non-photorealistic face (anime, paintings, creatures).
* **Manipulated faces: 640** images, 160 for each of four GAN-free methods that we implemented with OpenCV
  from YuNet's 5 landmarks. Swaps are made **only between synthetic faces** and only between roughly frontal
  faces of similar pose.
  1. `poisson`: similarity alignment plus an elliptical mask, then `seamlessClone(NORMAL_CLONE)`.
  2. `pwa_alpha`: Delaunay piecewise-affine warp, then Reinhard LAB colour transfer, then feathered alpha blending.
  3. `pyramid`: similarity alignment plus LAB transfer, then a 5-level Laplacian-pyramid blend.
  4. `morph`: a 50/50 identity morph (average shape, cross-dissolve), then `seamlessClone(MIXED_CLONE)`.
* **Control:** the 70 unswapped synthetic originals. This tests whether GenD reacts to the swap or just to the
  image being AI-generated.
* **In-the-wild (qualitative only, 10 frames from 3 files):** RepresentUs "Dictators – Kim Jong-Un"
  deepfake public-service video (CC BY 3.0, 8 frames); "DigiDoug DeepFake at TED2019" (CC BY 2.0); "Morphing
  madness" (CC BY-SA 2.0, the photographer's own morphs). We excluded "Face swap with Xavier.jpg" because it
  shows private individuals, not a public demo. Commons has no usable `Category:Deepfakes` corpus: the
  category is empty, and "Category:Deepfake" holds about 20 files, mostly audio and diagrams.

## 3. Method

YuNet (score ≥ 0.8, min face 32 px on a ≤1280 px working copy) finds faces. Each face is aligned with GenD's
own `detector.py::align_face` (5 points, 1.3× margin). The CLIP processor then resizes to 224 and
centre-crops. Per-face P(fake) is scored on the 4 largest faces. **Image score = max over faces.** This is the
pre-registered policy. "Largest face only" is also reported, post hoc. An image with no detected face cannot
be flagged.

The threshold is set on **calibration originals only**, as the lowest value giving ≤2% FPR (5 of 294).
Everything else is measured at that threshold on held-out data.

## 4. Results (primary policy: max over faces, threshold 0.962)

| Real-face FPR, test (n=256, 132 groups) | FP | FPR | Wilson 95% |
|---|---|---|---|
| original | 3 | 1.17% | 0.40–**3.39%** |
| JPEG q60 | 4 | 1.56% | 0.61–3.95% |
| 0.5× resize | 11 | **4.30%** | 2.42–7.53% |
| blur σ=1 | 8 | **3.12%** | 1.59–6.04% |

At 0.5× resize, 116 of 550 real images lose every face below 32 px. Those count as "not flagged".

| Recall on self-made swaps (n=160 each) | original | JPEG q60 | 0.5× | blur |
|---|---|---|---|---|
| poisson | 76.9% | 62.5% | 75.6% | 71.9% |
| pwa_alpha | 76.2% | 66.2% | 78.8% | 73.8% |
| pyramid | 68.8% | 52.5% | 69.4% | 64.4% |
| morph | **48.8%** | 20.6% | 43.8% | 41.9% |
| **overall (n=640)** | **67.7%** (63.9–71.2) | 50.5% | 66.9% | 63.0% |

* **Control.** Only 4.3% of unswapped synthetic faces are flagged. AUROC for unswapped vs swapped is 0.94, so
  GenD does respond to the swap, not just to the image being AI-generated. Ranking quality is high (AUROC
  real vs swaps: 0.94 for morph up to 0.98 for poisson). Recall is limited by the very high threshold that
  ≤2% FPR forces: 10% of real test faces score above 0.87.
* **Largest-face-only policy (post hoc):** test FPR 1.56% (Wilson upper bound 3.95%), recall 78.3% overall,
  morph 60.6%. It still fails both recall gates.
* **FPs** are real people photographed in crowds: small, blurred, background or partly occluded faces,
  sunglasses, deep shadow, black-and-white.
* **In the wild:** 0 of 8 RepresentUs deepfake frames were flagged (max 0.68). DigiDoug scored 0.97 under
  max-over-faces and was flagged. Morphing madness scored 0.43. The one real public deepfake in the set was missed entirely.

**CPU cost** (Apple-silicon arm64, torch 2.15, fp32, `torch.set_num_threads(2)`, end-to-end
decode → YuNet → align → GenD):

| | p50 | p95 |
|---|---|---|
| image, 1 face | 0.31 s | 0.32 s |
| image, 4 faces | 0.94 s | 1.06 s |
| video, 8 frames (1 face each, one batched GenD call) | 1.95 s | 2.06 s |
| added RSS after inference (vs. torch already imported) | **1.62 GB** (1.81 GB vs bare Python+cv2) | |

The weights are 1.2 GB of fp32 and are memory-mapped and assigned without a random init. A naive loader
peaked at 2.8–3.0 GB. Apple cores are much faster than a typical 2-vCPU x86 cloud box, so expect 2–4× higher
latency there. That should still fit the time gates, except possibly the 4-face image case.

## 5. Gates

| Gate | Result | Pass? |
|---|---|---|
| Real-face FPR ≤2% with 95% interval | 1.17% point, **Wilson upper bound 3.39%**. Degraded: 0.5× resize 4.3%, blur 3.1% | **FAIL** (point estimate passes, interval and degraded do not) |
| Overall recall ≥80% | 67.7%, on easier self-made swaps | **FAIL** |
| Per-method recall ≥65% | morph 48.8% | **FAIL** (poisson, pwa_alpha and pyramid pass) |
| Image p95 <3 s | 0.32 s (1 face), 1.06 s (4 faces) | pass (on this hardware) |
| 8-frame video p95 <15 s | 2.06 s | pass (on this hardware) |
| Added RSS <1.5 GB | 1.62 GB | **FAIL** (narrowly; fp16/int8 not evaluated) |
| ≥500 real / ≥500 manipulated, 3 unseen methods | 550 / 640 / 4 methods | met for the self-made set only |
| Checkpoint rights | MIT label, trained on non-commercial FF++ | **unresolved** |

**Decision: do not ship, not even as opt-in experimental.** No detector was added to the app. Even under the
most favourable reading (the largest-face policy chosen after the fact, recall
measured on our own crude swaps), recall stays below 80% and morph recall below 65%. The test FPR interval
cannot rule out roughly 3–4% of real photos being flagged, and degraded real images are worse. The only real
public deepfake in reach was missed.

## 6. Limitations

* The self-made swaps are **easier than and different from** real deepfakes. They are classical
  landmark-and-blend composites with visible seams, not GAN/diffusion face swaps or reenactment, and they use
  synthetic, mostly studio-like faces. Real-world recall is likely lower. The RepresentUs result points the same way.
* n=256 test reals cannot certify ≤2%. Even with 0 FPs the Wilson upper bound is 1.5%. Proving ≤2% at 95%
  confidence needs about 400–600 held-out real images with very few FPs.
* Commons "quality/featured" photos are cleaner than user uploads. FPR on phone selfies, beauty filters and
  messaging-app recompression is untested and probably higher.
* Latency was measured on Apple silicon with 2 torch threads, not on a 2-vCPU x86 container.

## 7. What would change the answer

1. The GenD authors confirm commercial use of the checkpoint, or a detector trained on permissively licensed data appears.
2. A larger real-face set (more than 1,000 images, including phone and compressed sources) to tighten the
   FPR interval, plus robustness to degraded inputs, for example calibrating on a mix of degraded variants.
3. A licence-clean set of modern (GAN/diffusion) swaps of consenting or synthetic identities to measure recall properly.
4. Re-evaluate in fp16/int8 to get under the RSS gate.

Reproduction scripts and raw outputs: `research/faceswap/` (see `report.md` there; git-ignored).

## Self-blended training with a licence-clean CLIP backbone (2026-10-31)

The second attempt trained CLIP ViT-B/16, whose code and weights are MIT-licensed, using the backbone
LayerNorms and a LayerNorm plus linear head. R3 also trained LoRA rank 8 adapters on the attention query and
value projections. Training used self-blended images made from the same real Commons face as each source: it
never blended in another person's face. The dataset contained 2,230 freely licensed real faces, with frozen
train, calibration and test splits that were disjoint by source group and identity. The locked test contained
640 swaps between synthetic faces, 160 each for morph, poisson, piecewise-affine alpha blending and
Laplacian-pyramid blending.

Criterion C was the recall on calibration self-blends at the threshold that kept every calibration real-image
variant at or below 2% FPR. It was declared before training and used to select the checkpoint; locked-test FPR
and swap recall did not affect selection.

| Run | Test FPR (Wilson 95%) | Overall recall | Morph | Poisson | PWA alpha | Pyramid | Image / 8-frame p95 | Added RSS | Criterion C |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| R1 `sbi2_b16` | 1.0% (0.5–2.2%) | 71.7% | 31.3% | 86.9% | 88.8% | 80.0% | 0.301 / 0.628 s | 1,169 MB | 44.5% |
| R2 `sbi3_b16` | 1.5% (0.8–2.9%) | 67.5% | 28.1% | 83.8% | 83.1% | 75.0% | 0.270 / 0.627 s | 1,136 MB | 42.6% |
| **R3 `sbi2_b16_lora8`** | **0.3% (0.1–1.2%)** | **35.9%** | **5.6%** | **48.8%** | **49.4%** | **40.0%** | **0.264 / 0.632 s** | **1,131 MB** | **57.5%** |

**Decision: DO NOT SHIP.** Selected R3 passed the real-face FPR gate (2/594, 0.3%; Wilson 95% 0.1–1.2%),
the image and eight-frame video latency gates, and the added-RSS gate. It failed the ≥80% overall-recall gate
at 35.9% and the ≥65% per-method gate for all four methods: morph 5.6%, poisson 48.8%, PWA alpha 49.4% and
pyramid 40.0%.

The pre-declared calibration criterion selected R3, but R3 was the worst run on the real-swap test. The LoRA
run overfit to self-blend artifacts that did not transfer to held-out swaps. Future rounds must therefore select
on a swap-based validation set that is disjoint from the confirmation set. The later Round 5 outcome is recorded
below.

## Round 5: validation promising, independence audit failed

Round 5 trained three registered CLIP ViT-B/16 candidates. Real face images were negative examples. The fake
examples were either self-blends made from one real face or swaps between synthetic adult faces; no real person's
face was swapped onto another person.

| Candidate | Registered training mix | `T_val` | Original `swaps_val` recall | Original `real_val` FPR | Trainable parameters |
|---|---|---:|---:|---:|---:|
| A `r5a_mix_sbi4_swaps` | 50% real / 25% SBI v4 self-blend / 25% synthetic swap | 0.9261 | 200/212 (94.34%) | 7/449 (1.56%) | 42,241 |
| **B `r5b_mix_swaps_only`** | 50% real / 0% self-blend / 50% synthetic swap | **0.6155** | **204/212 (96.23%)** | **4/449 (0.89%)** | **42,241** |
| C `r5c_sbi2_control` | 50% real / 50% SBI v2 self-blend / 0% synthetic swap | 0.9418 | 171/212 (80.66%) | 7/449 (1.56%) | 42,241 |

These are validation results only. The frozen selection rule chose candidate B because it had the highest original
`swaps_val` hit count. Its checkpoint SHA-256 was
`13dfa8a5d14787beb0ccec5371b997d233df0f4658c69416d7ef23c72334f700`.

The mandatory independent leakage audit reported **`AUDIT: FAIL`**. It found:

- 6 expected synthetic training/fresh-swap images and 56 expected real training/fresh-real images produced no
  SFace embedding;
- the real-training-to-fresh-real maximum cosine was 0.6238467693328857, above the required strict bound of 0.6;
- the all-training-to-all-fresh maximum cosine was also 0.6238467693328857, above the same strict bound; and
- 19 normalised real-uploader values were shared between training and fresh data.

The frozen-seal, file-independence, membership-refusal, augmentation-parity and artifact-binding checks passed,
but they do not override the failed identity- and group-independence checks. The required audit precondition was
therefore not met, so the confirmation test was deliberately not run. Face-swap detection stays off.

The Round 5 pre-registration says a substantive leak cannot be repaired by deleting examples after validation
results have been seen. Round 5 is closed as validation-promising but unconfirmed. Any further attempt requires a
new pre-registered round with new validation and confirmation data, with the stricter identity and uploader checks
applied before training or selection. The current unused confirmation set must not be used for tuning. These
validation results do not measure modern neural face swaps, reenactment, GAN edits or diffusion edits, do not
establish general deepfake-detection performance, and do not prove that any file is real.
