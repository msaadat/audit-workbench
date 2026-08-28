<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import Password from 'primevue/password'

import { api, ApiError } from '../api'
import { useSession } from '../composables/useSession'

const props = defineProps<{ token?: string }>()

const route = useRoute()
const router = useRouter()
const session = useSession()

const email = ref('')
const password = ref('')
const confirmation = ref('')
const displayName = ref('')
const error = ref('')
const busy = ref(false)
const inviteChecked = ref(false)

// An invite link carries a token: the same screen then collects a password for
// a known address rather than asking for credentials that do not exist yet.
const isInvite = computed(() => Boolean(props.token))
const heading = computed(() => (isInvite.value ? 'Set your password' : 'Sign in'))

const mismatch = computed(
  () => isInvite.value && confirmation.value.length > 0 && password.value !== confirmation.value,
)
const canSubmit = computed(() => {
  if (busy.value) return false
  if (isInvite.value) {
    return inviteChecked.value && password.value.length >= 8 && !mismatch.value
  }
  return email.value.trim().length > 0 && password.value.length > 0
})

onMounted(async () => {
  if (!isInvite.value) return
  try {
    const invite = await api.get<{ email: string }>(`/api/auth/invite/${props.token}`)
    email.value = invite.email
    inviteChecked.value = true
  } catch (caught) {
    error.value =
      caught instanceof ApiError
        ? caught.message
        : 'This invitation could not be read. Ask for a new one.'
  }
})

/**
 * Return to wherever the guard interrupted, defaulting to the workspace list.
 *
 * Only a same-site path is honoured. `//host/path` is excluded alongside
 * absolute URLs: it starts with a slash but a browser reads it as
 * protocol-relative, which would turn the login screen into an open redirect.
 */
function destination(): string {
  const wanted = route.query.redirect
  if (typeof wanted !== 'string') return '/'
  return wanted.startsWith('/') && !wanted.startsWith('//') ? wanted : '/'
}

async function submit() {
  if (!canSubmit.value) return
  busy.value = true
  error.value = ''
  try {
    if (isInvite.value) {
      await session.acceptInvite(props.token as string, password.value, displayName.value)
    } else {
      await session.signIn(email.value.trim(), password.value)
    }
    await router.replace(destination())
  } catch (caught) {
    error.value =
      caught instanceof ApiError ? caught.message : 'Something went wrong. Try again.'
    password.value = ''
    confirmation.value = ''
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="login-page">
    <form class="login-card" @submit.prevent="submit">
      <div class="login-brand">
        <span class="brand-mark"><i class="pi pi-verified" /></span>
        <strong>Audit Workbench</strong>
      </div>

      <h1>{{ heading }}</h1>
      <p v-if="isInvite && inviteChecked" class="login-hint">
        Choose a password for <strong>{{ email }}</strong>.
      </p>

      <Message v-if="error" severity="error" :closable="false">{{ error }}</Message>

      <label v-if="!isInvite" class="field">
        <span>Email</span>
        <InputText
          v-model="email"
          type="email"
          autocomplete="username"
          autofocus
          :disabled="busy"
        />
      </label>

      <label v-if="isInvite" class="field">
        <span>Your name</span>
        <InputText v-model="displayName" autocomplete="name" :disabled="busy" />
      </label>

      <label class="field">
        <span>Password</span>
        <Password
          v-model="password"
          :feedback="isInvite"
          toggle-mask
          :input-props="{ autocomplete: isInvite ? 'new-password' : 'current-password' }"
          :disabled="busy || (isInvite && !inviteChecked)"
        />
      </label>

      <label v-if="isInvite" class="field">
        <span>Confirm password</span>
        <Password
          v-model="confirmation"
          :feedback="false"
          toggle-mask
          :input-props="{ autocomplete: 'new-password' }"
          :disabled="busy || !inviteChecked"
        />
        <small v-if="mismatch" class="field-error">The passwords do not match.</small>
        <small v-else-if="password && password.length < 8" class="field-error">
          Use at least 8 characters.
        </small>
      </label>

      <Button
        type="submit"
        :label="isInvite ? 'Set password and continue' : 'Sign in'"
        :loading="busy"
        :disabled="!canSubmit"
      />
    </form>
  </div>
</template>

<style scoped>
/* The app's own surface tokens, which already flip for dark mode — a hardcoded
   light fallback here rendered a dark card on a light page under a dark theme. */
.login-page {
  flex: 1;
  min-height: 0;
  display: grid;
  place-items: center;
  padding: 2rem 1.25rem;
  background: var(--aw-canvas);
  color: var(--aw-ink);
}

.login-card {
  width: min(24rem, 100%);
  display: flex;
  flex-direction: column;
  gap: 1rem;
  padding: 2rem;
  border-radius: var(--aw-radius-surface);
  background: var(--aw-panel);
  border: 1px solid var(--aw-border);
  box-shadow: var(--aw-shadow-sm);
}

.login-brand {
  display: inline-flex;
  align-items: center;
  gap: 0.7rem;
  margin-bottom: 0.25rem;
}

.brand-mark {
  display: grid;
  place-items: center;
  width: 2rem;
  height: 2rem;
  border-radius: var(--aw-radius-control);
  color: var(--aw-navy-950);
  background: linear-gradient(135deg, var(--aw-mint) 0%, var(--aw-mint-600) 100%);
}

h1 {
  margin: 0;
  font-size: var(--aw-text-lg);
  font-weight: 700;
  letter-spacing: -0.01em;
}

.login-hint {
  margin: -0.4rem 0 0;
  font-size: var(--aw-text-sm);
  color: var(--aw-muted);
}

.field {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
  font-size: var(--aw-text-sm);
}

.field > span { font-weight: 600; }
.field :deep(.p-password),
.field :deep(.p-password-input) { width: 100%; }
.field-error { color: var(--aw-danger); }
</style>
