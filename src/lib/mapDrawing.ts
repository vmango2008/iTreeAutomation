import L from 'leaflet'

/**
 * Freehand and box (rectangle/ellipse) drawing aren't in leaflet-draw's
 * stock tool set (and leaflet-draw's own Rectangle tool turned out
 * unreliable here — see below), so all of it is implemented directly on
 * Pointer Events with explicit capture rather than by subclassing
 * leaflet-draw's internal handler classes or hand-rolling mouse-event
 * tracking.
 *
 * A few things that broke with a naive version of this:
 *
 * 1. Calling `map.dragging.disable()` *inside* the mousedown handler was
 *    too late: Leaflet's own left-drag panning is wired to that same
 *    mousedown and could already start negotiating a pan for that same
 *    press, racing our handler and sometimes ending the gesture the
 *    instant it started (mousedown "immediately released"). Fixed by not
 *    racing at all: left-drag panning is disabled permanently at the map
 *    level (see `setupRightClickPan`) rather than toggled per gesture, so
 *    there's nothing left-drag ever does that these tools could race.
 * 2. Manually-attached document-level mousemove/mouseup listeners can miss
 *    events (fast drags, iframes, whatever grabs focus). Pointer capture
 *    (`setPointerCapture`) is the browser-native fix: once captured, that
 *    element is *guaranteed* to keep receiving events for that pointer
 *    until an explicit up/cancel, regardless of where the cursor actually
 *    is on screen.
 * 3. Dragging over a page without preventing the browser's own default
 *    drag handling triggers native text-selection highlighting and
 *    image-ghost dragging (the map tiles are `<img>` elements). Both are
 *    prevented below (`preventDefault` on pointerdown/pointermove) as well
 *    as with the CSS in MapView.vue that turns off selection and native
 *    image dragging across the whole map container.
 */

export interface DrawHandle {
  cancel(): void
}

export interface ShapeStyle {
  color: string
  weight?: number
  dashArray?: string
  fillOpacity?: number
}

export type BoxShapeKind = 'rectangle' | 'ellipse'

const MIN_SHAPE_EXTENT_PX = 12
const ELLIPSE_RING_POINTS = 72
const HANDLE_SIZE_PX = 10

function toShapeOptions(style: ShapeStyle): L.PolylineOptions {
  return { color: style.color, weight: style.weight ?? 2.5, dashArray: style.dashArray, fillOpacity: style.fillOpacity ?? 0.15 }
}

function ringForBox(map: L.Map, p1: L.Point, p2: L.Point, kind: BoxShapeKind): L.LatLng[] {
  if (kind === 'rectangle') {
    return [L.point(p1.x, p1.y), L.point(p2.x, p1.y), L.point(p2.x, p2.y), L.point(p1.x, p2.y)].map((p) =>
      map.containerPointToLatLng(p),
    )
  }
  const cx = (p1.x + p2.x) / 2
  const cy = (p1.y + p2.y) / 2
  const rx = Math.abs(p2.x - p1.x) / 2
  const ry = Math.abs(p2.y - p1.y) / 2
  const ring: L.LatLng[] = []
  for (let i = 0; i < ELLIPSE_RING_POINTS; i++) {
    const t = (i / ELLIPSE_RING_POINTS) * 2 * Math.PI
    ring.push(map.containerPointToLatLng(L.point(cx + rx * Math.cos(t), cy + ry * Math.sin(t))))
  }
  return ring
}

// Which real corner each of the 4 visual box corners is sourced from, so
// dragging one updates the right axis of the right corner(s). 1 and 2 refer
// to corner1/corner2 below; corners 0 and 2 (the ones matching a real
// corner on both axes) move that corner outright, corners 1 and 3 (the
// "mixed" ones) each move one axis of each stored corner.
const HANDLE_SOURCE = [
  { xFrom: 1, yFrom: 1 },
  { xFrom: 2, yFrom: 1 },
  { xFrom: 2, yFrom: 2 },
  { xFrom: 1, yFrom: 2 },
] as const

/** A rectangle or ellipse defined by two opposite corners, re-editable via
 * on-map drag handles after being placed. */
