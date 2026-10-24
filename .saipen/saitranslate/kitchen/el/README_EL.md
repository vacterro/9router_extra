# 9Router Extra / Σαρωτής & Σουίτα Ενσωμάτωσης

Έκδοση: **v0.2.0**
Έργο: 9router_extra
Διαδρομή: `V:\___VAC\__K\__CODE\_PY\_9router_extra\`

## Επισκόπηση
Αυτό το αποθετήριο περιέχει επεκτάσεις, εργαλεία, αντίγραφα κατάστασης και επιδιορθώσεις για το 9Router:
1. **Γέφυρα παρόχου WorkBuddy AI (`wb/hy3`)**
   - Άμεση σύνδεση με τις συμπληρώσεις cloud της WorkBuddy AI (`https://www.workbuddy.ai/v2/chat/completions`).
   - Υποστηρίζει το μοντέλο συλλογισμού Hunyuan `hy3` (επιβάλλει `stream: true`, τυποποιημένα μπλοκ περιεχομένου, `reasoning_effort: high`, `reasoning_summary: auto`).
   - Αυτόματη εισαγωγή τοπικού κωδικού πρόσβασης από `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop-ai.info`.
   - Υποστήριξη ανανέωσης κωδικού πρόσβασης και παρακολούθησης ορίων χρήσης.
2. **Διόρθωση Antigravity Gemini 3.8 Flash και Δυναμική Επίλυση Επιπέδων**
   - Διορθώνει το πρόβλημα 404: η υπηρεσία Antigravity απαιτεί `gemini-3.8-flash-tiered(high|medium|low)`.
   - Προσθέτει δυναμική αντιστοίχιση μοτίβων, ώστε οποιοδήποτε `gemini-*-flash-(high|medium|low)` να αντιστοιχεί στην κλιμακωτή μορφή χωρίς σφάλμα 404.
3. **Προσθήκη του συνδυασμού SAIFREN στην αρχή**
   - Το `wb/hy3` έχει ρυθμιστεί ως 1ο μοντέλο και το `ag/gemini-3.8-flash-high` ως 2ο μοντέλο στο `SAIFREN`.
4. **Επιδιορθώσεις AgentRouter & Cline**
   - Εγγενής πάροχος AgentRouter, προώθηση του User-Agent του πελάτη, επίλυση της βασικής διεύθυνσης URL του Anthropic και διορθώσεις δρομολόγησης του Cline.
5. **Μηχανή Αντιγράφων και Μεταφοράς Κατάστασης**
   - Στιγμιότυπο όλων των 36 συνδέσεων, των 13 προσαρμοσμένων κόμβων (B.AI, Dahl, Vyce κ.λπ.) και των συνδυασμών.

## Δομή
- `apply-update.ps1` - Σενάριο ενημέρωσης με ένα κλικ για ασφαλή διακοπή, δημιουργία αντιγράφου, αναβάθμιση στην v0.5.65-extra, επαναφορά της κατάστασης και επανεκκίνηση του 9router.
- `packages/`
  - `9router-0.5.65-extra.tgz` - Πλήρως κατασκευασμένο, επιδιορθωμένο πακέτο v0.5.65, έτοιμο για καθολική εγκατάσταση.
  - `9router-0.5.59-agentrouter.tgz` - Παλαιότερο πακέτο v0.5.59.
- `patches/`
  - `9router-0.5.65-extra-unified.patch` - Ενοποιημένη επιδιόρθωση σε σύγκριση με την αρχική έκδοση v0.5.65.
  - `9router-extra-unified.patch` - Ενοποιημένη επιδιόρθωση σε σύγκριση με την v0.5.59.
