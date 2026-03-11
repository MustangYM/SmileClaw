# Internal AI Iterative Retriever (Design v0.1)

## 1. Goal
Build a reusable retrieval pipeline that can handle "find file"-like requests without hardcoding one keyword (for example, not only `简历`).

Design goals:
- Internal-only iteration: planner and retries are hidden from end users.
- Generic: can be reused for resume/contract/invoice/notes-like searches.
- Safe by default: approval/policy checks remain enforced before shell execution.
- User-facing output is concise and actionable.

## 2. Non-goals
- No full-text indexing service in v0.1.
- No vector DB dependency in v0.1.
- No channel-specific UI behavior in this module (Telegram button rendering stays in gateway layer).

## 3. Execution Model
For each user turn:
1. Detect whether the request is likely a file retrieval task.
2. If yes, call an internal planner LLM to produce search plan JSON:
   - `keywords`
   - `directories`
   - `extensions`
3. Execute staged shell searches (max 3 rounds) with policy/approval checks before each command.
4. Aggregate candidates and generate a user-safe summary.
5. Return one final assistant message only (no command JSON, no tool-result dump).

## 4. Staged Search Strategy
### Round 1: keyword + extension filtering
- Target likely directories (`~/Documents`, `~/Desktop`, `~/Downloads` by default).
- Search by planner keywords and selected extensions.

### Round 2: broader filename scan
- If round 1 has no hit, search same directories by extensions only.
- Collect broader candidate set.

### Round 3: semantic rerank (internal LLM)
- If candidates exist but confidence is low, ask internal LLM to rerank/select top items.
- Return top N candidates to user.

## 5. Safety & Approval
- Every generated shell command must call policy/approval check first.
- If approval is required:
  - Create pending approval via existing approval manager flow.
  - Return concise pending message (do not include raw command payload to user).
- If policy denies, return a clear denial message.

## 6. User-facing Output Contract
Allowed in final user message:
- What was searched (high-level directory names).
- Candidate file list (path only).
- Suggested next step when no match.

Not allowed:
- Raw tool JSON.
- Shell command bodies.
- Internal retry transcript.

## 7. Integration Points
- New module: `src/agent/file_retriever.py`
- Agent hook: invoke retriever before general LLM loop in `Agent.run()`.
- Existing approval manager remains source of truth for permission gating.

## 8. Fallback Behavior
- If planner JSON is invalid, use deterministic defaults.
- If tool errors occur, continue to next round when safe.
- If still no usable result, return concise "not found + next action" message.

## 9. Test Plan (v0.1)
- Intent detection works for Chinese/English search phrases.
- Retriever returns concise summary without leaking command/tool-result strings.
- Approval-required path creates pending approval and halts execution.
- Agent integrates retriever path and avoids normal LLM response leakage for those turns.

## 10. Content-level Retrieval (Implemented in current branch)
- Candidate content extraction:
  - `TXT/MD/RTF`: direct text read
  - `DOCX`: unzip `word/document.xml` and extract paragraph text
  - `PDF`: use `pdftotext` if available, otherwise best-effort text stream fallback
- Ranking:
  - filename/path lexical match score
  - content keyword hit score
  - optional internal rerank by LLM on top candidates
- Security:
  - content extraction only runs on files returned by policy-checked search commands

## 11. Next Iteration
- Configurable directory profiles by channel/user preference.
- Caching recent search candidates per session.
- Better PDF parser with richer Unicode extraction when dependency budget allows.