export class BoxShape {
  readonly kind: BoxShapeKind
  readonly layer: L.Polygon
  private map: L.Map
  private corner1: L.LatLng
  private corner2: L.LatLng
  private handles: L.Marker[] = []

  constructor(map: L.Map, kind: BoxShapeKind, corner1: L.LatLng, corner2: L.LatLng, style: ShapeStyle) {
    this.map = map
    this.kind = kind
    this.corner1 = corner1
    this.corner2 = corner2
    this.layer = L.polygon(this.computeRing(), toShapeOptions(style)).addTo(map)
  }

  private computeRing(): L.LatLng[] {
    const p1 = this.map.latLngToContainerPoint(this.corner1)
    const p2 = this.map.latLngToContainerPoint(this.corner2)
    return ringForBox(this.map, p1, p2, this.kind)
  }

  private redraw() {
    this.layer.setLatLngs(this.computeRing())
    this.repositionHandles()
  }

  getLatLngs(): L.LatLng[] {
    return this.computeRing()
  }

  /** The two opposite corners that define this shape, e.g. for snapshotting
   * before a resize so the drag can be undone with `setCorners`. */
  getCorners(): [L.LatLng, L.LatLng] {
    return [this.corner1, this.corner2]
  }

  setCorners(corner1: L.LatLng, corner2: L.LatLng) {
    this.corner1 = corner1
    this.corner2 = corner2
    this.redraw()
  }

  setStyle(style: Partial<ShapeStyle>) {
    this.layer.setStyle(style)
  }

  get handlesVisible() {
    return this.handles.length > 0
  }

  showHandles(onChange?: () => void) {
    if (this.handlesVisible || !this.map) return
    const icon = L.divIcon({
      className: 'shape-resize-handle',
      iconSize: [HANDLE_SIZE_PX, HANDLE_SIZE_PX],
      iconAnchor: [HANDLE_SIZE_PX / 2, HANDLE_SIZE_PX / 2],
    })

    this.handles = HANDLE_SOURCE.map((_, i) => {
      const marker = L.marker(this.corner1, { icon, draggable: false, zIndexOffset: 1000 }).addTo(this.map)
      const el = marker.getElement()
      if (el) this.bindHandleDrag(el, i, onChange)
      return marker
    })
    this.repositionHandles()
  }

  hideHandles() {
    this.handles.forEach((h) => this.map.removeLayer(h))
    this.handles = []
  }

  private repositionHandles() {
    if (!this.handlesVisible) return
    const p1 = this.map.latLngToContainerPoint(this.corner1)
    const p2 = this.map.latLngToContainerPoint(this.corner2)
    const pixels = [L.point(p1.x, p1.y), L.point(p2.x, p1.y), L.point(p2.x, p2.y), L.point(p1.x, p2.y)]
    this.handles.forEach((marker, i) => marker.setLatLng(this.map.containerPointToLatLng(pixels[i])))
  }

  private applyHandleMove(handleIndex: number, newPoint: L.Point) {
    const p1 = this.map.latLngToContainerPoint(this.corner1)
    const p2 = this.map.latLngToContainerPoint(this.corner2)
    const { xFrom, yFrom } = HANDLE_SOURCE[handleIndex]
    const nextP1 = L.point(p1.x, p1.y)
    const nextP2 = L.point(p2.x, p2.y)
    if (xFrom === 1) nextP1.x = newPoint.x
    else nextP2.x = newPoint.x
    if (yFrom === 1) nextP1.y = newPoint.y
    else nextP2.y = newPoint.y
    this.corner1 = this.map.containerPointToLatLng(nextP1)
    this.corner2 = this.map.containerPointToLatLng(nextP2)
  }

  private bindHandleDrag(el: HTMLElement, handleIndex: number, onChange?: () => void) {
    const onMove = (ev: PointerEvent) => {
      ev.preventDefault()
      this.applyHandleMove(handleIndex, this.map.mouseEventToContainerPoint(ev))
      this.redraw()
    }
    const onUp = (ev: PointerEvent) => {
      el.releasePointerCapture(ev.pointerId)
      el.removeEventListener('pointermove', onMove)
      el.removeEventListener('pointerup', onUp)
      onChange?.()
    }
    el.addEventListener('pointerdown', (ev: PointerEvent) => {
      if (ev.button !== 0) return
      ev.stopPropagation()
      ev.preventDefault()
      el.setPointerCapture(ev.pointerId)
      el.addEventListener('pointermove', onMove)
      el.addEventListener('pointerup', onUp)
    })
  }

