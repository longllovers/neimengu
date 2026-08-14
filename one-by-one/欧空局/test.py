#!/usr/bin/env python3
"""将指定目录中的旧 JPG 缩略图替换为新 JPEG 缩略图。"""

from pathlib import Path

from clip_county_tifs import create_tif_thumbnail


ROOT_DIR = Path(r"E:\city_data\巴彦淖尔市")


def main() -> None:
    if not ROOT_DIR.is_dir():
        raise FileNotFoundError(f"找不到目录：{ROOT_DIR}")

    jpg_paths = sorted(
        path
        for path in ROOT_DIR.rglob("*")
        if path.is_file() and path.suffix.lower() == ".jpg"
    )
    print(f"找到 {len(jpg_paths)} 张旧 JPG，开始生成新 JPEG。")

    replaced = 0
    failed = 0
    for index, jpg_path in enumerate(jpg_paths, 1):
        tif_path = jpg_path.with_suffix(".tif")
        if not tif_path.is_file():
            failed += 1
            print(f"[{index}/{len(jpg_paths)}] 缺少同名 TIFF，保留原 JPG：{jpg_path}")
            continue

        try:
            jpeg_path = create_tif_thumbnail(tif_path)
            if not jpeg_path.is_file() or jpeg_path.stat().st_size == 0:
                raise RuntimeError(f"新 JPEG 未正确生成：{jpeg_path}")
            jpg_path.unlink()
            replaced += 1
            print(f"[{index}/{len(jpg_paths)}] 已替换：{jpg_path.name} -> {jpeg_path.name}")
        except Exception as exc:
            failed += 1
            print(f"[{index}/{len(jpg_paths)}] 替换失败，保留原 JPG：{jpg_path} - {exc}")

    print(f"处理完成：成功替换 {replaced} 张，失败 {failed} 张。")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
