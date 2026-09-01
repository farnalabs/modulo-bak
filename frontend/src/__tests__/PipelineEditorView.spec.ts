import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createWebHistory } from 'vue-router'
import { createPinia } from 'pinia'
import { nextTick } from 'vue'

vi.mock('../composables/useApi', () => ({
  useApi: vi.fn(() => ({
    get: vi.fn().mockImplementation((url: string) => {
      if (url.includes('/lifecycle-maps')) return Promise.resolve([])
      if (url.includes('/pipeline-folders')) return Promise.resolve([])
      return Promise.resolve({ items: [] })
    }),
    post: vi.fn().mockResolvedValue({}),
  })),
}))

vi.mock('../lib/api/client', () => {
  // The api client substitutes path params internally, so the mock sees the
  // templated route (e.g. `/api/v1/pipelines/{pipeline_id}/graph`).
  const get = (url: string) => {
    if (url.includes('/pipelines/{pipeline_id}/graph')) {
      return Promise.resolve({
        data: {
          nodes: [
            {
              id: 'node-1',
              node_type: 'agent',
              agent_id: 'agent-1',
              label: 'Agent Node',
              description: '',
              position: { x: 0, y: 0 },
              capability_scope: { allowed_connectors: ['conn-1'], allowed_tools: ['tool-a'], context_scope: ['ctx'] },
            },
          ],
          edges: [],
        },
        error: undefined,
      })
    }
    if (url.includes('/api/v1/agents')) {
      return Promise.resolve({ data: { items: [{ id: 'agent-1', name: 'Agent One', connector_type_refs: [{ connector_type: 'slack' }] }] }, error: undefined })
    }
    if (url.includes('/api/v1/connectors')) {
      return Promise.resolve({ data: { items: [{ id: 'conn-1', name: 'Slack Dev', connector_type_id: 'slack' }] }, error: undefined })
    }
    if (url.includes('/pipelines/{pipeline_id}')) {
      return Promise.resolve({ data: { id: 'test-pipeline-id', name: 'Test Pipeline' }, error: undefined })
    }
    return Promise.resolve({ data: { items: [] }, error: undefined })
  }
  return {
    api: {
      GET: vi.fn(get),
      POST: vi.fn().mockResolvedValue({ data: {}, error: undefined }),
      PATCH: vi.fn().mockResolvedValue({ data: {}, error: undefined }),
      PUT: vi.fn().mockResolvedValue({ data: {}, error: undefined }),
      DELETE: vi.fn().mockResolvedValue({ data: {}, error: undefined }),
    },
    getAccessToken: vi.fn().mockReturnValue('mock-token'),
  }
})

vi.mock('../composables/useDataFetch', async () => {
  const { ref } = await import('vue')
  return {
    useDataFetch: () => ({
      loading: ref(false),
      error: ref(null),
      data: ref(undefined),
      fetched: ref(true),
      load: async () => {},
    }),
  }
})

import PipelineEditorView from '../views/PipelineEditorView.vue'
import { api } from '../lib/api/client'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/pipelines/:id/editor', name: 'pipeline-editor', component: PipelineEditorView },
  ],
})

function mountEditor() {
  return mount(PipelineEditorView, {
    global: {
      plugins: [createPinia(), router],
      stubs: {
        VueFlow: { template: '<div><slot /></div>' },
        Background: true,
        Controls: true,
      },
    },
  })
}





