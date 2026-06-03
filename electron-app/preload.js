const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
  onToggleMic:   (cb)     => ipcRenderer.on('toggle-mic', cb),
  closeChat:     ()       => ipcRenderer.send('close-chat'),
  onMouseMove:   (cb)     => ipcRenderer.on('mouse-move', (_, pos) => cb(pos)),
  setIgnoreMouse:(ignore) => ipcRenderer.send('set-ignore-mouse', ignore),
  startDrag:     (offset) => ipcRenderer.send('start-drag', offset),
  endDrag:       ()       => ipcRenderer.send('end-drag'),
  captureScreen: ()       => ipcRenderer.invoke('capture-screen'),
});
