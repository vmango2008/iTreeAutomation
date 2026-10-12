"""Thin proxy over OpenStreetMap Nominatim for address/place search.

Proxied server-side (rather than called from the browser) so we can set a
descriptive User-Agent, which Nominatim's usage policy requires.
"""

import requests

USER_AGENT = "iTree-Canopy-AI/1.0 (local development prototype)"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"


def search(query: str, limit: int = 6) -> list[dict]:
    resp = requests.get(
        NOMINATIM_URL,
        params={"q": query, "format": "jsonv2", "limit": limit, "addressdetails": 0},
        headers={"User-Agent": USER_AGENT},
        timeout=8,
    )
    resp.raise_for_status()
    results = []
    for item in resp.json():
        bbox = item.get("boundingbox")
        results.append(
            {
                "display_name": item.get("display_name"),
                "lat": float(item["lat"]),
                "lon": float(item["lon"]),
                "boundingbox": [float(x) for x in bbox] if bbox else None,
            }
        )
    return results
