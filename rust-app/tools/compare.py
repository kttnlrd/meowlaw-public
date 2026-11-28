# Puts the web reference and the native window side by side, and reports how far apart they are.
#   py compare.py <reference.png> <native.png> <out.png>
# The output is reference | native | difference (brighter = further apart).
import sys
from PIL import Image, ImageChops

ref, nat, out = sys.argv[1:4]
a = Image.open(ref).convert("RGB")
b = Image.open(nat).convert("RGB")
w, h = min(a.width, b.width), min(a.height, b.height)
a, b = a.crop((0, 0, w, h)), b.crop((0, 0, w, h))
diff = ImageChops.difference(a, b)
grey = diff.convert("L")
hist = grey.histogram()
total = w * h
far = sum(hist[48:])
mean = sum(i * n for i, n in enumerate(hist)) / total
sheet = Image.new("RGB", (w * 3 + 16, h), (255, 0, 255))
sheet.paste(a, (0, 0))
sheet.paste(b, (w + 8, 0))
sheet.paste(grey.point(lambda v: min(255, v * 3)).convert("RGB"), (2 * w + 16, 0))
sheet.save(out)
print(f"{out}: mean difference {mean:.2f}/255; {100 * far / total:.2f}% of pixels differ by more than 48")
