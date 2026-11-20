"""2NK INCI Check — หน้าต่างโปรแกรม (pywebview) + API ที่หน้าเว็บเรียก"""
import base64, json, os, re, sys, tempfile, threading, traceback, webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import webview
import core
import updater


def clean_marks(x):
    return x


class Api:
    def __init__(self):
        self._window = None
        self._docs = {}
        self._label = None
        self._formula = None
        self._reg = core.RegDB()
        self._cosing = core.CosIng()
        self._lock = threading.Lock()
        self._seq = {}

    # ------------------------------------------------ helpers
    def _js(self, fn, *args):
        if self._window:
            try:
                self._window.evaluate_js("%s(%s)" % (fn, ",".join(json.dumps(a, ensure_ascii=False) for a in args)))
            except Exception:
                pass

    def _prog(self, slot):
        return lambda msg: self._js("onProgress", slot, msg)

    def info(self):
        db = self._reg.db
        return {"version": core.VERSION, "db_version": db.get("version", ""),
                "counts": {k: len(db.get(k, [])) for k in ("banned", "restricted", "words")},
                "sources": db.get("sources", {})}

    # ------------------------------------------------ files
    def pick_file(self):
        # pywebview รับชื่อตัวกรองเฉพาะตัวอักษรอังกฤษ (ภาษาไทยทำให้หน้าต่างไม่เปิด)
        types = ("Supported files (*.pdf;*.png;*.jpg;*.jpeg;*.bmp;*.tif;*.tiff;*.webp;*.xlsx;*.xlsm;*.csv)",
                 "All files (*.*)")
        try:
            r = self._window.create_file_dialog(webview.FileDialog.OPEN, file_types=types)
        except Exception:
            traceback.print_exc()
            r = self._window.create_file_dialog(webview.FileDialog.OPEN)
        return r[0] if r else None

    def load_bytes(self, slot, name, b64, opts=None):
        d = os.path.join(tempfile.gettempdir(), "2nk_inci_drop")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, re.sub(r'[\\/:*?"<>|]', "_", name))
        with open(p, "wb") as f:
            f.write(base64.b64decode(b64.split(",")[-1]))
        return self.load(slot, p, opts)

    def clear(self, slot):
        self._seq[slot] = self._seq.get(slot, 0) + 1   # งานอ่านไฟล์ที่ค้างอยู่จะถูกทิ้ง
        self._docs.pop(slot, None)
        if slot == "label":
            self._label = None
        elif slot == "formula":
            self._formula = None
        return True

    def load(self, slot, path, opts=None):
        opts = opts or {}
        seq = opts.get("seq")
        if seq is not None:
            self._seq[slot] = seq
        stale = lambda: seq is not None and self._seq.get(slot) != seq
        try:
            prog = self._prog(slot)
            prog("กำลังเปิดไฟล์…")
            doc = core.Doc(path, progress=prog, ocr_lang=opts.get("lang", "auto"), force_ocr=opts.get("ocr", False))
            if stale():
                return {"ok": False, "stale": True}
            self._docs[slot] = doc
            prog("กำลังแสดงผล…")
            view = doc.view()
            out = {"ok": True, "view": view, "path": path}
            if slot == "label":
                lab = core.extract_label(doc)
                if not lab["items"]:
                    rows = core.extract_formula(doc)
                    if rows:
                        lab = core._label_from_table(doc)
                self._label = lab
                out["label"] = {k: v for k, v in lab.items() if k != "seq"}
            elif slot == "formula":
                rows = core.extract_formula(doc)
                if not rows:
                    lab = core.extract_label(doc)
                    rows = [{"inci": it["text"], "jp": "", "th": "", "cas": [], "cas_text": "", "pct": None, "func": "",
                             "marks": {"inci": it["marks"]}, "row_marks": it["marks"], "idx": i, "names": [it["text"]],
                             "label": it["text"]} for i, it in enumerate(lab["items"])]
                    out["warn"] = "ไม่พบตารางสูตร (หัวคอลัมน์ INCI) — ใช้รายการส่วนประกอบในไฟล์แทน" if rows else \
                        "ไม่พบตารางสูตรหรือรายการส่วนประกอบในไฟล์นี้"
                self._formula = rows
                out["formula"] = rows
            elif slot == "reg":
                prog("กำลังตรวจกับฐานข้อมูล อย.…")
                out["reg"] = core.reg_check(doc, self._reg)
            prog("")
            return out
        except Exception as e:
            traceback.print_exc()
            self._js("onProgress", slot, "")
            return {"ok": False, "error": str(e)}

    def compare(self):
        if not (self._label and self._formula):
            return {"ok": False, "error": "เลือกไฟล์ฉลากและไฟล์สูตรก่อน"}
        try:
            res = core.compare(self._label, self._formula)
            res["items"] = [{k: v for k, v in it.items()} for it in self._label["items"]]
            return dict(res, ok=True)
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def edit_label(self, idx, text):
        """ผู้ใช้แก้ชื่อที่ OCR อ่านผิด (ใส่ , ถ้าเป็นหลายชื่อ) แล้วเทียบใหม่"""
        try:
            items = self._label["items"]
            it = items[int(idx)]
            parts = [p.strip() for p in re.split(r"[,、，;]", text) if p.strip()]
            new = [dict(it, text=p, edited=True) for p in parts]
            self._label["items"] = items[:int(idx)] + new + items[int(idx) + 1:]
            return self.compare()
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}

    def save_html(self, name, html):
        name = re.sub(r'[\\/:*?"<>|]', "_", name)[:120]
        r = self._window.create_file_dialog(webview.FileDialog.SAVE, save_filename=name, file_types=("HTML files (*.html)",))
        if not r:
            return {"ok": False}
        p = r if isinstance(r, str) else r[0]
        if not p.lower().endswith((".html", ".htm")):
            p += ".html"
        with open(p, "w", encoding="utf-8") as f:
            f.write(html)
        return {"ok": True, "path": p}

    # ------------------------------------------------ CosIng / web
    def cosing(self, name):
        return self._cosing.check(name)

    def open_url(self, url):
        if re.match(r"^https?://", url or ""):
            webbrowser.open(url)

    # ------------------------------------------------ regulation docs
    def reg_page(self, fn, page, rect=None):
        import pymupdf
        fn = os.path.basename(fn)
        for d in (os.path.join(core.USER_DIR, "regs"), os.path.join(core.DATA, "regs")):
            p = os.path.join(d, fn)
            if os.path.exists(p):
                doc = pymupdf.open(p)
                pg = doc[int(page) - 1]
                if pg.rotation:
                    pg.remove_rotation()
                z = 1600 / max(pg.rect.width, pg.rect.height)
                pix = pg.get_pixmap(matrix=pymupdf.Matrix(z, z), alpha=False)
                return {"ok": True, "w": pg.rect.width, "h": pg.rect.height, "pages": len(doc),
                        "img": "data:image/png;base64," + base64.b64encode(pix.tobytes("png")).decode(),
                        "title": self._reg.db.get("sources", {}).get(fn, fn)}
        return {"ok": False, "error": "ไม่พบไฟล์ประกาศ " + fn}

    def db_search(self, q):
        q = (q or "").strip()
        if len(q) < 2:
            return []
        db = self._reg.db
        nq = core.norm_latin(q)
        out = []
        cas = core.CAS_RE.findall(q)
        for kind in ("banned", "restricted"):
            for e in db[kind]:
                hay = e.get("name") or (e.get("chem", "") + " " + e.get("inci", "") + " " + " ".join(e.get("names", [])))
                if (cas and set(cas) & set(e["cas"])) or (nq and nq in core.norm_latin(hay)) or q.lower() in hay.lower():
                    out.append({"kind": kind, "entry": e})
                    if len(out) > 150:
                        return out
        for w in db.get("words", []):
            if q.lower() in w["word"].lower():
                out.append({"kind": "word", "entry": w})
        return out

    # ------------------------------------------------ export
    def export_report(self, kind, data, title):
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment
        wb = openpyxl.Workbook()
        ws = wb.active

        def sheet(ws, name, heads, rows):
            ws.title = name
            ws.append([title])
            ws["A1"].font = Font(bold=True, size=12)
            ws.append([h for h, _ in heads])
            for c in ws[2]:
                c.font = Font(bold=True, color="FFFFFF")
                c.fill = PatternFill("solid", fgColor="1F2937")
            for r in rows:
                ws.append([r.get(k, "") for _, k in heads])
            for i, (h, _) in enumerate(heads):
                ws.column_dimensions[openpyxl.utils.get_column_letter(i + 1)].width = max(10, min(60, len(h) * 2 + 6))
            for row in ws.iter_rows(min_row=3):
                for c in row:
                    c.alignment = Alignment(wrap_text=True, vertical="top")

        if kind == "cmp":
            sheet(ws, "ฉลาก vs สูตร", [("ผล", "status"), ("ลำดับในสูตร", "formula_no"), ("INCI", "inci"), ("ชื่อญี่ปุ่น", "jp"),
                                      ("%", "pct"), ("บนฉลาก", "label"), ("ลำดับบนฉลาก", "label_no"), ("ความเหมือน %", "score"),
                                      ("หมายเหตุ", "note")], data)
        else:
            sheet(ws, "สารห้ามใช้ อย.", [("ผล", "level"), ("ชื่อสาร", "name"), ("ชื่อญี่ปุ่น", "jp"), ("CAS", "cas"), ("%", "pct"),
                                        ("พบในบัญชี อย.", "hits"), ("ปริมาณสูงสุด", "limit"), ("CosIng", "cosing"),
                                        ("หลักเกณฑ์", "guide")], data["rows"])
            if data.get("words"):
                sheet(wb.create_sheet(), "คำห้ามใช้", [("คำห้ามใช้", "word"), ("ลำดับ", "no"), ("พบ", "found"), ("ข้อความ", "line")],
                      data["words"])
        name = re.sub(r'[\\/:*?"<>|]', "_", "ผลตรวจ " + os.path.splitext(title)[0])[:120] + ".xlsx"
        r = self._window.create_file_dialog(webview.FileDialog.SAVE, save_filename=name,
                                            file_types=("Excel files (*.xlsx)",))
        if not r:
            return {"ok": False}
        p = r if isinstance(r, str) else r[0]
        if not p.lower().endswith(".xlsx"):
            p += ".xlsx"
        wb.save(p)
        return {"ok": True, "path": p}

    def open_path(self, p):
        if p and os.path.exists(p):
            os.startfile(p)

    # ------------------------------------------------ update
    def check_update(self):
        try:
            return dict(updater.check(self._reg.db.get("version")), ok=True)
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def do_update(self, what):
        try:
            info = updater.check(self._reg.db.get("version"))["latest"]
            prog = lambda p: self._js("onUpdateProgress", what, p)
            if what == "db":
                updater.update_db(info, prog)
                self._reg.load()
                return {"ok": True, "info": self.info()}
            updater.update_app(info, prog)
            threading.Timer(0.8, lambda: self._window.destroy()).start()
            return {"ok": True}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": str(e)}


