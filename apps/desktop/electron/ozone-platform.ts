export function hasOzonePlatformArg(argv: readonly string[] = process.argv): boolean {
  return argv.some(arg => arg === '--ozone-platform' || String(arg || '').startsWith('--ozone-platform='))
}

export function resolveOzonePlatformSwitch({
  platform = process.platform,
  env = process.env,
  argv = process.argv
}: {
  platform?: string
  env?: Record<string, string | undefined>
  argv?: readonly string[]
} = {}): string | null {
  if (platform !== 'linux') {
    return null
  }

  if (hasOzonePlatformArg(argv)) {
    return null
  }

  const explicit = String(env.HERMES_DESKTOP_OZONE_PLATFORM || '').trim()
  if (explicit) {
    // `auto` is the opt-out: let Chromium/Electron choose its native default.
    return explicit.toLowerCase() === 'auto' ? null : explicit
  }

  if (String(env.XDG_SESSION_TYPE || '').toLowerCase() !== 'wayland') {
    return null
  }

  if (!env.DISPLAY) {
    return null
  }

  // Fedora/GNOME Wayland on ARM has been observed to keep the BrowserWindow
  // alive but unmapped/invisible even after show(). XWayland is already present
  // when DISPLAY exists, and it gives Electron a normal visible toplevel.
  return 'x11'
}
