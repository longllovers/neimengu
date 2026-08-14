#!/usr/bin/env python3
"""
Sentinel-1/2 数据自动下载脚本
支持：从 account.json 登录、批量搜索、并发下载、token 过期自动刷新
"""

import argparse
import hashlib
import json
import logging
import math
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import warnings
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed

from file_write_guard import output_file_lock, temporary_output_path
from pathlib import Path

import requests
import geopandas as gpd
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential
from shapely.geometry import box


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("download.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)
console = Console()


CITY_LAYER = Path("./00市边界/15_市边界.shp")
COUNTY_LAYER = Path("./00县边界/15_县边界.shp")
CITY_NAME_FIELD = "市名称"
COUNTY_NAME_FIELD = "area_name"
TEN_M_BANDS = ("B02", "B03", "B04", "B08")
THREADS_PER_ACCOUNT = 3
MAX_ACTIVE_ACCOUNTS = 5
CLIENT_USER_AGENT = "SentinelImageryDownloader/1.0"
CLIENT_ACCEPT = "application/octet-stream, application/json;q=0.9, */*;q=0.8"


class DownloadProxyPool:
    """从 HTTP 代理池按影像分配代理，并在一轮后顺序复用。"""

    def __init__(self, enabled=False, random_url=None, count_url=None):
        self.enabled = bool(enabled)
        self.random_url = str(random_url or "").strip()
        self.count_url = str(count_url or "").strip()
        self.proxy_count = 0
        self.proxies = []
        self.next_index = 0
        self.lock = threading.Lock()

        if self.enabled:
            self.proxy_count = self._fetch_count()

    @staticmethod
    def _local_request_kwargs():
        return {
            "timeout": 10,
            "proxies": {"http": "", "https": ""},
        }

    def _fetch_count(self):
        response = requests.get(self.count_url, **self._local_request_kwargs())
        response.raise_for_status()
        try:
            payload = response.json()
        except requests.exceptions.JSONDecodeError:
            payload = response.text.strip()

        if isinstance(payload, dict):
            payload = payload.get("count", payload.get("total"))
        try:
            count = int(payload)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"代理池 count 接口返回无效：{payload!r}") from exc
        if count < 1:
            raise ValueError("代理池当前没有可用代理")
        return count

    def _fetch_random_proxy(self):
        response = requests.get(self.random_url, **self._local_request_kwargs())
        response.raise_for_status()
        try:
            payload = response.json()
        except requests.exceptions.JSONDecodeError:
            payload = response.text.strip()

        if isinstance(payload, dict):
            payload = payload.get("proxy") or payload.get("ip") or payload.get("data")
        proxy = str(payload or "").strip().strip('"')
        if not proxy:
            raise ValueError("代理池 random 接口未返回代理地址")
        if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", proxy):
            proxy = f"http://{proxy}"
        return proxy

    def acquire(self):
        if not self.enabled:
            return None

        with self.lock:
            if self.next_index < len(self.proxies):
                proxy = self.proxies[self.next_index]
            elif len(self.proxies) < self.proxy_count:
                proxy = self._fetch_random_proxy()
                self.proxies.append(proxy)
            else:
                self.next_index = 0
                proxy = self.proxies[0]

            self.next_index += 1
            return proxy

    def acquire_different(self, current_proxy):
        """为失败影像切换代理，尽量避免再次分配到当前地址。"""
        if not self.enabled:
            return None

        candidate = current_proxy
        attempts = min(max(self.proxy_count, 1), 10)
        for _ in range(attempts):
            candidate = self.acquire()
            if candidate != current_proxy:
                return candidate
        return candidate


def require_rasterio():
    try:
        import rasterio
    except ImportError:
        console.print("[bold red]缺少依赖 rasterio，无法写带坐标的 GeoTIFF。[/bold red]")
        console.print("请先运行：uv add rasterio")
        sys.exit(1)
    warnings.filterwarnings(
        "ignore",
        message="Setting the shape on a NumPy array has been deprecated.*",
        category=DeprecationWarning,
    )
    return rasterio


def find_band_members(zip_path):
    band_members = {}
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.namelist():
            normalized = member.replace("\\", "/")
            if "/IMG_DATA/R10m/" not in normalized or not normalized.lower().endswith(".jp2"):
                continue
            for band in TEN_M_BANDS:
                if f"_{band}_10m.jp2" in normalized:
                    band_members[band] = member

    missing = [band for band in TEN_M_BANDS if band not in band_members]
    if missing:
        raise FileNotFoundError(f"缺少 10m 波段：{', '.join(missing)}")
    return band_members


