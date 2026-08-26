import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { nextTick } from 'vue'

vi.mock('vue-router', () => ({
  useRouter: vi.fn(() => ({ push: vi.fn() })),
  useRoute: vi.fn(() => ({ name: 'login' })),
}))

import LoginView from '../views/LoginView.vue'

beforeEach(() => {
  vi.stubGlobal(
    'location',
    {
      assign: vi.fn(),
      href: '',
      replace: vi.fn(),
      reload: vi.fn(),
    },
  )
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

function stubSsoProviders(response: unknown, ok = true) {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
    ok,
    json: vi.fn().mockResolvedValue(response),
  }))
}

describe('LoginView', () => {
  it('renders the sign-in form without an SSO section when none is configured', async () => {
    stubSsoProviders({ oidc: [], saml: false })
    const wrapper = mount(LoginView)
    await flushPromises()
    await nextTick()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Modulo')
    expect(wrapper.text()).toContain('Sign in')
    expect(wrapper.find('[data-testid="login-sso-saml"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid^="login-sso-oidc-"]').exists()).toBe(false)
  })

  it('hides the SSO section when the providers endpoint is feature-gated', async () => {
    stubSsoProviders({ detail: 'Feature not available' }, false)
    const wrapper = mount(LoginView)
    await flushPromises()
    await nextTick()
    expect(wrapper.find('[data-testid="login-sso-saml"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid^="login-sso-oidc-"]').exists()).toBe(false)
  })

  it('renders one button per configured OIDC provider and a SAML button', async () => {
    stubSsoProviders({ oidc: [{ provider_id: 'google' }, { provider_id: 'okta' }], saml: true })
    const wrapper = mount(LoginView)
    await flushPromises()
    await nextTick()
    expect(wrapper.find('[data-testid="login-sso-oidc-google"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="login-sso-oidc-okta"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="login-sso-saml"]').exists()).toBe(true)
  })

  it('redirects to the OIDC login URL when a provider button is clicked', async () => {
    stubSsoProviders({ oidc: [{ provider_id: 'okta' }], saml: false })
    const wrapper = mount(LoginView)
    await flushPromises()
    await nextTick()
    await wrapper.find('[data-testid="login-sso-oidc-okta"]').trigger('click')
    expect(window.location.assign).toHaveBeenCalledWith('/api/v1/auth/oidc/okta/login')
  })

  it('redirects to the SAML login URL when the SAML button is clicked', async () => {
    stubSsoProviders({ oidc: [], saml: true })
    const wrapper = mount(LoginView)
    await flushPromises()
    await nextTick()
    await wrapper.find('[data-testid="login-sso-saml"]').trigger('click')
    expect(window.location.assign).toHaveBeenCalledWith('/api/v1/auth/saml/login')
  })
})