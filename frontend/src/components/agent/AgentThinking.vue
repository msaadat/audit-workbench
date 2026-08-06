<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'

/**
 * The "it is working" line.
 *
 * Two modes, deliberately: when the run tells us what it is doing
 * (`label`, from the activity phase) we say exactly that and never rotate —
 * an audit tool must not narrate an operation it isn't performing. Only when
 * there is genuinely nothing to report do we cycle a pool, and those words
 * describe thinking rather than asserting any audit work.
 */
const props = defineProps<{
  label?: string | null
  intent?: 'ask' | 'act' | 'auto' | null
  startedAt?: string | number | null
  /** Rendered inline (in a card header) rather than as a chat bubble. */
  inline?: boolean
  /**
   * Live tail of the text the model is producing. Shown verbatim and never
   * treated as a result: it is unreviewed, uncommitted, and replaced by the
   * real artifact the moment the call settles.
   */
  stream?: string | null
  /**
   * A structured read of the same call — sections or rows drafted so far —
   * for a response whose raw text is not worth showing directly (mostly
   * JSON). When present, this replaces the raw tail: a reader gets one or the
   * other, never both saying the same thing two different ways.
   */
  progress?: { label: string; state: string }[] | null
}>()

const POOLS: Record<string, string[]> = {
  ask: ['Thinking', 'Reading through', 'Looking it up', 'Considering', 'Piecing it together', 'Drafting a reply'],
  act: ['Thinking', 'Strategizing', 'Sizing it up', 'Planning the work', 'Working out the order', 'Lining up the steps'],
  auto: ['Thinking', 'Strategizing', 'Working on it', 'Considering', 'Getting oriented', 'Putting it together'],
}
const ROTATE_MS = 3800
const ELAPSED_AFTER_MS = 5000

function shuffled(values: string[]) {
  // "Thinking" leads every time — it is the honest first beat — and the rest
  // vary so a long wait does not read as a fixed script.
  const [first, ...rest] = values
  for (let index = rest.length - 1; index > 0; index -= 1) {
    const swap = Math.floor(Math.random() * (index + 1))
    ;[rest[index], rest[swap]] = [rest[swap], rest[index]]
  }
  return [first, ...rest]
}

const pool = shuffled(POOLS[props.intent || 'auto'] ?? POOLS.auto)
const mounted = Date.now()
const step = ref(0)
const clock = ref(Date.now())
let rotateTimer: number | undefined
let clockTimer: number | undefined

const word = computed(() => props.label?.trim() || pool[step.value % pool.length])
const startedMs = computed(() => {
  const value = props.startedAt
  if (value == null) return mounted
  const parsed = typeof value === 'number' ? value : Date.parse(value)
  return Number.isFinite(parsed) ? parsed : mounted
})
const elapsedMs = computed(() => clock.value - startedMs.value)
const elapsed = computed(() => {
  if (elapsedMs.value < ELAPSED_AFTER_MS) return ''
  const seconds = Math.floor(elapsedMs.value / 1000)
  return seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m ${seconds % 60}s`
})
// A minute of silence is worth acknowledging rather than pretending nothing
// is unusual.
const slow = computed(() => elapsedMs.value >= 60_000)

onMounted(() => {
  if (!props.label) rotateTimer = window.setInterval(() => { step.value += 1 }, ROTATE_MS)
  clockTimer = window.setInterval(() => { clock.value = Date.now() }, 1000)
})
onUnmounted(() => {
  if (rotateTimer) window.clearInterval(rotateTimer)
  if (clockTimer) window.clearInterval(clockTimer)
})
</script>

<template>
  <!-- One stable label for assistive tech; the rotating word would otherwise
       announce itself every few seconds. -->
  <span class="thinking-block" :class="{ inline }">
    <!-- One stable label for assistive tech; the rotating word would otherwise
         announce itself every few seconds. -->
    <span class="thinking" role="status" aria-label="Working">
      <span class="word" aria-hidden="true">{{ word }}<span class="ellipsis">…</span></span>
      <span v-if="elapsed" class="meta" aria-hidden="true">{{ elapsed }}</span>
      <span v-if="slow" class="meta" aria-hidden="true">· taking a while</span>
    </span>
    <!-- A structured read of the draft outranks its raw text: both describe
         the same in-flight call, and the checklist is the one a reader can
         actually use. -->
    <ul v-if="progress?.length" class="checklist" aria-hidden="true">
      <li v-for="item in progress" :key="item.label" :class="item.state">
        <i :class="item.state === 'done' ? 'pi pi-check' : 'pi pi-spin pi-spinner'" />
        <span>{{ item.label }}</span>
      </li>
    </ul>
    <!-- Hidden from assistive tech: a tail that rewrites itself several times a
         second is noise to a screen reader, and the label above already says
         what is happening. -->
    <span v-else-if="stream" class="stream" aria-hidden="true"><span class="stream-text">{{ stream }}</span></span>
  </span>
</template>

<style scoped>
.thinking-block{display:grid;gap:.25rem;min-width:0}
.thinking{display:inline-flex;align-items:baseline;gap:.35rem;min-width:0;font-size:var(--aw-text-sm);line-height:1.4}
.thinking-block.inline .thinking{font-size:var(--aw-text-xs)}
/* The tail reads as provisional: dimmed, monospaced, fading at the top edge so
   it is legible as "being written" rather than as a finished statement.
   `column-reverse` pins the newest text to the bottom edge and lets older lines
   overflow upward into the fade — so the reader follows the writing, rather
   than staring at the opening line while the live end is clipped out of view. */
.stream{
  display:flex;flex-direction:column-reverse;
  max-height:4.2rem;overflow:hidden;
  padding-left:.55rem;border-left:2px solid var(--aw-border);
  color:var(--aw-muted);font-family:var(--aw-mono,ui-monospace,SFMono-Regular,Menlo,monospace);
  font-size:var(--aw-text-2xs);line-height:1.45;
  mask-image:linear-gradient(to bottom,transparent 0,#000 1.4rem);
  -webkit-mask-image:linear-gradient(to bottom,transparent 0,#000 1.4rem);
}
.stream-text{white-space:pre-wrap;word-break:break-word}
.checklist{display:grid;gap:.15rem;margin:0;padding-left:.55rem;border-left:2px solid var(--aw-border);list-style:none;max-height:8rem;overflow:hidden}
.checklist li{display:flex;align-items:baseline;gap:.35rem;color:var(--aw-muted);font-size:var(--aw-text-xs);line-height:1.5}
.checklist li>i{flex:0 0 auto;font-size:var(--aw-text-2xs)}
.checklist li.done>i{color:var(--aw-ok)}
.checklist li.current{color:var(--aw-ink,inherit)}
.checklist li.current>i{color:var(--aw-teal)}
.checklist li span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.word{
  min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
  background:linear-gradient(90deg,var(--aw-muted) 0%,var(--aw-muted) 35%,var(--aw-teal) 50%,var(--aw-muted) 65%,var(--aw-muted) 100%);
  background-size:220% 100%;
  -webkit-background-clip:text;background-clip:text;color:transparent;
  animation:thinking-sweep 2.4s linear infinite;
}
.ellipsis{letter-spacing:.05em}
.meta{flex:0 0 auto;color:var(--aw-muted);font-size:var(--aw-text-2xs);opacity:.8}
@keyframes thinking-sweep{from{background-position:120% 0}to{background-position:-120% 0}}
@media (prefers-reduced-motion: reduce){
  .word{animation:none;background:none;-webkit-text-fill-color:currentColor;color:var(--aw-muted)}
}
</style>
