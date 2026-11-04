# Agent workspace page redesign

## Context

The agent workspace (`/agents/:id`) is a single scrolling page with five stacked sections (Agent details, Runtime settings, Conversation flow, Validate and publish, Version history) in a left column and two sticky panels (Chat Test, Flow QA) in a right rail. All sections render simultaneously inside an 888-line `PlatformAgentWorkspace.tsx` component. The page is long, dense, and mixes unrelated workflows — identity editing, flow authoring, publishing, and testing all compete for vertical space and attention. The redesign replaces the vertical dump with a tabbed workspace that groups content by task, keeps the Chat Test panel always accessible, and decomposes the monolith into focused modules.

## Approach

### Step 1: Add a horizontal tab bar below the header

**What changes:** Replace the current vertical stack of five sections with a tab bar rendered after the header and error banners. Three tabs: **Configure**, **Flow**, **Publish**.

The tab bar uses the same `button-primary`/`button-secondary` toggle pattern already used by the flow editor's Steps / Flow view / Flow JSON switcher and the mobile Workspace / Test agent toggle. Store the active tab in a `workspaceTab` state variable (type `"configure" | "flow" | "publish"`, default `"configure"`). Persist it in a `?section=` query parameter alongside the existing `?tab=` for mobile, using `router.replace` with `scroll: false` (same pattern as `setMobileMode` at line 590–593).

The tab bar renders inside `<main>`, replacing the current `space-y-6` section list. Only the active tab's content renders. The right rail (`<aside>`) remains unchanged — Chat Test and Flow QA stay sticky on the right at `lg:` widths and behind the mobile Test agent toggle on small screens.

**Tab contents:**

- **Configure** — Agent details section + Runtime settings section (the two form sections, lines 680–758). These are the "what is this agent" settings that rarely change during a flow-editing session.
- **Flow** — Conversation flow editor (`ConversationFlowEditor` component). This is the primary authoring surface and gets the full main-column width to itself.
- **Publish** — Validate and publish section + Version history section (lines 770–858). These are the release-lifecycle actions, used after editing is done.

**Exact markup for the tab bar:**

```tsx
<div role="tablist" aria-label="Workspace section" className="flex gap-2 border-b border-ink-800 pb-4">
  {(["configure", "flow", "publish"] as const).map((tab) => (
    <button
      key={tab}
      type="button"
      role="tab"
      aria-selected={workspaceTab === tab}
      className={workspaceTab === tab ? "button-primary" : "button-secondary"}
      onClick={() => setWorkspaceTab(tab)}
    >
      {tab === "configure" ? "Configure" : tab === "flow" ? "Flow" : "Publish"}
    </button>
  ))}
</div>
```

Use `role="tablist"` / `role="tab"` / `aria-selected` for accessibility (the existing flow-editor toggle uses `aria-pressed` on a `role="group"`, which is acceptable for a toggle group but tablist is the correct pattern for switching visible panels). Each tab panel wraps its content in a `<div role="tabpanel">`.

**Files touched:** `web/src/components/PlatformAgentWorkspace.tsx` only.

### Step 2: Merge Agent details save into the header's Save draft flow

**What changes:** Remove the standalone "Save agent details" button and form submit handler. Instead, when the user edits name/purpose/persona and clicks "Save draft" in the header, save identity fields first (if dirty), then save the draft config. This eliminates the confusing two-save-button pattern visible in the screenshot.

The header's `saveDraftNow` function (currently at ~line 271) gains a preamble: if `identityDirty` is true, call `handleSaveIdentity` first and await it; on failure, bail. The existing `identityDirty` computation already compares the local identity fields against `agent.name`, `agent.description`, `agent.persona_prompt`. The Agent details section becomes a plain `<div>` instead of a `<form>` — its fields remain controlled inputs but have no local submit button.

The header's `draftStatusLabel` already shows "Unsaved agent details" when identity is dirty (line 615–618). The Save draft button is currently disabled when identity is dirty (`identityDirty` appears in `publishDisabled` but not in the Save draft button's `disabled`); fix this: the Save draft button should be enabled when *either* identity or config is dirty, and it saves both.

