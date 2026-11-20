"""สร้าง icon.ico (ขวดทดลอง สีฟ้าบนพื้นมืด)"""
import os
from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
S = 512
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle([16, 16, S - 16, S - 16], radius=110, fill=(15, 17, 21, 255), outline=(38, 43, 53, 255), width=10)
c = (56, 189, 248, 255)
w = 30
# คอขวด + ตัวขวด
d.line([(200, 110), (312, 110)], fill=c, width=w)
d.line([(220, 110), (220, 220)], fill=c, width=w)
d.line([(292, 110), (292, 220)], fill=c, width=w)
d.line([(220, 220), (128, 382)], fill=c, width=w)
d.line([(292, 220), (384, 382)], fill=c, width=w)
d.rounded_rectangle([118, 366, 394, 410], radius=20, fill=c)
d.polygon([(178, 300), (334, 300), (376, 378), (136, 378)], fill=(56, 189, 248, 110))
# เครื่องหมายถูก
d.line([(300, 330), (340, 372), (430, 262)], fill=(74, 222, 128, 255), width=34)
img.save(os.path.join(ROOT, "icon.ico"), sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
img.save(os.path.join(ROOT, "icon.png"))
print("icon ok")
