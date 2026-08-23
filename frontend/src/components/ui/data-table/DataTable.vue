<script setup lang="ts">
import { ref, computed } from 'vue'
import { cn } from '@/lib/utils'

export interface Column {
  key: string
  label: string
  sortable?: boolean
  numeric?: boolean
  width?: string
}

export interface DataTableRow {
  [key: string]: unknown
}

const props = withDefaults(defineProps<{
  columns: Column[]
  rows: DataTableRow[]
  loading?: boolean
  loadingRows?: number
  rowClickable?: boolean
}>(), {
  loading: false,
  loadingRows: 5,
  rowClickable: true,
})

const emit = defineEmits<{
  'row-click': [row: DataTableRow]
}>()

const sortColumn = ref<string | null>(null)
const sortDirection = ref<'asc' | 'desc'>('asc')

function toggleSort(key: string) {
  if (sortColumn.value === key) {
    sortDirection.value = sortDirection.value === 'asc' ? 'desc' : 'asc'
  } else {
    sortColumn.value = key
    sortDirection.value = 'asc'
  }
}

function getSortIndicator(key: string): string {
  if (sortColumn.value !== key) return ''
  return sortDirection.value === 'asc' ? ' ▲' : ' ▼'
}

const sortedRows = computed(() => {
  if (!sortColumn.value) return props.rows
  const col = props.columns.find(c => c.key === sortColumn.value)
  if (!col?.sortable) return props.rows

  const key = sortColumn.value
  const dir = sortDirection.value === 'asc' ? 1 : -1

  return [...props.rows].sort((a, b) => {
    const aVal = a[key]
    const bVal = b[key]
    if (aVal == null && bVal == null) return 0
    if (aVal == null) return 1
    if (bVal == null) return -1

    if (typeof aVal === 'number' && typeof bVal === 'number') {
      return (aVal - bVal) * dir
    }
    const aStr = String(aVal)
    const bStr = String(bVal)
    return aStr.localeCompare(bStr) * dir
  })
})

function onRowClick(row: DataTableRow) {
  if (props.rowClickable) emit('row-click', row)
}

function onRowKeydown(event: KeyboardEvent, row: DataTableRow) {
  if (!props.rowClickable) return
  if (event.key === 'Enter' || event.key === ' ') {
    event.preventDefault()
    emit('row-click', row)
  }
}
</script>

<template>
  <div class="relative w-full overflow-x-auto">
    <table class="w-full caption-bottom text-sm">
      <thead>
        <tr class="border-b transition-colors hover:bg-muted/50">
          <th
            v-for="col in columns"
            :key="col.key"
            :class="cn(
              'h-12 px-4 text-left align-middle font-medium text-muted-foreground',
              col.numeric && 'text-right tabular-nums',
              col.sortable && 'cursor-pointer select-none hover:text-foreground',
              sortColumn === col.key ? 'text-foreground' : 'text-muted-foreground',
            )"
            @click="col.sortable && toggleSort(col.key)"
          >
            {{ col.label }}<span v-if="col.sortable" class="text-xs ml-0.5">{{ getSortIndicator(col.key) }}</span>
          </th>
        </tr>
      </thead>
      <tbody v-if="loading" class="[&_tr:last-child]:border-0">
        <tr v-for="i in loadingRows" :key="i" class="border-b transition-colors">
          <td v-for="col in columns" :key="col.key" :class="cn('p-4 align-middle', col.numeric && 'text-right')">
            <div class="h-4 animate-pulse rounded bg-muted" :style="{ width: `${30 + (i * 7) % 50}%` }" />
          </td>
        </tr>
      </tbody>
      <tbody v-else-if="rows.length === 0" class="[&_tr:last-child]:border-0">
        <tr class="border-b transition-colors">
          <td :colspan="columns.length" class="p-4 align-middle text-center text-sm text-muted-foreground">
            <slot name="empty">
              No data available.
            </slot>
          </td>
        </tr>
      </tbody>
      <tbody v-else class="[&_tr:last-child]:border-0">
        <tr
          v-for="(row, index) in sortedRows"
          :key="index"
          class="border-b transition-colors hover:bg-muted/50"
          :class="props.rowClickable && 'cursor-pointer'"
          :role="props.rowClickable ? 'button' : undefined"
          :tabindex="props.rowClickable ? 0 : undefined"
          @click="onRowClick(row)"
          @keydown="onRowKeydown($event, row)"
        >
          <td
            v-for="col in columns"
            :key="col.key"
            :class="cn(
              'p-4 align-middle text-sm',
              col.numeric && 'text-right tabular-nums',
            )"
          >
            <slot :name="`cell-${col.key}`" :row="row" :value="row[col.key]">
              {{ row[col.key] }}
            </slot>
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>