The "Refresh draft snapshot" button (shown when persona changes require a re-snapshot) moves into the header area, next to Save draft, appearing only when `snapshotRefreshNeeded` is true.

**Exact identity-dirty derivation** (already exists but currently implicit):

```ts
const identityDirty = agent
  ? identityName !== agent.name ||
    identityDescription !== agent.description ||
    identityPersona !== agent.persona_prompt
  : false;
```

This is already computed inline; extract it as a `const` if it isn't one yet (scout report says it's inline in the status label logic — verify and extract).

**Files touched:** `web/src/components/PlatformAgentWorkspace.tsx` only.

### Step 3: Promote the Publish tab's validation + publish into a compact, action-oriented layout

**What changes:** Inside the Publish tab, replace the current vertical stack of explanatory text → validate button → status → warnings → version notes → publish button with a tighter two-row layout:

Row 1: "Validate current draft" button on the left, validation status badge on the right (the green/red eligibility indicator). Warning acknowledgements appear as a collapsible fieldset below this row only when warnings exist.

Row 2: Version notes input and Publish button on the same row, side by side. The version notes input takes `flex-1` and the Publish button sits at the end. The existing dirty-state hint ("Wait for the draft to save before publishing.") renders as a small `text-xs` note below the row when applicable.

Version history stays below, unchanged in structure.

**Files touched:** `web/src/components/PlatformAgentWorkspace.tsx` only — the publish/validate section is inline.

### Step 4: Extract inline sections into separate component files

**What changes:** Move the three tab bodies out of the monolith:

1. **`web/src/components/AgentConfigureTab.tsx`** — Receives agent identity fields (name, description, persona, setters, disabled states), runtime settings (config, updateRuntimeSetting, textOnly, updateTextOnly), and the creation date. Renders the Agent details form fields and the Runtime settings grid. No submit button; identity save is handled by the parent's Save draft.

2. **`web/src/components/AgentPublishTab.tsx`** — Receives validation state, publish state, warning acknowledgements, changelog, version list, and action callbacks (validate, publish, restore). Renders the compact validate/publish layout from Step 3 and the version history list.

3. **`ConversationFlowEditor.tsx`** stays as-is — it's already a separate component.

The parent `PlatformAgentWorkspace.tsx` shrinks from ~888 lines to ~450 lines: header, tab bar, tab routing, state, and API logic remain; rendering moves to the tab components.

**Prop interfaces** — each new component exports its props interface:

```ts
// AgentConfigureTab.tsx
interface AgentConfigureTabProps {
  createdAt: string;
  identityName: string;
  identityDescription: string;
  identityPersona: string;
  onIdentityNameChange: (value: string) => void;
  onIdentityDescriptionChange: (value: string) => void;
  onIdentityPersonaChange: (value: string) => void;
  identityDisabled: boolean;
  config: JsonObject;
  onRuntimeSettingChange: (key: RuntimeSettingKey, value: string) => void;
  textOnly: boolean;
  onTextOnlyChange: (checked: boolean) => void;
}

// AgentPublishTab.tsx
interface AgentPublishTabProps {
  validation: DraftValidation | null;
  currentValidation: boolean;
  eligibleToPublish: boolean;
  validationLoading: boolean;
  onValidate: () => void;
  validateDisabled: boolean;
  warningCodes: string[];
  acknowledgedWarnings: string[];
  onAcknowledgedWarningsChange: (warnings: string[]) => void;
  allWarningsAcknowledged: boolean;
  changelog: string;
  onChangelogChange: (value: string) => void;
  publishDisabled: boolean;
  publishLoading: boolean;
  onPublish: () => void;
  dirty: boolean;
  versions: VersionView[];
  versionsLoading: boolean;
  versionsError: string | null;
  onRetryVersions: () => void;
  restoringVersionId: string | null;
  onRestoreVersion: (version: VersionView) => void;
  restoreDisabled: boolean;
}
```

`RuntimeSettingKey` type and `SETTINGS` constant move to a shared location — export them from `PlatformAgentWorkspace.tsx` or a small `web/src/lib/agent-workspace-constants.ts`. Prefer the constants file since both the parent and `AgentConfigureTab` need them.

