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
  wasDemoSessionEnded,
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
  it('performs the hand-off for a tokenless visitor and lands on the dashboard', async () => {
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

  it('keeps an existing demo session and does not re-mint when navigating to /demo', async () => {
    // qa iter 1: a live demo session must never be torn down by revisiting
    // /demo (Back/Forward must not re-POST and burn the 10/hour mint budget).
    setAccessToken('demo-jwt-token')
    setDemoSession(true)

    await router.push('/demo')

    expect(mockedHandOff).not.toHaveBeenCalled()
    expect(router.currentRoute.value.name).toBe('dashboard')
    expect(getAccessToken()).toBe('demo-jwt-token')
    expect(isDemoSession()).toBe(true)
  }, 60_000)

  it('keeps a real session intact when navigating to /demo', async () => {
    // qa iter 1: visiting /demo with a REAL session must not log the user out.
    setAccessToken('real-jwt-token')

    await router.push('/demo')

    expect(mockedHandOff).not.toHaveBeenCalled()
    expect(router.currentRoute.value.name).toBe('dashboard')
    expect(getAccessToken()).toBe('real-jwt-token')
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

describe('demo-ended tombstone (qa iter 1)', () => {
  it('persists a tombstone when a demo session is cleared, surviving the marker removal', () => {
    setAccessToken('demo-jwt-token')
    setDemoSession(true)

    clearAccessToken()

    // The marker is gone but the tombstone outlives the clear — this is the
    // reload path where previously neither token nor marker existed and
    // first-mount auto-login silently escalated the visitor.
    expect(isDemoSession()).toBe(false)
    expect(getAccessToken()).toBeNull()
    expect(wasDemoSessionEnded()).toBe(true)
    expect(localStorage.getItem('modulo_demo_ended')).not.toBeNull()
  })

  it('does not write a tombstone when a non-demo session is cleared', () => {
    setAccessToken('real-jwt-token')

    clearAccessToken()

    expect(wasDemoSessionEnded()).toBe(false)
  })

  it('clears the tombstone and the demo marker on any new successful auth', () => {
    // Simulate the ended-demo state, then a fresh (real) login.
    setAccessToken('demo-jwt-token')
    setDemoSession(true)
    clearAccessToken()
    expect(wasDemoSessionEnded()).toBe(true)

    setAccessToken('fresh-login-token')

    expect(wasDemoSessionEnded()).toBe(false)
    expect(isDemoSession()).toBe(false)
    expect(getAccessToken()).toBe('fresh-login-token')
  })

  it('clears a stale demo marker when a real login stores a token (two-tab race)', () => {
    // qa iter 1: a real token must never carry the demo flag — previously only
    // clearAccessToken removed the marker, so a two-tab race left a real admin
    // token flagged as demo (demo banner, hidden private_preview nav).
    setDemoSession(true)
    setAccessToken('real-jwt-token')

    expect(isDemoSession()).toBe(false)
    expect(getAccessToken()).toBe('real-jwt-token')
  })
})

describe('runDemoHandOff implementation (real module, qa iter 1 hardening)', () => {
  // The file-wide mock replaces the demo module for the router-guard tests
  // above; these tests pull the REAL implementation via importActual and stub
  // fetch to exercise the hardening directly.
  async function realHandOff() {
    const actual = await vi.importActual<typeof import('../lib/api/demo')>('../lib/api/demo')
    return actual.runDemoHandOff
  }

  it('stores the token, sets the demo marker after the token, and succeeds', async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({ access_token: 'demo-jwt-token' }),
      }),
    )
    vi.stubGlobal('fetch', fetchMock)
    try {
      const run = await realHandOff()

      expect(await run()).toBe(true)
      expect(getAccessToken()).toBe('demo-jwt-token')
      expect(isDemoSession()).toBe(true)
      expect(wasDemoSessionEnded()).toBe(false)
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('returns false on a non-ok response and reports only the status', async () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const fetchMock = vi.fn(() => Promise.resolve({ ok: false, status: 503, json: async () => ({}) }))
    vi.stubGlobal('fetch', fetchMock)
    try {
      const run = await realHandOff()

      expect(await run()).toBe(false)
      expect(getAccessToken()).toBeNull()
      expect(isDemoSession()).toBe(false)
      expect(warnSpy).toHaveBeenCalledWith('[demo] demo hand-off failed with status 503')
    } finally {
      vi.unstubAllGlobals()
      warnSpy.mockRestore()
    }
  })

  it('treats a malformed access_token as a hand-off failure, not a false success', async () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const fetchMock = vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({ access_token: 'not a valid token' }),
      }),
    )
    vi.stubGlobal('fetch', fetchMock)
    try {
      const run = await realHandOff()

      expect(await run()).toBe(false)
      // storeToken pattern-checks too — nothing may be persisted.
      expect(getAccessToken()).toBeNull()
      expect(isDemoSession()).toBe(false)
    } finally {
      vi.unstubAllGlobals()
      warnSpy.mockRestore()
    }
  })

  it('returns false when the request fails (network error)', async () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const fetchMock = vi.fn(() => Promise.reject(new TypeError('Failed to fetch')))
    vi.stubGlobal('fetch', fetchMock)
    try {
      const run = await realHandOff()

      expect(await run()).toBe(false)
      expect(getAccessToken()).toBeNull()
    } finally {
      vi.unstubAllGlobals()
      warnSpy.mockRestore()
    }
  })

  it('aborts a hung hand-off at the timeout and returns false', async () => {
    vi.useFakeTimers()
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const fetchMock = vi.fn((_url: unknown, init?: RequestInit) =>
      new Promise((_resolve, reject) => {
        init?.signal?.addEventListener('abort', () =>
          reject(new DOMException('The operation was aborted.', 'AbortError')),
        )
      }),
    )
    vi.stubGlobal('fetch', fetchMock)
    try {
      const run = await realHandOff()
      const pending = run()

      await vi.advanceTimersByTimeAsync(15_000)

      expect(await pending).toBe(false)
      expect(getAccessToken()).toBeNull()
      expect(warnSpy).toHaveBeenCalledWith('[demo] demo hand-off timed out')
    } finally {
      vi.unstubAllGlobals()
      warnSpy.mockRestore()
      vi.useRealTimers()
    }
  })
})
