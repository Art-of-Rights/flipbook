"""Generate flipbook.ico (and icon-src.png) with Pillow. Run: python make_icon.py [preview.png]

The icon: a bold open book (cream pages, gold cover edge, red ribbon) on a slate-blue tile, with
one page standing up mid-turn - a "flip book". Every size in the .ico is drawn on its own, not
shrunk from the big picture: the small sizes (16-32 px, title bar and taskbar) get a simpler,
chunkier book so it still reads as a book there (v1.8.4; the old icon looked like a blob there).
All coordinates are in a 1024 x 1024 design space.
"""
import os
import sys
from PIL import Image, ImageDraw, ImageFilter, ImageChops

HERE = os.path.dirname(os.path.abspath(__file__))

BG_TOP, BG_BOT = (0x4A, 0x6A, 0x8E), (0x1F, 0x2E, 0x45)     # slate blue tile
PAGE_L, PAGE_R = (0xFF, 0xFC, 0xF4), (0xF1, 0xEA, 0xDA)     # warm paper
STACK = (0xD6, 0xCB, 0xB2)                                    # page edges
GOLD_T, GOLD_B = (0xF2, 0xB5, 0x4A), (0xD0, 0x86, 0x1F)       # cover
RED_T, RED_B = (0xE5, 0x4B, 0x3C), (0xB3, 0x2E, 0x26)         # ribbon
INK = (0x9C, 0xA9, 0xB8)                                      # text lines
SPINE = (0x8A, 0x7D, 0x66)


def bez(p0, p1, p2, p3, n=48):
    pts = []
    for i in range(n + 1):
        t = i / n; u = 1 - t
        pts.append((u**3*p0[0] + 3*u*u*t*p1[0] + 3*u*t*t*p2[0] + t**3*p3[0],
                    u**3*p0[1] + 3*u*u*t*p1[1] + 3*u*t*t*p2[1] + t**3*p3[1]))
    return pts


def mirror(pts):
    return [(1024 - x, y) for x, y in pts]


def grad(W, c1, c2, horizontal=False):
    g = Image.new("RGB", (W, 1) if horizontal else (1, W))
    for i in range(W):
        t = i / (W - 1)
        g.putpixel((i, 0) if horizontal else (0, i), tuple(round(c1[k] + (c2[k] - c1[k]) * t) for k in range(3)))
    return g.resize((W, W)).convert("RGBA")


