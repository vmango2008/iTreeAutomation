<script setup lang="ts">
import L from 'leaflet'
import 'leaflet-draw'
import { computed, onMounted, onUnmounted, ref, shallowRef } from 'vue'
import markerIcon2x from 'leaflet/dist/images/marker-icon-2x.png'
import markerIcon from 'leaflet/dist/images/marker-icon.png'
import markerShadow from 'leaflet/dist/images/marker-shadow.png'
import SearchBox from './SearchBox.vue'
import ResultsPanel from './ResultsPanel.vue'
import ClassPicker from './ClassPicker.vue'
import { fetchSegment, fetchSegmentOverrides } from '../../composables/useSegmentation'
import {
  CLASS_COLORS,
  CLASS_NAMES,
  DRAW_SHAPES,
  DRAW_SHAPE_META,
  type ClassName,
  type ClassStat,
  type DrawShape,
  type LatLon,
  type OverrideRegion,
  type SegmentResponse,
} from '../../types/canopy'
import {
  startBoxDraw,
  startFreehandDraw,
  setupRightClickPan,
  boxShapeFromBounds,
  type BoxShape,
  type BoxShapeKind,
  type DrawHandle,
} from '../../lib/mapDrawing'

// Leaflet's default marker icon URLs are relative and break under Vite's
// bundling; point them at the bundled asset URLs instead.
delete (L.Icon.Default.prototype as unknown as { _getIconUrl?: unknown })._getIconUrl
L.Icon.Default.mergeOptions({
  iconRetinaUrl: markerIcon2x,
  iconUrl: markerIcon,
  shadowUrl: markerShadow,
})

const AOI_STYLE = { color: '#059669', weight: 3 }
const STORAGE_KEY = 'itree-canopy-ai:v1'
const STORAGE_VERSION = 2
const UNDO_LIMIT = 50
const DEFAULT_CENTER: [number, number] = [38.9072, -77.0369]
const DEFAULT_ZOOM = 17

const mapEl = ref<HTMLDivElement | null>(null)
const map = shallowRef<L.Map | null>(null)
const searchMarker = shallowRef<L.Marker | null>(null)

// The one shape being drawn before it's promoted into a full Area below --
// every area starts here, unanalyzed, and only gets its own id/runtime/
// result once "Analyze this area" succeeds.
const hasPolygon = ref(false)
const drawnItems = new L.FeatureGroup() // holds the pending shape when it's a polygon/freehand result
let pendingBoxShape: BoxShape | null = null // holds it instead when it's a resizable rectangle/ellipse

// Tracks whichever draw interaction (leaflet-draw's Polygon, or one of our
// own box/freehand ones) is currently in progress, so starting a new one or
// switching mode always cleanly cancels the last one instead of leaving
// stray listeners or a half-drawn preview behind.
let activeDraw: DrawHandle | null = null
function cancelActiveDraw() {
  activeDraw?.cancel()
  activeDraw = null
}

// --- Multi-area model --------------------------------------------------
//
// Each drawn-and-analyzed area gets a reactive `Area` (template-facing data:
// result/overrides/loading/error, all plain JSON-safe values Vue can proxy
// directly) and a parallel, non-reactive `AreaRuntime` holding its actual
// Leaflet objects (layers, BoxShape controllers, undo closures) -- the same
// split the single-area version already used (reactive `overrides` ref vs.
// plain `overrideLayers` Map), just keyed by area id instead of singleton.
interface Area {
  id: string
  loading: boolean
  applying: boolean
  error: string | null
  result: SegmentResponse | null
  overrides: OverrideRegion[]
  includeInCombined: boolean
}

interface AreaRuntime {
  aoiLayer: L.Polygon | null
  aoiBoxShape: BoxShape | null
  overlayLayer: L.ImageOverlay | null
  overrideLayers: Map<string, L.Path>
  overrideBoxShapes: Map<string, BoxShape>
  undoStack: Array<() => void>
  overrideSeq: number
}

const areas = ref<Area[]>([])
const runtimes = new Map<string, AreaRuntime>()
const activeAreaId = ref<string | null>(null)
const showCombined = ref(false)

const activeArea = computed(() => areas.value.find((a) => a.id === activeAreaId.value) ?? null)
const checkedAreas = computed(() => areas.value.filter((a) => a.includeInCombined && a.result))

/** Client-computed aggregate across whichever areas are checked -- summing
 * pixel counts (not averaging percentages) so a big checked area correctly
 * outweighs a small one, then re-deriving percentages from those totals.
 * Fields ResultsPanel doesn't render (analysis_id, bounds, overlay_png,
 * zoom, image dimensions) are unused placeholders: there's no single
 * backing analysis for a virtual combined result. */
const combinedResult = computed<SegmentResponse | null>(() => {
  const list = checkedAreas.value
  if (list.length === 0) return null
  const totalPixels = list.reduce((sum, a) => sum + a.result!.total_pixels, 0)
  const totalAreaM2 = list.reduce((sum, a) => sum + a.result!.area_m2, 0)
  const classes = {} as Record<ClassName, ClassStat>
  for (const name of CLASS_NAMES) {
    const pixels = list.reduce((sum, a) => sum + a.result!.classes[name].pixels, 0)
    classes[name] = { pixels, percent: totalPixels ? Math.round((10000 * pixels) / totalPixels) / 100 : 0 }
  }
  return {
    analysis_id: '',
    bounds: list[0].result!.bounds,
    zoom: 0,
    image_width: 0,
    image_height: 0,
    overlay_png: '',
    total_pixels: totalPixels,
    area_m2: Math.round(totalAreaM2 * 10) / 10,
    classes,
  }
})

