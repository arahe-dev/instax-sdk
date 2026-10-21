"""Image preparation, in memory only: nothing here touches the disk."""

from __future__ import annotations

import io

from PIL import Image, ImageDraw, ImageFont, ImageOps


def prepare_jpeg(
    image: Image.Image | bytes,
    width: int,
    height: int,
    max_bytes: int,
    *,
    flip_vertical: bool = False,
    fit: str = "contain",
    background: tuple[int, int, int] = (255, 255, 255),
) -> bytes:
    """Resize to the printer's size and return the best-quality JPEG under max_bytes, without EXIF."""
    img = image if isinstance(image, Image.Image) else Image.open(io.BytesIO(image))
    img = ImageOps.exif_transpose(img).convert("RGB")
    if fit == "cover":
        img = ImageOps.fit(img, (width, height), Image.Resampling.LANCZOS)
    else:
        img = ImageOps.pad(img, (width, height), Image.Resampling.LANCZOS, color=background)
    if flip_vertical:
        img = ImageOps.flip(img)

    best = None
    low, high = 10, 95
    while low <= high:
        quality = (low + high) // 2
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality, optimize=True)
        if buf.tell() <= max_bytes:
            best, low = buf.getvalue(), quality + 1
        else:
            high = quality - 1
    if best is None:
        raise ValueError(f"cannot fit a {width}x{height} JPEG into {max_bytes} bytes")
    return best


def orientation_pattern(width: int = 600, height: int = 800) -> Image.Image:
    """A gradient with an arrow and "TOP" at the top and "L" on the left, to check print orientation."""
    img = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(img)
    for y in range(height):
        shade = round(255 * y / (height - 1))
        draw.line([(0, y), (width, y)], fill=(shade, 128, 255 - shade))
    font = ImageFont.load_default(size=72)
    mid = width // 2
    draw.polygon([(mid, 40), (mid - 70, 150), (mid + 70, 150)], fill="white")
    draw.text((mid, 170), "TOP", fill="white", font=font, anchor="ma")
    draw.text((30, height // 2), "L", fill="white", font=font, anchor="lm")
    return img
