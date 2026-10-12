<script setup lang="ts">
import { computed, ref } from 'vue'
import { CLASS_COLORS, CLASS_LABELS, CLASS_NAMES, standardError, type SegmentResponse } from '../../types/canopy'

const props = defineProps<{
  result: SegmentResponse
  title?: string
}>()

const sampleSize = ref(1000)

const rows = computed(() => {
  return CLASS_NAMES.map((name) => {
    const stat = props.result.classes[name]
    const se = standardError(stat.percent, sampleSize.value)
    return {
      name,
      label: CLASS_LABELS[name],
      color: CLASS_COLORS[name].light,
      percent: stat.percent,
      se,
      barLow: Math.max(0, stat.percent - se),
      barHigh: Math.min(100, stat.percent + se),
    }
  })
})

const areaLabel = computed(() => {
  const m2 = props.result.area_m2
  if (m2 >= 10000) return `${(m2 / 10000).toFixed(2)} ha (${(m2 / 4046.86).toFixed(2)} ac)`
  return `${m2.toFixed(0)} m²`
})
</script>

<template>
  <div class="flex flex-col gap-4 text-sm">
    <div>
      <h2 class="text-base font-semibold text-neutral-900">{{ title ?? 'Canopy cover results' }}</h2>
      <p class="mt-0.5 text-neutral-500">
        {{ areaLabel }} analyzed &middot; {{ result.total_pixels.toLocaleString() }} classified pixels
      </p>
    </div>

    <ul class="flex flex-col gap-3">
      <li v-for="row in rows" :key="row.name">
        <div class="mb-1 flex items-baseline justify-between gap-2">
          <span class="flex items-center gap-1.5 font-medium text-neutral-800">
            <span class="inline-block h-2.5 w-2.5 shrink-0 rounded-full" :style="{ backgroundColor: row.color }"></span>
            {{ row.label }}
          </span>
          <span class="whitespace-nowrap tabular-nums text-neutral-700">
            {{ row.percent.toFixed(1) }}%
            <span class="text-neutral-400">&plusmn; {{ row.se.toFixed(1) }}%</span>
          </span>
        </div>
        <div class="relative h-2.5 w-full overflow-hidden rounded-full bg-neutral-100">
          <div
            class="absolute inset-y-0 left-0 rounded-full"
            :style="{ width: row.percent + '%', backgroundColor: row.color }"
          ></div>
          <div
            class="absolute inset-y-0 rounded-full bg-black/25"
            :style="{ left: row.barLow + '%', width: Math.max(0, row.barHigh - row.barLow) + '%' }"
          ></div>
        </div>
      </li>
    </ul>

    <div class="rounded-lg border border-neutral-200 bg-neutral-50 p-3">
      <div class="flex items-center justify-between gap-2">
        <label for="sample-size" class="font-medium text-neutral-700">Equivalent sample size (n)</label>
        <span class="tabular-nums text-neutral-600">{{ sampleSize.toLocaleString() }} pts</span>
      </div>
      <input
        id="sample-size"
        v-model.number="sampleSize"
        type="range"
        min="100"
        max="5000"
        step="100"
        class="mt-2 w-full accent-emerald-600"
      />
    </div>

    <p class="text-xs leading-snug text-neutral-400">
      Classification uses color/texture/shape rules (no near-infrared band), so accuracy varies by
      class. Use the map overlay to visually spot-check results.
    </p>
  </div>
</template>
