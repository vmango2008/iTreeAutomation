<script setup lang="ts">
import { ref } from 'vue'
import { useGeocode } from '../../composables/useSegmentation'
import type { GeocodeResult } from '../../types/canopy'

const emit = defineEmits<{
  select: [target: { lat: number; lon: number; boundingbox: [number, number, number, number] | null }]
}>()

const query = ref('')
const results = ref<GeocodeResult[]>([])
const showResults = ref(false)
const { searching, searchError, search } = useGeocode()

const COORD_PATTERN = /^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$/

async function onSubmit() {
  const raw = query.value.trim()
  if (!raw) return

  const coordMatch = raw.match(COORD_PATTERN)
  if (coordMatch) {
    const lat = Number(coordMatch[1])
    const lon = Number(coordMatch[2])
    if (Math.abs(lat) <= 90 && Math.abs(lon) <= 180) {
      results.value = []
      showResults.value = false
      emit('select', { lat, lon, boundingbox: null })
      return
    }
  }

  results.value = await search(raw)
  showResults.value = true
}

function pick(r: GeocodeResult) {
  showResults.value = false
  query.value = r.display_name
  emit('select', { lat: r.lat, lon: r.lon, boundingbox: r.boundingbox })
}
</script>

<template>
  <div class="relative w-full max-w-md">
    <form class="flex items-center gap-2" @submit.prevent="onSubmit">
      <div class="relative flex-1">
        <i class="fa-solid fa-magnifying-glass pointer-events-none absolute top-1/2 left-3 -translate-y-1/2 text-sm text-neutral-400"></i>
        <input
          v-model="query"
          type="text"
          placeholder="Search address, place, or 'lat, lon'"
          class="w-full rounded-lg border border-neutral-300 bg-white py-2 pr-3 pl-9 text-sm text-neutral-900 shadow-sm outline-none placeholder:text-neutral-400 focus:border-emerald-500 focus:ring-1 focus:ring-emerald-500"
          @focus="showResults = results.length > 0"
        />
      </div>
      <button
        type="submit"
        class="shrink-0 rounded-lg bg-emerald-600 px-3 py-2 text-sm font-medium text-white shadow-sm hover:bg-emerald-700 disabled:opacity-50"
        :disabled="searching"
      >
        <i v-if="searching" class="fa-solid fa-spinner fa-spin"></i>
        <span v-else>Go</span>
      </button>
    </form>

    <div
      v-if="showResults"
      class="absolute z-[1100] mt-1 w-full overflow-hidden rounded-lg border border-neutral-200 bg-white shadow-lg"
    >
      <p v-if="searchError" class="px-3 py-2 text-sm text-red-600">{{ searchError }}</p>
      <p v-else-if="results.length === 0" class="px-3 py-2 text-sm text-neutral-500">No matches found.</p>
      <ul v-else class="max-h-64 overflow-y-auto">
        <li v-for="(r, i) in results" :key="i">
          <button
            type="button"
            class="block w-full px-3 py-2 text-left text-sm text-neutral-700 hover:bg-emerald-50"
            @click="pick(r)"
          >
            {{ r.display_name }}
          </button>
        </li>
      </ul>
    </div>
  </div>
</template>