**Files touched:** Create `web/src/components/AgentConfigureTab.tsx`, `web/src/components/AgentPublishTab.tsx`, `web/src/lib/agent-workspace-constants.ts`. Edit `web/src/components/PlatformAgentWorkspace.tsx` to import and render them.

### Step 5: Visual polish — section spacing and tab transition

**What changes:**

- Remove the outer `space-y-6` on `<main>` since each tab now shows one or two related sections. Use `space-y-6` inside each tab panel's wrapper div.
- Within the Configure tab, the two sections (Agent details, Runtime settings) render as visually distinct `.surface` cards separated by `gap-6`, same as today.
- The tab bar buttons get a bottom-border active indicator instead of relying solely on `button-primary` fill color: add `border-b-2 border-copper-500` to the active tab and `border-b-2 border-transparent` to inactive tabs for a more conventional tab affordance. Keep the `button-primary`/`button-secondary` classes for fill.
- On the Flow tab, the `ConversationFlowEditor`'s own Steps/Flow view/Flow JSON sub-tabs feel natural — the workspace tab selects the domain, the editor tabs select the view. No changes needed to the editor itself.

**Files touched:** `web/src/components/PlatformAgentWorkspace.tsx` (tab bar classes), `web/src/app/globals.css` (no changes needed — existing classes suffice).

## Critical files & anchors

| File | Symbol / region | Reason |
|---|---|---|
| `web/src/components/PlatformAgentWorkspace.tsx` | Lines 630–888, return statement | All layout changes happen here; the tab bar, tab routing, and section extraction |
| `web/src/components/PlatformAgentWorkspace.tsx` | Lines 73–120, state declarations | New `workspaceTab` state; `identityDirty` extraction |
| `web/src/components/PlatformAgentWorkspace.tsx` | `saveDraftNow` (~line 271) | Merge identity save into draft save |
| `web/src/components/ConversationFlowEditor.tsx` | Lines 119–130, view toggle | Reference pattern for the workspace tab bar |
| `web/src/app/globals.css` | `.button-primary`, `.button-secondary` | Confirm these suffice for tab styling; no additions needed |

## Verification

1. **Tab navigation:** Open `/agents/{id}` — the Configure tab is active and shows Agent details + Runtime settings. Click Flow — only the flow editor appears. Click Publish — validate/publish and version history appear. The right rail (Chat Test, Flow QA) stays visible throughout at `lg:` widths.

2. **URL persistence:** Click the Flow tab, reload the page — the `?section=flow` param restores the Flow tab. Switch to the mobile Test agent toggle — `?tab=test` coexists with `?section=flow`.

3. **Unified save:** Edit the agent name on the Configure tab, then switch to the Flow tab and edit a step. Click Save draft in the header — both the identity PATCH and the draft PATCH fire. Verify the status label transitions: "Unsaved agent details" → "Saving…" → "Saved rN".

4. **Publish flow:** On the Publish tab, click Validate current draft. After validation, fill in version notes and click Publish version. Confirm the new version appears in the history list below.

5. **Mobile:** At a narrow viewport, the Workspace / Test agent toggle still works. Within Workspace, the three tabs (Configure, Flow, Publish) appear and switch content. Confirm the tab bar scrolls horizontally if needed (`flex-wrap` or `overflow-x-auto`).

6. **Existing test suite:** Run `scripts/voice-ai.sh test` — all existing tests pass, particularly `tests/test_observers.py` and `tests/test_modularity.py`.

## Assumptions & contingencies

- **`identityDirty` is computed inline, not as a named const.** The plan extracts it as a const. If it's already a named variable, skip the extraction and use the existing name.
- **`saveDraftNow` is a standalone function, not entangled with form submit.** If `handleSaveIdentity` has side effects beyond the PATCH (e.g., resetting snapshot state), those must also run inside the merged save path. The plan assumes the identity save is a clean async function that resolves on success — if it throws, the draft save does not proceed.
- **No router-level code splitting needed.** The three tabs are small enough to bundle together. If the Flow tab's editor is heavy and causes a measurable load-time regression, wrap `ConversationFlowEditor` in `React.lazy` + `Suspense` as a follow-up — but this is unlikely given its 371-line size.