function activeRuntime(): AreaRuntime | null {
  return activeAreaId.value ? (runtimes.get(activeAreaId.value) ?? null) : null
}

// Correction mode: user-drawn regions that override the automatic
// classification wherever they cover it, scoped to whichever area is
// active. armedClass/overrideShape/eraseActive/selectedOverrideId only ever
// describe the active area's editing session -- switching areas resets them.
const mode = ref<'view' | 'edit'>('view')
const armedClass = ref<ClassName | null>(null)
const overrideShape = ref<DrawShape>('polygon')
const eraseActive = ref(false)
const selectedOverrideId = ref<string | null>(null)
const selectedOverride = computed(() => activeArea.value?.overrides.find((o) => o.id === selectedOverrideId.value) ?? null)

// Left-button-down tracking (container-level, so it survives the pointer
// moving off one region's SVG path and onto another's) for erase-by-drag.
let erasePointerDown = false

// Undo history lives per-area (AreaRuntime.undoStack); `canUndo` mirrors
// just the active area's stack for the template/keyboard shortcut. Each
// entry is a closure that reverses exactly one edit; popping and calling it
// is enough because every edit function builds its own inverse at the point
// it has the "before" state in hand. A whole area's stack is cleared
// whenever its overrides are invalidated wholesale (destroyAreaOverrides)
// since old entries would otherwise resurrect regions after a fresh clear.
const canUndo = ref(false)

function pushUndo(rt: AreaRuntime, fn: () => void) {
  rt.undoStack.push(fn)
  if (rt.undoStack.length > UNDO_LIMIT) rt.undoStack.shift()
  if (rt === activeRuntime()) canUndo.value = true
}

function undo() {
  const rt = activeRuntime()
  if (!rt) return
  const fn = rt.undoStack.pop()
  canUndo.value = rt.undoStack.length > 0
  fn?.()
}

function toLatLng(p: LatLon): L.LatLng {
  return L.latLng(p.lat, p.lon)
}
function toLatLon(ll: L.LatLng): LatLon {
  return { lat: ll.lat, lon: ll.lng }
}
function overrideStyle(className: ClassName) {
  return { color: CLASS_COLORS[className].light, weight: 2.5, dashArray: '6 4', fillOpacity: 0.15 }
}

/** Rebuilds a correction region's layer/BoxShape from saved data and wires
 * it up exactly like a freshly-drawn one (minus re-hitting the backend --
 * callers sync once afterward). Shared by undo (restoring an erased/
 * cleared region) and by restoring persisted state on reload. Always adds
 * the layer to the map; callers restoring a non-active area hide it again
 * afterward via setOverridesVisible, same as any other area switch. */
function materializeOverride(areaId: string, o: OverrideRegion, shapeKind: BoxShapeKind | null) {
  const area = areas.value.find((a) => a.id === areaId)
  const rt = runtimes.get(areaId)
  if (!map.value || !area || !rt) return
  const style = overrideStyle(o.className)
  if (shapeKind) {
    const shape = boxShapeFromBounds(map.value, shapeKind, o.polygon.map(toLatLng), style)
    rt.overrideBoxShapes.set(o.id, shape)
    rt.overrideLayers.set(o.id, shape.layer)
    bindOverrideSelection(areaId, shape.layer, o.id)
  } else {
    const layer = L.polygon(o.polygon.map(toLatLng), style).addTo(map.value)
    rt.overrideLayers.set(o.id, layer)
    bindOverrideSelection(areaId, layer, o.id)
  }
  area.overrides.push({ ...o })
}

/** Shows/hides one area's correction-outline layers on the map -- only the
 * active area's are ever shown, so a click can never land on a shape owned
 * by an area other than the one currently being corrected. The base AOI
 * outline + classification overlay stay visible for every area regardless. */
function setOverridesVisible(areaId: string, visible: boolean) {
  const rt = runtimes.get(areaId)
  if (!rt || !map.value) return
  rt.overrideLayers.forEach((layer) => {
    if (visible) layer.addTo(map.value!)
    else map.value!.removeLayer(layer)
  })
  if (!visible) rt.overrideBoxShapes.forEach((s) => s.hideHandles())
}

function restyleAoi(areaId: string, active: boolean) {
  const rt = runtimes.get(areaId)
  if (!rt) return
  const weight = active ? 3 : 1.5
  if (rt.aoiBoxShape) rt.aoiBoxShape.setStyle({ weight })
  else rt.aoiLayer?.setStyle({ weight })
}

function setActiveArea(id: string) {
  if (activeAreaId.value === id && !showCombined.value) return
  stopEditing()
  if (activeAreaId.value) {
    restyleAoi(activeAreaId.value, false)
    setOverridesVisible(activeAreaId.value, false)
  }
  showCombined.value = false
  activeAreaId.value = id
  mode.value = 'view'
  restyleAoi(id, true)
  setOverridesVisible(id, true)
  canUndo.value = (runtimes.get(id)?.undoStack.length ?? 0) > 0
  schedulePersist()
}

function setCombinedView() {
  if (showCombined.value) return
  stopEditing()
  if (activeAreaId.value) {
    restyleAoi(activeAreaId.value, false)
    setOverridesVisible(activeAreaId.value, false)
  }
  showCombined.value = true
  mode.value = 'view'
  schedulePersist()
}

