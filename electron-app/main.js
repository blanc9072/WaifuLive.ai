const {
  app, BrowserWindow, globalShortcut, ipcMain,
  screen, desktopCapturer, safeStorage,
  Tray, Menu, nativeImage, powerMonitor,
} = require('electron');
const path = require('path');
const fs   = require('fs');
const zlib = require('zlib');
const { SUPABASE_URL, SUPABASE_ANON_KEY, API_BASE } = require('./config');


// ---------------------------------------------------------------------------
// PNG icon generation — pure Node.js, no external deps
// ---------------------------------------------------------------------------

const _crcTable = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = (c & 1) ? (0xEDB88320 ^ (c >>> 1)) : (c >>> 1);
    t[n] = c;
  }
  return t;
})();

function _crc32(buf) {
  let crc = 0xFFFFFFFF;
  for (let i = 0; i < buf.length; i++) crc = _crcTable[(crc ^ buf[i]) & 0xFF] ^ (crc >>> 8);
  return (crc ^ 0xFFFFFFFF) >>> 0;
}

function _pngChunk(type, data) {
  const typeBytes = Buffer.from(type, 'ascii');
  const lenBuf    = Buffer.alloc(4); lenBuf.writeUInt32BE(data.length);
  const crcBuf    = Buffer.alloc(4); crcBuf.writeUInt32BE(_crc32(Buffer.concat([typeBytes, data])));
  return Buffer.concat([lenBuf, typeBytes, data, crcBuf]);
}

// Returns a Buffer containing a valid RGBA PNG of a filled circle on a
// transparent background.  size should be 22 for macOS menu-bar icons.
function _circlePng(size, r, g, b) {
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(size, 0); ihdr.writeUInt32BE(size, 4);
  ihdr[8] = 8; ihdr[9] = 6; // 8-bit RGBA

  const cx = size / 2, cy = size / 2, rad = size / 2 - 1;
  const rows = [];
  for (let y = 0; y < size; y++) {
    const row = Buffer.alloc(1 + size * 4); // filter byte + RGBA
    for (let x = 0; x < size; x++) {
      const dx = x + 0.5 - cx, dy = y + 0.5 - cy;
      const hit = dx * dx + dy * dy <= rad * rad;
      const o = 1 + x * 4;
      row[o] = hit ? r : 0; row[o + 1] = hit ? g : 0;
      row[o + 2] = hit ? b : 0; row[o + 3] = hit ? 255 : 0;
    }
    rows.push(row);
  }

  const PNG_SIG = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]);
  return Buffer.concat([
    PNG_SIG,
    _pngChunk('IHDR', ihdr),
    _pngChunk('IDAT', zlib.deflateSync(Buffer.concat(rows))),
    _pngChunk('IEND', Buffer.alloc(0)),
  ]);
}

// ---------------------------------------------------------------------------
// Session — encrypted on disk, never exposed to any renderer
// ---------------------------------------------------------------------------

const SESSION_PATH = path.join(app.getPath('userData'), 'session.bin');

// { access_token, refresh_token, user_id, email }
let session = null;

function loadStoredSession() {
  try {
    if (!safeStorage.isEncryptionAvailable()) return;
    const buf  = fs.readFileSync(SESSION_PATH);
    session    = JSON.parse(safeStorage.decryptString(buf));
  } catch (_) {
    session = null;
  }
}

function persistSession() {
  if (!safeStorage.isEncryptionAvailable()) return;
  const enc = safeStorage.encryptString(JSON.stringify(session));
  fs.writeFileSync(SESSION_PATH, enc);
}

function clearStoredSession() {
  session = null;
  try { fs.unlinkSync(SESSION_PATH); } catch (_) {}
}

// ---------------------------------------------------------------------------
// Supabase helpers — all run in main process; token never reaches renderer
// ---------------------------------------------------------------------------

const SB_HEADERS = {
  apikey:           SUPABASE_ANON_KEY,
  'Content-Type':   'application/json',
};