def extract_members(zip_path, band_members, temp_dir):
    extracted = {}
    with zipfile.ZipFile(zip_path) as zf:
        for band, member in band_members.items():
            output_path = Path(temp_dir) / Path(member).name
            with zf.open(member) as src, open(output_path, "wb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
            extracted[band] = output_path
    return extracted


def tif_output_path(zip_path, output_dir):
    name = Path(zip_path).name
    if name.endswith(".SAFE.zip"):
        tif_name = name[: -len(".SAFE.zip")] + ".tif"
    else:
        tif_name = Path(zip_path).stem + ".tif"
    return Path(output_dir) / tif_name


def convert_zip_to_tif(zip_path, output_path, overwrite=False):
    rasterio = require_rasterio()
    zip_path = Path(zip_path)
    output_path = Path(output_path)

    with output_file_lock(output_path):
        if output_path.exists() and not overwrite:
            return "skipped", output_path

        temporary_path = temporary_output_path(output_path)
        try:
            with tempfile.TemporaryDirectory(prefix="s2_10m_") as temp_dir:
                band_members = find_band_members(zip_path)
                extracted = extract_members(zip_path, band_members, temp_dir)

                with rasterio.Env(GDAL_PAM_ENABLED="NO"):
                    with rasterio.open(extracted["B02"]) as first:
                        profile = first.profile.copy()
                        profile.update(
                            driver="GTiff",
                            count=4,
                            compress="deflate",
                            predictor=2,
                            tiled=True,
                            BIGTIFF="IF_SAFER",
                        )

                        with rasterio.open(temporary_path, "w", **profile) as dst:
                            for band_index, band in enumerate(TEN_M_BANDS, 1):
                                with rasterio.open(extracted[band]) as src:
                                    if src.width != first.width or src.height != first.height:
                                        raise ValueError(f"{band} 尺寸与 B02 不一致")
                                    dst.write(src.read(1), band_index)
                                    dst.set_band_description(band_index, band)
            os.replace(temporary_path, output_path)
        finally:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass

        return "created", output_path


def finalize_downloaded_product(
    output_file,
    final_tif,
    marker_file,
    scene_label="",
    file_id=None,
    quiet=False,
):
    """把已完整下载的 ZIP 转为 TIFF，并清理 ZIP 与完成标记。"""
    output_file = Path(output_file)
    final_tif = Path(final_tif)
    marker_file = Path(marker_file)
    file_id = file_id or output_file.stem

    if final_tif.exists():
        for path in (output_file, marker_file):
            try:
                path.unlink()
            except OSError:
                pass
        return "skipped", final_tif

    if not quiet:
        console.print(f"[green]{scene_label}🧩 开始抽取 10m tif：{file_id}[/green]")
    convert_zip_to_tif(output_file, final_tif)
    for path in (output_file, marker_file):
        try:
            path.unlink()
        except OSError:
            pass
    if not quiet:
        console.print(f"[green]{scene_label}✅ tif 生成完成：{final_tif}[/green]")
    return "success", final_tif


def reset_temp_dir(temp_root="./temp_data"):
    """确保临时目录存在；保留 .part 文件以支持跨断点续传。"""
    temp_path = Path(temp_root)
    temp_path.mkdir(parents=True, exist_ok=True)
    return temp_path


def build_search_cache_entry(
    city,
    collection,
    start_date,
    end_date,
    geometry,
    contains,
    cloud_cover,
    tile_ids,
    limit,
):
    geometry_wkb = getattr(geometry, "wkb_hex", None)
    query = {
        "city": city,
        "collection": collection,
        "start_date": start_date,
        "end_date": end_date,
        "geometry_sha256": hashlib.sha256(
            str(geometry_wkb or "").encode("utf-8")
        ).hexdigest(),
        "contains": contains,
        "cloud_cover": cloud_cover,
        "tile_ids": sorted(tile_ids or []),
        "limit": limit,
    }
    cache_key = hashlib.sha256(
        json.dumps(query, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return cache_key, query


def load_cached_products(cache_file, cache_key):
    cache_path = Path(cache_file)
    if not cache_path.exists():
        return None
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("检索缓存读取失败，将重新检索：%s", exc)
        return None
    if not isinstance(data, dict):
        return None

    entry = (data.get("searches") or {}).get(cache_key)
    if not isinstance(entry, dict) or not isinstance(entry.get("products"), list):
        return None
    return entry["products"]


def save_cached_products(cache_file, cache_key, query, products):
    cache_path = Path(cache_file)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}

    cached_products = []
    for product in products:
        product_id = product.get("Id") or product.get("id")
        if not product_id:
            continue
        props = product.get("properties") or {}
        display_name = (
            product.get("Name")
            or props.get("title")
            or props.get("productIdentifier")
            or product_id
        )
        cached_products.append(
            {
                "Id": product_id,
                "Name": display_name,
                "DownloadUrl": (
                    "https://download.dataspace.copernicus.eu/odata/v1/"
                    f"Products({product_id})/$value"
                ),
            }
        )

    data["version"] = 1
    data.setdefault("searches", {})[cache_key] = {
        "query": query,
        "products": cached_products,
    }
    temp_path = cache_path.with_suffix(cache_path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temp_path, cache_path)


def normalize_region_name(name):
    text = str(name).strip()
    for suffix in ("市", "盟", "地区", "自治州"):
        if text.endswith(suffix):
            return text[: -len(suffix)]
    return text


def load_config(config_file="config.json"):
    """读取多账号和城市配置。"""
    config_path = Path(config_file)
    if not config_path.exists():
        raise FileNotFoundError(f"未找到配置文件：{config_path}")

    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"{config_path} 不是合法 JSON：{e}") from e

    accounts = data.get("accounts") or data.get("accounts") or []
    city_entries = data.get("city") or data.get("cities") or []

    valid_accounts = []
    for idx, account in enumerate(accounts, 1):
        username = account.get("username") or account.get("email") or account.get("user")
        password = account.get("password") or account.get("pass")
        if username and password:
            valid_accounts.append({
                "id": account.get("id", idx),
                "username": username.strip(),
                "password": password.strip(),
            })

    if not valid_accounts:
        raise ValueError("config.json 需要包含 accounts/accounts，且每个账号有 username 和 password")

    cities = []
    date_ranges_by_city = {}
    for entry in city_entries:
        if isinstance(entry, str):
            city_name = entry.strip()
            raw_ranges = []
        elif isinstance(entry, dict):
            city_name = str(entry.get("name") or entry.get("city") or "").strip()
            raw_ranges = entry.get("date_ranges") or []
        else:
            continue

        if not city_name:
            continue
        cities.append(city_name)

        valid_ranges = []
        for date_range in raw_ranges:
            if not isinstance(date_range, dict):
                raise ValueError(f"{city_name} 的 date_ranges 项必须是对象")
            start_date = str(date_range.get("start_date") or "").strip()
            end_date = str(date_range.get("end_date") or "").strip()
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", start_date) or not re.fullmatch(
                r"\d{4}-\d{2}-\d{2}", end_date
            ):
                raise ValueError(f"{city_name} 的下载时间必须使用 YYYY-MM-DD 格式")
            if start_date > end_date:
                raise ValueError(f"{city_name} 的开始日期不能晚于结束日期")
            valid_ranges.append({"start_date": start_date, "end_date": end_date})
        if valid_ranges:
            date_ranges_by_city[normalize_region_name(city_name)] = valid_ranges

    if not cities:
        raise ValueError("config.json 需要包含 city/cities 城市列表")

    download_settings = {
        "start_date": data.get("start_date"),
        "end_date": data.get("end_date"),
        "output_dir": data.get("output_dir"),
        "temp_dir": data.get("temp_dir"),
        "search_cache_file": data.get("search_cache_file", "./search_results.json"),
        "date_ranges_by_city": date_ranges_by_city,
        "max_active_accounts": data.get("max_active_accounts", MAX_ACTIVE_ACCOUNTS),
        "cloud_cover": data.get("cloud_cover"),
        "max_workers": data.get("max_workers"),
        "processing_workers": data.get("processing_workers", 1),
        "tif_extraction_workers": data.get("tif_extraction_workers", 3),
        "city_output_dir": data.get("city_output_dir", "./city_data"),
        "country_output_dir": data.get("country_output_dir", "./coutry_data"),
        "bypass_proxy": data.get("bypass_proxy", False),
        "proxy_pool": data.get("proxy_pool") or {},
    }

    try:
        download_settings["max_active_accounts"] = int(
            download_settings["max_active_accounts"]
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("max_active_accounts 必须是整数") from exc
    if not 1 <= download_settings["max_active_accounts"] <= MAX_ACTIVE_ACCOUNTS:
        raise ValueError(
            f"max_active_accounts 必须在 1 到 {MAX_ACTIVE_ACCOUNTS} 之间"
        )

    if download_settings["cloud_cover"] is not None:
        try:
            download_settings["cloud_cover"] = float(download_settings["cloud_cover"])
        except (TypeError, ValueError) as exc:
            raise ValueError("cloud_cover 必须是数字") from exc
        if not 0 <= download_settings["cloud_cover"] <= 100:
            raise ValueError("cloud_cover 必须在 0 到 100 之间")

    if download_settings["max_workers"] is not None:
        try:
            download_settings["max_workers"] = int(download_settings["max_workers"])
        except (TypeError, ValueError) as exc:
            raise ValueError("max_workers 必须是整数") from exc
        if download_settings["max_workers"] < 1:
            raise ValueError("max_workers 必须大于或等于 1")

    try:
        download_settings["processing_workers"] = int(
            download_settings["processing_workers"]
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("processing_workers 必须是整数") from exc
    if download_settings["processing_workers"] < 1:
        raise ValueError("processing_workers 必须大于或等于 1")
    try:
        download_settings["tif_extraction_workers"] = int(
            download_settings["tif_extraction_workers"]
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("tif_extraction_workers 必须是整数") from exc
    if download_settings["tif_extraction_workers"] < 1:
        raise ValueError("tif_extraction_workers 必须大于或等于 1")
    if not isinstance(download_settings["bypass_proxy"], bool):
        raise ValueError("bypass_proxy 必须是 true 或 false")
    proxy_pool = download_settings["proxy_pool"]
    if not isinstance(proxy_pool, dict):
        raise ValueError("proxy_pool 必须是对象")
    proxy_pool.setdefault("enabled", False)
    proxy_pool.setdefault("random_url", "http://localhost:5555/random")
    proxy_pool.setdefault("count_url", "http://localhost:5555/count")
    if not isinstance(proxy_pool["enabled"], bool):
        raise ValueError("proxy_pool.enabled 必须是 true 或 false")
    if proxy_pool["enabled"]:
        if not str(proxy_pool["random_url"] or "").strip():
            raise ValueError("proxy_pool.random_url 不能为空")
        if not str(proxy_pool["count_url"] or "").strip():
            raise ValueError("proxy_pool.count_url 不能为空")
    if not str(download_settings["city_output_dir"] or "").strip():
        raise ValueError("city_output_dir 不能为空")
    if not str(download_settings["country_output_dir"] or "").strip():
        raise ValueError("country_output_dir 不能为空")
    if not str(download_settings["search_cache_file"] or "").strip():
        raise ValueError("search_cache_file 不能为空")

    return valid_accounts, cities, download_settings


def load_credentials(account_file="account.json"):
    """从 account.json 读取账号密码。"""
    account_path = Path(account_file)
    if not account_path.exists():
        raise FileNotFoundError(f"未找到账号文件：{account_path}")

    try:
        data = json.loads(account_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"{account_path} 不是合法 JSON：{e}") from e

    username = data.get("username") or data.get("email") or data.get("user")
    password = data.get("password") or data.get("pass")

    if not username or not password:
        raise ValueError("account.json 需要包含 username 和 password 字段")

    return username.strip(), password.strip()


def read_boundary_layer(path):
    if not Path(path).exists():
        raise FileNotFoundError(f"边界文件不存在：{path}")
    gdf = gpd.read_file(path)
    if gdf.empty:
        raise ValueError(f"边界文件为空：{path}")
    if gdf.crs and not gdf.crs.equals("EPSG:4326"):
        gdf = gdf.to_crs(epsg=4326)
    elif not gdf.crs:
        gdf = gdf.set_crs(epsg=4326)
    return gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty].reset_index(drop=True)


def find_boundary_row(gdf, name, name_field, layer_label):
    if name_field not in gdf.columns:
        raise ValueError(f"{layer_label}边界缺少字段：{name_field}")

    wanted = normalize_region_name(name)
    names = gdf[name_field].fillna("").astype(str)
    normalized = names.map(normalize_region_name)
    exact = gdf[normalized == wanted]
    if not exact.empty:
        return exact.iloc[[0]].copy()

    fuzzy = gdf[names.str.contains(name, regex=False) | normalized.str.contains(wanted, regex=False)]
    if not fuzzy.empty:
        return fuzzy.iloc[[0]].copy()

    return None


def write_shapefile(gdf, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if gdf.empty:
        raise ValueError(f"没有可写入的要素：{path}")
    for sidecar in path.parent.glob(f"{path.stem}.*"):
        if sidecar.suffix.lower() in {".shp", ".shx", ".dbf", ".prj", ".cpg", ".qmd", ".sbn", ".sbx"}:
            sidecar.unlink()
    gdf.to_file(path, encoding="utf-8")
    return path


def keep_polygonal(gdf):
    if gdf.empty:
        return gdf
    return gdf[gdf.geometry.geom_type.isin(["Polygon", "MultiPolygon"])].copy()


def remove_stale_county_boundary(region_dir, saved_name):
    """城市任务不需要县边界，清理旧版本自动生成的县边界文件。"""
    for sidecar in Path(region_dir).glob(f"{saved_name}_县边界.*"):
        if sidecar.suffix.lower() in {".shp", ".shx", ".dbf", ".prj", ".cpg", ".qmd", ".sbn", ".sbx"}:
            sidecar.unlink()


def prepare_region_shapefiles(regions, shp_root="./shp", city_layer=CITY_LAYER, county_layer=COUNTY_LAYER):
    """按 config 名称列表生成下载边界：城市取市边界，县/旗/区取县边界。"""
    city_gdf = read_boundary_layer(city_layer)
    county_gdf = read_boundary_layer(county_layer)
    generated = []

    for region_name in regions:
        saved_name = safe_filename(region_name)
        region_dir = Path(shp_root) / saved_name
        region_shp = region_dir / f"{saved_name}.shp"

        boundary = find_boundary_row(city_gdf, region_name, CITY_NAME_FIELD, "市")
        level = "市"
        name_field = CITY_NAME_FIELD

        if boundary is None:
            boundary = find_boundary_row(county_gdf, region_name, COUNTY_NAME_FIELD, "县")
            level = "县"
            name_field = COUNTY_NAME_FIELD

        if boundary is None:
            raise ValueError(f"未在市边界或县边界中找到：{region_name}")

        boundary = boundary.copy()
        boundary[name_field] = region_name
        write_shapefile(boundary, region_shp)
        if level == "市":
            remove_stale_county_boundary(region_dir, saved_name)

        generated.append({"city": region_name, "roi_file": str(region_shp), "level": level})
        console.print(f"[green]✅ 已生成{level}级边界：{region_shp}[/green]")

    return generated


def load_roi_geometry(roi_file=None, bbox=None):
    """读取 shp/geojson/bbox，返回 EPSG:4326 shapely geometry。"""
    if roi_file:
        gdf = gpd.read_file(roi_file)
        if gdf.empty:
            raise ValueError(f"区域文件为空：{roi_file}")
        if gdf.crs and not gdf.crs.equals("EPSG:4326"):
            gdf = gdf.to_crs(epsg=4326)
        elif not gdf.crs:
            gdf = gdf.set_crs(epsg=4326)

        return gdf.union_all() if hasattr(gdf, "union_all") else gdf.unary_union

    if bbox:
        min_lon, min_lat, max_lon, max_lat = bbox
        return box(min_lon, min_lat, max_lon, max_lat)

    return None


def detect_roi_name(roi_file=None, bbox=None):
    """根据区域文件或 bbox 生成输出子文件夹名称。"""
    if bbox:
        values = [str(value).replace(".", "p").replace("-", "m") for value in bbox]
        return safe_filename("bbox_" + "_".join(values))

    if not roi_file:
        return "default"

    roi_path = Path(roi_file)
    try:
        gdf = gpd.read_file(roi_path)
        for field in (
            "area_name",
            "name",
            "Name",
            "NAME",
            "名称",
            "市名称",
            "县名称",
            "区名称",
            "旗名称",
            "盟名称",
            "省名称",
        ):
            if field in gdf.columns:
                values = [str(value).strip() for value in gdf[field].dropna().tolist()]
                values = [value for value in values if value and value.lower() != "none"]
                if values:
                    return safe_filename(values[0])
    except Exception as e:
        logger.warning("读取区域名称失败，将使用文件名：%s", e)

    return safe_filename(roi_path.stem)


def build_output_dir(output_root, roi_file=None, bbox=None):
    return os.path.join(output_root, detect_roi_name(roi_file=roi_file, bbox=bbox))


def make_polygon_for_odata(geom, max_vertices=50):
    """将 ROI 几何简化为 OData 可接受的 Polygon。"""
    if geom is None or geom.is_empty:
        return None

    if geom.geom_type != "Polygon":
        geom = geom.convex_hull

    if geom.geom_type != "Polygon":
        geom = box(*geom.bounds)

    tolerance = 0.001
    simplified = geom

    while len(list(simplified.exterior.coords)) > max_vertices:
        simplified = geom.simplify(tolerance, preserve_topology=True)
        tolerance *= 2

        if simplified.is_empty:
            simplified = geom
            break

        if simplified.geom_type != "Polygon":
            simplified = simplified.convex_hull

        if simplified.geom_type != "Polygon":
            simplified = box(*geom.bounds)
            break

        if tolerance > 1:
            break

    if simplified.geom_type != "Polygon" or len(list(simplified.exterior.coords)) > max_vertices:
        simplified = box(*geom.bounds)

    return simplified


def odata_polygon_filter(geom):
    polygon = make_polygon_for_odata(geom)
    if polygon is None:
        return None

    coordinates = list(polygon.exterior.coords)
    coordinates_str = ", ".join(f"{x} {y}" for x, y in coordinates)
    console.print(f"[bold blue]📍 检索多边形顶点数：{len(coordinates)}[/bold blue]")
    return f"OData.CSC.Intersects(area=geography'SRID=4326;POLYGON(({coordinates_str}))')"


def collection_to_odata(collection):
    normalized = collection.lower()
    mapping = {
        "sentinel-1": ("SENTINEL-1", ""),
        "sentinel-1-grd": ("SENTINEL-1", "GRD"),
        "sentinel-1-slc": ("SENTINEL-1", "SLC"),
        "sentinel-2": ("SENTINEL-2", ""),
        "sentinel-2-l1c": ("SENTINEL-2", "L1C"),
        "sentinel-2-l2a": ("SENTINEL-2", "L2A"),
    }
    return mapping.get(normalized, (collection.upper(), ""))


def get_tile_id(product):
    try:
        for attr in product.get("Attributes", []):
            if attr.get("Name") == "tileId":
                return attr.get("Value", "未知")
    except Exception:
        pass

    match = re.search(r"_T(\d{2}[A-Z]{3})_", product.get("Name", ""))
    return match.group(1) if match else "未知"


def safe_filename(name):
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in name)


class CDSEClient:
    """Copernicus Data Space 客户端。"""

    def __init__(
        self,
        username,
        password,
        account_id=None,
        token_refresh_margin=300,
        bypass_proxy=False,
    ):
        self.account_id = account_id
        self.username = username
        self.password = password
        self.token = None
        self.token_created_at = 0
        self.token_ttl = 55 * 60
        self.token_refresh_margin = token_refresh_margin
        self.bypass_proxy = bool(bypass_proxy)
        self.token_lock = threading.Lock()
        self.base_url = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE"

    def proxy_kwargs(self):
        """按配置禁用 requests 从环境变量继承的 HTTP(S) 代理。"""
        if not self.bypass_proxy:
            return {}
        return {"proxies": {"http": "", "https": ""}}

    def login(self, force_refresh=False):
        """获取或刷新 token。多线程下同一时间只允许一个线程刷新。"""
        with self.token_lock:
            token_valid = (
                self.token
                and not force_refresh
                and time.time() - self.token_created_at < self.token_ttl - self.token_refresh_margin
            )
            if token_valid:
                return self.token

            action = "刷新" if self.token else "登录"
            account_label = f"账号 {self.account_id}" if self.account_id is not None else self.username
            console.print(f"[bold green]🔐 正在{action} Copernicus Data Space token（{account_label}）...[/bold green]")

            token_url = f"{self.base_url}/protocol/openid-connect/token"
            response = requests.post(
                token_url,
                headers={
                    "User-Agent": CLIENT_USER_AGENT,
                    "Accept": "application/json",
                },
                data={
                    "grant_type": "password",
                    "username": self.username,
                    "password": self.password,
                    "client_id": "cdse-public",
                },
                timeout=60,
                **self.proxy_kwargs(),
            )

            if response.status_code != 200:
                raise RuntimeError(f"token 获取失败：{response.status_code} {response.text[:500]}")

            self.token = response.json()["access_token"]
            self.token_created_at = time.time()
            console.print(f"[bold green]✅ token 获取成功，长度：{len(self.token)}[/bold green]")
            return self.token

    def auth_headers(self, force_refresh=False):
        token = self.login(force_refresh=force_refresh)
        return {
            "Authorization": f"Bearer {token}",
            "User-Agent": CLIENT_USER_AGENT,
            "Accept": CLIENT_ACCEPT,
        }

    def request_with_token_refresh(self, method, url, **kwargs):
        """请求遇到 401 时强制刷新 token 并重试一次。"""
        headers = kwargs.pop("headers", {})
        for key, value in self.proxy_kwargs().items():
            kwargs.setdefault(key, value)
        merged_headers = {**self.auth_headers(), **headers}
        response = requests.request(method, url, headers=merged_headers, **kwargs)

        if response.status_code == 401:
            logger.warning("请求返回 401，刷新 token 后重试：%s", url)
            try:
                response.close()
            except Exception:
                pass
            merged_headers = {**self.auth_headers(force_refresh=True), **headers}
            response = requests.request(method, url, headers=merged_headers, **kwargs)

        return response

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        retry=retry_if_exception_type((requests.exceptions.ConnectionError, requests.exceptions.Timeout)),
    )
    def search_products(
        self,
        collection="sentinel-2-l2a",
        start_date="2025-06-01",
        end_date="2025-07-01",
        limit=0,
        geometry=None,
        contains=None,
        cloud_cover=None,
        tile_ids=None,
    ):
        """使用 OData 搜索遥感数据。"""
        satellite, default_contains = collection_to_odata(collection)
        contains = contains if contains is not None else default_contains
        console.print(f"[bold blue]🔍 正在用 OData 搜索 {satellite} 数据...[/bold blue]")

        filter_parts = []
        if contains:
            filter_parts.append(f"contains(Name,'{contains}')")

        filter_parts.append(f"Collection/Name eq '{satellite}'")

        roi_filter = odata_polygon_filter(geometry)
        if roi_filter:
            filter_parts.append(roi_filter)

        if satellite == "SENTINEL-2" and cloud_cover is not None:
            filter_parts.append(
                "Attributes/OData.CSC.DoubleAttribute/any("
                "att:att/Name eq 'cloudCover' "
                f"and att/OData.CSC.DoubleAttribute/Value le {cloud_cover})"
            )

        filter_parts.append(
            f"ContentDate/Start ge {start_date}T00:00:00.000Z "
            f"and ContentDate/Start le {end_date}T23:59:59.999Z"
        )

        base_url = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products?$filter="
        filter_text = " and ".join(filter_parts)
        all_products = []
        page_size = 1000 if limit <= 0 else min(limit, 1000)
        skip = 0

        while True:
            request_url = (
                base_url
                + filter_text
                + f"&$top={page_size}&$skip={skip}&$expand=Assets"
            )
            response = requests.get(
                request_url,
                headers={
                    "User-Agent": CLIENT_USER_AGENT,
                    "Accept": "application/json",
                },
                timeout=120,
                **self.proxy_kwargs(),
            )

            if response.status_code != 200:
                console.print(f"[bold red]❌ 搜索失败：{response.status_code}[/bold red]")
                console.print(response.text[:1000])
                console.print(f"[yellow]请求链接：{request_url[:1000]}[/yellow]")
                return []

            try:
                data = response.json()
            except json.JSONDecodeError as e:
                console.print(f"[bold red]❌ 搜索响应不是合法 JSON：{e}[/bold red]")
                console.print(response.text[:1000])
                return []

            products = data.get("value", [])
            if not isinstance(products, list):
                console.print("[bold red]❌ 搜索响应中的 value 不是列表[/bold red]")
                console.print(json.dumps(data, ensure_ascii=False)[:1000])
                return []

            all_products.extend(products)
            console.print(f"[blue]  已检索 {len(all_products)} 条...[/blue]")

            if not products:
                break
            if limit > 0 and len(all_products) >= limit:
                all_products = all_products[:limit]
                break
            if len(products) < page_size:
                break

            skip += len(products)

        products = all_products

        if tile_ids:
            tile_set = set(tile_ids)
            products = [product for product in products if get_tile_id(product) in tile_set]

        console.print(f"[bold green]✅ 找到 {len(products)} 条记录[/bold green]")
        for i, item in enumerate(products[:5], 1):
            content_date = item.get("ContentDate") or {}
            console.print(f"  {i}. {item.get('Name', item.get('Id', 'N/A'))}")
            console.print(f"     日期：{content_date.get('Start', 'N/A')}")
            console.print(f"     Tile：{get_tile_id(item)}")

        tile_counts = {}
        for product in products:
            tile_id = get_tile_id(product)
            tile_counts[tile_id] = tile_counts.get(tile_id, 0) + 1
        if tile_counts:
            console.print("\n[bold]========== Tile ID 统计 ==========[/bold]")
            for tile_id, count in sorted(tile_counts.items()):
                console.print(f"  {tile_id}: {count} 景")
            console.print(f"  总计: {len(products)} 景")
            console.print("[bold]==================================[/bold]\n")

        return products

    def download_product(
        self,
        product_id,
        temp_output_dir="./temp_data",
        final_output_dir="./sentinel_data",
        display_name=None,
        max_retries=3,
        chunk_size=512 * 1024,
        progress=None,
        overall_task=None,
        product_index=None,
        product_total=None,
        advance_on_failure=True,
        proxy_url=None,
        defer_tif=False,
    ):
        """下载单景产品到临时目录，抽取 10m tif 后删除 zip。"""
        os.makedirs(temp_output_dir, exist_ok=True)
        os.makedirs(final_output_dir, exist_ok=True)

        file_id = safe_filename(display_name or product_id)
        marker_file = os.path.join(temp_output_dir, f"{file_id}.txt")
        output_file = os.path.join(temp_output_dir, f"{file_id}.zip")
        temp_file = output_file + ".part"
        final_tif = tif_output_path(output_file, final_output_dir)
        account_label = (
            f"账号 {self.account_id}｜" if self.account_id is not None else ""
        )
        scene_label = (
            f"{account_label}第 {product_index}/{product_total} 景｜"
            if product_index is not None and product_total is not None
            else account_label
        )

        if final_tif.exists():
            console.print(f"[yellow]{scene_label}⏭️ 已生成 tif，跳过：{final_tif.name}[/yellow]")
            if progress and overall_task is not None:
                progress.update(overall_task, advance=1)
            return "skipped", file_id

        if defer_tif and os.path.exists(output_file):
            console.print(
                f"[cyan]{scene_label}📦 ZIP 已下载，加入 TIFF 抽取队列："
                f"{file_id}[/cyan]"
            )
            return "downloaded", file_id

        download_url = f"https://download.dataspace.copernicus.eu/odata/v1/Products({product_id})/$value"
        last_error = None

        for attempt in range(1, max_retries + 1):
            response = None
            task_id = None

            try:
                resume_from = os.path.getsize(temp_file) if os.path.exists(temp_file) else 0
                request_headers = {}
                if resume_from > 0:
                    request_headers["Range"] = f"bytes={resume_from}-"
                console.print(
                    f"[blue]{scene_label}📥 开始下载：{file_id}，"
                    f"第 {attempt}/{max_retries} 次尝试[/blue]"
                )
                if resume_from > 0:
                    console.print(
                        f"[cyan]{scene_label}↪️ 从 {resume_from / (1024 ** 2):.1f} MB "
                        f"处续传：{file_id}[/cyan]"
                    )
                response = self.request_with_token_refresh(
                    "GET",
                    download_url,
                    headers=request_headers,
                    stream=True,
                    timeout=(30, 300),
                    allow_redirects=True,
                    **(
                        {"proxies": {"http": proxy_url, "https": proxy_url}}
                        if proxy_url
                        else {}
                    ),
                )

                if response.status_code == 416 and resume_from > 0:
                    content_range = response.headers.get("Content-Range", "")
                    match = re.fullmatch(r"bytes \*/(\d+)", content_range)
                    remote_size = int(match.group(1)) if match else 0
                    if remote_size == resume_from:
                        response.close()
                        response = None
                        os.replace(temp_file, output_file)
                        downloaded_size = remote_size
                        total_size = remote_size
                    else:
                        raise RuntimeError(
                            f"服务端拒绝续传，本地已有 {resume_from} 字节，"
                            f"远端大小 {remote_size or '未知'}"
                        )

                if response is not None and response.status_code not in (200, 206):
                    raise requests.HTTPError(
                        f"HTTP {response.status_code}: {response.text[:500]}",
                        response=response,
                    )

                if response is not None:
                    content_length = int(response.headers.get("Content-Length", 0))
                    append_mode = response.status_code == 206 and resume_from > 0
                    if append_mode:
                        content_range = response.headers.get("Content-Range", "")
                        match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+|\*)", content_range)
                        if not match or int(match.group(1)) != resume_from:
                            raise RuntimeError(
                                f"服务端续传位置不匹配：{content_range or '缺少 Content-Range'}"
                            )
                        total_size = (
                            int(match.group(3))
                            if match.group(3) != "*"
                            else resume_from + content_length
                        )
                        downloaded_size = resume_from
                        file_mode = "ab"
                    else:
                        if resume_from > 0:
                            console.print(
                                f"[yellow]{scene_label}⚠️ 服务端未接受 Range，"
                                f"将从头下载：{file_id}[/yellow]"
                            )
                        resume_from = 0
                        downloaded_size = 0
                        total_size = content_length
                        file_mode = "wb"

                    if total_size > 0:
                        size_gb = total_size / (1024 ** 3)
                        console.print(
                            f"[cyan]{scene_label}📦 文件大小：{size_gb:.2f} GB[/cyan]"
                        )
                    else:
                        console.print(f"[cyan]{scene_label}📦 文件大小：服务端未提供[/cyan]")
                    task_id = (
                        progress.add_task(
                            f"{scene_label}{file_id}",
                            total=total_size or None,
                            completed=downloaded_size,
                        )
                        if progress
                        else None
                    )

                    with open(temp_file, file_mode) as f:
                        for chunk in response.iter_content(chunk_size=chunk_size):
                            if not chunk:
                                continue
                            f.write(chunk)
                            downloaded_size += len(chunk)
                            if progress and task_id is not None:
                                progress.update(task_id, advance=len(chunk))

                    if total_size > 0 and downloaded_size != total_size:
                        raise RuntimeError(
                            f"文件大小不完整，应下载 {total_size} 字节，"
                            f"实际下载 {downloaded_size} 字节"
                        )

                    os.replace(temp_file, output_file)
                with open(marker_file, "w", encoding="utf-8") as f:
                    f.write("file downloaded successfully.")

                logger.info("下载完成：%s -> %s", product_id, output_file)
                if defer_tif:
                    console.print(
                        f"[green]{scene_label}✅ ZIP 下载完成，已加入 TIFF "
                        f"抽取队列：{file_id}[/green]"
                    )
                    return "downloaded", file_id

                finalize_downloaded_product(
                    output_file,
                    final_tif,
                    marker_file,
                    scene_label=scene_label,
                    file_id=file_id,
                )

                if progress and overall_task is not None:
                    progress.update(overall_task, advance=1)
                return "success", file_id

            except requests.HTTPError as e:
                last_error = e
                status_code = getattr(e.response, "status_code", None)
                if status_code == 401:
                    self.login(force_refresh=True)
                    console.print(f"[yellow]{scene_label}⚠️ token 已刷新，将重试：{file_id}[/yellow]")
                elif status_code == 429:
                    console.print(f"[yellow]{scene_label}⚠️ 请求过多，将等待后重试：{file_id}[/yellow]")
                else:
                    console.print(
                        f"[yellow]{scene_label}⚠️ 下载失败：{file_id}，"
                        f"HTTP 状态码：{status_code}[/yellow]"
                    )

            except (
                requests.exceptions.ChunkedEncodingError,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                requests.exceptions.RequestException,
                RuntimeError,
            ) as e:
                last_error = e
                console.print(
                    f"[yellow]{scene_label}⚠️ 下载异常：{file_id}，"
                    f"{type(e).__name__}: {e}[/yellow]"
                )

            finally:
                if progress and task_id is not None:
                    progress.remove_task(task_id)
                if response is not None:
                    response.close()

            if os.path.exists(temp_file):
                partial_size = os.path.getsize(temp_file)
                console.print(
                    f"[cyan]{scene_label}💾 已保留 {partial_size / (1024 ** 2):.1f} MB，"
                    f"下次将断点续传：{file_id}[/cyan]"
                )

            if attempt < max_retries:
                sleep_seconds = 10 * attempt
                console.print(
                    f"[yellow]{scene_label}⏳ 等待 {sleep_seconds} 秒后重试："
                    f"{file_id}[/yellow]"
                )
                time.sleep(sleep_seconds)

        logger.error("本轮下载失败，将由批处理继续重试：%s - %s", product_id, last_error)
        console.print(
            f"[red]{scene_label}❌ 本轮下载失败：{file_id}，错误：{last_error}[/red]"
        )

        if advance_on_failure and progress and overall_task is not None:
            progress.update(overall_task, advance=1)
        return "failed", file_id

    def batch_download(
        self,
        products,
        temp_output_dir="./temp_data",
        final_output_dir="./sentinel_data",
        max_workers=3,
        max_retries=3,
    ):
        """批量多线程下载。"""
        console.print(f"[bold green]🚀 开始批量下载 {len(products)} 景数据[/bold green]")
        console.print(f"[bold blue]📁 临时 zip 目录：{temp_output_dir}[/bold blue]")
        console.print(f"[bold blue]📁 tif 输出目录：{final_output_dir}[/bold blue]")

        stats = {"success": 0, "failed": 0, "skipped": 0}

        progress = Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            console=console,
        )

        with progress:
            overall_task = progress.add_task("总下载进度", total=len(products))
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {}
                for item in products:
                    product_id = item.get("Id") or item.get("id")
                    props = item.get("properties") or {}
                    display_name = item.get("Name") or props.get("title") or props.get("productIdentifier") or product_id
                    if not product_id:
                        continue

                    future = executor.submit(
                        self.download_product,
                        product_id,
                        temp_output_dir,
                        final_output_dir,
                        display_name,
                        max_retries,
                        progress=progress,
                        overall_task=overall_task,
                    )
                    futures[future] = product_id

                for future in as_completed(futures):
                    try:
                        status, file_id = future.result()
                    except Exception as e:
                        status = "failed"
                        file_id = futures[future]
                        console.print(f"[red]❌ 下载任务异常：{file_id} - {e}[/red]")

                    stats[status] += 1
                    if status == "failed":
                        failed_files.append(file_id)

        if failed_files:
            failed_path = os.path.join(final_output_dir, "downloadfalse.txt")
            with open(failed_path, "w", encoding="utf-8") as f:
                f.write("Files that have not been downloaded:\n")
                f.write("\n".join(failed_files))

        console.print()
        console.print("=" * 60)
        console.print("[bold]📊 下载完成统计：[/bold]")
        console.print(f"  ✅ 成功：{stats['success']}")
        console.print(f"  ⏭️ 跳过：{stats['skipped']}")
        console.print(f"  ❌ 失败：{stats['failed']}")
        console.print(f"  📁 总计：{sum(stats.values())}")
        console.print("=" * 60)

        return stats


