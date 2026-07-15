type DeferredShowWebContents = {
  once?: (event: string, listener: () => void) => unknown
}

type DeferredShowWindow = {
  isDestroyed?: () => boolean
  isVisible?: () => boolean
  once?: (event: string, listener: () => void) => unknown
  show?: () => void
  webContents?: DeferredShowWebContents
}

export function installDeferredShow(
  win: DeferredShowWindow,
  options: {
    didFinishLoadFallback?: boolean
    label?: string
    platform?: string
    rememberLog?: (line: string) => void
  } = {}
): (reason: string) => boolean {
  const platform = options.platform || process.platform
  const label = options.label || 'window'
  const rememberLog = typeof options.rememberLog === 'function' ? options.rememberLog : null
  const didFinishLoadFallback = options.didFinishLoadFallback ?? platform !== 'darwin'

  const showOnce = (reason: string): boolean => {
    if (!win || win.isDestroyed?.()) {
      return false
    }

    if (win.isVisible?.()) {
      return false
    }

    win.show?.()
    rememberLog?.(`[window] showing ${label} via ${reason}`)
    return true
  }

  win.once?.('ready-to-show', () => showOnce('ready-to-show'))

  if (didFinishLoadFallback) {
    win.webContents?.once?.('did-finish-load', () => showOnce('did-finish-load'))
  }

  return showOnce
}
