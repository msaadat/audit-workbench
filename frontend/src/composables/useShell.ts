import { onUnmounted, ref, watchEffect } from 'vue'
import type { Ref } from 'vue'
import type { RouteLocationRaw } from 'vue-router'

/**
 * What the one bar across the top of the app says, and who says it.
 *
 * The shell used to be written twice — a 56 px header in `App.vue` for the
 * index and a 60 px one in `WorkspaceView.vue` for everything else, kept apart
 * by a route allowlist — with a third row, the crumb bar, drawn again in each
 * of three views. That is 99 px of chrome on a work product page and two
 * places to change any of it.
 *
 * This is the seam that replaced them. `AppHeader` is the only thing that
 * draws the bar; a surface says where it is by publishing here for as long as
 * it is mounted. Module-level state rather than provide/inject because the
 * publisher (a routed view, several levels down) and the reader (the header,
 * above the router view) are not on one component chain.
 *
 * Nothing here knows about the agent: the assistant's own state rides into the
 * header through a teleport from the workspace shell that owns it, so the bar
 * shows a run without owning one.
 */

export interface Crumb {
  label: string
  /** Where the piece leads. A piece with no destination is the current page. */
  to?: RouteLocationRaw
  /** Ids are drawn in mono, as the pages that own them draw them. */
  mono?: boolean
}

export interface ShellEngagement {
  id: string
  name: string
}

/**
 * The pieces after the engagement. The engagement itself is separate because
 * it is not a plain crumb: it is a split control whose name opens the record
 * and whose chevron opens the menu of the engagement's own views.
 */
const trail = ref<Crumb[]>([])
const engagement = ref<ShellEngagement | null>(null)

/**
 * Who published each of them last.
 *
 * A surface cannot simply clear on unmount, because Vue runs `onUnmounted` as
 * a post-render effect: on a route change the incoming view has already
 * published by the time the outgoing one is torn down, so an unguarded clear
 * wipes its successor's trail and the bar goes blank. Publishing claims the
 * slot; a teardown only clears a slot it still holds.
 */
let nextOwner = 0
const owner = { trail: 0, engagement: 0 }

function publish<T>(slot: 'trail' | 'engagement', target: Ref<T>, source: () => T) {
  const id = ++nextOwner
  watchEffect(() => {
    target.value = source()
    owner[slot] = id
  })
  onUnmounted(() => {
    if (owner[slot] === id) target.value = (slot === 'trail' ? [] : null) as T
  })
}

/** Publish the trail pieces after the engagement, for as long as this is mounted. */
export function useTrail(source: () => Crumb[]) {
  publish('trail', trail, source)
}

/** Publish which engagement the bar is inside, for as long as this is mounted. */
export function useEngagementCrumb(source: () => ShellEngagement | null) {
  publish('engagement', engagement, source)
}

/**
 * What the bar reads. The setters are for callers with no component to hang a
 * lifetime on — which in practice means tests driving the header directly.
 */
export function useShell() {
  return {
    trail,
    engagement,
    setTrail(crumbs: Crumb[]) { trail.value = crumbs },
    setEngagement(value: ShellEngagement | null) { engagement.value = value },
  }
}

/** Drop everything the shell is holding. Only tests need this. */
export function resetShell() {
  trail.value = []
  engagement.value = null
  owner.trail = 0
  owner.engagement = 0
}
