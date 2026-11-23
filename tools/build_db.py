"""สร้างฐานข้อมูล data/regdb.json จากเอกสาร อย. ใน data/regs/
รัน: python tools/build_db.py"""
import datetime, json, os, re, sys, unicodedata
import pymupdf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REGS = os.path.join(ROOT, "data", "regs")
QUOTES = "“”\"'"
CAS_RE = re.compile(r"\b\d{2,7}-\d{2}-\d\b")
TH = "ก-ฮ"
GARBLE = "@Ö×Üß$èðîê"
BROKEN = {"on": False}   # หน้าที่ฟอนต์ไทยเพี้ยน ("ส ำอำง")


def set_page(pg):
    BROKEN["on"] = bool(re.search(r"[%s] [ำา]|[%s]ำ[%s]ำ" % (TH, TH, TH), pg.get_text()))


def fix_thai(s):
    if not BROKEN["on"]:
        return s
    s = re.sub(r"([%s][่-๋]?)ำ(?=[%s])" % (TH, TH), r"\1า", s)  # กำร -> การ
    s = re.sub(r"([%s][่-๋]?)\s+[ำา]" % TH, r"\1ำ", s)          # ส ำ -> สำ
    return s


def garbled(s):
    return sum(s.count(c) for c in GARBLE) >= 3


def strip_garble(s):
    """ตัดข้อความไทยที่ฟอนต์เพี้ยน (อ่านไม่ได้) ออก"""
    toks = s.split(" ")
    for i, t in enumerate(toks):
        if any(c in t for c in GARBLE):
            head = " ".join(toks[:i]).rstrip()
            return (head + " " if head else "") + "[ข้อความภาษาไทยในเอกสารอ่านไม่ได้ — ดูหน้าเอกสาร]"
    return s


def clean(s):
    s = unicodedata.normalize("NFC", s.replace(" ", " ").replace("", "").replace("", ""))
    return re.sub(r"\s+", " ", s).strip()


def page_lines(pg):
    out = []
    for b in pg.get_text("dict")["blocks"]:
        for l in b.get("lines", []):
            t = "".join(s["text"] for s in l["spans"])
            if t.strip():
                out.append((l["bbox"], t))
    out.sort(key=lambda x: (round(x[0][1]), x[0][0]))
    return out


SKIP = re.compile(r"ราชกิจจา|^หน[้]?า\s|^เล[่]?ม\s|ตอนพิเศษ|Powered by|^-\s*\d+\s*-$|^CAS\s*Number|^ล\s*[ำา]\s*ดับ|^ลํ|วัตถุที่ห้ามใช้เป็นส่วนผสม")


def parse_banned(fn, label):
    d = pymupdf.open(os.path.join(REGS, fn))
    items, cur = [], None
    for pi, pg in enumerate(d):
        set_page(pg)
        W = pg.rect.width
        lines = page_lines(pg)
        if not any(re.match(r"\s*CAS\s*Number", t) for _, t in lines):
            continue
        start = min(bb[1] for bb, t in lines if re.match(r"\s*CAS\s*Number", t))
        left = min((bb[0] for bb, t in lines if bb[1] > start + 4 and bb[0] < W * 0.22
                    and re.match(r"^\d{1,4}\.?(\s|$)", t.strip().lstrip(QUOTES))), default=0)
        for bb, t in lines:
            if bb[1] <= start + 4:
                continue
            ts = t.strip().lstrip(QUOTES).strip()
            if garbled(ts) or re.match(r"^×Oอ|^ข้อ\s", ts):
                cur = None
                continue
            if SKIP.search(ts):
                continue
            m = re.match(r"^(\d{1,4})\.?\s*(.*)$", ts)
            if m and bb[0] <= left + 6 and not m.group(2).startswith(("-", ")", "]", ",")):
                cur = {"no": m.group(1), "name": m.group(2), "cas": "", "src": label, "file": fn,
                       "page": pi + 1, "rects": [list(bb)]}
                items.append(cur)
                continue
            if cur is None:
                continue
            if cur["page"] != pi + 1 and bb[0] < W * 0.22:
                continue
            if bb[0] > W * 0.74:
                cur["cas"] += " " + ts
            else:
                cur["name"] += " " + ts
            if cur["page"] == pi + 1:
                cur["rects"].append(list(bb))
    out = []
    for it in items:
        name = strip_garble(clean(fix_thai(it["name"]))).rstrip("”\"")
        if not name:
            continue
        r = it["rects"]
        rect = [min(x[0] for x in r), min(x[1] for x in r), max(x[2] for x in r), max(x[3] for x in r)]
        out.append({"no": it["no"], "name": name, "cas": sorted(set(CAS_RE.findall(it["cas"] + " " + name))),
                    "src": it["src"], "file": it["file"], "page": it["page"], "rect": [round(v, 1) for v in rect]})
    return out


