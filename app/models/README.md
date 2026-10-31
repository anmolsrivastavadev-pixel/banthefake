# Bundled model files

| File | Source | Licence | sha256 |
|---|---|---|---|
| `face_detection_yunet_2023mar.onnx` | [OpenCV Zoo — YuNet](https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet) | MIT (Copyright (c) 2020 Shiqi Yu) | `8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4` |

Used only by the opt-in experimental face-swap check (`BTF_FACESWAP=on`). Excluded from the Docker image
by `.dockerignore`; mount it and set `BTF_YUNET_PATH` if you enable the check.
