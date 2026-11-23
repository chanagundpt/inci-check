"""2NK INCI Check — แกนประมวลผล: อ่านไฟล์, OCR, ดึงรายชื่อสาร, จับคู่ฉลาก↔สูตร, ตรวจสารห้ามใช้"""
import base64, csv, io, json, os, re, sys, threading, time, unicodedata

import pymupdf
from rapidfuzz import fuzz

APP_NAME = "2NK INCI Check"
VERSION = "1.2.3"

if getattr(sys, "frozen", False):
    BASE = sys._MEIPASS
else:
    BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, "data")
USER_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), APP_NAME)
os.makedirs(USER_DIR, exist_ok=True)

CAS_RE = re.compile(r"\b\d{2,7}-\d{2}-\d\b")
IMG_EXT = (".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp", ".gif")
DASHES = "‐‑‒–—―−ー－ｰ﹣"
# ตัวอักษรที่ OCR สับสนบ่อย (ใช้เฉพาะตอนเปรียบเทียบ): คันจิ↔คาตาคานะหน้าตาเหมือนกัน, คานะตัวเล็ก↔ตัวใหญ่, ノ↔/
OCR_SAME = str.maketrans({"八": "ハ", "力": "カ", "口": "ロ", "二": "ニ", "工": "エ", "一": "-", "卜": "ト", "夕": "タ",
                          "ァ": "ア", "ィ": "イ", "ゥ": "ウ", "ェ": "エ", "ォ": "オ", "ャ": "ヤ", "ュ": "ユ", "ョ": "ヨ",
                          "ッ": "ツ", "ヮ": "ワ", "ヵ": "カ", "ヶ": "ケ", "ノ": "/", "／": "/"})


# ---------------------------------------------------------------- utils
def norm(s):
    """ทำชื่อให้อยู่รูปเดียวกันสำหรับเปรียบเทียบ (ไม่สนช่องว่าง/ขีด/ตัวพิมพ์)"""
    s = unicodedata.normalize("NFKC", s or "")
    for ch in DASHES:
        s = s.replace(ch, "-")
    s = re.sub(r"\*\s*\d*|※\s*\d*|[†‡]", "", s)
    s = s.upper()
    s = "".join(chr(ord(c) + 0x60) if "ぁ" <= c <= "ゖ" else c for c in s)  # ひらがな→カタカナ (OCR สับสน へ/ヘ)
    s = s.replace("Γ", "G").replace("γ".upper(), "G")
    s = s.translate(OCR_SAME)
    s = re.sub(r"[\s　]+", "", s)
    s = s.strip(".:;,、。・ ")
    return s


def norm_latin(s):
    """ชื่ออังกฤษ: ตัวพิมพ์เล็ก ตัดเครื่องหมาย เหลือช่องว่างเดียว"""
    s = unicodedata.normalize("NFKC", s or "").lower()
    for ch in DASHES:
        s = s.replace(ch, "-")
    s = re.sub(r"\(inci\)|\(iso\)", "", s)
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s


def has_cjk(s):
    return bool(re.search(r"[぀-ヿ一-鿿]", s or ""))


def has_thai(s):
    return bool(re.search(r"[฀-๿]", s or ""))


def union(rects):
    rects = [r for r in rects if r]
    if not rects:
        return None
    return [min(r[0] for r in rects), min(r[1] for r in rects), max(r[2] for r in rects), max(r[3] for r in rects)]


# ---------------------------------------------------------------- OCR
class OCR:
    _lock = threading.Lock()
    _eng = {}

    @classmethod
    def engine(cls, lang):
        from rapidocr import RapidOCR, LangRec, OCRVersion, ModelType
        with cls._lock:
            if lang not in cls._eng:
                cls._eng[lang] = RapidOCR(params={
                    "Global.log_level": "error",
                    "Rec.lang_type": {"th": LangRec.TH, "en": LangRec.EN}.get(lang, LangRec.CH),
                    "Rec.ocr_version": OCRVersion.PPOCRV5, "Rec.model_type": ModelType.MOBILE,
                    "Det.ocr_version": OCRVersion.PPOCRV5, "Det.model_type": ModelType.MOBILE,
                })
            return cls._eng[lang]

    @classmethod
    def run(cls, img, lang):
        # ต้องระบุ use_* ทุกครั้ง: rapidocr จำค่าจากการเรียกครั้งก่อน
        r = cls.engine(lang)(img, use_det=True, use_cls=False, use_rec=True, return_word_box=True)
        out = []
        if r.boxes is None:
            return out
        words = r.word_results or [()] * len(r.txts)
        for box, txt, sc, ws in zip(r.boxes, r.txts, r.scores, words):
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            out.append({"text": txt, "score": float(sc), "box": [float(min(xs)), float(min(ys)), float(max(xs)), float(max(ys))],
                        "words": [(w[0], [float(min(p[0] for p in w[2])), float(min(p[1] for p in w[2])),
                                          float(max(p[0] for p in w[2])), float(max(p[1] for p in w[2]))]) for w in ws if w and w[0]]})
        return out

    @classmethod
    def detect(cls, img):
        r = cls.engine("ch")(img, use_det=True, use_cls=False, use_rec=False)
        return [] if r.boxes is None else [list(map(list, b)) for b in r.boxes]

    @classmethod
    def rec(cls, crop, lang):
        r = cls.engine(lang)(crop, use_det=False, use_cls=False, use_rec=True)
        if not r.txts:
            return "", 0.0
        return r.txts[0], float(r.scores[0])


def _crop_quad(img, quad, pad=3):
    """ตัดบรรทัดตามกรอบสี่เหลี่ยม (ปรับเอียงให้ตรง)"""
    import cv2, numpy as np
    q = np.array(quad, dtype=np.float32)
    w = int(max(np.linalg.norm(q[0] - q[1]), np.linalg.norm(q[2] - q[3]))) + 2 * pad
    h = int(max(np.linalg.norm(q[0] - q[3]), np.linalg.norm(q[1] - q[2]))) + 2 * pad
    if w < 4 or h < 4:
        return None
    dst = np.array([[pad, pad], [w - pad, pad], [w - pad, h - pad], [pad, h - pad]], dtype=np.float32)
    m = cv2.getPerspectiveTransform(q, dst)
    return cv2.warpPerspective(img, m, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)


def _variants(c):
    """ภาพบรรทัดหลายแบบ: เพิ่มความคมชัด / ขยาย / ช่องสีเขียว (พื้นแดง) — ช่วยฉลากถ่ายจากของจริง"""
    import cv2
    g = cv2.cvtColor(c, cv2.COLOR_RGB2GRAY)
    up = lambda x: cv2.resize(x, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    gray3 = lambda x: cv2.cvtColor(x, cv2.COLOR_GRAY2RGB)
    yield up(c)
    yield up(gray3(cv2.createCLAHE(clipLimit=3.0, tileGridSize=(4, 4)).apply(g)))
    yield up(gray3(cv2.normalize(g, None, 0, 255, cv2.NORM_MINMAX)))
    yield up(gray3(c[:, :, 1].copy()))


def _read_line(crop, allow_th):
    """อ่านบรรทัดเดียวด้วยหลายโมเดล/หลายภาพ เลือกผลที่มั่นใจที่สุด"""
    best = (*OCR.rec(crop, "ch"), "ch")
    if has_cjk(best[0]) and best[1] >= 0.9:
        return best
    cands = [best]
    for lg in ([] if has_cjk(best[0]) else ["en"] + (["th"] if allow_th else [])):
        t, s = OCR.rec(crop, lg)
        if lg == "th" and not has_thai(t):
            continue
        cands.append((t, s, lg))
    top = max(cands, key=lambda c: c[1])
    if top[1] < 0.96:
        for v in _variants(crop):
            for lg in {top[2], "en"} if not has_cjk(top[0]) and top[2] != "th" else {top[2]}:
                t, s = OCR.rec(v, lg)
                if lg == "th" and not has_thai(t):
                    continue
                cands.append((t, s, lg))
        top = max(cands, key=lambda c: c[1])
    # ภาษาอังกฤษ: โมเดล en มีช่องว่างระหว่างคำ ใช้ถ้าคะแนนใกล้เคียง
    if top[2] == "ch" and not has_cjk(top[0]):
        en = max((c for c in cands if c[2] == "en"), key=lambda c: c[1], default=None)
        if en and en[1] >= top[1] - 0.04:
            top = en
    return top


def iou(a, b):
    x0, y0, x1, y1 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    inter = (x1 - x0) * (y1 - y0)
    return inter / ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter)


