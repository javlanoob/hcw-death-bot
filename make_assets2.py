"""One-off: cut the in-game stats-tab pieces (cell frame, digit font) out of assets/osrs_ref.png."""
import json
import numpy as np
from PIL import Image

a = np.asarray(Image.open("assets/osrs_ref.png").convert("RGB")).astype(int)
X0, Y0, CW, CH = (549, 611, 674), 207, 61, 29
cells = [a[Y0 + 30 * r:Y0 + 30 * r + CH, x:x + CW] for r in range(8) for x in X0]
st = np.stack(cells)
sat = st.max(3) - st.min(3)
bri = st.mean(3)
ok = (sat < 24) & (bri > 70) & (bri < 140)
out = np.median(st, 0)
for y in range(CH):
    for x in range(CW):
        s = st[:, y, x][ok[:, y, x]]
        if len(s) >= 3:
            out[y, x] = np.median(s, 0)
cell = out.astype(np.uint8)
for _ in range(3):
    base = cell.astype(int)
    new = cell.copy()
    for y in range(3, 26):
        for x in range(3, 58):
            win = base[max(y - 3, 3):y + 4, max(x - 3, 3):x + 4].reshape(-1, 3)
            med = np.median(win, 0)
            if abs(base[y, x] - med).max() > 16 or (base[y, x].max() - base[y, x].min()) > 26:
                new[y, x] = med
    cell = new
cell[27:29, 4:58] = 0
Image.fromarray(cell).save("assets/ig_cell.png")

# digits: top numbers of every cell (known levels), yellow pixels
top = [53, 50, 55, 57, 58, 61, 46, 42, 48, 47, 45, 49, 45, 41, 40, 64, 33, 39, 27, 41, 52, 50, 57, 1]
glyphs = {}
for i, lvl in enumerate(top):
    c = cells[i]
    y = (c[..., 0] > 200) & (c[..., 1] > 200) & (c[..., 2] < 100)
    reg = y[2:14, 28:]
    cols = np.where(reg.any(0))[0]
    runs, s = [], None
    for k in range(reg.shape[1] + 1):
        on = k < reg.shape[1] and reg[:, k].any()
        if on and s is None:
            s = k
        if not on and s is not None:
            runs.append((s, k)); s = None
    txt = str(lvl)
    if len(runs) != len(txt):
        continue
    for ch, (s, e) in zip(txt, runs):
        if ch in glyphs:
            continue
        rows = np.where(reg[:, s:e].any(1))[0]
        glyphs[ch] = (c[2 + rows[0]:2 + rows[-1] + 1, 28 + s:28 + e], reg[rows[0]:rows[-1] + 1, s:e], int(rows[0]) + 2, int(28 + s))
print(sorted(glyphs))
atlas, meta, x = [], {}, 0
for ch, (px, m, ty, tx) in sorted(glyphs.items()):
    h, w = m.shape
    g = np.zeros((h + 1, w + 1, 4), np.uint8)
    sh = np.zeros((h + 1, w + 1), bool); sh[1:, 1:] = m
    g[sh & ~np.pad(m, ((0, 1), (0, 1)))] = (0, 0, 0, 255)
    mm = np.pad(m, ((0, 1), (0, 1)))
    g[mm] = (255, 255, 0, 255)
    atlas.append((ch, g)); meta[ch] = {"w": w + 1, "h": h + 1, "top": ty}
W = sum(g.shape[1] + 1 for _, g in atlas); H = max(g.shape[0] for _, g in atlas)
img = np.zeros((H, W, 4), np.uint8); x = 0
for ch, g in atlas:
    img[:g.shape[0], x:x + g.shape[1]] = g; meta[ch]["x"] = x; x += g.shape[1] + 1
Image.fromarray(img).save("assets/ig_glyphs.png")
json.dump(meta, open("assets/ig_glyphs.json", "w"))
print(meta)
Image.fromarray(img).resize((W * 8, H * 8), Image.NEAREST).save("assets/_g.png")
Image.fromarray(cell).resize((CW * 8, CH * 8), Image.NEAREST).save("assets/_cell.png")

# "Total level:" label + its bar
tl = a[449:463, 600:662]
m = (tl[..., 0] > 200) & (tl[..., 1] > 200) & (tl[..., 2] < 100)
ys, xs = np.where(m)
y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
mm = np.pad(m[y0:y1, x0:x1], ((0, 1), (0, 1)))
sh = np.zeros_like(mm); sh[1:, 1:] = mm[:-1, :-1]
lab = np.zeros(mm.shape + (4,), np.uint8)
lab[sh & ~mm] = (0, 0, 0, 255); lab[mm] = (255, 255, 0, 255)
Image.fromarray(lab).save("assets/ig_label.png")
print("label", lab.shape)
Image.fromarray(lab).resize((lab.shape[1]*6, lab.shape[0]*6), Image.NEAREST).save("assets/_l.png")