async function supabaseAuthSignUp(email, password) {
  const res = await fetch(`${SUPABASE_URL}/auth/v1/signup`, {
    method:  'POST',
    headers: SB_HEADERS,
    body:    JSON.stringify({ email, password }),
  });
  const body = await res.json();
  if (!res.ok) throw new Error(body.error_description || body.msg || `Signup failed (${res.status})`);
  if (!body.access_token) {
    // Email confirmation is enabled — account created but can't log in yet.
    throw new Error('Account created! Check your email to confirm before signing in.');
  }
  return body;
}

async function supabaseAuthSignIn(email, password) {
  const res = await fetch(`${SUPABASE_URL}/auth/v1/token?grant_type=password`, {
    method:  'POST',
    headers: SB_HEADERS,
    body:    JSON.stringify({ email, password }),
  });
  const body = await res.json();
  if (!res.ok) throw new Error(body.error_description || body.msg || `Auth failed (${res.status})`);
  return body; // { access_token, refresh_token, user: { id, email } }
}

async function sbGet(endpoint, token = null) {
  const headers = { ...SB_HEADERS };
  if (token) headers['Authorization'] = `Bearer ${token}`;
  const res = await fetch(`${SUPABASE_URL}${endpoint}`, { headers });
  if (!res.ok) throw new Error(`Supabase ${res.status}: ${await res.text()}`);
  return res.json();
}

