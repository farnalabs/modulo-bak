import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

// Restore the REAL vue-router (the shared vitest setup mocks it globally) so
// the actual beforeEach guard in ../router/index.ts runs — same override
// pattern as app-bootstrap.spec.ts.
vi.mock('vue-router', async () => {
  const actual = await vi.importActual<typeof import('vue-router')>('vue-router')
  return actual
})

// Mock ONLY the hand-off module. The router guard calls runDemoHandOff(); each
// test replays what the REAL implementation does to storage (tear down any
// stored session exactly like logout, store the short-lived demo token, set
// the demo marker) while controlling the endpoint outcome.
vi.mock('../lib/api/demo', () => ({
  runDemoHandOff: vi.fn(async () => true),
}))

import router from '../router'
import { runDemoHandOff } from '../lib/api/demo'
import {
  clearAccessToken,
  getAccessToken,
  isDemoSession,
  setAccessToken,
  setDemoSession,
  wasDemoSessionCleared,
} from '../lib/api/auth'
import { shouldReRunAutoLogin } from '../lib/api/client'
import { usePlanStore } from '../stores/planStore'

const mockedHandOff = vi.mocked(runDemoHandOff)

beforeEach(() => {
  localStorage.clear()
  // Reset the module-level demo-end flag (clearAccessToken captures it from
  // the persisted demo marker, which localStorage.clear just removed).
  clearAccessToken()
  mockedHandOff.mockReset()
  // The dashboard route's manifest entry carries required_tier, so the router
  // guard reads the plan store; activate a fresh pinia per test with the plan
  // already loaded so the guard never attempts a network fetch.
  setActivePinia(createPinia())
  usePlanStore().loaded = true
})

describe('/demo route hand-off', () => {
  it('clears stored auth, stores the demo token and flag, and lands on the dashboard', async () => {
    setAccessToken('stale-session-token')
    mockedHandOff.mockImplementation(async () => {
      clearAccessToken()
      setAccessToken('demo-jwt-token')
      setDemoSession(true)
      return true
    })

    await router.push('/demo')

    expect(mockedHandOff).toHaveBeenCalledTimes(1)
    expect(router.currentRoute.value.name).toBe('dashboard')
    expect(getAccessToken()).toBe('demo-jwt-token')
    expect(isDemoSession()).toBe(true)
  }, 60_000)

  it('redirects to /login without storing a token when the demo endpoint is unavailable', async () => {
    setAccessToken('stale-session-token')
    mockedHandOff.mockImplementation(async () => {
      clearAccessToken()
      return false
    })

    await router.push('/demo')

    expect(mockedHandOff).toHaveBeenCalledTimes(1)
    expect(router.currentRoute.value.name).toBe('login')
    expect(getAccessToken()).toBeNull()
    expect(isDemoSession()).toBe(false)
  }, 60_000)
})

describe('demo session recovery gate', () => {
  it('never re-runs auto-login after a demo session clears', () => {
    setAccessToken('demo-jwt-token')
    setDemoSession(true)
    clearAccessToken()

    expect(wasDemoSessionCleared()).toBe(true)
    expect(shouldReRunAutoLogin(true, false, true)).toBe(false)
  })

  it('re-enables auto-login recovery once a fresh login supersedes the demo end', () => {
    setAccessToken('demo-jwt-token')
    setDemoSession(true)
    clearAccessToken()
    setAccessToken('fresh-login-token')
    clearAccessToken()

    expect(wasDemoSessionCleared()).toBe(false)
    expect(shouldReRunAutoLogin(true, false, true)).toBe(true)
  })
})