function deleteArea(areaId: string) {
  const wasActive = activeAreaId.value === areaId
  if (wasActive) stopEditing()

  const rt = runtimes.get(areaId)
  if (rt) {
    rt.overrideBoxShapes.forEach((s) => s.destroy())
    rt.overrideLayers.forEach((l) => map.value?.removeLayer(l))
    if (rt.overlayLayer) map.value?.removeLayer(rt.overlayLayer)
    if (rt.aoiBoxShape) rt.aoiBoxShape.destroy()
    else if (rt.aoiLayer) map.value?.removeLayer(rt.aoiLayer)
    runtimes.delete(areaId)
  }
  areas.value = areas.value.filter((a) => a.id !== areaId)

  if (wasActive) {
    activeAreaId.value = null
    showCombined.value = false
    mode.value = 'view'
    const next = areas.value[areas.value.length - 1]
    if (next) setActiveArea(next.id)
  }
  schedulePersist()
}

function clearAllAreas() {
  cancelActiveDraw()
  clearAoiShape()
  hasPolygon.value = false
  areas.value.forEach((a) => {
    const rt = runtimes.get(a.id)
    if (!rt) return
    rt.overrideBoxShapes.forEach((s) => s.destroy())
    rt.overrideLayers.forEach((l) => map.value?.removeLayer(l))
    if (rt.overlayLayer) map.value?.removeLayer(rt.overlayLayer)
    if (rt.aoiBoxShape) rt.aoiBoxShape.destroy()
    else if (rt.aoiLayer) map.value?.removeLayer(rt.aoiLayer)
  })
  runtimes.clear()
  areas.value = []
  activeAreaId.value = null
  showCombined.value = false
  mode.value = 'view'
  clearPersistedState()
}

// --- Persistence (resume on reload) -------------------------------------

interface PersistedOverride extends OverrideRegion {
  shapeKind: BoxShapeKind | null
}

interface AoiSnapshot {
  shapeKind: BoxShapeKind | null
  latlngs: LatLon[]
}

interface PersistedArea {
  id: string
  aoi: AoiSnapshot
  result: SegmentResponse
  overrides: PersistedOverride[]
  includeInCombined: boolean
}

interface PersistedState {
  version: number
  mapView: { lat: number; lon: number; zoom: number }
  pendingAoi: AoiSnapshot | null
  areas: PersistedArea[]
  activeAreaId: string | null
  showCombined: boolean
  overrideShape: DrawShape
}

function loadPersistedState(): PersistedState | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw) as PersistedState
    return parsed?.version === STORAGE_VERSION ? parsed : null
  } catch {
    return null
  }
}

function clearPersistedState() {
  try {
    localStorage.removeItem(STORAGE_KEY)
  } catch {
    // ignore
  }
}

function aoiSnapshotFor(rt: AreaRuntime): AoiSnapshot {
  if (rt.aoiBoxShape) return { shapeKind: rt.aoiBoxShape.kind, latlngs: rt.aoiBoxShape.getLatLngs().map(toLatLon) }
  return { shapeKind: null, latlngs: (rt.aoiLayer!.getLatLngs()[0] as L.LatLng[]).map(toLatLon) }
}

function currentPendingAoiSnapshot(): AoiSnapshot | null {
  if (pendingBoxShape) return { shapeKind: pendingBoxShape.kind, latlngs: pendingBoxShape.getLatLngs().map(toLatLon) }
  const layer = drawnItems.getLayers()[0] as L.Polygon | undefined
  if (!layer) return null
  return { shapeKind: null, latlngs: (layer.getLatLngs()[0] as L.LatLng[]).map(toLatLon) }
}

function persistState() {
  if (!map.value) return
  const center = map.value.getCenter()
  const state: PersistedState = {
    version: STORAGE_VERSION,
    mapView: { lat: center.lat, lon: center.lng, zoom: map.value.getZoom() },
    pendingAoi: currentPendingAoiSnapshot(),
    areas: areas.value
      .filter((a): a is Area & { result: SegmentResponse } => a.result !== null)
      .map((a) => {
        const rt = runtimes.get(a.id)!
        return {
          id: a.id,
          aoi: aoiSnapshotFor(rt),
          result: a.result,
          overrides: a.overrides.map((o) => ({ ...o, shapeKind: rt.overrideBoxShapes.get(o.id)?.kind ?? null })),
          includeInCombined: a.includeInCombined,
        }
      }),
    activeAreaId: activeAreaId.value,
    showCombined: showCombined.value,
    overrideShape: overrideShape.value,
  }
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state))
  } catch {
    // Storage full or unavailable (private browsing) -- losing "resume
    // where I left off" isn't worth surfacing an error for.
  }
}

let persistTimer: ReturnType<typeof setTimeout> | null = null
function schedulePersist() {
  if (persistTimer) clearTimeout(persistTimer)
  persistTimer = setTimeout(persistState, 300)
}

