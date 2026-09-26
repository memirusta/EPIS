import crypto from 'node:crypto'
import fs from 'node:fs'
import http from 'node:http'
import os from 'node:os'
import path from 'node:path'

import { Boom } from '@hapi/boom'
import makeWASocket, {
  DisconnectReason,
  useMultiFileAuthState,
} from '@whiskeysockets/baileys'
import pino from 'pino'
import qrcode from 'qrcode-terminal'
import { evaluateOutboundDisclosure } from './policy.mjs'


const LOCALAPPDATA =
  process.env.LOCALAPPDATA
  || path.join(os.homedir(), 'AppData', 'Local')

const DEFAULT_CONFIG = path.join(
  LOCALAPPDATA,
  'EPIS',
  'whatsapp-bridge.env',
)


function loadPrivateConfig() {
  const file =
    process.env.EPIS_WHATSAPP_BRIDGE_CONFIG
    || DEFAULT_CONFIG

  let text = ''

  try {
    text = fs.readFileSync(
      file,
      'utf8',
    )
  } catch {
    return
  }

  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim()

    if (
      !line
      || line.startsWith('#')
      || !line.includes('=')
    ) {
      continue
    }

    const splitAt = line.indexOf('=')
    const key = line
      .slice(0, splitAt)
      .trim()

    const value = line
      .slice(splitAt + 1)
      .trim()

    if (
      key
      && !(key in process.env)
    ) {
      process.env[key] = value
    }
  }
}


loadPrivateConfig()


const HOST = '127.0.0.1'

const PORT = Number(
  process.env.EPIS_WHATSAPP_BRIDGE_PORT
  || '8766',
)

const BRIDGE_TOKEN = String(
  process.env.EPIS_WHATSAPP_BRIDGE_TOKEN
  || '',
).trim()

const AUTH_DIR = String(
  process.env.EPIS_WHATSAPP_AUTH_DIR
  || path.join(
    LOCALAPPDATA,
    'EPIS',
    'whatsapp-baileys-auth',
  ),
)

const CONTACTS_FILE = String(
  process.env.EPIS_WHATSAPP_CONTACTS_FILE
  || path.join(
    LOCALAPPDATA,
    'EPIS',
    'whatsapp-contacts.json',
  ),
)

const SERVER_HTTP_URL = String(
  process.env.EPIS_SERVER_HTTP_URL
  || '',
)
  .trim()
  .replace(/\/+$/, '')

const INTERNAL_EVENT_TOKEN = String(
  process.env.EPIS_INTERNAL_EVENT_TOKEN
  || '',
).trim()


if (
  !Number.isInteger(PORT)
  || PORT < 1024
  || PORT > 65535
) {
  throw new Error(
    'Invalid EPIS_WHATSAPP_BRIDGE_PORT',
  )
}

if (BRIDGE_TOKEN.length < 32) {
  throw new Error(
    'EPIS_WHATSAPP_BRIDGE_TOKEN missing or too short',
  )
}

if (
  SERVER_HTTP_URL
  && !SERVER_HTTP_URL.startsWith('https://')
) {
  throw new Error(
    'EPIS_SERVER_HTTP_URL must use https',
  )
}

if (
  SERVER_HTTP_URL
  && INTERNAL_EVENT_TOKEN.length < 32
) {
  throw new Error(
    'EPIS_INTERNAL_EVENT_TOKEN missing or too short',
  )
}


fs.mkdirSync(
  AUTH_DIR,
  {
    recursive: true,
  },
)

fs.mkdirSync(
  path.dirname(CONTACTS_FILE),
  {
    recursive: true,
  },
)

if (
  !fs.existsSync(
    CONTACTS_FILE,
  )
) {
  fs.writeFileSync(
    CONTACTS_FILE,
    JSON.stringify(
      {
        contacts: {},
      },
      null,
      2,
    ),
    'utf8',
  )
}


const logger = pino({
  level: 'silent',
})

let sock = null
let connected = false
let reconnectTimer = null


function safeEqual(left, right) {
  const a = Buffer.from(
    String(left || ''),
  )

  const b = Buffer.from(
    String(right || ''),
  )

  return (
    a.length === b.length
    && crypto.timingSafeEqual(a, b)
  )
}


function authorized(req) {
  const supplied = String(
    req.headers.authorization
    || '',
  )

  return safeEqual(
    supplied,
    `Bearer ${BRIDGE_TOKEN}`,
  )
}