class AccountPool:
    """多账号客户端池。"""

    def __init__(
        self,
        accounts,
        max_active_accounts=MAX_ACTIVE_ACCOUNTS,
        bypass_proxy=False,
        proxy_pool=None,
    ):
        self.clients = [
            CDSEClient(
                username=account["username"],
                password=account["password"],
                account_id=account.get("id"),
                bypass_proxy=bypass_proxy,
            )
            for account in accounts
        ]
        self.index = 0
        self.lock = threading.Lock()
        self.disabled = set()
        self.max_active_accounts = min(
            MAX_ACTIVE_ACCOUNTS,
            max(1, int(max_active_accounts)),
        )
        proxy_pool = proxy_pool or {}
        self.download_proxy_pool = DownloadProxyPool(
            enabled=proxy_pool.get("enabled", False),
            random_url=proxy_pool.get("random_url"),
            count_url=proxy_pool.get("count_url"),
        )

    def concurrency_plan(self, max_workers):
        requested_workers = max(1, int(max_workers))
        configured_limit = self.max_active_accounts * THREADS_PER_ACCOUNT
        planned_workers = min(requested_workers, configured_limit)
        account_count = math.ceil(planned_workers / THREADS_PER_ACCOUNT)
        return requested_workers, planned_workers, account_count

    def login_active_clients(self, account_count):
        active_clients = []
        last_error = None
        for client in self.clients:
            if client.account_id in self.disabled:
                continue
            try:
                client.login()
                active_clients.append(client)
            except Exception as exc:
                last_error = exc
                self.disabled.add(client.account_id)
                logger.warning("账号 %s 登录失败：%s", client.account_id, exc)
            if len(active_clients) == account_count:
                return active_clients

        if not active_clients:
            raise RuntimeError(f"所有账号均无法登录：{last_error or '没有有效账号'}")
        return active_clients

    def next_client(self):
        with self.lock:
            if len(self.disabled) >= len(self.clients):
                raise RuntimeError("所有账号均不可用")

            for _ in range(len(self.clients)):
                client = self.clients[self.index % len(self.clients)]
                self.index += 1
                if client.account_id not in self.disabled:
                    return client

            raise RuntimeError("没有可用账号")

    def login_any(self):
        last_error = None
        for _ in range(len(self.clients)):
            client = self.next_client()
            try:
                client.login()
                return client
            except Exception as e:
                last_error = e
                self.disabled.add(client.account_id)
                logger.warning("账号 %s 登录失败：%s", client.account_id, e)
        raise RuntimeError(f"所有账号登录失败：{last_error}")

    def search_products(self, **kwargs):
        client = self.login_any()
        return client.search_products(**kwargs)

    def batch_download(
        self,
        products,
        temp_output_dir="./temp_data",
        final_output_dir="./sentinel_data",
        max_workers=3,
        max_retries=3,
    ):
        """每个账号固定三个线程，按账号分组批量下载。"""
        requested_workers, planned_workers, planned_accounts = self.concurrency_plan(
            max_workers
        )
        active_clients = self.login_active_clients(planned_accounts)
        account_count = len(active_clients)
        actual_workers = min(planned_workers, account_count * THREADS_PER_ACCOUNT)
        worker_limits = [THREADS_PER_ACCOUNT] * account_count
        worker_limits[-1] = actual_workers - THREADS_PER_ACCOUNT * (account_count - 1)

        if requested_workers > planned_workers:
            console.print(
                f"[bold yellow]⚠️ 请求 {requested_workers} 个下载并发，但配置最多同时使用 "
                f"{self.max_active_accounts} 个账号；已调整为 {planned_workers} 个并发[/bold yellow]"
            )
        if account_count < planned_accounts:
            console.print(
                f"[bold yellow]⚠️ 计划使用 {planned_accounts} 个账号，但只有 "
                f"{account_count} 个账号可用；已调整为 {actual_workers} 个并发[/bold yellow]"
            )
        console.print(f"[bold green]🚀 开始批量下载 {len(products)} 景数据[/bold green]")
        console.print(f"[bold blue]📁 临时 zip 目录：{temp_output_dir}[/bold blue]")
        console.print(f"[bold blue]📁 tif 输出目录：{final_output_dir}[/bold blue]")
        console.print(
            f"[bold blue]👥 并发策略：请求 {requested_workers}，实际 "
            f"{actual_workers}，启用 {account_count} 个账号[/bold blue]"
        )
        if self.download_proxy_pool.enabled:
            console.print(
                f"[bold blue]🌐 IP 池代理已启用："
                f"{self.download_proxy_pool.proxy_count} 个可用代理[/bold blue]"
            )
        for client, worker_limit in zip(active_clients, worker_limits):
            console.print(
                f"[blue]  账号 {client.account_id}：{worker_limit} 个下载并发[/blue]"
            )

        stats = {"success": 0, "failed": 0, "skipped": 0}
        failed_files = []

        progress = Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            console=console,
        )

        with progress:
            overall_task = progress.add_task("总下载进度", total=len(products))
            pending = []
            total_products = len(products)
            for index, item in enumerate(products, 1):
                product_id = item.get("Id") or item.get("id")
                props = item.get("properties") or {}
                display_name = (
                    item.get("Name")
                    or props.get("title")
                    or props.get("productIdentifier")
                    or product_id
                )
                if product_id:
                    worker_slot = (index - 1) % actual_workers
                    client_index = min(worker_slot // THREADS_PER_ACCOUNT, account_count - 1)
                    proxy_url = self.download_proxy_pool.acquire()
                    pending.append((index, product_id, display_name, client_index, proxy_url))

            assigned_counts = [0] * account_count
            for _, _, _, client_index, _ in pending:
                assigned_counts[client_index] += 1
            for client, assigned_count in zip(active_clients, assigned_counts):
                console.print(
                    f"[blue]  账号 {client.account_id}：本批分配 {assigned_count} 景[/blue]"
                )

            retry_round = 0
            while pending:
                retry_round += 1
                if retry_round > 1:
                    console.print(
                        f"[bold yellow]🔁 继续重试上一轮失败的 {len(pending)} 景，"
                        f"第 {retry_round} 轮[/bold yellow]"
                    )

                failed_items = []
                executors = [
                    ThreadPoolExecutor(
                        max_workers=worker_limit,
                        thread_name_prefix=f"account-{client.account_id}",
                    )
                    for client, worker_limit in zip(active_clients, worker_limits)
                ]
                try:
                    futures = {}
                    for index, product_id, display_name, client_index, proxy_url in pending:
                        client = active_clients[client_index]
                        future = executors[client_index].submit(
                            client.download_product,
                            product_id,
                            temp_output_dir,
                            final_output_dir,
                            display_name,
                            max_retries,
                            progress=progress,
                            overall_task=overall_task,
                            product_index=index,
                            product_total=total_products,
                            advance_on_failure=False,
                            proxy_url=proxy_url,
                        )
                        futures[future] = (
                            index,
                            product_id,
                            display_name,
                            client_index,
                            proxy_url,
                        )

                    for future in as_completed(futures):
                        item = futures[future]
                        try:
                            status, file_id = future.result()
                        except Exception as e:
                            status = "failed"
                            file_id = item[1]
                            console.print(f"[red]❌ 下载任务异常：{file_id} - {e}[/red]")

                        if status == "failed":
                            if self.download_proxy_pool.enabled:
                                old_proxy = item[4]
                                new_proxy = self.download_proxy_pool.acquire_different(
                                    old_proxy
                                )
                                item = (*item[:4], new_proxy)
                                console.print(
                                    f"[yellow]🌐 {file_id} 多次下载失败，"
                                    f"下一轮切换代理：{new_proxy}[/yellow]"
                                )
                            failed_items.append(item)
                        else:
                            stats[status] += 1
                finally:
                    for executor in executors:
                        executor.shutdown(wait=True)

                pending = failed_items
                if pending:
                    console.print(
                        f"[yellow]⏳ 本轮仍有 {len(pending)} 景未完成，"
                        "等待 30 秒后继续下载[/yellow]"
                    )
                    time.sleep(30)

        console.print()
        console.print("=" * 60)
        console.print("[bold]📊 下载完成统计：[/bold]")
        console.print(f"  ✅ 成功：{stats['success']}")
        console.print(f"  ⏭️ 跳过：{stats['skipped']}")
        console.print(f"  ❌ 失败：{stats['failed']}")
        console.print(f"  📁 总计：{sum(stats.values())}")
        console.print("=" * 60)

        return stats


def parse_args():
    parser = argparse.ArgumentParser(description="Sentinel-1/2 数据自动下载工具")
    parser.add_argument("--config-file", default="config.json", help="多账号和城市配置 JSON 文件")
    parser.add_argument("--account-file", default="account.json", help="单账号 JSON 文件；没有 config.json 时兼容使用")
    parser.add_argument("--collection", default="sentinel-2-l2a", help="数据集，例如 sentinel-2-l2a、sentinel-1-grd")
    parser.add_argument("--contains", help="产品名必须包含的字符串，例如 L2A、GRD；默认按 collection 自动设置")
    parser.add_argument("--cloud-cover", type=float, default=40, help="Sentinel-2 最大云量百分比")
    parser.add_argument("--start-date", default="2026-07-01", help="开始日期 YYYY-MM-DD")
    parser.add_argument("--end-date", default="2026-07-14", help="结束日期 YYYY-MM-DD")
    parser.add_argument("--limit", type=int, default=0, help="最多搜索/下载多少景；0 表示全部")
    parser.add_argument("--output-dir", default="./sentinel_data", help="tif 输出根目录，会按 shp 名称自动创建子文件夹")
    parser.add_argument("--temp-dir", default="./temp_data", help="临时 zip 根目录，程序启动时创建，结束时删除")
    parser.add_argument("--shp-root", default="./shp", help="自动生成城市边界的输出目录")
    parser.add_argument("--city-layer", default=str(CITY_LAYER), help="市级边界 shp")
    parser.add_argument("--county-layer", default=str(COUNTY_LAYER), help="县级边界 shp")
    parser.add_argument("--no-prepare-shp", action="store_true", help="不从 config 城市列表生成 shp")
    parser.add_argument(
        "--max-workers",
        type=int,
        default=3,
        help="期望下载并发数；每个账号最多 3 个并发，账号不足时自动降低",
    )
    parser.add_argument("--max-retries", type=int, default=5, help="每个文件最大重试次数")
    parser.add_argument("--search-only", action="store_true", help="只检索并展示结果，不下载")
    parser.add_argument(
        "--aoi-bbox",
        type=float,
        nargs=4,
        metavar=("MIN_LON", "MIN_LAT", "MAX_LON", "MAX_LAT"),
        help="区域外接矩形，例如：--aoi-bbox 116.1 40.6 117.0 41.2",
    )
    parser.add_argument("--roi-file", default="./shp/兴安盟/兴安盟.shp", help="区域 shp/geojson 文件")
    parser.add_argument("--aoi-geojson", dest="roi_file", help="区域 GeoJSON 文件，兼容旧参数名")
    parser.add_argument("--tile-id", action="append", default=[], help="按 Sentinel-2 tileId 过滤，可重复指定")
    return parser.parse_args()


def main():
    console.print("[bold cyan]🌍 Sentinel-1/2 数据自动下载工具[/bold cyan]")
    console.print()

    args = parse_args()
    download_settings = {"date_ranges_by_city": {}}

    try:
        if Path(args.config_file).exists():
            accounts, cities, download_settings = load_config(args.config_file)
            args.start_date = download_settings.get("start_date") or args.start_date
            args.end_date = download_settings.get("end_date") or args.end_date
            args.output_dir = download_settings.get("output_dir") or args.output_dir
            args.temp_dir = download_settings.get("temp_dir") or args.temp_dir
            if download_settings.get("cloud_cover") is not None:
                args.cloud_cover = download_settings["cloud_cover"]
            if download_settings.get("max_workers") is not None:
                args.max_workers = download_settings["max_workers"]
            console.print(f"[bold blue]👥 已读取 {len(accounts)} 个账号，{len(cities)} 个城市[/bold blue]")
            if args.no_prepare_shp:
                tasks = [
                    {"city": city, "roi_file": str(Path(args.shp_root) / safe_filename(city) / f"{safe_filename(city)}.shp")}
                    for city in cities
                ]
            else:
                tasks = prepare_region_shapefiles(
                    cities,
                    shp_root=args.shp_root,
                    city_layer=Path(args.city_layer),
                    county_layer=Path(args.county_layer),
                )
            client_pool = AccountPool(
                accounts,
                max_active_accounts=download_settings["max_active_accounts"],
                bypass_proxy=download_settings["bypass_proxy"],
                proxy_pool=download_settings["proxy_pool"],
            )
        else:
            username, password = load_credentials(args.account_file)
            tasks = [{"city": detect_roi_name(args.roi_file, args.aoi_bbox), "roi_file": args.roi_file}]
            client_pool = AccountPool([{"id": 1, "username": username, "password": password}])
    except Exception as e:
        console.print(f"[bold red]❌ 配置读取失败：{e}[/bold red]")
        sys.exit(1)

    temp_root = reset_temp_dir(args.temp_dir)
    console.print(f"[bold blue]📦 临时目录：{temp_root}[/bold blue]")

    try:
        client_pool.login_any()
    except Exception as e:
        console.print(f"[bold red]❌ 登录失败：{e}[/bold red]")
        sys.exit(1)

    any_products = False
    try:
        for task in tasks:
            city = task["city"]
            roi_file = task["roi_file"]
            console.print()
            console.print(f"[bold cyan]========== {city} ==========[/bold cyan]")

            try:
                geometry = load_roi_geometry(None if args.aoi_bbox else roi_file, args.aoi_bbox)
                output_dir = build_output_dir(
                    args.output_dir,
                    roi_file=None if args.aoi_bbox else roi_file,
                    bbox=args.aoi_bbox,
                )
                temp_output_dir = build_output_dir(
                    args.temp_dir,
                    roi_file=None if args.aoi_bbox else roi_file,
                    bbox=args.aoi_bbox,
                )
            except Exception as e:
                console.print(f"[bold red]❌ 区域读取失败：{city} - {e}[/bold red]")
                continue

            console.print(f"[bold blue]📁 tif 输出目录：{output_dir}[/bold blue]")
            console.print(f"[bold blue]📁 zip 临时目录：{temp_output_dir}[/bold blue]")

            date_ranges = download_settings.get("date_ranges_by_city", {}).get(
                normalize_region_name(city)
            ) or [{"start_date": args.start_date, "end_date": args.end_date}]

            for range_index, date_range in enumerate(date_ranges, 1):
                start_date = date_range["start_date"]
                end_date = date_range["end_date"]
                console.print(
                    f"[bold cyan]📅 {city} 下载时间段 {range_index}/{len(date_ranges)}："
                    f"{start_date} ～ {end_date}[/bold cyan]"
                )

                cache_key, cache_query = build_search_cache_entry(
                    city=city,
                    collection=args.collection,
                    start_date=start_date,
                    end_date=end_date,
                    geometry=geometry,
                    contains=args.contains,
                    cloud_cover=args.cloud_cover,
                    tile_ids=args.tile_id,
                    limit=args.limit,
                )
                cache_file = download_settings.get(
                    "search_cache_file", "./search_results.json"
                )
                products = load_cached_products(cache_file, cache_key)
                if products is None:
                    products = client_pool.search_products(
                        collection=args.collection,
                        start_date=start_date,
                        end_date=end_date,
                        limit=args.limit,
                        geometry=geometry,
                        contains=args.contains,
                        cloud_cover=args.cloud_cover,
                        tile_ids=args.tile_id,
                    )
                    if products:
                        save_cached_products(
                            cache_file,
                            cache_key,
                            cache_query,
                            products,
                        )
                        console.print(
                            f"[green]💾 已保存 {len(products)} 条检索结果到："
                            f"{cache_file}[/green]"
                        )
                else:
                    console.print(
                        f"[bold green]📋 使用本地检索缓存：{len(products)} 条，"
                        f"跳过在线检索[/bold green]"
                    )

                if not products:
                    console.print(
                        f"[yellow]⚠️ {city} 在 {start_date} ～ {end_date} 未找到数据[/yellow]"
                    )
                    continue

                any_products = True
                if args.search_only:
                    console.print(
                        f"[bold green]✅ {city} 此时间段仅检索，已停止在下载前[/bold green]"
                    )
                    continue

                client_pool.batch_download(
                    products,
                    temp_output_dir=temp_output_dir,
                    final_output_dir=output_dir,
                    max_workers=args.max_workers,
                    max_retries=args.max_retries,
                )

        if not any_products:
            console.print("[bold red]❌ 所有城市均未找到数据[/bold red]")
            sys.exit(1)
    finally:
        if temp_root.exists():
            console.print(
                f"[bold blue]💾 已保留临时目录，未完成文件可在下次续传："
                f"{temp_root}[/bold blue]"
            )


if __name__ == "__main__":
    main()