- `backup/`
  - `export_state.js` & `restore_state.js` - Εργαλεία δημιουργίας και συγχρονισμού αντιγράφων κατάστασης SQLite
    (η ιδιωτική κατάσταση αποθηκεύεται πλέον στο `%LOCALAPPDATA%\9router_WatchEdit\backups\private\`, ποτέ σε αυτό το αποθετήριο).

## Εκτέλεση των δοκιμών του πηγαίου κώδικα 9Router (παγίδα στην κλήση του vitest, T-21)
Ο πηγαίος κώδικας του πίνακα Next.js βρίσκεται στο `%APPDATA%\9router\source`. Τα ψευδώνυμα διαδρομών
(`@/` → `src`, `open-sse` → `open-sse`) ορίζονται ΜΟΝΟ στο
`tests/vitest.config.js`.

**Μην καλείτε το εκτελέσιμο αρχείο από τη ρίζα του πηγαίου κώδικα:**
```powershell
# WRONG: bypasses tests/vitest.config.js, every "@/..." import fails
.\tests\node_modules\.bin\vitest.cmd run tests/unit/model-test-routing.test.js
#   -> Error: Cannot find package '@/shared/constants/config' imported from
#      src/app/api/models/test/ping.js
```

Χρησιμοποιήστε αντί αυτού την κλήση που λαμβάνει υπόψη τη διαμόρφωση:
```powershell
# RIGHT: loads tests/vitest.config.js (aliases resolve)
npm --prefix tests test -- unit/model-test-routing.test.js
```

Ένα `vitest.config.js` σε επίπεδο ρίζας που επανεξάγει τη διαμόρφωση των δοκιμών κάνει επίσης λειτουργικό το
εκτελέσιμο αρχείο της ρίζας· διατηρείται στο δέντρο του πηγαίου κώδικα ώστε να συμφωνούν και οι δύο κλήσεις.
`tests/unit/model-routing.test.js` έχει ανεξάρτητο προϋπάρχον `EPERM` κατά τον
καθαρισμό στο `os.tmpdir()` σε αυτό το περιβάλλον — αποτυγχάνει με τον ίδιο τρόπο και στις
δύο κλήσεις και δεν αποτελεί πρόβλημα ψευδωνυμων.

## Μοντέλο ασφαλείας
Αυτό το αποθετήριο δεν περιέχει μυστικά και μπορεί να παραδοθεί με ασφάλεια σε εξωτερικούς προγραμματιστές.
Τα μυστικά υπάρχουν μόνο στο τοπικό επίπεδο εκτέλεσης (`%LOCALAPPDATA%\9router_WatchEdit\` και στην
εγκατάσταση 9Router). Το WatchEdit εκτελείται με `SECRETS: LOCKED` από προεπιλογή· οι λειτουργίες παρόχων
σε πραγματικό χρόνο απαιτούν ρητή απενεργοποίηση προστασίας (άδεια που υποστηρίζεται από το λειτουργικό
σύστημα ή προαιρετικό θησαυροφυλάκιο προστατευμένο με κύριο κωδικό πρόσβασης).
Δείτε **SECURITY_LOCAL.md** για την πλήρη περιγραφή, τον κατασκευαστή ασφαλών αρχείων κοινής χρήσης
(`python tools/build_safe_share.py`), τον ανιχνευτή μυστικών και τα εργαλεία ιδιωτικών αντιγράφων.
  - `data-snapshot/` - Πλήρης αντίγραφο της βάσης δεδομένων `%APPDATA%\9router\db\data.sqlite`.

## Πώς να εφαρμόσετε την ενημέρωση
Όταν ολοκληρωθούν οι ενεργές εργασίες σας στο 9router, απλώς εκτελέστε:
```powershell
.\apply-update.ps1
```
Αυτό θα:
1. Διακόψει την τρέχουσα διεργασία του 9router.
2. Δημιουργήσει αντίγραφο ασφαλείας με χρονική σήμανση στο `%APPDATA%\9router_backup_<timestamp>`.
3. Εγκαταστήσει `9router-0.5.65-extra.tgz` καθολικά.
4. Συγχρονίσει όλες τις συνδέσεις και ενημερώσει τον συνδυασμό SAIFREN.
5. Επανεκκινήσει το 9router σε λειτουργία περιδράφιου.

<!-- translation-model: qwen3:14b contract:structured-markdown-v2 -->
<!-- source-digest: README.md sha256:082c6cba0dbf49757cbab042ad6e9fec1162ed12091176b2cef3ed1ffe6c5c4c -->