function sendJson(
  res,
  status,
  payload,
) {
  const body = Buffer.from(
    JSON.stringify(payload),
    'utf8',
  )

  res.writeHead(
    status,
    {
      'content-type':
        'application/json; charset=utf-8',

      'content-length':
        String(body.length),

      'cache-control':
        'no-store',
    },
  )

  res.end(body)
}


async function readJson(
  req,
  maxBytes = 16 * 1024,
) {
  const chunks = []
  let total = 0

  for await (const chunk of req) {
    total += chunk.length

    if (total > maxBytes) {
      throw new Error(
        'request_too_large',
      )
    }

    chunks.push(chunk)
  }

  const raw = Buffer
    .concat(chunks)
    .toString('utf8')

  const value = JSON.parse(
    raw || '{}',
  )

  if (
    !value
    || typeof value !== 'object'
    || Array.isArray(value)
  ) {
    throw new Error(
      'invalid_json_object',
    )
  }

  return value
}


function loadContacts() {
  const parsed = JSON.parse(
    fs.readFileSync(
      CONTACTS_FILE,
      'utf8',
    ),
  )

  const contacts =
    parsed?.contacts
    && typeof parsed.contacts === 'object'
    && !Array.isArray(parsed.contacts)
      ? parsed.contacts
      : {}

  return contacts
}


function saveContacts(
  contacts,
) {
  const temp = (
    CONTACTS_FILE
    + '.tmp'
  )

  fs.writeFileSync(
    temp,
    JSON.stringify(
      {
        contacts,
      },
      null,
      2,
    ),
    'utf8',
  )

  fs.renameSync(
    temp,
    CONTACTS_FILE,
  )
}


function jidForEntry(
  entry,
) {
  return (
    typeof entry === 'string'
      ? entry
      : entry?.jid
  )
}


function validContactRef(value) {
  return (
    typeof value === 'string'
    && /^[A-Za-z0-9._:-]{1,160}$/
      .test(value)
  )
}


function resolveContactJid(
  contactRef,
) {
  if (
    !validContactRef(contactRef)
  ) {
    return null
  }

  const contacts = loadContacts()
  const entry = contacts[contactRef]

  const jid =
    typeof entry === 'string'
      ? entry
      : entry?.jid

  if (
    typeof jid !== 'string'
    || !/^\d{5,20}@s\.whatsapp\.net$/
      .test(jid)
  ) {
    return null
  }

  return jid
}


function reverseContactRef(
  jid,
) {
  const contacts = loadContacts()
  const matches = []

  for (
    const [
      contactRef,
      entry,
    ]
    of Object.entries(contacts)
  ) {
    const candidate =
      typeof entry === 'string'
        ? entry
        : entry?.jid

    if (
      candidate === jid
      && validContactRef(
        contactRef,
      )
    ) {
      matches.push(
        contactRef,
      )
    }
  }

  return (
    matches.length === 1
      ? matches[0]
      : null
  )
}


function unwrapMessage(
  message,
) {
  let current = message

  for (let i = 0; i < 4; i += 1) {
    if (
      current?.ephemeralMessage
        ?.message
    ) {
      current =
        current.ephemeralMessage
          .message
      continue
    }

    if (
      current?.viewOnceMessage
        ?.message
    ) {
      current =
        current.viewOnceMessage
          .message
      continue
    }

    if (
      current?.viewOnceMessageV2
        ?.message
    ) {
      current =
        current.viewOnceMessageV2
          .message
      continue
    }

    break
  }

  return current || {}
}


function textFromMessage(
  message,
) {
  const value = unwrapMessage(
    message,
  )

  const text =
    value.conversation
    || value.extendedTextMessage
      ?.text
    || value.imageMessage
      ?.caption
    || value.videoMessage
      ?.caption
    || ''

  return (
    typeof text === 'string'
      ? text.trim()
      : ''
  )
}


function contextInfoFromMessage(
  message,
) {
  const value = unwrapMessage(
    message,
  )

  return (
    value.extendedTextMessage
      ?.contextInfo
    || value.imageMessage
      ?.contextInfo
    || value.videoMessage
      ?.contextInfo
    || null
  )
}


