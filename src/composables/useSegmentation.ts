import { ref } from 'vue'
import type { GeocodeResult, LatLon, OverrideRegion, SegmentResponse } from '../types/canopy'

// Empty in local dev, so these calls stay relative and go through Vite's
// own /api proxy (vite.config.ts) to the local backend. Set at build time
// on the GitHub Pages deploy, which has no dev-server proxy to rely on and
// must call the deployed backend's URL directly.
const API_BASE = import.meta.env.VITE_API_BASE_URL ?? ''

async function parseError(res: Response): Promise<string> {
  try {
    const body = await res.json()
    if (typeof body?.detail === 'string') return body.detail
  } catch {
    // fall through to generic message
  }
  return `Request failed (${res.status})`
}

// Plain fetchers rather than a ref-holding composable: MapView.vue manages
// multiple concurrent areas, each with its own loading/error/result state
// (see its `Area` type), so there's no single shared result for a
// composable instance to hold -- callers own their own state and just await
// these.

export async function fetchSegment(polygon: LatLon[]): Promise<SegmentResponse> {
  const res = await fetch(`${API_BASE}/api/segment`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ polygon }),
  })
  if (!res.ok) throw new Error(await parseError(res))
  return (await res.json()) as SegmentResponse
}

export async function fetchSegmentOverrides(analysisId: string, overrides: OverrideRegion[]): Promise<SegmentResponse> {
  const res = await fetch(`${API_BASE}/api/segment/${analysisId}/overrides`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      overrides: overrides.map((o) => ({ class_name: o.className, polygon: o.polygon })),
    }),
  })
  if (!res.ok) throw new Error(await parseError(res))
  return (await res.json()) as SegmentResponse
}

export function useGeocode() {
  const searching = ref(false)
  const searchError = ref<string | null>(null)

  async function search(query: string): Promise<GeocodeResult[]> {
    searching.value = true
    searchError.value = null
    try {
      const res = await fetch(`${API_BASE}/api/geocode?q=${encodeURIComponent(query)}`)
      if (!res.ok) throw new Error(await parseError(res))
      const body = (await res.json()) as { results: GeocodeResult[] }
      return body.results
    } catch (e) {
      searchError.value = e instanceof Error ? e.message : 'Search failed'
      return []
    } finally {
      searching.value = false
    }
  }

  return { searching, searchError, search }
}
