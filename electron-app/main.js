const {
  app, BrowserWindow, globalShortcut, ipcMain,
  screen, desktopCapturer, safeStorage,
} = require('electron');
const path = require('path');
const fs   = require('fs');
const { SUPABASE_URL, SUPABASE_ANON_KEY, API_BASE } = require('./config');

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
// Windows
// ---------------------------------------------------------------------------

let loginWindow    = null;
let modelWindow    = null;
let chatWindow     = null;
let settingsWindow = null;
let chatVisible    = false;
let dragMode       = false;
let dragOffsetX    = 0;
let dragOffsetY    = 0;

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

function createModelWindow() {
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
  modelWindow.on('closed', () => { modelWindow = null; });
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
app.on('window-all-closed', (e) => { e.preventDefault(); });

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
  if (!session) throw new Error('Not logged in.');
  const res = await fetch(`${API_BASE}/chat`, {
    method:  'POST',
    headers: {
      'Content-Type':  'application/json',
      'Authorization': `Bearer ${session.access_token}`,
    },
    body: JSON.stringify({ username, message, screenshot }),
  });
  if (!res.ok) throw new Error(`Backend ${res.status}: ${await res.text()}`);
  return res.json();
});

// ---------------------------------------------------------------------------
// IPC — settings
// ---------------------------------------------------------------------------

ipcMain.handle('settings:load', async () => {
  if (!session) throw new Error('Not logged in.');
  const [personas, voices, avatars, profileArr] = await Promise.all([
    sbGet('/rest/v1/personas?select=id,name,is_default&order=name'),
    sbGet('/rest/v1/voices?select=id,name,description,is_default&order=name'),
    sbGet('/rest/v1/avatars?select=id,name,file_path,is_default&order=name'),
    sbGet(
      `/rest/v1/profiles?id=eq.${session.user_id}&select=persona_id,voice_id,avatar_id`,
      session.access_token,
    ),
  ]);
  const profile = profileArr[0] || {};
  return { personas, voices, avatars, profile };
});

ipcMain.handle('settings:save', async (_, { personaId, voiceId, avatarId }) => {
  if (!session) throw new Error('Not logged in.');
  await sbUpsert(
    '/rest/v1/profiles',
    { id: session.user_id, persona_id: personaId, voice_id: voiceId, avatar_id: avatarId },
    session.access_token,
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

ipcMain.handle('capture-screen', async () => {
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