async function postOutreachReply(
  message,
) {
  if (
    !SERVER_HTTP_URL
    || !INTERNAL_EVENT_TOKEN
    || !message
    || message.key?.fromMe
  ) {
    return
  }

  const content = textFromMessage(
    message.message,
  )

  if (
    !content
    || content.length > 12000
  ) {
    return
  }

  const incomingMessageRef =
    String(
      message.key?.id
      || '',
    ).trim()

  if (!incomingMessageRef) {
    return
  }

  const contextInfo =
    contextInfoFromMessage(
      message.message,
    )

  const quotedRef = String(
    contextInfo?.stanzaId
    || '',
  ).trim()

  const candidateJids = [
    message.key?.remoteJidAlt,
    message.key?.remoteJid,
  ].filter(
    (
      value,
      index,
      values,
    ) => (
      typeof value === 'string'
      && value
      && values.indexOf(value)
        === index
    ),
  )

  let contactRef = null

  for (
    const jid of candidateJids
  ) {
    contactRef =
      reverseContactRef(
        jid,
      )

    if (contactRef) {
      break
    }
  }

  if (!contactRef) {
    return
  }

  const payload = {
    incoming_message_ref:
      incomingMessageRef,

    provider_contact_ref:
      contactRef,

    content,

    confidence: 0.65,
  }

  if (quotedRef) {
    payload.provider_message_ref =
      quotedRef
  }

  const delays = [
    0,
    1000,
    2000,
    4000,
  ]

  for (
    let attempt = 0;
    attempt < delays.length;
    attempt += 1
  ) {
    if (delays[attempt]) {
      await new Promise(
        resolve => setTimeout(
          resolve,
          delays[attempt],
        ),
      )
    }

    let response

    try {
      response = await fetch(
        (
          SERVER_HTTP_URL
          + '/internal/whatsapp/outreach-reply'
        ),
        {
          method: 'POST',
          headers: {
            authorization:
              `Bearer ${INTERNAL_EVENT_TOKEN}`,
            'content-type':
              'application/json',
          },
          body:
            JSON.stringify(
              payload,
            ),
        },
      )
    } catch {
      continue
    }

    let result = {}

    try {
      result =
        await response.json()
    } catch {
      result = {}
    }

    if (!response.ok) {
      if (
        response.status >= 500
        && attempt
        < delays.length - 1
      ) {
        continue
      }

      return
    }

    const status = String(
      result?.status
      || '',
    )

    if (
      (
        status === 'unmatched'
        || status === 'not_pending'
      )
      && attempt
      < delays.length - 1
    ) {
      continue
    }

    return
  }
}

async function connectWhatsApp() {
  const {
    state,
    saveCreds,
  } = await useMultiFileAuthState(
    AUTH_DIR,
  )

  const current = makeWASocket({
    auth: state,
    logger,
    markOnlineOnConnect: false,
  })

  sock = current

  current.ev.on(
    'creds.update',
    saveCreds,
  )

  current.ev.on(
    'messages.upsert',
    async ({
      type,
      messages,
    }) => {
      if (type !== 'notify') {
        return
      }

      for (
        const message
        of messages || []
      ) {
        await postOutreachReply(
          message,
        )
      }
    },
  )

  current.ev.on(
    'connection.update',
    ({
      connection,
      lastDisconnect,
      qr,
    }) => {
      if (qr) {
        console.log(
          '\n[EPIS-WA] WhatsApp > Linked devices > Link a device\n',
        )

        qrcode.generate(
          qr,
          {
            small: true,
          },
        )
      }

      if (connection === 'open') {
        connected = true

        console.log(
          '[EPIS-WA] WhatsApp connected',
        )
      }

      if (connection === 'close') {
        connected = false

        let statusCode = null

        try {
          if (
            lastDisconnect?.error
          ) {
            statusCode = (
              new Boom(
                lastDisconnect.error,
              )
            ).output.statusCode
          }
        } catch {
          statusCode = null
        }

        if (
          statusCode
          === DisconnectReason.loggedOut
        ) {
          console.error(
            '[EPIS-WA] Logged out; QR pairing required again',
          )
          return
        }

        if (reconnectTimer) {
          clearTimeout(
            reconnectTimer,
          )
        }

        reconnectTimer = setTimeout(
          () => {
            reconnectTimer = null

            connectWhatsApp()
              .catch(() => {
                console.error(
                  '[EPIS-WA] reconnect failed',
                )
              })
          },
          1500,
        )
      }
    },
  )
}


