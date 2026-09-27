# Chat-first workbench

The WMS Assistant adopts a familiar chat layout without copying ChatGPT branding:
neutral sidebar, conversation history/search, a centered transcript, user bubbles,
formatted assistant answers and a bottom-pinned composer.

- Select **＋ 新对话** to start a blank conversation. Suggested prompts only populate
  the composer; they do not submit a request or incur model usage until sent.
- Use sidebar history/search to reopen sessions. Workspace remains host-selected.
- Sending from an existing conversation carries the selected revision. Historical,
  disabled and non-resumable states keep the composer disabled.
- **草稿、证据与审批** contains the existing draft, dependencies, evidence, review,
  export and feedback controls. They remain separate explicit actions, not chat commands.
- Review still requires a comment and confirmation. Backend checks remain authoritative.
- User text is displayed literally. Assistant Markdown is escaped for raw HTML and
  remote image syntax is removed, avoiding unsolicited image fetches.
- No database migration or provider/model change is part of this redesign.

The local full-index launcher uses the same renderer; the former large introductory
warning is replaced by workspace/privacy details in the sidebar and a persistent
no-WMS-write notice. The supplied documents and questions may still be sent to the
configured provider as described in the grounded-answer contract.

## Verification

494 full tests passed, 3 live-provider cases explicitly skipped, coverage 91.04%.
The renderer tests cover new chat, prompt selection without submission, sending,
history search, selected revision, disabled controls and actual assistant messages.
Headless Chromium checks use synthetic data only; screenshots cover 1440px desktop
and 390px mobile with no horizontal overflow. Issue #80 / internal PR #81 covers
the avatar-rendering and narrow-screen header fixes found during visual QA.
Issue #82 / internal PR #83 restores the mobile sidebar toggle; an actual 390px
browser click confirmed that history remains accessible. The live local test page
also passed a render-only check for its title, composer and new-chat button, without
submitting any private content or invoking a model.

Feature branch: `feature/chat-workbench`, based on the current working canary
candidate (`bugfix/model-token-budget`, PR #74), not yet merged to dev. The UI
feature remains for user review; previously open canary readiness issues are unchanged.
