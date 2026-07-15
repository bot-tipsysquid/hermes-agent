import assert from 'node:assert/strict'

import { test } from 'vitest'

import { hasOzonePlatformArg, resolveOzonePlatformSwitch } from './ozone-platform'

test('detects both ozone platform argument forms', () => {
  assert.equal(hasOzonePlatformArg(['Hermes']), false)
  assert.equal(hasOzonePlatformArg(['Hermes', '--ozone-platform', 'x11']), true)
  assert.equal(hasOzonePlatformArg(['Hermes', '--ozone-platform=wayland']), true)
})

test('linux wayland with XWayland available defaults to x11', () => {
  assert.equal(
    resolveOzonePlatformSwitch({
      platform: 'linux',
      env: { XDG_SESSION_TYPE: 'wayland', DISPLAY: ':0' },
      argv: ['Hermes']
    }),
    'x11'
  )
})

test('explicit desktop ozone platform override wins', () => {
  assert.equal(
    resolveOzonePlatformSwitch({
      platform: 'linux',
      env: { HERMES_DESKTOP_OZONE_PLATFORM: 'wayland', XDG_SESSION_TYPE: 'wayland', DISPLAY: ':0' },
      argv: ['Hermes']
    }),
    'wayland'
  )
})

test('auto override leaves Chromium default untouched', () => {
  assert.equal(
    resolveOzonePlatformSwitch({
      platform: 'linux',
      env: { HERMES_DESKTOP_OZONE_PLATFORM: 'auto', XDG_SESSION_TYPE: 'wayland', DISPLAY: ':0' },
      argv: ['Hermes']
    }),
    null
  )
})

test('does not override non-linux, x11 sessions, missing XWayland, or user args', () => {
  assert.equal(
    resolveOzonePlatformSwitch({ platform: 'darwin', env: { XDG_SESSION_TYPE: 'wayland', DISPLAY: ':0' }, argv: ['Hermes'] }),
    null
  )
  assert.equal(
    resolveOzonePlatformSwitch({ platform: 'linux', env: { XDG_SESSION_TYPE: 'x11', DISPLAY: ':0' }, argv: ['Hermes'] }),
    null
  )
  assert.equal(
    resolveOzonePlatformSwitch({ platform: 'linux', env: { XDG_SESSION_TYPE: 'wayland' }, argv: ['Hermes'] }),
    null
  )
  assert.equal(
    resolveOzonePlatformSwitch({
      platform: 'linux',
      env: { XDG_SESSION_TYPE: 'wayland', DISPLAY: ':0' },
      argv: ['Hermes', '--ozone-platform=wayland']
    }),
    null
  )
})
