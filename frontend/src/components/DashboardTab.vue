<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useConfirm } from 'primevue/useconfirm'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import InputText from 'primevue/inputtext'
import Skeleton from 'primevue/skeleton'
import Tag from 'primevue/tag'
import Textarea from 'primevue/textarea'

import { api, ApiError } from '../api'
import { useAgentRun } from '../composables/useAgentRun'
import { workspaceQuery } from '../composables/useWorkspaceNavigation'
import type {
  DashboardAction,
  DashboardAdvice,
  DashboardPayload,
  DashboardTarget,
  DashboardTile,
  WorkspaceSummary,
} from '../types'
import ChartView from './ChartView.vue'
import UiOverflowMenu from './ui/UiOverflowMenu.vue'

const props = defineProps<{ workspace: WorkspaceSummary }>()
const emit = defineEmits<{ 'import-requested': [] }>()

const router = useRouter()
const toast = useToast()
const confirm = useConfirm()
const agent = useAgentRun(props.workspace.id)

const board = ref<DashboardPayload | null>(null)
const loading = ref(true)
const adviceLoading = ref(false)
const adviceError = ref('')
const flashed = ref<Record<string, boolean>>({})
const editing = ref<DashboardTile | null>(null)
const editTitle = ref('')
const editNote = ref('')

const tiles = computed(() => board.value?.tiles ?? [])
const overview = computed(() => board.value?.overview)
const emptyWorkspace = computed(() => Boolean(overview.value && overview.value.tables === 0 && overview.value.documents === 0))
const primaryAction = computed(() => board.value?.actions[0] ?? null)
const verdictCounts = computed(() => ({
  ok: tiles.value.filter((tile) => tile.verdict === 'ok').length,
  warn: tiles.value.filter((tile) => tile.verdict === 'warn').length,
  fail: tiles.value.filter((tile) => tile.verdict === 'fail' || tile.error).length,
}))

const verdictSeverity: Record<string, string> = { ok: 'success', warn: 'warn', fail: 'danger', info: 'info' }
const tileIcon: Record<string, string> = {
  analytics: 'pi pi-shield', query: 'pi pi-search', python: 'pi pi-code',
  pivot: 'pi pi-table', validation: 'pi pi-check-square',
}
const tileKindLabel: Record<string, string> = {
  analytics: 'Analytics test', query: 'Query', python: 'Python snippet',
  pivot: 'Pivot table', validation: 'Validation rules',
}

function fail(summary: string, error: unknown) {
  const detail = error instanceof ApiError ? error.message : String(error)
  toast.add({ severity: 'error', summary, detail, life: 6000 })
}

async function load() {
  loading.value = true
  try {
    board.value = await api.get<DashboardPayload>(`/api/workspaces/${props.workspace.id}/dashboard`)
  } catch (error) {
    fail('Dashboard failed to load', error)
  } finally {
    loading.value = false
  }
}

function navigate(target: DashboardTarget) {
  void router.replace({ query: workspaceQuery(target.tab, target.query) })
}

function runAction(action: DashboardAction) {
  if (action.id === 'import-sources') emit('import-requested')
  else navigate(action.target)
}

async function refreshAdvice() {
  adviceLoading.value = true
  adviceError.value = ''
  try {
    const advice = await api.post<DashboardAdvice>(`/api/workspaces/${props.workspace.id}/dashboard/advice`)
    if (board.value) board.value.ai_advice = advice
  } catch (error) {
    adviceError.value = error instanceof ApiError ? error.message : String(error)
  } finally {
    adviceLoading.value = false
  }
}

async function patchTile(tile: DashboardTile, changes: Record<string, unknown>) {
  try {
    await api.patch(`/api/workspaces/${props.workspace.id}/tiles/${tile.id}`, changes)
    await load()
  } catch (error) {
    fail('Update failed', error)
  }
}

function openEdit(tile: DashboardTile) {
  editing.value = tile
  editTitle.value = tile.title
  editNote.value = tile.note
}

async function saveEdit() {
  if (!editing.value) return
  await patchTile(editing.value, { title: editTitle.value, note: editNote.value })
  editing.value = null
}

