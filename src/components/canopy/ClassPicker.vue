<script setup lang="ts">
import { computed } from 'vue'
import {
  CLASS_COLORS,
  CLASS_LABELS,
  CLASS_NAMES,
  DRAW_SHAPES,
  DRAW_SHAPE_META,
  type ClassName,
  type DrawShape,
  type OverrideRegion,
} from '../../types/canopy'

const props = defineProps<{
  armedClass: ClassName | null
  selectedOverride: OverrideRegion | null
  overrideCount: number
  shape: DrawShape
  eraseActive: boolean
  canUndo: boolean
}>()

const emit = defineEmits<{
  'arm-class': [className: ClassName]
  reassign: [className: ClassName]
  'delete-selected': []
  deselect: []
  'clear-all': []
  'set-shape': [shape: DrawShape]
  'toggle-erase': []
  undo: []
}>()

const activeClass = computed(() => props.selectedOverride?.className ?? props.armedClass)

function onPick(name: ClassName) {
  if (props.selectedOverride) emit('reassign', name)
  else emit('arm-class', name)
}
</script>

<template>
  <div class="flex flex-col gap-3 rounded-lg border border-emerald-200 bg-emerald-50/60 p-3">
    <div class="flex items-start justify-between gap-2">
      <div>
        <p class="text-sm font-semibold text-neutral-900">Correction mode</p>
        <p v-if="selectedOverride" class="mt-0.5 text-xs text-neutral-600">
          Region selected (labeled <span class="font-medium">{{ CLASS_LABELS[selectedOverride.className] }}</span>).
          Pick a class to relabel it.
        </p>
        <p v-else-if="eraseActive" class="mt-0.5 text-xs text-neutral-600">
          Erase tool active &mdash; click or drag over a region you drew to remove it.
        </p>
        <p v-else class="mt-0.5 text-xs text-neutral-600">
          Pick a class and a shape, then draw over the area you want to set &mdash; you can keep
          drawing more regions of the same class without reselecting it. Anything you don't cover
          keeps the automatic guess.
        </p>
      </div>
      <button
        v-if="canUndo"
        type="button"
        title="Undo (Ctrl+Z)"
        class="shrink-0 rounded-md p-1.5 text-neutral-500 hover:bg-white hover:text-neutral-700"
        @click="emit('undo')"
      >
        <i class="fa-solid fa-rotate-left"></i>
      </button>
    </div>

    <div class="flex gap-1.5">
      <template v-if="!selectedOverride">
        <button
          v-for="s in DRAW_SHAPES"
          :key="s"
          type="button"
          :title="DRAW_SHAPE_META[s].label"
          class="flex flex-1 items-center justify-center rounded-md border py-1.5 text-sm transition-colors"
          :class="
            !eraseActive && shape === s
              ? 'border-emerald-500 bg-white text-emerald-700 ring-1 ring-emerald-500'
              : 'border-neutral-200 bg-white text-neutral-500 hover:border-neutral-300'
          "
          @click="emit('set-shape', s)"
        >
          <i class="fa-solid" :class="DRAW_SHAPE_META[s].icon"></i>
        </button>
      </template>
      <button
        v-if="overrideCount > 0"
        type="button"
        title="Erase tool &mdash; click or drag over a region to delete it"
        class="flex h-8 w-9 shrink-0 items-center justify-center rounded-md border transition-colors"
        :class="
          eraseActive
            ? 'border-red-400 bg-red-50 text-red-600 ring-1 ring-red-400'
            : 'border-neutral-200 bg-white text-neutral-500 hover:border-neutral-300'
        "
        @click="emit('toggle-erase')"
      >
        <i class="fa-solid fa-eraser"></i>
      </button>
    </div>

    <div class="grid grid-cols-2 gap-1.5">
      <button
        v-for="name in CLASS_NAMES"
        :key="name"
        type="button"
        class="flex items-center gap-1.5 rounded-md border px-2 py-1.5 text-left text-xs font-medium transition-colors"
        :class="
          activeClass === name
            ? 'border-emerald-500 bg-white ring-1 ring-emerald-500'
            : 'border-neutral-200 bg-white hover:border-neutral-300'
        "
        @click="onPick(name)"
      >
        <span class="inline-block h-2.5 w-2.5 shrink-0 rounded-full" :style="{ backgroundColor: CLASS_COLORS[name].light }"></span>
        {{ CLASS_LABELS[name] }}
      </button>
    </div>

    <div v-if="selectedOverride || armedClass" class="flex gap-2">
      <button
        v-if="selectedOverride"
        type="button"
        class="flex-1 rounded-md border border-red-200 bg-white px-2 py-1.5 text-xs font-medium text-red-600 hover:bg-red-50"
        @click="emit('delete-selected')"
      >
        <i class="fa-solid fa-trash mr-1"></i>Delete region
      </button>
      <button
        type="button"
        class="flex-1 rounded-md border border-neutral-200 bg-white px-2 py-1.5 text-xs font-medium text-neutral-600 hover:bg-neutral-50"
        @click="emit('deselect')"
      >
        Done
      </button>
    </div>

    <div v-else-if="overrideCount > 0" class="flex items-center justify-between text-xs text-neutral-500">
      <span>{{ overrideCount }} region{{ overrideCount === 1 ? '' : 's' }} corrected</span>
      <button type="button" class="font-medium text-neutral-500 underline hover:text-neutral-700" @click="emit('clear-all')">
        Clear all
      </button>
    </div>
  </div>
</template>