  destroy() {
    this.hideHandles()
    this.map.removeLayer(this.layer)
  }
}

/** Rebuilds a BoxShape from a previously-saved ring (undo, or restoring
 * persisted state), using the ring's own bounding box as the two corners --
 * exact for a rectangle, and exact for an ellipse too since its ring is
 * itself always generated from a bounding box (see `ringForBox`), so the
 * min/max corners of either kind's ring reconstruct the original shape. */
export function boxShapeFromBounds(map: L.Map, kind: BoxShapeKind, latlngs: L.LatLng[], style: ShapeStyle): BoxShape {
  const lats = latlngs.map((ll) => ll.lat)
  const lngs = latlngs.map((ll) => ll.lng)
  const corner1 = L.latLng(Math.min(...lats), Math.min(...lngs))
  const corner2 = L.latLng(Math.max(...lats), Math.max(...lngs))
  return new BoxShape(map, kind, corner1, corner2, style)
}

export function startBoxDraw(
  map: L.Map,
  kind: BoxShapeKind,
  style: ShapeStyle,
  onComplete: (shape: BoxShape) => void,
): DrawHandle {
  const container = map.getContainer()
  const previousCursor = container.style.cursor
  container.style.cursor = 'crosshair'

  let preview: L.Polygon | null = null
  let startPoint: L.Point | null = null
  let pointerId: number | null = null

  function teardown() {
    container.style.cursor = previousCursor
    if (preview) {
      map.removeLayer(preview)
      preview = null
    }
    if (pointerId !== null) container.releasePointerCapture(pointerId)
    container.removeEventListener('pointerdown', onDown)
    container.removeEventListener('pointermove', onMove)
    container.removeEventListener('pointerup', onUp)
    document.removeEventListener('keydown', onKeyDown)
  }

  function onKeyDown(e: KeyboardEvent) {
    if (e.key === 'Escape') teardown()
  }

  function onDown(e: PointerEvent) {
    if (e.button !== 0) return
    e.preventDefault()
    startPoint = map.mouseEventToContainerPoint(e)
    pointerId = e.pointerId
    container.setPointerCapture(e.pointerId)
    preview = L.polygon(ringForBox(map, startPoint, startPoint, kind), toShapeOptions(style)).addTo(map)
  }

  function onMove(e: PointerEvent) {
    if (!startPoint || !preview) return
    e.preventDefault()
    let currentPoint = map.mouseEventToContainerPoint(e)
    if (e.shiftKey) {
      const dx = currentPoint.x - startPoint.x
      const dy = currentPoint.y - startPoint.y
      const s = Math.max(Math.abs(dx), Math.abs(dy))
      currentPoint = L.point(startPoint.x + (dx < 0 ? -s : s), startPoint.y + (dy < 0 ? -s : s))
    }
    preview.setLatLngs(ringForBox(map, startPoint, currentPoint, kind))
  }

  function onUp(e: PointerEvent) {
    if (!startPoint) return
    const endPoint = map.mouseEventToContainerPoint(e)
    const extentPx = startPoint.distanceTo(endPoint)
    const finalCorner1 = map.containerPointToLatLng(startPoint)
    const finalCorner2 = map.containerPointToLatLng(endPoint)

    teardown()

    if (extentPx > MIN_SHAPE_EXTENT_PX) {
      onComplete(new BoxShape(map, kind, finalCorner1, finalCorner2, style))
    }
  }

  container.addEventListener('pointerdown', onDown)
  container.addEventListener('pointermove', onMove)
  container.addEventListener('pointerup', onUp)
  document.addEventListener('keydown', onKeyDown)

  return { cancel: teardown }
}

