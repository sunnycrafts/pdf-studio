"""Generate a Windows 11-style app icon (icon.ico) for PDF Studio."""
from PIL import Image, ImageDraw

S = 256
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# Rounded background with a vertical blue gradient (Win11 accent).
top = (38, 138, 255)
bot = (0, 92, 214)
bg = Image.new("RGBA", (S, S), (0, 0, 0, 0))
bgd = ImageDraw.Draw(bg)
for y in range(S):
    t = y / S
    c = tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3)) + (255,)
    bgd.line([(0, y), (S, y)], fill=c)
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle([6, 6, S - 6, S - 6], radius=52, fill=255)
img.paste(bg, (0, 0), mask)

# White document with a folded top-right corner.
px0, py0, px1, py1 = 70, 54, 196, 210
fold = 30
d.polygon(
    [(px0, py0), (px1 - fold, py0), (px1, py0 + fold), (px1, py1), (px0, py1)],
    fill=(255, 255, 255, 255),
)
d.polygon([(px1 - fold, py0), (px1, py0 + fold), (px1 - fold, py0 + fold)],
          fill=(205, 222, 245, 255))

# Form lines + a ticked checkbox on the page.
gray = (176, 190, 205, 255)
for i, y in enumerate((92, 116, 140)):
    d.rounded_rectangle([px0 + 16, y, px1 - 18, y + 7], radius=3, fill=gray)
box = [px0 + 16, 162, px0 + 40, 186]
d.rounded_rectangle(box, radius=4, outline=(0, 92, 214, 255), width=4)
d.line([(px0 + 21, 175), (px0 + 28, 182), (px0 + 36, 167)], fill=(0, 150, 70, 255), width=5)

# Red signature swoosh across the lower page.
d.line([(px0 + 50, 178), (px0 + 78, 168), (px0 + 96, 184), (px1 - 22, 166)],
       fill=(224, 40, 40, 255), width=6, joint="curve")

img.save("icon.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("wrote icon.ico")