THNUM = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")


def cell_text(pg, rect):
    return strip_garble(clean(fix_thai(pg.get_text("text", clip=rect)))) if rect else ""


def parse_restricted(fn):
    d = pymupdf.open(os.path.join(REGS, fn))
    entries, cur, cols, amend = [], None, None, "ประกาศ พ.ศ. 2561 (ฉบับหลัก)"
    for pi, pg in enumerate(d):
        pg.remove_rotation()
        set_page(pg)
        txt = re.sub(r"\s+", " ", fix_thai(pg.get_text().translate(THNUM)))
        m = re.search(r"\(ฉบับที่ (\d+)\)", txt)
        if m:
            amend = "ประกาศแก้ไข (ฉบับที่ %s)" % m.group(1)
        for tab in pg.find_tables().tables:
            rows = tab.rows
            hdr, hdr_rows = {}, set()
            for ri, r in enumerate(rows[:2]):
                for c in r.cells:
                    if not c:
                        continue
                    t = clean(pg.get_text("text", clip=c))
                    if len(t) > 70:
                        continue
                    key = None
                    if re.search(r"Chemical", t, re.I): key = "chem"
                    elif re.search(r"Glossary", t, re.I): key = "inci"
                    elif re.search(r"^CAS", t): key = "cas"
                    elif re.search(r"บร.?เวณ|ร>Nว", t): key = "area"
                    elif re.search(r"เข้มข้น|N×้ม|ข้ม", t): key = "max"
                    elif re.search(r"เงื่อนไข|NÜ?่อî|อนไข", t): key = "cond"
                    elif re.search(r"ดับ|ลํ@", t) and c[0] < tab.bbox[0] + 40: key = "no"
                    elif re.search(r"ชื่อวัตถุ|ช\?่อว", t): hdr_rows.add(ri)
                    if key:
                        hdr_rows.add(ri)
                        if key not in hdr:
                            hdr[key] = (c[0], c[2])
            if len(hdr) >= 5:
                cols = dict(hdr)
                if "no" not in cols:
                    cols["no"] = (tab.bbox[0], min(v[0] for v in cols.values()))
                body = [r for ri, r in enumerate(rows) if ri not in hdr_rows]
            elif cols:
                body = rows
            else:
                continue

            def col_of(c):
                xc = (c[0] + c[2]) / 2
                best, bd = None, 1e9
                for k, (a, b) in cols.items():
                    dist = 0 if a <= xc <= b else min(abs(xc - a), abs(xc - b))
                    if dist < bd:
                        best, bd = k, dist
                return best

            for r in body:
                vals, raw = {}, []
                for c in r.cells:
                    if not c:
                        continue
                    t = cell_text(pg, c)
                    if not t:
                        continue
                    k = col_of(c)
                    vals[k] = (vals.get(k, "") + " " + t).strip()
                    if k in ("chem", "inci"):
                        raw += [clean(fix_thai(x)) for x in pg.get_text("text", clip=c).split("\n") if x.strip()]
                if not vals:
                    continue
                no = vals.get("no", "").strip().strip("“”\"").rstrip(".")
                if no and re.match(r"^\d{1,3}[a-z]?$", no):
                    cur = {"no": no, "chem": "", "inci": "", "cas": "", "uses": [], "lines": [],
                           "src": amend, "file": fn, "page": pi + 1, "rect": list(r.bbox)}
                    entries.append(cur)
                elif cur is None:
                    continue
                elif cur["page"] == pi + 1:
                    rb = r.bbox
                    cur["rect"] = [min(cur["rect"][0], rb[0]), min(cur["rect"][1], rb[1]),
                                   max(cur["rect"][2], rb[2]), max(cur["rect"][3], rb[3])]
                cur["lines"] += [x for x in raw if not garbled(x)]
                for k in ("chem", "inci", "cas"):
                    if vals.get(k):
                        cur[k] = (cur[k] + " " + vals[k]).strip()
                if any(vals.get(k) for k in ("area", "max", "cond")):
                    cur["uses"].append({k: vals.get(k, "") for k in ("area", "max", "cond")})
    out = []
    for e in entries:
        if not (e["chem"] or e["inci"]):
            continue
        last = {}
        for u in e["uses"]:  # ช่องผสาน: ว่าง = เหมือนแถวก่อนหน้า
            for k in ("area", "max", "cond"):
                if u[k]:
                    last[k] = u[k]
                else:
                    u[k] = last.get(k, "")
        e["cas"] = sorted(set(CAS_RE.findall(e["cas"])))
        e["rect"] = [round(v, 1) for v in e["rect"]]
        # ชื่อที่ใช้จับคู่: แต่ละบรรทัดในช่องชื่อ + บรรทัดที่ต่อกัน (ชื่อยาวถูกตัดบรรทัด)
        ls = e.pop("lines")
        names = set(ls)
        for a, b in zip(ls, ls[1:]):
            names.add((a + b) if a.endswith("-") else (a + " " + b))
        e["names"] = sorted(n for n in names if len(n) > 2 and n != "-")
        out.append(e)
    return out


