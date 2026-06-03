const { app, BrowserWindow, globalShortcut, ipcMain, screen, desktopCapturer } = require('electron');
const path = require('path');

let modelWindow = null;
let chatWindow  = null;
let chatVisible = false;

function createModelWindow() {
  const { width, height } = screen.getPrimaryDisplay().workAreaSize;

  modelWindow = new BrowserWindow({
    width: 400,
    height: 700,
    x: width - 420,
    y: height - 720,
    transparent: true,
    frame: false,
    alwaysOnTop: true,
    skipTaskbar: true,
    hasShadow: false,
    resizable: false,
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
    width: 420,
    height: 600,
    x: width - 440,
    y: height - 740,
    frame: false,
    transparent: false,
    alwaysOnTop: true,
    skipTaskbar: true,
    show: false,
    resizable: false,
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

function toggleChat() {
  if (!chatWindow) return;
  if (chatVisible) {
    chatWindow.hide();
    chatVisible = false;
  } else {
    chatWindow.show();
    chatWindow.focus();
    chatVisible = true;
  }
}

let dragMode    = false;
let dragOffsetX = 0;
let dragOffsetY = 0;

app.whenReady().then(() => {
  createModelWindow();
  createChatWindow();

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
      modelWindow.webContents.send('mouse-move', {
        x: pos.x - bounds.x,
        y: pos.y - bounds.y,
      });
    }
  }, 16);

  // Hotkey: Cmd+Shift+P to toggle chat
  globalShortcut.register('CommandOrControl+Shift+P', toggleChat);

  // Hotkey: Cmd+Shift+O to toggle model visibility
  let modelVisible = true;
  globalShortcut.register('CommandOrControl+Shift+O', () => {
    if (!modelWindow) return;
    modelVisible = !modelVisible;
    if (modelVisible) modelWindow.show();
    else modelWindow.hide();
  });

  // Hotkey: Cmd+Shift+M to toggle mic
  globalShortcut.register('CommandOrControl+Shift+M', () => {
    if (chatWindow) chatWindow.webContents.send('toggle-mic');
    if (modelWindow) modelWindow.webContents.send('toggle-mic');
  });
});

app.on('will-quit', () => {
  globalShortcut.unregisterAll();
});

// Keep app running when all windows closed (Mac behavior)
app.on('window-all-closed', (e) => {
  e.preventDefault();
});

ipcMain.on('close-chat', () => {
  if (chatWindow) { chatWindow.hide(); chatVisible = false; }
});

ipcMain.on('set-ignore-mouse', (_, ignore) => {
  if (modelWindow) modelWindow.setIgnoreMouseEvents(ignore, { forward: true });
});

ipcMain.on('start-drag', (_, { offsetX, offsetY }) => {
  dragMode    = true;
  dragOffsetX = offsetX;
  dragOffsetY = offsetY;
});

ipcMain.on('end-drag', () => { dragMode = false; });

ipcMain.handle('capture-screen', async () => {
  const sources = await desktopCapturer.getSources({
    types: ['screen'],
    thumbnailSize: { width: 1920, height: 1080 },
  });
  if (!sources.length) {
    console.warn('[capture-screen] No sources found — grant Screen Recording permission in System Settings → Privacy & Security.');
    return null;
  }
  const dataUrl = sources[0].thumbnail.toDataURL();
  console.log(`[capture-screen] Captured ${Math.round(dataUrl.length / 1024)}KB`);
  return dataUrl;
});