async function sbUpsert(endpoint, body, token) {
  const res = await fetch(`${SUPABASE_URL}${endpoint}`, {
    method:  'POST',
    headers: {
      ...SB_HEADERS,
      'Authorization': `Bearer ${token}`,
      'Prefer':        'resolution=merge-duplicates,return=representation',
    },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`Supabase ${res.status}: ${await res.text()}`);
  return res.json();
}

// ---------------------------------------------------------------------------
// Token refresh — keeps access_token fresh; refresh_token survives on disk
// ---------------------------------------------------------------------------

function _tokenExpiry(token) {
  try {
    // JWT payload is base64url — normalise to standard base64 before decoding.
    const b64 = token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/');
    const payload = JSON.parse(Buffer.from(b64, 'base64').toString('utf8'));
    return payload.exp || 0;
  } catch (_) {
    return 0;
  }
}

function _forceLogin() {
  clearStoredSession();
  for (const w of [modelWindow, chatWindow, settingsWindow]) {
    try { if (w && !w.isDestroyed()) w.close(); } catch (_) {}
  }
  modelWindow = chatWindow = settingsWindow = null;
  chatVisible = false;
  createLoginWindow();
}

// Returns a guaranteed-fresh access token, refreshing via the refresh_token
// when the current one is expired or expiring within 60 s.  Throws (after
// routing the user back to login) if the refresh token is also dead.
async function getValidToken() {
  if (!session) throw new Error('Not logged in.');
  const now = Math.floor(Date.now() / 1000);
  if (_tokenExpiry(session.access_token) - now > 60) {
    return session.access_token;
  }
  try {
    const res = await fetch(
      `${SUPABASE_URL}/auth/v1/token?grant_type=refresh_token`,
      {
        method:  'POST',
        headers: SB_HEADERS,
        body:    JSON.stringify({ refresh_token: session.refresh_token }),
      },
    );
    const body = await res.json();
    if (!res.ok) throw new Error(body.error_description || body.msg || `Refresh ${res.status}`);
    session.access_token  = body.access_token;
    session.refresh_token = body.refresh_token;
    persistSession();
    return session.access_token;
  } catch (err) {
    console.warn('[auth] Token refresh failed — forcing re-login:', err.message);
    _forceLogin();
    throw new Error('Session expired. Please log in again.');
  }
}

// ---------------------------------------------------------------------------
// Windows
// ---------------------------------------------------------------------------

let loginWindow       = null;
let modelWindow       = null;
let chatWindow        = null;
let settingsWindow    = null;
let chatVisible       = false;
let dragMode          = false;
let dragOffsetX       = 0;
let dragOffsetY       = 0;
let tray              = null;
let screenWatchEnabled = false;
let isQuitting         = false;   // set true by the tray "Quit" item so window-all-closed lets us exit
let agencyEnabled      = true;
let lastNudgeAt        = 0;
let lastUserSpokeAt    = Date.now();
const NUDGE_CHECK_MS     = 60_000;
const NUDGE_MIN_GAP_MS   = 45 * 60_000;
const NUDGE_SESSION_MS   = 90 * 60_000;
const NUDGE_QUIET_IDLE_S = 300;

// Fully exit the app. window-all-closed normally keeps us alive in the tray, so
// we flag the intentional quit first, then app.quit(). The timeout force-exits
// if any window/handler stalls the graceful quit, guaranteeing a real shutdown.
function quitApp() {
  isQuitting = true;
  app.quit();
  setTimeout(() => app.exit(0), 1000);
}

// Lazily built on first tray creation (requires app to be ready for nativeImage).
let _trayIconOn  = null;
let _trayIconOff = null;

function _trayIcons() {
  if (!_trayIconOn) {
    _trayIconOn  = nativeImage.createFromBuffer(_circlePng(22, 0x84, 0xB0, 0x67)); // green
    _trayIconOff = nativeImage.createFromBuffer(_circlePng(22, 0x9C, 0x97, 0x92)); // grey
  }
  return { on: _trayIconOn, off: _trayIconOff };
}

function setTrayState(enabled) {
  if (!tray) return;
  screenWatchEnabled = enabled;
  // Keep the chat overlay in sync with the tray
  if (chatWindow && !chatWindow.isDestroyed()) {
    chatWindow.webContents.send('screenwatch-changed', enabled);
  }
  const icons = _trayIcons();
  tray.setImage(enabled ? icons.on : icons.off);
  tray.setToolTip(enabled ? 'Pistachio — Screen watch: ON' : 'Pistachio — Screen watch: OFF');
  // Store the menu without calling setContextMenu — on macOS setContextMenu
  // intercepts left-click too, so we pop it up manually on right-click only.
  tray._contextMenu = Menu.buildFromTemplate([
    { label: enabled ? '● Screen watch ON' : '○ Screen watch OFF', enabled: false },
    { type: 'separator' },
    {
      label: enabled ? 'Turn OFF screen watch' : 'Turn ON screen watch',
      click: () => setTrayState(!screenWatchEnabled),
    },
    { type: 'separator' },
    { label: agencyEnabled ? '● Check-ins ON' : '○ Check-ins OFF', enabled: false },
    { label: agencyEnabled ? 'Turn OFF check-ins' : 'Turn ON check-ins',
      click: () => { agencyEnabled = !agencyEnabled; setTrayState(screenWatchEnabled); } },
    { type: 'separator' },
    { label: 'Quit Pistachio (fully exit)', click: () => quitApp() },
  ]);
}

function setupTray() {
  if (tray) return;
  const icons = _trayIcons();
  tray = new Tray(icons.off);
  tray.on('click',       () => setTrayState(!screenWatchEnabled));
  tray.on('right-click', () => tray.popUpContextMenu(tray._contextMenu));
  setTrayState(false);
}

function teardownTray() {
  if (!tray) return;
  screenWatchEnabled = false;
  tray.destroy();
  tray = null;
}

function createLoginWindow() {
  loginWindow = new BrowserWindow({
    width: 360, height: 440,
    resizable: false,
    frame: false,
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      preload: path.join(__dirname, 'preload.js'),
    },
  });
  loginWindow.loadFile('src/login.html');
  loginWindow.on('closed', () => { loginWindow = null; });
}

async function createModelWindow() {
  const { width, height } = screen.getPrimaryDisplay().workAreaSize;
  modelWindow = new BrowserWindow({
    width: 400, height: 700,
    x: width - 420, y: height - 720,
    transparent: true, frame: false,
    alwaysOnTop: true, skipTaskbar: true,
    hasShadow: false, resizable: false,
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      preload: path.join(__dirname, 'preload.js'),
    },
  });
  modelWindow.setAlwaysOnTop(true, 'screen-saver');
  modelWindow.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  modelWindow.loadFile('src/model.html');
  modelWindow.setIgnoreMouseEvents(true, { forward: true });
  const win = modelWindow;
  win.on('closed', () => { if (modelWindow === win) modelWindow = null; });
}