function restorePersistedState(saved: PersistedState) {
  for (const pa of saved.areas) {
    let aoiLayer: L.Polygon
    let aoiBoxShape: BoxShape | null = null
    if (pa.aoi.shapeKind) {
      aoiBoxShape = boxShapeFromBounds(map.value!, pa.aoi.shapeKind, pa.aoi.latlngs.map(toLatLng), AOI_STYLE)
      aoiLayer = aoiBoxShape.layer
    } else {
      aoiLayer = L.polygon(pa.aoi.latlngs.map(toLatLng), AOI_STYLE).addTo(map.value!)
    }
    runtimes.set(pa.id, {
      aoiLayer,
      aoiBoxShape,
      overlayLayer: null,
      overrideLayers: new Map(),
      overrideBoxShapes: new Map(),
      undoStack: [],
      overrideSeq: 0,
    })
    areas.value.push({
      id: pa.id,
      loading: false,
      applying: false,
      error: null,
      result: pa.result,
      overrides: [],
      includeInCombined: pa.includeInCombined,
    })
    updateOverlayForArea(pa.id)
    pa.overrides.forEach((o) => materializeOverride(pa.id, { id: o.id, className: o.className, polygon: o.polygon }, o.shapeKind))
  }

  overrideShape.value = saved.overrideShape
  activeAreaId.value = saved.areas.some((a) => a.id === saved.activeAreaId)
    ? saved.activeAreaId
    : (areas.value[areas.value.length - 1]?.id ?? null)
  showCombined.value = saved.showCombined && areas.value.length > 1

  for (const a of areas.value) {
    const isActiveShown = a.id === activeAreaId.value && !showCombined.value
    setOverridesVisible(a.id, isActiveShown)
    restyleAoi(a.id, isActiveShown)
  }

  if (saved.pendingAoi) {
    if (saved.pendingAoi.shapeKind) {
      finishAoiBoxShape(boxShapeFromBounds(map.value!, saved.pendingAoi.shapeKind, saved.pendingAoi.latlngs.map(toLatLng), AOI_STYLE))
    } else {
      finishAoiLayer(L.polygon(saved.pendingAoi.latlngs.map(toLatLng), AOI_STYLE))
    }
  }
}

onMounted(() => {
  if (!mapEl.value) return
  const saved = loadPersistedState()

  const m = L.map(mapEl.value, {
    center: saved ? [saved.mapView.lat, saved.mapView.lon] : DEFAULT_CENTER,
    zoom: saved ? saved.mapView.zoom : DEFAULT_ZOOM,
    zoomControl: false,
    // Left-drag panning is replaced by right-drag (setupRightClickPan
    // below) so left-click is always free for the draw tools instead of
    // the two fighting over the same button.
    dragging: false,
  })
  map.value = m

  L.control.zoom({ position: 'topright' }).addTo(m)
  setupRightClickPan(m)

  L.tileLayer(
    'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
    {
      maxZoom: 20,
      maxNativeZoom: 19,
      attribution: 'Imagery &copy; Esri &mdash; Esri, Maxar, Earthstar Geographics',
    },
  ).addTo(m)

  m.addLayer(drawnItems)
  m.on('moveend', () => schedulePersist())

  // Only the polygon tool (click-to-place-vertex) still runs on
  // leaflet-draw; rectangle/ellipse/freehand are our own pointer-event
  // based tools (see lib/mapDrawing.ts for why).
  m.on(L.Draw.Event.CREATED, (e) => {
    const layer = (e as L.DrawEvents.Created).layer as L.Polygon
    activeDraw = null

    if (mode.value === 'edit') {
      if (!armedClass.value || !activeAreaId.value) return
      addOverride(activeAreaId.value, armedClass.value, layer)
      return
    }

    finishAoiLayer(layer)
  })

  const container = m.getContainer()
  container.addEventListener('pointerdown', (e) => {
    if (e.button === 0) erasePointerDown = true
  })
  container.addEventListener('pointerup', () => {
    erasePointerDown = false
  })
  container.addEventListener('pointercancel', () => {
    erasePointerDown = false
  })

  document.addEventListener('keydown', onKeyDown)

  if (saved) restorePersistedState(saved)
})

onUnmounted(() => {
  cancelActiveDraw()
  document.removeEventListener('keydown', onKeyDown)
  map.value?.remove()
  map.value = null
})

function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false
  return target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable
}

function onKeyDown(e: KeyboardEvent) {
  if (isTypingTarget(e.target)) return
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'z') {
    e.preventDefault()
    undo()
  } else if (e.key === 'Escape' && armedClass.value) {
    stopEditing()
  }
}

function updateOverlayForArea(areaId: string) {
  const area = areas.value.find((a) => a.id === areaId)
  const rt = runtimes.get(areaId)
  if (!area?.result || !rt || !map.value) return
  if (rt.overlayLayer) map.value.removeLayer(rt.overlayLayer)
  const b = area.result.bounds
  const bounds = L.latLngBounds([b.south, b.west], [b.north, b.east])
  rt.overlayLayer = L.imageOverlay(area.result.overlay_png, bounds, { opacity: 1 }).addTo(map.value)
}

function destroyAreaOverrides(areaId: string) {
  const area = areas.value.find((a) => a.id === areaId)
  const rt = runtimes.get(areaId)
  if (!area || !rt) return
  rt.overrideBoxShapes.forEach((shape) => shape.destroy())
  rt.overrideBoxShapes.clear()
  rt.overrideLayers.forEach((layer) => map.value?.removeLayer(layer))
  rt.overrideLayers.clear()
  area.overrides = []
  rt.undoStack.length = 0
  if (areaId === activeAreaId.value) canUndo.value = false
}

function clearAoiShape() {
  drawnItems.clearLayers()
  pendingBoxShape?.destroy()
  pendingBoxShape = null
}

function clearPendingAoi() {
  cancelActiveDraw()
  clearAoiShape()
  hasPolygon.value = false
  schedulePersist()
}

