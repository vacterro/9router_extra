# 9Router Extra / Scanner et suite d'intégration

Version : **v0.2.0**
Projet : 9router_extra
Chemin : `V:\___VAC\__K\__CODE\_PY\_9router_extra\`

## Aperçu
Ce dépôt contient des extensions, des outils, des sauvegardes d'état et des correctifs pour 9Router :
1. **Pont de fournisseur WorkBuddy AI (`wb/hy3`)**
   - Intégration directe avec les completions cloud de WorkBuddy AI (`https://www.workbuddy.ai/v2/chat/completions`).
   - Prend en charge le modèle de raisonnement Hunyuan `hy3` (force `stream: true`, blocs de contenu typés, `reasoning_effort: high`, `reasoning_summary: auto`).
   - Import automatique local du jeton depuis `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop-ai.info`.
   - Prise en charge du renouvellement du jeton et du suivi du quota.
2. **Correctif Antigravity Gemini 3.8 Flash et résolution dynamique des paliers**
   - Corrige le problème 404 en amont : l'upstream Antigravity exige `gemini-3.8-flash-tiered(high|medium|low)`.
   - Ajoute une correspondance dynamique de motifs, de sorte que tout `gemini-*-flash-(high|medium|low)` soit mappé au format à paliers sans 404.
3. **Ajout en tête du combo SAIFREN**
   - `wb/hy3` configuré comme 1er modèle et `ag/gemini-3.8-flash-high` comme 2e modèle dans `SAIFREN`.
4. **Correctifs AgentRouter et Cline**
   - Fournisseur AgentRouter natif, transmission du User-Agent du client, résolution de l'URL de base Anthropic et correctifs de routage Cline.
5. **Moteur de sauvegarde et de migration d'état**
   - Instantané des 36 connexions, de 13 nœuds personnalisés (B.AI, Dahl, Vyce, etc.) et des combos.

## Structure
- `apply-update.ps1` - script de mise à jour en 1 clic qui arrête, sauvegarde, met à niveau en toute sécurité vers v0.5.65-extra, restaure l'état et redémarre 9router.
- `packages/`
  - `9router-0.5.65-extra.tgz` - paquet v0.5.65 entièrement construit et corrigé, prêt pour une installation globale.
  - `9router-0.5.59-agentrouter.tgz` - paquet hérité v0.5.59.
- `patches/`
  - `9router-0.5.65-extra-unified.patch` - correctif unifié contre l'upstream v0.5.65.
  - `9router-extra-unified.patch` - correctif unifié contre v0.5.59.
- `backup/`
  - `export_state.js` et `restore_state.js` - outils de sauvegarde et de synchronisation de l'état SQLite
    (l'état privé cible désormais `%LOCALAPPDATA%\9router_WatchEdit\backups\private\`, jamais ce dépôt).

## Exécuter les tests des sources 9Router (piège d'invocation vitest, T-21)
Les sources du tableau de bord Next.js se trouvent dans `%APPDATA%\9router\source`. Ses alias de chemin
vitest (`@/` → `src`, `open-sse` → `open-sse`) sont définis UNIQUEMENT dans
`tests/vitest.config.js`.

**N'invoquez PAS le binaire depuis la racine des sources :**
```powershell
# WRONG: bypasses tests/vitest.config.js, every "@/..." import fails
.\tests\node_modules\.bin\vitest.cmd run tests/unit/model-test-routing.test.js
#   -> Error: Cannot find package '@/shared/constants/config' imported from
#      src/app/api/models/test/ping.js
```

Utilisez plutôt l'invocation qui respecte la configuration :
```powershell
# RIGHT: loads tests/vitest.config.js (aliases resolve)
npm --prefix tests test -- unit/model-test-routing.test.js
```

Un `vitest.config.js` à la racine qui réexporte la configuration des tests fait aussi
fonctionner le binaire racine ; il est conservé dans l'arbre des sources pour que les deux
invocations concordent. `tests/unit/model-routing.test.js` présente un `EPERM` préexistant
et sans rapport lors du teardown de `os.tmpdir()` dans cet environnement : il échoue à
l'identique avec les deux invocations et ne relève pas d'un problème d'alias.

## Modèle de sécurité
Ce dépôt est exempt de secrets et peut être confié sans risque à des agents de programmation externes.
Les secrets ne vivent que dans la couche d'exécution locale (`%LOCALAPPDATA%\9router_WatchEdit\` et
l'installation de 9Router). WatchEdit fonctionne par défaut avec `SECRETS: LOCKED` ; les opérations
en direct sur les fournisseurs nécessitent un déverrouillage explicite (autorisation assurée par l'OS ou coffre facultatif à mot de passe maître).
Voir **SECURITY_LOCAL.md** pour le modèle complet, le générateur de partage sûr
(`python tools/build_safe_share.py`), le scanner de secrets et les outils de sauvegarde privée.
  - `data-snapshot/` - copie complète de la base de données `%APPDATA%\9router\db\data.sqlite`.

## Comment appliquer la mise à jour
Lorsque vos tâches actives dans 9router sont terminées, exécutez simplement :
```powershell
.\apply-update.ps1
```
Cela va :
1. Arrêter l'instance 9router en cours.
2. Créer une sauvegarde horodatée dans `%APPDATA%\9router_backup_<timestamp>`.
3. Installer `9router-0.5.65-extra.tgz` globalement.
4. Synchroniser toutes les connexions et mettre à jour le combo SAIFREN.
5. Redémarrer 9router avec une icône dans la zone de notification du système.

<!-- translation-model: codebuff-agent in-session translation (not the local qwen3:14b pipeline) -->
<!-- source-digest: README.md sha256:082c6cba0dbf49757cbab042ad6e9fec1162ed12091176b2cef3ed1ffe6c5c4c -->
