"""PaddleOCR runner: prints the words in an image as JSON lines with boxes,
in the same format as ocr.ps1 (first line: {"width", "height"}).

Runs inside .venv-ocr (see setup_paddleocr.ps1) as its own process, so the
large OCR models are only in memory while a flyer is being read.
Usage: .venv-ocr\\Scripts\\python.exe paddle_ocr.py <image> [<image> ...]
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


def words_in(ocr, path):
    img = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)  # handles non-ASCII paths
    if img is None:
        raise SystemExit(f"Can't read image: {path}")
    h, w = img.shape[:2]
    print(json.dumps({"width": w, "height": h}))
    for res in ocr.predict(img):
        for text, score, box in zip(res["rec_texts"], res["rec_scores"], res["rec_boxes"]):
            if score < MIN_SCORE or not text.strip():
                continue
            x1, y1, x2, y2 = (int(v) for v in box)
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
    for n, p in enumerate(paths):
        if n:
            print(json.dumps({"page": n}))
        words_in(ocr, p)


if __name__ == "__main__":
    main(sys.argv[1:])