export function startFreehandDraw(map: L.Map, style: ShapeStyle, onComplete: (latlngs: L.LatLng[]) => void): DrawHandle {
  const container = map.getContainer()
  const previousCursor = container.style.cursor
  container.style.cursor = 'crosshair'

  let latlngs: L.LatLng[] = []
  let preview: L.Polygon | null = null
  let pointerId: number | null = null

  function teardown() {
    container.style.cursor = previousCursor
    if (preview) {
      map.removeLayer(preview)
      preview = null
    }
    if (pointerId !== null) container.releasePointerCapture(pointerId)
    container.removeEventListener('pointerdown', onDown)
    container.removeEventListener('pointermove', onMove)
    container.removeEventListener('pointerup', onUp)
    document.removeEventListener('keydown', onKeyDown)
  }

  function onKeyDown(e: KeyboardEvent) {
    if (e.key === 'Escape') teardown()
  }

  function onDown(e: PointerEvent) {
    if (e.button !== 0) return
    e.preventDefault()
    pointerId = e.pointerId
    container.setPointerCapture(e.pointerId)
    latlngs = [map.containerPointToLatLng(map.mouseEventToContainerPoint(e))]
    preview = L.polygon(latlngs, toShapeOptions(style)).addTo(map)
  }

  function onMove(e: PointerEvent) {
    if (!preview) return
    e.preventDefault()
    const point = map.mouseEventToContainerPoint(e)
    const last = latlngs[latlngs.length - 1]
    const lastPoint = last ? map.latLngToContainerPoint(last) : null
    if (!lastPoint || point.distanceTo(lastPoint) > 4) {
      latlngs.push(map.containerPointToLatLng(point))
      preview.setLatLngs(latlngs)
    }
  }

  function onUp() {
    if (!preview) return
    const bounds = preview.getBounds()
    const extentPx = bounds.isValid()
      ? map.latLngToContainerPoint(bounds.getNorthEast()).distanceTo(map.latLngToContainerPoint(bounds.getSouthWest()))
      : 0
    const finalLatLngs = latlngs

    teardown()

    if (finalLatLngs.length >= 3 && extentPx > MIN_SHAPE_EXTENT_PX) {
      onComplete(finalLatLngs)
    }
  }

  container.addEventListener('pointerdown', onDown)
  container.addEventListener('pointermove', onMove)
  container.addEventListener('pointerup', onUp)
  document.addEventListener('keydown', onKeyDown)

  return { cancel: teardown }
}

/**
 * Right-button drag-to-pan, replacing Leaflet's default left-drag panning
 * (which is disabled by the caller, permanently, at map creation — see
 * MapView.vue). Left-click stays free for the draw tools above at all
 * times, instead of the two fighting over the same button.
 */
export function setupRightClickPan(map: L.Map): () => void {
  const container = map.getContainer()

  let lastPoint: { x: number; y: number } | null = null
  let pointerId: number | null = null

  function onContextMenu(e: MouseEvent) {
    e.preventDefault()
  }

  function onDown(e: PointerEvent) {
    if (e.button !== 2) return
    e.preventDefault()
    lastPoint = { x: e.clientX, y: e.clientY }
    pointerId = e.pointerId
    container.setPointerCapture(e.pointerId)
    container.style.cursor = 'grabbing'
  }

  function onMove(e: PointerEvent) {
    if (!lastPoint) return
    e.preventDefault()
    const dx = e.clientX - lastPoint.x
    const dy = e.clientY - lastPoint.y
    map.panBy(L.point(-dx, -dy), { animate: false })
    lastPoint = { x: e.clientX, y: e.clientY }
  }

  function onUp() {
    if (!lastPoint) return
    lastPoint = null
    if (pointerId !== null) container.releasePointerCapture(pointerId)
    container.style.cursor = ''
  }

  container.addEventListener('contextmenu', onContextMenu)
  container.addEventListener('pointerdown', onDown)
  container.addEventListener('pointermove', onMove)
  container.addEventListener('pointerup', onUp)

  return () => {
    container.removeEventListener('contextmenu', onContextMenu)
    container.removeEventListener('pointerdown', onDown)
    container.removeEventListener('pointermove', onMove)
    container.removeEventListener('pointerup', onUp)
  }
}
