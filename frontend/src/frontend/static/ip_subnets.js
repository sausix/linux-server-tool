"use strict";
// Compare addresses numerically, including compressed IPv6 and IPv4 tails.
function parseSubnetAddress(value) {
  const [literal, prefix, ...extra] = String(value).split('/');
  if (extra.length) return null;
  let text = literal.split('%')[0];
  let bits, number;
  if (!text.includes(':')) {
    const parts = text.split('.');
    if (parts.length !== 4 || parts.some(part => !/^\d{1,3}$/.test(part) || Number(part) > 255)) return null;
    bits = 32;
    number = parts.reduce((result, part) => (result << 8n) | BigInt(part), 0n);
  } else {
    if (text.includes('.')) {
      const tail = text.slice(text.lastIndexOf(':') + 1);
      const parsed = parseSubnetAddress(tail);
      if (!parsed || parsed.bits !== 32) return null;
      text = text.slice(0, text.lastIndexOf(':') + 1) + (parsed.number >> 16n).toString(16) + ':' + (parsed.number & 65535n).toString(16);
    }
    const halves = text.split('::');
    if (halves.length > 2) return null;
    const left = halves[0] ? halves[0].split(':') : [];
    const right = halves.length === 2 && halves[1] ? halves[1].split(':') : [];
    const missing = 8 - left.length - right.length;
    if (halves.length === 1 ? missing !== 0 : missing < 1) return null;
    const parts = [...left, ...Array(missing).fill('0'), ...right];
    if (parts.some(part => !/^[a-f\d]{1,4}$/i.test(part))) return null;
    bits = 128;
    number = parts.reduce((result, part) => (result << 16n) | BigInt('0x' + part), 0n);
  }
  if (prefix !== undefined && (!/^\d+$/.test(prefix) || Number(prefix) > bits)) return null;
  return {number, bits, prefix: prefix === undefined ? bits : Number(prefix)};
}
function addressInSubnets(value, subnets) {
  const address = parseSubnetAddress(value);
  if (!address) return false;
  return subnets.some(value => {
    const subnet = parseSubnetAddress(value);
    if (!subnet || address.bits !== subnet.bits || address.prefix < subnet.prefix) return false;
    const shift = BigInt(subnet.bits - subnet.prefix);
    return (address.number >> shift) === (subnet.number >> shift);
  });
}
