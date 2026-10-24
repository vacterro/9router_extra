# 9Router Extra / Scanner e suite di integrazione

Versione: **v0.2.0**
Progetto: 9router_extra
Percorso: `V:\___VAC\__K\__CODE\_PY\_9router_extra\`

## Panoramica
Questo repository contiene estensioni, strumenti, copie di backup dello stato e patch per 9Router:
1. **Bridge del provider WorkBuddy AI (`wb/hy3`)**
   - Integrazione diretta con i completamenti cloud di WorkBuddy AI (`https://www.workbuddy.ai/v2/chat/completions`).
   - Supporta il modello di ragionamento Hunyuan `hy3` (forza `stream: true`, usa blocchi di contenuto tipizzati, `reasoning_effort: high`, `reasoning_summary: auto`).
   - Importazione automatica del token locale da `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop-ai.info`.
   - Supporto per l'aggiornamento del token e il monitoraggio delle quote.
2. **Correzione di Antigravity Gemini 3.8 Flash e risoluzione dinamica dei livelli**
   - Risolve l'errore 404 restituito dal servizio upstream: il servizio Antigravity upstream richiede `gemini-3.8-flash-tiered(high|medium|low)`.
   - Aggiunge un abbinamento dinamico dei pattern, in modo che qualsiasi `gemini-*-flash-(high|medium|low)` venga mappato nel formato per livelli senza 404.
3. **Aggiunta iniziale alla combinazione SAIFREN**
   - Configurati `wb/hy3` come primo modello e `ag/gemini-3.8-flash-high` come secondo modello in `SAIFREN`.
4. **Patch per AgentRouter e Cline**
   - Fornitore nativo AgentRouter, inoltro dell'User-Agent del client, risoluzione dell'URL base di Anthropic e correzioni di routing di Cline.
5. **Motore di backup e migrazione dello stato**
   - Istantanea di tutte le 36 connessioni, dei 13 nodi personalizzati (B.AI, Dahl, Vyce, ecc.) e delle combinazioni.

## Struttura
- `apply-update.ps1` - Script di aggiornamento con un clic che arresta in sicurezza 9router, crea un backup, aggiorna a v0.5.65-extra, ripristina lo stato e riavvia 9router.
- `packages/`
  - `9router-0.5.65-extra.tgz` - Pacchetto v0.5.65 completamente compilato e corretto, pronto per l'installazione globale.
  - `9router-0.5.59-agentrouter.tgz` - Pacchetto legacy v0.5.59.
- `patches/`
  - `9router-0.5.65-extra-unified.patch` - Patch unificata per v0.5.65 upstream.
  - `9router-extra-unified.patch` - Patch unificata per v0.5.59.
- `backup/`
  - `export_state.js` & `restore_state.js` - Strumenti di backup e sincronizzazione dello stato SQLite
    (lo stato privato ora punta a `%LOCALAPPDATA%\9router_WatchEdit\backups\private\`, mai a questo repository).

## Esecuzione dei test del codice sorgente di 9Router (trappola dell'invocazione vitest, T-21)
Il codice sorgente del dashboard Next.js si trova in `%APPDATA%\9router\source`. Gli alias
di percorso di vitest (`@/` → `src`, `open-sse` → `open-sse`) sono definiti SOLO in
`tests/vitest.config.js`.

**Non invocare il binario dalla radice del sorgente:**
```powershell
# WRONG: bypasses tests/vitest.config.js, every "@/..." import fails
.\tests\node_modules\.bin\vitest.cmd run tests/unit/model-test-routing.test.js
#   -> Error: Cannot find package '@/shared/constants/config' imported from
#      src/app/api/models/test/ping.js
```

Utilizza invece l'invocazione che carica la configurazione:
```powershell
# RIGHT: loads tests/vitest.config.js (aliases resolve)
npm --prefix tests test -- unit/model-test-routing.test.js
```

Un file `vitest.config.js` a livello di radice che ri-esporta la configurazione dei test rende
funzionante anche l'esecuzione del binario dalla radice; viene mantenuto nell'albero del sorgente in modo che entrambe le invocazioni siano coerenti.
`tests/unit/model-routing.test.js` ha un errore `EPERM` preesistente e non correlato durante la pulizia di
`os.tmpdir()` in questo ambiente: fallisce allo stesso modo con entrambe le invocazioni e non è un problema di alias.

## Modello di sicurezza
Questo repository non contiene segreti e può essere consegnato in sicurezza ad agenti di programmazione esterni.
I segreti risiedono soltanto nel livello di esecuzione locale (`%LOCALAPPDATA%\9router_WatchEdit\` e nell'installazione di 9Router).
Per impostazione predefinita, WatchEdit usa `SECRETS: LOCKED`; le operazioni in tempo reale con i provider richiedono uno sblocco esplicito (autorizzazione supportata dal sistema operativo oppure cassaforte opzionale con password principale).
Consultare **SECURITY_LOCAL.md** per il modello completo, il generatore di condivisioni SAFE
(`python tools/build_safe_share.py`), l'analizzatore dei segreti e gli strumenti per i backup privati.
  - `data-snapshot/` - Copia completa del database `%APPDATA%\9router\db\data.sqlite`.

## Come applicare l'aggiornamento
Quando le tue attività in 9router sono concluse, esegui:
```powershell
.\apply-update.ps1
```
Questo comando:
1. Arresta l'istanza di 9router in esecuzione.
2. Crea una copia di backup con timestamp in `%APPDATA%\9router_backup_<timestamp>`.
3. Installa globalmente `9router-0.5.65-extra.tgz`.
4. Sincronizza tutte le connessioni e aggiorna la combinazione SAIFREN.
5. Riavvia 9router in modalità tray.

<!-- translation-model: qwen3:14b contract:structured-markdown-v2 -->
<!-- source-digest: README.md sha256:082c6cba0dbf49757cbab042ad6e9fec1162ed12091176b2cef3ed1ffe6c5c4c -->
