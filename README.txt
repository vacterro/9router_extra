9Router Quick Live Scanner
===========================

START
-----
Double-click:

    START.bat

Or run manually:

    powershell.exe -NoProfile -ExecutionPolicy Bypass -STA -File ".\9RouterQuickScanner.ps1"

WORKFLOW
--------
1. Enter provider:
       https://vsllm.com
   or:
       vsllm
   or any OpenAI-compatible/New API domain.

2. Enter API key.

3. Press:
       SCAN MODELS

4. The table shows ONLY models that completed a real minimal inference
   request with HTTP 200.

5. API column:
       CHAT       -> /v1/chat/completions
       RESPONSES  -> /v1/responses

6. COPY MODELS copies only the visible live model IDs, one per line.

QUICK SCAN POLICY
-----------------
- 8 models are tested in parallel.
- Fast request timeout: 4 seconds.
- If a request times out, that route gets one delayed retry up to 12 seconds.
- 400/404/405/422 route-shape failures may try the alternate OpenAI protocol.
- 401/402/403/429 and ordinary server failures are not shown as live.
- Embedding/image/TTS/video/moderation-like models are skipped.
- The API key stays in memory only.
- Closing the window stops active work.

WHY THIS IS QUICKER
-------------------
The old scanner could wait up to 180 seconds per request.
This scanner does short parallel probes and only spends the longer
12-second allowance on timeout candidates.

UI
--
UI.md is included as the canonical Golden Default contract used by this tool.
The implementation uses:
- Golden Default palette tokens
- Verdana
- aliased WPF text rendering
- square Win95-style geometry
- raised/sunken bevel language
- no gradients, blur, rounded corners, animation, or progress spinner
- fixed 640x480 layout
- text progress: CHECKED / LIVE

FIX NOTE
--------
Version 2.1 fixes a PowerShell scope bug where the async catalog job started,
but the WPF DispatcherTimer could not see its shared state. The visible symptom
was an endless:

    READING CATALOG | Requesting /v1/models...
    CHECKED 0/0 | LIVE 0

Shared async state now uses script scope, so catalog completion and model probes
are processed by the UI timer correctly.
