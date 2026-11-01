"""Build app/static/icon.ico (desktop shortcut icon) from app/static/icon.svg.

Renders the SVG with headless Edge at the usual Windows icon sizes and packs the PNGs into one .ico
(PNG-compressed entries, supported since Windows Vista). usage: python tools/make_icon.py
"""
import base64
import pathlib
import struct

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent
SVG = ROOT / "app" / "static" / "icon.svg"
ICO = ROOT / "app" / "static" / "icon.ico"
SIZES = (16, 24, 32, 48, 64, 128, 256)


def main():
    src = "data:image/svg+xml;base64," + base64.b64encode(SVG.read_bytes()).decode()
    images = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page()
        for size in SIZES:
            page.set_viewport_size({"width": size, "height": size})
            page.set_content(f'<html><body style="margin:0;background:transparent">'
                             f'<img src="{src}" width="{size}" height="{size}" style="display:block"></body></html>')
            page.wait_for_function("document.images[0].complete")
            images.append(page.screenshot(omit_background=True, clip={"x": 0, "y": 0, "width": size, "height": size}))
        browser.close()
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries, data = b"", b""
    for size, png in zip(SIZES, images):
        dim = 0 if size >= 256 else size   # 0 means 256 in the ICO directory
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset + len(data))
        data += png
    ICO.write_bytes(header + entries + data)
    print(f"{ICO} ({len(images)} sizes, {ICO.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
