import assert from 'node:assert/strict';
import test from 'node:test';

import {micE, parse, position, rfHops} from './aprs.js';
import {rows} from './adsb.js';

const near = (a, b) => Math.abs(a - b) < 1e-4;

test('plain and compressed positions', () => {
  const p = position('4903.50N/07201.75W-');
  assert.ok(near(p.lat, 49.058333) && near(p.lon, -72.029167));
  const c = position('/5L!!<*e7>7P[');          // the spec's example: 49.5 N, 72.75 W
  assert.ok(Math.abs(c.lat - 49.5) < 1e-3 && Math.abs(c.lon + 72.75) < 1e-3);
  assert.equal(position('hello'), null);
});

test('Mic-E', () => {
  // the spec's destination S32U6T: 33 25.64 N, no longitude offset, west; 112 07.74 in the body
  const p = micE('S32U6T', String.fromCharCode(112 + 28, 7 + 28, 74 + 28) + 'xyz');
  assert.ok(near(p.lat, 33 + 25.64 / 60));
  assert.ok(near(p.lon, -(112 + 7.74 / 60)));
});

test('packets', () => {
  const p = parse('DK4BP>APBM1D,TCPIP*,qAR,DL3OCK-12:=4833.76N/01117.04E[143/000/A=001553');
  assert.equal(p.name, 'DK4BP');
  assert.ok(near(p.lat, 48 + 33.76 / 60) && near(p.lon, 11 + 17.04 / 60));
  const o = parse('DB0SIF-S>APDG01,TCPIP*,qAC,DB0SIF-GS:;DB0SIF B *131650z5034.50ND00840.15EaRNG0016');
  assert.equal(o.name, 'DB0SIF B');
  assert.ok(near(o.lat, 50 + 34.5 / 60));
  assert.equal(parse('IQ2ZY>APMI06,TCPIP*,qAC,IQ2ARI-AS:T#239,009,006').lat, undefined);
  assert.equal(parse('# aprsc 2.1.19'), null);
});

test('RF hops', () => {
  assert.deepEqual(rfHops('A', ['DIGI1*', 'WIDE2-1', 'qAR', 'IGATE']), ['A', 'DIGI1', 'IGATE']);
  assert.deepEqual(rfHops('A', ['WIDE1*', 'qAR', 'IGATE']), ['A', null, 'IGATE']);
  assert.deepEqual(rfHops('A', ['TCPIP*', 'qAC', 'X']), ['A']);
});

test('aircraft rows leave military ones out', () => {
  const {aircraft, nopos} = rows([
    {hex: 'abc123', lat: 1, lon: 2, alt_baro: 35000, track: 90.4, gs: 450, category: 'a3', flight: 'CPA1  '},
    {hex: 'ae0001', lat: 1, lon: 2, alt_baro: 20000, dbFlags: 1, flight: 'RCH123'},
    {hex: 'def456', lat: 1, lon: 2, alt_baro: 'ground'},
    {hex: 'aaa000'},
  ]);
  assert.deepEqual(aircraft, [['abc123', 1, 2, 35000, 90, 450, 'A3', 0, 'CPA1'], ['def456', 1, 2, -2, -1, 0, '-', 0, 'DEF456']]);
  assert.equal(nopos, 1);
});