def main():
    if "--test-replace" in sys.argv:   # ทดสอบขั้นแทนที่ไฟล์ของการอัปเดต
        updater.start_replacer(sys.argv[sys.argv.index("--test-replace") + 1], sys.executable)
        return
    api = Api()
    ui = os.path.join(core.BASE, "ui", "index.html")
    win = webview.create_window("2NK INCI Check", ui, js_api=api, width=1500, height=920, min_size=(1100, 680),
                                background_color="#0F1115", text_select=True)
    api._window = win
    if "--selftest" in sys.argv:
        out = sys.argv[sys.argv.index("--selftest") + 1]

        def run():
            res = {}
            try:
                a = sys.argv[sys.argv.index("--selftest") + 2:]
                res["label"] = api.load("label", a[0])["ok"]
                res["formula"] = api.load("formula", a[1])["ok"]
                c = api.compare()
                res["summary"] = c.get("summary")
                res["reg"] = api.load("reg", a[1]).get("reg", {}).get("summary")
                res["cosing"] = api.cosing("PHENOXYETHANOL").get("status")
            except Exception as e:
                res["error"] = repr(e)
            with open(out, "w", encoding="utf-8") as f:
                json.dump(res, f, ensure_ascii=False)
            win.destroy()
        webview.start(run, gui="edgechromium", private_mode=False)
        return
    webview.start(gui="edgechromium", private_mode=False,
                  storage_path=os.path.join(core.USER_DIR, "webview"))


if __name__ == "__main__":
    main()
