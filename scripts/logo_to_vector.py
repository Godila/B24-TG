#!/usr/bin/env python3
"""
ЧатМост: конвертация лого JPG → PNG (без фона) + SVG (вектор)
Использование: python logo_to_vector.py <input.jpg> [output_dir]
"""

import sys
import os
import base64
from pathlib import Path

from PIL import Image
import numpy as np


def remove_background(input_path: str, output_path: str):
    """Удаляет белый фон, делает прозрачным PNG."""
    from rembg import remove

    print(f"  [1/3] Удаление фона: {input_path}")
    img = Image.open(input_path)
    
    # Конвертируем в RGBA если нужно
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    
    # Удаляем фон через rembg
    result = remove(img)
    
    data = np.array(result)
    r, g, b, a = data.T
    
    # 1. Убираем почти-белые пиксели с низкой альфой (артефакты JPEG по краям)
    white_fringe = (r > 240) & (g > 240) & (b > 240) & (a < 50)
    data[..., 3][white_fringe.T] = 0
    
    # 2. Удаляем белый фон ВНУТРИ лого (арки моста, низ пузыря — это дырки!)
    # Белые пиксели которые полностью непрозрачны → делаем прозрачными
    white_inside = (r > 225) & (g > 225) & (b > 225) & (a > 200)
    data[..., 3][white_inside.T] = 0
    
    result = Image.fromarray(data)
    result.save(output_path, "PNG")
    print(f"  ✓ Сохранено: {output_path} ({result.size[0]}×{result.size[1]})")
    return result


def png_to_svg(png_path: str, svg_path: str, color_mode="color"):
    """Конвертирует PNG в SVG через трассировку контуров."""
    try:
        import vtracer
        print(f"  [2/3] Векторизация (vtracer): {png_path}")
        
        svg_str = vtracer.convert_image_to_svg(
            png_path,
            filter_radius=2.0,      # сглаживание
            color_precision=6,       # точность цвета
            hierarchy_threshold=128,  # иерархия цветов
            mode=color_mode,
            max_colors=32,            # макс. цветов для градиента
            stroke_width=0,           # без обводки
            path_simplify=True,       # упрощение путей
        )
        
        with open(svg_path, "w", encoding="utf-8") as f:
            f.write(svg_str)
        print(f"  ✓ Сохранено: {svg_path}")
        
    except ImportError:
        # Fallback: SVG с embedded PNG + чистый viewBox
        print(f"  [2/3] vtracer недоступен → создаю SVG с embedded изображением")
        
        img = Image.open(png_path)
        width, height = img.size
        
        # Кодируем PNG в base64 для inline SVG
        with open(png_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        
        svg_content = f'''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"
     viewBox="0 0 {width} {height}" width="{width}" height="{height}">
  <title>ChatMost Logo</title>
  <image xlink:href="data:image/png;base64,{b64}"
         x="0" y="0" width="{width}" height="{height}" />
</svg>'''
        
        with open(svg_path, "w", encoding="utf-8") as f:
            f.write(svg_content)
        print(f"  ✓ Сохранено: {svg_path} (embedded raster)")


def create_favicon(png_path: str, sizes=[16, 32, 48, 64, 128, 180, 192, 512]):
    """Создаёт favicon.ico и PNG разных размеров С СОХРАНЕНИЕМ ПРОПОРЦИЙ."""
    print(f"  [3/3] Генерация favicon и размеров...")
    
    img = Image.open(png_path).convert("RGBA")
    orig_w, orig_h = img.size
    
    # Favicon .ico — с letterboxing (прозрачные поля по бокам)
    ico_path = str(Path(png_path).parent / "favicon.ico")
    # Для ICO берём наибольший из стандартных
    ico_sizes = []
    for s in [16, 32, 48]:
        padded = _resize_with_padding(img, s, s)
        ico_sizes.append((padded, (s, s)))
    # PIL ICO save expects image + list of (width, height) tuples
    ico_img = _resize_with_padding(img, 48, 48)
    ico_img.save(ico_path, format="ICO", sizes=[(s, s) for s in [16, 32, 48]])
    print(f"  ✓ {ico_path}")
    
    # Отдельные PNG — сохраняем пропорции, добавляем прозрачность
    for size in sizes:
        resized = _resize_with_proportions(img, size)
        out_name = f"logo-{resized.size[0]}x{resized.size[1]}.png"
        out_path = str(Path(png_path).parent / out_name)
        resized.save(out_path, "PNG")
    
    # Также квадратные варианты для мест где нужен квадрат (apple-touch-icon etc.)
    for size in [180, 192, 512]:
        padded = _resize_with_padding(img, size, size)
        out_name = f"logo-square-{size}x{size}.png"
        out_path = str(Path(png_path).parent / out_name)
        padded.save(out_path, "PNG")
    
    print(f"  ✓ PNG (с пропорциями): logo-WxH.png для каждого размера")
    print(f"  ✓ PNG (квадратные): logo-square-SxS.png с прозрачными полями")


def _resize_with_proportions(img: Image.Image, max_size: int) -> Image.Image:
    """Ресайзит сохраняя пропорции, fitting в max_size."""
    orig_w, orig_h = img.size
    ratio = min(max_size / orig_w, max_size / orig_h)
    new_w = int(orig_w * ratio)
    new_h = int(orig_h * ratio)
    return img.resize((new_w, new_h), Image.Resampling.LANCZOS)


def _resize_with_padding(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    """Ресайзит с сохранением пропорций + прозрачный padding до целевого размера."""
    resized = _resize_with_proportions(img, min(target_w, target_h))
    result = Image.new("RGBA", (target_w, target_h), (0, 0, 0, 0))
    offset_x = (target_w - resized.size[0]) // 2
    offset_y = (target_h - resized.size[1]) // 2
    result.paste(resized, (offset_x, offset_y), resized)
    return result


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        print(f"Пример: python {sys.argv[0]} logo.jpg ./output/")
        sys.exit(1)
    
    input_file = Path(sys.argv[1])
    if not input_file.exists():
        print(f"Ошибка: файл не найден: {input_file}")
        sys.exit(1)
    
    output_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else input_file.parent
    output_dir.mkdir(parents=True, exist_ok=True)
    
    stem = input_file.stem
    
    print(f"🌉 ЧатМост — обработка лого")
    print(f"   Вход:  {input_file.name}")
    print(f"   Выход: {output_dir}/")
    print()
    
    # Шаг 1: PNG без фона
    png_path = output_dir / f"{stem}-no-bg.png"
    remove_background(str(input_file), str(png_path))
    
    # Шаг 2: SVG вектор
    svg_path = output_dir / f"{stem}.svg"
    png_to_svg(str(png_path), str(svg_path))
    
    # Шаг 3: Favicon + размеры
    create_favicon(str(png_path))
    
    print()
    print("=" * 50)
    print("✅ Готово! Результаты:")
    print(f"   📄 PNG (прозрачный): {png_path.name}")
    print(f"   📐 Вектор:          {svg_path.name}")
    print(f"   🔖 Favicon:         favicon.ico")
    print()
    print("💡 Совет:")
    print("   Для идеального SVG открой logo-no-bg.png в Illustrator/Inkscape")
    print("   и используйте Image Trace (Shift+Alt+Ctrl+B)")
    print("=" * 50)


if __name__ == "__main__":
    main()
