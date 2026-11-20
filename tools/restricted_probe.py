import sys, re, pymupdf
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, "tools")
from build_db import fix_thai, clean
d = pymupdf.open("data/regs/restricted-2561-amend1-9.pdf")
for pn in [int(x) - 1 for x in sys.argv[1:]]:
    pg = d[pn]; pg.remove_rotation()
    tabs = pg.find_tables().tables
    print("## page", pn + 1, "tables", len(tabs))
    for t in tabs:
        print(" cols", t.col_count, "rows", t.row_count, [round(v) for v in t.bbox])
        for r in t.rows[:6]:
            cells = []
            for c in r.cells:
                cells.append(fix_thai(clean(pg.get_text("text", clip=c))) [:40] if c else None)
            print("  ", cells)
