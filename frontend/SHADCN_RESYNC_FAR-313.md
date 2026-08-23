# FAR-313: shadcn-vue ui/ re-sync — audit & outcome

## Scope of the re-sync

The ticket named ten primitives to re-sync with the canonical shadcn-vue registry:
`badge, button, card, dialog, dropdown-menu, input, select, tabs, tooltip, data-table`.

A grep of `frontend/src/components/ui/` shows that **only `data-table` is actually
present in the repo**. The other nine are not tracked — they appear only as
`vi.mock` factories inside specs and as `components.json` alias targets. There is
therefore nothing to re-sync for the nine absent primitives; the only real subject
is `frontend/src/components/ui/data-table/`.

## Why the CLI re-sync could not run

The modern shadcn-vue command is `add --overwrite` (the old `update` subcommand
was removed in shadcn-vue 2.8.2). When `pnpm dlx shadcn-vue@latest add --overwrite data-table` is attempted:

- The local pnpm dlx cache for `shadcn-vue@2.8.2` was corrupt (missing
  `dist/index.js`) and `npm`/`npx` downloads to this sandbox were too slow to
  complete within a session timeout.
- Independent of the local tool failure, the canonical registry has **no
  `data-table` entry for any style**. Verified by direct HTTP probe against the
  shadcn-vue registry host:

  ```
  https://shadcn-vue.com/r/reka-nova/data-table.json  -> 404 Not Found
  https://shadcn-vue.com/r/new-york/data-table.json   -> 404 Not Found
  https://shadcn-vue.com/r/default/data-table.json    -> 404 Not Found
  ```

  This matches the previously-reported blocker: *"data-table.json was not found
  at the registry"* for the `reka-nova` style set in `components.json`.

Conclusion: the tool genuinely cannot re-sync `data-table` — the registry entry
does not exist for any available style. The task's fallback path (manual class
alignment) is therefore the correct and only viable route.

## Manual alignment performed

The repository `DataTable.vue` is **not** the canonical shadcn-vue data-table
(TanStack + reka-ui suite). It is a self-contained, dependency-free component with
a fixed public contract that 5 views depend on:

- Props: `columns`, `rows`, `loading`, `loadingRows`, `rowClickable`
- Emit: `row-click`
- Slots: `cell-<key>`, `empty`

Replacing it with the canonical TanStack/reka-ui data-table would break all five
consumers (`AdminCostBreakdownView`, `AdminCostControlsView`, `AdminErrorsView`,
`RunsListView`, `CostComponentsView`) and pull in `reka-ui` + `@tanstack/vue-table`.
That is out of scope for a re-sync and would be a breaking change, not a sync.

Instead, the component's Tailwind classes were aligned to the canonical
shadcn-vue **Table** token conventions (the same tokens the registry's
`data-table` is built on), preserving the component's API and behaviour exactly:

| Element | Before | After (canonical token) |
|---|---|---|
| wrapper | `overflow-x-auto` | `relative w-full overflow-x-auto` |
| `<table>` | `w-full` | `w-full caption-bottom text-sm` |
| header `<tr>` | (none) | `border-b transition-colors hover:bg-muted/50` |
| `<th>` | `px-4 py-3 text-xs font-medium uppercase tracking-wider text-left` | `h-12 px-4 text-left align-middle font-medium text-muted-foreground` |
| body `<tr>` | `divide-y divide-border`, `hover:bg-muted/30` | `border-b transition-colors hover:bg-muted/50` (+ `[&_tr:last-child]:border-0` on `<tbody>`) |
| `<td>` | `px-4 py-3 text-sm` | `p-4 align-middle text-sm` |

The numeric right-align, sortable cursor/select behaviour, loading skeleton, and
`empty` slot are unchanged. No imports, props, emits, or slots were added or
removed.

## Verification

- `pnpm run lint -- --quiet` → exit 0
- `pnpm exec vue-tsc --noEmit` → exit 0
- `pnpm run test:unit` → 1429 passed, 141 files, exit 0

No test was deleted or disabled. `package.json`, `pnpm-lock.yaml`, and
`src/style.css` were NOT modified — the tool re-sync was not run, so its
forced dependency changes (reka-ui, lucide bump) were correctly avoided.
