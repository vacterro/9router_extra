# 9Router एक्स्ट्रा / स्कैनर और इंटीग्रेशन सूट

संस्करण: **v0.2.0**
प्रोजेक्ट: 9router_extra
पथ: `V:\___VAC\__K\__CODE\_PY\_9router_extra\`

## सारांश
यह रिपॉजिटरी 9Router के लिए एक्सटेंशन, टूल, स्टेट बैकअप और पैच शामिल करती है:
1. **WorkBuddy AI (`wb/hy3`) प्रोवाइडर ब्रिज**
   - WorkBuddy AI क्लाउड कंप्लीशंस से सीधा इंटीग्रेशन (`https://www.workbuddy.ai/v2/chat/completions`)।
   - Hunyuan तर्क मॉडल `hy3` का समर्थन (`stream: true` के बल, टाइप किए गए कंटेंट ब्लॉक, `reasoning_effort: high`, `reasoning_summary: auto`)।
   - `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop-ai.info` से स्थानीय टोकन का स्वतः आयात।
   - टोकन रिफ्रेश और कोटा ट्रैकिंग समर्थित।
2. **Antigravity Gemini 3.8 Flash सुधार और डाइनैमिक टियर रिज़ॉल्यूशन**
   - अपस्ट्रीम 404 समस्या ठीक करता है: अपस्ट्रीम Antigravity को `gemini-3.8-flash-tiered(high|medium|low)` चाहिए।
   - डाइनैमिक पैटर्न मैचिंग जोड़ता है, ताकि कोई भी `gemini-*-flash-(high|medium|low)` 404 के बिना स्तरयुक्त प्रारूप में मैप हो।
3. **SAIFREN कॉम्बो प्रीपेंड**
   - `SAIFREN` में `wb/hy3` को पहले मॉडल और `ag/gemini-3.8-flash-high` को दूसरे मॉडल के रूप में कॉन्फ़िगर करता है।
4. **AgentRouter और Cline पैच**
   - नेटिव AgentRouter प्रोवाइडर, क्लाइंट User-Agent फ़ॉरवर्डिंग, Anthropic बेस URL रिज़ॉल्यूशन और Cline रूटिंग सुधारता है।
5. **स्टेट बैकअप और माइग्रेशन इंजन**
   - सभी 36 कनेक्शनों, 13 कस्टम नोड्स (`B.AI`, Dahl, Vyce, आदि) और कॉम्बो का स्नैपशॉट।

## संरचना
- `apply-update.ps1` - सुरक्षित रूप से रोकने, बैकअप लेने, v0.5.65-extra में अपग्रेड करने, स्टेट बहाल करने और 9router फिर से शुरू करने के लिए एक-क्लिक अपडेट स्क्रिप्ट।
- `packages/`
  - `9router-0.5.65-extra.tgz` - पूरी तरह बनाया गया, पैच किया गया v0.5.65 पैकेज, ग्लोबल इंस्टॉल के लिए तैयार।
  - `9router-0.5.59-agentrouter.tgz` - पुराना v0.5.59 पैकेज।
- `patches/`
  - `9router-0.5.65-extra-unified.patch` - v0.5.65 अपस्ट्रीम के लिए एकीकृत पैच।
  - `9router-extra-unified.patch` - v0.5.59 के लिए एकीकृत पैच।
- `backup/`
  - `export_state.js` और `restore_state.js` - SQLite स्टेट बैकअप और सिंक टूल
    (निजी स्टेट अब `%LOCALAPPDATA%\9router_WatchEdit\backups\private\` में जाता है, इस रिपॉजिटरी में कभी नहीं)।

## 9Router स्रोत परीक्षण चलाएँ (vitest आह्वान फंदा, T-21)
Next.js डैशबोर्ड स्रोत `%APPDATA%\9router\source` में रहता है। इसके vitest
पथ एलियस (`@/` → `src`, `open-sse` → `open-sse`) केवल
`tests/vitest.config.js` में परिभाषित हैं।

**स्रोत रूट से बाइनरी सीधे आह्वान न करें:**
```powershell
# WRONG: bypasses tests/vitest.config.js, every "@/..." import fails
.\tests\node_modules\.bin\vitest.cmd run tests/unit/model-test-routing.test.js
#   -> Error: Cannot find package '@/shared/constants/config' imported from
#      src/app/api/models/test/ping.js
```

इसके बजाय कॉन्फ़िगरेशन-जागरूक आह्वान का उपयोग करें:
```powershell
# RIGHT: loads tests/vitest.config.js (aliases resolve)
npm --prefix tests test -- unit/model-test-routing.test.js
```

रूट-स्तर की `vitest.config.js`, जो परीक्षण कॉन्फ़िगरेशन को फिर से एक्सपोर्ट करती है, से रूट बाइनरी भी काम करता है; यह स्रोत वृक्ष में रखी गई है ताकि दोनों आह्वान समान रहें।

`tests/unit/model-routing.test.js` में इस वातावरण में `os.tmpdir()` टियरडाउन पर पहले से मौजूद, असंबंधित `EPERM` है — दोनों आह्वानों में एक जैसा विफल होता है और यह एलियस समस्या नहीं है।

## सुरक्षा मॉडल
यह रिपॉजिटरी बिना सीक्रेट की है और इसे बाहरी कोडिंग एजेंटों को सुरक्षित रूप से दिया जा सकता है।
सीक्रेट केवल स्थानीय रनटाइम लेयर में रहते हैं (`%LOCALAPPDATA%\9router_WatchEdit\` और
9Router इंस्टॉल)। WatchEdit डिफ़ॉल्ट रूप से `SECRETS: LOCKED` रहता है; लाइव प्रोवाइडर संचालन के लिए स्पष्ट अनलॉक आवश्यक है (OS-समर्थित ग्रांट या वैकल्पिक मास्टर-पासवर्ड वॉल्ट)।
पूरे मॉडल, SAFE शेयर बिल्डर (`python tools/build_safe_share.py`), सीक्रेट स्कैनर और निजी-बैकअप टूलिंग के लिए **SECURITY_LOCAL.md** देखें।
  - `data-snapshot/` - `%APPDATA%\9router\db\data.sqlite` का पूरा डेटाबेस कॉपी।

## अपडेट लागू करने का तरीका
जब आपके 9router में सक्रिय कार्य पूरे हों, तो बस यह चलाएँ:
```powershell
.\apply-update.ps1
```
यह करेगा:
1. चल रहे 9router के उदाहरण को रोकेगा।
2. में एक टाइमस्टैंप वाला बैकअप बनाएगा`%APPDATA%\9router_backup_<timestamp>`.
3. इस्ताल करें`9router-0.5.65-extra.tgz`वैश्विक रूप से।
4. सभी कनेक्शन को सिंक्रनाइज़ करें और SAIFREN कॉम्बो को अपडेट करें।
5. 9router को ट्रे मोड में पुनः शुरू करें।

<!-- translation-model: qwen3:14b contract:structured-markdown-v2 -->
<!-- source-digest: README.md sha256:082c6cba0dbf49757cbab042ad6e9fec1162ed12091176b2cef3ed1ffe6c5c4c -->
