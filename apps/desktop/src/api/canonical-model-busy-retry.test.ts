import { expect, test } from 'vitest'

import { CanonicalDesktopProtocol } from './canonical-protocol'

const pick = (profile = 'default') => ({ session_id: 's', profile, key: 'model', value: 'next --provider custom' })

const info = (protocol: CanonicalDesktopProtocol, revision: number, generation: number, profile = 'default') =>
  protocol.event({ type: 'session.info', session_id: 's', profile, payload: { revision, execution_generation: generation } })

test.each(['default', 'other'])('typed model busy retires refused intent and uses latest owner revision (%s)', profile => {
  const p = new CanonicalDesktopProtocol()
  info(p, 10, 2, profile)
  const refused = p.prepare('config.set', pick(profile))
  p.failure(refused, { data: { reason: 'session_busy' } })
  info(p, 11, 3, profile)
  const retry = p.prepare('config.set', pick(profile))
  expect(retry.expected_revision).toBe(11)
  expect(retry.expected_generation).toBe(3)
  expect(retry.request_id).not.toBe(refused.request_id)
})

test('ambiguous model transport failure preserves exact request even after a newer event', () => {
  const p = new CanonicalDesktopProtocol()
  info(p, 10, 2)
  const first = p.prepare('config.set', pick())
  p.failure(first, new Error('connection closed'))
  info(p, 11, 3)
  expect(p.prepare('config.set', pick())).toEqual(first)
})

test('busy model refusal does not retire a sibling owner request', () => {
  const p = new CanonicalDesktopProtocol()
  info(p, 10, 2)
  info(p, 20, 4, 'other')
  const other = p.prepare('config.set', pick('other'))
  const first = p.prepare('config.set', pick())
  p.failure(first, { data: { reason: 'session_busy' } })
  expect(p.prepare('config.set', pick('other'))).toEqual(other)
})

test('busy confirmed model request cannot silently reuse a consumed confirmation token', () => {
  const p = new CanonicalDesktopProtocol()
  info(p, 10, 2)
  const first = p.prepare('config.set', pick())
  p.result('config.set', first, { session_id: 's', operation: 'model', status: 'confirmation_required', confirm: 'fixture-token', confirm_message: 'Confirm target' })
  const confirmed = p.prepare('config.set', { ...pick(), confirm_expensive_model: true })
  expect(confirmed.payload).toMatchObject({ confirm: 'fixture-token' })
  p.failure(confirmed, { data: { reason: 'session_busy' } })
  info(p, 11, 3)
  expect(p.prepare('config.set', pick()).payload).toEqual({ model: 'next', provider: 'custom' })
})
