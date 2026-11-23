"""ถาม-ตอบแบบออฟไลน์จากเอกสาร อย. (ไม่ใช้ AI — ตอบจากข้อมูลในประกาศจริงพร้อมหน้าอ้างอิง)

1) คำถามเจาะจง (ชื่อสาร / CAS / ลำดับ / % / คำในชื่อสินค้า) → ตอบจากฐานข้อมูลโดยตรง
2) คำถามทั่วไป → หาย่อหน้าที่เกี่ยวข้องที่สุดในหลักเกณฑ์ + รายการในประกาศ (ค้นแบบ n-gram ใช้ได้กับภาษาไทยที่ไม่มีเว้นวรรค)
"""
import math, os, re, unicodedata
from collections import Counter

import pymupdf
from rapidfuzz import fuzz, process

import core

STOP_TH = ["ได้ไหม", "ได้มั้ย", "ได้หรือไม่", "หรือไม่", "ไหม", "มั้ย", "อะไรบ้าง", "อะไร", "อย่างไร", "ยังไง", "บ้าง", "คือ",
           "ครับ", "ค่ะ", "คะ", "นะ", "หน่อย", "ช่วย", "บอก", "ต้องการ", "อยากรู้", "สามารถ", "ใช้ได้", "ไหมคะ", "ไหมครับ"]

# คำบอกประเภทสินค้า → คำที่ปรากฏในช่อง "บริเวณที่ใช้" ของประกาศ
PRODUCT_HINTS = {
    "ล้างออก": ["ล้างออก"], "แชมพู": ["ล้างออก", "เส้นผม", "ผม"], "ครีมนวด": ["ล้างออก", "เส้นผม", "ผม"],
    "ทรีทเมนต์": ["เส้นผม", "ผม"], "ผม": ["เส้นผม", "ผม"], "ย้อม": ["ย้อม"], "ดัด": ["ดัด"], "ยืด": ["ยืด"],
    "ครีม": ["ไม่ต้องล้างออก", "ไม่ล้างออก", "ผิว"], "โลชั่น": ["ไม่ต้องล้างออก", "ไม่ล้างออก", "ผิว"],
    "เซรั่ม": ["ไม่ต้องล้างออก", "ไม่ล้างออก", "ผิว"], "ไม่ล้าง": ["ไม่ต้องล้างออก", "ไม่ล้างออก"],
    "เล็บ": ["เล็บ"], "ยาสีฟัน": ["ยาสีฟัน", "ช่องปาก"], "ช่องปาก": ["ช่องปาก"], "บ้วนปาก": ["บ้วนปาก", "ช่องปาก"],
    "เด็ก": ["เด็ก"], "สเปรย์": ["ฉีดพ่น", "สเปรย์", "spray"], "แป้ง": ["แป้ง"], "รอบดวงตา": ["ดวงตา", "ตา"],
    "ริมฝีปาก": ["ริมฝีปาก"], "ลิป": ["ริมฝีปาก"], "กำจัดขน": ["กำจัดขน"], "ระงับกลิ่น": ["ระงับกลิ่น"],
}


def _clean(s):
    s = unicodedata.normalize("NFKC", s or "").lower()
    return re.sub(r"[\s​]+", "", s)


def _grams(s, n=3):
    s = _clean(s)
    return [s[i:i + n] for i in range(max(0, len(s) - n + 1))] or ([s] if s else [])


