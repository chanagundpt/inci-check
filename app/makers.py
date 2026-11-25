# -*- coding: utf-8 -*-
"""อ่านชื่อผู้ผลิต / โรงงาน / ผู้จัดจำหน่าย และที่อยู่ จากฉลาก สูตร MSDS (อังกฤษ ญี่ปุ่น จีน เกาหลี ไทย) + แปลเป็นอังกฤษ"""
import json, os, re, threading, time, unicodedata

import core

ROLE_TH = {"mfr": "ผู้ผลิต", "factory": "โรงงานผลิต", "dist": "ผู้จัดจำหน่าย / เจ้าของแบรนด์",
           "applicant": "บริษัทที่ระบุในเอกสาร",
           "company": "บริษัทบนฉลาก (ไม่ได้ระบุว่าเป็นผู้ผลิตหรือผู้จัดจำหน่าย)"}

# (role, คำนำหน้า) — ภาษาละติน
LATIN = [
    ("mfr", r"manufacturer(?:'s)?(?:\s+name)?|manufactured\s+by|manufacturing\s+company|produced\s+by|made\s+by|producer|mfr\.?|mfg\.?\s+by"),
    ("factory", r"factory(?:\s+name)?|manufacturing\s+(?:site|plant)|production\s+site|plant"),
    ("dist", r"distributor|distributed\s+by|brand\s+owner|marketed\s+by|importer|imported\s+by|sold\s+by"),
    ("applicant", r"applicant|supplier(?:\s+name)?|company\s+name|company"),
    ("addr", r"(?:factory\s+|office\s+|company\s+)?address|add\.?"),
]
# ญี่ปุ่น / จีน / เกาหลี / ไทย (คำยาวก่อน)
CJK = [
    ("mfr", ["製造販売元", "製造販売業者", "製造業者", "製造元", "製造者", "製造所",
             "受托生产企业", "被委托生产企业", "受托企业", "被委托方", "受托方", "生产企业", "生产厂家", "生产商", "生产厂", "生产者", "制造商", "制造者",
             "화장품제조업자", "제조업자", "제조업체", "제조원", "제조사", "제조자",
             "ผลิตโดย", "ผู้ผลิต"]),
    ("factory", ["製造工場", "工場名", "工場", "生产工厂", "工厂", "공장", "โรงงานผลิต", "โรงงาน"]),
    ("dist", ["発売元", "販売元", "販売業者", "輸入元", "輸入者",
              "委托生产企业", "委托企业", "委托方", "经销商", "总经销", "出品商", "出品方", "出品", "注册人", "备案人", "进口商",
              "화장품책임판매업자", "책임판매업자", "제조판매업자", "판매업자", "판매원", "수입원", "수입자",
              "จัดจำหน่ายโดย", "ผู้จัดจำหน่าย", "นำเข้าโดย", "ผู้นำเข้า"]),
    ("applicant", ["供应商", "申请单位", "申请人", "企业名称", "公司名称", "会社名", "회사명", "ชื่อบริษัท"]),
    ("addr", ["生产地址", "企业地址", "公司地址", "注册地址", "详细地址", "地址", "住址", "所在地", "住所",
              "사업장소재지", "소재지", "주소", "ที่อยู่"]),
]
_cjk_role = {w: r for r, ws in CJK for w in ws}
_cjk_alt = "|".join(sorted((re.escape(w) for w in _cjk_role), key=len, reverse=True))
BUL = r"^[\s\-–—•·*■●◆◇▶▷※\[\(【《「]*"
CJK_RE = re.compile(BUL + r"(?P<lab>" + _cjk_alt + r")\s*(?:名称|名|명)?\s*[\]\)】》」]*\s*(?P<sep>[:：/／|]|\s|$)\s*(?P<val>.*)$")
LATIN_RE = [(r, re.compile(BUL + r"(?P<lab>" + p + r")\s*[\]\)】]*\s*(?P<sep>[:：]|$)\s*(?P<val>.*)$", re.I)) for r, p in LATIN]
BY_RE = re.compile(r"\bby$", re.I)

