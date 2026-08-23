#!/usr/bin/env node
// Run in CI immediately after the audit writes its report: re-scans the
// actual written file (not the in-memory object) for anything that looks
// like a leaked credential, using the same scanForLeakedSecrets() this
// script's own unit tests already exercise -- one source of truth for
// the pattern list, instead of a second hand-duplicated copy in YAML.
import { readFileSync } from 'node:fs';
import { scanForLeakedSecrets } from './lib.js';

const path = process.argv[2];
if (!path) {
  console.error('Usage: node verify-no-leaks.js <path-to-report.json>');
  process.exit(1);
}

const text = readFileSync(path, 'utf8');
const { clean, matchedPatterns } = scanForLeakedSecrets(text);

if (!clean) {
  console.error('Potential secret pattern(s) found in report output:', matchedPatterns.join(', '));
  process.exit(1);
}
console.log('No secret patterns detected in', path);