function remove(tile: DashboardTile) {
  confirm.require({
    header: 'Remove tile', message: `Remove "${tile.title}" from the dashboard?`,
    icon: 'pi pi-exclamation-triangle',
    acceptProps: { label: 'Remove', severity: 'danger' },
    rejectProps: { label: 'Cancel', severity: 'secondary', outlined: true },
    accept: async () => {
      try {
        await api.del(`/api/workspaces/${props.workspace.id}/tiles/${tile.id}`)
        await load()
      } catch (error) { fail('Remove failed', error) }
    },
  })
}

function tileActions(tile: DashboardTile, index: number) {
  return [
    { label: 'Move left', icon: 'pi pi-chevron-left', disabled: index === 0, command: () => void patchTile(tile, { move: -1 }) },
    { label: 'Move right', icon: 'pi pi-chevron-right', disabled: index === tiles.value.length - 1, command: () => void patchTile(tile, { move: 1 }) },
    { label: 'Rename or add note', icon: 'pi pi-pencil', command: () => openEdit(tile) },
    { separator: true },
    { label: 'Remove from dashboard', icon: 'pi pi-times', command: () => remove(tile) },
  ]
}

onMounted(async () => {
  await Promise.all([load(), agent.state.status ? Promise.resolve() : agent.refreshStatus()])
})
defineExpose({ load })

const unsubscribe = agent.onWorkspaceChanged(async (change) => {
  const refreshKinds = new Set(['tile', 'table', 'join', 'planning', 'rcm', 'planned_test', 'datatest', 'doctest', 'finding', 'report'])
  if (!refreshKinds.has(change.kind)) return
  await load()
  if (change.kind === 'tile' && change.action !== 'removed') {
    flashed.value = { ...flashed.value, [change.id]: true }
    window.setTimeout(() => {
      const { [change.id]: _gone, ...rest } = flashed.value
      flashed.value = rest
    }, 2500)
  }
})
onUnmounted(unsubscribe)
</script>

