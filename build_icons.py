#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_icons.py —— 生成 py++ 的文件图标（.ico）

设计是两张参考图的"缝合体"：
  · Python 的 .py 图标（文档页 + 蓝黄双蛇）
  · C++ 的 .cpp 图标（文档页 + 洋红 ++）
=> py++ = Python 的语法 + C++ 的速度，于是：
  · 源码 .cpy  → 文档页 + 双蛇 + 洋红 ++（文档样式）
  · exe        → 同样的三元素，换成应用方块（exe 样式）

做法：
  · 双蛇从参考图里按颜色规则**真实提取**（保住 Python logo 原真度）；
  · 文档页、++ 因原图太小（++ 只有 12×7），**高清重绘**，颜色沿用参考图采样值。

产物：
  assets/cpy.ico     .cpy 文件图标（注册表 DefaultIcon 用）
  assets/pypp.ico    exe 图标（PyInstaller --icon 用）
  assets/preview_*.png  256×256 预览图

用法：
  python build_icons.py            # 需要参考图在剪贴板目录，或用已提取的 assets/_snakes.png
  python build_icons.py --from-snakes   # 仅用 assets/_snakes.png（跳过参考图）
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, 'assets')
BUILD = os.path.join(HERE, 'build')

# 参考图采样色（两张截图里量出来的）
PAGE_FILL = (246, 246, 246)      # 文档页填充
PAGE_EDGE = (101, 101, 101)      # 文档页描边
CODE_LINE = (178, 178, 178)      # 页面里的代码行
FOLD_FILL = (226, 226, 226)      # 折角
PLUSPLUS = (155, 79, 150)        # C++ 的洋红 ++
SNAKES_SRC = os.path.join(ASSETS, '_snakes.png')   # 提取好的双蛇（40×32）

# 剪贴板目录里的原始参考图（提取双蛇用）
_CLIP = os.path.join(os.path.expanduser('~'), '.workbuddy-ai', 'clipboard-images')


def _find_reference():
    """在剪贴板目录里找 Python .py 图标那张参考图。"""
    if not os.path.isdir(_CLIP):
        return None
    cands = []
    for fn in sorted(os.listdir(_CLIP), reverse=True):
        if not fn.lower().endswith('.png'):
            continue
        p = os.path.join(_CLIP, fn)
        try:
            from PIL import Image
            with Image.open(p) as im:
                w, h = im.size
        except Exception:
            continue
        # Python 那张是 60×52；C++ 那张是 50×44
        if (w, h) == (60, 52):
            cands.append(p)
    return cands[0] if cands else None


def extract_snakes():
    """
    从参考图提取双蛇（蓝 + 黄），存成带透明背景的 PNG。

    颜色规则（已实测可与背景区分）：
      · 黄：   r>200 and g>170 and b<140
      · 蛇蓝： (b-r)>50 and b>140 and r<120
        （背景是 r≈167~207 的浅蓝，r<120 恰好把它排除）
    提取后还会做连通域清理：只保留面积 >= 最大值 10% 的块，
    否则原图底部漏进来的背景蓝横带会跟着一起被放大。
    """
    from PIL import Image, ImageFilter

    src = _find_reference()
    if not src:
        if os.path.isfile(SNAKES_SRC):
            print(f"· 没找到参考图，沿用已提取的 {SNAKES_SRC}")
            return Image.open(SNAKES_SRC).convert('RGBA')
        raise SystemExit("找不到参考图，也没有 assets/_snakes.png")

    print(f"· 参考图：{src}")
    im = Image.open(src).convert('RGB')
    W, H = im.size
    px = im.load()

    mask = [[False] * W for _ in range(H)]
    xs, ys = [], []
    for y in range(H):
        for x in range(W):
            r, g, b = px[x, y]
            is_yellow = r > 200 and g > 170 and b < 140
            is_blue = (b - r) > 50 and b > 140 and r < 120
            if is_yellow or is_blue:
                mask[y][x] = True
                xs.append(x)
                ys.append(y)

    # --- 连通域清理（4 邻域 BFS）---
    comps = []
    seen = [[False] * W for _ in range(H)]
    for y in range(H):
        for x in range(W):
            if not mask[y][x] or seen[y][x]:
                continue
            stack = [(x, y)]
            seen[y][x] = True
            cells = []
            while stack:
                cx, cy = stack.pop()
                cells.append((cx, cy))
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nx, ny = cx + dx, cy + dy
                    if 0 <= nx < W and 0 <= ny < H and mask[ny][nx] \
                            and not seen[ny][nx]:
                        seen[ny][nx] = True
                        stack.append((nx, ny))
            comps.append(cells)

    comps.sort(key=len, reverse=True)
    biggest = len(comps[0]) if comps else 0
    keep = set()
    dropped = 0
    for c in comps:
        if len(c) >= max(6, biggest * 0.10):
            keep.update(c)
        else:
            dropped += len(c)
    if dropped:
        print(f"· 连通域清理：丢掉 {dropped} 个杂散像素"
              f"（{len(comps)} 个连通域，主体 {biggest} px）")

    out = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    op = out.load()
    kxs, kys = [], []
    for (x, y) in keep:
        r, g, b = px[x, y]
        op[x, y] = (r, g, b, 255)
        kxs.append(x)
        kys.append(y)

    bbox = (min(kxs), min(kys), max(kxs) + 1, max(kys) + 1)
    print(f"· 双蛇提取范围：{bbox}（{bbox[2]-bbox[0]}×{bbox[3]-bbox[1]}）")

    # 边缘轻微羽化即可 —— 别糊太狠，锐度主要由 _paste_snakes 的
    # 三段式管线保证（源图只 28×28，放大 4.6 倍，羽化 1px 就等于
    # 成品上 4.6px 的软边，看起来就是"发虚"）。
    out.putalpha(out.split()[3].filter(ImageFilter.GaussianBlur(0.3)))
    crop = out.crop(bbox)

    os.makedirs(ASSETS, exist_ok=True)
    crop.save(SNAKES_SRC)
    print(f"· 已存 {SNAKES_SRC}")
    return crop


