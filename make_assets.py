"""One-off: derive the card board + digit glyphs from a reference capture (assets/reference.png).
Outputs assets/board_{left,tile,right}.png, assets/glyphs.png, assets/glyphs.json, assets/plaque.png."""
import json
import numpy as np
from PIL import Image

ref = Image.open("assets/reference.png").convert("RGB")
A = np.asarray(ref).astype(int)
SKILL_GAPS = [20, 135, 251, 367]
ACT_GAPS = [367, 534, 703, 866, 1032, 1198, 1367, 1530]
ROW_GAPS = [30 + 57 * k for k in range(10)]
TOP, BOT = 6, 599  # frame bounding box inside the capture


def yellow(region):
    r, g, b = region[..., 0], region[..., 1], region[..., 2]
    return np.clip((np.minimum(r, g) - b) / 230.0, 0, 1)


def runs(flags):
    out, start = [], None
    for i, v in enumerate(flags):
        if v and start is None:
            start = i
        elif not v and start is not None:
            out.append((start, i)); start = None
    if start is not None:
        out.append((start, len(flags)))
    return out


def harvest(gap_x, row, text, x_off, store, end_x=None):
    """Cut glyphs out of the number printed in a cell; `text` lists the characters in order."""
    y0, y1 = ROW_GAPS[row] + 6, ROW_GAPS[row] + 51
    reg = A[y0:y1, gap_x + x_off:(end_x - 4 if end_x else gap_x + x_off + 100)]
    ym = yellow(reg)
    cols = runs(ym.max(0) > 0.3)
    # merge runs that touch (a glyph split by anti-aliasing)
    if len(cols) != len(text):
        print("harvest mismatch", text, cols); return
    for (c0, c1), ch in zip(cols, text):
        rows = np.where(ym[:, c0:c1].max(1) > 0.3)[0]
        store.setdefault(ch, (gap_x + x_off + c0, y0 + rows.min(), gap_x + x_off + c1, y0 + rows.max() + 1))


found = {}
skill_vals = ["98", "99", "90", "99", "81", "80", "96", "86", "84", "99", "83", "94", "90", "93", "80",
              "99", "92", "85", "68", "99", "90", "74", "85", "62"]
for i, v in enumerate(skill_vals):
    harvest(SKILL_GAPS[i % 3], i // 3, v, 55, found, SKILL_GAPS[i % 3 + 1])
harvest(ACT_GAPS[1], 6, "7", 60, found, ACT_GAPS[2]) if False else None
harvest(ACT_GAPS[1], 0, "1,000", 60, found) if False else None
y0_, y1_ = ROW_GAPS[6] + 6, ROW_GAPS[6] + 51
reg7 = A[y0_:y1_, ACT_GAPS[1] + 60:ACT_GAPS[2] - 4]; y7 = yellow(reg7); r7 = runs(y7.max(0) > 0.3)[0]
rr = np.where(y7[:, r7[0]:r7[1]].max(1) > 0.3)[0]
found["7"] = (ACT_GAPS[1] + 60 + r7[0], y0_ + rr.min(), ACT_GAPS[1] + 60 + r7[1], y0_ + rr.max() + 1)
print("digits found:", sorted(k for k in found))

# comma from "1,000": runs are 1 , 0 0 0 (the zeros touch) -> take the 2nd run
y0, y1 = ROW_GAPS[0] + 6, ROW_GAPS[0] + 51
reg = A[y0:y1, ACT_GAPS[1] + 60:ACT_GAPS[1] + 160]
ym = yellow(reg); cr = runs(ym.max(0) > 0.3)
c0, c1 = cr[1]
rows = np.where(ym[:, c0:c1].max(1) > 0.3)[0]
found[","] = (ACT_GAPS[1] + 60 + c0, y0 + rows.min(), ACT_GAPS[1] + 60 + c1, y0 + rows.max() + 1)

rel = lambda y: (y - 30) % 57  # y relative to its row
digit_top = int(np.median([rel(found[c][1]) for c in "0123456789" if c in found]))
digit_bot = int(np.median([rel(found[c][3] - 1) + 1 for c in "0123456789" if c in found]))
M = 2  # margin that keeps the dark outline


def glyph_img(box):
    x0, y0_, x1, y1_ = box
    patch = A[y0_ - M:y1_ + M, x0 - M:x1 + M]
    ya = yellow(patch)
    sh = np.clip((90 - patch.max(2)) / 90.0, 0, 1) * (1 - ya)
    out = np.zeros(patch.shape[:2] + (4,), dtype=np.uint8)
    out[..., 0] = (255 * ya).astype(np.uint8); out[..., 1] = out[..., 0]
    out[..., 3] = (np.clip(np.maximum(ya, sh), 0, 1) * 255).astype(np.uint8)
    return Image.fromarray(out, "RGBA")


meta, parts = {}, []
for ch, box in found.items():
    g = glyph_img(box)
    parts.append((ch, g, box))
W = sum(g.width + 2 for _, g, _ in parts)
atlas = Image.new("RGBA", (W, 40), (0, 0, 0, 0))
x = 0
for ch, g, box in parts:
    atlas.paste(g, (x, 0))
    meta[ch] = {"x": x, "w": g.width, "h": g.height, "dy": int(rel(box[1]) - digit_top - M)}  # offset of glyph image top vs digit top
    x += g.width + 2
atlas.save("assets/glyphs.png")
json.dump({"glyphs": meta, "digit_h": int(digit_bot - digit_top), "gap": 1, "margin": M}, open("assets/glyphs.json", "w"))
print("digit height", digit_bot - digit_top, "meta", meta)

# ---- blank board: wipe every cell back to bare stone ---------------------------------------
B = ref.copy()
stone = ref.crop((ACT_GAPS[6] + 6, ROW_GAPS[6] + 6, ACT_GAPS[7] - 6, ROW_GAPS[6] + 51))  # an empty cell interior


def wipe(x0, x1, row):
    w, h = x1 - x0, 45
    B.paste(stone.resize((w, h), Image.BICUBIC), (x0, ROW_GAPS[row] + 6))


# the three bottom-left plaques are black boxes; keep the third as the "Total Level" plaque
plaque = ref.crop((SKILL_GAPS[2] + 3, ROW_GAPS[8] + 3, SKILL_GAPS[3] - 3, ROW_GAPS[8] + 54))
for row in range(9):
    for i in range(3):
        wipe(SKILL_GAPS[i] + 6, SKILL_GAPS[i + 1] - 6, row)
    for i in range(7):
        wipe(ACT_GAPS[i] + 6, ACT_GAPS[i + 1] - 6, row)
# caption bar -> plain black
B.paste((0, 0, 0), (30, 547, 1526, 571))
B = B.crop((0, TOP, ref.width, BOT))
B.crop((0, 0, 450, B.height)).save("assets/board_left.png")
B.crop((450, 0, 617, B.height)).save("assets/board_tile.png")
B.crop((1448, 0, ref.width, B.height)).save("assets/board_right.png")
plaque.save("assets/plaque.png")
print("board", B.size, "plaque", plaque.size)
