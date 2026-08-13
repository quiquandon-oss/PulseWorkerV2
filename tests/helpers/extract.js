import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const WORKER_JS_PATH = join(__dirname, '..', '..', 'worker.js');

let _cachedSrc = null;
function getSource() {
  if (_cachedSrc === null) _cachedSrc = readFileSync(WORKER_JS_PATH, 'utf8');
  return _cachedSrc;
}

export function extractFunctions(...names) {
  const src = getSource();
  const pieces = [];
  for (const name of names) {
    const startMatch = src.match(new RegExp(`function\\s+${name}\\s*\\(`));
    if (!startMatch) throw new Error(`Could not find "function ${name}(" in worker.js`);
    const startIdx = startMatch.index;
    const braceStart = src.indexOf('{', startIdx);
    let depth = 0, i = braceStart;
    for (; i < src.length; i++) {
      if (src[i] === '{') depth++;
      else if (src[i] === '}') { depth--; if (depth === 0) { i++; break; } }
    }
    pieces.push(src.slice(startIdx, i));
  }
  return pieces.join('\n\n');
}

export function evalInScope(source, extraGlobals = {}) {
  const sandbox = { console, ...extraGlobals };
  const keys = Object.keys(sandbox);
  const names = [...source.matchAll(/^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(/gm)].map(m => m[1]);
  const fn = new Function(...keys, `${source}\nreturn { ${names.join(',')} };`);
  return fn(...keys.map(k => sandbox[k]));
}