class QA:
    def __init__(self, reg):
        self.reg = reg
        self._index = None
        self._names = None

    # ------------------------------------------------ index
    def _build(self):
        docs = []
        db = self.reg.db
        # ย่อหน้าในหลักเกณฑ์ (คำห้ามใช้/แนวทางการจดแจ้ง)
        p = _reg_path("forbidden-words.pdf")
        if p:
            d = pymupdf.open(p)
            for pi, pg in enumerate(d):
                for b in pg.get_text("blocks"):
                    t = re.sub(r"\s+", " ", b[4]).strip()
                    if len(t) < 25 or re.fullmatch(r"[\d\s\-]+|ฉบับปรับปรุง.*", t):
                        continue
                    docs.append({"kind": "guide", "text": t, "file": "forbidden-words.pdf", "page": pi + 1,
                                 "rect": [round(v, 1) for v in b[:4]]})
        # รายการวัตถุที่อาจใช้ (มีเงื่อนไข) — ค้นด้วยบริเวณที่ใช้/เงื่อนไขได้ เช่น "สำหรับเด็ก"
        for i, e in enumerate(db.get("restricted", [])):
            t = " ".join([e.get("chem", ""), e.get("inci", "")] + [" ".join(u.values()) for u in e.get("uses", [])])
            docs.append({"kind": "restricted", "i": i, "text": t, "file": e["file"], "page": e["page"], "rect": e["rect"]})
        df = Counter()
        for d_ in docs:
            g = set(_grams(d_["text"]))
            d_["g"] = Counter(_grams(d_["text"]))
            d_["len"] = max(1, len(d_["text"]))
            df.update(g)
        n = len(docs)
        self._idf = {k: math.log(1 + n / v) for k, v in df.items()}
        self._index = docs

    def _all_names(self):
        if self._names is None:
            names = {}
            for kind in ("banned", "restricted"):
                for i, e in enumerate(self.reg.db.get(kind, [])):
                    cands = core.banned_alts(e["name"]) if kind == "banned" else e.get("names", []) + [e.get("inci", "")]
                    for c in cands:
                        c = c.strip()
                        if 3 <= len(c) <= 60 and re.search(r"[A-Za-z]{3}", c) and not re.search(r"[-,(]$|^[-),]", c):
                            names.setdefault(c.lower(), c)
            self._names = names
        return self._names

    # ------------------------------------------------ answer
    def ask(self, q):
        q = (q or "").strip()
        if not q:
            return {"answers": []}
        out, used = [], set()
        handled = False

        # จำนวนรายการในฐานข้อมูล
        if re.search(r"กี่(รายการ|ตัว|สาร|คำ)|ทั้งหมดกี่|มีกี่", q) and not re.search(r"%|เปอร์เซ็นต์", q):
            handled = True
            db = self.reg.db
            out.append({"type": "info", "title": "จำนวนรายการในฐานข้อมูล",
                        "lines": ["วัตถุห้ามใช้ %d รายการ (บัญชี พ.ศ. 2566 ปรับปรุงถึง ม.ค. 2568)" % len(db["banned"]),
                                  "วัตถุที่อาจใช้ (มีเงื่อนไข) %d รายการ (ประกาศ พ.ศ. 2561 + ฉบับแก้ไข)" % len(db["restricted"]),
                                  "คำห้ามใช้ในชื่อเครื่องสำอาง %d คำ (ฉบับปรับปรุง 2 ธ.ค. 2564)" % len(db.get("words", []))]})

        # ลำดับในประกาศ
        for m in re.finditer(r"ลำดับ(?:ที่)?\s*(\d{1,4}[a-z]?)", q):
            no = m.group(1)
            kind = "restricted" if re.search(r"อาจใช้|เงื่อนไข|จำกัด", q) else None
            for k in ([kind] if kind else ["banned", "restricted"]):
                for e in self.reg.db[k]:
                    if e["no"] == no:
                        out.append(self._entry(k, e, "ลำดับที่ %s" % no))
                        used.add((k, e["no"], e["page"]))
                        handled = True

        # สาร: เลข CAS หรือชื่อภาษาอังกฤษในคำถาม
        pct = None
        mp = re.search(r"(\d+(?:\.\d+)?)\s*%", q)
        if mp:
            pct = float(mp.group(1))
        hints = [h for k, hs in PRODUCT_HINTS.items() if k in q for h in hs]
        cas = core.CAS_RE.findall(q)
        latin = [s.strip(" .,'\"") for s in re.findall(r"[A-Za-z0-9][A-Za-z0-9 ,\-()/.'+]*[A-Za-z0-9)]", q)]
        # ตัดตัวเลขท้าย/หน้าที่เป็นปริมาณ เช่น "Climbazole 3" (จาก 3%) หรือ "2 Salicylic acid"
        latin = [re.sub(r"^\d+(?:\.\d+)?\s+|\s+\d+(?:\.\d+)?$", "", s).strip() for s in latin]
        latin = [s for s in latin if re.search(r"[A-Za-z]{3}", s) and not core.CAS_RE.fullmatch(s)]
        # คำถามเรื่องชื่อสินค้า: ข้อความในเครื่องหมายคำพูดเป็นชื่อสินค้า ไม่ใช่ชื่อสาร
        name_q = bool(re.search(r"ชื่อ|คำว่า|ใช้คำ", q))
        quoted_all = " ".join(re.findall(r"[\"“'‘]([^\"”'’]{2,60})[\"”'’]", q)).lower()
        found_any = handled
        for name in latin + cas:
            if name_q and (name.lower() in quoted_all or re.search(r"คำว่า\s*" + re.escape(name), q)):
                hits_ = self.reg.lookup([name], [])
                if not hits_:
                    continue
            is_cas = bool(core.CAS_RE.fullmatch(name))
            hits = self.reg.lookup([] if is_cas else [name], [name] if is_cas else [])
            guide = [] if is_cas else self.reg.guideline([name])
            label = name
            if not hits and not guide and not is_cas:
                # สะกดใกล้เคียงชื่อในประกาศ
                names = self._all_names()
                best = process.extractOne(name.lower(), list(names.keys()), scorer=fuzz.ratio)
                if best and best[1] >= 86 and best[0] != name.lower():
                    alt = names[best[0]]
                    hits = self.reg.lookup([alt], [])
                    guide = self.reg.guideline([alt])
                    if hits or guide:
                        label = alt
                        out.append({"type": "note", "title": "ไม่พบ “%s” ตรงตัว — แสดงผลของ “%s” ที่สะกดใกล้เคียง" % (name, alt)})
            if hits or guide:
                found_any = True
                out += self._substance(label, hits, guide, pct, hints)
                if not hits:
                    # ไม่อยู่ในบัญชี แต่หลักเกณฑ์มีเงื่อนไข (เช่น ALCOHOL ไม่เกิน 69.4%) → หาย่อหน้าที่พูดถึงสารนี้
                    out += self._passages(label + " " + q, used, limit=3, must=label)
            elif is_cas or len(name) >= 4:
                found_any = True
                out.append({"type": "ok", "title": "“%s” ไม่พบในบัญชีวัตถุห้ามใช้ / วัตถุที่อาจใช้" % name,
                            "lines": ["ไม่พบในเอกสารที่ตั้งค่าไว้ในโปรแกรม — ไม่ได้แปลว่าใช้ได้แน่นอน: ยังไม่มีบัญชีวัตถุกันเสีย สี และสารป้องกันแสงแดดของไทยในฐานข้อมูล",
                                      "ตรวจชื่อสากลและข้อจำกัดของ EU ได้ที่แท็บ “ตรวจสารห้ามใช้ อย.” (มีข้อมูล CosIng)"]})

        # คำในชื่อสินค้า: คำในเครื่องหมายคำพูด หรือคำถามที่พูดถึงชื่อ/คำว่า
        if re.search(r"ชื่อ|คำว่า|คำห้าม|ใช้คำ", q) or re.search(r"[\"“'‘]", q):
            quoted = re.findall(r"[\"“'‘]([^\"”'’]{2,60})[\"”'’]", q)
            m = re.search(r"คำว่า\s*([^\s?？]+(?:\s+[A-Za-z0-9\-]+)*)", q)
            if m:
                quoted.append(m.group(1))
            out += self._words(quoted or [q], q)

        # คำถามทั่วไป → ย่อหน้าที่เกี่ยวข้องที่สุด
        if not out or (not found_any and not name_q):
            out += self._passages(q, used)
        if not out:
            out.append({"type": "none", "title": "ไม่พบข้อมูลที่ตอบคำถามนี้ในเอกสาร",
                        "lines": ["ลองถามด้วยชื่อสารภาษาอังกฤษ (INCI) หรือเลข CAS เช่น “Hydroquinone ใช้ได้ไหม”, “Salicylic acid ในแชมพูใช้ได้กี่ %”"]})
        return {"answers": out}

    # ------------------------------------------------ builders
    def _entry(self, kind, e, why=""):
        ref = {"file": e["file"], "page": e["page"], "rect": e["rect"], "label": "%s หน้า %d" % (_src_short(e["file"]), e["page"])}
        if kind == "banned":
            return {"type": "bad", "title": "ห้ามใช้ — วัตถุห้ามใช้ ลำดับ %s%s" % (e["no"], " (" + why + ")" if why and "ลำดับ" not in why else ""),
                    "lines": [e["name"]] + (["CAS " + ", ".join(e["cas"])] if e["cas"] else []), "refs": [ref]}
        return {"type": "warn", "title": "วัตถุที่อาจใช้ ลำดับ %s — ใช้ได้ภายใต้เงื่อนไข" % e["no"],
                "lines": [e["chem"] + ((" (" + e["inci"] + ")") if e.get("inci") and e["inci"] != "-" else "")]
                + (["CAS " + ", ".join(e["cas"])] if e["cas"] else []),
                "uses": e["uses"], "src": e["src"], "refs": [ref]}

    def _substance(self, name, hits, guide, pct, hints):
        out = []
        cur = [h for h in hits if not h["entry"].get("superseded")]
        banned = [h for h in cur if h["kind"] == "banned"]
        restr = [h for h in cur if h["kind"] == "restricted"]
        if banned and not restr:
            out.append({"type": "bad", "title": "“%s” เป็นวัตถุห้ามใช้ในเครื่องสำอาง" % name,
                        "lines": ["ห้ามใช้เป็นส่วนผสมในการผลิตเครื่องสำอาง (พบโดย %s)" % ", ".join(sorted({h["how"] for h in banned}))]})
        elif banned and restr:
            out.append({"type": "bad", "title": "“%s” อยู่ทั้งในบัญชีห้ามใช้และบัญชีวัตถุที่อาจใช้" % name,
                        "lines": ["โดยทั่วไปห้ามใช้ ยกเว้นตามบริเวณและเงื่อนไขที่ระบุในบัญชีวัตถุที่อาจใช้ด้านล่างเท่านั้น"]})
        elif restr:
            out.append({"type": "warn", "title": "“%s” ใช้ได้ภายใต้เงื่อนไข (วัตถุที่อาจใช้)" % name,
                        "lines": ["ต้องเป็นไปตามบริเวณที่ใช้ ปริมาณสูงสุด และเงื่อนไขในประกาศ"]})
        for h in banned:
            out.append(self._entry("banned", h["entry"], h["how"]))
        for h in restr:
            a = self._entry("restricted", h["entry"])
            uses = a["uses"]
            if hints:
                match = [u for u in uses if any(k in (u["area"] + u["cond"]).lower() for k in hints)]
                if match:
                    a["focus"] = [uses.index(u) for u in match]
            if pct is not None:
                lims = [(core_pct(u["max"]), u) for u in uses]
                lims = [(m, u) for m, u in lims if m is not None]
                sel = [(m, u) for m, u in lims if not a.get("focus") or uses.index(u) in a["focus"]]
                if sel:
                    mx = max(m for m, _ in sel)
                    verdict = ("เกินกำหนด — %.4g%% มากกว่าปริมาณสูงสุด %.4g%%" % (pct, mx)) if pct > mx else \
                              ("ไม่เกินกำหนด — %.4g%% ไม่เกินปริมาณสูงสุด %.4g%%" % (pct, mx))
                    a["verdict"] = verdict + ("" if a.get("focus") or len(sel) == 1 else " (ขึ้นกับประเภทผลิตภัณฑ์ ดูตาราง)")
                    a["verdict_bad"] = pct > mx
            out.append(a)
        if guide:
            out.append({"type": "info", "title": "มีกล่าวถึงในหลักเกณฑ์การจดแจ้งของ อย.",
                        "lines": [g["line"] for g in guide[:4]],
                        "refs": [{"file": "forbidden-words.pdf", "page": g["page"], "rect": g["rect"],
                                  "label": "หลักเกณฑ์ หน้า %d" % g["page"]} for g in guide[:4]]})
        return out

    def _words(self, texts, q):
        out = []
        words = self.reg.db.get("words", [])
        found = []
        for t in texts:
            for w in words:
                ww = w["word"].replace("*", "").strip()
                if len(ww) < 2 or re.search(r"หลักเกณฑ์|ให้ใช้", ww):
                    continue
                if re.search(r"[A-Za-z]", ww):
                    hit = re.search(r"(?<![A-Za-z])" + re.escape(ww).replace(r"\ ", r"[\s\-]*") + r"(?![A-Za-z])", t, re.I)
                else:
                    hit = ww in t
                if hit and w not in found:
                    found.append(w)
        if found:
            out.append({"type": "bad", "title": "พบคำห้ามใช้ในชื่อการค้า/ชื่อเครื่องสำอาง %d คำ" % len(found),
                        "lines": ["“%s” — ลำดับ %d ในรายการคำห้ามใช้" % (w["word"], w["no"]) for w in found[:12]],
                        "refs": [{"file": w["file"], "page": w["page"], "rect": w["rect"], "label": "คำห้ามใช้ หน้า %d" % w["page"]}
                                 for w in found[:6]]})
        elif texts and texts[0] != q:
            out.append({"type": "ok", "title": "ไม่พบ “%s” ในรายการคำห้ามใช้ 438 คำ" % ", ".join(texts),
                        "lines": ["ยังต้องดูเงื่อนไขอื่นในหลักเกณฑ์ (คำที่สื่อสรรพคุณบางคำใช้ได้เมื่อสูตรมีสารที่กำหนด) — ดูย่อหน้าที่เกี่ยวข้องด้านล่าง"]})
            out += self._passages(" ".join(texts), set(), limit=3)
        return out

    def _passages(self, q, used, limit=4, must=None):
        if self._index is None:
            self._build()
        # ต้องมีชื่อสารเป็นคำเต็ม (Phenoxyethanol ≠ 2,4-Diaminophenoxyethanol)
        mustc = re.compile(r"(?<![a-z0-9\-)\]])(?<!- )" + re.escape(must.lower()).replace(r"\ ", r"\s*") + r"(?![a-z])") if must else None
        qq = q
        for s in STOP_TH:
            qq = qq.replace(s, " ")
        qg = Counter(_grams(qq))
        if not qg:
            return []
        scored = []
        for d in self._index:
            if mustc and not mustc.search(d["text"].lower()):
                continue
            common = [g for g in qg if g in d["g"]]
            if not common:
                continue
            s = sum(self._idf.get(g, 0) for g in common) / math.sqrt(sum(self._idf.get(g, 0) for g in qg) or 1)
            s /= (1 + math.log(1 + d["len"] / 400))
            scored.append((s, d))
        scored.sort(key=lambda x: -x[0])
        out, seen = [], set()
        top = scored[0][0] if scored else 0
        for s, d in scored:
            if s < top * 0.55 or len(out) >= limit or (s < 0.9 and not mustc):
                break
            key = (d["file"], d["page"], d.get("i"))
            if key in seen:
                continue
            seen.add(key)
            if d["kind"] == "restricted":
                e = self.reg.db["restricted"][d["i"]]
                a = self._entry("restricted", e)
                a["title"] = "เกี่ยวข้อง: " + a["title"]
                out.append(a)
            else:
                out.append({"type": "passage", "title": "หลักเกณฑ์การจดแจ้ง หน้า %d" % d["page"], "lines": [d["text"]],
                            "refs": [{"file": d["file"], "page": d["page"], "rect": d["rect"], "label": "เปิดหน้า %d" % d["page"]}]})
        if out:
            out.insert(0, {"type": "note", "title": "ข้อความในเอกสารที่ตรงกับคำถามมากที่สุด (อ่านเงื่อนไขในหน้าต้นฉบับประกอบ)"})
        return out


