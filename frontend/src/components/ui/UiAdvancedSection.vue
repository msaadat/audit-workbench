<script setup lang="ts">
defineProps<{ title?: string; description?: string; open?: boolean }>()
</script>

<template>
  <details class="ui-advanced" :open="open">
    <summary>
      <span><i class="pi pi-sliders-h" />{{ title || 'Advanced' }}</span>
      <small v-if="description">{{ description }}</small>
      <i class="pi pi-chevron-down ui-advanced__chevron" />
    </summary>
    <div class="ui-advanced__body"><slot /></div>
  </details>
</template>

