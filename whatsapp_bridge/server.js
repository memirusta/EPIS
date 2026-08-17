/**
 * EPIS WhatsApp Bridge
 *
 * WHATSAPP_MODE=epis_account (onerilen):
 *   Bridge EPIS'in ayri SIM/hesabina baglanir.
 *   MY_WHATSAPP_NUMBER = kullanıcının kisisel numarasi (push hedefi + gelen filtre).
 *
 * WHATSAPP_MODE=self_chat (eski):
 *   Bridge kullanıcının kendi hesabina baglanir, kendine yazilir.
 */

const { Client, LocalAuth } = require('whatsapp-web.js');
const express               = require('express');
const qrcode                = require('qrcode-terminal');

const MY_NUMBER   = process.env.MY_WHATSAPP_NUMBER;
const WA_MODE     = (process.env.WHATSAPP_MODE || 'epis_account').toLowerCase();
const WEBHOOK_URL = process.env.PYTHON_WEBHOOK_URL || 'http://localhost:8000/whatsapp/incoming';
const WEBHOOK_SECRET = process.env.WEBHOOK_SHARED_SECRET || '';
const PORT        = parseInt(process.env.BRIDGE_PORT || '3001', 10);

const client = new Client({
  authStrategy: new LocalAuth({ dataPath: '.wwebjs_auth' }),
  puppeteer: {
    headless: true,
    args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage', '--disable-gpu'],
  },
});

let qrCodeData  = null;
let clientReady = false;
const botSentIds = new Set();
const inFlight   = new Set();
let replyLock    = false;

function digitsOnly(id) {
  if (!id) return '';
  return String(id).replace(/@c\.us|@lid/g, '').replace(/\D/g, '');
}

function isAllowed(msg) {
  if (!MY_NUMBER) return true;

  if (WA_MODE === 'epis_account') {
    if (msg.fromMe) return false;
    return digitsOnly(msg.from) === digitsOnly(MY_NUMBER);
  }

  // self_chat
  if (msg.fromMe) return true;
  return digitsOnly(msg.from) === digitsOnly(MY_NUMBER);
}

async function handleIncoming(msg) {
  if (msg.isGroupMsg) return;
  const body = (msg.body || '').trim();
  if (!body) return;

  const msgId = msg.id?._serialized;
  if (!msgId || inFlight.has(msgId)) return;
  if (botSentIds.has(msgId)) return;
  if (msg.fromMe && replyLock) return;
  if (!isAllowed(msg)) {
    console.log(`[SKIP] mode=${WA_MODE} from=${msg.from} fromMe=${msg.fromMe}`);
    return;
  }

  inFlight.add(msgId);
  const tag = WA_MODE === 'epis_account' ? 'USER' : (msg.fromMe ? 'SELF' : digitsOnly(msg.from));
  console.log(`[IN/${tag}] ${body.substring(0, 80)}`);

  try {
    const headers = { 'Content-Type': 'application/json' };
    if (WEBHOOK_SECRET) {
      headers['Authorization'] = `Bearer ${WEBHOOK_SECRET}`;
    }
    const res = await fetch(WEBHOOK_URL, {
      method:  'POST',
      headers,
      body:    JSON.stringify({
        from: msg.from,
        body,
        timestamp: msg.timestamp,
        from_me: msg.fromMe,
      }),
    });

    if (!res.ok) {
      console.error(`Webhook HTTP ${res.status}: ${await res.text()}`);
      return;
    }

    const reply = (await res.json())?.reply;
    if (!reply) {
      console.log('[OUT] (bos yanit)');
      return;
    }

    replyLock = true;
    const chat = await msg.getChat();
    const sent = await chat.sendMessage(reply);
    if (sent?.id?._serialized) botSentIds.add(sent.id._serialized);
    console.log(`[OUT] ${reply.substring(0, 80)}`);
  } catch (err) {
    console.error('Islem hatasi:', err.message);
  } finally {
    replyLock = false;
    inFlight.delete(msgId);
  }
}

client.on('qr', (qr) => {
  qrCodeData = qr;
  clientReady = false;
  console.log('\n=== WhatsApp QR (terminal) ===');
  if (WA_MODE === 'epis_account') {
    console.log('EPIS hesabinin telefonundan QR tara (kullanıcı degil!).\n');
  }
  qrcode.generate(qr, { small: true });
  console.log(`veya http://localhost:${PORT}/qr\n`);
});

client.on('authenticated', () => {
  console.log('WhatsApp oturumu dogrulandi, senkronize ediliyor...');
});

client.on('ready', () => {
  clientReady = true;
  qrCodeData  = null;
  const episNum = client.info?.wid?.user;
  console.log(`WhatsApp bridge HAZIR.`);
  console.log(`  EPIS hesap numarasi : ${episNum}`);
  console.log(`  Mod                 : ${WA_MODE}`);
  console.log(`  kullanıcı (hedef/filtre) : ${MY_NUMBER || '(yok)'}`);
  if (WA_MODE === 'epis_account' && episNum && MY_NUMBER && digitsOnly(episNum) === digitsOnly(MY_NUMBER)) {
    console.warn('  UYARI: EPIS ve kullanıcı ayni numara! Ayri SIM ile QR tara veya WHATSAPP_MODE=self_chat yap.');
  }
});

client.on('auth_failure', (msg) => {
  clientReady = false;
  console.error('WhatsApp auth hatasi:', msg);
});

client.on('disconnected', (reason) => {
  clientReady = false;
  console.error('WhatsApp baglantisi kesildi:', reason);
});

client.on('message_create', (msg) => {
  handleIncoming(msg).catch((e) => console.error(e));
});

const app = express();
app.use(express.json());

app.post('/send', async (req, res) => {
  const { to, message } = req.body;
  if (!to || !message) return res.status(400).json({ error: '"to" ve "message" zorunlu.' });
  if (!clientReady) return res.status(503).json({ error: 'WhatsApp henuz hazir degil (QR tara).' });
  try {
    const chatId = to.includes('@') ? to : `${to}@c.us`;
    const sent   = await client.sendMessage(chatId, message);
    if (sent?.id?._serialized) botSentIds.add(sent.id._serialized);
    console.log(`[PUSH] -> ${to}: ${message.substring(0, 80)}`);
    res.json({ status: 'sent' });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.get('/status', (_req, res) => {
  res.json({
    ready:  clientReady,
    number: client.info?.wid?.user || null,
    mode:   WA_MODE,
    user:   MY_NUMBER || null,
  });
});

app.get('/qr', (_req, res) => {
  if (clientReady) return res.json({ status: 'already_authenticated' });
  if (!qrCodeData)  return res.json({ status: 'waiting_for_qr' });
  res.json({ qr: qrCodeData });
});

app.listen(PORT, '127.0.0.1', () => {
  console.log(`EPIS WhatsApp Bridge -- http://127.0.0.1:${PORT} (sadece localhost)`);
  console.log(`  Webhook : ${WEBHOOK_URL}`);
  console.log(`  Mod     : ${WA_MODE}`);
  console.log(`  kullanıcı no : ${MY_NUMBER || '(yok)'}`);
});

client.initialize();