describe('PipelineEditorView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders without crashing', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await nextTick()
    expect(wrapper.exists()).toBe(true)
  })

  it('renders the capability scope panel for an agent node and persists edits', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()

    const vm = wrapper.vm as any
    // useDataFetch is mocked out; seed the loader state directly so the panel
    // can be driven deterministically without relying on async graph loading.
    vm.rawNodes = [
      {
        id: 'node-1',
        node_type: 'agent',
        agent_id: 'agent-1',
        label: 'Agent Node',
        description: '',
        position: { x: 0, y: 0 },
        capability_scope: { allowed_connectors: ['conn-1'], allowed_tools: ['tool-a'], context_scope: ['ctx'] },
      },
    ]
    vm.flowNodes = [{ id: 'node-1', type: 'agent', data: { label: 'Agent Node', description: '' } }]
    vm.agents = [{ id: 'agent-1', name: 'Agent One', connector_type_refs: [{ connector_type: 'slack' }] }]
    vm.connectors = [{ id: 'conn-1', name: 'Slack Dev', connector_type_id: 'slack' }]
    vm.onNodeClick({ node: { id: 'node-1' } })
    await nextTick()
    expect((wrapper.vm as any).selectedNodeData).toBeTruthy()

    const panel = wrapper.find('[data-testid="pipeline-editor-capability-scope"]')
    expect(panel.exists()).toBe(true)

    // connector checkbox is present and pre-selected from the saved scope
    const connCheckbox = wrapper.find('[data-testid="pipeline-editor-scope-connector-conn-1"]')
    expect(connCheckbox.exists()).toBe(true)
    expect((connCheckbox.element as HTMLInputElement).checked).toBe(true)

    // displayed connector label
    expect(panel.text()).toContain('Slack Dev (slack)')

    // add a free-form tool
    await wrapper.find('[data-testid="pipeline-editor-scope-tool-input"]').setValue('tool-b')
    await wrapper.find('[data-testid="pipeline-editor-scope-tool-add"]').trigger('click')
    await nextTick()
    expect(vm.selectedNodeData.capability_scope.allowed_tools).toContain('tool-b')

    // reset to unrestricted clears scope
    await wrapper.find('[data-testid="pipeline-editor-scope-reset"]').trigger('click')
    await nextTick()
    expect(vm.selectedNodeData.capability_scope).toBeNull()
  })

  it('offers only the backend-supported retry policy events and filters unknown values', async () => {
    router.push('/pipelines/test-pipeline-id/editor')
    await router.isReady()
    const wrapper = mountEditor()
    await flushPromises()

    await wrapper.find('[data-testid="pipeline-editor-retry-policy-toggle"]').trigger('click')
    await nextTick()

    const panel = wrapper.find('[data-testid="pipeline-editor-retry-policy-panel"]')
    expect(panel.exists()).toBe(true)

    // Only the events the backend allowlist accepts are offered in the UI.
    // eval_failed became backend-supported in FAR-503: the API allowlist
    // (_RETRY_POLICY_EVENTS in api/routes/pipelines.py), the graph validator and
    // the executor's retry matching all accept it in lockstep, so the editor
    // offers it as a selectable event.
    const supportedEvents = ['stall', 'timeout', 'failure', 'eval_failed']
    for (const event of supportedEvents) {
      const checkbox = wrapper.find(`[data-testid="pipeline-editor-retry-event-${event}"]`)
      expect(checkbox.exists(), `retry event checkbox for ${event}`).toBe(true)
    }

    // round-trip: a persisted policy is loaded safely — the allowlist is derived
    // from retryPolicyOptions, so every backend-supported event (including
    // eval_failed) survives a reload while genuinely unknown values are dropped
    // and the editor never crashes on stale payloads.
    ;(wrapper.vm as any).pipeline = {
      retry_policy: { on: ['eval_failed', 'stall', 'bogus_event'], max_retries: 2 },
    }
    ;(wrapper.vm as any).syncRetryPolicyFromPipeline()
    await nextTick()
    expect((wrapper.vm as any).retryPolicyEvents).toEqual(['eval_failed', 'stall'])
    const stallCheckbox = wrapper.find('[data-testid="pipeline-editor-retry-event-stall"]')
    expect((stallCheckbox.element as HTMLInputElement).checked).toBe(true)
  })

  describe('FAR-525 retry backoff schedule round-trip', () => {
    async function mountWithPolicy(retryPolicy: Record<string, unknown>) {
      router.push('/pipelines/test-pipeline-id/editor')
      await router.isReady()
      const wrapper = mountEditor()
      await flushPromises()
      ;(wrapper.vm as any).pipeline = { retry_policy: retryPolicy }
      ;(wrapper.vm as any).syncRetryPolicyFromPipeline()
      await nextTick()
      return wrapper
    }

    function lastPatchBody(): any {
      const calls = vi.mocked(api.PATCH).mock.calls
      expect(calls.length).toBeGreaterThan(0)
      // PATCH(url, { params, body, signal }) — the request body lives on the
      // second argument.
      return (calls[calls.length - 1][1] as any).body
    }

    it('loads backoff_schedule delay/multiplier into the panel and preserves the legacy backoff on save', async () => {
      const wrapper = await mountWithPolicy({
        on: ['failure'],
        max_retries: 2,
        backoff: 12,
        backoff_schedule: { delay_seconds: 30, multiplier: 1.5 },
      })
      const vm = wrapper.vm as any
      expect(vm.retryPolicyDelaySeconds).toBe(30)
      expect(vm.retryPolicyMultiplier).toBe(1.5)

      // change only max_retries, then save: schedule AND legacy backoff survive
      vm.retryPolicyMaxRetries = 3
      await vm.saveRetryPolicy()
      await flushPromises()

      expect(lastPatchBody().retry_policy).toEqual({
        on: ['failure'],
        max_retries: 3,
        backoff: 12,
        backoff_schedule: { delay_seconds: 30, multiplier: 1.5 },
      })
    })

    it('save in the disable direction sends on: [] and preserves schedule and legacy backoff (not {})', async () => {
      const wrapper = await mountWithPolicy({
        on: ['failure', 'stall'],
        max_retries: 2,
        backoff: 7,
        backoff_schedule: { delay_seconds: 90, multiplier: 2 },
      })
      const vm = wrapper.vm as any
      vm.retryPolicyEvents = []
      await vm.saveRetryPolicy()
      await flushPromises()

      expect(lastPatchBody().retry_policy).toEqual({
        on: [],
        max_retries: 2,
        backoff: 7,
        backoff_schedule: { delay_seconds: 90, multiplier: 2 },
      })
    })

    it('rebuilds backoff_schedule from panel state, dropping junk inner keys (enable direction)', async () => {
      const wrapper = await mountWithPolicy({
        on: ['failure'],
        max_retries: 2,
        backoff_schedule: { delay_seconds: 45, multiplier: 2, junk_key: 'hand-edited' },
      })
      const vm = wrapper.vm as any
      await vm.saveRetryPolicy()
      await flushPromises()

      expect(lastPatchBody().retry_policy.backoff_schedule).toEqual({
        delay_seconds: 45,
        multiplier: 2,
      })
    })

    it('rebuilds backoff_schedule from panel state, dropping junk inner keys (disable direction)', async () => {
      const wrapper = await mountWithPolicy({
        on: ['timeout'],
        max_retries: 1,
        backoff_schedule: { delay_seconds: 20, multiplier: 3, junk_key: 'hand-edited' },
      })
      const vm = wrapper.vm as any
      vm.retryPolicyEvents = []
      await vm.saveRetryPolicy()
      await flushPromises()

      expect(lastPatchBody().retry_policy).toEqual({
        on: [],
        max_retries: 1,
        backoff_schedule: { delay_seconds: 20, multiplier: 3 },
      })
    })

    it('sends the default 45s x 2.0 schedule when no schedule is stored, without a legacy backoff key', async () => {
      const wrapper = await mountWithPolicy({ on: ['failure'], max_retries: 2 })
      const vm = wrapper.vm as any
      expect(vm.retryPolicyDelaySeconds).toBe(45)
      expect(vm.retryPolicyMultiplier).toBe(2)
      await vm.saveRetryPolicy()
      await flushPromises()

      expect(lastPatchBody().retry_policy).toEqual({
        on: ['failure'],
        max_retries: 2,
        backoff_schedule: { delay_seconds: 45, multiplier: 2 },
      })
    })

    it('clamps out-of-range stored schedule values and surfaces the runtime fail-open warning', async () => {
      const wrapper = await mountWithPolicy({
        on: ['failure'],
        max_retries: 1,
        backoff_schedule: { delay_seconds: 1000, multiplier: 25 },
      })
      const vm = wrapper.vm as any
      expect(vm.retryPolicyDelaySeconds).toBe(300)
      expect(vm.retryPolicyMultiplier).toBe(10)

      // open the panel (toggle re-syncs from the same stored policy) and check
      // the warning states the ACTUAL runtime behaviour: fail-open to default.
      await wrapper.find('[data-testid="pipeline-editor-retry-policy-toggle"]').trigger('click')
      await nextTick()
      const warning = wrapper.find('[data-testid="pipeline-editor-retry-policy-schedule-warning"]')
      expect(warning.exists()).toBe(true)
      expect(warning.text()).toContain('fails open')
    })

    it('does not warn for in-range or absent schedules', async () => {
      const wrapper = await mountWithPolicy({
        on: ['failure'],
        max_retries: 1,
        backoff_schedule: { delay_seconds: 60 },
      })
      const vm = wrapper.vm as any
      expect(vm.retryPolicyDelaySeconds).toBe(60)
      expect(vm.retryPolicyMultiplier).toBe(2)
      expect(vm.retryPolicyScheduleWarning).toBeNull()

      await wrapper.find('[data-testid="pipeline-editor-retry-policy-toggle"]').trigger('click')
      await nextTick()
      expect(wrapper.find('[data-testid="pipeline-editor-retry-policy-schedule-warning"]').exists()).toBe(false)
    })
  })
})