function finishAoiLayer(layer: L.Polygon) {
  clearAoiShape()
  drawnItems.addLayer(layer)
  hasPolygon.value = true
  schedulePersist()
}

function finishAoiBoxShape(shape: BoxShape) {
  clearAoiShape()
  pendingBoxShape = shape
  shape.showHandles()
  hasPolygon.value = true
  schedulePersist()
}

function startAoiDraw(shape: DrawShape) {
  if (!map.value) return
  cancelActiveDraw()

  if (shape === 'polygon') {
    const handler = new L.Draw.Polygon(map.value as unknown as L.DrawMap, { allowIntersection: false, shapeOptions: AOI_STYLE })
    handler.enable()
    activeDraw = { cancel: () => handler.disable() }
  } else if (shape === 'rectangle' || shape === 'ellipse') {
    activeDraw = startBoxDraw(map.value, shape, AOI_STYLE, (boxShape) => {
      activeDraw = null
      finishAoiBoxShape(boxShape)
    })
  } else {
    activeDraw = startFreehandDraw(map.value, AOI_STYLE, (latlngs) => {
      activeDraw = null
      finishAoiLayer(L.polygon(latlngs, AOI_STYLE))
    })
  }
}

async function onAnalyze() {
  if (!map.value) return
  const drawnLayer = drawnItems.getLayers()[0] as L.Polygon | undefined
  const latlngs = pendingBoxShape ? pendingBoxShape.getLatLngs() : (drawnLayer?.getLatLngs()[0] as L.LatLng[] | undefined)
  if (!latlngs || latlngs.length < 3) return

  pendingBoxShape?.hideHandles()

  let aoiLayer: L.Polygon
  let aoiBoxShape: BoxShape | null
  if (pendingBoxShape) {
    aoiBoxShape = pendingBoxShape
    aoiLayer = aoiBoxShape.layer
  } else {
    aoiBoxShape = null
    aoiLayer = L.polygon(latlngs, AOI_STYLE).addTo(map.value)
  }
  clearAoiShape()
  hasPolygon.value = false

  const id = crypto.randomUUID()
  runtimes.set(id, {
    aoiLayer,
    aoiBoxShape,
    overlayLayer: null,
    overrideLayers: new Map(),
    overrideBoxShapes: new Map(),
    undoStack: [],
    overrideSeq: 0,
  })
  areas.value.push({ id, loading: true, applying: false, error: null, result: null, overrides: [], includeInCombined: true })
  // Re-read through the reactive array rather than mutating the plain
  // object literal above directly -- Vue only tracks writes that go through
  // the reactive proxy it wraps pushed objects in, so holding onto the raw
  // literal and writing to it later updates the data but never triggers a
  // re-render (exactly the "doesn't show until I click away and back" bug).
  const area = areas.value.find((a) => a.id === id)!
  setActiveArea(id)

  try {
    area.result = await fetchSegment(latlngs.map(toLatLon))
    updateOverlayForArea(id)
  } catch (e) {
    area.error = e instanceof Error ? e.message : 'Analysis failed'
  } finally {
    area.loading = false
  }
  schedulePersist()
}

function onSearchSelect(target: { lat: number; lon: number; boundingbox: [number, number, number, number] | null }) {
  if (!map.value) return

  if (searchMarker.value) {
    map.value.removeLayer(searchMarker.value)
  }
  searchMarker.value = L.marker([target.lat, target.lon]).addTo(map.value)

  if (target.boundingbox) {
    const [south, north, west, east] = target.boundingbox
    map.value.fitBounds([
      [south, west],
      [north, east],
    ])
  } else {
    map.value.setView([target.lat, target.lon], 18)
  }
}

// --- Correction mode -------------------------------------------------

function setMode(next: 'view' | 'edit') {
  stopEditing()
  mode.value = next
}

function setEraseActive(active: boolean) {
  eraseActive.value = active
  if (map.value) map.value.getContainer().style.cursor = active ? 'crosshair' : ''
}

function toggleErase() {
  if (mode.value !== 'edit' || !activeAreaId.value) return
  cancelActiveDraw()
  armedClass.value = null
  setSelected(null)
  setEraseActive(!eraseActive.value)
}

/** Fully exits correction-drawing: cancels any in-progress shape, drops the
 * armed class (ending continuous-draw mode), exits erase, and deselects. */
function stopEditing() {
  cancelActiveDraw()
  armedClass.value = null
  setEraseActive(false)
  setSelected(null)
}

function setSelected(id: string | null) {
  const rt = activeRuntime()
  const area = activeArea.value
  if (!rt || !area) {
    selectedOverrideId.value = null
    return
  }
  const prevId = selectedOverrideId.value
  if (prevId && prevId !== id) {
    rt.overrideLayers.get(prevId)?.setStyle({ weight: 2.5 })
    rt.overrideBoxShapes.get(prevId)?.hideHandles()
  }
  selectedOverrideId.value = id
  if (!id) return

  armedClass.value = null
  rt.overrideLayers.get(id)?.setStyle({ weight: 4 })

  const shape = rt.overrideBoxShapes.get(id)
  if (shape) {
    let before = shape.getCorners()
    shape.showHandles(() => {
      const override = area.overrides.find((o) => o.id === id)
      if (!override) return
      const [beforeC1, beforeC2] = before
      pushUndo(rt, () => {
        shape.setCorners(beforeC1, beforeC2)
        override.polygon = shape.getLatLngs().map(toLatLon)
        void syncOverrides(area.id)
      })
      before = shape.getCorners()
      override.polygon = shape.getLatLngs().map(toLatLon)
      void syncOverrides(area.id)
    })
  }
}

