import os
from PIL import Image, ImageDraw

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
os.makedirs(OUT_DIR, exist_ok=True)
S, K = 1024, 4
W = S * K

def lerp(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))

base = Image.new("RGB", (W, W))
px = base.load()
c0, c1, c2 = (124, 58, 237), (99, 102, 241), (14, 165, 233)
for y in range(W):
    for x in range(W):
        t = (x + y) / (2.0 * (W - 1))
        px[x, y] = lerp(c0, c1, t / 0.48) if t < 0.48 else lerp(c1, c2, (t - 0.48) / 0.52)

base = base.convert("RGBA")
mask = Image.new("L", (W, W), 0)
ImageDraw.Draw(mask).rounded_rectangle([0, 0, W - 1, W - 1], radius=int(W * 0.3077), fill=255)
base.putalpha(mask)

sheen = Image.new("RGBA", (W, W), (0, 0, 0, 0))
sd = ImageDraw.Draw(sheen)
for r in range(int(W * 0.85), 0, -max(1, W // 260)):
    a = int(44 * (1 - r / (W * 0.85)) ** 2)
    sd.ellipse([-r * 0.55, -r * 0.72, r * 0.72, r * 0.42], fill=(255, 255, 255, a))
base = Image.alpha_composite(base, Image.composite(sheen, Image.new("RGBA", (W, W), (0, 0, 0, 0)), mask))

d = ImageDraw.Draw(base)
u = W / 24.0
p0, p1, p2, p3 = (6.1, 6.1), (13.5, 6.1), (5.3, 17.9), (17.9, 17.9)

def bez(t):
    mt = 1 - t
    return (mt**3*p0[0] + 3*mt*mt*t*p1[0] + 3*mt*t*t*p2[0] + t**3*p3[0],
            mt**3*p0[1] + 3*mt*mt*t*p1[1] + 3*mt*t*t*p2[1] + t**3*p3[1])

stroke = 1.95 * u
r = stroke / 2.0
for i in range(721):
    x, y = bez(i / 720.0)
    d.ellipse([x*u - r, y*u - r, x*u + r, y*u + r], fill=(255, 255, 255, 255))

for p in (p0, p3):
    rr = 2.02 * u
    d.ellipse([p[0]*u - rr, p[1]*u - rr, p[0]*u + rr, p[1]*u + rr], fill=(255, 255, 255, 255))

mx, my = bez(0.5)
rr = 1.42 * u
d.ellipse([mx*u - rr, my*u - rr, mx*u + rr, my*u + rr], fill=(56, 189, 248, 255))

d.rounded_rectangle([W*0.012, W*0.012, W-1-W*0.012, W-1-W*0.012], radius=int(W*0.30),
                    outline=(255, 255, 255, 30), width=max(2, int(W*0.0045)))

icon = base.resize((S, S), Image.LANCZOS)
icon.save(os.path.join(OUT_DIR, "icon.png"))
icon.save(os.path.join(OUT_DIR, "icon.ico"), sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])
print("ok")
