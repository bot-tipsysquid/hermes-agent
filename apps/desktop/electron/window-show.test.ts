import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'

import { test } from 'vitest'

import { installDeferredShow } from './window-show'

function fakeWindow() {
  const win = new EventEmitter() as EventEmitter & {
    destroyed: boolean
    isDestroyed: () => boolean
    isVisible: () => boolean
    show: () => void
    showCount: number
    visible: boolean
    webContents: EventEmitter
  }
  win.webContents = new EventEmitter()
  win.visible = false
  win.destroyed = false
  win.showCount = 0
  win.show = () => {
    win.showCount += 1
    win.visible = true
  }
  win.isVisible = () => win.visible
  win.isDestroyed = () => win.destroyed
  return win
}

test('non-mac windows show on did-finish-load when ready-to-show never fires', () => {
  const win = fakeWindow()
  const logs: string[] = []

  installDeferredShow(win, {
    label: 'main',
    platform: 'linux',
    rememberLog: line => logs.push(line)
  })

  win.webContents.emit('did-finish-load')

  assert.equal(win.showCount, 1)
  assert.equal(win.visible, true)
  assert.deepEqual(logs, ['[window] showing main via did-finish-load'])
})

test('mac windows keep ready-to-show-only behavior', () => {
  const win = fakeWindow()

  installDeferredShow(win, { platform: 'darwin' })

  win.webContents.emit('did-finish-load')
  assert.equal(win.showCount, 0)

  win.emit('ready-to-show')
  assert.equal(win.showCount, 1)
})

test('ready-to-show wins and did-finish-load does not show twice', () => {
  const win = fakeWindow()

  installDeferredShow(win, { platform: 'linux' })

  win.emit('ready-to-show')
  win.webContents.emit('did-finish-load')

  assert.equal(win.showCount, 1)
})

test('destroyed windows are not shown by deferred events', () => {
  const win = fakeWindow()
  win.destroyed = true

  installDeferredShow(win, { platform: 'linux' })

  win.webContents.emit('did-finish-load')
  win.emit('ready-to-show')

  assert.equal(win.showCount, 0)
})
