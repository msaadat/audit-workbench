<script setup lang="ts">
import { ref } from 'vue'
import Button from 'primevue/button'
import Menu from 'primevue/menu'
import type { MenuItem } from 'primevue/menuitem'

/**
 * One Export button for a written work product, opening the forms it can be
 * taken away in — Word, print or PDF, Markdown. They were becoming a button
 * each in a header that already carries Edit and Regenerate.
 */
withDefaults(defineProps<{ items: MenuItem[]; loading?: boolean }>(), { loading: false })
const menu = ref<InstanceType<typeof Menu> | null>(null)
</script>

<template>
  <Button
    label="Export"
    icon="aw-icon aw-icon-download"
    size="small"
    outlined
    severity="secondary"
    aria-haspopup="true"
    :loading="loading"
    @click="menu?.toggle($event)"
  />
  <Menu ref="menu" :model="items" popup />
</template>
