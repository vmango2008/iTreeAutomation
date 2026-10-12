"""Fetch and stitch XYZ satellite tiles for an arbitrary lat/lon bounding box.

Uses Esri World Imagery (free, no API key required). Tiles are fetched
concurrently, pasted onto a canvas, and cropped to the exact requested
bounding box in pixel space using standard Web Mercator tile math.
"""

import io
import math
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Tuple

import requests
from PIL import Image

TILE_SIZE = 256
TILE_URL_TEMPLATE = (
    "https://server.arcgisonline.com/ArcGIS/rest/services/"
    "World_Imagery/MapServer/tile/{z}/{y}/{x}"
)
USER_AGENT = "iTree-Canopy-AI/1.0 (local development prototype)"

MAX_DIMENSION_PX = 1536
PREFERRED_MAX_TILES = 256
HARD_MAX_TILES = 400
MIN_ZOOM = 14
MAX_ZOOM = 19

_tile_cache: dict[tuple[int, int, int], bytes] = {}
_CACHE_LIMIT = 800


class AreaTooLargeError(Exception):
    pass


def lon_to_x(lon: float, zoom: int) -> float:
    return (lon + 180.0) / 360.0 * (2 ** zoom)


def lat_to_y(lat: float, zoom: int) -> float:
    lat_rad = math.radians(lat)
    return (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * (2 ** zoom)


def meters_per_pixel(lat: float, zoom: int) -> float:
    return 156543.03392 * math.cos(math.radians(lat)) / (2 ** zoom)


def pad_bbox(south: float, west: float, north: float, east: float, frac: float = 0.12, min_pad_deg: float = 0.00025):
    dlat = north - south
    dlon = east - west
    pad_lat = max(dlat * frac, min_pad_deg)
    pad_lon = max(dlon * frac, min_pad_deg)
    return south - pad_lat, west - pad_lon, north + pad_lat, east + pad_lon


def _tile_span(south: float, west: float, north: float, east: float, zoom: int):
    x0f, x1f = lon_to_x(west, zoom), lon_to_x(east, zoom)
    y0f, y1f = lat_to_y(north, zoom), lat_to_y(south, zoom)
    width_px = (x1f - x0f) * TILE_SIZE
    height_px = (y1f - y0f) * TILE_SIZE
    tiles_x = math.floor(x1f) - math.floor(x0f) + 1
    tiles_y = math.floor(y1f) - math.floor(y0f) + 1
    return width_px, height_px, tiles_x * tiles_y


def choose_zoom(south: float, west: float, north: float, east: float) -> int:
    fallback = None
    for zoom in range(MAX_ZOOM, MIN_ZOOM - 1, -1):
        width_px, height_px, tile_count = _tile_span(south, west, north, east, zoom)
        if zoom == MIN_ZOOM:
            fallback = tile_count
        if width_px <= MAX_DIMENSION_PX and height_px <= MAX_DIMENSION_PX and tile_count <= PREFERRED_MAX_TILES:
            return zoom
    if fallback is not None and fallback <= HARD_MAX_TILES:
        return MIN_ZOOM
    raise AreaTooLargeError(
        "That area is too large to analyze in detail. Please draw a smaller polygon (a few square kilometers or less)."
    )


def _fetch_tile(z: int, x: int, y: int) -> Image.Image:
    key = (z, x, y)
    data = _tile_cache.get(key)
    if data is None:
        try:
            url = TILE_URL_TEMPLATE.format(z=z, x=x, y=y)
            resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=10)
            data = resp.content if resp.status_code == 200 else b""
        except requests.exceptions.RequestException:
            data = b""
        if data and len(_tile_cache) < _CACHE_LIMIT:
            _tile_cache[key] = data
    if not data:
        return Image.new("RGB", (TILE_SIZE, TILE_SIZE), (40, 40, 40))
    try:
        return Image.open(io.BytesIO(data)).convert("RGB")
    except Exception:
        return Image.new("RGB", (TILE_SIZE, TILE_SIZE), (40, 40, 40))


def fetch_polygon_area(
    south: float, west: float, north: float, east: float
) -> Tuple[Image.Image, int, Callable[[float, float], Tuple[float, float]], Tuple[float, float, float, float]]:
    """Fetch and stitch imagery covering the (padded) bbox.

    Returns (cropped_image, zoom, to_px(lon, lat) -> (x, y), (south, west, north, east) of the returned image).
    """
    p_south, p_west, p_north, p_east = pad_bbox(south, west, north, east)
    zoom = choose_zoom(p_south, p_west, p_north, p_east)

    x0f, x1f = lon_to_x(p_west, zoom), lon_to_x(p_east, zoom)
    y0f, y1f = lat_to_y(p_north, zoom), lat_to_y(p_south, zoom)

    tile_x0, tile_x1 = math.floor(x0f), math.floor(x1f)
    tile_y0, tile_y1 = math.floor(y0f), math.floor(y1f)

    tiles_x = list(range(tile_x0, tile_x1 + 1))
    tiles_y = list(range(tile_y0, tile_y1 + 1))

    canvas = Image.new("RGB", (len(tiles_x) * TILE_SIZE, len(tiles_y) * TILE_SIZE))

    def load(coord):
        tx, ty = coord
        return tx, ty, _fetch_tile(zoom, tx, ty)

    coords = [(tx, ty) for ty in tiles_y for tx in tiles_x]
    with ThreadPoolExecutor(max_workers=12) as ex:
        for tx, ty, tile_img in ex.map(load, coords):
            canvas.paste(tile_img, ((tx - tile_x0) * TILE_SIZE, (ty - tile_y0) * TILE_SIZE))

    left_i = round((x0f - tile_x0) * TILE_SIZE)
    top_i = round((y0f - tile_y0) * TILE_SIZE)
    right_i = round((x1f - tile_x0) * TILE_SIZE)
    bottom_i = round((y1f - tile_y0) * TILE_SIZE)

    cropped = canvas.crop((left_i, top_i, right_i, bottom_i))

    def to_px(lon: float, lat: float) -> Tuple[float, float]:
        fx = lon_to_x(lon, zoom)
        fy = lat_to_y(lat, zoom)
        px = (fx - tile_x0) * TILE_SIZE - left_i
        py = (fy - tile_y0) * TILE_SIZE - top_i
        return (px, py)

    return cropped, zoom, to_px, (p_south, p_west, p_north, p_east)
