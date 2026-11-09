"""One-off: cut a blank stats-tab cell and the slash out of the wiki's Skills_tab.png (assets/skills_tab.png)."""
import numpy as np
from PIL import Image

a = np.asarray(Image.open("assets/skills_tab.png").convert("RGB")).astype(int)
XS, CW, CH = (9, 72, 135), 60, 29
cells = [a[9 + 30 * r:9 + 30 * r + CH, x:x + CW] for r in range(8) for x in XS]
st = np.stack(cells)
sat = st.max(3) - st.min(3)
bri = st.mean(3)
ok = (sat < 14) & (bri > 60) & (bri < 100)
out = np.median(st, 0)
for y in range(CH):
    for x in range(CW):
        s = st[:, y, x][ok[:, y, x]]
        if len(s) >= 4:
            out[y, x] = np.median(s, 0)
cell = out.astype(np.uint8)
# number area is identical in nearly every cell (all 73s), so rebuild it from clean stone
tex = cell[3:27, 4:30]
for y in range(2, 28):
    for x in range(30, 57):
        cell[y, x] = tex[(y - 3) % 24, (x - 30) % 26] if x < 56 else cell[y, x]
for _ in range(3):
    base = cell.astype(int); new = cell.copy()
    for y in range(3, 28):
        for x in range(4, 56):
            win = base[max(y - 3, 3):min(y + 4, 28), max(x - 3, 4):x + 4].reshape(-1, 3)
            med = np.median(win, 0)
            if abs(base[y, x] - med).max() > 14 or (base[y, x].max() - base[y, x].min()) > 18:
                new[y, x] = med
    cell = new
Image.fromarray(cell).save("assets/ig_cell2.png")
# slash: dark pixels in the number area of the Attack cell, minus digit shadows
c = cells[0]
yel = (c[..., 0] > 200) & (c[..., 1] > 200) & (c[..., 2] < 100)
sh = np.zeros_like(yel); sh[1:, 1:] = yel[:-1, :-1]
dark = (c.sum(2) < 60) & ~yel & ~sh
dark[:, :31] = False; dark[:2] = False; dark[27:] = False
sl = np.zeros((CH, CW, 4), np.uint8); sl[dark] = (0, 0, 1, 255)
Image.fromarray(sl).save("assets/ig_slash.png")
print(dark.sum())
big = np.zeros((CH, CW, 3), np.uint8); big[...] = cell; big[dark] = (255, 0, 0)
Image.fromarray(big).resize((CW * 8, CH * 8), Image.NEAREST).save("wiki/_c.png")

import hcw_deaths as h
for i, name in enumerate(h.SKILL_GRID):
    r, c = divmod(i, 3)
    cell_ = a[9 + 30 * r:9 + 30 * r + CH, XS[c]:XS[c] + CW].astype(np.uint8)
    Image.fromarray(cell_[3:27, 3:32]).save("assets/skill_icons/%s.png" % name)
