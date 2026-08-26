<template>
  <div class="relative mx-auto flex min-h-screen max-w-md items-center justify-center overflow-x-hidden p-6">
    <div
      class="pointer-events-none fixed inset-0 -z-10"
      style="background-image: radial-gradient(circle at 1px 1px, var(--dot-color) 1px, transparent 0); background-size: 32px 32px;"
    />

    <div class="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 w-[500px] h-[500px] rounded-full bg-primary/3 blur-3xl pointer-events-none" />

    <div class="relative w-full space-y-6">
      <div class="text-center">
        <div class="mb-4 flex justify-center">
          <div class="flex h-14 w-14 items-center justify-center rounded-xl bg-primary/10 border border-primary/20">
            <svg width="32" height="32" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg" role="img" :aria-label="$t('components.LogoMark.modulo_logo')">
              <g stroke="#00FFD1" stroke-width="7" fill="none" stroke-linejoin="round" stroke-linecap="round">
                <line x1="30" y1="84.64" x2="70" y2="15.36" />
                <polygon points="36,28 31,36.66 21,36.66 16,28 21,19.34 31,19.34" />
                <polygon points="84,72 79,80.66 69,80.66 64,72 69,63.34 79,63.34" />
              </g>
            </svg>
          </div>
        </div>
        <h1 class="text-3xl font-bold tracking-tight">{{ $t('views.LoginView.modulo') }}</h1>
        <p class="mt-1 text-muted-foreground">{{ $t('views.LoginView.agent_governance_for_your_agentic_sdlc') }}</p>
      </div>

      <div v-if="error" class="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-sm text-destructive">
        {{ error }}
      </div>

      <form @submit.prevent="() => login()" class="rounded-xl border bg-card p-6 space-y-4 shadow-sm">
        <div class="space-y-2">
          <label for="loginview-field-2" class="text-sm font-medium">{{ $t('common.email') }}</label>
          <input id="loginview-field-2"
            v-model="email"
            type="text"
            class="input-teal w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
            placeholder="admin@example.com"
            required
            data-testid="login-email"
          />
        </div>
        <div class="space-y-2">
          <label for="loginview-field-1" class="text-sm font-medium">{{ $t('common.password') }}</label>
          <input id="loginview-field-1"
            v-model="password"
            type="password"
            class="input-teal w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
            :placeholder="$t('views.LoginView.enter_your_password')"
            required
            data-testid="login-password"
          />
        </div>
        <Button type="submit" :disabled="loading" class="w-full border-primary/30 hover:border-primary/60 px-4 py-2.5" data-testid="login-submit">
          {{ loading ? $t('common.signing_in') : $t('common.sign_in') }}
        </Button>
      </form>

      <div v-if="hasSsoProviders" class="space-y-3">
        <div class="flex items-center gap-3">
          <span class="h-px flex-1 bg-border" />
          <span class="text-xs font-medium uppercase tracking-wide text-muted-foreground">{{ $t('views.LoginView.sso_divider') }}</span>
          <span class="h-px flex-1 bg-border" />
        </div>
        <div class="grid gap-2">
          <Button
            v-for="provider in oidcProviders"
            :key="provider"
            type="button"
            variant="outlined"
            class="w-full border-input px-4 py-2.5"
            :data-testid="`login-sso-oidc-${provider}`"
            @click="beginSsoLogin(`/api/v1/auth/oidc/${provider}/login`)"
          >
            {{ $t('views.LoginView.sso_continue_with', { provider: providerLabel(provider) }) }}
          </Button>
          <Button
            v-if="samlEnabled"
            type="button"
            variant="outlined"
            class="w-full border-input px-4 py-2.5"
            data-testid="login-sso-saml"
            @click="beginSsoLogin('/api/v1/auth/saml/login')"
          >
            {{ $t('views.LoginView.sso_continue_with_saml') }}
          </Button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import Button from 'primevue/button'
import { useMutation } from '../composables/useMutation'
import { setAccessToken, setRefreshToken } from '../lib/api/client'

interface SsoProvidersResponse {
  oidc: Array<{ provider_id: string }>
  saml: boolean
}

const router = useRouter()
const email = ref('')
const password = ref('')
const oidcProviders = ref<string[]>([])
const samlEnabled = ref(false)
const hasSsoProviders = computed(() => oidcProviders.value.length > 0 || samlEnabled.value)

function providerLabel(provider: string): string {
  return provider
    .replace(/[-_]+/g, ' ')
    .replace(/\b\w/g, (char) => char.toUpperCase())
}

function beginSsoLogin(path: string) {
  window.location.assign(path)
}

async function loadSsoProviders() {
  try {
    const res = await fetch('/api/v1/auth/sso/providers', {
      headers: { Accept: 'application/json' },
    })
    if (!res.ok) {
      return
    }
    const data = (await res.json()) as SsoProvidersResponse
    oidcProviders.value = (data.oidc ?? []).map((p) => p.provider_id)
    samlEnabled.value = Boolean(data.saml)
  } catch {
    return
  }
}

onMounted(loadSsoProviders)

const { loading, error, mutate: login } = useMutation(async () => {
  const res = await fetch('/api/v1/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email: email.value, password: password.value }),
  })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(body.detail || res.statusText)
  }
  const data = await res.json()
  setAccessToken(data.access_token)
  if (data.refresh_token) setRefreshToken(data.refresh_token)
  router.push('/')
  return data
})
</script>