<template>
  <div class="dashboard-heading">
    <div>
      <h2>Audit dashboard</h2>
    </div>
    <Button icon="pi pi-refresh" label="Refresh" severity="secondary" size="small" :loading="loading" @click="load" />
  </div>

  <div v-if="loading && !board" class="dashboard-loading">
    <Skeleton height="8rem" borderRadius="10px" />
    <Skeleton height="14rem" borderRadius="10px" />
  </div>

  <template v-else-if="board">
    <section v-if="emptyWorkspace" class="onboarding-card">
      <div class="onboarding-copy">
        <span class="onboarding-icon"><i class="pi pi-folder-open" /></span>
        <div>
          <p class="eyebrow">Start the engagement</p>
          <h3>Bring in your audit files</h3>
          <p>Drop files anywhere, or pick files or a folder. The workbench stages and classifies supported data and documents before importing them.</p>
          <div class="onboarding-actions">
            <Button label="Import files" icon="pi pi-upload" @click="emit('import-requested')" />
          </div>
        </div>
      </div>
    </section>

    <section v-else-if="primaryAction" class="next-action-card">
      <div class="next-action-mark"><i class="pi pi-arrow-right" /></div>
      <div class="next-action-copy">
        <span>Next action</span>
        <h3>{{ primaryAction.title }}</h3>
        <p>{{ primaryAction.reason }}</p>
      </div>
      <Button label="Open" icon="pi pi-arrow-right" iconPos="right" @click="runAction(primaryAction)" />
    </section>

    <div class="dashboard-columns">
      <section class="work-card">
        <div class="section-heading"><h3>Suggested actions</h3></div>
        <div class="work-card-body">
          <div v-if="board.actions.length" class="action-list">
            <button v-for="action in board.actions" :key="action.id" type="button" class="action-row" @click="runAction(action)">
              <span class="priority-dot" :data-priority="action.priority" />
              <span><strong>{{ action.title }}</strong><small>{{ action.reason }}</small></span>
              <i class="pi pi-chevron-right" />
            </button>
          </div>
          <div v-else class="compact-empty"><i class="pi pi-check-circle" /><span>No deterministic next action is outstanding.</span></div>

          <div class="ai-advice-head">
            <div><strong>Audit assistant</strong><small>Optional suggestions</small></div>
            <Button :label="board.ai_advice ? 'Refresh' : 'Generate'" icon="pi pi-sparkles" size="small" outlined :loading="adviceLoading" :disabled="!agent.state.status?.configured" @click="refreshAdvice" />
          </div>
          <p v-if="!agent.state.status?.configured" class="advice-note"><i class="pi pi-info-circle" /> Configure the audit assistant to add judgment-based suggestions.</p>
          <p v-if="adviceError" class="advice-error"><i class="pi pi-exclamation-circle" /> {{ adviceError }}</p>
          <template v-if="board.ai_advice">
            <div class="advice-meta"><Tag v-if="board.ai_advice.stale" value="Refresh recommended" severity="warn" /><span>Generated {{ board.ai_advice.generated_at.slice(0, 16).replace('T', ' ') }} · {{ board.ai_advice.model }}</span></div>
            <div v-if="board.ai_advice.items.length" class="action-list ai-list">
              <button v-for="action in board.ai_advice.items" :key="action.id" type="button" class="action-row" @click="runAction(action)">
                <i class="pi pi-sparkles ai-mark" /><span><strong>{{ action.title }}</strong><small>{{ action.reason }}</small></span><i class="pi pi-chevron-right" />
              </button>
            </div>
            <p v-else class="advice-note">The assistant did not add any suggestions beyond the deterministic actions.</p>
          </template>
        </div>
      </section>

      <section class="work-card attention-card">
        <div class="section-heading"><h3>Needs attention</h3><Tag :value="String(board.attention.length)" :severity="board.attention.length ? 'warn' : 'success'" /></div>
        <div class="work-card-body">
          <div v-if="board.attention.length" class="attention-list">
            <button v-for="item in board.attention" :key="item.id" type="button" class="attention-row" @click="navigate(item.target)">
              <i :class="item.severity === 'error' ? 'pi pi-times-circle' : 'pi pi-exclamation-triangle'" :data-severity="item.severity" />
              <span><strong>{{ item.title }}</strong><small>{{ item.message }}</small></span>
            </button>
          </div>
          <div v-else class="compact-empty"><i class="pi pi-check-circle" /><span>No current exceptions or quality issues.</span></div>
        </div>
      </section>
    </div>

    <section class="monitoring-section">
      <div class="section-heading monitoring-heading">
        <h3>Pinned analysis</h3>
        <div v-if="tiles.length" class="tile-summary"><span>{{ tiles.length }} pinned</span><span class="ok">{{ verdictCounts.ok }} passed</span><span class="warn">{{ verdictCounts.warn }} review</span><span class="fail">{{ verdictCounts.fail }} exceptions</span></div>
      </div>
      <div v-if="!tiles.length" class="pin-empty"><i class="pi pi-thumbtack" /><div><strong>No pinned analysis</strong><p>Pin a useful query, validation run, or analysis.</p></div><Button label="Go to analysis" text @click="navigate({ tab: 'analysis', query: {} })" /></div>
      <div v-else class="tile-grid">
        <div v-for="(tile, index) in tiles" :key="tile.id" class="tile" :class="{ 'tile-error': tile.error, 'tile-flash': flashed[tile.id] }" :data-verdict="tile.error ? 'fail' : tile.verdict">
          <div class="tile-head">
            <div class="tile-title"><i :class="tileIcon[tile.kind]" v-tooltip.bottom="tileKindLabel[tile.kind]" /><strong>{{ tile.title }}</strong><Tag v-if="tile.verdict" :value="tile.verdict_text || tile.verdict" :severity="verdictSeverity[tile.verdict]" class="verdict-tag" /></div>
            <div class="tile-actions">
              <UiOverflowMenu :items="tileActions(tile, index)" tooltip="Tile actions" />
            </div>
          </div>
          <p class="tile-meta muted">{{ tile.table || (tile.kind === 'python' ? 'Python' : '') }}<template v-if="tile.total_rows !== undefined"> · {{ tile.total_rows.toLocaleString() }} rows</template> · pinned {{ tile.created }}</p>
          <p v-if="tile.note" class="tile-note">{{ tile.note }}</p>
          <div v-if="tile.error" class="tile-error-body"><i class="pi pi-exclamation-triangle" /><span>{{ tile.error }}</span></div>
          <template v-else>
            <div v-if="tile.stats?.length" class="tile-stats"><span v-for="stat in tile.stats" :key="stat.label" class="tile-stat"><span class="label">{{ stat.label }}</span><span class="value">{{ stat.value }}</span></span></div>
            <ChartView v-if="tile.frame" :frame="tile.frame" :viz="tile.viz" height="240px" />
          </template>
        </div>
      </div>
    </section>
  </template>

  <Dialog :visible="editing !== null" modal header="Edit tile" :style="{ width: '26rem' }" @update:visible="editing = null">
    <div class="field"><label>Title</label><InputText v-model="editTitle" /></div>
    <div class="field edit-note"><label>Note</label><Textarea v-model="editNote" rows="2" /></div>
    <template #footer><Button label="Cancel" severity="secondary" text @click="editing = null" /><Button label="Save" icon="pi pi-check" :disabled="!editTitle.trim()" @click="saveEdit" /></template>
  </Dialog>
