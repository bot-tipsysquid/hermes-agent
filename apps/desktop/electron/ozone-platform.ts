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

  // The current CLI bridges desktop.ozone_platform_hint into
  // ELECTRON_OZONE_PLATFORM_HINT. Keep the legacy override as a lower-priority
  // compatibility path for existing downstream installations.
  const explicit = String(
    env.ELECTRON_OZONE_PLATFORM_HINT || env.HERMES_DESKTOP_OZONE_PLATFORM || ''
  ).trim()

  if (explicit) {
    // `auto` is the opt-out: let Chromium/Electron choose its native default.
    return explicit.toLowerCase() === 'auto' ? null : explicit
  }

  return null
}