function createChatWindow() {
  const { width, height } = screen.getPrimaryDisplay().workAreaSize;
  chatWindow = new BrowserWindow({
    width: 420, height: 600,
    x: width - 440, y: height - 740,
    frame: false, transparent: false,
    alwaysOnTop: true, skipTaskbar: true,
    show: false, resizable: false,
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      preload: path.join(__dirname, 'preload.js'),
    },
  });
  chatWindow.loadFile('src/chat.html');
  chatWindow.on('closed', () => { chatWindow = null; });
  chatWindow.setAlwaysOnTop(true, 'screen-saver');
  chatWindow.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
}

function createSettingsWindow() {
  if (settingsWindow) { settingsWindow.focus(); return; }
  settingsWindow = new BrowserWindow({
    width: 480, height: 540,
    resizable: false, frame: false,
    alwaysOnTop: true,
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      preload: path.join(__dirname, 'preload.js'),
    },
  });
  settingsWindow.loadFile('src/settings.html');
  settingsWindow.on('closed', () => { settingsWindow = null; });
}

function toggleChat() {
  if (!chatWindow) return;
  if (chatVisible) { chatWindow.hide(); chatVisible = false; }
  else             { chatWindow.show(); chatWindow.focus(); chatVisible = true; }
}

function launchApp() {
  createModelWindow();
  createChatWindow();

  // Mouse polling for model window hit-testing / drag
  setInterval(() => {
    if (!modelWindow || modelWindow.isDestroyed()) return;
    const pos    = screen.getCursorScreenPoint();
    const bounds = modelWindow.getBounds();
    if (dragMode) {
      const { x: wx, y: wy, width: ww, height: wh } =
        screen.getDisplayNearestPoint(pos).workArea;
      modelWindow.setPosition(
        Math.round(Math.min(Math.max(pos.x - dragOffsetX, wx), wx + ww - bounds.width)),
        Math.round(Math.min(Math.max(pos.y - dragOffsetY, wy), wy + wh - bounds.height)),
      );
    } else {
      modelWindow.webContents.send('mouse-move', { x: pos.x - bounds.x, y: pos.y - bounds.y });
    }
  }, 16);

  setInterval(maybeNudge, NUDGE_CHECK_MS);

  globalShortcut.register('CommandOrControl+Shift+P', toggleChat);

  let modelVisible = true;
  globalShortcut.register('CommandOrControl+Shift+O', () => {
    if (!modelWindow) return;
    modelVisible = !modelVisible;
    if (modelVisible) modelWindow.show(); else modelWindow.hide();
  });

  globalShortcut.register('CommandOrControl+Shift+M', () => {
    if (chatWindow)  chatWindow.webContents.send('toggle-mic');
    if (modelWindow) modelWindow.webContents.send('toggle-mic');
  });

  setupTray();
}

// ---------------------------------------------------------------------------
// App lifecycle
// ---------------------------------------------------------------------------

app.whenReady().then(() => {
  loadStoredSession();
  if (session) launchApp();
  else         createLoginWindow();
});

app.on('will-quit', () => { globalShortcut.unregisterAll(); });
app.on('window-all-closed', (e) => { if (!isQuitting) e.preventDefault(); });

// ---------------------------------------------------------------------------
// IPC — auth
// ---------------------------------------------------------------------------

function _applySession(data) {
  session = {
    access_token:  data.access_token,
    refresh_token: data.refresh_token,
    user_id:       data.user.id,
    email:         data.user.email,
  };
  persistSession();
  if (loginWindow) { loginWindow.close(); }
  launchApp();
  return { email: session.email };
}

ipcMain.handle('auth:login', async (_, { email, password }) => {
  const data = await supabaseAuthSignIn(email, password);
  return _applySession(data);
});

