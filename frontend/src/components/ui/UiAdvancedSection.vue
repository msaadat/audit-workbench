<script setup lang="ts">
// `icon` defaults to the settings glyph the "Advanced" sections use; a section
// disclosing something other than options passes its own.
withDefaults(defineProps<{ title?: string; description?: string; open?: boolean; icon?: string }>(), {
  icon: 'aw-icon aw-icon-sliders-horizontal',
})
</script>

<template>
  <details class="ui-advanced" :open="open">
    <summary>
      <span><i :class="icon" />{{ title || 'Advanced' }}</span>
      <small v-if="description">{{ description }}</small>
      <i class="aw-icon aw-icon-chevron-down ui-advanced__chevron" />
    </summary>
    <div class="ui-advanced__body"><slot /></div>
  </details>
</template>

