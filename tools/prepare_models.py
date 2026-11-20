"""คัดลอกเฉพาะโมเดล OCR ที่ใช้ (PP-OCRv5 mobile: ตรวจจับ + จีน/ญี่ปุ่น/อังกฤษ + ไทย) ไปไว้สำหรับ build"""
import os, shutil, sys
import rapidocr

NEED = ["ch_PP-OCRv5_det_mobile.onnx", "ch_PP-OCRv5_rec_mobile.onnx", "th_PP-OCRv5_rec_mobile.onnx",
        "ch_ppocr_mobile_v2.0_cls_mobile.onnx"]
src = os.path.join(os.path.dirname(rapidocr.__file__), "models")
dst = sys.argv[1]
os.makedirs(dst, exist_ok=True)
for f in os.listdir(dst):
    os.remove(os.path.join(dst, f))
missing = [n for n in NEED if not os.path.exists(os.path.join(src, n))]
if missing:
    # ให้ rapidocr ดาวน์โหลดโมเดลที่ยังไม่มี
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
    import numpy as np, core
    img = np.full((64, 256, 3), 255, np.uint8)
    core.OCR.run(img, "ch")
    core.OCR.run(img, "th")
for n in NEED:
    shutil.copy2(os.path.join(src, n), os.path.join(dst, n))
# ไฟล์ตั้งค่าของ rapidocr -> โฟลเดอร์ rapid ข้างโฟลเดอร์โมเดล
pkg = os.path.dirname(rapidocr.__file__)
rd = os.path.join(os.path.dirname(dst), "rapid")
os.makedirs(os.path.join(rd, "nets"), exist_ok=True)
shutil.copy2(os.path.join(pkg, "config.yaml"), rd)
shutil.copy2(os.path.join(pkg, "default_models.yaml"), rd)
shutil.copy2(os.path.join(pkg, "inference_engine", "pytorch", "networks", "arch_config.yaml"), os.path.join(rd, "nets"))
print("models ok:", ", ".join(NEED))
