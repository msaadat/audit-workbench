import { computed, ref, watch } from 'vue'

/**
 * How the workbench is drawn: its theme, and the scale it is drawn at.
 *
 * Two independent choices, one place, because both are pure presentation and
 * neither belongs to any one screen. Both persist, because an auditor who
 * turns the scale up for a room should not have to do it again on every
 * navigation, and both apply to the document root, because the tokens they
 * override live on `:root`.
 *
 * Theme has three states rather than two. "System" is not a third palette —
 * it is the absence of a choice, and the stylesheet resolves it through
 * `prefers-color-scheme`. Only an explicit choice stamps `data-theme`, so a
 * viewer who has expressed no preference follows their OS and one who has
 * overrides it in both directions.
 */
export type AppearanceTheme = 'system' | 'light' | 'dark'
export type AppearanceDensity = 'default' | 'presentation'

const THEME_KEY = 'aw.appearance.theme'
const DENSITY_KEY = 'aw.appearance.density'

function stored<T extends string>(key: string, allowed: readonly T[], fallback: T): T {
  try {
    const value = window.localStorage.getItem(key) as T | null
    return value && allowed.includes(value) ? value : fallback
  } catch {
    // A browser with storage disabled still gets a working toggle, it just
    // does not remember the choice.
    return fallback
  }
}

function remember(key: string, value: string) {
  try {
    window.localStorage.setItem(key, value)
  } catch { /* not worth surfacing: the setting applied, it just won't persist */ }
}

const theme = ref<AppearanceTheme>(
  stored(THEME_KEY, ['system', 'light', 'dark'] as const, 'system'),
)
const density = ref<AppearanceDensity>(
  stored(DENSITY_KEY, ['default', 'presentation'] as const, 'default'),
)

const darkQuery = typeof window !== 'undefined' && window.matchMedia
  ? window.matchMedia('(prefers-color-scheme: dark)')
  : null
const systemPrefersDark = ref(darkQuery?.matches ?? false)
darkQuery?.addEventListener?.('change', event => { systemPrefersDark.value = event.matches })

/** What the viewer actually sees, with "system" resolved. */
const resolvedTheme = computed<'light' | 'dark'>(() =>
  theme.value === 'system' ? (systemPrefersDark.value ? 'dark' : 'light') : theme.value)

function apply() {
  if (typeof document === 'undefined') return
  const root = document.documentElement
  // The stylesheet reads `data-theme` for an explicit choice only; leaving it
  // off is what lets the media query decide.
  if (theme.value === 'system') root.removeAttribute('data-theme')
  else root.dataset.theme = theme.value
  // PrimeVue keys its own dark palette off a class and cannot read a media
  // query, so it is given the resolved answer rather than the choice.
  root.classList.toggle('app-dark', resolvedTheme.value === 'dark')
  if (density.value === 'default') root.removeAttribute('data-density')
  else root.dataset.density = density.value
}

watch([theme, density, resolvedTheme], apply, { immediate: true })
watch(theme, value => remember(THEME_KEY, value))
watch(density, value => remember(DENSITY_KEY, value))

export function useAppearance() {
  return {
    theme,
    density,
    resolvedTheme,
    presenting: computed({
      get: () => density.value === 'presentation',
      set: (value: boolean) => { density.value = value ? 'presentation' : 'default' },
    }),
    cycleTheme() {
      const order: AppearanceTheme[] = ['system', 'light', 'dark']
      theme.value = order[(order.indexOf(theme.value) + 1) % order.length]
    },
  }
}
