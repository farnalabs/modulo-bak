import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import type { Mock } from 'vitest'

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn(),
    POST: vi.fn(),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
  getAuthHeaders: vi.fn().mockReturnValue({}),
}))

import AdminRunRetentionView from '../views/AdminRunRetentionView.vue'
import { api } from '../lib/api/client'

const mockPipelines = {
  items: [
    { id: 'pipeline-1', name: 'Alpha Pipeline' },
    { id: 'pipeline-2', name: 'Beta Pipeline' },
  ],
  total: 2,
  page: 1,
  page_size: 100,
  has_more: false,
}

const mockCandidates = {
  runs: [
    { id: 'aaaaaaaa-bbbb-cccc-dddd-eeeeffffffff', created_at: '2026-08-01T00:00:00Z', status: 'complete', pipeline_id: 'pipeline-1', thread_id: 'thread-1', estimated_bytes: 26214400 },
    { id: '11111111-2222-3333-4444-555566667777', created_at: '2026-08-02T00:00:00Z', status: 'failed', pipeline_id: 'pipeline-1', thread_id: 'thread-2', estimated_bytes: 26214400 },
    { id: '99999999-8888-7777-6666-555544443333', created_at: '2026-08-03T00:00:00Z', status: 'running', pipeline_id: 'pipeline-2', thread_id: 'thread-3', estimated_bytes: 10485760 },
  ],
  total_count: 3,
  total_estimated_bytes: 62914560,
}

function setupDefaultMock() {
  ;(api.GET as Mock).mockImplementation(async (url: string) => {
    if (url === '/api/v1/pipelines') {
      return { data: mockPipelines, error: undefined }
    }
    if (url === '/api/v1/admin/run-retention/candidates') {
      return { data: mockCandidates, error: undefined }
    }
    return { data: null, error: undefined }
  })
  ;(api.POST as Mock).mockResolvedValue({
    data: { purged_runs: 2, purged_checkpoints: 6, freed_estimated_bytes: 52428800 },
    error: undefined,
  })
}

async function mountView() {
  const pinia = createPinia()
  setActivePinia(pinia)
  const wrapper = mount(AdminRunRetentionView, {
    global: {
      plugins: [pinia],
      stubs: { FeatureGate: { template: '<div><slot /></div>' } },
    },
  })
  for (let i = 0; i < 10; i++) {
    await flushPromises()
  }
  return wrapper
}

describe('AdminRunRetentionView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    setupDefaultMock()
  })

  it('renders without crashing', async () => {
    const wrapper = await mountView()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Run Retention')
  })

  it('shows candidate summary from the API', async () => {
    const wrapper = await mountView()
    expect(wrapper.find('[data-testid="admin-run-retention-total-runs"]').text()).toBe('3')
    expect(wrapper.find('[data-testid="admin-run-retention-total-bytes"]').text()).toBe('60.0 MB')
    expect(wrapper.find('[data-testid="admin-run-retention-terminal-runs"]').text()).toBe('2')
  })

  it('lists candidates with pipeline names resolved', async () => {
    const wrapper = await mountView()
    expect(wrapper.find('[data-testid="admin-run-retention-candidates-table"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('Alpha Pipeline')
  })

  it('shows a warning that in-flight runs are never purged', async () => {
    const wrapper = await mountView()
    const warning = wrapper.find('[data-testid="admin-run-retention-warning"]')
    expect(warning.exists()).toBe(true)
    expect(warning.text().toLowerCase()).toContain('never')
  })

  it('calls purge endpoint with confirm:true after confirming the dialog', async () => {
    const wrapper = await mountView()
    await wrapper.find('[data-testid="admin-run-retention-purge"]').trigger('click')
    await flushPromises()
    await flushPromises()

    const confirmBtn = document.body.querySelector('[data-testid="admin-run-retention-purge-confirm"]') as HTMLElement
    expect(confirmBtn).not.toBeNull()
    confirmBtn.click()
    await flushPromises()
    await flushPromises()

    const postMock = api.POST as Mock
    const purgeCall = postMock.mock.calls.find((c: unknown[]) => c[0] === '/api/v1/admin/run-retention/purge')
    expect(purgeCall).toBeDefined()
    const body = (purgeCall![1] as { body: { confirm: boolean } }).body
    expect(body.confirm).toBe(true)

    const result = wrapper.find('[data-testid="admin-run-retention-purge-result"]')
    expect(result.text()).toContain('Purged 2 run(s)')
  })

  it('disables the purge button when no terminal candidates match', async () => {
    const runningOnly = {
      runs: [
        { id: 'aaaaaaaa-bbbb-cccc-dddd-eeeeffffffff', created_at: '2026-08-01T00:00:00Z', status: 'running', pipeline_id: 'pipeline-1', thread_id: 'thread-1', estimated_bytes: 26214400 },
      ],
      total_count: 1,
      total_estimated_bytes: 26214400,
    }
    ;(api.GET as Mock).mockImplementation(async (url: string) => {
      if (url === '/api/v1/pipelines') return { data: mockPipelines, error: undefined }
      if (url === '/api/v1/admin/run-retention/candidates') return { data: runningOnly, error: undefined }
      return { data: null, error: undefined }
    })

    const wrapper = await mountView()
    expect(wrapper.find('[data-testid="admin-run-retention-purge"]').attributes('disabled')).toBeDefined()
  })
})
