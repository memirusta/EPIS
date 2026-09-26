import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { spawnSync } from 'node:child_process'


if (process.platform !== 'win32') {
  throw new Error(
    'EPIS WhatsApp bundled runtime preparation currently requires Windows',
  )
}

const major = Number(
  process.versions.node.split('.')[0],
)

if (
  !Number.isInteger(major)
  || major < 20
) {
  throw new Error(
    `Node.js 20+ is required; found ${process.versions.node}`,
  )
}


const here = path.dirname(
  fileURLToPath(import.meta.url),
)

const desktopRoot = path.resolve(
  here,
  '..',
)

const bridgeDir = path.resolve(
  desktopRoot,
  '..',
  'whatsapp-bridge',
)

const lockFile = path.join(
  bridgeDir,
  'package-lock.json',
)

if (!fs.existsSync(lockFile)) {
  throw new Error(
    `WhatsApp bridge package-lock.json missing: ${lockFile}`,
  )
}


const npmCli = String(
  process.env.npm_execpath
  || '',
).trim()

if (
  !npmCli
  || !fs.existsSync(npmCli)
) {
  throw new Error(
    'npm CLI path is unavailable; run this through npm',
  )
}


console.log(
  '[EPIS] Preparing WhatsApp bridge dependencies...',
)

const install = spawnSync(
  process.execPath,
  [
    npmCli,
    'ci',
    '--omit=dev',
    '--no-audit',
    '--no-fund',
  ],
  {
    cwd: bridgeDir,
    stdio: 'inherit',
    env: {
      ...process.env,
      NODE_ENV: 'production',
    },
  },
)

if (
  install.error
  || install.status !== 0
) {
  throw (
    install.error
    || new Error(
      `WhatsApp bridge npm ci failed with exit code ${install.status}`,
    )
  )
}


const generatedDir = path.join(
  desktopRoot,
  'src-tauri',
  'generated',
  'whatsapp-node',
)

fs.mkdirSync(
  generatedDir,
  {
    recursive: true,
  },
)

const runtimePath = path.join(
  generatedDir,
  'node.exe',
)

fs.copyFileSync(
  process.execPath,
  runtimePath,
)

console.log(
  `[EPIS] Bundled Node runtime: ${runtimePath}`,
)
