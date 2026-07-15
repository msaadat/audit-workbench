<script setup lang="ts">
withDefaults(defineProps<{ icon?: string; title: string; description?: string; compact?: boolean }>(), {
  icon: 'pi pi-inbox',
  compact: false,
})
</script>

<template>
  <div class="ui-empty-state" :class="{ 'ui-empty-state--compact': compact }">
    <span class="ui-empty-state__icon"><i :class="icon" /></span>
    <h3>{{ title }}</h3>
    <p v-if="description">{{ description }}</p>
    <div v-if="$slots.default" class="ui-empty-state__actions"><slot /></div>
  </div>
</template>

