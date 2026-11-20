"""2NK INCI Check — แกนประมวลผล: อ่านไฟล์, OCR, ดึงรายชื่อสาร, จับคู่ฉลาก↔สูตร, ตรวจสารห้ามใช้"""
import base64, csv, io, json, os, re, sys, threading, time, unicodedata

import pymupdf
from rapidfuzz import fuzz

APP_NAME = "2NK INCI Check"
VERSION = "1.0.0"

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
                    "Rec.lang_type": LangRec.TH if lang == "th" else LangRec.CH,
                    "Rec.ocr_version": OCRVersion.PPOCRV5, "Rec.model_type": ModelType.MOBILE,
                    "Det.ocr_version": OCRVersion.PPOCRV5, "Det.model_type": ModelType.MOBILE,
                })
            return cls._eng[lang]

    @classmethod
    def run(cls, img, lang):
        r = cls.engine(lang)(img, return_word_box=True)
        out = []
        if r.boxes is None:
            return out
        words = r.word_results or [()] * len(r.txts)
        for box, txt, sc, ws in zip(r.boxes, r.txts, r.scores, words):
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            out.append({"text": txt, "score": float(sc), "box": [min(xs), min(ys), max(xs), max(ys)],
                        "words": [(w[0], [min(p[0] for p in w[2]), min(p[1] for p in w[2]),
                                          max(p[0] for p in w[2]), max(p[1] for p in w[2])]) for w in ws if w and w[0]]})
        return out


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
    w, h = page.rect.width, page.rect.height
    dpi1 = max(120, min(300, int(3000 / (max(w, h) / 72))))
    first = _ocr_clip(page, None, dpi1, lang, progress)
    if not first:
        return first
    hts = sorted(l["box"][3] - l["box"][1] for l in first)
    gap = max(4.0, hts[len(hts) // 2] * 1.5)
    regs = _regions([l["box"] for l in first], gap)
    out = []
    for ri, rg in enumerate(regs):
        inside = [l for l in first if l["box"][0] >= rg[0] - 0.5 and l["box"][2] <= rg[2] + 0.5
                  and l["box"][1] >= rg[1] - 0.5 and l["box"][3] <= rg[3] + 0.5]
        lh = sorted(l["box"][3] - l["box"][1] for l in inside)[len(inside) // 2] if inside else 10
        target = min(1000, int(42 * 72 / max(lh, 0.5)))
        rw, rh = rg[2] - rg[0] + 8, rg[3] - rg[1] + 8
        target = min(target, int(4200 * 72 / max(rw, rh)))
        if target <= dpi1 * 1.3 or len("".join(l["text"] for l in inside)) < 6:
            out += inside
            continue
        if progress:
            progress("ซูมอ่านบริเวณข้อความ %d/%d…" % (ri + 1, len(regs)))
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
    langs = ["th"] if lang == "th" else ["ch"]
    res = OCR.run(img, langs[0])
    if lang == "auto" and res and not any(has_cjk(r["text"]) for r in res):
        if progress:
            progress("อ่านซ้ำด้วยโมเดลภาษาไทย…")
        th = OCR.run(img, "th")
        merged = []
        for t in th:
            best = max(res, key=lambda r: iou(r["box"], t["box"]), default=None)
            if best is not None and iou(best["box"], t["box"]) > 0.5:
                if best["score"] >= t["score"] and not has_thai(t["text"]):
                    continue
            merged.append(t)
        for r in res:
            if not any(iou(r["box"], t["box"]) > 0.5 for t in merged):
                merged.append(r)
        res = merged
    lines = []
    tr = lambda b: [b[0] * sc + ox, b[1] * sc + oy, b[2] * sc + ox, b[3] * sc + oy]
    for r in res:
        box = tr(r["box"])
        chars = []
        for wtxt, wb in (r["words"] or [(r["text"], r["box"])]):
            wb = tr(wb)
            n = max(1, len(wtxt))
            for i, ch in enumerate(wtxt):
                chars.append((ch, [wb[0] + (wb[2] - wb[0]) * i / n, wb[1], wb[0] + (wb[2] - wb[0]) * (i + 1) / n, wb[3]]))
        txt = "".join(c for c, _ in chars) or r["text"]
        if not chars:
            n = max(1, len(txt))
            chars = [(ch, [box[0] + (box[2] - box[0]) * i / n, box[1], box[0] + (box[2] - box[0]) * (i + 1) / n, box[3]])
                     for i, ch in enumerate(txt)]
        lines.append({"text": txt, "box": box, "chars": chars, "score": r["score"]})
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
                lines = ocr_page(pg, ocr_lang, progress)
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
    "inci": r"INCI|Ingredient\s*name|Ingredients?(?!.*Japan)|ชื่อสาร|Component",
    "jp": r"Japanese|成分名|表示名|和名|日本",
    "th": r"ภาษาไทย|Thai",
    "cas": r"CAS",
    "pct": r"%|％|wt|w/w|Conc|含有|配合量|ปริมาณ|ร้อยละ",
    "func": r"Function|配合目的|機能|目的|หน้าที่",
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


def extract_formula(doc):
    """คืน list ของแถวสูตร หรือ None ถ้าไม่พบตาราง"""
    rows = []
    if doc.kind == "table":
        t = doc.table
        for hi in range(min(15, len(t))):
            m = _map_header([(v, i) for i, v in enumerate(t[hi])])
            if "inci" in m or ("jp" in m and len(m) >= 2):
                for ri in range(hi + 1, len(t)):
                    r = t[ri]
                    inci = r[m["inci"]].strip() if "inci" in m else ""
                    jp = r[m["jp"]].strip() if "jp" in m else ""
                    if not (inci or jp) or re.match(r"^(total|รวม|合計)", inci + jp, re.I):
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
                  r"How to use|Directions|Caution|Warning|Manufactur|Distribut|Net\s|Exp|Mfg|Lot)", re.I)
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


def extract_label(doc):
    """หารายการส่วนประกอบบนฉลาก คืน {items, marker, text}"""
    lines = [l for l in doc.lines if l.get("chars")]
    if doc.kind == "table":
        return _label_from_table(doc)
    best = None
    for li, l in enumerate(lines):
        for pat, w in MARKERS:
            m = re.search(pat, l["text"], re.I)
            if m:
                after = l["text"][m.end():]
                seps = sum(after.count(c) for c in SEP)
                score = w * 10 + seps + (5 if m.start() < 3 else 0)
                if best is None or score > best[0]:
                    best = (score, li, m.end())
                break
    if best is None:
        return {"items": [], "marker": None, "error": "ไม่พบหัวข้อส่วนประกอบบนฉลาก (เช่น 全成分 / Ingredients / ส่วนประกอบ)"}
    _, li0, cut = best
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
        b, lb = l["box"], last["box"]
        if b[1] < lb[1] + lh * 0.3:
            continue          # คอลัมน์อื่นในแถวเดียวกัน
        if b[1] - lb[3] > lh * 1.6:
            break
        if abs(b[0] - x0) > max(width * 0.12, lh * 2.5):
            continue          # คนละคอลัมน์
        if STOP.match(l["text"]):
            break
        para.append(l)
        last = l
        if re.search(r"[。．]\s*$", l["text"]):
            break
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
        if (ch in SEP and depth == 0) or i == len(text):
            raw = text[start:i]
            a = start + (len(raw) - len(raw.lstrip()))
            nm = raw.strip().strip("。.")
            nm = re.sub(r"\s*[\*※]\s*\d*\s*$", "", nm)
            if nm and len(nm) < 120:
                b = a + len(raw.strip())
                items.append({"text": nm, "marks": char_marks(seq, a, b), "pos": len(items), "a": a, "b": b})
            start = i + 1
    return {"items": items, "marker": l0["text"][:cut].strip(), "text": text, "seq": seq}


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
    for it in label["items"]:
        t = it["text"]
        best1 = max((score_names({norm(t)}, f) for f in fa), default=0)
        best = None
        if best1 < 92 and seq is not None and "a" in it and 4 <= len(t) <= 80:
            for k in range(2, len(t) - 1):
                l, r = t[:k].strip(), t[k:].strip()
                if len(l) < 2 or len(r) < 2:
                    continue
                sl = max(score_names(alt_names(l), f) for f in fa)
                if sl < 90:
                    continue
                sr = max(score_names(alt_names(r), f) for f in fa)
                m = min(sl, sr)
                full = min(max(score_names({norm(l)}, f) for f in fa), max(score_names({norm(r)}, f) for f in fa))
                if m >= 90 and m > best1 + 3 and (best is None or (m, full) > best[0]):
                    best = ((m, full), k)
        if best:
            k, a = best[1], it["a"]
            out.append(dict(it, text=t[:k].strip(), marks=char_marks(seq, a, a + k), split=True))
            out.append(dict(it, text=t[k:].strip(), marks=char_marks(seq, a + k, it["b"]), split=True))
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
        self._index()

    def _index(self):
        self.by_cas, self.by_name = {}, {}
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
        self.mentions = {}
        for m in self.db.get("mentions", []):
            line = m["line"].strip()
            listed = re.match(r"^(\(\d+\)|\d+[.)]|[-•⚫●▪*])\s*", line)
            alone = len(re.sub(r"[^A-Z]", "", m["text"])) >= 0.7 * max(1, len(re.sub(r"[^A-Za-z]", "", line)))
            if not (listed or alone) or norm_latin(m["text"]) in ("water", "aqua", "fragrance", "parfum"):
                continue
            for part in re.split(r"\s*/\s*|\s+หรือ\s+", m["text"]):
                k = norm_latin(re.sub(r"\(.*?\)", "", part))
                if len(k) >= 5:
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
    alts = set()
    base = SALT_RE.sub("", name).strip(" ,;")
    for s in (name, base):
        for part in re.split(r";", s):
            p = part.strip()
            if p:
                alts.add(p)
                np_ = re.sub(r"\(([^()]*)\)", "", p).strip()
                if np_:
                    alts.add(np_)
                for inner in re.findall(r"\(([^()]{3,})\)", p):
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
                          "pct": r["pct"], "marks": r["marks"].get("inci") or r["row_marks"],
                          "row_marks": r["row_marks"], "from": "formula"})
        return items, "formula", None
    lab = extract_label(doc)
    items = [{"name": it["text"], "jp": "", "th": "", "cas": [], "pct": None, "marks": it["marks"],
              "row_marks": it["marks"], "from": "label"} for it in lab["items"]]
    return items, "label", lab.get("error")


def reg_check(doc, reg):
    items, mode, err = reg_items(doc)
    for it in items:
        names = [it["name"]] + [p for p in re.split(r"[/()]", it["name"]) if len(p.strip()) > 3]
        hits = reg.lookup(names, it["cas"])
        it["hits"] = hits
        it["guide"] = reg.guideline(names)
        latin = re.sub(r"[^A-Za-z]", "", it["name"])
        it["latin"] = len(latin) >= 3 and not has_cjk(it["name"]) and not has_thai(it["name"])
        it["level"] = ("banned" if any(h["kind"] == "banned" and not h["entry"].get("superseded") for h in hits)
                       else "restricted" if hits else "guide" if it["guide"] else "ok")
    words = check_words(doc, reg.db.get("words", [])) if mode == "label" or doc.source == "ocr" else []
    summary = {k: sum(1 for i in items if i["level"] == k) for k in ("banned", "restricted", "guide", "ok")}
    summary["words"] = len(words)
    return {"items": items, "mode": mode, "error": err, "words": words, "summary": summary}