def _regions(boxes, gap):
    """รวมกล่องบรรทัดที่อยู่ใกล้กันเป็นบริเวณข้อความ"""
    regs = [list(b) for b in boxes]
    changed = True
    while changed:
        changed = False
        out = []
        for b in regs:
            for o in out:
                if b[0] - gap < o[2] and o[0] - gap < b[2] and b[1] - gap < o[3] and o[1] - gap < b[3]:
                    o[0], o[1], o[2], o[3] = min(o[0], b[0]), min(o[1], b[1]), max(o[2], b[2]), max(o[3], b[3])
                    changed = True
                    break
            else:
                out.append(b)
        regs = out
    return regs


def ocr_page(page, lang="auto", progress=None):
    """OCR 2 รอบ: รอบแรกหาบริเวณข้อความ รอบสองซูมอ่านบริเวณที่ตัวอักษรเล็ก"""
    import numpy as np
    w, h = page.rect.width, page.rect.height
    dpi1 = max(72, min(300, int(3000 / (max(w, h) / 72))))
    # รอบแรก: หาเฉพาะตำแหน่งบรรทัด (ยังไม่อ่าน) เพื่อรู้ขนาดตัวอักษรแต่ละบริเวณ
    pix = page.get_pixmap(dpi=dpi1, alpha=False)
    img = np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w, pix.n)[:, :, :3].copy()
    s1 = 72.0 / dpi1
    first = []
    for q in OCR.detect(img):
        xs, ys = [p[0] * s1 for p in q], [p[1] * s1 for p in q]
        first.append([min(xs), min(ys), max(xs), max(ys)])
    if not first:
        return []
    hts = sorted(b[3] - b[1] for b in first)
    gap = max(4.0, hts[len(hts) // 2] * 1.5)
    regs = _regions(first, gap)
    out = []
    for ri, rg in enumerate(regs):
        inside = [b for b in first if b[0] >= rg[0] - 0.5 and b[2] <= rg[2] + 0.5 and b[1] >= rg[1] - 0.5 and b[3] <= rg[3] + 0.5]
        lh = sorted(b[3] - b[1] for b in inside)[len(inside) // 2] if inside else 10
        target = min(1000, int(42 * 72 / max(lh, 0.5)))
        rw, rh = rg[2] - rg[0] + 8, rg[3] - rg[1] + 8
        target = max(dpi1, min(target, int(4200 * 72 / max(rw, rh))))
        if progress:
            progress("อ่านข้อความบริเวณ %d/%d…" % (ri + 1, len(regs)))
        clip = pymupdf.Rect(rg[0] - 4, rg[1] - 4, rg[2] + 4, rg[3] + 4) & page.rect
        out += _ocr_clip(page, clip, target, lang, progress)
    out.sort(key=lambda l: (round(l["box"][1] / 4), l["box"][0]))
    return out


def _ocr_clip(page, clip, dpi, lang, progress):
    import numpy as np
    pix = page.get_pixmap(dpi=dpi, alpha=False, clip=clip)
    img = np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w, pix.n)[:, :, :3].copy()
    sc = 72.0 / dpi
    ox, oy = (clip.x0, clip.y0) if clip is not None else (0, 0)
    tr = lambda b: [b[0] * sc + ox, b[1] * sc + oy, b[2] * sc + ox, b[3] * sc + oy]
    lines = []
    full = OCR.run(img, "th" if lang == "th" else "ch")   # อ่านทั้งบริเวณ (มีตำแหน่งรายตัวอักษร)
    for q in OCR.detect(img):
        xs, ys = [float(p[0]) for p in q], [float(p[1]) for p in q]   # float ปกติ (numpy ส่งเป็น JSON ไม่ได้)
        qb = [min(xs), min(ys), max(xs), max(ys)]
        f = max(full, key=lambda r: iou(r["box"], qb), default=None)
        if f is not None and iou(f["box"], qb) < 0.5:
            f = None
        if f and f["score"] >= 0.92 and (has_cjk(f["text"]) or lang == "th"):
            t, s, words = f["text"], f["score"], f["words"]       # ภาษาญี่ปุ่น/จีนที่มั่นใจแล้ว ใช้ผลเดิม
        else:
            crop = _crop_quad(img, q)
            if crop is None:
                continue
            t, s, _ = _read_line(crop, allow_th=(lang == "auto"))
            words = []
            if f and f["score"] > s:
                t, s, words = f["text"], f["score"], f["words"]
        t = t.strip()
        if not t or s < 0.3:
            continue
        box = tr(qb)
        chars = []
        if words and "".join(w for w, _ in words) == t.replace(" ", ""):
            for wtxt, wb in words:
                wb = tr(wb)
                n = max(1, len(wtxt))
                chars += [(ch, [wb[0] + (wb[2] - wb[0]) * i / n, wb[1], wb[0] + (wb[2] - wb[0]) * (i + 1) / n, wb[3]])
                          for i, ch in enumerate(wtxt)]
            t = "".join(c for c, _ in chars)
        else:
            n = max(1, len(t))
            chars = [(ch, [box[0] + (box[2] - box[0]) * i / n, box[1], box[0] + (box[2] - box[0]) * (i + 1) / n, box[3]])
                     for i, ch in enumerate(t)]
        lines.append({"text": t, "box": box, "chars": chars, "score": float(s)})
    return lines


# ---------------------------------------------------------------- documents
class Doc:
    def __init__(self, path, progress=None, ocr_lang="auto", force_ocr=False):
        self.path = path
        self.name = os.path.basename(path)
        self.kind = "pdf"
        self.pdf = None
        self.table = None      # xlsx/csv: list of rows
        self.lines = []        # [{page, text, box, chars:[(ch, box)]}]
        self.source = "text"
        self.ocr_pages = []
        ext = os.path.splitext(path)[1].lower()
        if ext in (".xlsx", ".xlsm", ".xls", ".csv"):
            self.kind = "table"
            self._load_table(path, ext)
            return
        if ext in IMG_EXT:
            src = pymupdf.open(path)
            self.pdf = pymupdf.open("pdf", src.convert_to_pdf())
            self.kind = "image"
        else:
            self.pdf = pymupdf.open(path)
            if self.pdf.needs_pass:
                raise ValueError("ไฟล์ PDF มีรหัสผ่าน — ปลดรหัสก่อนแล้วเลือกใหม่")
        for pg in self.pdf:
            if pg.rotation:
                pg.remove_rotation()
        for pi, pg in enumerate(self.pdf):
            lines = [] if force_ocr else self._text_lines(pg, pi)
            if sum(len(l["text"]) for l in lines) < 15:
                if progress:
                    progress("กำลังอ่านข้อความจากรูป (OCR) หน้า %d/%d…" % (pi + 1, len(self.pdf)))
                lines = ocr_page(pg, ocr_lang, progress)   # ชิ้นข้อความดิบ (ต่อเป็นบรรทัดตอนหารายการส่วนประกอบ)
                for l in lines:
                    l["page"] = pi
                self.ocr_pages.append(pi)
            self.lines += lines
        if self.ocr_pages:
            self.source = "ocr"

    def _text_lines(self, pg, pi):
        out = []
        raw = pg.get_text("rawdict", flags=pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_MEDIABOX_CLIP)
        for b in raw["blocks"]:
            for l in b.get("lines", []):
                chars = []
                for s in l["spans"]:
                    for c in s["chars"]:
                        chars.append((c["c"], list(c["bbox"])))
                txt = "".join(c for c, _ in chars)
                if txt.strip():
                    out.append({"page": pi, "text": txt, "box": list(l["bbox"]), "chars": chars})
        out.sort(key=lambda l: (round(l["box"][1] / 3), l["box"][0]))
        return out

    def _load_table(self, path, ext):
        rows = []
        if ext == ".csv":
            with open(path, encoding="utf-8-sig", errors="replace") as f:
                rows = [r for r in csv.reader(f)]
        elif ext == ".xls":
            raise ValueError("ไฟล์ .xls รุ่นเก่า — กรุณาบันทึกเป็น .xlsx ใน Excel ก่อน")
        else:
            import openpyxl
            wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
            best = None
            for ws in wb.worksheets:
                rr = [["" if v is None else str(v).strip() for v in row] for row in ws.iter_rows(values_only=True)]
                rr = [r for r in rr if any(r)]
                score = sum(1 for r in rr for v in r if re.search(r"INCI|成分|ส่วนประกอบ|Ingredient", v, re.I))
                if best is None or score > best[0] or (score == best[0] and len(rr) > len(best[1])):
                    best = (score, rr)
            rows = best[1] if best else []
        w = max((len(r) for r in rows), default=0)
        self.table = [r + [""] * (w - len(r)) for r in rows]
        for ri, r in enumerate(self.table):
            for ci, v in enumerate(r):
                if v:
                    self.lines.append({"page": -1, "text": v, "cell": [ri, ci], "box": None, "chars": []})

    # ------------------------------------------------ view
    def view(self, dpi=110):
        if self.kind == "table":
            return {"kind": "table", "name": self.name, "rows": self.table, "source": "table"}
        pages = []
        for pg in self.pdf:
            z = dpi / 72
            # ไฟล์ที่ต้อง OCR (อาร์ตเวิร์กตัวเล็ก) ใช้ภาพละเอียดขึ้นเพื่อซูมดูได้
            cap = 4600 if self.ocr_pages else 2600
            z = max(z, cap / max(pg.rect.width, pg.rect.height)) if self.ocr_pages else z
            if max(pg.rect.width, pg.rect.height) * z > cap:
                z = cap / max(pg.rect.width, pg.rect.height)
            pix = pg.get_pixmap(matrix=pymupdf.Matrix(z, z), alpha=False)
            if max(pix.width, pix.height) > 2000:
                data = "data:image/jpeg;base64," + base64.b64encode(pix.tobytes("jpeg", jpg_quality=88)).decode()
            else:
                data = "data:image/png;base64," + base64.b64encode(pix.tobytes("png")).decode()
            pages.append({"w": pg.rect.width, "h": pg.rect.height, "img": data})
        return {"kind": self.kind, "name": self.name, "pages": pages, "source": self.source,
                "ocr_pages": self.ocr_pages}

    def full_text(self):
        return "\n".join(l["text"] for l in self.lines)


def mark(page, rect=None, cell=None):
    if cell is not None:
        return {"page": -1, "cell": cell}
    return {"page": page, "rect": [round(v, 2) for v in rect]}


def char_marks(seq, a, b):
    """seq = [(ch, page, box, lineid)] → กล่องต่อบรรทัดของช่วงตัวอักษร a..b"""
    groups = {}
    for ch, page, box, lid in seq[a:b]:
        if box is None or ch.isspace():
            continue
        groups.setdefault((page, lid), []).append(box)
    return [mark(p, union(bs)) for (p, _), bs in groups.items()]


# ---------------------------------------------------------------- formula
HDR = {
    "inci": r"INCI|Ingredient\s*name|Ingredients?(?!.*Japan)|ชื่อสาร|Component|Chemical\s*Name|Raw\s*Material|Substance",
    "jp": r"Japanese|成分名|表示名|和名|日本",
    "th": r"ภาษาไทย|Thai",
    "cas": r"CAS",
    "pct": r"%|％|wt|w/w|Conc|Percent|含有|配合量|ปริมาณ|ร้อยละ",
    "func": r"Function|Usage|Purpose|配合目的|機能|目的|หน้าที่",
    "no": r"^No\.?$|^#$|ลำดับ|^番号",
}


def _map_header(cells):
    """cells = [(text, idx)] → {key: idx}"""
    m = {}
    for t, i in cells:
        t = (t or "").strip()
        if not t or len(t) > 60:
            continue
        for k, pat in HDR.items():
            if k not in m and re.search(pat, t, re.I):
                if k == "inci" and re.search(HDR["jp"], t, re.I):
                    continue
                if k == "pct" and re.search(r"CAS|INCI", t, re.I):
                    continue
                m[k] = i
                break
    return m


def _clean_pct(s):
    s = re.sub(r"\s+", "", s or "").replace("％", "%")
    m = re.search(r"\d+(?:\.\d+)?", s)
    return float(m.group()) if m else None


def respace(s):
    """OCR มักทิ้งช่องว่างในชื่ออังกฤษ: "PropyleneGlycol" → "Propylene Glycol", "Peg-20Stearate" → "Peg-20 Stearate" """
    s = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", s)
    s = re.sub(r"(?<=[0-9)])(?=[A-Z][a-z])", " ", s)
    s = re.sub(r"(?<=[A-Za-z])\(", " (", s)
    return re.sub(r"\s+", " ", s).strip()


WATERMARK = re.compile(r"^[A-Z]{1,4}\W*$")   # ลายน้ำ/เศษตัวอักษร เช่น TRT, TR, Q


def _ocr_formula(doc):
    """ตารางสูตร/MSDS จากหน้าที่เป็นรูป (OCR): หาแถวหัวตาราง (Chemical Name / % / CAS) แล้วแบ่งคอลัมน์ตามตำแหน่งหัวตาราง"""
    out = []
    for pi in doc.ocr_pages:
        frags = [l for l in doc.lines if l.get("page") == pi and l.get("box") and l["text"].strip()]
        if not frags:
            continue
        hs = sorted(l["box"][3] - l["box"][1] for l in frags)
        h = hs[len(hs) // 2] or 10
        # จัดเป็นแถวตามกึ่งกลางแนวตั้ง
        rows = []
        for l in sorted(frags, key=lambda l: (l["box"][1] + l["box"][3]) / 2):
            cy = (l["box"][1] + l["box"][3]) / 2
            if rows and abs(cy - rows[-1]["cy"]) <= h * 0.55:
                rows[-1]["f"].append(l)
                rows[-1]["cy"] = sum((x["box"][1] + x["box"][3]) / 2 for x in rows[-1]["f"]) / len(rows[-1]["f"])
            else:
                rows.append({"cy": cy, "f": [l]})
        ri = 0
        while ri < len(rows):
            hdr = sorted(rows[ri]["f"], key=lambda l: l["box"][0])
            m = _map_header([(l["text"], i) for i, l in enumerate(hdr)])
            if "inci" not in m or not ("pct" in m or "cas" in m):
                ri += 1
                continue
            cols = sorted(((k, (hdr[i]["box"][0] + hdr[i]["box"][2]) / 2, hdr[i]["box"]) for k, i in m.items()), key=lambda c: c[1])
            bounds = []
            for j, (k, cx, bx) in enumerate(cols):
                lo = (cols[j - 1][1] + cx) / 2 if j else bx[0] - (bx[2] - bx[0]) * 2.5
                hi = (cx + cols[j + 1][1]) / 2 if j + 1 < len(cols) else bx[2] + (bx[2] - bx[0]) * 0.8
                bounds.append((k, lo, hi))
            last_y, pitch, empty = rows[ri]["cy"], None, 0
            for rj in range(ri + 1, len(rows)):
                r = rows[rj]
                if pitch and r["cy"] - last_y > pitch * 3.5:
                    break
                cell = {}
                for l in r["f"]:
                    t = l["text"].strip()
                    cx = (l["box"][0] + l["box"][2]) / 2
                    k = next((k for k, lo, hi in bounds if lo <= cx < hi), None)
                    if not k or WATERMARK.match(t):
                        continue
                    if k == "cas" and not (CAS_RE.search(t) or re.match(r"^(mix|-|/|n/?a)", t, re.I)):
                        continue
                    if k == "pct" and not re.search(r"\d", t):
                        continue
                    if k == "inci" and (len(re.sub(r"[^A-Za-z]", "", t)) < 3 or re.match(r"^section\b", t, re.I)):
                        continue
                    cell.setdefault(k, []).append(l)
                if "inci" not in cell:
                    empty += 1
                    if empty >= 2 or re.search(r"section", " ".join(l["text"] for l in r["f"]), re.I):
                        break
                    continue
                empty = 0
                pitch = r["cy"] - last_y if pitch is None else min(pitch, r["cy"] - last_y) if r["cy"] - last_y > h * 0.6 else pitch
                last_y = r["cy"]
                name = respace(" ".join(l["text"] for l in sorted(cell["inci"], key=lambda l: l["box"][0])))
                if re.match(r"^(total|รวม|合計)", name, re.I):
                    continue
                txt = lambda k: " ".join(l["text"] for l in cell.get(k, []))
                cas_t = txt("cas")
                out.append({"inci": name, "jp": "", "th": "", "cas": CAS_RE.findall(cas_t), "cas_text": cas_t,
                            "pct": _clean_pct(txt("pct")), "func": respace(txt("func")),
                            "marks": {"inci": [mark(pi, union([l["box"] for l in cell["inci"]]))]},
                            "row_marks": [mark(pi, union([l["box"] for l in r["f"] if not WATERMARK.match(l["text"].strip())]))]})
            ri += 1
    return out


def extract_formula(doc):
    """คืน list ของแถวสูตร หรือ None ถ้าไม่พบตาราง"""
    rows = []
    if doc.kind == "table":
        t = doc.table
        # หัวตาราง = แถวที่จับคู่หัวคอลัมน์ได้มากที่สุด (กันแถวชื่อเอกสาร เช่น "CERTIFICATE OF INGREDIENTS")
        cand = [(len(m), hi, m) for hi in range(min(15, len(t)))
                for m in [_map_header([(v, i) for i, v in enumerate(t[hi])])]
                if ("inci" in m or "jp" in m) and len(m) >= 2]
        for _, hi, m in sorted(cand, key=lambda c: (-c[0], c[1]))[:1]:
            if True:
                for ri in range(hi + 1, len(t)):
                    r = t[ri]
                    inci = r[m["inci"]].strip() if "inci" in m else ""
                    jp = r[m["jp"]].strip() if "jp" in m else ""
                    if not (inci or jp) or re.match(r"^(total|รวม|合計)", inci + jp, re.I):
                        continue
                    # แถวท้ายตาราง (ชื่อบริษัท/หมายเหตุ) ไม่มีทั้ง % และ CAS
                    if "pct" in m and not str(r[m["pct"]]).strip() and not ("cas" in m and str(r[m["cas"]]).strip()):
                        continue
                    row = {"inci": inci, "jp": jp, "th": r[m["th"]].strip() if "th" in m else "",
                           "cas": CAS_RE.findall(r[m["cas"]]) if "cas" in m else [],
                           "cas_text": r[m["cas"]].strip() if "cas" in m else "",
                           "pct": _clean_pct(r[m["pct"]]) if "pct" in m else None,
                           "func": r[m["func"]].strip() if "func" in m else "",
                           "marks": {k: [mark(None, cell=[ri, m[k]])] for k in ("inci", "jp", "th") if k in m},
                           "row_marks": [mark(None, cell=[ri, c]) for c in range(len(r))]}
                    rows.append(row)
                break
    else:
        cols = None
        for pi, pg in enumerate(doc.pdf):
            if pi in doc.ocr_pages:
                continue
            try:
                tabs = pg.find_tables().tables
            except Exception:
                tabs = []
            for tab in tabs:
                trows = tab.rows
                start = 0
                for hi, r in enumerate(trows[:3]):
                    m = _map_header([(pg.get_text("text", clip=c) if c else "", i) for i, c in enumerate(r.cells)])
                    if "inci" in m or ("jp" in m and len(m) >= 2):
                        cols, start = m, hi + 1
                        break
                if not cols:
                    continue
                for r in trows[start:]:
                    def cell(k):
                        i = cols.get(k)
                        if i is None or i >= len(r.cells) or not r.cells[i]:
                            return "", None
                        return re.sub(r"\s+", " ", pg.get_text("text", clip=r.cells[i])).strip(), r.cells[i]
                    inci, ib = cell("inci")
                    jp, jb = cell("jp")
                    th, tb = cell("th")
                    if not (inci or jp or th) or re.match(r"^(total|รวม|合計)", inci + jp, re.I):
                        continue
                    cas, _ = cell("cas")
                    pct, _ = cell("pct")
                    func, _ = cell("func")
                    marks = {}
                    for k, bx in (("inci", ib), ("jp", jb), ("th", tb)):
                        if bx:
                            marks[k] = [mark(pi, bx)]
                    rows.append({"inci": inci, "jp": jp, "th": th, "cas": CAS_RE.findall(cas), "cas_text": cas,
                                 "pct": _clean_pct(pct), "func": func, "marks": marks,
                                 "row_marks": [mark(pi, r.bbox)]})
    if doc.kind != "table" and doc.ocr_pages:
        rows += _ocr_formula(doc)
    if not rows:
        return None
    for i, r in enumerate(rows):
        r["idx"] = i
        r["names"] = [n for n in (r["inci"], r["jp"], r["th"]) if n]
        r["label"] = r["inci"] or r["jp"] or r["th"]
    return rows


# ---------------------------------------------------------------- label ingredient list
MARKERS = [
    (r"全\s*成\s*分\s*[:：]?", 3), (r"成\s*分\s*[:：]", 2), (r"INGREDIENTS?\s*[:：]", 3),
    (r"ส่วน\s*ประกอบ(?:\s*ทั้งหมด)?\s*[:：]?", 3), (r"ส่วน\s*ผสม\s*[:：]?", 2), (r"成\s*分", 1),
    (r"Ingredients?", 1), (r"COMPOSITION\s*[:：]", 2), (r"成份\s*[:：]?", 2), (r"전성분\s*[:：]?", 3),
]
STOP = re.compile(r"^\s*(【|■|●|◆|\[|※|＊|\*|\d\s*[\)）]|販売名|お客様|製造販売|発売元|内容量|MADE IN|Made in|"
                  r"วิธีใช้|คำเตือน|ผลิตโดย|นำเข้าโดย|จัดจำหน่าย|ผู้นำเข้า|ผู้ผลิต|ขนาดบรรจุ|ปริมาณสุทธิ|เลขที่|"
                  r"How to use|Directions|Caution|Warning|Manufactur|Distribut|Net\s|Exp|Mfg|Lot|"
                  r"\d+(?:\.\d+)?\s*(?:ml|g|oz|fl|กรัม|มล)\b)", re.I)
SEP = "、,，;；"


def _seq_from_lines(lines):
    seq = []
    for li, l in enumerate(lines):
        if seq:
            prev = seq[-1][0]
            first = l["text"][:1]
            if prev == "-" and re.match(r"[a-z]", first):
                pass
            elif re.match(r"[A-Za-z0-9)]", prev) and re.match(r"[A-Za-z0-9(]", first):
                seq.append((" ", l.get("page"), None, li))
            elif has_thai(prev) and has_thai(first):
                seq.append((" ", l.get("page"), None, li))
        for ch, box in l["chars"]:
            seq.append((ch, l.get("page"), box, li))
    return seq


def _collect_para(lines, li0, cut):
    """บรรทัดของย่อหน้าส่วนประกอบ เริ่มหลังหัวข้อ ไปจนเว้นบรรทัด/หัวข้อใหม่"""
    l0 = lines[li0]
    page = l0.get("page")
    para = [dict(l0, chars=l0["chars"][cut:], text=l0["text"][cut:])]
    x0 = l0["box"][0]
    width = l0["box"][2] - l0["box"][0]
    lh = l0["box"][3] - l0["box"][1]
    last = l0
    for l in lines[li0 + 1:]:
        if l.get("page") != page:
            break
        if l.get("score", 1) < 0.85 and len(l["text"].strip()) < 12:
            continue          # เศษข้อความที่อ่านไม่ชัด (เช่นใต้เทปใส/แสงสะท้อน) ไม่ใช้กำหนดตำแหน่งบรรทัด
        b, lb = l["box"], last["box"]
        if b[1] < lb[1] + lh * 0.3:
            continue          # คอลัมน์อื่นในแถวเดียวกัน
        if b[1] - lb[3] > lh * 1.6:
            break
        if abs(b[0] - x0) > max(width * 0.12, lh * 2.5):
            continue          # คนละคอลัมน์
        if STOP.match(l["text"]):
            break
        # ภาษาเปลี่ยน (รายการอังกฤษ/ญี่ปุ่น แล้วเจอบรรทัดภาษาไทย เช่นสติกเกอร์ผู้นำเข้าที่แปะใต้รายการ) = จบรายการ
        txt_so_far = "".join(p["text"] for p in para)
        # รายการจบด้วย "." แล้วบรรทัดถัดไปเป็นหัวเรื่องตัวพิมพ์ใหญ่ทั้งบรรทัด (เช่นชื่อสินค้าบนสติกเกอร์) = จบรายการ
        letters = re.sub(r"[^A-Za-z]", "", l["text"])
        if txt_so_far.rstrip().endswith(".") and len(letters) >= 8 and sum(c.isupper() for c in letters) >= 0.85 * len(letters):
            break
        if len(re.sub(r"\W", "", txt_so_far)) > 20 and not has_thai(txt_so_far) \
                and len(re.findall(r"[฀-๿]", l["text"])) > 0.3 * max(1, len(re.sub(r"\W", "", l["text"]))):
            break
        para.append(l)
        last = l
        if re.search(r"[。．]\s*$", l["text"]):
            break
    return para


def merge_fragments(lines):
    """OCR บนขวด/กระปุกโค้ง มักแยก 1 บรรทัดเป็นหลายชิ้น → ต่อชิ้นที่อยู่แถวเดียวกันและอยู่ติดกัน"""
    # ต่อเป็นสาย: แต่ละชิ้นเชื่อมกับชิ้นถัดไปทางขวาที่อยู่ระดับเดียวกันที่สุด (รองรับบรรทัดเอียง/โค้ง)
    n = len(lines)
    cy = [(l["box"][1] + l["box"][3]) / 2 for l in lines]
    hh = [max(1e-3, l["box"][3] - l["box"][1]) for l in lines]
    cand = []
    for i, a in enumerate(lines):
        for j, b in enumerate(lines):
            if i == j:
                continue
            h = min(hh[i], hh[j])
            gap = b["box"][0] - a["box"][2]
            dy = abs(cy[j] - cy[i])
            if has_cjk(a["text"]) or has_cjk(b["text"]):
                continue      # ญี่ปุ่น/จีน OCR อ่านทั้งบรรทัดอยู่แล้ว (ต่อแล้วจะไปติดคอลัมน์ข้างๆ)
            wmin = min(a["box"][2] - a["box"][0], b["box"][2] - b["box"][0])
            if (max(-0.8 * h, -0.5 * wmin) <= gap <= 1.6 * h and b["box"][2] > a["box"][2]
                    and dy < 0.55 * h and 0.6 < hh[i] / hh[j] < 1.65):
                cand.append((dy + 0.25 * max(gap, 0), i, j))
    nxt, prv = {}, {}
    for _, i, j in sorted(cand):
        if i not in nxt and j not in prv:
            nxt[i], prv[j] = j, i
    out = []
    for s in range(n):
        if s in prv:
            continue
        chain, k, seen = [lines[s]], s, {s}
        while k in nxt and nxt[k] not in seen:
            k = nxt[k]
            seen.add(k)
            chain.append(lines[k])
        r = dict(chain[0])
        for l in chain[1:]:
            r["chars"] = r["chars"] + [(" ", None)] + l["chars"]
            r["text"] = r["text"] + " " + l["text"]
            r["score"] = min(r.get("score", 1), l.get("score", 1))
        bs = [l["box"] for l in chain]
        # กล่องรวม: ซ้าย/บนใช้ชิ้นแรก (กันบรรทัดเอียงกินพื้นที่บรรทัดอื่นตอนจัดย่อหน้า)
        r["box"] = [bs[0][0], bs[0][1], max(b[2] for b in bs), max(b[3] for b in bs)]
        r["box0"] = bs[0]
        out.append(r)
    out.sort(key=lambda l: (round(l["box"][1] / 4), l["box"][0]))
    return out


def extract_label(doc):
    """หารายการส่วนประกอบบนฉลาก คืน {items, marker, text}"""
    if doc.kind == "table":
        return _label_from_table(doc)
    raw = [l for l in doc.lines if l.get("chars")]
    ocr = doc.source == "ocr"
    lines = merge_fragments(raw) if ocr else raw
    # หัวข้อที่ตามด้วยรายชื่อสารมากที่สุด = รายการส่วนประกอบจริง
    # (กันสติกเกอร์ไทย "ส่วนประกอบ: ระบุบนบรรจุภัณฑ์" ที่ชี้ไปหารายการภาษาอังกฤษ)
    best = None
    for li, l in enumerate(lines):
        for pat, w in MARKERS:
            m = re.search(pat, l["text"], re.I)
            if m:
                after = l["text"][m.end():].strip(" :：")
                if re.match(r"(ดู|ระบุ|แสดง|ตาม)", after) or re.search(r"(บน|ที่)(บรรจุภัณฑ์|กล่อง|ฉลาก|ขวด)", after):
                    break
                lns, li2, cut = lines, li, m.end()
                if ocr:
                    # ต่อชิ้นข้อความใหม่เฉพาะในคอลัมน์ของหัวข้อ (กันข้อความด้านข้างขวด/กระปุกโค้งมาปน)
                    mb = next((b for c, b in l["chars"][m.start():] if b), l["box"])
                    # ขอบซ้ายของคอลัมน์ = ขอบซ้ายของชิ้นข้อความหัวข้อ (เช่น "MAIN INGREDIENTS")
                    cxm, cym = (mb[0] + mb[2]) / 2, (mb[1] + mb[3]) / 2
                    hf = next((f for f in raw if f.get("page") == l.get("page") and f["box"][0] <= cxm <= f["box"][2]
                               and f["box"][1] <= cym <= f["box"][3]), None)
                    if hf:
                        # หัวข้ออาจถูกแยกเป็นหลายชิ้น ("MAIN" + "INGREDIENTS") → ไล่ไปทางซ้ายเฉพาะชิ้นที่ติดกัน
                        hb = hf["box"]
                        while True:
                            hh_ = hb[3] - hb[1]
                            left = [f for f in raw if f.get("page") == l.get("page") and f["box"][2] <= hb[0] + 0.5 * hh_
                                    and hb[0] - f["box"][2] <= 0.6 * hh_ and f["box"][0] < hb[0]
                                    and abs((f["box"][1] + f["box"][3]) / 2 - (hb[1] + hb[3]) / 2) < 0.5 * hh_]
                            if not left:
                                break
                            hb = max(left, key=lambda f: f["box"][2])["box"]
                        mb = [hb[0], mb[1], mb[2], mb[3]]
                    h = mb[3] - mb[1]
                    col = [f for f in raw if f.get("page") == l.get("page") and f["box"][0] >= mb[0] - 0.8 * h
                           and f["box"][3] > mb[1]]
                    lns = merge_fragments(col)
                    found = None
                    for k, x in enumerate(lns):
                        mm = re.search(pat, x["text"], re.I)
                        if mm and abs(x["box"][1] - mb[1]) < 2 * h:
                            found = (k, mm.end())
                            break
                    if not found:
                        break
                    li2, cut = found
                para = _collect_para(lns, li2, cut)
                txt = "".join(c for c, *_ in _seq_from_lines(para))
                n = sum(txt.count(c) for c in SEP) + len(re.findall(r"\.\s+[A-Z]", txt))
                score = n * 3 + w * 4 + (2 if m.start() < 3 else 0)
                if best is None or score > best[0]:
                    best = (score, m.group(0).strip(), para)
                break
    if best is None:
        return {"items": [], "marker": None, "error": "ไม่พบหัวข้อส่วนประกอบบนฉลาก (เช่น 全成分 / Ingredients / ส่วนประกอบ)"}
    _, marker, para = best
    seq = _seq_from_lines(para)
    text = "".join(c for c, *_ in seq)
    items, depth, start = [], 0, 0
    pstart = None
    for i, ch in enumerate(text + "、"):
        if ch in "(（[":
            depth += 1
            pstart = i
        elif ch in ")）]":
            depth = max(0, depth - 1)
        if depth and pstart is not None and i - pstart > 45:
            depth = 0
        # ชื่อสารคั่นด้วย , เสมอ — OCR อาจอ่าน , เป็น . (". ตามด้วยตัวพิมพ์ใหญ่" = คั่น)
        dot_sep = ch == "." and re.match(r"\s+[A-Z(]", text[i + 1:i + 3] or "") is not None
        if ((ch in SEP or dot_sep) and depth == 0) or i == len(text):
            raw = text[start:i]
            a = start + (len(raw) - len(raw.lstrip()))
            nm = raw.strip().strip("。.")
            nm = re.sub(r"\s*[\*※]\s*\d*\s*$", "", nm)
            if nm and len(nm) < 120:
                b = a + len(raw.strip())
                items.append({"text": nm, "marks": char_marks(seq, a, b), "pos": len(items), "a": a, "b": b})
            start = i + 1
    return {"items": items, "marker": marker, "text": text, "seq": seq}


def _label_from_table(doc):
    rows = extract_formula(doc)
    items = []
    if rows:
        for r in rows:
            items.append({"text": r["label"], "marks": r["marks"].get("inci") or r["row_marks"], "pos": len(items)})
    return {"items": items, "marker": "ตาราง", "text": ""}


# ---------------------------------------------------------------- tab 1: label vs formula
SYN = [{"WATER", "AQUA", "水", "น้ำ", "PURIFIEDWATER", "AQUA(WATER)", "WATER(AQUA)", "AQUA/WATER", "WATER/AQUA", "精製水"},
       {"FRAGRANCE", "PARFUM", "香料", "น้ำหอม", "FRAGRANCE(PARFUM)", "PARFUM(FRAGRANCE)", "PERFUME"},
       {"BG", "BUTYLENEGLYCOL"}, {"DPG", "DIPROPYLENEGLYCOL"}, {"PG", "PROPYLENEGLYCOL"}]


def alt_names(s):
    """ชื่อเดียวกันหลายแบบ เช่น AQUA (WATER) → AQUA, WATER"""
    n = norm(s)
    out = {n}
    for part in re.split(r"[/()（）]", unicodedata.normalize("NFKC", s or "")):
        p = norm(part)
        if len(p) >= 2:
            out.add(p)
    for g in SYN:
        if out & g:
            out |= g
    return out


def score_names(a_alts, b_alts):
    best = 0
    for a in a_alts:
        for b in b_alts:
            if a == b:
                return 100
            if min(len(a), len(b)) >= 2:
                best = max(best, fuzz.ratio(a, b))
    return best


def _split_items(label, fa):
    """OCR อ่านเครื่องหมายคั่นไม่ติด → ลองแยกเป็น 2 ชื่อที่ตรงกับสูตร"""
    seq, out = label.get("seq"), []
    queue = list(label["items"])
    while queue:
        it = queue.pop(0)
        t = it["text"]
        best1 = max((score_names({norm(t)}, f) for f in fa), default=0)
        best = None
        if best1 < 92 and not it.get("edited") and seq is not None and "a" in it and 4 <= len(t) <= 160:
            for k in range(2, len(t) - 1):
                l, r = t[:k].strip(), t[k:].strip()
                if len(l) < 2 or len(r) < 2:
                    continue
                # ภาษาอังกฤษ/ไทยแยกได้เฉพาะตรงช่องว่าง (ไม่ตัดกลางคำ); ญี่ปุ่นไม่มีช่องว่างจึงแยกได้ทุกตำแหน่ง
                if re.search(r"[A-Za-z฀-๿]", t) and not has_cjk(t) and t[k - 1] != " " and t[k] != " ":
                    continue
                sl = max(score_names(alt_names(l), f) for f in fa)
                if sl < 90:
                    continue
                sr = max(score_names(alt_names(r), f) for f in fa)
                fl = max(score_names({norm(l)}, f) for f in fa)
                fr = max(score_names({norm(r)}, f) for f in fa)
                if min(sl, sr) >= 90 and min(sl, sr) > best1 + 3:
                    key = (2, min(sl, sr), min(fl, fr))          # แยกแล้วตรงทั้งสองฝั่ง
                elif fl >= 95 and len(r) >= 4:
                    key = (1, fl, k)                              # ส่วนหน้าตรงชัด ส่วนหลังแยกต่อรอบถัดไป
                else:
                    continue
                if best is None or key > best[0]:
                    best = (key, k)
        if best:
            k, a = best[1], it["a"]
            # ส่วนท้ายอาจรวมหลายชื่อ → ตรวจแยกซ้ำ (OCR ไม่เห็น , หลายจุด)
            out.append(dict(it, text=t[:k].strip(), marks=char_marks(seq, a, a + k), split=True, b=a + k))
            queue.insert(0, dict(it, text=t[k:].strip(), marks=char_marks(seq, a + k, it["b"]), split=True, a=a + k))
        else:
            out.append(it)
    for i, it in enumerate(out):
        it["pos"] = i
    return out


def compare(label, formula):
    fa = []
    for r in formula:
        s = set()
        for n in r["names"]:
            s |= alt_names(n)
        fa.append(s)
    items = _split_items(label, fa)
    label["items"] = items
    la = [alt_names(it["text"]) for it in items]
    pairs = []
    for i, a in enumerate(la):
        for j, b in enumerate(fa):
            sc = score_names(a, b)
            if sc >= 62:
                pairs.append((sc, i, j))
    pairs.sort(key=lambda x: (-x[0], abs(x[1] - x[2])))
    used_l, used_f, match = set(), set(), {}
    for sc, i, j in pairs:
        if i in used_l or j in used_f:
            continue
        used_l.add(i)
        used_f.add(j)
        match[j] = (i, sc)
    results = []
    for j, r in enumerate(formula):
        if j in match:
            i, sc = match[j]
            st = "ok" if sc >= 97 else ("near" if sc >= 80 else "check")
            results.append({"status": st, "score": sc, "f": j, "l": i, "label_text": items[i]["text"], "formula": r})
        else:
            results.append({"status": "missing", "score": 0, "f": j, "l": None, "label_text": "", "formula": r})
    for i, it in enumerate(items):
        if i not in used_l:
            # ชื่อใกล้เคียงที่สุดในสูตร (ไว้แนะนำ)
            sug = max(((score_names(la[i], fa[j]), j) for j in range(len(formula))), default=(0, None))
            results.append({"status": "extra", "score": sug[0], "f": None, "l": i, "label_text": it["text"],
                            "formula": None, "suggest": formula[sug[1]]["label"] if sug[1] is not None and sug[0] >= 50 else ""})
    for r in results:
        if r["l"] is not None and items[r["l"]].get("edited"):
            r["edited"] = True
    # ลำดับบนฉลาก: สารที่ ≥1% ต้องเรียงจากมากไปน้อย
    order = sorted([r for r in results if r["l"] is not None and r["formula"] and r["formula"]["pct"] is not None
                    and r["formula"]["pct"] >= 1], key=lambda r: r["l"])
    for a, b in zip(order, order[1:]):
        if b["formula"]["pct"] > a["formula"]["pct"] + 1e-9:
            b["order_warn"] = "บนฉลากอยู่หลัง %s (%.4g%%) แต่ในสูตรมีปริมาณมากกว่า (%.4g%%)" % (
                a["label_text"], a["formula"]["pct"], b["formula"]["pct"])
    summary = {k: sum(1 for r in results if r["status"] == k) for k in ("ok", "near", "check", "missing", "extra")}
    summary["order"] = sum(1 for r in results if r.get("order_warn"))
    return {"results": results, "summary": summary}


# ---------------------------------------------------------------- regulation DB
class RegDB:
    def __init__(self):
        self.load()

    def load(self):
        paths = [os.path.join(USER_DIR, "regdb.json"), os.path.join(DATA, "regdb.json")]
        dbs = []
        for p in paths:
            try:
                with open(p, encoding="utf-8") as f:
                    dbs.append(json.load(f))
            except Exception:
                pass
        self.db = max(dbs, key=lambda d: d.get("version", "")) if dbs else {"banned": [], "restricted": [], "words": [], "mentions": []}
        for b in self.db.get("banned", []):  # อักษรฟอนต์ Symbol ที่หลุดมาจาก PDF
            b["name"] = b["name"].replace("", "≥").replace("", "≤").replace("", ">").replace("", "<")
        self._index()

    def _index(self):
        self.by_cas, self.by_name = {}, {}
        # วัตถุที่อาจใช้: ลำดับเดียวกันปรากฏซ้ำในประกาศแก้ไขฉบับหลัง = ฉบับหลังใช้แทน
        last = {}
        for i, r in enumerate(self.db.get("restricted", [])):
            r.pop("superseded", None)
            if r["no"] in last:
                self.db["restricted"][last[r["no"]]]["superseded"] = True
            last[r["no"]] = i
        def add_name(n, ref):
            k = norm_latin(n)
            if len(k) >= 4:
                self.by_name.setdefault(k, []).append(ref)
        for i, b in enumerate(self.db["banned"]):
            ref = ("banned", i)
            for c in b["cas"]:
                self.by_cas.setdefault(c, []).append(ref)
            for alt in banned_alts(b["name"]):
                add_name(alt, ref)
        for i, r in enumerate(self.db["restricted"]):
            ref = ("restricted", i)
            for c in r["cas"]:
                self.by_cas.setdefault(c, []).append(ref)
            for n in r.get("names", []):
                if re.search(r"[-,(]$|^[-),a-z]|ยกเว้น", n) or n.count("(") != n.count(")"):
                    continue
                add_name(n, ref)
                for alt in banned_alts(n):
                    add_name(alt, ref)
        self.conds = {}
        for ci, c in enumerate(self.db.get("conds", [])):
            for it in c["items"]:
                k = norm_latin(it["name"])
                if len(k) >= 4:
                    self.conds.setdefault(k, []).append((ci, it))
        self.mentions = {}
        for m in self.db.get("mentions", []):
            line = m["line"].strip()
            listed = re.match(r"^(\(\d+\)|\d+[.)]|[-•⚫●▪*])\s*", line)
            alone = len(re.sub(r"[^A-Z]", "", m["text"])) >= 0.7 * max(1, len(re.sub(r"[^A-Za-z]", "", line)))
            if not (listed or alone) or norm_latin(m["text"]) in ("water", "aqua", "fragrance", "parfum"):
                continue
            for part in re.split(r"\s*/\s*|\s+หรือ\s+", m["text"]):
                k = norm_latin(re.sub(r"\(.*?\)", "", part))
                if len(k) >= 4:
                    self.mentions.setdefault(k, []).append(m)
                for inner in re.findall(r"\(([^)]+)\)", part):
                    k2 = norm_latin(inner)
                    if len(k2) >= 5:
                        self.mentions.setdefault(k2, []).append(m)

    def lookup(self, names, cas):
        hits, seen = [], set()
        for c in cas:
            for ref in self.by_cas.get(c, []):
                if ref not in seen:
                    seen.add(ref)
                    hits.append((ref, "CAS " + c))
        for n in names:
            for alt in {n} | set(banned_alts(n)):
                for ref in self.by_name.get(norm_latin(alt), []):
                    if ref not in seen:
                        seen.add(ref)
                        hits.append((ref, "ชื่อ"))
        out = []
        for (kind, i), how in hits:
            e = self.db[kind][i]
            out.append({"kind": kind, "how": how, "entry": e})
        out.sort(key=lambda h: (h["kind"] != "banned", bool(h["entry"].get("superseded"))))
        return out

    def cond_hits(self, names):
        """เงื่อนไขที่ต้องเพิ่มประกอบการจดแจ้ง (หลักเกณฑ์ อย.) ของสารนี้"""
        out, seen = [], set()
        for n in names:
            for ci, it in self.conds.get(norm_latin(n), []):
                if ci not in seen:
                    seen.add(ci)
                    out.append((ci, it))
        return out

    def guideline(self, names):
        out, seen = [], set()
        for n in names:
            k = norm_latin(n)
            for m in self.mentions.get(k, []):
                key = (m["page"], m["line"])
                if key not in seen:
                    seen.add(key)
                    out.append(m)
        return out[:6]


SALT_RE = re.compile(r",?\s*(and\s+)?(its|their)\s+(salts?|esters?|isomers?|derivatives?|compounds?|ethers?|preparations?)"
                     r"(\s*(,|and|or)\s*(its|their)?\s*(salts?|esters?|isomers?|derivatives?|compounds?|ethers?))*.*$", re.I)


def banned_alts(name):
    name = re.sub(r"\[ข้อความ.*?\]", "", name)
    name = re.split(r"\s+ยกเว้น|\s+เว้นแต่|\s+except\b", name)[0]
    name = re.split(r",?\s+(?:if|when) (?:it|they) contains?\b", name)[0]  # "Butane, if it contains ≥0.1% Butadiene" → Butane
    alts = set()
    base = SALT_RE.sub("", name).strip(" ,;")
    for s in (name, base):
        for part in re.split(r";", s):
            p = part.strip()
            if p:
                alts.add(p)
                # วงเล็บที่เป็น "ชื่อเรียกอื่น" ต้องแยกเป็นคำ เช่น "... hydroxide (Acetylcholine)"
                # ไม่ใช่วงเล็บที่เป็นส่วนของชื่อเคมี เช่น "(2-Isopropylpent-4-enoyl)urea" (เดิมเหลือ "urea" → จับ Urea ผิด)
                alias = r"(?:(?<=\s)|^)\(([^()]{3,})\)(?=\s|$|[,;])"
                np_ = re.sub(alias, "", p).strip(" ,")
                if np_ and np_ != p:
                    alts.add(np_)
                for inner in re.findall(alias, p):
                    alts.add(inner.strip())
    return [a for a in alts if len(re.sub(r"[^A-Za-z]", "", a)) >= 3]


def check_words(doc, words):
    """คำห้ามใช้ในชื่อ/ข้อความบนฉลาก"""
    hits = []
    seqs = []
    for li, l in enumerate(doc.lines):
        if not l.get("chars"):
            continue
        seqs.append((l, [(c, l.get("page"), b, li) for c, b in l["chars"]]))
    for w in words:
        word = w["word"].replace("*", "").strip()
        if len(word) < 3 or re.search(r"หลักเกณฑ์|ให้ใช้", word):
            continue
        if re.search(r"[A-Za-z]", word):
            pat = re.compile(r"(?<![A-Za-z])" + re.escape(word).replace(r"\ ", r"[\s\-]*") + r"(?![A-Za-z])", re.I)
        else:
            pat = re.compile(re.escape(word))
        for l, seq in seqs:
            txt = "".join(c for c, *_ in seq)
            for m in pat.finditer(txt):
                hits.append({"word": w, "found": m.group(0), "line": l["text"].strip(),
                             "marks": char_marks(seq, m.start(), m.end())})
    return hits


# ---------------------------------------------------------------- CosIng (EU)
class CosIng:
    CONF_URL = "https://ec.europa.eu/growth/tools-databases/cosing/assets/env-json-config.json"

    def __init__(self):
        self.path = os.path.join(USER_DIR, "cosing_cache.json")
        try:
            with open(self.path, encoding="utf-8") as f:
                self.cache = json.load(f)
        except Exception:
            self.cache = {}
        self.conf = None
        self.lock = threading.Lock()

    def _save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.cache, f, ensure_ascii=False)
        except Exception:
            pass

    def _http(self, url, data=None, headers=None, timeout=25):
        import urllib.request
        req = urllib.request.Request(url, data=data, headers=dict({"User-Agent": "Mozilla/5.0 (INCI checker)"}, **(headers or {})))
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()

    def _config(self):
        if self.conf is None:
            self.conf = json.loads(self._http(self.CONF_URL))
        return self.conf

    def search(self, text, size=20, fuzzy=False):
        """เหมือนช่องค้นหาของเว็บ CosIng (fuzzy = ติ๊ก "Spelling not exact")"""
        conf = self._config()
        t = {"query": text.lower(), "fields": ["inciName.exact", "inciUsaName", "innName.exact",
                                       "phEurName", "chemicalName", "chemicalDescription"],
             "defaultOperator": "OR" if fuzzy else "AND"}
        if fuzzy:
            t["analyzeWildcard"] = True
        q = {"bool": {"must": [{"text": t}, {"terms": {"itemType": ["ingredient", "substance"]}}]}}
        bnd = "----inci%d" % int(time.time() * 1000)
        body = ("--%s\r\nContent-Disposition: form-data; name=\"query\"; filename=\"blob\"\r\nContent-Type: application/json\r\n\r\n%s\r\n--%s--\r\n"
                % (bnd, json.dumps(q), bnd)).encode()
        import urllib.parse
        url = "%s?%s" % (conf["euSearchApiUrl"], urllib.parse.urlencode(
            {"apiKey": conf["euSearchApiKey"], "text": "*", "pageSize": size, "pageNumber": 1}))
        res = json.loads(self._http(url, body, {"Content-Type": "multipart/form-data; boundary=" + bnd}))
        out = []
        for r in res.get("results", []):
            m = r.get("metadata", {})
            g = lambda k: [x for x in m.get(k, []) if x]
            out.append({"inci": (g("inciName") or g("nameOfCommonIngredientsGlossary") or [""])[0],
                        "type": (g("itemType") or [""])[0], "cas": g("casNo"), "ec": g("ecNo"),
                        "functions": g("functionName"), "chemical": (g("chemicalName") or [""])[0],
                        "annex": g("cosmeticRestriction") or g("annexNo"), "status": (g("status") or [""])[0],
                        "max": g("maximumConcentration"), "conditions": g("wordingOfConditions"),
                        "other": g("otherRestrictions"), "id": (g("substanceId") or [""])[0],
                        "inn": g("innName"), "usa": g("inciUsaName")})
        return out

    def check(self, name):
        """ตรวจชื่อ INCI กับ CosIng: คืน {status: exact|differs|notfound|error, best, suggestions}"""
        key = norm_latin(name)
        with self.lock:
            if key in self.cache and time.time() - self.cache[key].get("t", 0) < 30 * 86400:
                return self.cache[key]
        try:
            q = re.sub(r"[^\w\s\-/(),.']", " ", name).strip()
            res = self.search(q)
            match = lambda rs: [r for r in rs if norm_latin(r["inci"]) == key or key in [norm_latin(x) for x in r["usa"] + r["inn"]]]
            exact = match(res)
            parts = [p.strip() for p in re.split(r"[()/]", name) if len(p.strip()) >= 3]
            if not exact and len(parts) >= 2:
                # เช่น "AQUA (WATER)" → ตรวจทีละชื่อ
                subs = [self.check(p) for p in parts]
                if all(s["status"] == "exact" for s in subs):
                    out = {"status": "exact", "best": subs[0]["best"], "suggestions": [], "parts": [s["best"]["inci"] for s in subs],
                           "t": time.time()}
                    with self.lock:
                        self.cache[key] = out
                        self._save()
                    return out
            if not exact:
                # สะกดไม่ตรง: ค้นแบบ "Spelling not exact" ด้วยคำที่ตัดท้าย 1–2 ตัวอักษร แล้วจัดอันดับความใกล้เคียง
                words = [w for w in re.findall(r"[A-Za-z0-9\-]+", q) if len(w) >= 4] or q.split()[:1]
                seen = {r["inci"] for r in res}
                tries = []
                for cut in (1, 2):
                    tries.append((" ".join(w[:max(3, len(w) - cut)] for w in words[:4]), False))
                    tries.append((" ".join("*%s*" % w[:max(3, len(w) - cut)].lower() for w in words[:4]), True))
                for wq, fz in tries:
                    for r in self.search(wq, 60, fuzzy=fz):
                        if r["inci"] not in seen:
                            seen.add(r["inci"])
                            res.append(r)
                    if any(fuzz.ratio(norm_latin(r["inci"]), key) >= 85 for r in res):
                        break
            if exact:
                exact.sort(key=lambda r: (r["type"] != "ingredient", r["status"] != "Active"))
                out = {"status": "exact", "best": exact[0], "suggestions": []}
            else:
                ranked = sorted(res, key=lambda r: -fuzz.ratio(norm_latin(r["inci"]), key))
                sug = [r for r in ranked if r["inci"]][:5]
                out = {"status": "differs" if sug else "notfound", "best": None, "suggestions": sug}
            out["t"] = time.time()
            with self.lock:
                self.cache[key] = out
                self._save()
            return out
        except Exception as e:
            return {"status": "error", "error": str(e)[:200], "best": None, "suggestions": []}


# ---------------------------------------------------------------- tab 2: regulation check
def reg_items(doc):
    """รายชื่อสารที่จะตรวจ: ใช้ตารางสูตรถ้ามี ไม่งั้นใช้รายการบนฉลาก"""
    rows = extract_formula(doc)
    if rows:
        items = []
        for r in rows:
            items.append({"name": r["inci"] or r["label"], "jp": r["jp"], "th": r["th"], "cas": r["cas"],
                          "pct": r["pct"], "func": r.get("func", ""), "marks": r["marks"].get("inci") or r["row_marks"],
                          "row_marks": r["row_marks"], "from": "formula"})
        return items, "formula", None
    lab = extract_label(doc)
    items = [{"name": it["text"], "jp": "", "th": "", "cas": [], "pct": None, "func": "", "marks": it["marks"],
              "row_marks": it["marks"], "from": "label"} for it in lab["items"]]
    return items, "label", lab.get("error")


# ก๊าซขับดัน (propellant) — มีในสูตร = ผลิตภัณฑ์สเปรย์อัดแก๊ส (aerosol)
PROPELLANTS = {norm_latin(x) for x in (
    "Butane", "Isobutane", "n-Butane", "Propane", "Pentane", "Isopentane", "Dimethyl Ether", "DME",
    "Hydrofluorocarbon 152a", "HFC-152a", "HFC 152a", "1,1-Difluoroethane", "Difluoroethane",
    "Hydrofluorocarbon 134a", "HFC-134a", "Tetrafluoroethane", "Tetrafluoropropene", "HFO-1234ze", "Trans-1,3,3,3-Tetrafluoroprop-1-ene",
    "Hydrofluoroolefin", "Nitrogen", "Nitrous Oxide", "Carbon Dioxide", "Liquefied Petroleum Gas", "LPG",
    "Isobutane/Propane", "Butane/Propane", "Propane/Butane", "Compressed Air")}
PROPELLANT_JP = ("ＬＰＧ", "LPG", "ジメチルエーテル", "窒素", "二酸化炭素", "炭酸ガス", "亜酸化窒素", "イソブタン", "ブタン", "プロパン",
                 "イソペンタン", "ペンタン", "液化石油ガス", "噴射剤")
PROPELLANT_FUNC = re.compile(r"propellant|噴射剤|ก๊าซขับดัน|แก๊สขับดัน", re.I)


COND_BAN = re.compile(r"\b(if|when) (it|they) contains?\b|\bcontaining (more|less) than\b", re.I)


def is_propellant(it, names):
    if any(norm_latin(n) in PROPELLANTS for n in names):
        return True
    jp = unicodedata.normalize("NFKC", it.get("jp") or "")
    if jp and any(jp.strip() == unicodedata.normalize("NFKC", j) for j in PROPELLANT_JP):
        return True
    return bool(PROPELLANT_FUNC.search(it.get("func") or ""))


def reg_check(doc, reg):
    items, mode, err = reg_items(doc)
    conds = {}
    for idx, it in enumerate(items):
        names = [it["name"]] + [p for p in re.split(r"[/()]", it["name"]) if len(p.strip()) > 3]
        hits = reg.lookup(names, it["cas"])
        it["hits"] = hits
        it["conds"] = []
        for ci, ent in reg.cond_hits(names):
            c = reg.db["conds"][ci]
            it["conds"].append(c["key"])
            g = conds.setdefault(ci, {"key": c["key"], "title": c["title"], "page": c["page"], "rect": c["rect"],
                                      "items": []})
            g["items"].append({"i": idx, "name": it["name"], "listed": ent["name"], "page": ent["page"], "rect": ent["rect"]})
        it["propellant"] = is_propellant(it, names)
        it["guide"] = reg.guideline(names)
        latin = re.sub(r"[^A-Za-z]", "", it["name"])
        it["latin"] = len(latin) >= 3 and not has_cjk(it["name"]) and not has_thai(it["name"])
        for h in hits:  # ห้ามใช้แบบมีเงื่อนไข เช่น "Butane, if it contains ≥0.1% Butadiene"
            if h["kind"] == "banned" and COND_BAN.search(h["entry"].get("name", "")):
                h["conditional"] = True
        it["level"] = ("banned" if any(h["kind"] == "banned" and not h["entry"].get("superseded") and not h.get("conditional") for h in hits)
                       else "restricted" if hits else "guide" if it["guide"] else "ok")
    # คำห้ามใช้ = ชื่อ/ข้อความบนฉลากเท่านั้น — เอกสารสูตร / MSDS / COA ไม่ตรวจ (คำอย่าง Acute, irritate เป็นเนื้อหาปกติของ MSDS)
    techdoc = re.search(r"safety\s*data\s*sheet|\bMSDS\b|\bSDS\b|certificate\s*of\s*analysis|\bCOA\b|composition", doc.full_text(), re.I)
    words = check_words(doc, reg.db.get("words", [])) if mode == "label" and not techdoc else []
    summary = {k: sum(1 for i in items if i["level"] == k) for k in ("banned", "restricted", "guide", "ok")}
    summary["words"] = len(words)
    props = [i for i, it in enumerate(items) if it["propellant"]]
    cl = sorted(conds.values(), key=lambda g: [k for _, k in COND_ORDER].index(g["key"]) if g["key"] in [k for _, k in COND_ORDER] else 99)
    summary["conds"] = len(cl) + (1 if props else 0)
    return {"items": items, "mode": mode, "error": err, "words": words, "summary": summary,
            "conds": cl, "gas": {"props": props, "aerosol": bool(props)}}


COND_ORDER = [("", "aerosol"), ("", "spray"), ("", "child3"), ("", "child10"), ("", "purpose"), ("", "amount")]
