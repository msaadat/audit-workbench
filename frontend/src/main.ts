import { createApp } from 'vue'
import PrimeVue from 'primevue/config'
import Aura from '@primeuix/themes/aura'
import { definePreset } from '@primeuix/themes'
import ToastService from 'primevue/toastservice'
import ConfirmationService from 'primevue/confirmationservice'
import Tooltip from 'primevue/tooltip'
import 'primeicons/primeicons.css'

// Self-hosted fonts (no CDN — this tool runs fully local/offline).
import '@fontsource-variable/inter'
import '@fontsource-variable/jetbrains-mono'

import App from './App.vue'
import router from './router'
import './style.css'

// Extend Aura well beyond `primary`: override the neutral surface ramp, form
// fields, focus ring and text tokens so every component inherits the refreshed
// language instead of reading as stock Aura. Values are kept in lock-step with
// the CSS custom properties in style.css and the palette in public/about.html.
const WorkbenchPreset = definePreset(Aura, {
  semantic: {
    primary: {
      50: '#f0fdfa',
      100: '#ccfbf1',
      200: '#99f6e4',
      300: '#5eead4',
      400: '#2dd4bf',
      500: '#14b8a6',
      600: '#0d9488',
      700: '#0f766e',
      800: '#115e59',
      900: '#134e4a',
      950: '#042f2e',
    },
    // Slate neutrals tuned toward the app's navy ink so greys read cool, not muddy.
    surface: {
      0: '#ffffff',
      50: '#f6f8fb',
      100: '#eef2f7',
      200: '#dce5ee',
      300: '#c5d2e0',
      400: '#9aabc0',
      500: '#6b7c94',
      600: '#4a5a72',
      700: '#33445c',
      800: '#1b2c45',
      900: '#0d2340',
      950: '#07162b',
    },
    formField: {
      borderRadius: '8px',
      focusRing: {
        width: '3px',
        style: 'solid',
        color: 'rgba(13, 148, 136, 0.35)',
        offset: '1px',
      },
    },
    focusRing: {
      width: '3px',
      style: 'solid',
      color: 'rgba(13, 148, 136, 0.35)',
      offset: '2px',
    },
    colorScheme: {
      light: {
        surface: {
          ground: '#f6f8fb',
          border: '#dce5ee',
        },
        text: {
          color: '#0d2340',
          mutedColor: '#5f7088',
        },
        content: {
          borderColor: '#dce5ee',
        },
      },
    },
  },
})

const app = createApp(App)
app.use(router)
app.use(PrimeVue, {
  theme: {
    preset: WorkbenchPreset,
    options: { darkModeSelector: '.app-dark' },
  },
})
app.use(ToastService)
app.use(ConfirmationService)
app.directive('tooltip', Tooltip)
app.mount('#app')
