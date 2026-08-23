#!/usr/bin/env python3
"""
make_icon.py — Generate icon.png and icon.ico for GFA Detokenizer.

Simple rounded-square icon: dark slate-blue background (matching the app's
own PySimpleGUI window colors) with bold white "GFA" text.

Run:  python make_icon.py
Output: icon.png (1024x1024) and icon.ico (multi-size) in the current directory.
"""

from PIL import Image, ImageDraw, ImageFont
import struct
import io

SIZE = 1024
RADIUS = 200

BG_COLOR = (49, 61, 78, 255)       # dark slate blue, matches the app window
TEXT_COLOR = (235, 240, 250, 255)  # near-white
ACCENT_COLOR = (120, 180, 230, 255)  # light blue, matches the subtitle text


def make_rounded_mask(size, radius):
    mask = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return mask


def load_font(size):
    for name in ("arialbd.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def main():
    canvas = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    layer = Image.new("RGBA", (SIZE, SIZE), BG_COLOR)
    draw = ImageDraw.Draw(layer)

    # Thin accent underline below the text, nodding to a floppy-disk /
    # terminal-cursor feel without overcomplicating the design.
    text = "GFA"
    font = load_font(440)
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    tx = SIZE // 2 - tw // 2 - bbox[0]
    ty = SIZE // 2 - th // 2 - bbox[1] - 30

    # Drop shadow
    draw.text((tx + 8, ty + 8), text, font=font, fill=(0, 0, 0, 120))
    # Main text
    draw.text((tx, ty), text, font=font, fill=TEXT_COLOR)

    underline_y = ty + th + 70
    underline_w = tw * 0.7
    draw.rounded_rectangle(
        [SIZE // 2 - underline_w / 2, underline_y,
         SIZE // 2 + underline_w / 2, underline_y + 22],
        radius=11, fill=ACCENT_COLOR,
    )

    mask = make_rounded_mask(SIZE, RADIUS)
    canvas.paste(layer, mask=mask)

    png_path = "icon.png"
    canvas.save(png_path)
    print(f"Saved {png_path}")

    build_ico(png_path, "icon.ico")
    print("Saved icon.ico")


def build_ico(png_path, ico_path):
    sizes = [256, 128, 64, 48, 32, 16]
    images = []
    src = Image.open(png_path).convert("RGBA")
    for s in sizes:
        img = src.resize((s, s), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        images.append(buf.getvalue())

    count = len(images)
    header = struct.pack("<HHH", 0, 1, count)
    offset = 6 + count * 16
    directory = b""
    for i, data in enumerate(images):
        s = sizes[i]
        w = s if s < 256 else 0
        h = s if s < 256 else 0
        directory += struct.pack(
            "<BBBBHHII", w, h, 0, 0, 1, 32, len(data), offset
        )
        offset += len(data)

    with open(ico_path, "wb") as f:
        f.write(header)
        f.write(directory)
        for data in images:
            f.write(data)


if __name__ == "__main__":
    main()
