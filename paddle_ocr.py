"""PaddleOCR runner: prints the words in an image as JSON lines with boxes
(first line: {"width", "height"}; then {"t", "x", "y", "w", "h"} per word).

Runs inside .venv-ocr (see setup_paddleocr.ps1) as its own process, so the
large OCR models are only in memory while a flyer is being read.
Usage: .venv-ocr\\Scripts\\python.exe paddle_ocr.py <image> [<image> ...] [--crop x,y,w,h]
Several images are separated by a {"page": n} line.
"""

import json
import os
import re
import sys

os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")  # skip the slow online check

import cv2  # noqa: E402  (installed with paddleocr)
import numpy as np  # noqa: E402
from paddleocr import PaddleOCR  # noqa: E402

MIN_SCORE = 0.5


def split_words(text):
    """'Reg:$1.59' / 'save$0.60' -> separate tokens so prices stand alone."""
    out = []
    for tok in text.split():
        out += [p for p in re.split(r"(?<=[A-Za-z:])(?=\$)", tok) if p]
    return out


def words_in(ocr, path, crop=None):
    img = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)  # handles non-ASCII paths
    if img is None:
        raise SystemExit(f"Can't read image: {path}")
    h, w = img.shape[:2]
    print(json.dumps({"width": w, "height": h}))
    ox = oy = 0
    scale = 1.0
    if crop:  # read just this area, enlarged for sharper text; boxes are still reported in page pixels
        ox, oy = max(0, crop[0]), max(0, crop[1])
        cw, ch = min(crop[2], w - ox), min(crop[3], h - oy)
        img = img[oy:oy + ch, ox:ox + cw]
        scale = max(1.0, min(3.0, 2500 / max(cw, ch)))  # up to 3x for a single panel
        if scale > 1:
            img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    for res in ocr.predict(img):
        for text, score, box in zip(res["rec_texts"], res["rec_scores"], res["rec_boxes"]):
            if score < MIN_SCORE or not text.strip():
                continue
            x1, y1, x2, y2 = (int(v / scale) for v in box)
            x1, x2, y1, y2 = x1 + ox, x2 + ox, y1 + oy, y2 + oy
            toks = split_words(text)
            # spread the line's width over its words by character count
            total = sum(len(t) for t in toks) + len(toks) - 1
            x = x1
            for t in toks:
                tw = max(1, round((x2 - x1) * len(t) / max(total, 1)))
                print(json.dumps({"t": t, "x": x, "y": y1, "w": tw, "h": y2 - y1}, ensure_ascii=False))
                x += tw + round((x2 - x1) / max(total, 1))


def main(paths):
    sys.stdout.reconfigure(encoding="utf-8")
    ocr = PaddleOCR(
        lang="ch",  # Chinese + English model
        text_detection_model_name="PP-OCRv5_mobile_det",
        text_recognition_model_name="PP-OCRv5_mobile_rec",
        use_doc_orientation_classify=False, use_doc_unwarping=False, use_textline_orientation=False,
        enable_mkldnn=True,  # needs paddlepaddle 3.2.x: 3.3's oneDNN path crashes on Windows
    )
    crop = None
    if "--crop" in paths:  # --crop x,y,w,h
        i = paths.index("--crop")
        crop = [int(v) for v in paths[i + 1].split(",")]
        paths = paths[:i] + paths[i + 2:]
    for n, p in enumerate(paths):
        if n:
            print(json.dumps({"page": n}))
        words_in(ocr, p, crop)


if __name__ == "__main__":
    main(sys.argv[1:])
