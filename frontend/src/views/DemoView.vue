<template>
  <div class="relative mx-auto flex min-h-screen max-w-md items-center justify-center overflow-x-hidden p-6">
    <div class="text-center space-y-2" data-testid="demo-view">
      <h1 class="text-3xl font-bold tracking-tight">{{ $t('views.DemoView.preparing') }}</h1>
      <p class="text-muted-foreground">{{ $t('views.DemoView.signing_in') }}</p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { runDemoHandOff } from '../lib/api/demo'

const router = useRouter()

// Defensive fallback: the /demo route guard performs the hand-off pre-mount and
// redirects away before this component ever renders. If this view is ever
// reached anyway (guard bypassed), run the same hand-off here so the visitor is
// never stranded — dashboard on success, plain login on failure (no error may
// reveal demo internals).
onMounted(async () => {
  const ok = await runDemoHandOff()
  router.replace(ok ? '/' : '/login')
})
</script>