ADDR_HINT = re.compile(
    r"〒|\d.*(?:都|道|府|県|市|区|町|村|丁目|番地|省|路|号|镇|鎮|街|巷|栋|楼|시|군|구|동|로|길|번지|읍|면)"
    r"|(?:都|道|府|県|省|市|시|도)\S*(?:市|区|町|村|县|縣|구|군|동|로).*\d"
    r"|\d.*\b(?:road|rd\.?|street|st\.|avenue|ave\.?|district|city|province|prefecture|zone|building|bldg|floor|room|no\.|park|town|village)\b"
    r"|\b\w+-(?:ku|shi|gu|si|dong|ro|gil|do|cho|machi)\b"
    r"|\b(?:japan|korea|china|thailand|france|taiwan|hong\s*kong|p\.?\s*r\.?\s*c)\b.*\d|\d.*\b(?:japan|korea|china|thailand|france|taiwan)\b",
    re.I)
ADDR_WORDS = re.compile(r"\b(?:road|rd|street|avenue|district|city|province|prefecture|zone|building|bldg|floor|room|park|town|village|county|industrial|"
                        r"japan|korea|china|thailand|france|taiwan|tokyo|osaka|seoul|guangzhou|guangdong|shanghai|zhejiang|fujian|gyeonggi)\b", re.I)
CJK_COMPANY = re.compile(r"株式会社|有限会社|合同会社|㈱|\(株\)|有限公司|股份|公司|集团|\(주\)|㈜|주식회사|유한회사|工場|工厂|공장|บริษัท|หจก|ห้างหุ้นส่วน")


def _addr_like(t):
    return bool(ADDR_HINT.search(t)) or len(set(w.lower() for w in ADDR_WORDS.findall(t))) >= 2


def _fix_ocr_addr(a):
    """ที่อยู่ภาษาอังกฤษจาก OCR ที่ช่องว่างหาย: "Room1616,No.395" → "Room 1616, No.395" """
    if not _is_latin(a):
        return a
    a = re.sub(r",(?=\S)", ", ", a)
    a = re.sub(r"(?<=[a-z]{3})(?=\d)", " ", a)
    a = re.sub(r"(?<=[a-z])\.(?=[A-Z])", ". ", a)
    return core.respace(a)


COMPANY_HINT = re.compile(r"\b(?:co\.?|ltd\.?|inc\.?|corp\.?|corporation|company|limited|llc|gmbh|s\.?a\.?|plant|laborator(?:y|ies)|industr(?:y|ies|ial)|cosmetics?)\b", re.I)
STOP_RE = re.compile(r"^\s*(?:tel|fax|phone|e-?mail|website|url|http|www\.|電話|电话|传真|お客様|お問い?合わせ|전화|고객|โทร|เว็บไซต์|"
                     r"country|product|brand|date|no\.?\s|ingredients?|inci|section\s|emergency|page\s)", re.I)


def _label(text):
    """ถ้าบรรทัดขึ้นต้นด้วยคำนำหน้า (ผู้ผลิต/ที่อยู่/…) คืน (role, value) ไม่งั้น None"""
    t = unicodedata.normalize("NFKC", text or "").strip()
    if not t or len(t) > 220:
        return None
    m = CJK_RE.match(t)
    if m:
        val = m.group("val").strip(" :：")
        # "製造販売元コスメ…" (ไม่มีตัวคั่น) ก็รับ แต่คำสั้นอย่าง "出品"/"地址" ต้องมีตัวคั่นหรือจบบรรทัด
        return _cjk_role[m.group("lab")], val
    for role, rx in LATIN_RE:
        m = rx.match(t)
        if not m:
            continue
        lab, sep, val = m.group("lab"), m.group("sep"), m.group("val").strip()
        if not sep and not BY_RE.search(lab):
            continue
        if role == "addr" and lab.lower().startswith("add") and not sep:
            continue
        return role, val
    # "Manufactured by XYZ Co., Ltd." (ไม่มี :)
    m = re.match(BUL + r"(manufactured|produced|made|distributed|imported|marketed|sold)\s+by\s*[:：]?\s*(?P<val>.+)$", t, re.I)
    if m:
        return ("mfr" if m.group(1).lower() in ("manufactured", "produced", "made") else "dist"), m.group("val").strip()
    return None