def parse_words(fn):
    d = pymupdf.open(os.path.join(REGS, fn))
    words = []
    for pi, pg in enumerate(d):
        set_page(pg)
        lines = page_lines(pg)
        head = " ".join(fix_thai(t) for _, t in lines[:12])
        if not re.search(r"ห\s*้\s*[ำา]มใช้|คำห้ามใช้|ค\s*[ำา]\s*/\s*ชื่อ", head):
            continue
        for bb, t in lines:
            t = t.strip()
            if not re.fullmatch(r"\d{1,3}", t):
                continue
            cands = [(nb[0] - bb[2], nb, nt) for nb, nt in lines
                     if abs((nb[1] + nb[3]) / 2 - (bb[1] + bb[3]) / 2) < 5 and nb[0] > bb[2] - 1
                     and not re.fullmatch(r"[\d ]+", nt.strip())]
            if not cands:
                continue
            _, nb, nt = min(cands, key=lambda c: c[0])
            if nb[0] - bb[2] > 160:
                continue
            w = clean(fix_thai(nt))
            words.append({"no": int(t), "word": w, "file": fn, "page": pi + 1,
                          "rect": [round(bb[0], 1), round(min(bb[1], nb[1]), 1),
                                   round(nb[2], 1), round(max(bb[3], nb[3]), 1)]})
    seen, out = set(), []
    for w in words:
        if w["no"] not in seen:
            seen.add(w["no"])
            out.append(w)
    return out


def parse_mentions(fn, skip_pages=()):
    """ชื่อสาร (ตัวพิมพ์ใหญ่) ที่ถูกกล่าวถึงในหลักเกณฑ์ อย. พร้อมหน้าและบรรทัด"""
    d = pymupdf.open(os.path.join(REGS, fn))
    out = []
    for pi, pg in enumerate(d):
        if pi + 1 in skip_pages:
            continue
        set_page(pg)
        for bb, t in page_lines(pg):
            for m in re.finditer(r"[A-Z0-9][A-Z0-9 ,\-/().'\[\]]{2,}[A-Z0-9)]", t):
                # ตัดเลขลำดับหน้าชื่อ เช่น "43. UREA", "(7) ..."
                name = re.sub(r"^\(?\d{1,3}[.)]\s*", "", m.group(0)).strip(" ,-/(")
                if len(re.sub(r"[^A-Z]", "", name)) < 4:
                    continue
                out.append({"text": name, "page": pi + 1, "line": clean(fix_thai(t)),
                            "rect": [round(v, 1) for v in bb]})
    return out


def build():
    db = {"version": datetime.date.today().isoformat()}
    db["banned"] = parse_banned("banned-2566-update-2568.pdf", "บัญชีวัตถุห้ามใช้ พ.ศ. 2566 (ปรับปรุงถึง ม.ค. 2568)")
    last = {}
    for i, b in enumerate(db["banned"]):  # ลำดับเดิมซ้ำ = ประกาศแก้ไขภายหลังใช้แทน
        if b["no"] in last:
            db["banned"][last[b["no"]]]["superseded"] = True
        last[b["no"]] = i
    db["restricted"] = parse_restricted("restricted-2561-amend1-9.pdf")
    db["words"] = parse_words("forbidden-words.pdf")
    db["mentions"] = parse_mentions("forbidden-words.pdf", {w["page"] for w in db["words"]})
    db["sources"] = {
        "banned-2566-update-2568.pdf": "ประกาศกระทรวงสาธารณสุข เรื่อง ชื่อวัตถุที่ห้ามใช้เป็นส่วนผสมในการผลิตเครื่องสำอาง พ.ศ. 2566 (รวมฉบับแก้ไขถึง 31 ม.ค. 2568)",
        "restricted-2561-amend1-9.pdf": "ประกาศกระทรวงสาธารณสุข เรื่อง กำหนดชื่อ ปริมาณ และเงื่อนไขของวัตถุที่อาจใช้เป็นส่วนผสมในการผลิตเครื่องสำอาง พ.ศ. 2561 (รวมฉบับแก้ไขที่ 1–9)",
        "forbidden-words.pdf": "แนวทางการพิจารณารับจดแจ้ง / คำห้ามใช้ในชื่อเครื่องสำอาง (ฉบับปรับปรุง 2 ธ.ค. 2564)",
    }
    return db


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    db = build()
    print("banned", len(db["banned"]), "restricted", len(db["restricted"]),
          "words", len(db["words"]), "mentions", len(db["mentions"]))
    with open(os.path.join(ROOT, "data", "regdb.json"), "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, separators=(",", ":"))
