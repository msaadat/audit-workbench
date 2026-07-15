<script setup lang="ts">
withDefaults(defineProps<{ railWidth?: string; empty?: boolean }>(), {
  railWidth: '17rem',
  empty: false,
})
</script>

<template>
  <div class="ui-master-detail" :class="{ 'ui-master-detail--empty': empty }" :style="{ '--ui-rail-width': railWidth }">
    <aside v-if="!empty" class="ui-master-detail__rail"><slot name="rail" /></aside>
    <main class="ui-master-detail__content"><slot /></main>
  </div>
</template>