def _rounded(draw, box, r, **kw):
    draw.rounded_rectangle(box, radius=r, **kw)


def _draw_page(size=256):
    """高清重绘参考图里的文档页（圆角 + 右上折角 + 代码行）。"""
    from PIL import Image, ImageDraw

    im = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    s = size / 256.0

    x0, y0, x1, y1 = 56 * s, 28 * s, 200 * s, 228 * s
    ew = max(2, int(4 * s))
    _rounded(d, [x0, y0, x1, y1], 12 * s, fill=PAGE_FILL + (255,),
             outline=PAGE_EDGE + (255,), width=ew)

    # 折角（右上）：盖一个浅色三角 + 两条边
    f = 34 * s
    d.polygon([(x1 - f, y0), (x1, y0 + f), (x1 - f, y0 + f)],
              fill=FOLD_FILL + (255,))
    d.line([(x1 - f, y0), (x1, y0 + f)], fill=PAGE_EDGE + (255,), width=ew)
    d.line([(x1 - f, y0), (x1 - f, y0 + f)], fill=PAGE_EDGE + (255,), width=ew)

    # 代码行（左侧几条短横，示意"这是源码文件"）
    lw = max(2, int(6 * s))
    for yy in (70, 96, 122, 148):
        d.rounded_rectangle([80 * s, yy * s, 150 * s, yy * s + 7 * s],
                            radius=3 * s, fill=CODE_LINE + (255,))
    return im


# 矢量化配色（从参考图采样校准，比逐像素抄更干净）
SIL_BLUE = (48, 105, 152)        # 蛇身主蓝
SIL_BLUE2 = (75, 139, 190)       # 蓝蛇内阴影（浅蓝）
SIL_YEL = (255, 218, 74)         # 蛇身黄

# 矢量化参数
_VEC_SS = 16                     # 掩膜超采样倍率
_VEC_BLUR = 8.0                  # 高斯半径（候选 4/6/8/10 实测选 8：
                                 # 4 仍见像素阶梯，10 会把直角圆化）
_VEC_THRESH = 128                # 平滑后二值化阈值


def _label_map(snakes):
    """把源图每个不透明像素归到 0=空 / 1=蓝 / 2=黄 三类。"""
    W, H = snakes.size
    px = snakes.load()
    lab = [[0] * W for _ in range(H)]
    any_blue = [[False] * W for _ in range(H)]
    any_yel = [[False] * W for _ in range(H)]
    for y in range(H):
        for x in range(W):
            r, g, b, a = px[x, y]
            if a <= 128:
                continue
            if r > 200 and g > 170 and b < 140:
                lab[y][x] = 2
                any_yel[y][x] = True
            elif (b - r) > 50 and b > 140:
                lab[y][x] = 1
                any_blue[y][x] = True
    return lab, any_blue, any_yel


def _blur_labels(lab, src_has, W, H, ss):
    """把某一类的标签掩膜超采样 + 高斯平滑，返回浮点场（用于阈值化）。

    做法：标签掩膜 NEAREST 放大 ss 倍 → GaussianBlur(radius) →
    这样每条边都有 ss 像素宽的渐变过渡，阈值化后落到亚像素位置，
    轮廓就平滑了（纯色块本身仍是纯色，不会发虚）。
    """
    from PIL import Image, ImageFilter

    mask = Image.new('L', (W, H), 0)
    mp = mask.load()
    for y in range(H):
        for x in range(W):
            if src_has[y][x]:
                mp[x, y] = 255
    big = mask.resize((W * ss, H * ss), Image.NEAREST)
    return big.filter(ImageFilter.GaussianBlur(_VEC_BLUR * ss / 46.0))


