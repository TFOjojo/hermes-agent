import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import vm from 'node:vm'

import { transformSync } from 'esbuild'
import { afterEach, expect, test } from 'vitest'

import { createInstalledRuntimeGate } from './backend-resolution'
import * as sourceBackend from './source-backend'
import { resolveSourceInstallationBackend } from './source-backend'
import { chooseUpdaterArgs } from './windows-hermes-path'

const temps: string[] = []

function installation(exitCode: number) {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'desktop-existing-install-'))
  temps.push(temp)
  const root = path.join(temp, 'Hermes with spaces')
  const bin = path.join(root, '.hermes', 'bin')
  fs.mkdirSync(bin, { recursive: true })
  fs.mkdirSync(path.join(root, 'hermes_cli'))
  fs.writeFileSync(path.join(root, 'hermes_cli', 'main.py'), '')
  const launcher = path.join(bin, process.platform === 'win32' ? 'hermes.cmd' : 'hermes')
  fs.writeFileSync(launcher, process.platform === 'win32'
    ? `@echo off\r\nexit /b ${exitCode}\r\n`
    : `#!/bin/sh\nexit ${exitCode}\n`, { mode: 0o755 })

  return { root, launcher, home: path.join(temp, 'isolated home') }
}

afterEach(() => {
  for (const temp of temps.splice(0)) {fs.rmSync(temp, { recursive: true, force: true })}
})

test('a failing published installation never becomes the first-run installer choice', async () => {
  const fixture = installation(7)
  const gate = createInstalledRuntimeGate({}, () => undefined)

  // Same public decision seam as main.ts: only a genuinely absent install may fall through.
  const resolve = async () => (await gate.resolve(fixture.root, () =>
    resolveSourceInstallationBackend(fixture.root, ['serve'], { hermesHome: fixture.home }))) ?? 'bootstrap-needed'

  await expect(resolve()).rejects.toMatchObject({
    code: 'installed_runtime_unavailable',
    stage: 'probe-failed'
  })
})

test('a transient non-zero probe is retried once without reinstalling', async () => {
  const fixture = installation(7)
  const marker = path.join(fixture.root, 'probe-ran')
  const scriptMarker = process.platform === 'win32' ? '%~dp0..\\..\\probe-ran' : marker
  fs.writeFileSync(fixture.launcher, process.platform === 'win32'
    ? `@echo off\r\nif exist "${scriptMarker}" (\r\necho second>>"${scriptMarker}"\r\nexit /b 0\r\n)\r\necho first>"${scriptMarker}"\r\nexit /b 7\r\n`
    : `#!/bin/sh\nif [ -e '${marker}' ]; then echo second >> '${marker}'; exit 0; fi\necho first > '${marker}'\nexit 7\n`, { mode: 0o755 })

  const backend = await resolveSourceInstallationBackend(fixture.root, ['serve'], { hermesHome: fixture.home })
  expect(backend?.command).toBe(process.platform === 'win32' ? `"${fixture.launcher}"` : fixture.launcher)
  expect(backend?.bootstrap).toBe(false)
  expect(fs.readFileSync(marker, 'utf8').trim().split(/\r?\n/)).toEqual(['first', 'second'])
})

test('an existing checkout with a missing launcher cannot masquerade as a fresh install', async () => {
  const fixture = installation(0)
  fs.unlinkSync(fixture.launcher)
  await expect(resolveSourceInstallationBackend(fixture.root, ['serve'], { hermesHome: fixture.home }))
    .rejects.toMatchObject({ code: 'installed_runtime_unavailable', stage: 'launcher-missing' })
})

test('only explicit repair may hand an unavailable installation to the installer', async () => {
  const fixture = installation(7)
  await expect(resolveSourceInstallationBackend(fixture.root, ['serve'], {
    hermesHome: fixture.home, repairRequested: true
  })).resolves.toBeNull()
  fs.unlinkSync(fixture.launcher)
  await expect(resolveSourceInstallationBackend(fixture.root, ['serve'], {
    hermesHome: fixture.home, repairRequested: true
  })).resolves.toBeNull()
})

test('genuinely absent Hermes still offers first-run setup', async () => {
  const fixture = installation(0)
  fs.unlinkSync(path.join(fixture.root, 'hermes_cli', 'main.py'))
  await expect(resolveSourceInstallationBackend(fixture.root, [], { hermesHome: fixture.home })).resolves.toBeNull()
})

test('retry after a permanent failure can discover the repaired launcher without a latched failure', async () => {
  const fixture = installation(7)
  await expect(resolveSourceInstallationBackend(fixture.root, [], { hermesHome: fixture.home }))
    .rejects.toMatchObject({ code: 'installed_runtime_unavailable' })
  fs.writeFileSync(fixture.launcher, process.platform === 'win32' ? '@echo off\r\nexit /b 0\r\n' : '#!/bin/sh\nexit 0\n', { mode: 0o755 })
  const backend = await resolveSourceInstallationBackend(fixture.root, [], { hermesHome: fixture.home })
  expect(backend?.bootstrap).toBe(false)
  expect(backend?.root).toBe(fixture.root)
})

test('the actual Windows repair handoff still selects repair after an installed-runtime failure', async () => {
  const fixture = installation(7)
  const main = fs.readFileSync(path.join(import.meta.dirname, 'main.ts'), 'utf8')

  const extract = (name: string) => {
    const match = new RegExp(`^async function ${name}\\([^]*?^\\}`, 'm').exec(main)
    expect(match, `missing real main.ts caller ${name}`).not.toBeNull()

    return transformSync(match![0], { loader: 'ts', target: 'node20' }).code
  }

  const spawns: string[][] = []

  const context = {
    ...sourceBackend, HERMES_HOME: fixture.home,
    IS_WINDOWS: true, IS_PACKAGED: true, installShape: () => 'bootstrap',
    resolveUpdaterBinary: () => 'fixture-updater-not-executed',
    updateHandoffConflict: async () => null, resolveUpdateRoot: () => fixture.root,
    readDesktopUpdateConfig: () => ({ branch: 'main' }), DEFAULT_UPDATE_BRANCH: 'main',
    chooseUpdaterArgs, stopBackendsForUpdate: async () => undefined,
    // Only OS effects are substituted. Probe, main.ts callers and argument chooser are real.
    spawnUpdaterProcess: (_command: string, args: string[]) => { spawns.push(args);

 return { pid: 1234 } },
    stagedUpdaterSupportsPrewrittenMarker: () => false, rememberLog: () => undefined,
    observeUpdaterHandoff: async () => ({ ok: true }), UPDATE_HANDOFF_DWELL_MS: 1,
    setTimeout: () => undefined, app: { quit: () => undefined }, process: { env: {} }
  }

  const code = extract('isSourceRuntimeUsable') + extract('handOffWindowsBootstrapRecovery') +
    '\nhandOffWindowsBootstrapRecovery("explicit-fixture-repair")'

  await expect(vm.runInNewContext(code, context)).resolves.toBe(true)
  expect(spawns).toEqual([['--repair', '--branch', 'main']])
})