</template>

<style scoped>
.dashboard-heading,.section-heading,.next-action-card,.ai-advice-head,.advice-meta,.tile-head,.tile-title,.tile-actions { display:flex; align-items:center }
.dashboard-heading { align-items:flex-start; justify-content:space-between; gap:1rem; margin-bottom:.8rem }
.dashboard-heading h2 { margin:0; font-size:1.25rem }
.dashboard-loading { display:flex; flex-direction:column; gap:1rem }
.onboarding-card { min-height:18rem; display:flex; flex-direction:column; justify-content:center; gap:2rem; padding:2.2rem; border:1px solid #b9d9d5; border-radius:12px; background:linear-gradient(135deg,#fff 45%,#eef8f6); box-shadow:var(--aw-shadow-sm) }
.onboarding-copy { display:flex; align-items:flex-start; gap:1.25rem; max-width:48rem }.onboarding-icon { display:grid; place-items:center; width:4rem; height:4rem; flex:none; border-radius:14px; background:var(--aw-teal-soft); color:var(--aw-teal); font-size:1.8rem }
.onboarding-copy h3,.next-action-card h3,.section-heading h3 { margin:0 }.onboarding-copy p:not(.eyebrow) { color:var(--aw-muted); line-height:1.55 }.onboarding-actions { display:flex; gap:.65rem; flex-wrap:wrap }
.next-action-card { gap:1rem; padding:1rem 1.1rem; margin-bottom:1rem; border:1px solid #b9d9d5; border-left:4px solid var(--aw-teal); border-radius:10px; background:#fff; box-shadow:var(--aw-shadow-sm) }
.next-action-mark { display:grid; place-items:center; width:2.6rem; height:2.6rem; flex:none; border-radius:50%; background:var(--aw-teal-soft); color:var(--aw-teal) }.next-action-copy { flex:1 }.next-action-copy > span { color:var(--aw-teal); font-size:.67rem; font-weight:700; letter-spacing:.07em; text-transform:uppercase }.next-action-copy h3 { margin:.15rem 0 }.next-action-copy p { margin:0; color:var(--aw-muted); font-size:.8rem }
.monitoring-section { margin-top:1rem }.section-heading { justify-content:space-between; gap:1rem; margin-bottom:.55rem }.section-heading h3 { margin:0; font-size:.9rem }
.dashboard-columns { display:grid; grid-template-columns:minmax(0,1.35fr) minmax(18rem,.65fr); align-items:stretch; gap:1rem; margin-top:1rem }.work-card { display:flex; flex-direction:column; min-width:0; max-height:32rem; padding:1rem; overflow:hidden; border:1px solid var(--aw-border); border-radius:9px; background:#fff; box-shadow:var(--aw-shadow-sm) }.work-card > .section-heading { flex:none }.work-card-body { min-height:0; overflow-y:auto; scrollbar-gutter:stable }
.action-list,.attention-list { display:flex; flex-direction:column }.action-row,.attention-row { display:flex; align-items:flex-start; gap:.65rem; width:100%; padding:.65rem .25rem; border:0; border-bottom:1px solid var(--aw-border); background:transparent; color:inherit; text-align:left; cursor:pointer }.action-row:last-child,.attention-row:last-child { border-bottom:0 }.action-row:hover,.attention-row:hover { background:var(--aw-canvas) }.action-row > span:nth-child(2),.attention-row span { flex:1; display:flex; flex-direction:column; gap:.12rem }.action-row strong,.attention-row strong { font-size:.77rem }.action-row small,.attention-row small { color:var(--aw-muted); font-size:.69rem; line-height:1.4 }.action-row > .pi-chevron-right { align-self:center; color:var(--aw-muted); font-size:.65rem }
.priority-dot { width:.48rem; height:.48rem; flex:none; margin-top:.3rem; border-radius:50%; background:#94a3b8 }.priority-dot[data-priority='high'] { background:#dc2626 }.priority-dot[data-priority='medium'] { background:#d97706 }.priority-dot[data-priority='low'] { background:#3b82f6 }
.ai-advice-head { justify-content:space-between; gap:1rem; margin-top:.85rem; padding-top:.85rem; border-top:1px solid var(--aw-border) }.ai-advice-head > div { display:flex; flex-direction:column }.ai-advice-head strong { font-size:.77rem }.ai-advice-head small,.advice-note { color:var(--aw-muted); font-size:.68rem }.advice-note,.advice-error { margin:.55rem 0 }.advice-error { color:#b42318; font-size:.7rem }.advice-meta { gap:.45rem; margin-top:.55rem; color:var(--aw-muted); font-size:.64rem }.ai-list { margin-top:.25rem }.ai-mark { margin-top:.15rem; color:#7c3aed }
.attention-row > i { margin-top:.15rem }.attention-row > i[data-severity='error'] { color:#dc2626 }.attention-row > i[data-severity='warning'] { color:#d97706 }.compact-empty { display:flex; align-items:center; gap:.5rem; min-height:4rem; color:var(--aw-muted); font-size:.75rem }.compact-empty i { color:#16855b }
.monitoring-heading { align-items:flex-end }.tile-summary { display:flex; gap:.65rem; color:var(--aw-muted); font-size:.68rem }.tile-summary .ok { color:#15805c }.tile-summary .warn { color:#b45309 }.tile-summary .fail { color:#b42318 }
.pin-empty { display:flex; align-items:center; gap:.85rem; padding:1rem; border:1px dashed var(--aw-border); border-radius:9px; background:rgb(255 255 255 / 55%) }.pin-empty > i { color:var(--aw-teal); font-size:1.3rem }.pin-empty > div { flex:1 }.pin-empty strong { font-size:.8rem }.pin-empty p { margin:.15rem 0 0; color:var(--aw-muted); font-size:.7rem }
.tile-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(420px,1fr)); gap:1rem }.tile { min-width:0; padding:.9rem 1rem 1rem; border:1px solid var(--p-surface-200); border-top:3px solid #94a3b8; border-radius:8px; background:var(--p-surface-0); box-shadow:var(--aw-shadow) }.tile[data-verdict='ok'] { border-top-color:#16855b }.tile[data-verdict='warn'] { border-top-color:#d97706 }.tile[data-verdict='fail'] { border-top-color:#dc2626 }.tile[data-verdict='info'] { border-top-color:#3b82f6 }.tile-flash { animation:tile-flash 2.4s ease-out }@keyframes tile-flash { 0% { box-shadow:0 0 0 3px var(--p-primary-300) } 100% { box-shadow:var(--aw-shadow) } }.tile-error { border-color:var(--p-red-200) }
.tile-head { justify-content:space-between; align-items:flex-start; gap:.5rem }.tile-title { gap:.5rem; flex-wrap:wrap; min-width:0 }.tile-title i { color:var(--p-primary-500) }.verdict-tag { font-size:.7rem }.tile-actions { flex-shrink:0 }.tile:not(:hover) .tile-actions { opacity:.35 }.tile-actions { transition:opacity .15s }.tile-meta { margin:.15rem 0 .4rem; font-size:.78rem }.tile-note { margin:0 0 .5rem; color:var(--p-surface-600); font-size:.85rem; font-style:italic }.tile-stats { display:flex; flex-wrap:wrap; gap:.4rem 1.1rem; margin-bottom:.6rem }.tile-stat { display:flex; flex-direction:column }.tile-stat .label { color:var(--p-surface-500); font-size:.68rem; letter-spacing:.04em; text-transform:uppercase }.tile-stat .value { font-size:.95rem; font-weight:600 }.tile-error-body { display:flex; align-items:flex-start; gap:.5rem; padding:.75rem 0; color:var(--p-red-600); font-size:.9rem }.edit-note { margin-top:.75rem }
@media (max-width:1100px) { .dashboard-columns { grid-template-columns:1fr } }
@media (max-width:760px) { .dashboard-heading,.next-action-card { align-items:flex-start; flex-direction:column }.monitoring-heading { align-items:flex-start; flex-direction:column }.tile-summary { flex-wrap:wrap }.tile-grid { grid-template-columns:1fr }.onboarding-card { padding:1.25rem }.onboarding-copy { flex-direction:column }.pin-empty { align-items:flex-start; flex-wrap:wrap } }
</style>