function armClass(className: ClassName) {
  if (!map.value || !activeAreaId.value) return
  cancelActiveDraw()
  setSelected(null)
  setEraseActive(false)
  armedClass.value = className

  const style = overrideStyle(className)
  const shape = overrideShape.value

  if (shape === 'polygon') {
    const handler = new L.Draw.Polygon(map.value as unknown as L.DrawMap, { allowIntersection: false, shapeOptions: style })
    handler.enable()
    activeDraw = { cancel: () => handler.disable() }
  } else if (shape === 'rectangle' || shape === 'ellipse') {
    activeDraw = startBoxDraw(map.value, shape, style, (boxShape) => {
      activeDraw = null
      if (activeAreaId.value) addOverrideBoxShape(activeAreaId.value, className, boxShape)
    })
  } else {
    activeDraw = startFreehandDraw(map.value, style, (latlngs) => {
      activeDraw = null
      if (activeAreaId.value) addOverride(activeAreaId.value, className, L.polygon(latlngs))
    })
  }
}

/** Switching tools mid-correction restarts drawing immediately with
 * whichever class is still armed, matching the "keep drawing without
 * reselecting the class" behavior below. */
function setOverrideShape(shape: DrawShape) {
  overrideShape.value = shape
  if (armedClass.value) armClass(armedClass.value)
}

function bindOverrideSelection(areaId: string, layer: L.Path, id: string) {
  layer.on('mousedown', (e) => {
    if (!eraseActive.value || e.originalEvent.button !== 0 || areaId !== activeAreaId.value) return
    L.DomEvent.stopPropagation(e)
    eraseOverride(areaId, id)
  })
  layer.on('mouseover', () => {
    if (eraseActive.value && erasePointerDown && areaId === activeAreaId.value) eraseOverride(areaId, id)
  })
  layer.on('click', (e) => {
    L.DomEvent.stopPropagation(e)
    if (mode.value === 'edit' && !eraseActive.value && areaId === activeAreaId.value) setSelected(id)
  })
}

function addOverride(areaId: string, className: ClassName, layer: L.Polygon) {
  const area = areas.value.find((a) => a.id === areaId)
  const rt = runtimes.get(areaId)
  if (!map.value || !area || !rt) return
  const id = crypto.randomUUID()
  const polygon = (layer.getLatLngs()[0] as L.LatLng[]).map(toLatLon)
  area.overrides.push({ id, className, polygon })

  layer.setStyle(overrideStyle(className))
  layer.addTo(map.value)
  bindOverrideSelection(areaId, layer, id)
  rt.overrideLayers.set(id, layer)

  pushUndo(rt, () => {
    map.value?.removeLayer(layer)
    rt.overrideLayers.delete(id)
    area.overrides = area.overrides.filter((o) => o.id !== id)
    if (selectedOverrideId.value === id) setSelected(null)
    void syncOverrides(areaId)
  })

  void syncOverrides(areaId)
  // Stay armed with the same class + tool so multiple regions of one class
  // can be drawn back-to-back without reselecting it each time.
  armClass(className)
}

function addOverrideBoxShape(areaId: string, className: ClassName, shape: BoxShape) {
  const area = areas.value.find((a) => a.id === areaId)
  const rt = runtimes.get(areaId)
  if (!area || !rt) return
  const id = crypto.randomUUID()
  const polygon = shape.getLatLngs().map(toLatLon)
  area.overrides.push({ id, className, polygon })

  bindOverrideSelection(areaId, shape.layer, id)
  rt.overrideLayers.set(id, shape.layer)
  rt.overrideBoxShapes.set(id, shape)

  pushUndo(rt, () => {
    shape.destroy()
    rt.overrideBoxShapes.delete(id)
    rt.overrideLayers.delete(id)
    area.overrides = area.overrides.filter((o) => o.id !== id)
    if (selectedOverrideId.value === id) setSelected(null)
    void syncOverrides(areaId)
  })

  void syncOverrides(areaId)
  armClass(className)
}

function reassignSelected(className: ClassName) {
  const area = activeArea.value
  const rt = activeRuntime()
  const override = selectedOverride.value
  if (!area || !rt || !override || override.className === className) return
  const prevClassName = override.className
  override.className = className
  rt.overrideLayers.get(override.id)?.setStyle({ color: CLASS_COLORS[className].light })

  pushUndo(rt, () => {
    override.className = prevClassName
    rt.overrideLayers.get(override.id)?.setStyle({ color: CLASS_COLORS[prevClassName].light })
    void syncOverrides(area.id)
  })

  void syncOverrides(area.id)
}

function eraseOverride(areaId: string, id: string) {
  const area = areas.value.find((a) => a.id === areaId)
  const rt = runtimes.get(areaId)
  if (!area || !rt) return
  const override = area.overrides.find((o) => o.id === id)
  if (!override) return
  const snapshot: OverrideRegion = { ...override }
  const shapeKind = rt.overrideBoxShapes.get(id)?.kind ?? null

  const boxShape = rt.overrideBoxShapes.get(id)
  if (boxShape) {
    boxShape.destroy()
    rt.overrideBoxShapes.delete(id)
  } else {
    const layer = rt.overrideLayers.get(id)
    if (layer) map.value?.removeLayer(layer)
  }
  rt.overrideLayers.delete(id)
  area.overrides = area.overrides.filter((o) => o.id !== id)
  if (selectedOverrideId.value === id) setSelected(null)

  pushUndo(rt, () => {
    materializeOverride(areaId, snapshot, shapeKind)
    void syncOverrides(areaId)
  })

  void syncOverrides(areaId)
}

