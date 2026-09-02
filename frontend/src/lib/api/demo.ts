import { clearAccessToken, setAccessToken, setDemoSession } from './auth'
import { setMustChangePassword } from '../mustChangePassword'

// FAR-535 demo auto-login hand-off.
//
// One-shot: tears down any stored session exactly the way logout does (a stale
// session from a previous account must never survive the demo hand-off), then
// exchanges NOTHING for a short-lived read-only demo session — the server reads
// the demo credentials from env itself; no credential is ever sent, stored, or
// put in a URL. On success the demo marker is set (drives the demo-mode banner
// and the auto-login recovery gate). Returns whether a demo session was
// established; callers redirect to the dashboard on true and to /login on
// false — a failure must never surface an error that reveals demo internals.
export async function runDemoHandOff(): Promise<boolean> {
  clearAccessToken()
  try {
    const res = await fetch('/api/v1/auth/demo', { method: 'POST' })
    if (!res.ok) return false
    const data = (await res.json()) as { access_token?: string }
    if (!data.access_token) return false
    setAccessToken(data.access_token)
    setDemoSession(true)
    // The demo account is seeded with must_change_password=false; clear any
    // stale gate left by a previously logged-in account.
    setMustChangePassword(false)
    return true
  } catch {
    return false
  }
}