def summaries(db):
    """สรุปเอกสารแต่ละฉบับ (เขียนจากเนื้อหาในไฟล์ PDF; ตัวเลขนับจากฐานข้อมูลจริง)"""
    from collections import OrderedDict
    groups = OrderedDict()
    for e in db.get("restricted", []):
        groups.setdefault(e["src"], []).append(e["no"])
    amend = ["%s: ลำดับ %s" % (k, ", ".join(v[:12]) + (" …" if len(v) > 12 else "")) for k, v in groups.items() if "แก้ไข" in k]
    nb = len(db.get("banned", []))
    return [
        {"file": "banned-2566-update-2568.pdf", "title": "บัญชีวัตถุห้ามใช้ในเครื่องสำอาง",
         "sub": "ประกาศกระทรวงสาธารณสุข พ.ศ. 2566 รวมฉบับแก้ไขถึง 31 ม.ค. 2568",
         "points": ["รายชื่อสาร %d ลำดับ ที่ห้ามใช้เป็นส่วนผสมในการผลิตเครื่องสำอาง (มีชื่อสารและเลข CAS)" % nb,
                    "บางรายการมีข้อยกเว้นเขียนกำกับ เช่น ปนเปื้อนได้ไม่เกินค่าที่กำหนด หรือ “ยกเว้นวัตถุที่ระบุในประกาศวัตถุที่อาจใช้” (เช่น Hydroquinone ลำดับ 1233)",
                    "ท้ายเล่ม (หน้า 72–78) เป็นประกาศแก้ไข: แก้ลำดับ 343 (Dioxane — กำหนดการปนเปื้อน), เพิ่มลำดับ 1611–1612 (พ.ศ. 2567) และ 1613–1668 (ราชกิจจาฯ 31 ม.ค. 2568)",
                    "ข้อความภาษาไทยในหน้าประกาศแก้ไขบางหน้าฟอนต์เพี้ยน อ่านจากไฟล์ไม่ได้ — เปิดดูหน้าเอกสารแทน"],
         "page": 1},
        {"file": "restricted-2561-amend1-9.pdf", "title": "วัตถุที่อาจใช้เป็นส่วนผสม (มีปริมาณ/เงื่อนไข)",
         "sub": "ประกาศกระทรวงสาธารณสุข พ.ศ. 2561 รวมประกาศแก้ไข",
         "points": ["สาร %d รายการที่ใช้ได้ แต่ต้องอยู่ในบริเวณที่ใช้ (ประเภทผลิตภัณฑ์) ปริมาณสูงสุด และเงื่อนไขที่กำหนด" % len(db.get("restricted", [])),
                    "แต่ละรายการมี: ชื่อเคมี / ชื่อใน Common Ingredients Glossary / CAS / บริเวณที่ใช้ / ความเข้มข้นสูงสุดในเครื่องสำอางพร้อมใช้ / เงื่อนไข",
                    "ฉบับหลักมีลำดับ 1a–187 (ประมาณ 194 รายการ); ประกาศแก้ไขที่อ่านได้ในไฟล์:"] + amend +
                   ["ลำดับเดียวกันที่ถูกแก้ในฉบับหลัง โปรแกรมใช้ฉบับหลังเป็นหลัก",
                    "ไม่รวมบัญชีวัตถุกันเสีย สี และสารป้องกันแสงแดด (เป็นประกาศแยก ยังไม่มีในโปรแกรม)"],
         "page": 1},
        {"file": "forbidden-words.pdf", "title": "หลักเกณฑ์การพิจารณาการจดแจ้งเครื่องสำอาง + คำห้ามใช้",
         "sub": "อย. กลุ่มกำกับดูแลเครื่องสำอางก่อนออกสู่ตลาด — ฉบับปรับปรุง 2 ธ.ค. 2564 (83 หน้า)",
         "points": ["ผลิตภัณฑ์ใดเป็น/ไม่เป็นเครื่องสำอาง (หน้า 4–8), หน้าที่ของผู้ผลิต/ผู้นำเข้า, คำแนะนำการยื่นแบบ จ.ค. และเอกสารสูตร (หน้า 11–13)",
                    "การพิจารณาชื่อการค้า/ชื่อเครื่องสำอาง: คำที่รับแจ้งได้เมื่อมีเงื่อนไข เช่น WHITENING/BRIGHTENING ต้องมีสารที่กำหนดในสูตร, ACNE, SPRAY, BABY (หน้า 16–30)",
                    "รายการคำห้ามใช้เป็นชื่อหรือส่วนหนึ่งของชื่อ %d คำ (หน้า 31–40)" % len(db.get("words", [])),
                    "แนวทางสารที่ไม่มีชื่อในประกาศ เช่น ALCOHOL (ไม่เกิน 69.4% ยกเว้นบางประเภท), น้ำมันไพล, UREA (<10%), METHYL SALICYLATE (หน้า 41–46)",
                    "วัตถุกันเสียบางตัวที่เป็นวัตถุที่อาจใช้ด้วย, ผลิตภัณฑ์ความเสี่ยงสูง, กัญชา/กัญชง (สารบัญระบุว่าส่วนนี้ยกเลิก ให้ใช้ฉบับ มิ.ย. 2565), Nano, Anti-Bacterial, น้ำยาบ้วนปาก, ผลิตภัณฑ์ที่ต้องแนบฉลาก (หน้า 47–83)"],
         "page": 3},
    ]


def core_pct(s):
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", s or "")
    return float(m.group(1)) if m else None


def _reg_path(fn):
    for d in (os.path.join(core.USER_DIR, "regs"), os.path.join(core.DATA, "regs")):
        p = os.path.join(d, fn)
        if os.path.exists(p):
            return p
    return None


def _src_short(fn):
    return {"banned-2566-update-2568.pdf": "บัญชีวัตถุห้ามใช้", "restricted-2561-amend1-9.pdf": "บัญชีวัตถุที่อาจใช้",
            "forbidden-words.pdf": "หลักเกณฑ์/คำห้ามใช้"}.get(fn, fn)
