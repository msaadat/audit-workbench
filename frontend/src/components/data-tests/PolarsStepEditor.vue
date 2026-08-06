<script setup lang="ts">
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import Textarea from 'primevue/textarea'

import type { DataTestStep } from '../../types'
import CodeEditor from '../CodeEditor.vue'
import { emptyPolarsStep } from './steps'

const steps = defineModel<DataTestStep[]>({ required: true })

function add() { steps.value.push(emptyPolarsStep()) }
function remove(index: number) { steps.value.splice(index, 1) }
</script>

<template>
  <div class="steps-author">
    <div class="steps-header">
      <p>Steps</p>
      <Button label="Add step" icon="pi pi-plus" text size="small" @click="add" />
    </div>
    <div v-for="(step, index) in steps" :key="index" class="step-card">
      <div class="step-card-head">
        <span>Step {{ index + 1 }}</span>
        <Button
          icon="pi pi-trash"
          text
          rounded
          size="small"
          severity="danger"
          :disabled="steps.length <= 1"
          aria-label="Remove step"
          @click="remove(index)"
        />
      </div>
      <label>Label<InputText v-model="step.label" /></label>
      <label>Instruction<Textarea v-model="step.instruction" rows="2" autoResize /></label>
      <label>
        Code
        <small>Every workspace table and join is in scope. Rows the step returns are its exceptions.</small>
        <CodeEditor v-model="step.code" />
      </label>
    </div>
  </div>
</template>

<style scoped>
.steps-author { display: flex; flex-direction: column; gap: 0.6rem; min-width: 0; }
.steps-header { display: flex; align-items: center; justify-content: space-between; gap: 0.5rem; }
.steps-header p { margin: 0; color: var(--aw-muted); font-size: var(--aw-text-xs); font-weight: 700; }
.step-card { display: flex; flex-direction: column; gap: 0.45rem; min-width: 0; padding: 0.7rem; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-control); background: var(--aw-canvas); }
.step-card-head { display: flex; align-items: center; justify-content: space-between; color: var(--aw-muted); font-size: var(--aw-text-xs); font-weight: 600; }
label { display: flex; flex-direction: column; gap: 0.3rem; min-width: 0; color: var(--aw-ink-soft); font-size: var(--aw-text-sm); font-weight: 600; }
label small { color: var(--aw-muted); font-weight: 400; }
label :deep(.p-inputtext), label :deep(.p-textarea) { width: 100%; min-width: 0; }
</style>
