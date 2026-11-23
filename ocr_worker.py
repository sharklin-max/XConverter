# -*- coding: utf-8 -*-
"""XConverter AI 增强包: 画面文字 OCR worker。
用法: ocr_worker.exe <frames_dir> <out_json>
对 frames_dir 下所有 .jpg 按文件名排序逐帧 OCR, 结果写 JSON:
[{"file": "f00001.jpg", "lines": [[text, conf], ...]}, ...]
进度以 "progress i/N" 行输出到 stdout。"""
import json
import os
import sys


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if len(sys.argv) < 3:
        print("usage: ocr_worker FRAMES_DIR OUT_JSON")
        return 2
    frames_dir, out_json = sys.argv[1], sys.argv[2]
    from rapidocr_onnxruntime import RapidOCR
    engine = RapidOCR()
    frames = sorted(f for f in os.listdir(frames_dir) if f.endswith(".jpg"))
    results = []
    for i, fn in enumerate(frames):
        try:
            res, _ = engine(os.path.join(frames_dir, fn))
        except Exception:
            res = None
        lines = []
        if res:
            for item in res:
                try:
                    text = str(item[1]).strip()
                    conf = float(item[2])
                except Exception:
                    continue
                lines.append([text, conf])
        results.append({"file": fn, "lines": lines})
        print(f"progress {i + 1}/{len(frames)}", flush=True)
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
