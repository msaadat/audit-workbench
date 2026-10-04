<script setup lang="ts">
import { ref } from 'vue'
import Button from 'primevue/button'
import Menu from 'primevue/menu'
import type { MenuItem } from 'primevue/menuitem'

withDefaults(defineProps<{ items: MenuItem[]; label?: string; tooltip?: string }>(), {
  tooltip: 'More actions',
})
const menu = ref<InstanceType<typeof Menu> | null>(null)
</script>

<template>
  <Button
    :label="label"
    icon="aw-icon aw-icon-ellipsis-vertical"
    iconPos="right"
    severity="secondary"
    text
    size="small"
    aria-haspopup="true"
    :aria-label="tooltip"
    v-tooltip.bottom="tooltip"
    @click="menu?.toggle($event)"
  />
  <Menu ref="menu" :model="items" popup />
</template>

