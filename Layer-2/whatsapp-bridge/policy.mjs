export const DISCLOSURE_MAX_AGE_DAYS = 30

function foldText(
  value,
) {
  return String(
    value
    ?? '',
  )
    .normalize('NFKC')
    .toLocaleLowerCase(
      'tr-TR',
    )
    .replace(
      /[’`´']/g,
      '',
    )
    .replace(
      /ç/g,
      'c',
    )
    .replace(
      /ğ/g,
      'g',
    )
    .replace(
      /ı/g,
      'i',
    )
    .replace(
      /ö/g,
      'o',
    )
    .replace(
      /ş/g,
      's',
    )
    .replace(
      /ü/g,
      'u',
    )
    .replace(
      /\s+/g,
      ' ',
    )
    .trim()
}


const HARD_DECEPTION_PATTERNS = [
  // Explicitly impersonating Emir.
  /\bben\s+emirim\b/,
  /\bemir\s+olarak\s+yaziyorum\b/,

  // Explicitly pretending to be a human.
  /\bben\s+(?:gercek\s+bir\s+)?insanim\b/,

  // Explicitly denying AI/bot identity.
  /\bben\s+(?:bir\s+)?bot\s+degilim\b/,
  /\bben\s+(?:bir\s+)?ai\s+degilim\b/,
  /\bben\s+yapay\s+zeka\s+degilim\b/,

  // English equivalents.
  /\bi\s+am\s+emir\b/,
  /\bim\s+emir\b/,
  /\bi\s+am\s+(?:a\s+)?human\b/,
  /\bim\s+(?:a\s+)?human\b/,
  /\bi\s+am\s+not\s+(?:an?\s+)?(?:ai|bot)\b/,
  /\bim\s+not\s+(?:an?\s+)?(?:ai|bot)\b/,
]


const AI_IDENTITY_PATTERNS = [
  /\bepis\b/,
  /\byapay\s+zeka\b/,
  /\bai\b/,
  /\bai\s+asistani?\b/,
  /\basistan(?:im|iyim)?\b/,
  /\bdijital\s+asistan(?:im|iyim)?\b/,
  /\bkisisel\s+asistan(?:im|iyim)?\b/,
  /\bassistant\b/,
  /\bbot\b/,
]


const EMIR_CONTEXT_PATTERNS = [
  // "Emir'in AI/asistanıyım"
  /\bemirin\b/,

  // "Emir'den geliyorum"
  /\bemirden\b/,

  // Natural request/context forms.
  /\bemir\b.{0,90}\b(?:istedi|sormami|yazmami|merak\s+ediyor|adina|icin|beni\s+gonderdi|beni\s+yolladi|asistani|ai)\b/,

  // English.
  /\bemir\b.{0,90}\b(?:asked|wanted|wants|wondering|assistant|ai)\b/,
  /\bon\s+behalf\s+of\s+emir\b/,
]


function matchesAny(
  folded,
  patterns,
) {
  return patterns.some(
    pattern =>
      pattern.test(
        folded,
      ),
  )
}


export function disclosureIsFresh(
  lastDisclosedAt,
  {
    nowMs = Date.now(),
    maxAgeDays =
      DISCLOSURE_MAX_AGE_DAYS,
  } = {},
) {
  if (
    typeof lastDisclosedAt
    !== 'string'
    || !lastDisclosedAt.trim()
  ) {
    return false
  }

  const timestamp =
    Date.parse(
      lastDisclosedAt,
    )

  if (
    !Number.isFinite(
      timestamp,
    )
  ) {
    return false
  }

  const ageMs =
    nowMs
    - timestamp

  // Future timestamps are not trusted.
  if (ageMs < 0) {
    return false
  }

  return (
    ageMs
    <= (
      maxAgeDays
      * 24
      * 60
      * 60
      * 1000
    )
  )
}


export function evaluateOutboundDisclosure(
  message,
  {
    lastDisclosedAt = null,
    nowMs = Date.now(),
    maxAgeDays =
      DISCLOSURE_MAX_AGE_DAYS,
  } = {},
) {
  const clean =
    typeof message === 'string'
      ? message.trim()
      : ''

  if (!clean) {
    return {
      ok: false,
      error:
        'message_required',
      retryable: false,
      disclosure_required:
        false,
      disclosure_present:
        false,
    }
  }

  const folded =
    foldText(
      clean,
    )

  if (
    matchesAny(
      folded,
      HARD_DECEPTION_PATTERNS,
    )
  ) {
    return {
      ok: false,
      error:
        'recipient_identity_deception',
      retryable: false,
      disclosure_required:
        false,
      disclosure_present:
        false,
    }
  }

  const hasAiIdentity =
    matchesAny(
      folded,
      AI_IDENTITY_PATTERNS,
    )

  const hasEmirContext =
    matchesAny(
      folded,
      EMIR_CONTEXT_PATTERNS,
    )

  const disclosurePresent =
    hasAiIdentity
    && hasEmirContext

  const disclosureRequired =
    !disclosureIsFresh(
      lastDisclosedAt,
      {
        nowMs,
        maxAgeDays,
      },
    )

  if (
    disclosureRequired
    && !disclosurePresent
  ) {
    const required = []

    if (!hasAiIdentity) {
      required.push(
        'ai_identity',
      )
    }

    if (!hasEmirContext) {
      required.push(
        'emir_context',
      )
    }

    return {
      ok: false,
      error:
        'recipient_identity_context_missing',
      retryable: true,
      required,
      disclosure_required:
        true,
      disclosure_present:
        false,
    }
  }

  return {
    ok: true,
    disclosure_required:
      disclosureRequired,
    disclosure_present:
      disclosurePresent,
  }
}