function deleteSelected() {
  if (activeAreaId.value && selectedOverrideId.value) eraseOverride(activeAreaId.value, selectedOverrideId.value)
}

function clearAllOverrides() {
  const area = activeArea.value
  const rt = activeRuntime()
  if (!area || !rt || area.overrides.length === 0) return
  const snapshot = area.overrides.map((o) => ({ region: { ...o }, shapeKind: rt.overrideBoxShapes.get(o.id)?.kind ?? null }))
  destroyAreaOverrides(area.id)
  setSelected(null)
  pushUndo(rt, () => {
    snapshot.forEach(({ region, shapeKind }) => materializeOverride(area.id, region, shapeKind))
    void syncOverrides(area.id)
  })
  void syncOverrides(area.id)
}

async function syncOverrides(areaId: string) {
  const area = areas.value.find((a) => a.id === areaId)
  const rt = runtimes.get(areaId)
  if (!area || !rt || !area.result) return
  const seq = ++rt.overrideSeq
  area.applying = true
  area.error = null
  try {
    const body = await fetchSegmentOverrides(area.result.analysis_id, area.overrides)
    if (seq === rt.overrideSeq) {
      area.result = body
      updateOverlayForArea(areaId)
    }
  } catch (e) {
    if (seq === rt.overrideSeq) area.error = e instanceof Error ? e.message : 'Could not apply correction'
  } finally {
    if (seq === rt.overrideSeq) area.applying = false
  }
  schedulePersist()
}
</script>