def _render_layer(field, W, H, ss, color, thresh=_VEC_THRESH):
    """把平滑场阈值化后渲染成纯色 RGBA 层（超采样尺寸）。"""
    from PIL import Image

    rgb = Image.new('RGB', (W * ss, H * ss), color)
    a = field.point(lambda v: 255 if v >= thresh else 0)
    rgb.putalpha(a)
    return rgb


def _vectorize_snakes(snakes, out_w):
    """把 28×28 的双蛇矢量化放大到 out_w 宽，返回 RGBA 图。

    管线：分类掩膜 → 超采样高斯平滑 → 阈值二值化 → 纯色重绘 → 缩回目标尺寸。
    比"NEAREST 放大颜色 + LANCZOS 放大 alpha"更干净：边缘落在亚像素位置，
    没有像素阶梯，同时纯色块保持纯色（不发虚）。
    """
    from PIL import Image, ImageFilter

    ss = _VEC_SS
    W, H = snakes.size
    lab, any_blue, any_yel = _label_map(snakes)

    # 蛇身蓝色分成两层：外圈主蓝 + 内圈浅蓝阴影
    blue_dark = [[any_blue[y][x] and (snakes.load()[x, y][0] < 58)
                  for x in range(W)] for y in range(H)]
    blue_light = [[any_blue[y][x] and not blue_dark[y][x]
                   for x in range(W)] for y in range(H)]

    f_yel = _blur_labels(lab, any_yel, W, H, ss)
    f_bl = _blur_labels(lab, blue_light, W, H, ss)
    f_bd = _blur_labels(lab, blue_dark, W, H, ss)

    canvas = Image.new('RGBA', (W * ss, H * ss), (0, 0, 0, 0))
    # 先黄后蓝：蓝蛇压在黄蛇之上（参考图构图）
    canvas.alpha_composite(_render_layer(f_yel, W, H, ss, SIL_YEL))
    canvas.alpha_composite(_render_layer(f_bl, W, H, ss, SIL_BLUE2))
    canvas.alpha_composite(_render_layer(f_bd, W, H, ss, SIL_BLUE))

    w = int(round(out_w))
    h = int(round(H * w / W))
    return canvas.resize((w, h), Image.LANCZOS)


def _paste_snakes(base, snakes, cx, cy, width):
    """把矢量化后的双蛇贴到 base 上（居中于 cx, cy，宽度 width）。"""
    w = int(round(width))
    if w < 2:
        return 0
    mid = _vectorize_snakes(snakes, w)
    h = mid.height
    base.alpha_composite(mid, (int(round(cx - w / 2)), int(round(cy - h / 2))))
    return h


def _draw_plusplus(size, text='++', font_size=46, color=PLUSPLUS):
    """高清重绘洋红 ++（原图只有 12×7，放大必糊）。"""
    from PIL import Image, ImageDraw, ImageFont

    im = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)

    font = None
    for name in ('segoeuib.ttf', 'arialbd.ttf', 'arial.ttf'):
        p = os.path.join('C:/Windows/Fonts', name)
        if os.path.isfile(p):
            try:
                font = ImageFont.truetype(p, int(font_size * size / 256.0))
                break
            except Exception:
                continue
    if font is None:
        # 兜底：两个矩形拼一个加号
        t = font_size * size / 256.0
        arm = max(2, int(t * 0.22))
        for cx in (0.28 * size, 0.66 * size):
            cx *= 1.0
            d.rounded_rectangle([cx - t / 2, 0.5 * size - arm / 2,
                                 cx + t / 2, 0.5 * size + arm / 2],
                                radius=arm / 2, fill=color + (255,))
            d.rounded_rectangle([cx - arm / 2, 0.5 * size - t / 2,
                                 cx + arm / 2, 0.5 * size + t / 2],
                                radius=arm / 2, fill=color + (255,))
        return im

    # 居中绘制，并返回其 bbox 以便调用方精确定位
    l, t, r, b = d.textbbox((0, 0), text, font=font)
    tw, th = r - l, b - t
    d.text(((size - tw) / 2 - l, (size - th) / 2 - t), text,
           font=font, fill=color + (255,))
    return im