ipcMain.handle('auth:signup', async (_, { email, password }) => {
  const data = await supabaseAuthSignUp(email, password);
  return _applySession(data);
});

ipcMain.handle('auth:logout', () => {
  clearStoredSession();
  teardownTray();
  if (modelWindow)    { modelWindow.close(); }
  if (chatWindow)     { chatWindow.close(); }
  if (settingsWindow) { settingsWindow.close(); }
  chatVisible = false;
  createLoginWindow();
});

ipcMain.handle('auth:getUser', () => {
  if (!session) return null;
  return { email: session.email, user_id: session.user_id };
});

// ---------------------------------------------------------------------------
// IPC — API proxy (injects token; renderer never sees it)
// ---------------------------------------------------------------------------

ipcMain.handle('api:chat', async (_, { username, message, screenshot }) => {
  lastUserSpokeAt = Date.now();
  const token = await getValidToken();
  console.log('[chat-auth]', 'token_present=', !!token, 'token_len=', token ? token.length : 0, 'token_prefix=', token ? token.slice(0, 12) : 'none', 'session_expires_at=', session?.expires_at, 'expires_in_sec=', session?.expires_at ? (session.expires_at - Math.floor(Date.now() / 1000)) : 'unknown', 'now=', new Date().toISOString());
  const res = await fetch(`${API_BASE}/chat`, {
    method:  'POST',
    headers: {
      'Content-Type':  'application/json',
      'Authorization': `Bearer ${token}`,
    },
    body: JSON.stringify({ username, message, screenshot }),
  });
  if (!res.ok) throw new Error(`Backend ${res.status}: ${await res.text()}`);
  return res.json();
});

async function ipcNudgeCall(payload) {
  const token = await getValidToken();
  console.log('[nudge-auth]', 'token_present=', !!token, 'token_len=', token ? token.length : 0, 'token_prefix=', token ? token.slice(0, 12) : 'none', 'session_expires_at=', session?.expires_at, 'expires_in_sec=', session?.expires_at ? (session.expires_at - Math.floor(Date.now() / 1000)) : 'unknown', 'now=', new Date().toISOString());
  const res = await fetch(`${API_BASE}/nudge`, {
    method:  'POST',
    headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
    body:    JSON.stringify(payload),
    signal:  AbortSignal.timeout(40_000),
  });
  if (res.status === 429) return null;
  if (!res.ok) throw new Error(`Nudge ${res.status}: ${await res.text()}`);
  return res.json();
}

ipcMain.handle('api:nudge', async (_, payload) => {
  return ipcNudgeCall(payload);
});

async function maybeNudge() {
  if (!agencyEnabled || !session || !chatWindow || chatWindow.isDestroyed()) return;
  const now = Date.now();
  if (now - lastNudgeAt     < NUDGE_MIN_GAP_MS) return;
  if (now - lastUserSpokeAt < NUDGE_SESSION_MS) return;

  let idle = 0;
  try { idle = powerMonitor.getSystemIdleTime(); } catch (e) { return; }
  if (idle > NUDGE_QUIET_IDLE_S) return;

  let screenshot = null;
  if (screenWatchEnabled) {
    try {
      const sources = await desktopCapturer.getSources({
        types: ['screen'], thumbnailSize: { width: 1920, height: 1080 },
      });
      if (sources.length) screenshot = sources[0].thumbnail.toDataURL().split(',')[1];
    } catch (e) { /* no screenshot, carry on */ }
  }

  const mins = Math.round((now - lastUserSpokeAt) / 60000);
  const context  = `Still at their computer. Last spoke with you about ${mins} minutes ago.`;
  const username = (session.email || 'user').split('@')[0];

  try {
    const data = await ipcNudgeCall({ username, context, screenshot });
    if (!data || !data.message) return;
    lastNudgeAt = Date.now();
    if (modelWindow && !modelWindow.isDestroyed()) {
      modelWindow.webContents.send('nudge', data);
    }
    if (chatWindow && !chatWindow.isDestroyed()) {
      chatWindow.webContents.send('nudge', data);
    }
  } catch (e) {
    lastNudgeAt = Date.now();
    console.warn('[nudge] failed (non-fatal):', e.message);
  }
}