def _lines(doc):
    """บรรทัดตามลำดับการอ่าน: [{text, marks, page}]"""
    out = []
    if doc.kind == "table":
        for ri, row in enumerate(doc.table):
            for ci, v in enumerate(row):
                v = (v or "").strip()
                if v:
                    for part in re.split(r"[\r\n]+", v):
                        if part.strip():
                            out.append({"text": part.strip(), "marks": [core.mark(None, cell=[ri, ci])], "page": -1, "row": ri})
        return out
    by_page = {}
    for l in doc.lines:
        if l.get("box") and (l.get("text") or "").strip():
            by_page.setdefault(l.get("page", 0), []).append(l)
    for pi in sorted(by_page):
        ls = by_page[pi]
        if pi in doc.ocr_pages:
            ls = core.merge_fragments(ls)
        hs = sorted(l["box"][3] - l["box"][1] for l in ls)
        h = hs[len(hs) // 2] or 10
        # แถวเดียวกัน (กึ่งกลางแนวตั้งใกล้กัน) เรียงซ้าย→ขวา
        ls = sorted(ls, key=lambda l: (l["box"][1] + l["box"][3]) / 2)
        rows = []
        for l in ls:
            cy = (l["box"][1] + l["box"][3]) / 2
            if rows and abs(cy - rows[-1][0]) <= h * 0.5:
                rows[-1][1].append(l)
            else:
                rows.append([cy, [l]])
        for ri, (_, fs) in enumerate(rows):
            for l in sorted(fs, key=lambda l: l["box"][0]):
                t = l["text"].strip()
                if core.WATERMARK.match(t) and len(t) <= 4:
                    continue
                out.append({"text": t, "marks": [core.mark(pi, l["box"])], "page": pi, "row": (pi, ri), "box": l["box"]})
    return out


def _lang(s, doc_ja=False):
    if re.search(r"[぀-ヿ]", s):
        return "ja"
    if re.search(r"[가-힯]", s):
        return "ko"
    if re.search(r"[一-鿿]", s):
        return "ja" if doc_ja or re.search(r"[都道府県]|丁目|番地|株式会社|有限会社", s) else "zh"
    if re.search(r"[฀-๿]", s):
        return "th"
    return "en"


def _clean(s):
    s = unicodedata.normalize("NFKC", s or "")
    return re.sub(r"\s+", " ", s).strip(" :：,、;；")


def _is_latin(s):
    return bool(re.search(r"[A-Za-z]{3}", s)) and not re.search(r"[぀-ヿ一-鿿가-힯฀-๿]", s)


def _below(L, ni, skip, mk, name, addr_lines, used=None):
    """ฉลากหลายคอลัมน์: บรรทัดถัดไปตามลำดับอ่านอาจเป็นของคอลัมน์อื่น → หาชื่ออังกฤษ/ที่อยู่จากตำแหน่งจริง (บรรทัดใต้ชื่อโดยตรง)"""
    nl = L[ni]
    if mk["address"] or not nl.get("box"):
        return addr_lines
    nb = nl["box"]
    nh = max(1e-3, nb[3] - nb[1])
    below = []
    for q, c in enumerate(L):
        cb = c.get("box")
        if not cb or c["page"] != nl["page"] or q in skip:
            continue
        gap = cb[1] - nb[3]
        ov = min(nb[2], cb[2]) - max(nb[0], cb[0])
        if -0.3 * nh <= gap <= 3.2 * nh and ov > 0.25 * min(nb[2] - nb[0], cb[2] - cb[0]):
            below.append((gap, q))
    last_y = nb[3]
    for gap, q in sorted(below)[:4]:
        c = L[q]
        tc = _clean(c["text"])
        if _label(c["text"]) or STOP_RE.match(c["text"]):
            break
        if not mk["name_en"] and not _is_latin(name) and _is_latin(tc) and COMPANY_HINT.search(tc) and len(tc) < 90 and not mk["address"]:
            mk["name_en"] = tc
            mk["marks"] += c["marks"]
            last_y = c["box"][3]
            if used is not None:
                used.add(q)
            continue
        if _addr_like(tc) and len(tc) < 160 and c["box"][1] - last_y <= 1.8 * nh and addr_lines < 2:
            mk["address"] = (mk["address"] + " " + tc).strip()
            mk["addr_marks"] += c["marks"]
            addr_lines += 1
            last_y = c["box"][3]
            if used is not None:
                used.add(q)
            continue
        if mk["address"]:
            break
    return addr_lines


def extract_makers(doc):
    L = _lines(doc)
    doc_ja = any(re.search(r"[぀-ヿ]", l["text"]) for l in L)
    out, seen, used = [], {}, set()
    i = 0
    while i < len(L):
        lab = _label(L[i]["text"])
        if not lab or lab[0] == "addr":
            i += 1
            continue
        role, val = lab
        marks, j = list(L[i]["marks"]), i + 1
        if not val:
            # ค่าที่อยู่บรรทัด/ช่องถัดไป
            # ค่าอยู่คนละบรรทัดกับคำนำหน้า → ต้องดูเป็นชื่อบริษัทจริง (กันหัวตาราง เช่น "제조사 / MANUFACTURER")
            if j < len(L) and not _label(L[j]["text"]) and not STOP_RE.match(L[j]["text"]) and (
                    COMPANY_HINT.search(L[j]["text"]) or CJK_COMPANY.search(L[j]["text"])):
                val = L[j]["text"]
                marks = list(L[j]["marks"])
                j += 1
            else:
                i += 1
                continue
        name = _clean(val)
        latin_lab = bool(re.match(BUL + r"[A-Za-z]", L[i]["text"]))
        if (not re.search(r"[^\W\d_]{2}", name) or len(name) > 140 or len(name.split()) > 16
                or (latin_lab and role in ("applicant", "factory") and not COMPANY_HINT.search(name) and len(name.split()) > 6)
                or re.match(r"^(name|名称|名|n/?a|-+|none)$", name, re.I)):
            i += 1
            continue
        mk = {"role": role, "role_th": ROLE_TH[role], "name": name, "name_en": "", "address": "", "marks": marks,
              "addr_marks": [], "label": _clean(L[i]["text"])[:40] if not lab[1] else _clean(L[i]["text"].replace(val, ""))[:40]}
        addr_lines = 0
        k = j
        while k < len(L) and k < j + 6:
            t = L[k]["text"]
            lb = _label(t)
            if lb and lb[0] == "addr" and not mk["address"]:
                a = lb[1]
                k += 1
                am = list(L[k - 1]["marks"])
                if not a and k < len(L) and not _label(L[k]["text"]):
                    a, am = L[k]["text"], list(L[k]["marks"])
                    k += 1
                mk["address"], mk["addr_marks"] = _clean(a), am
                addr_lines = 1
                continue
            if lb or STOP_RE.match(t):
                break
            tc = _clean(t)
            if not mk["name_en"] and not _is_latin(name) and _is_latin(tc) and COMPANY_HINT.search(tc) and len(tc) < 90 and not mk["address"]:
                mk["name_en"] = tc
                mk["marks"] += L[k]["marks"]
                k += 1
                continue
            if _addr_like(tc) and addr_lines < 3 and len(tc) < 160:
                mk["address"] = (mk["address"] + " " + tc).strip() if mk["address"] else tc
                mk["addr_marks"] += L[k]["marks"]
                addr_lines += 1
                k += 1
                continue
            # ที่อยู่ภาษาอังกฤษขึ้นบรรทัดใหม่ (ต่อจากบรรทัดที่อยู่ก่อนหน้า ลงท้ายด้วย , )
            if mk["address"] and addr_lines < 3 and mk["address"].endswith(",") is False and re.search(r",\s*$", L[k - 1]["text"]) and len(tc) < 120:
                mk["address"] += " " + tc
                mk["addr_marks"] += L[k]["marks"]
                addr_lines += 1
                k += 1
                continue
            break
        addr_lines = _below(L, j - 1, {i, j - 1}, mk, name, addr_lines, used)
        # ไม่มีที่อยู่ต่อท้าย → ดูหัวกระดาษ 4 บรรทัดก่อนหน้า (ติดป้ายให้ตรวจ)
        if not mk["address"] and role in ("mfr", "factory"):
            for b in range(i - 1, max(-1, i - 5), -1):
                tb = _clean(L[b]["text"])
                if _label(L[b]["text"]):
                    break
                if _addr_like(tb) and len(tb) < 160 and re.search(r"\d", tb):
                    mk["address"], mk["addr_marks"], mk["addr_guess"] = tb, list(L[b]["marks"]), True
                    break
        if mk["address"] and L[i]["page"] in getattr(doc, "ocr_pages", []):
            mk["address"] = _fix_ocr_addr(mk["address"])
        key = (role, core.norm_latin(name) or name)
        prev = seen.get(key)
        if prev is None:
            seen[key] = mk
            out.append(mk)
        elif mk["address"].count(" ") > prev["address"].count(" ") or (mk["name_en"] and not prev["name_en"]):
            # เจอซ้ำ (เช่น หน้าปก + หน้าใน) → ใช้ฉบับที่อ่านได้ครบกว่า
            prev.update({k2: mk[k2] for k2 in ("name", "name_en", "address", "marks", "addr_marks")})
            prev["addr_guess"] = mk.get("addr_guess", False)
        used.update(range(i, max(k, i + 1)))
        i = max(k, i + 1)
    # ชื่อบริษัทที่ไม่มีคำนำหน้า (เช่น ฉลากญี่ปุ่นเขียน "株式会社○○" แล้วตามด้วยที่อยู่) — รับเฉพาะเมื่อมีที่อยู่อยู่ใต้ชื่อ
    for q, l in enumerate(L):
        if q in used or not l.get("box"):
            continue
        t = _clean(l["text"])
        if len(t) > 40 or _label(l["text"]) or STOP_RE.match(l["text"]) or _addr_like(t):
            continue
        if not (re.match(r"^.{0,22}(?:株式会社|有限会社|合同会社|有限公司|股份有限公司|주식회사|\(주\)|㈜|㈱).{0,22}$", t)
                or (_is_latin(t) and re.search(r"(?:co\.?,?\s*ltd|ltd|inc|corp|corporation|limited)\.?$", t, re.I))):
            continue
        mk = {"role": "company", "role_th": ROLE_TH["company"], "name": t, "name_en": "", "address": "", "marks": list(l["marks"]),
              "addr_marks": [], "label": ""}
        loc = set()
        _below(L, q, {q}, mk, t, 0, loc)
        if not mk["address"]:
            continue
        if l["page"] in getattr(doc, "ocr_pages", []):
            mk["address"] = _fix_ocr_addr(mk["address"])
        key = core.norm_latin(t) or t
        if any((core.norm_latin(m["name"]) or m["name"]) == key or (m["name_en"] and core.norm_latin(m["name_en"]) == core.norm_latin(t)) for m in out):
            continue
        used |= loc | {q}
        out.append(mk)
    for mk in out:
        mk["lang"] = _lang(mk["name"] + " " + mk["address"], doc_ja)
        mk["need_tr"] = ([mk["name"]] if not _is_latin(mk["name"]) and not mk["name_en"] else []) + \
                        ([mk["address"]] if mk["address"] and not _is_latin(mk["address"]) else [])
    order = {"mfr": 0, "factory": 1, "dist": 2, "applicant": 3, "company": 4}
    out.sort(key=lambda m: order[m["role"]])
    return out


# ---------------------------------------------------------------- แปลเป็นอังกฤษ (ออนไลน์ ฟรี) + แคช
class Translator:
    def __init__(self):
        self.path = os.path.join(core.USER_DIR, "translate_cache.json")
        try:
            with open(self.path, encoding="utf-8") as f:
                self.cache = json.load(f)
        except Exception:
            self.cache = {}
        self.lock = threading.Lock()

    def _get(self, url, timeout=15):
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (INCI checker)"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())

    def _mymemory(self, text, sl):
        import urllib.parse
        pair = {"ja": "ja", "ko": "ko", "zh": "zh-CN", "th": "th"}.get(sl, sl) + "|en"
        r = self._get("https://api.mymemory.translated.net/get?" + urllib.parse.urlencode({"q": text, "langpair": pair}))
        t = (r.get("responseData") or {}).get("translatedText") or ""
        if not t or r.get("responseStatus") not in (200, "200") or re.search(r"MYMEMORY WARNING|QUERY LENGTH LIMIT|INVALID", t):
            raise ValueError("mymemory: " + str(r.get("responseStatus")))
        return t

    def _google(self, text, sl):
        import urllib.parse
        r = self._get("https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=en&dt=t&q=" + urllib.parse.quote(text))
        return "".join(x[0] for x in r[0] if x and x[0])

    def one(self, text, sl):
        text = (text or "").strip()
        if not text:
            return {"en": "", "by": ""}
        key = "v2|" + sl + "|" + text
        with self.lock:
            if key in self.cache:
                return self.cache[key]
        res = None
        for by, fn in (("Google Translate", self._google), ("MyMemory", self._mymemory)):
            try:
                en = re.sub(r"\s+", " ", fn(text, sl)).strip()
                if en and en != text:
                    res = {"en": en, "by": by}
                    break
            except Exception:
                continue
        if res is None:
            return {"en": "", "by": "", "error": "แปลไม่ได้ (ออฟไลน์ หรือบริการแปลไม่ตอบ)"}
        with self.lock:
            self.cache[key] = res
            try:
                with open(self.path, "w", encoding="utf-8") as f:
                    json.dump(self.cache, f, ensure_ascii=False)
            except Exception:
                pass
        return res

    def many(self, items):
        """items = [{text, lang}] → [{en, by}]"""
        return [self.one(it.get("text", ""), it.get("lang", "auto")) for it in items or []]