<template>
  <div class="flex h-screen w-screen overflow-hidden bg-neutral-100">
    <div class="relative flex-1">
      <div ref="mapEl" class="h-full w-full"></div>

      <div v-if="mode === 'view'" class="absolute top-4 left-4 z-[1000] flex flex-col gap-1 rounded-lg bg-white p-1.5 shadow-md">
        <button
          v-for="s in DRAW_SHAPES"
          :key="s"
          type="button"
          :title="DRAW_SHAPE_META[s].label + (s === 'ellipse' ? ' (hold Shift for a perfect circle)' : '')"
          class="flex h-8 w-8 items-center justify-center rounded-md text-neutral-600 hover:bg-neutral-100 hover:text-emerald-700"
          @click="startAoiDraw(s)"
        >
          <i class="fa-solid" :class="DRAW_SHAPE_META[s].icon"></i>
        </button>
      </div>

      <div
        v-if="!hasPolygon && areas.length === 0"
        class="pointer-events-none absolute top-4 left-1/2 z-[1000] w-max max-w-[90%] -translate-x-1/2 rounded-lg bg-white/95 px-4 py-2 text-sm text-neutral-700 shadow-md"
      >
        <i class="fa-solid fa-draw-polygon mr-1.5 text-emerald-600"></i>
        Pick a tool at top-left — polygon, rectangle, freehand, or circle/oval — and outline the area
        you want to analyze. Drag out a rectangle or oval, then drag its corner handles to resize
        before analyzing.
      </div>

      <div
        v-if="hasPolygon"
        class="absolute bottom-6 left-1/2 z-[1000] flex -translate-x-1/2 gap-2"
      >
        <button
          type="button"
          class="rounded-lg bg-emerald-600 px-4 py-2 text-sm font-medium text-white shadow-lg hover:bg-emerald-700"
          @click="onAnalyze"
        >
          <i class="fa-solid fa-tree mr-1.5"></i>Analyze this area
        </button>
        <button
          type="button"
          class="rounded-lg bg-white px-3 py-2 text-sm font-medium text-neutral-600 shadow-lg hover:bg-neutral-50"
          @click="clearPendingAoi"
        >
          Clear
        </button>
      </div>
    </div>

    <aside class="flex w-96 shrink-0 flex-col gap-4 overflow-y-auto border-l border-neutral-200 bg-white p-4">
      <div>
        <h1 class="text-lg font-semibold text-neutral-900">
          <i class="fa-solid fa-tree mr-1.5 text-emerald-600"></i>CanopyCensus
        </h1>
        <p class="text-xs text-neutral-500">Instant canopy cover via image segmentation</p>
      </div>

      <SearchBox @select="onSearchSelect" />

      <div v-if="areas.length > 0" class="flex flex-col gap-1 rounded-lg border border-neutral-200 p-2">
        <div
          v-for="(area, i) in areas"
          :key="area.id"
          class="flex items-center gap-2 rounded-md px-1.5 py-1 text-sm"
          :class="!showCombined && activeAreaId === area.id ? 'bg-emerald-50 ring-1 ring-emerald-300' : 'hover:bg-neutral-50'"
        >
          <input
            v-model="area.includeInCombined"
            type="checkbox"
            title="Include in combined view"
            class="shrink-0 accent-emerald-600"
            @change="schedulePersist"
          />
          <button type="button" class="flex-1 truncate text-left" @click="setActiveArea(area.id)">
            <span class="font-medium text-neutral-800">Area {{ i + 1 }}</span>
            <span v-if="area.loading" class="ml-1 text-neutral-400">Analyzing&hellip;</span>
            <span v-else-if="area.error" class="ml-1 text-red-500">Failed</span>
            <span v-else-if="area.result" class="ml-1 text-neutral-500">{{ area.result.classes.tree.percent.toFixed(1) }}% tree</span>
          </button>
          <button type="button" title="Delete this area" class="shrink-0 px-1 text-neutral-400 hover:text-red-600" @click="deleteArea(area.id)">
            <i class="fa-solid fa-trash-can"></i>
          </button>
        </div>

        <button
          v-if="areas.length > 1"
          type="button"
          class="mt-1 rounded-md py-1.5 text-sm font-medium transition-colors"
          :class="showCombined ? 'bg-emerald-600 text-white' : 'bg-neutral-100 text-neutral-600 hover:bg-neutral-200'"
          @click="setCombinedView"
        >
          <i class="fa-solid fa-layer-group mr-1.5"></i>Combined ({{ checkedAreas.length }} selected)
        </button>
      </div>

      <div v-if="activeArea && !showCombined && activeArea.result" class="flex gap-1 rounded-lg bg-neutral-100 p-1 text-sm">
        <button
          type="button"
          class="flex-1 rounded-md py-1.5 font-medium transition-colors"
          :class="mode === 'view' ? 'bg-white text-neutral-900 shadow-sm' : 'text-neutral-500 hover:text-neutral-700'"
          @click="setMode('view')"
        >
          View results
        </button>
        <button
          type="button"
          class="flex-1 rounded-md py-1.5 font-medium transition-colors"
          :class="mode === 'edit' ? 'bg-white text-neutral-900 shadow-sm' : 'text-neutral-500 hover:text-neutral-700'"
          @click="setMode('edit')"
        >
          Correct classifications
        </button>
      </div>

      <template v-if="showCombined">
        <ResultsPanel v-if="combinedResult" :result="combinedResult" :title="`Combined results (${checkedAreas.length} of ${areas.length} areas)`" />
        <div v-else class="rounded-lg border border-dashed border-neutral-300 p-4 text-sm text-neutral-500">
          No areas are checked for the combined view — check at least one area above.
        </div>
      </template>

      <template v-else-if="activeArea">
        <div v-if="activeArea.error" class="rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-700">
          <i class="fa-solid fa-triangle-exclamation mr-1"></i>{{ activeArea.error }}
        </div>

        <div v-else-if="activeArea.loading" class="flex flex-col items-center gap-2 rounded-lg border border-neutral-200 p-6 text-center text-sm text-neutral-500">
          <i class="fa-solid fa-satellite fa-spin text-xl text-emerald-600"></i>
          Fetching imagery and segmenting&hellip;
        </div>

        <template v-else-if="activeArea.result">
          <ClassPicker
            v-if="mode === 'edit'"
            :armed-class="armedClass"
            :selected-override="selectedOverride"
            :override-count="activeArea.overrides.length"
            :shape="overrideShape"
            :erase-active="eraseActive"
            :can-undo="canUndo"
            @arm-class="armClass"
            @reassign="reassignSelected"
            @delete-selected="deleteSelected"
            @deselect="stopEditing"
            @clear-all="clearAllOverrides"
            @set-shape="setOverrideShape"
            @toggle-erase="toggleErase"
            @undo="undo"
          />
          <p v-if="mode === 'edit' && selectedOverride && activeRuntime()?.overrideBoxShapes.has(selectedOverride.id)" class="-mt-2 text-xs text-neutral-500">
            Drag the corner handles on the map to resize this region.
          </p>
          <div :class="{ 'pointer-events-none opacity-60': activeArea.applying }" class="transition-opacity">
            <ResultsPanel :result="activeArea.result" />
          </div>
        </template>
      </template>

      <div v-else class="rounded-lg border border-dashed border-neutral-300 p-4 text-sm text-neutral-500">
        <p class="font-medium text-neutral-700">How to use this tool</p>
        <ol class="mt-2 list-decimal space-y-1 pl-4">
          <li>Search for a place, or pan/zoom the map manually.</li>
          <li>Draw your area of interest with any tool at top-left.</li>
          <li>Click <span class="font-medium">Analyze this area</span>.</li>
        </ol>
        <p class="mt-2">
          Draw as many separate areas as you like — each is analyzed independently. View any one's
          results, or check several in the list above to see their combined totals.
        </p>
        <p class="mt-2">
          Afterwards, switch to <span class="font-medium">Correct classifications</span> to manually
          label any area yourself — draw over just the big, obvious regions and leave the small,
          fiddly details to the automatic result, or paint over the whole thing if you'd rather
          classify it entirely by hand.
        </p>
      </div>

      <button
        v-if="hasPolygon || areas.length > 0"
        type="button"
        class="mt-auto rounded-lg border border-neutral-200 px-3 py-2 text-sm font-medium text-neutral-600 hover:bg-neutral-50"
        @click="clearAllAreas"
      >
        <i class="fa-solid fa-rotate-left mr-1.5"></i>Clear all areas
      </button>
    </aside>
  </div>
</template>

<style>
.leaflet-control-layers,
.leaflet-bar {
  z-index: 900;
}

/* Dragging to draw shouldn't trigger native text selection or let the map
   tiles (plain <img> elements) start a native "drag this image" gesture. */
.leaflet-container {
  user-select: none;
  -webkit-user-select: none;
}
.leaflet-container img {
  -webkit-user-drag: none;
  user-drag: none;
}

.shape-resize-handle {
  width: 10px;
  height: 10px;
  box-sizing: border-box;
  background: #ffffff;
  border: 2px solid #1f2937;
  border-radius: 2px;
  cursor: pointer;
}
</style>
