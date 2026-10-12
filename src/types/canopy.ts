export const CLASS_NAMES = ['tree', 'grass', 'building', 'road', 'bare_soil', 'water', 'other'] as const

export type ClassName = (typeof CLASS_NAMES)[number]

export const CLASS_LABELS: Record<ClassName, string> = {
  tree: 'Tree',
  grass: 'Grass',
  building: 'Building',
  road: 'Road',
  bare_soil: 'Bare Soil',
  water: 'Water',
  other: 'Impervious Other',
}

// Must visually match backend/app/segmentation.py CLASS_COLORS (light-mode
// hex of the same validated categorical set) so the map overlay and this UI
// read as one system. tree/grass/building/water/other are the dataviz-skill
// categorical palette (green/yellow/magenta/blue/aqua, re-validated via
// scripts/validate_palette.js for this 7-class set -- worst adjacent pair
// beyond the two off-palette neutrals below clears the CVD/normal-vision
// floors); road/bare_soil are deliberate off-palette neutrals (gray/brown
// aren't valid categorical hues, but are strong, recessive cartographic
// conventions for infrastructure).
export const CLASS_COLORS: Record<ClassName, { light: string; dark: string }> = {
  tree: { light: '#008300', dark: '#008300' },
  grass: { light: '#eda100', dark: '#c98500' },
  building: { light: '#e87ba4', dark: '#d55181' },
  road: { light: '#64748b', dark: '#94a3b8' },
  bare_soil: { light: '#a8763e', dark: '#c99a5b' },
  water: { light: '#2a78d6', dark: '#3987e5' },
  other: { light: '#1baf7a', dark: '#199e70' },
}

export interface ClassStat {
  percent: number
  pixels: number
}

export interface SegmentResponse {
  analysis_id: string
  bounds: { south: number; west: number; north: number; east: number }
  zoom: number
  image_width: number
  image_height: number
  overlay_png: string
  total_pixels: number
  area_m2: number
  classes: Record<ClassName, ClassStat>
}

export interface LatLon {
  lat: number
  lon: number
}

/** A user-drawn correction region: this class, painted over the automatic
 * classification wherever the polygon covers. Sent to the backend in list
 * order, where later entries win over earlier ones on overlap. */
export interface OverrideRegion {
  id: string
  className: ClassName
  polygon: LatLon[]
}

export const DRAW_SHAPES = ['polygon', 'rectangle', 'freehand', 'ellipse'] as const
export type DrawShape = (typeof DRAW_SHAPES)[number]

export const DRAW_SHAPE_META: Record<DrawShape, { label: string; icon: string }> = {
  polygon: { label: 'Polygon', icon: 'fa-draw-polygon' },
  rectangle: { label: 'Rectangle', icon: 'fa-square' },
  freehand: { label: 'Freehand', icon: 'fa-pen' },
  ellipse: { label: 'Circle / Oval', icon: 'fa-circle' },
}

export interface GeocodeResult {
  display_name: string
  lat: number
  lon: number
  boundingbox: [number, number, number, number] | null
}

/**
 * Standard error of a proportion, the same formula i-Tree Canopy itself
 * reports for manual point-sampling accuracy: SE = sqrt(p(100-p)/n).
 * Since segmentation classifies every pixel (not a sample), percentages
 * are exact; this expresses "if this measurement had instead come from n
 * random point samples" so results stay comparable to the manual workflow.
 */
export function standardError(percent: number, n: number): number {
  if (n <= 0) return 0
  return Math.sqrt((percent * (100 - percent)) / n)
}
