const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
  // ── Model window ──────────────────────────────────────────────────────────
  onMouseMove:    (cb)     => ipcRenderer.on('mouse-move', (_, pos) => cb(pos)),
  setIgnoreMouse: (ignore) => ipcRenderer.send('set-ignore-mouse', ignore),
  startDrag:      (offset) => ipcRenderer.send('start-drag', offset),
  endDrag:        ()       => ipcRenderer.send('end-drag'),

  // ── Chat window ───────────────────────────────────────────────────────────
  onToggleMic:    (cb) => ipcRenderer.on('toggle-mic', cb),
  closeChat:      ()   => ipcRenderer.send('close-chat'),
  captureScreen:  ()   => ipcRenderer.invoke('capture-screen'),
  openSettings:   ()   => ipcRenderer.send('open-settings'),

  // ── Settings window ───────────────────────────────────────────────────────
  closeSettings:  ()   => ipcRenderer.send('close-settings'),

  // ── Auth (credentials/token never touch the renderer) ────────────────────
  login:   (email, password) => ipcRenderer.invoke('auth:login',  { email, password }),
  signup:  (email, password) => ipcRenderer.invoke('auth:signup', { email, password }),
  logout:  ()                => ipcRenderer.invoke('auth:logout'),
  getUser: ()                => ipcRenderer.invoke('auth:getUser'),

  // ── Backend proxy (main injects the token before forwarding) ─────────────
  sendChat: (payload) => ipcRenderer.invoke('api:chat', payload),

  // ── Settings data ─────────────────────────────────────────────────────────
  loadSettings: ()       => ipcRenderer.invoke('settings:load'),
  saveSettings: (prefs)  => ipcRenderer.invoke('settings:save', prefs),
});
