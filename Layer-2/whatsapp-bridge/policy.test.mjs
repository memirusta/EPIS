import assert from 'node:assert/strict'
import test from 'node:test'

import {
  evaluateOutboundDisclosure,
} from './policy.mjs'


const NOW =
  Date.parse(
    '2026-09-26T12:00:00Z',
  )


test(
  'natural EPIS + Emir disclosure passes',
  () => {
    const result =
      evaluateOutboundDisclosure(
        (
          'Selam Mervee 😭 '
          + 'Ben EPIS, Emir bir şey '
          + 'sormamı istedi.'
        ),
        {
          nowMs: NOW,
        },
      )

    assert.equal(
      result.ok,
      true,
    )

    assert.equal(
      result.disclosure_present,
      true,
    )
  },
)


test(
  'Emir personal AI wording passes without exact canned phrase',
  () => {
    const result =
      evaluateOutboundDisclosure(
        (
          "Selam :) Emir'in kişisel "
          + "AI asistanıyım. Kendisi "
          + "senin fikrini merak ediyor."
        ),
        {
          nowMs: NOW,
        },
      )

    assert.equal(
      result.ok,
      true,
    )
  },
)


test(
  'first outreach without identity context is retryable',
  () => {
    const result =
      evaluateOutboundDisclosure(
        (
          'Selam Merve, sana bir şey '
          + 'soracağım.'
        ),
        {
          nowMs: NOW,
        },
      )

    assert.equal(
      result.ok,
      false,
    )

    assert.equal(
      result.error,
      'recipient_identity_context_missing',
    )

    assert.equal(
      result.retryable,
      true,
    )

    assert.deepEqual(
      result.required.sort(),
      [
        'ai_identity',
        'emir_context',
      ],
    )
  },
)


test(
  'recently disclosed contact can receive normal follow-up',
  () => {
    const result =
      evaluateOutboundDisclosure(
        'Bu arada bir şey daha soracağım 😭',
        {
          nowMs: NOW,
          lastDisclosedAt:
            '2026-09-10T12:00:00Z',
        },
      )

    assert.equal(
      result.ok,
      true,
    )

    assert.equal(
      result.disclosure_required,
      false,
    )
  },
)


test(
  'disclosure expires after thirty days',
  () => {
    const result =
      evaluateOutboundDisclosure(
        'Selam, bir şey soracağım.',
        {
          nowMs: NOW,
          lastDisclosedAt:
            '2026-08-20T12:00:00Z',
        },
      )

    assert.equal(
      result.ok,
      false,
    )

    assert.equal(
      result.error,
      'recipient_identity_context_missing',
    )
  },
)


test(
  'explicit Emir impersonation is always blocked',
  () => {
    const result =
      evaluateOutboundDisclosure(
        "Selam, ben Emir'im.",
        {
          nowMs: NOW,
          lastDisclosedAt:
            '2026-09-25T12:00:00Z',
        },
      )

    assert.equal(
      result.ok,
      false,
    )

    assert.equal(
      result.error,
      'recipient_identity_deception',
    )

    assert.equal(
      result.retryable,
      false,
    )
  },
)


test(
  'explicit AI denial is always blocked',
  () => {
    const result =
      evaluateOutboundDisclosure(
        (
          'Merak etme, ben yapay '
          + 'zeka değilim.'
        ),
        {
          nowMs: NOW,
          lastDisclosedAt:
            '2026-09-25T12:00:00Z',
        },
      )

    assert.equal(
      result.ok,
      false,
    )

    assert.equal(
      result.error,
      'recipient_identity_deception',
    )
  },
)


test(
  'ordinary use of insan is not blacklisted',
  () => {
    const result =
      evaluateOutboundDisclosure(
        (
          'Bence insanlar bu konuda '
          + 'çok farklı düşünüyor.'
        ),
        {
          nowMs: NOW,
          lastDisclosedAt:
            '2026-09-25T12:00:00Z',
        },
      )

    assert.equal(
      result.ok,
      true,
    )
  },
)


test(
  'random Emir and AI words do not fake disclosure relationship',
  () => {
    const result =
      evaluateOutboundDisclosure(
        (
          'AI konusu ilginç. '
          + 'Emir bugün nasıl?'
        ),
        {
          nowMs: NOW,
        },
      )

    assert.equal(
      result.ok,
      false,
    )

    assert.equal(
      result.error,
      'recipient_identity_context_missing',
    )
  },
)