def build_cpy_icon(snakes, size=256):
    """源码 .cpy 图标：文档页 + 双蛇 + 洋红 ++。"""
    from PIL import Image

    im = _draw_page(size)
    # 双蛇：居中偏右下，故意跨出页面右缘（对齐参考图的构图）
    _paste_snakes(im, snakes, cx=140 * size / 256.0,
                  cy=166 * size / 256.0, width=130 * size / 256.0)

    # ++ 放页面右上、压一点折角
    pp = _draw_plusplus(size, '++', 46)
    # 只取 ++ 的实体区域，摆到目标位置
    bbox = pp.getbbox()
    if bbox:
        crop = pp.crop(bbox)
        tx = int(150 * size / 256.0 - crop.width / 2)
        ty = int(46 * size / 256.0 - crop.height / 2)
        im.alpha_composite(crop, (tx, ty))
    return im


def build_exe_icon(snakes, size=256):
    """exe 图标：应用方块 + 双蛇 + 洋红 ++。"""
    from PIL import Image, ImageDraw

    im = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    s = size / 256.0

    # 圆角应用方块（和文档页同色系，靠"形状"区分源码/程序）
    _rounded(d, [24 * s, 24 * s, 232 * s, 232 * s], 52 * s,
             fill=PAGE_FILL + (255,), outline=PAGE_EDGE + (255,),
             width=max(2, int(5 * s)))

    # 双蛇居中偏上
    _paste_snakes(im, snakes, cx=128 * s, cy=112 * s, width=142 * s)

    # ++ 放右下角
    pp = _draw_plusplus(size, '++', 54)
    bbox = pp.getbbox()
    if bbox:
        crop = pp.crop(bbox)
        tx = int(176 * s - crop.width / 2)
        ty = int(178 * s - crop.height / 2)
        im.alpha_composite(crop, (tx, ty))
    return im


ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48),
             (64, 64), (128, 128), (256, 256)]


def save_ico(im, path):
    im.save(path, format='ICO', sizes=ICO_SIZES)
    print(f"· 已生成 {os.path.relpath(path, HERE)}  "
          f"({os.path.getsize(path)} 字节, {len(ICO_SIZES)} 个尺寸)")


def dump_radius_candidates():
    """把双蛇按不同高斯半径矢量化，拼一张对比图（调参用）。"""
    from PIL import Image, ImageDraw

    global _VEC_BLUR
    if not os.path.isfile(SNAKES_SRC):
        raise SystemExit("需要先有 assets/_snakes.png")
    snakes = Image.open(SNAKES_SRC).convert('RGBA')

    radii = [0, 4, 6, 8, 10]
    keep = _VEC_BLUR
    tiles = []
    for r in radii:
        _VEC_BLUR = float(r)
        tiles.append((r, _vectorize_snakes(snakes, 520)))
    _VEC_BLUR = keep

    tw = max(t.width for _, t in tiles)
    th = max(t.height for _, t in tiles)
    pad = 16
    W = pad + len(tiles) * (tw + pad)
    H = th + 40
    canvas = Image.new('RGB', (W, H), (235, 235, 238))
    d = ImageDraw.Draw(canvas)
    x = pad
    for r, t in tiles:
        canvas.paste(t.convert('RGB'), (x, 30))
        d.text((x + 4, 8), f'blur={r}', fill=(190, 30, 30))
        x += tw + pad
    os.makedirs(BUILD, exist_ok=True)
    out = os.path.join(BUILD, '_vec_candidates.png')
    canvas.save(out)
    print(f"· 已存对比图 {os.path.relpath(out, HERE)}")


def main():
    os.makedirs(ASSETS, exist_ok=True)
    os.makedirs(BUILD, exist_ok=True)

    if '--candidates' in sys.argv:
        dump_radius_candidates()
        return

    only_snakes = '--from-snakes' in sys.argv
    if only_snakes and os.path.isfile(SNAKES_SRC):
        from PIL import Image
        snakes = Image.open(SNAKES_SRC).convert('RGBA')
        print(f"· 沿用 {os.path.relpath(SNAKES_SRC, HERE)}")
    else:
        snakes = extract_snakes()

    print("· 渲染 cpy.ico …")
    cpy = build_cpy_icon(snakes)
    cpy.resize((256, 256), cpy.resample if False else 1).save(
        os.path.join(BUILD, 'icon_cpy_256.png'))
    save_ico(cpy, os.path.join(ASSETS, 'cpy.ico'))

    print("· 渲染 pypp.ico …")
    exe = build_exe_icon(snakes)
    save_ico(exe, os.path.join(ASSETS, 'pypp.ico'))

    # 预览图（放大 3 倍便于查看细节）
    for tag, im in (('cpy', cpy), ('pypp', exe)):
        prev = im.resize((256 * 3, 256 * 3), 1)
        p = os.path.join(ASSETS, f'preview_{tag}.png')
        prev.save(p)
        print(f"· 预览 {os.path.relpath(p, HERE)}")

    print("完成。")


if __name__ == '__main__':
    main()