const server = http.createServer(
  async (
    req,
    res,
  ) => {
    if (!authorized(req)) {
      sendJson(
        res,
        401,
        {
          ok: false,
          error: 'unauthorized',
        },
      )
      return
    }

    if (
      req.method === 'GET'
      && req.url === '/health'
    ) {
      sendJson(
        res,
        200,
        {
          ok: true,
          whatsapp_connected:
            connected,
        },
      )
      return
    }

    if (
      req.method === 'POST'
      && req.url === '/contacts/enroll'
    ) {
      if (
        !connected
        || !sock
      ) {
        sendJson(
          res,
          503,
          {
            ok: false,
            error:
              'whatsapp_not_connected',
          },
        )
        return
      }

      let body

      try {
        body = await readJson(
          req,
          8 * 1024,
        )
      } catch {
        sendJson(
          res,
          400,
          {
            ok: false,
            error:
              'invalid_request',
          },
        )
        return
      }

      const contactRef =
        body.contact_ref

      const rawPhone =
        typeof body.phone_number
          === 'string'
          ? body.phone_number
          : ''

      const phoneDigits =
        rawPhone.replace(
          /\D/g,
          '',
        )

      // Full international number is required.
      // No implicit country-code guessing.
      if (
        !validContactRef(
          contactRef,
        )
        || !/^[1-9]\d{6,14}$/
          .test(phoneDigits)
      ) {
        sendJson(
          res,
          400,
          {
            ok: false,
            error:
              'invalid_contact_enrollment',
          },
        )
        return
      }

      let results

      try {
        results = await sock.onWhatsApp(
          phoneDigits,
        )
      } catch {
        sendJson(
          res,
          502,
          {
            ok: false,
            error:
              'whatsapp_lookup_failed',
          },
        )
        return
      }

      const verified = (
        Array.isArray(results)
          ? results.find(
              (
                item,
              ) => (
                item
                && item.exists === true
                && typeof item.jid
                  === 'string'
              ),
            )
          : null
      )

      const jid = String(
        verified?.jid
        || '',
      ).trim()

      if (
        !/^\d{5,20}@s\.whatsapp\.net$/
          .test(jid)
      ) {
        sendJson(
          res,
          404,
          {
            ok: false,
            error:
              'number_not_on_whatsapp',
          },
        )
        return
      }

      let contacts

      try {
        contacts = loadContacts()
      } catch {
        sendJson(
          res,
          500,
          {
            ok: false,
            error:
              'contact_store_unavailable',
          },
        )
        return
      }

      const existing =
        contacts[contactRef]

      const existingJid =
        jidForEntry(
          existing,
        )

      if (
        existingJid
        && existingJid !== jid
      ) {
        sendJson(
          res,
          409,
          {
            ok: false,
            error:
              'contact_ref_conflict',
          },
        )
        return
      }

      for (
        const [
          otherRef,
          entry,
        ]
        of Object.entries(
          contacts
        )
      ) {
        if (
          otherRef !== contactRef
          && jidForEntry(entry)
            === jid
        ) {
          sendJson(
            res,
            409,
            {
              ok: false,
              error:
                'whatsapp_identity_already_mapped',
            },
          )
          return
        }
      }

      contacts[contactRef] = {
        jid,
        updated_at:
          new Date()
            .toISOString(),
      }

      try {
        saveContacts(
          contacts,
        )
      } catch {
        sendJson(
          res,
          500,
          {
            ok: false,
            error:
              'contact_store_write_failed',
          },
        )
        return
      }

      // Deliberately never return JID or phone.
      sendJson(
        res,
        200,
        {
          ok: true,
          status: 'enrolled',
          contact_ref:
            contactRef,
        },
      )

      return
    }

    if (
      req.method === 'POST'
      && req.url === '/contacts/remove'
    ) {
      let body

      try {
        body = await readJson(
          req,
          4 * 1024,
        )
      } catch {
        sendJson(
          res,
          400,
          {
            ok: false,
            error:
              'invalid_request',
          },
        )
        return
      }

      const contactRef =
        body.contact_ref

      if (
        !validContactRef(
          contactRef,
        )
      ) {
        sendJson(
          res,
          400,
          {
            ok: false,
            error:
              'invalid_contact_ref',
          },
        )
        return
      }

      let contacts

      try {
        contacts = loadContacts()
      } catch {
        sendJson(
          res,
          500,
          {
            ok: false,
            error:
              'contact_store_unavailable',
          },
        )
        return
      }

      const existed = (
        Object.prototype
          .hasOwnProperty.call(
            contacts,
            contactRef,
          )
      )

      if (existed) {
        delete contacts[
          contactRef
        ]

        try {
          saveContacts(
            contacts,
          )
        } catch {
          sendJson(
            res,
            500,
            {
              ok: false,
              error:
                'contact_store_write_failed',
            },
          )
          return
        }
      }

      sendJson(
        res,
        200,
        {
          ok: true,
          status:
            existed
              ? 'removed'
              : 'already_absent',
        },
      )

      return
    }

    if (
      req.method === 'POST'
      && req.url === '/send'
    ) {
      if (
        !connected
        || !sock
      ) {
        sendJson(
          res,
          503,
          {
            ok: false,
            error:
              'whatsapp_not_connected',
          },
        )
        return
      }

      let body

      try {
        body = await readJson(
          req,
        )
      } catch {
        sendJson(
          res,
          400,
          {
            ok: false,
            error:
              'invalid_request',
          },
        )
        return
      }

      const contactRef =
        body.contact_ref

      const message =
        typeof body.message
          === 'string'
          ? body.message.trim()
          : ''

      const outreachId =
        typeof body.outreach_id
          === 'string'
          ? body.outreach_id.trim()
          : ''

      if (
        !validContactRef(
          contactRef,
        )
        || !message
        || message.length > 4000
        || !/^[0-9a-f]{32}$/
          .test(outreachId)
      ) {
        sendJson(
          res,
          400,
          {
            ok: false,
            error:
              'invalid_send_request',
          },
        )
        return
      }

      let jid

      try {
        jid = resolveContactJid(
          contactRef,
        )
      } catch {
        jid = null
      }

      if (!jid) {
        sendJson(
          res,
          404,
          {
            ok: false,
            error:
              'contact_not_configured',
          },
        )
        return
      }

      // EPIS_DISCLOSURE_GUARD_BEGIN
      let disclosureContacts

      try {
        disclosureContacts =
          loadContacts()
      } catch {
        sendJson(
          res,
          500,
          {
            ok: false,
            error:
              'contact_store_unavailable',
          },
        )
        return
      }

      const disclosureEntry =
        disclosureContacts[
          contactRef
        ]

      const lastDisclosedAt =
        (
          disclosureEntry
          && typeof disclosureEntry
            === 'object'
        )
          ? (
              disclosureEntry
                .last_disclosed_at
              || null
            )
          : null

      const disclosurePolicy =
        evaluateOutboundDisclosure(
          message,
          {
            lastDisclosedAt,
          },
        )

      if (!disclosurePolicy.ok) {
        const payload = {
          ok: false,
          error:
            disclosurePolicy.error,
          retryable:
            disclosurePolicy.retryable
            === true,
        }

        if (
          Array.isArray(
            disclosurePolicy.required,
          )
        ) {
          payload.required =
            disclosurePolicy.required
        }

        sendJson(
          res,
          disclosurePolicy.retryable
            ? 409
            : 422,
          payload,
        )

        return
      }
      // EPIS_DISCLOSURE_GUARD_END

      try {
        const sent =
          await sock.sendMessage(
            jid,
            {
              text: message,
            },
          )

        const providerMessageRef =
          String(
            sent?.key?.id
            || '',
          ).trim()


        // EPIS_DISCLOSURE_STATE_BEGIN
        try {
          const now =
            new Date()
              .toISOString()

          const currentEntry =
            disclosureContacts[
              contactRef
            ]

          const updatedEntry =
            (
              currentEntry
              && typeof currentEntry
                === 'object'
            )
              ? {
                  ...currentEntry,
                }
              : {
                  jid,
                }

          updatedEntry.last_outreach_at =
            now

          if (
            disclosurePolicy
              .disclosure_present
          ) {
            updatedEntry.last_disclosed_at =
              now
          }

          disclosureContacts[
            contactRef
          ] = updatedEntry

          saveContacts(
            disclosureContacts,
          )
        } catch {
          // WhatsApp send already happened.
          // Do NOT turn this into a failed send:
          // retrying could duplicate the message.
          console.warn(
            '[EPIS-WA] disclosure state persistence failed',
          )
        }
        // EPIS_DISCLOSURE_STATE_END

        if (!providerMessageRef) {
          sendJson(
            res,
            502,
            {
              ok: false,
              error:
                'provider_message_ref_missing',
            },
          )
          return
        }

        // Never return the JID.
        sendJson(
          res,
          200,
          {
            ok: true,
            status: 'sent',
            provider_message_ref:
              providerMessageRef,
          },
        )
      } catch {
        sendJson(
          res,
          502,
          {
            ok: false,
            error:
              'whatsapp_send_failed',
          },
        )
      }

      return
    }

    sendJson(
      res,
      404,
      {
        ok: false,
        error: 'not_found',
      },
    )
  },
)


server.listen(
  PORT,
  HOST,
  () => {
    console.log(
      `[EPIS-WA] Local bridge listening on http://${HOST}:${PORT}`,
    )
  },
)


connectWhatsApp()
  .catch(() => {
    console.error(
      '[EPIS-WA] Initial WhatsApp connection failed',
    )
  })
