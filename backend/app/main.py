import base64
import io
import time

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from . import cache, geocode, segmentation, tiles

app = FastAPI(title="iTree Canopy AI")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "https://vmango2008.github.io",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)


class LatLon(BaseModel):
    lat: float
    lon: float


class SegmentRequest(BaseModel):
    polygon: list[LatLon] = Field(..., min_length=3)


class OverrideRegion(BaseModel):
    class_name: str
    polygon: list[LatLon] = Field(..., min_length=3)


class OverridesRequest(BaseModel):
    overrides: list[OverrideRegion] = Field(default_factory=list)


def _build_response(
    result: segmentation.SegmentationResult,
    analysis_id: str,
    bounds: tuple[float, float, float, float],
    zoom: int,
    image_width: int,
    image_height: int,
    meters_per_pixel: float,
) -> dict:
    stats, total_px = segmentation.compute_stats(result)
    if total_px == 0:
        raise HTTPException(status_code=400, detail="Drawn polygon is too small to analyze. Please draw a larger area.")

    overlay = segmentation.render_overlay(result)
    buf = io.BytesIO()
    overlay.save(buf, format="PNG")
    overlay_b64 = base64.b64encode(buf.getvalue()).decode("ascii")

    p_south, p_west, p_north, p_east = bounds
    area_m2 = total_px * (meters_per_pixel ** 2)

    return {
        "analysis_id": analysis_id,
        "bounds": {"south": p_south, "west": p_west, "north": p_north, "east": p_east},
        "zoom": zoom,
        "image_width": image_width,
        "image_height": image_height,
        "overlay_png": f"data:image/png;base64,{overlay_b64}",
        "total_pixels": total_px,
        "area_m2": round(area_m2, 1),
        "classes": stats,
    }


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/geocode")
def geocode_search(q: str = Query(..., min_length=1)):
    try:
        return {"results": geocode.search(q)}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Geocoding lookup failed: {exc}") from exc


@app.post("/api/segment")
def segment(req: SegmentRequest):
    lats = [p.lat for p in req.polygon]
    lons = [p.lon for p in req.polygon]
    south, north = min(lats), max(lats)
    west, east = min(lons), max(lons)

    try:
        image, zoom, to_px, bounds = tiles.fetch_polygon_area(south, west, north, east)
    except tiles.AreaTooLargeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    pixel_polygon = [to_px(p.lon, p.lat) for p in req.polygon]

    p_south, p_north = bounds[0], bounds[2]
    lat_mid = (p_south + p_north) / 2
    mpp = tiles.meters_per_pixel(lat_mid, zoom)

    result = segmentation.analyze(image, pixel_polygon, meters_per_pixel=mpp)

    analysis_id = cache.put(
        cache.CachedAnalysis(
            image=image,
            base_pixel_class=result.pixel_class,
            mask=result.mask,
            to_px=to_px,
            bounds=bounds,
            zoom=zoom,
            meters_per_pixel=mpp,
            created_at=time.time(),
        )
    )

    return _build_response(result, analysis_id, bounds, zoom, image.width, image.height, mpp)


@app.post("/api/segment/{analysis_id}/overrides")
def apply_region_overrides(analysis_id: str, req: OverridesRequest):
    entry = cache.get(analysis_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="This analysis has expired. Please analyze the area again.")

    valid_names = set(segmentation.CLASS_NAMES)
    for override in req.overrides:
        if override.class_name not in valid_names:
            raise HTTPException(status_code=400, detail=f"Unknown class '{override.class_name}'.")

    overrides = [(o.class_name, [(p.lon, p.lat) for p in o.polygon]) for o in req.overrides]
    result = segmentation.apply_overrides(entry.base_pixel_class, entry.mask, overrides, entry.to_px)

    return _build_response(
        result, analysis_id, entry.bounds, entry.zoom, entry.image.width, entry.image.height, entry.meters_per_pixel
    )
