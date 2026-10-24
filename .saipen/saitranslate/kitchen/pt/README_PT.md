# 9Router Extra / Scanner e suíte de integração

Versão: **v0.2.0**
Projeto: 9router_extra
Caminho: `V:\___VAC\__K\__CODE\_PY\_9router_extra\`

## Visão geral
Este repositório contém extensões, ferramentas, backups de estado e patches para o 9Router:
1. **Ponte de provedor WorkBuddy AI (`wb/hy3`)**
   - Integração direta com as completions na nuvem do WorkBuddy AI (`https://www.workbuddy.ai/v2/chat/completions`).
   - Suporta o modelo de raciocínio Hunyuan `hy3` (força `stream: true`, blocos de conteúdo tipados, `reasoning_effort: high`, `reasoning_summary: auto`).
   - Importação automática local do token a partir de `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop-ai.info`.
   - Suporte a renovação de token e acompanhamento de cota.
2. **Correção do Antigravity Gemini 3.8 Flash e resolução dinâmica de nível**
   - Corrige o problema 404 do upstream: o upstream do Antigravity exige `gemini-3.8-flash-tiered(high|medium|low)`.
   - Adiciona correspondência dinâmica de padrões, de modo que qualquer `gemini-*-flash-(high|medium|low)` seja mapeado para o formato em níveis sem 404.
3. **Adição de modelos no início do combo SAIFREN**
   - `wb/hy3` configurado como 1.º modelo e `ag/gemini-3.8-flash-high` como 2.º modelo no `SAIFREN`.
4. **Patches do AgentRouter e do Cline**
   - Provedor nativo AgentRouter, encaminhamento do User-Agent do cliente, resolução da URL base da Anthropic e correções de roteamento do Cline.
5. **Motor de backup e migração de estado**
   - Instantâneo de todas as 36 conexões, 13 nós personalizados (B.AI, Dahl, Vyce etc.) e combos.

## Estrutura
- `apply-update.ps1` - script de atualização em 1 clique que para, faz backup, atualiza com segurança para v0.5.65-extra, restaura o estado e reinicia o 9router.
- `packages/`
  - `9router-0.5.65-extra.tgz` - pacote v0.5.65 totalmente compilado e corrigido, pronto para instalação global.
  - `9router-0.5.59-agentrouter.tgz` - pacote legado v0.5.59.
- `patches/`
  - `9router-0.5.65-extra-unified.patch` - patch unificado contra o upstream v0.5.65.
  - `9router-extra-unified.patch` - patch unificado contra a v0.5.59.
- `backup/`
  - `export_state.js` e `restore_state.js` - ferramentas de backup e sincronização do estado SQLite
    (o estado privado agora aponta para `%LOCALAPPDATA%\9router_WatchEdit\backups\private\`, nunca para este repositório).

## Executar os testes do código-fonte do 9Router (armadilha de invocação do vitest, T-21)
O código-fonte do painel Next.js fica em `%APPDATA%\9router\source`. Seus aliases de caminho
do vitest (`@/` → `src`, `open-sse` → `open-sse`) são definidos SOMENTE em
`tests/vitest.config.js`.

**NÃO invoque o binário a partir da raiz do código-fonte:**
```powershell
# WRONG: bypasses tests/vitest.config.js, every "@/..." import fails
.\tests\node_modules\.bin\vitest.cmd run tests/unit/model-test-routing.test.js
#   -> Error: Cannot find package '@/shared/constants/config' imported from
#      src/app/api/models/test/ping.js
```

Use a invocação que respeita a configuração:
```powershell
# RIGHT: loads tests/vitest.config.js (aliases resolve)
npm --prefix tests test -- unit/model-test-routing.test.js
```

Um `vitest.config.js` na raiz que reexporte a configuração de testes também faz o
binário da raiz funcionar; ele é mantido na árvore do código-fonte para que as duas
invocações concordem. `tests/unit/model-routing.test.js` tem um `EPERM` preexistente
e não relacionado no teardown de `os.tmpdir()` neste ambiente — ele falha de forma
idêntica nas duas invocações e não é um problema de alias.

## Modelo de segurança
Este repositório não contém segredos e é seguro entregá-lo a agentes de programação externos.
Os segredos vivem apenas na camada de execução local (`%LOCALAPPDATA%\9router_WatchEdit\` e na
instalação do 9Router). O WatchEdit roda com `SECRETS: LOCKED` por padrão; operações ao vivo
com provedores exigem um desbloqueio explícito (concessão respaldada pelo SO ou cofre opcional com senha mestra).
Consulte **SECURITY_LOCAL.md** para o modelo completo, o gerador de compartilhamento seguro
(`python tools/build_safe_share.py`), o scanner de segredos e as ferramentas de backup privado.
  - `data-snapshot/` - cópia completa do banco de dados `%APPDATA%\9router\db\data.sqlite`.

## Como aplicar a atualização
Quando suas tarefas ativas no 9router terminarem, basta executar:
```powershell
.\apply-update.ps1
```
Isso irá:
1. Parar a instância do 9router em execução.
2. Criar um backup com carimbo de data/hora em `%APPDATA%\9router_backup_<timestamp>`.
3. Instalar o `9router-0.5.65-extra.tgz` globalmente.
4. Sincronizar todas as conexões e atualizar o combo SAIFREN.
5. Reiniciar o 9router com um ícone na área de notificação do sistema.

<!-- translation-model: codebuff-agent in-session translation (not the local qwen3:14b pipeline) -->
<!-- source-digest: README.md sha256:082c6cba0dbf49757cbab042ad6e9fec1162ed12091176b2cef3ed1ffe6c5c4c -->
