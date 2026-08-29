<script setup lang="ts">
defineProps<{ title: string }>()
</script>

<template>
  <header class="ui-page-header">
    <div class="ui-page-header__copy">
      <h2>{{ title }}</h2>
    </div>
    <div class="ui-page-header__actions"><slot /></div>
  </header>
</template>
