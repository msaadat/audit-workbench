<script setup lang="ts">
import type { AgentMilestone } from '../../types'

const props = defineProps<{ milestone: AgentMilestone }>()

function display(value: string | number | boolean | null) {
  if (typeof value === 'boolean') return value ? 'Yes' : 'No'
  return value == null ? '—' : String(value)
}
</script>

<template>
  <article class="milestone" :data-status="props.milestone.status">
    <span class="icon">
      <i :class="props.milestone.status === 'completed' ? 'pi pi-check' : 'pi pi-exclamation-circle'" />
    </span>
    <div class="body">
      <strong>{{ props.milestone.headline }}</strong>
      <p>{{ props.milestone.summary }}</p>
      <dl v-if="props.milestone.metrics.length" class="metrics">
        <div v-for="item in props.milestone.metrics" :key="item.label">
          <dt>{{ item.label }}</dt>
          <dd>{{ display(item.value) }}</dd>
        </div>
      </dl>
      <ul v-if="props.milestone.highlights.length" class="highlights">
        <li v-for="item in props.milestone.highlights" :key="`${item.label}:${item.detail}`" :data-severity="item.severity">
          <i :class="item.severity === 'error' ? 'pi pi-times-circle' : 'pi pi-exclamation-triangle'" />
          <span><b>{{ item.label }}</b><small>{{ item.detail }}</small></span>
        </li>
      </ul>
    </div>
  </article>
</template>

<style scoped>
.milestone{display:grid;grid-template-columns:1.7rem minmax(0,1fr);gap:.55rem;align-self:flex-start;width:min(92%,42rem);padding:.65rem .75rem;border:1px solid var(--aw-border);border-radius:10px;background:var(--p-surface-50)}
.icon{display:grid;place-items:center;width:1.7rem;height:1.7rem;border-radius:7px;background:var(--aw-teal-soft);color:var(--aw-teal);font-size:.72rem}
.milestone[data-status='completed_with_issues'] .icon,.milestone[data-status='needs_review'] .icon{background:var(--aw-warn-soft);color:var(--aw-warn)}
.body{display:grid;gap:.28rem;min-width:0}.body>strong{font-size:.8rem}.body>p{margin:0;color:var(--aw-muted);font-size:.74rem;line-height:1.45}
.metrics{display:flex;flex-wrap:wrap;gap:.3rem .75rem;margin:.2rem 0 0}.metrics div{display:flex;gap:.25rem;font-size:.66rem}.metrics dt{color:var(--aw-muted)}.metrics dd{margin:0;font-weight:600}
.highlights{display:grid;gap:.25rem;margin:.25rem 0 0;padding:0;list-style:none}.highlights li{display:grid;grid-template-columns:.8rem minmax(0,1fr);gap:.35rem;color:var(--aw-warn);font-size:.68rem}.highlights li[data-severity='error']{color:var(--aw-danger)}.highlights i{padding-top:.12rem;font-size:.62rem}.highlights span{display:grid}.highlights b{font-weight:600}.highlights small{color:var(--aw-muted);line-height:1.35}
</style>
