import { existsSync } from 'node:fs'
import path from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'

import { buildDesktopBackendEnv } from './backend-env'
import { execProbe, isTimeoutError, PROBE_TIMEOUT_MS } from './backend-probes'
import { resolveInstallationLauncher } from './updater-process'

export interface SourceBackend {
  kind: 'command' | 'python'
  label: string
  command: string
  args: string[]
  env: NodeJS.ProcessEnv
  root: string
  bootstrap: false
  shell: boolean
  local: 'installed'
}

interface SourceOptions {
  isWindows?: boolean
  env?: NodeJS.ProcessEnv
  /** Set only by the user's explicit Repair install action, never by Retry. */
  repairRequested?: boolean
}

export class InstalledRuntimeUnavailableError extends Error {
  readonly code = 'installed_runtime_unavailable'
  readonly stage: 'launcher-missing' | 'probe-failed'

  constructor(root: string, stage: 'launcher-missing' | 'probe-failed', failure?: unknown) {
    const value = failure && typeof failure === 'object' ? failure as { code?: unknown; signal?: unknown; killed?: boolean } : {}

    const code = typeof value.code === 'number' || (typeof value.code === 'string' && /^[A-Z0-9_]+$/.test(value.code))
      ? `; exit=${value.code}` : ''

    const timeout = value.killed ? '; timeout' : ''
    super(`Existing Hermes installation at ${root} is unavailable (${stage}${code}${timeout}). Retry startup; this is not a first-time installation. No reinstall was started.`)
    this.name = 'InstalledRuntimeUnavailableError'
    this.stage = stage
  }
}

/** Keep the validated command. PM owns interpreter and generation selection. */
export async function resolveSourceInstallationBackend(
  root: string,
  args: string[],
  options: SourceOptions & { hermesHome?: string } = {}
): Promise<SourceBackend | null> {
  if (!existsSync(path.join(root, 'hermes_cli', 'main.py'))) {
    return null
  }

  const isWindows: boolean = options.isWindows ?? process.platform === 'win32'
  const launcher: string | null = resolveInstallationLauncher(root, isWindows, options.hermesHome)

  if (!launcher) {
    if (options.repairRequested) {return null}
    throw new InstalledRuntimeUnavailableError(root, 'launcher-missing')
  }

  const shell: boolean = isWindows && /\.(cmd|bat)$/i.test(launcher)
  const command: string = shell ? `"${launcher}"` : launcher
  const env: NodeJS.ProcessEnv = buildDesktopBackendEnv({ currentEnv: options.env ?? process.env })

  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      await execProbe(command, ['--version'], {
        cwd: root,
        env: { ...process.env, ...options.env, ...env },
        shell,
        stdio: 'ignore',
        timeout: PROBE_TIMEOUT_MS,
        windowsHide: true
      })

      break
    } catch (error) {
      // execProbe already retries timeouts. Retry one transient launch/non-zero
      // failure, but never turn an existing installation into first-run setup.
      if (attempt === 0 && !isTimeoutError(error)) {
        await delay(250)

        continue
      }

      if (options.repairRequested) {return null}
      throw new InstalledRuntimeUnavailableError(root, 'probe-failed', error)
    }
  }

  return {
    kind: 'command',
    label: `Hermes at ${root}`,
    command,
    args: [...args],
    env,
    root,
    bootstrap: false,
    shell,
    local: 'installed'
  }
}

/** Developer overrides retain their interpreter, even outside the checkout. */
export function createSourcePythonBackend(
  root: string,
  python: string | null,
  args: string[],
  options: SourceOptions = {}
): SourceBackend | null {
  if (!python) {
    return null
  }

  let command: string = python

  if ((options.isWindows ?? process.platform === 'win32') && /[\\/]pythonw\.exe$/i.test(python)) {
    // Use only the console interpreter beside the selected windowless one.
    const consolePython: string = python.replace(/pythonw\.exe$/i, 'python.exe')

    if (existsSync(consolePython)) {
      command = consolePython
    }
  }

  return {
    kind: 'python',
    label: `Hermes source at ${root}`,
    command,
    args: ['-m', 'hermes_cli.main', ...args],
    // The backend runs in the user's workspace cwd, and the selected
    // interpreter need not have this checkout installed: name it explicitly.
    // (The scrubbed inherited value could point at another checkout.)
    env: { ...buildDesktopBackendEnv({ currentEnv: options.env ?? process.env }), PYTHONPATH: root },
    root,
    bootstrap: false,
    shell: false,
    local: 'installed'
  }
}