def render(size):
    """Draw the icon for one pixel size (at 8x, then downsample)."""
    small = size <= 24          # simplest book: no lifted page, no text lines
    mid = size <= 48            # no soft shadows, fewer lines
    SS = 8 if size < 256 else 2
    W = size * SS
    k = W / 1024
    sc = lambda pts: [(x * k, y * k) for x, y in pts]

    def mask(pts):
        m = Image.new("L", (W, W), 0); ImageDraw.Draw(m).polygon(sc(pts), fill=255); return m

    def soft(m, blur, dx=0, dy=0, op=120):
        m = ImageChops.offset(m, round(dx * k), round(dy * k)).filter(ImageFilter.GaussianBlur(blur * k))
        return m.point(lambda v: v * op // 255)

    # the open book: half-width and height grow for small sizes so it fills the tile
    if small:
        X0, TOP, MID, BOT, SPT, SPB = 92, 268, 300, 760, 330, 820
    else:
        X0, TOP, MID, BOT, SPT, SPB = 128, 300, 330, 730, 352, 790
    left = (bez((512, SPT), (420, MID - 40), (260, TOP - 30), (X0, TOP))
            + bez((X0, BOT), (260, BOT - 26), (420, BOT - 6), (512, SPB)))
    right = mirror(left)
    pages = left + list(reversed(right))     # whole book outline (top edge left->right is not needed)

    img = grad(W, BG_TOP, BG_BOT)
    black = Image.new("RGBA", (W, W), (0, 0, 0, 255))

    lm, rm = mask(left), mask(right)
    both = ImageChops.lighter(lm, rm)
    # cover: the book outline, pushed down and out a little
    cw, cd = (34, 46) if small else (24, 34)
    cover = mask([(512 + (x - 512) * (1 + cw / 400), y + cd) for x, y in left + list(reversed(right))])
    if not mid:
        img.paste(black, (0, 0), soft(ImageChops.lighter(cover, both), 26, 0, 22, 140))
    else:
        img.paste(black, (0, 0), soft(ImageChops.lighter(cover, both), 10, 0, 14, 110))
    img.paste(grad(W, GOLD_T, GOLD_B), (0, 0), cover)

    # ribbon tail hanging below the spine
    rw = 66 if small else 48
    rib = mask([(512 - rw, 700), (512 + rw, 700), (512 + rw, 940), (512, 880), (512 - rw, 940)])
    img.paste(grad(W, RED_T, RED_B), (0, 0), rib)

    # page edges (stack) between pages and cover
    sd = 22 if small else 16
    stack = ImageChops.lighter(ImageChops.offset(lm, 0, round(sd * k)), ImageChops.offset(rm, 0, round(sd * k)))
    img.paste(Image.new("RGBA", (W, W), STACK + (255,)), (0, 0), stack)

    # pages
    img.paste(grad(W, PAGE_R, PAGE_L, horizontal=True), (0, 0), lm)
    img.paste(grad(W, PAGE_L, PAGE_R, horizontal=True), (0, 0), rm)

    # spine shading
    sp = Image.new("L", (W, W), 0)
    spw = 70 if small else 26
    ImageDraw.Draw(sp).rectangle([(512 - spw / 2) * k, 0, (512 + spw / 2) * k, W], fill=255)
    if not mid:
        sp = sp.filter(ImageFilter.GaussianBlur(14 * k))
    sp = ImageChops.multiply(sp, both).point(lambda v: v * (200 if small else 150) // 255)
    img.paste(Image.new("RGBA", (W, W), SPINE + (255,)), (0, 0), sp)

    d = ImageDraw.Draw(img)
    # text lines on the left page
    if not small:
        rows = (420, 510, 600) if mid else (400, 470, 540, 610, 680)
        lw = 30 if mid else 18
        for i, y in enumerate(rows):
            x1 = 452 if i % 2 == 0 else 410
            d.rounded_rectangle([X0 * 1.45 * k, (y + (512 - 300) * 0) * k, x1 * k, (y + lw) * k], radius=lw / 2 * k, fill=INK + (255,))

    # the page mid-turn: standing up from the spine over the right page
    if not small:
        top = bez((512, SPT), (585, 245), (700, 196), (806, 206))
        edge = bez((806, 206), (782, 360), (752, 560), (676, 700))
        bot = bez((676, 700), (612, 752), (556, 776), (512, SPB))
        leaf = top + edge[1:] + bot[1:]
        fm = mask(leaf)
        if not mid:
            img.paste(black, (0, 0), ImageChops.multiply(soft(fm, 24, 34, 14, 120), rm))
        else:
            img.paste(black, (0, 0), ImageChops.multiply(soft(fm, 8, 30, 10, 90), rm))
        img.paste(grad(W, (0xE6, 0xDE, 0xCC), (0xFF, 0xFD, 0xF8), horizontal=True), (0, 0), fm)
        # curl: a darker band along the free edge
        e = Image.new("L", (W, W), 0)
        ImageDraw.Draw(e).line(sc(edge), fill=255, width=max(1, round((40 if mid else 28) * k)), joint="curve")
        e = ImageChops.multiply(e.filter(ImageFilter.GaussianBlur((6 if mid else 10) * k)), fm).point(lambda v: v * 110 // 255)
        img.paste(Image.new("RGBA", (W, W), (0xC9, 0xBC, 0xA0, 255)), (0, 0), e)

    # rounded tile
    tile = Image.new("L", (W, W), 0)
    ImageDraw.Draw(tile).rounded_rectangle([0, 0, W - 1, W - 1], radius=(150 if small else 200) * k, fill=255)
    out = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    out.paste(img, (0, 0), tile)
    return out.resize((size, size), Image.LANCZOS)


SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]


def main():
    imgs = {s: render(s) for s in SIZES}
    master = render(1024)
    if len(sys.argv) > 1:                      # preview sheet: every size, plus each one enlarged
        pad = 24
        sheet = Image.new("RGBA", (1700, 900), (0x26, 0x28, 0x2C, 255))
        light = Image.new("RGBA", (1700, 260), (0xEE, 0xEE, 0xEE, 255)); sheet.paste(light, (0, 640))
        x = pad
        for s in SIZES:
            sheet.paste(imgs[s], (x, 30), imgs[s]); sheet.paste(imgs[s], (x, 660), imgs[s])
            x += s + pad
        x = pad
        for s in (16, 24, 32, 48):
            big = imgs[s].resize((s * 8 if s < 48 else 256,) * 2, Image.NEAREST)
            sheet.paste(big, (x, 320), big); x += big.width + pad
        m = master.resize((280, 280), Image.LANCZOS); sheet.paste(m, (1390, 320), m)
        sheet.save(sys.argv[1]); print("preview:", sys.argv[1]); return
    master.save(os.path.join(HERE, "icon-src.png"))
    big = imgs[256]
    big.save(os.path.join(HERE, "flipbook.ico"), format="ICO", sizes=[(s, s) for s in SIZES],
             append_images=[imgs[s] for s in SIZES if s != 256])
    print("wrote flipbook.ico and icon-src.png")


if __name__ == "__main__":
    main()
