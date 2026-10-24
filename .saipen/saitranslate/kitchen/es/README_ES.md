# 9Router Extra / Escáner y suite de integración

Versión: **v0.2.0**
Proyecto: 9router_extra
Ruta: `V:\___VAC\__K\__CODE\_PY\_9router_extra\`

## Descripción general
Este repositorio contiene extensiones, herramientas, copias de seguridad de estado y parches para 9Router:
1. **Puente de proveedor WorkBuddy AI (`wb/hy3`)**
   - Integración directa con las completions en la nube de WorkBuddy AI (`https://www.workbuddy.ai/v2/chat/completions`).
   - Admite el modelo de razonamiento Hunyuan `hy3` (fuerza `stream: true`, bloques de contenido tipados, `reasoning_effort: high`, `reasoning_summary: auto`).
   - Importación automática local del token desde `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop-ai.info`.
   - Soporte de refresco de token y seguimiento de cuota.
2. **Corrección de Antigravity Gemini 3.8 Flash y resolución dinámica de nivel**
   - Corrige el problema 404 del upstream: el upstream de Antigravity exige `gemini-3.8-flash-tiered(high|medium|low)`.
   - Añade coincidencia dinámica de patrones, de modo que cualquier `gemini-*-flash-(high|medium|low)` se asigna al formato por niveles sin 404.
3. **Añadir modelos al principio del combo SAIFREN**
   - `wb/hy3` configurado como 1.er modelo y `ag/gemini-3.8-flash-high` como 2.º modelo en `SAIFREN`.
4. **Parches de AgentRouter y Cline**
   - Proveedor nativo de AgentRouter, reenvío del User-Agent del cliente, resolución de la URL base de Anthropic y correcciones de enrutamiento de Cline.
5. **Motor de copia de seguridad y migración de estado**
   - Instantánea de las 36 conexiones, 13 nodos personalizados (B.AI, Dahl, Vyce, etc.) y combos.

## Estructura
- `apply-update.ps1` - script de actualización en 1 clic que detiene, respalda, actualiza de forma segura a v0.5.65-extra, restaura el estado y reinicia 9router.
- `packages/`
  - `9router-0.5.65-extra.tgz` - paquete v0.5.65 totalmente compilado y parcheado, listo para instalación global.
  - `9router-0.5.59-agentrouter.tgz` - paquete heredado v0.5.59.
- `patches/`
  - `9router-0.5.65-extra-unified.patch` - parche unificado contra el upstream v0.5.65.
  - `9router-extra-unified.patch` - parche unificado contra v0.5.59.
- `backup/`
  - `export_state.js` y `restore_state.js` - herramientas de copia de seguridad y sincronización del estado SQLite
    (el estado privado ahora se dirige a `%LOCALAPPDATA%\9router_WatchEdit\backups\private\`, nunca a este repositorio).

## Ejecutar las pruebas del código fuente de 9Router (trampa de invocación de vitest, T-21)
El código fuente del panel Next.js vive en `%APPDATA%\9router\source`. Sus alias de ruta
de vitest (`@/` → `src`, `open-sse` → `open-sse`) se definen SOLO en
`tests/vitest.config.js`.

**NO invoques el binario desde la raíz del código fuente:**
```powershell
# WRONG: bypasses tests/vitest.config.js, every "@/..." import fails
.\tests\node_modules\.bin\vitest.cmd run tests/unit/model-test-routing.test.js
#   -> Error: Cannot find package '@/shared/constants/config' imported from
#      src/app/api/models/test/ping.js
```

Usa en su lugar la invocación que respeta la configuración:
```powershell
# RIGHT: loads tests/vitest.config.js (aliases resolve)
npm --prefix tests test -- unit/model-test-routing.test.js
```

Un `vitest.config.js` en la raíz que reexporte la configuración de pruebas también hace
funcionar el binario de la raíz; se mantiene en el árbol de código fuente para que ambas
invocaciones coincidan. `tests/unit/model-routing.test.js` tiene un `EPERM` preexistente
y no relacionado en el teardown de `os.tmpdir()` en este entorno: falla igual bajo ambas
invocaciones y no es un problema de alias.

## Modelo de seguridad
Este repositorio está libre de secretos y es seguro entregarlo a agentes de programación externos.
Los secretos viven solo en la capa de ejecución local (`%LOCALAPPDATA%\9router_WatchEdit\` y la
instalación de 9Router). WatchEdit se ejecuta con `SECRETS: LOCKED` por defecto; las operaciones en vivo
con proveedores requieren un desbloqueo explícito (concesión respaldada por el sistema operativo o una bóveda opcional con contraseña maestra).
Consulta **SECURITY_LOCAL.md** para el modelo de seguridad completo, el generador de compartición segura
(`python tools/build_safe_share.py`), el escáner de secretos y las herramientas de copia de seguridad privada.
  - `data-snapshot/` - copia completa de la base de datos `%APPDATA%\9router\db\data.sqlite`.

## Cómo aplicar la actualización
Cuando tus tareas activas en 9router hayan terminado, simplemente ejecuta:
```powershell
.\apply-update.ps1
```
Esto hará lo siguiente:
1. Detener la instancia de 9router en ejecución.
2. Crear una copia de seguridad con marca de tiempo en `%APPDATA%\9router_backup_<timestamp>`.
3. Instalar `9router-0.5.65-extra.tgz` de forma global.
4. Sincronizar todas las conexiones y actualizar el combo SAIFREN.
5. Reiniciar 9router con un icono en el área de notificación del sistema.

<!-- translation-model: codebuff-agent in-session translation (not the local qwen3:14b pipeline) -->
<!-- source-digest: README.md sha256:082c6cba0dbf49757cbab042ad6e9fec1162ed12091176b2cef3ed1ffe6c5c4c -->