ipcMain.handle('api:transcribe', async (_, audioB64) => {
  const token = await getValidToken();
  // 60 s timeout: first call downloads the Whisper model (~154 MB).
  const res = await fetch(`${API_BASE}/transcribe`, {
    method:  'POST',
    headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
    body:    JSON.stringify({ audio_b64: audioB64 }),
    signal:  AbortSignal.timeout(60_000),
  });
  if (!res.ok) throw new Error(`Transcribe ${res.status}: ${await res.text()}`);
  return res.json();
});

ipcMain.handle('avatar:config', async () => {
  try {
    const token = await getValidToken();
    const res = await fetch(`${API_BASE}/profile`, {
      headers: { 'Authorization': `Bearer ${token}` },
      signal: AbortSignal.timeout(3000),
    });
    if (!res.ok) return { file_path: null, expression_map: null };
    const body = await res.json();
    return { file_path: body.avatar_file_path || null, expression_map: body.expression_map || null };
  } catch (e) {
    console.warn('[avatar] config fetch failed, using defaults:', e.message);
    return { file_path: null, expression_map: null };
  }
});

// ---------------------------------------------------------------------------
// IPC — settings
// ---------------------------------------------------------------------------

ipcMain.handle('settings:load', async () => {
  const token = await getValidToken();
  const [personas, voices, avatars, profileArr] = await Promise.all([
    sbGet('/rest/v1/personas?select=id,name,is_default&order=name', token),
    sbGet('/rest/v1/voices?select=id,name,description,is_default&order=name', token),
    sbGet('/rest/v1/avatars?select=id,name,file_path,is_default&order=name', token),
    sbGet(
      `/rest/v1/profiles?id=eq.${session.user_id}&select=persona_id,voice_id,avatar_id`,
      token,
    ),
  ]);
  const profile = profileArr[0] || {};
  return { personas, voices, avatars, profile };
});

ipcMain.handle('settings:save', async (_, { personaId, voiceId, avatarId }) => {
  const token = await getValidToken();
  await sbUpsert(
    '/rest/v1/profiles',
    { id: session.user_id, persona_id: personaId, voice_id: voiceId, avatar_id: avatarId },
    token,
  );
  return { ok: true };
});

// ---------------------------------------------------------------------------
// IPC — windows
// ---------------------------------------------------------------------------

ipcMain.on('close-chat', () => {
  if (chatWindow) { chatWindow.hide(); chatVisible = false; }
});

ipcMain.on('open-settings', () => createSettingsWindow());

ipcMain.on('close-settings', () => {
  if (settingsWindow) settingsWindow.close();
});

ipcMain.on('set-ignore-mouse', (_, ignore) => {
  if (modelWindow) modelWindow.setIgnoreMouseEvents(ignore, { forward: true });
});

ipcMain.on('start-drag', (_, { offsetX, offsetY }) => {
  dragMode = true; dragOffsetX = offsetX; dragOffsetY = offsetY;
});

ipcMain.on('end-drag', () => { dragMode = false; });

ipcMain.handle('screenwatch:get', () => screenWatchEnabled);

ipcMain.handle('screenwatch:set', (_, enabled) => {
  setTrayState(!!enabled);
  return screenWatchEnabled;
});

ipcMain.handle('capture-screen', async () => {
  if (!screenWatchEnabled) return null;
  const sources = await desktopCapturer.getSources({
    types: ['screen'],
    thumbnailSize: { width: 1920, height: 1080 },
  });
  if (!sources.length) {
    console.warn('[capture-screen] No sources — grant Screen Recording in System Settings.');
    return null;
  }
  const dataUrl = sources[0].thumbnail.toDataURL();
  console.log(`[capture-screen] Captured ${Math.round(dataUrl.length / 1024)}KB`);
  return dataUrl;
});
